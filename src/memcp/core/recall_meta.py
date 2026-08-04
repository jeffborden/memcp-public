"""Recall results that can say when a token budget cut them.

max_tokens=8000 silently returning 1-2 results on a token-heavy store read
as "only 2 matches exist" — the budget needs to announce itself. The budget
is applied deep in the backends (graph traversal / JSON fallback), so the
result list itself carries the metadata up to the tool layer.
"""

from __future__ import annotations

from typing import Any

from memcp.core.fileutil import estimate_tokens


class RecallResults(list):
    """A list of insights that optionally carries truncation metadata.

    ``truncation`` is None when nothing was cut; otherwise a dict with
    reason/budget/returned/matched/tokens_returned. Behaves exactly like a
    plain list everywhere else.
    """

    truncation: dict[str, Any] | None = None


def apply_token_budget(results: list[dict[str, Any]], max_tokens: int) -> RecallResults:
    """Trim *results* to *max_tokens*, recording what was cut.

    Same semantics as the loops this replaces: stop at the first insight
    that would exceed the budget (the first insight is always returned so a
    budget smaller than one insight still yields something).
    """
    out = RecallResults()
    tokens_used = 0
    for r in results:
        r_tokens = r.get("token_count") or estimate_tokens(r.get("content", ""))
        if tokens_used + r_tokens > max_tokens and out:
            break
        out.append(r)
        tokens_used += r_tokens

    if len(out) < len(results):
        out.truncation = {
            "reason": "max_tokens",
            "budget": max_tokens,
            "returned": len(out),
            "matched": len(results),
            "tokens_returned": tokens_used,
        }
    return out
