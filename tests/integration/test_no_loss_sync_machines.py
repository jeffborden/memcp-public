"""Two-machine no-loss sync integration via the real remember()/pull path.

Each machine keeps its own local DB; they share a Drive snapshot dir. Exercises
the additive union and the fresh-local ingest_seq assignment for merged rows
(§3.1, §3.4) through the public API, not the SnapshotSync internals.

Also covers deferred-pull convergence (Task 4): two machines driven through the
flusher tick cadence (pull_if_newer → push) deterministically, without sleeping
on a real daemon thread.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

import memcp.config as config_module
from memcp.core.snapshot_sync import _SNAPSHOT_PTR, SnapshotSync
from memcp.core.write_lock import WriteLock


def _reset() -> None:
    config_module._config = None


def _use_machine(
    monkeypatch: pytest.MonkeyPatch, data_dir: Path, snapshot_dir: Path | None
) -> None:
    monkeypatch.setenv("MEMCP_DATA_DIR", str(data_dir))
    if snapshot_dir is None:
        monkeypatch.delenv("MEMCP_SNAPSHOT_DIR", raising=False)
    else:
        monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(snapshot_dir))
    _reset()


def _ingest_seqs(data_dir: Path) -> list[int]:
    import sqlite3

    conn = sqlite3.connect(str(data_dir / "graph.db"))
    try:
        return [r[0] for r in conn.execute("SELECT ingest_seq FROM nodes")]
    finally:
        conn.close()


def _contents(data_dir: Path) -> set[str]:
    import sqlite3

    conn = sqlite3.connect(str(data_dir / "graph.db"))
    try:
        return {r[0] for r in conn.execute("SELECT content FROM nodes")}
    finally:
        conn.close()


def test_union_assigns_fresh_local_ingest_seq_to_merged_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    snap = tmp_path / "drive"
    a_dir = tmp_path / "machine_a"
    b_dir = tmp_path / "machine_b"
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))

    from memcp.core.node_store import NodeStore

    # ── Machine B builds an independent local DB FIRST (sync off, no pull) ──
    _use_machine(monkeypatch, b_dir, None)
    NodeStore()._get_conn()  # materialize graph backend
    from memcp.core.memory import remember

    remember("B unique insight one", project="p")
    remember("B unique insight two", project="p")

    # ── Machine A writes + publishes a snapshot ──
    _use_machine(monkeypatch, a_dir, snap)
    NodeStore()._get_conn()
    from memcp.core.memory import remember as remember_a

    remember_a("A unique insight one", project="p")
    remember_a("A unique insight two", project="p")
    # close everything so A's final snapshot is flushed
    NodeStore().close()

    # ── Machine B now turns sync ON → next open unions A's snapshot in ──
    # This test exercises the union + ingest_seq MECHANICS (§3.1/§3.4), so pin the
    # pull to the synchronous-at-open path via MEMCP_SNAPSHOT_PULL_BLOCKING (set
    # BEFORE _use_machine so the config reset picks it up). The deferred/flusher-
    # driven pull (the new default) is covered by
    # test_deferred_pull_two_machines_converge_on_union.
    monkeypatch.setenv("MEMCP_SNAPSHOT_PULL_BLOCKING", "1")
    _use_machine(monkeypatch, b_dir, snap)
    store = NodeStore()
    store._get_conn()  # blocking mode → synchronous pull_if_newer → union + ingest_seq backfill
    store.close()

    # No insight lost: B holds both machines' rows.
    contents = _contents(b_dir)
    assert "A unique insight one" in contents
    assert "A unique insight two" in contents
    assert "B unique insight one" in contents
    assert "B unique insight two" in contents

    # Merged rows got FRESH local ingest_seq — every row's seq is non-null + unique.
    seqs = _ingest_seqs(b_dir)
    assert all(s is not None for s in seqs), "merged rows must be backfilled"
    assert len(seqs) == len(set(seqs)), "ingest_seq must stay unique across the merge"


# ---------------------------------------------------------------------------
# Helpers for deferred-pull convergence tests (Task 4).
# These bypass NodeStore / embeddings and drive SnapshotSync directly so the
# tests are fast and deterministic, while still exercising the exact code paths
# the background flusher uses.
# ---------------------------------------------------------------------------

_SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, content TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL, resurrected_at TEXT DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0');
"""


