"""N6 — OpenViking design-candidate INTEGRATION regression tests.

These are the regression guards for the four confirmed N6 live-integration
defects that made the Decision → OpenViking → DesignGuidance leg silently
produce ZERO candidates against a live OpenViking:

A. The retrieval scope was passed the library PROJECT id instead of a set of
   CATEGORY names, so ``normalize_scope`` raised and the provider swallowed it.
B. The provider read ``item.source_uri`` from a retrieved item, but the real
   ``ContextItem`` field is ``uri`` — so every item mapped to ``None``.
C. ``library_project_id`` was never supplied to the pre-build design decision
   context, so the provider returned ``()`` before it ever called the adapter.
D. With ``design_provider`` set, an unavailable provider silently resolved to
   the deterministic fallback and was reported as a clean success.

UNLIKE the older unit fakes, these tests drive the **real** D4a adapter
(``OpenVikingRetrievalAdapter``) over the **real** deterministic backend
(``FakeOpenVikingBackend`` + ``ingest_sources``), so the actual
``retrieve_context`` contract, the real ``ContextItem`` schema, and the real
provenance path are all exercised. No live server, model, browser, or network
is required.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.core import openviking_library as lib  # noqa: E402
from app.core import openviking_retrieval as ovr  # noqa: E402
from app.core.decision_engine import (  # noqa: E402
    SOURCE_FALLBACK,
    SOURCE_PROVIDER,
    DecisionEngine,
)
from app.core.decision_provider import (  # noqa: E402
    DETERMINISTIC_PROVIDER_NAME,
    DecisionProviderRegistry,
)
from app.core.openviking_decision import (  # noqa: E402
    DESIGN_RETRIEVAL_CATEGORIES,
    OpenVikingDecisionProvider,
)

PROJECT = "wb-design"


def _indexed_adapter(content: bytes = b"# Typography\nEditorial type scale, 1.2 ratio."):
    """A REAL adapter over a REAL (deterministic) backend with ingested content."""
    backend = lib.FakeOpenVikingBackend()
    src = lib.SourceSpec(
        source_id="refero_typography",
        project_id=PROJECT,
        category="design_dna",
        trust="reviewed",
        locator="skills/refero/references/typography.md",
    )
    lib.ingest_sources(
        backend,
        [src],
        reader=lambda loc: content,
        project_id=PROJECT,
        clock="2026-01-01T00:00:00Z",
    )
    return ovr.build_adapter(ovr.OpenVikingConfig(enabled=True), backend)


def _engine_with(provider):
    reg = DecisionProviderRegistry()
    reg.register(provider)
    return DecisionEngine(reg, provider_name="openviking")


class RealAdapterContractTests(unittest.TestCase):
    """The provider drives the REAL adapter contract end-to-end."""

    def test_real_retrieval_yields_a_provenanced_candidate(self):
        adapter = _indexed_adapter()
        provider = OpenVikingDecisionProvider(adapter)
        candidates = provider.propose_design_candidates(
            {"query": "typography", "library_project_id": PROJECT}, limit=5
        )
        self.assertTrue(candidates, "real retrieval produced no candidate")
        for c in candidates:
            # Real provenance, mapped from ContextItem.uri/source_id/source_revision.
            self.assertTrue(c.source_uri)
            self.assertTrue(c.source_revision)
            self.assertTrue(c.candidate_id)
            self.assertTrue(c.source_uri.startswith("viking://"))

    def test_real_candidate_reaches_decide_design(self):
        adapter = _indexed_adapter()
        engine = _engine_with(OpenVikingDecisionProvider(adapter))
        decision = engine.decide_design(
            {"query": "typography", "library_project_id": PROJECT}
        )
        self.assertFalse(decision.abstained)
        self.assertEqual(decision.source, SOURCE_PROVIDER)
        assert decision.selected is not None
        self.assertTrue(decision.selected.has_provenance)

    def test_scope_is_not_the_project_id(self):
        # Defect A: the scope handed to the adapter must be category names.
        self.assertNotIn(PROJECT, DESIGN_RETRIEVAL_CATEGORIES)
        for category in DESIGN_RETRIEVAL_CATEGORIES:
            self.assertIn(category, lib.CATEGORIES)

    def test_wrong_scope_shape_would_fail_but_ours_does_not(self):
        # Directly demonstrate the defect: passing the project id as scope is
        # refused by normalize_scope, which is exactly what the old code did.
        with self.assertRaises(ValueError):
            ovr.normalize_scope((PROJECT,))
        # Our real scope is accepted.
        self.assertEqual(
            ovr.normalize_scope(DESIGN_RETRIEVAL_CATEGORIES),
            DESIGN_RETRIEVAL_CATEGORIES,
        )


class RealAdapterOutageTests(unittest.TestCase):
    """A real backend outage degrades honestly (no fabricated candidate)."""

    def test_backend_outage_abstains_without_raising(self):
        backend = lib.FakeOpenVikingBackend()
        backend.fail_with = RuntimeError("backend down")
        adapter = ovr.build_adapter(ovr.OpenVikingConfig(enabled=True), backend)
        engine = _engine_with(OpenVikingDecisionProvider(adapter))
        decision = engine.decide_design(
            {"query": "typography", "library_project_id": PROJECT}
        )
        self.assertTrue(decision.abstained)
        self.assertIsNone(decision.selected)

    def test_disabled_adapter_abstains(self):
        adapter = ovr.build_adapter(ovr.OpenVikingConfig(enabled=False))
        engine = _engine_with(OpenVikingDecisionProvider(adapter))
        decision = engine.decide_design(
            {"query": "typography", "library_project_id": PROJECT}
        )
        self.assertTrue(decision.abstained)


class ConfiguredProviderAvailabilityTests(unittest.TestCase):
    """Defect D: a configured-but-unavailable provider degrades honestly."""

    def test_unavailable_openviking_is_reported_as_fallback(self):
        engine = DecisionEngine(
            DecisionProviderRegistry(), provider_name="openviking"
        )
        decision = engine.decide_design(
            {"design_dna": {"brand_personality": "warm"}}
        )
        self.assertEqual(decision.source, SOURCE_FALLBACK)
        self.assertTrue(decision.degraded)
        self.assertEqual(decision.provider, DETERMINISTIC_PROVIDER_NAME)

    def test_available_openviking_is_reported_as_provider(self):
        adapter = _indexed_adapter()
        engine = _engine_with(OpenVikingDecisionProvider(adapter))
        decision = engine.decide_design(
            {"query": "typography", "library_project_id": PROJECT}
        )
        self.assertEqual(decision.source, SOURCE_PROVIDER)
        self.assertFalse(decision.degraded)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
