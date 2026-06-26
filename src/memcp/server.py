"""MemCP MCP Server — persistent memory tools for Claude Code.

Phase 1: 5 tools (ping, remember, recall, forget, status).
Phase 2: +9 tools (context load/inspect/get/chunk/peek/filter/list/clear, search).
Phase 3: +2 tools (related, graph_stats).
Phase 6: +3 tools (retention_preview, retention_run, restore).
Phase 7: +2 tools (projects, sessions).
Step 2: +3 tools (reinforce, consolidation_preview, consolidate).
"""

from __future__ import annotations

import functools
import inspect
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from mcp.server.fastmcp import FastMCP

from memcp import __version__
from memcp.core import telemetry
from memcp.core.async_utils import run_sync
from memcp.core.errors import MemCPError
from memcp.core.memory import (
    forget,
    generate_index,
    get_insight,
    grep,
    memory_status,
    recall,
    remember,
    update,
)
from memcp.tools.consolidation_tools import do_consolidate, do_consolidation_preview
from memcp.tools.context_tools import (
    do_chunk_context,
    do_clear_context,
    do_filter_context,
    do_list_contexts,
    do_peek_chunk,
    get_context,
    inspect_context,
    load_context,
)
from memcp.tools.feedback_tools import do_reinforce
from memcp.tools.graph_tools import do_graph_stats, do_related
from memcp.tools.project_tools import do_projects, do_sessions
from memcp.tools.reindex_tools import do_reindex
from memcp.tools.retention_tools import do_restore, do_retention_preview, do_retention_run
from memcp.tools.search_tools import do_search
from memcp.tools.sync_tools import do_sync

mcp = FastMCP("MemCP")

_F = TypeVar("_F", bound=Callable[..., Any])


def _out_bytes(result: Any) -> int:
    """UTF-8 byte length of a tool's JSON-string result (0 for non-strings).

    A *length only* — never the content. Encoding to UTF-8 makes ``out_bytes``
    truthful for multibyte payloads (``len(str)`` counts code points, which
    undercounts the real serialized size for emoji/CJK/accented content).
    """
    if not isinstance(result, str):
        return 0
    return len(result.encode("utf-8"))


def _traced(fn: _F) -> _F:
    """Wrap an MCP tool handler to emit one metadata-only telemetry line per call.

    Records ``{name, dur_ms, out_bytes, ok}`` — never arguments or result
    content (tools return a JSON string; we record only its length). Handles both
    sync and async handlers and preserves the signature (via ``functools.wraps``,
    so ``__wrapped__`` is set and FastMCP's ``inspect.signature`` still sees the
    original parameters for schema generation). ``ok`` reflects whether the call
    raised — a tool that returns its own ``status:"error"`` JSON still counts as
    ``ok`` at the transport level, and we never parse the body to learn more.
    """
    name = fn.__name__

    if inspect.iscoroutinefunction(fn):

        @functools.wraps(fn)
        async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
            start = time.perf_counter()
            ok = True
            result: Any = None
            try:
                result = await fn(*args, **kwargs)
                return result
            except BaseException:
                ok = False
                raise
            finally:
                telemetry.emit_tool(
                    name,
                    dur_ms=(time.perf_counter() - start) * 1000,
                    out_bytes=_out_bytes(result),
                    ok=ok,
                )

        return async_wrapper  # type: ignore[return-value]

    @functools.wraps(fn)
    def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
        start = time.perf_counter()
        ok = True
        result: Any = None
        try:
            result = fn(*args, **kwargs)
            return result
        except BaseException:
            ok = False
            raise
        finally:
            telemetry.emit_tool(
                name,
                dur_ms=(time.perf_counter() - start) * 1000,
                out_bytes=_out_bytes(result),
                ok=ok,
            )

    return sync_wrapper  # type: ignore[return-value]


def _tool(*d_args: Any, **d_kwargs: Any) -> Callable[[_F], _F]:
    """``mcp.tool()`` that also wraps the handler in :func:`_traced`.

    Used in place of ``@mcp.tool()`` so every tool is instrumented consistently
    (and any tool added later picks it up automatically).
    """
    register = mcp.tool(*d_args, **d_kwargs)

    def decorator(fn: _F) -> _F:
        return register(_traced(fn))  # type: ignore[no-any-return]

    return decorator


# ── Phase 1: Memory Tools ──────────────────────────────────────────────


