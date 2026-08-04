"""Step 6 — immutable-blob publish + pointer (§3.2), robustness layer.

Today push overwrites a single mutable ``graph.snapshot.db``. Two concurrent
publishers can have one's blob overwritten. Per §1 that data isn't *lost* (it
lives on the origin and re-propagates via the union), but Step 6 removes the
race/convergence-slowness window:

* publish a globally-unique, immutable blob ``graph.snapshot.<gen>.<host>.<rand>.db``
* name it from a tiny ``graph.snapshot.ptr.json`` pointer (blob + hash + gen)
* self-verify (gen + hash in an in-DB meta row AND the pointer)
* pointer-ahead-of-blob: readers tolerate, publishers defer
* GC old blobs by per-host last-merged-gen
* enforced re-push outbox (§3.3a)

The v2 format is **default-OFF** (the rollout flip is a later two-machine step).
See docs/superpowers/specs/2026-06-01-no-loss-merge-sync-design.md §3.2/§3.3a.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from memcp.core.snapshot_sync import _SNAPSHOT_MERGED, _SNAPSHOT_PTR, SnapshotSync
from memcp.core.write_lock import WriteLock


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, content TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL, resurrected_at TEXT DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0');
"""


def _make_db(path: Path, nodes: list[tuple]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    for nid, content in nodes:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            (nid, content, "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


def _v2(db: Path, drive: Path) -> SnapshotSync:
    return SnapshotSync(db, drive, WriteLock(db), min_interval=0.0, immutable=True)


def _v1(db: Path, drive: Path) -> SnapshotSync:
    return SnapshotSync(db, drive, WriteLock(db), min_interval=0.0)


def _blobs(drive: Path) -> list[Path]:
    return sorted(drive.glob("graph.snapshot.*.db"))


# ── increment A: immutable write produces a unique blob + pointer ──────


def test_immutable_push_writes_unique_blob_and_pointer(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "from A")])

    s = _v2(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is True

    ptr_path = drive / _SNAPSHOT_PTR
    assert ptr_path.exists(), "v2 push must write a pointer file"
    ptr = json.loads(ptr_path.read_text())
    assert ptr["format_version"] == 2
    assert ptr["generation"] >= 1
    assert ptr["content_hash"], "pointer must carry a content hash"

    blob = drive / ptr["blob"]
    assert blob.exists(), "pointer must name an existing blob"
    # The blob is the unique, gen+host-suffixed name — never the bare file.
    assert blob.name.startswith(f"graph.snapshot.{ptr['generation']}.")
    assert blob.name != "graph.snapshot.db"
    assert s._host in blob.name, "blob name must include the host for global uniqueness"


def test_default_off_writes_v1_bare_file_and_no_pointer(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "from A")])

    s = _v1(db, drive)  # default: immutable OFF
    s.mark_durable_dirty()
    assert s.push(force=True) is True

    assert (drive / "graph.snapshot.db").exists(), "v1 push writes the bare mutable file"
    assert not (drive / _SNAPSHOT_PTR).exists(), "v1 push must NOT write a v2 pointer"
    assert not _blobs(drive), "v1 push must NOT write unique immutable blobs"


def test_immutable_flag_from_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The flag is read from MEMCP_SNAPSHOT_IMMUTABLE when not passed explicitly."""
    monkeypatch.setenv("MEMCP_SNAPSHOT_IMMUTABLE", "true")
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "from A")])

    s = SnapshotSync(db, drive, WriteLock(db), min_interval=0.0)
    assert s._immutable is True
    s.mark_durable_dirty()
    s.push(force=True)
    assert (drive / _SNAPSHOT_PTR).exists()


# ── increment B: v2 reader reads via the pointer; v1 fallback retained ──


def _ids(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT id FROM nodes")}
    finally:
        conn.close()


def test_v2_reader_unions_via_pointer(tmp_path: Path) -> None:
    """A v2 reader resolves the blob named by the pointer and unions it in."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "from A")])
    _make_db(db_b, [("b1", "from B")])

    sa = _v2(db_a, drive)
    sa.mark_durable_dirty()
    sa.push(force=True)

    assert _v2(db_b, drive).pull_if_newer() is True
    assert _ids(db_b) == {"a1", "b1"}, "v2 pull must union both machines' rows"


