"""N3 — Decision Engine & OpenViking.

Covers the N3 deliverables and, above all, the three invariants the roadmap
makes gates:

* a provider outage never breaks the safe FAST fallback;
* invalid or untrusted design candidates never override requirements;
* no forced selection when no suitable design exists (abstention).

Plus: the provider interface is replaceable, retrieval is bounded, scores are
validated, and provenance is required end-to-end.

No live LLM, browser, npm, network, or subprocess is required.
"""
from __future__ import annotations

import unittest
from dataclasses import dataclass
from typing import Any, Mapping, Sequence

from app.core.decision_engine import (
    ABSTAIN_BELOW_FLOOR,
    ABSTAIN_NO_CANDIDATES,
    ABSTAIN_NO_PROVENANCE,
    DEFAULT_SCORE_FLOOR,
    MAX_CANDIDATES,
    SOURCE_FALLBACK,
    SOURCE_PROVIDER,
    DecisionEngine,
)
from app.core.decision_provider import (
    DETERMINISTIC_PROVIDER_NAME,
    INTAKE_NEW,
    INTAKE_REVISION,
    DecisionProviderError,
    DecisionProviderRegistry,
    DesignCandidate,
    DeterministicDecisionProvider,
    IntakeClassification,
)
from app.core.openviking_decision import OpenVikingDecisionProvider


def _candidate(**over: Any) -> DesignCandidate:
    base: dict = dict(
        candidate_id="c1",
        label="Editorial",
        descriptor="warm editorial layout",
        palette=("#101014",),
        source_uri="viking://wb-design/x",
        source_revision="abc123",
        score=0.9,
    )
    base.update(over)
    return DesignCandidate(**base)


# --- providers used to exercise the engine ---------------------------------


class _ExplodingProvider:
    name = "exploding"

    def classify_intake(self, request_text, context):
        raise DecisionProviderError("BOOM", "provider is down")

    def propose_design_candidates(self, context, *, limit):
        raise DecisionProviderError("BOOM", "provider is down")


class _HostileProvider:
    """Returns unprovenanced, out-of-range, duplicate and non-object items."""

    name = "hostile"

    def classify_intake(self, request_text, context):
        return IntakeClassification(kind=INTAKE_REVISION, confidence=0.9)

    def propose_design_candidates(self, context, *, limit):
        return [
            _candidate(candidate_id="ok", source_uri="", source_revision=""),  # no provenance
            _candidate(candidate_id="bad_score", score=5.0),  # out of range
            _candidate(candidate_id="ok", score=0.8),  # duplicate id
            {"not": "a candidate"},  # wrong shape
            _candidate(candidate_id="good", score=0.7),  # the only survivor
        ]


class _StubAdapter:
    """A stub adapter recording the call contract (so scope is asserted)."""

    def __init__(self, items, *, raise_on=None):
        self.items = items
        self.raise_on = raise_on
        self.last_scope = None
        self.last_project_id = None

    def retrieve_context(self, query, project_id, scope=None, budget=None):
        self.last_project_id = project_id
        self.last_scope = scope
        if self.raise_on:
            raise self.raise_on

        class _Result:
            items: Sequence[Any] = ()

        r = _Result()
        r.items = self.items
        return r


@dataclass
class _Item:
    """Mirrors the REAL ``app.core.openviking_library.ContextItem`` schema.

    The previous fake carried ``source_uri``/``excerpt``/``relevance`` — fields
    the real contract does NOT have — which is precisely why the live
    integration defect (reading ``item.source_uri`` when the real field is
    ``uri``) went undetected. Keeping this in lockstep with ``ContextItem``
    makes the unit tests exercise the real field names.
    """

    uri: str = "viking://resources/website-builder/projects/wb-design/design_dna/s1"
    source_id: str = "s1"
    source_revision: str = "rev1"
    trust: str = "reviewed"
    category: str = "design_dna"
    level: int = 1
    score: float = 0.9
    title: str = "summary"
    body: str = "excerpt"
    summary: str = "summary"
    estimated_tokens: int = 10


class RegistryTests(unittest.TestCase):
    def test_deterministic_provider_is_always_registered(self):
        reg = DecisionProviderRegistry()
        self.assertEqual(reg.fallback.name, DETERMINISTIC_PROVIDER_NAME)
        self.assertIsNotNone(reg.resolve("nonexistent"))

    def test_resolve_falls_back_for_unknown_name(self):
        reg = DecisionProviderRegistry()
        self.assertEqual(reg.resolve("nope").name, DETERMINISTIC_PROVIDER_NAME)


