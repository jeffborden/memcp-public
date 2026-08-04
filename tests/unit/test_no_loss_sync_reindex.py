"""Step 5 — incremental reindex cuts on ingest_seq, not created_at (§3.4).

Merged rows carry their origin's OLDER created_at, so a `created_at > built_at`
cut skips them forever (invisible = looks like loss). They do carry a FRESH
local ingest_seq (assigned by the union backfill, step 5a), so an
`ingest_seq > built_against_seq` cut picks them up incrementally.
"""

from __future__ import annotations

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


def _insert_merged_row(node_id: str, content: str, old_created_at: str) -> None:
    """Simulate a row merged from a peer: OLD created_at, fresh local ingest_seq."""
    from memcp.core.node_store import NodeStore, _next_ingest_seq
    from memcp.core.revision import bump_revision

    store = NodeStore()
    try:
        conn = store._get_conn()
        seq = _next_ingest_seq(conn)
        conn.execute(
            "INSERT INTO nodes (id, content, project, created_at, ingest_seq) "
            "VALUES (?, ?, 'p', ?, ?)",
            (node_id, content, old_created_at, seq),
        )
        bump_revision(conn)  # the union bumps revision so _is_stale fires
        conn.commit()
    finally:
        store.close()


def test_incremental_edges_pick_up_merged_old_created_at_row() -> None:
    from memcp.core import memory
    from memcp.core.graph import GraphMemory
    from memcp.core.reindex import rebuild_edges

    GraphMemory()._get_conn()
    memory.remember("first local insight about graphs", category="fact", project="p")
    memory.remember("second local insight about nodes", category="fact", project="p")
    rebuild_edges(mode="full", force=True)

    # A merged row with a created_at far in the PAST but a fresh ingest_seq.
    _insert_merged_row(
        "mergedid", "a merged insight about edges and graphs", "2000-01-01T00:00:00+00:00"
    )

    result = rebuild_edges(mode="incremental", force=False)
    assert result["skipped"] is False
    assert result["items"] >= 1, "merged row (old created_at) must be reindexed via ingest_seq cut"

    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        edge_count = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            ("mergedid", "mergedid"),
        ).fetchone()[0]
    finally:
        graph.close()
    assert edge_count > 0, "merged row must have edges after incremental rebuild"


def test_incremental_entities_pick_up_merged_old_created_at_row() -> None:
    from memcp.core import memory
    from memcp.core.graph import GraphMemory
    from memcp.core.reindex import rebuild_entities

    GraphMemory()._get_conn()
    memory.remember("local insight one", category="fact", project="p")
    rebuild_entities(mode="full", force=True)

    _insert_merged_row(
        "mergedent",
        "merged content mentioning src/memcp/core/graph.py file",
        "2000-01-01T00:00:00+00:00",
    )

    result = rebuild_entities(mode="incremental", force=False)
    assert result["skipped"] is False
    assert result["items"] >= 1

    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        n = conn.execute(
            "SELECT COUNT(*) FROM entity_index WHERE node_id = ?", ("mergedent",)
        ).fetchone()[0]
    finally:
        graph.close()
    assert n > 0, "merged row entities must be indexed"