def test_v2_reader_fresh_machine_adopts_blob(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    _make_db(db_a, [("a1", "from A"), ("a2", "also A")])
    sa = _v2(db_a, drive)
    sa.mark_durable_dirty()
    sa.push(force=True)

    db_b = tmp_path / "b" / "graph.db"  # no local DB yet
    assert _v2(db_b, drive).pull_if_newer() is True
    assert _ids(db_b) == {"a1", "a2"}


def test_v2_reader_falls_back_to_bare_v1_file(tmp_path: Path) -> None:
    """A v2-aware reader still pulls a legacy v1 snapshot (no pointer present)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "from A")])
    _make_db(db_b, [("b1", "from B")])

    sa = _v1(db_a, drive)  # A publishes the legacy v1 way (bare file, no pointer)
    sa.mark_durable_dirty()
    sa.push(force=True)
    assert not (drive / _SNAPSHOT_PTR).exists()

    assert _v2(db_b, drive).pull_if_newer() is True, "v2 reader must read legacy v1"
    assert _ids(db_b) == {"a1", "b1"}


# ── increment C: globally-unique blobs survive a same-gen collision ────


def _ptr(drive: Path) -> dict:
    return json.loads((drive / _SNAPSHOT_PTR).read_text())


def test_v2_push_advances_past_remote_pointer_generation(tmp_path: Path) -> None:
    """A fresh publisher that hasn't pulled still mints a generation ABOVE the
    remote pointer's — so the bare ``<gen>`` counter is read from the pointer
    (the Drive-synced source of truth), not the absent v1 sidecar."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    _make_db(db_b, [("b1", "B")])

    sa = _v2(db_a, drive)
    sa._host = "hostA"
    sa.mark_durable_dirty()
    sa.push(force=True)
    assert _ptr(drive)["generation"] == 1

    # B never pulled (local_known == 0); without pointer-aware gen it would
    # collide at gen 1. It must advance to gen 2.
    sb = _v2(db_b, drive)
    sb._host = "hostB"
    sb.mark_durable_dirty()
    sb.push(force=True)
    assert _ptr(drive)["generation"] == 2, "gen must advance past the remote pointer"


def test_same_gen_collision_keeps_distinct_blobs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two publishers that both stale-read the same base gen mint the same
    ``<gen>`` integer; the ``<host>.<rand>`` suffix makes both blobs survive
    under distinct names (§3.2 Drive mechanics)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    _make_db(db_b, [("b1", "B")])

    # A lagging peer in the ledger keeps the GC floor low, so both freshly
    # published gen-42 blobs are retained (GC would otherwise reap the orphan
    # once both publishers had merged it — which is correct, but here we're
    # demonstrating the no-clobber naming property).
    drive.mkdir(parents=True, exist_ok=True)
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"hostLagging": 0}))

    sa = _v2(db_a, drive)
    sa._host = "hostA"
    sb = _v2(db_b, drive)
    sb._host = "hostB"
    # Force the collision window: both see the same stale base gen 41 → both 42.
    for s in (sa, sb):
        monkeypatch.setattr(s, "_remote_generation", lambda: 41)
        monkeypatch.setattr(s, "_local_known_generation", lambda: 41)

    sa.mark_durable_dirty()
    assert sa.push(force=True) is True
    sb.mark_durable_dirty()
    assert sb.push(force=True) is True

    blobs = sorted(p.name for p in drive.glob("graph.snapshot.42.*.db"))
    assert len(blobs) == 2, f"both gen-42 blobs must survive, got {blobs}"
    assert any("hostA" in b for b in blobs)
    assert any("hostB" in b for b in blobs)


# ── increment D: self-verifying — torn / half-synced blob rejected ─────


