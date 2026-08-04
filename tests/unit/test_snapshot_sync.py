"""Tests for cross-machine snapshot sync (snapshot_sync.py).

Simulates two machines sharing a Drive-synced snapshot directory while each
keeps its own local DB: push from machine A, pull on machine B, generation
gating, and integrity validation before trusting a snapshot.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memcp.core.snapshot_sync import SnapshotSync
from memcp.core.write_lock import WriteLock


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))


def _make_db(path: Path, rows: int) -> None:
    # Use the real `nodes` schema so pull exercises the additive union (§3.1).
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO nodes (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def _count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
    finally:
        conn.close()


def _sync(db: Path, drive: Path) -> SnapshotSync:
    return SnapshotSync(db, drive, WriteLock(db), min_interval=0.0)


def test_push_creates_snapshot_and_meta(tmp_path: Path) -> None:
    db = tmp_path / "local" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, 5)
    s = _sync(db, drive)
    s.mark_dirty()
    assert s.push(force=True) is True
    assert (drive / "graph.snapshot.db").exists()
    assert s._remote_generation() == 1
    assert _count(drive / "graph.snapshot.db") == 5


def test_pull_brings_newer_snapshot_to_second_machine(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    # Machine A pushes 7 rows.
    db_a = tmp_path / "a" / "graph.db"
    _make_db(db_a, 7)
    sa = _sync(db_a, drive)
    sa.mark_dirty()
    sa.push(force=True)

    # Machine B has an older local DB (3 rows) → should pull A's 7.
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_b, 3)
    sb = _sync(db_b, drive)
    assert sb.pull_if_newer() is True
    assert _count(db_b) == 7


def test_pull_skips_when_local_current(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    db = tmp_path / "a" / "graph.db"
    _make_db(db, 4)
    s = _sync(db, drive)
    s.mark_dirty()
    s.push(force=True)  # generation 1, local meta now knows gen 1
    # Same machine pulls again — remote gen == local known gen → no-op.
    assert s.pull_if_newer() is False


def test_pull_rejects_corrupt_snapshot(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir(parents=True)
    # Write a bogus snapshot + meta claiming a high generation.
    (drive / "graph.snapshot.db").write_bytes(b"this is not a sqlite db")
    (drive / "graph.snapshot.meta.json").write_text('{"generation": 99, "host": "x"}')
    db = tmp_path / "a" / "graph.db"
    _make_db(db, 2)
    s = _sync(db, drive)
    assert s.pull_if_newer() is False  # corrupt → not trusted
    assert _count(db) == 2  # local preserved


def test_round_trip_generations_increment(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    db = tmp_path / "a" / "graph.db"
    _make_db(db, 1)
    s = _sync(db, drive)
    for expected in (1, 2, 3):
        # The durable projection must actually change for a new generation to be
        # published — quiescence (§3.3) suppresses no-op republishes.
        conn = sqlite3.connect(str(db))
        conn.execute("INSERT INTO nodes (v) VALUES (?)", (f"extra{expected}",))
        conn.commit()
        conn.close()
        s.mark_dirty()
        s.push(force=True)
        assert s._remote_generation() == expected


def test_disabled_when_no_snapshot_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # NodeStore wiring: snapshot_dir unset → no sync object created.
    from memcp.config import get_config
    from memcp.core.node_store import NodeStore

    monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MEMCP_SNAPSHOT_DIR", raising=False)
    import memcp.config as config_module

    config_module._config = None
    get_config().ensure_dirs()
    store = NodeStore()
    try:
        store._get_conn()
        assert store._sync is None
    finally:
        store.close()