@_tool()
def memcp_ping() -> str:
    """Health check. Returns server status and memory statistics."""
    status = memory_status()
    return json.dumps(
        {
            "status": "ok",
            "server": "MemCP",
            "version": __version__,
            "memory": status,
        },
        indent=2,
        default=str,
    )


@_tool()
async def memcp_remember(
    content: str,
    category: str = "general",
    importance: str = "medium",
    tags: str = "",
    summary: str = "",
    entities: str = "",
    project: str = "",
    session: str = "",
) -> str:
    """Save an important insight to persistent memory.

    Use this to remember key decisions, facts, user preferences, or technical
    findings that should be preserved across conversations.

    Args:
        content: The insight or fact to remember (be concise but complete)
        category: Type — decision, fact, preference, finding, todo, general
        importance: Priority — low, medium, high, critical
        tags: Comma-separated keywords for retrieval (e.g., "api,auth,v2")
        summary: Optional one-line summary
        entities: Optional comma-separated entities mentioned
        project: Optional project name
        session: Optional session ID
    """
    try:
        # Pass by keyword (not position) so a signature drift in remember()
        # can't silently scramble metadata onto the wrong parameter — the
        # 8377c279 drop-to-general/medium/no-tags failure mode.
        result = await run_sync(
            remember,
            content=content,
            category=category,
            importance=importance,
            tags=tags,
            summary=summary,
            entities=entities,
            project=project,
            session=session,
        )

        if result.get("_duplicate"):
            return json.dumps(
                {
                    "status": "duplicate",
                    "message": "This insight already exists in memory.",
                    "existing_id": result["id"],
                },
                indent=2,
                default=str,
            )

        response = {
            "status": "saved",
            "id": result["id"],
            "category": result["category"],
            "importance": result["importance"],
            "token_count": result["token_count"],
            "tags": result["tags"],
        }
        if result.get("_pruned"):
            response["pruned"] = result["_pruned"]
            response["message"] = (
                f"Saved. Auto-pruned {result['_pruned']}"
                " low-importance insights to stay within limits."
            )

        return json.dumps(response, indent=2, default=str)
    except (ValueError, MemCPError) as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)


@_tool()
async def memcp_recall(
    query: str = "",
    category: str = "",
    importance: str = "",
    limit: int = 10,
    max_tokens: int = 8000,
    project: str = "",
    session: str = "",
    scope: str = "project",
    use_graph: bool = True,
) -> str:
    """Retrieve insights from memory.

    Use this to find previously stored knowledge — decisions, preferences,
    technical findings. Call at session start to load relevant context.

    Args:
        query: Search term (searches content, tags, and summary)
        category: Filter by type
        importance: Filter by priority
        limit: Max results (default 10)
        max_tokens: Token budget — returns results until budget is exhausted.
            Default 8000 to stay under the typical context cap; pass 0 for unlimited.
        project: Filter by project
        session: Filter by session ID
        scope: "project" (default), "session" (current only), "all" (cross-project)
        use_graph: Use graph edge boosting in ranking (default True).
            Set False for keyword-only scoring against the same data.
    """
    try:
        results = await run_sync(
            recall,
            query,
            category,
            importance,
            limit,
            max_tokens,
            project,
            session,
            scope,
            use_graph,
        )

        if not results:
            return json.dumps(
                {
                    "status": "ok",
                    "count": 0,
                    "insights": [],
                    "message": "No matching insights found.",
                },
                indent=2,
                default=str,
            )

        insights = []
        for ins in results:
            insights.append(
                {
                    "id": ins["id"],
                    "content": ins["content"],
                    "category": ins.get("category", "general"),
                    "importance": ins.get("importance", "medium"),
                    "tags": ins.get("tags", []),
                    "project": ins.get("project", "default"),
                    "token_count": ins.get("token_count", 0),
                    "access_count": ins.get("access_count", 0),
                    "created_at": ins.get("created_at", ""),
                }
            )

        total_tokens = sum(i["token_count"] for i in insights)
        return json.dumps(
            {
                "status": "ok",
                "count": len(insights),
                "total_tokens": total_tokens,
                "insights": insights,
            },
            indent=2,
            default=str,
        )
    except (ValueError, MemCPError) as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)


