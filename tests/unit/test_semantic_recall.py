"""Phase 3 Item 1 — semantic-similarity term in the recall ranker.

Closes the oracle-confirmed vocabulary-bridging gap (MemCP insight `4154e880`):
the keyword ranker scores 0.0 on abstract behavioral phrasings because it skips
any node with zero query-keyword overlap (`if not overlap: continue`). A
semantic term, behind `MEMCP_SEMANTIC_RECALL`, lets a concrete "P8-like" node
surface for an abstract query that shares ~no keywords with it.

These tests pin the contract:
  1. semantic ON bridges an abstract query to a zero-keyword-overlap node; OFF
     does not (red against today's ranker);
  2. flag OFF is a no-op — bit-identical results and ZERO embedding calls;
  3. a provider that raises degrades to keyword-only — one log, no exception,
     no index/model_version churn (P4 embedder discipline);
  4. one recall embeds the QUERY at most once and never re-embeds the corpus
     (stored node vectors are read from the insight VectorStore);
  5. the kind-weight demotion (Phase 2 Item 1) applies to the BLENDED score —
     a high-semantic kind:pointer node is demoted below a moderate-semantic kb.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import pytest

import memcp.config as config_module
from memcp.core.fileutil import content_hash, estimate_tokens
from memcp.core.graph import GraphMemory


# ── Fixtures / helpers ────────────────────────────────────────────────────────
def _make_insight(content: str, tags: list[str], idx: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": content_hash(content + str(idx) + now.isoformat()),
        "content": content,
        "summary": "",
        "category": "general",
        "importance": "medium",
        "effective_importance": 0.5,
        "tags": tags,
        "entities": [],
        "project": "testproj",
        "session": "",
        "token_count": estimate_tokens(content),
        "access_count": 0,
        "last_accessed_at": None,
        "created_at": now.isoformat(),
    }


class FakeEmbedder:
    """Deterministic embedder — returns registered vectors, counts every call.

    The corpus is pre-populated directly in the VectorStore, so during recall
    this embedder is only ever asked to embed the QUERY. The call counters let
    test 4 assert "at most one embed, zero corpus re-embeds".
    """

    def __init__(self, vectors: dict[str, list[float]], dim: int = 4) -> None:
        self._vectors = vectors
        self._dim = dim
        self.embed_calls: list[str] = []
        self.embed_batch_calls = 0

    @property
    def name(self) -> str:
        return "FakeEmbedder/test"

    def embed(self, text: str) -> list[float]:
        self.embed_calls.append(text)
        return self._vectors.get(text, [0.0] * self._dim)

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        self.embed_batch_calls += 1
        return [self.embed(t) for t in texts]

    def dim(self) -> int:
        return self._dim


class RaisingEmbedder:
    """Embedder whose provider is transiently unavailable (raises on embed)."""

    @property
    def name(self) -> str:
        return "RaisingEmbedder/test"

    def embed(self, text: str) -> list[float]:
        raise RuntimeError("embedding provider down")

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        raise RuntimeError("embedding provider down")

    def dim(self) -> int:
        return 4


def _populate_store(cache_dir, id_to_vec: dict[str, list[float]]) -> None:
    """Write the reindex-built insight VectorStore the recall path reads."""
    from memcp.core.vecstore import VectorStore

    cache_dir.mkdir(parents=True, exist_ok=True)
    store = VectorStore(cache_dir / "insight_embeddings.npz")
    for nid, vec in id_to_vec.items():
        store.add(nid, vec)
    store.save()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated data dir + reset of all relevant singletons/caches."""
    from memcp.core.embed_cache import reset_embed_cache

    monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMCP_HEBBIAN_ENABLED", "false")
    monkeypatch.setenv("MEMCP_EDGE_BOOST", "false")
    monkeypatch.setenv("MEMCP_KIND_WEIGHT", "true")
    config_module._config = None
    reset_embed_cache()
    from memcp.core import semantic_recall

    semantic_recall.reset_store_cache()
    yield tmp_path
    config_module._config = None
    reset_embed_cache()
    semantic_recall.reset_store_cache()


def _install_provider(monkeypatch, provider) -> None:
    monkeypatch.setattr("memcp.core.embeddings.get_provider", lambda: provider)


