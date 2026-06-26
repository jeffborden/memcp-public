"""Memory system — remember, recall, forget with token counting and importance decay.

Phase 1: Flat JSON backend at ~/.memcp/memory.json.
Phase 3: Delegates to GraphMemory (SQLite + edges), same interface.

The public API (remember, recall, forget, memory_status) auto-detects which
backend to use. Once GraphMemory is available (graph.db exists or first write),
all operations go through the graph. Legacy JSON data is auto-migrated.
"""

from __future__ import annotations

import json
import math
import re
from datetime import datetime, timezone
from typing import Any

from memcp import __version__
from memcp.config import get_config
from memcp.core.errors import ValidationError
from memcp.core.fileutil import (
    atomic_write_json,
    content_hash,
    estimate_tokens,
    insight_id,
    locked_read_json,
)
from memcp.core.graph import GraphMemory
from memcp.core.project import get_current_project, get_current_session
from memcp.core.snapshot_sync import snapshot_health

VALID_CATEGORIES = {"decision", "fact", "preference", "finding", "todo", "general", "episode"}
VALID_IMPORTANCES = {"low", "medium", "high", "critical"}
IMPORTANCE_WEIGHTS = {"low": 0.25, "medium": 0.5, "high": 0.75, "critical": 1.0}


def _use_graph() -> bool:
    """Check whether to use the graph backend.

    True if graph.db exists OR a snapshot dir is configured. Sync mode is always
    graph-backed: on a fresh machine the pull that materializes graph.db is a
    lazy side effect of NodeStore._get_conn, so without this a first remember()
    would route to _remember_json and strand the row in memory.json, never to
    propagate (§3.7). The _get_conn funnel then absorbs any legacy memory.json.
    """
    config = get_config()
    return config.graph_db_path.exists() or bool(config.snapshot_dir)


def _get_graph() -> GraphMemory:
    """Get a GraphMemory instance."""
    return GraphMemory()


def _ensure_graph_migrated() -> GraphMemory:
    """Get a graph, auto-migrating from JSON if needed."""
    graph = _get_graph()
    config = get_config()

    # If JSON memory exists and graph is empty, migrate
    if config.memory_path.exists():
        json_data = locked_read_json(config.memory_path)
        if json_data and json_data.get("insights"):
            stats = graph.stats()
            if stats["node_count"] == 0:
                graph.migrate_from_json(json_data)

    return graph


def _default_memory() -> dict[str, Any]:
    """Default empty memory structure."""
    return {
        "version": __version__,
        "insights": [],
        "metadata": {"created_at": datetime.now(timezone.utc).isoformat()},
    }


def _load_memory() -> dict[str, Any]:
    """Load memory from disk, returning default structure if missing."""
    config = get_config()
    data = locked_read_json(config.memory_path)
    if data is None:
        return _default_memory()
    return data


def _save_memory(memory: dict[str, Any]) -> None:
    """Save memory to disk atomically."""
    config = get_config()
    memory["metadata"]["updated_at"] = datetime.now(timezone.utc).isoformat()
    memory["metadata"]["count"] = len(memory["insights"])
    atomic_write_json(config.memory_path, memory)


def _compute_effective_importance(insight: dict[str, Any]) -> float:
    """Compute effective importance with access boost and time decay.

    Formula: base_weight * (1 + log(1 + access_count)) * time_decay
    Time decay: halves every `importance_decay_days` days of non-access.
    Critical insights never decay below 0.5.
    """
    config = get_config()
    base = IMPORTANCE_WEIGHTS.get(insight.get("importance", "medium"), 0.5)
    access_count = insight.get("access_count", 0)
    access_boost = 1.0 + math.log(1 + access_count)

    # Time decay based on last access (or creation if never accessed)
    last_access = insight.get("last_accessed_at") or insight.get("created_at", "")
    if last_access:
        try:
            last_dt = datetime.fromisoformat(last_access)
            if last_dt.tzinfo is None:
                last_dt = last_dt.replace(tzinfo=timezone.utc)
            days_since = (datetime.now(timezone.utc) - last_dt).total_seconds() / 86400
            half_life = config.importance_decay_days
            decay = 0.5 ** (days_since / half_life) if half_life > 0 else 1.0
        except (ValueError, TypeError):
            decay = 1.0
    else:
        decay = 1.0

    effective = base * access_boost * decay

    # Critical insights never decay below 0.5
    if insight.get("importance") == "critical":
        effective = max(effective, 0.5)

    return round(effective, 4)


