"""Phase 8 tests: QA + bounded repair loop.

Tests do NOT require a live LLM, browser, or npm. All external boundaries
(LocalRenderer, ScreenshotCapture, HermesAdapter) are injected as mocks.
"""

from __future__ import annotations

import shutil
import subprocess
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


def _png_bytes(width: int, height: int) -> bytes:
    """Build a minimal valid PNG with the given pixel dimensions (stdlib only).

    Used by orchestrator fixtures so the evidence-integrity guard exercises
    real IHDR data: fixtures must be genuine PNGs at the intended viewport
    sizes, proving the guard accepts valid evidence and only rejects actual
    dimension mismatches.
    """
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
        desktop.write_bytes(_png_bytes(1440, 900))
        mobile.write_bytes(_png_bytes(390, 844))
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


def _tiny_png_bytes() -> bytes:
    """Return a minimal valid 1x1 transparent PNG (real bytes, not a stub).

    ``agent.image_routing.build_native_content_parts`` sniffs magic bytes and
    will skip anything it cannot parse/transcode as an image, so tests that
    exercise the real multimodal attachment path need genuine PNG bytes.
    """
    import base64

    return base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYII="
    )


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
                desktop.write_bytes(_tiny_png_bytes())
                mobile.write_bytes(_tiny_png_bytes())

                adapter.vision_inspect(desktop, mobile, {"name": "N", "what": "W", "why": "Y"})

            # vision_inspect must route through the zero-tool programmatic
            # boundary (_run_fast_programmatic), never the FRONTEND CLI
            # boundary that grants file/terminal toolsets.
            mock_fast.assert_called_once()
        finally:
            import shutil
            shutil.rmtree(tmpdir, ignore_errors=True)