# ── Test 1 — bridging ─────────────────────────────────────────────────────────
class TestSemanticBridgesBehavioralPhrasing:
    """An abstract query reaches a zero-keyword-overlap concrete node iff ON."""

    QUERY = "describe an occasion where you weighed momentum versus collaboration"
    # Shares NO content tokens with the query — keyword recall filters it out.
    P8 = (
        "Drove the urgent production rollout to hit the hard external launch date, "
        "coordinating the cross-team fixer effort end to end."
    )
    # Distractor shares one query token ('momentum') so the keyword arm returns
    # SOMETHING, making 'OFF does not rank P8 top-10' a non-vacuous comparison.
    DISTRACTOR = "momentum trading strategy applied to liquid equity markets"

    def _seed(self, env):
        graph = GraphMemory(db_path=":memory:")
        p8 = _make_insight(self.P8, ["kind:kb"], 1)
        dis = _make_insight(self.DISTRACTOR, ["kind:kb"], 2)
        graph.store(p8)
        graph.store(dis)
        # query ↔ P8 are semantically close; distractor is orthogonal.
        _populate_store(
            config_module.get_config().cache_dir,
            {p8["id"]: [0.95, 0.31, 0.0, 0.0], dis["id"]: [0.0, 1.0, 0.0, 0.0]},
        )
        return graph, p8["id"]

    def test_off_does_not_surface_p8(self, env, monkeypatch):
        monkeypatch.setenv("MEMCP_SEMANTIC_RECALL", "false")
        config_module._config = None
        _install_provider(monkeypatch, FakeEmbedder({self.QUERY: [1.0, 0.0, 0.0, 0.0]}))
        graph, p8_id = self._seed(env)
        try:
            ids = [r["id"] for r in graph.query(query=self.QUERY, scope="all")]
            assert p8_id not in ids
        finally:
            graph.close()

    def test_on_surfaces_p8_top10(self, env, monkeypatch):
        monkeypatch.setenv("MEMCP_SEMANTIC_RECALL", "true")
        config_module._config = None
        _install_provider(monkeypatch, FakeEmbedder({self.QUERY: [1.0, 0.0, 0.0, 0.0]}))
        graph, p8_id = self._seed(env)
        try:
            ids = [r["id"] for r in graph.query(query=self.QUERY, scope="all", limit=10)]
            assert p8_id in ids
        finally:
            graph.close()


# ── Test 2 — off is a no-op ───────────────────────────────────────────────────
class TestSemanticOffIsNoop:
    """Flag off → no embedding calls, P8 stays unreachable (bit-identical)."""

    def test_off_makes_zero_embedding_calls(self, env, monkeypatch):
        monkeypatch.setenv("MEMCP_SEMANTIC_RECALL", "false")
        config_module._config = None
        fake = FakeEmbedder({"q balanced rollout": [1.0, 0.0, 0.0, 0.0]})
        _install_provider(monkeypatch, fake)

        graph = GraphMemory(db_path=":memory:")
        p8 = _make_insight("totally unrelated launch fixer content", ["kind:kb"], 1)
        kw = _make_insight("q balanced something", ["kind:kb"], 2)
        graph.store(p8)
        graph.store(kw)
        _populate_store(
            config_module.get_config().cache_dir,
            {p8["id"]: [1.0, 0.0, 0.0, 0.0]},  # high sim, but flag is OFF
        )
        # store() embeds node content for edge generation — only count what the
        # RECALL path does.
        fake.embed_calls.clear()
        fake.embed_batch_calls = 0
        try:
            ids = [r["id"] for r in graph.query(query="q balanced rollout", scope="all")]
            # No embedding work happened at all when disabled.
            assert fake.embed_calls == []
            assert fake.embed_batch_calls == 0
            # And the high-sim zero-keyword node never surfaced.
            assert p8["id"] not in ids
        finally:
            graph.close()


# ── Test 3 — degrade to keyword ───────────────────────────────────────────────
class TestEmbedderUnavailableDegradesToKeyword:
    """Provider raises → keyword results, one log, no exception, no churn."""

    def test_degrades_logs_once_no_churn(self, env, monkeypatch, caplog):
        monkeypatch.setenv("MEMCP_SEMANTIC_RECALL", "true")
        config_module._config = None
        _install_provider(monkeypatch, RaisingEmbedder())

        graph = GraphMemory(db_path=":memory:")
        p8 = _make_insight("zero overlap launch fixer rollout content", ["kind:kb"], 1)
        kw = _make_insight("graph database keyword node", ["kind:kb"], 2)
        graph.store(p8)
        graph.store(kw)
        store_path = config_module.get_config().cache_dir / "insight_embeddings.npz"
        _populate_store(
            config_module.get_config().cache_dir,
            {p8["id"]: [1.0, 0.0, 0.0, 0.0]},
        )
        mtime_before = store_path.stat().st_mtime_ns
        try:
            with caplog.at_level(logging.WARNING, logger="memcp.semantic_recall"):
                ids = [r["id"] for r in graph.query(query="graph database keyword", scope="all")]
            # Keyword path: the keyword node is returned, the zero-overlap node is not.
            assert kw["id"] in ids
            assert p8["id"] not in ids
            # Logged exactly once.
            sem_logs = [r for r in caplog.records if r.name == "memcp.semantic_recall"]
            assert len(sem_logs) == 1
            # No index churn: the vector store file was not rewritten.
            assert store_path.stat().st_mtime_ns == mtime_before
        finally:
            graph.close()


