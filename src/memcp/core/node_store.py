"""NodeStore — SQLite connection management and node CRUD.

Manages the SQLite database connection, schema, and all node operations
(insert, get, delete, update). Extracted from GraphMemory.
"""

from __future__ import annotations

import json
import re
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from memcp.config import get_config

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
"""


# ── NodeStore ─────────────────────────────────────────────────────────


class NodeStore:
    """SQLite connection management and node CRUD operations."""

    def __init__(self, db_path: str | None = None) -> None:
        if db_path is None:
            config = get_config()
            db_path = str(config.graph_db_path)

        self._db_path = db_path
        self._extractor: EntityExtractor = RegexEntityExtractor()
        self._conn: sqlite3.Connection | None = None

    # ── Connection management ─────────────────────────────────────

    def _get_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = sqlite3.connect(self._db_path)
            self._conn.row_factory = sqlite3.Row
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.execute("PRAGMA busy_timeout=5000")
            self._conn.executescript(_SCHEMA)
        return self._conn

    def close(self) -> None:
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

        conn.execute(
            """INSERT OR REPLACE INTO nodes
               (id, content, summary, category, importance,
                effective_importance, tags, entities, project, session,
                token_count, access_count, last_accessed_at, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
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
            ),
        )
        conn.commit()

        # Populate entity index
        if entities:
            node_id = insight["id"]
            # Clear old entries for this node (in case of REPLACE)
            conn.execute("DELETE FROM entity_index WHERE node_id = ?", (node_id,))
            for entity in entities:
                conn.execute(
                    "INSERT OR IGNORE INTO entity_index (entity, node_id) VALUES (?, ?)",
                    (entity.lower(), node_id),
                )
            conn.commit()

        return insight

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Get a single node by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete_node(self, node_id: str) -> bool:
        """Delete a node and all its edges + entity index entries."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        conn.execute(
            "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        )
        conn.execute("DELETE FROM entity_index WHERE node_id = ?", (node_id,))
        conn.commit()
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
        }
        filtered = {k: v for k, v in updates.items() if k in allowed}
        if not filtered:
            return False

        for key in ("tags", "entities"):
            if key in filtered and isinstance(filtered[key], list):
                filtered[key] = json.dumps(filtered[key])

        set_clause = ", ".join(f"{k} = ?" for k in filtered)
        values = list(filtered.values()) + [node_id]
        cursor = conn.execute(
            f"UPDATE nodes SET {set_clause} WHERE id = ?",  # noqa: S608
            values,
        )
        conn.commit()
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
