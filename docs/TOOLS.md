# MemCP Tool Reference

24 MCP tools across 8 phases. All tools return JSON strings.

## Phase 1: Memory Tools

### memcp_ping

```
memcp_ping() → str
```

Health check. Returns server status, version, and memory statistics.

**Example:**
```json
{"status": "ok", "server": "MemCP", "version": "0.1.0", "memory": {"total_insights": 42, ...}}
```

---

### memcp_remember

```
memcp_remember(
    content: str,
    category: str = "general",
    importance: str = "medium",
    tags: str = "",
    summary: str = "",
    entities: str = "",
    project: str = "",
    session: str = "",
) → str
```

Save an insight to persistent memory. Creates a graph node with auto-generated edges (semantic, temporal, causal, entity). This tool is async — it runs in a background thread to avoid blocking concurrent MCP calls.

| Parameter | Description |
|-----------|-------------|
| `content` | The insight or fact to remember (be concise but complete) |
| `category` | `decision`, `fact`, `preference`, `finding`, `todo`, `general` |
| `importance` | `low`, `medium`, `high`, `critical` |
| `tags` | Comma-separated keywords for retrieval (e.g., `"api,auth,v2"`) |
| `summary` | Optional one-line summary |
| `entities` | Optional comma-separated entities mentioned |
| `project` | Project name (auto-detected from git root if empty) |
| `session` | Session ID (auto-populated from current session if empty) |

**Examples:**
```
memcp_remember("Client prefers 500ml bottles", category="preference", importance="high", tags="client,packaging")
memcp_remember("Decided to use SQLite for graph backend", category="decision", importance="critical", tags="architecture,db")
memcp_remember("API rate limit is 100/min", category="fact", tags="api,limits")
```

**Tips:**
- **Secret detection**: Content is scanned for API keys, tokens, and credentials before storage. If a secret is detected, the tool returns `status: "error"` with a description. Patterns include AWS keys (`AKIA...`), OpenAI/Anthropic keys, GitHub tokens, Stripe keys, private key blocks, and password assignments. Disable with `MEMCP_SECRET_DETECTION=false`.
- **Duplicate detection**: Exact-match duplicates (SHA-256 hash) return `status: "duplicate"` with the existing ID. Optional semantic deduplication (cosine similarity ≥ 0.95) is available when embeddings are configured — enable with `MEMCP_SEMANTIC_DEDUP=true`.
- At the 10K insight limit, low-importance insights are auto-pruned
- Use `importance="critical"` for rules that must never be forgotten

---

### memcp_recall

```
memcp_recall(
    query: str = "",
    category: str = "",
    importance: str = "",
    limit: int = 10,
    max_tokens: int = 0,
    project: str = "",
    session: str = "",
    scope: str = "project",
) → str
```

Retrieve insights from memory with intent-aware graph traversal. This tool is async — it runs in a background thread to avoid blocking concurrent MCP calls.

| Parameter | Description |
|-----------|-------------|
| `query` | Search term (searches content, tags, summary). Prefix with "why"/"when"/"who" for intent-aware traversal |
| `category` | Filter by type |
| `importance` | Filter by priority |
| `limit` | Max results (default 10) |
| `max_tokens` | Token budget — returns results until budget exhausted (0 = unlimited) |
| `project` | Filter by project (auto-detected if empty) |
| `session` | Filter by session ID |
| `scope` | `"project"` (default), `"session"` (current only), `"all"` (cross-project) |

**Examples:**
```
memcp_recall(query="client preferences")
memcp_recall(importance="critical")
memcp_recall(query="why SQLite?")  # Intent-aware: follows causal edges
memcp_recall(query="database", max_tokens=500)  # Token-budgeted
memcp_recall(scope="all")  # Cross-project search
```

**Tips:**
- Call at session start: `memcp_recall(importance="critical")` to load critical rules
- `max_tokens` is useful to control how much context enters the prompt
- Access count is incremented on each recall, affecting importance decay

**Ranking knobs (env vars):** The intent ranker decouples two independent terms.

