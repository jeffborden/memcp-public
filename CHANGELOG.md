# Changelog

All notable changes to this project will be documented in this file.

## [0.4.0] - 2026-08-04

Export-hardening release: the fixes an adopting teammate would otherwise hit
in week one. After merging, tag with `git tag v0.4.0 && git push --tags` so
installs can pin to it.

### ⚠ Breaking Changes (export-hardening, 2026-08-04)

- **Write tools no longer accept `project`/`session` parameters**
  (`memcp_remember`, `memcp_log_episode`, `memcp_load_context`). Attribution
  comes from the server's process identity, resolved once at startup. When
  the params existed, LLM callers filled them in ~78% of calls and forked
  half a store out of the default read path.
- **Read scope defaults to `"all"`** (`memcp_recall`, `memcp_search`,
  `memcp_recall_episodes`). The data dir is the isolation boundary; project
  labels are filters. Rationale: `docs/design/default-scope.md`.
- **`memcp_consolidation_preview` output changed**: raw similarity groups
  are replaced by `mergeable` subsets (supersession-justified, with
  `suggested_keep_id`) and `unjustified` members. **`memcp_consolidate`
  refuses groups larger than 8**, and the bulk `consolidate()` path merges
  only gate-justified subsets.

### Fixed (export-hardening, 2026-08-04)

- **Cross-process attribution**: `state.json`'s `current_project`/
  `current_session` are machine-global and overwritten by every server
  startup, so any concurrently running server stamped its writes with the
  LAST server's identity. Servers now pin identity in process memory at
  `_init_session` and never re-read the global file on write paths.
  Repro: `tests/integration/test_process_identity.py` (two real server
  processes, interleaved writes).
- **`memcp_status` scoping made explicit**: responses carry `scope` (what
  the counts cover) and `store` (whole-store totals + per-project
  breakdown), so a store forked across project labels is visible
  immediately instead of masquerading as the full count.
- **Silent `max_tokens` truncation**: `memcp_recall` now reports a
  `truncated` field naming the budget when the token budget cut results.
- **Consolidation blob hazard**: single-linkage similarity grouping is
  transitive and grouped 150 of 196 insights into one mergeable "group" at
  the default threshold; merging now requires explicit supersession claims
  between members (id cited near a supersession verb, or a lane-scoped
  blanket claim). Fixtures include the measured false-merge traps.
