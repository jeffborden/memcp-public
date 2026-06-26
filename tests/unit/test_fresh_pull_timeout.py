"""Item 3 / P1 — fresh-machine pull timeout.

The 2026-06-08 non-blocking-startup plan's Task 3 was documented as shipped but
never implemented: a fresh machine (no local DB) still pulled synchronously and
UNBOUNDED, so a stalled Drive mount hung startup forever. These tests pin the
bounded fresh-machine pull and the flusher's consumption of the deferred-pull
flag.

See docs/superpowers/specs/2026-06-10-hardening-implementation-SPEC.md Item 3.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

import memcp.config as config_module
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


def _ids(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT id FROM nodes")}
    finally:
        conn.close()


# ── bounded fresh-machine pull ─────────────────────────────────────────


def test_fresh_pull_times_out_and_defers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A fresh machine whose pull stalls must NOT hang startup: _get_conn()
    returns within ~timeout with a usable (empty) DB and pull_pending set."""
    from memcp.core.node_store import NodeStore

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    snap = tmp_path / "drive"
    monkeypatch.setenv("MEMCP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("MEMCP_SNAPSHOT_PULL_TIMEOUT", "0.3")
    monkeypatch.delenv("MEMCP_SNAPSHOT_PULL_BLOCKING", raising=False)
    config_module._config = None

    def slow_pull(self: SnapshotSync) -> bool:
        time.sleep(3.0)  # far longer than the 0.3s timeout
        return False

    monkeypatch.setattr(SnapshotSync, "pull_if_newer", slow_pull)

    store = NodeStore()
    try:
        start = time.monotonic()
        conn = store._get_conn()
        elapsed = time.monotonic() - start

        assert elapsed < 2.0, f"_get_conn hung {elapsed:.1f}s past the 0.3s timeout"
        # DB is usable despite the abandoned slow pull.
        assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
        assert store._sync is not None
        assert store._sync.pull_pending is True, "timed-out pull must defer to flusher"
    finally:
        store.close()
        config_module._config = None


def test_blocking_pull_bypasses_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """MEMCP_SNAPSHOT_PULL_BLOCKING=true forces the legacy synchronous pull
    (the existing test pin) — the pull fully completes before _get_conn returns."""
    from memcp.core.node_store import NodeStore

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    snap = tmp_path / "drive"
    monkeypatch.setenv("MEMCP_DATA_DIR", str(data_dir))
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("MEMCP_SNAPSHOT_PULL_BLOCKING", "true")
    config_module._config = None

    pulled_on_main = {"flag": False}
    import threading as _threading

    main_thread = _threading.current_thread()

    def tracking_pull(self: SnapshotSync) -> bool:
        # Record whether the pull ran synchronously on the calling (main) thread.
        if _threading.current_thread() is main_thread:
            pulled_on_main["flag"] = True
        return False

    monkeypatch.setattr(SnapshotSync, "pull_if_newer", tracking_pull)

    store = NodeStore()
    try:
        store._get_conn()
        assert pulled_on_main["flag"] is True, (
            "blocking mode must run the pull synchronously on the main thread"
        )
        assert store._sync is not None
        assert store._sync.pull_pending is False, "blocking mode must not defer"
    finally:
        store.close()
        config_module._config = None


# ── flusher consumes pull_pending ──────────────────────────────────────


def test_flusher_tick_consumes_pull_pending(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A deferred fresh-machine pull (pull_pending=True) is completed and the
    flag cleared by the next flusher tick."""
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "from A"), ("a2", "also A")])
    _make_db(db_b, [("b1", "from B")])

    # Machine A publishes.
    pub = SnapshotSync(db_a, drive, WriteLock(db_a), min_interval=0.0)
    pub.mark_durable_dirty()
    assert pub.push(force=True) is True

    # Machine B has a deferred pull pending.
    s = SnapshotSync(db_b, drive, WriteLock(db_b), min_interval=0.0)
    s.pull_pending = True
    s._flush_tick()

    assert _ids(db_b) >= {"a1", "a2", "b1"}, "flusher tick must complete the pull"
    assert s.pull_pending is False, "flusher must clear pull_pending after pulling"
