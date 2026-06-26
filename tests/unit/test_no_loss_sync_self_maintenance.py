"""Self-maintaining snapshot GC (§ design 2026-06-02, revised after review):
stable persisted host identity + content-verified count-cap backstop + observability.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from pathlib import Path

import pytest

from memcp.core.snapshot_sync import (
    _HOST_ID,
    _SNAPSHOT_MERGED,
    SnapshotSync,
    snapshot_health,
)
from memcp.core.write_lock import WriteLock


@pytest.fixture(autouse=True)
def _hermetic_sync_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("MEMCP_SNAPSHOT_MAX_BLOBS", raising=False)
    monkeypatch.delenv("MEMCP_SNAPSHOT_DIR", raising=False)


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


def _make_db(path: Path, nodes: list[tuple], tombstones: list | None = None) -> None:
    """tombstones entries may be a bare id (str) or a full
    ``(id, deleted_at, resurrected_at)`` tuple for state-sensitive tests."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    for nid, content in nodes:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            (nid, content, "2026-01-01T00:00:00+00:00"),
        )
    for t in tombstones or []:
        if isinstance(t, str):
            tid, d_at, r_at = t, "2026-01-01T00:00:00+00:00", None
        else:
            tid, d_at, r_at = (t + (None,))[:3] if len(t) == 2 else t
        conn.execute(
            "INSERT OR REPLACE INTO tombstones (id, deleted_at, resurrected_at) VALUES (?, ?, ?)",
            (tid, d_at, r_at),
        )
    conn.commit()
    conn.close()


def _corrupt_blob(drive: Path, name: str) -> None:
    (drive / name).write_bytes(b"not a sqlite database at all")


def _v2(db: Path, drive: Path, **kw) -> SnapshotSync:
    return SnapshotSync(db, drive, WriteLock(db), min_interval=0.0, immutable=True, **kw)


def _patch_nodename(monkeypatch: pytest.MonkeyPatch, name: str) -> None:
    class _UN:
        nodename = name
        sysname = "Darwin"
        release = "x"
        version = "x"
        machine = "arm64"

    monkeypatch.setattr(os, "uname", lambda: _UN())


# ── Component 1: stable host identity ─────────────────────────────────


def test_host_id_persists_qualified_name_verbatim(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_nodename(monkeypatch, "Jeffs-Mac-mini.local")
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "x")])
    s = _v2(db, tmp_path / "drive")
    assert s._host == "Jeffs-Mac-mini.local"
    assert (db.parent / _HOST_ID).read_text().strip() == "Jeffs-Mac-mini.local"


def test_host_id_is_stable_across_instances(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_nodename(monkeypatch, "Jeffs-Mac-mini.local")
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "x")])
    s1 = _v2(db, tmp_path / "drive")
    s2 = _v2(db, tmp_path / "drive")
    assert s1._host == s2._host == "Jeffs-Mac-mini.local"


def test_untrusted_bare_mac_not_persisted_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_nodename(monkeypatch, "Mac")
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "x")])
    s = _v2(db, tmp_path / "drive")
    # Session-only id, suffixed for uniqueness; NOT persisted (retry next boot).
    assert s._host.startswith("Mac-")
    assert not (db.parent / _HOST_ID).exists(), "untrusted nodename must not freeze an id"


@pytest.mark.parametrize("bad", ["Bordens-MacBook-Pro", "localhost", "", "Mac"])
def test_untrusted_dotless_names_not_persisted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad: str
) -> None:
    _patch_nodename(monkeypatch, bad)
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "x")])
    s = _v2(db, tmp_path / "drive")
    assert not (db.parent / _HOST_ID).exists()
    assert s._host and s._host != "Mac"  # never a bare ghost name


