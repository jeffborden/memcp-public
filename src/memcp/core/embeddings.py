"""Embedding providers — Model2Vec (fast) and FastEmbed (accurate).

Auto-selects the best available provider. Gracefully degrades if
no embedding libraries are installed.

Tier selection (Phase 4 Item 1):
  hq (FastEmbed / bge-small-en-v1.5) > model2vec > keyword-only.
  - MEMCP_EMBEDDER_TIER forces a tier ("hq" | "model2vec" | "keyword" | "auto"),
    degrading DOWN the ladder if the forced tier's library is absent.
  - MEMCP_EMBEDDING_PROVIDER ("model2vec" | "fastembed") is the legacy escape
    hatch and preserves the exact pre-P4 construction (FastEmbed keeps its old
    multilingual default model under "fastembed", NOT the hq bge tier).

No-churn discipline (Phase 4): a *transiently* unavailable tier (its lib is
installed but the model fails to load) degrades to keyword-only for that call —
it must NOT silently fall through to a different model tier, which would flip the
embeddings model_version and trigger a full re-embed storm. A GENUINE tier switch
(a different installed library) is a real model change and re-embeds once.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from abc import ABC, abstractmethod

logger = logging.getLogger("memcp.embeddings")

# The high-quality contextual tier: FastEmbed serving bge-small-en-v1.5. Chosen
# in the 2026-06-11 embedder bake-off (bridges 2/5 dead behavioral queries alone,
# 4/5 with theme enrichment) — docs/eval/embedder-bakeoff-2026-06-11.md.
HQ_MODEL_NAME = "BAAI/bge-small-en-v1.5"


class EmbeddingProvider(ABC):
    """Abstract base class for embedding providers."""

    @abstractmethod
    def embed(self, text: str) -> list[float]:
        """Embed a single text (a stored passage/document) into a vector."""

    def embed_query(self, text: str) -> list[float]:
        """Embed a search QUERY into a vector.

        Default: identical to :meth:`embed`. Asymmetric retrieval models (e.g.
        bge-small-en-v1.5) override this to apply the model's query instruction
        prefix, which materially improves short-query→long-passage recall — the
        exact bridging case this tier exists for.
        """
        return self.embed(text)

    @abstractmethod
    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts into vectors."""

    @abstractmethod
    def dim(self) -> int:
        """Return the embedding dimension."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Stable identifier reflecting the actually-loaded model.

        Must change when the underlying model changes, so reindex staleness
        detection correctly triggers a rebuild.
        """


class Model2VecProvider(EmbeddingProvider):
    """Model2Vec static embeddings — fast, small (~30MB, 256 dims)."""

    MODEL_NAME = "minishlab/potion-multilingual-128M"
    DIM = 256

    def __init__(self, model_name: str = "") -> None:
        from model2vec import StaticModel

        self.model_name = model_name or self.MODEL_NAME
        self._model = StaticModel.from_pretrained(self.model_name)

    @property
    def name(self) -> str:
        """Stable identifier reflecting the actually-loaded model.

        Used by reindex staleness detection — changing MEMCP_EMBEDDING_MODEL
        must change this string so indexes rebuild.
        """
        return f"Model2VecProvider/{self.model_name}"

    def embed(self, text: str) -> list[float]:
        result = self._model.encode([text])
        return result[0].tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        result = self._model.encode(texts)
        return [v.tolist() for v in result]

    def dim(self) -> int:
        return self.DIM


class FastEmbedProvider(EmbeddingProvider):
    """FastEmbed ONNX embeddings — higher quality (~200MB, 384 dims)."""

    MODEL_NAME = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
    DIM = 384

    def __init__(self, model_name: str = "") -> None:
        from fastembed import TextEmbedding

        self.model_name = model_name or self.MODEL_NAME
        self._model = TextEmbedding(model_name=self.model_name)

    @property
    def name(self) -> str:
        """Stable identifier reflecting the actually-loaded model."""
        return f"FastEmbedProvider/{self.model_name}"

    def embed(self, text: str) -> list[float]:
        results = list(self._model.embed([text]))
        return results[0].tolist()

    def embed_query(self, text: str) -> list[float]:
        """FastEmbed's query encoding — applies the model's query instruction
        prefix for asymmetric models (bge), a no-op for symmetric ones."""
        results = list(self._model.query_embed([text]))
        return results[0].tolist()

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        results = list(self._model.embed(texts))
        return [v.tolist() for v in results]

    def dim(self) -> int:
        return self.DIM


