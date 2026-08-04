"""Write-tool schemas must not expose attribution params.

When memcp_remember accepted project/session, models filled them in ~78% of
lifetime calls — 195 of 394 insights forked outside the default read path
while every prompt-level "leave it blank" instruction decayed. The interface
is the guard: a param models shouldn't fill must not exist.

Read tools legitimately take project/session as FILTERS; only tools that
create or mutate data are locked down here.
"""

from __future__ import annotations

import inspect

import pytest

from memcp import server

# Every tool that creates or mutates stored data. New write tools must be
# added here — the sweep test below fails if one slips through unlisted.
WRITE_TOOLS = [
    "memcp_remember",
    "memcp_log_episode",
    "memcp_load_context",
    "memcp_update",
    "memcp_forget",
    "memcp_archive",
    "memcp_restore",
    "memcp_reinforce",
    "memcp_consolidate",
    "memcp_retention_run",
    "memcp_clear_context",
]


def _tool_params(tool_name: str) -> set[str]:
    fn = getattr(server, tool_name)
    return set(inspect.signature(fn).parameters)


@pytest.mark.parametrize("tool_name", WRITE_TOOLS)
def test_write_tool_takes_no_attribution_params(tool_name):
    params = _tool_params(tool_name)
    forbidden = params & {"project", "session"}
    assert not forbidden, (
        f"{tool_name} exposes {sorted(forbidden)} — attribution comes from "
        f"process identity, never from a caller-supplied override"
    )


# Read-only tools, where project/session are legitimate FILTER params.
READ_TOOLS = [
    "memcp_ping",
    "memcp_recall",
    "memcp_get",
    "memcp_grep",
    "memcp_topic",
    "memcp_status",
    "memcp_index",
    "memcp_inspect_context",
    "memcp_get_context",
    "memcp_chunk_context",
    "memcp_peek_chunk",
    "memcp_filter_context",
    "memcp_list_contexts",
    "memcp_search",
    "memcp_related",
    "memcp_graph_stats",
    "memcp_retention_preview",
    "memcp_reindex",
    "memcp_sync",
    "memcp_projects",
    "memcp_sessions",
    "memcp_consolidation_preview",
    "memcp_recall_episodes",
]


def test_every_tool_is_classified():
    """Fail when a new tool is added without deciding read vs write.

    Forces every future tool through the question that matters here: does it
    mutate stored data (then it must not take attribution params), or is it a
    read (then project/session are filters and allowed).
    """
    all_tools = sorted(
        name
        for name in dir(server)
        if name.startswith("memcp_") and callable(getattr(server, name))
    )
    unclassified = [n for n in all_tools if n not in WRITE_TOOLS and n not in READ_TOOLS]
    both = sorted(set(WRITE_TOOLS) & set(READ_TOOLS))
    stale = [n for n in WRITE_TOOLS + READ_TOOLS if n not in all_tools]
    assert not unclassified, f"new tools need classifying read-vs-write: {unclassified}"
    assert not both, f"tools in both lists: {both}"
    assert not stale, f"listed tools that no longer exist: {stale}"
