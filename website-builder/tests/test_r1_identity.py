"""Phase B — R1 identity scenarios (Bug 1 description-as-name, Bug 2
clarified-name propagation) exercised through the REAL runtime/router handoff.

These tests drive ``TelegramReceiveLoop._process_update`` (the real entry
point) over real temp state, using the local R1 simulation harness. Only the
Hermes/Telegram/Vercel/smoke boundaries are faked.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from r1_harness import LocalR1Scenario  # noqa: E402


LONG_DESCRIPTION = (
    "aku mau bikin web untuk membaca, jadi disitu bisa sewa bahan bacaan, "
    "soft tone dan bright"
)


class TestBug1DescriptionIsNotName:
    def test_long_description_asks_for_name_and_allocates_nothing(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name=None)
        h.send_user_message(LONG_DESCRIPTION)

        reg = h.registry.load_or_create(h.chat_id)
        # No canonical display name derived from the descriptive tail.
        assert reg.projects == []
        assert reg.active_project_id is None
        # System asked for a name; pending action records the clarification.
        assert reg.pending_action == {"action": "CREATE_PROJECT", "awaiting": "NAME"}
        assert any("nama" in t.lower() for _, t in h.telegram.text_calls)
        # No Vercel project creation yet.
        assert h.vercel.post_calls == 0

    def test_explicit_name_marker_still_yields_a_name(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name=None)
        h.set_intake_response(name="thedailybake", what="bakery", why="order",
                              readiness="DISCOVERY_READY", clarification_needed=False)
        h.send_user_message("bikin website baru namanya thedailybake")

        entry = h.registry_entry()
        assert entry is not None
        assert entry.display_name == "thedailybake"


class TestBug2ClarifiedNamePropagates:
    def test_clarified_name_becomes_canonical_same_project(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name=None)
        h.send_user_message(LONG_DESCRIPTION)
        assert h.registry.load_or_create(h.chat_id).projects == []

        # Answer the name clarification.
        h.set_intake_response(name="kitsunereading", what="reading rental",
                              why="rent books", readiness="DISCOVERY_READY",
                              clarification_needed=False)
        h.send_user_message("kitsunereading")

        reg = h.registry.load_or_create(h.chat_id)
        # Exactly ONE project allocated, and its canonical identity is the name.
        assert len(reg.projects) == 1
        entry = reg.projects[0]
        assert entry.display_name == "kitsunereading"
        assert entry.project_id == "tg-555-p1"
        assert reg.active_project_id == entry.project_id

        state = h.project_state()
        assert state.brief.get("name") == "kitsunereading"

        # Slug candidate derived from the display name BEFORE any Vercel create.
        assert h.vercel.post_calls == 0
        assert h._slug_for(entry.project_id, state) == "kitsunereading"

        # No second project id.
        assert h.store.load("tg-555-p2") is None

    def test_no_second_project_id_allocated_across_clarification(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name=None)
        h.send_user_message(LONG_DESCRIPTION)
        h.set_intake_response(name="kitsunereading", what="r", why="r",
                              readiness="DISCOVERY_READY", clarification_needed=False)
        h.send_user_message("kitsunereading")

        reg = h.registry.load_or_create(h.chat_id)
        assert [p.project_id for p in reg.projects] == ["tg-555-p1"]

    def test_existing_bound_slug_is_never_overwritten(self, tmp_path):
        """A later display-name rename must never rename the bound remote slug."""
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name="kitsunereading")
        h.set_intake_response(name="kitsunereading", what="r", why="r",
                              readiness="DISCOVERY_READY", clarification_needed=False)
        h.send_user_message("bikin website baru namanya kitsunereading")
        pid = h.current_project_id
        # Simulate a first Vercel bind already having happened.
        h.registry.set_vercel_slug_once(h.chat_id, pid, "kitsunereading")
        # A supposedly newer bind attempt must be a no-op.
        h.registry.set_vercel_slug_once(h.chat_id, pid, "kitsune-reading-new")
        assert h.registry_entry(pid).vercel_slug == "kitsunereading"
