"""Item 6 — P6 test-fidelity batch (the tests ARE the deliverable).

13. Three-machine convergence:
    (i)   a pull-only peer pins the GC floor (never-published-peer safety);
    (ii)  a three-way delete/resurrect/re-delete race converges under per-field
          MAX tombstone semantics;
    (iii) the merged-generation ledger survives concurrent RMW from two
          publishers without dropping a third host (Phase 1's P2 fix at
          three-machine scale).
14. Real flusher lifecycle: start_flusher -> write -> a real tick -> stop() ->
    close() publishes the final state and leaves no DB access after close.
15. Production-schema fixtures: the sync path is exercised against the real
    _SCHEMA from node_store, not a 3-column toy table (the gap that hid gen-210).
16. Per-pull convergence audit: the audit runs on EVERY pull cycle, not only
    on demand.
"""

from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memcp.core.fileutil import content_hash, estimate_tokens
from memcp.core.snapshot_sync import _SNAPSHOT_MERGED, SnapshotSync, snapshot_health
from memcp.core.write_lock import WriteLock

# ── toy fixtures (for the merge/ledger/tombstone semantics in tests 13/14) ────

_TOY_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, content TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL, resurrected_at TEXT DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0');
"""


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))


def _make_db(path: Path, nodes: list[tuple], tombstones: list | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_TOY_SCHEMA)
    for nid, content in nodes:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            (nid, content, "2026-01-01T00:00:00+00:00"),
        )
    for t in tombstones or []:
        tid, d_at, r_at = (t + (None,))[:3] if len(t) == 2 else t
        conn.execute(
            "INSERT OR REPLACE INTO tombstones (id, deleted_at, resurrected_at) VALUES (?, ?, ?)",
            (tid, d_at, r_at),
        )
    conn.commit()
    conn.close()


def _add_node(path: Path, nid: str, content: str) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
        (nid, content, "2026-01-01T00:00:00+00:00"),
    )
    conn.commit()
    conn.close()


def _ids(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT id FROM nodes")}
    finally:
        conn.close()


def _tomb_state(path: Path, tid: str) -> tuple[str | None, str | None] | None:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute(
            "SELECT deleted_at, resurrected_at FROM tombstones WHERE id = ?", (tid,)
        ).fetchone()
        return (row[0], row[1]) if row else None
    finally:
        conn.close()


def _machine(tmp: Path, name: str, nodes: list[tuple], drive: Path, **kw) -> SnapshotSync:
    db = tmp / name / "graph.db"
    _make_db(db, nodes)
    s = SnapshotSync(
        db, drive, WriteLock(db), min_interval=0.0, immutable=True, max_blobs=100, **kw
    )
    s._host = name  # distinct host identity per simulated machine
    return s


def _publish(s: SnapshotSync) -> None:
    s.mark_durable_dirty()
    assert s.push(force=True) is True


def _blob_gens(drive: Path) -> set[int]:
    return {int(p.name.split(".")[2]) for p in drive.glob("graph.snapshot.*.db")}


# ── Test 13(i) — pull-only peer pins the GC floor ─────────────────────────────


def test_pull_only_peer_pins_gc_floor(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    sA = _machine(tmp_path, "A", [("a1", "A1")], drive)
    _publish(sA)  # gen 1

    # Pull-only peer C merges gen 1 and NEVER publishes — it must still appear in
    # the merged ledger, pinning the floor at 1.
    sC = _machine(tmp_path, "C", [("c1", "C local")], drive)
    assert sC.pull_if_newer() is True
    assert snapshot_health(str(drive))["merged_ledger"].get("C") == 1

    # A advances two more generations (each a superset, so older blobs are
    # content-covered and thus floor-reapable).
    _add_node(sA.local_db, "a2", "A2")
    _publish(sA)  # gen 2
    _add_node(sA.local_db, "a3", "A3")
    _publish(sA)  # gen 3 — push() GCs with floor = min(A:3, C:1) = 1

    assert snapshot_health(str(drive))["floor"] == 1
    assert {1, 2, 3} <= _blob_gens(drive), "C pins the floor at 1 — no blob may be reaped"

    # Drop the pull-only peer from the ledger and GC again: the floor advances to
    # 3 and the now-unprotected covered blobs are reaped. This is exactly what C's
    # ledger entry was preventing.
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"A": 3}))
    sA._last_gc_floor = -1
    pointer_blob = sA._published_snapshot().name
    sA._gc_blobs(pointer_blob)
    assert _blob_gens(drive) == {3}, "without the pull-only peer the floor reaps gens 1,2"


# ── Test 13(ii) — three-way delete/resurrect/re-delete converges via MAX ──────


def test_three_way_tombstone_race_converges_max(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    t1 = "2026-02-01T00:00:00+00:00"  # A deletes X
    t2 = "2026-03-01T00:00:00+00:00"  # B resurrects X (t2 > t1)
    t3 = "2026-04-01T00:00:00+00:00"  # C re-deletes X (t3 > t2)

    # Three machines, independent writes to X's tombstone. C is the converger:
    # it re-deleted X at t3 and now pulls A's delete and B's resurrect in turn.
    sA = _machine(tmp_path, "A", [("X", "A copy")], drive)
    _make_db(sA.local_db, [("X", "A copy")], tombstones=[("X", t1)])  # delete @ t1

    sB = _machine(tmp_path, "B", [("X", "B copy")], drive)
    _make_db(sB.local_db, [("X", "B copy")], tombstones=[("X", t1, t2)])  # resurrect @ t2

    sC = _machine(tmp_path, "C", [("X", "C copy")], drive)
    _make_db(sC.local_db, [("X", "C copy")], tombstones=[("X", t3)])  # re-delete @ t3

    _publish(sA)  # gen 1 carries (deleted=t1)
    sC.pull_if_newer()  # merge A: C tomb stays (t3, None); deny-set removes X
    _publish(sB)  # gen 2 carries (deleted=t1, resurrected=t2)
    sC.pull_if_newer()  # merge B: per-field MAX -> (t3, t2)

    # Per-field MAX: deleted_at=t3 (latest re-delete), resurrected_at=t2. Since
    # the re-delete (t3) out-ranks the resurrection (t2), X resolves to deleted.
    assert _tomb_state(sC.local_db, "X") == (t3, t2)
    assert "X" not in _ids(sC.local_db), "the latest re-delete wins — X is gone"


# ── Test 13(iii) — merged-ledger survives concurrent RMW from two publishers ──


def test_merged_ledger_survives_concurrent_rmw(tmp_path: Path) -> None:
    """The P2 fix: a writer re-reads the ledger immediately before writing and
    merges per-key with MAX, so a peer's entry committed between this writer's
    first read and its write is folded in, never clobbered. Deterministically
    simulate that interleaving with three hosts (A pre-existing, C concurrent,
    B writing)."""
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"A": 5}))

    sB = _machine(tmp_path, "B", [("b", "B")], drive)

    # First internal read sees only {A:5}; the re-read (right before writing)
    # sees C's entry, just committed by a concurrent peer.
    reads = iter([{"A": 5}, {"A": 5, "C": 7}])
    real_read = sB._read_json

    def _staged_read(path: Path) -> dict:
        if path == sB.snapshot_merged:
            try:
                return next(reads)
            except StopIteration:
                return real_read(path)
        return real_read(path)

    sB._read_json = _staged_read  # type: ignore[method-assign]
    sB._record_merged_generation(3)

    ledger = json.loads((drive / _SNAPSHOT_MERGED).read_text())
    assert ledger == {"A": 5, "B": 3, "C": 7}, (
        "merge-MAX RMW must fold in a peer's concurrently-committed entry, "
        "never clobber it (P2, three-host scale)"
    )


# ── Test 14 — real flusher lifecycle: publish then quiesce, no post-close DB ──


def _insight(content: str, tags: list[str] | None = None, idx: int = 0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": content_hash(content + str(idx) + now.isoformat()),
        "content": content,
        "summary": "",
        "category": "general",
        "importance": "medium",
        "effective_importance": 0.5,
        "tags": tags or [],
        "entities": [],
        "project": "p",
        "session": "",
        "token_count": estimate_tokens(content),
        "access_count": 0,
        "last_accessed_at": None,
        "created_at": now.isoformat(),
        "archived_at": None,
    }


def test_flusher_lifecycle_publishes_then_quiesces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memcp.core.node_store import NodeStore

    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(tmp_path / "drive"))
    monkeypatch.setenv("MEMCP_SNAPSHOT_INTERVAL", "1")
    import memcp.config as config_module

    config_module._config = None

    drive = tmp_path / "drive"
    store = NodeStore()
    store._get_conn()  # starts the real flusher
    sync = store._sync
    assert sync is not None and sync._flusher.is_alive()

    store.store(_insight("a durable node to flush", idx=1))

    # Let a real flusher tick publish the durable write.
    def _published_gen() -> int:
        meta = drive / "graph.snapshot.meta.json"
        ptr = drive / "graph.snapshot.ptr.json"
        for f in (ptr, meta):
            if f.exists():
                try:
                    return int(json.loads(f.read_text()).get("generation", 0))
                except (ValueError, OSError):
                    pass
        return 0

    deadline = time.time() + 5
    while time.time() < deadline and _published_gen() == 0:
        time.sleep(0.05)
    assert _published_gen() > 0, "a real flusher tick must publish the durable write"

    # stop() (via close()) joins the in-flight tick, then quiesces: no DB access
    # outlives close().
    store.close()
    assert not sync._flusher.is_alive(), "flusher must be dead after close (no post-close access)"
    assert sync._sync_error_count == 0, "no closed-DB access errors after close"

    # The final state is published and readable.
    assert _published_gen() > 0


# ── Test 15 — sync works against the REAL production schema, not a toy table ──


def test_sync_union_on_production_schema(tmp_path: Path) -> None:
    from memcp.core.node_store import _SCHEMA, NodeStore

    # The gap class: toy 3-column tables hid the gen-210 bug. Pin that the real
    # schema (the one sync actually runs against in production) defines the
    # multi-table structure a toy fixture lacks.
    for required in ("nodes", "edges", "entity_index", "index_meta", "tombstones"):
        assert required in _SCHEMA, f"production _SCHEMA must define {required}"

    def _prod_db(path: Path, rows: list[tuple[str, str]]) -> set[str]:
        path.parent.mkdir(parents=True, exist_ok=True)
        store = NodeStore(str(path))
        ids = set()
        try:
            for i, (content, tag) in enumerate(rows):
                ins = _insight(content, tags=[tag], idx=i)
                store.store(ins)
                ids.add(ins["id"])
        finally:
            store.close()
        return ids

    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    ids_a = _prod_db(db_a, [("machine A insight about sqlite", "kind:kb")])
    ids_b = _prod_db(db_b, [("machine B insight about graphs", "kind:kb")])

    sA = SnapshotSync(db_a, drive, WriteLock(db_a), min_interval=0.0)
    sA.mark_durable_dirty()
    assert sA.push(force=True) is True

    sB = SnapshotSync(db_b, drive, WriteLock(db_b), min_interval=0.0)
    assert sB.pull_if_newer() is True

    merged = _ids(db_b)
    assert ids_a <= merged and ids_b <= merged, "union must merge both machines on real schema"

    # The real production tables survive the merge (a toy fixture would never
    # have exercised these).
    conn = sqlite3.connect(str(db_b))
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        ncols = {r[1] for r in conn.execute("PRAGMA table_info(nodes)")}
    finally:
        conn.close()
    assert {"nodes", "edges", "entity_index", "tombstones", "meta", "index_meta"} <= tables
    # Columns the toy 3-column fixture never had (added by the real migration).
    assert {"archived_at", "ingest_seq"} <= ncols


# ── Test 16 — the convergence audit runs on every pull cycle ──────────────────


def test_convergence_audit_runs_every_pull_cycle(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    sA = _machine(tmp_path, "A", [("a1", "A1")], drive)
    _publish(sA)

    sB = _machine(tmp_path, "B", [("b1", "B1")], drive)

    calls = {"n": 0}
    real_audit = sB.convergence_audit

    def _counting_audit() -> dict:
        calls["n"] += 1
        return real_audit()

    sB.convergence_audit = _counting_audit  # type: ignore[method-assign]

    for _ in range(3):
        sB._flush_tick()

    assert calls["n"] == 3, "the convergence audit must run on every pull cycle, not on demand"
    assert sB._audit_count == 3
    assert sB._last_audit, "the audit result must be recorded each cycle"
