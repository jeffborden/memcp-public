"""Issue #82 — memcp_remember reported dropping tags/summary/importance.

Two production instances (a wrap pointer saved with tags=[] despite all
three fields passed), zero repros so far. Not established whether the drop
is server-side or MCP-client-side. These tests exercise every layer this
repo owns, end to end through a REAL MCP client session (JSON-RPC over the
in-memory transport, full schema validation + serialization) — the way the
runtime builds the input, not a fabricated shape.

If these stay green, the drop is upstream of the server (client/transport),
and the received-vs-stored telemetry added alongside them is what carries
evidence when it next happens.
"""

from __future__ import annotations

import json

import pytest
from mcp.shared.memory import create_connected_server_and_client_session

from memcp.core.memory import get_insight

FIELDS = {
    "content": "Ruling A executed: 195 rows collapsed to one project, verified rank-1",
    "category": "decision",
    "importance": "high",
    "tags": "kind:pointer,memcp,ruling-a",
    "summary": "one-line state + path to the handoff file",
    "entities": "memcp,ruling-a",
}


async def _call_remember_over_mcp(arguments: dict) -> dict:
    """Send tools/call memcp_remember through a real MCP client session."""
    from memcp import server as server_module

    async with create_connected_server_and_client_session(
        server_module.mcp._mcp_server
    ) as client:
        result = await client.call_tool("memcp_remember", arguments)
    assert result.content and result.content[0].type == "text"
    return json.loads(result.content[0].text)


class TestRememberFieldIntegrity:
    async def test_all_fields_survive_the_full_mcp_stack(self, isolated_data_dir):
        response = await _call_remember_over_mcp(dict(FIELDS))

        # The response must reflect what was sent...
        assert response["status"] == "saved", response
        assert response["importance"] == "high"
        assert response["tags"] == ["kind:pointer", "memcp", "ruling-a"]

        # ...and so must the actual stored row, independently re-read.
        stored = get_insight(response["id"])
        assert stored is not None
        assert stored["tags"] == ["kind:pointer", "memcp", "ruling-a"]
        assert stored["summary"] == FIELDS["summary"]
        assert stored["importance"] == "high"
        assert stored["category"] == "decision"

    async def test_duplicate_content_reports_duplicate_not_saved(self, isolated_data_dir):
        """The nearest server-side lookalike of #82: same content re-saved
        with different metadata returns the EXISTING row's metadata. Assert
        the status distinguishes it — a client that reads 'duplicate' as
        'saved' would see its tags 'dropped'."""
        first = await _call_remember_over_mcp(dict(FIELDS))
        assert first["status"] == "saved"

        again = dict(FIELDS)
        again["tags"] = "completely,different,tags"
        response = await _call_remember_over_mcp(again)

        assert response["status"] == "duplicate"
        assert response["existing_id"] == first["id"]
        # The duplicate response deliberately carries no 'tags' key at all —
        # nothing a client could misread as "saved with these tags".
        assert "tags" not in response

    async def test_telemetry_records_received_vs_stored(self, isolated_data_dir, tmp_path):
        """The evidence channel for the next #82 occurrence: every remember
        emits a remember_fields event with received + stored field shapes
        (counts/bools/bounded enums only — never tag values or content)."""
        telemetry_dir = tmp_path / "telemetry"
        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("MEMCP_TELEMETRY", "true")
            mp.setenv("MEMCP_TELEMETRY_DIR", str(telemetry_dir))
            await _call_remember_over_mcp(dict(FIELDS))

        files = list(telemetry_dir.glob("*.jsonl"))
        assert files, "no telemetry written"
        events = [
            json.loads(line)
            for f in files
            for line in f.read_text().splitlines()
            if line.strip()
        ]
        field_events = [e for e in events if e.get("kind") == "remember_fields"]
        assert field_events, f"no remember_fields event among {[e.get('kind') for e in events]}"
        ev = field_events[-1]
        assert ev["received"]["tags"] == 3  # count, not content
        assert ev["received"]["summary"] is True
        assert ev["received"]["importance"] == "high"
        assert ev["stored"]["tags"] == 3
        assert ev["stored"]["summary"] is True
        assert ev["stored"]["importance"] == "high"
        assert ev["stored"]["status"] == "saved"
