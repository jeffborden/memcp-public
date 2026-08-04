"""Phase 4 Item 3 — blind theme backfill script.

The blind protocol is a HARD rule: the theme-generation prompt is built ONLY
from node content — it must never contain eval queries or query-derived
vocabulary. (The bake-off's 4/5 had author bias; the gate must measure the blind
version.) The backfill is resumable: a node with an already-valid (sha-matching)
theme is skipped, so a second run over unchanged content makes zero LLM calls.

Tests:
  5. blind-prompt guard: the prompt template (and a built prompt) contain none of
     the 5 eval-query strings from docs/eval/queries.json, and the built prompt
     is composed purely from node content.
  6. resumability: a second run with unchanged content makes zero LLM calls;
     changed-sha nodes re-theme.
"""

from __future__ import annotations

# The script lives under scripts/ — import it by path.
import importlib.util
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

import memcp.config as config_module
from memcp.core.fileutil import content_hash, estimate_tokens
from memcp.core.graph import GraphMemory
from memcp.core.theme_cache import content_sha, get_theme_cache, reset_theme_cache

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "theme_backfill.py"
_spec = importlib.util.spec_from_file_location("theme_backfill", _SCRIPT)
theme_backfill = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(theme_backfill)

_QUERIES_JSON = Path(__file__).resolve().parents[2] / "docs" / "eval" / "queries.json"
BRIDGING = ("q09", "q10", "q12", "q14", "q18")


def _bridging_query_strings() -> list[str]:
    if not _QUERIES_JSON.exists():
        pytest.skip("eval query fixture absent (excluded from the public repo)")
    queries = json.loads(_QUERIES_JSON.read_text())
    return [q["query"] for q in queries if q["id"] in BRIDGING]


# ── Test 5 — blind-prompt guard ───────────────────────────────────────────────
class TestBlindPromptGuard:
    def test_template_contains_no_eval_query(self) -> None:
        template = theme_backfill.PROMPT_TEMPLATE.lower()
        for qs in _bridging_query_strings():
            assert qs.lower() not in template

    def test_built_prompt_has_no_eval_query_and_only_node_content(self) -> None:
        nodes = [
            ("nodeA", "Drove a production rollout end to end under a launch deadline."),
            ("nodeB", "Chose Postgres over MySQL for the analytics workload."),
        ]
        prompt = theme_backfill.build_prompt(nodes)
        low = prompt.lower()
        # No eval query phrase leaks into the prompt.
        for qs in _bridging_query_strings():
            assert qs.lower() not in low
        # Built purely from node content: each node's id + content appears, and
        # nothing beyond the static template + the injected node text.
        for nid, content in nodes:
            assert nid in prompt
            assert content in prompt
        # Sanity: the substantive query vocabulary ("accessibility", "fork",
        # "workstream", "partnership") is absent — the prompt never names tasks.
        for word in ("accessibility", "workstream", "distinctive engineering"):
            assert word not in low


# ── Test 6 — resumability ─────────────────────────────────────────────────────
def _make_insight(content: str, tags: list[str], idx: int) -> dict:
    now = datetime.now(timezone.utc)
    return {
        "id": content_hash(content + str(idx) + now.isoformat()),
        "content": content,
        "summary": "",
        "category": "finding",
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


class CountingLLM:
    """Fake claude -p: counts calls, returns one theme line per node in batch."""

    def __init__(self) -> None:
        self.calls = 0
        self.themed_ids: list[str] = []

    def __call__(self, prompt: str, batch: list[tuple[str, str]]) -> dict[str, str]:
        self.calls += 1
        out = {}
        for nid, _content in batch:
            self.themed_ids.append(nid)
            out[nid] = f"theme for {nid}"
        return out


@pytest.fixture
def graph_env(tmp_path, monkeypatch):
    monkeypatch.setenv("MEMCP_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("MEMCP_HEBBIAN_ENABLED", "false")
    config_module._config = None
    reset_theme_cache()
    db_path = tmp_path / "graph.db"
    graph = GraphMemory(db_path=str(db_path))
    yield graph, db_path
    graph.close()
    config_module._config = None
    reset_theme_cache()


class TestResumability:
    def test_eligibility_skips_demoted_kinds(self, graph_env) -> None:
        graph, db_path = graph_env
        kb = _make_insight("durable knowledge body", ["kind:kb"], 1)
        ptr = _make_insight("session pointer body", ["kind:pointer"], 2)
        op = _make_insight("open task body", ["kind:op"], 3)
        ep = _make_insight("episode body", ["kind:episode"], 4)
        for n in (kb, ptr, op, ep):
            graph.store(n)
        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        eligible = {nid for nid, _ in theme_backfill.select_eligible_nodes(conn)}
        conn.close()
        assert kb["id"] in eligible
        assert ptr["id"] not in eligible
        assert op["id"] not in eligible
        assert ep["id"] not in eligible

    def test_second_run_zero_llm_calls(self, graph_env) -> None:
        graph, db_path = graph_env
        ids = []
        for i in range(3):
            n = _make_insight(f"durable knowledge body number {i}", ["kind:kb"], i)
            graph.store(n)
            ids.append(n["id"])

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cache = get_theme_cache()

        llm1 = CountingLLM()
        stats1 = theme_backfill.run_backfill(conn, cache, llm1, batch_size=20)
        assert stats1["themed"] == 3
        assert llm1.calls >= 1
        assert cache.count() == 3

        # Second run, unchanged content → all skipped, zero LLM calls.
        llm2 = CountingLLM()
        stats2 = theme_backfill.run_backfill(conn, cache, llm2, batch_size=20)
        assert llm2.calls == 0
        assert stats2["themed"] == 0
        assert stats2["skipped"] == 3
        conn.close()

    def test_changed_sha_rethemes_only_that_node(self, graph_env) -> None:
        graph, db_path = graph_env
        n = _make_insight("original durable body", ["kind:kb"], 1)
        graph.store(n)

        conn = sqlite3.connect(str(db_path))
        conn.row_factory = sqlite3.Row
        cache = get_theme_cache()

        llm1 = CountingLLM()
        theme_backfill.run_backfill(conn, cache, llm1, batch_size=20)
        assert llm1.calls == 1

        # Mutate the node's content in place (simulating an edit → new sha).
        conn.execute(
            "UPDATE nodes SET content = ? WHERE id = ?",
            ("edited durable body with new meaning", n["id"]),
        )
        conn.commit()

        llm2 = CountingLLM()
        stats = theme_backfill.run_backfill(conn, cache, llm2, batch_size=20)
        assert llm2.calls == 1  # re-themed the changed node
        assert stats["themed"] == 1
        # The new theme is keyed to the NEW sha.
        new_sha = content_sha("edited durable body with new meaning")
        assert cache.get_valid(n["id"], new_sha) is not None
        conn.close()
