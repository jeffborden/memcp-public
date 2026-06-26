"""Tests for memcp.core.memory.grep — Direct Corpus Interaction over the insight store.

Oracle for the `memcp_grep` tool (SPEC-memcp_grep.md). grep is exact/regex/tag-conjunction
search with NO ranking and NO embeddings — the deterministic complement to the
similarity-first memcp_search/memcp_recall path. Jeff signed off on this oracle 2026-06-19.
"""

from __future__ import annotations

import pytest

from memcp.core.errors import ValidationError
from memcp.core.graph import GraphMemory
from memcp.core.memory import grep, remember


def _set_archived(insight_id: str) -> None:
    """Mark an insight archived in-band (set the archived_at column) — the synced
    posture Jeff runs in production. (Local-only archive_insight hard-deletes the
    row to a side-file, which is out of grep's nodes-table scope; see grep docstring.)"""
    graph = GraphMemory()
    try:
        graph.update_node(insight_id, {"archived_at": "2026-06-19T00:00:00+00:00"})
    finally:
        graph.close()


def _seed_triage() -> str:
    """Seed the canonical 'triage V2 won at 4.73' insight; return its id."""
    res = remember(
        "Triage Agent V2 grep-based component deep-dive won at 4.73/5.0 vs semantic methods",
        category="finding",
        importance="high",
        tags="kind:kb,triage-agent,dci",
        project="memcp",
    )
    return res["id"]


class TestLiteralKnownItem:
    """Test 1 — literal/fixed-string match, regex metacharacters escaped."""

    def test_fixed_string_finds_insight(self) -> None:
        tid = _seed_triage()
        results = grep("4.73", fixed_strings=True)
        ids = [r["id"] for r in results]
        assert tid in ids

    def test_fixed_string_escapes_metacharacters(self) -> None:
        # "4.73" as a literal must NOT match "4x73" (the '.' is escaped, not "any char").
        remember("score was 4x73 once", category="general", tags="kind:kb")
        tid = _seed_triage()
        results = grep("4.73", fixed_strings=True)
        ids = [r["id"] for r in results]
        assert tid in ids
        assert all("4x73" not in m["snippet"] for r in results for m in r["matches"])


class TestTagConjunction:
    """Test 2 — tags_all is a boolean AND over tags."""

    def test_requires_all_tags(self) -> None:
        both = remember("has both tags", category="general", tags="kind:kb,triage-agent")["id"]
        remember("only kb", category="general", tags="kind:kb")
        remember("only triage", category="general", tags="triage-agent")
        results = grep(".", tags_all=["kind:kb", "triage-agent"])
        ids = [r["id"] for r in results]
        assert ids == [both]


class TestRegexAndCase:
    """Test 3 — regex matching and case-insensitivity toggle."""

    def test_regex_score_shape(self) -> None:
        tid = _seed_triage()
        results = grep(r"\b4\.\d{2}\b")
        assert tid in [r["id"] for r in results]

    def test_case_insensitive_default(self) -> None:
        remember("The TRIAGE pipeline", category="general", tags="kind:kb")
        results = grep("triage", ignore_case=True)
        assert len(results) >= 1

    def test_case_sensitive_misses(self) -> None:
        remember("The TRIAGE pipeline", category="general", tags="kind:kb")
        results = grep("triage", ignore_case=False)
        assert all("TRIAGE" not in m["snippet"] or "triage" in m["snippet"]
                   for r in results for m in r["matches"])
        # Lowercase 'triage' does not exist; case-sensitive search returns nothing here.
        assert results == []


class TestNegative:
    """Test 4 — no hits returns [], not an error."""

    def test_no_match_returns_empty(self) -> None:
        _seed_triage()
        assert grep("zzz_no_such_token_zzz") == []


class TestDeterminism:
    """Test 5 — same args produce identical ordering, sorted by (created_at, id)."""

    def test_stable_ordering(self) -> None:
        for i in range(5):
            remember(f"determinism token_{i}", category="general", tags="kind:kb")
        first = [r["id"] for r in grep("determinism")]
        second = [r["id"] for r in grep("determinism")]
        assert first == second
        # Sorted ascending by created_at then id.
        rows = grep("determinism")
        keys = [(r["created_at"], r["id"]) for r in rows]
        assert keys == sorted(keys)


class TestArchived:
    """Test 6 — archived excluded by default, included with include_archived=True."""

    def test_archived_excluded_by_default(self) -> None:
        aid = remember("archived archtoken here", category="general", tags="kind:kb")["id"]
        _set_archived(aid)
        assert grep("archtoken") == []

    def test_archived_included_when_requested(self) -> None:
        aid = remember("archived archtoken here", category="general", tags="kind:kb")["id"]
        _set_archived(aid)
        results = grep("archtoken", include_archived=True)
        assert aid in [r["id"] for r in results]


class TestFieldScoping:
    """Test 7 — a term only in summary is found with fields=['summary'] but not ['content']."""

    def test_summary_only_term(self) -> None:
        sid = remember(
            "body text without the special word",
            category="general",
            tags="kind:kb",
            summary="this summary holds uniquesummaryword",
        )["id"]
        in_summary = grep("uniquesummaryword", fields=["summary"])
        in_content = grep("uniquesummaryword", fields=["content"])
        assert sid in [r["id"] for r in in_summary]
        assert in_content == []


class TestInvalidRegex:
    """Test 8 (added) — malformed regex raises a structured error, not a stack trace."""

    def test_bad_pattern_raises_validation_error(self) -> None:
        with pytest.raises(ValidationError):
            grep("[")


class TestColumnFilters:
    """Test 9 (added) — project / category / importance filter exactly."""

    def test_project_filter(self) -> None:
        a = remember("colfilter token", category="general", tags="kind:kb", project="memcp")["id"]
        remember("colfilter token", category="general", tags="kind:kb", project="other")
        results = grep("colfilter", project="memcp")
        assert [r["id"] for r in results] == [a]

    def test_category_filter(self) -> None:
        a = remember("catfilter token", category="decision", tags="kind:kb")["id"]
        remember("catfilter token", category="general", tags="kind:kb")
        results = grep("catfilter", category="decision")
        assert [r["id"] for r in results] == [a]

    def test_importance_filter(self) -> None:
        a = remember(
            "impfilter token", category="general", importance="critical", tags="kind:kb"
        )["id"]
        remember("impfilter token", category="general", importance="low", tags="kind:kb")
        results = grep("impfilter", importance="critical")
        assert [r["id"] for r in results] == [a]
