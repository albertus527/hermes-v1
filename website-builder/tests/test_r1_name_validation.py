"""PHASE D — canonical validation of MACHINE-PROPOSED project names.

FAST's ``proposed_new_project_name`` is machine inference, and may be a
descriptive phrase rather than a name. All machine-proposed names must clear
the same canonical name-likeness policy as deterministic extraction. An
EXPLICIT human answer to a NAME clarification uses a less restrictive (but
still syntactically safe) path — a human naming their site "Soft Corner" must
not be rejected merely because "soft" is a descriptor token.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.conversations import ConversationRouter, ConversationRoute  # noqa: E402
from r1_harness import LocalR1Scenario  # noqa: E402


class TestMachineProposedNameValidation:
    def test_descriptive_machine_name_rejected_asks_clarification(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name="membaca soft tone dan bright")
        h.send_user_message("aku mau bikin web untuk membaca, soft tone dan bright")

        # No machine-named identity invented; the system asks for a name.
        assert h.conversation_registry.projects == []
        assert h.pending_action["action"] == "CREATE_PROJECT"
        assert h.pending_action["awaiting"] == "NAME"
        assert any("nama" in t.lower() for _, t in h.telegram.text_calls)

    def test_single_word_machine_name_accepted(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name="cozyreadingspace")
        h.set_intake_response(name="cozyreadingspace", what="rental", why="read",
                              readiness="DISCOVERY_READY", clarification_needed=False)
        h.send_user_message("bikin website baru")

        entry = h.registry_entry()
        assert entry is not None
        assert entry.display_name == "cozyreadingspace"

    def test_multiword_titlecase_machine_name_accepted(self, tmp_path):
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name="Cozy Reading Space")
        h.set_intake_response(name="Cozy Reading Space", what="rental", why="read",
                              readiness="DISCOVERY_READY", clarification_needed=False)
        h.send_user_message("bikin website baru")

        entry = h.registry_entry()
        assert entry is not None
        assert entry.display_name == "Cozy Reading Space"


class TestExplicitHumanConfirmedName:
    def test_descriptor_token_human_name_accepted(self, tmp_path):
        """An explicit NAME clarification answer "Soft Corner" must be accepted
        even though "soft" is a descriptor token."""
        h = LocalR1Scenario(tmp_path)
        h.set_router_decision("CREATE_PROJECT", confidence="high",
                              proposed_new_project_name=None)
        h.send_user_message("aku mau bikin web untuk membaca, soft tone dan bright")
        assert h.pending_action["awaiting"] == "NAME"

        h.set_intake_response(name="Soft Corner", what="reading rental", why="read",
                              readiness="DISCOVERY_READY", clarification_needed=False)
        h.send_user_message("Soft Corner")

        entry = h.registry_entry()
        assert entry is not None
        assert entry.display_name == "Soft Corner"


class TestNamePolicyUnit:
    def test_looks_like_project_name_policy(self):
        assert ConversationRouter._looks_like_project_name("cozyreadingspace")
        assert ConversationRouter._looks_like_project_name("Cozy Reading Space")
        assert not ConversationRouter._looks_like_project_name(
            "membaca soft tone dan bright"
        )
        # multi-clause descriptive text is not a name
        assert not ConversationRouter._looks_like_project_name("reading, rental site")

    def test_safe_confirmed_name_is_less_restrictive_but_safe(self):
        # Descriptor tokens allowed for an explicit human answer.
        assert ConversationRouter._is_safe_confirmed_name("Soft Corner")
        assert ConversationRouter._is_safe_confirmed_name("bright")
        # Still bounded / syntactically safe.
        assert not ConversationRouter._is_safe_confirmed_name("")
        assert not ConversationRouter._is_safe_confirmed_name("a" * 200)
        assert not ConversationRouter._is_safe_confirmed_name("reading, rental")
        assert not ConversationRouter._is_safe_confirmed_name("!!!")
