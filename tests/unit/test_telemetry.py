"""Tests for local, metadata-only event telemetry (src/memcp/core/telemetry.py)
and its wiring into the MCP tool layer + sync engine.

Hermetic: the autouse conftest fixture strips all ``MEMCP_*`` vars; each test
points ``MEMCP_TELEMETRY_DIR`` at its own ``tmp_path``.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from memcp.core import telemetry
from memcp.core.node_store import _SCHEMA
from memcp.core.snapshot_sync import SnapshotSync
from memcp.core.write_lock import WriteLock

# The metadata fields telemetry is ALLOWED to record. The whole point of the
# feature is that content/query/tag *values* never appear — this set is the
# guardrail the leak test enforces.
_ALLOWED_KEYS = {
    "ts",
    "session",
    "project",
    "kind",
    "name",  # envelope
    "dur_ms",
    "out_bytes",
    "ok",  # tool fields
    "bytes",
    "gen",
    "immutable",  # sync push
    "changed",
    "adopted",
    "rows_inserted",
    "rows_deleted",  # sync merge
    "reclaimed",
    "refused",
    "floor",
    "max_blobs",  # sync gc
}

_FORBIDDEN_KEYS = {"content", "query", "tags", "args", "arguments", "summary", "result"}


def _read_lines(dir_: Path) -> list[dict]:
    lines: list[dict] = []
    for f in sorted(Path(dir_).glob("events-*.jsonl")):
        for ln in f.read_text(encoding="utf-8").splitlines():
            if ln.strip():
                lines.append(json.loads(ln))
    return lines


@pytest.fixture()
def tele_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "tele"
    monkeypatch.setenv("MEMCP_TELEMETRY_DIR", str(d))
    return d


# ── emit() basics ────────────────────────────────────────────────────


def test_emit_writes_one_parseable_line(tele_dir: Path) -> None:
    telemetry.emit("tool", "memcp_recall", dur_ms=1.2, out_bytes=42, ok=True)
    lines = _read_lines(tele_dir)
    assert len(lines) == 1
    rec = lines[0]
    assert rec["kind"] == "tool"
    assert rec["name"] == "memcp_recall"
    assert rec["dur_ms"] == 1.2
    assert rec["out_bytes"] == 42
    assert rec["ok"] is True
    # envelope present
    assert "ts" in rec
    datetime.fromisoformat(rec["ts"])  # parseable timestamp


def test_emit_is_metadata_only(tele_dir: Path) -> None:
    # Even if a caller mistakenly passed content-ish kwargs, the schema we *emit*
    # is bounded; but the real guarantee is that the wiring only passes metadata.
    telemetry.emit("tool", "memcp_remember", dur_ms=3.0, out_bytes=10, ok=True)
    telemetry.emit("sync", "push", dur_ms=5.0, bytes=999, gen=7, immutable=True)
    for rec in _read_lines(tele_dir):
        assert set(rec).issubset(_ALLOWED_KEYS), f"unexpected keys: {set(rec) - _ALLOWED_KEYS}"
        assert not (set(rec) & _FORBIDDEN_KEYS)


def test_disabled_writes_nothing(tele_dir: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_TELEMETRY", "false")
    telemetry.emit("tool", "memcp_ping", dur_ms=1.0, out_bytes=1, ok=True)
    assert not list(tele_dir.glob("events-*.jsonl"))
    assert _read_lines(tele_dir) == []


@pytest.mark.parametrize("val", ["0", "no", "off", "FALSE", "Off"])
def test_disable_values(tele_dir: Path, monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("MEMCP_TELEMETRY", val)
    assert telemetry.is_enabled() is False
    telemetry.emit("tool", "x", ok=True)
    assert _read_lines(tele_dir) == []


def test_fail_open_unwritable_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Point the telemetry dir *inside a regular file* so mkdir raises
    # NotADirectoryError. emit must swallow it and never raise.
    blocker = tmp_path / "not-a-dir"
    blocker.write_text("i am a file")
    monkeypatch.setenv("MEMCP_TELEMETRY_DIR", str(blocker / "sub"))
    # Must not raise.
    telemetry.emit("tool", "memcp_recall", dur_ms=1.0, out_bytes=1, ok=True)


def test_emit_tool_helper(tele_dir: Path) -> None:
    telemetry.emit_tool("memcp_search", dur_ms=2.3456, out_bytes=100, ok=False)
    rec = _read_lines(tele_dir)[0]
    assert rec["name"] == "memcp_search"
    assert rec["dur_ms"] == 2.346  # rounded to 3 dp
    assert rec["ok"] is False


# ── daily rotation ───────────────────────────────────────────────────


def test_daily_filename_rotation() -> None:
    d = Path("/tmp/whatever")
    p1 = telemetry._daily_path(d, datetime(2026, 6, 2, 23, 0, tzinfo=timezone.utc))
    p2 = telemetry._daily_path(d, datetime(2026, 6, 3, 0, 5, tzinfo=timezone.utc))
    assert p1.name == "events-2026-06-02.jsonl"
    assert p2.name == "events-2026-06-03.jsonl"
    assert p1 != p2


# ── default dir resolution + Drive guard ─────────────────────────────


def test_default_dir_explicit_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMCP_TELEMETRY_DIR", "/some/where/tele")
    assert telemetry.default_telemetry_dir() == Path("/some/where/tele")


def test_default_dir_tracks_local_data_dir(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MEMCP_TELEMETRY_DIR", raising=False)
    monkeypatch.setenv("MEMCP_DATA_DIR", "/Users/alice/.memcp-local")
    assert telemetry.default_telemetry_dir() == Path("/Users/alice/.memcp-local/telemetry")


def test_default_dir_drive_guard_via_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A nominally-"local" data dir that is actually a symlink onto a cloud mount
    # must still be caught — the guard resolves the real path, not just the literal.
    drive = tmp_path / "CloudStorage" / "My Drive" / "memcp"
    drive.mkdir(parents=True)
    link = tmp_path / "looks-local"
    link.symlink_to(drive)
    monkeypatch.delenv("MEMCP_TELEMETRY_DIR", raising=False)
    monkeypatch.setenv("MEMCP_DATA_DIR", str(link))
    resolved = telemetry.default_telemetry_dir()
    assert resolved == Path("~/.memcp-local/telemetry").expanduser()


def test_default_dir_drive_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    # The footgun: a stale shell points MEMCP_DATA_DIR at the Drive mount.
    # Telemetry must NOT land there — it falls back to a guaranteed-local dir.
    monkeypatch.delenv("MEMCP_TELEMETRY_DIR", raising=False)
    monkeypatch.setenv(
        "MEMCP_DATA_DIR",
        "/Users/alice/Library/CloudStorage/GoogleDrive-alice@example.com/My Drive/memcp",
    )
    resolved = telemetry.default_telemetry_dir()
    assert "CloudStorage" not in str(resolved)
    assert "My Drive" not in str(resolved)
    assert resolved == Path("~/.memcp-local/telemetry").expanduser()


# ── @_traced tool wrapper ────────────────────────────────────────────


def test_traced_sync_records_line(tele_dir: Path) -> None:
    from memcp.server import _traced

    @_traced
    def dummy(x: int) -> str:
        return json.dumps({"value": x})

    out = dummy(5)
    assert out == '{"value": 5}'
    rec = _read_lines(tele_dir)[0]
    assert rec["kind"] == "tool"
    assert rec["name"] == "dummy"
    assert rec["out_bytes"] == len(out)
    assert rec["ok"] is True


def test_traced_out_bytes_is_utf8_byte_length(tele_dir: Path) -> None:
    from memcp.server import _traced

    @_traced
    def emoji() -> str:
        return "café 🌍"  # 6 code points, more UTF-8 bytes

    out = emoji()
    rec = _read_lines(tele_dir)[0]
    assert rec["out_bytes"] == len(out.encode("utf-8"))
    assert rec["out_bytes"] > len(out)  # bytes exceed code-point count for multibyte


def test_traced_async_records_line(tele_dir: Path) -> None:
    from memcp.server import _traced

    @_traced
    async def adummy() -> str:
        return "abcd"

    out = asyncio.run(adummy())
    assert out == "abcd"
    rec = _read_lines(tele_dir)[0]
    assert rec["name"] == "adummy"
    assert rec["out_bytes"] == 4
    assert rec["ok"] is True


def test_traced_records_ok_false_on_raise_and_reraises(tele_dir: Path) -> None:
    from memcp.server import _traced

    @_traced
    def boom() -> str:
        raise ValueError("kaboom")

    with pytest.raises(ValueError, match="kaboom"):
        boom()
    rec = _read_lines(tele_dir)[0]
    assert rec["name"] == "boom"
    assert rec["ok"] is False


def test_traced_preserves_signature() -> None:
    import inspect

    from memcp.server import _traced

    def handler(content: str, importance: str = "medium") -> str:
        return "{}"

    wrapped = _traced(handler)
    sig = inspect.signature(wrapped)
    assert list(sig.parameters) == ["content", "importance"]


# ── memcp_status surfaces the telemetry path ─────────────────────────


def test_status_surfaces_telemetry_json_backend(tele_dir: Path) -> None:
    from memcp.core import memory

    # No graph.db yet → JSON backend.
    st = memory.memory_status()
    assert st["backend"] == "json"
    assert "telemetry" in st
    assert st["telemetry"]["enabled"] is True
    assert st["telemetry"]["dir"] == str(tele_dir)


def test_status_surfaces_telemetry_graph_backend(
    tele_dir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from memcp.core import memory

    # A configured snapshot dir forces the graph backend (_use_graph), exercising
    # the _status_graph code path.
    monkeypatch.setenv("MEMCP_SNAPSHOT_DIR", str(tmp_path / "snap"))
    memory.remember("status telemetry probe", project="proj", tags="kind:op")
    st = memory.memory_status(project="proj")
    assert st["backend"] == "graph"
    assert "telemetry" in st
    assert st["telemetry"]["enabled"] is True
    assert st["telemetry"]["dir"] == str(tele_dir)


# ── sync-event wiring: a real push emits a "sync"/"push" line ─────────


def _seed_min_db(path: Path, rows: int = 3) -> None:
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        now = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            "INSERT OR IGNORE INTO nodes (id, content, created_at) VALUES (?, ?, ?)",
            [(f"n{i}", f"content {i}", now) for i in range(rows)],
        )
        conn.commit()
    finally:
        conn.close()


def test_push_emits_sync_line(tele_dir: Path, tmp_path: Path) -> None:
    local = tmp_path / "graph.db"
    _seed_min_db(local)
    snap = tmp_path / "snap"
    snap.mkdir()
    lock = WriteLock(local, enabled=False)
    sync = SnapshotSync(local, snap, lock, immutable=True, min_interval=0)

    assert sync.push(force=True) is True

    pushes = [r for r in _read_lines(tele_dir) if r["kind"] == "sync" and r["name"] == "push"]
    assert len(pushes) == 1
    rec = pushes[0]
    assert rec["bytes"] > 0
    assert rec["gen"] == 1
    assert "dur_ms" in rec
    assert rec["immutable"] is True
    # metadata-only guard holds for sync lines too
    assert set(rec).issubset(_ALLOWED_KEYS)


def test_merge_emits_sync_line(tele_dir: Path, tmp_path: Path) -> None:
    # Build an N-row snapshot and fold it into an empty peer → a "merge" event.
    snap_db = tmp_path / "snap.db"
    _seed_min_db(snap_db, rows=5)
    peer = tmp_path / "peer.db"
    _seed_min_db(peer, rows=0)  # schema-only empty peer
    lock = WriteLock(peer, enabled=False)
    sync = SnapshotSync(peer, tmp_path / "snapdir", lock, immutable=True, min_interval=0)

    sync._merge_or_adopt(snap_db, remote_gen=4)

    merges = [r for r in _read_lines(tele_dir) if r["kind"] == "sync" and r["name"] == "merge"]
    assert len(merges) == 1
    rec = merges[0]
    assert rec["gen"] == 4
    assert rec["rows_inserted"] == 5
    assert rec["changed"] is True
    assert set(rec).issubset(_ALLOWED_KEYS)