class IntakeDecisionTests(unittest.TestCase):
    def setUp(self):
        self.engine = DecisionEngine()

    def test_revision_requires_existing_source(self):
        d = self.engine.decide_intake("please change the header", {"has_source": True})
        self.assertEqual(d.kind, INTAKE_REVISION)
        self.assertEqual(d.source, SOURCE_PROVIDER)

    def test_change_without_source_is_not_a_revision(self):
        d = self.engine.decide_intake("change the header", {"has_source": False})
        self.assertNotEqual(d.kind, INTAKE_REVISION)

    def test_reference_detection(self):
        d = self.engine.decide_intake("here is a reference https://x.test", {})
        self.assertTrue(d.has_reference)

    def test_provider_outage_uses_fallback_and_never_raises(self):
        reg = DecisionProviderRegistry()
        reg.register(_ExplodingProvider())
        engine = DecisionEngine(reg, provider_name="exploding")
        d = engine.decide_intake("build a new website", {})
        self.assertEqual(d.source, SOURCE_FALLBACK)
        self.assertTrue(d.degraded)
        self.assertEqual(d.error_code, "BOOM")
        self.assertEqual(d.provider, DETERMINISTIC_PROVIDER_NAME)


class DesignDecisionTests(unittest.TestCase):
    def test_engine_abstains_when_no_candidates(self):
        engine = DecisionEngine()
        d = engine.decide_design({})
        self.assertTrue(d.abstained)
        self.assertIsNone(d.selected)
        self.assertEqual(d.abstain_reason, ABSTAIN_NO_CANDIDATES)

    def test_deterministic_provider_derives_from_approved_dna_only(self):
        engine = DecisionEngine()
        d = engine.decide_design(
            {
                "design_dna": {
                    "brand_personality": "warm editorial",
                    "palette": {"primary": "#101014"},
                },
                "source_revision": 3,
            }
        )
        self.assertFalse(d.abstained)
        assert d.selected is not None
        self.assertEqual(d.selected.source_uri, "design-dna.json")
        self.assertEqual(d.selected.source_revision, "3")

    def test_engine_never_returns_unprovenanced_candidate(self):
        reg = DecisionProviderRegistry()
        reg.register(_HostileProvider())
        engine = DecisionEngine(reg, provider_name="hostile")
        d = engine.decide_design({})
        # Only the "good" candidate survives; "ok" (no provenance) is dropped.
        self.assertFalse(d.abstained)
        assert d.selected is not None
        self.assertEqual(d.selected.candidate_id, "good")
        reasons = {v.reason for v in d.dropped}
        self.assertIn(ABSTAIN_NO_PROVENANCE, reasons)

    def test_out_of_range_score_is_not_trusted(self):
        engine = DecisionEngine()
        d = engine._rank(
            [_candidate(candidate_id="bad", score=5.0)],
            source=SOURCE_PROVIDER,
            provider_name="x",
            degraded=False,
            error_code="",
        )
        self.assertTrue(d.abstained)

    def test_nan_and_inf_scores_are_not_trusted(self):
        for bad in (float("nan"), float("inf"), "not-a-number", None):
            with self.subTest(bad=bad):
                engine = DecisionEngine()
                d = engine._rank(
                    [_candidate(candidate_id="bad", score=bad)],
                    source=SOURCE_PROVIDER,
                    provider_name="x",
                    degraded=False,
                    error_code="",
                )
                self.assertTrue(d.abstained)

    def test_below_floor_abstains(self):
        engine = DecisionEngine()
        d = engine._rank(
            [_candidate(candidate_id="low", score=DEFAULT_SCORE_FLOOR - 0.01)],
            source=SOURCE_PROVIDER,
            provider_name="x",
            degraded=False,
            error_code="",
        )
        self.assertTrue(d.abstained)
        self.assertEqual(d.abstain_reason, ABSTAIN_BELOW_FLOOR)

    def test_ranking_is_deterministic_by_score_then_id(self):
        engine = DecisionEngine()
        d = engine._rank(
            [
                _candidate(candidate_id="b", score=0.8),
                _candidate(candidate_id="a", score=0.8),
                _candidate(candidate_id="c", score=0.9),
            ],
            source=SOURCE_PROVIDER,
            provider_name="x",
            degraded=False,
            error_code="",
        )
        self.assertEqual([c.candidate_id for c in d.ranked], ["c", "a", "b"])

    def test_retrieval_is_bounded_by_max_candidates(self):
        engine = DecisionEngine(max_candidates=MAX_CANDIDATES + 100)
        # The constructor clamps to the hard ceiling.
        self.assertEqual(engine.max_candidates, MAX_CANDIDATES)
        d = engine.decide_design(
            {"design_dna": {"brand_personality": "x"}}, limit=1000
        )
        self.assertLessEqual(len(d.ranked), MAX_CANDIDATES)

    def test_provider_outage_falls_back_and_still_returns(self):
        reg = DecisionProviderRegistry()
        reg.register(_ExplodingProvider())
        engine = DecisionEngine(reg, provider_name="exploding")
        d = engine.decide_design(
            {"design_dna": {"brand_personality": "warm"}}
        )
        self.assertEqual(d.source, SOURCE_FALLBACK)
        self.assertTrue(d.degraded)
        self.assertEqual(d.error_code, "BOOM")
        # The fallback still produced the DNA-derived candidate.
        self.assertFalse(d.abstained)

    def test_provider_outage_with_no_dna_abstains_safely(self):
        reg = DecisionProviderRegistry()
        reg.register(_ExplodingProvider())
        engine = DecisionEngine(reg, provider_name="exploding")
        d = engine.decide_design({})
        self.assertTrue(d.abstained)
        self.assertIsNone(d.selected)

    def test_decision_serialises_to_dict(self):
        engine = DecisionEngine()
        d = engine.decide_design(
            {"design_dna": {"brand_personality": "warm"}}
        )
        payload = d.to_dict()
        self.assertIn("selected", payload)
        self.assertIn("abstained", payload)


