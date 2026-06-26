"""Item 4 — P5: fail-closed local write lock + thread-safe _get_conn.

10. When the LOCAL flock tier can't be acquired (e.g. its lock dir is
    uncreatable), a write retries a bounded number of times (~600ms) and then
    RAISES a counted error surfaced via status — it does NOT silently fall
    open and write unserialized. (The cross-machine lease tier stays
    fail-open; only the local tier is fail-closed.)
11. Four threads racing the first _get_conn() create exactly ONE connection
    and ONE flusher thread (the double-checked-init race).
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest

import memcp.config as config_module
from memcp.core import node_store as ns_mod
from memcp.core import write_lock as wl
from memcp.core.fileutil import content_hash, estimate_tokens
from memcp.core.node_store import NodeStore


def _insight(content: str, idx: int = 0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": content_hash(content + str(idx) + now.isoformat()),
        "content": content,
        "summary": "",
        "category": "general",
        "importance": "medium",
        "effective_importance": 0.5,
        "tags": [],
        "entities": [],
        "project": "testproj",
        "session": "",
        "token_count": estimate_tokens(content),
        "access_count": 0,
        "last_accessed_at": None,
        "created_at": now.isoformat(),
        "archived_at": None,
    }


# ── Test 10 — local lock failure fails closed, counted, surfaced in status ────


def test_local_lock_failure_fails_closed_and_counts(monkeypatch: pytest.MonkeyPatch) -> None:
    store = NodeStore()
    store._get_conn()  # establish the schema/connection while the lock works
    store.store(_insight("baseline insight", idx=0))  # baseline write succeeds

    before = wl.local_lock_failure_count()

    # Simulate the local flock dir being uncreatable: every flock attempt fails.
    def _boom(self) -> None:  # noqa: ANN001
        raise OSError("lock dir uncreatable")

    monkeypatch.setattr(wl.WriteLock, "_acquire_flock", _boom)

    t0 = time.monotonic()
    with pytest.raises(wl.WriteLockError):
        store.store(_insight("must not write unserialized", idx=1))
    elapsed = time.monotonic() - t0

    # Bounded retry (3 x 200ms) before giving up — not an instant fail, not a
    # silent fail-open.
    assert elapsed >= 0.5, f"expected ~600ms of retries, got {elapsed:.3f}s"
    assert elapsed < 3.0
    assert wl.local_lock_failure_count() == before + 1

    # The lock object is not wedged after a fail-closed acquire.
    monkeypatch.undo()

    # status surfaces the counted failure.
    from memcp.core.memory import memory_status

    st = memory_status()
    assert st["write_lock"]["local_lock_failures"] >= 1

    store.close()


def test_lease_tier_stays_fail_open(monkeypatch: pytest.MonkeyPatch) -> None:
    # The cross-machine lease tier is best-effort: a failure there must NOT raise
    # (only the local flock tier is fail-closed).
    lock = wl.WriteLock("/tmp/memcp-test-lease.db", enabled=True)

    def _lease_boom(self) -> None:  # noqa: ANN001
        raise OSError("synced mount unavailable")

    monkeypatch.setattr(wl.WriteLock, "_acquire_lease", _lease_boom)
    # Should acquire (flock succeeds) and swallow the lease error.
    lock.acquire()
    lock.release()


# ── Test 11 — concurrent first-call init creates one connection + one flusher ─


def _count_flusher_threads() -> int:
    return sum(1 for t in threading.enumerate() if t.name == "memcp-snapshot" and t.is_alive())


def test_concurrent_first_call_single_connection_and_flusher(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    # A snapshot dir (a throwaway tmp dir, NOT a protected one) so the flusher
    # path runs and we can count flusher threads.
    snap = tmp_path / "snap"
    snap.mkdir()
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(snap))
    config_module._config = None

    connect_calls = {"n": 0}
    real_connect = ns_mod.sqlite3.connect

    def counting_connect(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        connect_calls["n"] += 1
        # Test-only: allow the main thread to close a conn a worker created.
        kwargs.setdefault("check_same_thread", False)
        return real_connect(*args, **kwargs)

    monkeypatch.setattr(ns_mod.sqlite3, "connect", counting_connect)

    store = NodeStore()
    flushers_before = _count_flusher_threads()

    results: list[int] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()  # release all four at once to maximize the race
        results.append(id(store._get_conn()))

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    try:
        assert connect_calls["n"] == 1, f"expected one connection, got {connect_calls['n']}"
        assert len(set(results)) == 1, "threads observed different connection objects"
        assert _count_flusher_threads() - flushers_before == 1, "expected exactly one flusher"
    finally:
        store.close()
