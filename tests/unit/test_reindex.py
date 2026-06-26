"""Tests for memcp_reindex — rebuild derived indexes from the node store."""

from __future__ import annotations

from pathlib import Path

import pytest

from memcp.core import memory
from memcp.core.graph import GraphMemory
from memcp.core.reindex import (
    rebuild_all,
    rebuild_edges,
    rebuild_embeddings,
    rebuild_entities,
)
from memcp.core.revision import get_index_meta, get_revision
from memcp.core.vecstore import NUMPY_AVAILABLE


@pytest.fixture()
def seeded_graph(isolated_data_dir: Path):
    # Force graph backend by pre-creating the graph DB
    graph = GraphMemory()
    graph._get_conn()
    # Content chosen to include entity-regex matches (file path, URL, CamelCase)
    memory.remember(
        "Implemented GraphMemory in src/memcp/core/graph.py",
        category="fact",
        importance="low",
    )
    memory.remember(
        "See https://example.com/docs for the NodeStore design rationale",
        category="fact",
        importance="low",
    )
    memory.remember(
        "Decided to use GraphMemory because of the MAGMA 4-graph pattern",
        category="decision",
        importance="medium",
    )
    return GraphMemory()


def test_rebuild_edges_full_populates_edges_table(seeded_graph: GraphMemory) -> None:
    conn = seeded_graph._get_conn()
    conn.execute("DELETE FROM edges")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0

    result = rebuild_edges(mode="full", force=True)
    assert result["items"] == 3
    assert result["mode"] == "full"
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] > 0


def test_rebuild_edges_updates_index_meta(seeded_graph: GraphMemory) -> None:
    rebuild_edges(mode="full", force=True)
    conn = seeded_graph._get_conn()
    meta = get_index_meta(conn, "edges")
    assert meta is not None
    assert meta["built_against_revision"] == get_revision(conn)


def test_rebuild_edges_skips_if_up_to_date(seeded_graph: GraphMemory) -> None:
    rebuild_edges(mode="full", force=True)
    result = rebuild_edges(mode="incremental", force=False)
    assert result["skipped"] is True
    assert "up-to-date" in result["reason"]


def test_rebuild_edges_force_rebuilds_even_when_fresh(seeded_graph: GraphMemory) -> None:
    rebuild_edges(mode="full", force=True)
    result = rebuild_edges(mode="full", force=True)
    assert result["skipped"] is False
    assert result["items"] == 3


def test_rebuild_edges_incremental_after_new_write(seeded_graph: GraphMemory) -> None:
    rebuild_edges(mode="full", force=True)
    memory.remember("New insight about concurrency", category="fact", importance="low")

    result = rebuild_edges(mode="incremental", force=False)
    assert result["skipped"] is False
    assert result["items"] == 1


def test_rebuild_entities_full_populates_entity_index(seeded_graph: GraphMemory) -> None:
    conn = seeded_graph._get_conn()
    conn.execute("DELETE FROM entity_index")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM entity_index").fetchone()[0] == 0

    result = rebuild_entities(mode="full", force=True)
    assert result["skipped"] is False
    assert result["items"] == 3
    assert conn.execute("SELECT COUNT(*) FROM entity_index").fetchone()[0] > 0


def test_rebuild_entities_skips_if_up_to_date(seeded_graph: GraphMemory) -> None:
    rebuild_entities(mode="full", force=True)
    result = rebuild_entities(mode="incremental", force=False)
    assert result["skipped"] is True


class _FakeEmbeddingProvider:
    """Deterministic fake provider for embedding rebuild tests."""

    name = "fake-provider-v1"

    def embed(self, text: str) -> list[float]:
        import hashlib

        h = hashlib.sha256(text.encode()).digest()
        return [b / 255.0 for b in h[:8]]