def test_concurrent_seed_converges_under_untrusted_name(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Two windows (§3.6) seeding at once must not diverge into two ids.

    For a qualified name the id is deterministic; the risk is the suffixed
    untrusted path. We assert that once a qualified name IS seen and persisted,
    a racing instance adopts the on-disk winner rather than its own value.
    """
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "x")])
    # First instance sees a qualified name and persists it.
    _patch_nodename(monkeypatch, "Jeffs-Mac-mini.local")
    s1 = _v2(db, tmp_path / "drive")
    # Second instance, even if it momentarily reads a bare name, adopts the file.
    _patch_nodename(monkeypatch, "Mac")
    s2 = _v2(db, tmp_path / "drive")
    assert s2._host == s1._host == "Jeffs-Mac-mini.local"


def test_existing_host_id_file_honored(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "x")])
    (db.parent / _HOST_ID).write_text("frozen-identity\n")
    s = _v2(db, tmp_path / "drive")
    assert s._host == "frozen-identity"


def test_fail_open_never_emits_bare_mac(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_nodename(monkeypatch, "Mac")
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "x")])

    # Force the write path to raise so we hit the fail-open branch.
    real_write = Path.write_text

    def _boom(self: Path, *a, **k):  # type: ignore[no-untyped-def]
        if self.name == _HOST_ID:
            raise OSError("disk full")
        return real_write(self, *a, **k)

    monkeypatch.setattr(Path, "write_text", _boom)
    s = _v2(db, tmp_path / "drive")
    assert s._host.startswith("Mac-"), "fail-open must still normalize a bare Mac"


# ── Component 2: content-verified count cap ───────────────────────────


def _seed_blob(
    drive: Path, gen: int, host: str, nodes: list[tuple], tombstones: list[str] | None = None
) -> str:
    drive.mkdir(parents=True, exist_ok=True)
    name = f"graph.snapshot.{gen}.{host}.{gen:08x}.db"
    _make_db(drive / name, nodes, tombstones)
    return name


def test_cap_reclaims_blobs_whose_rows_are_in_pointer(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=3)

    # Pointer blob (the retained superset) holds all rows r1..r5.
    pointer = _seed_blob(drive, 10, "h", [(f"r{i}", "x") for i in range(1, 6)])
    # Older blobs whose rows are all subsets of the pointer → safe to reclaim.
    for g in range(1, 6):
        _seed_blob(drive, g, "h", [(f"r{g}", "x")])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))  # floor protects all

    s._gc_blobs(pointer)

    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert pointer in remaining, "pointer blob always retained"
    assert len(remaining) == 3, remaining  # cap honored
    # The reclaimed ones are the oldest (gens 1..3 gone, 4..5 + pointer kept).
    gens_left = sorted(int(n.split(".")[2]) for n in remaining)
    assert gens_left == [4, 5, 10]


def test_cap_refuses_blob_with_rows_absent_from_pointer(tmp_path: Path) -> None:
    """The core safety test: an unmerged single-copy blob is NEVER reclaimed."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)

    pointer = _seed_blob(drive, 50, "A", [("r1", "x"), ("r2", "x")])
    # Filler blobs whose rows ARE in the pointer (safely reclaimable).
    _seed_blob(drive, 10, "A", [("r1", "x")])
    _seed_blob(drive, 11, "A", [("r2", "x")])
    # A blob from offline host C holding a row that is in NO other DB.
    orphan = _seed_blob(drive, 12, "C", [("only_on_C", "x")])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))

    s._gc_blobs(pointer)

    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert orphan in remaining, "blob with rows absent from pointer must be refused"
    assert pointer in remaining


def test_cap_refuses_blob_with_tombstone_absent_from_pointer(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)

    pointer = _seed_blob(drive, 50, "A", [("r1", "x")])
    _seed_blob(drive, 10, "A", [("r1", "x")])
    # Blob carrying a tombstone the pointer lacks → deleting it could resurrect.
    orphan = _seed_blob(drive, 11, "A", [("r1", "x")], tombstones=["del_me"])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))

    s._gc_blobs(pointer)
    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert orphan in remaining, "blob with unmerged tombstone must be refused"


