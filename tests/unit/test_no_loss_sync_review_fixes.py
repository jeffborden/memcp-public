"""Fixes from the /code-review pass on the no-loss sync diff."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

import memcp.config as config_module


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MEMCP_SNAPSHOT_DIR", raising=False)
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    config_module._config = None
    yield
    config_module._config = None


# ── Fix 1: projection hash ignores the per-machine ingest_seq ──────────


def _nodes_db_with_seq(path: Path, seq: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, content TEXT, created_at TEXT, "
        "access_count INTEGER, ingest_seq INTEGER)"
    )
    conn.execute(
        "INSERT INTO nodes (id, content, created_at, access_count, ingest_seq) "
        "VALUES ('n1', 'hello', '2026-01-01', 0, ?)",
        (seq,),
    )
    conn.commit()
    conn.close()


def test_projection_hash_ignores_ingest_seq(tmp_path: Path) -> None:
    """Two machines with identical durable content but different local ingest_seq
    must compute the SAME projection hash (else the snapshot generation pings
    back and forth forever)."""
    from memcp.core.snapshot_sync import SnapshotSync

    a = tmp_path / "a.db"
    b = tmp_path / "b.db"
    _nodes_db_with_seq(a, 5)
    _nodes_db_with_seq(b, 999)
    assert SnapshotSync._projection_hash(a) == SnapshotSync._projection_hash(b)


# ── Fix 2: archived rows excluded from index + status ─────────────────


def test_archived_excluded_from_index_and_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(tmp_path / "snap"))
    config_module._config = None

    from memcp.core.memory import generate_index, memory_status, remember
    from memcp.core.node_store import NodeStore
    from memcp.core.retention import archive_insight

    NodeStore()._get_conn()
    keep = remember("a visible kept insight about alpha", project="p")
    gone = remember("an archived insight about beta", project="p")
    archive_insight(gone["id"])

    idx = generate_index(project="p")
    assert "alpha" in idx
    assert gone["id"][:8] not in idx, "archived insight must not appear in the index"

    status = memory_status(project="p")
    assert status["total_insights"] == 1, "archived row must not be counted in status"
    assert keep["id"]  # sanity


# ── Fix 3: malformed memory.json row doesn't wedge startup ────────────


def test_migrate_skips_idless_row_without_crashing() -> None:
    from memcp.config import get_config
    from memcp.core.node_store import NodeStore

    cfg = get_config()
    cfg.ensure_dirs()
    cfg.memory_path.write_text(
        json.dumps(
            {
                "insights": [
                    {"content": "id-less malformed row"},  # no id
                    {
                        "id": "good1",
                        "content": "a good row",
                        "created_at": "2025-01-01T00:00:00+00:00",
                    },
                ]
            }
        )
    )

    store = NodeStore()
    try:
        conn = store._get_conn()  # must NOT raise
        ids = {r[0] for r in conn.execute("SELECT id FROM nodes").fetchall()}
    finally:
        store.close()
    assert "good1" in ids, "the well-formed row must migrate"
    assert not cfg.memory_path.exists(), "migration must complete + rename memory.json"


# ── Fix 4: a model_version change forces a full rebuild ───────────────


def test_model_version_change_forces_full_rebuild() -> None:
    from memcp.core import memory
    from memcp.core.graph import GraphMemory
    from memcp.core.reindex import rebuild_edges
    from memcp.core.revision import set_index_meta

    GraphMemory()._get_conn()
    memory.remember("first insight about graphs and edges", category="fact", project="p")
    memory.remember("second insight about nodes and stores", category="fact", project="p")
    rebuild_edges(mode="full", force=True)

    # Simulate edges built under a DIFFERENT (stale) model version.
    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        meta_before = conn.execute(
            "SELECT model_version FROM index_meta WHERE index_name='edges'"
        ).fetchone()[0]
        set_index_meta(
            conn,
            index_name="edges",
            built_against_revision=0,
            built_at="2020-01-01T00:00:00+00:00",
            model_version="some-old-model",
            built_against_seq=999,
        )
        conn.commit()
        node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        graph.close()

    # Incremental requested, but the model changed → must full-rebuild ALL nodes,
    # not just rows past the (bogus 999) seq cut.
    result = rebuild_edges(mode="incremental", force=False)
    assert result["skipped"] is False
    assert result["items"] == node_count, "model change must reprocess every node (full rebuild)"
    assert meta_before  # sanity
