# SPEC — Content versioning for living insights (the vN-chain drift fix)

Status: DRAFT (2026-07-01). Origin: idea-scan comparing Karpathy's "LLM Wiki" to Garry Tan's `gbrain` "compiled truth + timeline" pattern. Precedent format: `docs/SPEC-memcp_grep.md`.

## 1. Problem statement

`memcp_update(insight_id, tags=, importance=, category=, summary=, entities=)` mutates **metadata only**. There is no `content` parameter — confirmed at two layers:
- Tool signature: `src/memcp/server.py:447` (no `content` arg).
- Storage allowlist: `NodeStore.update_node` (`src/memcp/core/node_store.py:636-647`) permits `access_count, last_accessed_at, effective_importance, summary, entities, tags, feedback_score, category, importance, archived_at`. **`content` is deliberately absent.**

Because content can't be edited, the workaround for a "living" fact is to `memcp_remember()` a brand-new insight that restates everything and says "supersedes vN". That vN-chain has caused silent data loss twice in practice:
1. **A periodically re-ranked reference list**: v1 → v2 → v3 → v3.1, where v3 was authored from the **stale v2** instead of the true reconciled state, silently dropping a batch of entries added in between. Caught only by a human noticing it was "a replacement, not a rearrangement."
2. **MemCP's own corpus**: `d88e32a2` ("MEMORY SYSTEM DESIGN — CURRENT as of 2026-06-11") states it "SUPERSEDES `b3ccd3e1`'s 5-layer pyramid" — the same vN-chain, inside MemCP's self-documentation.

**Root cause:** there is no content-edit path AND no single stable entrypoint per topic, so a new version is authored against whichever prior row the author happened to load. There's nothing that forces "there is exactly one current version" or "here is the append-only evidence trail."

## 2. Investigation findings

### 2.1 `revision.py` does NOT solve this (the lead was a false positive)
`src/memcp/core/revision.py` is a **monotonic `meta.revision` counter for derived-index staleness detection**, not per-insight content history. `bump_revision()` increments a single global `meta` row; `get/set/invalidate_index_meta` track which revision each derived index (edges, entity_index, embeddings) was built against (ADR-014). It stores **no prior content, no per-node version chain**. Finding `47fc8ffa`'s label "revision/version-history" is a mischaracterization. Critically, **ADR-014 §Alternatives explicitly lists "Full append-only event log of writes … Deferred as a separate project"** — so the append-only machinery this bug wants was consciously *not* built. There is nothing to wrap; a fix is net-new (but small).

### 2.2 The sync merge model — the hard constraint, stated precisely
`src/memcp/core/snapshot_sync.py` syncs **only `graph.db`**. The merge (`_union_pull`, lines 602-695) is:
- **Nodes:** `INSERT OR IGNORE INTO nodes (...) SELECT ... FROM snap.nodes` keyed on `id` (PRIMARY KEY, `node_store.py:200`). **If the id already exists locally, the incoming row is ignored.**
- **Deletes:** propagate via the `tombstones` table applied as a newest-wins deny-set (lines 652-674).
- Fresh machine with no local DB adopts the snapshot wholesale (line 561-567); an **established** machine only unions.

**Consequence (this is the whole ballgame):** under this merge, exactly two operations converge cross-machine — (1) **INSERT of a new-`id` row**, and (2) a **tombstone delete**. An **in-place UPDATE of an existing `id` does NOT converge on an established peer** — the peer already has that id, so `INSERT OR IGNORE` keeps its *old* copy forever. This is precisely finding `d88e32a2`: "in-place row mutations do NOT propagate cross-machine." It means **even today's metadata-only `memcp_update` silently fails to converge** on an established second machine (the `_mark_durable()` re-push at `node_store.py:688` bumps a snapshot generation but the union still ignores the changed id on the peer). The FK-check baseline logic in `_union_pull` (lines 645, 676-685) is the exact code path behind the gen-210 / 43-insight silent loss (`684834b2`) — it is fragile and must not be touched.

**Design rule that falls out:** anything that must converge MUST be expressed as a **new-`id` INSERT** (or a tombstone). A canonical/"compiled" view must therefore be either **derived on read** from append rows, or itself a **new row per revision** — never an in-place field edit.