def _capacity_eviction_enabled() -> bool:
    """Capacity-eviction (auto-prune) is incompatible with the no-loss union.

    When a Drive snapshot dir is configured (cross-machine sync), two machines
    prune *different* rows (effective_importance is a non-converging local
    counter), causing permanent cross-machine loss with zero operator error.
    So capacity-eviction is disabled whenever sync is on. See spec §3.10.
    """
    return not get_config().snapshot_dir


def _auto_prune(memory: dict[str, Any]) -> int:
    """Remove lowest effective_importance insights when at capacity.

    Returns the number of pruned insights.
    """
    config = get_config()
    insights = memory["insights"]
    if not _capacity_eviction_enabled():
        return 0
    if len(insights) <= config.max_insights:
        return 0

    # Recalculate effective importance for all
    for ins in insights:
        ins["effective_importance"] = _compute_effective_importance(ins)

    # Sort by effective importance, prune lowest 10%
    insights.sort(key=lambda x: x.get("effective_importance", 0))
    prune_count = max(1, len(insights) - config.max_insights)
    # Never prune critical insights
    pruned = []
    kept = []
    for ins in insights:
        if len(pruned) < prune_count and ins.get("importance") != "critical":
            pruned.append(ins)
        else:
            kept.append(ins)

    memory["insights"] = kept
    return len(pruned)


def remember(
    content: str,
    category: str = "general",
    importance: str = "medium",
    tags: str = "",
    summary: str = "",
    entities: str = "",
    project: str = "",
    session: str = "",
) -> dict[str, Any]:
    """Save an insight to persistent memory.

    Returns the created insight dict.
    Uses GraphMemory backend when available, falls back to JSON.
    """
    if category not in VALID_CATEGORIES:
        raise ValidationError(f"Invalid category {category!r}. Must be one of {VALID_CATEGORIES}")
    if importance not in VALID_IMPORTANCES:
        raise ValidationError(
            f"Invalid importance {importance!r}. Must be one of {VALID_IMPORTANCES}"
        )
    if not content.strip():
        raise ValidationError("Content cannot be empty")

    # Secret detection — block storage of credentials
    from memcp.core.secrets import get_secret_detector

    get_secret_detector().check(content)

    now = datetime.now(timezone.utc)
    tag_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    entity_list = [e.strip() for e in entities.split(",") if e.strip()] if entities else []

    new_id = insight_id(content, now.isoformat())

    insight = {
        "id": new_id,
        "content": content.strip(),
        "summary": summary.strip(),
        "category": category,
        "importance": importance,
        "effective_importance": IMPORTANCE_WEIGHTS.get(importance, 0.5),
        "tags": tag_list,
        "entities": entity_list,
        "project": project or get_current_project(),
        "session": session or get_current_session(),
        "token_count": estimate_tokens(content),
        "access_count": 0,
        "last_accessed_at": None,
        "created_at": now.isoformat(),
    }

    # Try graph backend first, fall back to JSON
    if _use_graph():  # noqa: SIM108
        result = _remember_graph(insight, content)
    else:
        result = _remember_json(insight, content)

    # Invalidate BM25 cache so next search rebuilds
    from memcp.core.search import invalidate_bm25_cache

    invalidate_bm25_cache()
    return result


def _try_semantic_dedup(content: str, graph: GraphMemory) -> dict[str, Any] | None:
    """Check for semantic duplicates using embeddings. Returns dict if dup found, else None.

    Only active when MEMCP_SEMANTIC_DEDUP=true and an embedding provider is available.
    """
    import os

    if os.getenv("MEMCP_SEMANTIC_DEDUP", "false").lower() != "true":
        return None

    try:
        from memcp.core.embeddings import get_provider
        from memcp.core.vecstore import VectorStore

        provider = get_provider()
        if provider is None:
            return None

        config = get_config()
        store = VectorStore(config.cache_dir / "insight_embeddings.npz")
        store.load()

        if store.count() == 0:
            return None

        vec = provider.embed(content)
        threshold = float(os.getenv("MEMCP_DEDUP_THRESHOLD", "0.95"))
        results = store.search(vec, top_k=1)
        if results and results[0][1] >= threshold:
            existing_node = graph.get_node(results[0][0])
            if existing_node:
                return {**existing_node, "_duplicate": True, "_similarity": results[0][1]}
    except Exception:
        pass

    return None


