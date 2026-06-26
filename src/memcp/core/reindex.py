"""Rebuild derived indexes from the node store.

Each rebuild function:
  1. Checks staleness via index_meta.built_against_revision vs meta.revision
     (unless force=True).
  2. Rebuilds the index (full = wipe and regenerate; incremental = only
     process nodes added since last build).
  3. Updates index_meta atomically on success.

Rebuilds are idempotent and safe to interrupt: if the process crashes before
index_meta is updated, the next run retries.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from memcp.core.graph import GraphMemory
from memcp.core.revision import (
    get_index_meta,
    get_revision,
    set_index_meta,
)

_EDGES_INDEX = "edges"
_EDGES_BASE_VERSION = "magma-v1"
# Provenance token used when no embedding provider is available. A transition
# to/from this token is a *fallback* (provider temporarily down), not a genuine
# model change, so it must not trigger a full rebuild (P4(e)).
_KEYWORD_FALLBACK = "keyword-fallback"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _current_seq_cut(conn) -> int:
    """The ingest_seq high-water-mark to persist as this build's cut (§3.4)."""
    return conn.execute("SELECT COALESCE(MAX(ingest_seq), 0) FROM nodes").fetchone()[0]


def _full_if_model_changed(conn, index_name: str, model_version: str, mode: str) -> str:
    """Promote an incremental rebuild to full when the model_version changed.

    An incremental seq-cut rebuild only touches rows past the cut, so on a model
    change it would stamp the new model_version while leaving every existing row
    built under the OLD model — exactly the silent cross-machine divergence the
    provenance is meant to prevent. A model change invalidates ALL rows. §3.4.

    Exception (P4(e)): a *fallback* transition — the embedding provider going
    temporarily unavailable, flipping the provenance to/from the keyword-fallback
    token — is NOT a genuine model change. Promoting it to a full rebuild would
    churn the whole graph on every provider blip (and again when it recovers),
    so fallback transitions stay incremental.
    """
    meta = get_index_meta(conn, index_name)
    if not meta:
        return mode
    prev_version = meta.get("model_version") or ""
    if prev_version == model_version:
        return mode
    if _KEYWORD_FALLBACK in prev_version or _KEYWORD_FALLBACK in model_version:
        return mode  # fallback transition — keep it incremental
    return "full"


def _edges_model_version() -> str:
    """Edge-build provenance: fold the embedding model (or a keyword-fallback
    token) into the edges model_version so a keyword-built graph is never
    treated as equivalent to an embedding-built one — otherwise neither machine
    re-derives the other's edges and ranking diverges silently (§3.4).
    """
    embed = _current_embedding_model_version() or "keyword-fallback"
    return f"{_EDGES_BASE_VERSION}+{embed}"


def _is_stale(
    conn,
    index_name: str,
    current_model_version: str,
) -> tuple[bool, str]:
    """Check whether an index is stale. Returns (stale, reason)."""
    store_revision = get_revision(conn)
    meta = get_index_meta(conn, index_name)
    if meta is None:
        return True, f"no metadata for {index_name} (first build)"
    if meta["built_against_revision"] < store_revision:
        return True, (
            f"index revision {meta['built_against_revision']} < store revision {store_revision}"
        )
    if meta["model_version"] != current_model_version:
        return True, (
            f"model version changed: {meta['model_version']!r} → {current_model_version!r}"
        )
    return False, f"up-to-date at revision {store_revision}"


def rebuild_edges(mode: str = "incremental", force: bool = False) -> dict[str, Any]:
    """Rebuild the graph edges table from the nodes table.

    mode="full": delete all edges, iterate all nodes, regenerate per-node.
    mode="incremental": only regenerate edges for nodes created after last build.
    force=True: skip the staleness check.
    """
    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        model_version = _edges_model_version()

        if not force:
            stale, reason = _is_stale(conn, _EDGES_INDEX, model_version)
            if not stale:
                return {
                    "index": _EDGES_INDEX,
                    "skipped": True,
                    "reason": reason,
                    "mode": mode,
                    "items": 0,
                }

        store_revision_before = get_revision(conn)
        started_at = _now_iso()
        # Cut on ingest_seq, not created_at: merged rows carry an older
        # created_at but a fresh local ingest_seq, and clock skew makes the
        # wall-clock cut non-monotonic in local-insert order (§3.4).
        seq_cut = _current_seq_cut(conn)
        mode = _full_if_model_changed(conn, _EDGES_INDEX, model_version, mode)

        # The DELETE, the regeneration, and set_index_meta are ONE transaction
        # (set_index_meta commits last). A crash after the DELETE rolls back, so
        # the index is never left empty while its meta still reads current — the
        # documented-fixed-never-shipped DELETE-then-commit bug (P4(a)). Both
        # paths filter archived nodes (P4(d)).
        items = 0
        with conn.atomic():
            if mode == "full":
                conn.execute("DELETE FROM edges")
                rows = conn.execute(
                    "SELECT id, content, project, session, created_at, entities "
                    "FROM nodes WHERE archived_at IS NULL ORDER BY ingest_seq"
                ).fetchall()
            else:
                meta = get_index_meta(conn, _EDGES_INDEX)
                prev_seq = meta["built_against_seq"] if meta else -1
                rows = conn.execute(
                    "SELECT id, content, project, session, created_at, entities "
                    "FROM nodes WHERE ingest_seq > ? AND archived_at IS NULL ORDER BY ingest_seq",
                    (prev_seq,),
                ).fetchall()
                for r in rows:
                    conn.execute(
                        "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
                        (r["id"], r["id"]),
                    )

            for row in rows:
                insight = graph._node_store._row_to_dict(row)
                graph._edge_manager.generate_edges(insight)
                items += 1

            set_index_meta(
                conn,
                index_name=_EDGES_INDEX,
                built_against_revision=store_revision_before,
                built_at=started_at,
                model_version=model_version,
                built_against_seq=seq_cut,
            )

        return {
            "index": _EDGES_INDEX,
            "skipped": False,
            "mode": mode,
            "items": items,
            "built_against_revision": store_revision_before,
        }
    finally:
        graph.close()


