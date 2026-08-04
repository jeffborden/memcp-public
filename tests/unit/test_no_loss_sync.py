"""Tests for the no-loss cross-machine sync redesign.

See docs/superpowers/specs/2026-06-01-no-loss-merge-sync-design.md.

These cover the precondition-1 fixes (§3.10): when a Drive snapshot dir is
configured, capacity-eviction (auto-prune) and hard retention-purge are
fundamentally incompatible with an additive no-loss union, so they must be
disabled. The corresponding behavior must remain intact when sync is OFF
(local-only mode keeps its capacity management).
"""

from __future__ import annotations

from pathlib import Path

import pytest

import memcp.config as config_module


def _reset_config() -> None:
    config_module._config = None


@pytest.fixture()
def synced_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Configure a snapshot dir + local lock dir so sync is 'on'."""
    snap = tmp_path / "drive-snapshot"
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(snap))
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    _reset_config()
    yield snap
    _reset_config()


@pytest.fixture()
def local_only_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMCP_SNAPSHOT_DIR", raising=False)
    _reset_config()
    yield
    _reset_config()


def _force_graph_backend() -> None:
    """Materialize graph.db so remember()/recall() take the graph path, not JSON."""
    from memcp.core.node_store import NodeStore

    store = NodeStore()
    try:
        store._get_conn()
    finally:
        store.close()


def _graph_node_count() -> int:
    from memcp.core.node_store import NodeStore

    store = NodeStore()
    try:
        return store._get_conn().execute("SELECT COUNT(*) FROM nodes").fetchone()[0]
    finally:
        store.close()


# ── §3.10: auto-prune disabled when synced ────────────────────────────


def test_auto_prune_disabled_when_synced(synced_env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """With snapshot_dir set, remembering past max_insights must NOT hard-delete."""
    monkeypatch.setenv("MEMCP_MAX_INSIGHTS", "3")
    _reset_config()

    from memcp.core.memory import remember

    _force_graph_backend()
    for i in range(5):
        remember(f"distinct insight number {i} about topic {i}", project="p")

    assert _graph_node_count() == 5, "auto-prune must not delete rows when synced (no-loss)"


def test_auto_prune_still_runs_when_not_synced(
    local_only_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local-only mode keeps capacity management — prune over max_insights."""
    monkeypatch.setenv("MEMCP_MAX_INSIGHTS", "3")
    _reset_config()

    from memcp.core.memory import remember

    _force_graph_backend()
    for i in range(5):
        remember(f"local distinct insight number {i} re subject {i}", project="p")

    assert _graph_node_count() <= 3, "local-only mode should still prune to capacity"


# ── §3.10: hard retention-purge disabled when synced ──────────────────


def _seed_old_archived_insight(monkeypatch: pytest.MonkeyPatch) -> str:
    """Write an archived insight whose archived_at is far past the purge window."""
    from memcp.config import get_config
    from memcp.core.fileutil import atomic_write_json

    cfg = get_config()
    cfg.ensure_dirs()
    insight_id = "deadbeefdeadbeef"
    archived = [
        {
            "id": insight_id,
            "content": "an old archived insight",
            "archived_at": "2000-01-01T00:00:00+00:00",
            "created_at": "1999-01-01T00:00:00+00:00",
        }
    ]
    atomic_write_json(cfg.archive_dir / "insights.json", archived)
    return insight_id