def _remember_graph(insight: dict[str, Any], content: str) -> dict[str, Any]:
    """Save insight via GraphMemory."""
    graph = _get_graph()
    try:
        # Check for duplicate content (exact hash match). Skip archived rows so
        # re-remembering archived content creates a fresh active node rather than
        # silently returning the hidden archived one as a no-op (§3.5).
        existing_hash = content_hash(content)
        conn = graph._get_conn()
        rows = conn.execute("SELECT * FROM nodes WHERE archived_at IS NULL").fetchall()
        for row in rows:
            if content_hash(row["content"]) == existing_hash:
                return {**graph._row_to_dict(row), "_duplicate": True}

        # Optional: semantic deduplication via embeddings
        dedup_result = _try_semantic_dedup(content, graph)
        if dedup_result is not None:
            return dedup_result

        # Store with auto-edge generation
        result = graph.store(insight)

        # Bump revision for derived-index staleness detection
        from memcp.core.revision import bump_revision

        bump_revision(graph._get_conn())
        graph._get_conn().commit()

        # Auto-prune if at capacity (disabled when synced — see §3.10)
        config = get_config()
        pruned = 0
        if _capacity_eviction_enabled():
            node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
            if node_count > config.max_insights:
                pruned = _auto_prune_graph(graph, config.max_insights)

        if pruned > 0:
            result["_pruned"] = pruned
        return result
    finally:
        graph.close()


def _remember_json(insight: dict[str, Any], content: str) -> dict[str, Any]:
    """Save insight via JSON backend (Phase 1 fallback)."""
    memory = _load_memory()

    # Check for duplicate content
    existing_hash = content_hash(content)
    for existing in memory["insights"]:
        if content_hash(existing["content"]) == existing_hash:
            return {**existing, "_duplicate": True}

    memory["insights"].append(insight)

    # Auto-prune if at capacity
    pruned = _auto_prune(memory)

    _save_memory(memory)

    result = {**insight}
    if pruned > 0:
        result["_pruned"] = pruned
    return result


def _auto_prune_graph(graph: GraphMemory, max_insights: int) -> int:
    """Prune lowest effective_importance nodes from graph when over capacity."""
    conn = graph._get_conn()
    rows = conn.execute(
        "SELECT id, importance, effective_importance FROM nodes ORDER BY effective_importance ASC"
    ).fetchall()

    over = len(rows) - max_insights
    if over <= 0:
        return 0

    pruned = 0
    for row in rows:
        if pruned >= over:
            break
        if row["importance"] != "critical":
            graph.delete_node(row["id"])
            pruned += 1

    if pruned > 0:
        from memcp.core.revision import bump_revision, invalidate_index

        bump_revision(graph._get_conn())
        # Node deletions can leave surviving nodes' top-K semantic edges stale;
        # invalidate so next rebuild runs a full edge regeneration.
        invalidate_index(graph._get_conn(), "edges")
        graph._get_conn().commit()

    return pruned


def recall(
    query: str = "",
    category: str = "",
    importance: str = "",
    limit: int = 10,
    max_tokens: int = 0,
    project: str = "",
    session: str = "",
    scope: str = "project",
    use_graph: bool = True,
) -> list[dict[str, Any]]:
    """Retrieve insights from memory.

    Searches content, tags, and summary. Filters by category/importance.
    If max_tokens > 0, returns results until the token budget is exhausted.
    Increments access_count on returned insights.

    Uses GraphMemory (intent-aware traversal) when available, JSON fallback.
    Set use_graph=False to bypass graph ranking and use keyword-only matching.
    """
    if category and category not in VALID_CATEGORIES:
        raise ValidationError(f"Invalid category {category!r}")
    if importance and importance not in VALID_IMPORTANCES:
        raise ValidationError(f"Invalid importance {importance!r}")

    # Auto-populate project/session from active state
    if scope == "project" and not project:
        project = get_current_project()
    if scope == "session" and not session:
        session = get_current_session()

    if _use_graph():
        return _recall_graph(
            query=query,
            category=category,
            importance=importance,
            limit=limit,
            max_tokens=max_tokens,
            project=project,
            session=session,
            scope=scope,
            use_edges=use_graph,
        )

    return _recall_json(
        query=query,
        category=category,
        importance=importance,
        limit=limit,
        max_tokens=max_tokens,
        project=project,
        session=session,
        scope=scope,
    )


