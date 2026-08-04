"""GC 0-byte debris sweep — oracle tests.

TDD ORACLE — approved by Jeff 2026-07-15 ("ok, looks good, proceed",
including the 1-hour age threshold). Written RED-first per the gc-debris
handoff (docs/handoffs/2026-07-15-gc-zero-byte-debris-handoff.md).

Problem (measured, docs/eval/sync-lock-stall-measurement-2026-07-15.md):
22+ of ~41 blobs in the real sync dir are 0-byte torn-publish debris. GC's
conservative "refuse what you can't read" rule retains them forever — a
floor-refusal warning per blob per pass, and a blob count permanently over
the cap. A 0-byte blob is provably content-free, so deleting it cannot lose
data: the one safe exception to refuse-unreadable. The 1-hour age guard is
the race protection — a blob mid-sync from the other machine can transiently
surface as 0-byte, but real debris is weeks old.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memcp.core.snapshot_sync import SnapshotSync
from memcp.core.write_lock import WriteLock


@pytest.fixture(autouse=True)
def _local_lock_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.setenv("MEMCP_TELEMETRY_DIR", str(tmp_path / "tele"))


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


def _events(tmp_path: Path, name: str) -> list[dict]:
    out: list[dict] = []
    for f in sorted((tmp_path / "tele").glob("events-*.jsonl")):
        for ln in f.read_text(encoding="utf-8").splitlines():
            if not ln.strip():
                continue
            rec = json.loads(ln)
            if rec.get("kind") == "sync" and rec.get("name") == name:
                out.append(rec)
    return out


def _plant_zero_byte_blob(drive: Path, name: str, age_s: float | None = None) -> Path:
    """Create a 0-byte snapshot blob; backdate its mtime by ``age_s`` if given."""
    blob = drive / name
    blob.touch()
    if age_s is not None:
        past = time.time() - age_s
        os.utime(blob, (past, past))
    return blob


# ── old 0-byte debris is reclaimed (RED pre-fix) ─────────────────────


def test_old_zero_byte_debris_reclaimed(tmp_path: Path) -> None:
    """RED on current code: a 0-byte torn-publish blob older than the 1-hour
    debris threshold is unlinked by the next publish's GC, and a single
    gc_debris summary event records the count. Today _blob_id_sets can't read
    it, so both GC passes refuse it and it is retained forever."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db)
    s = _v2(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is True  # valid pointer blob now exists

    debris = _plant_zero_byte_blob(
        drive, "graph.snapshot.1.dead-host.local.deadbeef.db", age_s=2 * 3600
    )

    _make_db(db, rows=5)  # durable change so the next push publishes
    s.mark_durable_dirty()
    assert s.push(force=True) is True

    assert not debris.exists(), (
        "0-byte debris older than the age threshold survived the publish — "
        "the GC debris sweep did not reclaim it"
    )
    debris_events = _events(tmp_path, "gc_debris")
    assert len(debris_events) == 1, (
        f"expected exactly one gc_debris summary event, got {len(debris_events)}"
    )
    assert debris_events[0]["deleted"] == 1
    assert debris_events[0]["dur_ms"] >= 0


# ── fresh 0-byte blob is retained (mid-sync race guard) ──────────────


def test_fresh_zero_byte_blob_retained(tmp_path: Path) -> None:
    """GUARD: a 0-byte blob with a recent mtime survives the publish. A blob
    mid-sync from the other machine can transiently surface as 0-byte on the
    Drive mount — the age guard is the race protection."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db)
    s = _v2(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is True

    fresh = _plant_zero_byte_blob(drive, "graph.snapshot.1.peer-host.local.cafebabe.db")

    _make_db(db, rows=5)
    s.mark_durable_dirty()
    assert s.push(force=True) is True

    assert fresh.exists(), (
        "fresh 0-byte blob was deleted — the debris sweep must retain blobs "
        "younger than the age threshold (mid-sync race protection)"
    )
    assert _events(tmp_path, "gc_debris") == [], (
        "gc_debris event emitted although nothing was reclaimed"
    )


# ── the pointer blob is never a debris target ────────────────────────


def test_zero_byte_pointer_blob_never_deleted(tmp_path: Path) -> None:
    """GUARD: a 0-byte blob that is the pointer-named blob survives the sweep
    — extends the "pointer blob is never a GC target" invariant to the new
    sweep, even when the blob is old and 0-byte."""
    db = tmp_path / "a" / "graph.db"
    drive = tmp_path / "drive"
    _make_db(db)
    s = _v2(db, drive)
    s.mark_durable_dirty()
    assert s.push(force=True) is True  # ensures the snapshot dir exists

    ptr_name = "graph.snapshot.9.this-host.local.feedface.db"
    ptr_blob = _plant_zero_byte_blob(drive, ptr_name, age_s=2 * 3600)

    s._gc_blobs(keep_blob_name=ptr_name)

    assert ptr_blob.exists(), (
        "the pointer-named blob was deleted by the debris sweep — "
        "the pointer blob must never be a GC target"
    )
