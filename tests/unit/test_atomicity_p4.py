"""Item 3 — P4 atomicity sweep.

Pins five atomicity invariants from the 2026-06-10 design review:

  5. reindex crash after the DELETE point never leaves the index empty while its
     meta still reads current (the documented-fixed-never-shipped DELETE-then-
     commit bug).
  6. update_node(entities=...) keeps entity_index == nodes.entities (the
     consolidation-drift divergence).
  7. an archived node generates zero entity edges from either rebuilder.
  8. an embedding-provider fallback flip (embedding -> None -> embedding) does
     NOT promote the edge rebuild to a full rebuild.
  9. store() crashing between the old node/entity commit points leaves the node
     either fully present with its entity rows or absent — never half.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from memcp.core import reindex
from memcp.core.edge_manager import EdgeManager
from memcp.core.fileutil import content_hash, estimate_tokens
from memcp.core.graph import GraphMemory
from memcp.core.node_store import NodeStore


def _insight(content: str, *, tags=None, entities=None, archived_at=None, idx=0) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": content_hash(content + str(idx) + now.isoformat()),
        "content": content,
        "summary": "",
        "category": "general",
        "importance": "medium",
        "effective_importance": 0.5,
        "tags": tags or [],
        "entities": entities if entities is not None else [],
        "project": "testproj",
        "session": "",
        "token_count": estimate_tokens(content),
        "access_count": 0,
        "last_accessed_at": None,
        "created_at": now.isoformat(),
        "archived_at": archived_at,
    }


# ── Test 5 — reindex crash after DELETE never leaves empty-while-current ──────


def test_reindex_crash_after_delete_keeps_index_recoverable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Seed two linked nodes and build a clean edge index + current meta.
    graph = GraphMemory()
    graph.store(_insight("sqlite graph storage backend", tags=["sqlite", "graph"], idx=1))
    graph.store(_insight("the graph storage uses sqlite", tags=["sqlite", "graph"], idx=2))
    graph.close()

    res = reindex.rebuild_edges(mode="full", force=True)
    assert res["items"] >= 2

    graph = GraphMemory()
    edges_before = graph._get_conn().execute("SELECT COUNT(*) FROM edges").fetchone()[0]
    graph.close()
    assert edges_before > 0  # otherwise the test is vacuous

    # Crash mid-rebuild, AFTER the DELETE FROM edges, by raising in generate_edges.
    def _boom(self, insight):  # noqa: ANN001, ANN202
        raise RuntimeError("simulated crash after DELETE")

    monkeypatch.setattr(EdgeManager, "generate_edges", _boom)
    with pytest.raises(RuntimeError):
        reindex.rebuild_edges(mode="full", force=True)
    monkeypatch.undo()

    # Reopen: the index must never be empty while its meta still reads current.
    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        edges_after = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        model_version = reindex._edges_model_version()
        stale, _reason = reindex._is_stale(conn, reindex._EDGES_INDEX, model_version)
        assert not (edges_after == 0 and not stale), (
            "edge index is empty while meta reads current — silent empty-index bug"
        )
    finally:
        graph.close()


# ── Test 6 — update_node(entities=...) keeps entity_index in sync ─────────────


def test_update_node_entities_maintains_entity_index() -> None:
    store = NodeStore()
    try:
        ins = _insight("alpha node", entities=["alpha"], idx=1)
        store.store(ins)
        nid = ins["id"]

        assert store.update_node(nid, {"entities": ["beta", "gamma"]}) is True

        node = store.get_node(nid)
        assert set(node["entities"]) == {"beta", "gamma"}

        rows = (
            store._get_conn()
            .execute("SELECT entity FROM entity_index WHERE node_id = ?", (nid,))
            .fetchall()
        )
        index_entities = {r["entity"] for r in rows}
        assert index_entities == {"beta", "gamma"}, (
            "entity_index diverged from nodes.entities after update_node"
        )
    finally:
        store.close()


# ── Test 7 — archived node generates zero entity edges from either rebuilder ──


def test_archived_node_generates_no_entity_edges() -> None:
    # Store via NodeStore (no auto edge generation) so the rebuilders are the
    # only thing that could create edges.
    store = NodeStore()
    live = _insight("shared work on SharedEntity", entities=["SharedEntity"], idx=1)
    archived = _insight(
        "archived work on SharedEntity",
        entities=["SharedEntity"],
        archived_at="2026-01-01T00:00:00+00:00",
        idx=2,
    )
    store.store(live)
    store.store(archived)
    store.close()

    reindex.rebuild_entities(mode="full", force=True)
    reindex.rebuild_edges(mode="full", force=True)

    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        entity_edges = conn.execute(
            "SELECT source_id, target_id FROM edges WHERE edge_type = 'entity'"
        ).fetchall()
        for e in entity_edges:
            assert archived["id"] not in (e["source_id"], e["target_id"]), (
                "archived node appears in an entity edge"
            )
        idx_rows = conn.execute(
            "SELECT 1 FROM entity_index WHERE node_id = ?", (archived["id"],)
        ).fetchall()
        assert idx_rows == [], "archived node was written into entity_index"
    finally:
        graph.close()


# ── Test 8 — provider fallback flip does not force a full rebuild ─────────────


def test_provider_fallback_flip_no_full_rebuild(monkeypatch: pytest.MonkeyPatch) -> None:
    graph = GraphMemory()
    graph.store(_insight("a node for the edge rebuild", tags=["x"], idx=1))
    graph.store(_insight("another node for the rebuild", tags=["x"], idx=2))
    graph.close()

    # Session 1: a real embedding model is present.
    monkeypatch.setattr(reindex, "_current_embedding_model_version", lambda: "stmodel-v1")
    reindex.rebuild_edges(mode="incremental", force=True)

    # Session 2: provider temporarily unavailable -> keyword-fallback provenance.
    monkeypatch.setattr(reindex, "_current_embedding_model_version", lambda: None)
    res_down = reindex.rebuild_edges(mode="incremental", force=False)
    assert res_down["skipped"] is False  # model_version changed -> it rebuilds
    assert res_down["mode"] != "full", "fallback transition wrongly promoted to full rebuild"

    # Session 3: provider returns — must not force a full rebuild either.
    monkeypatch.setattr(reindex, "_current_embedding_model_version", lambda: "stmodel-v1")
    res_up = reindex.rebuild_edges(mode="incremental", force=False)
    assert res_up["mode"] != "full", "fallback recovery wrongly promoted to full rebuild"

    # A genuine model change still promotes to full.
    monkeypatch.setattr(reindex, "_current_embedding_model_version", lambda: "stmodel-v2")
    res_real = reindex.rebuild_edges(mode="incremental", force=False)
    assert res_real["mode"] == "full", "a genuine model change must promote to full"


# ── Test 9 — store() crash between commit points leaves no half-written node ──


def test_store_crash_between_commit_points_no_half_node() -> None:
    store = NodeStore()
    conn = store._get_conn()
    ins = _insight("node with entities", entities=["myentity"], idx=1)
    nid = ins["id"]

    orig_execute = conn.execute

    def _patched(sql, *args, **kwargs):  # noqa: ANN001, ANN202
        if "entity_index" in sql and "INSERT" in sql:
            raise RuntimeError("simulated crash between node and entity commits")
        return orig_execute(sql, *args, **kwargs)

    conn.execute = _patched  # type: ignore[method-assign]
    with pytest.raises(RuntimeError):
        store.store(ins)
    conn.execute = orig_execute  # type: ignore[method-assign]
    store.close()

    # Reopen: the node is either fully present with its entity rows, or absent.
    store2 = NodeStore()
    try:
        node = store2.get_node(nid)
        ent_rows = (
            store2._get_conn()
            .execute("SELECT entity FROM entity_index WHERE node_id = ?", (nid,))
            .fetchall()
        )
        assert node is None or len(ent_rows) == 1, (
            "node was committed without its entity rows — half-written"
        )
        # The specific bug: present node with zero entity rows.
        assert not (node is not None and len(ent_rows) == 0)
    finally:
        store2.close()