| Var | Default | Effect |
|-----|---------|--------|
| `MEMCP_KIND_WEIGHT` | `true` | Apply the `kind:` demotion as a multiplicative factor on the final relevance score (kb/untagged 1.0×, op 0.5×, pointer 0.3×, episode 0.2×). Independent of edge counts — a `kind:op` note never outranks durable `kind:kb` knowledge on equal keywords. |
| `MEMCP_EDGE_BOOST` | `false` | Add the intent-weighted edge boost to the score. **Off by default** (Phase 2 eval Arm D): when off, the two per-node `COUNT(*) FROM edges` queries do not execute at all — a ~8.9× p50 latency win (92.5 ms → 10.4 ms on the 1501-node freeze) with no significant nDCG loss. Set `MEMCP_EDGE_BOOST=true` to opt back in. |
| `MEMCP_SEMANTIC_RECALL` | `true` | Blend a semantic term into recall — embed the query once and sweep it against the stored node embeddings, so abstract phrasings can bridge to concrete nodes that share ~no keywords. Score = `(1 − w)·keyword + w·semantic`. **On by default**: the governing pre-registered flip gate passed all three criteria on the full embedding+theme stack — semantic-ON beats OFF on nDCG@10 (two-sided sign test p=0.0041), zero contamination delta, and p50 latency under the 75 ms cap. (An earlier gate on weaker `model2vec` static embeddings found no bridging; the bge-small hq tier + theme enrichment closed that gap.) A transiently unavailable or uninstalled embedder degrades the call to keyword-only (no exception, no index churn). Set `MEMCP_SEMANTIC_RECALL=false` to disable. |
| `MEMCP_SEMANTIC_WEIGHT` | `0.5` | Blend weight `w` on the semantic term (0 = pure keyword, 1 = pure semantic). Only consulted when `MEMCP_SEMANTIC_RECALL=true`. |
| `MEMCP_EMBEDDER_TIER` | `auto` | Which embedder the semantic path uses: `hq` (FastEmbed / **bge-small-en-v1.5** — the contextual tier that bridges abstract behavioral queries), `model2vec` (fast static), `keyword` (none), or `auto` (hq if `fastembed` installed → model2vec → keyword). **Degrade chain:** a transiently unavailable tier falls back to keyword-only *for that call* without flipping the embeddings `model_version` (no full re-embed storm); a genuine tier switch is a real model change and re-embeds once. `MEMCP_EMBEDDING_PROVIDER` (legacy) still forces a concrete provider and bypasses the ladder. |

**Theme cache (Phase 4, machine-local derived data — ADR-014).** When `MEMCP_SEMANTIC_RECALL` is on, each node's *embedded* text is `themes + "\n" + content[:2000]` when a valid theme exists, plain content otherwise. Themes are 1–2 behavioral/conceptual lines generated **blind** from node content (never from queries) by `scripts/theme_backfill.py` and stored in `<data_dir>/cache/themes.sqlite`, keyed by `(node_id, content_sha)`. They are **not** a column on the synced `nodes` table — in-place mutations of synced rows don't propagate (INSERT-OR-IGNORE union), so themes are local, rebuildable, and add zero sync surface. A content change invalidates the theme automatically (sha mismatch → treated as missing → plain-content embed). Each machine pays its own cheap backfill; a missing/locked theme cache silently degrades to plain content (never load-bearing).

`use_graph=False` (the keyword-only path) disables **all** of these terms regardless of the env vars above — its meaning is unchanged.

---

### memcp_topic

```
memcp_topic(
    slug: str,
    project: str = "",
    include_archived: bool = False,
) → str
```

Render a **living doc** as "compiled truth on top + chronological timeline below" (the gbrain shape), read-only, from its `topic:<slug>` chain. Additive — no new storage, no sync surface, no schema change; same posture as `memcp_grep`.

**The content-versioning convention it reads.** A living doc (e.g. a runbook, a release-notes page, an architecture-state note) is a *topic* whose updates are ordinary new-id `memcp_remember()` saves — never in-place content edits, which don't converge cross-machine under the snapshot union merge. Each row carries:

