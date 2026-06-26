"""NodeStore — SQLite connection management and node CRUD.

Manages the SQLite database connection, schema, and all node operations
(insert, get, delete, update). Extracted from GraphMemory.
"""

from __future__ import annotations

import contextlib
import json
import logging
import re
import sqlite3
import threading
from abc import ABC, abstractmethod
from collections.abc import Iterator
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from memcp.config import get_config
from memcp.core.fileutil import content_hash
from memcp.core.snapshot_sync import SnapshotSync
from memcp.core.write_lock import WriteLock, WriteLockError

logger = logging.getLogger(__name__)


class LockedConnection(sqlite3.Connection):
    """SQLite connection whose ``commit()`` is guarded by a :class:`WriteLock`.

    Every persisted write goes through ``commit()``, and in DELETE journal mode
    the main db file is mutated during commit — so wrapping commit serializes
    the file-write critical section across processes/machines without touching
    any call site. Reads never commit, so they never take the lock.

    Snapshot republish is *not* triggered here: a bare commit can't tell which
    tables a transaction touched (reindex's derived-table writes and recall's
    access-bumps all commit too). Durable republish is signalled explicitly by
    the durable node ops (:meth:`NodeStore.store` / ``delete_node`` / a metadata
    ``update_node``) via ``SnapshotSync.mark_durable_dirty``. See spec §3.3.

    The lock is attached after construction (the ``sqlite3.connect(
    factory=...)`` hook can't pass extra args).
    """

    _write_lock: WriteLock | None = None
    _defer_depth: int = 0

    def attach_lock(self, lock: WriteLock) -> None:
        self._write_lock = lock

    def commit(self) -> None:
        # Inside an atomic() block, intermediate commit() calls (e.g. the
        # per-edge commits in EdgeManager._add_edge, or store()'s old
        # node-then-entity split) are suppressed so the whole batch is one
        # transaction with a single real commit at the block boundary.
        if self._defer_depth > 0:
            return
        if self._write_lock is None:
            super().commit()
            return
        try:
            with self._write_lock:
                super().commit()
        except WriteLockError:
            # Fail-closed: the local lock is unavailable. Roll the pending write
            # transaction back so it neither commits unserialized nor strands a
            # lock on the db file, then surface the error to the caller.
            super().rollback()
            raise

    @contextlib.contextmanager
    def atomic(self) -> Iterator[None]:
        """Run a write batch as ONE transaction.

        Inner ``commit()`` calls become no-ops; a single real commit fires on
        clean exit, and any exception rolls the whole batch back. This is what
        lets a reindex DELETE + regenerate + ``set_index_meta`` (or store()'s
        node INSERT + entity_index writes) be atomic even though the helpers
        they call commit internally. Re-entrant: nested ``atomic()`` blocks
        defer to the outermost commit.
        """
        self._defer_depth += 1
        try:
            yield
        except BaseException:
            self._defer_depth -= 1
            if self._defer_depth == 0:
                super().rollback()
            raise
        else:
            self._defer_depth -= 1
            if self._defer_depth == 0:
                self.commit()


# ── Entity Extraction ─────────────────────────────────────────────────

_ENTITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("file", re.compile(r"(?:^|[\s\"'(])([.\w/-]+\.\w{1,10})(?:[\s\"'),]|$)")),
    ("module", re.compile(r"\b([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*){2,})\b")),
    ("url", re.compile(r"https?://[^\s\"'<>)]+", re.ASCII)),
    ("mention", re.compile(r"@([a-zA-Z_]\w+)")),
    ("identifier", re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")),
]


class EntityExtractor(ABC):
    """Abstract entity extractor — Phase 3: regex, Phase 4: LLM."""

    @abstractmethod
    def extract(self, content: str) -> list[str]:
        """Extract entity strings from content."""


class RegexEntityExtractor(EntityExtractor):
    """Extract entities via regex patterns — filenames, modules, URLs, etc."""

    def extract(self, content: str) -> list[str]:
        entities: list[str] = []
        seen: set[str] = set()

        for _kind, pattern in _ENTITY_PATTERNS:
            for match in pattern.finditer(content):
                entity = match.group(0).strip(" \"'(),")
                if len(entity) < 3:
                    continue
                key = entity.lower()
                if key not in seen:
                    seen.add(key)
                    entities.append(entity)

        return entities