# ── Tier selection ─────────────────────────────────────────────────────

def _fastembed_available() -> bool:
    """True if the fastembed library can be imported (no model load)."""
    return importlib.util.find_spec("fastembed") is not None


def _model2vec_available() -> bool:
    """True if the model2vec library can be imported (no model load)."""
    return importlib.util.find_spec("model2vec") is not None


def select_tier() -> str:
    """Resolve the embedder tier: ``"hq"`` | ``"model2vec"`` | ``"keyword"``.

    hq (FastEmbed/bge-small) is preferred when its library is installed, else
    model2vec, else keyword-only. ``MEMCP_EMBEDDER_TIER`` forces a tier but
    degrades DOWN the ladder when the forced tier's library is absent (a hard
    failure here would only push users off the semantic path entirely).
    """
    forced = os.getenv("MEMCP_EMBEDDER_TIER", "auto").lower()
    if forced == "keyword":
        return "keyword"
    if forced == "hq" and _fastembed_available():
        return "hq"
    if forced == "model2vec" and _model2vec_available():
        return "model2vec"
    # auto (or a forced tier whose lib is missing → degrade down the ladder).
    if _fastembed_available():
        return "hq"
    if _model2vec_available():
        return "model2vec"
    return "keyword"


# ── Singleton with lazy initialization ────────────────────────────────

_cached_provider: EmbeddingProvider | None = None
_provider_loaded: bool = False


def get_provider() -> EmbeddingProvider | None:
    """Get the cached embedding provider (singleton, lazy init).

    Resolves the tier (hq > model2vec > keyword-only) unless the legacy
    MEMCP_EMBEDDING_PROVIDER override is set. Reads MEMCP_EMBEDDING_MODEL for a
    custom model name. Returns None when no provider is available (keyword-only)
    or when the resolved tier transiently fails to load (degrade, no churn).
    """
    global _cached_provider, _provider_loaded

    if _provider_loaded:
        return _cached_provider

    model_name = os.getenv("MEMCP_EMBEDDING_MODEL", "")
    legacy = os.getenv("MEMCP_EMBEDDING_PROVIDER", "auto").lower()

    # Legacy explicit override — preserves exact pre-P4 construction.
    if legacy == "fastembed":
        _provider_loaded = True
        try:
            _cached_provider = FastEmbedProvider(model_name)
        except Exception:
            _cached_provider = None
        return _cached_provider
    if legacy == "model2vec":
        _provider_loaded = True
        try:
            _cached_provider = Model2VecProvider(model_name)
        except Exception:
            _cached_provider = None
        return _cached_provider

    # P4 tier ladder.
    tier = select_tier()
    if tier == "keyword":
        _provider_loaded = True
        _cached_provider = None
        logger.info("no embedding provider available; keyword-only recall")
        return _cached_provider

    try:
        if tier == "hq":
            _cached_provider = FastEmbedProvider(model_name or HQ_MODEL_NAME)
        else:  # model2vec
            _cached_provider = Model2VecProvider(model_name)
    except Exception:
        # No-churn discipline: the resolved tier's lib is installed but the model
        # transiently failed to load. Degrade to keyword-only for now WITHOUT
        # caching a different model tier — silently switching would flip the
        # embeddings model_version and churn the whole index. Leave the loaded
        # flag clear so a later call retries the genuine tier.
        logger.warning(
            "embedder tier %r failed to load (transient); degrading to "
            "keyword-only for this call without a model_version flip",
            tier,
        )
        return None

    _provider_loaded = True
    return _cached_provider


def reset_provider() -> None:
    """Reset the cached provider (for testing)."""
    global _cached_provider, _provider_loaded
    _cached_provider = None
    _provider_loaded = False
