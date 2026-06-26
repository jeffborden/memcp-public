"""Tests for memcp.core.embeddings."""

from __future__ import annotations

import logging

import pytest

import memcp.core.embeddings as emb
from memcp.core.embeddings import (
    EmbeddingProvider,
    get_provider,
    reset_provider,
    select_tier,
)


class FakeProvider(EmbeddingProvider):
    """Deterministic 8-dim provider for testing."""

    name = "FakeProvider/test-stub-v1"

    def embed(self, text: str) -> list[float]:
        h = hash(text) & 0xFFFFFFFF
        return [float((h >> (i * 4)) & 0xF) / 15.0 for i in range(8)]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        return [self.embed(t) for t in texts]

    def dim(self) -> int:
        return 8


class TestFakeProvider:
    def test_embed_returns_list(self) -> None:
        p = FakeProvider()
        vec = p.embed("hello")
        assert isinstance(vec, list)
        assert len(vec) == 8

    def test_embed_deterministic(self) -> None:
        p = FakeProvider()
        assert p.embed("hello") == p.embed("hello")

    def test_embed_different_texts(self) -> None:
        p = FakeProvider()
        assert p.embed("hello") != p.embed("world")

    def test_embed_batch(self) -> None:
        p = FakeProvider()
        results = p.embed_batch(["a", "b", "c"])
        assert len(results) == 3
        assert all(len(v) == 8 for v in results)

    def test_dim(self) -> None:
        p = FakeProvider()
        assert p.dim() == 8

    def test_embed_query_defaults_to_embed(self) -> None:
        # Providers that don't override embed_query embed queries as passages.
        p = FakeProvider()
        assert p.embed_query("hello") == p.embed("hello")


class TestGetProvider:
    def test_get_provider_returns_none_without_deps(self, monkeypatch: pytest.MonkeyPatch) -> None:
        reset_provider()
        monkeypatch.setenv("MEMCP_EMBEDDING_PROVIDER", "model2vec")
        # Provider will return None if model2vec isn't installed
        # (or return an actual provider if it is — both are valid)
        result = get_provider()
        assert result is None or isinstance(result, EmbeddingProvider)

    def test_reset_provider(self) -> None:
        reset_provider()
        # After reset, _provider_loaded should be False
        # Calling get_provider again should re-initialize
        result = get_provider()
        assert result is None or isinstance(result, EmbeddingProvider)

    def test_singleton_returns_same_instance(self) -> None:
        reset_provider()
        p1 = get_provider()
        p2 = get_provider()
        assert p1 is p2


class TestABCContract:
    def test_cannot_instantiate_abc(self) -> None:
        with pytest.raises(TypeError):
            EmbeddingProvider()  # type: ignore[abstract]


# ── Phase 4 Item 1 — semantic-hq embedder tier ────────────────────────────────
class TestSelectTier:
    """Test 1 — tier resolution: hq > model2vec > keyword-only.

    fastembed present → hq (bge-small); fastembed absent + model2vec → model2vec;
    both absent → keyword-only with one log line and no exception.
    """

    def _avail(self, monkeypatch, *, fastembed: bool, model2vec: bool) -> None:
        monkeypatch.setattr(emb, "_fastembed_available", lambda: fastembed)
        monkeypatch.setattr(emb, "_model2vec_available", lambda: model2vec)

    def test_fastembed_present_selects_hq(self, monkeypatch) -> None:
        monkeypatch.delenv("MEMCP_EMBEDDER_TIER", raising=False)
        self._avail(monkeypatch, fastembed=True, model2vec=True)
        assert select_tier() == "hq"

    def test_fastembed_absent_selects_model2vec(self, monkeypatch) -> None:
        monkeypatch.delenv("MEMCP_EMBEDDER_TIER", raising=False)
        self._avail(monkeypatch, fastembed=False, model2vec=True)
        assert select_tier() == "model2vec"

    def test_both_absent_selects_keyword(self, monkeypatch) -> None:
        monkeypatch.delenv("MEMCP_EMBEDDER_TIER", raising=False)
        self._avail(monkeypatch, fastembed=False, model2vec=False)
        assert select_tier() == "keyword"

    def test_keyword_tier_provider_none_logs_once_no_exception(self, monkeypatch, caplog) -> None:
        reset_provider()
        monkeypatch.delenv("MEMCP_EMBEDDING_PROVIDER", raising=False)
        monkeypatch.delenv("MEMCP_EMBEDDER_TIER", raising=False)
        self._avail(monkeypatch, fastembed=False, model2vec=False)
        with caplog.at_level(logging.INFO, logger="memcp.embeddings"):
            assert get_provider() is None  # no exception
            # cached — a second call must not re-log
            assert get_provider() is None
        logs = [r for r in caplog.records if r.name == "memcp.embeddings"]
        assert len(logs) == 1

    def test_explicit_hq_degrades_gracefully_when_fastembed_absent(self, monkeypatch) -> None:
        # Explicitly asking for hq when fastembed isn't installed must not raise;
        # it degrades down the ladder to the next available tier.
        monkeypatch.setenv("MEMCP_EMBEDDER_TIER", "hq")
        self._avail(monkeypatch, fastembed=False, model2vec=True)
        assert select_tier() == "model2vec"


class TestTransientHqFailureNoChurn:
    """Test 2 — a transient hq load failure degrades for the call WITHOUT a
    model_version flip: get_provider returns None (keyword) rather than silently
    switching to a different model tier, which would churn the whole index.
    """

    def test_transient_hq_failure_degrades_without_flip(self, monkeypatch) -> None:
        reset_provider()
        monkeypatch.delenv("MEMCP_EMBEDDING_PROVIDER", raising=False)
        monkeypatch.delenv("MEMCP_EMBEDDER_TIER", raising=False)
        # hq is the resolved tier, model2vec is ALSO installed — a naive
        # fall-through would pick model2vec and flip the model_version.
        monkeypatch.setattr(emb, "_fastembed_available", lambda: True)
        monkeypatch.setattr(emb, "_model2vec_available", lambda: True)

        def _boom(*_a, **_k):
            raise RuntimeError("bge model file transiently unavailable")

        monkeypatch.setattr(emb, "FastEmbedProvider", _boom)
        monkeypatch.setattr(emb, "Model2VecProvider", _boom)  # must NOT be reached

        provider = get_provider()
        assert provider is None  # degraded to keyword for this call

        # No model_version flip: the reindex provenance sees no provider, so it
        # never promotes to a full re-embed under a different model.
        from memcp.core.reindex import _current_embedding_model_version

        assert _current_embedding_model_version() is None
