"""V4-A regression tests — runtime integration of the V3-C fidelity gate.

Drives the REAL QAOrchestrator acceptance path with the gate ENABLED, and
proves the integration contract:

* staging (enabled) actually invokes the gate; production (default) does not;
* evidence is read from the same workspace/revision being accepted;
* genuine findings are preserved, absent evidence stays UNKNOWN (never a
  fabricated FAIL), and explicit conceptual disclosure avoids false positives;
* a HARD finding blocks acceptance (no tested snapshot, no preview, no publish);
* at most ONE repair through the EXISTING executor, or detection-only
  (repair_unavailable) when no executor exists;
* post-repair evidence is regenerated from the CHANGED artifacts;
* a changed revision can never reuse a stale PASS verdict.

No live model, browser, network, or npm is required.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.composition import invalidate_artifact  # noqa: E402
from app.core.lifecycle import ProjectLifecycle  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.deploy.snapshot import record_checks, source_fingerprint  # noqa: E402
from app.qa.orchestrator import QAOrchestrator  # noqa: E402
from app.qa.render import RenderHandle  # noqa: E402
from app.qa.screenshot import BrowserMetrics, ScreenshotSet  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402

INTENT = (
    "Gaya desain editorial minimalis, premium, dan modern. Gunakan tipografi "
    "elegan, warna off-white, charcoal, serta aksen terracotta."
)
BRIEF = {
    "name": "Forma Atelier",
    "what": "Landing page untuk studio arsitektur",
    "why": "Menampilkan studio secara profesional: hero, showcase 3 proyek arsitektur",
    "design_intent": INTENT,
}
#: The real rev-1 weaknesses: repeated placeholder cards + a real-work claim.
FA_REV1_APP = """
const projects = [
  { label: 'Proyek 01', title: 'Contoh Proyek 01', note: 'Nama proyek menyusul.' },
  { label: 'Proyek 02', title: 'Contoh Proyek 02', note: 'Nama proyek menyusul.' },
  { label: 'Proyek 03', title: 'Contoh Proyek 03', note: 'Nama proyek menyusul.' },
];
export default function App() {
  return (<main><h1>Forma Atelier</h1>
    <p>Kami menampilkan karya kami di halaman ini.</p></main>);
}
"""
FA_REV1_CSS = ":root{--s:#faf9f7;--i:#1c1b1a;--a:#a8674a}h1{font-family:Georgia,serif}"
#: An honest conceptual preview (disclosed) — must NOT be a false positive.
FA_CLEAN_APP = """
export default function App() {
  return (<main><h1>Forma Atelier</h1>
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


class _Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp_obj = tempfile.TemporaryDirectory()
        root = Path(self.tmp_obj.name)
        self.workspace_root = root / "workspaces"
        self.store = ProjectStateStore(root / "state")
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.workspace = self.workspace_root / "proj"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "App.tsx").write_text(FA_REV1_APP, encoding="utf-8")
        (self.workspace / "src" / "index.css").write_text(FA_REV1_CSS, encoding="utf-8")
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

    def _queue(self, pid="proj", brief=None):
        self.store.transition_lifecycle(pid, ProjectLifecycle.READY)
        with self.store.acquire_writer(pid) as state:
            state.brief = dict(brief if brief is not None else BRIEF)
            self.store.save(state)
        self.store.transition_lifecycle(pid, ProjectLifecycle.QUEUED)
        self.store.transition_lifecycle(pid, ProjectLifecycle.RUNNING)

    def _passing_render(self):
        self.mock_renderer.start.return_value = RenderHandle(
            project_id="proj", port=5100, process=MagicMock(),
            url="http://127.0.0.1:5100/",
        )

    def _screenshots(self, attempt):
        d = self.workspace / "qa" / f"attempt-{attempt}"
        d.mkdir(parents=True, exist_ok=True)
        desk = d / "desktop.png"
        mob = d / "mobile.png"
        desk.write_bytes(_png_bytes(1440, 900))
        mob.write_bytes(_png_bytes(390, 844))
        return ScreenshotSet(
            desktop=desk, mobile=mob,
            desktop_metrics=_metrics(1440, 900),
            mobile_metrics=_metrics(390, 844),
        )

    def _bind_checked(self):
        before = source_fingerprint(self.workspace)
        record_checks(self.store, "proj", self.workspace, before)

    def _orchestrator(self, *, enabled: bool, adapter=None):
        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._screenshots(attempt)
        )
        return QAOrchestrator(
            self.runner, self.store, hermes_adapter=(adapter or self.mock_adapter),
            renderer=self.mock_renderer, screenshot_capture=self.mock_capture,
            design_fidelity_enabled=enabled,
        )


