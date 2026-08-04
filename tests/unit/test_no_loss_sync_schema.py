"""Step 2 — additive schema migration for the no-loss sync redesign.

Adds the substrate the merge core (steps 3-6) builds on, all backward-compatible:
- a ``tombstones(id, deleted_at, resurrected_at)`` table (§3.5)
- a monotonic, unique ``nodes.ingest_seq`` allocator (§3.4)
- a synced ``nodes.archived_at`` soft-state column (§3.5 archive fix)

Id-widening (§3.9) is intentionally NOT here — the spec couples it with the
§3.7 bootstrap migrate (step 7) to avoid a migrate-time id-collision orphan.

See docs/superpowers/specs/2026-06-01-no-loss-merge-sync-design.md.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import memcp.config as config_module


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MEMCP_SNAPSHOT_DIR", raising=False)
    config_module._config = None
    yield
    config_module._config = None


def _open_store():  # noqa: ANN202
    from memcp.core.node_store import NodeStore

    store = NodeStore()
    store._get_conn()
    return store


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return (
        conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        is not None
    )


# ── tombstones table (§3.5) ───────────────────────────────────────────


def test_tombstones_table_created() -> None:
    store = _open_store()
    try:
        conn = store._get_conn()
        assert _table_exists(conn, "tombstones")
        cols = _columns(conn, "tombstones")
        assert {"id", "deleted_at", "resurrected_at"} <= cols
    finally:
        store.close()


# ── archived_at column (§3.5) ─────────────────────────────────────────


def test_nodes_has_archived_at_column() -> None:
    store = _open_store()
    try:
        assert "archived_at" in _columns(store._get_conn(), "nodes")
    finally:
        store.close()


# ── ingest_seq allocator (§3.4) ───────────────────────────────────────


def test_nodes_has_ingest_seq_column() -> None:
    store = _open_store()
    try:
        assert "ingest_seq" in _columns(store._get_conn(), "nodes")
    finally:
        store.close()


def test_store_allocates_monotonic_unique_ingest_seq() -> None:
    store = _open_store()
    try:
        for i in range(4):
            store.store(
                {
                    "id": f"id{i}",
                    "content": f"content {i}",
                    "created_at": f"2026-01-0{i + 1}T00:00:00+00:00",
                }
            )
        conn = store._get_conn()
        seqs = [
            r[0]
            for r in conn.execute("SELECT ingest_seq FROM nodes ORDER BY ingest_seq").fetchall()
        ]
        assert all(s is not None for s in seqs), "every stored node gets an ingest_seq"
        assert seqs == sorted(set(seqs)), "ingest_seq must be strictly increasing + unique"
        assert len(seqs) == 4
    finally:
        store.close()


# ── id widening (§3.9) ────────────────────────────────────────────────


def test_insight_id_is_full_sha256() -> None:
    from memcp.core.fileutil import insight_id

    h = insight_id("some content", "2026-01-01T00:00:00+00:00")
    assert len(h) == 64, "insight ids must be full sha256 (256-bit), not truncated"
    assert all(c in "0123456789abcdef" for c in h)


def test_insight_id_deterministic_and_varies() -> None:
    from memcp.core.fileutil import insight_id

    a = insight_id("content", "2026-01-01T00:00:00+00:00")
    b = insight_id("content", "2026-01-01T00:00:00+00:00")
    c = insight_id("content", "2026-01-02T00:00:00+00:00")
    d = insight_id("other", "2026-01-01T00:00:00+00:00")
    assert a == b
    assert a != c and a != d


def test_remember_mints_wide_id() -> None:
    from memcp.core.memory import remember

    _open_store()  # materialize graph.db so remember takes the graph path
    ins = remember("a brand new insight with a wide id", project="p")
    assert len(ins["id"]) == 64, "new insight ids must be full sha256"


def test_ingest_seq_backfilled_for_pre_existing_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Opening an old DB (no ingest_seq) backfills existing rows + continues above max."""
    db = tmp_path / "data" / "graph.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    # Build a legacy-shape nodes table without ingest_seq/archived_at.
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE nodes (
            id TEXT PRIMARY KEY, content TEXT NOT NULL, summary TEXT DEFAULT '',
            category TEXT DEFAULT 'general', importance TEXT DEFAULT 'medium',
            effective_importance REAL DEFAULT 0.5, tags TEXT DEFAULT '[]',
            entities TEXT DEFAULT '[]', project TEXT DEFAULT 'default',
            session TEXT DEFAULT '', token_count INTEGER DEFAULT 0,
            access_count INTEGER DEFAULT 0, last_accessed_at TEXT, created_at TEXT NOT NULL
        )"""
    )
    for i in range(3):
        conn.execute(
            "INSERT INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            (f"old{i}", f"legacy {i}", f"2025-12-0{i + 1}T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()

    from memcp.core.node_store import NodeStore

    store = NodeStore()
    try:
        conn = store._get_conn()  # triggers _migrate_schema
        seqs = [r[0] for r in conn.execute("SELECT ingest_seq FROM nodes").fetchall()]
        assert all(s is not None for s in seqs), "legacy rows must be backfilled"
        assert len(set(seqs)) == 3, "backfilled seqs must be distinct"

        store.store({"id": "new", "content": "fresh", "created_at": "2026-06-01T00:00:00+00:00"})
        new_seq = conn.execute("SELECT ingest_seq FROM nodes WHERE id = 'new'").fetchone()[0]
        assert new_seq > max(seqs), "allocator must continue above the backfilled max"
    finally:
        store.close()