def test_blob_carries_embedded_gen_and_hash(tmp_path: Path) -> None:
    """The blob self-describes: gen + content-hash live in an in-DB meta row,
    matching the pointer (§3.2.5)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    sa = _v2(db_a, drive)
    sa.mark_durable_dirty()
    sa.push(force=True)

    ptr = _ptr(drive)
    blob = drive / ptr["blob"]
    conn = sqlite3.connect(str(blob))
    try:
        meta = dict(
            conn.execute("SELECT key, value FROM meta WHERE key LIKE 'snapshot_%'").fetchall()
        )
    finally:
        conn.close()
    assert int(meta["snapshot_generation"]) == ptr["generation"]
    assert meta["snapshot_content_hash"] == ptr["content_hash"]


def test_torn_blob_content_mismatch_rejected(tmp_path: Path) -> None:
    """A blob whose content no longer hashes to its claimed value (a torn /
    half-synced Drive file) is treated as "no newer yet", never trusted."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    _make_db(db_b, [("b1", "B")])
    sa = _v2(db_a, drive)
    sa.mark_durable_dirty()
    sa.push(force=True)

    # Tamper the blob: add a row so its projection no longer matches the pointer
    # hash — but it's still valid sqlite (simulates a half-synced superset).
    blob = drive / _ptr(drive)["blob"]
    conn = sqlite3.connect(str(blob))
    conn.execute(
        "INSERT INTO nodes (id, content, created_at) VALUES ('torn', 'x', ?)",
        ("2026-01-01T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()

    assert _v2(db_b, drive).pull_if_newer() is False, "torn blob must not be pulled"
    assert _ids(db_b) == {"b1"}, "local must be preserved when the blob is torn"


def test_pointer_hash_disagreeing_with_blob_rejected(tmp_path: Path) -> None:
    """If the pointer's content_hash disagrees with the blob's embedded hash
    (inconsistent carriers), the reader refuses the blob."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    _make_db(db_b, [("b1", "B")])
    sa = _v2(db_a, drive)
    sa.mark_durable_dirty()
    sa.push(force=True)

    ptr_path = drive / _SNAPSHOT_PTR
    ptr = json.loads(ptr_path.read_text())
    ptr["content_hash"] = "deadbeef" * 8  # a hash the blob doesn't match
    ptr_path.write_text(json.dumps(ptr))

    assert _v2(db_b, drive).pull_if_newer() is False, "pointer/blob hash mismatch must not pull"
    assert _ids(db_b) == {"b1"}


# ── increment E: pointer-ahead-of-blob — tolerate (read) / defer (push) ─


def _write_dangling_pointer(drive: Path, gen: int = 5) -> None:
    """A pointer naming a gen whose blob hasn't synced down yet (the common
    pointer-ahead-of-blob case: the tiny pointer syncs before the big blob)."""
    drive.mkdir(parents=True, exist_ok=True)
    (drive / _SNAPSHOT_PTR).write_text(
        json.dumps(
            {
                "generation": gen,
                "blob": f"graph.snapshot.{gen}.hostX.deadbeef.db",  # absent
                "content_hash": "abc123",
                "format_version": 2,
                "host": "hostX",
            }
        )
    )


def test_reader_tolerates_pointer_ahead_of_blob(tmp_path: Path) -> None:
    """A reader seeing a pointer whose blob is absent treats it as no-newer-yet
    — no crash, local preserved (§3.2.6)."""
    drive = tmp_path / "drive"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_b, [("b1", "B")])
    _write_dangling_pointer(drive)

    assert _v2(db_b, drive).pull_if_newer() is False
    assert _ids(db_b) == {"b1"}


def test_publisher_defers_when_pointer_ahead_of_blob(tmp_path: Path) -> None:
    """A publisher in catch-up must DEFER until the named blob is readable —
    never publish its own superset that omits the not-yet-readable gen's rows
    (which would re-create single-copy exposure) (§3.2.6)."""
    drive = tmp_path / "drive"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_b, [("b1", "B")])
    _write_dangling_pointer(drive, gen=5)

    sb = _v2(db_b, drive)
    sb._host = "hostB"
    sb.mark_durable_dirty()
    assert sb.push(force=True) is False, "must defer while the remote blob is unreadable"
    # The remote pointer is untouched and B published nothing.
    assert _ptr(drive)["generation"] == 5
    assert not list(drive.glob("graph.snapshot.*.hostB.*.db")), "B must not publish a superset"


def test_push_catches_up_before_publishing_superset(tmp_path: Path) -> None:
    """When the remote snapshot advanced, push folds it in FIRST so the
    published blob is a true superset (local ⊇ snapshot), never row-dropping."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    db_c = tmp_path / "c" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    _make_db(db_b, [("b1", "B")])

    sa = _v2(db_a, drive)
    sa._host = "hostA"
    sa.mark_durable_dirty()
    sa.push(force=True)  # gen 1, blob {a1}

    # B hasn't pulled (local_known 0) but pushes — it must catch up to gen 1
    # before publishing, so its blob is {a1, b1}, not {b1}.
    sb = _v2(db_b, drive)
    sb._host = "hostB"
    sb.mark_durable_dirty()
    assert sb.push(force=True) is True
    assert _ids(db_b) == {"a1", "b1"}, "push must fold in the newer snapshot first"

    # A third machine reading the new pointer sees the full superset.
    assert _v2(db_c, drive).pull_if_newer() is True
    assert _ids(db_c) == {"a1", "b1"}, "published blob must be a true superset"


# ── increment F: GC old blobs by per-host last-merged-gen ──────────────


def _touch_blob(drive: Path, gen: int, host: str) -> str:
    drive.mkdir(parents=True, exist_ok=True)
    name = f"graph.snapshot.{gen}.{host}.aa{gen:02d}.db"
    (drive / name).write_bytes(b"x")  # GC only parses names + checks existence
    return name


def test_gc_retains_blobs_above_lagging_host_floor(tmp_path: Path) -> None:
    """A blob is retained while ANY known host hasn't merged a gen >= it — so a
    lagging peer never loses a blob it still needs (§3.2.7)."""
    drive = tmp_path / "drive"
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("x", "x")])
    b3 = _touch_blob(drive, 3, "hostA")
    b4 = _touch_blob(drive, 4, "hostA")
    b5 = _touch_blob(drive, 5, "hostA")
    # hostB lags at gen 2 → floor is 2, so gens 3/4/5 must all survive.
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"hostA": 5, "hostB": 2}))

    _v2(db, drive)._gc_blobs(keep_blob_name=b5)

    assert (drive / b3).exists(), "blob above the lagging floor must be retained"
    assert (drive / b4).exists()
    assert (drive / b5).exists(), "current blob is always retained"


