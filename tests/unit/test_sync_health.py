"""Item 2 / P0 — sync detection + convergence audit.

Every catastrophic MemCP sync failure to date reported success. These tests
pin the detection surface that converts the next silent loss into a counted,
visible incident:

  * a consecutive-failure counter in the flusher tick (escalates to error at 3),
  * an instance health surface (errors / dirty / staleness / flusher liveness),
  * pointer staleness fields on the disk-based snapshot_health(),
  * a convergence audit that compares the local active corpus against the
    currently-published snapshot and reports any row delta.

See docs/superpowers/specs/2026-06-10-hardening-implementation-SPEC.md Item 2.

NOTE (per spec sign-off): the convergence-audit test proves detection works
when invoked; it deliberately does NOT assert the audit runs on every pull
cycle (that assertion was flagged as a soft spot and held for Jeff's go).
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

import pytest

from memcp.core.snapshot_sync import SnapshotSync, snapshot_health
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


def _make_db(path: Path, nodes: list[tuple[str, str]]) -> None:
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


def _sync(db: Path, drive: Path) -> SnapshotSync:
    return SnapshotSync(db, drive, WriteLock(db), min_interval=0.0)


def _publish(db: Path, drive: Path) -> None:
    """Publish ``db`` as the current (v1) snapshot from a notional machine A."""
    s = _sync(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is True


# ── (a) failure counter + escalation ──────────────────────────────────


def test_flusher_counts_consecutive_failures(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("n1", "row")])
    s = _sync(db, drive)
    s.mark_durable_dirty()

    def boom(*_a: object, **_k: object) -> bool:
        raise RuntimeError("drive down")

    s.push = boom  # type: ignore[method-assign]

    with caplog.at_level(logging.ERROR):
        for _ in range(3):
            s._flush_tick()

    assert s._sync_error_count == 3, "each failed tick must increment the counter"
    assert any(
        r.levelno >= logging.ERROR and "consecutive" in r.getMessage()
        for r in caplog.records
    ), "must escalate to error level at >=3 consecutive failures"


def test_sync_error_count_resets_after_success(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("n1", "row")])
    s = _sync(db, drive)
    s.mark_durable_dirty()

    failures = {"n": 2}

    real_push = s.push

    def flaky(*a: object, **k: object) -> bool:
        if failures["n"] > 0:
            failures["n"] -= 1
            raise RuntimeError("drive down")
        return real_push(force=True)

    s.push = flaky  # type: ignore[method-assign]

    s._flush_tick()
    s._flush_tick()
    assert s._sync_error_count == 2
    s.mark_durable_dirty()
    s._flush_tick()  # this one succeeds
    assert s._sync_error_count == 0, "a successful push must reset the failure counter"


# ── (b) instance health surface ───────────────────────────────────────


def test_instance_health_surface(tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db, [("n1", "row")])
    s = _sync(db, drive)
    h = s.instance_health()
    for key in (
        "sync_error_count",
        "durable_dirty",
        "seconds_since_last_push",
        "flusher_alive",
    ):
        assert key in h, f"instance_health missing {key!r}"
    assert h["sync_error_count"] == 0
    assert h["flusher_alive"] is False  # flusher not started in this test


def test_status_includes_sync_health_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """memcp_status (memory.status) must surface the new sync-health fields."""
    import memcp.config as config_module

    snap = tmp_path / "drive-snapshot"
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    config_module._config = None
    try:
        from memcp.core.memory import memory_status, remember

        remember("an insight to materialize the graph", project="p")
        result = memory_status(project="p")
        assert "snapshot" in result
        assert "instance" in result["snapshot"], "status must include instance health"
        inst = result["snapshot"]["instance"]
        assert "sync_error_count" in inst
        assert "seconds_since_last_push" in inst
        assert "flusher_alive" in inst
    finally:
        config_module._config = None


# ── (c) pointer staleness on snapshot_health() ────────────────────────


def test_snapshot_health_reports_pointer_age(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    drive.mkdir(parents=True)
    written = time.time() - 7200  # 2 hours ago
    (drive / "graph.snapshot.ptr.json").write_text(
        json.dumps(
            {
                "generation": 5,
                "blob": "graph.snapshot.5.host.abcd.db",
                "content_hash": "x",
                "format_version": 2,
                "host": "mac-mini.local",
                "written_at": written,
            }
        )
    )
    h = snapshot_health(str(drive))
    assert h["pointer_host"] == "mac-mini.local"
    assert h["pointer_written_at"] == pytest.approx(written, abs=1)
    assert h["pointer_age_seconds"] is not None
    assert h["pointer_age_seconds"] >= 7000  # ~2h elapsed


# ── (c) convergence audit ─────────────────────────────────────────────


def test_convergence_audit_reports_delta(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"  # publisher: 5 rows
    db_b = tmp_path / "b" / "graph.db"  # local: missing 3 of them
    _make_db(db_a, [(f"n{i}", f"row{i}") for i in range(5)])
    _make_db(db_b, [("n0", "row0"), ("n1", "row1")])
    _publish(db_a, drive)

    s = _sync(db_b, drive)
    conv = s.convergence_audit()
    assert conv, "audit must return a report when a snapshot is published"
    assert conv["delta"] == 3, "snapshot has 3 rows the local DB is missing"
    assert conv["converged"] is False


def test_convergence_audit_converged_when_equal(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    rows = [(f"n{i}", f"row{i}") for i in range(4)]
    _make_db(db_a, rows)
    _make_db(db_b, rows)
    _publish(db_a, drive)

    s = _sync(db_b, drive)
    conv = s.convergence_audit()
    assert conv["delta"] == 0, "identical corpora have no row delta"
