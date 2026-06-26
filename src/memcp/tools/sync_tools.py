"""MCP tool wrapper for memcp_sync — on-demand cross-machine snapshot sync."""

from __future__ import annotations

import json


def do_sync() -> str:
    """Force an immediate pull + push snapshot sync.

    Returns a JSON string with ``status``, ``pulled``, ``pushed``, and
    ``generation`` keys on success, or ``status: noop`` when no snapshot dir
    is configured, or ``status: error`` on failure (mirrors ``do_reindex``).
    """
    try:
        from memcp.core.memory import _get_graph

        graph = _get_graph()
        node_store = graph._node_store
        # Ensure the connection (and _sync) is initialised.
        node_store._get_conn()
        sync = node_store._sync

        if sync is None:
            return json.dumps(
                {
                    "status": "noop",
                    "message": "sync disabled (no MEMCP_SNAPSHOT_DIR configured)",
                },
                indent=2,
            )

        result = sync.sync_now()
        return json.dumps({"status": "ok", **result}, indent=2, default=str)
    except Exception as e:
        return json.dumps({"status": "error", "message": f"sync failed: {e}"}, indent=2)