@_tool()
async def memcp_get(insight_id: str) -> str:
    """Fetch a single insight by its ID (full id, or an unambiguous prefix).

    Use this to resolve a KNOWN id directly instead of fanning out recall
    calls. A full id always returns that exact insight. A prefix that matches
    exactly one insight returns it; a prefix that matches 2+ insights returns
    status "ambiguous" with the candidate ids so you can disambiguate (it never
    returns a wrong single result). A non-existent id returns status
    "not_found"; empty/invalid input returns a structured "error".

    Args:
        insight_id: Full id or an unambiguous id prefix.
    """
    try:
        result = await run_sync(get_insight, insight_id)
    except (ValueError, MemCPError) as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)

    if result is None:
        return json.dumps(
            {"status": "not_found", "insight_id": insight_id},
            indent=2,
            default=str,
        )
    if isinstance(result, list):
        return json.dumps(
            {
                "status": "ambiguous",
                "prefix": insight_id,
                "count": len(result),
                "candidates": [r.get("id") for r in result],
            },
            indent=2,
            default=str,
        )
    return json.dumps({"status": "ok", "insight": result}, indent=2, default=str)


@_tool()
async def memcp_grep(
    pattern: str,
    fields: str = "content",
    project: str = "",
    tags_all: str = "",
    category: str = "",
    importance: str = "",
    fixed_strings: bool = False,
    ignore_case: bool = True,
    context_chars: int = 120,
    limit: int = 50,
    include_archived: bool = False,
) -> str:
    """Exact / regex / tag-conjunction search over the insight store — no ranking, no embeddings.

    The deterministic, Direct-Corpus-Interaction complement to memcp_recall/memcp_search.
    Use this for KNOWN-ITEM lookups: an exact phrase, an ID/number (e.g. "4.73"), a precise
    tag conjunction, or a regex. Use memcp_recall/memcp_search instead for open-ended,
    conceptual DISCOVERY. The corpus is tiny so grep is exhaustive and instant. Pair with
    memcp_get(id) to read a full match.

    Args:
        pattern: Regex (Python re), or a literal string if fixed_strings=True. Required.
        fields: Comma-separated subset of content,summary,tags,entities (default "content").
        project: Filter to a project (exact).
        tags_all: Comma-separated tags ANDed together (e.g. "kind:kb,triage-agent").
        category: Exact category filter.
        importance: Exact importance filter.
        fixed_strings: Treat pattern as a literal (regex metacharacters escaped).
        ignore_case: Case-insensitive match (default True).
        context_chars: Chars of surrounding context per snippet (default 120).
        limit: Max matching insights (default 50).
        include_archived: Include archived insights (default False).
    """
    field_list = [f.strip() for f in fields.split(",") if f.strip()] or ["content"]
    tags_all_list = [t.strip() for t in tags_all.split(",") if t.strip()]
    try:
        results = await run_sync(
            grep,
            pattern=pattern,
            fields=field_list,
            project=project,
            tags_all=tags_all_list,
            category=category,
            importance=importance,
            fixed_strings=fixed_strings,
            ignore_case=ignore_case,
            context_chars=context_chars,
            limit=limit,
            include_archived=include_archived,
        )
    except (ValueError, MemCPError) as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)

    return json.dumps(
        {"status": "ok", "count": len(results), "results": results},
        indent=2,
        default=str,
    )


@_tool()
def memcp_forget(insight_id: str) -> str:
    """Remove an insight from memory by ID.

    Args:
        insight_id: The ID of the insight to remove
    """
    removed = forget(insight_id)
    if removed:
        return json.dumps(
            {"status": "removed", "id": insight_id},
            indent=2,
        )
    return json.dumps(
        {
            "status": "not_found",
            "message": f"No insight found with ID {insight_id!r}",
        },
        indent=2,
    )