_ENTITIES_INDEX = "entities"


def _current_entity_model_version() -> str:
    """Return a stable string identifying the current entity extractor.

    For spacy-backed extractors, includes the spacy model name + version so
    upgrading or switching spacy models (en_core_web_sm → en_core_web_lg)
    triggers a rebuild.
    """
    from memcp.core.node_store import (
        CombinedEntityExtractor,
        SpacyEntityExtractor,
        _get_best_extractor,
    )

    extractor = _get_best_extractor()
    name = type(extractor).__name__

    spacy_nlp = None
    if isinstance(extractor, SpacyEntityExtractor):
        spacy_nlp = extractor._nlp
    elif isinstance(extractor, CombinedEntityExtractor):
        spacy_nlp = getattr(extractor._spacy, "_nlp", None)

    if spacy_nlp is not None:
        try:
            meta = spacy_nlp.meta
            model_name = meta.get("name", "unknown")
            version = meta.get("version", "unknown")
            return f"{name}/spacy/{model_name}/{version}"
        except Exception:
            pass

    return name


def rebuild_entities(mode: str = "incremental", force: bool = False) -> dict[str, Any]:
    """Rebuild the entity_index table from node content."""
    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        model_version = _current_entity_model_version()

        if not force:
            stale, reason = _is_stale(conn, _ENTITIES_INDEX, model_version)
            if not stale:
                return {
                    "index": _ENTITIES_INDEX,
                    "skipped": True,
                    "reason": reason,
                    "mode": mode,
                    "items": 0,
                }

        store_revision_before = get_revision(conn)
        started_at = _now_iso()
        extractor = graph._node_store._extractor
        seq_cut = _current_seq_cut(conn)
        mode = _full_if_model_changed(conn, _ENTITIES_INDEX, model_version, mode)

        # One transaction, set_index_meta last (P4(a)); archived nodes filtered
        # (P4(d)).
        items = 0
        import json as _json

        with conn.atomic():
            if mode == "full":
                conn.execute("DELETE FROM entity_index")
                rows = conn.execute(
                    "SELECT id, content FROM nodes WHERE archived_at IS NULL ORDER BY ingest_seq"
                ).fetchall()
            else:
                meta = get_index_meta(conn, _ENTITIES_INDEX)
                prev_seq = meta["built_against_seq"] if meta else -1
                rows = conn.execute(
                    "SELECT id, content FROM nodes "
                    "WHERE ingest_seq > ? AND archived_at IS NULL ORDER BY ingest_seq",
                    (prev_seq,),
                ).fetchall()
                for r in rows:
                    conn.execute("DELETE FROM entity_index WHERE node_id = ?", (r["id"],))

            for row in rows:
                node_id = row["id"]
                entities = extractor.extract(row["content"])
                for entity in entities:
                    conn.execute(
                        "INSERT OR IGNORE INTO entity_index (entity, node_id) VALUES (?, ?)",
                        (entity.lower(), node_id),
                    )
                conn.execute(
                    "UPDATE nodes SET entities = ? WHERE id = ?",
                    (_json.dumps(entities), node_id),
                )
                items += 1

            set_index_meta(
                conn,
                index_name=_ENTITIES_INDEX,
                built_against_revision=store_revision_before,
                built_at=started_at,
                model_version=model_version,
                built_against_seq=seq_cut,
            )

        return {
            "index": _ENTITIES_INDEX,
            "skipped": False,
            "mode": mode,
            "items": items,
            "built_against_revision": store_revision_before,
        }
    finally:
        graph.close()


_EMBEDDINGS_INDEX = "embeddings"


def _current_embedding_model_version() -> str | None:
    """Return a stable string identifying the current embedding provider+model,
    or None if no provider is available.

    Relies on the EmbeddingProvider.name contract: every provider must return
    an identifier that changes when the underlying model changes.
    """
    try:
        from memcp.core import embeddings as emb

        provider = emb.get_provider()
    except Exception:
        return None
    if provider is None:
        return None
    return provider.name


