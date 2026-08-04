"""Backlog rec #5 — the 16 approved oracle tests (PROPOSED-TEST-LIST.md, signed
off by Jeff 2026-06-10).

Covers:
  - Patch 01: default max_tokens=8000 on memcp_recall / memcp_search (tests 1-5)
  - Patch 02: memcp_get(insight_id) with prefix resolution (tests 6-10)
  - Item 3:   memcp_remember metadata integrity + keyword passing + dedup (11-13)
  - Item 5:   pending-writes queue + SessionStart replay surface + guards (14-16)
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from memcp.server import (
    memcp_get,
    memcp_recall,
    memcp_remember,
    memcp_search,
)

# claude-hooks lives in a sibling repo; tests 14-16 exercise its pending_queue
# module and session_start / guard hooks directly.
CLAUDE_HOOKS = Path("~/projects/claude-hooks/memcp").expanduser()


async def _seed_large_corpus(n: int, chars: int) -> list[str]:
    """Save n insights each ~`chars` long (so each is ~chars/4 tokens). Returns ids."""
    ids = []
    for i in range(n):
        # Distinct, query-matchable, large body.
        body = f"bigcorpus marker{i} " + ("lorem ipsum dolor sit amet " * (chars // 28))
        result = json.loads(await memcp_remember(content=body, category="fact", tags="bigcorpus"))
        ids.append(result["id"])
    return ids


# ── Patch 01 — default max_tokens on memcp_recall / memcp_search ──────────────


class TestPatch01RecallDefaultMaxTokens:
    async def test_1_recall_default_caps_large_corpus(self, isolated_data_dir) -> None:
        # 10 insights × ~1000 tokens = ~10000 tokens; default budget is 8000.
        await _seed_large_corpus(n=10, chars=4000)
        result = json.loads(await memcp_recall(query="bigcorpus", scope="all", limit=20))
        assert result["status"] == "ok"
        # The summed token_count of returned insights must respect the 8000 budget.
        assert result["total_tokens"] <= 8000
        # And the cap actually bit — fewer than all 10 came back.
        assert result["count"] < 10

    async def test_2_recall_max_tokens_zero_is_unlimited(self, isolated_data_dir) -> None:
        await _seed_large_corpus(n=10, chars=4000)
        result = json.loads(
            await memcp_recall(query="bigcorpus", scope="all", limit=20, max_tokens=0)
        )
        assert result["status"] == "ok"
        # Opt-out sentinel preserved: all 10 returned, well over the 8000 budget.
        assert result["count"] == 10
        assert result["total_tokens"] > 8000

    async def test_3_recall_explicit_small_budget(self, isolated_data_dir) -> None:
        await _seed_large_corpus(n=10, chars=4000)
        result = json.loads(
            await memcp_recall(query="bigcorpus", scope="all", limit=20, max_tokens=500)
        )
        assert result["status"] == "ok"
        # ~1000-token insights, 500 budget: the budget always yields at least one
        # (can't return empty) but no more than that single first insight.
        assert result["count"] == 1

    async def test_4_search_default_and_budgets(self, isolated_data_dir) -> None:
        await _seed_large_corpus(n=10, chars=4000)

        # Default 8000 budget bites.
        default = json.loads(await memcp_search(query="bigcorpus", scope="all", limit=20))
        assert default["status"] == "ok"
        default_tokens = sum(r.get("token_count", 0) for r in default["results"])
        assert default_tokens <= 8000
        assert default["count"] < 10

        # max_tokens=0 unlimited.
        unlimited = json.loads(
            await memcp_search(query="bigcorpus", scope="all", limit=20, max_tokens=0)
        )
        assert unlimited["count"] == 10

        # Explicit small budget.
        small = json.loads(
            await memcp_search(query="bigcorpus", scope="all", limit=20, max_tokens=500)
        )
        assert small["count"] == 1

    async def test_5_small_corpus_identical_with_and_without_default(
        self, isolated_data_dir
    ) -> None:
        # 3 tiny insights — total well under 8000 tokens, so the cap must not bite.
        for i in range(3):
            await memcp_remember(content=f"smallcorpus item {i}", category="fact", tags="small")

        with_default = json.loads(await memcp_recall(query="smallcorpus", scope="all"))
        without = json.loads(await memcp_recall(query="smallcorpus", scope="all", max_tokens=0))

        assert with_default["count"] == without["count"] == 3
        ids_default = sorted(i["id"] for i in with_default["insights"])
        ids_without = sorted(i["id"] for i in without["insights"])
        assert ids_default == ids_without


# ── Patch 02 — memcp_get(insight_id) ─────────────────────────────────────────


class TestPatch02MemcpGet:
    async def test_6_get_by_full_id(self, isolated_data_dir) -> None:
        created = json.loads(
            await memcp_remember(content="Full id lookup target", category="fact", tags="getid")
        )
        full_id = created["id"]
        result = json.loads(await memcp_get(full_id))
        assert result["status"] == "ok"
        assert result["insight"]["id"] == full_id
        assert result["insight"]["content"] == "Full id lookup target"

    async def test_7_get_by_unambiguous_prefix(self, isolated_data_dir) -> None:
        created = json.loads(
            await memcp_remember(content="Unique prefix target xyz", category="fact")
        )
        full_id = created["id"]
        prefix = full_id[:8]
        result = json.loads(await memcp_get(prefix))
        assert result["status"] == "ok"
        assert result["insight"]["id"] == full_id

    async def test_8_get_ambiguous_prefix_returns_candidates(self, isolated_data_dir) -> None:
        # Ids are 16-char hex; the first char has 16 possible values, so by
        # pigeonhole >=17 insights guarantee at least two share a 1-char prefix.
        ids = []
        for i in range(20):
            r = json.loads(
                await memcp_remember(content=f"ambiguous corpus item {i}", category="fact")
            )
            ids.append(r["id"])

        # Find a 1-char prefix shared by 2+ ids (guaranteed to exist).
        from collections import defaultdict

        by_first: dict[str, list[str]] = defaultdict(list)
        for i in ids:
            by_first[i[0]].append(i)
        shared_char, collided = next((c, v) for c, v in by_first.items() if len(v) >= 2)

        result = json.loads(await memcp_get(shared_char))
        assert result["status"] == "ambiguous"
        assert set(result["candidates"]) >= set(collided)
        # It did NOT return a single (possibly wrong) insight.
        assert "insight" not in result

    async def test_9_get_nonexistent_id(self, isolated_data_dir) -> None:
        result = json.loads(await memcp_get("ffffffffffffffff"))
        assert result["status"] == "not_found"

    async def test_10_get_empty_and_malformed(self, isolated_data_dir) -> None:
        empty = json.loads(await memcp_get(""))
        assert empty["status"] == "error"
        # Whitespace-only is also empty after strip.
        ws = json.loads(await memcp_get("   "))
        assert ws["status"] == "error"


# ── Item 3 — memcp_remember metadata integrity + dedup ───────────────────────


class TestItem3RememberMetadata:
    async def test_11_metadata_persists_exactly(self, isolated_data_dir) -> None:
        result = json.loads(
            await memcp_remember(
                content="metadata integrity probe",
                category="decision",
                importance="high",
                tags="a,b",
            )
        )
        assert result["status"] == "saved"
        # The remember response echoes the persisted metadata.
        assert result["category"] == "decision"
        assert result["importance"] == "high"
        assert set(result["tags"]) == {"a", "b"}

        # Round-trip through recall confirms it actually persisted (not just echoed).
        got = json.loads(await memcp_get(result["id"]))
        ins = got["insight"]
        assert ins["category"] == "decision"
        assert ins["importance"] == "high"
        assert set(ins["tags"]) == {"a", "b"}

    async def test_12_server_passes_remember_by_keyword(self) -> None:
        """If the server passed positionally and remember()'s signature drifted,
        metadata would scramble. Assert the wiring is keyword-based so a reorder
        of remember()'s params cannot misroute category/importance/tags."""
        import inspect

        from memcp.core import memory as memory_mod

        # Patch remember() to capture HOW it was called (args vs kwargs).
        captured: dict = {}
        orig = memory_mod.remember

        def spy(*args, **kwargs):  # noqa: ANN002, ANN003
            captured["args"] = args
            captured["kwargs"] = kwargs
            return orig(*args, **kwargs)

        # The server imported `remember` by name into its own namespace.
        import memcp.server as server_mod

        server_orig = server_mod.remember
        server_mod.remember = spy
        try:
            await memcp_remember(
                content="keyword wiring probe",
                category="finding",
                importance="critical",
                tags="x,y",
            )
        finally:
            server_mod.remember = server_orig

        # Content is positional-or-keyword; the metadata fields MUST arrive as
        # keywords so a signature drift can't scramble them.
        assert captured["kwargs"].get("category") == "finding"
        assert captured["kwargs"].get("importance") == "critical"
        assert captured["kwargs"].get("tags") == "x,y"
        # No metadata leaked into positional slots beyond content.
        assert len(captured["args"]) <= 1
        # Sanity: signature still has these params (guards the test itself).
        params = inspect.signature(orig).parameters
        assert {"category", "importance", "tags"} <= set(params)

    async def test_13_distinct_persist_identical_dedup(self, isolated_data_dir) -> None:
        r1 = json.loads(await memcp_remember(content="distinct save ONE", category="fact"))
        r2 = json.loads(await memcp_remember(content="distinct save TWO", category="fact"))
        assert r1["status"] == "saved"
        assert r2["status"] == "saved"
        assert r1["id"] != r2["id"]

        # Identical content dedups to one.
        dup = json.loads(await memcp_remember(content="distinct save ONE", category="fact"))
        assert dup["status"] == "duplicate"
        assert dup["existing_id"] == r1["id"]