class TestVisionMultimodalRouting(unittest.TestCase):
    """Phase 8 runtime-wiring fix: VISION must receive real multimodal image
    input (image_url content parts), not filesystem paths in text.
    """

    def setUp(self):
        self.tmpdir_obj = tempfile.TemporaryDirectory()
        self.tmpdir = Path(self.tmpdir_obj.name)
        from app.hermes.adapter import HermesAdapter

        self.store = ProjectStateStore(self.tmpdir / "state")
        self.adapter = HermesAdapter(
            self.store,
            hermes_home=self.tmpdir / "home",
            repo_root=self.tmpdir / "repo",
        )
        self.desktop = self.tmpdir / "desktop.png"
        self.mobile = self.tmpdir / "mobile.png"
        self.desktop.write_bytes(_tiny_png_bytes())
        self.mobile.write_bytes(_tiny_png_bytes())
        self.brief = {"name": "N", "what": "W", "why": "Y"}

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def test_content_parts_contains_text_and_two_images(self):
        """run_conversation receives a list with 1 text part + 2 image_url parts."""
        with patch.object(self.adapter, "_run_fast_programmatic") as mock_fast:
            mock_fast.return_value = MagicMock(
                success=True,
                response='{"pass": true, "critical": [], "major": [], "minor": [], "summary": "ok"}',
            )
            self.adapter.vision_inspect(self.desktop, self.mobile, self.brief)

        call_kwargs = mock_fast.call_args[1]
        parts = call_kwargs["content_parts"]
        self.assertIsInstance(parts, list)
        types = [p["type"] for p in parts]
        self.assertEqual(types.count("text"), 1)
        self.assertEqual(types.count("image_url"), 2)
        # require_vision must be requested for the model-capability audit.
        self.assertTrue(call_kwargs.get("require_vision"))

    def test_run_conversation_receives_list_not_string(self):
        """AIAgent.run_conversation must receive the content-part list, not
        a plain string containing filesystem paths."""
        captured = {}

        with patch("app.hermes.adapter.AIAgent") as mock_agent_cls, patch(
            "app.hermes.adapter.load_config",
            return_value={"website_builder": {"models": {"VISION": {"model": "m", "provider": "p"}}}},
        ), patch("agent.image_routing._lookup_supports_vision", return_value=True
        ), patch(
            "app.hermes.adapter.resolve_runtime_provider",
            return_value={
                "api_key": "k", "base_url": "https://x", "provider": "p",
                "requested_provider": "p", "api_mode": "chat_completions",
                "credential_pool": None,
            },
        ), patch(
            "app.hermes.adapter.get_fallback_chain", return_value=[],
        ), patch(
            "app.hermes.adapter._build_preloaded_skills_prompt", return_value=None,
        ), patch(
            "app.hermes.adapter._create_session_db_for_oneshot", return_value=None,
        ):
            agent_instance = MagicMock()

            def _run_conversation(msg):
                captured["message"] = msg
                return {"final_response": '{"pass": true, "critical": [], "major": [], "minor": [], "summary": "ok"}'}

            agent_instance.run_conversation.side_effect = _run_conversation
            mock_agent_cls.return_value = agent_instance

            self.adapter.vision_inspect(self.desktop, self.mobile, self.brief)

        self.assertIsInstance(captured["message"], list)
        image_parts = [p for p in captured["message"] if p.get("type") == "image_url"]
        self.assertEqual(len(image_parts), 2)

    def test_enabled_toolsets_is_empty_list(self):
        """VISION remains zero-tools even with multimodal content."""
        with patch("app.hermes.adapter.AIAgent") as mock_agent_cls, patch(
            "app.hermes.adapter.load_config",
            return_value={"website_builder": {"models": {"VISION": {"model": "m", "provider": "p"}}}},
        ), patch("agent.image_routing._lookup_supports_vision", return_value=True
        ), patch(
            "app.hermes.adapter.resolve_runtime_provider",
            return_value={
                "api_key": "k", "base_url": "https://x", "provider": "p",
                "requested_provider": "p", "api_mode": "chat_completions",
                "credential_pool": None,
            },
        ), patch(
            "app.hermes.adapter.get_fallback_chain", return_value=[],
        ), patch(
            "app.hermes.adapter._build_preloaded_skills_prompt", return_value=None,
        ), patch(
            "app.hermes.adapter._create_session_db_for_oneshot", return_value=None,
        ):
            agent_instance = MagicMock()
            agent_instance.run_conversation.return_value = {
                "final_response": '{"pass": true, "critical": [], "major": [], "minor": [], "summary": "ok"}'
            }
            mock_agent_cls.return_value = agent_instance

            self.adapter.vision_inspect(self.desktop, self.mobile, self.brief)

        self.assertEqual(mock_agent_cls.call_args[1]["enabled_toolsets"], [])

    def test_missing_desktop_screenshot_fails_closed(self):
        """Missing desktop screenshot -> vision_inspect fails closed, never
        silently proceeds text-only or with a partial image set."""
        with patch.object(self.adapter, "_run_fast_programmatic") as mock_fast:
            missing = self.tmpdir / "does-not-exist.png"
            result = self.adapter.vision_inspect(missing, self.mobile, self.brief)

        self.assertFalse(result["pass"])
        self.assertIn("error", result)
        mock_fast.assert_not_called()

    def test_missing_mobile_screenshot_fails_closed(self):
        with patch.object(self.adapter, "_run_fast_programmatic") as mock_fast:
            missing = self.tmpdir / "does-not-exist.png"
            result = self.adapter.vision_inspect(self.desktop, missing, self.brief)

        self.assertFalse(result["pass"])
        self.assertIn("error", result)
        mock_fast.assert_not_called()

    def test_prompt_wording_updated_to_attached_not_disk(self):
        """Prompt no longer claims screenshots are 'on disk'; states they
        are attached to the message."""
        prompt = self.adapter._build_vision_prompt(self.brief, None)
        self.assertIn("attached to this message", prompt)
        self.assertNotIn("on disk", prompt)

    def test_vision_model_capability_check_fails_closed(self):
        """A confirmed non-vision-capable model/provider blocks VISION."""
        with patch("app.hermes.adapter.load_config", return_value={"website_builder": {"models": {"VISION": {"model": "m", "provider": "p"}}}}), patch(
            "app.hermes.adapter.resolve_runtime_provider",
            return_value={
                "api_key": "k", "base_url": "https://x", "provider": "p",
                "requested_provider": "p", "api_mode": "chat_completions",
                "credential_pool": None,
            },
        ), patch(
            "app.hermes.adapter.get_fallback_chain", return_value=[],
        ), patch(
            "app.hermes.adapter._build_preloaded_skills_prompt", return_value=None,
        ), patch(
            "app.hermes.adapter._create_session_db_for_oneshot", return_value=None,
        ), patch(
            "agent.image_routing._lookup_supports_vision", return_value=False,
        ):
            result = self.adapter.vision_inspect(self.desktop, self.mobile, self.brief)

        self.assertFalse(result["pass"])
        self.assertIn("does not support image input", result["error"])


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


