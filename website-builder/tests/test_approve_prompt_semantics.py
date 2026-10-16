"""Close Approval Audit — FAST APPROVE prompt semantics + classifier safeguards.

Context
-------
The Production-vs-R2 Approval Safety Audit found that the FAST
intent-classification prompt in ``_build_intent_prompt`` described APPROVE as
"This binds the preview but does NOT publish", while the real behaviour
(``_handle_approve`` -> ``PromotionOrchestrator.approve`` and, when
``publication_enabled`` is True, the follow-on promotion) accepts the verified
current preview and immediately initiates production publication.

This module pins TWO distinct things, and deliberately keeps them separate:

1. PROMPT EXPECTATION (advisory text only). The APPROVE definition in the
   bounded FAST prompt must describe the real accept-then-publish semantics,
   and must no longer carry the false "does NOT publish" claim. This is copy
   the model *reads*; it is NOT an enforceable safeguard.

2. ENFORCEABLE RUNTIME BEHAVIOUR (deterministic). ``_classify_intent`` maps the
   EXACT FAST response to an intent and fails safe to INTAKE on anything
   non-exact. The tests here use deterministic mocked FAST outcomes; they do
   not call a real model.

What this module does NOT claim
-------------------------------
A prompt change cannot guarantee that FAST never misclassifies ambiguous text.
``test_classifier_is_not_an_enforceable_guard_*`` below documents exactly that:
with a FAST double returning APPROVE for ambiguous text, ``_classify_intent``
honours it. The enforceable protections against an unintended publication live
ELSEWHERE and are unchanged by this patch: the ``publication_enabled``
publication boundary, owner-only ``promote()`` authorization, preview-identity
binding, and stale/replay claim protection.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.runtime import ConversationIntent, TelegramReceiveLoop  # noqa: E402


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------


def _loop(hermes=None, lifecycle="PREVIEW_READY"):
    """A receive loop whose only non-real boundary is FAST (``hermes``)."""
    dispatcher = MagicMock()
    state = MagicMock()
    state.lifecycle = lifecycle
    state.revisions.queued_revision_seq = 0
    state.revisions.source_revision = 1
    state.conversation_id = "555"
    dispatcher.store.load.return_value = state
    dispatcher.dispatch.return_value = MagicMock(success=True)
    return TelegramReceiveLoop(
        bot_token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
        dispatcher=dispatcher,
        telegram_out=MagicMock(),
        hermes=hermes,
        transport=MagicMock(),
    )


def _fast(response, success=True):
    """A deterministic FAST double returning an exact ``response`` string."""
    hermes = MagicMock()
    hermes._run_fast_programmatic.return_value = MagicMock(
        success=success, response=response
    )
    return hermes


_PREVIEW_INTENTS = ["INTAKE", "REVISE", "APPROVE", "PUBLISH"]


# ---------------------------------------------------------------------------
# 1. PROMPT EXPECTATION — APPROVE is described as accept-then-publish
# ---------------------------------------------------------------------------


class TestApprovePromptDescribesPublishSemantics:
    def test_approve_definition_states_it_initiates_publication(self):
        """The APPROVE copy must say it accepts the preview AND publishes."""
        prompt = _loop()._build_intent_prompt(
            "approve", "PREVIEW_READY", _PREVIEW_INTENTS
        )
        assert "APPROVE:" in prompt
        # The accurate description is present.
        assert "accepts the verified current preview" in prompt
        assert "immediately initiates production publication" in prompt
        # The subject-to-existing-checks qualifier is present.
        assert "subject to all existing" in prompt

    def test_approve_definition_no_longer_claims_it_does_not_publish(self):
        """The false 'does NOT publish' claim must be gone."""
        prompt = _loop()._build_intent_prompt(
            "approve", "PREVIEW_READY", _PREVIEW_INTENTS
        )
        assert "does NOT publish" not in prompt
        assert "binds the preview but" not in prompt

    def test_publish_definition_is_preserved(self):
        """PUBLISH keeps its explicit go-live definition."""
        prompt = _loop()._build_intent_prompt(
            "oke live", "PREVIEW_READY", _PREVIEW_INTENTS
        )
        assert "PUBLISH: The user clearly intends to go-live" in prompt
        assert "requires an" in prompt and "existing approval" in prompt

    def test_intent_examples_are_preserved(self):
        """Ordinary explicit approval/publish phrasings stay listed."""
        prompt = _loop()._build_intent_prompt(
            "approve", "PREVIEW_READY", _PREVIEW_INTENTS
        )
        for example in ("looks good", "approve", "ok", "publish", "go live", "launch"):
            assert example in prompt

    def test_prompt_still_lists_the_lifecycle_valid_intents(self):
        """The bounded allowlist for PREVIEW_READY is unchanged."""
        prompt = _loop()._build_intent_prompt(
            "approve", "PREVIEW_READY", _PREVIEW_INTENTS
        )
        for name in _PREVIEW_INTENTS:
            assert name in prompt

    def test_ambiguity_rule_is_unchanged(self):
        """The 'never guess APPROVE for ambiguous text' rule survives."""
        prompt = _loop()._build_intent_prompt(
            "approve", "PREVIEW_READY", _PREVIEW_INTENTS
        )
        assert "Never guess APPROVE for ambiguous text" in prompt
        assert "Never guess PUBLISH for ambiguous text" in prompt


# ---------------------------------------------------------------------------
# 2. ENFORCEABLE RUNTIME BEHAVIOUR — exact-match mapping via mocked FAST
# ---------------------------------------------------------------------------


class TestExplicitIntentMappingWithMockedFast:
    @pytest.mark.parametrize(
        "text",
        ["approve", "looks good", "ok", "approve website ini"],
    )
    def test_explicit_approval_phrasings_map_to_approve(self, text):
        loop = _loop(hermes=_fast("APPROVE"))
        assert loop._classify_intent(text, "PREVIEW_READY") == (
            ConversationIntent.APPROVE
        )

    @pytest.mark.parametrize("text", ["publish", "oke live", "go live", "launch"])
    def test_explicit_publish_phrasings_map_to_publish(self, text):
        loop = _loop(hermes=_fast("PUBLISH"))
        assert loop._classify_intent(text, "PREVIEW_READY") == (
            ConversationIntent.PUBLISH
        )

    def test_revision_request_maps_to_revise(self):
        loop = _loop(hermes=_fast("REVISE"))
        assert loop._classify_intent("revisi hero", "PREVIEW_READY") == (
            ConversationIntent.REVISE
        )

    @pytest.mark.parametrize("text", ["halo", "oke", "bagus", "hmm not sure"])
    def test_ambiguous_messages_map_to_intake_when_fast_says_intake(self, text):
        """Deterministic FAST outcome: ambiguity -> INTAKE, never destructive."""
        loop = _loop(hermes=_fast("INTAKE"))
        assert loop._classify_intent(text, "PREVIEW_READY") == (
            ConversationIntent.INTAKE
        )


class TestClassifierFailsSafeOnNonExactFastOutput:
    @pytest.mark.parametrize(
        "response",
        ["MAYBE_APPROVE", "approve?", "APPROVE the preview", "yes", "APPROVE_IT"],
    )
    def test_non_exact_response_falls_back_to_intake(self, response):
        """Anything that is not an exact allowlisted token is INTAKE."""
        loop = _loop(hermes=_fast(response))
        assert loop._classify_intent("approve", "PREVIEW_READY") == (
            ConversationIntent.INTAKE
        )

    @pytest.mark.parametrize("response", ["approve", "  approve  ", "`APPROVE`", '"approve"'])
    def test_surrounding_whitespace_and_quotes_are_tolerated(self, response):
        """Whitespace/quotes/fences are stripped before the exact match."""
        loop = _loop(hermes=_fast(response))
        assert loop._classify_intent("approve", "PREVIEW_READY") == (
            ConversationIntent.APPROVE
        )

    def test_fast_failure_falls_back_to_intake(self):
        loop = _loop(hermes=_fast("APPROVE", success=False))
        assert loop._classify_intent("approve", "PREVIEW_READY") == (
            ConversationIntent.INTAKE
        )

    def test_fast_exception_falls_back_to_intake(self):
        hermes = MagicMock()
        hermes._run_fast_programmatic.side_effect = RuntimeError("boom")
        loop = _loop(hermes=hermes)
        assert loop._classify_intent("approve", "PREVIEW_READY") == (
            ConversationIntent.INTAKE
        )

    def test_pre_build_lifecycle_never_calls_fast(self):
        hermes = MagicMock()
        loop = _loop(hermes=hermes, lifecycle="DISCOVERING")
        assert loop._classify_intent("approve", "DISCOVERING") == (
            ConversationIntent.INTAKE
        )
        hermes._run_fast_programmatic.assert_not_called()


# ---------------------------------------------------------------------------
# 3. RESIDUAL CLASSIFIER RISK — the prompt is advisory, not an enforceable guard
#
#    These tests DOCUMENT the residual risk; they do not celebrate it. They
#    prove that a prompt change alone cannot stop a misclassifying classifier,
#    and that the enforceable protections live elsewhere (publication_enabled
#    boundary, owner-only promote(), preview-identity binding, replay claims).
# ---------------------------------------------------------------------------


class TestClassifierIsNotAnEnforceableGuard:
    @pytest.mark.parametrize("text", ["halo", "oke", "bagus"])
    def test_misclassified_ambiguous_text_is_honoured_by_classifier(self, text):
        """If FAST wrongly returns APPROVE, the classifier returns APPROVE.

        This is the residual risk the prompt change cannot remove. The
        enforceable safeguards are downstream (see the audit report):
        ``publication_enabled`` gates promotion, ``promote()`` is owner-only,
        and the approval is bound to the exact shown preview identity.
        """
        loop = _loop(hermes=_fast("APPROVE"))
        assert loop._classify_intent(text, "PREVIEW_READY") == (
            ConversationIntent.APPROVE
        )

    def test_classifier_has_no_independent_text_guard(self):
        """There is no regex/keyword guard between FAST and the intent.

        The mapping is FAST -> exact-match parse -> intent; the original
        message text is not re-validated for approval intent. This pins the
        current design so a future change that adds (or removes) such a guard
        is a conscious, visible decision.
        """
        loop = _loop(hermes=_fast("APPROVE"))
        # The SAME intent results for a canonical approval and for a
        # non-approval message when FAST returns APPROVE.
        assert loop._classify_intent("approve", "PREVIEW_READY") == (
            ConversationIntent.APPROVE
        )
        assert loop._classify_intent("halo", "PREVIEW_READY") == (
            ConversationIntent.APPROVE
        )
