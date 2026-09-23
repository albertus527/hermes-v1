"""Phase D — R1 conversation routing scenarios (Bug 5 duplicate create,
Bug 6 pending-name stealing an intake answer).

These drive the REAL TelegramReceiveLoop -> ConversationRouter -> dispatcher
handoff over real temp state.
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


class TestBug5DuplicateCreateClarifies:
    def test_duplicate_name_create_does_not_switch_or_mutate(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        # Existing project "kitsunereading".
        pid = h.seed_project("kitsunereading")
        before = h.store.load(pid).to_dict()

        # FAST sees a CREATE_PROJECT proposal naming the existing project.
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name="kitsunereading")
        h.send_user_message("buat website baru namanya kitsunereading")

        reg = h.registry.load_or_create(h.chat_id)
        # Exactly one project — no duplicate allocated.
        assert [p.project_id for p in reg.projects] == [pid]
        # Active pointer NOT switched automatically (it was already pid; the
        # point is no NEW project). And crucially nothing was mutated.
        assert reg.active_project_id == pid
        after = h.store.load(pid).to_dict()
        assert after == before
        # Clarification asked, not a silent switch into intake.
        assert any("sudah punya project" in t for _, t in h.telegram.text_calls)

    def test_duplicate_create_does_not_send_old_project_into_intake(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        pid = h.seed_project("kitsunereading")
        # No intake was ever expected on the old project for this turn.
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name="kitsunereading")
        h.send_user_message("bikin website baru namanya kitsunereading")

        state = h.store.load(pid)
        # The old project must not have been driven through intake/revise.
        assert state.lifecycle == "DISCOVERING"
        assert state.dispatch_events == {}
        assert state.revisions.requirements_version == 0
        # No Vercel create attempted.
        assert h.vercel.post_calls == 0


class TestBug6PendingNameStealsIntakeAnswer:
    def test_intake_answer_not_consumed_as_new_project_name(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        # Project A is WAITING_INPUT expecting an intake answer.
        pid = h.seed_project("alpha")
        with h.store.acquire_writer(pid) as state:
            state.brief = {}  # nothing collected yet
            state.lifecycle = "WAITING_INPUT"
            h.store.save(state)
        # The conversation ALSO has a pending CREATE_PROJECT/NAME clarification.
        h.registry.set_pending_action(h.chat_id, {"action": "CREATE_PROJECT",
                                                  "awaiting": "NAME"})

        # The next message clearly answers Project A's intake question.
        h.set_intake_response(name="Alpha Reading", what="reading rental",
                              why="rent books", readiness="DISCOVERY_READY",
                              clarification_needed=False)
        h.send_user_message("ini tempat sewa buku bacaan di bandung")

        reg = h.registry.load_or_create(h.chat_id)
        # No accidental second project allocated.
        assert [p.project_id for p in reg.projects] == [pid]
        assert h.store.load("tg-555-p2") is None

    def test_pending_name_is_not_blindly_consumed_when_answer_is_intake(self, tmp_path):
        """The safety invariant: do not blindly treat the message as
        CREATE_PROJECT/NAME when it plausibly answers the active project."""
        h = LocalR1Scenario(tmp_path)
        pid = h.seed_project("alpha")
        with h.store.acquire_writer(pid) as state:
            state.brief = {}
            state.lifecycle = "WAITING_INPUT"
            h.store.save(state)
        h.registry.set_pending_action(h.chat_id, {"action": "CREATE_PROJECT",
                                                  "awaiting": "NAME"})

        h.set_router_decision("PROJECT_TURN", confidence="high")
        h.set_intake_response(name="Alpha Reading", what="rental", why="rent",
                              readiness="DISCOVERY_READY", clarification_needed=False)
        h.send_user_message("tempat sewa buku bacaan di bandung")

        # Project A kept its answer.
        state = h.store.load(pid)
        assert state.brief.get("name") == "Alpha Reading"
        assert state.brief.get("what") == "rental"
        # No accidental project allocated.
        assert len(h.registry.load_or_create(h.chat_id).projects) == 1