def test_cap_never_deletes_pointer_even_if_oldest(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)
    pointer = _seed_blob(drive, 1, "h", [(f"r{i}", "x") for i in range(5)])
    for g in range(2, 6):
        _seed_blob(drive, g, "h", [("r0", "x")])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))
    s._gc_blobs(pointer)
    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert pointer in remaining


def test_cap_disabled_when_zero(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=0)
    pointer = _seed_blob(drive, 10, "h", [("r1", "x")])
    for g in range(1, 6):
        _seed_blob(drive, g, "h", [("r1", "x")])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))
    s._gc_blobs(pointer)
    assert len(list(drive.glob("graph.snapshot.*.db"))) == 6, "cap=0 disables the cap pass"


def test_cap_runs_on_static_floor(tmp_path: Path) -> None:
    """Regression: the floor short-circuit must not skip the cap pass."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=3)
    pointer = _seed_blob(drive, 100, "h", [("r1", "x")])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))  # static floor 0
    for g in range(1, 5):
        _seed_blob(drive, g, "h", [("r1", "x")])
    s._gc_blobs(pointer)  # primes _last_gc_floor
    for g in range(5, 9):
        _seed_blob(drive, g, "h", [("r1", "x")])  # more arrive above static floor
    s._gc_blobs(pointer)
    assert len(list(drive.glob("graph.snapshot.*.db"))) == 3


def test_cap_skips_unparseable_blob_names(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)
    pointer = _seed_blob(drive, 10, "h", [("r1", "x")])
    _seed_blob(drive, 1, "h", [("r1", "x")])
    _seed_blob(drive, 2, "h", [("r1", "x")])
    # A malformed name with no parseable generation.
    (drive / "graph.snapshot.garbage.db").write_bytes(b"x")
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))
    s._gc_blobs(pointer)
    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert "graph.snapshot.garbage.db" in remaining, "unparseable names are skipped, not deleted"


def test_cap_log_names_floor_pinning_host(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)
    pointer = _seed_blob(drive, 50, "local", [("r1", "x"), ("r2", "x")])
    for g in range(1, 5):
        _seed_blob(drive, g, "local", [("r1", "x")])
    # Floor pinned by "ghost" at 0; local host at 50.
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"ghost": 0, "local": 50}))
    with caplog.at_level(logging.INFO):
        s._gc_blobs(pointer)
    msgs = " ".join(r.getMessage() for r in caplog.records)
    assert "snapshot cap" in msgs
    assert "pinned by host=ghost" in msgs


# ── Component 3: observability helper ─────────────────────────────────


def test_snapshot_health_reports_counts_ledger_and_floor(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    _seed_blob(drive, 1, "A", [("r1", "x")])
    _seed_blob(drive, 2, "B", [("r2", "x")])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"A": 5, "B": 2}))
    health = snapshot_health(str(drive))
    assert health["blob_count"] == 2
    assert health["disk_bytes"] > 0
    assert health["merged_ledger"] == {"A": 5, "B": 2}
    assert health["floor"] == 2
    assert health["floor_pinned_by"] == "B"


def test_snapshot_health_absent_dir() -> None:
    assert snapshot_health("") == {}
    assert snapshot_health("/nonexistent/path/xyz") == {}


# ── added after code review: gaps the review surfaced ─────────────────


def test_cap_retains_above_max_when_all_overlimit_are_single_copy(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """THE safety property: when over cap but every over-limit candidate holds
    rows absent from the pointer, the cap REFUSES all and leaves MORE than
    max_blobs on disk. A count-only cap would fail this."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)

    pointer = _seed_blob(drive, 50, "A", [("r1", "x")])
    o1 = _seed_blob(drive, 10, "h1", [("o1", "x")])
    o2 = _seed_blob(drive, 11, "h2", [("o2", "x")])
    o3 = _seed_blob(drive, 12, "h3", [("o3", "x")])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))

    with caplog.at_level(logging.WARNING):
        s._gc_blobs(pointer)

    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert len(remaining) == 4, remaining  # cap intentionally exceeded
    assert {pointer, o1, o2, o3} <= remaining
    refusals = sum(1 for r in caplog.records if "refusing to reclaim" in r.getMessage())
    assert refusals == 3


