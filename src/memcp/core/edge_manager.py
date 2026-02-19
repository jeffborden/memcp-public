"""EdgeManager — auto-generate and query edges between nodes.

Handles all 4 edge types: temporal, entity, semantic, causal.
Extracted from GraphMemory.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from memcp.core.node_store import NodeStore

# Patterns that indicate causal relationships
_CAUSAL_PATTERNS = re.compile(
    r"\b(?:because|therefore|due to|caused by|as a result|decided to|"
    r"chosen because|so that|in order to|leads to|results in)\b",
    re.IGNORECASE,
)


class EdgeManager:
    """Generates and queries edges between graph nodes."""

    def __init__(self, node_store: NodeStore) -> None:
        self._node_store = node_store

    # ── Edge generation ───────────────────────────────────────────

    def generate_edges(self, insight: dict[str, Any]) -> None:
        """Auto-generate all 4 edge types for a new insight."""
        self._generate_temporal_edges(insight)
        self._generate_entity_edges(insight)
        self._generate_semantic_edges(insight)
        self._generate_causal_edges(insight)

    def _generate_temporal_edges(self, insight: dict[str, Any]) -> None:
        """Link to insights created within 30 minutes."""
        conn = self._node_store._get_conn()
        now_str = insight.get("created_at", "")
        if not now_str:
            return

        try:
            now = datetime.fromisoformat(now_str)
            if now.tzinfo is None:
                now = now.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return

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
        conn = self._node_store._get_conn()
        entities = insight.get("entities", [])
        if not entities:
            return

        for entity in entities:
            entity_lower = entity.lower()
            # Use inverted entity index for O(matches) lookup instead of O(N) scan
            rows = conn.execute(
                "SELECT node_id FROM entity_index WHERE entity = ? AND node_id != ?",
                (entity_lower, insight["id"]),
            ).fetchall()

            for row in rows:
                self._add_edge(
                    insight["id"],
                    row["node_id"],
                    "entity",
                    1.0,
                    metadata={"entity": entity},
                )

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

        conn = self._node_store._get_conn()

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

        conn = self._node_store._get_conn()
        rows = conn.execute(
            """SELECT id, content FROM nodes
               WHERE id != ? AND project = ?
               ORDER BY created_at DESC LIMIT 10""",
            (insight["id"], insight.get("project", "default")),
        ).fetchall()

        content_lower = content.lower()
        for row in rows:
            other_lower = row["content"].lower()
            other_tokens = set(re.findall(r"\w+", other_lower))
            content_tokens = set(re.findall(r"\w+", content_lower))
            overlap = other_tokens & content_tokens
            if len(overlap) >= 3:
                score = len(overlap) / max(len(other_tokens), len(content_tokens))
                if score >= 0.15:
                    self._add_edge(insight["id"], row["id"], "causal", score)
                    break

    def _add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        weight: float = 1.0,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """Insert an edge, ignoring duplicates."""
        conn = self._node_store._get_conn()
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

    # ── Edge queries ──────────────────────────────────────────────

    def get_edges(self, node_id: str, edge_type: str = "") -> list[dict[str, Any]]:
        """Get all edges connected to a node, optionally filtered by type."""
        conn = self._node_store._get_conn()
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
