# SPEC — `memcp_grep` tool (Direct Corpus Interaction over the insight store)

Status: QUEUED (2026-06-19). Origin: idea-scan of the "Unix tools beat vector search" / DCI line of work
(DCI arXiv 2605.05242, Pi-Serini 2605.10848, GrepSeek 2605.29307).
build belongs here in the memcp repo. Draft-only until Jeff greenlights the build.

## Why

MemCP's only retrieval interface for insights today is **similarity-first** (`memcp_search`/`memcp_recall`:
BM25 + fuzzy + semantic + graph-edge boosting). That is a *fixed similarity interface* — it cannot express
the things the DCI papers show matter for exact retrieval: exact lexical match, sparse-clue **conjunctions**,
regex, ID-prefix lookup, local-context checks. The whole corpus is tiny — **~1700 rows, ~1MB of text** in
`nodes.content` of `~/.memcp-local/graph.db` — so exact grep is instant and exhaustive.

Evidence this is real, not theoretical: Claude already routes around the MCP interface to `sqlite3 graph.db`
for the wedged-DB probe (global CLAUDE.md), and during the 2026-06-19 session fell back to `sqlite3`/`grep`
for a known-item lookup (the `4.73` triage insight) that semantic recall returned only diffusely. A behavioral
rule now lives in global CLAUDE.md ("route known-item → grep/sqlite, discovery → semantic"); this tool makes
that ergonomic and first-class instead of hand-rolled SQL.

## What (interface)

Tool name: `memcp_grep`

Args:
- `pattern` (str, required): regex (Python `re`, default) or literal if `fixed_strings=True`.
- `fields` (list[str], default `["content"]`): subset of `content`, `summary`, `tags`, `entities`.
- `project` (str, optional): filter to a project (column filter, exact).
- `tags_all` (list[str], optional): boolean AND over tags (e.g. `["kind:kb","triage-agent"]`).
- `category` / `importance` (str, optional): exact column filters.
- `ignore_case` (bool, default True).
- `context_chars` (int, default 120): chars of surrounding context to return around each match.
- `limit` (int, default 50): max matching insights.
- `include_archived` (bool, default False): respect `archived_at`.

Returns (no ranking, no embedding — deterministic): list of
`{ id, category, importance, project, tags, matches: [{field, snippet}], created_at }`.
Pairs with existing `memcp_get(id)` to read the full insight.

## How (implementation sketch)

- Read straight from SQLite: `SELECT id,content,summary,tags,entities,category,importance,project,created_at,archived_at FROM nodes`.
  At 1MB the whole-table scan + Python `re` is sub-50ms; no FTS/index work required for v1.
- Apply column filters (`project`/`category`/`importance`/archived) in SQL; apply regex + `tags_all` in Python.
- `tags`/`entities` are JSON-text columns — `json.loads` then match.
- Respect the same locking/contention posture as other read paths (read-only connection; don't block on writer).

## Acceptance / tests (oracle — review before trusting, per TDD rule)

1. Literal known-item: `memcp_grep("4.73", fixed_strings=True)` returns the triage V2 insight `8fa882b0…`.
2. Tag conjunction: `tags_all=["kind:kb","triage-agent"]` returns only insights with BOTH tags.
3. Regex: `pattern=r"\b4\.\d{2}\b"` matches score-shaped strings; case-insensitivity toggles correctly.
4. Negative: a pattern with no hits returns `[]`, not an error.
5. Determinism: same args → byte-identical result ordering (sort by `created_at,id`).
6. Archived excluded by default; included when `include_archived=True`.
7. Field scoping: a term only in `summary` is found with `fields=["summary"]`, not with `fields=["content"]`.

## Non-goals (boundary — DCI improves the interface, it does NOT replace the index)

- Do **not** remove or alter semantic search, hybrid ranking, or the `memcp_related` graph. Those are the
  *discovery* path and a genuine strength (Pi-Serini: well-tuned lexical stays competitive; dense isn't useless).
- No new sync surface / flat-file mirror in v1 (that was a considered B-track alternative; `memcp_grep` is the
  narrower wedge that needs no new storage).