@_tool()
def memcp_update(
    insight_id: str,
    tags: str = "",
    importance: str = "",
    category: str = "",
    summary: str = "",
    entities: str = "",
) -> str:
    """Update fields on an existing insight in place — preserves id and edges.

    Use this to retag, reclassify, or downgrade importance on existing
    insights without losing the graph structure (incoming/outgoing edges,
    access history). Pass empty strings for fields you don't want to change.
    Tags and entities replace the existing list (not append) — pass the full
    intended set, comma-separated.

    Args:
        insight_id: ID of the insight to update
        tags: Comma-separated tags (replaces existing). Leave empty to keep.
        importance: New importance level (low|medium|high|critical). Empty to keep.
        category: New category (decision|fact|finding|preference|
            todo|general|episode). Empty to keep.
        summary: New one-line summary. Empty to keep.
        entities: Comma-separated entities (replaces existing). Empty to keep.
    """
    try:
        updated = update(
            insight_id=insight_id,
            tags=tags if tags else None,
            importance=importance if importance else None,
            category=category if category else None,
            summary=summary if summary else None,
            entities=entities if entities else None,
        )
    except (ValueError, MemCPError) as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)

    if updated is None:
        return json.dumps(
            {
                "status": "not_found",
                "message": f"No insight found with ID {insight_id!r}",
            },
            indent=2,
        )
    return json.dumps(
        {
            "status": "updated",
            "id": insight_id,
            "insight": {
                "id": updated.get("id"),
                "category": updated.get("category"),
                "importance": updated.get("importance"),
                "tags": updated.get("tags"),
                "entities": updated.get("entities"),
                "summary": updated.get("summary"),
            },
        },
        indent=2,
        default=str,
    )


@_tool()
def memcp_archive(insight_id: str) -> str:
    """Archive a single insight by ID — moves it out of active recall, preserves it on disk.

    Use this to retire superseded or obsolete insights without losing them.
    Archived insights are restorable via `memcp_restore`. Unlike `memcp_forget`
    (which destroys the insight + edges), archive preserves the full record
    in `archive/insights.json` with an `archived_at` timestamp.

    Edges to/from the archived node are removed from the active graph but the
    archived record retains its tags, entities, and metadata so a future
    restore can re-seed connections.

    Args:
        insight_id: ID of the insight to archive
    """
    from memcp.core.errors import InsightNotFoundError
    from memcp.core.retention import archive_insight

    try:
        archived = archive_insight(insight_id)
    except InsightNotFoundError:
        return json.dumps(
            {
                "status": "not_found",
                "message": f"No insight found with ID {insight_id!r}",
            },
            indent=2,
        )
    except (ValueError, MemCPError) as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)

    return json.dumps(
        {
            "status": "archived",
            "id": archived.get("id"),
            "archived_at": archived.get("archived_at"),
            "category": archived.get("category"),
            "importance": archived.get("importance"),
            "summary": archived.get("summary") or (archived.get("content") or "")[:120],
        },
        indent=2,
        default=str,
    )


@_tool()
def memcp_status(project: str = "", session: str = "") -> str:
    """Current memory statistics — insight count, categories, importance distribution.

    Args:
        project: Filter stats by project
        session: Filter stats by session
    """
    status = memory_status(project=project, session=session)
    return json.dumps(
        {"status": "ok", **status},
        indent=2,
        default=str,
    )


@_tool()
async def memcp_index(project: str = "", save: bool = False) -> str:
    """Generate a progressive disclosure index of all insights.

    Returns a lightweight markdown overview grouped by category, with
    one-line summaries per insight. Read this FIRST before doing deeper
    queries — it helps you decide what to look up without loading
    full content into context.

    Optionally saves the index as index.md in the data directory.

    Args:
        project: Filter by project (empty = current project)
        save: If True, also write index.md to the data directory
    """
    try:
        index_md = await run_sync(generate_index, project)

        if save:
            from memcp.config import get_config

            config = get_config()
            index_path = config.data_dir / "index.md"
            index_path.write_text(index_md, encoding="utf-8")
            return json.dumps(
                {"status": "ok", "saved_to": str(index_path), "index": index_md},
                indent=2,
                default=str,
            )

        return json.dumps(
            {"status": "ok", "index": index_md},
            indent=2,
            default=str,
        )
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)


# ── Phase 2: Context + Chunking + Search Tools ─────────────────────────


@_tool()
def memcp_load_context(
    name: str,
    content: str = "",
    file_path: str = "",
    project: str = "",
) -> str:
    """Store content as a named context variable on disk.

    Use this to save large content (files, conversation history, code)
    that should be accessible without loading into the prompt.

    Args:
        name: Unique name for this context (alphanumeric, dots, hyphens, underscores)
        content: The content to store (provide content OR file_path, not both)
        file_path: Path to a file to load as context
        project: Optional project name
    """
    return load_context(name=name, content=content, file_path=file_path, project=project)


@_tool()
def memcp_inspect_context(name: str) -> str:
    """Inspect a stored context — metadata and preview without loading full content.

    Use this to check a context's type, size, and token count before deciding
    whether to load it into the prompt.

    Args:
        name: Context name to inspect
    """
    return inspect_context(name=name)