| Tag | Meaning |
|-----|---------|
| `topic:<slug>` | Stable entrypoint; every row of the doc carries it. Look up the topic, never a remembered id — so you can't read a stale version by accident. |
| `entry:compiled` | A full current-understanding restatement (the "compiled truth"). |
| `entry:log` | A dated evidence/correction append (one fact/change). |
| `supersedes:<id8>` | On a compiled row, the 8-char id-prefix of the prior compiled row. |

Rationale and rejected alternatives (in-place `content` edits; local-only contexts): `docs/SPEC-content-versioning.md`.

| Parameter | Description |
|-----------|-------------|
| `slug` | The topic slug — the value after `topic:` (e.g. `deploy-runbook`). Required. |
| `project` | Scope to one project so the same slug in two projects doesn't merge. |
| `include_archived` | Include archived rows (default `false`). |

Returns `{status, slug, current, count, timeline, warnings}`:
- `current` — the latest `entry:compiled` row with full content (or `null` if the topic has no compiled row yet).
- `timeline` — every row for the topic, ascending by `(created_at, id)`, each a lightweight `{id, entry_type, created_at, tags, supersedes, preview}`. Pair with `memcp_get(id)` for a full entry.
- `warnings` — flags a compiled head whose `supersedes:` link is missing or points at the wrong prior compiled row (the one behavioral gap the design can only *detect*, not prevent). An unknown slug returns empties, not an error.

**Examples:**
```
memcp_topic("deploy-runbook")
memcp_topic("architecture-state", project="memcp")
```

---

### memcp_forget

```
memcp_forget(insight_id: str) → str
```

Remove an insight from memory by ID. Also deletes all connected graph edges.

**Example:**
```
memcp_forget("a1b2c3d4")
```

---

### memcp_status

```
memcp_status(project: str = "", session: str = "") → str
```

Current memory statistics — insight count, categories, importance distribution, total tokens stored.

**Examples:**
```
memcp_status()
memcp_status(project="my-project")
```

---

## Phase 2: Context + Chunking + Search Tools

### memcp_load_context

```
memcp_load_context(
    name: str,
    content: str = "",
    file_path: str = "",
    project: str = "",
) → str
```

Store content as a named context variable on disk. Provide `content` OR `file_path`, not both.

| Parameter | Description |
|-----------|-------------|
| `name` | Unique name (alphanumeric, dots, hyphens, underscores) |
| `content` | The content to store |
| `file_path` | Path to a file to load as context |
| `project` | Project name (auto-detected if empty) |

**Examples:**
```
memcp_load_context("report", file_path="docs/ARCHITECTURE.md")
memcp_load_context("session-summary", content="Key decisions from this session...")
```

**Tips:**
- Max context size: 10MB (configurable via `MEMCP_MAX_CONTEXT_SIZE_MB`)
- Duplicate content is detected via SHA-256 hash
- Auto-detects content type: markdown, python, json, csv, text

---

### memcp_inspect_context

```
memcp_inspect_context(name: str) → str
```

Inspect a stored context — metadata and 5-line preview without loading full content. This is the RLM "peeking" pattern: see what's there before deciding whether to load it.

**Example:**
```
memcp_inspect_context("report")
# Returns: type=markdown, size=45KB, tokens=~11000, preview of first 5 lines
```

---

### memcp_get_context

```
memcp_get_context(name: str, start: int = 0, end: int = 0) → str
```

Read a stored context's content or a line range.

| Parameter | Description |
|-----------|-------------|
| `name` | Context name |
| `start` | Start line (1-indexed, 0 = from beginning) |
| `end` | End line (1-indexed, inclusive, 0 = to end) |

**Examples:**
```
memcp_get_context("report")  # Full content
memcp_get_context("report", start=10, end=30)  # Lines 10-30 only
```

---

### memcp_chunk_context

