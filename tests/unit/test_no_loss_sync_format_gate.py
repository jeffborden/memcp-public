"""Step 8 — format_version gate (§3.8), reader-first.

The gate must ship to BOTH machines before any future snapshot-format change:
- pull refuses a snapshot whose format_version exceeds what this binary
  supports (fail closed — never blind-read a format it can't parse).
- push refuses to overwrite a higher-format snapshot.
- a legacy snapshot with no format_version is treated as v1 (compatible).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from memcp.core.snapshot_sync import _FORMAT_V2, _FORMAT_VERSION, SnapshotSync
from memcp.core.write_lock import WriteLock


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))


def _make_db(path: Path, rows: int) -> None:
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute("CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY, v TEXT)")
    conn.executemany("INSERT INTO nodes (v) VALUES (?)", [(f"r{i}",) for i in range(rows)])
    conn.commit()
    conn.close()


def _count(path: Path) -> int:
    import sqlite3

    conn = sqlite3.connect(str(path))
    try:
        return conn.execute("SELECT count(*) FROM nodes").fetchone()[0]
    finally:
        conn.close()


def _sync(db: Path, drive: Path) -> SnapshotSync:
    return SnapshotSync(db, drive, WriteLock(db), min_interval=0.0)


def test_push_stamps_format_version(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, 3)
    s = _sync(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is True
    meta = json.loads((drive / "graph.snapshot.meta.json").read_text())
    assert meta["format_version"] == _FORMAT_VERSION


def test_pull_fails_closed_on_higher_format(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir(parents=True)
    # A future-format snapshot (valid sqlite, but format_version above the read
    # ceiling — which is now v2 since a v2-aware binary reads both v1 and v2).
    _make_db(drive / "graph.snapshot.db", 9)
    (drive / "graph.snapshot.meta.json").write_text(
        json.dumps({"generation": 5, "host": "future", "format_version": _FORMAT_V2 + 1})
    )
    db = tmp_path / "b" / "graph.db"
    _make_db(db, 2)
    s = _sync(db, drive)
    assert s.pull_if_newer() is False, "must not pull a higher-format snapshot"
    assert _count(db) == 2, "local must be preserved"


def test_push_refuses_to_overwrite_higher_format(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir(parents=True)
    _make_db(drive / "graph.snapshot.db", 9)
    (drive / "graph.snapshot.meta.json").write_text(
        json.dumps({"generation": 5, "host": "future", "format_version": _FORMAT_V2 + 1})
    )
    db = tmp_path / "a" / "graph.db"
    _make_db(db, 3)
    s = _sync(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is False, "must not clobber a higher-format snapshot"
    # The higher-format snapshot/meta is untouched.
    meta = json.loads((drive / "graph.snapshot.meta.json").read_text())
    assert meta["format_version"] == _FORMAT_V2 + 1


def test_pull_accepts_legacy_snapshot_without_format_version(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    # Machine A publishes the legacy way (no format_version in meta).
    db_a = tmp_path / "a" / "graph.db"
    _make_db(db_a, 4)
    sa = _sync(db_a, drive)
    sa.mark_durable_dirty()
    sa.push(force=True)
    meta_path = drive / "graph.snapshot.meta.json"
    meta = json.loads(meta_path.read_text())
    meta.pop("format_version", None)  # simulate a pre-gate writer
    meta_path.write_text(json.dumps(meta))

    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_b, 1)
    assert _sync(db_b, drive).pull_if_newer() is True, "legacy (no version) == v1, compatible"
