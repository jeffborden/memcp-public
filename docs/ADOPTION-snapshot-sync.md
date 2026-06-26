# Adoption: snapshot-sync (live DB local + Drive snapshot) — 2026-06-01

The real cure for the recurring `graph.db` corruption/lock: the live SQLite
DB no longer lives on Google Drive. macOS `fileproviderd` was holding it open
for write and shearing/locking it. Now the live DB is **local**, and only a
static **snapshot** is exchanged through a Drive-synced directory.

Merged to `main`: `dc147af` (sync) on top of `e4f28b7` (write-lock + DELETE).

## How it works
- Live DB: `MEMCP_DATA_DIR` → a **local** dir (e.g. `~/.memcp-local`).
- Snapshot exchange: `MEMCP_SNAPSHOT_DIR` → a **Drive** dir (e.g.
  `…/My Drive/memcp/sync`), holding `graph.snapshot.db` + `…meta.json`.
- Startup: MemCP pulls a newer snapshot and **merges it into** the local DB via
  an **additive union** — `INSERT OR IGNORE` of snapshot rows plus a tombstone
  deny-set. Pull never deletes a local row to match the snapshot, so no
  remembered insight is ever lost (the no-loss guarantee). A fresh machine with
  no local DB adopts the snapshot directly.
- After **durable** writes only (a remember / forget / metadata edit — *not*
  reads or reindex): a background flusher pushes a fresh, consistent snapshot
  (SQLite backup API → temp → atomic move) and bumps the generation. Reads and
  derived-index churn never republish (quiescence), so a stale machine can't
  clobber a peer just by recalling.
- Deletes travel as **tombstones** (kept indefinitely); archive is an in-band
  `archived_at` soft-state; capacity-eviction (auto-prune) and hard
  retention-purge are **disabled while synced** (incompatible with no-loss).
- Drive never holds the live DB open → no shear, no lock.

> **Still: one machine at a time, restart before switching.** The union makes
> the design no-loss under accidental concurrency, but fully-safe *concurrent*
> two-machine use also needs the immutable-blob publish protocol (a tracked
> follow-up). Until then, don't run live MemCP sessions on both machines at once.

## This machine (Jeffs-Mac-mini) — ALREADY DONE
- Live DB migrated to `~/.memcp-local/` (1,159 insights, integrity ok).
- `.claude.json` memcp env set: `MEMCP_DATA_DIR=~/.memcp-local`,
  `MEMCP_SNAPSHOT_DIR=…/My Drive/memcp/sync`. (Backup: `~/.claude.json.bak-20260601`.)
- Seeded snapshot at generation 3.
- Old Drive DB retired to `graph.db.RETIRED-20260601`; `-wal`/`-shm` removed.
- **Action: restart Claude Code once** to load the new code + config.

## The OTHER machine (the laptop) — DO THIS
1. `cd ~/projects/memcp && git pull origin main`
2. Edit that machine's `~/.claude.json`, memcp server `env`:
   ```json
   "MEMCP_DATA_DIR": "/Users/alice/.memcp-local",
   "MEMCP_SNAPSHOT_DIR": "/Users/alice/Library/CloudStorage/GoogleDrive-alice@example.com/My Drive/memcp/sync"
   ```
3. Restart Claude Code. On first start MemCP pulls the snapshot from Drive and
   **merges** it into the local `~/.memcp-local/graph.db` (additive union; a
   brand-new machine with no local DB adopts the snapshot directly) — no manual
   data copy needed for insights.

## Re-enable Google Drive sync
Safe now — Drive only syncs the static snapshot in `memcp/sync/`, never a live
DB. Resume Drive syncing whenever (it was paused during recovery).

## Known v1 limitation
Snapshot-sync covers **`graph.db` (insights)** only. The `contexts/` markdown
(loaded documents) stays per-machine local for now. This machine's contexts
were copied into `~/.memcp-local/contexts/`; the laptop will start without them
(re-load as needed). Extending the snapshot to the whole data dir is a future
step.

## Safety net
- Verified pre-migration copy: `/tmp/graph-CURRENT-verified.db` (1,159 nodes).
- Recovery runbook for any SQLite malformation: `sqlite3 bad.db ".recover" | sqlite3 good.db`.
