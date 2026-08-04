# Default read scope is "all" — the data dir is the isolation boundary

**Decision (2026-08-04):** read tools (`memcp_recall`, `memcp_search`,
`memcp_recall_episodes`) default to `scope="all"`. `memcp_grep` and
`memcp_topic` already searched the whole store when no project filter was
given, and keep doing so. Writes are unaffected — they stamp the server's
process identity.

## Why

Isolation between bodies of knowledge that must never mix belongs at the
**data directory** (`MEMCP_DATA_DIR`): separate stores, separate backups,
separate blast radius. Within one store, `project` is a label for filtering
and attribution — not a wall.

The previous default, `scope="project"`, silently narrowed every read to
whatever project the server had resolved at startup. Measured consequence on
the originating store: writes had forked across project labels (195 of 394
insights outside the default project), and the default read path could not
see them — 3 of the top 8 hits for real queries were invisible, while
`memcp_status` reported the scoped count as if it were the store total, so
nothing surfaced the gap for two weeks.

A default must be honest about what it hides. `scope="all"` hides nothing;
narrowing is a visible, deliberate act (`scope="project"` or a `project=`
filter).

## What this means for setup

- **One store per domain** (recommended): e.g. one `MEMCP_DATA_DIR` for work,
  another for personal. Cross-project reads inside a store are then reads
  within one domain — exactly what you want.
- **Many unrelated repos sharing one store:** if you genuinely want per-repo
  walls, that is what separate data dirs are for. A per-repo `project` label
  still works as a filter, but it is not the isolation mechanism.

## Consequences

- Rank order still governs relevance — a query only matching one project's
  content returns that content regardless of scope.
- `memcp_status` names its scope and always reports whole-store totals
  alongside (`store.total_insights`, `store.by_project`), so a label fork is
  visible the day it starts.
