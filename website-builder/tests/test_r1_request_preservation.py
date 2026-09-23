"""PHASE B — preserve the ORIGINAL descriptive request through name clarification.

Real conversation shape:

  TURN 1: a long DESCRIPTIVE create request with no business name ("aku mau
          bikin web untuk membaca ... soft tone dan bright") -> the system
          asks for the project/business name.
  TURN 2: "cozyreadingspace" -> the project is created.

The risk is that the runtime feeds ONLY TURN 2's bare name into intake,
losing TURN 1's semantics (reading/rental use case, books + magazines,
on-site atmosphere, collection info, soft/bright design). These tests assert
the original TURN 1 description is durably preserved and re-supplied into
intake WITHOUT relying on a fake FAST response re-supplying it on TURN 2.

Driven through the REAL production control flow (router, registry, intake,
dispatcher, runtime loop) with only external boundaries faked.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from r1_harness import LocalR1Scenario, patch_qa_boundaries  # noqa: E402


@pytest.fixture(autouse=True)
def _qa_boundaries(monkeypatch):
    patch_qa_boundaries(monkeypatch)


# The exact real TURN 1 request from the campaign brief.
TURN_1 = (
    "aku mau bikin web untuk membaca, jadi disitu bisa sewa bahan bacaan kaya "
    "buku dan majalah dan ada info buat bisa baca langsung di tempat kaya vibe "
    "nya gimana dan suasananya gimana, dan koleksi buku yg di punya\n"
    "buat desainnya dibikin soft tone dan bright"
)
TURN_2 = "cozyreadingspace"


class TestPhaseBRequestPreservation:
    def test_original_request_is_persisted_on_the_naming_turn(self, tmp_path):
        """TURN 1's descriptive request must be durably stored in the pending
        action (not just the fact that a name is needed)."""
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name=None)
        h.send_user_message(TURN_1)

        assert h.conversation_registry.projects == []
        pending = h.pending_action
        assert pending["action"] == "CREATE_PROJECT"
        assert pending["awaiting"] == "NAME"
        preserved = pending.get("original_request") or ""
        # Key TURN 1 semantics survive verbatim in durable state.
        assert "membaca" in preserved
        assert "sewa bahan bacaan" in preserved
        assert "majalah" in preserved
        assert "soft tone" in preserved
        assert "bright" in preserved
        assert h.vercel_calls["create_post"] == 0

    def test_name_answer_continues_same_project_with_original_semantics(self, tmp_path):
        """After the name answer, the SAME project is created and the intake
        FAST boundary receives BOTH the confirmed name AND the original TURN 1
        description — WITHOUT the fake FAST re-supplying those details."""
        h = LocalR1Scenario(tmp_path)

        # Capture what text the REAL intake process actually hands to FAST.
        seen_texts = []
        real_fast_interpret = h.hermes.fast_interpret

        def _spy(text, project_id=None, conversation_context=None):
            seen_texts.append(text)
            return real_fast_interpret(text, project_id, conversation_context)

        h.hermes.fast_interpret = _spy  # type: ignore[assignment]

        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name=None)
        h.send_user_message(TURN_1)
        assert h.conversation_registry.projects == []

        # TURN 2: the bare name. The fake intake response supplies NAME only
        # (no what/why) so the details must come from the preserved request.
        h.set_intake_response(name="cozyreadingspace", what=None, why=None,
                              readiness="NEEDS_CLARIFICATION",
                              clarification_needed=True)
        h.send_user_message(TURN_2)

        pid = h.current_project_id
        assert pid == "tg-555-p1"
        assert len(h.conversation_registry.projects) == 1
        entry = h.registry_entry(pid)
        assert entry.display_name == "cozyreadingspace"

        # The original TURN 1 description reached intake on the SAME turn the
        # name was confirmed.
        assert seen_texts, "intake FAST boundary was never invoked"
        last = seen_texts[-1]
        assert "cozyreadingspace" in last
        for fragment in ("membaca", "sewa bahan bacaan", "majalah", "soft tone", "bright"):
            assert fragment in last, f"missing original context fragment: {fragment!r}"

        # The pending action was consumed and cleared.
        assert h.pending_action is None

    def test_brief_name_converges_and_original_intent_survives(self, tmp_path):
        """End-to-end: registry.display_name == brief.name == confirmed name,
        and the accumulated brief still reflects TURN 1's intent."""
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name=None)
        h.send_user_message(TURN_1)

        # TURN 2 supplies the name; FAST (fake) interprets the COMBINED text
        # into the full brief exactly as the real FAST would.
        h.set_intake_response(
            name="cozyreadingspace",
            what="reading rental: books and magazines available on-site",
            why="read on site and rent reading materials",
            readiness="DISCOVERY_READY", clarification_needed=False,
        )
        h.send_user_message(TURN_2)

        pid = h.current_project_id
        state = h.project_state(pid)
        entry = h.registry_entry(pid)
        assert entry.display_name == "cozyreadingspace"
        assert state.brief["name"] == "cozyreadingspace"
        assert state.brief.get("what")
        assert state.brief.get("why")
        # A complete brief auto-triggers the build (existing R1 contract).
        assert state.revisions.source_revision >= 0
        # Exactly one project, no duplicate identity.
        assert [p.project_id for p in h.conversation_registry.projects] == ["tg-555-p1"]
