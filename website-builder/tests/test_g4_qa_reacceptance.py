"""QA-only re-acceptance regression tests.

Locks the operator-only recovery mechanism approved after the Lumen Studio p3
(G4) build: a previously FAILED, ALREADY-BUILT revision can undergo FRESH QA
acceptance on the SAME source revision, with NO FRONTEND invocation, NO budget
reset, and NO manual lifecycle edit.

No live model, browser, network, or npm is required: the orchestrator is driven
with mocks exactly as the V4-A/V4-B2 suites do.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.core.lifecycle import ProjectLifecycle, can_transition  # noqa: E402
from app.core.state import ProjectStateStore  # noqa: E402
from app.deploy.snapshot import (  # noqa: E402
    TestedSnapshot as _TestedSnapshot,
    digest,
    read_tree,
    record_checks,
    source_fingerprint,
)
from app.qa.orchestrator import QAOrchestrator  # noqa: E402
from app.qa.reacceptance import (  # noqa: E402
    DEPLOYMENT_KEY,
    ReacceptanceError,
    VisionOnlyAdapter,
    run_qa_reacceptance,
)
from app.qa.render import RenderHandle  # noqa: E402
from app.qa.screenshot import BrowserMetrics, ScreenshotSet  # noqa: E402
from app.sandbox.runner import ProjectRunner  # noqa: E402

OWNER = "telegram:6329821361"

APP = """
export default function App() {
  return (<main><h1>Lumen Studio</h1>
    <p>Studi konseptual — bukan proyek selesai.</p></main>);
}
"""
CSS = ":root{--s:#f5f1ea;--i:#1f1d1a;--a:#b45a38}h1{font-family:'Playfair Display',serif}"
PRD = "# PRD\n\nLanding page untuk studio arsitektur.\n"
GUIDANCE = "# Design Guidance\n\nGaya editorial minimalis, premium, modern.\n"


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
        self.root = root
        self.workspace_root = root / "workspaces"
        self.store = ProjectStateStore(root / "state")
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.workspace = self.workspace_root / "tg-6329821361-p3"
        (self.workspace / "src").mkdir(parents=True)
        (self.workspace / "src" / "App.tsx").write_text(APP, encoding="utf-8")
        (self.workspace / "src" / "index.css").write_text(CSS, encoding="utf-8")
        (self.workspace / "design-dna.json").write_text('{"version": 1}', encoding="utf-8")
        (self.workspace / "PRD.md").write_text(PRD, encoding="utf-8")
        (self.workspace / "DesignGuidance.md").write_text(GUIDANCE, encoding="utf-8")
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

    # -- helpers -------------------------------------------------------

    def _passing_render(self):
        self.mock_renderer.start.return_value = RenderHandle(
            project_id="tg-6329821361-p3", port=5100, process=MagicMock(),
            url="http://127.0.0.1:5100/",
        )

    def _screenshots(self, attempt):
        d = self.workspace / "qa" / "reaccept" / f"attempt-{attempt}"
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

    def _orchestrator(self, adapter=None):
        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._screenshots(attempt)
        )
        # The deterministic rebuild shells out to npm; stub the runner so the
        # test exercises the re-acceptance LOGIC, not a real toolchain.
        self.runner.run_command = MagicMock(
            return_value=MagicMock(returncode=0, stdout="", stderr="")
        )
        return QAOrchestrator(
            self.runner, self.store,
            hermes_adapter=VisionOnlyAdapter(adapter or self.mock_adapter),
            renderer=self.mock_renderer, screenshot_capture=self.mock_capture,
            design_fidelity_enabled=True,
        )

    def _failed_project(self, *, revision=2):
        """A FAILED, already-built project whose checked binding matches disk."""
        pid = "tg-6329821361-p3"
        with self.store.acquire_writer(pid) as state:
            state.owner_id = OWNER
            state.roles["owner"] = OWNER
            state.brief = {"name": "Lumen Studio", "what": "studio arsitektur"}
            state.lifecycle = ProjectLifecycle.READY.value
            state.revisions.source_revision = revision
            state.revisions.design_dna_version = 1
            self.store.save(state)
        # Bind the exact build identity the failed build recorded.
        before = source_fingerprint(self.workspace)
        record_checks(self.store, pid, self.workspace, before)
        with self.store.acquire_writer(pid) as state:
            state.lifecycle = ProjectLifecycle.FAILED.value
            state.failure = {
                "phase": "qa", "error": "DESIGN_FIDELITY_FIDELITY_REPAIR_FAILED",
                "failed_at": 123.0, "repair_attempts": 2,
            }
            self.store.save(state)
        return pid

    # -- 1. re-evaluation without FRONTEND -----------------------------

    def test_existing_source_reevaluated_without_frontend(self):
        pid = self._failed_project()
        orch = self._orchestrator()
        result = run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        self.assertTrue(result.success, result.error)
        # FRONTEND was never invoked.
        self.assertEqual(self.mock_adapter.frontend_build.call_count, 0)
        # VISION WAS re-run (fresh evidence required).
        self.assertGreaterEqual(self.mock_adapter.vision_inspect.call_count, 1)

    # -- 2. same source revision preserved -----------------------------

    def test_source_revision_preserved(self):
        pid = self._failed_project(revision=2)
        orch = self._orchestrator()
        run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        state = self.store.load(pid)
        self.assertEqual(state.revisions.source_revision, 2)
        self.assertEqual(state.revisions.qa_revision, 2)

    # -- 3. fresh evidence required ------------------------------------

    def test_vision_did_not_run_fails_closed(self):
        pid = self._failed_project()
        self.mock_adapter.vision_inspect.side_effect = RuntimeError("vision down")
        orch = self._orchestrator()
        result = run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        self.assertFalse(result.success)
        self.assertIn("QA_REACCEPTANCE_INFRASTRUCTURE", result.error)
        self.assertEqual(self.store.load(pid).lifecycle, ProjectLifecycle.FAILED.value)

    def test_fresh_screenshots_are_written(self):
        pid = self._failed_project()
        orch = self._orchestrator()
        run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        shot_dir = self.workspace / "qa" / "reaccept" / "attempt-0"
        self.assertTrue((shot_dir / "desktop.png").is_file())
        self.assertTrue((shot_dir / "mobile.png").is_file())

    # -- 4. stale identity fails closed --------------------------------

    def test_changed_source_fails_closed(self):
        pid = self._failed_project()
        # Mutate the source AFTER the identity was recorded.
        (self.workspace / "src" / "App.tsx").write_text(APP + "\n// tampered\n", encoding="utf-8")
        orch = self._orchestrator()
        with self.assertRaises(ReacceptanceError) as ctx:
            run_qa_reacceptance(
                store=self.store, orchestrator=orch, workspace=self.workspace,
                project_id=pid, principal_id=OWNER,
            )
        self.assertEqual(ctx.exception.code, "QA_REACCEPTANCE_STALE_SOURCE")
        self.assertEqual(self.store.load(pid).lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(self.mock_adapter.frontend_build.call_count, 0)

    def test_changed_artifact_fails_closed(self):
        pid = self._failed_project()
        (self.workspace / "dist" / "index.html").write_text("<html>changed</html>", encoding="utf-8")
        orch = self._orchestrator()
        with self.assertRaises(ReacceptanceError) as ctx:
            run_qa_reacceptance(
                store=self.store, orchestrator=orch, workspace=self.workspace,
                project_id=pid, principal_id=OWNER,
            )
        self.assertEqual(ctx.exception.code, "QA_REACCEPTANCE_STALE_ARTIFACT")

    # -- 5. N6-E guidance parity ---------------------------------------

    def test_guidance_parity_mismatch_fails_closed(self):
        pid = self._failed_project()
        # Inject a RECORDED invocation snapshot whose guidance digest does NOT
        # match the on-disk document. This proves parity is verified against the
        # RECORD, independently of the source fingerprint (which still matches).
        from app.core.guidance_snapshot import DEPLOYMENT_GUIDANCE_KEY
        with self.store.acquire_writer(pid) as state:
            state.deployment[DEPLOYMENT_GUIDANCE_KEY] = {
                "latest_invocation_id": "r2", "latest_source_revision": 2,
                "history": [{
                    "invocation_id": "r2", "source_revision": 2,
                    "prd_digest": None, "design_digest": None,
                    "design_guidance_digest": "0" * 64,
                }],
            }
            self.store.save(state)
        orch = self._orchestrator()
        with self.assertRaises(ReacceptanceError) as ctx:
            run_qa_reacceptance(
                store=self.store, orchestrator=orch, workspace=self.workspace,
                project_id=pid, principal_id=OWNER,
            )
        self.assertEqual(ctx.exception.code, "GUIDANCE_VERSION_MISMATCH")
        self.assertEqual(self.mock_adapter.frontend_build.call_count, 0)

    # -- 6. blocking findings prevent acceptance -----------------------

    def test_vision_blocking_findings_prevent_acceptance(self):
        pid = self._failed_project()
        self.mock_adapter.vision_inspect.return_value = {
            "pass": False, "blocking": ["Mobile nav overlaps the hero"],
            "observations": [], "summary": "defect",
        }
        orch = self._orchestrator()
        result = run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        self.assertFalse(result.success)
        state = self.store.load(pid)
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertNotIn("tested_snapshot", state.deployment)

    # -- 7. success produces a verified tested snapshot ----------------

    def test_success_writes_verified_tested_snapshot(self):
        pid = self._failed_project()
        orch = self._orchestrator()
        result = run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        self.assertTrue(result.success, result.error)
        state = self.store.load(pid)
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)
        snap = _TestedSnapshot.from_dict(state.deployment["tested_snapshot"])
        # The snapshot is VERIFIABLE against the exact current bytes.
        snap.verify(self.workspace)
        self.assertEqual(snap.source_sha256, source_fingerprint(self.workspace))
        self.assertEqual(snap.artifact_sha256, digest(read_tree(self.workspace / "dist")))

    # -- 8. no duplicate acceptance / replay ---------------------------

    def test_replay_is_idempotent_no_duplicate_snapshot(self):
        pid = self._failed_project()
        orch = self._orchestrator()
        first = run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        self.assertTrue(first.success)
        self.mock_adapter.vision_inspect.reset_mock()
        # Second run: already PREVIEW_READY -> refused, and no second snapshot.
        second = run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        self.assertTrue(second.success)
        self.assertTrue(second.evidence.get("already_accepted"))
        self.assertEqual(self.mock_adapter.vision_inspect.call_count, 0)

    def test_concurrent_claim_is_refused(self):
        pid = self._failed_project()
        # Simulate an in-flight claim left by another (crashed) run.
        with self.store.acquire_writer(pid) as state:
            state.deployment[DEPLOYMENT_KEY] = {
                "status": "CLAIMED", "claim_id": "x", "source_revision": 2,
            }
            self.store.save(state)
        orch = self._orchestrator()
        with self.assertRaises(ReacceptanceError) as ctx:
            run_qa_reacceptance(
                store=self.store, orchestrator=orch, workspace=self.workspace,
                project_id=pid, principal_id=OWNER,
            )
        self.assertEqual(ctx.exception.code, "QA_REACCEPTANCE_IN_PROGRESS")

    def test_crash_reconcile_releases_dangling_claim(self):
        from app.core.state import reconcile_stranded_projects
        pid = self._failed_project()
        with self.store.acquire_writer(pid) as state:
            state.deployment[DEPLOYMENT_KEY] = {
                "status": "CLAIMED", "claim_id": "x", "source_revision": 2,
            }
            state.lifecycle = ProjectLifecycle.QA_REACCEPTING.value
            self.store.save(state)
        recovered = reconcile_stranded_projects(self.store)
        self.assertIn(pid, recovered)
        state = self.store.load(pid)
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(state.deployment[DEPLOYMENT_KEY]["status"], "REJECTED")
        # The recovered project is now admissible again.
        orch = self._orchestrator()
        result = run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        self.assertTrue(result.success, result.error)

    # -- 9. original evidence + budget preserved -----------------------

    def test_original_failure_and_budget_preserved(self):
        pid = self._failed_project()
        original_failure = dict(self.store.load(pid).failure)
        # A durable FRONTEND ledger at 2/2 must be untouched.
        ledger_dir = self.store.root / ".frontend_budget" / pid
        ledger_dir.mkdir(parents=True, exist_ok=True)
        ledger = ledger_dir / "build-r1.json"
        ledger.write_text('{"used": 2, "limit": 2}', encoding="utf-8")
        orch = self._orchestrator()
        result = run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        self.assertTrue(result.success, result.error)
        state = self.store.load(pid)
        # Original failure is preserved verbatim AND carried in the record.
        self.assertEqual(state.failure, original_failure)
        self.assertEqual(state.deployment[DEPLOYMENT_KEY]["original_failure"], original_failure)
        # Budget ledger byte-identical.
        self.assertEqual(ledger.read_text(encoding="utf-8"), '{"used": 2, "limit": 2}')
        self.assertEqual(self.mock_adapter.frontend_build.call_count, 0)

    # -- 10. no deployment / approval / publish side effects -----------

    def test_no_deployment_or_publication_side_effects(self):
        pid = self._failed_project()
        orch = self._orchestrator()
        run_qa_reacceptance(
            store=self.store, orchestrator=orch, workspace=self.workspace,
            project_id=pid, principal_id=OWNER,
        )
        state = self.store.load(pid)
        for forbidden in ("approval", "promotion_intent", "latest_shown_preview",
                          "last_live_deployment", "last_live_release"):
            self.assertNotIn(forbidden, state.deployment)
        self.assertIsNone(state.production_url)

    # -- 11. authorization: owner only ---------------------------------

    def test_non_owner_refused(self):
        pid = self._failed_project()
        orch = self._orchestrator()
        with self.assertRaises(ReacceptanceError) as ctx:
            run_qa_reacceptance(
                store=self.store, orchestrator=orch, workspace=self.workspace,
                project_id=pid, principal_id="telegram:999",
            )
        self.assertEqual(ctx.exception.code, "UNAUTHORIZED_ROLE")
        self.assertEqual(self.mock_adapter.frontend_build.call_count, 0)

    # -- 12. lifecycle admissibility -----------------------------------

    def test_not_allowed_unless_failed(self):
        pid = self._failed_project()
        with self.store.acquire_writer(pid) as state:
            state.lifecycle = ProjectLifecycle.PREVIEW_READY.value
            self.store.save(state)
        orch = self._orchestrator()
        with self.assertRaises(ReacceptanceError) as ctx:
            run_qa_reacceptance(
                store=self.store, orchestrator=orch, workspace=self.workspace,
                project_id=pid, principal_id=OWNER,
            )
        self.assertEqual(ctx.exception.code, "QA_REACCEPTANCE_NOT_ALLOWED_IN_LIFECYCLE")

    # -- 13. FRONTEND is structurally unreachable ----------------------

    def test_vision_only_adapter_blocks_frontend(self):
        wrapped = VisionOnlyAdapter(self.mock_adapter)
        # VISION delegates fine.
        self.assertTrue(callable(wrapped.vision_inspect))
        # FRONTEND is unreachable.
        with self.assertRaises(AssertionError):
            wrapped.frontend_build


class LifecycleEdgeTests(unittest.TestCase):
    """The lawful re-acceptance edges are exactly the approved set."""

    def test_edges(self):
        L = ProjectLifecycle
        self.assertTrue(can_transition(L.FAILED, L.QA_REACCEPTING))
        self.assertTrue(can_transition(L.QA_REACCEPTING, L.PREVIEW_READY))
        self.assertTrue(can_transition(L.QA_REACCEPTING, L.FAILED))
        # Never a door into a build or a publication.
        for target in (L.QUEUED, L.RUNNING, L.REVISION_REQUESTED, L.PUBLISHING, L.LIVE):
            self.assertFalse(can_transition(L.QA_REACCEPTING, target), target)
        # No other state may ENTER re-acceptance.
        for source in (L.READY, L.QUEUED, L.RUNNING, L.PREVIEW_READY, L.LIVE):
            self.assertFalse(can_transition(source, L.QA_REACCEPTING), source)


if __name__ == "__main__":
    unittest.main()