@_tool()
def memcp_get_context(name: str, start: int = 0, end: int = 0) -> str:
    """Read a stored context's content or a line range.

    Args:
        name: Context name
        start: Start line (1-indexed, 0 = from beginning)
        end: End line (1-indexed, inclusive, 0 = to end)
    """
    return get_context(name=name, start=start, end=end)


@_tool()
def memcp_chunk_context(
    name: str,
    strategy: str = "auto",
    chunk_size: int = 0,
    overlap: int = 0,
) -> str:
    """Split a stored context into navigable numbered chunks.

    Args:
        name: Context name (must already be loaded)
        strategy: Splitting strategy — auto, lines, paragraphs, headings, chars, regex
        chunk_size: Size per chunk (lines for 'lines', chars for 'chars', tokens for 'paragraphs')
        overlap: Overlap between chunks (lines or chars)
    """
    return do_chunk_context(name=name, strategy=strategy, chunk_size=chunk_size, overlap=overlap)


@_tool()
def memcp_peek_chunk(
    context_name: str,
    chunk_index: int,
    start: int = 0,
    end: int = 0,
) -> str:
    """Read a specific chunk from a chunked context.

    Args:
        context_name: Context name
        chunk_index: Chunk number (0-indexed)
        start: Start line within chunk (1-indexed, 0 = from beginning)
        end: End line within chunk (1-indexed, inclusive, 0 = to end)
    """
    return do_peek_chunk(context_name=context_name, chunk_index=chunk_index, start=start, end=end)


@_tool()
def memcp_filter_context(name: str, pattern: str, invert: bool = False) -> str:
    """Filter context content by regex pattern.

    Returns only lines matching (or not matching) the pattern.

    Args:
        name: Context name
        pattern: Regex pattern to match lines
        invert: If True, return lines that DON'T match the pattern
    """
    return do_filter_context(name=name, pattern=pattern, invert=invert)


@_tool()
def memcp_list_contexts(project: str = "") -> str:
    """List all stored context variables.

    Args:
        project: Filter by project name (empty = all projects)
    """
    return do_list_contexts(project=project)


@_tool()
def memcp_clear_context(name: str) -> str:
    """Delete a stored context and its chunks.

    Args:
        name: Context name to delete
    """
    return do_clear_context(name=name)


@_tool()
async def memcp_search(
    query: str,
    limit: int = 10,
    source: str = "all",
    max_tokens: int = 8000,
    project: str = "",
    scope: str = "project",
) -> str:
    """Search across memory insights and context chunks.

    Auto-selects the best available search method (BM25 > keyword).
    Install optional packages for better search: pip install memcp[search]

    Args:
        query: Search query
        limit: Max results (default 10)
        source: Where to search — "all" (default), "memory", "contexts"
        max_tokens: Token budget (default 8000; pass 0 for unlimited).
        project: Filter by project
        scope: "project" (default), "session", "all"
    """
    return await run_sync(
        do_search,
        query,
        limit,
        source,
        max_tokens,
        project,
        scope,
    )


# ── Phase 3: Graph Memory Tools ───────────────────────────────────────


@_tool()
def memcp_related(
    insight_id: str,
    edge_type: str = "",
    depth: int = 1,
) -> str:
    """Traverse graph from an insight — find connected knowledge.

    Discovers insights related via semantic similarity, temporal proximity,
    causal chains, or shared entities.

    Args:
        insight_id: The ID of the insight to start from
        edge_type: Filter by edge type — semantic, temporal, causal, entity (empty = all)
        depth: How many hops to traverse (default 1)
    """
    return do_related(insight_id=insight_id, edge_type=edge_type, depth=depth)


@_tool()
def memcp_graph_stats(project: str = "") -> str:
    """Graph statistics — node count, edge counts by type, top entities.

    Shows how knowledge is connected in the graph.

    Args:
        project: Filter by project (empty = all projects)
    """
    return do_graph_stats(project=project)


# ── Phase 6: Retention Lifecycle Tools ─────────────────────────────────


@_tool()
def memcp_retention_preview(archive_days: int = 0, purge_days: int = 0) -> str:
    """Preview what would be archived or purged — dry-run, no changes made.

    Shows candidates for archiving (stale, low-access items) and purging
    (archived items past retention period). Items with high importance,
    frequent access, or protected tags are immune from archiving.

    Args:
        archive_days: Override archive threshold (default from env, 30 days)
        purge_days: Override purge threshold (default from env, 180 days)
    """
    return do_retention_preview(archive_days=archive_days, purge_days=purge_days)