```
memcp_chunk_context(
    name: str,
    strategy: str = "auto",
    chunk_size: int = 0,
    overlap: int = 0,
) → str
```

Split a stored context into navigable numbered chunks.

| Parameter | Description |
|-----------|-------------|
| `name` | Context name (must already be loaded) |
| `strategy` | `auto`, `lines`, `paragraphs`, `headings`, `chars`, `regex` |
| `chunk_size` | Size per chunk (lines for `lines`, chars for `chars`) |
| `overlap` | Overlap between chunks |

**Examples:**
```
memcp_chunk_context("report", strategy="headings")  # Split on ## headers
memcp_chunk_context("code", strategy="lines", chunk_size=100, overlap=10)
memcp_chunk_context("doc", strategy="auto")  # Auto-selects based on content type
```

**Tips:**
- `auto` selects: headings for markdown, lines for code, paragraphs for prose
- Target: ~10 chunks for auto strategy

---

### memcp_peek_chunk

```
memcp_peek_chunk(
    context_name: str,
    chunk_index: int,
    start: int = 0,
    end: int = 0,
) → str
```

Read a specific chunk from a chunked context. This is the RLM "peeking" pattern.

| Parameter | Description |
|-----------|-------------|
| `context_name` | Context name |
| `chunk_index` | Chunk number (0-indexed) |
| `start` | Start line within chunk (1-indexed, 0 = from beginning) |
| `end` | End line within chunk (1-indexed, inclusive, 0 = to end) |

**Example:**
```
memcp_peek_chunk("report", 2)  # Read chunk #2
memcp_peek_chunk("report", 0, start=1, end=5)  # First 5 lines of chunk 0
```

---

### memcp_filter_context

```
memcp_filter_context(name: str, pattern: str, invert: bool = False) → str
```

Filter context content by regex pattern. Returns only matching (or non-matching) lines. This is the RLM "grepping" pattern.

**Examples:**
```
memcp_filter_context("code", pattern="def\\s+\\w+")  # Find function definitions
memcp_filter_context("log", pattern="ERROR|WARN")  # Find errors and warnings
memcp_filter_context("config", pattern="^#", invert=True)  # Non-comment lines
```

---

### memcp_list_contexts

```
memcp_list_contexts(project: str = "") → str
```

List all stored context variables with metadata summaries.

**Example:**
```
memcp_list_contexts()  # All contexts
memcp_list_contexts(project="my-project")  # Project-scoped
```

---

### memcp_clear_context

```
memcp_clear_context(name: str) → str
```

Delete a stored context and its chunks.

**Example:**
```
memcp_clear_context("old-report")
```

---

### memcp_search

```
memcp_search(
    query: str,
    limit: int = 10,
    source: str = "all",
    max_tokens: int = 0,
    project: str = "",
    scope: str = "project",
) → str
```

Search across memory insights and context chunks. Auto-selects the best available search method. This tool is async — it runs in a background thread to avoid blocking concurrent MCP calls.

| Parameter | Description |
|-----------|-------------|
| `query` | Search query |
| `limit` | Max results (default 10) |
| `source` | `"all"` (default), `"memory"`, `"contexts"` |
| `max_tokens` | Token budget (0 = unlimited) |
| `project` | Filter by project |
| `scope` | `"project"` (default), `"session"`, `"all"` |

**Examples:**
```
memcp_search("authentication patterns")
memcp_search("database", source="memory", max_tokens=500)
memcp_search("error handling", source="contexts")
```

**Tips:**
- Response includes `method` field showing which search tier was used
- Response includes `capabilities` showing which tiers are available
- BM25 index is cached in memory and invalidated automatically when insights change — no per-query rebuild
- Install `pip install memcp[search]` for BM25, `[semantic]` for embeddings, `[hnsw]` for HNSW vector index

---

## Phase 3: Graph Memory Tools

### memcp_related

```
memcp_related(
    insight_id: str,
    edge_type: str = "",
    depth: int = 1,
) → str
```

Traverse graph from an insight — find connected knowledge.