def test_retention_purge_skipped_when_synced(
    synced_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """retention_run(purge=True) must NOT permanently delete when synced."""
    from memcp.config import get_config
    from memcp.core.fileutil import locked_read_json
    from memcp.core.retention import retention_run

    insight_id = _seed_old_archived_insight(monkeypatch)

    summary = retention_run(archive=False, purge=True)

    assert summary["total_purged"] == 0, "purge must be a no-op when synced"
    cfg = get_config()
    remaining = locked_read_json(cfg.archive_dir / "insights.json") or []
    assert any(i["id"] == insight_id for i in remaining), "archived insight must survive"


def test_retention_purge_runs_when_not_synced(
    local_only_env: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Local-only mode still purges past the window."""
    from memcp.config import get_config
    from memcp.core.fileutil import locked_read_json
    from memcp.core.retention import retention_run

    insight_id = _seed_old_archived_insight(monkeypatch)

    summary = retention_run(archive=False, purge=True)

    assert summary["total_purged"] == 1
    cfg = get_config()
    remaining = locked_read_json(cfg.archive_dir / "insights.json") or []
    assert not any(i["id"] == insight_id for i in remaining)


# ── §3.3: stop read-side syncing (quiescence) ─────────────────────────


def _snapshot_generation(snap_dir: Path) -> int:
    """Read the published snapshot generation from the Drive meta file."""
    import json

    meta = snap_dir / "graph.snapshot.meta.json"
    if not meta.exists():
        return 0
    return int(json.loads(meta.read_text()).get("generation", 0))


def test_durable_remember_publishes_generation(synced_env: Path) -> None:
    """Control: a durable remember DOES publish a new snapshot generation."""
    from memcp.core.memory import remember

    _force_graph_backend()
    gen_before = _snapshot_generation(synced_env)
    remember("a durable decision about the architecture", category="decision", project="p")
    gen_after = _snapshot_generation(synced_env)

    assert gen_after > gen_before, "a durable write must publish a new generation"


def test_recall_does_not_publish_new_generation(synced_env: Path) -> None:
    """A read-only recall on a (possibly stale) machine must NOT republish.

    This is the live §0 data-loss bug: today every recall force-pushes the
    whole local DB over the snapshot on close, clobbering a peer's additions.
    """
    from memcp.core.memory import recall, remember

    _force_graph_backend()
    remember("fact: the API rate limit is 100 per minute", category="fact", project="p")
    gen_before = _snapshot_generation(synced_env)

    results = recall(query="API rate limit", project="p")
    assert results, "recall should return the remembered fact"

    gen_after = _snapshot_generation(synced_env)
    assert gen_after == gen_before, "a read-only recall must not publish a new generation"


def test_reindex_only_session_does_not_publish(synced_env: Path) -> None:
    """A session that only rebuilds derived indexes must not republish."""
    from memcp.core.memory import remember
    from memcp.core.node_store import NodeStore
    from memcp.core.revision import bump_revision, invalidate_index

    _force_graph_backend()
    remember("decision: use sqlite-vec for vectors", category="decision", project="p")
    gen_before = _snapshot_generation(synced_env)

    # Simulate a derived-index churn session: bump revision + invalidate, commit.
    store = NodeStore()
    try:
        conn = store._get_conn()
        invalidate_index(conn, "edges")
        bump_revision(conn)
        conn.commit()
    finally:
        store.close()

    gen_after = _snapshot_generation(synced_env)
    assert gen_after == gen_before, "derived-index churn must not publish a new generation"


# ── §3.3: precise content-hash projection domain ──────────────────────


def _nodes_schema_db(path: Path) -> None:
    import sqlite3

    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(
        """CREATE TABLE nodes (
            id TEXT PRIMARY KEY, content TEXT, summary TEXT, category TEXT,
            importance TEXT, effective_importance REAL, tags TEXT, entities TEXT,
            project TEXT, session TEXT, token_count INTEGER, access_count INTEGER,
            last_accessed_at TEXT, created_at TEXT, feedback_score REAL
        )"""
    )
    conn.execute(
        "INSERT INTO nodes (id, content, access_count, effective_importance, "
        "last_accessed_at, feedback_score, created_at) VALUES "
        "('n1', 'hello', 0, 0.5, NULL, 0.0, '2026-01-01')"
    )
    conn.commit()
    conn.close()


def test_projection_hash_excludes_derived_columns(tmp_path: Path) -> None:
    """Bumping access_count / last_accessed_at / effective_importance / feedback_score
    must NOT change the projection hash (those are non-syncing derived counters)."""
    import sqlite3

    from memcp.core.snapshot_sync import SnapshotSync

    db = tmp_path / "graph.db"
    _nodes_schema_db(db)
    h1 = SnapshotSync._projection_hash(db)
    assert h1 is not None

    conn = sqlite3.connect(str(db))
    conn.execute(
        "UPDATE nodes SET access_count = 99, last_accessed_at = '2026-02-02', "
        "effective_importance = 0.99, feedback_score = 1.0 WHERE id = 'n1'"
    )
    conn.commit()
    conn.close()

    h2 = SnapshotSync._projection_hash(db)
    assert h2 == h1, "derived-counter changes must not affect the projection hash"


def test_projection_hash_changes_on_durable_content(tmp_path: Path) -> None:
    """Adding a node (durable) must change the projection hash."""
    import sqlite3

    from memcp.core.snapshot_sync import SnapshotSync

    db = tmp_path / "graph.db"
    _nodes_schema_db(db)
    h1 = SnapshotSync._projection_hash(db)

    conn = sqlite3.connect(str(db))
    conn.execute(
        "INSERT INTO nodes (id, content, access_count, effective_importance, "
        "created_at) VALUES ('n2', 'world', 0, 0.5, '2026-01-02')"
    )
    conn.commit()
    conn.close()

    h2 = SnapshotSync._projection_hash(db)
    assert h2 != h1, "a new durable node must change the projection hash"


def test_projection_hash_none_without_nodes_table(tmp_path: Path) -> None:
    """A non-MemCP DB (no nodes table) yields no projection — never short-circuits."""
    import sqlite3

    from memcp.core.snapshot_sync import SnapshotSync

    db = tmp_path / "other.db"
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t (id INTEGER PRIMARY KEY)")
    conn.commit()
    conn.close()

    assert SnapshotSync._projection_hash(db) is None


# ── §3.5: archive is in-band (no hard delete / no tombstone) when synced ──


def _tombstone_exists(node_id: str) -> bool:
    from memcp.core.node_store import NodeStore

    store = NodeStore()
    try:
        return (
            store._get_conn()
            .execute("SELECT 1 FROM tombstones WHERE id = ?", (node_id,))
            .fetchone()
            is not None
        )
    finally:
        store.close()


def test_synced_archive_is_in_band(synced_env: Path) -> None:
    """Archiving when synced sets archived_at, keeps the row, emits NO tombstone,
    and hides the row from recall."""
    from memcp.core.graph import GraphMemory
    from memcp.core.memory import recall, remember
    from memcp.core.retention import archive_insight

    _force_graph_backend()
    ins = remember("an archivable insight about widgets", project="p")
    archive_insight(ins["id"])

    graph = GraphMemory()
    try:
        node = graph.get_node(ins["id"])
    finally:
        graph.close()
    assert node is not None, "archived row must stay in the synced DB"
    assert node["archived_at"] is not None, "archived_at must be set"
    assert not _tombstone_exists(ins["id"]), "in-band archive must NOT tombstone"

    results = recall(query="archivable widgets", project="p")
    assert all(r["id"] != ins["id"] for r in results), "archived row must be hidden from recall"


def test_synced_restore_clears_archived_at(synced_env: Path) -> None:
    """Restoring when synced clears archived_at; the row returns to recall."""
    from memcp.core.graph import GraphMemory
    from memcp.core.memory import recall, remember
    from memcp.core.retention import archive_insight, restore_insight

    _force_graph_backend()
    ins = remember("a restorable insight about gadgets", project="p")
    archive_insight(ins["id"])
    restore_insight(ins["id"])

    graph = GraphMemory()
    try:
        node = graph.get_node(ins["id"])
    finally:
        graph.close()
    assert node["archived_at"] is None, "restore must clear archived_at"

    results = recall(query="restorable gadgets", project="p")
    assert any(r["id"] == ins["id"] for r in results), "restored row must be recallable again"
