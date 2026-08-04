"""File safety utilities — atomic writes, locking, validation, hashing."""

from __future__ import annotations

import fcntl
import hashlib
import json
import re
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from memcp.core.errors import ValidationError

_SAFE_NAME_RE = re.compile(r"^[\w.\-]+$")


def safe_name(name: str) -> str:
    """Validate a name for use as a filename. Raises ValidationError if invalid."""
    if not name or not _SAFE_NAME_RE.match(name):
        raise ValidationError(
            f"Invalid name {name!r}: must match ^[\\w.\\-]+$"
            " (alphanumeric, dots, hyphens, underscores)"
        )
    if ".." in name:
        raise ValidationError(f"Invalid name {name!r}: path traversal not allowed")
    return name


def content_hash(text: str) -> str:
    """SHA-256 hash of normalized text (stripped, lowered).

    16-hex (64-bit) — used as a dedup / context identity key, NOT as a durable
    node id. For node ids use :func:`insight_id` (full width) — a truncated id
    under the merge union's INSERT OR IGNORE would silently drop a genuinely
    different insight on a 64-bit collision (§3.9).
    """
    normalized = text.strip().lower()
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def insight_id(content: str, created_at: str) -> str:
    """Full-width (256-bit / 64-hex) durable id for an insight.

    Never truncated: under the cross-machine merge union (``INSERT OR IGNORE``)
    a 64-bit prefix collision between two genuinely different insights would be
    real data loss. See spec §3.9.
    """
    return hashlib.sha256((content + created_at).encode("utf-8")).hexdigest()


def atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically with file locking.

    1. Acquire exclusive lock on a .lock file
    2. Write to a temp file in the same directory
    3. os.replace() the temp file over the target (atomic on POSIX)
    4. Release lock
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2, default=str)
                Path(tmp_path).replace(path)
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def atomic_write_text(path: Path, content: str) -> None:
    """Write text atomically with file locking."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    f.write(content)
                Path(tmp_path).replace(path)
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def locked_read_json(path: Path) -> Any:
    """Read JSON with shared lock for safe concurrent reads."""
    path = Path(path)
    if not path.exists():
        return None

    lock_path = path.with_suffix(path.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_SH)
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def locked_update_json(path: Path, mutate: Callable[[Any], Any], default: Any = None) -> Any:
    """Read-modify-write a JSON file under a SINGLE exclusive lock (TOCTOU-safe).

    Holds ``LOCK_EX`` across the whole read → mutate → atomic-write cycle, so two
    concurrent updaters can never each read the old contents and clobber the
    other's write. ``mutate`` receives the current contents (or ``default`` if
    the file is missing/empty/corrupt) and returns the new contents. Returns the
    written value.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(path.suffix + ".lock")

    with open(lock_path, "w") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            current = default
            if path.exists():
                try:
                    with open(path, encoding="utf-8") as f:
                        current = json.load(f)
                except (ValueError, OSError):
                    current = default

            new_data = mutate(current)

            # Atomic replace while still holding the lock (don't call
            # atomic_write_json — it would re-lock the same file).
            fd, tmp_path = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
            try:
                with open(fd, "w", encoding="utf-8") as f:
                    json.dump(new_data, f, ensure_ascii=False, indent=2, default=str)
                Path(tmp_path).replace(path)
            except BaseException:
                Path(tmp_path).unlink(missing_ok=True)
                raise
            return new_data
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def estimate_tokens(text: str) -> int:
    """Estimate token count using the ~4 chars per token heuristic."""
    return max(1, len(text) // 4)