class TestEvidenceIntegrityGuard(unittest.TestCase):
    """Evidence-integrity guard: actual PNG pixel dimensions must match the
    intended viewports before evidence may be consumed downstream.

    The tg-6329821361 incident produced valid-looking PNGs at the browser's
    default 1280px width for BOTH viewports; these tests pin the guard that
    rejects that class of broken evidence.
    """

    def test_valid_dimensions_pass(self):
        from app.qa.screenshot import validate_screenshot_dimensions

        with tempfile.TemporaryDirectory() as tmpdir:
            desktop = Path(tmpdir) / "desktop.png"
            mobile = Path(tmpdir) / "mobile.png"
            desktop.write_bytes(_png_bytes(1440, 900))
            mobile.write_bytes(_png_bytes(390, 844))
            # Must not raise.
            validate_screenshot_dimensions(ScreenshotSet(desktop=desktop, mobile=mobile))

    def test_desktop_wrong_width_rejected(self):
        from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions

        with tempfile.TemporaryDirectory() as tmpdir:
            desktop = Path(tmpdir) / "desktop.png"
            mobile = Path(tmpdir) / "mobile.png"
            desktop.write_bytes(_png_bytes(1280, 720))  # browser default width
            mobile.write_bytes(_png_bytes(390, 844))
            with self.assertRaises(ScreenshotError) as ctx:
                validate_screenshot_dimensions(ScreenshotSet(desktop=desktop, mobile=mobile))
        self.assertIn("desktop", str(ctx.exception))
        self.assertIn("1440", str(ctx.exception))

    def test_mobile_wrong_width_rejected(self):
        from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions

        with tempfile.TemporaryDirectory() as tmpdir:
            desktop = Path(tmpdir) / "desktop.png"
            mobile = Path(tmpdir) / "mobile.png"
            desktop.write_bytes(_png_bytes(1440, 900))
            mobile.write_bytes(_png_bytes(1280, 720))  # browser default width
            with self.assertRaises(ScreenshotError) as ctx:
                validate_screenshot_dimensions(ScreenshotSet(desktop=desktop, mobile=mobile))
        self.assertIn("mobile", str(ctx.exception))
        self.assertIn("390", str(ctx.exception))

    def test_non_png_evidence_rejected(self):
        from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions

        with tempfile.TemporaryDirectory() as tmpdir:
            desktop = Path(tmpdir) / "desktop.png"
            mobile = Path(tmpdir) / "mobile.png"
            desktop.write_bytes(b"fake-png")  # not a PNG at all
            mobile.write_bytes(_png_bytes(390, 844))
            with self.assertRaises(ScreenshotError):
                validate_screenshot_dimensions(ScreenshotSet(desktop=desktop, mobile=mobile))

    def test_missing_files_are_skipped_here(self):
        """Missing files are the deterministic presence checks' job, not the
        dimension guard's — the guard only validates files that exist."""
        from app.qa.screenshot import validate_screenshot_dimensions

        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist.png"
            # Must not raise.
            validate_screenshot_dimensions(ScreenshotSet(desktop=missing, mobile=missing))


