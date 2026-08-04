"""Regression tests: slow Drive I/O must not block concurrent writers.

TDD ORACLE — approved by Jeff 2026-07-15 ("approved", after tightening the
writer budget to 0.5s and picking close-flush option (b): close skips the
peer fold and defers convergence instead of paying for it inline). These
tests were written RED-first per the sync-lock stall handoff and gate the
fix set: (1) stage remote blobs locally, (2b) close-flush skip, (3) publish/
ledger/GC out of the flock.

Mechanism under test (measured 2026-07-15): memcp_remember blocked 217.9s /
218.6s behind pushes whose Drive I/O (catch-up download of an evicted foreign
blob; publish + GC blob reads) ran while the flusher held the shared
WriteLock. In-process, writers wait on WriteLock._guard — an UNTIMED
threading.Lock — so the configured 30s flock timeout never applies to them.

Each test injects a 3s delay into ONE sync segment and asserts a concurrent
writer commit completes in well under that (the writer would need the flock
for only ~ms of real work). Budget 0.5s vs 3s injected (tightened from 1.5s
per Jeff's review, 2026-07-15).
"""

from __future__ import annotations

import json
import shutil
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memcp.core.node_store import LockedConnection
from memcp.core.snapshot_sync import _SNAPSHOT_PTR, SnapshotSync
from memcp.core.write_lock import WriteLock

_INJECTED_DELAY_S = 3.0
_WRITER_BUDGET_S = 0.5


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("MEMCP_TELEMETRY_DIR", str(tmp_path / "tele"))


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