@_tool()
def memcp_retention_run(archive: bool = True, purge: bool = False) -> str:
    """Execute retention actions — archive old items, optionally purge.

    Archiving compresses and moves stale items to the archive directory.
    Purging permanently deletes archived items past the purge threshold
    and logs metadata to purge_log.json for audit.

    Args:
        archive: Archive eligible items (default True)
        purge: Purge archived items past retention period (default False)
    """
    return do_retention_run(archive=archive, purge=purge)


@_tool()
def memcp_reindex(
    index: str = "all",
    mode: str = "incremental",
    force: bool = False,
) -> str:
    """Rebuild derived indexes (graph edges, entity index, embeddings) from the node store.

    Indexes are derived views; if stale (nodes written since last build) or
    the model version changed, they are rebuilt. Usually invoked automatically
    by the SessionStart hook; also available for manual use after model changes
    or corruption.

    Args:
        index: which index to rebuild ('all' | 'edges' | 'entities' | 'embeddings')
        mode: 'incremental' (default, only changed nodes) or 'full' (wipe + rebuild)
        force: bypass the staleness check
    """
    return do_reindex(index=index, mode=mode, force=force)


@_tool()
def memcp_sync() -> str:
    """Force an immediate cross-machine snapshot sync (pull newer, then publish local).

    Sync normally happens automatically on a background interval; use this to
    sync NOW without reconnecting. No-op if no snapshot dir is configured.
    """
    return do_sync()


@_tool()
def memcp_restore(name: str, item_type: str = "auto") -> str:
    """Restore an archived context or insight back to active.

    Decompresses archived contexts and re-inserts archived insights
    into the knowledge graph.

    Args:
        name: Context name or insight ID to restore
        item_type: "context", "insight", or "auto" (tries both)
    """
    return do_restore(name=name, item_type=item_type)


# ── Phase 7: Multi-Project & Session Tools ─────────────────────────


@_tool()
def memcp_projects() -> str:
    """List all projects with insight/context/session counts.

    Shows every project that has data in MemCP.
    """
    return do_projects()


@_tool()
def memcp_sessions(project: str = "", limit: int = 20) -> str:
    """List sessions, optionally filtered by project.

    Args:
        project: Filter by project (empty = all)
        limit: Max sessions to return (default 20)
    """
    return do_sessions(project=project, limit=limit)


# ── Step 2: Cognitive Memory Tools ─────────────────────────────────


@_tool()
async def memcp_reinforce(
    insight_id: str,
    helpful: bool = True,
    note: str = "",
) -> str:
    """Provide feedback on an insight — mark it as helpful or misleading.

    Helpful insights get a score boost and stronger edges.
    Misleading insights get penalized. This closes the learning loop.

    Args:
        insight_id: The ID of the insight to reinforce
        helpful: True if the insight was helpful, False if misleading
        note: Optional note about why
    """
    return await run_sync(do_reinforce, insight_id, helpful, note)


@_tool()
async def memcp_consolidation_preview(
    threshold: float = 0.0,
    limit: int = 20,
    project: str = "",
) -> str:
    """Preview groups of similar insights that could be merged.

    Finds near-duplicate or very similar insights and groups them.
    Dry-run — no changes made. Use memcp_consolidate to merge.

    Args:
        threshold: Similarity threshold (0 = use default 0.85)
        limit: Max groups to return
        project: Filter by project
    """
    return await run_sync(do_consolidation_preview, threshold, limit, project)


@_tool()
async def memcp_consolidate(
    group_ids: str,
    keep_id: str = "",
    merged_content: str = "",
) -> str:
    """Merge a group of similar insights into one.

    Keeps the best insight (most accessed by default), merges tags/entities
    from others, redirects edges, and deletes duplicates.

    Args:
        group_ids: Comma-separated insight IDs to merge
        keep_id: Which ID to keep (default: most accessed)
        merged_content: Optional override for the merged content
    """
    return await run_sync(do_consolidate, group_ids, keep_id, merged_content)


# ── Episodic Memory Tools ─────────────────────────────────────────