class TestEvidenceIntegrityBlocksBeforeVision(_FixtureBase):
    """Wrong-dimension evidence is a capture-infrastructure failure: it must
    never reach VISION and must never consume the FRONTEND repair budget."""

    def _run_with_bad_evidence(self, width: int, height: int, target="desktop"):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()

        def _bad_capture(url, qa_dir, attempt):
            s = self._passing_screenshots(attempt)
            getattr(s, target).write_bytes(_png_bytes(width, height))
            return s

        self.mock_capture.capture.side_effect = _bad_capture
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        return self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

    def test_bad_desktop_dimensions_fail_as_capture_error(self):
        result = self._run_with_bad_evidence(1280, 720)

        self.assertFalse(result.success)
        # VISION must never consume invalid evidence.
        self.mock_adapter.vision_inspect.assert_not_called()
        # Capture infrastructure failure must not consume the repair budget.
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        # Explicit capture/evidence failure, not a visual QA failure.
        self.assertIn("screenshot evidence integrity", result.error)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(state.failure["phase"], "qa")
        self.assertIn("screenshot evidence integrity", state.failure["error"])

    def test_bad_mobile_dimensions_fail_as_capture_error(self):
        result = self._run_with_bad_evidence(1280, 720, target="mobile")

        self.assertFalse(result.success)
        self.mock_adapter.vision_inspect.assert_not_called()
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        self.assertIn("screenshot evidence integrity", result.error)
        self.assertIn("mobile", result.error)
        self.mock_renderer.stop.assert_called_once()
        self.assertEqual(self.store.load("proj").failure["vision_findings"], [])

    def test_bad_evidence_after_repair_stops_without_another_repair(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()

        def capture(url, qa_dir, attempt):
            screenshots = self._passing_screenshots(attempt)
            if attempt:
                screenshots.mobile.write_bytes(_png_bytes(1280, 2501))
            return screenshots

        self.mock_capture.capture.side_effect = capture
        self.mock_adapter.vision_inspect.return_value = self._blocking_vision()
        self.mock_adapter.frontend_build.return_value = {
            "success": True, "design_dna": self.design_dna,
        }
        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertEqual(result.repair_attempts, 1)
        self.mock_adapter.frontend_build.assert_called_once()
        self.mock_adapter.vision_inspect.assert_called_once()
        self.assertIn("mobile", result.error)
        self.assertIn("screenshot evidence integrity", result.error)
        self.assertEqual(self.mock_renderer.stop.call_count, 2)

    def test_bad_evidence_does_not_consume_repair_budget(self):
        """Even with a passing VISION stub ready, invalid evidence fails the
        run with zero repairs — the budget is reserved for visual QA only."""
        result = self._run_with_bad_evidence(1280, 720)

        self.assertEqual(result.repair_attempts, 0)
        self.assertEqual(self.mock_adapter.frontend_build.call_count, 0)
        state = self.store.load("proj")
        self.assertEqual(state.failure["repair_attempts"], 0)


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
        self.assertEqual(len(calls), 4)  # open, set viewport, screenshot, close
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
        self.assertEqual(len(calls_noenv), 4)
        self.assertEqual(len(calls_env), 4)

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


class TestCaptureTimeoutHandling(unittest.TestCase):
    """Regression: VPS mobile-capture hang (subprocess.TimeoutExpired) must
    be converted to a controlled capture failure (False), never propagate
    as an uncaught exception — per _default_capture_fn's documented
    contract ("Returns False (never raises) on any failure")."""

    def test_open_timeout_returns_false_not_raises(self):
        import app.qa.screenshot as screenshot_mod

        def _fake_run(argv, **kwargs):
            if "open" in argv:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
            return MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=_fake_run):
                result = screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )
        self.assertFalse(result)

    def test_screenshot_timeout_returns_false_not_raises(self):
        import app.qa.screenshot as screenshot_mod

        def _fake_run(argv, **kwargs):
            if "screenshot" in argv:
                raise subprocess.TimeoutExpired(cmd=argv, timeout=30)
            return MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=_fake_run):
                result = screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )
        self.assertFalse(result)

    def test_close_timeout_does_not_mask_successful_result(self):
        """A hung cleanup `close` call must not raise past the function or
        override an otherwise-successful capture result."""
        import app.qa.screenshot as screenshot_mod

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"

            def _fake_run(argv, **kwargs):
                if "close" in argv:
                    raise subprocess.TimeoutExpired(cmd=argv, timeout=10)
                if "screenshot" in argv:
                    out_path.write_bytes(b"fake-png")
                return MagicMock(returncode=0)

            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=_fake_run):
                result = screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )
        self.assertTrue(result)

    def test_capture_timeout_does_not_consume_frontend_repair_budget(self):
        """A capture-layer TimeoutExpired surfaces as a deterministic
        capture failure (missing screenshot), not an uncaught exception
        that corrupts the QAOrchestrator's repair-loop accounting."""
        from app.core.state import ProjectStateStore
        from app.sandbox.runner import ProjectRunner
        from app.qa.orchestrator import QAOrchestrator
        from app.qa.render import RenderHandle

        with tempfile.TemporaryDirectory() as tmpdir_str:
            tmpdir = Path(tmpdir_str)
            store = ProjectStateStore(tmpdir / "state")
            runner = ProjectRunner(tmpdir / "work", store)
            workspace = _make_workspace(tmpdir, "proj-timeout")
            _queue_and_run_project(store, "proj-timeout")

            def _timeout_capture_fn(url, width, height, out_path):
                raise subprocess.TimeoutExpired(cmd=["agent-browser"], timeout=30)

            mock_renderer = MagicMock()
            handle = RenderHandle(project_id="proj-timeout", port=5100, process=MagicMock(),
                                   url="http://127.0.0.1:5100/")
            mock_renderer.start.return_value = handle

            # capture_fn raising is a bug in a *test double*; the real
            # _default_capture_fn must never raise (see tests above). Here
            # we assert the orchestrator's own boundary: if a capture_fn
            # somehow raises, the run() outer exception boundary converts
            # it to a failed QAResult rather than crashing the build path,
            # and it does not silently report success.
            from app.qa.screenshot import ScreenshotCapture
            capture = ScreenshotCapture(capture_fn=_timeout_capture_fn)

            orchestrator = QAOrchestrator(
                runner, store, hermes_adapter=MagicMock(),
                renderer=mock_renderer, screenshot_capture=capture,
            )
            result = orchestrator.run(
                "proj-timeout", workspace, {"name": "N", "what": "W", "why": "Y"}, {"version": 1}
            )
        self.assertFalse(result.success)


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