def _make_db(path: Path, rows: int = 3, prefix: str = "n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT OR IGNORE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            [(f"{prefix}{i}", f"content {i}", now) for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def _timed_commit(db: Path, lock: WriteLock, start_evt: threading.Event) -> list[float]:
    """Spawn a writer that waits for start_evt, then does one real commit
    through a LockedConnection sharing ``lock``. Returns a single-element list
    the thread fills with the commit's wall-clock seconds."""
    result: list[float] = []

    def _write() -> None:
        conn = sqlite3.connect(str(db), factory=LockedConnection, check_same_thread=False)
        conn.attach_lock(lock)
        try:
            start_evt.wait(timeout=10)
            t = time.monotonic()
            conn.execute(
                "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
                ("writer-probe", "concurrent writer", "2026-07-15T00:00:00+00:00"),
            )
            conn.commit()
            result.append(time.monotonic() - t)
        finally:
            conn.close()

    th = threading.Thread(target=_write, daemon=True)
    th.start()
    _timed_commit._threads.append(th)  # type: ignore[attr-defined]
    return result


_timed_commit._threads = []  # type: ignore[attr-defined]


def _join_writers() -> None:
    for th in _timed_commit._threads:  # type: ignore[attr-defined]
        th.join(timeout=30)
    _timed_commit._threads.clear()  # type: ignore[attr-defined]


# ── (a) slow Drive publish must not block writers ───────────────────


def test_slow_drive_publish_does_not_block_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED on current code: _publish_v2's copy2 to the Drive dir runs inside
    push()'s `with self.lock:` — a slow Drive write stalls every commit."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db)
    lock = WriteLock(db, enabled=True)
    s = SnapshotSync(db, drive, lock, min_interval=0.0, immutable=True)
    s.mark_durable_dirty()

    real_copy2 = shutil.copy2

    def slow_drive_copy2(src, dst, **kw):  # noqa: ANN001
        if str(drive) in str(dst):
            time.sleep(_INJECTED_DELAY_S)
        return real_copy2(src, dst, **kw)

    monkeypatch.setattr("memcp.core.snapshot_sync.shutil.copy2", slow_drive_copy2)

    start = threading.Event()
    timing = _timed_commit(db, lock, start)

    # Start the push, releasing the writer once the publish copy is underway.
    pushed: list[bool] = []
    push_th = threading.Thread(target=lambda: pushed.append(s.push(force=True)), daemon=True)
    push_th.start()
    time.sleep(0.5)  # push is now inside the slow publish copy
    start.set()
    push_th.join(timeout=30)
    _join_writers()

    assert pushed == [True]
    assert timing, "writer never completed"
    assert timing[0] < _WRITER_BUDGET_S, (
        f"writer commit blocked {timing[0]:.1f}s behind the Drive publish copy "
        f"(injected {_INJECTED_DELAY_S}s) — publish still runs under the write lock"
    )


# ── (a) slow GC blob reads must not block writers ───────────────────


def test_slow_gc_blob_reads_do_not_block_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED on current code: _gc_blobs (called from _publish_v2, inside the
    lock) content-verifies candidate blobs with full reads — a slow/evicted
    blob read stalls every commit. GC touches only Drive files and never the
    local DB, so it needs no write lock at all."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db)
    lock = WriteLock(db, enabled=True)
    s = SnapshotSync(db, drive, lock, min_interval=0.0, immutable=True)

    # Publish once so a below-floor candidate blob exists for the next push's
    # floor pass (the ledger floor advances with each publish here).
    s.mark_durable_dirty()
    assert s.push(force=True) is True
    _make_db(db, rows=5)  # durable change so the next push publishes
    s.mark_durable_dirty()

    real_id_sets = SnapshotSync._blob_id_sets

    def slow_id_sets(path):  # noqa: ANN001
        time.sleep(_INJECTED_DELAY_S / 2)  # pointer blob + 1 candidate = ~3s
        return real_id_sets(path)

    monkeypatch.setattr(SnapshotSync, "_blob_id_sets", staticmethod(slow_id_sets))

    start = threading.Event()
    timing = _timed_commit(db, lock, start)
    pushed: list[bool] = []
    push_th = threading.Thread(target=lambda: pushed.append(s.push(force=True)), daemon=True)
    push_th.start()
    time.sleep(0.5)
    start.set()
    push_th.join(timeout=30)
    _join_writers()

    assert pushed == [True]
    assert timing, "writer never completed"
    assert timing[0] < _WRITER_BUDGET_S, (
        f"writer commit blocked {timing[0]:.1f}s behind GC blob reads — "
        f"GC still runs under the write lock"
    )


# ── (b) slow catch-up validate already runs outside the lock — GUARD ─


def test_slow_catchup_remote_read_does_not_block_writers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUARD: catch-up's wire read of the remote blob runs BEFORE push takes
    the lock. Pin that so the fix never regresses it. (Seam updated with fix
    1: the wire read is now the staging shutil.copyfile — validation reads
    the local staged copy — so the delay is injected into the staging read.)"""
    drive = tmp_path / "drive"
    peer_db = tmp_path / "peer" / "graph.db"
    _make_db(peer_db, rows=5, prefix="peer")
    peer = SnapshotSync(peer_db, drive, WriteLock(peer_db, enabled=False), immutable=True)
    peer.mark_durable_dirty()
    assert peer.push(force=True) is True
    # Force the local pusher to see the peer's pointer as foreign-unseen.
    ptr_path = drive / _SNAPSHOT_PTR
    ptr = json.loads(ptr_path.read_text())
    ptr["host"] = "some-other-host.local"
    ptr_path.write_text(json.dumps(ptr))

    db = tmp_path / "b" / "graph.db"
    _make_db(db, rows=2, prefix="local")
    lock = WriteLock(db, enabled=True)
    s = SnapshotSync(db, drive, lock, min_interval=0.0, immutable=True)
    s.mark_durable_dirty()

    real_copyfile = shutil.copyfile

    def slow_remote_read(src, dst, **kw):  # noqa: ANN001
        if str(drive) in str(src):
            time.sleep(_INJECTED_DELAY_S)
        return real_copyfile(src, dst, **kw)

    monkeypatch.setattr("memcp.core.snapshot_sync.shutil.copyfile", slow_remote_read)

    start = threading.Event()
    timing = _timed_commit(db, lock, start)
    pushed: list[bool] = []
    push_th = threading.Thread(target=lambda: pushed.append(s.push(force=True)), daemon=True)
    push_th.start()
    time.sleep(0.5)  # push is inside the slow staging read now
    start.set()
    push_th.join(timeout=30)
    _join_writers()

    assert pushed == [True]
    assert timing, "writer never completed"
    assert timing[0] < _WRITER_BUDGET_S, (
        f"writer commit blocked {timing[0]:.1f}s behind the catch-up remote read — "
        f"the wire read must stay outside the write lock"
    )


# ── (2b) close must skip the peer fold and return fast ──────────────


def _foreign_pointer_setup(tmp_path: Path) -> Path:
    """Publish a peer snapshot and relabel its pointer as a foreign host's,
    so a local pusher sees an unmerged peer generation (the catch-up fold
    condition)."""
    drive = tmp_path / "drive"
    peer_db = tmp_path / "peer" / "graph.db"
    _make_db(peer_db, rows=5, prefix="peer")
    peer = SnapshotSync(peer_db, drive, WriteLock(peer_db, enabled=False), immutable=True)
    peer.mark_durable_dirty()
    assert peer.push(force=True) is True
    ptr_path = drive / _SNAPSHOT_PTR
    ptr = json.loads(ptr_path.read_text())
    ptr["host"] = "some-other-host.local"
    ptr_path.write_text(json.dumps(ptr))
    return drive


def test_close_skips_peer_fold_and_returns_fast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """RED on current code: stop()'s final flush runs push(force=True), whose
    catch-up validates the peer blob — the measured ~236s cold read — inline
    in the closing tool call. Option (b): with an unmerged peer generation
    pending, close must defer the flush entirely. Three invariants:
    fast return, the durable-dirty flag survives (the change publishes
    later), and NOTHING is published (a superset can't be guaranteed without
    the fold — no-loss)."""
    drive = _foreign_pointer_setup(tmp_path)
    ptr_before = (drive / _SNAPSHOT_PTR).read_text()

    db = tmp_path / "b" / "graph.db"
    _make_db(db, rows=2, prefix="local")
    lock = WriteLock(db, enabled=True)
    s = SnapshotSync(db, drive, lock, min_interval=0.0, immutable=True)
    s.mark_durable_dirty()

    real_valid = SnapshotSync._is_valid_sqlite

    def slow_valid(path):  # noqa: ANN001
        if str(drive) in str(path):
            time.sleep(_INJECTED_DELAY_S)
        return real_valid(path)

    monkeypatch.setattr(SnapshotSync, "_is_valid_sqlite", staticmethod(slow_valid))

    t = time.monotonic()
    s.stop()
    close_s = time.monotonic() - t

    assert close_s < _WRITER_BUDGET_S, (
        f"close blocked {close_s:.1f}s folding the peer snapshot "
        f"(injected {_INJECTED_DELAY_S}s) — the final flush still runs catch-up inline"
    )
    assert s._durable_dirty is True, (
        "close cleared _durable_dirty without publishing — the change would never propagate"
    )
    assert (drive / _SNAPSHOT_PTR).read_text() == ptr_before, (
        "close published despite an unmerged peer generation — "
        "non-superset publish (no-loss violation)"
    )


def test_close_final_flush_still_publishes_when_caught_up(tmp_path: Path) -> None:
    """GUARD (passes pre-fix, must keep passing): with no unmerged peer
    generation, close's final flush still publishes durable changes — the
    single-machine / caught-up behavior is unchanged."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db)
    lock = WriteLock(db, enabled=True)
    s = SnapshotSync(db, drive, lock, min_interval=0.0, immutable=True)
    s.mark_durable_dirty()

    s.stop()

    ptr_path = drive / _SNAPSHOT_PTR
    assert ptr_path.exists(), "caught-up close must still flush durable changes"
    assert s._durable_dirty is False


# ── failure ordering: publish failure must restore the dirty flag ───


def test_publish_failure_restores_durable_dirty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """GUARD (passes pre-fix, must keep passing): when the publish fails, the
    next tick must retry — _durable_dirty may not be lost. This is the
    handoff §2a failure-ordering invariant that moving publish outside the
    lock could silently break."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db)
    lock = WriteLock(db, enabled=True)
    s = SnapshotSync(db, drive, lock, min_interval=0.0, immutable=True)
    s.mark_durable_dirty()

    def broken_copy2(src, dst, **kw):  # noqa: ANN001
        raise OSError("simulated Drive failure")

    monkeypatch.setattr("memcp.core.snapshot_sync.shutil.copy2", broken_copy2)

    assert s.push(force=True) is False
    assert s._durable_dirty is True, (
        "publish failed but _durable_dirty was cleared — the change would never re-publish"
    )
