"""N6-B — Decision Engine intake modes (disabled / shadow / active).

Proves the N6 requirement that the Decision Engine's intake classification can
be disabled, shadowed, or active, and that:

* ``disabled`` and ``shadow`` produce an **identical** IntakeResult — shadow is
  observational only and changes nothing.
* ``active`` may set an advisory routing hint but **FAST remains authoritative**
  for scope, facts, requirements, and readiness.
* a provider failure never breaks intake in any mode.

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.channels.telegram import NormalizedMessage  # noqa: E402
from app.core.decision_engine import DecisionEngine  # noqa: E402
from app.core.decision_modes import (  # noqa: E402
    DecisionEngineConfig,
    DecisionEngineMode,
)
from app.core.decision_provider import (  # noqa: E402
    DecisionProviderRegistry,
    IntakeClassification,
    INTAKE_NEW,
    INTAKE_REVISION,
)
from app.core.intake import IntakeProcessor  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402


class _FixedProvider:
    """A provider that returns a fixed intake classification."""

    def __init__(self, kind: str, has_reference: bool = False):
        self._kind = kind
        self._has_reference = has_reference

    @property
    def name(self) -> str:
        return "fixed"

    def classify_intake(self, request_text, context):
        return IntakeClassification(
            kind=self._kind, has_reference=self._has_reference, confidence=0.9
        )

    def propose_design_candidates(self, context, *, limit):
        return ()


class _ExplodingProvider:
    @property
    def name(self) -> str:
        return "exploding"

    def classify_intake(self, request_text, context):
        raise RuntimeError("provider down")

    def propose_design_candidates(self, context, *, limit):
        raise RuntimeError("provider down")


def _engine(provider, *, name=None):
    registry = DecisionProviderRegistry()
    if provider is not None:
        registry.register(provider)
    # Resolve the provider by its OWN name so an exploding provider is actually
    # consulted (and its failure exercised) rather than silently falling back.
    return DecisionEngine(registry, provider_name=name or getattr(provider, "name", None))


def _message(text: str) -> NormalizedMessage:
    return NormalizedMessage(event_id="e1", user_id="u1", conversation_id="c1",
                             project_id="p1", text=text)


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.store = ProjectStateStore(Path(self._tmp.name))
        # Register a project so persisted-brief lookups are well-defined.
        from app.core.state import ProjectState
        self.store.save(ProjectState(project_id="p1"))

    def tearDown(self):
        self._tmp.cleanup()

    def _processor(self, *, engine=None, mode=DecisionEngineMode.DISABLED):
        cfg = DecisionEngineConfig(intake_mode=mode)
        return IntakeProcessor(self.store, decision_engine=engine,
                               decision_engine_config=cfg)


class DisabledTests(_Base):
    def test_disabled_never_consults_the_engine(self):
        proc = self._processor(engine=_engine(_FixedProvider(INTAKE_REVISION)),
                               mode=DecisionEngineMode.DISABLED)
        result = proc.process(_message("buat website baru untuk kafe"))
        self.assertIsNone(proc.last_shadow_decision)
        self.assertIsNone(proc.last_intake_routing_hint)
        self.assertIsNotNone(result)

    def test_no_engine_is_identical_to_disabled(self):
        a = self._processor(engine=None, mode=DecisionEngineMode.DISABLED)
        b = self._processor(engine=_engine(_FixedProvider(INTAKE_NEW)),
                            mode=DecisionEngineMode.DISABLED)
        ra = a.process(_message("website kafe baru"))
        rb = b.process(_message("website kafe baru"))
        self.assertEqual(ra.brief, rb.brief)
        self.assertEqual(ra.scope, rb.scope)
        self.assertEqual(ra.readiness, rb.readiness)


class ShadowTests(_Base):
    def test_shadow_records_but_changes_nothing(self):
        text = "website kafe baru"
        disabled = self._processor(engine=None, mode=DecisionEngineMode.DISABLED)
        shadow = self._processor(
            engine=_engine(_FixedProvider(INTAKE_REVISION)),
            mode=DecisionEngineMode.SHADOW,
        )
        rd = disabled.process(_message(text))
        rs = shadow.process(_message(text))
        # The shadow decision is recorded...
        self.assertIsNotNone(shadow.last_shadow_decision)
        decision = shadow.last_shadow_decision
        assert decision is not None
        self.assertEqual(decision.kind, INTAKE_REVISION)
        # ...but the result is identical to disabled.
        self.assertEqual(rd.brief, rs.brief)
        self.assertEqual(rd.scope, rs.scope)
        self.assertEqual(rd.readiness, rs.readiness)
        self.assertEqual(rd.clarification_question, rs.clarification_question)

    def test_shadow_sets_no_routing_hint(self):
        proc = self._processor(engine=_engine(_FixedProvider(INTAKE_NEW)),
                               mode=DecisionEngineMode.SHADOW)
        proc.process(_message("website baru"))
        self.assertIsNone(proc.last_intake_routing_hint)

    def test_shadow_failure_never_breaks_intake(self):
        proc = self._processor(engine=_engine(_ExplodingProvider()),
                               mode=DecisionEngineMode.SHADOW)
        result = proc.process(_message("website kafe"))
        # Intake is never broken; the shadow record is the engine's safe fallback.
        self.assertIsNotNone(result)
        decision = proc.last_shadow_decision
        assert decision is not None
        self.assertEqual(decision.source, "fallback")
        self.assertTrue(decision.degraded)


class ActiveTests(_Base):
    def test_active_sets_a_routing_hint(self):
        proc = self._processor(engine=_engine(_FixedProvider(INTAKE_REVISION, True)),
                               mode=DecisionEngineMode.ACTIVE)
        proc.process(_message("ubah hero jadi lebih kecil"))
        hint = proc.last_intake_routing_hint
        assert hint is not None
        self.assertEqual(hint["kind"], INTAKE_REVISION)
        self.assertTrue(hint["has_reference"])

    def test_active_does_not_change_scope_brief_or_readiness(self):
        text = "website kafe baru untuk promo"
        disabled = self._processor(engine=None, mode=DecisionEngineMode.DISABLED)
        active = self._processor(
            engine=_engine(_FixedProvider(INTAKE_REVISION)),  # deliberately "wrong"
            mode=DecisionEngineMode.ACTIVE,
        )
        rd = disabled.process(_message(text))
        ra = active.process(_message(text))
        # FAST remains authoritative: the decision engine's disagreement with the
        # actual new-website turn changes nothing about the result.
        self.assertEqual(rd.brief, ra.brief)
        self.assertEqual(rd.scope, ra.scope)
        self.assertEqual(rd.readiness, ra.readiness)
        self.assertEqual(rd.clarification_question, ra.clarification_question)

    def test_active_sets_no_shadow_record(self):
        proc = self._processor(engine=_engine(_FixedProvider(INTAKE_NEW)),
                               mode=DecisionEngineMode.ACTIVE)
        proc.process(_message("website baru"))
        self.assertIsNone(proc.last_shadow_decision)

    def test_active_failure_never_breaks_intake(self):
        proc = self._processor(engine=_engine(_ExplodingProvider()),
                               mode=DecisionEngineMode.ACTIVE)
        result = proc.process(_message("website kafe"))
        # Intake is never broken; the active hint carries the safe fallback.
        self.assertIsNotNone(result)
        hint = proc.last_intake_routing_hint
        assert hint is not None
        self.assertEqual(hint["source"], "fallback")
        self.assertTrue(hint["degraded"])

    def test_active_provider_outage_falls_back_to_deterministic(self):
        """An exploding provider still yields a hint, via the safe fallback."""
        proc = self._processor(engine=_engine(_ExplodingProvider()),
                               mode=DecisionEngineMode.ACTIVE)
        proc.process(_message("website baru"))
        hint = proc.last_intake_routing_hint
        assert hint is not None
        self.assertEqual(hint["source"], "fallback")
        self.assertTrue(hint["degraded"])


class EngineRaiseTests(_Base):
    """The engine itself may raise; intake must still survive (not just provider)."""

    class _RaisingEngine:
        def decide_intake(self, *a, **k):
            raise RuntimeError("engine exploded")

    def test_shadow_engine_raise_never_breaks_intake(self):
        proc = self._processor(engine=self._RaisingEngine(),
                               mode=DecisionEngineMode.SHADOW)
        result = proc.process(_message("website kafe"))
        self.assertIsNotNone(result)
        self.assertIsNone(proc.last_shadow_decision)

    def test_active_engine_raise_never_breaks_intake(self):
        proc = self._processor(engine=self._RaisingEngine(),
                               mode=DecisionEngineMode.ACTIVE)
        result = proc.process(_message("website kafe"))
        self.assertIsNotNone(result)
        self.assertIsNone(proc.last_intake_routing_hint)


class InjectedEngineDefaultTests(_Base):
    """An injected engine with NO explicit config defaults to shadow (N3)."""

    def test_injected_engine_without_config_is_shadow(self):
        proc = IntakeProcessor(
            self.store, decision_engine=_engine(_FixedProvider(INTAKE_REVISION))
        )
        self.assertIs(proc.decision_engine_config.intake_mode,
                      DecisionEngineMode.SHADOW)
        proc.process(_message("website kafe"))
        self.assertIsNotNone(proc.last_shadow_decision)
        self.assertIsNone(proc.last_intake_routing_hint)

    def test_no_engine_without_config_is_disabled(self):
        proc = IntakeProcessor(self.store)
        self.assertIs(proc.decision_engine_config.intake_mode,
                      DecisionEngineMode.DISABLED)


class RuntimeBuilderTests(unittest.TestCase):
    """The runtime helper builds no engine when both modes are disabled."""

    def test_disabled_builds_no_engine(self):
        from app.runtime import _build_decision_engine

        self.assertIsNone(
            _build_decision_engine(DecisionEngineConfig(), openviking_adapter=None)
        )

    def test_enabled_builds_an_engine(self):
        from app.runtime import _build_decision_engine

        engine = _build_decision_engine(
            DecisionEngineConfig(intake_mode=DecisionEngineMode.SHADOW),
            openviking_adapter=None,
        )
        self.assertIsNotNone(engine)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