### 2.3 Contexts (`memcp_load_context`) are overwritable but **local-only** — not sync-safe
`context_store.load` (`src/memcp/core/context_store.py:83-157`) writes `data_dir/contexts/{name}/content.md` + `meta.json` and **overwrites by name** (with a content-hash duplicate short-circuit). So contexts *are* freely overwritable. **But `snapshot_sync.py` has zero references to `contexts_dir`** — contexts live under `data_dir` (`config.py:216`) and are never included in the Drive snapshot. A "canonical living doc" stored as a context would **silently not exist on the other machine** — a *new* silent-divergence failure, exactly MemCP's signature failure mode. Contexts are appropriate only for genuinely single-machine, ephemeral dumps (their current idea-scan use). The motivating cases (a cross-machine reference list, an architecture-state doc) span machines, so contexts fail them.

### 2.4 No `supersedes` edge type exists
`edges.edge_type` has a CHECK constraint `IN ('semantic','temporal','causal','entity')` (`node_store.py:219`). A first-class "supersedes" pointer as a new edge_type would require a CHECK-constraint migration. A **tag/metadata convention avoids that entirely** (see design).

### 2.5 Anti-reinvention sanity check (`abc7e6ba`)
The closest OSS/prior-art pattern is an **append-only event log / git-log-over-a-topic** (and gbrain's "compiled truth + timeline"). MemCP already gestures at this: ADR-014 names the append-only log as its deferred north star, and `memcp_grep` set the precedent for an additive, read-only, no-new-storage tool over `nodes`. This feature is **not** infra in the sync/storage/replication sense the rule polices — it's a thin user-facing convention over the existing `nodes` INSERT path. A custom (tiny) build is justified because the reusable substrate (`memcp_remember` = new-id INSERT) already exists; we're adding a convention + optional read helper, not new plumbing.

## 3. Design options

### Option A — Topic log: append-only rows + a derived "compiled truth + timeline" view  ← RECOMMENDED
**What (concretely):** A "living doc" becomes a **topic**, identified by a stable tag `topic:<slug>` (e.g. `topic:reference-list`). Every update is a **new `memcp_remember()` row** (new id) carrying:
- `topic:<slug>` (the stable entrypoint), and
- an entry-type tag: `entry:log` (a dated evidence/correction append) or `entry:compiled` (a full current-understanding restatement), and
- for `entry:compiled` rows, a `supersedes:<id8>` tag naming the **prior compiled row's** id.

Read side (optional thin tool, `memcp_grep`-style, no new storage): **`memcp_topic(slug)`** returns the latest `entry:compiled` row on top, then all rows for that topic ordered by `created_at` as the timeline — the gbrain shape. Until/unless that tool is built, the convention works today via `memcp_grep(pattern="topic:reference-list", fields=["tags"])`.

**Sync interaction — safe, by construction:** every write is a **new-id INSERT**, which is one of the only two convergent operations under `_union_pull` (§2.2). The compiled view is **derived on read**, so nothing needs an in-place UPDATE and nothing new touches the merge algorithm, the FK-check, or tombstones. Zero new sync surface (identical posture to `memcp_grep`).

**Schema migration:** **None.** Tags are an existing JSON column; `topic:`/`entry:`/`supersedes:` are pure conventions. No new edge_type (avoids the §2.4 CHECK migration).

**Why it kills the bug:** there is now **one stable entrypoint per topic**, so an author updating the doc looks up the *topic*, not a remembered id — they can't "read v2 by accident." The `supersedes:<id8>` guard forces the author to fetch the true latest compiled id before writing the next one. The old rows are never destroyed (full audit trail), matching MemCP's no-loss / append-only philosophy.

**Effort:** **S** for the convention alone (documentation + a helper snippet); **M** if `memcp_topic` is built as a first-class read tool. Recommend shipping the convention first, tool second.

**Risk:** **LOW.** Failure modes are behavioral, not silent-sync: (a) an author appends an `entry:compiled` without reading the latest — mitigated by the single entrypoint + the `supersedes:` cite requirement, and detectable because a compiled row missing/duplicating a `supersedes:` link is greppable; (b) tag typos partition a topic — mitigable with a slug allowlist if `memcp_topic` is built. No path here can silently diverge cross-machine, because every artifact is a converging INSERT.

### Option B — Add `content` to `memcp_update` (the naive in-place edit)  ← REJECTED
**What:** add a `content` param to `memcp_update` and to the `update_node` allowlist so an insight's body can be edited in place.

**Sync interaction — unsafe:** an in-place `content` UPDATE changes an existing `id`. Under `_union_pull`'s `INSERT OR IGNORE` (§2.2), an established peer keeps its **old** content indefinitely — the edit silently never propagates. Making it propagate would require changing the union to compare a per-row version/`updated_at` and do `UPDATE`-on-conflict — i.e. **rewriting the merge algorithm**, including the exact FK-check/rollback path implicated in the 43-insight silent loss (`684834b2`). That is the highest-blast-radius, most-dangerous code in the repo.

**Schema migration:** would need a per-row version/`updated_at` column *and* merge-logic changes to be even theoretically convergent.

**Effort:** S to add the param; **effectively L-and-dangerous** to make it sync-correct. **Risk: HIGH / SILENT** — the default outcome is per-machine content divergence discovered days later by a human, MemCP's signature failure. Reject.

### Option C — Route living docs through overwritable `memcp_load_context`  ← REJECTED for cross-machine docs
**What:** store the canonical doc as a named context and overwrite it each time (optionally prepend a timestamped changelog line before overwrite).

**Sync interaction — unsafe for the motivating cases:** contexts are **local-only** (§2.3); `snapshot_sync.py` never touches `contexts_dir`. A cross-machine reference list stored this way would silently be absent on the other machine — a new silent-divergence mode. It is acceptable *only* for genuinely single-machine ephemeral content (its current idea-scan use), which the reference list and architecture-state doc are not.

**Schema migration:** none. **Effort:** S. **Risk:** HIGH/SILENT for anything cross-machine (missing doc on peer); LOW only if the doc provably never leaves one machine. Reject for the stated use cases.

## 4. Recommendation

**Adopt Option A (topic log + derived compiled view), shipped in two steps:**
1. **Now (effort S, zero code):** adopt the `topic:<slug>` / `entry:compiled` / `entry:log` / `supersedes:<id8>` tag convention with `memcp_remember`, and read it via existing `memcp_grep`. Document it in `CLAUDE.md`/`TOOLS.md`. This immediately removes the "read a stale vN" failure because the topic tag is the single entrypoint, and it converges cross-machine by construction (new-id INSERTs only).
2. **Next (effort M):** build **`memcp_topic(slug)`** as a read-only tool alongside `memcp_grep` (same additive, no-new-storage posture) that renders "latest compiled on top, timeline below," and optionally warns when a `entry:compiled` row lacks a `supersedes:` link to the current head.

This is maximally reuse-first (it's `memcp_remember` + tags + an optional grep-shaped reader), needs **no schema migration**, and — decisively — is the **only** option that is safe against MemCP's silent-sync history, because it expresses every change as one of the two operations the union merge actually converges (new-id INSERT). It's also a bounded, user-facing first step toward the append-only event log that ADR-014 already endorsed as the north star.

Honest caveat: Option A does not *mechanically* prevent a careless author from writing a compiled entry off a stale head; it makes the correct action the obvious one (one entrypoint) and makes violations greppable/detectable. That residual is behavioral and cheap to catch — strictly better than today's undetectable-until-a-human-notices state, and far safer than B or C.

## 5. Non-goals (blast radius containment)

- **Do NOT modify `_union_pull` or any part of the snapshot merge algorithm** (`snapshot_sync.py:602-695`).
- **Do NOT touch the FK-check baseline / rollback logic** (`snapshot_sync.py:645, 676-685`) — the `684834b2` silent-loss path.
- **Do NOT add `content` to `update_node`'s allowlist** (`node_store.py:636-647`) — creates a silently non-propagating edit (Option B).
- **Do NOT add a `supersedes` edge_type** — avoids the `edges` CHECK-constraint migration (`node_store.py:219`); use a tag convention instead.
- **Do NOT route cross-machine living docs through `memcp_load_context`** — contexts are local-only (§2.3).
- **Do NOT change tombstones, embeddings, `reindex.py`, or `revision.py`.**
- **No new synced files / flat-file mirror.** The compiled view is derived on read.
- No auto-migration of existing vN chains; adoption is forward-going (a one-time manual "seed the topic" is optional and out of scope here).

## Critical files for implementation
- `src/memcp/server.py` (tool surface: `memcp_update:447`, `memcp_remember:164`, `memcp_load_context:615`; add `memcp_topic` here)
- `src/memcp/core/node_store.py` (nodes schema `:199`, `update_node` allowlist `:636`, tombstones — read-only reference for the convention)
- `src/memcp/core/snapshot_sync.py` (`_union_pull:602` — the INSERT-OR-IGNORE/tombstone merge the design must stay compatible with; do not modify)
- `src/memcp/core/memory.py` (where `memcp_grep`/recall read paths live; `memcp_topic` read logic belongs alongside)
- `docs/SPEC-memcp_grep.md` (format precedent for this spec and the read-only-tool pattern to mirror)