def test_gc_deletes_blobs_all_hosts_have_merged(tmp_path: Path) -> None:
    """Once every host has merged a gen >= a blob's, it is collectable (except
    the current pointer's blob).

    The floor pass is content-verified (P2): a below-floor blob is reclaimed only
    when its rows/tombstones are a subset of the retained pointer blob, so these
    fixtures are real, nested-subset sqlite blobs (b3 ⊆ b4 ⊆ b5=keep).
    """
    drive = tmp_path / "drive"
    drive.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("x", "x")])

    def _blob(gen: int, ids: list[str]) -> str:
        name = f"graph.snapshot.{gen}.hostA.aa{gen:02d}.db"
        p = drive / name
        _make_db(p, [(i, i) for i in ids])
        SnapshotSync._stamp_blob_identity(p, gen, SnapshotSync._projection_hash(p))
        return name

    b3 = _blob(3, ["n1"])
    b4 = _blob(4, ["n1", "n2"])
    b5 = _blob(5, ["n1", "n2", "n3"])  # pointer/keep blob — superset of b3, b4
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"hostA": 5, "hostB": 5}))

    _v2(db, drive)._gc_blobs(keep_blob_name=b5)

    assert not (drive / b3).exists(), "fully-merged subset blob must be collected"
    assert not (drive / b4).exists()
    assert (drive / b5).exists(), "current blob is never collected"


def test_gc_retains_everything_without_peer_info(tmp_path: Path) -> None:
    """With no merged-gen ledger yet, GC is conservative and deletes nothing."""
    drive = tmp_path / "drive"
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("x", "x")])
    b3 = _touch_blob(drive, 3, "hostA")
    b4 = _touch_blob(drive, 4, "hostA")

    _v2(db, drive)._gc_blobs(keep_blob_name=b4)

    assert (drive / b3).exists() and (drive / b4).exists()