- **`remember` field-drop telemetry (#82)**: the reported tag/summary/
  importance drops do not reproduce server-side through a real MCP client
  session; every save now emits a metadata-only `remember_fields` event
  (received-vs-stored shapes) so the next occurrence carries evidence.

### Features

- Per-segment sync telemetry for stall attribution: `push` events carry `catchup/lock_wait/backup/hash/publish/gc` splits, and previously invisible paths emit their own events (`push_quiescent`, `catchup` with a stage/validate/merge split, `sweep`, `gc_floor`, `pull_deferred`, `close_flush_skipped`). Metadata-only (durations/counts), fail-open.
- Add a recall-latency gauge backing the codified ANN-index trigger: every `recall()` emits a metadata-only latency event (retrieval-path label + duration + result count), and `memcp_status` surfaces `telemetry.recall_latency` — overall and per-path (semantic/keyword/filter) p50/p95, the ~150 ms trigger threshold, and an `over_trigger` flag. The per-path split is what makes the trigger actionable (an ANN index cuts the semantic vector sweep, not the keyword ranker floor). Extends the existing telemetry — no new dependency, `mode` is a bounded path label (metadata-only contract preserved), fail-open throughout.
- Add `memcp_topic` — living-doc content versioning via a `topic:` / `entry:` / `supersedes:` tag convention over append-only saves, read back as "compiled truth on top + chronological timeline below." Every update is a new-id insert (converges cross-machine); no in-place content edits, no new storage, sync surface, or schema change.
- `memcp_update` and `memcp_archive` now resolve unambiguous id prefixes the same way `memcp_get` does; ambiguous prefixes return the candidate ids instead of guessing. `memcp_forget` deliberately still requires the full id (destructive).

### Bug Fixes

- Snapshot GC now reclaims 0-byte torn-publish debris blobs — a stat-only sweep at the top of `_gc_blobs` unlinks any non-pointer `graph.snapshot.*.db` that is 0 bytes AND older than 1 hour (`_DEBRIS_MIN_AGE_S`). A 0-byte blob is provably content-free, so this is the one safe exception to the refuse-unreadable rule, which had retained 22+ debris blobs forever (a floor-refusal warning per blob per pass, 13k+ refusal events since 06-03, and a blob count permanently over the cap so the cap pass ran on every publish). The age guard protects the mid-sync race (a peer's in-flight blob can transiently surface as 0-byte on the Drive mount); one `gc_debris` summary event (info-level) is emitted only when something was reclaimed. Oracle: `tests/unit/test_gc_zero_byte_debris.py`; evidence: `docs/eval/sync-lock-stall-measurement-2026-07-15.md`.
- Multi-minute MCP write stalls eliminated (measured: one page-random SQLite read of a dataless 91MB File Provider blob = ~236s at 0.4 MB/s, executed inline in tool calls). Three-part fix: remote snapshot blobs are staged to a local temp with one streaming copy before any SQLite open (pull, catch-up, orphan sweep); `close()` defers its final flush when an unmerged peer generation would force that read inline (local changes stay durable and publish after a later fold — never a non-superset publish); and push's write lock now covers only the consistent backup — hash, publish, ledger write, and GC blob reads run outside it, so writers no longer wait on Drive I/O. Oracle: `tests/unit/test_sync_lock_stall_regression.py`; evidence: `docs/eval/sync-lock-stall-measurement-2026-07-15.md`.
- `WriteLockError` is now a `MemCPError` subclass, so local-lock acquisition failures surface as clean client errors instead of opaque `RuntimeError` tracebacks.
- `rebuild_embeddings` now filters archived nodes (parity with the edges/entities builders) and sweeps an archived node's vector as an orphan on incremental rebuilds, so archived insights no longer serve semantic recall; its final meta-write runs inside one transaction.
- The per-tick snapshot convergence audit skips re-auditing when neither side has changed, and divergence inside the normal write-to-push window (or with a zero row delta) logs at debug instead of warning — a genuine nonzero-delta divergence still warns.
- Embedding-model load failures are capped at 3 consecutive attempts (a corrupted model file no longer re-attempts the load on every search); `reset_provider()` re-opens the retry budget.

### Performance

- 10k-node scale + retention stress evaluated (2026-07-01): write and recall both scale linearly with corpus size (~94 ms/insert and 100–173 ms recall p50 at 10k nodes); retention archived 3,225 insights cleanly and the incremental reindex swept exactly their vectors. Follow-up triggers (batch edge deferral, ANN indexing) documented in the eval report.

## [0.3.0] - 2026-02-19

### Documentation

- Add 3 ADRs and update all documentation for Step 2 ([55a31e4](https://github.com/maydali28/memcp/commit/55a31e483a39f947ad831ba304d4558a7c46e870))

### Features

- Add Hebbian co-retrieval strengthening and activation-based edge decay ([0772625](https://github.com/maydali28/memcp/commit/0772625e57143c59ba1cdc8972799f3f88d4f586))
- Add Reciprocal Rank Fusion (RRF) for hybrid search ([e912235](https://github.com/maydali28/memcp/commit/e9122350c0501399bd53fe2e5f3c5d3c74169c44))
- Add memory feedback API (memcp_reinforce) ([8f63db7](https://github.com/maydali28/memcp/commit/8f63db7e1f125c1c47ff1f376fd1ca3c6ad600a4))
- Add optional spaCy NER entity extraction ([d29067a](https://github.com/maydali28/memcp/commit/d29067a27834f6c4e00f0e3b9b3191a2c56117aa))
- Add memory consolidation (detect and merge similar insights) ([fc915f3](https://github.com/maydali28/memcp/commit/fc915f3bfacca3ec9f052b18bb9565d3cf669e73))
- Integrate cognitive memory features into config, server, and query ([4e4f5bb](https://github.com/maydali28/memcp/commit/4e4f5bb8f7a4216ed7590329122abbaca1e733f9))

### Miscellaneous

- Bump version to 0.3.0 ([a41c714](https://github.com/maydali28/memcp/commit/a41c7140036aa4d752a92b14039e41d3ebfa9bbd))

## [0.2.0] - 2026-02-19

### Bug Fixes

- Update install script extras and fix ask_choice display bug ([a632a7c](https://github.com/maydali28/memcp/commit/a632a7c212f6221ff7efa9b4238b22a2b371c1bb))

### Documentation

- Update CHANGELOG.md for v0.1.0 ([6723006](https://github.com/maydali28/memcp/commit/6723006af775d492b26cbd45b4addeb743161858))
- Update documentation for foundation hardening changes ([9e57569](https://github.com/maydali28/memcp/commit/9e5756996619053a39986dc04ac1615c695fc66f))

### Features

- Add error hierarchy, config validation, and secret detection ([724de76](https://github.com/maydali28/memcp/commit/724de763903fbf7374aa2d0b2a03043774215950))
- Add persistent BM25 cache and HNSW vector index ([96ae300](https://github.com/maydali28/memcp/commit/96ae300043808f0bc1a1bdd7d821142bc3715a9d))
- Add async I/O wrappers and semantic deduplication ([9ff74cb](https://github.com/maydali28/memcp/commit/9ff74cb90153818e0970fc9f2520371fd99c95d3))

### Miscellaneous

- Bump version to 0.2.0 ([f8deb62](https://github.com/maydali28/memcp/commit/f8deb6207515ca32cf4a5c40f2f9fee561079e04))
- Centralize version in __init__.py and add integration tests to CI ([ecee9ab](https://github.com/maydali28/memcp/commit/ecee9ab6b4a7d6186e1edd983d7bbc37bf4aefe6))

### Refactoring

- Split GraphMemory god object into focused components ([6b59ea8](https://github.com/maydali28/memcp/commit/6b59ea8f13693d7d9dbde08d1a3a113fae5094f7))

### Testing

- Add integration and concurrency stress tests ([0adae4f](https://github.com/maydali28/memcp/commit/0adae4f6f8a68cc335211b85e4496262604c687e))

## [0.1.0] - 2026-02-11

### Bug Fixes

- Fix lint for different python scripts ([05af086](https://github.com/maydali28/memcp/commit/05af086e314ca6a8a803323a203c52e089686efb))
- Fix multi project issues (#4) ([f7652e4](https://github.com/maydali28/memcp/commit/f7652e43b99c26cc5602629de7678ae456f72d24))
- Fix issues related to context and multi-sessions (#5) ([a6faaf9](https://github.com/maydali28/memcp/commit/a6faaf95e147640799aa8095aba981682eea7b26))
- Rename pypi package name ([2ac7674](https://github.com/maydali28/memcp/commit/2ac76743ebe3783360be6835365382ea63c03df1))

### Features

- Add benchmark tests and generated results and report (#1) ([7c28078](https://github.com/maydali28/memcp/commit/7c2807874a2b76d352eef8a8729a69e1071bf287))

### Miscellaneous

- Adjust structure and install scripts (#2) ([66cc1a9](https://github.com/maydali28/memcp/commit/66cc1a96f6ec68ce3b1296968837198320f48aa1))


