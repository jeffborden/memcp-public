"""Item 1 — decouple kind-weight from the edge term + config-gated edge boost.

The pre-Phase-2 ranker only ever applied the kind: demotion as a multiplier on
the edge boost (`keyword*0.7 + edge_boost*kind_factor*0.3`), so with edge counts
zeroed the kind demotion was dead code (Arm C, byte-identical C==B). These tests
pin the decoupled contract:

  - kind factor is an independent multiplicative demotion on the FINAL score;
  - the edge boost is behind MEMCP_EDGE_BOOST and, when off, the per-node
    `COUNT(*) FROM edges` queries do not execute at all;
  - kind weighting is behind MEMCP_KIND_WEIGHT (default on);
  - use_edges=False (use_graph=False) disables BOTH, unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

import memcp.config as config_module
from memcp.core.fileutil import content_hash, estimate_tokens
from memcp.core.graph import GraphMemory


def _make_insight(content: str, tags: list[str], idx: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": content_hash(content + str(idx) + now.isoformat()),
        "content": content,
        "summary": "",
        "category": "general",
        "importance": "medium",
        "effective_importance": 0.5,
        "tags": tags,
        "entities": [],
        "project": "testproj",
        "session": "",
        "token_count": estimate_tokens(content),
        "access_count": 0,
        "last_accessed_at": None,
        "created_at": now.isoformat(),
    }


def _set_config(monkeypatch: pytest.MonkeyPatch, **env: str) -> None:
    """Set MEMCP_* env vars and force the config singleton to rebuild."""
    for key, val in env.items():
        monkeypatch.setenv(key, val)
    config_module._config = None


class TestDefaultConfig:
    """Test 3 — the flipped default (lands only after the Item 2 eval gate).

    Arm D passed the pre-registered rule (D nDCG not significantly worse than A
    at p=0.40; contamination 0.0 == A; p50 10.4ms within 2x of B's 10.1ms vs A's
    92.5ms), so the default flips: edge boost OFF, kind weight ON, out of the box.
    """

    def test_fresh_config_edge_boost_off_kind_weight_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # No MEMCP_EDGE_BOOST / MEMCP_KIND_WEIGHT in env — exercise the defaults.
        monkeypatch.delenv("MEMCP_EDGE_BOOST", raising=False)
        monkeypatch.delenv("MEMCP_KIND_WEIGHT", raising=False)
        config_module._config = None
        config = config_module.get_config()
        assert config.edge_boost_enabled is False
        assert config.kind_weight_enabled is True


class TestKindDemotionIndependentOfEdges:
    """Test 1 — kb outranks a higher-keyword pointer iff kind weight is on."""

    def _seed(self) -> GraphMemory:
        graph = GraphMemory(db_path=":memory:")
        # pointer node has the STRONGER raw-keyword match (all 3 query tokens);
        # kb node matches only 2 of 3. Without kind weighting the pointer wins.
        graph.store(_make_insight("graph database sqlite", ["kind:pointer"], 1))
        graph.store(_make_insight("graph database", ["kind:kb"], 2))
        return graph

    def test_kind_weight_on_promotes_kb(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config(
            monkeypatch,
            MEMCP_EDGE_BOOST="false",
            MEMCP_KIND_WEIGHT="true",
            MEMCP_HEBBIAN_ENABLED="false",
        )
        graph = self._seed()
        try:
            results = graph.query(query="graph database sqlite", scope="all")
            assert results[0]["tags"] == ["kind:kb"]
        finally:
            graph.close()

    def test_kind_weight_off_lets_pointer_win(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _set_config(
            monkeypatch,
            MEMCP_EDGE_BOOST="false",
            MEMCP_KIND_WEIGHT="false",
            MEMCP_HEBBIAN_ENABLED="false",
        )
        graph = self._seed()
        try:
            results = graph.query(query="graph database sqlite", scope="all")
            assert results[0]["tags"] == ["kind:pointer"]
        finally:
            graph.close()


class TestEdgeBoostOffSkipsCountQueries:
    """Test 2 — edge boost off → zero COUNT(*) FROM edges during recall."""

    def _seed_and_trace(self, monkeypatch: pytest.MonkeyPatch, edge_boost: str) -> int:
        _set_config(
            monkeypatch,
            MEMCP_EDGE_BOOST=edge_boost,
            MEMCP_KIND_WEIGHT="true",
            MEMCP_HEBBIAN_ENABLED="false",
        )
        graph = GraphMemory(db_path=":memory:")
        graph.store(_make_insight("graph database sqlite storage", ["kind:kb"], 1))
        graph.store(_make_insight("graph database sqlite backend", ["kind:kb"], 2))

        counts = {"n": 0}

        def _trace(sql: str) -> None:
            norm = " ".join(sql.lower().split())
            if "count(*) from edges" in norm:
                counts["n"] += 1

        graph._get_conn().set_trace_callback(_trace)
        try:
            graph.query(query="graph database sqlite", scope="all")
        finally:
            graph._get_conn().set_trace_callback(None)
            graph.close()
        return counts["n"]

    def test_edge_boost_off_runs_no_edge_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._seed_and_trace(monkeypatch, "false") == 0

    def test_edge_boost_on_runs_edge_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Sanity: the trace actually fires when the boost is on, so the
        # off-case assertion above is meaningful and not vacuous.
        assert self._seed_and_trace(monkeypatch, "true") > 0


class TestUseGraphFalseBackCompat:
    """Test 4 — use_edges=False disables both edge boost AND kind weight."""

    def test_both_disabled_pointer_wins_and_no_edge_counts(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # kind weight is configured ON, but use_edges=False must override it.
        _set_config(
            monkeypatch,
            MEMCP_EDGE_BOOST="true",
            MEMCP_KIND_WEIGHT="true",
            MEMCP_HEBBIAN_ENABLED="false",
        )
        graph = GraphMemory(db_path=":memory:")
        graph.store(_make_insight("graph database sqlite", ["kind:pointer"], 1))
        graph.store(_make_insight("graph database", ["kind:kb"], 2))

        counts = {"n": 0}

        def _trace(sql: str) -> None:
            if "count(*) from edges" in " ".join(sql.lower().split()):
                counts["n"] += 1

        graph._get_conn().set_trace_callback(_trace)
        try:
            results = graph.query(query="graph database sqlite", scope="all", use_edges=False)
            # keyword-only: pointer has the stronger raw match and is not demoted
            assert results[0]["tags"] == ["kind:pointer"]
            assert counts["n"] == 0
        finally:
            graph._get_conn().set_trace_callback(None)
            graph.close()
