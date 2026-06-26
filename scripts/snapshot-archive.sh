#!/bin/bash
#
# snapshot-archive.sh — periodic, permanent, FULL-STATE backup of the MemCP store.
#
# WHY THIS EXISTS: MemCP's own cross-machine snapshots (the graph.snapshot.<gen>…
# blobs in the sync/ dir) are a *sliding window* — old generations are
# garbage-collected once every machine has merged past them. That gives crash
# safety but NOT "restore from three months ago." This script keeps a separate,
# never-GC'd, timestamped archive so deep history is always recoverable.
#
# WHAT IT CAPTURES (a complete, drop-in-restorable copy, bundled as one tarball):
#   - graph.db          the memory graph — copied via SQLite's online backup API
#                       (consistent even while the MCP server is writing) + verified
#   - sessions.json     session history
#   - state.json        server state
#   - index.md          progressive-disclosure index
#   - contexts/         stored context variables (real user data)
#   - cache/            BM25 + embeddings cache (so restore needs no re-embedding)
#   - archive/          archived insights
#   - RESTORE.txt       how to restore (written into the tarball)
# Excluded: graph.db.prepull (transient), memory.json.migrated (already in graph.db),
#           *.lock files.
#
# LOCATION is LOCAL on purpose (see ARCHIVE_DIR below).
#
# Scheduled via ~/Library/LaunchAgents/com.memcp.snapshot-archive.plist
# (1st & 15th of each month). Safe to run by hand any time:  bash snapshot-archive.sh
#
# RESTORE:  stop the MCP server, then:
#   mkdir -p ~/.memcp-local && tar -xzf <archive>.tar.gz -C ~/.memcp-local
#
set -euo pipefail

# --- config (override via the environment / the launchd plist) ---------------
DATA_DIR="${MEMCP_DATA_DIR:-$HOME/.memcp-local}"
DB="$DATA_DIR/graph.db"
# Local on purpose: a launchd-spawned job has full access to local paths (it can
# create AND enumerate AND prune), but macOS TCC lets it create-but-not-list files
# on the Google Drive CloudStorage mount — which would silently break auto-pruning.
# Current-state off-machine redundancy is already covered by MemCP's own Drive sync;
# this archive's job is time-depth (restore an older version), which local serves fine.
ARCHIVE_DIR="${MEMCP_ARCHIVE_DIR:-$HOME/Library/Application Support/memcp/snapshots}"
KEEP="${MEMCP_ARCHIVE_KEEP:-52}"          # how many archives to retain (52 ≈ 2yr biweekly)
HOST="$(hostname -s)"
TS="$(date +%Y%m%d-%H%M%S)"
# Log LOCALLY, never on the synced mount. Logging never fails the run.
LOG="${MEMCP_ARCHIVE_LOG:-$HOME/Library/Logs/memcp-snapshot-archive.log}"
mkdir -p "$(dirname "$LOG")"

log() { echo "[$(date '+%Y-%m-%d %H:%M:%S')] $*" >> "$LOG" 2>/dev/null || true; }

mkdir -p "$ARCHIVE_DIR"

# Safety guard: the LIVE db must be local. If MEMCP_DATA_DIR was inherited from a
# stale interactive shell pointing at the synced Drive folder, $DB resolves to the
# 159 KB *legacy* file, not the real store — refuse rather than archive garbage.
if [[ "$DB" == *"/CloudStorage/"* || "$DB" == *"/My Drive/"* ]]; then
  log "ERROR: DB resolves to a synced path ($DB). The live DB must be local (~/.memcp-local). Check MEMCP_DATA_DIR — refusing to back up the stale legacy file."
  exit 3
fi

if [[ ! -f "$DB" ]]; then
  log "ERROR: live DB not found at $DB — nothing to archive."
  exit 1
fi

STAGE="$(mktemp -d -t memcp-archive)"
trap 'rm -rf "$STAGE"' EXIT

# 1. Consistent online backup of graph.db (safe even mid-write — not a raw cp).
sqlite3 "$DB" ".backup '$STAGE/graph.db'"

# 2. Integrity gate — never archive a corrupt copy.
CHECK="$(sqlite3 "$STAGE/graph.db" 'PRAGMA quick_check;' 2>&1 || true)"
if [[ "$CHECK" != "ok" ]]; then
  log "ERROR: integrity check failed ($CHECK) — refusing to archive a bad copy."
  exit 2
fi

# 3. Stage the sidecars (real user data + derived caches). Skip what's transient.
for f in sessions.json state.json index.md; do
  [[ -f "$DATA_DIR/$f" ]] && cp -p "$DATA_DIR/$f" "$STAGE/"
done
for d in contexts cache archive chunks; do
  [[ -d "$DATA_DIR/$d" ]] && cp -Rp "$DATA_DIR/$d" "$STAGE/"
done

# 4. Restore note, bundled in.
cat > "$STAGE/RESTORE.txt" <<EOF
MemCP full-state backup — $TS ($HOST)
Restore (with the MCP server stopped):
  mkdir -p ~/.memcp-local
  tar -xzf $(basename "$ARCHIVE_DIR")/memcp.$TS.$HOST.tar.gz -C ~/.memcp-local
graph.db here is a consistent SQLite .backup; sidecars are as-of backup time.
EOF

# 5. Bundle the whole staging dir into ONE tarball (tidy: one file per run).
OUT="memcp.$TS.$HOST.tar.gz"
tar -czf "$ARCHIVE_DIR/$OUT" -C "$STAGE" .
SIZE="$(ls -lh "$ARCHIVE_DIR/$OUT" | awk '{print $5}')"
log "archived $OUT ($SIZE)"

# 6. Prune to the most-recent $KEEP. BEST-EFFORT ONLY: the backup above is already
#    safely written, so housekeeping must never fail the run. Operate on full paths.
prune() {
  local listing n=0 f
  listing="$(ls -1t "$ARCHIVE_DIR"/memcp.*.tar.gz 2>/dev/null)" || return 0
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    n=$((n + 1))
    if [ "$n" -gt "$KEEP" ]; then
      rm -f -- "$f" 2>/dev/null && log "pruned $(basename "$f")"
    fi
  done <<< "$listing"
}
prune || true

RETAINED="$(ls -1 "$ARCHIVE_DIR"/memcp.*.tar.gz 2>/dev/null | wc -l | tr -d ' ' || true)"
log "done — ${RETAINED:-?} archives retained (keep=$KEEP)"
exit 0
