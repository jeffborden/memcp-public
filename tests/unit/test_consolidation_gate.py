"""Consolidation safety — similarity may NOMINATE a merge, never justify one.

Measured on a real 196-insight store: the default threshold (0.85) with
single-linkage union-find grouped 150 of 196 insights into ONE blob —
single linkage is transitive by construction (A~B, B~C, C~D chains A..D
together even when A and D are unrelated), so NO threshold fixes it alone.
An adopter who ran the advertised preview→consolidate flow on that group
would have destroyed three quarters of their store.

The gate: a group member may only be merged when another member EXPLICITLY
claims to supersede it — by citing its id near a supersession verb, or by a
blanket "supersedes all earlier <lane> pointers" claim whose lane the older
member's text carries. Everything else is reported, never merged.

Fixture shapes are the real failure shapes measured on that store
(2026-08-04), with neutralized prose. The rejected fixtures matter most —
a version of this gate that keyed on any shared snake_case token merged a
standing build-gate note into an unrelated pointer chain, and its self-test
passed while it was broken because the fixtures were too kind.
"""

from __future__ import annotations

import re

from memcp.core.consolidation import (
    claim_keeper,
    justified_subsets,
    merge_group,
)
from memcp.core.memory import remember


def _n(id8: str, created_at: str, content: str) -> dict:
    return {"id": id8 + "0" * 56, "created_at": created_at, "content": content}


# ── Gate fixtures: (name, expect_mergeable, group) ────────────────────

CHAIN_ID_CITED = [
    _n("0dbb2e34", "2026-08-04T00:10", "deploy-runbook lane wrapped: step A of the rollout is done."),
    _n("41941426", "2026-08-04T00:40", "deploy-runbook lane WRAP (supersedes pointer 0dbb2e34). Step A CLOSED."),
    _n("b75ebe65", "2026-08-04T01:10", "deploy-runbook lane FINAL wrap (supersedes pointers 0dbb2e34 and 41941426)."),
]

CHAIN_BLANKET_LANE = [
    _n("0354249a", "2026-08-03T18:00", "search_reindex_button lane code-complete: reindex button added to the admin page."),
    _n("a21caf2b", "2026-08-03T21:00", "SUPERSEDES all earlier search_reindex_button pointers. Button code-complete AND reviewed."),
]

TRAP_INDEPENDENT_FACTS = [
    _n("ea3e39cc", "2026-07-29T10:00", "[pages/preview-visibility-semantics.md] Pre-release preview vs visibility conditions."),
    _n("4d816090", "2026-07-31T10:00", "DOMAIN FACT (workflow preview / visibility conditions) - source: teammate DM."),
    _n("37b09681", "2026-07-31T18:00", "TICKET-219 resolved on the facts: the editor CAN emit a hide-effect condition."),
]

TRAP_CROSS_DATE_FAMILY = [
    _n("d814047f", "2026-07-29T10:00", "POINTER - 2026-07-29 standup session wrap. Update drafted and checked."),
    _n("9c9dee15", "2026-07-30T18:00", "Board 2026-07-30, SEAT 3 WRAP (claim 14:40 - released ~18:0x). See BOARD.md."),
    _n("34ec10b3", "2026-08-03T11:00", "Board 2026-08-03 midmorning seat: update POSTED 10:43 and verified in the bot."),
]

TRAP_SHARED_TOKEN = [
    # The group a broken version of this gate merged: both mention
    # project_hub and record_id; neither claims the other's lane. The older
    # entry is a STANDING build gate, not a superseded wrap pointer.
    _n("0faf7376", "2026-07-30T09:00", "TICKET-123 BUILD GATE - read before writing any seed code. Lives under ~/project_hub. Keyed on record_id."),
    _n("01ec1793", "2026-07-30T21:00", "ticket-123-plan-repair FINAL wrap pointer (supersedes all earlier ticket-123-plan-repair pointers) in ~/project_hub. Uses record_id."),
]