def test_rebuild_embeddings_no_provider_returns_skipped(
    seeded_graph: GraphMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If no embedding provider is available, rebuild reports skipped cleanly."""
    import memcp.core.embeddings as emb

    monkeypatch.setattr(emb, "get_provider", lambda: None)

    result = rebuild_embeddings(mode="full", force=True)
    assert result["skipped"] is True
    assert "no embedding provider" in result["reason"].lower()


@pytest.mark.skipif(not NUMPY_AVAILABLE, reason="numpy not installed")
def test_rebuild_embeddings_full_writes_vectorstore(
    seeded_graph: GraphMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With a fake provider, rebuild writes the .npz and updates index_meta."""
    import memcp.core.embeddings as emb

    monkeypatch.setattr(emb, "get_provider", lambda: _FakeEmbeddingProvider())

    result = rebuild_embeddings(mode="full", force=True)
    assert result["skipped"] is False
    assert result["items"] == 3

    from memcp.config import get_config

    cfg = get_config()
    assert (cfg.cache_dir / "insight_embeddings.npz").exists()

    conn = seeded_graph._get_conn()
    meta = get_index_meta(conn, "embeddings")
    assert meta is not None
    assert meta["built_against_revision"] == get_revision(conn)
    assert meta["model_version"] == "fake-provider-v1"


def test_rebuild_embeddings_skips_if_up_to_date(
    seeded_graph: GraphMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memcp.core.embeddings as emb

    monkeypatch.setattr(emb, "get_provider", lambda: _FakeEmbeddingProvider())

    rebuild_embeddings(mode="full", force=True)
    result = rebuild_embeddings(mode="incremental", force=False)
    assert result["skipped"] is True


def test_rebuild_all_runs_all_three_indexes(
    seeded_graph: GraphMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    import memcp.core.embeddings as emb

    monkeypatch.setattr(emb, "get_provider", lambda: None)  # no embeddings

    result = rebuild_all(mode="full", force=True)
    names = {r["index"] for r in result["results"]}
    assert names == {"edges", "entities", "embeddings"}
    assert isinstance(result["total_duration_ms"], int)
    not_skipped = [r for r in result["results"] if not r["skipped"]]
    assert {r["index"] for r in not_skipped} == {"edges", "entities"}


def test_rebuild_all_index_parameter_limits_scope(
    seeded_graph: GraphMemory,
) -> None:
    result = rebuild_all(index="edges", mode="full", force=True)
    names = [r["index"] for r in result["results"]]
    assert names == ["edges"]


def test_rebuild_all_rejects_unknown_index(seeded_graph: GraphMemory) -> None:
    with pytest.raises(ValueError, match="Unknown index"):
        rebuild_all(index="bogus", mode="full", force=True)


# ── Review-issue fixes ────────────────────────────────────────────────


def test_forget_invalidates_edges_index(seeded_graph: GraphMemory) -> None:
    """I2: deleting a node triggers a full edge rebuild on next session."""
    from memcp.core.revision import get_index_meta

    rebuild_edges(mode="full", force=True)
    conn = seeded_graph._get_conn()
    assert get_index_meta(conn, "edges") is not None

    node_id = conn.execute("SELECT id FROM nodes LIMIT 1").fetchone()[0]
    memory.forget(node_id)

    # index_meta row for edges should be gone
    assert get_index_meta(conn, "edges") is None


@pytest.mark.skipif(not NUMPY_AVAILABLE, reason="numpy not installed")
def test_rebuild_embeddings_removes_orphans(
    seeded_graph: GraphMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """I4: vectors for deleted nodes are removed during incremental rebuild."""
    import memcp.core.embeddings as emb

    monkeypatch.setattr(emb, "get_provider", lambda: _FakeEmbeddingProvider())

    rebuild_embeddings(mode="full", force=True)

    # Delete one insight, bypassing the usual forget path so we can verify
    # the orphan-cleanup logic in isolation
    conn = seeded_graph._get_conn()
    victim_id = conn.execute("SELECT id FROM nodes LIMIT 1").fetchone()[0]
    conn.execute("DELETE FROM nodes WHERE id = ?", (victim_id,))
    conn.commit()

    # Bump revision manually so the rebuild isn't skipped as up-to-date
    from memcp.core.revision import bump_revision

    bump_revision(conn)
    conn.commit()

    result = rebuild_embeddings(mode="incremental", force=False)
    assert result["orphans_removed"] == 1

    # Verify the vector is actually gone from the .npz
    from memcp.config import get_config
    from memcp.core.vecstore import VectorStore

    store = VectorStore(get_config().cache_dir / "insight_embeddings.npz")
    store.load()
    assert victim_id not in store.ids


def test_embedding_provider_name_reflects_loaded_model(
    isolated_data_dir, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1: provider.name includes the actually-loaded model name, not just class default."""
    try:
        from memcp.core.embeddings import Model2VecProvider
    except ImportError:
        pytest.skip("model2vec not installed")

    # Skip if model2vec can't load — test env may not have network access
    try:
        default = Model2VecProvider()
    except Exception:
        pytest.skip("model2vec cannot load default model in this test env")

    assert default.name.startswith("Model2VecProvider/")
    assert Model2VecProvider.MODEL_NAME in default.name


def test_model_version_uses_provider_name(
    seeded_graph: GraphMemory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """C1 (no real model required): _current_embedding_model_version returns
    the provider instance's .name, which changes with instance config."""
    from memcp.core.reindex import _current_embedding_model_version

    class _ProviderA:
        name = "Model2VecProvider/minishlab/potion-multilingual-128M"

        def embed(self, text: str) -> list[float]:
            return [0.0]

    class _ProviderB:
        name = "Model2VecProvider/some-other-model"

        def embed(self, text: str) -> list[float]:
            return [0.0]

    import memcp.core.embeddings as emb

    monkeypatch.setattr(emb, "get_provider", lambda: _ProviderA())
    version_a = _current_embedding_model_version()

    monkeypatch.setattr(emb, "get_provider", lambda: _ProviderB())
    version_b = _current_embedding_model_version()

    assert version_a != version_b
    assert version_a == "Model2VecProvider/minishlab/potion-multilingual-128M"
    assert version_b == "Model2VecProvider/some-other-model"
