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

# ── Supersession gate ─────────────────────────────────────────────────
#
# Similarity may NOMINATE a merge; it can never justify one. Single-linkage
# union-find is transitive by construction (A~B, B~C, C~D chains A..D even
# when A and D are unrelated) — measured on a real 196-insight store, the
# default threshold grouped 150 insights into ONE blob, so no threshold
# fixes it alone. A member is only mergeable when another member EXPLICITLY
# claims to supersede it:
#
#   * by citing its id within CITE_WINDOW chars after a supersession verb
#     ("supersedes 0dbb2e34"), or
#   * by a blanket claim naming a lane ("supersedes all earlier <lane>
#     pointers") where the older member's text carries that exact lane name.
#
# The lane comes from the claimant's supersession PHRASE, never from tokens
# floating in the prose: an earlier gate that treated any shared snake_case
# token as proof of a common lane merged a standing build-gate note into an
# unrelated pointer chain — and its self-test passed while broken because
# the fixtures were too kind. The rejected fixtures in
# tests/unit/test_consolidation_gate.py are the regression net for that.

_BLANKET_LANE = re.compile(
    r"supersede[sd]?\s+(?:all\s+)?(?:the\s+)?earlier\s+([a-z0-9][a-z0-9_-]{5,})\s+pointer",
    re.I,
)
_BLANKET_BARE = re.compile(r"supersede[sd]?\s+(?:all\s+)?(?:the\s+)?earlier\s+ones\b", re.I)
_ID_CITE = re.compile(r"\b([0-9a-f]{6,12})\b")
# A cited id only counts as a supersession claim when it sits shortly after a
# supersession verb — pointers also cite ids merely to INDEX other entries.
_SUPERSEDE_WORD = re.compile(r"supersede[sd]?|replaces?\b|obsoletes?\b", re.I)
_CITE_WINDOW = 120
_MIN_ID_PREFIX = 6


def _node_ts(node: dict[str, Any]) -> str:
    return node.get("created_at") or node.get("timestamp") or ""


def _blanket_lanes(text: str) -> set[str]:
    """Lane names this insight explicitly claims to supersede."""
    return {m.lower() for m in _BLANKET_LANE.findall(text)}


def _superseding_cites(text: str) -> set[str]:
    """Id prefixes cited as being SUPERSEDED, not merely cross-referenced."""
    low = text.lower()
    spans = [m.end() for m in _SUPERSEDE_WORD.finditer(low)]
    if not spans:
        return set()
    out: set[str] = set()
    for m in _ID_CITE.finditer(low):
        if len(m.group(1)) < _MIN_ID_PREFIX:
            continue
        if any(0 <= m.start() - end <= _CITE_WINDOW for end in spans):
            out.add(m.group(1))
    return out


def supersession_edges(group: list[dict[str, Any]]) -> list[tuple[str, str, str]]:
    """Edges (superseded_id, superseding_id, reason) — explicit claims only.

    Direction comes from the CLAIM, not from timestamps: whoever names the
    other is the survivor. Timestamps are only a tiebreak, because
    created_at ordering has been observed to put a superseded UPDATE newer
    than the CLOSED entry that replaced it.
    """
    edges: list[tuple[str, str, str]] = []

    for claimant in group:
        c_text = claimant.get("content", "")
        c_id = claimant.get("id", "")
        cited = _superseding_cites(c_text)
        claimed_lanes = _blanket_lanes(c_text)
        bare_blanket = bool(_BLANKET_BARE.search(c_text))

        for other in group:
            o_id = other.get("id", "")
            if o_id == c_id:
                continue
            o_text = other.get("content", "")

            # Strongest: the claimant cites the other's id.
            if any(o_id.startswith(c) for c in cited):
                edges.append((o_id, c_id, "names the id"))
                continue

            # Blanket claim naming a lane: the other qualifies only if THAT
            # lane name appears in its text.
            hit = next((lane for lane in claimed_lanes if lane in o_text.lower()), None)
            if hit:
                edges.append((o_id, c_id, f"blanket supersession of lane `{hit}`"))
                continue

            # "supersedes all earlier ones" names no lane, so it only counts
            # when the OTHER member declares the lane and the claimant
            # mentions that exact lane name.
            if bare_blanket:
                for lane in _blanket_lanes(o_text):
                    if lane in c_text.lower():
                        edges.append(
                            (
                                o_id,
                                c_id,
                                f"blanket supersession of lane `{lane}` "
                                f"(lane named by the older entry)",
                            )
                        )
                        break

    return edges


