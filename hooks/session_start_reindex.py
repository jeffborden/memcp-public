#!/usr/bin/env python3
"""SessionStart hook: rebuild stale derived indexes before the session begins.

Failure mode: log and exit 0. Never block the session.
"""

from __future__ import annotations

import sys
import time


def main() -> int:
    try:
        from memcp.config import get_config

        cfg = get_config()
        if not cfg.reindex_on_session_start:
            return 0

        from memcp.core import reindex

        t0 = time.monotonic()
        result = reindex.rebuild_all(mode="incremental", force=False)
        duration_ms = int((time.monotonic() - t0) * 1000)

        rebuilt = [r for r in result["results"] if not r["skipped"]]
        if rebuilt:
            names = ", ".join(f"{r['index']}({r['items']})" for r in rebuilt)
            print(f"[memcp] reindexed {names} in {duration_ms}ms", file=sys.stderr)

        if duration_ms > cfg.reindex_latency_warn_ms:
            print(
                f"[memcp] warning: session-start reindex took {duration_ms}ms "
                f"(threshold {cfg.reindex_latency_warn_ms}ms) — consider "
                f"MEMCP_REINDEX_ON_SESSION_START=false and manual memcp_reindex",
                file=sys.stderr,
            )

    except Exception as e:
        print(f"[memcp] session-start reindex failed (ignored): {e}", file=sys.stderr)

    return 0


if __name__ == "__main__":
    sys.exit(main())
