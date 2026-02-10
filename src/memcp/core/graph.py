"""MAGMA-inspired 4-graph memory — SQLite-backed with auto-edges.

Four edge types on the same node set (insights):
  - semantic: similar content (keyword overlap)
  - temporal: created close in time (same session, <30 min gap)
  - causal: cause→effect (detected by keyword patterns)
  - entity: shared extracted entities (files, modules, URLs, etc.)

Storage: SQLite at ~/.memcp/graph.db with WAL mode for concurrent reads.
"""

from __future__ import annotations

import re
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from memcp.config import get_config

# ── Entity Extraction ─────────────────────────────────────────────────

# Patterns for regex-based entity extraction
_ENTITY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    # File paths (e.g. src/memcp/server.py, ./config.json)
    ("file", re.compile(r"(?:^|[\s\"'(])([.\w/-]+\.\w{1,10})(?:[\s\"'),]|$)")),
    # Module paths (e.g. memcp.core.graph, os.path)
    ("module", re.compile(r"\b([a-zA-Z_]\w*(?:\.[a-zA-Z_]\w*){2,})\b")),
    # URLs
    ("url", re.compile(r"https?://[^\s\"'<>)]+", re.ASCII)),
    # @mentions
    ("mention", re.compile(r"@([a-zA-Z_]\w+)")),
    # CamelCase identifiers (likely class/type names)
    ("identifier", re.compile(r"\b([A-Z][a-z]+(?:[A-Z][a-z]+)+)\b")),
]

