"""Benchmark: cross-machine snapshot-sync cost as the corpus grows.

The sync transport publishes a **whole-DB copy per push** (O(corpus)) via the
SQLite backup API to an immutable Drive-synced blob, and folds peers in with an
additive ``INSERT OR IGNORE`` union on pull. None of that was measured. This
module produces the real latency + bytes curve so we know where whole-DB sync
starts to bite and whether a delta-sync optimization is warranted.

Four things are measured, all against a *real* :class:`SnapshotSync` driving a
real local SQLite DB (immutable=True, the production format):

1. **push**       — ``push(force=True)`` latency + resulting blob bytes vs N.
2. **merge-cold** — fold an N-row snapshot into an *empty* peer (union inserts N).
3. **merge-incr** — fold an (N+K)-row snapshot into a peer that already has N
                    (union inserts K — but still *scans* the whole snapshot).
4. **gc-cap**     — ``_gc_blobs`` content-verified cap cost with one blob over
                    the cap (the per-push GC cost the cap change added).

Hermetic: the conftest strips ``MEMCP_*`` and gives each test its own
``tmp_path``; the write lock is disabled (a no-op) so nothing touches
``~/.cache`` or the real Drive mount. Synthetic rows are smaller than
production insights, so absolute bytes are a *lower bound* — the **shape**
(linear in N) is the deliverable.

Heavy sizes can be deselected in CI with ``-k "not 50K"``.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from memcp.core.node_store import _SCHEMA
from memcp.core.snapshot_sync import SnapshotSync
from memcp.core.write_lock import WriteLock

from .datasets import generate_insights

# ── Scale parameters ─────────────────────────────────────────────────
# 100k omitted by design (the backup copy makes it slow). 50K is the heavy
# case — deselect with -k "not 50K". Mirrors how test_scale.py caps its top size.
_SIZES = [1_000, 10_000, 50_000]
_SIZE_IDS = ["1K", "10K", "50K"]

# Per-row content size. Real graph.db rows are larger (they carry full insight
# text), so this is a deliberate, documented lower bound chosen to keep 50K's
# backup copy in the sub-second range while still exercising a multi-MB DB.
_CONTENT_BYTES = 600

# pytest-benchmark rounds. Push is cheap; merge/gc rebuild state in setup() each
# round (setup time is not measured), so keep round counts modest at scale.
_PUSH_ROUNDS = 5
_MERGE_ROUNDS = 5
_GC_ROUNDS = 5

# A paragraph of realistic prose used to pad synthetic rows to _CONTENT_BYTES.
_FILLER = (
    "The system uses a layered architecture with clear separation of concerns. "
    "Authentication is handled via tokens with a bounded expiry and a server-side "
    "refresh store. Database queries are optimized using connection pooling and "
    "prepared statements, and the cache layer follows a write-through pattern. "
)


# ── Results capture (durable, so overnight numbers survive) ──────────

_SYNC_RESULTS: list[dict[str, Any]] = []


def _project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _record_result(row: dict[str, Any]) -> None:
    """Append one measurement and rewrite the JSON report (cheap, idempotent)."""
    _SYNC_RESULTS.append(row)
    out_dir = _project_root() / "benchmark_output"
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "sync_scale_results.json").write_text(json.dumps(_SYNC_RESULTS, indent=2))


def _bench_ms(benchmark: Any) -> tuple[float, float]:
    """(mean_ms, min_ms) from the benchmark fixture after a run. Best-effort."""
    try:
        s = benchmark.stats.stats
        return round(s.mean * 1000, 3), round(s.min * 1000, 3)
    except Exception:  # pragma: no cover - defensive
        return -1.0, -1.0


# ── DB seeding ───────────────────────────────────────────────────────


def _pad(text: str, target: int) -> str:
    if len(text) >= target:
        return text
    out = text
    while len(out) < target:
        out += " " + _FILLER
    return out[:target]


def _seed_db(path: Path, n: int, *, content_bytes: int = _CONTENT_BYTES, seed: int = 42) -> None:
    """Create a real MemCP SQLite DB at ``path`` with ``n`` nodes.

    Uses the production ``_SCHEMA`` and the seeded ``generate_insights`` content
    (padded to ``content_bytes``). ``generate_insights`` shares one RNG across
    indices, so ``generate_insights(n)`` is a prefix of ``generate_insights(n+k)``
    — the row ids of the first ``n`` rows match, which the incremental-merge test
    relies on to make the (n+k)-row snapshot a true superset of the n-row peer.
    Padding only changes the stored ``content`` column, never the id (the id is
    hashed from the unpadded content inside ``generate_insights``).
    """
    insights = generate_insights(n, seed=seed)
    now = datetime.now(timezone.utc).isoformat()
    conn = sqlite3.connect(str(path))
    try:
        conn.executescript(_SCHEMA)
        rows = [
            (
                ins["id"],
                _pad(ins["content"], content_bytes),
                "",
                ins["category"],
                ins["importance"],
                0.5,
                json.dumps(ins["tags"]),
                json.dumps(ins["entities"]),
                ins["project"],
                ins["session"],
                ins["token_count"],
                0,
                None,
                now,
            )
            for ins in insights
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO nodes (id, content, summary, category, importance, "
            "effective_importance, tags, entities, project, session, token_count, "
            "access_count, last_accessed_at, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            rows,
        )
        conn.commit()
    finally:
        conn.close()


def _row_count(path: Path) -> int:
    conn = sqlite3.connect(str(path))
    try:
        return int(conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0])
    finally:
        conn.close()


# ── Test 1: push latency + blob bytes vs N ───────────────────────────


@pytest.mark.parametrize("n", _SIZES, ids=_SIZE_IDS)
def test_push_scale(benchmark: Any, tmp_path: Path, n: int) -> None:
    """``push(force=True)`` — the O(corpus) whole-DB publish cost + blob bytes."""
    local_db = tmp_path / "graph.db"
    _seed_db(local_db, n)
    lock = WriteLock(local_db, enabled=False)
    meta = local_db.parent / ".sync_meta.json"
    counter = {"i": 0}

    def setup() -> tuple[tuple[Any, ...], dict[str, Any]]:
        # Fresh snapshot dir + cleared last-pushed hash so every measured push
        # does the full backup+publish (no quiescence short-circuit).
        counter["i"] += 1
        snap = tmp_path / f"snap_push_{counter['i']}"
        snap.mkdir()
        meta.unlink(missing_ok=True)
        sync = SnapshotSync(local_db, snap, lock, immutable=True, min_interval=0)
        return (sync,), {}

    def target(sync: SnapshotSync) -> None:
        assert sync.push(force=True) is True

    benchmark.pedantic(target, setup=setup, rounds=_PUSH_ROUNDS, iterations=1)

    # Measure the published blob size in a clean dir (one extra real push).
    snap = tmp_path / "snap_push_bytes"
    snap.mkdir()
    meta.unlink(missing_ok=True)
    sync = SnapshotSync(local_db, snap, lock, immutable=True, min_interval=0)
    assert sync.push(force=True) is True
    blobs = list(snap.glob("graph.snapshot.*.db"))
    assert len(blobs) == 1
    blob_bytes = blobs[0].stat().st_size

    mean_ms, min_ms = _bench_ms(benchmark)
    benchmark.extra_info.update(
        {"n": n, "blob_bytes": blob_bytes, "blob_mb": round(blob_bytes / 1e6, 2)}
    )
    _record_result(
        {
            "test": "push",
            "n": n,
            "mean_ms": mean_ms,
            "min_ms": min_ms,
            "blob_bytes": blob_bytes,
            "blob_mb": round(blob_bytes / 1e6, 2),
        }
    )


# ── Test 2: cold merge (union N rows into an empty peer) ──────────────


@pytest.mark.parametrize("n", _SIZES, ids=_SIZE_IDS)
def test_merge_cold_scale(benchmark: Any, tmp_path: Path, n: int) -> None:
    """Fold an N-row snapshot into an *empty* peer — union inserts all N rows.

    Measures the ``_union_pull`` step only. The real fresh-machine pull takes the
    ``_merge_or_adopt`` *adopt* branch (a whole-file copy) instead, so this is the
    union-into-existing-empty-peer cost, not the fresh-adopt cost.
    """
    snap_db = tmp_path / "snap.db"
    _seed_db(snap_db, n)
    peer_db = tmp_path / "peer.db"
    snap_dir = tmp_path / "snapdir"
    snap_dir.mkdir()
    lock = WriteLock(peer_db, enabled=False)
    meta = peer_db.parent / ".sync_meta.json"

    def setup() -> tuple[tuple[Any, ...], dict[str, Any]]:
        peer_db.unlink(missing_ok=True)
        _seed_db(peer_db, 0)  # schema-only empty peer
        meta.unlink(missing_ok=True)
        sync = SnapshotSync(peer_db, snap_dir, lock, immutable=True, min_interval=0)
        return (sync,), {}

    def target(sync: SnapshotSync) -> None:
        sync._union_pull(snap_db)

    benchmark.pedantic(target, setup=setup, rounds=_MERGE_ROUNDS, iterations=1)

    # Correctness: a cold union into an empty peer lands exactly N rows.
    peer_db.unlink(missing_ok=True)
    _seed_db(peer_db, 0)
    meta.unlink(missing_ok=True)
    sync = SnapshotSync(peer_db, snap_dir, lock, immutable=True, min_interval=0)
    sync._union_pull(snap_db)
    assert _row_count(peer_db) == n

    mean_ms, min_ms = _bench_ms(benchmark)
    benchmark.extra_info.update({"n": n, "rows_merged": n})
    _record_result(
        {"test": "merge_cold", "n": n, "rows_merged": n, "mean_ms": mean_ms, "min_ms": min_ms}
    )


# ── Test 3: incremental merge (union K new rows into an N-row peer) ───


@pytest.mark.parametrize("n", _SIZES, ids=_SIZE_IDS)
def test_merge_incremental_scale(benchmark: Any, tmp_path: Path, n: int) -> None:
    """Fold an (N+K)-row snapshot into a peer that already has N rows.

    Only K rows are new, but ``INSERT OR IGNORE ... SELECT FROM snap.nodes``
    still *scans the whole snapshot* — so this reveals whether the incremental
    union is genuinely O(K) or secretly O(N).

    NOTE — scope: this measures the ``_union_pull`` step in isolation. The real
    startup pull (``_merge_or_adopt``) additionally does a full O(N)
    ``shutil.copy2`` prepull backup of the peer DB *before* the union, so true
    everyday-startup cost = this number + a peer-sized file copy (of the same
    order as the push backup measured above). These numbers are a lower bound on
    the full pull.
    """
    k = 10
    snap_db = tmp_path / "snap.db"
    _seed_db(snap_db, n + k)  # superset: first N ids match the peer, plus K new
    peer_template = tmp_path / "peer_template.db"
    _seed_db(peer_template, n)
    peer_db = tmp_path / "peer.db"
    snap_dir = tmp_path / "snapdir"
    snap_dir.mkdir()
    lock = WriteLock(peer_db, enabled=False)
    meta = peer_db.parent / ".sync_meta.json"

    def setup() -> tuple[tuple[Any, ...], dict[str, Any]]:
        shutil.copy2(peer_template, peer_db)  # fast restore of the N-row peer
        meta.unlink(missing_ok=True)
        sync = SnapshotSync(peer_db, snap_dir, lock, immutable=True, min_interval=0)
        return (sync,), {}

    def target(sync: SnapshotSync) -> None:
        sync._union_pull(snap_db)

    benchmark.pedantic(target, setup=setup, rounds=_MERGE_ROUNDS, iterations=1)

    # Correctness: peer grows by exactly K.
    shutil.copy2(peer_template, peer_db)
    meta.unlink(missing_ok=True)
    sync = SnapshotSync(peer_db, snap_dir, lock, immutable=True, min_interval=0)
    sync._union_pull(snap_db)
    assert _row_count(peer_db) == n + k

    mean_ms, min_ms = _bench_ms(benchmark)
    benchmark.extra_info.update({"n": n, "rows_new": k})
    _record_result(
        {"test": "merge_incremental", "n": n, "rows_new": k, "mean_ms": mean_ms, "min_ms": min_ms}
    )


# ── Test 3b/3c: FULL startup pull (_merge_or_adopt, incl. the prepull copy) ──
# The merge tests above isolate _union_pull. The real startup pull goes through
# _merge_or_adopt, which ALSO pays an O(N) shutil.copy2 (a .prepull backup of the
# peer on a non-fresh machine, or an adopt-copy of the whole snapshot on a fresh
# one) that the union tests exclude. These two measure that full path so the
# recorded numbers are the true startup cost, not a lower bound. Telemetry is
# disabled here so we measure sync I/O only (a real pull also emits one line).


@pytest.mark.parametrize("n", _SIZES, ids=_SIZE_IDS)
def test_pull_full_incremental_scale(
    benchmark: Any, tmp_path: Path, monkeypatch: Any, n: int
) -> None:
    """Full everyday-startup pull on a non-fresh machine: ``_merge_or_adopt``
    folds an (N+K)-row snapshot into an N-row peer — the O(N) prepull copy PLUS
    the union. This is the real number the incremental-merge test is a lower
    bound on."""
    monkeypatch.setenv("MEMCP_TELEMETRY", "false")
    k = 10
    snap_db = tmp_path / "snap.db"
    _seed_db(snap_db, n + k)
    peer_template = tmp_path / "peer_template.db"
    _seed_db(peer_template, n)
    peer_db = tmp_path / "peer.db"
    snap_dir = tmp_path / "snapdir"
    snap_dir.mkdir()
    lock = WriteLock(peer_db, enabled=False)
    meta = peer_db.parent / ".sync_meta.json"

    def setup() -> tuple[tuple[Any, ...], dict[str, Any]]:
        shutil.copy2(peer_template, peer_db)  # restore the N-row peer each round
        meta.unlink(missing_ok=True)
        peer_db.with_suffix(".db.prepull").unlink(missing_ok=True)
        sync = SnapshotSync(peer_db, snap_dir, lock, immutable=True, min_interval=0)
        return (sync,), {}

    def target(sync: SnapshotSync) -> None:
        sync._merge_or_adopt(snap_db, remote_gen=2)

    benchmark.pedantic(target, setup=setup, rounds=_MERGE_ROUNDS, iterations=1)

    # Correctness: peer grows by exactly K and the prepull backup was written.
    shutil.copy2(peer_template, peer_db)
    meta.unlink(missing_ok=True)
    sync = SnapshotSync(peer_db, snap_dir, lock, immutable=True, min_interval=0)
    sync._merge_or_adopt(snap_db, remote_gen=2)
    assert _row_count(peer_db) == n + k
    assert peer_db.with_suffix(".db.prepull").exists()

    mean_ms, min_ms = _bench_ms(benchmark)
    benchmark.extra_info.update({"n": n, "rows_new": k})
    _record_result(
        {
            "test": "pull_full_incremental",
            "n": n,
            "rows_new": k,
            "mean_ms": mean_ms,
            "min_ms": min_ms,
        }
    )


@pytest.mark.parametrize("n", _SIZES, ids=_SIZE_IDS)
def test_pull_full_cold_adopt_scale(
    benchmark: Any, tmp_path: Path, monkeypatch: Any, n: int
) -> None:
    """Full fresh-machine pull: ``_merge_or_adopt`` with NO local DB takes the
    adopt branch — a whole-snapshot ``shutil.copy2`` of N rows (no union). This is
    what a brand-new machine actually pays on first sync."""
    monkeypatch.setenv("MEMCP_TELEMETRY", "false")
    snap_db = tmp_path / "snap.db"
    _seed_db(snap_db, n)
    peer_db = tmp_path / "peer.db"
    snap_dir = tmp_path / "snapdir"
    snap_dir.mkdir()
    lock = WriteLock(peer_db, enabled=False)
    meta = peer_db.parent / ".sync_meta.json"

    def setup() -> tuple[tuple[Any, ...], dict[str, Any]]:
        peer_db.unlink(missing_ok=True)  # fresh machine: no local DB → adopt path
        meta.unlink(missing_ok=True)
        sync = SnapshotSync(peer_db, snap_dir, lock, immutable=True, min_interval=0)
        return (sync,), {}

    def target(sync: SnapshotSync) -> None:
        sync._merge_or_adopt(snap_db, remote_gen=1)

    benchmark.pedantic(target, setup=setup, rounds=_MERGE_ROUNDS, iterations=1)

    # Correctness: a fresh adopt lands exactly N rows.
    peer_db.unlink(missing_ok=True)
    meta.unlink(missing_ok=True)
    sync = SnapshotSync(peer_db, snap_dir, lock, immutable=True, min_interval=0)
    sync._merge_or_adopt(snap_db, remote_gen=1)
    assert _row_count(peer_db) == n

    mean_ms, min_ms = _bench_ms(benchmark)
    benchmark.extra_info.update({"n": n, "rows_adopted": n})
    _record_result(
        {
            "test": "pull_full_cold_adopt",
            "n": n,
            "rows_adopted": n,
            "mean_ms": mean_ms,
            "min_ms": min_ms,
        }
    )


# ── Test 4: content-verified cap GC cost ─────────────────────────────


def _make_blob(path: Path, n: int, *, gen: int) -> None:
    """A snapshot blob of ``n`` rows with its identity stamped, named so
    ``_blob_gen_from_name`` parses ``gen``."""
    _seed_db(path, n)
    # Stamp identity the way _publish_v2 does, so the blob looks real to GC.
    SnapshotSync._stamp_blob_identity(path, gen, None)


@pytest.mark.parametrize("over", [1, 5], ids=["1-over", "5-over"])
def test_gc_cap_scale(benchmark: Any, tmp_path: Path, over: int) -> None:
    """``_gc_blobs`` cap pass with ``over`` blobs above the cap.

    The content-verified cap opens the pointer blob once, then opens
    oldest-first candidates until it has reclaimed ``over`` of them — each open
    is the subset check the GC change added. Candidates carry a subset of the
    pointer's rows (and no tombstones), so they are reclaimable.
    """
    cap = 20
    snap_dir = tmp_path / "snapdir"
    snap_dir.mkdir()
    local_db = tmp_path / "graph.db"
    _seed_db(local_db, 500)
    lock = WriteLock(local_db, enabled=False)

    keep_name = f"graph.snapshot.{1000}.testhost.deadbeef.db"
    n_candidates = (cap - 1) + over  # one slot reserved for the pointer blob

    def setup() -> tuple[tuple[Any, ...], dict[str, Any]]:
        for p in snap_dir.glob("graph.snapshot.*.db"):
            p.unlink(missing_ok=True)
        # Pointer/keep blob: the retained superset (500 rows).
        _make_blob(snap_dir / keep_name, 500, gen=1000)
        # Candidates: subsets (first 100 rows) → reclaimable, older gens.
        for i in range(n_candidates):
            _make_blob(snap_dir / f"graph.snapshot.{i}.testhost.{i:08x}.db", 100, gen=i)
        sync = SnapshotSync(local_db, snap_dir, lock, immutable=True, min_interval=0)
        return (sync,), {}

    def target(sync: SnapshotSync) -> None:
        sync._gc_blobs(keep_name)

    benchmark.pedantic(target, setup=setup, rounds=_GC_ROUNDS, iterations=1)

    # Correctness: exactly `over` candidates reclaimed (keep + (cap-1) survive).
    for p in snap_dir.glob("graph.snapshot.*.db"):
        p.unlink(missing_ok=True)
    _make_blob(snap_dir / keep_name, 500, gen=1000)
    for i in range(n_candidates):
        _make_blob(snap_dir / f"graph.snapshot.{i}.testhost.{i:08x}.db", 100, gen=i)
    sync = SnapshotSync(local_db, snap_dir, lock, immutable=True, min_interval=0)
    sync._gc_blobs(keep_name)
    remaining = len(list(snap_dir.glob("graph.snapshot.*.db")))
    assert remaining == (n_candidates + 1) - over

    mean_ms, min_ms = _bench_ms(benchmark)
    benchmark.extra_info.update({"cap": cap, "over": over, "blobs": n_candidates + 1})
    _record_result(
        {
            "test": "gc_cap",
            "cap": cap,
            "over": over,
            "blobs": n_candidates + 1,
            "mean_ms": mean_ms,
            "min_ms": min_ms,
        }
    )
