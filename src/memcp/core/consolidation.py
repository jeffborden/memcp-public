"""Memory consolidation — detect and merge similar insights.

Finds groups of near-duplicate insights and merges them:
- Union tags and entities
- Keep highest importance
- Sum access_counts
- Re-point edges from deleted nodes to the kept node
"""

from __future__ import annotations

import re
from typing import Any

from memcp.config import get_config

IMPORTANCE_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _keyword_similarity(a: str, b: str) -> float:
    """Compute Jaccard similarity between two texts using word tokens."""
    tokens_a = set(re.findall(r"\w+", a.lower()))
    tokens_b = set(re.findall(r"\w+", b.lower()))
    if not tokens_a or not tokens_b:
        return 0.0
    intersection = tokens_a & tokens_b
    union = tokens_a | tokens_b
    return len(intersection) / len(union)


def _embedding_similarity(texts: list[str]) -> list[list[float]] | None:
    """Compute pairwise cosine similarity matrix using embeddings. Returns None if unavailable."""
    try:
        from memcp.core.embeddings import get_provider

        provider = get_provider()
        if provider is None:
            return None

        import numpy as np

        vectors = provider.embed_batch(texts)
        arr = np.array(vectors, dtype=np.float32)
        norms = np.linalg.norm(arr, axis=1, keepdims=True)
        norms = np.where(norms == 0, 1, norms)
        normalized = arr / norms
        sim_matrix = (normalized @ normalized.T).tolist()
        return sim_matrix
    except (ImportError, Exception):
        return None


def find_similar_groups(
    threshold: float = 0.0,
    project: str = "",
    limit: int = 20,
) -> list[list[dict[str, Any]]]:
    """Find groups of similar insights above the threshold.

    Returns groups sorted by size (largest first).
    Each group is a list of insight dicts.
    """
    config = get_config()
    if threshold <= 0:
        threshold = config.consolidation_threshold

    from memcp.core.memory import _ensure_graph_migrated

    graph = _ensure_graph_migrated()
    try:
        scope = "all" if not project else "project"
        all_nodes = graph.query(
            query="",
            limit=10000,
            project=project,
            scope=scope,
        )
        if len(all_nodes) < 2:
            return []

        texts = [n.get("content", "") for n in all_nodes]
        sim_matrix = _embedding_similarity(texts)

        # Build adjacency: which pairs are similar enough?
        similar_pairs: list[tuple[int, int, float]] = []
        for i in range(len(all_nodes)):
            for j in range(i + 1, len(all_nodes)):
                if sim_matrix is not None:
                    sim = sim_matrix[i][j]
                else:
                    sim = _keyword_similarity(texts[i], texts[j])
                if sim >= threshold:
                    similar_pairs.append((i, j, sim))

        # Union-Find to group similar insights
        parent = list(range(len(all_nodes)))

        def find(x: int) -> int:
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for i, j, _ in similar_pairs:
            union(i, j)

        # Collect groups
        groups_map: dict[int, list[int]] = {}
        for idx in range(len(all_nodes)):
            root = find(idx)
            groups_map.setdefault(root, []).append(idx)

        # Filter to groups with 2+ members, sort by size desc
        groups = [
            [all_nodes[i] for i in indices] for indices in groups_map.values() if len(indices) >= 2
        ]
        groups.sort(key=lambda g: -len(g))
        return groups[:limit]
    finally:
        graph.close()


def consolidate(
    threshold: float = 0.0,
    project: str = "",
    limit: int = 20,
) -> dict[str, Any]:
    """Find similar insight groups and merge each one.

    Returns a summary with the total number of groups merged and insights deleted.
    Only bumps the revision counter if at least one merge actually happened.
    """
    groups = find_similar_groups(threshold=threshold, project=project, limit=limit)

    merged_count = 0
    deleted_count = 0
    results = []

    for group in groups:
        group_ids = [n["id"] for n in group]
        result = merge_group(group_ids)
        if result.get("status") == "ok":
            merged_count += 1
            deleted_count += result.get("merged_count", 0)
            results.append(result)

    if merged_count > 0:
        from memcp.core.graph import GraphMemory

        graph = GraphMemory()
        try:
            from memcp.core.revision import bump_revision, invalidate_index

            bump_revision(graph._get_conn())
            # Consolidate destructively deletes source insights; surviving
            # nodes' semantic top-K may change — invalidate edges.
            invalidate_index(graph._get_conn(), "edges")
            graph._get_conn().commit()
        finally:
            graph.close()

    return {
        "status": "ok",
        "groups_merged": merged_count,
        "insights_deleted": deleted_count,
        "results": results,
    }