# Patterns that indicate causal relationships
_CAUSAL_PATTERNS = re.compile(
    r"\b(?:because|therefore|due to|caused by|as a result|decided to|"
    r"chosen because|so that|in order to|leads to|results in)\b",
    re.IGNORECASE,
)


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
"""


# ── GraphMemory ───────────────────────────────────────────────────────


class GraphMemory:
    """SQLite-backed 4-graph memory inspired by MAGMA.

    Nodes are insights; edges encode semantic, temporal, causal, and entity
    relationships.  Auto-generates edges on insert.
    """

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
            self._conn.executescript(_SCHEMA)
        return self._conn

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    # ── Node operations ───────────────────────────────────────────

    def store(self, insight: dict[str, Any]) -> dict[str, Any]:
        """Insert a node and auto-generate edges.

        Args:
            insight: Dict with id, content, category, importance, tags,
                     entities, project, session, etc.

        Returns the stored insight with any auto-extracted entities added.
        """
        conn = self._get_conn()

        # Auto-extract entities if none provided
        entities = insight.get("entities", [])
        if not entities:
            entities = self._extractor.extract(insight.get("content", ""))
            insight["entities"] = entities

        import json

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

        # Auto-generate edges
        self._generate_edges(insight)

        return insight

    def get_node(self, node_id: str) -> dict[str, Any] | None:
        """Get a single node by ID."""
        conn = self._get_conn()
        row = conn.execute("SELECT * FROM nodes WHERE id = ?", (node_id,)).fetchone()
        if row is None:
            return None
        return self._row_to_dict(row)

    def delete_node(self, node_id: str) -> bool:
        """Delete a node and all its edges."""
        conn = self._get_conn()
        cursor = conn.execute("DELETE FROM nodes WHERE id = ?", (node_id,))
        # CASCADE should handle edges, but be explicit
        conn.execute(
            "DELETE FROM edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        )
        conn.commit()
        return cursor.rowcount > 0

    def update_node(self, node_id: str, updates: dict[str, Any]) -> bool:
        """Update specific fields on a node."""
        conn = self._get_conn()
        import json

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

        # Serialize lists
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

    # ── Edge generation ───────────────────────────────────────────

    def _generate_edges(self, insight: dict[str, Any]) -> None:
        """Auto-generate all 4 edge types for a new insight."""
        self._generate_temporal_edges(insight)
        self._generate_entity_edges(insight)
        self._generate_semantic_edges(insight)
        self._generate_causal_edges(insight)

    def _generate_temporal_edges(self, insight: dict[str, Any]) -> None:
        """Link to insights created within 30 minutes."""
        conn = self._get_conn()
        now_str = insight.get("created_at", "")
        if not now_str:
            return

        try:
            now = datetime.fromisoformat(now_str)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return

        # Find recent nodes (same project, within 30 min)
        rows = conn.execute(
            """SELECT id, created_at FROM nodes
               WHERE id != ? AND project = ?
               ORDER BY created_at DESC LIMIT 20""",
            (insight["id"], insight.get("project", "default")),
        ).fetchall()

        for row in rows:
            try:
                other_dt = datetime.fromisoformat(row["created_at"])
                if other_dt.tzinfo is None:
                    other_dt = other_dt.replace(tzinfo=timezone.utc)
                delta_minutes = abs((now - other_dt).total_seconds()) / 60
                if delta_minutes <= 30:
                    weight = max(0.1, 1.0 - delta_minutes / 30)
                    self._add_edge(insight["id"], row["id"], "temporal", weight)
            except (ValueError, TypeError):
                continue

    def _generate_entity_edges(self, insight: dict[str, Any]) -> None:
        """Link to insights sharing the same entities."""
        conn = self._get_conn()
        entities = insight.get("entities", [])
        if not entities:
            return

        import json

        # Find other nodes that mention the same entities
        for entity in entities:
            entity_lower = entity.lower()
            rows = conn.execute(
                "SELECT id, entities FROM nodes WHERE id != ?",
                (insight["id"],),
            ).fetchall()

            for row in rows:
                try:
                    other_entities = json.loads(row["entities"])
                    if any(e.lower() == entity_lower for e in other_entities):
                        self._add_edge(
                            insight["id"],
                            row["id"],
                            "entity",
                            1.0,
                            metadata={"entity": entity},
                        )
                except (json.JSONDecodeError, TypeError):
                    continue

    def _try_embedding_semantic_edges(self, insight: dict[str, Any]) -> bool:
        """Try to create semantic edges using embeddings. Returns True on success."""
        try:
            from memcp.core.embeddings import get_provider
            from memcp.core.vecstore import VectorStore

            provider = get_provider()
            if provider is None:
                return False

            from memcp.config import get_config

            config = get_config()
            store_path = config.cache_dir / "insight_embeddings.npz"
            store = VectorStore(store_path)
            store.load()

            text = " ".join(
                [
                    insight.get("content", ""),
                    " ".join(insight.get("tags", [])),
                ]
            )
            vec = provider.embed(text)
            store.add(insight["id"], vec)

            results = store.search(vec, top_k=4)
            for target_id, score in results:
                if target_id != insight["id"] and score >= 0.3:
                    self._add_edge(insight["id"], target_id, "semantic", score)

            store.save()
            return True
        except Exception:
            return False

    def _generate_semantic_edges(self, insight: dict[str, Any]) -> None:
        """Link to top-3 most similar insights by keyword overlap.

        Tries embedding-based similarity first, falls back to keyword overlap.
        """
        if self._try_embedding_semantic_edges(insight):
            return

        conn = self._get_conn()
        import json

        content_tokens = set(re.findall(r"\w+", insight.get("content", "").lower()))
        tag_tokens = {t.lower() for t in insight.get("tags", [])}
        query_tokens = content_tokens | tag_tokens

        if not query_tokens:
            return

        rows = conn.execute(
            "SELECT id, content, tags FROM nodes WHERE id != ? AND project = ?",
            (insight["id"], insight.get("project", "default")),
        ).fetchall()

        scored: list[tuple[float, str]] = []
        for row in rows:
            other_tokens = set(re.findall(r"\w+", row["content"].lower()))
            try:
                other_tags = {t.lower() for t in json.loads(row["tags"])}
            except (json.JSONDecodeError, TypeError):
                other_tags = set()
            other_all = other_tokens | other_tags

            overlap = query_tokens & other_all
            if overlap:
                score = len(overlap) / max(len(query_tokens), len(other_all))
                scored.append((score, row["id"]))

        scored.sort(key=lambda x: -x[0])
        for score, target_id in scored[:3]:
            if score >= 0.1:
                self._add_edge(insight["id"], target_id, "semantic", score)

    def _generate_causal_edges(self, insight: dict[str, Any]) -> None:
        """Detect causal language and link to referenced insights."""
        content = insight.get("content", "")
        if not _CAUSAL_PATTERNS.search(content):
            return

        conn = self._get_conn()
        # Find recent insights in the same project that might be the cause
        rows = conn.execute(
            """SELECT id, content FROM nodes
               WHERE id != ? AND project = ?
               ORDER BY created_at DESC LIMIT 10""",
            (insight["id"], insight.get("project", "default")),
        ).fetchall()

        content_lower = content.lower()
        for row in rows:
            other_lower = row["content"].lower()
            # Check if key words from the other insight appear in this one
            other_tokens = set(re.findall(r"\w+", other_lower))
            content_tokens = set(re.findall(r"\w+", content_lower))
            overlap = other_tokens & content_tokens
            # Meaningful overlap suggests a causal reference
            if len(overlap) >= 3:
                score = len(overlap) / max(len(other_tokens), len(content_tokens))
                if score >= 0.15:
                    self._add_edge(insight["id"], row["id"], "causal", score)
                    break  # Link to at most one cause

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert an edge, ignoring duplicates."""
        import json

        conn = self._get_conn()
        try:
            conn.execute(
                """INSERT OR IGNORE INTO edges
                   (source_id, target_id, edge_type, weight, metadata, created_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (
                    source_id,
                    target_id,
                    edge_type,
                    weight,
                    json.dumps(metadata or {}),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError:
            pass

    # ── Query / Traversal ─────────────────────────────────────────

    def query(
        self,
        query: str = "",
        category: str = "",
        importance: str = "",
        limit: int = 10,
        max_tokens: int = 0,
        project: str = "",
        session: str = "",
        scope: str = "project",
    ) -> list[dict[str, Any]]:
        """Query nodes with intent-aware graph traversal.

        Detects intent from query (what/when/why/who) and emphasizes
        the corresponding edge type for result ranking.
        """
        conn = self._get_conn()

        # Build base query
        conditions = []
        params: list[Any] = []

        if scope == "session" and session:
            conditions.append("session = ?")
            params.append(session)
        elif scope == "project" and project:
            conditions.append("project = ?")
            params.append(project)

        if category:
            conditions.append("category = ?")
            params.append(category)

        if importance:
            conditions.append("importance = ?")
            params.append(importance)

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE {where} ORDER BY created_at DESC",  # noqa: S608
            params,
        ).fetchall()

        nodes = [self._row_to_dict(r) for r in rows]

        # If query provided, score and rank
        if query.strip():
            intent = self._detect_intent(query)
            nodes = self._rank_by_intent(query, nodes, intent, limit)
        else:
            nodes = nodes[:limit]

        # Apply token budget
        if max_tokens > 0:
            budgeted: list[dict[str, Any]] = []
            tokens_used = 0
            for node in nodes:
                n_tokens = node.get("token_count", 0)
                if tokens_used + n_tokens > max_tokens and budgeted:
                    break
                budgeted.append(node)
                tokens_used += n_tokens
            nodes = budgeted

        return nodes

    def get_related(
        self,
        insight_id: str,
        edge_type: str = "",
        depth: int = 1,
    ) -> dict[str, Any]:
        """Traverse graph from a node, optionally filtering by edge type.

        Returns dict with center node, related nodes, and edges.
        """
        center = self.get_node(insight_id)
        if center is None:
            raise FileNotFoundError(f"Insight {insight_id!r} not found")

        visited: set[str] = {insight_id}
        related_nodes: list[dict[str, Any]] = []
        related_edges: list[dict[str, Any]] = []

        frontier = [insight_id]
        for _d in range(depth):
            next_frontier: list[str] = []
            for node_id in frontier:
                edges = self._get_edges(node_id, edge_type)
                for edge in edges:
                    other_id = (
                        edge["target_id"] if edge["source_id"] == node_id else edge["source_id"]
                    )
                    if other_id not in visited:
                        visited.add(other_id)
                        node = self.get_node(other_id)
                        if node:
                            related_nodes.append(node)
                            next_frontier.append(other_id)
                    related_edges.append(edge)
            frontier = next_frontier

        return {
            "center": center,
            "related": related_nodes,
            "edges": related_edges,
            "depth": depth,
            "edge_type_filter": edge_type or "all",
        }

    def stats(self, project: str = "") -> dict[str, Any]:
        """Graph statistics: node/edge counts, top entities."""
        conn = self._get_conn()

        if project:
            node_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE project = ?", (project,)
            ).fetchone()[0]
        else:
            node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

        # Edge counts by type
        edge_counts: dict[str, int] = {}
        for etype in ("semantic", "temporal", "causal", "entity"):
            if project:
                count = conn.execute(
                    """SELECT COUNT(*) FROM edges e
                       JOIN nodes n ON e.source_id = n.id
                       WHERE e.edge_type = ? AND n.project = ?""",
                    (etype, project),
                ).fetchone()[0]
            else:
                count = conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE edge_type = ?", (etype,)
                ).fetchone()[0]
            edge_counts[etype] = count

        # Top entities (most connected)
        import json

        entity_freq: dict[str, int] = {}
        if project:
            rows = conn.execute(
                "SELECT entities FROM nodes WHERE project = ?", (project,)
            ).fetchall()
        else:
            rows = conn.execute("SELECT entities FROM nodes").fetchall()

        for row in rows:
            try:
                entities = json.loads(row["entities"])
                for e in entities:
                    entity_freq[e] = entity_freq.get(e, 0) + 1
            except (json.JSONDecodeError, TypeError):
                continue

        top_entities = sorted(entity_freq.items(), key=lambda x: -x[1])[:10]

        return {
            "node_count": node_count,
            "edge_counts": edge_counts,
            "total_edges": sum(edge_counts.values()),
            "top_entities": [{"entity": e, "count": c} for e, c in top_entities],
        }

    # ── Migration ─────────────────────────────────────────────────

    def migrate_from_json(self, memory: dict[str, Any]) -> int:
        """Import insights from a Phase 1 memory.json into the graph.

        Returns the number of imported insights.
        """
        imported = 0
        for ins in memory.get("insights", []):
            if self.get_node(ins["id"]) is None:
                self.store(ins)
                imported += 1
        return imported

    # ── Internal helpers ──────────────────────────────────────────

    def _detect_intent(self, query: str) -> str:
        """Detect query intent from keywords."""
        q = query.lower().strip()
        if q.startswith("why") or "reason" in q or "cause" in q:
            return "why"
        if q.startswith("when") or "timeline" in q or "chronolog" in q:
            return "when"
        if q.startswith("who") or q.startswith("which") or "entity" in q:
            return "who"
        return "what"

    def _rank_by_intent(
        self,
        query: str,
        nodes: list[dict[str, Any]],
        intent: str,
        limit: int,
    ) -> list[dict[str, Any]]:
        """Rank nodes by combining keyword match with intent-weighted edge scores."""
        query_tokens = set(re.findall(r"\w+", query.lower()))
        if not query_tokens:
            return nodes[:limit]

        # Base keyword scores
        scored: list[tuple[float, dict[str, Any]]] = []
        for node in nodes:
            text = " ".join(
                [
                    node.get("content", ""),
                    node.get("summary", ""),
                    " ".join(node.get("tags", [])),
                ]
            ).lower()
            doc_tokens = set(re.findall(r"\w+", text))
            overlap = query_tokens & doc_tokens
            if not overlap:
                continue

            keyword_score = len(overlap) / len(query_tokens)

            # Boost score based on intent-relevant edges
            edge_boost = self._compute_edge_boost(node["id"], intent)
            total_score = keyword_score * 0.7 + edge_boost * 0.3

            scored.append((total_score, node))

        scored.sort(key=lambda x: -x[0])
        return [node for _, node in scored[:limit]]

    def _compute_edge_boost(self, node_id: str, intent: str) -> float:
        """Compute edge-based boost for a given intent."""
        intent_to_type = {
            "what": "semantic",
            "when": "temporal",
            "why": "causal",
            "who": "entity",
        }
        primary_type = intent_to_type.get(intent, "semantic")

        conn = self._get_conn()
        # Count edges of the primary type
        primary_count = conn.execute(
            """SELECT COUNT(*) FROM edges
               WHERE (source_id = ? OR target_id = ?) AND edge_type = ?""",
            (node_id, node_id, primary_type),
        ).fetchone()[0]

        # Count total edges
        total_count = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        ).fetchone()[0]

        if total_count == 0:
            return 0.0

        # Normalize: more edges of the right type = higher boost
        return min(1.0, primary_count / max(1, total_count) + 0.1 * primary_count)

    def _get_edges(self, node_id: str, edge_type: str = "") -> list[dict[str, Any]]:
        """Get all edges connected to a node, optionally filtered by type."""
        import json

        conn = self._get_conn()
        if edge_type:
            rows = conn.execute(
                """SELECT * FROM edges
                   WHERE (source_id = ? OR target_id = ?) AND edge_type = ?""",
                (node_id, node_id, edge_type),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM edges WHERE source_id = ? OR target_id = ?",
                (node_id, node_id),
            ).fetchall()

        return [
            {
                "source_id": r["source_id"],
                "target_id": r["target_id"],
                "edge_type": r["edge_type"],
                "weight": r["weight"],
                "metadata": json.loads(r["metadata"]) if r["metadata"] else {},
            }
            for r in rows
        ]

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        """Convert a sqlite3.Row to a plain dict with parsed JSON fields."""
        import json

        d = dict(row)
        for field in ("tags", "entities"):
            if field in d and isinstance(d[field], str):
                try:
                    d[field] = json.loads(d[field])
                except (json.JSONDecodeError, TypeError):
                    d[field] = []
        return d