def rebuild_embeddings(mode: str = "incremental", force: bool = False) -> dict[str, Any]:
    """Rebuild the insight embeddings VectorStore from node content.

    Machine-local: embeddings live in ~/.memcp/cache/insight_embeddings.npz
    (not synced via GDrive).

    If no embedding provider is configured, skips with reason.
    """
    model_version = _current_embedding_model_version()
    if model_version is None:
        return {
            "index": _EMBEDDINGS_INDEX,
            "skipped": True,
            "reason": "no embedding provider available",
            "mode": mode,
            "items": 0,
        }

    graph = GraphMemory()
    try:
        conn = graph._get_conn()

        if not force:
            stale, reason = _is_stale(conn, _EMBEDDINGS_INDEX, model_version)
            if not stale:
                return {
                    "index": _EMBEDDINGS_INDEX,
                    "skipped": True,
                    "reason": reason,
                    "mode": mode,
                    "items": 0,
                }

        store_revision_before = get_revision(conn)
        started_at = _now_iso()
        seq_cut = _current_seq_cut(conn)
        # A model change means every existing vector is from the old model →
        # full rebuild (mixed-model vectors would break similarity). §3.4.
        mode = _full_if_model_changed(conn, _EMBEDDINGS_INDEX, model_version, mode)

        from memcp.config import get_config
        from memcp.core import embeddings as emb
        from memcp.core.vecstore import VectorStore

        cfg = get_config()
        cfg.cache_dir.mkdir(parents=True, exist_ok=True)
        store_path = cfg.cache_dir / "insight_embeddings.npz"
        store = VectorStore(store_path)

        orphans_removed = 0
        if mode == "incremental":
            store.load()
            meta = get_index_meta(conn, _EMBEDDINGS_INDEX)
            prev_seq = meta["built_against_seq"] if meta else -1
            # Single pass: fetch id + content + ingest_seq for all nodes,
            # partition into "new" (ingest_seq past the cut → to embed) vs
            # "live_ids" (to check orphan status against). The cut is ingest_seq,
            # not created_at, so merged rows (older created_at, fresh seq) and
            # clock-skewed writes are never missed (§3.4).
            all_rows = conn.execute("SELECT id, content, ingest_seq FROM nodes").fetchall()
            live_ids = {r["id"] for r in all_rows}
            rows = [r for r in all_rows if (r["ingest_seq"] or 0) > prev_seq]
            rows.sort(key=lambda r: r["ingest_seq"] or 0)

            for stored_id in list(store.ids):
                if stored_id not in live_ids:
                    store.remove(stored_id)
                    orphans_removed += 1
        else:
            # Reset store in-memory; save() will overwrite the file.
            store.ids = []
            store.vectors = None
            rows = conn.execute("SELECT id, content FROM nodes ORDER BY ingest_seq").fetchall()

        from memcp.core.embedding_text import compose_embedding_text

        provider = emb.get_provider()
        ids_to_add: list[str] = []
        vecs_to_add: list[list[float]] = []
        for row in rows:
            # Theme-enrich the embedded text when MEMCP_SEMANTIC_RECALL is on and
            # a valid (sha-matching) theme exists; otherwise this is the raw
            # content, bit-identical to the phase-3 path (P4 Item 2).
            text = compose_embedding_text(row["id"], row["content"])
            vec = provider.embed(text)
            ids_to_add.append(row["id"])
            vecs_to_add.append(vec)

        if ids_to_add or orphans_removed:
            if ids_to_add:
                store.add_batch(ids_to_add, vecs_to_add)
            store.save()

        set_index_meta(
            conn,
            index_name=_EMBEDDINGS_INDEX,
            built_against_revision=store_revision_before,
            built_at=started_at,
            model_version=model_version,
            built_against_seq=seq_cut,
        )
        conn.commit()

        return {
            "index": _EMBEDDINGS_INDEX,
            "skipped": False,
            "mode": mode,
            "items": len(ids_to_add),
            "orphans_removed": orphans_removed,
            "built_against_revision": store_revision_before,
        }
    finally:
        graph.close()


def rebuild_all(
    index: str = "all",
    mode: str = "incremental",
    force: bool = False,
) -> dict[str, Any]:
    """Rebuild one or all derived indexes.

    index: 'all' | 'edges' | 'entities' | 'embeddings'
    mode:  'incremental' | 'full'
    force: bypass staleness check
    """
    import time

    dispatchers = {
        "edges": rebuild_edges,
        "entities": rebuild_entities,
        "embeddings": rebuild_embeddings,
    }

    if index == "all":
        names = list(dispatchers.keys())
    elif index in dispatchers:
        names = [index]
    else:
        raise ValueError(
            f"Unknown index {index!r}. Use 'all' | 'edges' | 'entities' | 'embeddings'."
        )

    t0 = time.monotonic()
    results = [dispatchers[name](mode=mode, force=force) for name in names]
    duration_ms = int((time.monotonic() - t0) * 1000)

    graph = GraphMemory()
    try:
        store_revision = get_revision(graph._get_conn())
    finally:
        graph.close()

    return {
        "results": results,
        "total_duration_ms": duration_ms,
        "store_revision": store_revision,
    }
