"""Semantic recall term — query-time embedding vs stored node embeddings.

Closes the vocabulary-bridging gap (MemCP insight `4154e880`): the keyword
ranker skips any node with zero query-keyword overlap, so abstract behavioral
phrasings ("tell me about a time you balanced progress against partnership")
never reach the concrete node that answers them. This module embeds the QUERY
once at call time and compares it against the pre-computed insight embeddings
(the reindex-built ``insight_embeddings.npz`` VectorStore) — one vector sweep,
no per-query corpus re-embedding.

Embedder discipline (P4): a transiently unavailable embedding provider degrades
the call to keyword-only — ``compute_semantic_scores`` returns ``None``, logs
once, and never raises, churns the index, or flips a model_version. A node that
simply lacks a stored embedding gets a semantic term of 0, never an error.
"""

from __future__ import annotations

import logging

logger = logging.getLogger("memcp.semantic_recall")

NUMPY_AVAILABLE = False
try:
    import numpy as _np  # noqa: F401

    NUMPY_AVAILABLE = True
except ImportError:
    pass


# Cache the loaded VectorStore across recalls, keyed by (path, mtime_ns), so a
# warm corpus is not re-read from disk on every query. Invalidated automatically
# when the .npz is rebuilt (mtime changes) or explicitly via reset_store_cache.
_store_cache: dict[str, object] = {}


def reset_store_cache() -> None:
    """Drop the cached VectorStore (tests / after a reindex)."""
    _store_cache.clear()


def _load_store():  # noqa: ANN202
    """Load the insight VectorStore the search/reindex path maintains.

    Returns the VectorStore on success, or None if it is absent/empty. Cached by
    file mtime so repeated recalls do not re-read the .npz.
    """
    from memcp.config import get_config
    from memcp.core.vecstore import VectorStore

    path = get_config().cache_dir / "insight_embeddings.npz"
    if not path.exists():
        return None
    key = str(path)
    mtime = path.stat().st_mtime_ns
    cached = _store_cache.get(key)
    if cached is not None and cached[0] == mtime:  # type: ignore[index]
        return cached[1]  # type: ignore[index]
    store = VectorStore(path)
    if not store.load() or store.count() == 0:
        return None
    _store_cache[key] = (mtime, store)  # type: ignore[assignment]
    return store


def compute_semantic_scores(query: str, node_ids: list[str]) -> dict[str, float] | None:
    """Cosine similarity of ``query`` against each node's stored embedding.

    Returns ``{node_id: sim in [0, 1]}`` for nodes that have a stored embedding
    with positive similarity (others are absent → caller treats as 0).

    Returns ``None`` to signal *degrade to keyword-only* — when numpy or the
    embedding provider is unavailable, or the provider raises (logged once). A
    populated provider with no stored corpus returns ``{}`` (semantic term 0 for
    every node), which is NOT a degrade.
    """
    if not NUMPY_AVAILABLE:
        return None

    from memcp.core.embeddings import get_provider

    provider = get_provider()
    if provider is None:
        return None

    try:
        from memcp.core.embed_cache import get_embed_cache

        cache = get_embed_cache()
        provider_name = type(provider).__name__
        query_vec = cache.get(query, provider_name)
        if query_vec is None:
            # Use the provider's QUERY encoding when it has one (bge applies its
            # query instruction prefix — the asymmetric short-query→long-passage
            # case this tier targets). Duck-typed: fakes without embed_query fall
            # back to embed.
            embed_query = getattr(provider, "embed_query", None)
            query_vec = embed_query(query) if embed_query else provider.embed(query)
            cache.put(query, provider_name, query_vec)
    except Exception:
        # P4: a transiently unavailable provider degrades to keyword for THIS
        # call only — no exception, no index churn, no model_version flip.
        logger.warning(
            "semantic recall: embedding provider unavailable; "
            "degrading to keyword-only for this call"
        )
        return None

    store = _load_store()
    if store is None:
        return {}

    # One vectorized cosine sweep over the whole corpus, then keep the requested
    # nodes. store.search clips negatives to 0 and only returns positive sims.
    #
    # A query/store embedding-DIMENSION mismatch makes the cosine matmul raise —
    # e.g. the embedding model changed (potion 256-dim → bge 384-dim) but the
    # stored vectors were built under the old model and the index has not been
    # rebuilt yet. That is a stale-index condition, NOT a reason to crash recall:
    # degrade to keyword-only for this call (return None) exactly like a
    # transiently-unavailable provider, honoring this module's "never raises"
    # contract. The real fix for the staleness is a reindex; this guard only keeps
    # the in-between window from throwing instead of cleanly degrading.
    wanted = set(node_ids)
    try:
        results = store.search(list(query_vec), top_k=store.count())
    except Exception:
        logger.warning(
            "semantic recall: vector store search failed (likely a query/store "
            "embedding-dimension mismatch after a model change with no reindex); "
            "degrading to keyword-only for this call — run "
            "memcp_reindex(index='embeddings') to rebuild"
        )
        return None
    return {nid: sim for nid, sim in results if nid in wanted}