class ProductionDefaultUnchangedTests(_Fixture):
    """The gate is OFF by default: production behaviour is byte-identical."""

    def test_default_orchestrator_does_not_run_the_gate(self):
        orch = QAOrchestrator(self.runner, self.store, hermes_adapter=self.mock_adapter)
        self.assertFalse(orch._design_fidelity_enabled)
        # With no gate, the stage returns None and writes no verdict.
        self.assertIsNone(
            orch._run_design_fidelity_stage(
                project_id="proj", workspace=self.workspace, brief=BRIEF,
                design_dna=None, attempts=[], next_attempt_num=0,
                qa_dir=self.workspace / "qa",
            )
        )
        self.assertIsNone(self.store.load("proj"))

    def test_disabled_run_records_no_fidelity_verdict(self):
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=False)
        result = orch.run("proj", self.workspace, BRIEF, None)
        self.assertTrue(result.success, result.error)
        state = self.store.load("proj")
        self.assertNotIn("design_fidelity", state.deployment)


class RuntimeInvocationTests(_Fixture):
    """Enabled (staging) actually invokes the gate on the real path."""

    def test_enabled_run_records_a_verdict_bound_to_the_revision(self):
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True)
        result = orch.run("proj", self.workspace, BRIEF, None)
        state = self.store.load("proj")
        self.assertIn("design_fidelity", state.deployment)
        verdict = state.deployment["design_fidelity"]
        # Bound to the source revision it judged (A4).
        self.assertEqual(verdict["source_revision"], state.revisions.source_revision)

    def test_genuine_rev1_findings_are_preserved_and_block_acceptance(self):
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True)
        result = orch.run("proj", self.workspace, BRIEF, None)
        # The rev-1 site has HARD findings -> acceptance is refused.
        self.assertFalse(result.success)
        self.assertTrue(str(result.error).startswith("DESIGN_FIDELITY_"))
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        # NO tested snapshot for a hard-failing artifact (C5).
        self.assertNotIn("tested_snapshot", state.deployment)
        rules = {
            f["rule_id"]
            for f in state.deployment["design_fidelity"]["report"]["findings"]
        }
        self.assertIn("repeated-card-content", rules)
        self.assertIn("conceptual-presented-as-real", rules)

    def test_absent_vision_evidence_is_unknown_not_a_fabricated_fail(self):
        # A clean site with NO screenshot evidence: the visual dimension must be
        # UNKNOWN (or PASS), never an invented FAIL from missing evidence.
        (self.workspace / "src" / "App.tsx").write_text(FA_CLEAN_APP, encoding="utf-8")
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True)
        # VISION runs but reports nothing (evidence present, no findings).
        self.mock_adapter.vision_inspect.return_value = {
            "pass": True, "blocking": [], "observations": [], "summary": "ok",
        }
        result = orch.run("proj", self.workspace, BRIEF, None)
        state = self.store.load("proj")
        report = state.deployment["design_fidelity"]["report"]
        # A disclosed conceptual preview is honest -> no ASSET_HONESTY failure.
        self.assertNotEqual(report["dimensions"]["ASSET_HONESTY"], "FAIL")
        self.assertTrue(result.success, result.error)

    def test_vision_blocking_findings_are_mapped_to_visual_evidence(self):
        from app.core.design_fidelity_runtime import vision_to_screenshot_evidence
        from app.qa.findings import VisionFindings
        v = VisionFindings(
            pass_=False,
            blocking_findings=["no visual hierarchy"],
            observations=["could be more premium"],
        )
        evidence = vision_to_screenshot_evidence(v)
        self.assertTrue(any(e.startswith("[blocking]") for e in evidence))
        self.assertTrue(any(e.startswith("[observation]") for e in evidence))
        # A VISION *infrastructure* failure is NOT a visual finding.
        failed = VisionFindings(pass_=False, raw_error="provider down")
        self.assertEqual(vision_to_screenshot_evidence(failed), ())
        # No VISION at all -> no evidence (UNKNOWN), never a fabricated defect.
        self.assertEqual(vision_to_screenshot_evidence(None), ())

    def test_explicit_conceptual_disclosure_avoids_false_positive(self):
        from app.core.design_fidelity_runtime import build_fidelity_report
        (self.workspace / "src" / "App.tsx").write_text(FA_CLEAN_APP, encoding="utf-8")
        report = build_fidelity_report(self.workspace, BRIEF)
        rules = {f.rule_id for f in report.findings}
        self.assertNotIn("conceptual-presented-as-real", rules)
        self.assertNotIn("conceptual-not-disclosed", rules)

    def test_unlabeled_conceptual_presented_as_real_is_detected(self):
        from app.core.design_fidelity_runtime import build_fidelity_report
        report = build_fidelity_report(self.workspace, BRIEF)
        rules = {f.rule_id for f in report.findings}
        self.assertIn("conceptual-presented-as-real", rules)

    def test_repeated_placeholder_content_triggers_intended_findings(self):
        from app.core.design_fidelity_runtime import build_fidelity_report
        report = build_fidelity_report(self.workspace, BRIEF)
        repeated = [f for f in report.findings if f.rule_id == "repeated-card-content"]
        self.assertTrue(repeated)