def justified_subsets(
    group: list[dict[str, Any]],
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """Split a similarity-nominated group into (mergeable subsets, leftovers).

    A subset is mergeable only when supersession claims connect its members.
    Leftovers are nominated-but-unjustified: similar prose with no member
    claiming to supersede another — transitive-closure artifacts and
    independent facts about one subject both land there.
    """
    edges = supersession_edges(group)
    if not edges:
        return [], list(group)

    by_id = {n.get("id", ""): n for n in group}
    parent = {i: i for i in by_id}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for older, newer, _ in edges:
        if older in parent and newer in parent:
            ra, rb = find(older), find(newer)
            if ra != rb:
                parent[ra] = rb

    comps: dict[str, list[dict[str, Any]]] = {}
    for i in by_id:
        comps.setdefault(find(i), []).append(by_id[i])

    safe = [c for c in comps.values() if len(c) >= 2]
    safe_ids = {n.get("id") for c in safe for n in c}
    leftover = [n for n in group if n.get("id") not in safe_ids]
    return safe, leftover


def claim_keeper(component: list[dict[str, Any]]) -> dict[str, Any]:
    """The survivor is the member nothing else claims to supersede.

    NOT the newest by created_at — that has been observed to pick a
    superseded UPDATE over the CLOSED entry that replaced it. Timestamp is
    the tiebreak only when the claim graph is ambiguous.
    """
    superseded = {older for older, _, _ in supersession_edges(component)}
    survivors = [n for n in component if n.get("id") not in superseded]
    pool = survivors or component
    return sorted(pool, key=_node_ts, reverse=True)[0]


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
    """Find similar insight groups and merge the JUSTIFIED subsets of each.

    Similarity only nominates; each nominated group passes through the
    supersession gate (justified_subsets) and only subsets whose members
    explicitly claim to supersede one another are merged — with the claim
    sink as keeper. Nominated-but-unjustified members are reported in
    ``skipped_unjustified``, never merged.

    Returns a summary with the total number of groups merged and insights deleted.
    Only bumps the revision counter if at least one merge actually happened.
    """
    groups = find_similar_groups(threshold=threshold, project=project, limit=limit)

    merged_count = 0
    deleted_count = 0
    results = []
    skipped_unjustified = 0

    for group in groups:
        safe, leftover = justified_subsets(group)
        skipped_unjustified += len(leftover)
        for component in safe:
            keep = claim_keeper(component)
            group_ids = [n["id"] for n in component]
            result = merge_group(group_ids, keep_id=keep["id"])
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
        "skipped_unjustified": skipped_unjustified,
        "results": results,
    }


# Largest merge a single call will perform. Merging is destructive (source
# insights are deleted); the observed failure is a transitive-closure blob of
# 150 similar-ish insights arriving here as one "group". A genuine
# supersession chain is short; anything larger is almost certainly an
# artifact and must be split up deliberately, not merged in one shot.
MAX_MERGE_GROUP_SIZE = 8


def merge_group(
    group_ids: list[str],
    keep_id: str = "",
    merged_content: str = "",
) -> dict[str, Any]:
    """Merge a group of insights into one.

    - Refuses groups larger than MAX_MERGE_GROUP_SIZE (merge is destructive)
    - Keeps the insight with keep_id (or the most accessed one)
    - Unions tags and entities
    - Keeps highest importance
    - Sums access_counts
    - Re-points edges from deleted nodes to kept node
    - Deletes the rest
    """
    if len(group_ids) > MAX_MERGE_GROUP_SIZE:
        return {
            "status": "refused",
            "message": (
                f"Refusing to merge {len(group_ids)} insights in one group "
                f"(max {MAX_MERGE_GROUP_SIZE}). Merging deletes source insights, "
                f"and similarity grouping is transitive — oversized groups are "
                f"usually chained artifacts, not duplicates. Split the group "
                f"into explicit supersession chains and merge those."
            ),
        }

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