def test_publish_records_merged_gen_and_gcs(tmp_path: Path) -> None:
    """A v2 publish records the host's merged gen and runs GC; a lagging peer's
    blob survives the race (GC-races-pull no-loss, §3.2.7)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    _make_db(db_b, [("b1", "B")])

    sa = _v2(db_a, drive)
    sa._host = "hostA"
    sa.mark_durable_dirty()
    sa.push(force=True)  # gen 1
    merged = json.loads((drive / _SNAPSHOT_MERGED).read_text())
    assert merged.get("hostA") == 1, "publish must record the host's merged gen"

    sb = _v2(db_b, drive)
    sb._host = "hostB"
    sb.pull_if_newer()  # merges gen1 → records hostB=1
    sb.mark_durable_dirty()
    sb.push(force=True)  # gen 2 superset, GC runs

    # a1 is never lost — present on a fresh reader of the current pointer.
    db_c = tmp_path / "c" / "graph.db"
    assert _v2(db_c, drive).pull_if_newer() is True
    assert _ids(db_c) == {"a1", "b1"}


# ── increment G: enforced re-push outbox / high-water-mark (§3.3a) ─────


def _craft_clobber_snapshot(drive: Path, gen: int, host: str, nodes: list[tuple]) -> None:
    """Overwrite the pointer with a self-consistent v2 snapshot for `host` at
    `gen` — simulating a CONCURRENT publisher that clobbered the pointer at the
    same generation, bypassing catch-up (a true same-gen collision)."""
    tmp = drive.parent / f"craft_{host}_{gen}.db"
    _make_db(tmp, nodes)
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


def test_outbox_quiet_for_single_machine(tmp_path: Path) -> None:
    """No peers → our rows are in our own named lineage → never a forced re-push
    (the single-machine common case must not churn generations)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    sa = _v2(db_a, drive)
    sa._host = "hostA"
    sa.mark_durable_dirty()
    sa.push(force=True)

    assert sa._needs_outbox_repush() is False


def test_outbox_repushes_orphaned_local_row_until_echoed(tmp_path: Path) -> None:
    """A same-gen concurrent clobber orphans a1 (the pointer no longer names it).
    The outbox detects our rows fell out of the named lineage and re-pushes a
    superset, so the row is never stranded single-copy (§3.3a)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    sa = _v2(db_a, drive)
    sa._host = "hostA"
    sa.mark_durable_dirty()
    sa.push(force=True)  # gen 1 {a1}, pointer = hostA
    assert sa._local_known_generation() == 1

    # A concurrent publisher clobbers the pointer at the SAME gen with {b1}.
    _craft_clobber_snapshot(drive, 1, "hostB", [("b1", "B")])

    # a1 fell out of the NAMED lineage (the pointer names only {b1}), but A's
    # gen-1 blob {a1} is still on disk as an orphan. A fresh reader now recovers
    # it via the startup orphan-blob sweep (§3.3a) rather than waiting on A's
    # outbox — so it sees {a1, b1}, not {b1}.
    db_c = tmp_path / "c" / "graph.db"
    _v2(db_c, drive).pull_if_newer()
    assert _ids(db_c) == {"a1", "b1"}, "sweep folds the orphaned a1 blob the pointer omits"

    # Outbox fires regardless: A's rows are no longer in the current pointer's
    # lineage, so A still re-publishes a superset to rejoin it.
    assert sa._needs_outbox_repush() is True
    assert sa.push(force=True) is True  # catch up {b1} + republish {a1, b1}

    db_d = tmp_path / "d" / "graph.db"
    _v2(db_d, drive).pull_if_newer()
    assert _ids(db_d) == {"a1", "b1"}, "a1 must be re-pushed into the named lineage"
    assert sa._needs_outbox_repush() is False, "after rejoining the lineage, no more re-push"


# ── increment H: review hardening (torn pointer, §3.8 beacon, orphan GC) ─


def test_torn_pointer_does_not_crash_outbox(tmp_path: Path) -> None:
    """A half-synced pointer with a non-int generation must not raise out of the
    outbox check (which runs unguarded in the daemon flush loop / stop())."""
    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / _SNAPSHOT_PTR).write_text(json.dumps({"generation": None, "host": "hostB"}))
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "A")])
    s = _v2(db, drive)
    s._host = "hostA"

    assert s._needs_outbox_repush() is False  # must not raise


def test_v2_publish_writes_failclosed_sidecar_beacon(tmp_path: Path) -> None:
    """A v2 publish must stamp format_version:2 into the legacy sidecar too, so a
    pre-gate / v1-only binary reads it and FAILS CLOSED (§3.8 reader-first)."""
    drive = tmp_path / "drive"
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "A")])
    s = _v2(db, drive)
    s.mark_durable_dirty()
    s.push(force=True)

    meta = json.loads((drive / "graph.snapshot.meta.json").read_text())
    assert meta["format_version"] == 2, (
        "sidecar beacon must advertise v2 so old readers fail closed"
    )
    assert meta["generation"] == _ptr(drive)["generation"]


def test_v1_push_stands_down_when_v2_pointer_present(tmp_path: Path) -> None:
    """A default-off (v1) writer must NOT clobber a live v2 lineage — once a
    pointer exists, v1 stands down (rollout safety)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "A")])
    _make_db(db_b, [("b1", "B")])

    sa = _v2(db_a, drive)  # A is on v2
    sa.mark_durable_dirty()
    sa.push(force=True)

    sb = _v1(db_b, drive)  # B still default-off
    sb.mark_durable_dirty()
    assert sb.push(force=True) is False, "v1 must not clobber a v2 lineage"
    assert not (drive / "graph.snapshot.db").exists(), "no bare-file overwrite from the v1 writer"


