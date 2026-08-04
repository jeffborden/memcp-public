"""Defaults that lied in production — each test reproduces the observed lie.

Three observed failures on the originating machine:

1. memcp_status reported project-scoped counts with nothing in the response
   saying so — a 196-vs-393 store split stayed invisible for two weeks
   because "total_insights: 196" read as the whole store.
2. memcp_recall's max_tokens=8000 silently returned 1-2 results on stores
   with token-heavy insights — no signal that a budget cut anything, so
   "2 results" read as "only 2 matches exist".
3. memcp_recall's scope="project" default hid ~half the store whenever
   writes had forked across project labels. The data dir is the real
   isolation boundary; within one store, projects are labels, so the
   honest default for reads is scope="all" (docs/design/default-scope.md).
"""

from __future__ import annotations

import json

from memcp.core.memory import memory_status, remember
from memcp.core.project import set_process_identity
from memcp.server import memcp_recall, memcp_status


class TestStatusNamesItsScope:
    def test_status_reports_scope_and_store_totals(self, isolated_data_dir):
        remember("alpha insight one about caching", project="proj-alpha")
        remember("alpha insight two about queues", project="proj-alpha")
        remember("beta insight one about billing", project="proj-beta")

        set_process_identity("proj-alpha", "proj-alpha_2026-01-01_001")
        status = memory_status()

        # The scoped count keeps its meaning...
        assert status["total_insights"] == 2
        # ...but the response must now SAY it is scoped and show the store.
        assert status["scope"] == {"project": "proj-alpha"}
        assert status["store"]["total_insights"] == 3
        assert status["store"]["by_project"] == {"proj-alpha": 2, "proj-beta": 1}

    def test_status_tool_carries_store_totals(self, isolated_data_dir):
        remember("alpha insight about deploys", project="proj-alpha")
        remember("beta insight about invoices", project="proj-beta")

        set_process_identity("proj-alpha", "proj-alpha_2026-01-01_001")
        payload = json.loads(memcp_status())

        assert payload["total_insights"] == 1
        assert payload["store"]["total_insights"] == 2

    def test_explicit_project_filter_still_scopes(self, isolated_data_dir):
        remember("alpha insight", project="proj-alpha")
        remember("beta insight", project="proj-beta")

        status = memory_status(project="proj-beta")
        assert status["total_insights"] == 1
        assert status["scope"] == {"project": "proj-beta"}
        assert status["store"]["total_insights"] == 2


class TestRecallNamesItsTruncation:
    async def test_token_budget_cut_is_reported(self, isolated_data_dir):
        # ~50 tokens each; a 60-token budget fits exactly one.
        for i in range(3):
            remember(f"insight {i}: " + ("database migration rollback plan " * 25))

        payload = json.loads(await memcp_recall(query="", max_tokens=60, scope="all"))

        assert payload["status"] == "ok"
        assert payload["count"] < 3, "budget did not truncate — test is void"
        trunc = payload.get("truncated")
        assert trunc, "truncation happened but the response does not say so"
        assert trunc["budget"] == 60
        assert trunc["returned"] == payload["count"]
        assert trunc["matched"] == 3
        assert "max_tokens" in payload["message"]

    async def test_no_truncation_no_notice(self, isolated_data_dir):
        remember("short insight about linting")
        payload = json.loads(await memcp_recall(query="", max_tokens=8000, scope="all"))
        assert payload["count"] == 1
        assert "truncated" not in payload


class TestRecallDefaultScopeIsAll:
    async def test_default_recall_sees_other_projects(self, isolated_data_dir):
        remember("alpha fact about redis eviction", project="proj-alpha")
        remember("beta fact about redis eviction", project="proj-beta")

        set_process_identity("proj-alpha", "proj-alpha_2026-01-01_001")
        payload = json.loads(await memcp_recall(query="redis eviction"))

        got = {i["project"] for i in payload["insights"]}
        assert got == {"proj-alpha", "proj-beta"}, (
            f"default scope hid part of the store: saw only {got}"
        )

    async def test_explicit_project_scope_still_narrows(self, isolated_data_dir):
        remember("alpha fact about redis", project="proj-alpha")
        remember("beta fact about redis", project="proj-beta")

        set_process_identity("proj-alpha", "proj-alpha_2026-01-01_001")
        payload = json.loads(await memcp_recall(query="redis", scope="project"))

        got = {i["project"] for i in payload["insights"]}
        assert got == {"proj-alpha"}