def _make_sync_db(path: Path, nodes: list[tuple[str, str]]) -> None:
    """Create a minimal MemCP-schema SQLite DB seeded with the given (id, content) rows."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SYNC_SCHEMA)
    for nid, content in nodes:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            (nid, content, "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


def _node_ids(db_path: Path) -> set[str]:
    conn = sqlite3.connect(str(db_path))
    try:
        return {r[0] for r in conn.execute("SELECT id FROM nodes")}
    finally:
        conn.close()


def _sync(db: Path, drive: Path, host: str) -> SnapshotSync:
    """Build a v1 (default, non-immutable) SnapshotSync for `db`/`drive`."""
    s = SnapshotSync(db, drive, WriteLock(db), min_interval=0.0)
    s._host = host
    return s


def _flusher_tick(s: SnapshotSync) -> None:
    """One deterministic flusher tick: pull (if newer) then push (if dirty).

    Mirrors _flush_loop's body exactly so the test exercises the real convergence
    path without spinning a daemon thread or sleeping.
    """
    s.pull_pending = False
    s.pull_if_newer()
    if s._durable_dirty:
        s.push()
    elif s._needs_outbox_repush():
        s.push(force=True)


def _craft_same_gen_orphan(
    drive: Path,
    gen: int,
    host: str,
    nodes: list[tuple[str, str]],
) -> str:
    """Write a self-consistent v2 blob at `gen` for `host` and point the pointer
    at it, leaving the OLD pointer's blob as an un-named orphan on disk.

    Returns the blob name of the freshly written (now-current) blob.
    Mirrors the `_craft_clobber_snapshot` helper in the unit test suite.
    """
    tmp = drive.parent / f"craft_{host}_{gen}.db"
    _make_sync_db(tmp, nodes)
    proj = SnapshotSync._projection_hash(tmp)
    SnapshotSync._stamp_blob_identity(tmp, gen, proj)
    blob_name = f"graph.snapshot.{gen}.{host}.craftaa.db"
    drive.mkdir(parents=True, exist_ok=True)
    shutil.copy2(tmp, drive / blob_name)
    tmp.unlink()
    (drive / _SNAPSHOT_PTR).write_text(
        json.dumps(
            {
                "generation": gen,
                "blob": blob_name,
                "content_hash": proj,
                "format_version": 2,
                "host": host,
            }
        )
    )
    return blob_name


# ---------------------------------------------------------------------------
# Task 4 — Test 1: deferred-pull union convergence under default (v1) format
# ---------------------------------------------------------------------------


def test_deferred_pull_two_machines_converge_on_union(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two machines sharing a snapshot dir, each with DISTINCT rows, converge to
    the UNION of all rows with ZERO loss when driven through the flusher-tick
    cadence (pull_if_newer → push) rather than the old blocking-startup pull.

    This is the core regression guard for the non-blocking startup pull change:
    snapshot_pull_blocking=False (the new default) must not lose any durable row
    written on either machine.

    The test drives ticks deterministically (no daemon thread, no sleep) to stay
    fast while still exercising the exact code path _flush_loop runs.
    """
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))

    drive = tmp_path / "drive"
    db_a = tmp_path / "machine_a" / "graph.db"
    db_b = tmp_path / "machine_b" / "graph.db"

    # Each machine starts with its own distinct local DB (established — not fresh).
    _make_sync_db(db_a, [("a1", "A row one"), ("a2", "A row two")])
    _make_sync_db(db_b, [("b1", "B row one"), ("b2", "B row two")])

    sa = _sync(db_a, drive, "hostA")
    sb = _sync(db_b, drive, "hostB")

    # Mark both dirty (simulating writes that occurred before the flusher starts).
    sa.mark_durable_dirty()
    sb.mark_durable_dirty()

    # --- Tick 1: A pulls (nothing yet), then pushes its rows to Drive. --------
    _flusher_tick(sa)
    # A published; B still hasn't pulled yet.
    assert _node_ids(db_a) == {"a1", "a2"}, "A must only have its own rows after tick 1"

    # --- Tick 2: B pulls A's snapshot (union), then pushes the superset. ------
    _flusher_tick(sb)
    b_after_tick2 = _node_ids(db_b)
    assert "a1" in b_after_tick2, "B must have folded A's rows by tick 2"
    assert "a2" in b_after_tick2
    assert "b1" in b_after_tick2
    assert "b2" in b_after_tick2

    # --- Tick 3: A pulls B's superset (union). --------------------------------
    _flusher_tick(sa)
    a_final = _node_ids(db_a)
    assert "b1" in a_final, "A must have B's rows after pulling B's superset"
    assert "b2" in a_final

    # --- Final convergence check: both DBs hold the full union, zero loss. ----
    union = {"a1", "a2", "b1", "b2"}
    assert _node_ids(db_a) == union, f"A final mismatch: {_node_ids(db_a)} != {union}"
    assert _node_ids(db_b) == union, f"B final mismatch: {_node_ids(db_b)} != {union}"


