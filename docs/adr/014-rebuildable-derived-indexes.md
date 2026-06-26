# ADR-014: Rebuildable Derived Indexes

**Status:** Accepted
**Date:** 2026-04-20
**Related:** ADR-002 (tiered search), ADR-003 (MAGMA 4-graph)

## Context

MemCP's derived indexes — the graph `edges` table, the `entity_index` table, and the embeddings cache at `~/.memcp/cache/insight_embeddings.npz` — were previously maintained by mutating state in place during `remember` / `forget` / `consolidate` calls. There was no mechanism to rebuild any of these indexes from the `nodes` table alone.

Consequences of that design:

- Switching the embedding model required manual cache deletion with undefined behavior.
- Changing edge-generation heuristics required a migration script rather than a replay.
- Corruption in the `edges` table had no repair path — rewriting the file was the only option.
- On a new machine, embeddings would rebuild lazily per-query without visibility, and cached edges had no way to be validated against the DB.
- Jeff runs MemCP on two machines (desktop + laptop) with the SQLite DB synced via Google Drive. Any derived cache that lived alongside the DB was exposed to GDrive sync races.

DDIA Part III (Derived Data) argues that any index, cache, or materialized view should be rebuildable from the source of truth. If it cannot be rebuilt, the "derived view" has accidentally become primary state.

## Decision

Treat the `nodes` table as the authoritative source of truth and make derived indexes rebuildable via a new `memcp_reindex` MCP tool. Introduce a monotonic `meta.revision` counter in `graph.db` that bumps on every content-mutating write (`remember`, `forget`, `_auto_prune_graph`, `consolidate`, retention archive/purge). Each index stores the revision it was built against plus a `model_version` string identifying the derivation function. A SessionStart hook (`hooks/session_start_reindex.py`) triggers rebuild automatically when any index is stale.

Staleness is a pure integer comparison: `index.built_against_revision < store.revision`, with an additional check on `model_version` to catch derivation-function changes that don't correspond to content mutations.

Storage zones:

| Index | Storage | Cross-machine behavior |
|---|---|---|
| Graph edges | `edges` + `index_meta` tables in `graph.db` (shared via GDrive) | Rebuilt by whichever machine opens the first session after a write; both benefit. |
| Entity index | `entity_index` + `index_meta` tables in `graph.db` (shared) | Same. |
| Embeddings | `~/.memcp/cache/insight_embeddings.npz` + `embeddings.meta.json` (local per machine) | Each machine rebuilds its own on first stale session. |

Rebuild modes:
- **incremental** (default): only process nodes created after `index.built_at`.
- **full**: wipe the index and regenerate everything. Triggered automatically on `model_version` mismatch or by `force=True`.

## Alternatives considered

- **Per-index dirty flags.** More granular, but requires a shared coordination primitive across two machines. A monotonic revision in the shared DB subsumes this naturally — no extra files, no extra sync semantics.
- **Full append-only event log of writes.** Strongly DDIA-aligned and enables time-travel debugging and non-destructive consolidation, but significantly more invasive. Deferred as a separate project; this ADR is the first bounded step in that direction.
- **Rebuild only on manual invocation.** Simpler but requires discipline. SessionStart automation eliminates a common forgetting failure mode and the hook costs are negligible at current scale (<1 s for tens of insights).
- **Per-machine dirty-flag file synced via GDrive.** Introduces a second piece of shared state that can desync from the DB. The in-DB revision cannot desync with itself, which is the whole point.
- **Rebuild the BM25 cache via this tool too.** Current BM25 behavior (invalidate on write, rebuild lazily on next query) is already acceptable and self-healing. Adding it to `memcp_reindex` would be feature creep; revisit if it becomes a bottleneck.

## Consequences

**Positive:**

- Embedding-model changes become routine: bump the `MODEL_NAME` class attribute (or the `model_version` heuristic), next session rebuilds.
- Edge-heuristic changes become routine: bump `_EDGES_MODEL_VERSION` in `reindex.py`, next session rebuilds.
- Corruption in `edges` or `entity_index` is repairable with `memcp_reindex(force=True)`.
- Cross-machine consistency is automatic: whichever machine opens the next session sees the revision gap and rebuilds locally; no coordination primitive beyond the DB.
- Creates a clear foundation for the eventual event-log project.

**Negative / limitations:**

- Concurrent multi-machine writes are still unsupported. This ADR assumes GDrive-sequential sessions. Documented as a limitation in `docs/ARCHITECTURE.md`.
- Hebbian edge weights, feedback scores, and destructive consolidation merges are *not* rebuildable. These are accepted non-goals — they mutate state destructively and have no authoritative source to replay from.
- Session-start rebuild adds latency. Mitigated by incremental mode and the `reindex_latency_warn_ms` threshold (default 3000 ms). At current corpus size it is sub-second.
- A third file on disk (`embeddings.meta.json`) per machine. Minor cost.

## Implementation

- Spec: `docs/superpowers/specs/2026-04-20-memcp-reindex-design.md`
- Plan: `docs/superpowers/plans/2026-04-20-memcp-reindex.md`
- Core: `src/memcp/core/reindex.py`, `src/memcp/core/revision.py`
- Tool: `src/memcp/tools/reindex_tools.py`, `memcp_reindex` in `src/memcp/server.py`
- Hook: `hooks/session_start_reindex.py`
- Config: `MEMCP_REINDEX_ON_SESSION_START`, `MEMCP_REINDEX_LATENCY_WARN_MS`
- Schema: `meta` + `index_meta` tables in `src/memcp/core/node_store.py`
