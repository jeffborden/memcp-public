"""Step 4 — additive union-on-pull (the core merge).

Replaces the blind os.replace in pull_if_newer with an additive union: pull
only ever ADDS rows (INSERT OR IGNORE) and applies tombstones as a deny-set;
it never deletes a local row to match the snapshot. This is the basis of the
no-loss guarantee (local-DB monotonicity, §1).

See docs/superpowers/specs/2026-06-01-no-loss-merge-sync-design.md §3.1.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memcp.core.snapshot_sync import SnapshotSync
from memcp.core.write_lock import WriteLock


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))


_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, content TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL, resurrected_at TEXT DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0');
"""


def _make_db(path: Path, nodes: list[tuple], tombstones: list[tuple] = ()) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    for nid, content in nodes:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            (nid, content, "2026-01-01T00:00:00+00:00"),
        )
    for t in tombstones:
        rid, dat = t[0], t[1]
        rat = t[2] if len(t) > 2 else None
        conn.execute(
            "INSERT OR REPLACE INTO tombstones (id, deleted_at, resurrected_at) VALUES (?, ?, ?)",
            (rid, dat, rat),
        )
    conn.commit()
    conn.close()


def _ids(path: Path) -> set[str]:
    conn = sqlite3.connect(str(path))
    try:
        return {r[0] for r in conn.execute("SELECT id FROM nodes")}
    finally:
        conn.close()


def _content(path: Path, node_id: str) -> str | None:
    conn = sqlite3.connect(str(path))
    try:
        row = conn.execute("SELECT content FROM nodes WHERE id = ?", (node_id,)).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def _sync(db: Path, drive: Path) -> SnapshotSync:
    return SnapshotSync(db, drive, WriteLock(db), min_interval=0.0)


def _publish(db: Path, drive: Path) -> None:
    """Publish db as the current snapshot (machine A)."""
    s = _sync(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is True


# ── core union semantics ──────────────────────────────────────────────


def test_union_merges_disjoint_nodes(tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "from A"), ("a2", "also A")])
    _make_db(db_b, [("b1", "from B"), ("b2", "also B")])
    _publish(db_a, drive)

    assert _sync(db_b, drive).pull_if_newer() is True
    assert _ids(db_b) == {"a1", "a2", "b1", "b2"}, "union must keep BOTH machines' rows"


def test_union_same_id_keeps_local(tmp_path: Path) -> None:
    """INSERT OR IGNORE on an existing id keeps the local copy (metadata edits
    intentionally don't propagate; both copies survive)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("x", "A-version")])
    _make_db(db_b, [("x", "B-version")])
    _publish(db_a, drive)

    _sync(db_b, drive).pull_if_newer()
    assert _content(db_b, "x") == "B-version", "local row must win on id conflict"


def test_tombstone_denyset_removes_node(tmp_path: Path) -> None:
    """A node present in the snapshot AND tombstoned resolves to deleted —
    regardless of arrival order (commutativity)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("y", "doomed")], tombstones=[("y", "2026-02-01T00:00:00+00:00")])
    _make_db(db_b, [("y", "local doomed"), ("keep", "survivor")])
    _publish(db_a, drive)

    _sync(db_b, drive).pull_if_newer()
    assert _ids(db_b) == {"keep"}, "tombstoned node must be removed by the deny-set"


def test_tombstone_resurrected_keeps_node(tmp_path: Path) -> None:
    """A restore (resurrected_at >= deleted_at) out-ranks the tombstone."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(
        db_a,
        [("z", "restored")],
        tombstones=[("z", "2026-02-01T00:00:00+00:00", "2026-03-01T00:00:00+00:00")],
    )
    _make_db(db_b, [("other", "x")])
    _publish(db_a, drive)

    _sync(db_b, drive).pull_if_newer()
    assert "z" in _ids(db_b), "a resurrected node must survive the deny-set"


def test_fresh_machine_adopts_snapshot(tmp_path: Path) -> None:
    """A machine with no local DB adopts the snapshot (bootstrap, no merge)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    _make_db(db_a, [("a1", "from A")])
    _publish(db_a, drive)

    db_b = tmp_path / "b" / "graph.db"  # does not exist yet
    assert _sync(db_b, drive).pull_if_newer() is True
    assert _ids(db_b) == {"a1"}