class TestBrowserOpenFailureCannotProceed(unittest.TestCase):
    """Audit #3: a failed `open` must never be treated as a successful
    screenshot capture."""

    def test_failed_open_short_circuits_before_screenshot(self):
        import app.qa.screenshot as screenshot_mod

        run_calls = []

        def _fake_run(argv, **kwargs):
            run_calls.append(list(argv))
            if "open" in argv:
                return MagicMock(returncode=1)
            if "screenshot" in argv:
                # Must never be reached.
                run_calls.append(["SHOULD_NOT_RUN"])
                return MagicMock(returncode=0)
            return MagicMock(returncode=0)

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=_fake_run):
                ok = screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )

        self.assertFalse(ok)
        self.assertFalse(out_path.exists())
        # screenshot command must never have run.
        self.assertFalse(any("screenshot" in c for c in run_calls if c != ["SHOULD_NOT_RUN"]))
        # close must still run for cleanup.
        self.assertTrue(any("close" in c for c in run_calls))


class TestViewportAppliedBeforeScreenshot(unittest.TestCase):
    """Regression: QA screenshots must be captured at the intended viewport.

    Root cause of the tg-6329821361 production incident: `_default_capture_fn`
    passed `--width/--height` to `agent-browser open`, but the CLI has no such
    flags — they were silently ignored, so every capture ran at the daemon's
    default viewport (1280x720) and desktop.png/mobile.png were identical in
    width. The documented viewport mechanism is the separate runtime command
    `agent-browser set viewport <w> <h>`, which must run between `open` and
    `screenshot` on the same session.
    """

    def _run_capture(self, tmpdir, viewport_returncode=0, viewport=(1440, 900)):
        import app.qa.screenshot as screenshot_mod

        out_path = Path(tmpdir) / "shot.png"
        run_calls = []

        def _fake_run(argv, **kwargs):
            run_calls.append(list(argv))
            if "screenshot" in argv:
                out_path.write_bytes(b"fake-png")
            return MagicMock(returncode=viewport_returncode if "viewport" in argv else 0)

        with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(screenshot_mod.subprocess, "run", side_effect=_fake_run):
            ok = screenshot_mod._default_capture_fn(
                "https://example.com", *viewport, out_path
            )

        return ok, run_calls, out_path

    def test_set_viewport_runs_between_open_and_screenshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls, _ = self._run_capture(tmpdir)

        self.assertTrue(ok)
        self.assertEqual(len(calls), 4)  # open, set viewport, screenshot, close

        open_idx = next(i for i, c in enumerate(calls) if "open" in c)
        viewport_idx = next(i for i, c in enumerate(calls) if "viewport" in c)
        screenshot_idx = next(i for i, c in enumerate(calls) if "screenshot" in c)
        self.assertLess(open_idx, viewport_idx)
        self.assertLess(viewport_idx, screenshot_idx)

        viewport_argv = calls[viewport_idx]
        self.assertIn("set", viewport_argv)
        self.assertIn("viewport", viewport_argv)
        w_idx = viewport_argv.index("viewport")
        self.assertEqual(viewport_argv[w_idx + 1 : w_idx + 3], ["1440", "900"])
        # Same session as the open call — viewport applies to that session.
        self.assertEqual(
            viewport_argv[viewport_argv.index("--session") + 1],
            calls[open_idx][calls[open_idx].index("--session") + 1],
        )

    def test_mobile_capture_uses_mobile_viewport(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls, _ = self._run_capture(tmpdir, viewport=(390, 844))

        self.assertTrue(ok)
        viewport_argv = next(c for c in calls if "viewport" in c)
        w_idx = viewport_argv.index("viewport")
        self.assertEqual(viewport_argv[w_idx + 1 : w_idx + 3], ["390", "844"])

    def test_open_no_longer_passes_width_height_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls, _ = self._run_capture(tmpdir)

        self.assertTrue(ok)
        open_argv = next(c for c in calls if "open" in c)
        self.assertNotIn("--width", open_argv)
        self.assertNotIn("--height", open_argv)

    def test_failed_set_viewport_short_circuits_before_screenshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls, out_path = self._run_capture(tmpdir, viewport_returncode=1)

        self.assertFalse(ok)
        self.assertFalse(out_path.exists())
        self.assertFalse(any("screenshot" in c for c in calls))
        # close must still run for cleanup.
        self.assertTrue(any("close" in c for c in calls))


def _png_dimensions(path):
    """Read (width, height) from a PNG's IHDR chunk — stdlib only."""
    import struct

    with open(path, "rb") as f:
        header = f.read(24)
    if header[:8] != b"\x89PNG\r\n\x1a\n" or header[12:16] != b"IHDR":
        raise ValueError(f"not a PNG: {path}")
    return struct.unpack(">II", header[16:24])


class _ResponsivePageServer:
    """Local HTTP server serving a viewport-responsive page (stdlib only).

    The E2E test must not depend on public network reachability, so the
    real-browser capture runs against this loopback page instead of an
    external site.
    """

    def __init__(self):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        page = (
            "<!doctype html><html><head><title>qa</title></head>"
            "<body style='margin:0'>"
            "<div id='vp' style='height:2000px'></div>"
            "<script>document.title = window.innerWidth + 'x' + window.innerHeight;</script>"
            "</body></html>"
        ).encode("utf-8")

        class _Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(page)))
                self.end_headers()
                self.wfile.write(page)

            def log_message(self, *args):
                pass

        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def shutdown(self):
        self._httpd.shutdown()
        self._httpd.server_close()