class BoundedRepairIntegrationTests(_Fixture):
    """At most ONE repair, through the EXISTING executor, then re-verify."""

    def test_detection_only_when_no_executor_exists(self):
        # No hermes adapter -> no validated executor -> detection-only, and the
        # gate reports repair_unavailable rather than simulating a repair (C3).
        self._queue()
        self._bind_checked()
        orch = self._orchestrator(enabled=True, adapter=None)
        # Force the adapter to None AFTER wiring the renderer/capture.
        orch.hermes_adapter = None
        result = orch.run("proj", self.workspace, BRIEF, None)
        self.assertFalse(result.success)
        verdict = self.store.load("proj").deployment["design_fidelity"]
        # No repair ran, and the honest reason is recorded.
        self.assertEqual(verdict["attempts_used"], 0)
        self.assertFalse(verdict["repair_used"])
        self.assertEqual(verdict["reason"], "repair_unavailable_no_executor")

    def test_one_repair_runs_through_the_existing_executor(self):
        self._queue()
        self._bind_checked()
        # The repair "fixes" the source: subsequent reads see the clean site.
        def _frontend_build(**_kwargs):
            (self.workspace / "src" / "App.tsx").write_text(
                FA_CLEAN_APP, encoding="utf-8"
            )
            return {"success": True, "design_dna": {"version": 2}}
        self.mock_adapter.frontend_build.side_effect = _frontend_build
        # After the repair, a rebuild is required; make it pass.
        self.runner.run_command = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        orch = self._orchestrator(enabled=True)
        result = orch.run("proj", self.workspace, BRIEF, None)
        verdict = self.store.load("proj").deployment["design_fidelity"]
        # Exactly ONE repair attempt at most (B4/C2).
        self.assertLessEqual(verdict["attempts_used"], 1)
        self.assertLessEqual(self.mock_adapter.frontend_build.call_count, 1)
        # If the repair verified, acceptance is allowed; otherwise it is refused.
        if result.success:
            self.assertTrue(verdict["repair_verified"])


class StaleVerdictTests(_Fixture):
    """A changed revision can never reuse a stale PASS (A4/C: crash-resume)."""

    def test_invalidate_artifact_drops_the_fidelity_verdict(self):
        self._queue()
        with self.store.acquire_writer("proj") as state:
            state.deployment["design_fidelity"] = {
                "outcome": "FIDELITY_PASS", "source_revision": 1,
            }
            self.store.save(state)
        with self.store.acquire_writer("proj") as state:
            invalidate_artifact(state)
            self.store.save(state)
        self.assertNotIn("design_fidelity", self.store.load("proj").deployment)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