# ── increment I: startup orphan-blob sweep (§3.3a convergence latency) ──


def test_startup_sweep_folds_unmerged_peer_orphan_blob(tmp_path: Path) -> None:
    """A peer's same-gen orphan blob (retained by GC, not named by the current
    pointer) is folded by the startup sweep even when the reader's OWN lineage
    has already advanced to that generation.

    The pointer pull reconciles only against the blob the current pointer names;
    when ``remote_gen <= local_gen`` it short-circuits entirely. So a same-gen
    orphan from a generation collision is never folded by a machine whose
    lineage already reached that gen — convergence then waits on the orphaned
    host re-publishing via the outbox (§3.3a). The sweep closes that latency.
    """
    drive = tmp_path / "drive"
    db_m = tmp_path / "m" / "graph.db"
    db_l = tmp_path / "l" / "graph.db"
    _make_db(db_m, [("m1", "from M")])
    _make_db(db_l, [("l1", "from L")])

    # M publishes gen 1 {m1}: pointer names M's blob.
    sm = _v2(db_m, drive)
    sm._host = "hostM"
    sm.mark_durable_dirty()
    sm.push(force=True)
    assert _ptr(drive)["generation"] == 1

    # A same-gen clobber re-points to a DIFFERENT gen-1 blob {l1}; M's {m1} blob
    # is now an orphan — still on disk (retained by GC) but no longer named.
    _craft_clobber_snapshot(drive, 1, "hostL", [("l1", "from L")])
    assert _ptr(drive)["host"] == "hostL"

    # L's own lineage is already at gen 1 (it holds {l1}), so the pointer pull
    # short-circuits (remote_gen 1 <= local_gen 1) and would never fold {m1}.
    sl = _v2(db_l, drive)
    sl._host = "hostL"
    sl._write_json(sl.local_meta, {"generation": 1, "host": "hostL", "content_hash": None})
    assert sl._local_known_generation() == 1

    assert sl.pull_if_newer() is True, "the sweep must fold the unmerged orphan blob"
    assert _ids(db_l) == {"l1", "m1"}, "orphan {m1} must be folded despite the gen short-circuit"