@unittest.skipIf(shutil.which("agent-browser") is None, "agent-browser CLI not installed")
class TestRealViewportDimensions(unittest.TestCase):
    """E2E: the real agent-browser capture path must produce PNGs at the
    intended viewport widths (desktop 1440, mobile 390). This is the
    pixel-level regression the mocked tests could never catch — the
    tg-6329821361 incident produced valid PNGs at the wrong width.
    """

    def test_desktop_and_mobile_widths_match_viewport_constants(self):
        from app.qa.screenshot import (
            DESKTOP_VIEWPORT,
            MOBILE_VIEWPORT,
            ScreenshotCapture,
        )

        server = _ResponsivePageServer()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                qa_dir = Path(tmpdir) / "qa"
                screenshots = ScreenshotCapture().capture(
                    f"http://127.0.0.1:{server.port}/", qa_dir, attempt=1
                )
                self.assertTrue(screenshots.complete, "both screenshots must be captured")
                desktop_w, _ = _png_dimensions(screenshots.desktop)
                mobile_w, _ = _png_dimensions(screenshots.mobile)
        finally:
            server.shutdown()

        self.assertEqual(desktop_w, DESKTOP_VIEWPORT[0])
        self.assertEqual(mobile_w, MOBILE_VIEWPORT[0])


