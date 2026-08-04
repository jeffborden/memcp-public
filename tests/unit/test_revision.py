"""Tests for the revision counter and index metadata schema."""

from __future__ import annotations

from pathlib import Path

import pytest

from memcp.core import memory
from memcp.core.node_store import NodeStore
from memcp.core.revision import (
    bump_revision,
    get_index_meta,
    get_revision,
    invalidate_index,
    set_index_meta,
)


@pytest.fixture()
def store(tmp_path: Path) -> NodeStore:
    db = tmp_path / "graph.db"
    s = NodeStore(str(db))
    s._get_conn()  # trigger schema creation
    return s


def test_meta_table_created_with_revision_zero(store: NodeStore) -> None:
    conn = store._get_conn()
    row = conn.execute("SELECT value FROM meta WHERE key = 'revision'").fetchone()
    assert row is not None
    assert row["value"] == "0"


def test_index_meta_table_exists(store: NodeStore) -> None:
    conn = store._get_conn()
    # Should not raise
    conn.execute("SELECT index_name, built_against_revision FROM index_meta")


def test_get_revision_initial_value(store: NodeStore) -> None:
    conn = store._get_conn()
    assert get_revision(conn) == 0


def test_bump_revision_increments_monotonically(store: NodeStore) -> None:
    conn = store._get_conn()
    assert bump_revision(conn) == 1
    assert bump_revision(conn) == 2
    assert bump_revision(conn) == 3
    assert get_revision(conn) == 3


def test_get_index_meta_returns_none_for_unknown(store: NodeStore) -> None:
    conn = store._get_conn()
    assert get_index_meta(conn, "edges") is None


def test_set_and_get_index_meta_roundtrip(store: NodeStore) -> None:
    conn = store._get_conn()
    set_index_meta(
        conn,
        index_name="edges",
        built_against_revision=5,
        built_at="2026-04-20T12:00:00+00:00",
        model_version="regex-v1",
        built_against_seq=42,
    )
    meta = get_index_meta(conn, "edges")
    assert meta == {
        "index_name": "edges",
        "built_against_revision": 5,
        "built_at": "2026-04-20T12:00:00+00:00",
        "model_version": "regex-v1",
        "built_against_seq": 42,
    }


def test_set_index_meta_overwrites(store: NodeStore) -> None:
    conn = store._get_conn()
    set_index_meta(conn, "entities", 1, "t1", "v1")
    set_index_meta(conn, "entities", 2, "t2", "v2")
    meta = get_index_meta(conn, "entities")
    assert meta["built_against_revision"] == 2
    assert meta["built_at"] == "t2"
    assert meta["model_version"] == "v2"


def test_invalidate_index_removes_row(store: NodeStore) -> None:
    conn = store._get_conn()
    set_index_meta(conn, "edges", 5, "t", "v")
    assert get_index_meta(conn, "edges") is not None

    invalidate_index(conn, "edges")
    assert get_index_meta(conn, "edges") is None


def test_invalidate_index_noop_on_missing(store: NodeStore) -> None:
    conn = store._get_conn()
    # Should not raise on nonexistent index
    invalidate_index(conn, "does_not_exist")


def test_remember_bumps_revision(isolated_data_dir: Path) -> None:
    graph = _open_graph_for_tests()
    before = get_revision(graph._get_conn())
    memory.remember("Test insight", category="fact", importance="low")
    after = get_revision(graph._get_conn())
    assert after == before + 1


def test_forget_bumps_revision(isolated_data_dir: Path) -> None:
    # Ensure graph DB is created (eager init) before remember so graph backend is used
    graph = _open_graph_for_tests()
    graph._get_conn()  # trigger DB creation so _use_graph() returns True for remember

    result = memory.remember("To be forgotten", category="fact", importance="low")
    insight_id = result["id"]

    before = get_revision(graph._get_conn())
    memory.forget(insight_id)
    after = get_revision(graph._get_conn())
    assert after == before + 1


def _open_graph_for_tests():
    from memcp.core.graph import GraphMemory

    return GraphMemory()