| Parameter | Description |
|-----------|-------------|
| `insight_id` | The ID of the insight to start from |
| `edge_type` | Filter: `semantic`, `temporal`, `causal`, `entity` (empty = all) |
| `depth` | How many hops to traverse (default 1) |

**Examples:**
```
memcp_related("a1b2c3d4")  # All connected insights
memcp_related("a1b2c3d4", edge_type="entity")  # Same entities only
memcp_related("a1b2c3d4", edge_type="causal", depth=2)  # Cause chain, 2 hops
```

---

### memcp_graph_stats

```
memcp_graph_stats(project: str = "") → str
```

Graph statistics — node count, edge counts by type, top 10 entities.

**Example:**
```
memcp_graph_stats()
# Returns: node_count, edge_counts (semantic/temporal/causal/entity), top_entities
```

---

## Phase 4: Cognitive Memory Tools

### memcp_reinforce

```
memcp_reinforce(
    insight_id: str,
    helpful: bool = True,
    note: str = "",
) → str
```

Provide feedback on an insight — mark it as helpful or misleading. Affects future ranking via `feedback_score`.

| Parameter | Description |
|-----------|-------------|
| `insight_id` | The ID of the insight to reinforce |
| `helpful` | `True` if the insight was helpful, `False` if misleading |
| `note` | Optional note about why |

**Logic:**
- `helpful=True`: `feedback_score += 0.1`, boost connected edges by 0.02
- `helpful=False`: `feedback_score -= 0.2`, weaken connected edges by 0.05
- `feedback_score` clamped to `[-1.0, 1.0]`
- Ranking adjustment: `total_score *= (1 + feedback_score * 0.3)`

**Examples:**
```
memcp_reinforce("a1b2c3d4", helpful=True)
memcp_reinforce("a1b2c3d4", helpful=False, note="Outdated info")
```

---

### memcp_consolidation_preview

```
memcp_consolidation_preview(
    threshold: float = 0.85,
    limit: int = 20,
    project: str = "",
) → str
```

Preview groups of similar insights that could be merged. Dry-run — no changes made.

| Parameter | Description |
|-----------|-------------|
| `threshold` | Similarity threshold for grouping (default 0.85, configurable via `MEMCP_CONSOLIDATION_THRESHOLD`) |
| `limit` | Max groups to return (default 20) |
| `project` | Filter by project (empty = all) |

Uses embedding similarity (if available) or keyword Jaccard overlap. Groups found via Union-Find.

**Example:**
```
memcp_consolidation_preview(threshold=0.7)
# Returns: groups of similar insights with their IDs and content
```

---

### memcp_consolidate

```
memcp_consolidate(
    group_ids: str,
    keep_id: str = "",
    merged_content: str = "",
) → str
```

Merge a group of similar insights into one.

| Parameter | Description |
|-----------|-------------|
| `group_ids` | Comma-separated IDs to merge |
| `keep_id` | Which ID to keep (default: most accessed) |
| `merged_content` | Optional override for the kept insight's content |

**Merge logic:**
- Union all tags and entities from the group
- Keep the highest importance level
- Sum access counts
- Re-point edges from deleted nodes to the kept node
- Delete the other nodes

**Examples:**
```
memcp_consolidate("id1,id2,id3")
memcp_consolidate("id1,id2", keep_id="id1")
memcp_consolidate("id1,id2", merged_content="Combined insight text")
```

---

## Phase 6: Retention Lifecycle Tools

### memcp_retention_preview

```
memcp_retention_preview(archive_days: int = 0, purge_days: int = 0) → str
```

Dry-run — show what would be archived or purged without making changes.

| Parameter | Description |
|-----------|-------------|
| `archive_days` | Override archive threshold (default from env: 30 days) |
| `purge_days` | Override purge threshold (default from env: 180 days) |

Items are immune from archiving if:
- `importance` is `"critical"` or `"high"`
- `access_count >= 3`
- Tags contain `"keep"`, `"important"`, or `"pinned"`

