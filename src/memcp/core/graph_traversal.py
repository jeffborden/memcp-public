"""GraphTraversal — query, intent detection, and graph traversal.

Handles query routing, intent-aware ranking, and related-node traversal.
Extracted from GraphMemory.
"""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING, Any

from memcp.core.errors import InsightNotFoundError

if TYPE_CHECKING:
    from memcp.core.edge_manager import EdgeManager
    from memcp.core.node_store import NodeStore


class GraphTraversal:
    """Query routing, intent detection, and graph traversal."""

    def __init__(self, node_store: NodeStore, edge_manager: EdgeManager) -> None:
        self._node_store = node_store
        self._edge_manager = edge_manager

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
        use_edges: bool = True,
    ) -> list[dict[str, Any]]:
        """Query nodes with intent-aware graph traversal.

        Set use_edges=False to use keyword-only scoring against the graph's
        node set (no edge boost). Useful for A/B comparison of edge value.
        """
        conn = self._node_store._get_conn()

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

        # Archived rows are a synced soft-state (§3.5) — never surface them.
        conditions.append("archived_at IS NULL")

        where = " AND ".join(conditions) if conditions else "1=1"
        rows = conn.execute(
            f"SELECT * FROM nodes WHERE {where} ORDER BY created_at DESC",  # noqa: S608
            params,
        ).fetchall()

        nodes = [self._node_store._row_to_dict(r) for r in rows]

        if query.strip():
            intent = self._detect_intent(query)
            nodes = self._rank_by_intent(query, nodes, intent, limit, use_edges=use_edges)
        else:
            nodes = nodes[:limit]

        # Hebbian: strengthen edges between co-retrieved nodes
        # Skip when use_edges=False to avoid side-effects during A/B testing
        if use_edges and len(nodes) >= 2:
            from memcp.config import get_config

            config = get_config()
            if config.hebbian_enabled:
                result_ids = [n["id"] for n in nodes[:10]]
                self._edge_manager.strengthen_co_retrieved(result_ids, config.hebbian_boost)
                # Lazy edge decay (rate-limited to once per hour)
                self._edge_manager.decay_stale_edges(
                    config.edge_decay_half_life, config.edge_min_weight
                )

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
        """Traverse graph from a node, optionally filtering by edge type."""
        center = self._node_store.get_node(insight_id)
        if center is None:
            raise InsightNotFoundError(f"Insight {insight_id!r} not found")

        visited: set[str] = {insight_id}
        related_nodes: list[dict[str, Any]] = []
        related_edges: list[dict[str, Any]] = []

        frontier = [insight_id]
        for _d in range(depth):
            next_frontier: list[str] = []
            for node_id in frontier:
                edges = self._edge_manager.get_edges(node_id, edge_type)
                for edge in edges:
                    other_id = (
                        edge["target_id"] if edge["source_id"] == node_id else edge["source_id"]
                    )
                    if other_id not in visited:
                        visited.add(other_id)
                        node = self._node_store.get_node(other_id)
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
        conn = self._node_store._get_conn()

        if project:
            node_count = conn.execute(
                "SELECT COUNT(*) FROM nodes WHERE project = ?", (project,)
            ).fetchone()[0]
        else:
            node_count = conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0]

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
        use_edges: bool = True,
    ) -> list[dict[str, Any]]:
        """Rank nodes by combining keyword match with intent-weighted edge scores.

        When use_edges=False, ranks by keyword score only (no edge boost,
        no Hebbian side-effects). Same candidate set from graph.db either way.
        """
        query_tokens = set(re.findall(r"\w+", query.lower()))
        if not query_tokens:
            return nodes[:limit]

        # use_edges=False (use_graph=False) is the back-compat off-switch: it
        # disables BOTH the edge boost and the kind demotion. Otherwise each is
        # gated independently by config. When the edge boost is off, the two
        # per-node COUNT(*) FROM edges queries in _compute_edge_boost must not
        # run at all — that absence is the 8.5x p50 latency win.
        from memcp.config import get_config

        config = get_config()
        edge_boost_on = use_edges and config.edge_boost_enabled
        kind_weight_on = use_edges and config.kind_weight_enabled
        semantic_on = use_edges and config.semantic_recall_enabled

        # Embed the query ONCE and sweep it against the stored node embeddings.
        # semantic_scores is None when the provider degraded (P4) — we then fall
        # back to the exact keyword path (no semantic term, keyword-overlap gate
        # unchanged), so off / degraded scores are bit-identical to today's.
        semantic_scores: dict[str, float] | None = None
        if semantic_on:
            from memcp.core.semantic_recall import compute_semantic_scores

            semantic_scores = compute_semantic_scores(query, [n["id"] for n in nodes])
        semantic_active = semantic_scores is not None
        sem_weight = config.semantic_recall_weight

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
            # With semantic active, a zero-keyword-overlap node is still a
            # candidate if it has positive semantic similarity — that absence of
            # a recency/keyword gate is what bridges abstract phrasings (P3).
            sem = semantic_scores.get(node["id"], 0.0) if semantic_active else 0.0
            if not overlap and sem <= 0.0:
                continue

            keyword_score = len(overlap) / len(query_tokens) if overlap else 0.0
            if semantic_active:
                keyword_score = (1.0 - sem_weight) * keyword_score + sem_weight * sem

            if edge_boost_on:
                edge_boost = self._compute_edge_boost(
                    node["id"],
                    intent,
                    node.get("token_count", 100),
                )
                total_score = keyword_score * 0.7 + edge_boost * 0.3
            else:
                total_score = keyword_score

            # Kind: demotion is now an INDEPENDENT multiplicative factor on the
            # final relevance score — no longer tied to (and zeroed with) the
            # edge term. Demotes operational/pointer/episode content beneath
            # durable kb knowledge regardless of edge counts.
            if kind_weight_on:
                total_score *= self._kind_weight(node.get("tags", []))

            # Apply feedback score boost/penalty
            feedback_score = node.get("feedback_score", 0.0) or 0.0
            total_score *= 1 + feedback_score * 0.3

            scored.append((total_score, node))

        scored.sort(key=lambda x: -x[0])
        return [node for _, node in scored[:limit]]

    def _compute_edge_boost(self, node_id: str, intent: str, token_count: int = 100) -> float:
        """Compute edge-based boost for a given intent.

        Normalizes edge density by token count so long documents (session
        handoffs) don't get systematically boosted over concise insights.
        """
        intent_to_type = {
            "what": "semantic",
            "when": "temporal",
            "why": "causal",
            "who": "entity",
        }
        primary_type = intent_to_type.get(intent, "semantic")

        conn = self._node_store._get_conn()
        primary_count = conn.execute(
            """SELECT COUNT(*) FROM edges
               WHERE (source_id = ? OR target_id = ?) AND edge_type = ?""",
            (node_id, node_id, primary_type),
        ).fetchone()[0]

        total_count = conn.execute(
            "SELECT COUNT(*) FROM edges WHERE source_id = ? OR target_id = ?",
            (node_id, node_id),
        ).fetchone()[0]

        if total_count == 0:
            return 0.0

        # Proportion of edges that match the intent type (0-1)
        proportion = primary_count / max(1, total_count)
        # Edge density normalized by content length (edges per 100 tokens, capped)
        # Prevents long documents from dominating via raw edge count
        density = min(1.0, (primary_count / max(50, token_count)) * 5.0)

        return min(1.0, proportion * 0.6 + density * 0.4)

    @staticmethod
    def _kind_weight(tags: list[str]) -> float:
        """Weight factor based on kind: tag — durable knowledge ranks higher.

        Applied as an independent multiplicative demotion on the final score
        (Phase 2 Item 1), so untagged/kb content is neutral (1.0x) and only
        operational/pointer/episode content is demoted.
        """
        for tag in tags:
            if tag == "kind:kb":
                return 1.0
            if tag == "kind:op":
                return 0.5
            if tag == "kind:pointer":
                return 0.3
            if tag == "kind:episode":
                return 0.2
        # No kind tag — neutral, same as kb.
        return 1.0