TRAP_INDEX_CITATION = [
    # An index pointer CITES an id without claiming to supersede it.
    _n("803432da", "2026-08-02T21:00", "DELIVERY PLAN banked 2026-08-02 evening. The packet is FINISHED and verified."),
    _n("f09b95ac", "2026-08-03T20:00", "ONBOARDING PACKET - SESSION WRAP. Read this first; it indexes everything else, incl. 803432da. DM SENT."),
]

ORDERING_CHAIN = [
    # created_at puts UPDATE last, but CLOSED names it, so CLOSED must survive.
    _n("cfd8abb4", "2026-08-04T09:00", "verify lane CLOSED (final; supersedes 7e8568ba). All three fixes verified."),
    _n("7e8568ba", "2026-08-04T10:00", "verify lane UPDATE: the PATCH-to-PUT defect in the narrow remediation path."),
]


class TestSupersessionGate:
    def test_id_cited_chain_is_mergeable(self):
        safe, leftover = justified_subsets(CHAIN_ID_CITED)
        assert len(safe) == 1 and len(safe[0]) == 3
        assert leftover == []

    def test_blanket_lane_chain_is_mergeable(self):
        safe, leftover = justified_subsets(CHAIN_BLANKET_LANE)
        assert len(safe) == 1 and len(safe[0]) == 2
        assert leftover == []

    def test_independent_facts_are_rejected(self):
        safe, leftover = justified_subsets(TRAP_INDEPENDENT_FACTS)
        assert safe == []
        assert len(leftover) == 3

    def test_cross_date_family_is_rejected(self):
        safe, leftover = justified_subsets(TRAP_CROSS_DATE_FAMILY)
        assert safe == []

    def test_shared_token_without_lane_claim_is_rejected(self):
        safe, leftover = justified_subsets(TRAP_SHARED_TOKEN)
        assert safe == []

    def test_index_citation_is_not_supersession(self):
        safe, leftover = justified_subsets(TRAP_INDEX_CITATION)
        assert safe == []

    def test_keeper_is_claim_sink_not_newest(self):
        safe, _ = justified_subsets(ORDERING_CHAIN)
        assert len(safe) == 1
        assert claim_keeper(safe[0])["id"].startswith("cfd8abb4")

    def test_gate_is_load_bearing(self):
        """Strip the supersession language from a known-true chain — the
        gate must flip to rejected, or it is not the discriminator."""
        stripped = [
            dict(n, content=re.sub(r"\(supersedes[^)]*\)", "", n["content"]))
            for n in CHAIN_ID_CITED
        ]
        safe, _ = justified_subsets(stripped)
        assert safe == [], "chain with supersession language removed was STILL accepted"


class TestMergeGroupSizeGuard:
    def test_oversized_group_is_refused(self, isolated_data_dir):
        ids = []
        for i in range(9):
            r = remember(f"guard test insight number {i} with distinct content {i}")
            ids.append(r["id"])

        result = merge_group(ids)
        assert result["status"] == "refused", result
        assert "9" in result["message"] and "8" in result["message"]

        # Nothing was merged — every insight is still retrievable.
        from memcp.core.memory import get_insight

        for iid in ids:
            assert get_insight(iid) is not None

    def test_small_group_still_merges(self, isolated_data_dir):
        a = remember("merge candidate alpha about the same topic")
        b = remember("merge candidate beta about the same topic")
        result = merge_group([a["id"], b["id"]])
        assert result["status"] == "ok"


class TestBulkConsolidateIsGated:
    def test_similar_but_unclaimed_pair_is_not_merged(self, isolated_data_dir):
        """The 150-blob scenario in miniature: similarity nominates the
        pair, but neither claims supersession — bulk consolidate must skip
        it, not destroy it."""
        from memcp.core.consolidation import consolidate
        from memcp.core.memory import get_insight

        a = remember("Python uses the GIL to lock the interpreter")
        b = remember("Python uses the GIL to limit the interpreter")

        result = consolidate(threshold=0.5)

        assert result["groups_merged"] == 0
        assert result["skipped_unjustified"] >= 2
        assert get_insight(a["id"]) is not None
        assert get_insight(b["id"]) is not None
