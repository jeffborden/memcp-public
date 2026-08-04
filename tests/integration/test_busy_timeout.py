"""SQLite busy timeout must survive a multi-second lock hold.

Observed: test_100_parallel_remembers failed with OperationalError
('database is locked') on runs where the embedding model cache was COLD —
the first semantic-edge call downloads/loads the model, and under 10
writer threads the GIL/network stall let one thread's open write
transaction outlive the previous 5s busy timeout. An adopter's first
session (cold cache, semantic extras installed per INSTALL.md) is exactly
that condition.

The cross-process write lock already waits config.write_lock_timeout
(default 30s); the in-process SQLite wait now matches it. This test pins
the behavior: a writer blocked by an 8s-held transaction must wait it
out, not error. Red-capable: with busy_timeout=5000 the second writer
raises 'database is locked' (verified before the fix); with the matched
timeout it succeeds.
"""

from __future__ import annotations

import sqlite3
import threading
import time

from memcp.config import get_config
from memcp.core.graph import GraphMemory

HOLD_SECONDS = 8.0  # > the old 5s busy_timeout, << the 30s write-lock wait


class TestBusyTimeoutSurvivesSlowWriter:
    def test_write_waits_out_an_8s_transaction_hold(self, isolated_data_dir):
        # Materialize the schema first so the holder has a table to write to.
        seed = GraphMemory()
        seed._get_conn()
        seed.close()

        txn_open = threading.Event()

        def hold_write_txn() -> None:
            # A raw connection opened IN this thread (sqlite3 thread
            # affinity), holding an open write transaction — simulates a
            # writer GIL-starved by cold model load mid-commit-window.
            conn = sqlite3.connect(str(get_config().graph_db_path))
            try:
                conn.execute("BEGIN IMMEDIATE")
                conn.execute(
                    "INSERT INTO nodes (id, content, project, session, created_at) "
                    "VALUES ('holder-node', 'held row', 'p', 's', '2026-01-01T00:00:00')"
                )
                txn_open.set()
                time.sleep(HOLD_SECONDS)
                conn.commit()
            finally:
                conn.close()

        t = threading.Thread(target=hold_write_txn)
        t.start()
        try:
            assert txn_open.wait(5), "holder thread never opened its transaction"

            # Second, independent connection: this write must WAIT, not error.
            writer = GraphMemory()
            start = time.monotonic()
            writer._node_store.store(
                {
                    "id": "waiting-node" + "0" * 52,
                    "content": "write issued while another txn holds the DB",
                }
            )
            waited = time.monotonic() - start
        finally:
            t.join()

        # It genuinely waited out the hold (i.e. the lock was real and this
        # test is capable of failing), and both rows landed.
        assert waited > HOLD_SECONDS * 0.5, (
            f"writer returned in {waited:.1f}s — the holder never actually "
            f"blocked it, so this test proved nothing"
        )
        conn = writer._node_store._get_conn()
        count = conn.execute(
            "SELECT COUNT(*) FROM nodes WHERE id IN "
            "('holder-node', 'waiting-node" + "0" * 52 + "')"
        ).fetchone()[0]
        writer.close()
        assert count == 2