def test_cap_refuses_blob_with_fresher_tombstone_state(tmp_path: Path) -> None:
    """A tombstone id present in BOTH but with a fresher re-delete in the
    candidate must be RETAINED — id-equality is not state-equality."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)

    # Pointer: X resurrected (live) — deleted t1, resurrected t2 > t1.
    pointer = _seed_blob(
        drive,
        50,
        "A",
        [("r1", "x")],
        tombstones=[("X", "2026-01-01T00:00:00", "2026-02-01T00:00:00")],
    )
    _seed_blob(drive, 10, "A", [("r1", "x")])  # reclaimable filler
    # Orphan carries a FRESHER re-delete of X (t3 > t2), no resurrection.
    orphan = _seed_blob(
        drive,
        11,
        "A",
        [("r1", "x")],
        tombstones=[("X", "2026-03-01T00:00:00", None)],
    )
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))

    s._gc_blobs(pointer)
    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert orphan in remaining, "blob advancing a tombstone's state must be refused"


def test_cap_reclaims_blob_with_covered_tombstone_state(tmp_path: Path) -> None:
    """A tombstone whose state is already fully covered by the pointer (equal or
    older) is safe — the blob may be reclaimed."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)

    pointer = _seed_blob(
        drive,
        50,
        "A",
        [("r1", "x")],
        tombstones=[("X", "2026-03-01T00:00:00", None)],  # X already re-deleted
    )
    # Candidate carries an OLDER state for X (subset under MAX) → covered.
    old = _seed_blob(
        drive,
        10,
        "A",
        [("r1", "x")],
        tombstones=[("X", "2026-01-01T00:00:00", None)],
    )
    _seed_blob(drive, 11, "A", [("r1", "x")])  # second filler so over>=1
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))

    s._gc_blobs(pointer)
    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert old not in remaining, "a covered (older) tombstone state is safe to reclaim"
    assert len(remaining) == 2


def test_floor_pass_deletes_then_cap_trims(tmp_path: Path) -> None:
    """Floor pass reaps gen<floor; cap trims the survivors to the limit."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=4)
    pointer = _seed_blob(drive, 100, "h", [(f"r{i}", "x") for i in range(1, 13)])
    for g in range(1, 5):  # below floor 5 → floor pass
        _seed_blob(drive, g, "h", [(f"r{g}", "x")])
    for g in range(6, 13):  # above floor, subsets → cap pass
        _seed_blob(drive, g, "h", [(f"r{g}", "x")])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peerA": 5, "peerB": 5}))

    s._gc_blobs(pointer)

    gens_left = sorted(int(p.name.split(".")[2]) for p in drive.glob("graph.snapshot.*.db"))
    assert all(g not in gens_left for g in range(1, 5)), "below-floor blobs must be gone"
    assert len(gens_left) == 4, gens_left
    assert s._last_gc_floor == 5


def test_cap_retains_candidate_with_corrupt_content(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """A parseable-named but corrupt/half-synced candidate is retained."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)
    pointer = _seed_blob(drive, 50, "h", [("r1", "x"), ("r2", "x")])
    _seed_blob(drive, 10, "h", [("r1", "x")])
    _seed_blob(drive, 11, "h", [("r2", "x")])
    corrupt = _seed_blob(drive, 1, "h", [("r1", "x")])
    _corrupt_blob(drive, corrupt)
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))

    with caplog.at_level(logging.WARNING):
        s._gc_blobs(pointer)
    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert corrupt in remaining, "corrupt-content candidate must be retained"
    assert "cannot read candidate" in " ".join(r.getMessage() for r in caplog.records)