**Example:**
```
memcp_retention_preview()  # Show candidates with default thresholds
memcp_retention_preview(archive_days=7)  # More aggressive: 7-day archive threshold
```

---

### memcp_retention_run

```
memcp_retention_run(archive: bool = True, purge: bool = False) → str
```

Execute retention actions.

| Parameter | Description |
|-----------|-------------|
| `archive` | Archive eligible items — compress contexts to `.gz`, move insights to archive (default True) |
| `purge` | Purge archived items past retention period — permanently deletes and logs to `purge_log.json` (default False) |

**Examples:**
```
memcp_retention_run()  # Archive only (safe default)
memcp_retention_run(archive=True, purge=True)  # Archive + purge
```

---

### memcp_restore

```
memcp_restore(name: str, item_type: str = "auto") → str
```

Restore an archived context or insight back to active.

| Parameter | Description |
|-----------|-------------|
| `name` | Context name or insight ID to restore |
| `item_type` | `"context"`, `"insight"`, or `"auto"` (tries both) |

**Examples:**
```
memcp_restore("old-report")  # Restore archived context
memcp_restore("a1b2c3d4", item_type="insight")  # Restore archived insight
```

---

## Phase 7: Multi-Project & Session Tools

### memcp_projects

```
memcp_projects() → str
```

List all projects with aggregate stats: insight count, context count, session count, last activity.

Aggregates data from graph.db nodes, contexts meta.json, and sessions.json.

**Example:**
```
memcp_projects()
# Returns: [{name, insight_count, context_count, session_count, last_activity}, ...]
```

---

### memcp_sessions

```
memcp_sessions(project: str = "", limit: int = 20) → str
```

List sessions, optionally filtered by project. Sorted by most recent first.

| Parameter | Description |
|-----------|-------------|
| `project` | Filter by project (empty = all) |
| `limit` | Max sessions to return (default 20) |

**Examples:**
```
memcp_sessions()  # All sessions
memcp_sessions(project="memcp", limit=5)  # Last 5 sessions for memcp project
```

---

## Tool Summary Table

| # | Tool | Phase | Purpose |
|---|------|-------|---------|
| 1 | `memcp_ping` | 1 | Health check + stats |
| 2 | `memcp_remember` | 1 | Save insight (graph node + auto-edges) |
| 3 | `memcp_recall` | 1 | Intent-aware graph retrieval |
| 4 | `memcp_topic` | 1 | Living-doc "compiled truth + timeline" over a `topic:` chain |
| 5 | `memcp_forget` | 1 | Remove insight + edges |
| 6 | `memcp_status` | 1 | Memory statistics |
| 7 | `memcp_load_context` | 2 | Store named context variable |
| 8 | `memcp_inspect_context` | 2 | Metadata + preview without loading |
| 9 | `memcp_get_context` | 2 | Read content or line slice |
| 10 | `memcp_chunk_context` | 2 | Split into numbered chunks |
| 11 | `memcp_peek_chunk` | 2 | Read a specific chunk |
| 12 | `memcp_filter_context` | 2 | Regex filter within context |
| 13 | `memcp_list_contexts` | 2 | List all variables |
| 14 | `memcp_clear_context` | 2 | Delete variable |
| 15 | `memcp_search` | 2 | Search across memory + contexts |
| 16 | `memcp_related` | 3 | Graph traversal |
| 17 | `memcp_graph_stats` | 3 | Graph statistics |
| 18 | `memcp_reinforce` | 4 | Feedback — mark insight as helpful/misleading |
| 19 | `memcp_consolidation_preview` | 4 | Preview similar insight groups (dry-run) |
| 20 | `memcp_consolidate` | 4 | Merge similar insights into one |
| 21 | `memcp_retention_preview` | 6 | Dry-run retention actions |
| 22 | `memcp_retention_run` | 6 | Execute archive/purge |
| 23 | `memcp_restore` | 6 | Restore from archive |
| 24 | `memcp_projects` | 7 | List projects with stats |
| 25 | `memcp_sessions` | 7 | Browse sessions |