def list_active(
    project: str = "",
    session: str = "",
    scope: str = "project",
    limit: int = 0,
) -> list[dict[str, Any]]:
    """Return active insights in scope WITHOUT mutating access metrics.

    Unlike recall(), this does NOT bump access_count / last_accessed_at and does
    NOT trigger Hebbian edge strengthening. It is the candidate-gathering path:
    memcp_search must score the FULL active corpus (P3 — a recency-bounded
    candidate window left ~96% of nodes unreachable), and touching every node's
    access metadata on each search would be both semantically wrong and a
    write-amplification hazard on a synced DB.

    limit=0 means "all active nodes" (capped at config.max_insights).
    """
    if scope == "project" and not project:
        project = get_current_project()
    if scope == "session" and not session:
        session = get_current_session()

    cap = limit if limit > 0 else get_config().max_insights

    if _use_graph():
        graph = _get_graph()
        try:
            # use_edges=False skips Hebbian co-retrieval side effects; an empty
            # query returns all active in-scope nodes (newest-first) up to cap.
            return graph.query(
                query="",
                limit=cap,
                project=project,
                session=session,
                scope=scope,
                use_edges=False,
            )
        finally:
            graph.close()

    # JSON fallback: filter without bumping access metrics.
    memory = _load_memory()
    insights = list(memory["insights"])
    if scope == "session" and session:
        insights = [i for i in insights if i.get("session") == session]
    elif scope == "project" and project:
        insights = [i for i in insights if i.get("project") == project]
    insights.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return insights[:cap]


def _recall_graph(
    query: str = "",
    category: str = "",
    importance: str = "",
    limit: int = 10,
    max_tokens: int = 0,
    project: str = "",
    session: str = "",
    scope: str = "project",
    use_edges: bool = True,
) -> list[dict[str, Any]]:
    """Recall via GraphMemory with intent-aware traversal."""
    graph = _get_graph()
    try:
        results = graph.query(
            query=query,
            category=category,
            importance=importance,
            limit=limit,
            max_tokens=max_tokens,
            project=project,
            session=session,
            scope=scope,
            use_edges=use_edges,
        )

        # Update access metrics
        if results:
            now = datetime.now(timezone.utc).isoformat()
            for node in results:
                graph.update_node(
                    node["id"],
                    {
                        "access_count": node.get("access_count", 0) + 1,
                        "last_accessed_at": now,
                        "effective_importance": _compute_effective_importance(node),
                    },
                )

        return results
    finally:
        graph.close()


def _recall_json(
    query: str = "",
    category: str = "",
    importance: str = "",
    limit: int = 10,
    max_tokens: int = 0,
    project: str = "",
    session: str = "",
    scope: str = "project",
) -> list[dict[str, Any]]:
    """Recall via JSON backend (Phase 1 fallback)."""
    memory = _load_memory()
    insights = memory["insights"]

    # Filter by project/session/scope
    if scope == "session" and session:
        insights = [i for i in insights if i.get("session") == session]
    elif scope == "project" and project:
        insights = [i for i in insights if i.get("project") == project]

    # Filter by category
    if category:
        insights = [i for i in insights if i.get("category") == category]

    # Filter by importance
    if importance:
        insights = [i for i in insights if i.get("importance") == importance]

    # Search by query (keyword match in content + tags + summary)
    if query:
        query_lower = query.lower()
        query_tokens = query_lower.split()

        scored = []
        for ins in insights:
            text = " ".join(
                [
                    ins.get("content", ""),
                    ins.get("summary", ""),
                    " ".join(ins.get("tags", [])),
                ]
            ).lower()

            score = sum(1 for token in query_tokens if token in text)
            if score > 0:
                scored.append((score, ins))

        scored.sort(key=lambda x: (-x[0], x[1].get("created_at", "")))
        insights = [ins for _, ins in scored]
    else:
        insights.sort(key=lambda x: x.get("created_at", ""), reverse=True)

    insights = insights[:limit]

    # Apply token budget
    if max_tokens > 0:
        budgeted: list[dict[str, Any]] = []
        tokens_used = 0
        for ins in insights:
            ins_tokens = ins.get("token_count", estimate_tokens(ins.get("content", "")))
            if tokens_used + ins_tokens > max_tokens and budgeted:
                break
            budgeted.append(ins)
            tokens_used += ins_tokens
        insights = budgeted

    # Update access metrics on returned insights
    if insights:
        now = datetime.now(timezone.utc).isoformat()
        returned_ids = {ins["id"] for ins in insights}
        modified = False
        for ins in memory["insights"]:
            if ins["id"] in returned_ids:
                ins["access_count"] = ins.get("access_count", 0) + 1
                ins["last_accessed_at"] = now
                ins["effective_importance"] = _compute_effective_importance(ins)
                modified = True
        if modified:
            _save_memory(memory)

    return insights