def test_cap_short_circuits_when_pointer_unreadable(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """If the pointer blob itself is unreadable, no reclaim happens at all."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)
    pointer = _seed_blob(drive, 50, "h", [("r1", "x")])
    for g in range(1, 6):
        _seed_blob(drive, g, "h", [("r1", "x")])
    _corrupt_blob(drive, pointer)
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))

    before = {p.name for p in drive.glob("graph.snapshot.*.db")}
    with caplog.at_level(logging.WARNING):
        s._gc_blobs(pointer)
    after = {p.name for p in drive.glob("graph.snapshot.*.db")}
    assert after == before, "unreadable pointer must short-circuit the cap"
    assert "cannot read pointer blob" in " ".join(r.getMessage() for r in caplog.records)


def test_cap_counts_unparseable_blob_toward_limit(tmp_path: Path) -> None:
    """An unparseable blob occupies a cap slot (never deleted), so GC reclaims a
    well-named subset blob to hold the bound."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("a1", "x")])
    s = _v2(db, drive, max_blobs=2)
    pointer = _seed_blob(drive, 50, "h", [("r1", "x")])
    _seed_blob(drive, 1, "h", [("r1", "x")])
    _seed_blob(drive, 2, "h", [("r1", "x")])
    (drive / "graph.snapshot.garbage.db").write_bytes(b"x")
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"peer": 0}))
    s._gc_blobs(pointer)
    remaining = {p.name for p in drive.glob("graph.snapshot.*.db")}
    # pointer + garbage (never deleted) + 0..1 well-named = bounded by cap=2 budget.
    assert "graph.snapshot.garbage.db" in remaining
    assert len(remaining) == 2, remaining  # cap honored counting the unparseable file


def test_snapshot_health_dir_with_blobs_no_ledger(tmp_path: Path) -> None:
    """Fresh-machine state reaching memcp_status: floor=None, still serializable."""
    drive = tmp_path / "drive"
    _seed_blob(drive, 1, "A", [("r1", "x")])
    _seed_blob(drive, 2, "B", [("r2", "x")])
    health = snapshot_health(str(drive))
    assert health["blob_count"] == 2
    assert isinstance(health["disk_bytes"], int) and health["disk_bytes"] > 0
    assert health["merged_ledger"] == {}
    assert health["floor"] is None
    assert health["floor_pinned_by"] is None
    json.dumps(health)  # must serialize (floor -> null)


def test_snapshot_health_corrupt_ledger_floors_none(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    _seed_blob(drive, 1, "A", [("r1", "x")])
    (drive / _SNAPSHOT_MERGED).write_text(json.dumps({"A": "notanint"}))
    health = snapshot_health(str(drive))
    assert health["floor"] is None and health["floor_pinned_by"] is None
    json.dumps(health)
    (drive / _SNAPSHOT_MERGED).write_text("not json at all")
    h2 = snapshot_health(str(drive))
    assert h2["floor"] is None and h2["merged_ledger"] == {}


def test_snapshot_health_includes_v1_bare_file(tmp_path: Path) -> None:
    """On a v1 deployment the bare graph.snapshot.db must count toward disk."""
    from memcp.core.snapshot_sync import _SNAPSHOT_DB

    drive = tmp_path / "drive"
    drive.mkdir()
    (drive / _SNAPSHOT_DB).write_bytes(b"x" * 4096)
    health = snapshot_health(str(drive))
    assert health["v1_present"] is True
    assert health["v1_bytes"] == 4096
    assert health["disk_bytes"] >= 4096


def test_host_id_session_seed_is_stable_across_boots(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Repeated untrusted-name boots reuse ONE ghost key (deterministic seed),
    instead of minting a fresh one each time."""
    _patch_nodename(monkeypatch, "Mac")
    db = tmp_path / "a" / "graph.db"
    _make_db(db, [("a1", "x")])
    s1 = _v2(db, tmp_path / "drive")
    s2 = _v2(db, tmp_path / "drive")
    assert s1._host == s2._host, "untrusted host id must be stable across instances"
    assert s1._host.startswith("Mac-")