def test_union_is_idempotent(tmp_path: Path) -> None:
    """Merging the same snapshot twice == once (no double-apply, no loss)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "A")], tombstones=[("gone", "2026-02-01T00:00:00+00:00")])
    _make_db(db_b, [("b1", "B"), ("gone", "to be denied")])
    _publish(db_a, drive)

    sb = _sync(db_b, drive)
    sb._union_pull()
    first = _ids(db_b)
    sb._union_pull()
    second = _ids(db_b)
    assert first == second == {"a1", "b1"}, "union must be idempotent"


def test_no_loss_under_clobber_race(tmp_path: Path) -> None:
    """Both machines pull the same base then publish; after a follow-up cycle
    no insight is ever permanently lost (the §0 regression test)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_db(db_a, [("a1", "A unique")])
    _make_db(db_b, [("b1", "B unique")])

    # A publishes; B pulls+merges then publishes its superset; A pulls back.
    _publish(db_a, drive)
    sb = _sync(db_b, drive)
    sb.pull_if_newer()
    sb.mark_durable_dirty()
    sb.push(force=True)
    _sync(db_a, drive).pull_if_newer()

    assert "a1" in _ids(db_a) and "b1" in _ids(db_a)
    assert "a1" in _ids(db_b) and "b1" in _ids(db_b)


# ── regression: a pre-existing FK orphan must not abort the union (§3.1) ─


_FK_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY, content TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS entity_index (
    entity TEXT NOT NULL, node_id TEXT NOT NULL,
    PRIMARY KEY (entity, node_id),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY, deleted_at TEXT NOT NULL, resurrected_at TEXT DEFAULT NULL
);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0');
"""


def _make_fk_db(path: Path, nodes: list[tuple], orphans: list[tuple] = ()) -> None:
    """Build a db whose schema has the real FK-bearing entity_index. `orphans`
    are entity_index rows whose node_id is absent — a latent FK violation that
    foreign_keys=OFF lets us insert (matching how they arise in the wild)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_FK_SCHEMA)
    for nid, content in nodes:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            (nid, content, "2026-01-01T00:00:00+00:00"),
        )
        conn.execute(
            "INSERT OR IGNORE INTO entity_index (entity, node_id) VALUES (?, ?)",
            (f"e_{nid}", nid),
        )
    for entity, node_id in orphans:
        conn.execute(
            "INSERT OR IGNORE INTO entity_index (entity, node_id) VALUES (?, ?)",
            (entity, node_id),
        )
    conn.commit()
    conn.close()


def test_preexisting_fk_orphan_does_not_abort_union(tmp_path: Path) -> None:
    """A pre-existing, unrelated FK orphan in the LOCAL db must NOT roll back the
    union. A whole-DB foreign_key_check sees the latent orphan and discards every
    incoming row — while the generation is still recorded as merged. That is the
    gen-210 / 43-insight silent loss reproduced in miniature (§3.1)."""
    drive = tmp_path / "drive"
    db_a = tmp_path / "a" / "graph.db"
    db_b = tmp_path / "b" / "graph.db"
    _make_fk_db(db_a, [("a1", "from A")])  # clean snapshot, brings a new node
    _make_fk_db(db_b, [("b1", "from B")], orphans=[("ghost", "missing_node")])
    _publish(db_a, drive)

    assert _sync(db_b, drive).pull_if_newer() is True, "a clean incoming snapshot must merge"
    assert _ids(db_b) == {"a1", "b1"}, "pre-existing orphan must not drop the merged rows"