def update(
    insight_id: str,
    tags: str | None = None,
    importance: str | None = None,
    category: str | None = None,
    summary: str | None = None,
    entities: str | None = None,
) -> dict[str, Any] | None:
    """Update an existing insight in place — preserves id and all edges.

    Only the provided fields are changed. Returns the updated insight dict, or
    None if the insight wasn't found. Mutating importance also re-snapshots
    effective_importance so ranking stays consistent with the categorical
    level. Mutating category or importance is validated against the same
    enums remember() enforces.
    """
    updates: dict[str, Any] = {}

    if tags is not None:
        updates["tags"] = [t.strip() for t in tags.split(",") if t.strip()]
    if entities is not None:
        updates["entities"] = [e.strip() for e in entities.split(",") if e.strip()]
    if summary is not None:
        updates["summary"] = summary.strip()
    if category is not None:
        if category not in VALID_CATEGORIES:
            raise ValidationError(
                f"Invalid category {category!r}. Must be one of {VALID_CATEGORIES}"
            )
        updates["category"] = category
    if importance is not None:
        if importance not in VALID_IMPORTANCES:
            raise ValidationError(
                f"Invalid importance {importance!r}. Must be one of {VALID_IMPORTANCES}"
            )
        updates["importance"] = importance
        updates["effective_importance"] = IMPORTANCE_WEIGHTS.get(importance, 0.5)

    if not updates:
        raise ValidationError("update() requires at least one field to change")

    if _use_graph():
        graph = _get_graph()
        try:
            ok = graph.update_node(insight_id, updates)
            if not ok:
                return None
            from memcp.core.revision import bump_revision, invalidate_index

            bump_revision(graph._get_conn())
            invalidate_index(graph._get_conn(), "edges")
            graph._get_conn().commit()
            updated = graph.get_node(insight_id)
        finally:
            graph.close()
    else:
        memory = _load_memory()
        updated = None
        for ins in memory["insights"]:
            if ins.get("id") == insight_id:
                if "tags" in updates:
                    ins["tags"] = updates["tags"]
                if "entities" in updates:
                    ins["entities"] = updates["entities"]
                if "summary" in updates:
                    ins["summary"] = updates["summary"]
                if "category" in updates:
                    ins["category"] = updates["category"]
                if "importance" in updates:
                    ins["importance"] = updates["importance"]
                    ins["effective_importance"] = updates["effective_importance"]
                updated = ins
                break
        if updated is None:
            return None
        _save_memory(memory)

    from memcp.core.search import invalidate_bm25_cache

    invalidate_bm25_cache()
    return updated


def forget(insight_id: str) -> bool:
    """Remove an insight by ID. Returns True if found and removed.

    Uses GraphMemory when available (removes node + all edges).
    """
    if _use_graph():
        graph = _get_graph()
        try:
            removed = graph.delete_node(insight_id)
            # Only bump if something was actually deleted
            if removed:
                from memcp.core.revision import bump_revision, invalidate_index

                bump_revision(graph._get_conn())
                invalidate_index(graph._get_conn(), "edges")
                graph._get_conn().commit()
        finally:
            graph.close()
    else:
        # JSON fallback
        memory = _load_memory()
        original_count = len(memory["insights"])
        memory["insights"] = [i for i in memory["insights"] if i.get("id") != insight_id]

        if len(memory["insights"]) < original_count:
            _save_memory(memory)
            removed = True
        else:
            removed = False

    if removed:
        from memcp.core.search import invalidate_bm25_cache

        invalidate_bm25_cache()
    return removed


def get_insight(insight_id: str) -> dict[str, Any] | list[dict[str, Any]] | None:
    """Resolve a single insight by full id or by an id prefix.

    Returns:
        - the insight dict when exactly one match is found (full id or
          unambiguous prefix),
        - a list of candidate dicts when a prefix matches 2+ insights
          (the caller disambiguates — never a wrong single result),
        - None when nothing matches.

    Works against both the graph backend and the JSON fallback. Archived
    rows are excluded so a prefix resolves only active insights.
    """
    needle = (insight_id or "").strip()
    if not needle:
        raise ValidationError("insight_id cannot be empty")

    if _use_graph():
        graph = _get_graph()
        try:
            conn = graph._get_conn()
            # Exact match first — a full id always wins outright.
            exact = conn.execute(
                "SELECT * FROM nodes WHERE id = ? AND archived_at IS NULL",
                (needle,),
            ).fetchone()
            if exact is not None:
                return graph._row_to_dict(exact)
            rows = conn.execute(
                "SELECT * FROM nodes WHERE id LIKE ? AND archived_at IS NULL",
                (needle + "%",),
            ).fetchall()
            matches = [graph._row_to_dict(r) for r in rows]
        finally:
            graph.close()
    else:
        memory = _load_memory()
        insights = [i for i in memory["insights"] if not i.get("archived_at")]
        exact_match = next((i for i in insights if i.get("id") == needle), None)
        if exact_match is not None:
            return exact_match
        matches = [i for i in insights if str(i.get("id", "")).startswith(needle)]

    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    return matches