class SpacyEntityExtractor(EntityExtractor):
    """Extract entities using spaCy NER — better quality than regex.

    Requires: pip install spacy && python -m spacy download en_core_web_sm
    Falls back to RegexEntityExtractor if spaCy or model not available.
    """

    MODEL = "en_core_web_sm"

    def __init__(self) -> None:
        import spacy  # noqa: F811

        try:
            self._nlp = spacy.load(self.MODEL)
        except OSError:
            raise ImportError(f"spaCy model {self.MODEL!r} not installed") from None

    def extract(self, content: str) -> list[str]:
        doc = self._nlp(content[:10000])  # cap for performance
        entities: list[str] = []
        seen: set[str] = set()
        for ent in doc.ents:
            key = ent.text.lower().strip()
            if len(key) >= 3 and key not in seen:
                seen.add(key)
                entities.append(ent.text.strip())
        return entities


class CombinedEntityExtractor(EntityExtractor):
    """Combine regex + spaCy extractors, deduplicating results."""

    def __init__(self, regex: RegexEntityExtractor, spacy_ext: SpacyEntityExtractor) -> None:
        self._regex = regex
        self._spacy = spacy_ext

    def extract(self, content: str) -> list[str]:
        regex_entities = self._regex.extract(content)
        spacy_entities = self._spacy.extract(content)
        seen: set[str] = set()
        combined: list[str] = []
        for e in regex_entities + spacy_entities:
            key = e.lower()
            if key not in seen:
                seen.add(key)
                combined.append(e)
        return combined


def _get_best_extractor() -> EntityExtractor:
    """Auto-select the best available entity extractor."""
    regex = RegexEntityExtractor()
    try:
        spacy_ext = SpacyEntityExtractor()
        return CombinedEntityExtractor(regex, spacy_ext)
    except (ImportError, OSError):
        return regex


# ── SQLite Schema ─────────────────────────────────────────────────────

_SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    summary TEXT DEFAULT '',
    category TEXT DEFAULT 'general',
    importance TEXT DEFAULT 'medium',
    effective_importance REAL DEFAULT 0.5,
    tags TEXT DEFAULT '[]',
    entities TEXT DEFAULT '[]',
    project TEXT DEFAULT 'default',
    session TEXT DEFAULT '',
    token_count INTEGER DEFAULT 0,
    access_count INTEGER DEFAULT 0,
    last_accessed_at TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS edges (
    source_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    edge_type TEXT NOT NULL CHECK(edge_type IN ('semantic','temporal','causal','entity')),
    weight REAL DEFAULT 1.0,
    metadata TEXT DEFAULT '{}',
    created_at TEXT NOT NULL,
    PRIMARY KEY (source_id, target_id, edge_type),
    FOREIGN KEY (source_id) REFERENCES nodes(id) ON DELETE CASCADE,
    FOREIGN KEY (target_id) REFERENCES nodes(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source_id);
CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target_id);
CREATE INDEX IF NOT EXISTS idx_edges_type ON edges(edge_type);
CREATE INDEX IF NOT EXISTS idx_nodes_project ON nodes(project);
CREATE INDEX IF NOT EXISTS idx_nodes_category ON nodes(category);
CREATE INDEX IF NOT EXISTS idx_nodes_importance ON nodes(importance);