class TestRenderLocalhostBind(unittest.TestCase):
    """Audit #5: QA preview render must bind explicitly to 127.0.0.1, not
    the generated project's public `host: true` vite config."""

    def test_preview_command_binds_localhost(self):
        from app.qa.render import LocalRenderer

        mock_runner = MagicMock()
        mock_runner.port_allocator.allocate.return_value = 5100
        mock_runner.start_background.return_value = MagicMock()

        with patch("app.qa.render._wait_for_http_ready", return_value=True):
            renderer = LocalRenderer(mock_runner)
            renderer.start("proj", Path("/tmp/ws"))

        call_args = mock_runner.start_background.call_args
        command = call_args[0][1]
        self.assertIn("--host", command)
        idx = command.index("--host")
        self.assertEqual(command[idx + 1], "127.0.0.1")


class TestRepairAttemptNumbering(unittest.TestCase):
    """Audit #8: attempt numbers never collide/duplicate across the repair
    loop, even when a post-repair rebuild fails."""

    def test_no_duplicate_attempt_numbers_on_rebuild_failure(self):
        tmpdir_obj = tempfile.TemporaryDirectory()
        try:
            tmpdir = Path(tmpdir_obj.name)
            workspace_root = tmpdir / "workspaces"
            store = ProjectStateStore(tmpdir / "state")
            runner = ProjectRunner(workspace_root, store)
            workspace = _make_workspace(tmpdir, "proj")
            _queue_and_run_project(store, "proj")

            mock_renderer = MagicMock()
            mock_capture = MagicMock()
            mock_adapter = MagicMock()

            def _passing_screenshots(attempt):
                qa_dir = workspace / "qa" / f"attempt-{attempt}"
                qa_dir.mkdir(parents=True, exist_ok=True)
                desktop = qa_dir / "desktop.png"
                mobile = qa_dir / "mobile.png"
                desktop.write_bytes(_png_bytes(1440, 900))
                mobile.write_bytes(_png_bytes(390, 844))
                return ScreenshotSet(desktop=desktop, mobile=mobile)

            handle = RenderHandle(project_id="proj", port=5100, process=MagicMock(), url="http://127.0.0.1:5100/")
            mock_renderer.start.return_value = handle
            mock_capture.capture.side_effect = lambda url, qa_dir, attempt: _passing_screenshots(attempt)
            mock_adapter.vision_inspect.return_value = {
                "pass": False, "critical": ["bad"], "major": [], "minor": [], "summary": "x",
            }
            mock_adapter.frontend_build.return_value = {"success": True, "design_dna": {"version": 1}}

            orchestrator = QAOrchestrator(
                runner, store, hermes_adapter=mock_adapter,
                renderer=mock_renderer, screenshot_capture=mock_capture,
            )

            # First repair's rebuild fails, second repair's rebuild succeeds
            # but QA still blocks -> budget exhausted at MAX_REPAIR_ATTEMPTS.
            with patch.object(
                orchestrator, "_run_rebuild_checks",
                side_effect=[(False, True), (True, True)],
            ):
                result = orchestrator.run("proj", workspace, {"name": "N", "what": "W", "why": "Y"}, {"version": 1})

            attempt_numbers = [a.attempt for a in result.attempts]
            # No duplicate attempt numbers anywhere in the recorded history.
            self.assertEqual(len(attempt_numbers), len(set(attempt_numbers)))
            # Attempt numbers must be sequential starting at 0.
            self.assertEqual(attempt_numbers, sorted(attempt_numbers))
        finally:
            tmpdir_obj.cleanup()


class TestVisionFailureBlocksQA(unittest.TestCase):
    """Audit #11: a VISION runtime failure must be a blocking QA condition,
    never silently treated as 'no findings'."""

    def test_vision_raw_error_is_blocking(self):
        from app.qa.findings import VisionFindings

        findings = VisionFindings(pass_=False, critical=[], major=[], minor=[], raw_error="provider timeout")
        self.assertTrue(findings.blocking)

    def test_vision_no_error_no_findings_not_blocking(self):
        from app.qa.findings import VisionFindings

        findings = VisionFindings(pass_=True, critical=[], major=[], minor=[])
        self.assertFalse(findings.blocking)


# ---------------------------------------------------------------------------
# MEDIUM-1: QA infrastructure failure isolation
# ---------------------------------------------------------------------------


