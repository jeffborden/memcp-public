# Adoption: write-lock + DELETE journal (2026-06-01)

Cross-machine handoff for turning on the `graph.db` corruption fix.

## Why
`graph.db` lives on Google Drive. SQLite over a sync daemon shears b-tree
pages when two writers commit at overlapping moments — this corrupted the
store on 2026-05-06 and again 2026-06-01 (recovered via `sqlite3 .recover`,
~zero loss). The fix is on `main` (merge `e4f28b7`).

## ⚠️ Both machines must be on the new code before concurrent writing is safe
If one machine runs the old code (WAL, no lock) while the other runs the new
code (DELETE, lock), they disagree on journal mode and the old machine won't
honor the lock — corruption is still possible. **Update and restart BOTH
machines before writing MemCP from two machines at once.**

## Steps (run on EACH machine — desktop already merged locally)

```bash
cd ~/projects/memcp
git pull origin main          # gets the write-lock commit (e4f28b7)
# no reinstall needed: editable install, lock is stdlib-only (fcntl/json)
```

Then **fully restart Claude Code** on that machine so the MemCP MCP server
(`.venv/bin/python -m memcp`) reloads and reopens `graph.db` under the new
code.

## Verify after restart (optional)
- `memcp_ping` returns ok.
- A save creates/removes `<MEMCP_DATA_DIR>/.writer.lock` around the commit.
- No `graph.db-wal` / `graph.db-shm` sidecars reappear next to `graph.db`.
- Flock files live locally at `~/.cache/memcp/locks/` (per machine).

## Housekeeping
- After BOTH machines are on DELETE mode and restarted, any leftover
  `graph.db-wal` / `graph.db-shm` on Drive from old WAL sessions are orphans;
  safe to delete **only when no MemCP server is running**. (SQLite also
  checkpoints/removes a `-wal` on first DELETE-mode open.)
- The corrupt originals are preserved: `graph.db.malformed-20260601` (+ a
  recovered safety copy in `backups/recovered-20260601/`).

## Config knobs (env, all optional)
- `MEMCP_SQLITE_JOURNAL_MODE` (default `DELETE`; set `WAL` only for local-only storage)
- `MEMCP_WRITE_LOCK` (default `true`)
- `MEMCP_WRITE_LOCK_TTL` (lease stale-reclaim seconds, default `180`)
- `MEMCP_WRITE_LOCK_TIMEOUT` (max block on a foreign lease, default `30`)
- `MEMCP_WRITE_LOCK_SETTLE_MS` (cross-machine race re-check, default `0` = off, no per-save latency)
- `MEMCP_LOCK_DIR` (local flock dir, default `~/.cache/memcp/locks` — must be off the synced mount)
