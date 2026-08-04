# MemCP — Instructions for Claude Code Sessions

## Project Attribution (IMPORTANT — changed in 0.4.0)

Write tools no longer accept a `project` parameter. The server stamps every
write with the identity it resolved once at startup (`MEMCP_PROJECT` env var,
else git repo / directory name). To control where findings land, set
`MEMCP_PROJECT` in the MCP server's env block — never per call.

Reads default to `scope="all"` (the data dir is the isolation boundary;
project labels are filters). Pass `project=`/`scope="project"` on read tools
when you want one project's view. Rationale: `docs/design/default-scope.md`.

## Build-vs-Adopt: Push Back Before Building Infra (IMPORTANT)

Before building any **new infrastructure/plumbing feature** for MemCP — sync,
storage, indexing, replication, retention, locking, embeddings infra, etc. —
**push back first**: check whether a mature open-source tool already implements
it, and say so explicitly. Default to **adopt over build** for commodity infra;
reserve custom builds for genuinely unserved niches.

Why: "vibe coding infrastructure" tends to silently recreate existing tools. A
worked example lives in MemCP itself — the cross-machine sync layer largely
reimplements [cr-sqlite](https://github.com/vlcn-io/cr-sqlite)'s merge (additive
union + tombstones ≈ its grow-only set + delete-log), and most of
`snapshot_sync.py`'s complexity is the tax of forcing Google Drive to be the
transport. What *is* legitimately custom is the no-server / personal-Drive /
no-loss packaging, which no polished OSS tool serves.

The push-back is **not to block** new work — it's to make the build-vs-adopt
decision conscious and evidence-based. When a new infra feature comes up:
1. Name the closest OSS option(s) and what they do / don't cover.
2. State whether the custom build is justified (unserved niche) or duplicative.
3. If duplicative, recommend adopting; if it's a real niche, proceed and note why.

Canonical rule: MemCP preference `abc7e6ba` (critical). Related: the cr-sqlite
migration line `3aac48e4` and the prior-art analysis `1820c7b9`. Ties to the
global rule "verify empirical evidence before a verdict."

## Queue Hygiene — op-rows are claims, not facts (added 2026-07-01)

Origin: the 2026-07-01 backlog sweep found ~70% of the queued memcp backlog
already done but every `kind:op` row still open, and a cross-surface
suggestion recommended building a tool hours after it shipped. Canonical
finding: MemCP `e69e0d5d`.

1. **Before acting on any queued/suggested project:** spend 60 seconds
   verifying it is still open — `git log --oneline` since the row's date plus
   `memcp_grep` on its tags. A queue item is a claim, not a fact.
2. **After completing queued work:** `memcp_update` the corresponding
   `kind:op` row to a RESOLVED-prefixed summary + `resolved` tag **in the same
   session** that did the work. Prefix ids are accepted (5785ecb).