# ── Item 5 — pending-writes queue + SessionStart replay ──────────────────────


def _import_pending_queue():
    if not (CLAUDE_HOOKS / "pending_queue.py").exists():
        pytest.skip("claude-hooks repo not present at ~/projects/claude-hooks")
    sys.path.insert(0, str(CLAUDE_HOOKS))
    import pending_queue  # type: ignore

    return pending_queue


class TestItem5PendingWrites:
    def test_14_enqueue_on_unreachable_appends_jsonl(self, tmp_path, monkeypatch) -> None:
        pq = _import_pending_queue()
        queue_file = tmp_path / "memcp-pending.jsonl"
        monkeypatch.setenv("MEMCP_PENDING_QUEUE", str(queue_file))

        pq.enqueue(
            {
                "content": "buffered while unreachable",
                "category": "decision",
                "importance": "high",
                "tags": "kind:kb,buffered",
            }
        )
        assert queue_file.exists()
        lines = queue_file.read_text().strip().splitlines()
        assert len(lines) == 1
        obj = json.loads(lines[0])
        assert obj["content"] == "buffered while unreachable"
        assert obj["category"] == "decision"
        assert obj["importance"] == "high"
        assert obj["tags"] == "kind:kb,buffered"
        assert "queued_at" in obj

    def test_15_sessionstart_surfaces_and_clears_only_after_success(
        self, tmp_path, monkeypatch
    ) -> None:
        pq = _import_pending_queue()
        queue_file = tmp_path / "memcp-pending.jsonl"
        monkeypatch.setenv("MEMCP_PENDING_QUEUE", str(queue_file))

        pq.enqueue({"content": "replay me one", "category": "fact"})
        pq.enqueue({"content": "replay me two", "category": "finding"})

        # A reachable data dir for session_start (graph.db present + queryable).
        data_dir = tmp_path / "memcp-data"
        data_dir.mkdir()
        subprocess.run(
            ["sqlite3", str(data_dir / "graph.db"), "create table t(x);"],
            check=True,
            timeout=10,
        )

        env = {
            **os.environ,
            "MEMCP_DATA_DIR": str(data_dir),
            "MEMCP_PENDING_QUEUE": str(queue_file),
            "HOME": str(tmp_path),  # keep _candidate_data_dirs off the real ~/.claude.json
        }
        proc = subprocess.run(
            [sys.executable, str(CLAUDE_HOOKS / "session_start.py")],
            input=json.dumps({"source": "startup", "cwd": str(tmp_path)}),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        assert proc.returncode == 0, proc.stderr
        out = json.loads(proc.stdout or "{}")
        msg = out.get("systemMessage", "")
        assert "pending MemCP write" in msg
        assert "replay me one" in msg
        assert "replay me two" in msg

        # The hook surfaces but does NOT truncate — saves haven't landed yet.
        assert queue_file.exists()
        assert len(pq.pending_payloads()) == 2

        # Caller clears only after a successful replay.
        pq.clear_queue()
        assert pq.pending_payloads() == []

    def test_16_replay_trips_readonly_and_pointer_guards(self, tmp_path, monkeypatch) -> None:
        """A replayed payload is fed to the PreToolUse guards as a normal
        memcp_remember event — both guards must still fire, proving replay goes
        through the guarded path, not a raw DB write."""
        pq = _import_pending_queue()
        queue_file = tmp_path / "memcp-pending.jsonl"
        monkeypatch.setenv("MEMCP_PENDING_QUEUE", str(queue_file))

        # A pointer payload referencing a path that does NOT exist on disk.
        missing_path = str(tmp_path / "does-not-exist" / "artifact.md")
        pq.enqueue(
            {
                "content": f"EOD pointer — pick up at {missing_path}",
                "category": "todo",
                "tags": "kind:pointer",
            }
        )
        payload = pq.pending_payloads()[0]

        # Build the PreToolUse event the way Claude Code would for the replay.
        event = json.dumps(
            {
                "tool_name": "mcp__memcp__memcp_remember",
                "tool_input": {
                    "content": payload["content"],
                    "category": payload.get("category", "general"),
                    "tags": payload.get("tags", ""),
                },
            }
        )

        # (a) readonly guard blocks (exit 2) when MEMCP_READONLY=1.
        ro = subprocess.run(
            [sys.executable, str(CLAUDE_HOOKS / "pre_write_readonly_guard.py")],
            input=event,
            env={**os.environ, "MEMCP_READONLY": "1"},
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert ro.returncode == 2
        assert "MEMCP_READONLY" in ro.stderr

        # (b) pointer-artifact guard blocks (exit 2) — kind:pointer + missing artifact.
        ptr_env = {k: v for k, v in os.environ.items() if k != "MEMCP_READONLY"}
        ptr = subprocess.run(
            [sys.executable, str(CLAUDE_HOOKS / "pointer_artifact_guard.py")],
            input=event,
            env=ptr_env,
            capture_output=True,
            text=True,
            timeout=10,
        )
        assert ptr.returncode == 2
        assert "artifact" in ptr.stderr.lower()