class TestInfrastructureFailureIsolation(_FixtureBase):
    """Render/capture/VISION infrastructure failures must fail immediately
    without FRONTEND repair, repair-budget consumption, or source mutation.
    Genuine visual findings still use bounded repair normally."""

    def test_render_failure_is_infrastructure_not_repairable(self):
        _queue_and_run_project(self.store, "proj")
        self.mock_renderer.start.side_effect = RenderError("pid did not become ready")

        result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertTrue(result.error.startswith("INFRASTRUCTURE_ERROR:render_failed:"))
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        self.mock_adapter.vision_inspect.assert_not_called()
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertTrue(
            state.failure["error"].startswith("INFRASTRUCTURE_ERROR:render_failed:")
        )

    def test_capture_failure_is_infrastructure_not_repairable(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()
        self.mock_capture.capture.side_effect = RuntimeError("browser crashed")

        result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertTrue(result.error.startswith("INFRASTRUCTURE_ERROR:capture_failed:"))
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        self.mock_adapter.vision_inspect.assert_not_called()
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_vision_exception_is_infrastructure_not_repairable(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        )
        self.mock_adapter.vision_inspect.side_effect = RuntimeError("provider exploded")

        result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertTrue(result.error.startswith("INFRASTRUCTURE_ERROR:vision_failed:"))
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_vision_error_field_is_infrastructure_not_repairable(self):
        """VISION returning an error payload (quota/auth/timeout) short-
        circuits as infrastructure, never consumed as a visual finding."""
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        )
        self.mock_adapter.vision_inspect.return_value = {
            "pass": False,
            "error": "provider 429: quota exhausted",
        }

        result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertTrue(result.error.startswith("INFRASTRUCTURE_ERROR:vision_failed:"))
        self.assertIn("quota exhausted", result.error)
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_genuine_visual_findings_still_use_bounded_repair(self):
        """Control: real blocking visual findings (no infra error) still
        consume the bounded FRONTEND repair budget exactly as before."""
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        )
        self.mock_adapter.vision_inspect.return_value = self._blocking_vision()
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": self.design_dna,
        }

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertEqual(result.repair_attempts, MAX_REPAIR_ATTEMPTS)
        self.assertEqual(
            self.mock_adapter.frontend_build.call_count, MAX_REPAIR_ATTEMPTS
        )
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)


# ---------------------------------------------------------------------------
# MEDIUM-5: protected toolchain files verified after every QA repair
# ---------------------------------------------------------------------------


class TestToolchainProtectionRepair(_FixtureBase):
    """After EVERY FRONTEND repair the protected toolchain files are
    re-verified; mutation/removal deterministically fails with
    TOOLCHAIN_MUTATION_REJECTED."""

    def _orch_with_verify(self, verify):
        return QAOrchestrator(
            self.runner,
            self.store,
            hermes_adapter=self.mock_adapter,
            renderer=self.mock_renderer,
            screenshot_capture=self.mock_capture,
            toolchain_verify=verify,
        )

    def test_toolchain_mutation_during_repair_fails_deterministically(self):
        _queue_and_run_project(self.store, "proj")
        verify = MagicMock(return_value="modified:package.json")
        orchestrator = self._orch_with_verify(verify)

        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        )
        # Attempt 0 blocks visually -> repair runs and mutates the toolchain.
        self.mock_adapter.vision_inspect.side_effect = [
            self._blocking_vision(),
            self._passing_vision(),
        ]
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": self.design_dna,
        }

        with patch.object(orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertTrue(result.error.startswith("TOOLCHAIN_MUTATION_REJECTED"))
        self.assertIn("modified:package.json", result.error)
        self.assertEqual(result.repair_attempts, 1)
        self.mock_adapter.frontend_build.assert_called_once()
        verify.assert_called_once()
        # No second repair, no budget burn beyond the violating repair.
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertIn("TOOLCHAIN_MUTATION_REJECTED", state.failure["error"])

    def test_clean_repair_is_verified_and_continues(self):
        _queue_and_run_project(self.store, "proj")
        verify = MagicMock(return_value=None)
        orchestrator = self._orch_with_verify(verify)

        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        )
        self.mock_adapter.vision_inspect.side_effect = [
            self._blocking_vision(),
            self._passing_vision(),
        ]
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": self.design_dna,
        }

        with patch.object(orchestrator, "_run_rebuild_checks", return_value=(True, True)):
            result = orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 1)
        verify.assert_called_once()



if __name__ == "__main__":
    unittest.main()
