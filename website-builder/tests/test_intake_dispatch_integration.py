"""Real integration tests: Telegram -> dispatch -> intake -> build, external boundaries mocked only.

Exercises the actual `TelegramDispatcher` + `IntakeProcessor` + `ProjectStateStore`
collaborators (no mocking of internal application logic) against the
Northcut/barbershop/WhatsApp-booking scenario described in the canonical spec:
three conversational turns, two clarifying questions, then exactly one build.

Only the Hermes FAST/FRONTEND adapter and the frontend builder's I/O are
mocked -- those are the genuine external/expensive boundaries (LLM calls,
npm/workspace builds). Dispatch claims, authz, lifecycle, and intake merge
semantics all run for real.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.channels.dispatch import AuthenticatedTelegramContext, TelegramDispatcher
from app.core.authz import ProjectAccess
from app.core.intake import IntakeProcessor
from app.core.state import ProjectStateStore


def _payload(event_id, user=1, chat=555, text="hello"):
    return {
        "update_id": event_id,
        "message": {
            "from": {"id": user},
            "chat": {"id": chat},
            "text": text,
            "date": event_id,
        },
    }


def _fast(scope, name=None, what=None, why=None, why_destination=None,
          clarification_needed=False, clarification_question=None):
    return {
        "scope": scope,
        "name": name,
        "what": what,
        "why": why,
        "why_destination": why_destination,
        "ambiguity": None,
        "clarification_needed": clarification_needed,
        "clarification_question": clarification_question,
        "readiness": "NEEDS_CLARIFICATION" if clarification_needed else "DISCOVERY_READY",
    }


def _make(tmp_path):
    store = ProjectStateStore(tmp_path / "state")
    ProjectAccess(store).create("app", "telegram:1", channel="telegram", conversation_id="555")
    adapter = MagicMock()
    intake = IntakeProcessor(store, hermes_adapter=adapter)
    builder = MagicMock()
    builder.build.return_value = MagicMock(success=True)
    dispatcher = TelegramDispatcher(store, intake, builder=builder,
                                     workspace_for=lambda pid: tmp_path / "ws")
    return store, adapter, builder, dispatcher


def _auth():
    return AuthenticatedTelegramContext("1", "555")


class TestThreeTurnDiscoveryToBuild:
    """Northcut barbershop: three turns, two clarifying questions, one build."""

    def test_three_turns_two_questions_then_exactly_one_build(self, tmp_path):
        store, adapter, builder, dispatcher = _make(tmp_path)

        # Turn 1: only a name -> FAST asks for WHAT.
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut",
            clarification_needed=True,
            clarification_question="What is Northcut?",
        )
        r1 = dispatcher.dispatch(_payload(1, text="Northcut"), "app", "intake", authenticated=_auth())
        assert r1.success
        assert r1.data["readiness"] == "NEEDS_CLARIFICATION"
        assert r1.data["clarification_question"] == "What is Northcut?"
        assert builder.build.call_count == 0
        assert store.load("app").brief.get("name") == "Northcut"

        # Turn 2: answers WHAT -> FAST asks for WHY. NAME must be preserved
        # from persisted brief, not re-derived from this turn's isolated text.
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", what="barbershop",
            clarification_needed=True,
            clarification_question="What should visitors do on Northcut?",
        )
        r2 = dispatcher.dispatch(_payload(2, text="barbershop"), "app", "intake", authenticated=_auth())
        assert r2.success
        assert r2.data["readiness"] == "NEEDS_CLARIFICATION"
        assert builder.build.call_count == 0
        state = store.load("app")
        assert state.brief.get("name") == "Northcut"
        assert state.brief.get("what") == "barbershop"

        # Turn 3: answers WHY -> DISCOVERY_READY -> exactly one build.
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", what="barbershop", why="booking WA",
            clarification_needed=False,
        )
        r3 = dispatcher.dispatch(_payload(3, text="booking WA"), "app", "intake", authenticated=_auth())
        assert r3.success
        assert r3.data["readiness"] == "DISCOVERY_READY"
        assert r3.data.get("build_triggered") is True
        assert r3.data.get("build_success") is True
        assert builder.build.call_count == 1

        state = store.load("app")
        assert state.brief == {
            "name": "Northcut", "what": "barbershop", "why": "booking WA",
        }
        assert state.lifecycle == "QUEUED"

        # Context handed to FAST on turn 2/3 includes the accumulated brief.
        second_call_context = adapter.fast_interpret.call_args_list[1].args[2]
        assert second_call_context is not None
        assert any("name=Northcut" in m["content"] for m in second_call_context)

    def test_duplicate_turn_replay_no_additional_effects(self, tmp_path):
        """Replaying the SAME Telegram update never re-runs intake or build."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", what="barbershop", why="booking WA",
            clarification_needed=False,
        )
        payload = _payload(1, text="Northcut, barbershop, booking WA")

        r1 = dispatcher.dispatch(payload, "app", "intake", authenticated=_auth())
        assert r1.success
        assert builder.build.call_count == 1
        assert adapter.fast_interpret.call_count == 1

        # Replay of the identical update (same event_id) is idempotent.
        r2 = dispatcher.dispatch(payload, "app", "intake", authenticated=_auth())
        assert r2.success
        assert r2.data.get("duplicate") is True
        assert builder.build.call_count == 1
        assert adapter.fast_interpret.call_count == 1

    def test_restart_replay_no_additional_effects(self, tmp_path):
        """A fresh dispatcher instance (simulating process restart) sees the
        same persisted claim and does not re-run effects for the same event.
        """
        store, adapter, builder, dispatcher = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", what="barbershop", why="booking WA",
            clarification_needed=False,
        )
        payload = _payload(1, text="Northcut, barbershop, booking WA")
        r1 = dispatcher.dispatch(payload, "app", "intake", authenticated=_auth())
        assert r1.success
        assert builder.build.call_count == 1

        # Simulate restart: brand-new store/dispatcher objects over the same
        # persisted state root.
        store2 = ProjectStateStore(tmp_path / "state")
        adapter2 = MagicMock()
        adapter2.fast_interpret.return_value = adapter.fast_interpret.return_value
        intake2 = IntakeProcessor(store2, hermes_adapter=adapter2)
        builder2 = MagicMock()
        builder2.build.return_value = MagicMock(success=True)
        dispatcher2 = TelegramDispatcher(store2, intake2, builder=builder2,
                                          workspace_for=lambda pid: tmp_path / "ws")

        r2 = dispatcher2.dispatch(payload, "app", "intake", authenticated=_auth())
        assert r2.success
        assert r2.data.get("duplicate") is True
        assert builder2.build.call_count == 0
        assert adapter2.fast_interpret.call_count == 0

    def test_build_dispatch_failure_recorded_and_not_silently_retried(self, tmp_path):
        """A build failure inside the intake claim is recorded FAILED on the
        sub-claim, and reported back to the caller -- it never crashes the
        top-level intake claim, which itself must stay DONE.
        """
        store, adapter, builder, dispatcher = _make(tmp_path)
        builder.build.return_value = MagicMock(success=False, error="npm ci failed")
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", what="barbershop", why="booking WA",
            clarification_needed=False,
        )
        payload = _payload(1, text="Northcut, barbershop, booking WA")
        result = dispatcher.dispatch(payload, "app", "intake", authenticated=_auth())

        assert result.success  # the intake claim itself succeeded
        assert result.data.get("build_triggered") is True
        assert result.data.get("build_success") is False
        assert result.data.get("build_error") == "npm ci failed"
        assert builder.build.call_count == 1

        state = store.load("app")
        # The top-level intake claim is DONE.
        top_claims = [c for c in state.dispatch_events.values() if c["action"] == "intake"]
        assert top_claims[0]["status"] == "DONE"
        # The build sub-claim recorded FAILED, not left CLAIMED forever.
        build_claims = [c for c in state.dispatch_events.values() if c["action"] == "build"]
        assert build_claims[0]["status"] == "FAILED"

    def test_persistence_failure_before_effects_blocks_effect(self, tmp_path):
        """If persisting the claim raises before any effect runs, no effect
        (FAST call, build) ever executes.
        """
        store, adapter, builder, dispatcher = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast("WEBSITE", name="Northcut")

        original_save = store.save
        calls = {"n": 0}

        def flaky_save(state):
            calls["n"] += 1
            if calls["n"] == 1:
                raise IOError("disk full")
            return original_save(state)

        store.save = flaky_save
        payload = _payload(1, text="Northcut")
        try:
            dispatcher.dispatch(payload, "app", "intake", authenticated=_auth())
        except IOError:
            pass
        assert adapter.fast_interpret.call_count == 0
        assert builder.build.call_count == 0

    def test_unauthorized_principal_cannot_trigger_intake(self, tmp_path):
        """A principal with no role on the project cannot mutate it."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        stranger = AuthenticatedTelegramContext("999", "999")
        payload = {
            "update_id": 1,
            "message": {"from": {"id": 999}, "chat": {"id": 999}, "text": "hi", "date": 1},
        }
        result = dispatcher.dispatch(payload, "app", "intake", authenticated=stranger)
        assert not result.success
        assert result.error_code == "UNAUTHORIZED_ROLE"
        assert adapter.fast_interpret.call_count == 0
        assert builder.build.call_count == 0

    def test_fast_ambiguity_question_preserved_over_generic_fallback(self, tmp_path):
        """When FAST flags a material ambiguity/correction with its own
        question, that question is surfaced verbatim -- not silently
        replaced by the generic per-field fallback prompt.
        """
        store, adapter, builder, dispatcher = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", what="barbershop",
            clarification_needed=True,
            clarification_question=(
                "You mentioned both 'barbershop' and 'salon' -- which one is it?"
            ),
        )
        payload = _payload(1, text="Northcut, barbershop or salon?")
        result = dispatcher.dispatch(payload, "app", "intake", authenticated=_auth())
        assert result.success
        assert result.data["clarification_question"] == (
            "You mentioned both 'barbershop' and 'salon' -- which one is it?"
        )

    def test_fast_correction_across_turns_overrides_persisted_name(self, tmp_path):
        """FAST is semantic authority: when it re-labels a corrected NAME on
        a later turn, that correction wins over the previously persisted
        value -- the merge must not blindly prefer old state.
        """
        store, adapter, builder, dispatcher = _make(tmp_path)
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Norhtcut", clarification_needed=True,
            clarification_question="What is Norhtcut?",
        )
        dispatcher.dispatch(_payload(1, text="Norhtcut"), "app", "intake", authenticated=_auth())
        assert store.load("app").brief.get("name") == "Norhtcut"

        # User corrects the typo; FAST relabels NAME for this turn.
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="Northcut", clarification_needed=True,
            clarification_question="What is Northcut?",
        )
        dispatcher.dispatch(_payload(2, text="I meant Northcut"), "app", "intake", authenticated=_auth())
        assert store.load("app").brief.get("name") == "Northcut"
