"""Step 3 — durable mutation/delete paths route through a tombstone or new id.

§3.10 (the precondition-1 fix): every code path that removes or rewrites a
durable ``nodes`` row must emit a synced tombstone (delete) or mint a new-id
insert (content change). Tombstone merge uses newest-wins per field
(MAX(deleted_at), MAX(resurrected_at)) so the node's state reflects the most
recent delete/restore across machines — see HANDOFF correction.

See docs/superpowers/specs/2026-06-01-no-loss-merge-sync-design.md §3.5, §3.10.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import memcp.config as config_module


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("MEMCP_SNAPSHOT_DIR", raising=False)
    config_module._config = None
    yield
    config_module._config = None


def _tombstone(conn: sqlite3.Connection, node_id: str) -> tuple | None:
    return conn.execute(
        "SELECT id, deleted_at, resurrected_at FROM tombstones WHERE id = ?", (node_id,)
    ).fetchone()


# ── 3a: delete_node / forget emit a tombstone ─────────────────────────


def test_delete_node_emits_tombstone() -> None:
    from memcp.core.node_store import NodeStore

    store = NodeStore()
    try:
        store.store(
            {"id": "n1", "content": "to be deleted", "created_at": "2026-01-01T00:00:00+00:00"}
        )
        assert store.delete_node("n1") is True

        conn = store._get_conn()
        assert conn.execute("SELECT 1 FROM nodes WHERE id='n1'").fetchone() is None
        tomb = _tombstone(conn, "n1")
        assert tomb is not None, "delete must emit a tombstone"
        assert tomb[1] is not None, "deleted_at must be set"
        assert tomb[2] is None, "resurrected_at must be NULL on a fresh delete"
    finally:
        store.close()


def test_delete_missing_node_no_tombstone() -> None:
    from memcp.core.node_store import NodeStore

    store = NodeStore()
    try:
        assert store.delete_node("nope") is False
        assert _tombstone(store._get_conn(), "nope") is None
    finally:
        store.close()


def test_redelete_advances_deleted_at_max() -> None:
    """A re-delete must keep the NEWEST deleted_at (so it out-ranks a prior restore)."""
    from memcp.core.node_store import NodeStore

    store = NodeStore()
    try:
        conn = store._get_conn()
        store.store({"id": "n1", "content": "x", "created_at": "2026-01-01T00:00:00+00:00"})
        store.delete_node("n1")
        # Simulate an older tombstone, then re-store + re-delete.
        conn.execute("UPDATE tombstones SET deleted_at = '2000-01-01T00:00:00+00:00' WHERE id='n1'")
        conn.commit()
        store.store({"id": "n1", "content": "x", "created_at": "2026-01-01T00:00:00+00:00"})
        store.delete_node("n1")
        deleted_at = _tombstone(conn, "n1")[1]
        assert deleted_at > "2000-01-01T00:00:00+00:00", "re-delete must advance deleted_at (MAX)"
    finally:
        store.close()


def test_forget_emits_tombstone_via_memory_api() -> None:
    from memcp.core.memory import forget, remember
    from memcp.core.node_store import NodeStore

    # Force graph backend.
    NodeStore()._get_conn()  # materialize graph.db
    ins = remember("a fact to forget", category="fact", project="p")
    assert forget(ins["id"]) is True

    store = NodeStore()
    try:
        assert _tombstone(store._get_conn(), ins["id"]) is not None
    finally:
        store.close()


# ── 3c: consolidation content-change mints a new id + tombstones all ───


def test_consolidation_content_change_mints_new_node() -> None:
    """A content-merge must mint a NEW immutable node and tombstone ALL members
    (incl. the keeper) — never raw-rewrite content under an existing id (§3.10)."""
    from memcp.core.consolidation import merge_group
    from memcp.core.graph import GraphMemory
    from memcp.core.memory import remember
    from memcp.core.node_store import NodeStore

    NodeStore()._get_conn()  # materialize graph.db
    r1 = remember("cats are mammals", project="p", tags="a")
    r2 = remember("cats are feline animals entirely", project="p", tags="b")

    result = merge_group([r1["id"], r2["id"]], merged_content="cats are feline mammals")
    assert result["status"] == "ok"
    new_id = result["kept_id"]
    assert new_id not in (r1["id"], r2["id"]), "content merge must mint a NEW id"

    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        node = graph.get_node(new_id)
        assert node is not None and node["content"] == "cats are feline mammals"
        for old in (r1["id"], r2["id"]):
            assert graph.get_node(old) is None, "old members must be removed"
            assert (
                conn.execute("SELECT 1 FROM tombstones WHERE id=?", (old,)).fetchone() is not None
            ), "every old member must be tombstoned"
    finally:
        graph.close()


# ── 3e: enforcement — no unguarded durable mutation/delete SQL (§3.10) ─


def test_no_unguarded_durable_mutation_sql() -> None:
    """Every `DELETE FROM nodes` must emit a tombstone or apply the deny-set,
    and `UPDATE nodes SET content` must not exist (content is immutable). §3.10."""
    import ast

    src_root = Path(__file__).resolve().parents[2] / "src" / "memcp"

    content_rewrites: list[str] = []
    unguarded_deletes: list[str] = []

    for py in src_root.rglob("*.py"):
        text = py.read_text()
        rel = py.relative_to(src_root)
        if "UPDATE nodes SET content" in text:
            content_rewrites.append(str(rel))
        if "DELETE FROM nodes" not in text:
            continue
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            seg = ast.get_source_segment(text, node) or ""
            if "DELETE FROM nodes" not in seg:
                continue
            # Safe iff the same function records a tombstone (delete) or applies
            # the union deny-set (which selects FROM tombstones).
            if "write_tombstone" not in seg and "tombstones" not in seg:
                unguarded_deletes.append(f"{rel}::{node.name}")

    assert not content_rewrites, (
        "content is immutable — no `UPDATE nodes SET content` allowed (§3.10); "
        f"found in: {content_rewrites}"
    )
    assert not unguarded_deletes, (
        "each `DELETE FROM nodes` must emit a tombstone or apply the deny-set "
        f"(§3.10); unguarded sites: {unguarded_deletes}"
    )


def test_consolidation_metadata_only_keeps_keeper_id() -> None:
    """A metadata-only merge (no merged_content) keeps the keeper id — no
    content rewrite, so no new node needed; non-keepers tombstoned."""
    from memcp.core.consolidation import merge_group
    from memcp.core.graph import GraphMemory
    from memcp.core.memory import remember
    from memcp.core.node_store import NodeStore

    NodeStore()._get_conn()
    r1 = remember("alpha distinct one", project="p", importance="low", tags="t1")
    r2 = remember("alpha distinct two", project="p", importance="high", tags="t3")

    result = merge_group([r1["id"], r2["id"]], keep_id=r2["id"])
    assert result["status"] == "ok"
    assert result["kept_id"] == r2["id"], "metadata-only merge keeps the keeper id"

    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        assert graph.get_node(r2["id"]) is not None
        assert graph.get_node(r1["id"]) is None
        assert (
            conn.execute("SELECT 1 FROM tombstones WHERE id=?", (r1["id"],)).fetchone() is not None
        )
    finally:
        graph.close()