CREATE TABLE IF NOT EXISTS entity_index (
    entity TEXT NOT NULL,
    node_id TEXT NOT NULL,
    PRIMARY KEY (entity, node_id),
    FOREIGN KEY (node_id) REFERENCES nodes(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_entity_index_entity ON entity_index(entity);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS index_meta (
    index_name TEXT PRIMARY KEY,
    built_against_revision INTEGER NOT NULL DEFAULT -1,
    built_at TEXT NOT NULL DEFAULT '',
    model_version TEXT NOT NULL DEFAULT '',
    built_against_seq INTEGER NOT NULL DEFAULT -1
);

-- Durable deletes carry a synced tombstone so a delete propagates across
-- machines and the additive union re-applies it as a deny-set (§3.5).
CREATE TABLE IF NOT EXISTS tombstones (
    id TEXT PRIMARY KEY,
    deleted_at TEXT NOT NULL,
    resurrected_at TEXT DEFAULT NULL
);

INSERT OR IGNORE INTO meta (key, value) VALUES ('revision', '0');
INSERT OR IGNORE INTO meta (key, value) VALUES ('ingest_seq', '0');
"""


def _next_ingest_seq(conn: sqlite3.Connection) -> int:
    """Atomically allocate the next ``ingest_seq``. Returns the new value.

    A dedicated ``meta`` counter bumped in the *same transaction* as the row
    INSERT, so two processes never hand out a duplicate seq (a read-modify-write
    ``next_seq`` would under §3.6 concurrency). See spec §3.4.
    """
    from memcp.core.revision import bump_meta_counter

    return bump_meta_counter(conn, "ingest_seq")


def write_tombstone(conn: sqlite3.Connection, node_id: str, deleted_at: str | None = None) -> None:
    """Record a synced tombstone for a durable delete (§3.5).

    Merge rule is newest-wins per field: keep ``MAX(deleted_at)`` so a re-delete
    out-ranks any prior ``resurrected_at`` and the union stays commutative.
    Must run inside the same transaction as the row DELETE. Callers commit.
    """
    deleted_at = deleted_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "INSERT INTO tombstones (id, deleted_at) VALUES (?, ?) "
        "ON CONFLICT(id) DO UPDATE SET "
        "deleted_at = MAX(tombstones.deleted_at, excluded.deleted_at)",
        (node_id, deleted_at),
    )


def resurrect_tombstone(
    conn: sqlite3.Connection, node_id: str, resurrected_at: str | None = None
) -> None:
    """Mark a tombstone resurrected so the deny-set spares the row (§3.10 restore).

    Newest-wins: keep ``MAX(resurrected_at)``. No-op if no tombstone exists.
    Must run inside the restore transaction. Callers commit.
    """
    resurrected_at = resurrected_at or datetime.now(timezone.utc).isoformat()
    conn.execute(
        "UPDATE tombstones SET resurrected_at = MAX(COALESCE(resurrected_at, ''), ?) WHERE id = ?",
        (resurrected_at, node_id),
    )


# ── NodeStore ─────────────────────────────────────────────────────────


class NodeStore:
    """SQLite connection management and node CRUD operations."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            config = get_config()
            db_path = str(config.graph_db_path)

        self._db_path = db_path
        self._extractor: EntityExtractor = _get_best_extractor()
        self._conn: sqlite3.Connection | None = None
        self._sync: SnapshotSync | None = None
        # Serializes first-call connection init: without it, concurrent threads
        # each see _conn is None and build a second connection + a second
        # flusher (and race the schema migration). P5.
        self._conn_lock = threading.Lock()

    # ── Connection management ─────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        # Double-checked locking: the common case (already open) is lock-free;
        # the first-call init is serialized so exactly one connection + one
        # flusher are created.
        if self._conn is not None:
            return self._conn
        with self._conn_lock:
            if self._conn is not None:
                return self._conn
            return self._init_conn()

    def _init_conn(self) -> sqlite3.Connection:
        """Build the connection + sync + flusher. Caller holds ``_conn_lock``."""
        config = get_config()
        lock = WriteLock(
            self._db_path,
            enabled=config.write_lock_enabled,
            lease_ttl=config.write_lock_lease_ttl,
            timeout=config.write_lock_timeout,
            settle=config.write_lock_settle_ms / 1000.0,
        )
        # Cross-machine snapshot sync: pull a newer snapshot BEFORE opening
        # the DB (no handle is held yet, so replacing the file is safe).
        if config.snapshot_dir:
            self._sync = SnapshotSync(
                self._db_path,
                config.snapshot_dir,
                lock,
                min_interval=config.snapshot_min_interval,
                max_blobs=config.snapshot_max_blobs,
            )
            # Fresh machine MUST adopt before connect (the adopt path
            # os.replace()s the DB file; unsafe under an open handle). An
            # established machine defers the pull to the flusher so a stalled
            # snapshot mount can't block startup/first-request (see plan
            # docs/superpowers/plans/2026-06-08-nonblocking-startup-pull.md).
            if config.snapshot_pull_blocking:
                # Legacy fully-synchronous pull (test pin).
                self._sync.pull_if_newer()
            elif not Path(self._db_path).exists():
                # Fresh machine: bound the adopt-pull so a stalled Drive mount
                # can't hang startup forever. On timeout, start with an empty
                # DB and defer convergence to the flusher (pull_pending).
                self._pull_fresh_with_timeout(config.snapshot_pull_timeout)
            else:
                self._sync.pull_pending = True  # flusher will pick this up

        conn = sqlite3.connect(self._db_path, factory=LockedConnection)
        conn.attach_lock(lock)  # guard commits before any write happens
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA journal_mode={config.sqlite_journal_mode}")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=5000")
        conn.executescript(_SCHEMA)
        self._migrate_schema(conn)
        self._conn = conn

        # Absorb a legacy memory.json into the graph through the one funnel
        # every backend op passes (§3.7) — not _ensure_graph_migrated, which
        # the remember/recall hot path never hits.
        self._migrate_legacy_json(conn)

        if self._sync is not None:
            self._sync.start_flusher()
        return self._conn

    def _pull_fresh_with_timeout(self, timeout: float) -> None:
        """Run the fresh-machine adopt-pull in a worker thread, bounded by
        ``timeout`` seconds. If it doesn't finish in time, warn, set
        ``pull_pending`` so the flusher completes convergence later, and return so
        startup can proceed on an empty DB. A non-positive timeout means "no
        bound" — wait for the pull (used when sync is effectively local)."""
        assert self._sync is not None
        done = threading.Event()

        def _worker() -> None:
            try:
                self._sync.pull_if_newer()  # type: ignore[union-attr]
            except Exception:  # pragma: no cover - pull_if_newer is already fail-open
                logger.exception("fresh-machine snapshot pull failed")
            finally:
                done.set()

        threading.Thread(target=_worker, name="memcp-fresh-pull", daemon=True).start()
        if timeout and timeout > 0 and not done.wait(timeout):
            logger.warning(
                "fresh-machine snapshot pull exceeded %.1fs; starting with an empty "
                "DB and deferring the pull to the background flusher",
                timeout,
            )
            self._sync.pull_pending = True
        elif timeout <= 0:
            done.wait()

    def _migrate_legacy_json(self, conn: sqlite3.Connection) -> None:
        """One-time union of ~/.memcp/memory.json into the graph, deduped by
        content. Renames memory.json → .migrated only after a successful commit
        so a crash mid-migrate retries (§3.7, §3.9)."""
        config = get_config()
        mem_path = config.memory_path
        if not mem_path.exists():
            return
        try:
            data = json.loads(mem_path.read_text())
        except (ValueError, OSError):
            return
        insights = data.get("insights") if isinstance(data, dict) else None
        migrated: list[dict[str, Any]] = []
        if insights:
            existing = {content_hash(r[0]) for r in conn.execute("SELECT content FROM nodes")}
            for ins in insights:
                # Skip malformed rows (missing id/content) — a single bad entry
                # must not raise out of _get_conn and wedge startup.
                if not ins.get("id") or not ins.get("content"):
                    continue
                ch = content_hash(ins["content"])
                if ch in existing:
                    continue
                if conn.execute("SELECT 1 FROM nodes WHERE id = ?", (ins.get("id"),)).fetchone():
                    continue
                migrated.append(self.store(ins))  # allocates ingest_seq, extracts entities
                existing.add(ch)
        # NodeStore.store doesn't generate edges (GraphMemory.store does), so
        # generate them here to keep migrated data fully indexed — matching the
        # legacy migrate_from_json path. Deferred import avoids a cycle.
        if migrated:
            from memcp.core.edge_manager import EdgeManager

            edge_manager = EdgeManager(self)
            for ins in migrated:
                edge_manager.generate_edges(ins)
        # Rename only after the migration committed (store() commits per row).
        mem_path.replace(mem_path.with_suffix(mem_path.suffix + ".migrated"))

    # Durable node fields — an update touching any of these is a metadata edit
    # that must republish. The rest (access_count, last_accessed_at,
    # effective_importance, feedback_score) are non-syncing derived counters.
    _DURABLE_UPDATE_FIELDS = frozenset(
        {"summary", "entities", "tags", "category", "importance", "archived_at"}
    )

    def _mark_durable(self) -> None:
        """Signal a durable node change so the snapshot flusher republishes."""
        if self._sync is not None:
            self._sync.mark_durable_dirty()

    @staticmethod
    def _migrate_schema(conn: sqlite3.Connection) -> None:
        """Apply incremental schema migrations for Step 2 columns."""
        # edges.last_activated_at — Hebbian learning / edge decay
        try:
            conn.execute("SELECT last_activated_at FROM edges LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE edges ADD COLUMN last_activated_at TEXT")
            conn.commit()
        # nodes.feedback_score — feedback/reinforce API
        try:
            conn.execute("SELECT feedback_score FROM nodes LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE nodes ADD COLUMN feedback_score REAL DEFAULT 0.0")
            conn.commit()
        # nodes.archived_at — synced soft-state so "archived" travels in the DB
        # instead of being a hard delete + un-synced side file (§3.5).
        try:
            conn.execute("SELECT archived_at FROM nodes LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE nodes ADD COLUMN archived_at TEXT")
            conn.commit()
        # nodes.ingest_seq — monotonic per-row sequence for merge reindex cuts (§3.4)
        try:
            conn.execute("SELECT ingest_seq FROM nodes LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute("ALTER TABLE nodes ADD COLUMN ingest_seq INTEGER")
            conn.commit()
        # index_meta.built_against_seq — ingest_seq reindex cut (§3.4)
        try:
            conn.execute("SELECT built_against_seq FROM index_meta LIMIT 1")
        except sqlite3.OperationalError:
            conn.execute(
                "ALTER TABLE index_meta ADD COLUMN built_against_seq INTEGER NOT NULL DEFAULT -1"
            )
            conn.commit()
        NodeStore._backfill_ingest_seq(conn)

    @staticmethod
    def _backfill_ingest_seq(conn: sqlite3.Connection) -> None:
        """Assign ingest_seq to any rows missing one (legacy / old-binary inserts),
        then advance the allocator past the max so new inserts stay unique."""
        missing = conn.execute(
            "SELECT id FROM nodes WHERE ingest_seq IS NULL ORDER BY created_at, id"
        ).fetchall()
        if not missing:
            return
        meta_row = conn.execute("SELECT value FROM meta WHERE key = 'ingest_seq'").fetchone()
        meta_val = int(meta_row[0])
        max_existing = conn.execute("SELECT COALESCE(MAX(ingest_seq), 0) FROM nodes").fetchone()[0]
        cur = max(meta_val, int(max_existing))
        for (node_id,) in missing:
            cur += 1
            conn.execute("UPDATE nodes SET ingest_seq = ? WHERE id = ?", (cur, node_id))
        conn.execute("UPDATE meta SET value = ? WHERE key = 'ingest_seq'", (str(cur),))
        conn.commit()

    def close(self) -> None:
        if self._sync is not None:
            self._sync.stop()  # final snapshot flush
            self._sync = None
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Node operations ───────────────────────────────────────────

    def store(self, insight: dict[str, Any]) -> dict[str, Any]:
        """Insert a node. Returns the stored insight with auto-extracted entities."""
        conn = self._get_conn()

        entities = insight.get("entities", [])
        if not entities:
            entities = self._extractor.extract(insight.get("content", ""))
            insight["entities"] = entities

        # Node INSERT + entity_index writes are ONE transaction: a crash between
        # them must not leave a node present without its entity rows (P4). The
        # ingest_seq allocation rides the same transaction (§3.4).
        with conn.atomic():
            seq = _next_ingest_seq(conn)
            conn.execute(
                """INSERT OR REPLACE INTO nodes
                   (id, content, summary, category, importance,
                    effective_importance, tags, entities, project, session,
                    token_count, access_count, last_accessed_at, created_at,
                    archived_at, ingest_seq)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    insight["id"],
                    insight["content"],
                    insight.get("summary", ""),
                    insight.get("category", "general"),
                    insight.get("importance", "medium"),
                    insight.get("effective_importance", 0.5),
                    json.dumps(insight.get("tags", [])),
                    json.dumps(entities),
                    insight.get("project", "default"),
                    insight.get("session", ""),
                    insight.get("token_count", 0),
                    insight.get("access_count", 0),
                    insight.get("last_accessed_at"),
                    insight.get("created_at", datetime.now(timezone.utc).isoformat()),
                    insight.get("archived_at"),
                    seq,
                ),
            )

            # Populate entity index in the same transaction.
            if entities:
                node_id = insight["id"]
                # Clear old entries for this node (in case of REPLACE)
                conn.execute("DELETE FROM entity_index WHERE node_id = ?", (node_id,))
                for entity in entities:
                    conn.execute(
                        "INSERT OR IGNORE INTO entity_index (entity, node_id) VALUES (?, ?)",
                        (entity.lower(), node_id),
                    )

        self._mark_durable()
        return insight

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Get a single node by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete_node(self, node_id: str) -> bool:
        """Delete a node and all its edges + entity index entries.

        Records a synced tombstone in the same transaction so the delete
        propagates cross-machine and the union deny-set re-applies it (§3.5).
        Tombstones merge newest-wins: keep ``MAX(deleted_at)`` so a re-delete
        out-ranks any prior ``resurrected_at``.
        """
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.execute(
            "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        )
        conn.execute("DELETE FROM entity_index WHERE node_id = ?", (node_id,))
        if cursor.rowcount > 0:
            write_tombstone(conn, node_id)
        conn.commit()
        if cursor.rowcount > 0:
            self._mark_durable()
        return cursor.rowcount > 0

    def update_node(self, node_id: str, updates: dict[str, Any]) -> bool:
        """Update specific fields on a node."""
        conn = self._get_conn()

        allowed = {
            "access_count",
            "last_accessed_at",
            "effective_importance",
            "summary",
            "entities",
            "tags",
            "feedback_score",
            "category",
            "importance",
            "archived_at",
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False

        # Capture the new entity list (if any) BEFORE JSON-encoding for storage,
        # so we can rebuild entity_index from it (the consolidation-drift bug:
        # update_node(entities=…) previously left entity_index stale, P4).
        new_entities: list[str] | None = None
        if "entities" in filtered:
            raw = filtered["entities"]
            if isinstance(raw, list):
                new_entities = raw
            elif isinstance(raw, str):
                with contextlib.suppress(json.JSONDecodeError, TypeError):
                    parsed = json.loads(raw)
                    if isinstance(parsed, list):
                        new_entities = parsed

        for key in ("tags", "entities"):
            if key in filtered and isinstance(filtered[key], list):
                filtered[key] = json.dumps(filtered[key])

        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [node_id]
        # The nodes UPDATE and the dependent entity_index rewrite are ONE
        # transaction so entity_index can never diverge from nodes.entities.
        with conn.atomic():
            cursor = conn.execute(
                f"UPDATE nodes SET {set_clause} WHERE id = ?",  # noqa: S608
                values,
            )
            if cursor.rowcount > 0 and new_entities is not None:
                conn.execute("DELETE FROM entity_index WHERE node_id = ?", (node_id,))
                for entity in new_entities:
                    conn.execute(
                        "INSERT OR IGNORE INTO entity_index (entity, node_id) VALUES (?, ?)",
                        (entity.lower(), node_id),
                    )
        # Only a durable metadata edit republishes; derived-counter bumps
        # (access_count etc. from recall) are non-syncing. See spec §3.3.
        if cursor.rowcount > 0 and filtered.keys() & self._DURABLE_UPDATE_FIELDS:
            self._mark_durable()
        return cursor.rowcount > 0

    # ── Helpers ───────────────────────────────────────────────────

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a sqlite3.Row to a plain dict with parsed JSON fields."""
        d = dict(row)
        for field in ("tags", "entities"):
            if field in d and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = []
        return d
