"""Item 5 / P8 — orphan-sweep truth guard.

In _sweep_orphan_blobs, ``newly_folded.add(name)`` ran unconditionally — even
when ``_union_pull(blob)`` returned False — permanently blacklisting an UNFOLDED
blob in ``.sync_swept.json`` so it was never retried. The add must run only on
the success branch.

See docs/superpowers/specs/2026-06-10-hardening-implementation-SPEC.md Item 5.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from memcp.core.snapshot_sync import SnapshotSync
from memcp.core.write_lock import WriteLock

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


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))


def _make_db(path: Path, node_ids: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.executescript(_SCHEMA)
    for nid in node_ids:
        conn.execute(
            "INSERT OR REPLACE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            (nid, f"content-{nid}", "2026-01-01T00:00:00+00:00"),
        )
    conn.commit()
    conn.close()


def test_failed_union_does_not_blacklist_orphan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drive = tmp_path / "drive"
    drive.mkdir()
    db = tmp_path / "b" / "graph.db"
    _make_db(db, ["b1"])  # local DB must exist so the sweep doesn't early-return

    # A self-consistent peer orphan blob (valid sqlite + embedded hash == proj).
    orphan = drive / "graph.snapshot.7.hostB.cccc.db"
    _make_db(orphan, ["z"])
    SnapshotSync._stamp_blob_identity(orphan, 7, SnapshotSync._projection_hash(orphan))

    s = SnapshotSync(db, drive, WriteLock(db), min_interval=0.0, immutable=True)

    # Folding fails (e.g. a transient lock/IO error inside the union).
    monkeypatch.setattr(s, "_union_pull", lambda *_a, **_k: False)

    s._sweep_orphan_blobs()

    assert orphan.name not in s._read_swept_blobs(), (
        "an unfolded blob must NOT be recorded as swept — it must be retried"
    )
