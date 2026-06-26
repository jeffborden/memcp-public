"""Cross-machine snapshot sync for the local SQLite store.

The live ``graph.db`` lives on **local disk** (so the Google Drive file
provider can never hold it open and shear it). Cross-machine durability is
restored by syncing a *static snapshot* through a Drive-synced directory:

* **Pull** on startup (before the DB is opened): if the Drive snapshot is a
  newer generation than what this machine last saw, copy it down to the local
  DB. Safe because nothing has the local DB open yet.
* **Push** after writes (debounced) and at exit: take a *consistent* copy of
  the local DB via the SQLite backup API, write it to a local temp, then move
  it onto the Drive directory and bump the generation.

Drive only ever sees a closed, complete file — never a database an engine is
mid-writing — so the shear/lock failure mode is gone. Cross-machine semantics
are last-writer-wins at the snapshot level (matches one-machine-at-a-time use);
a generation counter prevents an older push from clobbering a newer snapshot
silently (it logs).

Enabled only when ``MEMCP_SNAPSHOT_DIR`` is set; otherwise this is a no-op and
MemCP is purely local.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import os
import secrets
import shutil
import sqlite3
import threading
import time
from pathlib import Path

from memcp.core import telemetry
from memcp.core.write_lock import WriteLock

logger = logging.getLogger(__name__)

_SNAPSHOT_DB = "graph.snapshot.db"
_SNAPSHOT_META = "graph.snapshot.meta.json"
_SNAPSHOT_PTR = "graph.snapshot.ptr.json"
# Per-host last-merged generation ledger (Drive-synced). Drives GC: a blob is
# only collectable once EVERY known host has merged a generation >= it (§3.2.7).
_SNAPSHOT_MERGED = "graph.snapshot.merged.json"
_LOCAL_META = ".sync_meta.json"
# Local-only ledger of orphan blob NAMES this machine has already folded via the
# startup sweep (§3.3a). Deliberately named distinctly from the Drive-synced
# per-host last-merged-GENERATION ledger above (graph.snapshot.merged.json): this
# is blob-name granularity, never on Drive, and only gates the sweep's "don't
# re-union an already-folded blob" skip so a stable set of retained orphans isn't
# re-scanned on every startup.
_SWEPT_BLOBS = ".sync_swept.json"
# Persisted, local-only stable host identity (never on Drive). os.uname().nodename
# is unstable on macOS (a machine transiently reports a bare "Mac" when unresolved),
# and every distinct reading becomes a permanent merged-ledger key that pins the GC
# floor forever — so we freeze the id once a trusted (qualified) name is seen.
_HOST_ID = ".host_id"
# Stable per-machine token for suffixing an *untrusted* (transient) nodename, so
# repeated boots under a bad name reuse ONE disposable ledger key instead of
# minting a fresh ghost each boot. Persisted locally, separate from _HOST_ID
# (which only ever holds a frozen *qualified* name).
_HOST_SEED = ".host_seed"
# A nodename is only trusted to be persisted if it is fully-qualified (contains a
# ".") and is not a known-generic placeholder. Anything else is treated as a
# transient reading: used session-only (suffixed for uniqueness) and re-resolved
# on the next startup until a qualified name appears.
_GENERIC_NODENAMES = {"", "localhost", "localhost.localdomain"}
# Single source of truth for the cap default (config.py reads this constant).
_DEFAULT_MAX_BLOBS = 20


def snapshot_health(snapshot_dir: str) -> dict:
    """Read-only snapshot disk/ledger health for ``memcp_status``. Returns an
    empty dict when no snapshot dir is configured or it does not exist yet.
    Surfaces the GC ``floor`` and the ``floor_pinned_by`` host so a ghost ledger
    entry (a dead identity pinning the floor) is visible to an operator."""
    if not snapshot_dir:
        return {}
    d = Path(snapshot_dir).expanduser()
    if not d.exists():
        return {}

    def _size(p: Path) -> int:
        try:
            return p.stat().st_size
        except OSError:  # unlinked by a concurrent GC between glob and stat
            return 0

    # v2 immutable blobs (managed by the cap) plus the bare v1 file, so the
    # disk figure is honest on a v1-default deployment (where the glob misses it).
    blobs = [b for b in d.glob("graph.snapshot.*.db") if b.exists()]
    v1_path = d / _SNAPSHOT_DB
    v1_present = v1_path.exists()
    v1_bytes = _size(v1_path) if v1_present else 0

    ledger_path = d / _SNAPSHOT_MERGED
    try:
        ledger = json.loads(ledger_path.read_text()) if ledger_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        ledger = {}
    floor: int | None = None
    pinned_by: str | None = None
    if ledger:
        try:
            pinned_by = min(ledger, key=lambda k: int(ledger[k]))
            floor = int(ledger[pinned_by])
        except (TypeError, ValueError):
            floor, pinned_by = None, None

    # Pointer staleness (P0): surface when the named lineage was last published
    # and by whom, so an operator can see a pointer that has stopped advancing
    # (a stalled publisher) before it becomes a silent divergence.
    ptr_path = d / _SNAPSHOT_PTR
    try:
        ptr = json.loads(ptr_path.read_text()) if ptr_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        ptr = {}
    pointer_written_at = ptr.get("written_at")
    pointer_age_seconds: float | None = None
    if isinstance(pointer_written_at, (int, float)):
        pointer_age_seconds = round(time.time() - pointer_written_at, 3)

    return {
        "blob_count": len(blobs),
        "disk_bytes": sum(_size(b) for b in blobs) + v1_bytes,
        "v1_present": v1_present,
        "v1_bytes": v1_bytes,
        "merged_ledger": ledger,
        "floor": floor,
        "floor_pinned_by": pinned_by,
        "pointer_written_at": pointer_written_at,
        "pointer_age_seconds": pointer_age_seconds,
        "pointer_host": ptr.get("host"),
    }


# Snapshot wire-format version. The reader fails closed on any snapshot whose
# format_version exceeds this, and the writer refuses to overwrite a higher
# one — so a future format change (e.g. immutable blobs + pointer, §3.2) can be
# rolled out reader-first: ship this gate to every machine, confirm, then bump
# the version. A snapshot with no format_version is a pre-gate writer == v1.
_FORMAT_VERSION = 1

# Immutable-blob format (Step 6, §3.2): globally-unique gen-suffixed blobs named
# by a tiny pointer. Written ONLY when immutable mode is on (default OFF — the
# rollout flip is a deliberate two-machine step). A v2-aware binary can READ both
# v1 and v2, so the read ceiling rises to _FORMAT_V2 once the pull path
# understands the pointer; v1 stays the default *written* format until the flip.
_FORMAT_V2 = 2

# Derived / machine-local node columns — excluded from the durable projection
# hash so access bumps / re-ranking never mint a new snapshot generation (§3.3).
# ingest_seq is excluded too: it's a per-MACHINE counter (the union strips it and
# each machine re-assigns its own), so including it would make two machines that
# hold byte-identical durable content compute different hashes.
_DERIVED_NODE_COLS = frozenset(
    {
        "access_count",
        "last_accessed_at",
        "effective_importance",
        "feedback_score",
        "ingest_seq",
    }
)


class SnapshotSync:
    """Pull-on-start / debounced-push snapshot sync between a local DB and Drive."""

    # Process-global counter for unique temp filenames in _write_json.
    _tmp_counter = itertools.count()

    def __init__(
        self,
        local_db_path: str | Path,
        snapshot_dir: str | Path,
        lock: WriteLock,
        *,
        min_interval: float = 30.0,
        immutable: bool | None = None,
        max_blobs: int | None = None,
    ) -> None:
        self.local_db = Path(local_db_path)
        self.snapshot_dir = Path(snapshot_dir).expanduser()
        self.snapshot_db = self.snapshot_dir / _SNAPSHOT_DB
        self.snapshot_meta = self.snapshot_dir / _SNAPSHOT_META
        self.snapshot_ptr = self.snapshot_dir / _SNAPSHOT_PTR
        self.snapshot_merged = self.snapshot_dir / _SNAPSHOT_MERGED
        self.local_meta = self.local_db.parent / _LOCAL_META
        self.swept_meta = self.local_db.parent / _SWEPT_BLOBS
        self.lock = lock
        self.min_interval = min_interval

        # Immutable-blob publish (§3.2) is default-OFF; the flip is a deliberate
        # two-machine step. Honor an explicit kwarg, else the env flag.
        if immutable is None:
            immutable = os.environ.get("MEMCP_SNAPSHOT_IMMUTABLE", "").lower() in {
                "1",
                "true",
                "yes",
                "on",
            }
        self._immutable = immutable

        # Hard backstop on retained blob count, independent of the per-host GC
        # floor, so a stale/ghost host can never pin disk growth. <=0 disables.
        # Production routes the value through config (node_store passes
        # config.snapshot_max_blobs); the constant fallback only fires for direct
        # construction (tests/tools) and never reads env, so it is hermetic.
        self._max_blobs = _DEFAULT_MAX_BLOBS if max_blobs is None else max_blobs

        self._host = self._resolve_host_id()
        self._durable_dirty = False
        self._last_push = 0.0
        # Consecutive sync-failure counter (P0 detection). Incremented per failed
        # flusher tick (pull or push raised / a real Drive error was swallowed),
        # reset on a successful push; escalates to error-level logging at >=3 so a
        # wedged flusher becomes visible instead of silently dropping writes.
        self._sync_error_count = 0
        self._last_gc_floor = -1  # skip the blob-dir scan when the GC floor is static
        self._last_merge_stats = {"inserted": 0, "deleted": 0}  # for telemetry only
        self._guard = threading.Lock()
        # Serializes a manual sync_now() against the background _flush_loop tick so
        # the two never overlap within this instance (lock order: _tick_lock → _guard).
        self._tick_lock = threading.Lock()
        self._flusher: threading.Thread | None = None
        self._stop = threading.Event()
        self.pull_pending: bool = False
        # Per-pull convergence audit (P6): the audit runs on every flusher pull
        # cycle, not only on demand, so a silent no-merge becomes a continuously
        # detected condition. Count + last result are kept for observability.
        self._audit_count = 0
        self._last_audit: dict = {}

    # ── stable host identity ──────────────────────────────────────

    def _normalize_host(self, nodename: str) -> str:
        """Turn a live nodename into a usable id. A trusted (qualified) name is
        returned verbatim; an untrusted/transient one is suffixed from a stable
        per-machine seed, so it is unique-but-disposable, never collides on a
        bare ghost name, and reuses one key across boots."""
        norm = (nodename or "").strip()
        if self._is_trusted_nodename(norm):
            return norm
        return f"{norm or 'host'}-{self._host_seed()}"

    def _host_seed(self) -> str:
        """Stable 2-byte hex token persisted locally, created once. Best-effort:
        any I/O error degrades to a fresh random token (one ghost per boot), never
        raising — the content-verified cap bounds disk regardless."""
        path = self.local_db.parent / _HOST_SEED
        try:
            if path.exists():
                existing = path.read_text(encoding="utf-8").strip()
                if existing:
                    return existing
            token = secrets.token_hex(2)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{_HOST_SEED}.{os.getpid()}.tmp")
            tmp.write_text(token, encoding="utf-8")
            os.replace(tmp, path)
            winner = path.read_text(encoding="utf-8").strip()
            return winner or token
        except OSError:
            return secrets.token_hex(2)

    @staticmethod
    def _is_trusted_nodename(nodename: str) -> bool:
        """A nodename is trusted to persist only if fully-qualified (contains a
        ".") and not a known-generic placeholder. The bug this guards against is
        a transient bare "Mac"/"localhost" reading being frozen into a permanent
        ledger key that pins the GC floor forever."""
        norm = (nodename or "").strip()
        return "." in norm and norm.lower() not in _GENERIC_NODENAMES

    def _resolve_host_id(self) -> str:
        """Stable per-machine identity, persisted in the local (non-Drive) dir.

        Reads an existing ``host_id`` verbatim. Otherwise it only *persists* a
        trusted (qualified) nodename — freezing the canonical identity with zero
        ledger churn for machines that already report a ``.local`` name. An
        untrusted/transient reading is used session-only and re-resolved on the
        next startup, so a flaky bare name can never become a permanent ghost.
        Writes are atomic (tmp + ``os.replace``) and the on-disk value is adopted
        after writing, so two processes on one machine (§3.6) converge on one id.
        Fail-open: any I/O error falls back to the normalized live nodename.
        """
        path = self.local_db.parent / _HOST_ID
        try:
            if path.exists():
                existing = path.read_text(encoding="utf-8").strip()
                if existing:
                    return existing
            norm = os.uname().nodename.strip()
            if not self._is_trusted_nodename(norm):
                # Don't freeze a transient name; retry on the next startup.
                return self._normalize_host(norm)
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f"{_HOST_ID}.{os.getpid()}.tmp")
            tmp.write_text(norm, encoding="utf-8")  # persist the normalized value
            os.replace(tmp, path)
            # Adopt the on-disk winner so racing seeders converge on one value.
            winner = path.read_text(encoding="utf-8").strip()
            return winner or norm
        except OSError as exc:  # pragma: no cover - defensive fail-open
            logger.warning("host_id resolve failed (%s); using normalized nodename", exc)
            return self._normalize_host(os.uname().nodename)

    # ── meta helpers ──────────────────────────────────────────────

    @staticmethod
    def _read_json(path: Path) -> dict:
        try:
            return json.loads(path.read_text())
        except (FileNotFoundError, OSError, ValueError):
            return {}

    @staticmethod
    def _write_json(path: Path, data: dict) -> None:
        # Unique tmp name per writer (pid + thread + counter) so two concurrent
        # writers to the same JSON file never collide on a shared .tmp and lose
        # one os.replace to FileNotFoundError. os.replace itself is atomic.
        tmp = path.with_suffix(
            f"{path.suffix}.{os.getpid()}.{threading.get_ident()}."
            f"{next(SnapshotSync._tmp_counter)}.tmp"
        )
        tmp.write_text(json.dumps(data))
        os.replace(tmp, path)

    def _remote_generation(self) -> int:
        # The pointer is the Drive-synced source of truth for the gen counter in
        # v2; fall back to the v1 sidecar otherwise.
        if self.snapshot_ptr.exists():
            return int(self._read_json(self.snapshot_ptr).get("generation", 0))
        return int(self._read_json(self.snapshot_meta).get("generation", 0))

    def _local_known_generation(self) -> int:
        return int(self._read_json(self.local_meta).get("generation", 0))

    def _last_pushed_hash(self) -> str | None:
        return self._read_json(self.local_meta).get("content_hash")

    def _remote_format_version(self) -> int:
        # A pointer means the immutable v2 format; otherwise read the v1 sidecar.
        # Absent format_version == a pre-gate writer == v1 (compatible).
        if self.snapshot_ptr.exists():
            return int(self._read_json(self.snapshot_ptr).get("format_version", _FORMAT_V2))
        return int(self._read_json(self.snapshot_meta).get("format_version", 1))

    # ── pull (startup only) ───────────────────────────────────────

    def pull_if_newer(self) -> bool:
        """If the Drive snapshot is newer, copy it down to the local DB.

        Called ONCE at startup before the DB connection is opened. Returns True
        if a pull happened. Dispatches to the immutable-blob path when a pointer
        is present (§3.2), else the legacy bare-file path — a v2-aware binary
        reads BOTH formats.
        """
        try:
            if self.snapshot_ptr.exists():
                pulled = self._pull_v2()
                # After reconciling against the pointer's blob, fold any
                # self-consistent peer orphan blob the pointer doesn't name —
                # otherwise a same-gen collision converges only when its host
                # re-publishes via the outbox (§3.3a).
                swept = self._sweep_orphan_blobs()
                return pulled or swept
            return self._pull_v1()
        except Exception:
            logger.exception("snapshot pull failed; continuing with local DB")
            self._record_sync_failure()
            return False

    def _pull_v1(self) -> bool:
        """Legacy pull: read the single mutable ``graph.snapshot.db`` + sidecar."""
        if not self.snapshot_db.exists():
            return False
        # Fail closed on a format this binary can't parse (§3.8).
        remote_format = self._remote_format_version()
        if remote_format > _FORMAT_V2:
            logger.error(
                "snapshot format_version %d exceeds supported %d; refusing to pull "
                "(upgrade this machine)",
                remote_format,
                _FORMAT_V2,
            )
            return False
        remote_gen = self._remote_generation()
        local_gen = self._local_known_generation()
        if remote_gen <= local_gen and self.local_db.exists():
            return False  # local is current (or ahead)

        # Validate the snapshot before trusting it over local.
        if not self._is_valid_sqlite(self.snapshot_db):
            logger.warning("snapshot at %s failed integrity check; not pulling", self.snapshot_db)
            return False

        return self._merge_or_adopt(self.snapshot_db, remote_gen)

    def _pull_v2(self) -> bool:
        """Immutable-blob pull (§3.2): resolve the blob named by the pointer and
        union it in.

        Pointer-ahead-of-blob is the common case (the tiny pointer syncs before
        the multi-MB blob): a reader that sees a pointer whose blob is absent or
        unreadable treats it as "no newer snapshot yet" and defers — never an
        error, never acted upon (§3.2.6).
        """
        ptr = self._read_json(self.snapshot_ptr)
        remote_format = int(ptr.get("format_version", _FORMAT_V2))
        if remote_format > _FORMAT_V2:
            logger.error(
                "snapshot pointer format_version %d exceeds supported %d; refusing to pull "
                "(upgrade this machine)",
                remote_format,
                _FORMAT_V2,
            )
            return False
        remote_gen = int(ptr.get("generation", 0))
        local_gen = self._local_known_generation()
        if remote_gen <= local_gen and self.local_db.exists():
            return False  # local is current (or ahead)

        blob = self._resolve_pointer_blob(ptr, remote_gen)
        if blob is None:
            # Pointer-ahead-of-blob / torn / unverified: no newer snapshot yet.
            logger.info(
                "snapshot pointer gen %d not readable/verified yet; deferring pull", remote_gen
            )
            return False

        changed = self._merge_or_adopt(blob, remote_gen)
        # Record that this host has now merged up to remote_gen so a peer's GC
        # never collects a blob we still need (§3.2.7).
        self._record_merged_generation(remote_gen)
        return changed

    def _resolve_pointer_blob(self, ptr: dict, remote_gen: int) -> Path | None:
        """Return the present, valid, self-verified blob the pointer names, or
        None if it isn't trustworthy yet — pointer-ahead-of-blob, torn, or
        inconsistent. All three are tolerated as "no newer snapshot yet" (§3.2.6);
        callers decide whether that means defer-pull or defer-push."""
        blob_name = ptr.get("blob")
        blob = self.snapshot_dir / blob_name if blob_name else None
        if not blob or not blob.exists():
            return None  # pointer-ahead-of-blob — the common, un-noteworthy case
        if not self._is_valid_sqlite(blob):
            logger.warning("snapshot blob %s failed integrity check; not trusting", blob)
            return None
        if not self._blob_matches_pointer(blob, remote_gen, ptr.get("content_hash")):
            logger.warning(
                "snapshot blob %s failed self-verification (torn/inconsistent); not trusting",
                blob,
            )
            return None
        return blob

    # ── startup orphan-blob sweep (§3.3a convergence) ─────────────

    def _sweep_orphan_blobs(self) -> bool:
        """Fold every self-consistent peer blob the current pointer does NOT name
        and we have not already folded, additively (§3.3a).

        The pointer pull reconciles only against the blob the pointer names, and
        short-circuits entirely once ``remote_gen <= local_gen``. So a peer's
        same-gen orphan blob (a generation collision, retained by GC) is never
        folded by a machine whose own lineage already reached that gen —
        convergence then waits on the orphaned host re-publishing via the outbox.
        This sweep removes that latency by additively unioning each trustworthy
        orphan on disk; the union is no-loss (INSERT OR IGNORE + tombstone
        deny-set), so folding a lower-gen orphan can never resurrect a deleted
        row or drop a local one.

        Skips: (a) the blob the pointer names — folding it is the v2 reader's job,
        and skipping it keeps a torn/hash-mismatched pointer from being worked
        around here (the adversarial pointer tests rely on this); (b) blobs
        already recorded in the local ``.sync_swept.json`` sidecar; (c) blobs
        that aren't self-consistent (invalid sqlite, or embedded identity hash !=
        projection hash — torn/half-synced). Returns True if any orphan changed
        the local node set. Each fold is wrapped so one bad blob can't abort the
        rest of the sweep.
        """
        if not self.local_db.exists():
            # Nothing to union into yet; a fresh machine's pointer pull adopts the
            # blob wholesale first. (Guards against _union_pull creating an empty,
            # schema-less local DB.)
            return False
        pointer_blob = self._read_json(self.snapshot_ptr).get("blob")
        already_folded = self._read_swept_blobs()
        changed = False
        newly_folded: set[str] = set()
        present: set[str] = set()
        for blob in sorted(self.snapshot_dir.glob("graph.snapshot.*.db")):
            name = blob.name
            present.add(name)
            if name == pointer_blob or name in already_folded:
                continue
            try:
                if not self._blob_self_consistent(blob):
                    continue
                with self.lock:  # serialize the union against any live writer (§3.6)
                    if self._union_pull(blob):
                        changed = True
                        # Folded durable rows make local a superset worth
                        # re-publishing so the orphan's content rejoins the
                        # named lineage and the snapshot converges (§3.3a).
                        self._durable_dirty = True
                        # Only mark a blob swept once it ACTUALLY folded — a
                        # failed union (transient lock/IO) must be retried, not
                        # permanently blacklisted in .sync_swept.json (P8).
                        newly_folded.add(name)
            except Exception:
                logger.exception("orphan-blob sweep: failed folding %s; skipping", name)
        # Persist the folded set, pruned to blobs still on disk: an immutable
        # blob name never recurs once GC'd, so dropping vanished names keeps the
        # sidecar bounded by the live blob set instead of growing forever.
        keep = (already_folded | newly_folded) & present
        if keep != already_folded:
            self._record_swept_blobs(keep)
        if newly_folded:
            logger.info("orphan-blob sweep: folded %d blob(s)", len(newly_folded))
        return changed

    def _blob_self_consistent(self, blob: Path) -> bool:
        """True iff ``blob`` is valid sqlite AND its embedded identity content-hash
        equals its actual projection hash — i.e. the on-disk bytes are internally
        consistent (not torn/half-synced). Mirrors the pointer self-verification
        (``_blob_matches_pointer``) but needs no pointer: an orphan has none to
        check against. A blob with no embedded identity (pre-v2 / non-MemCP) is
        not trusted."""
        if not self._is_valid_sqlite(blob):
            return False
        embedded = self._read_blob_identity(blob)
        if embedded is None:
            return False
        _gen, emb_hash = embedded
        return (self._projection_hash(blob) or "") == emb_hash

    def _read_swept_blobs(self) -> set[str]:
        folded = self._read_json(self.swept_meta).get("folded", [])
        return set(folded) if isinstance(folded, list) else set()

    def _record_swept_blobs(self, names: set[str]) -> None:
        self._write_json(self.swept_meta, {"folded": sorted(names)})

    def _merge_or_adopt(self, snap_path: Path, remote_gen: int) -> bool:
        """Fold ``snap_path`` into the local DB under the flock, then record the
        generation. Fresh machine (no local DB) adopts the snapshot wholesale;
        otherwise additively unions it (§3.1). Returns True if anything changed.
        """
        self.local_db.parent.mkdir(parents=True, exist_ok=True)
        start = time.monotonic()
        adopted = False
        self._last_merge_stats = {"inserted": 0, "deleted": 0}
        with self.lock:  # hold the local flock for the whole pull (§3.6)
            if not self.local_db.exists():
                # Fresh machine: no local DB to merge into — adopt the snapshot.
                tmp = self.local_db.with_suffix(".db.pulltmp")
                shutil.copy2(snap_path, tmp)
                os.replace(tmp, self.local_db)
                changed = True
                adopted = True
            else:
                # Back up the existing local DB before merging (never lose it).
                shutil.copy2(self.local_db, self.local_db.with_suffix(".db.prepull"))
                changed = self._union_pull(snap_path)
            # Record the snapshot generation we've now folded in (never lower our
            # known generation — a same-gen/older foreign fold must not rewind it).
            # Keep the last-pushed content-hash as-is: a merge makes local a
            # superset of the snapshot, so the next durable push must still detect
            # a difference and re-publish the superset (§3.2 catch-up).
            self._write_json(
                self.local_meta,
                {
                    "generation": max(remote_gen, self._local_known_generation()),
                    "host": self._host,
                    "content_hash": self._last_pushed_hash(),
                },
            )
            # A changed merge is durable local state worth re-publishing as a
            # superset so it propagates and the snapshot converges (§3.2/§3.3a).
            if changed:
                self._durable_dirty = True
        logger.info("pulled snapshot generation %d from %s", remote_gen, snap_path)
        telemetry.emit(
            "sync",
            "merge",
            dur_ms=round((time.monotonic() - start) * 1000, 3),
            gen=remote_gen,
            changed=changed,
            adopted=adopted,
            rows_inserted=self._last_merge_stats["inserted"],
            rows_deleted=self._last_merge_stats["deleted"],
        )
        return changed

    def _union_pull(self, snap_path: Path | None = None) -> bool:
        """Additively merge ``snap_path`` (default: the bare snapshot) into the
        local DB.

        Pull only ever ADDS rows (``INSERT OR IGNORE``) and applies tombstones
        as a deny-set; it never deletes a local row to match the snapshot. This
        is the basis of the no-loss guarantee (local-DB monotonicity, §1).
        Returns True if the local node set changed. See spec §3.1.
        """
        snap_path = snap_path or self.snapshot_db
        conn = sqlite3.connect(str(self.local_db), isolation_level=None)
        try:
            conn.execute("PRAGMA foreign_keys=ON")  # deny-set cascade cleans edges
            # Old local DBs may predate the tombstones table.
            conn.execute(
                "CREATE TABLE IF NOT EXISTS tombstones ("
                "id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL, "
                "resurrected_at TEXT DEFAULT NULL)"
            )
            conn.execute("ATTACH DATABASE ? AS snap", (str(snap_path),))
            try:
                # Explicit column intersection — never SELECT * (divergent ALTER
                # histories make ordinal binding silently corrupt columns).
                # Exclude ingest_seq: it's a per-MACHINE monotonic counter, so the
                # origin's value is meaningless here and would collide with the
                # local sequence. Merged rows land with ingest_seq NULL and get a
                # fresh local seq from the backfill on the next open, which keeps
                # the §3.4 reindex cut correct.
                local_cols = [r[1] for r in conn.execute("PRAGMA main.table_info(nodes)")]
                snap_cols = {r[1] for r in conn.execute("PRAGMA snap.table_info(nodes)")}
                common = [c for c in local_cols if c in snap_cols and c != "ingest_seq"]
                col_list = ", ".join(common)

                # Baseline of PRE-EXISTING FK violations (latent orphans from an
                # old bug or an interrupted cascade). The union only INSERTs nodes
                # and cascade-deletes, so it cannot itself create a dangling ref —
                # a whole-DB check therefore only ever re-reports these pre-existing
                # orphans, and aborting on them silently discarded the ENTIRE pull
                # (the gen-210 / 43-insight loss). Compare against this baseline so
                # we abort ONLY on violations the merge actually introduced. Read
                # it BEFORE the write txn so the scan cursor isn't held across the
                # attached-db DETACH. snap's own (constant) violations appear in
                # both the pre and post counts and cancel out of the delta.
                pre_fk_violations = len(conn.execute("PRAGMA foreign_key_check").fetchall())
                conn.execute("BEGIN IMMEDIATE")
                ins = conn.execute(
                    f"INSERT OR IGNORE INTO nodes ({col_list}) "  # noqa: S608 — cols from schema
                    f"SELECT {col_list} FROM snap.nodes"
                ).rowcount

                # Union tombstones, newest-wins per field (MAX), if snap has them.
                snap_has_tomb = conn.execute(
                    "SELECT 1 FROM snap.sqlite_master WHERE type='table' AND name='tombstones'"
                ).fetchone()
                if snap_has_tomb:
                    conn.execute(
                        "INSERT INTO tombstones (id, deleted_at, resurrected_at) "
                        "SELECT id, deleted_at, resurrected_at FROM snap.tombstones WHERE true "
                        "ON CONFLICT(id) DO UPDATE SET "
                        "deleted_at = MAX(tombstones.deleted_at, excluded.deleted_at), "
                        "resurrected_at = NULLIF(MAX("
                        "COALESCE(tombstones.resurrected_at, ''), "
                        "COALESCE(excluded.resurrected_at, '')), '')"
                    )

                # Deny-set: drop any node whose tombstone is not out-ranked by a
                # restore. Applied AFTER the node union so "present in snapshot +
                # tombstoned" resolves to deleted regardless of arrival order.
                deleted = conn.execute(
                    "DELETE FROM nodes WHERE id IN ("
                    "SELECT id FROM tombstones "
                    "WHERE resurrected_at IS NULL OR deleted_at > resurrected_at)"
                ).rowcount

                violations = conn.execute("PRAGMA foreign_key_check").fetchall()
                if len(violations) > pre_fk_violations:
                    conn.execute("ROLLBACK")
                    logger.error(
                        "union pull aborted: merge introduced %d new FK violation(s): %s",
                        len(violations) - pre_fk_violations,
                        violations,
                    )
                    self._last_merge_stats = {"inserted": 0, "deleted": 0}
                    return False
                conn.execute("COMMIT")

                if ins or deleted:
                    self._invalidate_derived_indexes(conn)
                self._last_merge_stats = {"inserted": ins, "deleted": deleted}
                return bool(ins or deleted)
            finally:
                conn.execute("DETACH DATABASE snap")
        finally:
            conn.close()

    @staticmethod
    def _invalidate_derived_indexes(conn: sqlite3.Connection) -> None:
        """Signal a derived-index rebuild after a merge changed the node set.

        Bump meta.revision so _is_stale fires; the next reindex then rebuilds
        incrementally on the ingest_seq cut (§3.4). Merged rows carry a fresh
        local ingest_seq (the union assigns it via backfill), so they fall ABOVE
        the last built_against_seq and get indexed — without a full O(N) rebuild.
        index_meta is intentionally kept (not cleared) so the incremental cut
        applies."""
        if conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
        ).fetchone():
            from memcp.core.revision import bump_revision

            bump_revision(conn)

    # ── push (after writes) ───────────────────────────────────────

    def mark_durable_dirty(self) -> None:
        """Signal that a durable node change occurred (store/delete/metadata edit).

        Only durable changes republish — reads and derived-index churn must not.
        See spec §3.3.
        """
        self._durable_dirty = True

    # Backwards-compatible alias: a bare ``mark_dirty`` means "something durable
    # changed" (used by tests and any external caller).
    def mark_dirty(self) -> None:
        self.mark_durable_dirty()

    def _catch_up_or_defer(self) -> bool:
        """Before publishing a v2 superset, fold in any snapshot we haven't
        incorporated so ``local ⊇ snapshot`` (§3.2.2).

        "Newer" is judged by content, not just the generation counter: a *same-gen*
        clobber by another host carries different rows under an equal ``<gen>``,
        so we fold whenever the pointer is a peer's and its content hash isn't one
        we've already published. Returns True if it is safe to publish; False
        (defer) when the snapshot we'd need to fold isn't readable yet —
        publishing then would drop its rows and re-create single-copy exposure
        (§3.2.6).
        """
        if not self.snapshot_ptr.exists():
            return True
        ptr = self._read_json(self.snapshot_ptr)
        remote_gen = int(ptr.get("generation", 0))
        newer_gen = remote_gen > self._local_known_generation()
        foreign_unseen = (
            ptr.get("host") != self._host and ptr.get("content_hash") != self._last_pushed_hash()
        )
        if not (newer_gen or foreign_unseen):
            return True  # already incorporated — nothing to fold in
        blob = self._resolve_pointer_blob(ptr, remote_gen)
        if blob is None:
            logger.info(
                "remote pointer gen %d carries content we lack but its blob is not "
                "readable yet; deferring push to avoid a row-dropping superset",
                remote_gen,
            )
            return False
        self._merge_or_adopt(blob, remote_gen)
        return True

    def _needs_outbox_repush(self) -> bool:
        """True when our locally-originated rows have fallen out of the current
        named lineage — a peer clobbered the pointer at or below our published
        generation with a snapshot that need not contain them (§3.3a).

        Quiet for single-machine use: if the current pointer is our own, our rows
        are in the lineage, so this never forces a (churning) re-push.
        """
        if not self._immutable or not self.snapshot_ptr.exists():
            return False
        ptr = self._read_json(self.snapshot_ptr)
        if ptr.get("host") == self._host:
            return False  # our rows are in the current named lineage
        try:
            remote_gen = int(ptr.get("generation", 0))
        except (TypeError, ValueError):
            return False  # torn / half-synced pointer — treat as no-newer-yet
        return remote_gen <= self._local_known_generation()

    def push(self, *, force: bool = False) -> bool:
        """Take a consistent snapshot of the local DB and publish it to Drive."""
        with self._guard:
            now = time.monotonic()
            if not force and (not self._durable_dirty or now - self._last_push < self.min_interval):
                return False
            try:
                # Never clobber a snapshot written in a newer format (§3.8).
                if self._remote_format_version() > _FORMAT_V2:
                    logger.error(
                        "remote snapshot format_version exceeds supported %d; refusing to "
                        "overwrite (upgrade this machine)",
                        _FORMAT_V2,
                    )
                    return False
                # A v1 (immutable-off) writer must stand down once a v2 lineage is
                # live: overwriting the bare file would fork the snapshot away from
                # the pointer readers follow. Stragglers wait for the v2 flip (§3.8).
                if not self._immutable and self.snapshot_ptr.exists():
                    logger.warning(
                        "v2 snapshot pointer present; this v1 writer is standing down "
                        "(enable MEMCP_SNAPSHOT_IMMUTABLE to publish)"
                    )
                    return False
                # v2: fold in any newer snapshot BEFORE publishing so our blob is
                # a true superset; defer if the remote blob isn't readable yet
                # (pointer-ahead-of-blob — §3.2.2/§3.2.6).
                if self._immutable and not self._catch_up_or_defer():
                    return False
                with self.lock:  # serialize against any commit while we snapshot
                    self.snapshot_dir.mkdir(parents=True, exist_ok=True)
                    # Consistent copy of the (possibly live) local DB → local temp.
                    local_tmp = self.local_db.with_suffix(".db.snaptmp")
                    self._backup_db(self.local_db, local_tmp)
                    # Quiescence short-circuit: if the durable projection (nodes
                    # minus derived counters, plus tombstones) is byte-identical
                    # to what we last published, skip — derived churn must never
                    # mint a new generation. Foreign DBs (hash None) always push.
                    proj = self._projection_hash(local_tmp)
                    if proj is not None and proj == self._last_pushed_hash():
                        local_tmp.unlink(missing_ok=True)
                        self._durable_dirty = False
                        self._last_push = now
                        logger.debug("snapshot unchanged (quiescent); skipping push")
                        return False
                    next_gen = max(self._remote_generation(), self._local_known_generation()) + 1
                    if self._immutable:
                        published = self._publish_v2(local_tmp, proj, next_gen)
                    else:
                        published = self._publish_v1(local_tmp, proj, next_gen)
                    self._write_json(
                        self.local_meta,
                        {"generation": next_gen, "host": self._host, "content_hash": proj},
                    )
                    self._durable_dirty = False
                    self._last_push = now
                logger.info("pushed snapshot generation %d to %s", next_gen, self.snapshot_db)
                # Telemetry (metadata only; emitted outside the lock so the append
                # never serializes a real writer). Fail-open inside emit().
                try:
                    blob_bytes = published.stat().st_size
                except OSError:
                    blob_bytes = 0
                telemetry.emit(
                    "sync",
                    "push",
                    dur_ms=round((time.monotonic() - now) * 1000, 3),
                    bytes=blob_bytes,
                    gen=next_gen,
                    immutable=self._immutable,
                )
                # A real publish propagated local state — clear the failure run.
                self._sync_error_count = 0
                return True
            except Exception:
                logger.exception("snapshot push failed; will retry on next flush")
                self._record_sync_failure()
                return False

    def _publish_v1(self, local_tmp: Path, proj: str | None, next_gen: int) -> Path:
        """Legacy publish: overwrite the single mutable ``graph.snapshot.db`` +
        sidecar meta. The default until the v2 flip (§3.8 reader-first). Returns
        the published file path."""
        drive_tmp = self.snapshot_db.with_suffix(".db.uploadtmp")
        shutil.copy2(local_tmp, drive_tmp)
        os.replace(drive_tmp, self.snapshot_db)
        local_tmp.unlink(missing_ok=True)
        self._write_json(
            self.snapshot_meta,
            {
                "generation": next_gen,
                "host": self._host,
                "written_at": time.time(),
                "format_version": _FORMAT_VERSION,
            },
        )
        return self.snapshot_db

    def _publish_v2(self, local_tmp: Path, proj: str | None, next_gen: int) -> Path:
        """Immutable publish (§3.2): write a globally-unique, never-overwritten
        blob ``graph.snapshot.<gen>.<host>.<rand>.db`` and name it from a tiny
        pointer. The ``<host>.<rand>`` suffix makes the filename globally unique
        without a synced read, so two machines that both mint the same stale
        ``<gen>`` still produce distinct blobs that both survive. Returns the
        published blob path."""
        blob_name = f"graph.snapshot.{next_gen}.{self._host}.{secrets.token_hex(4)}.db"
        blob_path = self.snapshot_dir / blob_name
        blob_tmp = blob_path.with_suffix(".db.uploadtmp")
        # Stamp the blob's identity (gen + content hash) INSIDE the DB so a pull
        # can verify the on-disk bytes are the ones the pointer names — defeats
        # a torn / half-synced Drive blob (§3.2.5). meta is not in the projection
        # hash, so stamping it doesn't change content_hash.
        self._stamp_blob_identity(local_tmp, next_gen, proj)
        shutil.copy2(local_tmp, blob_tmp)
        os.replace(blob_tmp, blob_path)
        local_tmp.unlink(missing_ok=True)
        # The pointer is the tiny, fast-syncing source of truth: name + hash + gen.
        self._write_json(
            self.snapshot_ptr,
            {
                "generation": next_gen,
                "blob": blob_name,
                "content_hash": proj,
                "format_version": _FORMAT_V2,
                "host": self._host,
                "written_at": time.time(),
            },
        )
        # Fail-closed beacon (§3.8): stamp the v2 format into the LEGACY sidecar
        # too, so a pre-gate / v1-only binary — which never opens the pointer —
        # still sees format_version > its max and refuses to pull or overwrite,
        # rather than reading a stale bare graph.snapshot.db and clobbering us.
        self._write_json(
            self.snapshot_meta,
            {
                "generation": next_gen,
                "host": self._host,
                "written_at": time.time(),
                "format_version": _FORMAT_V2,
            },
        )
        # We caught up to the prior gen before publishing, so our local now
        # ⊇ gen next_gen; record it and GC blobs every host has moved past.
        self._record_merged_generation(next_gen)
        self._gc_blobs(blob_name)
        return blob_path

    # ── per-host last-merged-gen ledger + blob GC (§3.2.7) ────────

    def _record_merged_generation(self, generation: int) -> None:
        """Bump this host's last-merged generation in the Drive-synced ledger.

        This is a lock-free read-modify-write of a shared Drive file, so a naive
        ``read → set our key → write`` loses a concurrent writer's entry: the
        loser's write overwrites the winner's key. Dropping a host's entry
        SHRINKS the floor ``min(ledger.values())`` is computed from is wrong —
        it actually INFLATES the floor (the dropped host no longer pins it low),
        so the floor pass can then delete blobs that host still needs. That is
        the predicted "fourth silent loss", NOT the harmless "retains blobs
        longer" the old docstring claimed.

        Mitigation (cr-sqlite-style grow-only merge): re-read the ledger
        immediately before writing and merge per-key with MAX, so our write can
        never drop or roll back another host's entry — the host set only ever
        grows. A re-read that shows FEWER hosts than our first read is a sign of
        ledger loss elsewhere and is logged at error level. (A flock would close
        the window entirely; merge-MAX narrows it without a Drive-wide lock.)
        """
        before = self._read_json(self.snapshot_merged)
        # Re-read right before writing so a peer's just-committed entry is folded
        # in rather than clobbered.
        current = self._read_json(self.snapshot_merged)
        if len(current) < len(before):
            logger.error(
                "merged-ledger host set shrank (%d -> %d hosts) between reads; "
                "possible concurrent ledger loss",
                len(before),
                len(current),
            )
        merged: dict = {}
        for host in set(before) | set(current):
            try:
                merged[host] = max(int(before.get(host, 0)), int(current.get(host, 0)))
            except (TypeError, ValueError):
                continue  # skip a malformed entry rather than abort the whole write
        merged[self._host] = max(int(merged.get(self._host, 0)), generation)
        if merged != current:
            self._write_json(self.snapshot_merged, merged)

    @staticmethod
    def _blob_gen_from_name(name: str) -> int | None:
        """Parse the ``<gen>`` integer from ``graph.snapshot.<gen>.<host>.<rand>.db``."""
        parts = name.split(".")
        if len(parts) >= 3 and parts[0] == "graph" and parts[1] == "snapshot":
            try:
                return int(parts[2])
            except ValueError:
                return None
        return None

    @staticmethod
    def _tomb_covered(
        ptr: tuple[str | None, str | None], cand: tuple[str | None, str | None]
    ) -> bool:
        """True iff merging the candidate's tombstone state would NOT change the
        pointer's, under the same per-field newest-wins rule the union applies
        (``deleted_at = MAX``, ``resurrected_at = MAX``). A candidate carrying a
        fresher re-delete or resurrection is therefore NOT covered, so it must
        not be reclaimed — its state is the sole record of that transition."""
        pd, pr = ptr
        cd, cr = cand
        merged_d = max(pd or "", cd or "") or None
        merged_r = max(pr or "", cr or "") or None
        return merged_d == pd and merged_r == pr

    @staticmethod
    def _blob_id_sets(
        path: Path,
    ) -> tuple[set[str], dict[str, tuple[str | None, str | None]]] | None:
        """Return ``(node_ids, {tomb_id: (deleted_at, resurrected_at)})`` for a
        snapshot blob. ``None`` if the blob can't be read (corrupt/half-synced).

        Tombstone *state* (not just id) is returned because a tombstone's meaning
        is its ``(deleted_at, resurrected_at)`` values: the cap must refuse a blob
        whose tombstone advances a re-delete/resurrection the pointer lacks, or it
        would silently drop that transition (no live host re-publishes it). Opened
        without a URI so paths with ``#``/``?``/``%`` are never mis-parsed;
        immutable blobs are never reopened for write."""
        try:
            conn = sqlite3.connect(str(path))
        except sqlite3.Error:
            return None
        try:
            nodes = {r[0] for r in conn.execute("SELECT id FROM nodes")}
            tombs: dict[str, tuple[str | None, str | None]] = {}
            try:
                for tid, d_at, r_at in conn.execute(
                    "SELECT id, deleted_at, resurrected_at FROM tombstones"
                ):
                    tombs[tid] = (d_at, r_at)
            except sqlite3.OperationalError:
                pass  # older blobs may predate the tombstones table
            return nodes, tombs
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def _blob_content_covered(
        self, blob: Path, ptr_sets: tuple[set[str], dict] | None
    ) -> bool:
        """True iff every node-id and tombstone-state in ``blob`` is already
        present in the pointer superset ``ptr_sets`` (as returned by
        ``_blob_id_sets``) — i.e. reclaiming ``blob`` cannot lose content.
        Conservative: an unreadable pointer or candidate returns False (refuse to
        delete), so a blob is only reclaimed when its preservation is proven."""
        if ptr_sets is None:
            return False
        sets = self._blob_id_sets(blob)
        if sets is None:
            return False
        c_nodes, c_tombs = sets
        ptr_nodes, ptr_tombs = ptr_sets
        if not (c_nodes <= ptr_nodes):
            return False
        for tid, state in c_tombs.items():
            if tid not in ptr_tombs or not self._tomb_covered(ptr_tombs[tid], state):
                return False
        return True

    def _gc_blobs(self, keep_blob_name: str) -> None:
        """Reclaim immutable snapshot blobs in two passes.

        Floor pass (§3.2.7): delete blobs every known host has merged past
        (``gen < floor``) — but, like the cap pass, ONLY when the blob's
        node-ids and tombstone-states are a subset of the retained pointer blob's
        (content-verified). A below-floor blob holding a row the pointer lacks
        (an offline originator's single copy) is refused, never silently
        unlinked — closing the orphan hole (P2). Skipped while the floor is
        static. Cap pass (hard backstop): if the blob count still exceeds
        ``self._max_blobs``, reclaim oldest-first, but **only** a blob whose
        node-ids and tombstone-ids are subsets of the live pointer blob's — i.e.
        whose content is already preserved in the retained superset. A blob with
        any row/tombstone not yet in the pointer is refused, so an offline
        originator's single-copy rows can never be deleted. The pointer blob is
        never deleted by either pass.
        """
        ledger = self._read_json(self.snapshot_merged)
        floor: int | None = None
        if ledger:
            try:
                floor = min(int(g) for g in ledger.values())
            except (TypeError, ValueError):
                floor = None

        # ── floor pass (content-verified) ───────────────────────────
        if floor is not None and floor > self._last_gc_floor:
            floor_ptr_sets = self._blob_id_sets(self.snapshot_dir / keep_blob_name)
            for blob in self.snapshot_dir.glob("graph.snapshot.*.db"):
                if blob.name == keep_blob_name:
                    continue
                gen = self._blob_gen_from_name(blob.name)
                if gen is None or gen >= floor:
                    continue
                # Content-verify before deleting (P2): a below-floor blob whose
                # rows/tombstones are NOT all in the pointer superset holds
                # single-copy content and must never be silently unlinked.
                if self._blob_content_covered(blob, floor_ptr_sets):
                    blob.unlink(missing_ok=True)
                    logger.info(
                        "GC floor: reclaimed blob %s (gen %d < floor %d)",
                        blob.name,
                        gen,
                        floor,
                    )
                    telemetry.emit(
                        "sync",
                        "gc",
                        **{"pass": "floor"},
                        blob=blob.name,
                        gen=gen,
                        floor=floor,
                        action="deleted",
                    )
                else:
                    logger.warning(
                        "GC floor: %s holds content not in the pointer superset; "
                        "refusing to reclaim (single-copy content)",
                        blob.name,
                    )
                    telemetry.emit(
                        "sync",
                        "gc",
                        **{"pass": "floor"},
                        blob=blob.name,
                        gen=gen,
                        floor=floor,
                        action="refused",
                    )
            self._last_gc_floor = floor

        # ── cap pass (content-verified; floor-independent) ──────────
        if not self._max_blobs or self._max_blobs <= 0:
            return
        candidates: list[tuple[int, str, Path]] = []
        unparseable = 0
        for p in self.snapshot_dir.glob("graph.snapshot.*.db"):
            if p.name == keep_blob_name:
                continue
            gen = self._blob_gen_from_name(p.name)
            if gen is None:
                # Counts toward the cap (it occupies disk) but is never a
                # deletion target — so GC compensates by reclaiming that many
                # extra well-named, content-covered blobs to hold the bound.
                unparseable += 1
                logger.warning(
                    "snapshot cap: unparseable blob name %s (counts toward cap, never deleted)",
                    p.name,
                )
                continue
            candidates.append((gen, p.name, p))

        # One slot is reserved for the always-retained pointer blob.
        over = (len(candidates) + unparseable) - (self._max_blobs - 1)
        if over <= 0:
            return

        ptr_sets = self._blob_id_sets(self.snapshot_dir / keep_blob_name)
        if ptr_sets is None:
            logger.warning(
                "snapshot cap: cannot read pointer blob %s; skipping cap reclaim", keep_blob_name
            )
            return
        ptr_nodes, ptr_tombs = ptr_sets

        candidates.sort(key=lambda t: (t[0], t[1]))  # oldest gen, then name
        reclaimed = 0
        refused = 0
        for _gen, name, blob in candidates:
            if reclaimed >= over:
                break
            sets = self._blob_id_sets(blob)
            if sets is None:
                logger.warning("snapshot cap: cannot read candidate %s; retaining", name)
                continue
            c_nodes, c_tombs = sets
            # Safe to reclaim only if every row id is already in the pointer AND
            # every tombstone's STATE is already covered (merging it would not
            # advance a delete/resurrect). Otherwise its content is single-copy.
            nodes_ok = c_nodes <= ptr_nodes
            uncovered_tombs = [
                tid
                for tid, state in c_tombs.items()
                if tid not in ptr_tombs or not self._tomb_covered(ptr_tombs[tid], state)
            ]
            if nodes_ok and not uncovered_tombs:
                blob.unlink(missing_ok=True)
                reclaimed += 1
            else:
                refused += 1
                logger.warning(
                    "snapshot cap: %s holds %d row(s)/%d tombstone-state(s) not in pointer; "
                    "refusing to reclaim (content not yet in superset)",
                    name,
                    len(c_nodes - ptr_nodes),
                    len(uncovered_tombs),
                )

        if reclaimed:
            pinned_by = "n/a"
            if floor is not None and ledger:
                try:
                    pinned_by = min(ledger, key=lambda k: int(ledger[k]))
                except (TypeError, ValueError):
                    pinned_by = "n/a"
            logger.info(
                "snapshot cap: reclaimed %d blob(s) over limit %d; floor=%s pinned by host=%s",
                reclaimed,
                self._max_blobs,
                floor if floor is not None else "n/a",
                pinned_by,
            )
        if refused:
            logger.info(
                "snapshot cap: %d blob(s) over limit retained — "
                "content not yet in pointer superset",
                refused,
            )
        if reclaimed or refused:
            telemetry.emit(
                "sync",
                "gc",
                reclaimed=reclaimed,
                refused=refused,
                floor=floor if floor is not None else -1,
                max_blobs=self._max_blobs,
            )

    @staticmethod
    def _stamp_blob_identity(db_path: Path, generation: int, content_hash: str | None) -> None:
        """Write the blob's gen + content hash into its own ``meta`` table so a
        reader can self-verify the bytes. ``meta`` is excluded from the projection
        hash, so this never perturbs ``content_hash``."""
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('snapshot_generation', ?)",
                (str(generation),),
            )
            conn.execute(
                "INSERT OR REPLACE INTO meta (key, value) VALUES ('snapshot_content_hash', ?)",
                (content_hash or "",),
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _read_blob_identity(blob: Path) -> tuple[int, str] | None:
        """Return the blob's embedded ``(generation, content_hash)`` or None if
        it carries no identity (a pre-v2 or non-MemCP blob)."""
        try:
            conn = sqlite3.connect(str(blob))
        except sqlite3.Error:
            return None
        try:
            if not conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='meta'"
            ).fetchone():
                return None
            rows = dict(
                conn.execute(
                    "SELECT key, value FROM meta "
                    "WHERE key IN ('snapshot_generation', 'snapshot_content_hash')"
                ).fetchall()
            )
            if "snapshot_generation" not in rows:
                return None
            return (int(rows["snapshot_generation"]), rows.get("snapshot_content_hash", ""))
        except (sqlite3.Error, ValueError):
            return None
        finally:
            conn.close()

    @classmethod
    def _blob_matches_pointer(cls, blob: Path, gen: int, ptr_hash: str | None) -> bool:
        """True iff the blob's embedded identity agrees with the pointer AND its
        content actually hashes to the claimed value (§3.2.5)."""
        embedded = cls._read_blob_identity(blob)
        if embedded is None:
            return False
        emb_gen, emb_hash = embedded
        expected = ptr_hash or ""
        if emb_gen != gen or emb_hash != expected:
            return False
        # Content must hash to the claimed value (catches byte-level tears that
        # leave the embedded meta row intact).
        return (cls._projection_hash(blob) or "") == expected

    # ── background flusher ────────────────────────────────────────

    def start_flusher(self) -> None:
        if self._flusher is not None:
            return
        self._flusher = threading.Thread(
            target=self._flush_loop, name="memcp-snapshot", daemon=True
        )
        self._flusher.start()

    def _flush_loop(self) -> None:
        # Immediate first pass so a deferred startup pull lands without waiting a
        # full interval; then settle into the periodic cadence.
        first = True
        while first or not self._stop.wait(self.min_interval):
            first = False
            self._flush_tick()

    def _flush_tick(self) -> None:
        """One pull+push cycle, serialized against a manual sync_now().

        Extracted from _flush_loop so the failure-counting path is unit-testable
        without driving the daemon thread. push() and pull_if_newer() swallow
        their own Drive/IO errors (so neither the daemon nor startup dies), and
        bump _sync_error_count from inside those handlers; a push/pull that
        *raises* out (e.g. a monkeypatched failure in tests) is caught here and
        counted too. Either way a wedged flusher is detected, never silent.
        """
        with self._tick_lock:
            # Pull every tick (a peer may have published since the last tick);
            # an established machine reaches only the additive _union_pull path
            # here (the DB exists, so _merge_or_adopt never file-replaces).
            try:
                # A deferred fresh-machine pull (pull_pending) is completed by
                # this tick like any other; clear it only after the pull attempt
                # succeeds, so a still-failing pull stays flagged as pending.
                self.pull_if_newer()
                self.pull_pending = False
            except Exception:
                logger.exception("snapshot pull tick failed; will retry next interval")
                self._record_sync_failure()
            # Per-pull convergence audit (P6): run it EVERY pull cycle, not only
            # on demand, so a silent non-merge (local diverged from the published
            # snapshot) is detected continuously instead of only when memcp_status
            # happens to be called.
            try:
                self._last_audit = self.convergence_audit()
                self._audit_count += 1
                if self._last_audit and not self._last_audit.get("converged", True):
                    logger.warning(
                        "snapshot convergence audit: local diverged from snapshot "
                        "(delta=%s, snapshot_host=%s)",
                        self._last_audit.get("delta"),
                        self._last_audit.get("snapshot_host"),
                    )
            except Exception:
                logger.exception("snapshot convergence audit tick failed")
            # Existing push logic:
            try:
                if self._durable_dirty:
                    self.push()
                elif self._needs_outbox_repush():
                    # Our rows fell out of the named lineage (a peer clobbered the
                    # pointer); re-push a superset so they're never single-copy (§3.3a).
                    self.push(force=True)
            except Exception:
                # Never let a transient Drive/IO error kill the daemon flusher —
                # it must keep trying on the next tick (push() retries on failure).
                logger.exception("snapshot flush tick failed; will retry next interval")
                self._record_sync_failure()

    def _record_sync_failure(self) -> None:
        """Count one sync failure and escalate to error level once a run of
        failures (>=3 consecutive) suggests the flusher is wedged rather than
        hitting a transient blip. Reset by a successful push (see push())."""
        self._sync_error_count += 1
        if self._sync_error_count >= 3:
            logger.error(
                "snapshot sync failing: %d consecutive failure(s); local changes "
                "may not be propagating to Drive",
                self._sync_error_count,
            )

    def instance_health(self) -> dict:
        """In-memory health of THIS sync instance, for memcp_status (P0).

        Returns the consecutive-failure count, whether durable local state is
        awaiting a push, seconds since the last successful push (None if never),
        and whether the background flusher thread is alive.
        """
        last_push_age = (
            round(time.monotonic() - self._last_push, 3) if self._last_push else None
        )
        return {
            "sync_error_count": self._sync_error_count,
            "durable_dirty": self._durable_dirty,
            "seconds_since_last_push": last_push_age,
            "flusher_alive": self._flusher.is_alive() if self._flusher is not None else False,
            "pull_pending": self.pull_pending,
        }

    def _published_snapshot(self) -> Path | None:
        """Path to the currently-published snapshot — the v2 pointer's blob when
        a pointer is live, else the v1 bare file — or None if none is present."""
        if self.snapshot_ptr.exists():
            blob_name = self._read_json(self.snapshot_ptr).get("blob")
            if not blob_name:
                return None
            blob = self.snapshot_dir / blob_name
            return blob if blob.exists() else None
        return self.snapshot_db if self.snapshot_db.exists() else None

    @staticmethod
    def _active_node_count(path: Path) -> int | None:
        """Count of active (non-archived) nodes in a DB/snapshot, or None if the
        DB has no ``nodes`` table or can't be opened."""
        try:
            conn = sqlite3.connect(str(path))
        except sqlite3.Error:
            return None
        try:
            has_nodes = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
            ).fetchone()
            if not has_nodes:
                return None
            cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
            if "archived_at" in cols:
                return conn.execute(
                    "SELECT COUNT(*) FROM nodes WHERE archived_at IS NULL"
                ).fetchone()[0]
            return conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    def convergence_audit(self) -> dict:
        """Compare the local active corpus against the currently-published
        snapshot, so a silent no-merge becomes a visible, counted condition (P0).

        Reports the local vs snapshot active row counts, their ``delta``, whether
        the durable projections agree (``converged``), and the publishing host +
        snapshot age. On-demand and purely disk-based (needs no prior pull), so it
        is correct even from a fresh instance — e.g. the one memcp_status spins
        up. Returns ``{}`` when there is no local DB or no published snapshot to
        compare against.

        Per-host row counts are intentionally NOT persisted here: that ride on the
        merged ledger is gated on the merge-safe ledger write (Item 4a).
        """
        if not self.local_db.exists():
            return {}
        snap = self._published_snapshot()
        if snap is None:
            return {}
        local_rows = self._active_node_count(self.local_db)
        snap_rows = self._active_node_count(snap)
        if local_rows is None or snap_rows is None:
            return {}

        meta = (
            self._read_json(self.snapshot_ptr)
            if self.snapshot_ptr.exists()
            else self._read_json(self.snapshot_meta)
        )
        written_at = meta.get("written_at")
        age = (
            round(time.time() - written_at, 3)
            if isinstance(written_at, (int, float))
            else None
        )
        remote_hash = meta.get("content_hash")
        local_hash = self._projection_hash(self.local_db)
        delta = snap_rows - local_rows
        converged = delta == 0 and (remote_hash is None or local_hash == remote_hash)
        return {
            "local_rows": local_rows,
            "snapshot_rows": snap_rows,
            "delta": delta,
            "converged": converged,
            "snapshot_host": meta.get("host"),
            "snapshot_age_seconds": age,
        }

    def stop(self) -> None:
        self._stop.set()
        # Join any in-flight flusher tick BEFORE the final flush so no DB access
        # outlives stop()/close(). Without this, a tick already past the _stop
        # check (holding _tick_lock) could still touch the DB after the owning
        # NodeStore.close() closed the connection — the stop()-races-_tick_lock
        # window (P6 test 14). The loop exits promptly once _stop is set.
        flusher = self._flusher
        if flusher is not None and flusher.is_alive():
            flusher.join(timeout=10.0)
        # Final flush on shutdown: durable changes, or an un-echoed local row that
        # a peer clobbered out of the lineage (§3.3a outbox).
        if self._durable_dirty or self._needs_outbox_repush():
            self.push(force=True)

    def sync_now(self) -> dict:
        """Force an immediate pull + push, serialized against the flusher tick.

        Uses ``_tick_lock`` so a concurrent background flush and a manual sync
        never overlap within this instance (lock order: ``_tick_lock`` → push's
        internal ``_guard`` — consistent with ``_flush_loop``).

        Returns a small dict::

            {
                "pulled": bool,   # True if pull_if_newer() saw a newer snapshot
                "pushed": bool,   # True if push(force=True) published a snapshot
                "generation": int # local known generation after the sync
            }
        """
        with self._tick_lock:
            pulled = self.pull_if_newer()
            pushed = self.push(force=True)
            generation = self._local_known_generation()
        return {"pulled": pulled, "pushed": pushed, "generation": generation}

    # ── sqlite helpers ────────────────────────────────────────────

    @staticmethod
    def _backup_db(src: Path, dst: Path) -> None:
        """Consistent copy of a possibly-live SQLite DB via the backup API."""
        dst.unlink(missing_ok=True)
        src_conn = sqlite3.connect(str(src))
        try:
            dst_conn = sqlite3.connect(str(dst))
            try:
                src_conn.backup(dst_conn)
            finally:
                dst_conn.close()
        finally:
            src_conn.close()

    @staticmethod
    def _projection_hash(path: Path) -> str | None:
        """Hash the durable projection of the DB: ``nodes`` minus derived counters,
        plus ``tombstones`` if present. Returns None for a DB with no ``nodes``
        table (a non-MemCP DB never short-circuits). Never hashes
        ``edges``/``entity_index``/``index_meta`` — those are regenerated every
        session and would defeat quiescence. See spec §3.3.
        """
        try:
            conn = sqlite3.connect(str(path))
        except sqlite3.Error:
            return None
        try:
            has_nodes = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='nodes'"
            ).fetchone()
            if not has_nodes:
                return None

            cols = [r[1] for r in conn.execute("PRAGMA table_info(nodes)").fetchall()]
            durable_cols = [c for c in cols if c not in _DERIVED_NODE_COLS]
            col_list = ", ".join(durable_cols)
            h = hashlib.sha256()
            for row in conn.execute(
                f"SELECT {col_list} FROM nodes ORDER BY id"  # noqa: S608 — cols from schema
            ):
                h.update(repr(tuple(row)).encode("utf-8"))
                h.update(b"\x00")

            has_tomb = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='tombstones'"
            ).fetchone()
            if has_tomb:
                h.update(b"|tombstones|")
                for row in conn.execute(
                    "SELECT id, deleted_at, resurrected_at FROM tombstones ORDER BY id"
                ):
                    h.update(repr(tuple(row)).encode("utf-8"))
                    h.update(b"\x00")
            return h.hexdigest()
        except sqlite3.Error:
            return None
        finally:
            conn.close()

    @staticmethod
    def _is_valid_sqlite(path: Path) -> bool:
        try:
            conn = sqlite3.connect(str(path))
            try:
                return conn.execute("PRAGMA quick_check").fetchone()[0] == "ok"
            finally:
                conn.close()
        except sqlite3.Error:
            return False
