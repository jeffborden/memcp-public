"""Step 7 — eager bootstrap + memory.json migrate funnel (§3.7).

A fresh machine with a snapshot configured must route the first remember() to
the GRAPH backend (not memory.json), or the row is stranded in JSON and never
propagates. And a legacy memory.json must be absorbed into the graph via the
one funnel every backend op passes (_get_conn), not a function the hot path
never hits.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import memcp.config as config_module


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    data = tmp_path / "data"
    monkeypatch.setenv("MEMCP_DATA_DIR", str(data))
    monkeypatch.delenv("MEMCP_SNAPSHOT_DIR", raising=False)
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    config_module._config = None
    yield data
    config_module._config = None


def _write_memory_json(data_dir: Path, insights: list[dict]) -> Path:
    from memcp.config import get_config

    cfg = get_config()
    cfg.ensure_dirs()
    path = cfg.memory_path
    path.write_text(json.dumps({"version": "x", "insights": insights, "metadata": {}}))
    return path


# ── _use_graph broadening ─────────────────────────────────────────────


def test_use_graph_true_when_snapshot_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Sync mode is always graph-backed — even before graph.db exists."""
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(tmp_path / "nonexistent-snap"))
    config_module._config = None
    from memcp.core.memory import _use_graph

    assert _use_graph() is True


# ── no stranding on a fresh+snapshot machine ──────────────────────────


def test_fresh_snapshot_machine_routes_remember_to_graph(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(tmp_path / "snap"))
    config_module._config = None

    from memcp.config import get_config
    from memcp.core.memory import remember
    from memcp.core.node_store import NodeStore

    ins = remember("a fresh-machine insight that must not strand", project="p")

    cfg = get_config()
    assert cfg.graph_db_path.exists(), "graph.db must be created"
    assert not cfg.memory_path.exists(), "remember must NOT strand the row in memory.json"

    store = NodeStore()
    try:
        row = store._get_conn().execute("SELECT 1 FROM nodes WHERE id = ?", (ins["id"],)).fetchone()
    finally:
        store.close()
    assert row is not None, "the row must live in the graph"


# ── memory.json migrate funnel ────────────────────────────────────────


def test_memory_json_migrated_through_get_conn(_isolated: Path) -> None:
    """Opening the store absorbs a legacy memory.json and renames it."""
    from memcp.config import get_config
    from memcp.core.node_store import NodeStore

    _write_memory_json(
        _isolated,
        [
            {
                "id": "legacy1",
                "content": "a legacy insight from memory.json",
                "created_at": "2025-01-01T00:00:00+00:00",
                "project": "p",
            }
        ],
    )

    store = NodeStore()
    try:
        conn = store._get_conn()  # funnel migrates memory.json
        row = conn.execute("SELECT content FROM nodes WHERE id = 'legacy1'").fetchone()
    finally:
        store.close()

    assert row is not None and "legacy insight" in row[0]
    cfg = get_config()
    assert not cfg.memory_path.exists(), "memory.json must be renamed after migration"


def test_memory_json_migrate_keeps_both_sets(_isolated: Path) -> None:
    """A machine with graph rows AND a memory.json keeps BOTH after migration."""
    from memcp.core.node_store import NodeStore

    # Pre-existing graph row.
    store = NodeStore()
    store._get_conn()
    store.store(
        {"id": "graphrow", "content": "already in graph", "created_at": "2026-01-01T00:00:00+00:00"}
    )
    store.close()

    _write_memory_json(
        _isolated,
        [
            {
                "id": "jsonrow",
                "content": "only in memory.json",
                "created_at": "2025-01-01T00:00:00+00:00",
            }
        ],
    )

    store = NodeStore()
    try:
        ids = {r[0] for r in store._get_conn().execute("SELECT id FROM nodes").fetchall()}
    finally:
        store.close()
    assert {"graphrow", "jsonrow"} <= ids, "both the graph row and the migrated json row survive"
