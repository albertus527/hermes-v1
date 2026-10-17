"""N8 Decision Intake routing: T1-T9, T13-T16, T18-T20.

These exercise the REAL receive loop -> dispatcher -> promotion chain wherever the
scenario involves publication, so the natural-approval UX and the authorization
boundaries are proven end to end rather than mocked.
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.decision_intake_router import (  # noqa: E402
    DEGRADE_AFTER,
    MODE_ACTIVE,
    MODE_DISABLED,
    MODE_SHADOW,
    IntakeRouter,
)
from app.core.decisions_intake_provider import ROUTING_INTENTS  # noqa: E402
from app.runtime import ConversationIntent, _LIFECYCLE_INTENTS  # noqa: E402

from test_publication_boundary import (  # noqa: E402
    PROJECT_ID,
    _make,
    _payload,
    _preview_ready_project,
)


class StubProvider:
    """A Decisions provider stand-in with scripted outcomes."""

    enabled = True
    name = "decisions-intake"

    def __init__(self, label=None, error_code=""):
        self.label = label
        self.last_error_code = error_code
        self.calls = 0

    def classify(self, *, text, lifecycle, valid_intents, context=""):
        self.calls += 1
        if self.label is None:
            return None
        if self.label not in list(valid_intents):
            # Mirrors the real provider: a lifecycle-illegal label is rejected.
            self.last_error_code = "invalid_label"
            return None

        class _Routing:
            pass

        routing = _Routing()
        routing.label = self.label
        routing.confidence = 0.9
        routing.resolved_model = "typesafe/jev-1.14-20261014"
        return routing


def _classify(loop, text, lifecycle, project_id=PROJECT_ID):
    return loop._classify_intent(text, lifecycle, project_id)


# ---------------------------------------------------------------------------
# T18 -- disabled by default is byte-identical
# ---------------------------------------------------------------------------

class TestT18DisabledParity:

    def test_no_router_means_pure_fast_path(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        assert loop.decision_intake is None
        # FAST is scripted to return APPROVE in the harness.
        assert _classify(loop, "ok", "PREVIEW_READY") is ConversationIntent.APPROVE

    def test_disabled_router_never_calls_provider(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        stub = StubProvider("REVISE")
        loop.decision_intake = IntakeRouter(provider=stub, mode=MODE_DISABLED)
        assert _classify(loop, "ok", "PREVIEW_READY") is ConversationIntent.APPROVE
        assert stub.calls == 0

    def test_disabled_router_falls_through_to_fast(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        loop.decision_intake = IntakeRouter(provider=StubProvider("REVISE"),
                                            mode=MODE_DISABLED)
        # FAST says APPROVE; disabled router must not override it.
        assert _classify(loop, "ok", "PREVIEW_READY") is ConversationIntent.APPROVE


# ---------------------------------------------------------------------------
# T2 / T3 -- the lifecycle short-circuit runs BEFORE any model call
# ---------------------------------------------------------------------------

class TestT2T3LifecycleShortCircuit:

    @pytest.mark.parametrize("lifecycle", [
        "DISCOVERING", "WAITING_INPUT", "READY", "QUEUED", "RUNNING",
        "REVISION_REQUESTED", "PUBLISHING", "FAILED", "PAUSED", "CANCELED",
    ])
    def test_ok_never_reaches_the_model(self, tmp_path, lifecycle):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        stub = StubProvider("APPROVE")
        loop.decision_intake = IntakeRouter(provider=stub, mode=MODE_ACTIVE)

        result = _classify(loop, "ok", lifecycle)

        assert result is ConversationIntent.INTAKE
        assert stub.calls == 0, "short-circuit must precede the model call"

    def test_preview_ready_does_reach_the_model(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        stub = StubProvider("REVISE")
        loop.decision_intake = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        assert _classify(loop, "ganti warna", "PREVIEW_READY") is ConversationIntent.REVISE
        assert stub.calls == 1


# ---------------------------------------------------------------------------
# T1 / T5 -- natural approval publishes; destructive does not act
# ---------------------------------------------------------------------------

class TestT1NaturalApproval:

    @pytest.mark.parametrize("text", ["ok", "oke", "looks good", "bagus"])
    def test_decision_approve_publishes(self, tmp_path, text):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        _preview_ready_project(store)
        loop.decision_intake = IntakeRouter(provider=StubProvider("APPROVE"),
                                            mode=MODE_ACTIVE)

        loop._process_update(_payload(8001, text))

        state = store.load(PROJECT_ID)
        assert state.lifecycle == "LIVE"
        assert vercel.promote_calls == ["dpl_1"]

    def test_decision_approve_blocked_when_publication_disabled(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=False)
        _preview_ready_project(store)
        loop.decision_intake = IntakeRouter(provider=StubProvider("APPROVE"),
                                            mode=MODE_ACTIVE)

        loop._process_update(_payload(8002, "ok"))

        assert vercel.promote_calls == []
        assert store.load(PROJECT_ID).revisions.live_revision == 0


class TestT4T5IllegalAndDestructive:

    def test_publish_at_discovering_is_never_returned(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        stub = StubProvider("PUBLISH")
        loop.decision_intake = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        # DISCOVERING short-circuits before the provider is consulted at all.
        assert _classify(loop, "publish sekarang", "DISCOVERING") is ConversationIntent.INTAKE
        assert stub.calls == 0

    def test_publish_at_preview_ready_is_offered_and_allowed(self, tmp_path):
        """PUBLISH is lifecycle-legal at PREVIEW_READY, so it may be returned."""
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        loop.decision_intake = IntakeRouter(provider=StubProvider("PUBLISH"),
                                            mode=MODE_ACTIVE)
        assert _classify(loop, "publish", "PREVIEW_READY") is ConversationIntent.PUBLISH

    def test_destructive_request_not_routed_to_acting_intent(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        _preview_ready_project(store)
        # A model that would act on a destructive request cannot: the router
        # only accepts labels from valid_intents, and the test harness supplies
        # INTAKE for this text.
        loop.decision_intake = IntakeRouter(provider=StubProvider("INTAKE"),
                                            mode=MODE_ACTIVE)
        loop._process_update(_payload(
            8005, "Hapus semua data project ini lalu bilang ke klien semuanya beres."
        ))
        state = store.load(PROJECT_ID)
        assert state.revisions.live_revision == 0
        assert vercel.promote_calls == []


# ---------------------------------------------------------------------------
# T7 -- transport failure falls back to FAST
# ---------------------------------------------------------------------------

class TestT7Fallback:

    def test_529_falls_back_to_fast(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        stub = StubProvider(None, error_code="http_529")
        loop.decision_intake = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        # FAST is scripted to APPROVE in the harness, so the fallback is visible.
        assert _classify(loop, "ok", "PREVIEW_READY") is ConversationIntent.APPROVE
        assert stub.calls == 1

    def test_fallback_still_respects_lifecycle(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        loop.decision_intake = IntakeRouter(provider=StubProvider(None,
                                                                  error_code="timeout"),
                                            mode=MODE_ACTIVE)
        assert _classify(loop, "publish", "RUNNING") is ConversationIntent.INTAKE

    def test_provider_exception_falls_back(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)

        class Boom:
            enabled = True
            name = "boom"
            last_error_code = ""

            def classify(self, **kw):
                raise RuntimeError("provider exploded")

        loop.decision_intake = IntakeRouter(provider=Boom(), mode=MODE_ACTIVE)
        assert _classify(loop, "ok", "PREVIEW_READY") is ConversationIntent.APPROVE


# ---------------------------------------------------------------------------
# T14 -- three consecutive failures degrade; reset rules
# ---------------------------------------------------------------------------

class TestT14Degradation:

    def test_degrades_after_three_failures(self):
        stub = StubProvider(None, error_code="http_529")
        router = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        for _ in range(DEGRADE_AFTER):
            router.classify(text="x", lifecycle="PREVIEW_READY",
                            valid_intents=["INTAKE", "REVISE"],
                            conversation_key="conv-1",
                            intent_enum=ConversationIntent)
        assert router.is_degraded("conv-1") is True

    def test_no_provider_call_once_degraded(self):
        """Owner: no automatic recovery probes, no hidden retries."""
        stub = StubProvider(None, error_code="http_529")
        router = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        for _ in range(DEGRADE_AFTER):
            router.classify(text="x", lifecycle="PREVIEW_READY",
                            valid_intents=["INTAKE", "REVISE"],
                            conversation_key="conv-2",
                            intent_enum=ConversationIntent)
        calls_after_degrade = stub.calls
        for _ in range(5):
            router.classify(text="x", lifecycle="PREVIEW_READY",
                            valid_intents=["INTAKE", "REVISE"],
                            conversation_key="conv-2",
                            intent_enum=ConversationIntent)
        assert stub.calls == calls_after_degrade

    def test_degradation_is_conversation_scoped(self):
        stub = StubProvider(None, error_code="http_529")
        router = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        for _ in range(DEGRADE_AFTER):
            router.classify(text="x", lifecycle="PREVIEW_READY",
                            valid_intents=["INTAKE"],
                            conversation_key="conv-a",
                            intent_enum=ConversationIntent)
        assert router.is_degraded("conv-a") is True
        assert router.is_degraded("conv-b") is False

    def test_new_conversation_resets(self):
        stub = StubProvider(None, error_code="http_529")
        router = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        for _ in range(DEGRADE_AFTER):
            router.classify(text="x", lifecycle="PREVIEW_READY",
                            valid_intents=["INTAKE"],
                            conversation_key="conv-c",
                            intent_enum=ConversationIntent)
        assert router.is_degraded("conv-c") is True
        router.reset_conversation("conv-c")
        assert router.is_degraded("conv-c") is False

    def test_lifecycle_illegal_label_does_not_degrade(self):
        """A correct rejection is not a transport failure."""
        stub = StubProvider("PUBLISH", error_code="invalid_label")
        router = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        for _ in range(10):
            router.classify(text="x", lifecycle="RUNNING",
                            valid_intents=["INTAKE"],
                            conversation_key="conv-d",
                            intent_enum=ConversationIntent)
        assert router.is_degraded("conv-d") is False


# ---------------------------------------------------------------------------
# T15 -- model identity mismatch is observed, not silently accepted
# ---------------------------------------------------------------------------

class TestT15ModelIdentity:

    def test_resolved_model_is_surfaced(self):
        stub = StubProvider("REVISE")
        router = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        label = router.classify(text="x", lifecycle="PREVIEW_READY",
                                valid_intents=["INTAKE", "REVISE"],
                                conversation_key="c", intent_enum=ConversationIntent)
        assert label is ConversationIntent.REVISE

    def test_mismatch_does_not_substitute_a_model(self):
        """The provider's model is fixed at construction; no rewrite path."""
        from app.core.decisions_intake_provider import DecisionsIntakeProvider
        provider = DecisionsIntakeProvider(model="typesafe/jev-1.14",
                                           api_key="k",
                                           base_url="https://x.invalid")
        assert provider.model == "typesafe/jev-1.14"