(No hook can enforce this — there is no deterministic "acting on a stale
queue item" trigger — so it lives here, where every session loads it.)

## Design Rules Learned From Evals (2026-07-01)

- **No happy-path warnings.** A log warning that fires during normal operation
  (the old per-tick "diverged" warning during the write-to-push window) trains
  operators to ignore the one real event. A warning must indicate something
  worth acting on; expected churn logs at debug.
- **ID-resolution symmetry.** Any tool taking `insight_id` resolves
  full-id-or-unambiguous-prefix via `get_insight` (memcp_get / memcp_update /
  memcp_archive). Deliberate exception: destructive ops (`memcp_forget`) stay
  full-id-only.
- **Performance envelope** (10k eval: `docs/eval/scale-stress-2026-07-01.md`):
  write and recall are both O(corpus size) — ~94 ms/insert and 100–173 ms
  recall p50 at 10k nodes. Bulk imports (1k+ nodes) should defer edge building
  and run `rebuild_edges` once afterward; revisit ANN indexing if production
  recall p50 exceeds ~150 ms. That p50 is now measured live —
  `memcp_status` surfaces `telemetry.recall_latency` (overall + per-path
  semantic/keyword/filter p50/p95, with an `over_trigger` flag against the
  ~150 ms threshold), so the trigger reads off a real gauge rather than a guess.
  ⚠️ Caveat (2026-07-15): the current gauge counts sync-contaminated samples —
  do NOT act on `over_trigger` until the gauge fix (exclude in-flight-sync
  samples, require n≥10) lands. All-time recall p50 = 471 ms is likewise
  contaminated by session-start sync; raw SQLite is fast (7.7 ms count).
- **Never open a snapshot blob on the Drive mount with SQLite** (measured
  2026-07-15, `docs/eval/sync-lock-stall-measurement-2026-07-15.md`):
  page-random reads of a dataless File Provider blob run at ~0.4 MB/s (236 s
  for 91 MB; warm = instant), while a sequential `shutil.copyfile` streams at
  real network bandwidth. Always stage to a local temp first
  (`SnapshotSync._stage_blob`), then validate/union the local copy. Same eval:
  Drive I/O (publish, GC blob reads) must never run under the write flock —
  writers wait UNTIMED (`WriteLock._guard` in-process, blocking
  `fcntl.LOCK_EX` cross-process; the 30 s timeout bounds only the lease) —
  and per-call `close()` must never fold a peer snapshot inline
  (close-flush gate, oracle `tests/unit/test_sync_lock_stall_regression.py`).

## Session Startup

At the beginning of every session:
1. Call `memcp_recall(importance="critical")` to load all critical rules and decisions
2. Call `memcp_status()` to see memory statistics
3. Call `memcp_index()` — progressive-disclosure map of all insights before deeper queries
4. If working on a specific project, call `memcp_recall(project="project-name", limit=20)` to load project context

## When to Save

Call `memcp_remember()` whenever you encounter:
- **Decisions** (`category="decision"`): Architecture choices, library selections, approach decisions
- **Facts** (`category="fact"`): API limits, configuration values, system constraints
- **Preferences** (`category="preference"`): User's coding style, formatting preferences, tool preferences
- **Findings** (`category="finding"`): Bug discoveries, performance insights, edge cases found
- **TODOs** (`category="todo"`): Tasks to complete later, follow-up items
- **General** (`category="general"`): Anything else worth remembering

## Importance Levels

- `critical` — Must never be forgotten (e.g., "Never push to main without PR")
- `high` — Important context (e.g., "Client requires WCAG 2.1 AA compliance")
- `medium` — Useful knowledge (e.g., "API rate limit is 100/min")
- `low` — Nice to have (e.g., "Tried approach X, didn't work well")

## Tags

Always add relevant tags for better retrieval:
```
memcp_remember("...", tags="api,auth,jwt")
```

## `kind:` Tag — REQUIRED on Every Save

Every `memcp_remember()` call MUST include exactly one `kind:` tag. This separates durable knowledge from operational/handoff content so default recall doesn't get muddled.

| Tag | Use for | Importance ceiling | Lifespan |
|---|---|---|---|
| `kind:kb` | Durable knowledge — decisions, facts, findings, preferences | `critical` | indefinite |
| `kind:op` | Operational — open tasks, follow-ups, in-flight workstream | `medium` | archive after workstream closes |
| `kind:pointer` | Session handoffs / EOD pointers | `low` | archive ~14 days |
| `kind:episode` | What happened in a session — replay, not recall | `low` | archive aggressively |

Examples:
```
memcp_remember("ADR-007 chose fcose over cose-bilkent because <reasons>", category="decision", importance="high", tags="kind:kb,layout,viz")
memcp_remember("Follow up on benchmark fixture skip in pytest", category="todo", importance="medium", tags="kind:op,tests,pytest")
memcp_remember("EOD 2026-04-30 — wrapped memcp_update tool, retrofit pending", category="todo", importance="low", tags="kind:pointer,session-end")
```

For default recall, prefer `tags="kind:kb"` so durable findings rank above session ephemera. For session resume, query `tags="kind:pointer"` explicitly.

## `topic:` / `entry:` / `supersedes:` — Content Versioning for Living Docs

A **living doc** — a record that keeps changing (a vendor master list, an
architecture-state note) — must NOT be maintained as a "vN → vN+1 supersedes"
chain of ad-hoc new insights. That pattern has caused silent data loss twice,
because a new version gets authored against whatever prior row the author
happened to load (read a stale v2, silently drop everything added since). It is
also unfixable by editing content in place: `memcp_update` has **no `content`
param on purpose**, and an in-place content edit would **not** propagate
cross-machine under the snapshot union merge (only new-id INSERTs and tombstones
converge). Full analysis: `docs/SPEC-content-versioning.md`.

**The convention** — a living doc is a **topic**, and every update is an ordinary
new-id `memcp_remember()` save (sync-safe by construction) carrying these tags:

| Tag | Meaning |
|---|---|
| `topic:<slug>` | The stable entrypoint. **Every** row of the doc carries it (e.g. `topic:vendor-master-list`). You look up the *topic*, never a remembered id — so you can't read a stale version by accident. |
| `entry:compiled` | A full current-understanding restatement (the "compiled truth"). |
| `entry:log` | A dated evidence/correction append (a single fact/change, not a full restatement). |
| `supersedes:<id8>` | On an `entry:compiled` row, the 8-char id-prefix of the **prior** `entry:compiled` row. Forces you to fetch the true latest compiled head before writing the next one. |

Old rows are never destroyed — the topic is an append-only audit trail, matching
MemCP's no-loss philosophy.

**Reading a topic** — use **`memcp_topic(slug)`**: it returns the latest
`entry:compiled` row as `current` (full content) on top, then every row for the
topic as a chronological `timeline`, and `warnings` when a compiled head's
`supersedes:` link is missing or points at the wrong row. It works via
`memcp_grep(pattern=".", tags_all="topic:<slug>")` too if you want raw rows.

**Writing an update:**
```python
# 1. Read the current head to get the id you're superseding:
#    memcp_topic("vendor-master-list")  → note current["id"][:8]
# 2a. Append a small correction as it happens:
memcp_remember("Added Acme to the vendor list (confirmed 2026-07-01)", category="general",
               tags="kind:kb,topic:vendor-master-list,entry:log")
# 2b. Or restate the whole current understanding, citing the prior compiled head:
memcp_remember("<full current list>", category="general",
               tags="kind:kb,topic:vendor-master-list,entry:compiled,supersedes:013c7417")
```

Use a topic for anything that will be revised over time and read as "the current
state." For a one-off fact that won't change, a plain `memcp_remember()` is fine.

## Updating Existing Insights

`memcp_update(insight_id, tags=..., importance=..., category=..., summary=..., entities=...)` mutates an insight in place — **preserves id and all edges**. Use it to retag, reclassify, or downgrade importance without losing graph structure. Tags and entities replace the existing list, so pass the full intended set comma-separated. Empty fields are left untouched.

## Provenance on Factual Saves — REQUIRED

`memcp_remember()` with `category="fact"` or `category="finding"` MUST include a `source:` line in the **content body**. Forms:

- External: `source: <URL>, fetched <YYYY-MM-DD>`
- Local artifact (screenshot, .playwright-mcp capture, repo file): `source: <relative-path>, captured <YYYY-MM-DD>`
- Live conversation: `source: user-conversation, <YYYY-MM-DD>`
- Derived from prior insight(s): `source: inference from <insight-id>[, <insight-id>…]`

Exempt: `category="decision"` and `category="preference"`. Existing un-sourced facts: lazy backfill via `memcp_update` only when about to gate a real action.

Full rationale + the triggering insight (`831716d36f9d7956`) are in global `~/.claude/CLAUDE.md` under the same heading.

## Tool Quick Reference

### Phase 1: Memory Tools
| Tool | Purpose |
|------|---------|
| `memcp_ping()` | Health check + memory stats |
| `memcp_remember(content, category, importance, tags)` | Save an insight (graph node + auto-edges) |
| `memcp_recall(query, category, importance, limit, max_tokens)` | Intent-aware graph retrieval (DISCOVERY — semantic/hybrid) |
| `memcp_grep(pattern, fields, project, tags_all, category, importance, fixed_strings, ignore_case, context_chars, limit, include_archived)` | KNOWN-ITEM exact/regex/tag-conjunction search — no ranking, no embeddings (DCI). Use for an exact phrase, ID/number, or tag conjunction; pair with `memcp_get(id)`. Scaling checkpoint: grep-first is exhaustive-and-instant at the current ~1,700 rows; revisit the routing split if the store approaches ~10K rows (operator threshold "grep starts to suck" — Garry Tan on gbrain, 2026-06-30; log the real crossing when grep first feels noisy rather than trusting the number) |
| `memcp_topic(slug, project, include_archived)` | Render a living-doc topic as "compiled truth on top + chronological timeline below" from its `topic:<slug>` chain (read-only, no new storage). Warns on a missing/mis-pointing `supersedes:` link. See the content-versioning convention above |
| `memcp_update(insight_id, tags, importance, category, summary, entities)` | Mutate an insight in place — preserves id + edges |
| `memcp_archive(insight_id)` | Move a single insight to archive (preserves on disk, restorable via `memcp_restore`) |
| `memcp_forget(insight_id)` | Remove an insight + edges (destructive, not recoverable) |
| `memcp_status(project, session)` | Memory statistics |

### Phase 2: Context + Chunking + Search Tools
| Tool | Purpose |
|------|---------|
| `memcp_load_context(name, content, file_path)` | Store content as named disk variable |
| `memcp_inspect_context(name)` | Metadata + preview without loading |
| `memcp_get_context(name, start, end)` | Read content or line slice |
| `memcp_chunk_context(name, strategy, chunk_size, overlap)` | Split into numbered chunks |
| `memcp_peek_chunk(context_name, chunk_index, start, end)` | Read a specific chunk |
| `memcp_filter_context(name, pattern, invert)` | Regex filter within context |
| `memcp_list_contexts(project)` | List all variables |
| `memcp_clear_context(name)` | Delete variable |
| `memcp_search(query, limit, source, max_tokens)` | Search across memory + contexts |

### Phase 3: Graph Memory Tools
| Tool | Purpose |
|------|---------|
| `memcp_related(insight_id, edge_type, depth)` | Traverse graph — find connected insights |
| `memcp_graph_stats(project)` | Graph statistics: nodes, edges, top entities |

### Phase 6: Retention Lifecycle Tools
| Tool | Purpose |
|------|---------|
| `memcp_retention_preview(archive_days, purge_days)` | Dry-run — show what would be archived/purged |
| `memcp_retention_run(archive, purge)` | Execute archive/purge actions |
| `memcp_restore(name, item_type)` | Restore archived context or insight |

### Phase 7: Multi-Project & Session Tools
| Tool | Purpose |
|------|---------|
| `memcp_projects()` | List all projects with insight/context/session counts |
| `memcp_sessions(project, limit)` | Browse sessions by project |

## RLM Sub-Agents

MemCP includes 4 Claude Code sub-agents that implement the RLM (Recursive Language Model) map-reduce pattern. They run as independent Claude sessions with their own context windows, so they don't consume your main context.

### Setup

Sub-agent templates live in `agents/` and are deployed to `~/.claude/agents/` (user-level) by the installer, making them available across all projects:

```bash
# Via installer (recommended)
bash scripts/install.sh    # Step 6 deploys agents

# Manual deployment
mkdir -p ~/.claude/agents
cp agents/memcp-*.md ~/.claude/agents/
```

Each agent uses proper Claude Code frontmatter (`tools`, `mcpServers`, `model`, `maxTurns`). They reference the `memcp` MCP server and restrict their tool access to only the MCP tools they need.

### Available Sub-Agents

| Sub-Agent | Model | Purpose |
|-----------|-------|---------|
| `memcp-analyzer` | Haiku | Single-chunk analysis using peek-identify-load-analyze |
| `memcp-mapper` | Haiku | MAP phase — process one chunk in parallel |
| `memcp-synthesizer` | Sonnet | REDUCE phase — combine mapper outputs into coherent answer |
| `memcp-entity-extractor` | Haiku | Extract entities and relationships from content |

> **Recursion depth: single-level by design.** Mappers and the synthesizer have MCP data tools only — no `Task` tool — so they cannot spawn further sub-agents. Dispatch is from the main session: fan out N mappers in parallel → reduce via one synthesizer. The paper's recursive sub-call pattern is intentionally replaced with typed MCP tools (see [ADR-006](docs/adr/006-mcp-tools-over-python-repl.md)).

### Pattern 1: Single-Chunk Analysis

When you need to answer a question about a stored context without loading it all:

1. `memcp_search(query)` — find which contexts are relevant
2. Launch `memcp-analyzer` with the context name and question
3. The analyzer follows the RLM pattern: inspect, identify, load only relevant sections, answer with citations

### Pattern 2: Map-Reduce (Large Context Analysis)

When analyzing a context too large for a single pass:

1. `memcp_chunk_context(name, strategy="auto")` — partition the context
2. Launch N `memcp-mapper` instances in **background** (one per chunk):
   - Each mapper gets: context_name, chunk_index, and the question
   - Mappers run on Haiku in parallel — cheap and fast
3. Collect all mapper outputs
4. Launch `memcp-synthesizer` in **foreground** with the question + all mapper outputs:
   - Synthesizer runs on Sonnet — better reasoning for combining results
   - Cross-references with `memcp_recall()` and `memcp_related()` for verification
   - May save new insights via `memcp_remember()` if synthesis produces novel findings

### Pattern 3: Entity Enrichment

After storing new content, enrich it with LLM-extracted entities:

1. Store content: `memcp_remember(content, ...)` or `memcp_load_context(name, content)`
2. Launch `memcp-entity-extractor` in background with the content
3. Collect extracted entities
4. Update the insight: `memcp_remember(content, entities="entity1,entity2,...")`

This creates richer entity edges in the MAGMA graph, improving future
`memcp_related()` and `memcp_recall()` results.

### When to Use Sub-Agents vs Direct Tools

**Use sub-agents when:**
- Analyzing large contexts (>4000 tokens) — avoid loading everything into main
  context. Fidelity verified 2026-07-01: a mapper+synthesizer smoke eval
  extracted with zero fabricated figures and honest coverage-flagging (MemCP
  `33e19061`) — lean on this to conserve main-session context/tokens.
- Answering complex questions that need cross-referencing across chunks
- Processing multiple chunks in parallel for speed
- Extracting entities from lengthy content

**Use direct tools when:**
- Simple recall: `memcp_recall(query)` — fast, already optimized
- Saving a quick insight: `memcp_remember(content)` — one call
- Checking status: `memcp_status()`, `memcp_graph_stats()`
- Small context operations: inspect, peek, filter on known locations

## Auto-Save Hooks

Hooks are configured in `~/.claude/settings.json` and run automatically — you don't need to trigger them:

- **PreCompact**: Before `/compact`, you'll get a blocking message requiring you to save context first
- **Progressive reminders**: At 10+ turns (consider), 20+ (recommended), 30+ (action required) — only when context >= 55% full
- **Reset counter**: After `memcp_remember()` or `memcp_load_context()`, the turn counter resets