# ── Direct Corpus Interaction (DCI): exact / regex / tag-conjunction grep ──

VALID_GREP_FIELDS = ("content", "summary", "tags", "entities")


def _as_str_list(value: Any) -> list[str]:
    """Coerce a tags/entities cell (JSON-text from the graph, or an already-parsed
    list from the JSON backend) to a list of strings."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (json.JSONDecodeError, TypeError):
            return []
    if isinstance(value, list):
        return [str(v) for v in value]
    return []


def _grep_snippet(text: str, match: re.Match[str], context_chars: int) -> str:
    """Return text around a match, ±context_chars, with ellipsis on truncated sides."""
    start = max(0, match.start() - context_chars)
    end = min(len(text), match.end() + context_chars)
    snippet = text[start:end]
    if start > 0:
        snippet = "…" + snippet
    if end < len(text):
        snippet = snippet + "…"
    return snippet


def grep(
    pattern: str,
    fields: list[str] | None = None,
    project: str = "",
    tags_all: list[str] | None = None,
    category: str = "",
    importance: str = "",
    fixed_strings: bool = False,
    ignore_case: bool = True,
    context_chars: int = 120,
    limit: int = 50,
    include_archived: bool = False,
) -> list[dict[str, Any]]:
    """Exact / regex / tag-conjunction search over the insight store — no ranking, no embeddings.

    Direct Corpus Interaction (DCI): the deterministic complement to the
    similarity-first ``recall``/``search`` path. The corpus is tiny (~1700 rows,
    ~1MB) so a whole-table scan + Python ``re`` is sub-50ms — no FTS/index needed.
    Pairs with ``get_insight(id)`` to read a full match. ADDITIVE — does not touch
    semantic search, hybrid ranking, or the graph.

    Args mirror SPEC-memcp_grep.md. ``fields`` defaults to ``["content"]`` and may
    include any of ``content``/``summary``/``tags``/``entities``. ``tags_all`` is a
    boolean AND over the insight's tag list (exact membership). Column filters
    (``project``/``category``/``importance``/archived) are applied in SQL; regex +
    ``tags_all`` in Python. Results are sorted deterministically by
    ``(created_at, id)`` and capped at ``limit``.

    Raises ValidationError on an empty pattern, an unknown field, or an invalid regex.

    Note on archived: grep scans the live ``nodes`` table and toggles the
    ``archived_at IS NULL`` filter via ``include_archived``. In synced mode
    (the production posture) archiving sets ``archived_at`` in-band, so
    ``include_archived=True`` surfaces archived rows. In local-only mode
    ``archive_insight`` hard-deletes the row to a side-file (out of grep's
    nodes-table scope), so archived rows are not grep-reachable there.
    """
    if not (pattern or "").strip():
        raise ValidationError("grep requires a non-empty pattern")

    fields = list(fields) if fields else ["content"]
    bad_fields = [f for f in fields if f not in VALID_GREP_FIELDS]
    if bad_fields:
        raise ValidationError(
            f"Invalid grep field(s) {bad_fields}. Must be subset of {list(VALID_GREP_FIELDS)}"
        )

    tags_all = [t.strip() for t in (tags_all or []) if t.strip()]

    needle = re.escape(pattern) if fixed_strings else pattern
    try:
        rx = re.compile(needle, re.IGNORECASE if ignore_case else 0)
    except re.error as e:
        raise ValidationError(f"Invalid regex pattern {pattern!r}: {e}") from e

    rows = _grep_fetch_rows(project, category, importance, include_archived)

    results: list[dict[str, Any]] = []
    for row in rows:
        row_tags = _as_str_list(row.get("tags"))
        if tags_all and not all(t in row_tags for t in tags_all):
            continue

        matches: list[dict[str, str]] = []
        for field in fields:
            if field in ("tags", "entities"):
                items = row_tags if field == "tags" else _as_str_list(row.get("entities"))
                hits = [it for it in items if rx.search(it)]
                if hits:
                    matches.append({"field": field, "snippet": ", ".join(hits)})
            else:
                text = row.get(field) or ""
                m = rx.search(text)
                if m:
                    matches.append(
                        {"field": field, "snippet": _grep_snippet(text, m, context_chars)}
                    )
        if not matches:
            continue

        results.append(
            {
                "id": row["id"],
                "category": row.get("category"),
                "importance": row.get("importance"),
                "project": row.get("project"),
                "tags": row_tags,
                "matches": matches,
                "created_at": row.get("created_at"),
            }
        )

    results.sort(key=lambda r: (r["created_at"] or "", r["id"]))
    return results[:limit]


def _grep_fetch_rows(
    project: str, category: str, importance: str, include_archived: bool
) -> list[dict[str, Any]]:
    """Fetch column-filtered rows (archived honored) from whichever backend is active.

    Read-only; mirrors get_insight's posture (don't block on the writer)."""
    if _use_graph():
        graph = _get_graph()
        try:
            conn = graph._get_conn()
            sql = (
                "SELECT id, content, summary, tags, entities, category, importance, "
                "project, created_at, archived_at FROM nodes"
            )
            conds: list[str] = []
            params: list[Any] = []
            if not include_archived:
                conds.append("archived_at IS NULL")
            if project:
                conds.append("project = ?")
                params.append(project)
            if category:
                conds.append("category = ?")
                params.append(category)
            if importance:
                conds.append("importance = ?")
                params.append(importance)
            if conds:
                sql += " WHERE " + " AND ".join(conds)
            return [dict(r) for r in conn.execute(sql, params).fetchall()]
        finally:
            graph.close()

    memory = _load_memory()
    rows: list[dict[str, Any]] = []
    for ins in memory["insights"]:
        if not include_archived and ins.get("archived_at"):
            continue
        if project and ins.get("project") != project:
            continue
        if category and ins.get("category") != category:
            continue
        if importance and ins.get("importance") != importance:
            continue
        rows.append(ins)
    return rows


def memory_status(project: str = "", session: str = "") -> dict[str, Any]:
    """Return memory statistics.

    Includes graph stats (edge counts, top entities) when graph backend is active.
    """
    # Auto-populate project when neither project nor session is specified
    if not project and not session:
        project = get_current_project()

    if _use_graph():
        return _status_graph(project=project, session=session)
    return _status_json(project=project, session=session)


def generate_index(project: str = "") -> str:
    """Generate a progressive disclosure index as markdown.

    Returns a lightweight markdown string listing all insights grouped by
    category, with one-line summaries. Designed to be read by the agent
    before deciding what to query in depth.
    """
    if not project:
        project = get_current_project()

    # Fetch all insights for the project
    if _use_graph():
        graph = _get_graph()
        try:
            conn = graph._get_conn()
            if project:
                rows = conn.execute(
                    "SELECT * FROM nodes WHERE project = ? AND archived_at IS NULL "
                    "ORDER BY category, created_at DESC",
                    (project,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM nodes WHERE archived_at IS NULL "
                    "ORDER BY category, created_at DESC"
                ).fetchall()
            insights = [graph._row_to_dict(r) for r in rows]
        finally:
            graph.close()
    else:
        memory = _load_memory()
        insights = memory["insights"]
        if project:
            insights = [i for i in insights if i.get("project") == project]
        insights.sort(key=lambda x: (x.get("category", "general"), x.get("created_at", "")))

    if not insights:
        return f"# Index: {project or 'all'}\n\nNo insights stored yet.\n"

    # Group by category
    grouped: dict[str, list[dict[str, Any]]] = {}
    for ins in insights:
        cat = ins.get("category", "general")
        grouped.setdefault(cat, []).append(ins)

    lines = [f"# Index: {project or 'all'}", ""]
    total = len(insights)
    total_tokens = sum(i.get("token_count", 0) for i in insights)
    lines.append(f"**{total} insights** | ~{total_tokens} tokens")
    lines.append("")

    for cat in sorted(grouped.keys()):
        items = grouped[cat]
        lines.append(f"## {cat.title()} ({len(items)})")
        lines.append("")
        for ins in items:
            summary = ins.get("summary", "").strip()
            if not summary:
                # Truncate content as fallback summary
                summary = ins.get("content", "")[:80].replace("\n", " ").strip()
                if len(ins.get("content", "")) > 80:
                    summary += "..."
            importance = ins.get("importance", "medium")
            tag_str = ""
            tags = ins.get("tags", [])
            if tags:
                tag_str = f" `{', '.join(tags[:3])}`"
            lines.append(f"- **[{importance}]** {summary}{tag_str} — `{ins['id'][:8]}`")
        lines.append("")

    return "\n".join(lines)


def _status_graph(project: str = "", session: str = "") -> dict[str, Any]:
    """Status via GraphMemory."""
    graph = _get_graph()
    try:
        conn = graph._get_conn()

        # Build conditions
        conditions = []
        params: list[Any] = []
        if project:
            conditions.append("project = ?")
            params.append(project)
        if session:
            conditions.append("session = ?")
            params.append(session)
        # Archived rows are a synced soft-state (§3.5) — exclude from status
        # counts so synced (in-band archive) matches non-synced (hard delete).
        conditions.append("archived_at IS NULL")

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE {where}",  # noqa: S608
            params,
        ).fetchall()

        insights = [graph._row_to_dict(r) for r in rows]

        by_category: dict[str, int] = {}
        by_importance: dict[str, int] = {}
        total_tokens = 0
        effective_importances: list[float] = []

        for ins in insights:
            cat = ins.get("category", "general")
            by_category[cat] = by_category.get(cat, 0) + 1
            imp = ins.get("importance", "medium")
            by_importance[imp] = by_importance.get(imp, 0) + 1
            total_tokens += ins.get("token_count", 0)
            effective_importances.append(_compute_effective_importance(ins))

        avg_effective = (
            sum(effective_importances) / len(effective_importances) if effective_importances else 0
        )

        config = get_config()
        result: dict[str, Any] = {
            "total_insights": len(insights),
            "max_insights": config.max_insights,
            "capacity_pct": (
                round(len(insights) / config.max_insights * 100, 1) if insights else 0
            ),
            "total_tokens": total_tokens,
            "by_category": by_category,
            "by_importance": by_importance,
            "avg_effective_importance": round(avg_effective, 4),
            "oldest": min((i.get("created_at", "") for i in insights), default=None),
            "newest": max((i.get("created_at", "") for i in insights), default=None),
            "backend": "graph",
        }

        # Add graph-specific stats
        graph_stats = graph.stats(project=project)
        result["graph"] = {
            "edge_counts": graph_stats["edge_counts"],
            "total_edges": graph_stats["total_edges"],
            "top_entities": graph_stats["top_entities"],
        }

        # Cross-machine snapshot health (blob count, disk, GC floor + pinning
        # host) — surfaces ledger drift before it grows disk. Omitted when no
        # snapshot dir is configured.
        snap = snapshot_health(config.snapshot_dir)
        if snap:
            result["snapshot"] = snap

        # Live sync-instance health + convergence audit (P0 detection). The
        # status query above opened the connection through graph._node_store, so
        # its SnapshotSync (if sync is configured) is initialized and reachable.
        sync = getattr(graph._node_store, "_sync", None)
        if sync is not None:
            snap_section = result.setdefault("snapshot", {})
            snap_section["instance"] = sync.instance_health()
            conv = sync.convergence_audit()
            if conv:
                snap_section["convergence"] = conv

        # Where (and whether) metadata-only event telemetry is being written.
        result["telemetry"] = {
            "enabled": config.telemetry_enabled,
            "dir": config.telemetry_dir,
        }

        # Fail-closed local write-lock failures (P5): a non-zero count means a
        # misconfigured/unwritable lock dir forced writes to surface errors
        # instead of silently degrading serialization.
        from memcp.core.write_lock import local_lock_failure_count

        result["write_lock"] = {"local_lock_failures": local_lock_failure_count()}

        return result
    finally:
        graph.close()


def _status_json(project: str = "", session: str = "") -> dict[str, Any]:
    """Status via JSON backend."""
    memory = _load_memory()
    insights = memory["insights"]

    if project:
        insights = [i for i in insights if i.get("project") == project]
    if session:
        insights = [i for i in insights if i.get("session") == session]

    by_category: dict[str, int] = {}
    for ins in insights:
        cat = ins.get("category", "general")
        by_category[cat] = by_category.get(cat, 0) + 1

    by_importance: dict[str, int] = {}
    for ins in insights:
        imp = ins.get("importance", "medium")
        by_importance[imp] = by_importance.get(imp, 0) + 1

    total_tokens = sum(
        ins.get("token_count", estimate_tokens(ins.get("content", ""))) for ins in insights
    )

    effective_importances = [_compute_effective_importance(ins) for ins in insights]
    avg_effective = (
        sum(effective_importances) / len(effective_importances) if effective_importances else 0
    )

    config = get_config()
    return {
        "total_insights": len(insights),
        "max_insights": config.max_insights,
        "capacity_pct": round(len(insights) / config.max_insights * 100, 1) if insights else 0,
        "total_tokens": total_tokens,
        "by_category": by_category,
        "by_importance": by_importance,
        "avg_effective_importance": round(avg_effective, 4),
        "oldest": min((i.get("created_at", "") for i in insights), default=None),
        "newest": max((i.get("created_at", "") for i in insights), default=None),
        "backend": "json",
        "telemetry": {
            "enabled": config.telemetry_enabled,
            "dir": config.telemetry_dir,
        },
        "write_lock": {
            "local_lock_failures": _local_lock_failures_in_status(),
        },
    }


def _local_lock_failures_in_status() -> int:
    from memcp.core.write_lock import local_lock_failure_count

    return local_lock_failure_count()
