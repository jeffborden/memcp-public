"""Tests for memcp.core.memory.topic — the "compiled truth + timeline" reader.

Oracle for the `memcp_topic` tool (SPEC-content-versioning.md, Option A). A topic is a
"living doc" expressed as a chain of ordinary new-id `memcp_remember()` rows that all
carry a stable `topic:<slug>` tag. Each row is typed `entry:compiled` (a full
current-understanding restatement) or `entry:log` (a dated evidence/correction append);
a compiled row cites the prior compiled head via `supersedes:<id8>`. `topic(slug)`
returns the latest compiled row as "current" on top and every row for the topic as a
chronological timeline underneath — the gbrain shape — and warns when a compiled head's
`supersedes:` link is missing or points at the wrong row.

Every write is a new-id INSERT (never an in-place content edit), so the pattern converges
cross-machine under snapshot_sync's union merge by construction (SPEC §2.2). This reader
adds no storage and mirrors the additive, read-only posture of `memcp_grep`.

Jeff signed off on this oracle (tests 1-9) 2026-07-01.
"""

from __future__ import annotations

from memcp.core.graph import GraphMemory
from memcp.core.memory import remember, topic


def _set_created_at(insight_id: str, iso: str) -> None:
    """Force a row's created_at to a fixed ISO timestamp so ordering is deterministic.

    `remember()` stamps created_at = now() and `update_node`'s allowlist deliberately
    excludes created_at, so the ordering-sensitive tests set it directly via SQL. This
    removes any dependency on wall-clock resolution between rapid sequential saves."""
    graph = GraphMemory()
    try:
        conn = graph._get_conn()
        conn.execute("UPDATE nodes SET created_at = ? WHERE id = ?", (iso, insight_id))
        conn.commit()
    finally:
        graph.close()


def _set_archived(insight_id: str) -> None:
    """Mark an insight archived in-band (set archived_at) — the synced production posture,
    mirroring tests/unit/test_grep.py's helper."""
    graph = GraphMemory()
    try:
        graph.update_node(insight_id, {"archived_at": "2026-07-01T00:00:00+00:00"})
    finally:
        graph.close()


def _seed(
    content: str,
    slug: str,
    entry: str,
    *,
    supersedes: str | None = None,
    project: str = "",
    created_at: str | None = None,
) -> str:
    """Seed one topic row and return its id.

    entry is "compiled" or "log". supersedes (if given) is the prior compiled row's
    id-prefix. created_at (if given) is forced for deterministic ordering."""
    tags = ["kind:kb", f"topic:{slug}", f"entry:{entry}"]
    if supersedes is not None:
        tags.append(f"supersedes:{supersedes}")
    res = remember(content, category="general", tags=",".join(tags), project=project)
    if created_at is not None:
        _set_created_at(res["id"], created_at)
    return res["id"]


class TestCurrentIsLatestCompiled:
    """Test 1 — current is the newest entry:compiled row (by created_at), not v1, not a log."""

    def test_latest_compiled_wins(self) -> None:
        v1 = _seed("compiled v1", "demo", "compiled", created_at="2026-07-01T10:00:00+00:00")
        _seed("a correction log", "demo", "log", created_at="2026-07-01T11:00:00+00:00")
        v2 = _seed(
            "compiled v2",
            "demo",
            "compiled",
            supersedes=v1[:8],
            created_at="2026-07-01T12:00:00+00:00",
        )
        result = topic("demo")
        assert result["current"] is not None
        assert result["current"]["id"] == v2
        # current carries the full compiled content so the truth renders in place.
        assert result["current"]["content"] == "compiled v2"


class TestTimelineChronological:
    """Test 2 — timeline is every row for the topic, sorted (created_at, id) asc, deterministic."""

    def test_timeline_is_all_rows_in_order(self) -> None:
        v1 = _seed("compiled v1", "demo", "compiled", created_at="2026-07-01T10:00:00+00:00")
        lg = _seed("a log", "demo", "log", created_at="2026-07-01T11:00:00+00:00")
        v2 = _seed(
            "compiled v2",
            "demo",
            "compiled",
            supersedes=v1[:8],
            created_at="2026-07-01T12:00:00+00:00",
        )
        result = topic("demo")
        ids = [e["id"] for e in result["timeline"]]
        assert ids == [v1, lg, v2]

    def test_deterministic_ordering(self) -> None:
        for i in range(4):
            _seed(f"row {i}", "demo", "log", created_at=f"2026-07-01T10:0{i}:00+00:00")
        first = [e["id"] for e in topic("demo")["timeline"]]
        second = [e["id"] for e in topic("demo")["timeline"]]
        assert first == second
        rows = topic("demo")["timeline"]
        keys = [(e["created_at"], e["id"]) for e in rows]
        assert keys == sorted(keys)