# ── Test 3b — dim mismatch degrades (does not raise) ──────────────────────────
class TestDimMismatchDegradesToKeyword:
    """A query/store embedding-dimension mismatch (model changed, no reindex yet)
    must degrade to keyword-only — return None, log once, NOT raise. Regression
    for the 2026-06-11 bug where a 384-dim bge query hit a 256-dim potion store
    and numpy's cosine matmul threw out of compute_semantic_scores (the store
    .search call sat outside the provider try/except, breaking the module's
    documented "never raises" contract)."""

    def test_mismatched_query_dim_returns_none_not_raises(self, env, monkeypatch, caplog):
        from memcp.core import semantic_recall

        monkeypatch.setenv("MEMCP_SEMANTIC_RECALL", "true")
        config_module._config = None
        # Store built under the OLD model: 4-dim vectors.
        _populate_store(
            config_module.get_config().cache_dir,
            {"n1": [1.0, 0.0, 0.0, 0.0], "n2": [0.0, 1.0, 0.0, 0.0]},
        )
        # NEW model embeds the query at a DIFFERENT dim (6) → cosine matmul mismatch.
        _install_provider(monkeypatch, FakeEmbedder({"q": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0]}, dim=6))

        with caplog.at_level(logging.WARNING, logger="memcp.semantic_recall"):
            result = semantic_recall.compute_semantic_scores("q", ["n1", "n2"])

        # Degrades (None), does not raise, logs exactly once.
        assert result is None
        sem_logs = [r for r in caplog.records if r.name == "memcp.semantic_recall"]
        assert len(sem_logs) == 1


# ── Test 4 — no per-query corpus re-embedding ─────────────────────────────────
class TestNoPerQueryCorpusReembedding:
    """One recall embeds the query once; corpus vectors are read, never embedded."""

    def test_one_query_embed_zero_corpus_embeds(self, env, monkeypatch):
        monkeypatch.setenv("MEMCP_SEMANTIC_RECALL", "true")
        config_module._config = None
        query = "abstract behavioral phrasing about leadership"
        fake = FakeEmbedder({query: [1.0, 0.0, 0.0, 0.0]})
        _install_provider(monkeypatch, fake)

        graph = GraphMemory(db_path=":memory:")
        ids_vecs = {}
        for i in range(5):
            node = _make_insight(f"corpus node number {i} content body", ["kind:kb"], i)
            graph.store(node)
            ids_vecs[node["id"]] = [0.5, 0.5, 0.0, 0.0]
        _populate_store(config_module.get_config().cache_dir, ids_vecs)
        # store() embeds node content for edge generation — reset so we count
        # only what the RECALL path does.
        fake.embed_calls.clear()
        fake.embed_batch_calls = 0
        try:
            graph.query(query=query, scope="all")
            assert len(fake.embed_calls) == 1  # only the query
            assert fake.embed_calls[0] == query
            assert fake.embed_batch_calls == 0  # corpus never re-embedded
        finally:
            graph.close()


# ── Test 5 — kind weight applies to the blended score ─────────────────────────
class TestKindWeightAppliesToBlendedScore:
    """A high-sim kind:pointer is demoted below a moderate-sim kind:kb."""

    QUERY = "alpha beta gamma"  # shares no tokens with either node body

    def _seed(self, env, monkeypatch):
        _install_provider(monkeypatch, FakeEmbedder({self.QUERY: [1.0, 0.0, 0.0, 0.0]}))
        graph = GraphMemory(db_path=":memory:")
        pointer = _make_insight("delta epsilon zeta body", ["kind:pointer"], 1)
        kb = _make_insight("eta theta iota body", ["kind:kb"], 2)
        graph.store(pointer)
        graph.store(kb)
        _populate_store(
            config_module.get_config().cache_dir,
            {
                pointer["id"]: [0.9, 0.436, 0.0, 0.0],  # high semantic sim ~0.9
                kb["id"]: [0.6, 0.8, 0.0, 0.0],  # moderate semantic sim ~0.6
            },
        )
        return graph, pointer["id"], kb["id"]

    def test_kind_weight_on_demotes_pointer(self, env, monkeypatch):
        monkeypatch.setenv("MEMCP_SEMANTIC_RECALL", "true")
        monkeypatch.setenv("MEMCP_KIND_WEIGHT", "true")
        config_module._config = None
        graph, pointer_id, kb_id = self._seed(env, monkeypatch)
        try:
            ids = [r["id"] for r in graph.query(query=self.QUERY, scope="all")]
            assert ids.index(kb_id) < ids.index(pointer_id)
        finally:
            graph.close()

    def test_kind_weight_off_lets_high_sim_pointer_win(self, env, monkeypatch):
        # Sanity: without the kind demotion the higher-sim pointer wins, so the
        # ON assertion above is non-vacuous.
        monkeypatch.setenv("MEMCP_SEMANTIC_RECALL", "true")
        monkeypatch.setenv("MEMCP_KIND_WEIGHT", "false")
        config_module._config = None
        graph, pointer_id, kb_id = self._seed(env, monkeypatch)
        try:
            ids = [r["id"] for r in graph.query(query=self.QUERY, scope="all")]
            assert ids.index(pointer_id) < ids.index(kb_id)
        finally:
            graph.close()
