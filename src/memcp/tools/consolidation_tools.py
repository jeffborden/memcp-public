"""Consolidation tools — preview and merge similar insights."""

from __future__ import annotations

import json
from typing import Any

from memcp.core.consolidation import (
    claim_keeper,
    find_similar_groups,
    justified_subsets,
    merge_group,
)
from memcp.core.errors import MemCPError


def do_consolidation_preview(
    threshold: float = 0.0,
    limit: int = 20,
    project: str = "",
) -> str:
    """Preview similar-insight groups, split by the supersession gate. Dry-run.

    Similarity NOMINATES; the gate decides. Each nominated group is split
    into ``mergeable`` subsets (members explicitly claim to supersede one
    another — safe to pass to memcp_consolidate, with the suggested keeper)
    and ``unjustified`` members (similar prose, no supersession claim —
    transitive-closure artifacts and independent facts; do not merge).
    """
    try:
        groups = find_similar_groups(threshold=threshold, project=project, limit=limit)

        def _head(n: dict[str, Any]) -> dict[str, Any]:
            return {
                "id": n["id"],
                "content": n["content"][:100],
                "importance": n.get("importance", "medium"),
                "access_count": n.get("access_count", 0),
            }

        mergeable: list[dict[str, Any]] = []
        unjustified: list[dict[str, Any]] = []
        for group in groups:
            safe, leftover = justified_subsets(group)
            for component in safe:
                keep = claim_keeper(component)
                mergeable.append(
                    {
                        "count": len(component),
                        "suggested_keep_id": keep["id"],
                        "insights": [_head(n) for n in component],
                    }
                )
            if leftover:
                unjustified.append(
                    {"count": len(leftover), "insights": [_head(n) for n in leftover]}
                )

        result: dict[str, Any] = {
            "status": "ok",
            "groups_found": len(groups),
            "mergeable": mergeable,
            "unjustified": unjustified,
            "note": (
                "mergeable = members explicitly claim supersession (id cited "
                "after a supersession verb, or a blanket 'supersedes all "
                "earlier <lane> pointers' the older member's text matches). "
                "unjustified = similar prose with no such claim — do not merge."
            ),
        }
        return json.dumps(result, indent=2, default=str)
    except MemCPError as exc:
        return json.dumps({"status": "error", "message": str(exc)}, indent=2)


def do_consolidate(
    group_ids: str,
    keep_id: str = "",
    merged_content: str = "",
) -> str:
    """Merge a group of similar insights into one."""
    try:
        ids = [s.strip() for s in group_ids.split(",") if s.strip()]
        if len(ids) < 2:
            return json.dumps(
                {"status": "error", "message": "Need at least 2 comma-separated insight IDs"},
                indent=2,
            )
        result = merge_group(ids, keep_id=keep_id, merged_content=merged_content)
        return json.dumps(result, indent=2, default=str)
    except MemCPError as exc:
        return json.dumps({"status": "error", "message": str(exc)}, indent=2)
