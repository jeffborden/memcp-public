"""Regression + convergence tests for the non-blocking startup pull.

Task 0 (RED):  proved NodeStore._get_conn() called pull_if_newer() synchronously.
Task 1 (GREEN): _get_conn() defers the pull (sets pull_pending=True for established machines).
Task 2 (GREEN): _flush_loop() performs the pull on its first tick, restoring cross-machine sync.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

import pytest

import memcp.config as config_module
from memcp.core.snapshot_sync import SnapshotSync
from memcp.core.write_lock import WriteLock

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_SLOW_DELAY = 5.0  # seconds the fake Drive read sleeps
_FAST_THRESHOLD = 1.0  # first _get_conn() must return in under this many seconds


# ---------------------------------------------------------------------------
# Low-level snapshot helpers (mirrors test_snapshot_sync.py)
# ---------------------------------------------------------------------------


def _make_proper_db(path: Path) -> None:
    """Create a NodeStore-schema DB with a seed row.

    Uses _SCHEMA from node_store so the DB is compatible with NodeStore._get_conn().
    """
    from memcp.core.node_store import _SCHEMA

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    now = "2026-01-01T00:00:00+00:00"
    conn.execute(
        "INSERT INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
        ("seed-row-1", "seed content", now),
    )
    conn.commit()
    conn.close()


# ---------------------------------------------------------------------------
# Test
# ---------------------------------------------------------------------------


def test_first_db_access_does_not_block_on_slow_snapshot_pull(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """On the current branch this test FAILS because pull_if_newer() blocks.

    Scene: established machine B has a non-empty local DB (properly seeded),
    the snapshot dir contains a newer generation-1 snapshot (v2 immutable
    blob, pushed by machine A with MEMCP_SNAPSHOT_IMMUTABLE=true so the pull
    path exercises _resolve_pointer_blob — the real blocking call), and that
    Drive read is artificially slowed to 5 s.  The first call to
    NodeStore._get_conn() must return in well under 1 s on a correctly
    non-blocking implementation.
    """
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))

    drive = tmp_path / "drive"
    data_b = tmp_path / "machine_b"

    # -- Machine A: push a v2 (immutable-blob) generation-1 snapshot -----------
    # MEMCP_SNAPSHOT_IMMUTABLE=true makes push() write an immutable blob + pointer
    # (graph.snapshot.ptr.json), so pull_if_newer() calls _pull_v2() which calls
    # _resolve_pointer_blob — the actual Drive-reading bottleneck.
    monkeypatch.setenv("MEMCP_SNAPSHOT_IMMUTABLE", "true")
    db_a = tmp_path / "machine_a" / "graph.db"
    _make_proper_db(db_a)
    sa = SnapshotSync(db_a, drive, WriteLock(db_a), min_interval=0.0)
    sa.mark_dirty()
    sa.push(force=True)
    # Sanity: pointer exists and generation is 1.
    assert sa.snapshot_ptr.exists(), "v2 pointer must exist after immutable push"
    assert sa._remote_generation() == 1
    # Done with machine A; clear the immutable flag so it doesn't affect machine B.
    monkeypatch.delenv("MEMCP_SNAPSHOT_IMMUTABLE", raising=False)

    # -- Machine B: seed a proper local DB that has NOT seen gen 1 yet ---------
    db_b = data_b / "graph.db"
    _make_proper_db(db_b)
    # No .sync_meta.json in data_b → local known generation = 0 →
    # remote_gen (1) > local_gen (0) → pull will be attempted.

    # -- Wire the slow-Drive monkeypatch BEFORE opening NodeStore ---------------
    # _resolve_pointer_blob reads the blob from "Drive" — the real slow I/O.
    real_resolve = SnapshotSync._resolve_pointer_blob

    def slow_resolve(self: SnapshotSync, ptr: dict, remote_gen: int) -> Path | None:
        time.sleep(_SLOW_DELAY)
        return real_resolve(self, ptr, remote_gen)

    # _sweep_orphan_blobs also does Drive reads on startup.
    real_sweep = SnapshotSync._sweep_orphan_blobs

    def slow_sweep(self: SnapshotSync) -> bool:
        time.sleep(_SLOW_DELAY)
        return real_sweep(self)

    monkeypatch.setattr(SnapshotSync, "_resolve_pointer_blob", slow_resolve)
    monkeypatch.setattr(SnapshotSync, "_sweep_orphan_blobs", slow_sweep)

    # -- Configure NodeStore to use machine B's data dir + the shared drive dir -
    # (conftest isolated_data_dir autouse already set MEMCP_DATA_DIR; override it)
    monkeypatch.setenv("MEMCP_DATA_DIR", str(data_b))
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(drive))
    config_module._config = None  # bust the config singleton so our new env vars apply

    from memcp.core.node_store import NodeStore

    store = NodeStore()
    try:
        t0 = time.perf_counter()
        store._get_conn()
        elapsed = time.perf_counter() - t0
    finally:
        store.close()
        config_module._config = None  # clean up for subsequent tests

    assert elapsed < _FAST_THRESHOLD, (
        f"NodeStore._get_conn() blocked for {elapsed:.2f}s — "
        f"pull_if_newer() is still synchronous in the request path "
        f"(expected < {_FAST_THRESHOLD}s once the pull is deferred to the background)"
    )


# ---------------------------------------------------------------------------
# Task 2 convergence: flusher tick folds a peer's row
# ---------------------------------------------------------------------------


def test_flusher_pull_folds_peer_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prove that machine B's established DB picks up machine A's row via the
    flusher tick (i.e. via pull_if_newer(), which is exactly what _flush_loop
    calls on every tick).

    The test is deterministic — it calls pull_if_newer() directly rather than
    sleeping against a real daemon thread, which lets it run fast and avoid
    race conditions while still exercising the exact code path the flusher uses.

    Scene:
      - Machine A pushes a v2 (immutable-blob) snapshot containing a unique row.
      - Machine B has its own established local DB (seed row only, has NOT seen A's row).
      - We confirm B does NOT yet have A's row.
      - We simulate one flusher tick: call pull_if_newer() on B's SnapshotSync.
      - We assert B now has A's row (additive union succeeded).
    """
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("MEMCP_SNAPSHOT_IMMUTABLE", "true")

    drive = tmp_path / "drive"

    # -- Machine A: push a snapshot containing a unique peer row ----------------
    db_a = tmp_path / "machine_a" / "graph.db"
    _make_proper_db(db_a)
    # Insert a row that machine B should not have yet.
    conn_a = sqlite3.connect(str(db_a))
    conn_a.execute(
        "INSERT INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
        ("peer-row-from-a", "data from machine A", "2026-01-02T00:00:00+00:00"),
    )
    conn_a.commit()
    conn_a.close()

    sa = SnapshotSync(db_a, drive, WriteLock(db_a), min_interval=0.0)
    sa.mark_dirty()
    sa.push(force=True)
    assert sa.snapshot_ptr.exists(), "v2 pointer must exist after immutable push"

    monkeypatch.delenv("MEMCP_SNAPSHOT_IMMUTABLE", raising=False)

    # -- Machine B: established local DB with only the seed row -----------------
    db_b = tmp_path / "machine_b" / "graph.db"
    _make_proper_db(db_b)

    sb = SnapshotSync(db_b, drive, WriteLock(db_b), min_interval=0.0)

    # Confirm B does NOT have A's row before the pull.
    conn_b = sqlite3.connect(str(db_b))
    rows_before = [r[0] for r in conn_b.execute("SELECT id FROM nodes").fetchall()]
    conn_b.close()
    assert "peer-row-from-a" not in rows_before, "Machine B should not have A's row before any pull"

    # Simulate a single flusher tick: pull_if_newer() is exactly what
    # _flush_loop calls on every pass (including the immediate first tick).
    sb.pull_pending = False
    sb.pull_if_newer()

    # B should now contain A's row (additive union path — local DB exists).
    conn_b2 = sqlite3.connect(str(db_b))
    rows_after = [r[0] for r in conn_b2.execute("SELECT id FROM nodes").fetchall()]
    conn_b2.close()
    assert "peer-row-from-a" in rows_after, (
        f"Machine B's DB should contain A's row after pull_if_newer() tick; "
        f"found rows: {rows_after}"
    )
