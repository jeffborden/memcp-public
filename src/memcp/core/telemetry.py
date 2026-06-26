"""Local, metadata-only telemetry for MCP tool calls and sync events.

Emits one JSON line per event to a daily-rotated ``events-YYYY-MM-DD.jsonl``
file so a session can be analyzed later (which tools ran, how long, how much
data moved, how the cross-machine sync behaved).

Design rules (locked — see the handoff spec):

* **Metadata only.** Names, durations, byte/row/blob counts, generation,
  project/session ids, ok/error. **Never** memory content, query text, or tag
  *values*. The wiring passes only those metadata fields; this module never
  serializes tool arguments.
* **Local, never the Drive mount.** Telemetry must not contend with the very
  sync it measures. The default dir tracks the live DB's parent
  (``MEMCP_DATA_DIR/telemetry``) — which is local at runtime — but if that
  resolves onto a cloud-sync mount (the stale-shell footgun) it falls back to
  ``~/.memcp-local/telemetry``.
* **Fail-open.** Every I/O path is wrapped; a telemetry failure must never
  raise into a tool call or the sync engine. The worst case is a missing line.
* **On by default**; ``MEMCP_TELEMETRY=false`` disables. ``MEMCP_TELEMETRY_DIR``
  overrides the location.

Self-contained on purpose: only stdlib is imported at module load (``project``
is imported lazily inside :func:`emit`) so ``config`` can import this module
without a circular import.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# Serializes concurrent appends from run_sync's thread pool. POSIX append is
# already atomic for small writes, but the lock makes ordering deterministic and
# is effectively free (held only for the duration of one short write).
_LOCK = threading.Lock()

_DISABLED_VALUES = {"0", "false", "no", "off"}

# Path fragments that mean "this is a cloud-sync mount" — telemetry must never
# land here (it would contend with the snapshot sync). Mirrors the guard in
# scripts/snapshot-archive.sh.
_CLOUD_MARKERS = ("CloudStorage", "My Drive", "Dropbox", "com~apple~CloudDocs", "OneDrive")

# Local fallback when the computed default would land on a cloud mount.
_LOCAL_FALLBACK = "~/.memcp-local/telemetry"


def is_enabled() -> bool:
    """True unless ``MEMCP_TELEMETRY`` is set to a falsey value. Default on."""
    return os.getenv("MEMCP_TELEMETRY", "true").strip().lower() not in _DISABLED_VALUES


def _is_cloud_path(path: Path) -> bool:
    s = str(path)
    return any(marker in s for marker in _CLOUD_MARKERS)


def default_telemetry_dir() -> Path:
    """Resolve the telemetry directory (does not create it).

    1. ``MEMCP_TELEMETRY_DIR`` if set (explicit override — used verbatim).
    2. else ``MEMCP_DATA_DIR/telemetry`` — tracks the live DB's parent.
    3. but if that resolves onto a cloud-sync mount, fall back to a guaranteed
       local dir so telemetry never rides Drive.
    """
    raw = os.getenv("MEMCP_TELEMETRY_DIR", "").strip()
    if raw:
        return Path(raw).expanduser()
    data_dir = Path(os.getenv("MEMCP_DATA_DIR", "~/.memcp")).expanduser()
    candidate = data_dir / "telemetry"
    # Check BOTH the literal path AND its symlink-resolved real path, so a
    # nominally-"local" data dir that is actually a symlink / APFS firmlink onto
    # a cloud mount cannot evade the guard. os.path.realpath is best-effort and
    # never raises (config.py resolves data_dir too — keep this consistent).
    resolved = Path(os.path.realpath(candidate))
    if _is_cloud_path(candidate) or _is_cloud_path(resolved):
        return Path(_LOCAL_FALLBACK).expanduser()
    return candidate


def _daily_path(dir_: Path, now: datetime) -> Path:
    return dir_ / f"events-{now.strftime('%Y-%m-%d')}.jsonl"


def emit(kind: str, name: str, **fields: object) -> None:
    """Append one metadata-only JSON line for an event. Never raises.

    Args:
        kind: event class — ``"tool"`` or ``"sync"``.
        name: tool name (e.g. ``memcp_recall``) or sync event
            (``push``/``pull``/``merge``/``gc``).
        **fields: metadata only (durations, counts, ids, ok). Callers must not
            pass content, query text, or tag values.
    """
    try:
        if not is_enabled():
            return
        now = datetime.now(timezone.utc)
        record: dict[str, object] = {"ts": now.isoformat()}
        # session/project ids — lazy + fail-open (project imports config, so a
        # top-level import here would risk a cycle).
        try:
            from memcp.core.project import get_current_project, get_current_session

            record["session"] = get_current_session()
            record["project"] = get_current_project()
        except Exception:  # pragma: no cover - defensive
            pass
        record["kind"] = kind
        record["name"] = name
        record.update(fields)

        dir_ = default_telemetry_dir()
        dir_.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, default=str)
        path = _daily_path(dir_, now)
        with _LOCK, open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:  # noqa: BLE001 - telemetry must never break a real call
        logger.debug("telemetry emit failed (ignored)", exc_info=True)


def emit_tool(name: str, *, dur_ms: float, out_bytes: int, ok: bool) -> None:
    """Record one MCP tool call (metadata only)."""
    emit("tool", name, dur_ms=round(dur_ms, 3), out_bytes=out_bytes, ok=ok)