@_tool()
async def memcp_log_episode(
    task: str,
    approach: str,
    outcome: str,
    notes: str = "",
    quality_score: str = "",
    tags: str = "",
    entities: str = "",
    project: str = "",
    session: str = "",
) -> str:
    """Log a task episode — what was attempted, how, and what happened.

    Episodic memory records outcomes of actions so the agent can learn
    which strategies work and avoid repeating mistakes. Use this after
    completing a research task, debugging session, or any multi-step work.

    Args:
        task: What was the task or goal (e.g., "Research Cedar role for job fit")
        approach: How was it approached (e.g., "Analyzed JD, cross-referenced with resume")
        outcome: Result — success, partial, failure
        notes: Lessons learned, what to do differently next time
        quality_score: Optional 0.0–1.0 rating of result quality
        tags: Comma-separated keywords
        entities: Comma-separated entities (companies, tools, people)
        project: Optional project name
        session: Optional session ID
    """
    # Build structured episode content
    parts = [
        f"Task: {task}",
        f"Approach: {approach}",
        f"Outcome: {outcome}",
    ]
    if notes:
        parts.append(f"Notes: {notes}")
    if quality_score:
        parts.append(f"Quality: {quality_score}")
    content = "\n".join(parts)

    # Determine importance from outcome
    importance = "medium"
    if outcome == "failure":
        importance = "high"  # failures are valuable to remember
    elif outcome == "success" and quality_score:
        try:
            if float(quality_score) >= 0.9:
                importance = "high"
        except ValueError:
            pass

    # Add outcome to tags for easy filtering
    tag_parts = [t.strip() for t in tags.split(",") if t.strip()] if tags else []
    tag_parts.append(f"outcome:{outcome}")
    if quality_score:
        tag_parts.append(f"quality:{quality_score}")

    try:
        result = await run_sync(
            remember,
            content,
            "episode",
            importance,
            ",".join(tag_parts),
            f"{outcome}: {task[:80]}",
            entities,
            project,
            session,
        )

        if result.get("_duplicate"):
            return json.dumps(
                {"status": "duplicate", "existing_id": result["id"]},
                indent=2,
                default=str,
            )

        return json.dumps(
            {
                "status": "logged",
                "id": result["id"],
                "task": task,
                "outcome": outcome,
                "importance": importance,
            },
            indent=2,
            default=str,
        )
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)


@_tool()
async def memcp_recall_episodes(
    query: str = "",
    outcome: str = "",
    limit: int = 5,
    project: str = "",
    scope: str = "project",
) -> str:
    """Recall past episodes to inform strategy for a similar task.

    Use this before starting a task to see what approaches worked
    or failed in similar past situations.

    Args:
        query: Search for similar tasks (e.g., "company research")
        outcome: Filter by outcome — success, partial, failure (empty = all)
        limit: Max results (default 5)
        project: Filter by project
        scope: "project" (default), "session", "all"
    """
    try:
        results = await run_sync(
            recall,
            query,
            "episode",
            "",
            limit,
            0,
            project,
            "",
            scope,
        )

        # Filter by outcome tag if specified
        if outcome:
            results = [r for r in results if f"outcome:{outcome}" in r.get("tags", [])]

        if not results:
            return json.dumps(
                {
                    "status": "ok",
                    "count": 0,
                    "episodes": [],
                    "message": "No matching episodes found.",
                },
                indent=2,
                default=str,
            )

        episodes = []
        for ins in results:
            episodes.append(
                {
                    "id": ins["id"],
                    "content": ins["content"],
                    "tags": ins.get("tags", []),
                    "importance": ins.get("importance", "medium"),
                    "created_at": ins.get("created_at", ""),
                    "access_count": ins.get("access_count", 0),
                }
            )

        return json.dumps(
            {"status": "ok", "count": len(episodes), "episodes": episodes},
            indent=2,
            default=str,
        )
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)


def _init_session() -> None:
    """Detect project and register a new session on server startup.

    Ensures state.json has current_project/current_session and
    sessions.json tracks the session lifecycle.
    """
    from memcp.core.project import detect_project, generate_session_id, register_session

    project = detect_project()
    session_id = generate_session_id(project)
    register_session(session_id, project)


def main() -> None:
    """Run the MemCP MCP server.

    Startup must NOT do the snapshot pull/union synchronously: that work can
    take longer than the MCP initialize handshake window (a large Drive snapshot
    + first-run model load), which made the server hang and fail to connect.
    The pull happens lazily on the first tool call instead (no handshake limit),
    and `_use_graph()` already routes the first op to the graph backend whenever
    a snapshot dir is set, so nothing is stranded in memory.json (§3.7).
    """
    _init_session()
    mcp.run()
