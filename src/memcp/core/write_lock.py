"""Write-grained lock for the SQLite store on synced storage.

MemCP's ``graph.db`` is designed to live on a synced mount (Google Drive,
Dropbox, etc.) so memory follows the user across machines. SQLite's own
file locking is unreliable there — a network/FUSE filesystem does not give
real POSIX lock semantics, and the sync daemon copies the file
asynchronously, which can shear a b-tree page when two writers commit at
overlapping moments. That has corrupted the store in practice.

The fix is a lock *above* SQLite, held only around the write critical
section (the commit) so it is **write-grained, not session-grained** —
concurrent sessions (same machine or two machines) coexist and only their
brief commits serialize. Reads never take the lock.

Two tiers:

* **Local flock** — ``fcntl.flock`` on a lockfile kept on *local* disk
  (never the synced mount, where flock is meaningless). Serializes every
  writer process on this machine instantly and reliably. This is the
  rock-solid tier.

* **Cross-machine lease** — a ``.writer.lock`` file next to ``graph.db``
  on the synced mount, holding ``{host, pid, acquired_at, heartbeat_at}``.
  Best-effort: bounded by the sync daemon's latency, it catches the common
  "the other machine is actively writing" case without adding latency when
  uncontended. A stale lease (older than the TTL — e.g. a crashed holder)
  is reclaimed.

Design guarantees:

* Reentrant per process (nested ``with`` won't deadlock or double-claim).
* Fail-open: any error in the locking machinery logs and proceeds rather
  than losing or blocking a user's save. Corruption-prevention is
  best-effort; never drop data to enforce it.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import logging
import os
import socket
import threading
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_LEASE_FILENAME = ".writer.lock"

# Default bounded-retry policy for the fail-closed LOCAL flock tier.
_FLOCK_RETRIES = 3
_FLOCK_RETRY_DELAY = 0.2

# Process-global count of fail-closed local-lock acquisition failures, surfaced
# via memcp_status so a misconfigured/unwritable lock dir is visible instead of
# silently degrading serialization.
_local_lock_failures = 0
_local_lock_failures_guard = threading.Lock()


class WriteLockError(RuntimeError):
    """Raised when the LOCAL flock tier cannot be acquired after bounded retries.

    The local tier is fail-CLOSED: rather than write unserialized (which has
    corrupted the store on synced mounts), a write surfaces this error. Only the
    best-effort cross-machine lease tier is fail-open.
    """


def local_lock_failure_count() -> int:
    """Number of fail-closed local-lock acquisition failures this process."""
    return _local_lock_failures


def _record_local_lock_failure() -> None:
    global _local_lock_failures
    with _local_lock_failures_guard:
        _local_lock_failures += 1


def _local_lock_dir() -> Path:
    """Directory for flock files — must be on *local* disk, not the synced mount."""
    raw = os.getenv("MEMCP_LOCK_DIR", "~/.cache/memcp/locks")
    return Path(raw).expanduser()


class WriteLock:
    """Write-grained, cross-process + best-effort cross-machine lock.

    Use as a context manager around each commit::

        with write_lock:
            conn.commit()
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        enabled: bool = True,
        lease_ttl: float = 180.0,
        settle: float = 0.0,
        timeout: float = 30.0,
        poll: float = 0.25,
        flock_retries: int = _FLOCK_RETRIES,
        flock_retry_delay: float = _FLOCK_RETRY_DELAY,
    ) -> None:
        self.db_path = Path(db_path)
        self.enabled = enabled
        self.lease_ttl = lease_ttl
        self.settle = settle
        self.timeout = timeout
        self.poll = poll
        self.flock_retries = flock_retries
        self.flock_retry_delay = flock_retry_delay

        # Local flock file keyed by a stable hash of the resolved db path, so
        # every process pointing at the same db contends on the same lockfile.
        digest = hashlib.sha1(str(self.db_path.resolve()).encode()).hexdigest()[:16]
        self._flock_path = _local_lock_dir() / f"{digest}.flock"
        # Cross-machine lease lives next to the db on the synced mount.
        self._lease_path = self.db_path.parent / _LEASE_FILENAME

        self._host = socket.gethostname()
        self._pid = os.getpid()
        self._guard = threading.RLock()
        self._depth = 0
        self._fd = None  # type: ignore[var-annotated]
        self._holds_lease = False

    # ── context manager ───────────────────────────────────────────

    def __enter__(self) -> WriteLock:
        self.acquire()
        return self

    def __exit__(self, *exc: object) -> None:
        self.release()

    # ── acquire / release ─────────────────────────────────────────

    def acquire(self) -> None:
        self._guard.acquire()
        self._depth += 1
        if self._depth > 1 or not self.enabled:
            return  # reentrant or disabled — nothing further to do
        # LOCAL flock tier is fail-CLOSED: a write must not proceed unserialized,
        # so on exhausted retries we roll back the bookkeeping and RAISE.
        try:
            self._acquire_flock_with_retry()
        except BaseException:
            self._depth -= 1
            self._guard.release()
            raise
        # Cross-machine lease tier is best-effort (fail-OPEN): a synced-mount
        # hiccup must never block or fail a local save.
        try:
            self._acquire_lease()
        except Exception:
            logger.exception(
                "write-lock: cross-machine lease acquire failed; proceeding (best-effort)"
            )

    def _acquire_flock_with_retry(self) -> None:
        """Acquire the local flock with bounded retry; raise WriteLockError on
        exhaustion (fail-closed). Counts the failure for status surfacing."""
        attempt = 0
        last_exc: BaseException | None = None
        while True:
            try:
                self._acquire_flock()
                return
            except Exception as exc:
                last_exc = exc
                attempt += 1
                if attempt > self.flock_retries:
                    break
                time.sleep(self.flock_retry_delay)
        _record_local_lock_failure()
        raise WriteLockError(
            f"local write lock unavailable after {self.flock_retries} retries "
            f"({self._flock_path}): {last_exc}"
        ) from last_exc

    def release(self) -> None:
        try:
            if self._depth == 1 and self.enabled:
                self._release_lease()
                self._release_flock()
        except Exception:
            logger.exception("write-lock release failed")
        finally:
            self._depth -= 1
            self._guard.release()

    # ── local flock tier ──────────────────────────────────────────

    def _acquire_flock(self) -> None:
        self._flock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fd = open(self._flock_path, "w")  # noqa: SIM115
        fcntl.flock(self._fd.fileno(), fcntl.LOCK_EX)

    def _release_flock(self) -> None:
        if self._fd is not None:
            with contextlib.suppress(Exception):
                fcntl.flock(self._fd.fileno(), fcntl.LOCK_UN)
            self._fd.close()
            self._fd = None

    # ── cross-machine lease tier ──────────────────────────────────

    def _read_lease(self) -> dict | None:
        try:
            return json.loads(self._lease_path.read_text())
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            return None  # unreadable/garbage lease → treat as absent

    def _lease_is_ours(self, lease: dict) -> bool:
        return lease.get("host") == self._host and lease.get("pid") == self._pid

    def _lease_is_fresh(self, lease: dict) -> bool:
        hb = lease.get("heartbeat_at", 0)
        return (time.time() - float(hb)) < self.lease_ttl

    def _write_lease(self) -> None:
        now = time.time()
        payload = {
            "host": self._host,
            "pid": self._pid,
            "acquired_at": now,
            "heartbeat_at": now,
        }
        tmp = self._lease_path.with_suffix(".lock.tmp")
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, self._lease_path)  # atomic on local fs

    def _acquire_lease(self) -> None:
        # Wait out a *foreign, fresh* lease (the other machine is actively
        # writing and the sync daemon has propagated its lease). Reclaim if it
        # goes stale, or proceed past the timeout (assume a crashed holder).
        deadline = time.time() + self.timeout
        while True:
            lease = self._read_lease()
            if lease is None or self._lease_is_ours(lease) or not self._lease_is_fresh(lease):
                break
            if time.time() >= deadline:
                logger.warning(
                    "write-lock: foreign lease held by %s:%s exceeded timeout; "
                    "reclaiming (assuming crashed holder)",
                    lease.get("host"),
                    lease.get("pid"),
                )
                break
            time.sleep(self.poll)

        self._write_lease()
        self._holds_lease = True

        # Optional settle: give the sync daemon time, then re-check for a racing
        # foreign lease. Off by default (settle=0) to keep uncontended saves fast.
        if self.settle > 0:
            time.sleep(self.settle)
            lease = self._read_lease()
            if lease is not None and not self._lease_is_ours(lease) and self._lease_is_fresh(lease):
                logger.warning(
                    "write-lock: raced with %s:%s for the cross-machine lease",
                    lease.get("host"),
                    lease.get("pid"),
                )

    def _release_lease(self) -> None:
        if not self._holds_lease:
            return
        self._holds_lease = False
        lease = self._read_lease()
        # Only remove the lease if it is still ours (don't clobber a reclaimer).
        if lease is None or self._lease_is_ours(lease):
            with contextlib.suppress(OSError):
                self._lease_path.unlink()