# ---------------------------------------------------------------------------
# Task 4 — Test 2: same-gen collision → orphan sweep folds the stranded blob
# ---------------------------------------------------------------------------


def test_same_gen_collision_sweep_folds_orphan_under_deferred_pull(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A same-generation collision leaves one machine's blob un-named by the current
    pointer (a §3.3a orphan).  The startup orphan-blob sweep inside pull_if_newer()
    folds it in additively so both machines converge to the union with ZERO loss,
    without waiting for the orphaned host to re-publish via the outbox.

    Scenario (mirrors §3.3a spec + unit test test_startup_sweep_folds_unmerged_peer_orphan_blob):
      - Machine M publishes gen-1 {m1} as a v2 (immutable) blob; pointer names M's blob.
      - A same-gen clobber re-points to a DIFFERENT gen-1 blob {l1}; M's blob is orphaned.
      - Machine L's lineage is already at gen 1, so the pointer pull short-circuits.
      - The orphan-blob sweep inside pull_if_newer() folds {m1} into L despite the
        short-circuit, producing the union {l1, m1} with zero loss.
    """
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))

    drive = tmp_path / "drive"
    db_m = tmp_path / "machine_m" / "graph.db"
    db_l = tmp_path / "machine_l" / "graph.db"

    # M: establish a local DB with {m1}, publish a v2 snapshot at gen 1.
    _make_sync_db(db_m, [("m1", "from M")])
    sm = SnapshotSync(db_m, drive, WriteLock(db_m), min_interval=0.0, immutable=True)
    sm._host = "hostM"
    sm.mark_durable_dirty()
    sm.push(force=True)
    assert (drive / _SNAPSHOT_PTR).exists(), "v2 pointer must be present after M's push"

    # Save M's blob name before the clobber (it becomes the orphan).
    ptr_before = json.loads((drive / _SNAPSHOT_PTR).read_text())
    m_blob_name = ptr_before["blob"]

    # L: establish a local DB with {l1} at gen 1 (lineage already there).
    _make_sync_db(db_l, [("l1", "from L")])
    sl = SnapshotSync(db_l, drive, WriteLock(db_l), min_interval=0.0, immutable=True)
    sl._host = "hostL"
    # Record L's local gen as 1 so the pointer pull short-circuits (remote_gen <= local_gen).
    sl._write_json(sl.local_meta, {"generation": 1, "host": "hostL", "content_hash": None})
    assert sl._local_known_generation() == 1

    # Simulate the same-gen clobber: pointer now names L's blob, M's blob is orphaned.
    _craft_same_gen_orphan(drive, 1, "hostL", [("l1", "from L")])
    assert json.loads((drive / _SNAPSHOT_PTR).read_text())["host"] == "hostL"
    # M's original blob is still on disk as an orphan.
    assert (drive / m_blob_name).exists(), "M's orphan blob must still be on disk"

    # One flusher tick on L: pull_if_newer() short-circuits the pointer pull but
    # the orphan-blob sweep folds {m1} in additively.
    _flusher_tick(sl)

    l_after = _node_ids(db_l)
    assert "l1" in l_after, "L must keep its own row"
    assert "m1" in l_after, "orphan sweep must fold M's row despite the gen short-circuit"

    # Zero loss: the union {l1, m1} is present on L.
    assert l_after == {"l1", "m1"}, f"L must hold the full union; got {l_after}"