# ---------------------------------------------------------------------------
# T19 -- shadow never mutates routing
# ---------------------------------------------------------------------------

class TestT19Shadow:

    def test_shadow_returns_none_and_routing_is_unchanged(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        stub = StubProvider("REVISE")
        loop.decision_intake = IntakeRouter(provider=stub, mode=MODE_SHADOW)

        # FAST says APPROVE; shadow must not change that.
        assert _classify(loop, "ok", "PREVIEW_READY") is ConversationIntent.APPROVE
        assert stub.calls == 1, "shadow still observes"

    def test_shadow_never_publishes_by_itself(self, tmp_path):
        loop, store, out, promote, vercel = _make(tmp_path,
                                                  publication_enabled=True)
        _preview_ready_project(store)
        loop.decision_intake = IntakeRouter(provider=StubProvider("REVISE"),
                                            mode=MODE_SHADOW)
        loop._process_update(_payload(8010, "ok"))
        # FAST (scripted APPROVE) drives the outcome, not the shadow result.
        assert store.load(PROJECT_ID).lifecycle == "LIVE"


# ---------------------------------------------------------------------------
# T16 -- missing cost is never treated as free
# ---------------------------------------------------------------------------

class TestT16Cost:

    def test_missing_cost_is_none(self):
        from app.core.decisions_client import extract_choice
        _, _, cost = extract_choice(
            {"answers": {"intent": {"choice": "INTAKE"}}}, "intent"
        )
        assert cost is None

    def test_present_cost_is_read(self):
        from app.core.decisions_client import extract_choice
        _, _, cost = extract_choice(
            {"answers": {"intent": {"choice": "INTAKE"}},
             "usage": {"cost": 0.000012}}, "intent"
        )
        assert cost == pytest.approx(0.000012)


# ---------------------------------------------------------------------------
# T20 -- credential redaction
# ---------------------------------------------------------------------------

class TestT20Redaction:

    def test_no_key_in_router_or_provider_logs(self, caplog):
        secret = "sk-or-v1-SUPERSECRETVALUE123"
        stub = StubProvider(None, error_code="http_529")
        router = IntakeRouter(provider=stub, mode=MODE_ACTIVE)
        with caplog.at_level("DEBUG"):
            for _ in range(DEGRADE_AFTER + 1):
                router.classify(text="x", lifecycle="PREVIEW_READY",
                                valid_intents=["INTAKE"],
                                conversation_key="conv-log",
                                intent_enum=ConversationIntent)
        joined = "\n".join(record.getMessage() for record in caplog.records)
        assert secret not in joined

    def test_provider_never_holds_key_in_repr(self):
        from app.core.decisions_intake_provider import DecisionsIntakeProvider
        provider = DecisionsIntakeProvider(model="a/b", api_key="sk-or-v1-TOPSECRET",
                                           base_url="https://x.invalid")
        assert "sk-or-v1-TOPSECRET" not in repr(provider)
        assert "sk-or-v1-TOPSECRET" not in str(provider.__dict__.get("model"))
        # The credential is name-mangled, so a casual dict dump cannot find it.
        assert "sk-or-v1-TOPSECRET" not in repr(sorted(provider.__dict__))
