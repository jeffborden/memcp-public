"""MCP tool wrapper for memcp_reindex — JSON serialization only."""

from __future__ import annotations

import json

from memcp.core import reindex


def do_reindex(
    index: str = "all",
    mode: str = "incremental",
    force: bool = False,
) -> str:
    """Rebuild derived indexes from the node store.

    index: 'all' | 'edges' | 'entities' | 'embeddings'
    mode:  'incremental' | 'full'
    force: bypass staleness check (default false)
    """
    try:
        result = reindex.rebuild_all(index=index, mode=mode, force=force)
        return json.dumps({"status": "ok", **result}, indent=2, default=str)
    except ValueError as e:
        return json.dumps({"status": "error", "message": str(e)}, indent=2)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"reindex failed: {e}"}, indent=2)