def merge_group(
    group_ids: list[str],
    keep_id: str = "",
    merged_content: str = "",
) -> dict[str, Any]:
    """Merge a group of insights into one.

    - Keeps the insight with keep_id (or the most accessed one)
    - Unions tags and entities
    - Keeps highest importance
    - Sums access_counts
    - Re-points edges from deleted nodes to kept node
    - Deletes the rest
    """
    from memcp.core.memory import _ensure_graph_migrated

    graph = _ensure_graph_migrated()
    try:
        nodes = []
        for nid in group_ids:
            node = graph.get_node(nid)
            if node:
                nodes.append(node)

        if len(nodes) < 2:
            return {"status": "error", "message": "Need at least 2 valid insights to merge"}

        # Select which node to keep
        if keep_id and any(n["id"] == keep_id for n in nodes):
            keeper = next(n for n in nodes if n["id"] == keep_id)
        else:
            keeper = max(nodes, key=lambda n: n.get("access_count", 0))

        others = [n for n in nodes if n["id"] != keeper["id"]]

        # Merge metadata
        all_tags: set[str] = set()
        all_entities: set[str] = set()
        total_access = 0
        best_importance = "low"

        for n in nodes:
            for t in n.get("tags", []):
                all_tags.add(t)
            for e in n.get("entities", []):
                all_entities.add(e)
            total_access += n.get("access_count", 0)
            node_imp = IMPORTANCE_ORDER.get(n.get("importance", "low"), 0)
            if node_imp > IMPORTANCE_ORDER.get(best_importance, 0):
                best_importance = n["importance"]

        merged_content = merged_content.strip()
        member_ids = [n["id"] for n in nodes]

        if merged_content:
            # Content changed → mint a NEW immutable node and tombstone EVERY
            # group member (incl. the old keeper). A raw `UPDATE nodes SET
            # content` under an existing id breaks §2 immutability and, under
            # the additive union, causes durable cross-machine content
            # divergence (a stale peer keeps the old content). "New memory =
            # new id." See spec §3.10.
            from datetime import datetime, timezone

            from memcp.core.fileutil import insight_id

            now = datetime.now(timezone.utc)
            new_id = insight_id(merged_content, now.isoformat())
            graph.store(
                {
                    "id": new_id,
                    "content": merged_content,
                    "summary": keeper.get("summary", ""),
                    "category": keeper.get("category", "general"),
                    "importance": best_importance,
                    "tags": sorted(all_tags),
                    "entities": sorted(all_entities),
                    "project": keeper.get("project", "default"),
                    "session": keeper.get("session", ""),
                    "access_count": total_access,
                    "created_at": now.isoformat(),
                }
            )
            for mid in member_ids:
                graph.delete_node(mid)  # tombstones each member
            kept_id = new_id
            deleted_ids = member_ids
        else:
            # No content change → keep the keeper, union metadata onto it via
            # the allow-listed update path, tombstone the non-keepers.
            graph.update_node(
                keeper["id"],
                {
                    "tags": sorted(all_tags),
                    "entities": sorted(all_entities),
                    "access_count": total_access,
                    "importance": best_importance,
                },
            )
            deleted_ids = [n["id"] for n in others]
            for did in deleted_ids:
                graph.delete_node(did)  # tombstones each non-keeper
            kept_id = keeper["id"]

        from memcp.core.revision import bump_revision, invalidate_index

        conn = graph._get_conn()
        bump_revision(conn)
        invalidate_index(conn, "edges")
        conn.commit()

        return {
            "status": "ok",
            "kept_id": kept_id,
            "merged_count": len(others),
            "deleted_ids": deleted_ids,
            "tags": sorted(all_tags),
            "entities": sorted(all_entities),
            "importance": best_importance,
        }
    finally:
        graph.close()
