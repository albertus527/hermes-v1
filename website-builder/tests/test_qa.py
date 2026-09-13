"""Phase 8 tests: QA + bounded repair loop.

Tests do NOT require a live LLM, browser, or npm. All external boundaries
(LocalRenderer, ScreenshotCapture, HermesAdapter) are injected as mocks.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.qa.findings import DeterministicFindings, QAAttempt, VisionFindings
from app.qa.orchestrator import MAX_REPAIR_ATTEMPTS, QAOrchestrator
from app.qa.render import RenderHandle, RenderError
from app.qa.screenshot import ScreenshotSet
from app.sandbox.runner import ProjectRunner


def _queue_and_run_project(store: ProjectStateStore, project_id: str) -> None:
    """Advance a fresh project to RUNNING (post-Phase-7 state)."""
    store.transition_lifecycle(project_id, ProjectLifecycle.READY)
    store.transition_lifecycle(project_id, ProjectLifecycle.QUEUED)
    store.transition_lifecycle(project_id, ProjectLifecycle.RUNNING)


def _make_workspace(tmpdir: Path, project_id: str) -> Path:
    ws = tmpdir / "workspaces" / project_id
    (ws / "src").mkdir(parents=True)
    (ws / "src" / "App.tsx").write_text("// real content, not starter", encoding="utf-8")
    (ws / "design-dna.json").write_text('{"version": 1}', encoding="utf-8")
    return ws


class _FixtureBase(unittest.TestCase):
    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        self.workspace_root = self.tmpdir / "workspaces"
        self.state_root = self.tmpdir / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)

        self.workspace = _make_workspace(self.tmpdir, "proj")

        self.mock_renderer = MagicMock()
        self.mock_capture = MagicMock()
        self.mock_adapter = MagicMock()

        self.orchestrator = QAOrchestrator(
            self.runner,
            self.store,
            hermes_adapter=self.mock_adapter,
            renderer=self.mock_renderer,
            screenshot_capture=self.mock_capture,
        )

        self.brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
        self.design_dna = {"version": 1, "brand_personality": "premium"}

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _passing_render(self):
        handle = RenderHandle(project_id="proj", port=5100, process=MagicMock(), url="http://127.0.0.1:5100/")
        self.mock_renderer.start.return_value = handle
        return handle

    def _passing_screenshots(self, attempt: int):
        qa_dir = self.workspace / "qa" / f"attempt-{attempt}"
        qa_dir.mkdir(parents=True, exist_ok=True)
        desktop = qa_dir / "desktop.png"
        mobile = qa_dir / "mobile.png"
        desktop.write_bytes(b"fake-png")
        mobile.write_bytes(b"fake-png")
        return ScreenshotSet(desktop=desktop, mobile=mobile)

    def _passing_vision(self):
        return {"pass": True, "critical": [], "major": [], "minor": [], "summary": "Looks good."}

    def _blocking_vision(self, critical=None, major=None):
        return {
            "pass": False,
            "critical": critical or ["Hero image missing"],
            "major": major or [],
            "minor": [],
            "summary": "Blocking issue found.",
        }

    def _minor_only_vision(self):
        return {"pass": True, "critical": [], "major": [], "minor": ["Slightly uneven spacing"], "summary": "Minor polish only."}


class TestQASuccess(_FixtureBase):
    """1. QA success: RUNNING -> QA pass -> PREVIEW_READY."""

    def test_qa_success_reaches_preview_ready(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()

        result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 0)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)


class TestInitialBlockingThenRepairPasses(_FixtureBase):
    """2. Initial visual blocking failure -> repair 1 -> fresh QA passes -> PREVIEW_READY."""

    def test_repair_once_then_pass(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.side_effect = [
            self._blocking_vision(),
            self._passing_vision(),
        ]
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 1)
        self.assertEqual(len(result.attempts), 2)
        self.mock_adapter.frontend_build.assert_called_once()
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)


class TestRepairBudgetExhaustion(_FixtureBase):
    """3. Repair budget exhaustion -> repair1 -> repair2 -> still blocking -> FAILED, no 3rd repair."""

    def test_exhausts_budget_and_fails(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.side_effect = [
            self._blocking_vision(),
            self._blocking_vision(),
            self._blocking_vision(),
        ]
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertEqual(result.repair_attempts, MAX_REPAIR_ATTEMPTS)
        self.assertEqual(self.mock_adapter.frontend_build.call_count, MAX_REPAIR_ATTEMPTS)
        # No third repair: at most MAX_REPAIR_ATTEMPTS+1 QA attempts recorded
        self.assertLessEqual(len(result.attempts), MAX_REPAIR_ATTEMPTS + 1)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(state.failure["phase"], "qa")
        self.assertEqual(state.failure["repair_attempts"], MAX_REPAIR_ATTEMPTS)


class TestVisionBoundary(unittest.TestCase):
    """4. VISION has no source-edit tool access."""

    def test_vision_inspect_uses_zero_tool_boundary(self):
        from app.hermes.adapter import HermesAdapter

        tmpdir = tempfile.mkdtemp()
        try:
            store = ProjectStateStore(Path(tmpdir) / "state")
            adapter = HermesAdapter(store, hermes_home=Path(tmpdir) / "home", repo_root=Path(tmpdir) / "repo")

            with patch.object(adapter, "_run_fast_programmatic") as mock_fast:
                mock_fast.return_value = MagicMock(
                    success=True,
                    response='{"pass": true, "critical": [], "major": [], "minor": [], "summary": "ok"}',
                )
                desktop = Path(tmpdir) / "desktop.png"
                mobile = Path(tmpdir) / "mobile.png"
                desktop.write_bytes(b"x")
                mobile.write_bytes(b"x")

                adapter.vision_inspect(desktop, mobile, {"name": "N", "what": "W", "why": "Y"})

            # vision_inspect must route through the zero-tool programmatic
            # boundary (_run_fast_programmatic), never the FRONTEND CLI
            # boundary that grants file/terminal toolsets.
            mock_fast.assert_called_once()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestScreenshotsRequired(_FixtureBase):
    """5 & 6. Desktop + mobile screenshots both required; missing one blocks PREVIEW_READY."""

    def test_missing_desktop_screenshot_blocks(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()

        def _partial(url, qa_dir, attempt):
            s = self._passing_screenshots(attempt)
            return ScreenshotSet(desktop=None, mobile=s.mobile)

        self.mock_capture.capture.side_effect = _partial
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_missing_mobile_screenshot_blocks(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()

        def _partial(url, qa_dir, attempt):
            s = self._passing_screenshots(attempt)
            return ScreenshotSet(desktop=s.desktop, mobile=None)

        self.mock_capture.capture.side_effect = _partial
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)


class TestBrokenBuildAfterRepairBlocks(_FixtureBase):
    """7. Broken build after repair blocks PREVIEW_READY."""

    def test_broken_build_after_repair(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.return_value = self._blocking_vision()
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        # Rebuild checks always fail after repair.
        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(False, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)


class TestDeterministicFunctionalFailureBlocks(_FixtureBase):
    """8. Deterministic functional failure (render failure) blocks final pass."""

    def test_render_failure_blocks(self):
        _queue_and_run_project(self.store, "proj")
        self.mock_renderer.start.side_effect = RenderError("did not become ready")
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)


class TestMinorFindingsDoNotTriggerRepair(_FixtureBase):
    """9. Minor-only VISION findings do NOT trigger repair."""

    def test_minor_only_passes_immediately(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.return_value = self._minor_only_vision()

        result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)


class TestLifecycleNeverEarlyPreviewReady(_FixtureBase):
    """10. Lifecycle never reaches PREVIEW_READY before final QA pass."""

    def test_stays_running_during_repair_then_preview_ready(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)

        observed_lifecycles = []
        original_vision_calls = [self._blocking_vision(), self._passing_vision()]
        call_idx = {"n": 0}

        def _vision(*a, **kw):
            observed_lifecycles.append(self.store.load("proj").lifecycle)
            r = original_vision_calls[call_idx["n"]]
            call_idx["n"] += 1
            return r

        self.mock_adapter.vision_inspect.side_effect = _vision
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertTrue(result.success)
        # At the time VISION ran (both times), lifecycle must still be RUNNING —
        # never PREVIEW_READY before the final passing verification completes.
        for lc in observed_lifecycles:
            self.assertEqual(lc, ProjectLifecycle.RUNNING.value)


class TestCleanupRuns(_FixtureBase):
    """11. Cleanup runs on success, failure, exception, repair failure."""

    def test_cleanup_on_success(self):
        _queue_and_run_project(self.store, "proj")
        handle = self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()

        self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.mock_renderer.stop.assert_called_with(handle)

    def test_cleanup_on_failure(self):
        _queue_and_run_project(self.store, "proj")
        handle = self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.return_value = self._blocking_vision()
        self.mock_adapter.frontend_build.return_value = {"success": False, "error": "repair failed"}

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        # Render started once (attempt 0); repair failed to execute so the
        # loop stops there — renderer.stop must still have been called for
        # every render that was started.
        self.assertEqual(self.mock_renderer.stop.call_count, self.mock_renderer.start.call_count)

    def test_cleanup_on_exception(self):
        _queue_and_run_project(self.store, "proj")
        handle = self._passing_render()
        self.mock_capture.capture.side_effect = RuntimeError("boom")

        result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.mock_renderer.stop.assert_called_with(handle)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_cleanup_on_repair_failure(self):
        """Repair call itself fails (adapter returns success=False) -> stop loop, cleanup, FAILED."""
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.return_value = self._blocking_vision()
        self.mock_adapter.frontend_build.return_value = {"success": False, "error": "repair failed"}

        result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertEqual(self.mock_adapter.frontend_build.call_count, 1)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)


class TestMaxWorkersAndIsolationPreserved(_FixtureBase):
    """12 & 13. MAX_WORKERS / writer semantics and workspace isolation preserved."""

    def test_max_workers_unaffected_by_qa(self):
        from app.sandbox.runner import MAX_WORKERS

        self.assertEqual(MAX_WORKERS, 1)
        self.assertTrue(self.runner.acquire_project("other"))
        self.assertFalse(self.runner.acquire_project("proj"))
        self.runner.release_project("other")

    def test_workspace_isolation_preserved(self):
        from app.sandbox.runner import project_workspace_path

        path = project_workspace_path(self.workspace_root, "proj")
        self.assertTrue(str(path).startswith(str(self.workspace_root.resolve())))


class TestRepairPromptNoFabrication(_FixtureBase):
    """14. Repair prompt does not invent business facts."""

    def test_repair_instructions_forbid_fabrication(self):
        failed = QAAttempt(
            attempt=0,
            deterministic=DeterministicFindings(failures=["Desktop screenshot missing"]),
            vision=VisionFindings(pass_=False, critical=["Hero missing"], summary="bad"),
        )
        instructions = self.orchestrator._build_repair_instructions(self.design_dna, failed)

        self.assertIn("Do NOT invent business facts", instructions)
        self.assertIn("smallest targeted fix", instructions)
        self.assertIn("Do NOT redesign unrelated areas", instructions)


class TestAgentBrowserArgsEnv(unittest.TestCase):
    """Regression: AGENT_BROWSER_ARGS env seam for VPS sandbox-less hosts.

    Covers the Phase 8 runtime compatibility issue where ``agent-browser``
    fails with "No usable sandbox!" unless launched with e.g. ``--no-sandbox``.
    The seam must be opt-in only: unset/empty env preserves the original argv.
    """

    def _run_capture(self, env_value, tmpdir):
        """Invoke _default_capture_fn with a mocked subprocess + which, return recorded argv lists."""
        import app.qa.screenshot as screenshot_mod

        out_path = Path(tmpdir) / "shot.png"
        run_calls = []

        def _fake_run(argv, **kwargs):
            run_calls.append(list(argv))
            # Simulate a successful screenshot on the screenshot call.
            if "screenshot" in argv:
                out_path.write_bytes(b"fake-png")
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        env = {}
        if env_value is not None:
            env["AGENT_BROWSER_ARGS"] = env_value

        with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(screenshot_mod.subprocess, "run", side_effect=_fake_run), \
             patch.dict("os.environ", env, clear=False):
            # Ensure unset case truly removes the var even if the host has it.
            if env_value is None:
                import os
                os.environ.pop("AGENT_BROWSER_ARGS", None)
            result = screenshot_mod._default_capture_fn(
                "https://example.com", 1440, 900, out_path
            )

        return result, run_calls

    def test_env_unset_emits_no_args_flag(self):
        """A. env unset -> no --args emitted anywhere in the flow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls = self._run_capture(None, tmpdir)
        self.assertTrue(ok)
        self.assertEqual(len(calls), 3)  # open, screenshot, close
        for argv in calls:
            self.assertNotIn("--args", argv)

    def test_env_empty_emits_no_args_flag(self):
        """A2. env set but empty/whitespace -> treated as unset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls = self._run_capture("   ", tmpdir)
        self.assertTrue(ok)
        for argv in calls:
            self.assertNotIn("--args", argv)

    def test_env_single_arg_passed_through(self):
        """B. AGENT_BROWSER_ARGS='--no-sandbox' -> open argv contains --args '--no-sandbox'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls = self._run_capture("--no-sandbox", tmpdir)
        self.assertTrue(ok)
        open_argv = calls[0]
        self.assertIn("open", open_argv)
        self.assertIn("--args", open_argv)
        idx = open_argv.index("--args")
        self.assertEqual(open_argv[idx + 1], "--no-sandbox")

    def test_env_multiple_args_remain_intact(self):
        """C. Multiple args stay intact as a single --args payload."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls = self._run_capture(
                "--no-sandbox --disable-dev-shm-usage", tmpdir
            )
        self.assertTrue(ok)
        open_argv = calls[0]
        idx = open_argv.index("--args")
        self.assertEqual(
            open_argv[idx + 1], "--no-sandbox --disable-dev-shm-usage"
        )

    def test_screenshot_close_flow_unchanged(self):
        """D. screenshot + close invocations are identical with and without env args."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ok_noenv, calls_noenv = self._run_capture(None, tmpdir)
            ok_env, calls_env = self._run_capture("--no-sandbox", tmpdir)

        self.assertTrue(ok_noenv)
        self.assertTrue(ok_env)
        self.assertEqual(len(calls_noenv), 3)
        self.assertEqual(len(calls_env), 3)

        # Strip the open call's optional --args tail, then the flows must match
        # command-for-command (same tmpdir + same inputs -> same session name
        # and same out path, so argv must be identical apart from --args).
        def _normalize(calls):
            norm = []
            for argv in calls:
                if "--args" in argv:
                    argv = argv[: argv.index("--args")]
                norm.append(argv)
            return norm

        self.assertEqual(_normalize(calls_noenv), _normalize(calls_env))
        # close must be the final call in both flows.
        self.assertIn("close", calls_noenv[-1])
        self.assertIn("close", calls_env[-1])


class TestPreviousPhasesStillPass(unittest.TestCase):
    """15 & 16. Existing Phase 2-7 tests and timeout recovery remain passing.

    Verified by running the full existing suite alongside this file (see
    scripts/run_tests). This class asserts the Phase 8 package does not
    shadow or interfere with Phase 7's public surface.
    """

    def test_frontend_builder_still_importable_and_unaffected(self):
        from app.projects.build import FrontendBuilder, BuildResult  # noqa: F401

    def test_hermes_adapter_still_has_frontend_build(self):
        from app.hermes.adapter import HermesAdapter

        self.assertTrue(hasattr(HermesAdapter, "frontend_build"))
        self.assertTrue(hasattr(HermesAdapter, "vision_inspect"))


if __name__ == "__main__":
    unittest.main()
