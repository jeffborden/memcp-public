"""Per-segment sync telemetry (stall attribution).

The 2026-07-15 sync-lock stall investigation found multi-minute MCP write
stalls whose cost was invisible: push telemetry's ``dur_ms`` spanned the whole
function, so catch-up, backup, hash, publish, and GC were indistinguishable —
and several paths (quiescent skip, deferred catch-up, orphan sweep) emitted
nothing at all while holding or feeding the write flock.

These tests pin the segment fields those paths now emit. They assert presence,
non-negativity, and rough additivity (segments never exceed the enclosing
``dur_ms`` by more than scheduling noise) — NOT absolute timings, which are
machine-dependent.

Handoff: chief_of_staff/docs/handoffs/2026-07-15-memcp-sync-lock-stall-handoff.md
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memcp.core.snapshot_sync import _SNAPSHOT_PTR, SnapshotSync
from memcp.core.write_lock import WriteLock

# Generous slack for scheduling noise between adjacent monotonic() reads.
_SLACK_MS = 250.0


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))


@pytest.fixture()
def tele_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "tele"
    monkeypatch.setenv("MEMCP_TELEMETRY_DIR", str(d))
    return d


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


def _make_db(path: Path, rows: int = 3, prefix: str = "n") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT OR IGNORE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            [(f"{prefix}{i}", f"content {i}", now) for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def _v2(db: Path, drive: Path) -> SnapshotSync:
    return SnapshotSync(db, drive, WriteLock(db, enabled=False), min_interval=0.0, immutable=True)


def _events(tele_dir: Path, name: str) -> list[dict]:
    out: list[dict] = []
    for f in sorted(tele_dir.glob("events-*.jsonl")):
        for ln in f.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            rec = json.loads(ln)
            if rec.get("kind") == "sync" and rec.get("name") == name:
                out.append(rec)
    return out


def _assert_segments(rec: dict, keys: list[str]) -> None:
    for k in keys:
        assert k in rec, f"missing segment field {k!r} in {sorted(rec)}"
        assert isinstance(rec[k], (int, float)), f"{k} not numeric: {rec[k]!r}"
        assert rec[k] >= 0, f"{k} negative: {rec[k]}"
    assert sum(rec[k] for k in keys) <= rec["dur_ms"] + _SLACK_MS, (
        "segments exceed enclosing dur_ms — a segment is being double-counted"
    )


# ── push: happy-path v2 publish ─────────────────────────────────────


def test_push_event_carries_all_segments(tele_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    _make_db(db)
    s = _v2(db, tmp_path / "drive")
    s.mark_durable_dirty()

    assert s.push(force=True) is True

    pushes = _events(tele_dir, "push")
    assert len(pushes) == 1
    _assert_segments(
        pushes[0], ["catchup_ms", "lock_wait_ms", "backup_ms", "hash_ms", "publish_ms"]
    )
    # v2 publish split (stashed by _publish_v2) rides on the same event.
    _assert_segments(pushes[0], ["stamp_ms", "copy_ms", "pointer_ms", "gc_ms"])


def test_v1_push_event_carries_publish_split(tele_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    _make_db(db)
    s = SnapshotSync(db, tmp_path / "drive", WriteLock(db, enabled=False), min_interval=0.0)
    s.mark_durable_dirty()

    assert s.push(force=True) is True

    pushes = _events(tele_dir, "push")
    assert len(pushes) == 1
    _assert_segments(pushes[0], ["lock_wait_ms", "backup_ms", "hash_ms", "publish_ms"])
    _assert_segments(pushes[0], ["copy_ms", "pointer_ms"])
    assert "stamp_ms" not in pushes[0]  # v2-only segment must not leak into v1


# ── push: quiescent skip (previously invisible lock hold) ───────────


def test_quiescent_skip_emits_push_quiescent(tele_dir: Path, tmp_path: Path) -> None:
    db = tmp_path / "a" / "graph.db"
    _make_db(db)
    s = _v2(db, tmp_path / "drive")
    s.mark_durable_dirty()
    assert s.push(force=True) is True

    # Same durable content, dirtied again → quiescence short-circuit.
    s.mark_durable_dirty()
    assert s.push(force=True) is False

    quiescent = _events(tele_dir, "push_quiescent")
    assert len(quiescent) == 1
    _assert_segments(quiescent[0], ["catchup_ms", "lock_wait_ms", "backup_ms", "hash_ms"])
    assert len(_events(tele_dir, "push")) == 1  # the skip must NOT mint a push event


# ── catch-up: fold and defer ────────────────────────────────────────


def _publish_peer(tmp_path: Path, drive: Path, rows: int, prefix: str) -> None:
    peer_db = tmp_path / "peer" / "graph.db"
    _make_db(peer_db, rows=rows, prefix=prefix)
    peer = _v2(peer_db, drive)
    peer.mark_durable_dirty()
    assert peer.push(force=True) is True


def test_catchup_fold_emits_validate_and_merge_split(tele_dir: Path, tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    _publish_peer(tmp_path, drive, rows=5, prefix="peer")

    db = tmp_path / "b" / "graph.db"
    _make_db(db, rows=2, prefix="local")
    s = _v2(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is True

    catchups = _events(tele_dir, "catchup")
    assert len(catchups) == 1
    rec = catchups[0]
    assert rec["deferred"] is False
    _assert_segments(rec, ["validate_ms", "merge_ms"])
    assert rec["validate_ms"] > 0  # the peer blob was actually read/verified

    # The fold's merge event carries its own inside-the-flock split.
    merges = _events(tele_dir, "merge")
    assert len(merges) == 1
    _assert_segments(merges[0], ["lock_wait_ms", "backup_ms", "union_ms"])
    # validate cost is attributed on the merge event too (stash handoff).
    assert merges[0]["validate_ms"] > 0

    # The push that folded reports its catch-up cost.
    my_push = _events(tele_dir, "push")[-1]
    assert my_push["catchup_ms"] >= rec["dur_ms"] - _SLACK_MS


def test_catchup_defer_emits_deferred_event(tele_dir: Path, tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    _publish_peer(tmp_path, drive, rows=5, prefix="peer")
    # Tear the blob away: pointer-ahead-of-blob → catch-up must defer.
    ptr = json.loads((drive / _SNAPSHOT_PTR).read_text())
    (drive / ptr["blob"]).unlink()

    db = tmp_path / "b" / "graph.db"
    _make_db(db, rows=2, prefix="local")
    s = _v2(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is False  # deferred, nothing published

    catchups = _events(tele_dir, "catchup")
    assert len(catchups) == 1
    assert catchups[0]["deferred"] is True
    # Only the peer's original publish emitted a push event — the deferred
    # push must not mint one.
    assert len(_events(tele_dir, "push")) == 1


# ── orphan sweep ────────────────────────────────────────────────────


def test_sweep_fold_emits_check_and_union_split(tele_dir: Path, tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    # Peer B wins the pointer at gen 1. Peer A publishes gen 1 into a SIDE dir
    # and its blob is copied in — a same-gen collision orphan with content the
    # pointer blob lacks, exactly what the sweep exists to fold (§3.3a).
    _publish_peer(tmp_path, drive, rows=3, prefix="b")
    side = tmp_path / "side-drive"
    peer_a_db = tmp_path / "peera" / "graph.db"
    _make_db(peer_a_db, rows=3, prefix="a")
    peer_a = _v2(peer_a_db, side)
    peer_a.mark_durable_dirty()
    assert peer_a.push(force=True) is True
    orphan = next(p for p in side.glob("graph.snapshot.*.db"))
    (drive / orphan.name).write_bytes(orphan.read_bytes())

    db = tmp_path / "c" / "graph.db"
    _make_db(db, rows=1, prefix="local")
    s = _v2(db, drive)
    assert s.pull_if_newer() is True  # pointer pull + orphan sweep

    sweeps = _events(tele_dir, "sweep")
    assert len(sweeps) == 1
    rec = sweeps[0]
    assert rec["folded"] >= 1
    assert rec["checked"] >= rec["folded"]
    _assert_segments(rec, ["check_ms", "lock_wait_ms", "union_ms"])


# ── deferred pull cost visibility ───────────────────────────────────


def test_pull_defer_on_torn_blob_emits_validate_cost(tele_dir: Path, tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    _publish_peer(tmp_path, drive, rows=5, prefix="peer")
    # Corrupt the blob in place: still present, fails integrity → the reader
    # pays a full validation read and then defers — that cost must be visible
    # because it repeats every tick until the blob heals.
    ptr = json.loads((drive / _SNAPSHOT_PTR).read_text())
    (drive / ptr["blob"]).write_bytes(b"not a sqlite file" * 1024)

    db = tmp_path / "b" / "graph.db"
    _make_db(db, rows=2, prefix="local")
    s = _v2(db, drive)
    assert s.pull_if_newer() is False

    deferred = _events(tele_dir, "pull_deferred")
    assert len(deferred) == 1
    assert deferred[0]["validate_ms"] > 0
    assert deferred[0]["gen"] == ptr["generation"]


def test_pull_defer_on_missing_blob_stays_silent(tele_dir: Path, tmp_path: Path) -> None:
    drive = tmp_path / "drive"
    _publish_peer(tmp_path, drive, rows=5, prefix="peer")
    ptr = json.loads((drive / _SNAPSHOT_PTR).read_text())
    (drive / ptr["blob"]).unlink()  # pointer-ahead-of-blob: the cheap, common case

    db = tmp_path / "b" / "graph.db"
    _make_db(db, rows=2, prefix="local")
    s = _v2(db, drive)
    assert s.pull_if_newer() is False

    assert _events(tele_dir, "pull_deferred") == []