class OpenVikingProviderTests(unittest.TestCase):
    def test_disabled_provider_returns_empty(self):
        provider = OpenVikingDecisionProvider(None)
        self.assertEqual(provider.propose_design_candidates({"query": "x"}, limit=3), ())

    def test_no_query_returns_empty(self):
        provider = OpenVikingDecisionProvider(_StubAdapter([_Item()]))
        self.assertEqual(provider.propose_design_candidates({}, limit=3), ())

    def test_items_without_provenance_are_dropped(self):
        items = [
            _Item(source_id="ok", uri="viking://a", source_revision="r1"),
            _Item(source_id="nouri", uri="", source_revision="r1"),
            _Item(source_id="norev", uri="viking://b", source_revision=""),
        ]
        provider = OpenVikingDecisionProvider(_StubAdapter(items))
        out = provider.propose_design_candidates(
            {"query": "warm", "library_project_id": "wb-design"}, limit=5
        )
        self.assertEqual([c.candidate_id for c in out], ["ok"])

    def test_adapter_outage_returns_empty_not_raise(self):
        provider = OpenVikingDecisionProvider(_StubAdapter([], raise_on=RuntimeError("down")))
        out = provider.propose_design_candidates(
            {"query": "warm", "library_project_id": "wb-design"}, limit=5
        )
        self.assertEqual(out, ())

    def test_candidates_are_bounded(self):
        items = [_Item(source_id=f"s{i}", uri=f"viking://{i}", source_revision="r") for i in range(20)]
        provider = OpenVikingDecisionProvider(_StubAdapter(items))
        out = provider.propose_design_candidates(
            {"query": "warm", "library_project_id": "wb-design"}, limit=3
        )
        self.assertLessEqual(len(out), 3)

    def test_intake_is_not_this_providers_job(self):
        provider = OpenVikingDecisionProvider(None)
        with self.assertRaises(DecisionProviderError):
            provider.classify_intake("x", {})

    def test_openviking_candidates_reach_the_engine(self):
        items = [_Item(source_id="s1", score=0.95)]
        provider = OpenVikingDecisionProvider(_StubAdapter(items))
        reg = DecisionProviderRegistry()
        reg.register(provider)
        engine = DecisionEngine(reg, provider_name="openviking")
        d = engine.decide_design(
            {"query": "warm editorial", "library_project_id": "wb-design"}
        )
        self.assertFalse(d.abstained)
        assert d.selected is not None
        self.assertEqual(d.selected.candidate_id, "s1")
        self.assertEqual(d.source, SOURCE_PROVIDER)


