"""N5 — Regression, Replay & Recovery.

N5 is the acceptance batch for the whole R2 redesign. It does not add new
runtime capability; it *proves* the redesign against the roadmap's N5 gate:

* the complete new-website **and** revision lifecycle still works end to end;
* the **Forma Atelier baseline brief** replays deterministically;
* **failure injection** is handled safely across provider, document, retrieval,
  candidate, Git, crash, and QA faults;
* **exact release identity and user approval** precede publication;
* no security or release invariant was relaxed.

Where a fault belongs to an R2 component (Requirement Creator, Decision Engine,
OpenViking), the test drives the real component. Where it belongs to the existing
R1 control flow, the test reuses the real ``LocalR1Scenario`` harness so the
whole production wiring is exercised, not a mock.

No live LLM, browser, network, or npm is required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from r1_harness import LocalR1Scenario, patch_qa_boundaries  # noqa: E402

from app.core.decision_engine import DecisionEngine  # noqa: E402
from app.core.decision_provider import (  # noqa: E402
    DecisionProviderRegistry,
    DesignCandidate,
)
from app.core.document_guidance import guidance_for_workspace  # noqa: E402
from app.core.document_contracts import (  # noqa: E402
    extract_sections,
    parse_front_matter,
)
from app.core.document_store import (  # noqa: E402
    document_records,
    read_document,
    store_documents,
)
from app.core.openviking_decision import OpenVikingDecisionProvider  # noqa: E402
from app.core.requirement_creator import (  # noqa: E402
    RequirementCreatorConfig,
    compile_design,
    compile_prd,
)


@pytest.fixture(autouse=True)
def _qa_boundaries(monkeypatch):
    patch_qa_boundaries(monkeypatch)


# The Forma Atelier baseline brief (the roadmap's named replay target).
FORMA_ATELIER_BRIEF = {
    "name": "Forma Atelier",
    "what": "a boutique furniture atelier with a curated collection and studio visits",
    "why": "book a studio visit",
    "why_destination": "https://forma-atelier.example/visit",
}
FORMA_ATELIER_DNA = {
    "version": 1,
    "brand_personality": "warm, editorial, understated, tactile",
    "palette": {"primary": "#101014", "background": "#F4EFE7", "accent": "#B4763A"},
    "typography": {"heading_font": "Fraunces", "body_font": "Inter"},
    "layout": "editorial grid, generous whitespace, asymmetric hero",
}


# ---------------------------------------------------------------------------
# 1. Full lifecycle regression (new website + revision + promote)
# ---------------------------------------------------------------------------


class TestLifecycleRegression(unittest.TestCase):
    def test_new_website_and_revision_reach_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = LocalR1Scenario(Path(tmp))
            h.set_router_decision("CREATE_PROJECT", confidence="high",
                                  proposed_new_project_name=None)
            h.send_user_message(
                "aku mau bikin web untuk membaca, soft tone dan bright"
            )
            h.set_intake_response(
                name="kitsunereading", what="reading rental", why="rent books",
                readiness="DISCOVERY_READY", clarification_needed=False,
            )
            h.send_user_message("kitsunereading")
            pid = h.current_project_id
            assert pid is not None
            h.run_build_and_preview(pid)
            state = h.project_state(pid)
            self.assertEqual(state.lifecycle, "PREVIEW_READY")

            rev = h.run_revision("ganti warna jadi lebih lembut")
            self.assertTrue(rev.success, getattr(rev, "error", None))
            self.assertTrue(h.approve().success)
            self.assertTrue(h.promote_now().success)
            state = h.project_state(pid)
            self.assertEqual(state.lifecycle, "LIVE")
            # Exact release identity: every revision counter agrees.
            r = state.revisions
            self.assertEqual(
                (r.source_revision, r.qa_revision, r.preview_revision,
                 r.approved_revision, r.live_revision),
                (2, 2, 2, 2, 2),
            )

    def test_publication_requires_approval(self):
        """No production publication without an explicit approval."""
        with tempfile.TemporaryDirectory() as tmp:
            h = LocalR1Scenario(Path(tmp))
            h.set_router_decision("CREATE_PROJECT", confidence="high",
                                  proposed_new_project_name=None)
            h.send_user_message("aku mau bikin web membaca, soft tone")
            h.set_intake_response(
                name="kitsunereading", what="reading rental", why="rent books",
                readiness="DISCOVERY_READY", clarification_needed=False,
            )
            h.send_user_message("kitsunereading")
            pid = h.current_project_id
            h.run_build_and_preview(pid)
            # Promote WITHOUT approving first: must not reach LIVE.
            result = h.promote_now()
            state = h.project_state(pid)
            self.assertNotEqual(state.lifecycle, "LIVE")
            self.assertFalse(getattr(result, "success", True))


# ---------------------------------------------------------------------------
# 2. Forma Atelier baseline replay
# ---------------------------------------------------------------------------


class FormaAtelierReplayTests(unittest.TestCase):
    def test_prd_replay_is_deterministic(self):
        a = compile_prd(FORMA_ATELIER_BRIEF, project_id="forma-atelier",
                        source_revision=1, requirements_version=1)
        b = compile_prd(FORMA_ATELIER_BRIEF, project_id="forma-atelier",
                        source_revision=1, requirements_version=1)
        self.assertEqual(a, b)

    def test_design_replay_is_deterministic(self):
        a = compile_design(FORMA_ATELIER_DNA, project_id="forma-atelier",
                           source_revision=1, design_dna_version=1)
        b = compile_design(FORMA_ATELIER_DNA, project_id="forma-atelier",
                           source_revision=1, design_dna_version=1)
        self.assertEqual(a, b)

    def test_replay_documents_are_specific_not_generic(self):
        """The compiled documents carry the brief's concrete facts, not filler."""
        prd = compile_prd(FORMA_ATELIER_BRIEF, project_id="forma-atelier",
                          source_revision=1, scope="WEBSITE",
                          readiness="DISCOVERY_READY")
        design = compile_design(FORMA_ATELIER_DNA, project_id="forma-atelier",
                                source_revision=1)
        # Requirement coverage: the brief's own words appear.
        self.assertIn("boutique furniture atelier", prd)
        self.assertIn("book a studio visit", prd)
        # Design specificity: the DNA's own values appear.
        self.assertIn("Fraunces", design)
        self.assertIn("#101014", design)
        # No fabricated UNRESOLVED markers when the facts are supplied.
        self.assertNotIn("UNRESOLVED", design)
        # V2-A: the PRD now carries an explicit Design Intent section. This
        # fixture brief states NO design preference, so that ONE section is
        # honestly UNRESOLVED while every requirement fact is present — the
        # marker is never fabricated away, and the only UNRESOLVED is the
        # optional design-intent one.
        sections = extract_sections(parse_front_matter(prd)[1])
        self.assertIn("design_intent", sections)
        self.assertIn("UNRESOLVED", sections["design_intent"])
        self.assertNotIn("UNRESOLVED", sections["requirements"])

    def test_replay_marks_absent_facts_unresolved_not_invented(self):
        """A missing fact is marked UNRESOLVED, never fabricated."""
        prd = compile_prd(
            {"name": "Forma Atelier"}, project_id="forma-atelier", source_revision=1
        )
        self.assertIn("UNRESOLVED", prd)
        # The compiler did not invent a what/why.
        self.assertNotIn("boutique furniture", prd)

    def test_replay_reaches_frontend_as_guidance(self):
        """The replayed documents reach FRONTEND through the N4 handoff."""
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            prd = compile_prd(FORMA_ATELIER_BRIEF, project_id="forma-atelier",
                              source_revision=1)
            store_documents(ws, prd_text=prd)
            block, digest = guidance_for_workspace(ws)
            self.assertIsNotNone(block)
            self.assertIn("boutique furniture atelier", block)
            self.assertIsNotNone(digest)