class TestLogOnlyTopic:
    """Test 3 — a topic with only entry:log rows has current=None but a full timeline."""

    def test_log_only_topic(self) -> None:
        a = _seed("log a", "notes", "log", created_at="2026-07-01T10:00:00+00:00")
        b = _seed("log b", "notes", "log", created_at="2026-07-01T11:00:00+00:00")
        result = topic("notes")
        assert result["current"] is None
        assert [e["id"] for e in result["timeline"]] == [a, b]
        assert result["warnings"] == []


class TestUnknownSlug:
    """Test 4 — an unknown slug returns empties, not an error (mirrors grep's [] posture)."""

    def test_unknown_slug_is_empty(self) -> None:
        _seed("some doc", "demo", "compiled")
        result = topic("no-such-slug")
        assert result == {
            "slug": "no-such-slug",
            "current": None,
            "timeline": [],
            "warnings": [],
        }


class TestSupersedesWarning:
    """Test 5 — a compiled head lacking a supersedes: link (when a prior compiled exists) warns;
    a correct supersedes: link produces no warning."""

    def test_missing_supersedes_warns(self) -> None:
        _seed("compiled v1", "demo", "compiled", created_at="2026-07-01T10:00:00+00:00")
        _seed("compiled v2 (no link)", "demo", "compiled", created_at="2026-07-01T12:00:00+00:00")
        result = topic("demo")
        assert result["warnings"]  # non-empty

    def test_correct_supersedes_is_clean(self) -> None:
        v1 = _seed("compiled v1", "demo", "compiled", created_at="2026-07-01T10:00:00+00:00")
        _seed(
            "compiled v2",
            "demo",
            "compiled",
            supersedes=v1[:8],
            created_at="2026-07-01T12:00:00+00:00",
        )
        result = topic("demo")
        assert result["warnings"] == []

    def test_empty_supersedes_warns_like_missing(self) -> None:
        # A bare `supersedes:` (empty value) is a broken citation, not a valid link —
        # it must warn like a missing link, not slip through startswith("") always-true.
        _seed("compiled v1", "demo", "compiled", created_at="2026-07-01T10:00:00+00:00")
        _seed(
            "compiled v2 (empty link)",
            "demo",
            "compiled",
            supersedes="",
            created_at="2026-07-01T12:00:00+00:00",
        )
        result = topic("demo")
        assert result["warnings"]  # non-empty — empty supersedes: treated as missing


class TestSupersedesWrongTarget:
    """Test 6 — a supersedes: pointing at the wrong id (not the prior compiled head) warns."""

    def test_wrong_supersedes_target_warns(self) -> None:
        _seed("compiled v1", "demo", "compiled", created_at="2026-07-01T10:00:00+00:00")
        _seed(
            "compiled v2",
            "demo",
            "compiled",
            supersedes="deadbeef",
            created_at="2026-07-01T12:00:00+00:00",
        )
        result = topic("demo")
        assert result["warnings"]  # non-empty — deadbeef is not v1's prefix


class TestExactSlugMembership:
    """Test 7 — slug match is exact tag membership, not substring; no slug bleed."""

    def test_prefix_slug_not_returned(self) -> None:
        demo = _seed("real demo doc", "demo", "compiled", created_at="2026-07-01T10:00:00+00:00")
        _seed(
            "different topic", "demo-extended", "compiled", created_at="2026-07-01T10:00:00+00:00"
        )
        result = topic("demo")
        ids = {e["id"] for e in result["timeline"]}
        # Exactly the topic:demo row — never the topic:demo-extended row.
        assert ids == {demo}


class TestArchived:
    """Test 8 — archived rows excluded by default, included with include_archived=True."""

    def test_archived_excluded_by_default(self) -> None:
        a = _seed("live compiled", "demo", "compiled", created_at="2026-07-01T10:00:00+00:00")
        old = _seed("old compiled", "demo", "compiled", created_at="2026-07-01T09:00:00+00:00")
        _set_archived(old)
        result = topic("demo")
        ids = [e["id"] for e in result["timeline"]]
        assert old not in ids
        assert a in ids

    def test_archived_included_when_requested(self) -> None:
        _seed("live compiled", "demo", "compiled", created_at="2026-07-01T10:00:00+00:00")
        old = _seed("old compiled", "demo", "compiled", created_at="2026-07-01T09:00:00+00:00")
        _set_archived(old)
        result = topic("demo", include_archived=True)
        ids = [e["id"] for e in result["timeline"]]
        assert old in ids


class TestProjectScoping:
    """Test 9 — the same slug in two projects does not merge when project= is passed."""

    def test_project_filter_isolates_topic(self) -> None:
        a = _seed(
            "proj a doc",
            "shared-slug",
            "compiled",
            project="proja",
            created_at="2026-07-01T10:00:00+00:00",
        )
        _seed(
            "proj b doc",
            "shared-slug",
            "compiled",
            project="projb",
            created_at="2026-07-01T10:00:00+00:00",
        )
        result = topic("shared-slug", project="proja")
        ids = [e["id"] for e in result["timeline"]]
        assert ids == [a]