def test_sweep_skips_torn_or_unverified_orphan_blob(tmp_path: Path) -> None:
    """The sweep must refuse an orphan whose content no longer hashes to its
    embedded identity (a torn / half-synced Drive blob) — same self-verification
    the pointer path applies, so a corrupt orphan can never be merged in."""
    drive = tmp_path / "drive"
    db_l = tmp_path / "l" / "graph.db"
    _make_db(db_l, [("l1", "from L")])

    # Pointer names L's own valid gen-1 blob {l1}.
    _craft_clobber_snapshot(drive, 1, "hostL", [("l1", "from L")])

    # A torn orphan: valid sqlite, identity stamped for {m1}, but then mutated so
    # its projection no longer matches the embedded hash.
    torn = tmp_path / "torn_src.db"
    _make_db(torn, [("m1", "from M")])
    SnapshotSync._stamp_blob_identity(torn, 1, SnapshotSync._projection_hash(torn))
    conn = sqlite3.connect(str(torn))
    conn.execute(
        "INSERT INTO nodes (id, content, created_at) VALUES ('m2', 'x', ?)",
        ("2026-01-01T00:00:00+00:00",),
    )
    conn.commit()
    conn.close()
    shutil.copy2(torn, drive / "graph.snapshot.1.hostM.torn0001.db")
    torn.unlink()

    sl = _v2(db_l, drive)
    sl._host = "hostL"
    sl._write_json(sl.local_meta, {"generation": 1, "host": "hostL", "content_hash": None})

    sl.pull_if_newer()
    assert _ids(db_l) == {"l1"}, "a torn/unverified orphan must NOT be folded"


def test_sweep_records_and_skips_already_folded_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The first sweep records the folded orphan's NAME in the local sidecar, and
    a later startup skips it — so a stable set of retained orphans isn't
    re-unioned on every boot."""
    drive = tmp_path / "drive"
    db_m = tmp_path / "m" / "graph.db"
    db_l = tmp_path / "l" / "graph.db"
    _make_db(db_m, [("m1", "from M")])
    _make_db(db_l, [("l1", "from L")])

    sm = _v2(db_m, drive)
    sm._host = "hostM"
    sm.mark_durable_dirty()
    sm.push(force=True)  # gen 1 {m1}, pointer names M's blob
    orphan_name = _ptr(drive)["blob"]

    _craft_clobber_snapshot(drive, 1, "hostL", [("l1", "from L")])  # repoint; {m1} orphaned

    sl = _v2(db_l, drive)
    sl._host = "hostL"
    sl._write_json(sl.local_meta, {"generation": 1, "host": "hostL", "content_hash": None})
    sl.pull_if_newer()
    assert _ids(db_l) == {"l1", "m1"}, "first sweep folds the orphan"
    assert orphan_name in json.loads(sl.swept_meta.read_text())["folded"], (
        "the folded orphan's name must be persisted to the local sidecar"
    )

    # A second startup must NOT re-union the already-recorded orphan.
    sl2 = _v2(db_l, drive)
    sl2._host = "hostL"
    refolded: list[str] = []
    real_union = sl2._union_pull

    def _spy_union(blob: Path | None = None) -> bool:
        if blob is not None:
            refolded.append(Path(blob).name)
        return real_union(blob)

    monkeypatch.setattr(sl2, "_union_pull", _spy_union)
    sl2.pull_if_newer()
    assert orphan_name not in refolded, "an already-folded orphan must not be re-unioned"


def test_gc_retains_same_gen_orphan_until_floor_exceeds(tmp_path: Path) -> None:
    """A same-gen sibling orphan (two blobs at the same gen, one un-named) must
    survive until the floor STRICTLY exceeds its gen — i.e. every host published a
    superset past it. Otherwise GC can drop the sole off-machine copy of a row."""
    drive = tmp_path / "drive"
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("x", "x")])
    keep = _touch_blob(drive, 5, "hostA")
    orphan = _touch_blob(drive, 5, "hostB")  # same gen, different host
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"hostA": 5, "hostB": 5}))

    _v2(db, drive)._gc_blobs(keep_blob_name=keep)

    assert (drive / orphan).exists(), "same-gen orphan must not be reaped at floor == its gen"
