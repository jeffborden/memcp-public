"""Tests for the write-grained SQLite lock (write_lock.py).

Covers the local flock tier (cross-process mutual exclusion, exercised here
across threads since flock contends on separate open file descriptions),
reentrancy, fail-safe release, the cross-machine lease (stale reclaim, foreign
fresh lease handling), and end-to-end behaviour through NodeStore.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest

from memcp.core.write_lock import WriteLock


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep flock files inside the test's tmp dir, not ~/.cache."""
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))


def _make_lock(tmp_path: Path, **kw) -> WriteLock:
    return WriteLock(tmp_path / "graph.db", **kw)


# ── local flock tier ──────────────────────────────────────────────


def test_flock_serializes_concurrent_writers(tmp_path: Path) -> None:
    """Two independent locks on the same db must not hold simultaneously."""
    overlaps = 0
    inside = 0
    lock_state = threading.Lock()

    def worker() -> None:
        nonlocal overlaps, inside
        wl = _make_lock(tmp_path)
        for _ in range(20):
            with wl:
                with lock_state:
                    inside += 1
                    if inside > 1:
                        overlaps += 1
                time.sleep(0.001)  # widen the window for a race to show
                with lock_state:
                    inside -= 1

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert overlaps == 0, f"critical section was entered concurrently {overlaps}x"


def test_disabled_lock_is_noop(tmp_path: Path) -> None:
    wl = _make_lock(tmp_path, enabled=False)
    with wl:
        pass
    # No flock file dir, no lease created.
    assert not (tmp_path / "graph.db").with_name(".writer.lock").exists()


# ── reentrancy + fail-safe release ────────────────────────────────


def test_reentrant_acquire(tmp_path: Path) -> None:
    wl = _make_lock(tmp_path)
    with wl:
        with wl:  # nested — must not deadlock or double-claim
            assert wl._depth == 2
        assert wl._depth == 1
    assert wl._depth == 0


def test_lock_released_on_exception(tmp_path: Path) -> None:
    wl = _make_lock(tmp_path)
    with pytest.raises(RuntimeError), wl:
        raise RuntimeError("boom")
    assert wl._depth == 0
    # Re-acquirable afterwards (flock + lease were released).
    with wl:
        assert wl._depth == 1


# ── cross-machine lease tier ──────────────────────────────────────


def test_lease_written_and_removed(tmp_path: Path) -> None:
    wl = _make_lock(tmp_path)
    lease_path = (tmp_path / "graph.db").with_name(".writer.lock")
    with wl:
        assert lease_path.exists()
        data = json.loads(lease_path.read_text())
        assert data["pid"] == wl._pid
        assert data["host"] == wl._host
    assert not lease_path.exists()


def test_stale_lease_is_reclaimed_fast(tmp_path: Path) -> None:
    lease_path = (tmp_path / "graph.db").with_name(".writer.lock")
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    # Foreign lease with an ancient heartbeat → stale.
    lease_path.write_text(
        json.dumps(
            {
                "host": "other-machine",
                "pid": 99999,
                "acquired_at": 0,
                "heartbeat_at": 0,
            }
        )
    )
    wl = _make_lock(tmp_path, lease_ttl=60, timeout=5)
    start = time.time()
    with wl:
        pass
    assert time.time() - start < 1.0, "stale lease should be reclaimed without waiting"


def test_foreign_fresh_lease_blocks_then_reclaims(tmp_path: Path) -> None:
    lease_path = (tmp_path / "graph.db").with_name(".writer.lock")
    lease_path.parent.mkdir(parents=True, exist_ok=True)
    lease_path.write_text(
        json.dumps(
            {
                "host": "other-machine",
                "pid": 99999,
                "acquired_at": time.time(),
                "heartbeat_at": time.time(),  # fresh
            }
        )
    )
    wl = _make_lock(tmp_path, lease_ttl=600, timeout=0.5, poll=0.05)
    start = time.time()
    with wl:  # should block ~timeout then reclaim (assume crashed holder)
        pass
    elapsed = time.time() - start
    assert 0.4 < elapsed < 3.0, f"expected ~timeout block, got {elapsed:.2f}s"


# ── end-to-end through NodeStore ──────────────────────────────────


def test_nodestore_uses_delete_journal_and_no_wal_sidecar(isolated_data_dir: Path) -> None:
    from memcp.config import get_config
    from memcp.core.node_store import LockedConnection, NodeStore

    get_config().ensure_dirs()
    store = NodeStore()
    conn = store._get_conn()
    try:
        assert isinstance(conn, LockedConnection)
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
        assert mode.lower() == "delete"

        store.store(
            {
                "id": "wl-test-1",
                "content": "write-lock integration check",
                "category": "general",
                "importance": "low",
            }
        )
        assert store.get_node("wl-test-1") is not None
    finally:
        store.close()

    db_path = get_config().graph_db_path
    assert db_path.exists()
    assert not db_path.with_name(db_path.name + "-wal").exists()


def test_nodestore_persists_across_reopen(isolated_data_dir: Path) -> None:
    from memcp.config import get_config
    from memcp.core.node_store import NodeStore

    get_config().ensure_dirs()
    store = NodeStore()
    store.store({"id": "persist-1", "content": "durable", "category": "fact", "importance": "high"})
    store.close()

    store2 = NodeStore()
    try:
        assert store2.get_node("persist-1") is not None
    finally:
        store2.close()