class OpenVikingContractRegressionTests(unittest.TestCase):
    """Live-integration regression guards (the four corrected defects).

    These pin the REAL adapter contract, so the defects that produced a silent
    zero-candidate abstention against a live OpenViking can never return:

    * scope is a set of CATEGORIES, never the library project id;
    * provenance maps from the real ``ContextItem.uri`` (not ``source_uri``);
    * a configured-but-unavailable provider is reported as a degraded fallback,
      never as a clean provider result.
    """

    def test_scope_is_categories_not_project_id(self):
        stub = _StubAdapter([_Item()])
        provider = OpenVikingDecisionProvider(stub)
        provider.propose_design_candidates(
            {"query": "warm", "library_project_id": "wb-design"}, limit=3
        )
        # The project id goes to project_id; the scope is a category tuple.
        self.assertEqual(stub.last_project_id, "wb-design")
        self.assertIsInstance(stub.last_scope, tuple)
        self.assertNotIn("wb-design", stub.last_scope)
        for category in stub.last_scope:
            from app.core.openviking_library import CATEGORIES
            self.assertIn(category, CATEGORIES)

    def test_caller_may_narrow_scope_with_design_categories(self):
        stub = _StubAdapter([_Item()])
        provider = OpenVikingDecisionProvider(stub)
        provider.propose_design_candidates(
            {
                "query": "warm",
                "library_project_id": "wb-design",
                "design_categories": ["motion"],
            },
            limit=3,
        )
        self.assertEqual(stub.last_scope, ("motion",))

    def test_real_context_item_maps_to_provenanced_candidate(self):
        # The REAL ContextItem, not a bespoke fake.
        from app.core.openviking_library import ContextItem

        item = ContextItem(
            uri="viking://resources/website-builder/projects/wb-design/design_dna/refero_typography",
            source_id="refero_typography",
            source_revision="1dcbd16676c3",
            trust="reviewed",
            category="design_dna",
            level=1,
            score=0.75,
            title="Typography guide",
            body="type scale, pairing rules",
            summary="Typography reference",
            estimated_tokens=42,
        )
        provider = OpenVikingDecisionProvider(_StubAdapter([item]))
        reg = DecisionProviderRegistry()
        reg.register(provider)
        engine = DecisionEngine(reg, provider_name="openviking")
        d = engine.decide_design(
            {"query": "typography", "library_project_id": "wb-design"}
        )
        self.assertFalse(d.abstained)
        assert d.selected is not None
        # Provenance is the REAL item's, mapped verbatim.
        self.assertEqual(d.selected.source_uri, item.uri)
        self.assertEqual(d.selected.source_revision, item.source_revision)
        self.assertEqual(d.selected.candidate_id, item.source_id)

    def test_configured_but_unavailable_provider_degrades_honestly(self):
        # design_provider names "openviking" but no such provider is registered,
        # so the registry resolves the fallback. That MUST be reported as a
        # degraded fallback — never as a clean provider selection.
        engine = DecisionEngine(
            DecisionProviderRegistry(), provider_name="openviking"
        )
        d = engine.decide_design(
            {"design_dna": {"brand_personality": "warm"}}
        )
        self.assertEqual(d.source, SOURCE_FALLBACK)
        self.assertTrue(d.degraded)
        self.assertEqual(d.provider, DETERMINISTIC_PROVIDER_NAME)

    def test_configured_but_unavailable_provider_degrades_intake(self):
        engine = DecisionEngine(
            DecisionProviderRegistry(), provider_name="openviking"
        )
        d = engine.decide_intake("build a new website", {})
        self.assertEqual(d.source, SOURCE_FALLBACK)
        self.assertTrue(d.degraded)
        self.assertEqual(d.provider, DETERMINISTIC_PROVIDER_NAME)


class ShadowModeIntakeTests(unittest.TestCase):
    """The Decision Engine observes intake but never changes its result."""

    def _processor(self, engine):
        import tempfile
        from pathlib import Path
        from app.core.intake import IntakeProcessor
        from app.core.state import ProjectStateStore
        from app.channels.telegram import NormalizedMessage

        tmp = tempfile.mkdtemp()
        store = ProjectStateStore(Path(tmp) / "state")
        proc = IntakeProcessor(store, decision_engine=engine)
        return proc, NormalizedMessage

    def test_shadow_records_a_decision(self):
        engine = DecisionEngine()
        proc, NormalizedMessage = self._processor(engine)
        msg = NormalizedMessage(event_id="e1", text="build a new website for a bakery")
        proc.process(msg, project_id="p1")
        self.assertIsNotNone(proc.last_shadow_decision)
        decision = proc.last_shadow_decision
        assert decision is not None
        self.assertEqual(decision.source, SOURCE_PROVIDER)

    def test_shadow_does_not_change_the_result(self):
        engine = DecisionEngine()
        proc_a, NormalizedMessage = self._processor(engine)
        proc_b, _ = self._processor(None)
        msg = NormalizedMessage(event_id="e1", text="build a new website for a bakery")
        r_shadow = proc_a.process(msg, project_id="p1")
        r_plain = proc_b.process(msg, project_id="p1")
        self.assertEqual(r_shadow.brief, r_plain.brief)
        self.assertEqual(r_shadow.readiness, r_plain.readiness)
        self.assertEqual(r_shadow.scope, r_plain.scope)

    def test_shadow_failure_never_breaks_intake(self):
        class _Broken:
            def decide_intake(self, *a, **k):
                raise RuntimeError("shadow engine exploded")

        proc, NormalizedMessage = self._processor(_Broken())
        msg = NormalizedMessage(event_id="e1", text="build a new website")
        result = proc.process(msg, project_id="p1")
        self.assertIsNotNone(result)
        self.assertIsNone(proc.last_shadow_decision)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
