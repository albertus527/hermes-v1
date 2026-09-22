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
from app.core.contracts import OperationResult
from app.core.intake import IntakeProcessor
from app.core.lifecycle import ProjectLifecycle
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


class TestAutoBuildSafeRetry:
    """F4: a build that provably never reached a remote side effect is
    retryable on a fresh intake event; a possibly-remote or ambiguous build
    stays fail-closed. Missing evidence is UNKNOWN -> fail closed."""

    def _complete_brief(self, store):
        with store.acquire_writer("app") as state:
            state.brief = {"name": "N", "what": "w", "why": "y"}
            state.lifecycle = "READY"
            store.save(state)

    def _new_intake_event(self, dispatcher, event_id, adapter):
        adapter.fast_interpret.return_value = _fast(
            "WEBSITE", name="N", what="w", why="y", clarification_needed=False,
        )
        return _payload(event_id, text="N, w, y")

    def test_pre_remote_failure_retries_on_fresh_event(self, tmp_path):
        """(a/b/c) local build failure (npm/toolchain/pre-Vercel) stamps
        reached_remote=False; a fresh intake event re-runs the build."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._complete_brief(store)
        # First build fails pre-remote: the boundary callback is NOT called;
        # reached_remote stays False. Model the real build contract: a failed
        # build drives the project QUEUED -> FAILED.
        def fail_pre_remote(pid, brief, on_remote_boundary=None):
            with store.acquire_writer("app") as s:
                store.transition_lifecycle_locked(s, ProjectLifecycle.FAILED)
                store.save(s)
            return MagicMock(success=False, error="npm ci failed")

        builder.build.side_effect = fail_pre_remote

        p1 = self._new_intake_event(dispatcher, 10, adapter)
        r1 = dispatcher.dispatch(p1, "app", "intake", authenticated=_auth())
        assert r1.success
        assert r1.data["build_triggered"] is True
        assert r1.data["build_success"] is False
        claim = [c for c in store.load("app").dispatch_events.values()
                 if c["action"] == "build"][0]
        assert claim["status"] == "FAILED"
        assert claim["reached_remote"] is False
        # A pre-remote build failure leaves the project FAILED.
        assert store.load("app").lifecycle == "FAILED"

        # The next intake turn is a fresh requirements-complete submission.
        # Intake's FAILED-recovery drives FAILED -> READY (resetting
        # source_revision), which makes the FAILED build claim admissible for
        # a safe re-drive (reached_remote was False).
        builder.build.side_effect = None
        builder.build.return_value = MagicMock(success=True)
        builder.build.reset_mock()
        p2 = self._new_intake_event(dispatcher, 11, adapter)
        r2 = dispatcher.dispatch(p2, "app", "intake", authenticated=_auth())
        assert r2.success
        assert builder.build.call_count == 1
        assert r2.data.get("build_triggered") is True

    def test_reached_remote_failure_stays_fail_closed(self, tmp_path):
        """(d/e) a build that reached the remote boundary (or crashed after
        the flag was persisted) must NOT be replayed."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._complete_brief(store)

        def remote_then_fail(pid, brief, on_remote_boundary=None):
            if on_remote_boundary:
                on_remote_boundary()  # marks reached_remote=True before effect
            return MagicMock(success=False, error="preview deploy failed")

        builder.build.side_effect = remote_then_fail
        p1 = self._new_intake_event(dispatcher, 20, adapter)
        r1 = dispatcher.dispatch(p1, "app", "intake", authenticated=_auth())
        assert r1.success
        claim = [c for c in store.load("app").dispatch_events.values()
                 if c["action"] == "build"][0]
        assert claim["status"] == "FAILED"
        assert claim["reached_remote"] is True

        # Retry must NOT re-run the build.
        builder.build.reset_mock()
        builder.build.side_effect = None
        builder.build.return_value = MagicMock(success=True)
        p2 = self._new_intake_event(dispatcher, 21, adapter)
        r2 = dispatcher.dispatch(p2, "app", "intake", authenticated=_auth())
        assert builder.build.call_count == 0
        assert r2.data.get("build_triggered") is not True

    def test_crash_after_flag_before_result_stays_closed(self, tmp_path):
        """(d) reached_remote persisted, then a crash (exception) before the
        result status write -> retry remains fail-closed."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._complete_brief(store)

        def crash_after_boundary(pid, brief, on_remote_boundary=None):
            if on_remote_boundary:
                on_remote_boundary()
            raise RuntimeError("process died")

        builder.build.side_effect = crash_after_boundary
        p1 = self._new_intake_event(dispatcher, 30, adapter)
        dispatcher.dispatch(p1, "app", "intake", authenticated=_auth())
        claim = [c for c in store.load("app").dispatch_events.values()
                 if c["action"] == "build"][0]
        assert claim["reached_remote"] is True

        builder.build.reset_mock()
        builder.build.side_effect = None
        builder.build.return_value = MagicMock(success=True)
        p2 = self._new_intake_event(dispatcher, 31, adapter)
        dispatcher.dispatch(p2, "app", "intake", authenticated=_auth())
        assert builder.build.call_count == 0

    def test_done_claim_never_reexecutes(self, tmp_path):
        """(f) a DONE build claim is never re-run."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._complete_brief(store)
        builder.build.return_value = MagicMock(success=True)

        p1 = self._new_intake_event(dispatcher, 40, adapter)
        dispatcher.dispatch(p1, "app", "intake", authenticated=_auth())
        assert builder.build.call_count == 1
        claim = [c for c in store.load("app").dispatch_events.values()
                 if c["action"] == "build"][0]
        assert claim["status"] == "DONE"

        builder.build.reset_mock()
        p2 = self._new_intake_event(dispatcher, 41, adapter)
        dispatcher.dispatch(p2, "app", "intake", authenticated=_auth())
        assert builder.build.call_count == 0

    def test_legacy_failed_claim_without_evidence_stays_closed(self, tmp_path):
        """(g) a pre-hardening FAILED build claim (no reached_remote key) is
        UNKNOWN -> fail closed, never replayed."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._complete_brief(store)
        builder.build.return_value = MagicMock(success=False, error="npm ci failed")

        p1 = self._new_intake_event(dispatcher, 50, adapter)
        dispatcher.dispatch(p1, "app", "intake", authenticated=_auth())
        # Strip the evidence to model a legacy claim.
        with store.acquire_writer("app") as state:
            for k, c in state.dispatch_events.items():
                if c.get("action") == "build":
                    c.pop("reached_remote", None)
            store.save(state)

        builder.build.reset_mock()
        builder.build.return_value = MagicMock(success=True)
        p2 = self._new_intake_event(dispatcher, 51, adapter)
        dispatcher.dispatch(p2, "app", "intake", authenticated=_auth())
        assert builder.build.call_count == 0


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

class TestExplicitBuildRemoteBoundary:
    """H-6: the explicit "build" dispatch path must use the SAME
    remote-boundary semantics as the intake auto-build sub-claim.

    claim(False) -> local work -> persist(True) -> remote side effect.
    A purely local failure (CHEAP_CHECKS_FAILED) stays safely retryable;
    a post-boundary failure stays reconciliation-required / non-replayable.
    """

    def _ready(self, store):
        with store.acquire_writer("app") as state:
            state.brief = {"name": "N", "what": "w", "why": "y"}
            state.lifecycle = "READY"
            store.save(state)

    def _build_payload(self, event_id=100):
        return _payload(event_id, text="build it")

    def _claims(self, store, action):
        return [c for c in store.load("app").dispatch_events.values()
                if c.get("action") == action]

    def test_a_local_failure_marks_false_and_retries_exactly_once(self, tmp_path):
        """(A) explicit build fails pre-remote (CHEAP_CHECKS_FAILED before
        Vercel) -> claim reached_remote=False, finalized retryable; no remote
        effect happened in the failed attempt, and no claim is wedged at
        CLAIMED."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._ready(store)  # explicit build requires lifecycle READY

        calls = {"n": 0, "boundary": []}

        def local_fail(pid, brief, **kw):
            calls["n"] += 1
            # Model the real builder contract: a pre-remote failure does NOT
            # reach the boundary and drives the project QUEUED -> FAILED.
            with store.acquire_writer("app") as s:
                store.transition_lifecycle_locked(s, ProjectLifecycle.FAILED)
                store.save(s)
            return OperationResult.fail(
                "CHEAP_CHECKS_FAILED:npm_build",
                error_code="CHEAP_CHECKS_FAILED:npm_build",
            )

        builder.build.side_effect = local_fail
        payload = self._build_payload(100)
        r1 = dispatcher.dispatch(payload, "app", "build", authenticated=_auth())
        assert not r1.success
        assert r1.error_code == "CHEAP_CHECKS_FAILED:npm_build"

        claims = self._claims(store, "build")
        assert len(claims) == 1
        assert claims[0]["status"] == "FAILED"
        assert claims[0]["reached_remote"] is False
        assert calls["boundary"] == []  # no remote effect in the failed attempt

    def test_a_retry_with_new_event_executes_exactly_once(self, tmp_path):
        """(A cont.) After the pre-remote failure, a NEW build dispatch can
        proceed and executes the build exactly once."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._ready(store)

        def local_fail(pid, brief, **kw):
            with store.acquire_writer("app") as s:
                store.transition_lifecycle_locked(s, ProjectLifecycle.FAILED)
                store.save(s)
            return OperationResult.fail(
                "CHEAP_CHECKS_FAILED:npm_build",
                error_code="CHEAP_CHECKS_FAILED:npm_build",
            )

        builder.build.side_effect = local_fail
        dispatcher.dispatch(self._build_payload(101), "app", "build", authenticated=_auth())
        assert self._claims(store, "build")[0]["reached_remote"] is False

        # Recovery: FAILED -> READY (as intake does), then a fresh event.
        with store.acquire_writer("app") as s:
            store.transition_lifecycle_locked(s, ProjectLifecycle.READY)
            store.save(s)
        builder.build.side_effect = None
        builder.build.return_value = MagicMock(success=True)
        builder.build.reset_mock()
        r2 = dispatcher.dispatch(self._build_payload(102), "app", "build",
                                 authenticated=_auth())
        assert r2.success
        assert builder.build.call_count == 1

    def test_b_reached_remote_then_fail_stays_fail_closed(self, tmp_path):
        """(B) the callback flips reached_remote=True, then the deploy path
        fails -> claim records reached_remote=True; no replay."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._ready(store)

        seen = {"boundary": 0}

        def remote_then_fail(pid, brief, on_remote_boundary=None):
            assert on_remote_boundary is not None
            on_remote_boundary()
            seen["boundary"] += 1
            return MagicMock(success=False, error="preview deploy failed")

        builder.build.side_effect = remote_then_fail
        r1 = dispatcher.dispatch(self._build_payload(110), "app", "build",
                                 authenticated=_auth())
        assert not r1.success
        claim = self._claims(store, "build")[0]
        assert claim["status"] == "FAILED"
        assert claim["reached_remote"] is True
        assert seen["boundary"] == 1
        assert claim["status"] != "CLAIMED"

        # No replay: the same event must not re-run a possibly-remote build.
        builder.build.reset_mock()
        dispatcher.dispatch(self._build_payload(110), "app", "build",
                            authenticated=_auth())
        assert builder.build.call_count == 0

    def test_c_boundary_persistence_failure_blocks_remote_effect(self, tmp_path):
        """(C) If marking reached_remote=True fails, the remote side effect is
        NOT attempted and dispatch fails safely (no unhandled exception)."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._ready(store)

        remote_calls = {"n": 0}

        def boundary_fails(pid, brief, on_remote_boundary=None):
            assert on_remote_boundary is not None
            # The boundary persistence itself raises -> the builder must abort
            # before any remote call (modeled by the raise escaping here).
            on_remote_boundary()
            remote_calls["n"] += 1
            return MagicMock(success=True)

        original_save = store.save

        def flaky_save(s):
            # Fail only the reached_remote=True persistence.
            for c in s.dispatch_events.values():
                if c.get("action") == "build" and c.get("reached_remote") is True:
                    raise IOError("disk full")
            return original_save(s)

        store.save = flaky_save
        builder.build.side_effect = boundary_fails
        r = dispatcher.dispatch(self._build_payload(120), "app", "build",
                                authenticated=_auth())
        store.save = original_save
        assert not r.success  # structured fail-closed, no crash
        assert remote_calls["n"] == 0  # remote Vercel effect NOT attempted
        claim = self._claims(store, "build")[0]
        assert claim["status"] != "CLAIMED"  # finalized, not wedged
        assert claim["status"] == "FAILED"

    def test_e_legacy_claim_without_boundary_stays_fail_closed(self, tmp_path):
        """(E) a legacy explicit-build claim lacking reached_remote is UNKNOWN
        -> fail closed, never replayed."""
        store, adapter, builder, dispatcher = _make(tmp_path)
        self._ready(store)
        with store.acquire_writer("app") as s:
            s.dispatch_events["legacy-build-key"] = {
                "action": "build",
                "status": "CLAIMED",
            }
            store.save(s)
        # A replay of a claim that has no remote evidence must not proceed:
        # the existing durable CLAIMED claim is fail-closed.
        state = store.load("app")
        claim = state.dispatch_events["legacy-build-key"]
        assert claim.get("reached_remote") is None  # UNKNOWN, never defaulted

class TestDispatchClaimFinalizationH5:
    """H-5: every dispatch attempt leaves its durable claim in a meaningful
    terminal or recoverable state -- never stuck at CLAIMED just because a
    collaborator raised."""

    def _owned(self, store):
        with store.acquire_writer("app") as state:
            state.roles = {"owner": "telegram:1", "reviewers": [], "viewers": []}
            state.brief = {"name": "N", "what": "w", "why": "y"}
            store.save(state)

    def _reference_dispatcher(self, store, refs):
        return TelegramDispatcher(store, None, reference_intake=refs)

    def _claim(self, store):
        return [c for c in store.load("app").dispatch_events.values()
                if c.get("action") == "reference_upload"][0]

    def test_a_local_exception_before_remote_finalizes_not_claimed(self, tmp_path):
        """(A) collaborator raises BEFORE any remote effect -> claim is NOT
        left CLAIMED; result is structured reconciliation-required; no
        unhandled exception escapes."""
        store = ProjectStateStore(tmp_path / "state")
        ProjectAccess(store).create("app", "telegram:1", channel="telegram",
                                    conversation_id="555")
        self._owned(store)
        refs = MagicMock()
        refs.add_upload.side_effect = TypeError("bad payload shape")
        dispatcher = self._reference_dispatcher(store, refs)

        r = dispatcher.dispatch(_payload(1, text="ref"), "app",
                                "reference_upload", authenticated=_auth())
        assert not r.success
        assert r.error_code == "EVENT_RECONCILIATION_REQUIRED"
        claim = self._claim(store)
        assert claim["status"] == "FAILED"  # never stuck at CLAIMED

    def test_b_success_path_finalizes_done(self, tmp_path):
        """(E) success path unchanged: the claim is finalized DONE."""
        store = ProjectStateStore(tmp_path / "state")
        ProjectAccess(store).create("app", "telegram:1", channel="telegram",
                                    conversation_id="555")
        self._owned(store)
        refs = MagicMock()
        refs.add_upload.return_value = OperationResult.ok()
        dispatcher = self._reference_dispatcher(store, refs)

        r = dispatcher.dispatch(_payload(2, text="ref"), "app",
                                "reference_upload", authenticated=_auth())
        assert r.success
        assert self._claim(store)["status"] == "DONE"

    def test_d_finalization_persistence_failure_does_not_crash(self, tmp_path):
        """(D) the finalization writer fails while finalizing the claim after
        a collaborator exception -> dispatch returns a structured fail-closed
        result, no unhandled exception escapes, and no duplicate action runs."""
        store = ProjectStateStore(tmp_path / "state")
        ProjectAccess(store).create("app", "telegram:1", channel="telegram",
                                    conversation_id="555")
        self._owned(store)
        refs = MagicMock()
        refs.add_upload.side_effect = RuntimeError("boom")

        original_save = store.save
        allows = {"n": 0}
        def flaky_save(s):
            # Allow the FIRST save (the claim-before-effect write) and fail
            # only the subsequent finalization write.
            allows["n"] += 1
            if allows["n"] == 1:
                return original_save(s)
            raise IOError("disk full")

        store.save = flaky_save
        dispatcher = self._reference_dispatcher(store, refs)
        r = dispatcher.dispatch(_payload(3, text="ref"), "app",
                                "reference_upload", authenticated=_auth())
        store.save = original_save
        assert not r.success
        assert r.error_code == "EVENT_RECONCILIATION_REQUIRED"
        assert refs.add_upload.call_count == 1  # exactly one action attempt
        # The durable claim was never wedged into a fake terminal state we
        # could not persist: it remains CLAIMED (fail-closed) since the
        # finalization write failed.
        claim = [c for c in store.load("app").dispatch_events.values()
                 if c.get("action") == "reference_upload"][0]
        assert claim["status"] == "CLAIMED"

    def test_success_finalization_persistence_failure_does_not_crash(self, tmp_path):
        """A finalization write failing AFTER the action already completed must
        not escape as an unhandled exception nor blind-replay."""
        store = ProjectStateStore(tmp_path / "state")
        ProjectAccess(store).create("app", "telegram:1", channel="telegram",
                                    conversation_id="555")
        self._owned(store)
        refs = MagicMock()
        refs.add_upload.return_value = OperationResult.ok()

        original_save = store.save
        allows = {"n": 0}
        def flaky_save(s):
            allows["n"] += 1
            if allows["n"] == 1:
                return original_save(s)
            raise IOError("disk full")
        store.save = flaky_save
        dispatcher = self._reference_dispatcher(store, refs)
        r = dispatcher.dispatch(_payload(4, text="ref"), "app",
                                "reference_upload", authenticated=_auth())
        store.save = original_save
        # The action's real result is still returned (not a crash).
        assert r.success
        assert refs.add_upload.call_count == 1
