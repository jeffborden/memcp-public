"""Simulate the desktop/laptop flow: Machine A writes; Machine B rebuilds.

Both "machines" share the same SQLite DB file (simulating GDrive sync) but
have separate cache directories (simulating per-machine local embedding caches).
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest


def test_machine_b_detects_stale_index_after_machine_a_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared_data_dir = tmp_path / "shared"
    machine_b_data_dir = tmp_path / "machine_b"

    # ── Machine A: seed data, build indexes ──
    monkeypatch.setenv("MEMCP_DATA_DIR", str(shared_data_dir))
    from memcp import config as cfg_mod

    cfg_mod._config = None

    from memcp.core import memory, reindex
    from memcp.core.graph import GraphMemory
    from memcp.core.revision import get_revision

    # Pre-create graph DB so memory.remember uses the graph backend
    graph_a = GraphMemory()
    graph_a._get_conn()

    memory.remember(
        "Machine A insight 1 about GraphMemory",
        category="fact",
        importance="low",
    )
    reindex.rebuild_all(mode="full", force=True)

    graph_a = GraphMemory()
    revision_after_first_build = get_revision(graph_a._get_conn())
    graph_a.close()

    # ── Machine A writes more after index was built ──
    memory.remember(
        "Machine A insight 2 about NodeStore",
        category="fact",
        importance="low",
    )
    graph_a = GraphMemory()
    revision_after_new_write = get_revision(graph_a._get_conn())
    graph_a.close()
    assert revision_after_new_write == revision_after_first_build + 1

    # ── "Sync" DB to Machine B (copy SQLite file) but NOT cache dir ──
    machine_b_data_dir.mkdir()
    shutil.copy(shared_data_dir / "graph.db", machine_b_data_dir / "graph.db")
    # Machine B has no cache dir — fresh-machine simulation
    assert not (machine_b_data_dir / "cache").exists()

    # ── Machine B: new session → staleness detected → rebuild ──
    monkeypatch.setenv("MEMCP_DATA_DIR", str(machine_b_data_dir))
    cfg_mod._config = None

    # Reset embedding provider cache too (simulating fresh session start)
    try:
        from memcp.core.embeddings import reset_provider

        reset_provider()
    except ImportError:
        pass

    result = reindex.rebuild_all(mode="incremental", force=False)
    names_built = {r["index"] for r in result["results"] if not r["skipped"]}

    # The shared DB carried Machine A's index_meta showing built_against_revision
    # = revision_after_first_build. The current revision on Machine B is
    # revision_after_new_write (one higher). So edges and entities are stale
    # and must rebuild.
    assert "edges" in names_built
    assert "entities" in names_built
    # Embeddings may or may not rebuild depending on whether a provider is installed
    # in the test env; either outcome is acceptable.


def test_machine_b_with_matching_revision_skips(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Machine B opens a session after Machine A wrote AND rebuilt, Machine B
    should see up-to-date shared indexes and skip the edges/entities rebuild."""
    shared_data_dir = tmp_path / "shared"
    machine_b_data_dir = tmp_path / "machine_b"

    monkeypatch.setenv("MEMCP_DATA_DIR", str(shared_data_dir))
    from memcp import config as cfg_mod

    cfg_mod._config = None

    from memcp.core import memory, reindex
    from memcp.core.graph import GraphMemory

    graph_a = GraphMemory()
    graph_a._get_conn()
    memory.remember(
        "Shared insight about MemCP",
        category="fact",
        importance="low",
    )
    reindex.rebuild_all(mode="full", force=True)
    graph_a.close()

    # Copy DB at this point — both machines at same revision
    machine_b_data_dir.mkdir()
    shutil.copy(shared_data_dir / "graph.db", machine_b_data_dir / "graph.db")

    monkeypatch.setenv("MEMCP_DATA_DIR", str(machine_b_data_dir))
    cfg_mod._config = None

    try:
        from memcp.core.embeddings import reset_provider

        reset_provider()
    except ImportError:
        pass

    result = reindex.rebuild_all(mode="incremental", force=False)
    # edges + entities should both be skipped (up to date)
    skipped_names = {r["index"] for r in result["results"] if r["skipped"]}
    assert "edges" in skipped_names
    assert "entities" in skipped_names