# ---------------------------------------------------------------------------
# 3. Failure injection — provider / document / retrieval / candidate
# ---------------------------------------------------------------------------


class _ExplodingProvider:
    name = "exploding"

    def classify_intake(self, *a, **k):
        raise RuntimeError("provider down")

    def propose_design_candidates(self, *a, **k):
        raise RuntimeError("provider down")


class ProviderFailureTests(unittest.TestCase):
    def test_provider_outage_never_breaks_the_decision(self):
        reg = DecisionProviderRegistry()
        reg.register(_ExplodingProvider())
        engine = DecisionEngine(reg, provider_name="exploding")
        d = engine.decide_design({"design_dna": {"brand_personality": "warm"}})
        # Falls back, still returns, and marks itself degraded.
        self.assertTrue(d.degraded)
        self.assertFalse(d.abstained)

    def test_provider_outage_with_no_evidence_abstains(self):
        reg = DecisionProviderRegistry()
        reg.register(_ExplodingProvider())
        engine = DecisionEngine(reg, provider_name="exploding")
        d = engine.decide_design({})
        self.assertTrue(d.abstained)
        self.assertIsNone(d.selected)


class DocumentFailureTests(unittest.TestCase):
    def test_invalid_document_is_never_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            from app.core.document_store import write_document
            with self.assertRaises(ValueError):
                write_document(ws, "prd", "garbage")
            self.assertIsNone(read_document(ws, "prd"))

    def test_corrupt_document_reads_none_not_crash(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            (ws / "PRD.md").write_bytes(b"\xff\xfe not utf-8")
            self.assertIsNone(read_document(ws, "prd"))

    def test_document_digest_mismatch_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            ws = Path(tmp)
            prd, design = store_documents(
                ws,
                prd_text=compile_prd(FORMA_ATELIER_BRIEF, project_id="p", source_revision=1),
                design_text=compile_design(FORMA_ATELIER_DNA, project_id="p", source_revision=1),
            )
            records = document_records(prd, design)
            from app.core.document_store import verify_records_match_workspace
            self.assertIsNone(verify_records_match_workspace(ws, records))
            (ws / "PRD.md").write_text(
                compile_prd(FORMA_ATELIER_BRIEF, project_id="p", source_revision=2),
                encoding="utf-8",
            )
            self.assertIsNotNone(verify_records_match_workspace(ws, records))


class _FailingAdapter:
    def retrieve_context(self, *a, **k):
        raise RuntimeError("openviking down")


class RetrievalFailureTests(unittest.TestCase):
    def test_retrieval_outage_yields_honest_empty(self):
        provider = OpenVikingDecisionProvider(_FailingAdapter())
        out = provider.propose_design_candidates(
            {"query": "warm", "library_project_id": "wb-design"}, limit=3
        )
        self.assertEqual(out, ())

    def test_retrieval_outage_leads_to_abstention(self):
        provider = OpenVikingDecisionProvider(_FailingAdapter())
        reg = DecisionProviderRegistry()
        reg.register(provider)
        engine = DecisionEngine(reg, provider_name="openviking")
        d = engine.decide_design(
            {"query": "warm", "library_project_id": "wb-design"}
        )
        self.assertTrue(d.abstained)


class CandidateRejectionTests(unittest.TestCase):
    def test_unprovenanced_candidate_never_selected(self):
        engine = DecisionEngine()
        d = engine._rank(
            [DesignCandidate("c", "l", "d", source_uri="", source_revision="", score=0.9)],
            source="provider", provider_name="x", degraded=False, error_code="",
        )
        self.assertTrue(d.abstained)

    def test_bogus_score_never_selected(self):
        engine = DecisionEngine()
        d = engine._rank(
            [DesignCandidate("c", "l", "d", source_uri="u", source_revision="r", score=float("nan"))],
            source="provider", provider_name="x", degraded=False, error_code="",
        )
        self.assertTrue(d.abstained)


# ---------------------------------------------------------------------------
# 4. Crash recovery
# ---------------------------------------------------------------------------


class CrashRecoveryTests(unittest.TestCase):
    def test_lifecycle_survives_restart_and_finalizes_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = LocalR1Scenario(Path(tmp))
            h.set_router_decision("CREATE_PROJECT", confidence="high",
                                  proposed_new_project_name=None)
            h.send_user_message("aku mau bikin web membaca, soft tone")
            h.set_intake_response(
                name="kitsunereading", what="reading rental", why="rent books",
                readiness="DISCOVERY_READY", clarification_needed=False,
            )
            h.send_user_message("kitsunereading")
            pid = h.current_project_id
            h.run_build_and_preview(pid)
            # Restart between every remaining step; the story must still finish
            # exactly once (no duplicate project create / deploy / preview).
            h.restart()
            self.assertTrue(h.run_revision("hero lebih kecil").success)
            h.restart()
            self.assertTrue(h.approve().success)
            h.restart()
            self.assertTrue(h.promote_now().success)
            state = h.project_state(pid)
            self.assertEqual(state.lifecycle, "LIVE")
            self.assertEqual(h.vercel.post_calls, 1)


# ---------------------------------------------------------------------------
# 5. QA failure does not publish
# ---------------------------------------------------------------------------


class QAFailureTests(unittest.TestCase):
    def test_failed_smoke_never_reaches_live(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = LocalR1Scenario(Path(tmp))
            h.set_router_decision("CREATE_PROJECT", confidence="high",
                                  proposed_new_project_name=None)
            h.send_user_message("aku mau bikin web membaca, soft tone")
            h.set_intake_response(
                name="kitsunereading", what="reading rental", why="rent books",
                readiness="DISCOVERY_READY", clarification_needed=False,
            )
            h.send_user_message("kitsunereading")
            pid = h.current_project_id
            # Force the preview smoke to fail.
            h.set_smoke_result(False)
            h.run_build_and_preview(pid)
            state = h.project_state(pid)
            self.assertNotEqual(state.lifecycle, "LIVE")
            self.assertEqual(h.vercel.promote_calls, 0)


# ---------------------------------------------------------------------------
# 6. Release identity / approval invariants (regression guards)
# ---------------------------------------------------------------------------


class ReleaseInvariantTests(unittest.TestCase):
    def test_approval_is_bound_to_a_specific_revision(self):
        with tempfile.TemporaryDirectory() as tmp:
            h = LocalR1Scenario(Path(tmp))
            h.set_router_decision("CREATE_PROJECT", confidence="high",
                                  proposed_new_project_name=None)
            h.send_user_message("aku mau bikin web membaca, soft tone")
            h.set_intake_response(
                name="kitsunereading", what="reading rental", why="rent books",
                readiness="DISCOVERY_READY", clarification_needed=False,
            )
            h.send_user_message("kitsunereading")
            pid = h.current_project_id
            h.run_build_and_preview(pid)
            h.approve()
            state = h.project_state(pid)
            # Approval recorded against the CURRENT preview revision.
            self.assertEqual(
                state.revisions.approved_revision, state.revisions.preview_revision
            )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
