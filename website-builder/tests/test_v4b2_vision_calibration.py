"""V4-B2 regression tests — VISION verdict calibration.

Locks the scoped correction:

* A — error semantics: a MISSING VISION result (``vision is None``) surfaces
  ``VISION_REQUIRED``; a COMPLETED VISION that returned blocking findings
  surfaces the distinct ``VISION_BLOCKING_FINDINGS``. The two user messages
  are accurate, actionable, and must NOT suggest a generic retry for a genuine
  visual-quality finding.
* B — consistency: the fidelity diagnostic records the VISION finding's
  location/severity/evidence AND classifies it as
  ``VISION_BLOCKING_REPORTED`` / ``FIDELITY_CONFIRMED_DEFECT`` /
  ``INCONSISTENT_OR_UNVERIFIED_FINDING``. QA blocking stays authoritative;
  nothing bypasses a blocking finding.
* D — identical evidence with contradictory verdicts is recorded as a
  consistency issue (never silently accepted); genuine overlapping navigation
  stays blocking; no third FRONTEND call; p1 state and the p2 budget ledger
  are untouched by these code paths.

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.design_fidelity import (  # noqa: E402
    CONFIDENCE_HIGH,
    DIM_VISUAL_EXECUTION,
    SEVERITY_HARD,
    FidelityFinding,
    FidelityReport,
)
from app.core.design_fidelity_runtime import (  # noqa: E402
    FIDELITY_CONFIRMED_DEFECT,
    INCONSISTENT_OR_UNVERIFIED_FINDING,
    VISION_BLOCKING_REPORTED,
    VISION_CONSISTENCY_CLASSES,
    build_vision_consistency,
)
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.deploy.snapshot import record_checks, source_fingerprint  # noqa: E402
from app.qa.findings import (  # noqa: E402
    DeterministicFindings,
    QAAttempt,
    VisionFindings,
)
from app.qa.orchestrator import QAOrchestrator  # noqa: E402
from app.qa.render import RenderHandle  # noqa: E402
from app.qa.screenshot import BrowserMetrics, ScreenshotSet  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402

BRIEF = {
    "name": "Atelier Forma",
    "what": "Landing page untuk studio arsitektur",
    "why": "hero, showcase 3 proyek arsitektur, tentang, layanan, kontak",
    "design_intent": (
        "editorial minimalis, premium, dan modern. Tipografi elegan, warna "
        "off-white, charcoal, serta aksen terracotta."
    ),
}

# The EXACT blocking finding VISION produced on the real p2 attempt-1.
REAL_BLOCKING = (
    "Mobile navigation text is squished/overlapping ('Karya Tentang Layanan "
    "Kontak' lacks spacing between items), making the menu unusable on mobile "
    "viewport (390px width)."
)

CLEAN_APP = """
export default function App() {
  return (<main><h1>Atelier Forma</h1>
    <p>Contoh karya (placeholder) — materi menyusul dari studio.</p>
    <a href="#kontak">Hubungi studio</a></main>);
}
"""


def _png_bytes(width: int, height: int) -> bytes:
    import struct
    import zlib

    def _chunk(ctype: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data)) + ctype + data
            + struct.pack(">I", zlib.crc32(ctype + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def _metrics(width: int, height: int) -> BrowserMetrics:
    return BrowserMetrics(
        inner_width=width, inner_height=height,
        document_client_width=width, document_scroll_width=width,
        body_scroll_width=width,
    )


def _hard_visual_finding(text: str) -> FidelityFinding:
    return FidelityFinding(
        rule_id="repeated-card-content",
        dimension=DIM_VISUAL_EXECUTION,
        severity=SEVERITY_HARD,
        confidence=CONFIDENCE_HIGH,
        location="showcase",
        finding=text,
        evidence=text,
        suggested_action="vary the composition",
    )


# ---------------------------------------------------------------------------
# A — error semantics (pure)
# ---------------------------------------------------------------------------

class ErrorSemanticsTests(unittest.TestCase):
    def test_codes_are_distinct_and_both_have_copy(self):
        from app.runtime import ERROR_MESSAGES, render_error_message

        self.assertIn("VISION_REQUIRED", ERROR_MESSAGES)
        self.assertIn("VISION_BLOCKING_FINDINGS", ERROR_MESSAGES)
        self.assertNotEqual(
            ERROR_MESSAGES["VISION_REQUIRED"],
            ERROR_MESSAGES["VISION_BLOCKING_FINDINGS"],
        )
        for code in ("VISION_REQUIRED", "VISION_BLOCKING_FINDINGS"):
            self.assertEqual(render_error_message(code), ERROR_MESSAGES[code])

    def test_missing_vision_copy_is_about_completion(self):
        from app.runtime import ERROR_MESSAGES

        text = ERROR_MESSAGES["VISION_REQUIRED"].lower()
        self.assertIn("could not be completed", text)

    def test_blocking_copy_does_not_suggest_a_generic_retry(self):
        from app.runtime import ERROR_MESSAGES

        text = ERROR_MESSAGES["VISION_BLOCKING_FINDINGS"].lower()
        # A retry of the same artifact cannot clear a real layout defect.
        self.assertNotIn("try again", text)
        self.assertNotIn("temporary problem", text)
        # It must be honest that nothing was published.
        self.assertIn("no preview", text)
        # And it must invite an actual change rather than a blind resend.
        self.assertIn("change", text)


# ---------------------------------------------------------------------------
# B/D — the pure consistency classifier
# ---------------------------------------------------------------------------

class ConsistencyClassifierTests(unittest.TestCase):
    def _entry(self, attempt, pass_, blocking, shots):
        return {
            "attempt": attempt, "pass": pass_, "blocking": list(blocking),
            "observations": [], "screenshots_sha": shots,
        }

    def test_unconfirmed_finding_is_reported_not_confirmed(self):
        entries = [self._entry(0, True, [], {"desktop": "d0", "mobile": "m0"}),
                   self._entry(1, False, [REAL_BLOCKING], {"desktop": "d1", "mobile": "m1"})]
        report = FidelityReport(dimensions={DIM_VISUAL_EXECUTION: "PASS"})
        res = build_vision_consistency(entries, report)
        self.assertEqual(res["classes"][0]["classification"], VISION_BLOCKING_REPORTED)
        self.assertFalse(res["contradiction_detected"])
        # Evidence + location are preserved, and location is grounded in text.
        self.assertEqual(res["classes"][0]["location"], "navigation")
        self.assertEqual(res["classes"][0]["location_source"], "vision_text")
        self.assertTrue(res["classes"][0]["evidence"])
        self.assertTrue(res["notes"])

    def test_contradictory_verdicts_on_identical_evidence_is_recorded(self):
        same = {"desktop": "same-d", "mobile": "same-m"}
        entries = [self._entry(0, True, [], same),
                   self._entry(1, False, [REAL_BLOCKING], same)]
        report = FidelityReport(dimensions={DIM_VISUAL_EXECUTION: "PASS"})
        res = build_vision_consistency(entries, report)
        self.assertTrue(res["contradiction_detected"])
        self.assertEqual(
            res["classes"][0]["classification"],
            INCONSISTENT_OR_UNVERIFIED_FINDING,
        )
        self.assertEqual(
            res["contradictions"][0]["reason"],
            "identical_screenshots_contradictory_verdicts",
        )
        # Never silently accepted: it is flagged, not dropped.
        self.assertTrue(any("consistency issue" in n for n in res["notes"]))

    def test_fidelity_corroboration_confirms_the_defect(self):
        entries = [self._entry(0, True, [], {"desktop": "d0", "mobile": "m0"}),
                   self._entry(1, False, [REAL_BLOCKING], {"desktop": "d1", "mobile": "m1"})]
        # The gate's OWN detector found a matching hard visual finding.
        report = FidelityReport(
            dimensions={DIM_VISUAL_EXECUTION: "FAIL"},
            findings=(_hard_visual_finding(
                "Mobile navigation text overlapping lacks spacing menu unusable"
            ),),
        )
        res = build_vision_consistency(entries, report)
        self.assertEqual(res["classes"][0]["classification"], FIDELITY_CONFIRMED_DEFECT)
        self.assertEqual(
            res["classes"][0]["corroborating_fidelity_rule_ids"],
            ["repeated-card-content"],
        )

    def test_bare_fail_never_becomes_a_confirmed_defect(self):
        """A bare pass_=False with NO corroboration is never a confirmed defect."""
        entries = [self._entry(0, False, ["something vague"], {"desktop": "d", "mobile": "m"})]
        report = FidelityReport(dimensions={DIM_VISUAL_EXECUTION: "PASS"})
        res = build_vision_consistency(entries, report)
        self.assertNotEqual(
            res["classes"][0]["classification"], FIDELITY_CONFIRMED_DEFECT
        )

    def test_dict_form_report_is_accepted(self):
        entries = [self._entry(0, True, [], {"desktop": "a", "mobile": "b"}),
                   self._entry(1, False, [REAL_BLOCKING], {"desktop": "c", "mobile": "d"})]
        report = {
            "dimensions": {DIM_VISUAL_EXECUTION: "FAIL"},
            "findings": [{
                "rule_id": "repeated-card-content",
                "dimension": DIM_VISUAL_EXECUTION,
                "severity": SEVERITY_HARD,
                "confidence": CONFIDENCE_HIGH,
                "location": "navigation",
                "finding": "mobile navigation overlapping lacks spacing unusable",
                "evidence": "mobile navigation overlapping lacks spacing unusable",
                "suggested_action": "space the nav items",
            }],
        }
        res = build_vision_consistency(entries, report)
        self.assertEqual(res["classes"][0]["classification"], FIDELITY_CONFIRMED_DEFECT)

    def test_class_vocabulary_is_closed(self):
        self.assertEqual(
            set(VISION_CONSISTENCY_CLASSES),
            {VISION_BLOCKING_REPORTED, FIDELITY_CONFIRMED_DEFECT,
             INCONSISTENT_OR_UNVERIFIED_FINDING},
        )

    def test_no_blocking_findings_yields_empty(self):
        entries = [self._entry(0, True, [], {"desktop": "d", "mobile": "m"})]
        res = build_vision_consistency(entries, FidelityReport(dimensions={}))
        self.assertEqual(res["classes"], [])
        self.assertFalse(res["contradiction_detected"])


# ---------------------------------------------------------------------------
# A/D — the REAL orchestrator acceptance path
# ---------------------------------------------------------------------------

class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory()
        root = Path(self.tmp_obj.name)
        self.workspace_root = root / "workspaces"
        self.store = ProjectStateStore(root / "state")
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.workspace = self.workspace_root / "proj"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "App.tsx").write_text(CLEAN_APP, encoding="utf-8")
        (self.workspace / "src" / "index.css").write_text(":root{}", encoding="utf-8")
        (self.workspace / "design-dna.json").write_text('{"version": 1}', encoding="utf-8")
        (self.workspace / "dist").mkdir(parents=True)
        (self.workspace / "dist" / "index.html").write_text("<html></html>", encoding="utf-8")

        self.mock_renderer = MagicMock()
        self.mock_capture = MagicMock()
        self.mock_adapter = MagicMock()
        self.mock_adapter.vision_inspect.return_value = {
            "pass": True, "blocking": [], "observations": [], "summary": "ok",
        }
        self.mock_adapter.frontend_build.return_value = {
            "success": True, "design_dna": {"version": 1},
        }

    def tearDown(self):
        self.tmp_obj.cleanup()

    def _queue(self, pid="proj"):
        self.store.transition_lifecycle(pid, ProjectLifecycle.READY)
        with self.store.acquire_writer(pid) as state:
            state.brief = dict(BRIEF)
            self.store.save(state)
        self.store.transition_lifecycle(pid, ProjectLifecycle.QUEUED)
        self.store.transition_lifecycle(pid, ProjectLifecycle.RUNNING)

    def _passing_render(self):
        self.mock_renderer.start.return_value = RenderHandle(
            project_id="proj", port=5100, process=MagicMock(),
            url="http://127.0.0.1:5100/",
        )

    def _screenshots(self, attempt, *, identical=False):
        d = self.workspace / "qa" / f"attempt-{attempt}"
        d.mkdir(parents=True, exist_ok=True)
        desk = d / "desktop.png"
        mob = d / "mobile.png"
        if identical:
            desk.write_bytes(_png_bytes(1440, 900))
            mob.write_bytes(_png_bytes(390, 844))
        else:
            desk.write_bytes(_png_bytes(1440, 900 + attempt))
            mob.write_bytes(_png_bytes(390, 844 + attempt))
        return ScreenshotSet(
            desktop=desk, mobile=mob,
            desktop_metrics=_metrics(1440, 900),
            mobile_metrics=_metrics(390, 844),
        )

    def _bind_checked(self):
        before = source_fingerprint(self.workspace)
        record_checks(self.store, "proj", self.workspace, before)

    def _orchestrator(self, *, enabled=False, adapter=None, identical_shots=False):
        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._screenshots(
                attempt, identical=identical_shots
            )
        )
        return QAOrchestrator(
            self.runner, self.store, hermes_adapter=(adapter or self.mock_adapter),
            renderer=self.mock_renderer, screenshot_capture=self.mock_capture,
            design_fidelity_enabled=enabled,
        )

    def _passing_attempt(self, num):
        return QAAttempt(
            attempt=num,
            deterministic=DeterministicFindings(
                render_ok=True, desktop_screenshot_ok=True, mobile_screenshot_ok=True,
                design_dna_valid=True, source_present=True, build_ok=True,
                typecheck_ok=True),
            vision=VisionFindings(pass_=True, blocking_findings=[]),
            desktop_screenshot=str(self._screenshots(num).desktop),
            mobile_screenshot=str(self._screenshots(num).mobile),
        )

    def _blocked_attempt(self, num, *, vision=None, blocking=(REAL_BLOCKING,)):
        return QAAttempt(
            attempt=num,
            deterministic=DeterministicFindings(
                render_ok=True, desktop_screenshot_ok=True, mobile_screenshot_ok=True,
                design_dna_valid=True, source_present=True, build_ok=True,
                typecheck_ok=True),
            vision=(vision if vision is not None
                    else VisionFindings(pass_=False, blocking_findings=list(blocking))),
            desktop_screenshot=str(self._screenshots(num).desktop),
            mobile_screenshot=str(self._screenshots(num).mobile),
        )

    def _drive_to_acceptance_with_final(self, orch, blocked, *, prior_pass=True):
        """Reproduce p2's EXACT path: a passing attempt, then the fidelity stage
        appends the final (blocked) attempt, then acceptance evaluates it.

        The stage is stubbed (no real gate/model); everything else — the
        acceptance precondition, the error-code split, the consistency record —
        runs for real.
        """
        first = self._passing_attempt(0) if prior_pass else blocked
        seq = [first]
        orch._run_one_attempt = lambda *a, **k: seq.pop(0) if seq else blocked

        def _stage(**kwargs):
            attempts = kwargs["attempts"]
            attempts.append(blocked)
            return {
                "attempts": attempts,
                "next_attempt_num": kwargs["next_attempt_num"] + 1,
                "extra_repairs": 0,
                "failed": False,
                "error": None,
                "record": {"outcome": "FIDELITY_PASS", "source_revision": 1},
            }

        orch._run_design_fidelity_stage = _stage
        return orch.run("proj", self.workspace, BRIEF, None)


class AcceptanceErrorCodeTests(_Fixture):
    def test_missing_vision_surfaces_vision_required(self):
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=False)
        clean = QAAttempt(
            attempt=0,
            deterministic=DeterministicFindings(
                render_ok=True, desktop_screenshot_ok=True, mobile_screenshot_ok=True,
                design_dna_valid=True, source_present=True, build_ok=True,
                typecheck_ok=True),
            vision=None,
        )
        orch._run_one_attempt = lambda *a, **k: clean
        result = orch.run("proj", self.workspace, BRIEF, None)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "VISION_REQUIRED")
        self.assertEqual(
            self.store.load("proj").failure["error"], "VISION_REQUIRED"
        )
        # No fabricated consistency record for a missing inspection.
        self.assertNotIn("vision_consistency", self.store.load("proj").failure)

    def test_completed_blocking_vision_surfaces_blocking_findings(self):
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True)
        result = self._drive_to_acceptance_with_final(orch, self._blocked_attempt(1))
        self.assertFalse(result.success)
        self.assertEqual(result.error, "VISION_BLOCKING_FINDINGS")
        state = self.store.load("proj")
        self.assertEqual(state.failure["error"], "VISION_BLOCKING_FINDINGS")
        # The consistency record is present and honest.
        cons = state.failure["vision_consistency"]
        self.assertEqual(cons["classes"][0]["classification"], VISION_BLOCKING_REPORTED)
        # Safety preserved: NO tested snapshot, no preview artifacts.
        self.assertNotIn("tested_snapshot", state.deployment)
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_genuine_overlapping_navigation_stays_blocking(self):
        """A real overlap remains blocking — the split never softens it."""
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True)
        blocked = self._blocked_attempt(
            1, blocking=("mobile nav overlapping text, items unusable",)
        )
        result = self._drive_to_acceptance_with_final(orch, blocked)
        self.assertFalse(result.success)
        self.assertEqual(result.error, "VISION_BLOCKING_FINDINGS")
        self.assertNotIn("tested_snapshot", self.store.load("proj").deployment)

    def test_identical_evidence_contradiction_is_recorded_on_real_path(self):
        """Identical screenshots with contradictory verdicts -> consistency issue."""
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True, identical_shots=True)

        # Build the exact attempt history the production path would carry, using
        # the REAL orchestrator classifier and REAL screenshot digest helper.
        def _attempt(num, pass_, blocking):
            shots = self._screenshots(num, identical=True)
            return QAAttempt(
                attempt=num,
                deterministic=DeterministicFindings(
                    render_ok=True, desktop_screenshot_ok=True,
                    mobile_screenshot_ok=True, design_dna_valid=True,
                    source_present=True, build_ok=True, typecheck_ok=True),
                vision=VisionFindings(pass_=pass_, blocking_findings=list(blocking)),
                desktop_screenshot=str(shots.desktop),
                mobile_screenshot=str(shots.mobile),
            )

        attempts = [_attempt(0, True, []), _attempt(1, False, [REAL_BLOCKING])]
        cons = orch._classify_vision_consistency(attempts)
        self.assertTrue(cons["contradiction_detected"])
        self.assertEqual(
            cons["classes"][0]["classification"],
            INCONSISTENT_OR_UNVERIFIED_FINDING,
        )
        self.assertEqual(cons["classes"][0]["location"], "navigation")

    def test_no_third_frontend_call_when_vision_blocks(self):
        """The split never adds a FRONTEND invocation."""
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True)
        result = self._drive_to_acceptance_with_final(orch, self._blocked_attempt(1))
        self.assertFalse(result.success)
        # A blocking VISION verdict at acceptance never triggers a repair.
        self.assertEqual(self.mock_adapter.frontend_build.call_count, 0)


class EvidencePreservationTests(_Fixture):
    """The consistency record preserves evidence + uncertainty."""

    def test_diagnostic_preserves_evidence_and_uncertainty(self):
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True)
        blocked = self._blocked_attempt(
            1,
            vision=VisionFindings(
                pass_=False,
                blocking_findings=[REAL_BLOCKING],
                observations=["nav could be tighter"],
                summary="mobile nav broken",
            ),
        )
        self._drive_to_acceptance_with_final(orch, blocked)
        state = self.store.load("proj")

        # Raw VISION evidence is preserved verbatim in the failure record.
        self.assertTrue(state.failure["vision_findings"])
        blocked_findings = [
            f for f in state.failure["vision_findings"] if f["blocking"]
        ]
        self.assertTrue(blocked_findings)
        self.assertEqual(blocked_findings[-1]["blocking"], [REAL_BLOCKING])
        # The classification record carries location/severity/evidence + a note
        # stating the finding is unconfirmed (uncertainty preserved).
        cons = state.failure["vision_consistency"]
        entry = cons["classes"][0]
        self.assertEqual(entry["severity"], "blocking")
        self.assertEqual(entry["location"], "navigation")
        self.assertEqual(entry["evidence"], REAL_BLOCKING)
        self.assertNotEqual(entry["classification"], FIDELITY_CONFIRMED_DEFECT)
        self.assertTrue(cons["notes"])

    def test_split_does_not_touch_project_state_or_budget(self):
        """p1-style safety: the split writes nothing outside the failing project."""
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True)
        result = self._drive_to_acceptance_with_final(orch, self._blocked_attempt(1))
        self.assertFalse(result.success)
        # No durable FRONTEND budget ledger is created by the calibration path
        # (the budget is only ever created by the build/revise boundary).
        budget_dir = Path(self.tmp_obj.name) / "state" / ".frontend_budget"
        self.assertFalse(budget_dir.exists())


if __name__ == "__main__":
    unittest.main()
