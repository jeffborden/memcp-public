"""Item 4 / P2 — close the GC-floor loss chain.

_record_merged_generation was a lock-free read-modify-write of the Drive ledger:
concurrent writers could drop a host's entry, inflating min(ledger.values())
(the GC floor), and the floor pass then deleted below-floor blobs with NO
content verification and DEBUG-only logging — the predicted "fourth silent
loss". These tests pin:

  (a) merge-MAX on the ledger write so a concurrent peer entry survives,
  (b) the floor pass content-verifies before deleting (a below-floor blob
      holding a row absent from the pointer superset is NEVER silently unlinked),
  (c) every floor-pass deletion/refusal emits a telemetry event.

See docs/superpowers/specs/2026-06-10-hardening-implementation-SPEC.md Item 4.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memcp.core import telemetry
from memcp.core.snapshot_sync import SnapshotSync
from memcp.core.write_lock import WriteLock

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


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))


def _make_db(path: Path, node_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    for nid in node_ids:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            (nid, f"content-{nid}", "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


def _make_blob(path: Path, node_ids: list[str], gen: int) -> None:
    """A valid, self-consistent immutable snapshot blob with stamped identity."""
    _make_db(path, node_ids)
    h = SnapshotSync._projection_hash(path)
    SnapshotSync._stamp_blob_identity(path, gen, h)


def _sync(db: Path, drive: Path) -> SnapshotSync:
    # immutable=True (v2 GC path); max_blobs=0 disables the cap pass so these
    # tests exercise the FLOOR pass in isolation.
    return SnapshotSync(db, drive, WriteLock(db), min_interval=0.0, immutable=True, max_blobs=0)


# ── (a) merge-MAX ledger write ─────────────────────────────────────────


def test_record_merged_generation_survives_two_writer_race(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    db_a = tmp_path / "a" / "graph.db"
    _make_db(db_a, ["a1"])
    (db_a.parent / ".host_id").write_text("hostA.local")
    s = _sync(db_a, drive)
    assert s._host == "hostA.local"
    merged_path = s.snapshot_merged

    real_read = SnapshotSync._read_json
    real_write = SnapshotSync._write_json
    injected = {"done": False}

    def racing_read(path: Path) -> dict:
        data = real_read(path)
        # Between host A's read of the (empty) ledger and its write, peer host B
        # commits its entry — the classic lost-update window.
        if Path(path).name == "graph.snapshot.merged.json" and not injected["done"]:
            injected["done"] = True
            cur = real_read(merged_path)
            cur["hostB.local"] = 3
            real_write(merged_path, cur)
        return data

    monkeypatch.setattr(SnapshotSync, "_read_json", staticmethod(racing_read))

    s._record_merged_generation(5)

    final = real_read(merged_path)
    assert final.get("hostA.local") == 5
    assert final.get("hostB.local") == 3, (
        "a concurrent peer's ledger entry must survive the merge-MAX write"
    )


# ── (b)/(c) floor pass: content verification + telemetry ───────────────


def test_floor_pass_deletion_emits_telemetry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    db = tmp_path / "a" / "graph.db"
    _make_db(db, ["x"])
    (db.parent / ".host_id").write_text("hostA.local")
    s = _sync(db, drive)

    keep = drive / "graph.snapshot.10.hostA.local.aaaa.db"
    _make_blob(keep, ["n1", "n2"], 10)
    old = drive / "graph.snapshot.1.hostB.local.bbbb.db"  # below floor, subset of keep
    _make_blob(old, ["n1"], 1)
    s._write_json(s.snapshot_merged, {"hostA.local": 5, "hostB.local": 5})  # floor = 5

    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        telemetry, "emit", lambda kind, name, **f: events.append((kind, name, f))
    )

    s._gc_blobs(keep.name)

    assert not old.exists(), "a subset below-floor blob should be reclaimed"
    floor_events = [
        f for k, n, f in events if k == "sync" and n == "gc" and f.get("pass") == "floor"
    ]
    assert floor_events, "a floor-pass deletion must emit a telemetry event"
    assert any(f.get("blob") == old.name for f in floor_events)


def test_below_floor_orphan_with_unique_row_not_deleted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    db = tmp_path / "a" / "graph.db"
    _make_db(db, ["x"])
    (db.parent / ".host_id").write_text("hostA.local")
    s = _sync(db, drive)

    keep = drive / "graph.snapshot.10.hostA.local.aaaa.db"
    _make_blob(keep, ["n1", "n2"], 10)
    # Below-floor blob holding a row ABSENT from the pointer superset — an offline
    # originator's single copy. It must never be silently unlinked.
    orphan = drive / "graph.snapshot.1.hostB.local.bbbb.db"
    _make_blob(orphan, ["single_copy_row"], 1)
    s._write_json(s.snapshot_merged, {"hostA.local": 5, "hostB.local": 5})  # floor = 5

    events: list[tuple[str, str, dict]] = []
    monkeypatch.setattr(
        telemetry, "emit", lambda kind, name, **f: events.append((kind, name, f))
    )

    s._gc_blobs(keep.name)

    assert orphan.exists(), (
        "a below-floor blob with a row not in the pointer superset must NEVER be "
        "silently unlinked"
    )
    # The refusal is observable (not silent).
    assert any(
        f.get("pass") == "floor" and f.get("blob") == orphan.name for _k, _n, f in events
    ), "refusal to reclaim must be surfaced via telemetry"
