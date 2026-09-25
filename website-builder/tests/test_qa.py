"""Phase 8 tests: QA + bounded repair loop.

Tests do NOT require a live LLM, browser, or npm. All external boundaries
(LocalRenderer, ScreenshotCapture, HermesAdapter) are injected as mocks.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.qa.findings import DeterministicFindings, QAAttempt, VisionFindings
from app.qa.orchestrator import MAX_REPAIR_ATTEMPTS, QAOrchestrator
from app.qa.render import RenderHandle, RenderError
from app.qa.screenshot import BrowserMetrics, CaptureResult, ScreenshotSet
from app.sandbox.runner import ProjectRunner


def _metrics(
    width: int,
    height: int,
    *,
    document_width: int | None = None,
    body_width: int | None = None,
) -> BrowserMetrics:
    document_width = width if document_width is None else document_width
    body_width = width if body_width is None else body_width
    return BrowserMetrics(
        inner_width=width,
        inner_height=height,
        document_client_width=width,
        document_scroll_width=document_width,
        body_scroll_width=body_width,
    )


def _agent_metrics_response(metrics: BrowserMetrics) -> str:
    payload = json.dumps({
        "innerWidth": metrics.inner_width,
        "innerHeight": metrics.inner_height,
        "documentClientWidth": metrics.document_client_width,
        "documentScrollWidth": metrics.document_scroll_width,
        "bodyScrollWidth": metrics.body_scroll_width,
    })
    return json.dumps({"success": True, "data": {"result": payload}})


class _FakeBrowserRun:
    """Records argv and answers like the real file-backed browser spawn.

    ``_run_browser_command`` hands the child a temporary file as stdout and
    reads the reply back from that file, so a fake must WRITE to
    ``kwargs["stdout"]`` rather than return a ``stdout`` attribute.
    """

    def __init__(self, metrics: Optional[BrowserMetrics] = None, *,
                 returncodes: Optional[dict] = None, fail_on=()):
        self.calls: List[List[str]] = []
        self.metrics = metrics
        self.returncodes = returncodes or {}
        self.fail_on = fail_on

    def _command(self, argv: List[str]) -> str:
        if "open" in argv:
            return "open"
        if "viewport" in argv:
            return "viewport"
        if "eval" in argv:
            return "eval"
        if "screenshot" in argv:
            return "screenshot"
        if "close" in argv:
            return "close"
        return "unknown"

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        command = self._command(argv)
        if command in self.fail_on:
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 30))
        stdout = kwargs.get("stdout")
        out_path = None
        if command == "screenshot":
            out_path = argv[argv.index("--full") + 1]
            if out_path:
                Path(out_path).write_bytes(b"fake-png")
        payload = ""
        if command == "eval" and self.metrics is not None:
            payload = _agent_metrics_response(self.metrics)
        if command == "close":
            payload = json.dumps({"success": True, "data": {"closed": True}})
        if stdout is not None and payload:
            stdout.write(payload)
        return MagicMock(returncode=self.returncodes.get(command, 0), stdout=payload)


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
        return ScreenshotSet(
            desktop=desktop,
            mobile=mobile,
            desktop_metrics=_metrics(1440, 900),
            mobile_metrics=_metrics(390, 844),
        )

    def _passing_vision(self):
        return {"pass": True, "blocking": [], "observations": [], "summary": "Looks good."}

    def _blocking_vision(self, blocking=None):
        return {
            "pass": False,
            "blocking": blocking or ["Hero image missing"],
            "observations": [],
            "summary": "Blocking issue found.",
        }

    def _observations_only_vision(self, observations=None):
        return {
            "pass": True,
            "blocking": [],
            "observations": observations or ["Slightly uneven spacing"],
            "summary": "Non-blocking polish only.",
        }


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

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 1)
        self.assertEqual(len(result.attempts), 2)
        self.mock_adapter.frontend_build.assert_called_once()
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)


class TestRepairValidatesAgainstLockedReferenceSnapshot(_FixtureBase):
    """H-2: the QA repair path must validate the freshly-composed Design DNA
    against the design_references that were current when the repair
    instructions were composed — NOT against a live state object whose
    writer lock has already been released.

    Regression lock: previously ``_repair`` called
    ``validate_composed_dna(dna, state)`` using the ``state`` variable from
    the (already-exited) ``acquire_writer`` block.
    """

    def test_repair_passes_reference_snapshot_not_live_state(self):
        _queue_and_run_project(self.store, "proj")
        # Seed a reference set on the project so the snapshot is non-empty.
        # design_references is a role -> {"item": ReferenceItem(...).to_dict(),
        # "evidence": str} mapping.
        from app.core.references import ReferenceItem
        refs = {
            "UX": {
                "item": ReferenceItem("UX", "upload", "a" * 64, "image/png", 100).to_dict(),
                "evidence": "Clear hierarchy",
            }
        }
        with self.store.acquire_writer("proj") as state:
            state.design_references = dict(refs)
            self.store.save(state)

        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.side_effect = [
            self._blocking_vision(),
            self._passing_vision(),
        ]
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        seen = {}

        def _capture(dna, state_arg):
            seen["state"] = state_arg

        with patch(
            "app.qa.orchestrator.validate_composed_dna",
            side_effect=_capture,
        ) as mock_validate, patch.object(
            self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)
        ):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertTrue(result.success, result)
        mock_validate.assert_called_once()
        # The object handed to validate_composed_dna must expose the SAME
        # reference set captured under the lock; it must not be the live
        # (lock-released) ProjectState instance.
        self.assertEqual(seen["state"].design_references, refs)
        self.assertNotIsInstance(seen["state"], type(self.store.load("proj")))


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

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
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
                    response='{"pass": true, "blocking": [], "observations": [], "summary": "ok"}',
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
                response='{"pass": true, "blocking": [], "observations": [], "summary": "ok"}',
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
                return {"final_response": '{"pass": true, "blocking": [], "observations": [], "summary": "ok"}'}

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
                "final_response": '{"pass": true, "blocking": [], "observations": [], "summary": "ok"}'
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


class TestVisionPromptDesignDnaAuthority(unittest.TestCase):
    """Authority boundary: model-authored Design DNA must not manufacture new
    implementation-level blocking QA contracts.

    These tests exercise the real ``_build_vision_prompt`` output (the
    contract text VISION is given), asserting the authority boundary itself
    rather than matching only literal p5 strings. The boundary under test:
    the BRIEF is the sole authoritative source of explicit requirements;
    Design DNA is declarative design intent / context, never an
    acceptance-test contract.
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
        self.brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def _prompt(self, design_dna):
        return self.adapter._build_vision_prompt(self.brief, design_dna)

    def test_a_dna_implementation_rules_not_authoritative(self):
        """A. DNA containing DOM/CSS/file-level entries cannot become a
        blocking contract: the prompt must state such entries are not
        authoritative even when present in Design DNA."""
        dna = {
            "version": 1,
            "brand_personality": "premium",
            "implementation_contract": {
                "cta_button_class": "btn-primary-lg",
                "aria_disabled": "false",
                "file": "src/components/CTA.tsx",
            },
        }
        prompt = self._prompt(dna)

        # The DNA content is still embedded (context), but the prompt must
        # explicitly deny it implementation-level authority.
        self.assertIn("NOT a requirements source", prompt)
        self.assertIn("not an acceptance-test contract", prompt)
        self.assertIn("EVEN WHEN they appear inside Design DNA", prompt)
        self.assertIn("not an authoritative requirement", prompt)

    def test_b_self_authored_requirements_not_blockers(self):
        """B. p5-shaped self-authored rules (exclusivity / component counts /
        per-component contracts) do not become authoritative blockers merely
        because they appear in Design DNA."""
        dna = {
            "version": 1,
            "rules": [
                "there may only be one CTA",
                "exactly 3 feature cards",
                "hero must contain a <nav> element",
            ],
        }
        prompt = self._prompt(dna)

        # Numeric/exclusivity restrictions not explicit in the brief are
        # classified invalid; self-authored DNA rules are at most observations.
        self.assertIn("numeric/exclusivity restrictions", prompt)
        self.assertIn("not explicit in the brief", prompt)
        self.assertIn("at most an observation", prompt)
        self.assertIn("never a blocking finding", prompt)

    def test_c_declarative_dna_remains_available(self):
        """C. Legitimate declarative Design DNA (palette, typography,
        personality) remains available to VISION as intent/context."""
        dna = {
            "version": 1,
            "brand_personality": "premium, minimalist",
            "palette": {"primary": "#1a1a2e", "accent": "#c9a227"},
            "typography": {"heading_font": "Fraunces", "body_font": "Inter"},
        }
        prompt = self._prompt(dna)

        # Declarative intent is still embedded for VISION to judge alignment.
        self.assertIn("premium, minimalist", prompt)
        self.assertIn("#1a1a2e", prompt)
        self.assertIn("Fraunces", prompt)
        # And it is framed as declarative design intent.
        self.assertIn("declarative design intent", prompt)

    def test_d_brief_requirements_remain_authoritative(self):
        """D. Explicit user-visible requirements (the brief) remain
        authoritative: the prompt keeps the contradicts-explicit-requirement
        blocking rule and names the brief as the requirement source."""
        prompt = self._prompt({"version": 1, "brand_personality": "premium"})

        self.assertIn("BRIEF is the only authoritative source", prompt)
        self.assertIn("explicit requirement", prompt)
        # The blocking rule for contradicting an explicit requirement survives.
        self.assertIn("directly contradicts an explicit requirement", prompt)

    def test_e_blocking_observation_split_intact(self):
        """E. The blocking-vs-observation VISION contract remains intact."""
        prompt = self._prompt(None)

        self.assertIn("BLOCKING findings", prompt)
        self.assertIn("OBSERVATIONS", prompt)
        self.assertIn("NON-BLOCKING", prompt)
        # pass is still derived from an empty blocking list.
        self.assertIn('"pass" must be true if and only if "blocking" is empty', prompt)

    def test_f_raw_error_remains_fail_closed(self):
        """F. A VISION runtime failure (raw_error) remains fail-closed:
        it blocks regardless of an empty blocking list."""
        findings = VisionFindings(pass_=False, blocking_findings=[], raw_error="provider boom")
        self.assertTrue(findings.blocking)

        ok = VisionFindings(pass_=True, blocking_findings=[], observations=["nice"], raw_error=None)
        self.assertFalse(ok.blocking)

    def test_g_repair_budget_remains_exactly_two(self):
        """G. The bounded repair budget remains exactly 2 (no third repair)."""
        self.assertEqual(MAX_REPAIR_ATTEMPTS, 2)

    def test_frontend_prompt_forbids_implementation_level_dna(self):
        """The FRONTEND build prompt must forbid implementation-level
        acceptance criteria in Design DNA at authoring time (the source of
        the self-manufactured contract)."""
        prompt = self.adapter._build_frontend_prompt(self.brief, self.tmpdir / "ws")

        self.assertIn("DECLARATIVE description of design intent only", prompt)
        self.assertIn("no DOM attributes", prompt)
        self.assertIn("CSS", prompt)
        self.assertIn("class names", prompt)
        self.assertIn("no file paths", prompt)
        self.assertIn("no event handlers", prompt)
        self.assertIn("no component counts", prompt)
        self.assertIn("not a test specification", prompt)


class TestScreenshotsRequired(_FixtureBase):
    """5 & 6. Desktop + mobile screenshots both required; missing one blocks PREVIEW_READY."""

    def test_missing_desktop_screenshot_blocks(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()

        def _partial(url, qa_dir, attempt):
            s = self._passing_screenshots(attempt)
            return ScreenshotSet(
                desktop=None,
                mobile=s.mobile,
                mobile_metrics=s.mobile_metrics,
            )

        self.mock_capture.capture.side_effect = _partial
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_missing_mobile_screenshot_blocks(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()

        def _partial(url, qa_dir, attempt):
            s = self._passing_screenshots(attempt)
            return ScreenshotSet(
                desktop=s.desktop,
                mobile=None,
                desktop_metrics=s.desktop_metrics,
            )

        self.mock_capture.capture.side_effect = _partial
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)


class TestEvidenceIntegrityGuard(unittest.TestCase):
    """Live metrics prove viewport integrity; full-page PNG overflow is valid."""

    def _evidence(self, tmpdir: str, *, mobile_width=390, desktop_width=1440,
                  mobile_metrics=None, desktop_metrics=None):
        desktop = Path(tmpdir) / "desktop.png"
        mobile = Path(tmpdir) / "mobile.png"
        desktop.write_bytes(_png_bytes(desktop_width, 900))
        mobile.write_bytes(_png_bytes(mobile_width, 844))
        return ScreenshotSet(
            desktop=desktop,
            mobile=mobile,
            desktop_metrics=_metrics(1440, 900) if desktop_metrics is None else desktop_metrics,
            mobile_metrics=_metrics(390, 844) if mobile_metrics is None else mobile_metrics,
        )

    def test_mobile_live_metrics_and_matching_png_pass(self):
        from app.qa.screenshot import validate_screenshot_dimensions
        with tempfile.TemporaryDirectory() as tmpdir:
            validate_screenshot_dimensions(self._evidence(tmpdir))

    def test_wider_full_page_png_is_valid_for_390_viewport(self):
        from app.qa.screenshot import validate_screenshot_dimensions
        with tempfile.TemporaryDirectory() as tmpdir:
            validate_screenshot_dimensions(self._evidence(tmpdir, mobile_width=411))

    def test_mobile_live_width_mismatch_rejected(self):
        from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions
        malformed = BrowserMetrics(411, 844, 390, 411, 411)
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaises(ScreenshotError) as ctx:
                validate_screenshot_dimensions(
                    self._evidence(tmpdir, mobile_width=411, mobile_metrics=malformed)
                )
        self.assertIn("mobile innerWidth is 411", str(ctx.exception))
        self.assertIn("expected 390", str(ctx.exception))

    def test_missing_metrics_rejected(self):
        from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence = self._evidence(tmpdir)
            evidence.mobile_metrics = None
            with self.assertRaisesRegex(ScreenshotError, "live browser metrics are missing"):
                validate_screenshot_dimensions(evidence)

    def test_malformed_metric_probe_rejected_as_integrity_failure(self):
        from app.qa.screenshot import ScreenshotError
        with self.assertRaisesRegex(ScreenshotError, "documentClientWidth is missing or non-numeric"):
            BrowserMetrics.from_payload({
                "innerWidth": 390,
                "innerHeight": 844,
                "documentScrollWidth": 411,
                "bodyScrollWidth": 411,
            })

    def test_non_finite_or_fractional_metrics_rejected(self):
        from app.qa.screenshot import ScreenshotError
        with self.assertRaisesRegex(ScreenshotError, "not a finite non-negative integer"):
            BrowserMetrics.from_payload({
                "innerWidth": 390.5,
                "innerHeight": 844,
                "documentClientWidth": 390,
                "documentScrollWidth": 411,
                "bodyScrollWidth": 411,
            })

    def test_malformed_stored_metric_rejected_by_integrity_guard(self):
        from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions
        malformed = _metrics(390, 844)
        object.__setattr__(malformed, "inner_height", float("nan"))
        with tempfile.TemporaryDirectory() as tmpdir:
            with self.assertRaisesRegex(ScreenshotError, "live browser metrics are malformed"):
                validate_screenshot_dimensions(
                    self._evidence(tmpdir, mobile_metrics=malformed)
                )

    def test_metric_probe_failure_surfaces_without_screenshoting(self):
        import app.qa.screenshot as screenshot_mod

        fake = _FakeBrowserRun()
        original = fake.__call__

        def _run(argv, **kwargs):
            result = original(argv, **kwargs)
            if "eval" in argv:
                kwargs.get("stdout").write(json.dumps({
                    "success": False, "error": "probe failed",
                }))
            return result

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=_run):
                result = screenshot_mod._default_capture_fn(
                    "https://example.com", 390, 844, out_path
                )
        self.assertEqual(result.status, "metric_probe_failed")
        self.assertIsNone(result.metrics)
        self.assertFalse(any("screenshot" in call for call in fake.calls))
        self.assertEqual(fake.calls[-1][-1], "close")

    def test_browser_commands_never_spawn_with_inherited_pipes(self):
        """Regression: a cold Chromium launch inherits the parent's stdout
        handle, so a PIPE-backed spawn never sees EOF and every capture times
        out on Windows. Every browser call must be file-backed."""
        import app.qa.screenshot as screenshot_mod

        seen_kwargs = []

        def _fake_run(argv, **kwargs):
            seen_kwargs.append(kwargs)
            return MagicMock(returncode=0, stdout="")

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=_fake_run):
                screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )

        self.assertTrue(seen_kwargs)
        for kwargs in seen_kwargs:
            self.assertNotIn("capture_output", kwargs)
            self.assertIn("stdout", kwargs)
            self.assertIs(kwargs["stderr"], subprocess.STDOUT)

    def test_browser_timeout_is_reported_as_capture_failure(self):
        import app.qa.screenshot as screenshot_mod

        def _fake_run(argv, **kwargs):
            raise subprocess.TimeoutExpired(argv, kwargs.get("timeout", 30))

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=_fake_run):
                result = screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )
        self.assertFalse(result.captured)
        self.assertEqual(result.status, "capture_failed")
        self.assertIn("timed out", result.error)

    def test_non_png_evidence_rejected(self):
        from app.qa.screenshot import ScreenshotError, validate_screenshot_dimensions
        with tempfile.TemporaryDirectory() as tmpdir:
            evidence = self._evidence(tmpdir)
            evidence.desktop.write_bytes(b"fake-png")
            with self.assertRaises(ScreenshotError):
                validate_screenshot_dimensions(evidence)

    def test_missing_files_are_skipped_for_presence_checks(self):
        from app.qa.screenshot import validate_screenshot_dimensions
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "does-not-exist.png"
            validate_screenshot_dimensions(ScreenshotSet(desktop=missing, mobile=missing))


class TestDeterministicHorizontalOverflow(unittest.TestCase):
    """Live scrollWidth overflow is one deterministic finding per viewport."""

    def _findings(self, screenshots: ScreenshotSet):
        from app.qa.deterministic import run_deterministic_checks
        with tempfile.TemporaryDirectory() as tmpdir:
            workspace = _make_workspace(Path(tmpdir), "proj")
            return run_deterministic_checks(workspace, screenshots, True, True, True)

    def _screens(self, desktop_metrics, mobile_metrics, mobile_png=390):
        with tempfile.TemporaryDirectory() as tmpdir:
            desktop = Path(tmpdir) / "desktop.png"
            mobile = Path(tmpdir) / "mobile.png"
            desktop.write_bytes(_png_bytes(1440, 900))
            mobile.write_bytes(_png_bytes(mobile_png, 844))
            return ScreenshotSet(desktop, mobile, desktop_metrics, mobile_metrics)

    def test_clean_mobile_has_no_overflow_finding(self):
        findings = self._findings(self._screens(_metrics(1440, 900), _metrics(390, 844)))
        self.assertNotIn("mobile horizontal overflow: 390px document width exceeds 390px viewport", findings.failures)
        self.assertFalse(any("horizontal overflow" in item for item in findings.failures))

    def test_ordinary_1440_desktop_behavior_is_unchanged(self):
        findings = self._findings(self._screens(_metrics(1440, 900), _metrics(390, 844)))
        self.assertEqual(findings.failures, [])

    def test_root_and_body_411_emit_one_exact_mobile_finding(self):
        metrics = _metrics(390, 844, document_width=411, body_width=411)
        findings = self._findings(self._screens(_metrics(1440, 900), metrics, mobile_png=411))
        expected = "mobile horizontal overflow: 411px document width exceeds 390px viewport"
        self.assertEqual(findings.failures.count(expected), 1)

    def test_root_only_uses_largest_observed_width(self):
        metrics = _metrics(390, 844, document_width=423, body_width=390)
        findings = self._findings(self._screens(_metrics(1440, 900), metrics, mobile_png=423))
        self.assertIn("mobile horizontal overflow: 423px document width exceeds 390px viewport", findings.failures)

    def test_body_only_uses_largest_observed_width(self):
        metrics = _metrics(390, 844, document_width=390, body_width=435)
        findings = self._findings(self._screens(_metrics(1440, 900), metrics, mobile_png=435))
        self.assertIn("mobile horizontal overflow: 435px document width exceeds 390px viewport", findings.failures)

    def test_desktop_uses_analogous_wording(self):
        metrics = _metrics(1440, 900, document_width=1440, body_width=1480)
        findings = self._findings(self._screens(metrics, _metrics(390, 844)))
        self.assertIn("desktop horizontal overflow: 1480px document width exceeds 1440px viewport", findings.failures)

    def test_combined_viewports_emit_one_finding_each(self):
        findings = self._findings(self._screens(
            _metrics(1440, 900, document_width=1500),
            _metrics(390, 844, body_width=411),
            mobile_png=411,
        ))
        overflow = [item for item in findings.failures if "horizontal overflow" in item]
        self.assertEqual(overflow, [
            "desktop horizontal overflow: 1500px document width exceeds 1440px viewport",
            "mobile horizontal overflow: 411px document width exceeds 390px viewport",
        ])


class TestEvidenceIntegrityBlocksBeforeVision(_FixtureBase):
    """Untrusted live metrics fail before VISION and consume no repair budget."""

    def _run_with_metrics(self, mutate):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()

        def _capture(url, qa_dir, attempt):
            screenshots = self._passing_screenshots(attempt)
            mutate(screenshots, attempt)
            return screenshots

        self.mock_capture.capture.side_effect = _capture
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()
        self.mock_adapter.frontend_build.return_value = {
            "success": True, "design_dna": self.design_dna,
        }
        return self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

    def test_requested_390_actual_411_is_infrastructure_failure(self):
        def mutate(screenshots, attempt):
            screenshots.mobile_metrics = BrowserMetrics(411, 844, 390, 411, 411)

        result = self._run_with_metrics(mutate)
        self.assertFalse(result.success)
        self.assertIn("INFRASTRUCTURE_ERROR:capture_failed", result.error)
        self.assertIn("mobile innerWidth is 411", result.error)
        self.mock_adapter.vision_inspect.assert_not_called()
        self.mock_adapter.frontend_build.assert_not_called()
        self.assertEqual(result.repair_attempts, 0)

    def test_missing_metrics_fail_before_vision_without_repair(self):
        result = self._run_with_metrics(lambda screenshots, attempt: setattr(screenshots, "mobile_metrics", None))
        self.assertFalse(result.success)
        self.assertIn("live browser metrics are missing", result.error)
        self.mock_adapter.vision_inspect.assert_not_called()
        self.mock_adapter.frontend_build.assert_not_called()
        self.assertEqual(result.repair_attempts, 0)

    def test_metric_probe_failure_is_infrastructure_without_repair(self):
        from app.qa.screenshot import ScreenshotCapture

        _queue_and_run_project(self.store, "proj")
        self._passing_render()

        def _probe_failed(url, width, height, out_path):
            return CaptureResult.failure("metric_probe_failed", "data.result missing")

        self.orchestrator.screenshot_capture = ScreenshotCapture(capture_fn=_probe_failed)
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()
        self.mock_adapter.frontend_build.return_value = {
            "success": True, "design_dna": self.design_dna,
        }
        result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertIn("metric_probe_failed: data.result missing", result.error)
        self.mock_adapter.vision_inspect.assert_not_called()
        self.mock_adapter.frontend_build.assert_not_called()
        self.assertEqual(result.repair_attempts, 0)

    def test_bad_metrics_after_repair_stop_without_another_repair(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()

        def capture(url, qa_dir, attempt):
            screenshots = self._passing_screenshots(attempt)
            if attempt:
                screenshots.mobile_metrics = None
            return screenshots

        self.mock_capture.capture.side_effect = capture
        self.mock_adapter.vision_inspect.return_value = self._blocking_vision()
        self.mock_adapter.frontend_build.return_value = {
            "success": True, "design_dna": self.design_dna,
        }
        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        self.assertEqual(result.repair_attempts, 1)
        self.mock_adapter.frontend_build.assert_called_once()
        self.mock_adapter.vision_inspect.assert_called_once()
        self.assertIn("screenshot evidence integrity", result.error)
        self.assertEqual(self.mock_renderer.stop.call_count, 2)


class TestP8OverflowUsesBoundedFrontendRepair(_FixtureBase):
    """P8: a valid 390px viewport with 411px overflow reaches VISION as
    full-page evidence, then FRONTEND receives the deterministic blocker and
    one successful repair is sufficient."""

    def test_overflow_repairs_once_then_passes(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        captured_paths = []

        def _capture(url, qa_dir, attempt):
            screenshots = self._passing_screenshots(attempt)
            if attempt == 0:
                screenshots.mobile.write_bytes(_png_bytes(411, 2000))
                screenshots.mobile_metrics = _metrics(
                    390, 844, document_width=411, body_width=411
                )
            captured_paths.append(screenshots.mobile)
            return screenshots

        self.mock_capture.capture.side_effect = _capture
        self.mock_adapter.vision_inspect.return_value = self._passing_vision()
        self.mock_adapter.frontend_build.return_value = {
            "success": True, "design_dna": self.design_dna,
        }

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertTrue(result.success, result.error)
        self.assertEqual(result.repair_attempts, 1)
        first_mobile = self.mock_adapter.vision_inspect.call_args_list[0].args[1]
        self.assertEqual(_png_dimensions(first_mobile)[0], 411)
        self.assertEqual(first_mobile, captured_paths[0])
        instructions = self.mock_adapter.frontend_build.call_args.kwargs["design_dna_instructions"]
        self.assertIn(
            "mobile horizontal overflow: 411px document width exceeds 390px viewport",
            instructions,
        )
        self.assertEqual(self.mock_adapter.vision_inspect.call_count, 2)
        self.mock_adapter.frontend_build.assert_called_once()


class TestBrokenBuildAfterRepairBlocks(_FixtureBase):
    """7. Broken build after repair blocks PREVIEW_READY."""

    def test_broken_build_after_repair(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.return_value = self._blocking_vision()
        self.mock_adapter.frontend_build.return_value = {"success": True, "design_dna": self.design_dna}

        # Rebuild checks always fail after repair.
        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(False, True, True)):
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

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
            result = self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertFalse(result.success)
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)


class TestObservationsOnlyFindingsDoNotTriggerRepair(_FixtureBase):
    """9. Non-blocking VISION observations do NOT trigger repair."""

    def test_observations_only_passes_immediately(self):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        self.mock_adapter.vision_inspect.return_value = self._observations_only_vision()

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

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
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

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
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
            vision=VisionFindings(pass_=False, blocking_findings=["Hero missing"], summary="bad"),
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
        fake = _FakeBrowserRun(metrics=_metrics(1440, 900))

        env = {}
        if env_value is not None:
            env["AGENT_BROWSER_ARGS"] = env_value

        with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(screenshot_mod.subprocess, "run", side_effect=fake), \
             patch.dict("os.environ", env, clear=False):
            # Ensure unset case truly removes the var even if the host has it.
            if env_value is None:
                import os
                os.environ.pop("AGENT_BROWSER_ARGS", None)
            result = screenshot_mod._default_capture_fn(
                "https://example.com", 1440, 900, out_path
            )

        return result, fake.calls

    def test_env_unset_emits_no_args_flag(self):
        """A. env unset -> no --args emitted anywhere in the flow."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls = self._run_capture(None, tmpdir)
        self.assertTrue(ok.captured)
        self.assertEqual(len(calls), 5)  # open, set viewport, eval, screenshot, close
        for argv in calls:
            self.assertNotIn("--args", argv)

    def test_env_empty_emits_no_args_flag(self):
        """A2. env set but empty/whitespace -> treated as unset."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls = self._run_capture("   ", tmpdir)
        self.assertTrue(ok.captured)
        for argv in calls:
            self.assertNotIn("--args", argv)

    def test_env_single_arg_passed_through(self):
        """B. AGENT_BROWSER_ARGS='--no-sandbox' -> open argv contains --args '--no-sandbox'."""
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls = self._run_capture("--no-sandbox", tmpdir)
        self.assertTrue(ok.captured)
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
        self.assertTrue(ok.captured)
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

        self.assertTrue(ok_noenv.captured)
        self.assertTrue(ok_env.captured)
        self.assertEqual(len(calls_noenv), 5)
        self.assertEqual(len(calls_env), 5)

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
    """Regression: VPS mobile-capture hangs become structured capture failures
    and never propagate as uncaught exceptions from the browser seam."""

    def test_open_timeout_returns_structured_failure_not_raises(self):
        import app.qa.screenshot as screenshot_mod

        fake = _FakeBrowserRun(metrics=_metrics(1440, 900), fail_on=("open",))
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=fake):
                result = screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )
        self.assertFalse(result.captured)
        self.assertEqual(result.status, "capture_failed")

    def test_screenshot_timeout_returns_structured_failure_not_raises(self):
        import app.qa.screenshot as screenshot_mod

        fake = _FakeBrowserRun(metrics=_metrics(1440, 900), fail_on=("screenshot",))
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=fake):
                result = screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )
        self.assertFalse(result.captured)
        self.assertEqual(result.status, "capture_failed")

    def test_close_timeout_does_not_mask_successful_result(self):
        """A hung cleanup `close` call must not raise past the function or
        override an otherwise-successful capture result."""
        import app.qa.screenshot as screenshot_mod

        fake = _FakeBrowserRun(metrics=_metrics(1440, 900), fail_on=("close",))
        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=fake):
                result = screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )
        self.assertTrue(result.captured)
        self.assertEqual(result.metrics, _metrics(1440, 900))

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

        fake = _FakeBrowserRun(metrics=_metrics(1440, 900), returncodes={"open": 1})

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "shot.png"
            with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
                 patch.object(screenshot_mod.subprocess, "run", side_effect=fake):
                ok = screenshot_mod._default_capture_fn(
                    "https://example.com", 1440, 900, out_path
                )

        self.assertFalse(ok.captured)
        self.assertFalse(out_path.exists())
        # screenshot command must never have run.
        self.assertFalse(any("screenshot" in c for c in fake.calls))
        # close must still run for cleanup.
        self.assertTrue(any("close" in c for c in fake.calls))


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
        fake = _FakeBrowserRun(
            metrics=_metrics(*viewport),
            returncodes={"viewport": viewport_returncode},
        )

        with patch.object(screenshot_mod.shutil, "which", return_value="/usr/bin/agent-browser"), \
             patch.object(screenshot_mod.subprocess, "run", side_effect=fake):
            ok = screenshot_mod._default_capture_fn(
                "https://example.com", *viewport, out_path
            )

        return ok, fake.calls, out_path

    def test_commands_run_in_required_order_on_one_session(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls, _ = self._run_capture(tmpdir)

        self.assertTrue(ok.captured)
        self.assertEqual(len(calls), 5)
        command_names = [
            "open" if "open" in call else
            "viewport" if "viewport" in call else
            "eval" if "eval" in call else
            "screenshot" if "screenshot" in call else
            "close" if "close" in call else "unknown"
            for call in calls
        ]
        self.assertEqual(command_names, ["open", "viewport", "eval", "screenshot", "close"])
        sessions = {call[call.index("--session") + 1] for call in calls}
        self.assertEqual(len(sessions), 1)
        screenshot_argv = next(call for call in calls if "screenshot" in call)
        self.assertEqual(
            screenshot_argv[screenshot_argv.index("screenshot") + 1:],
            ["--full", str(Path(tmpdir) / "shot.png")],
        )

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

    def test_mobile_capture_uses_mobile_viewport(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls, _ = self._run_capture(tmpdir, viewport=(390, 844))

        self.assertTrue(ok.captured)
        viewport_argv = next(c for c in calls if "viewport" in c)
        w_idx = viewport_argv.index("viewport")
        self.assertEqual(viewport_argv[w_idx + 1 : w_idx + 3], ["390", "844"])
        self.assertEqual(ok.metrics, _metrics(390, 844))

    def test_open_no_longer_passes_width_height_flags(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls, _ = self._run_capture(tmpdir)

        self.assertTrue(ok.captured)
        open_argv = next(c for c in calls if "open" in c)
        self.assertNotIn("--width", open_argv)
        self.assertNotIn("--height", open_argv)

    def test_failed_set_viewport_short_circuits_before_screenshot(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            ok, calls, out_path = self._run_capture(tmpdir, viewport_returncode=1)

        self.assertFalse(ok.captured)
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
            "<!doctype html><html><head><title>qa</title>"
            "<style>body{margin:0}@media(max-width:500px){body{width:411px}}</style>"
            "</head><body>"
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


class TestRealViewportDimensions(unittest.TestCase):
    """E2E: real agent-browser live metrics prove a 390px viewport even when
    the 411px document makes the accepted full-page PNG wider than the viewport.
    """

    def test_390_viewport_and_411_overflow_are_classified_exactly(self):
        from app.qa.screenshot import (
            DESKTOP_VIEWPORT,
            MOBILE_VIEWPORT,
            ScreenshotCapture,
            validate_screenshot_dimensions,
        )

        server = _ResponsivePageServer()
        try:
            with tempfile.TemporaryDirectory() as tmpdir:
                qa_dir = Path(tmpdir) / "qa"
                screenshots = ScreenshotCapture().capture(
                    f"http://127.0.0.1:{server.port}/", qa_dir, attempt=1
                )
                self.assertTrue(screenshots.complete, "both screenshots must be captured")
                validate_screenshot_dimensions(screenshots)
                desktop_w, _ = _png_dimensions(screenshots.desktop)
                mobile_w, _ = _png_dimensions(screenshots.mobile)
                workspace = _make_workspace(Path(tmpdir), "live")
                from app.qa.deterministic import run_deterministic_checks
                findings = run_deterministic_checks(
                    workspace, screenshots, render_ok=True, build_ok=True, typecheck_ok=True
                )
                metrics = screenshots.mobile_metrics
        finally:
            server.shutdown()

        self.assertEqual(screenshots.desktop_metrics.inner_width, DESKTOP_VIEWPORT[0])
        self.assertEqual(screenshots.desktop_metrics.inner_height, DESKTOP_VIEWPORT[1])
        self.assertEqual(metrics.inner_width, MOBILE_VIEWPORT[0])
        self.assertEqual(metrics.inner_height, MOBILE_VIEWPORT[1])
        self.assertEqual(metrics.document_client_width, MOBILE_VIEWPORT[0])
        self.assertEqual(metrics.document_scroll_width, 411)
        self.assertEqual(metrics.body_scroll_width, 411)
        self.assertEqual(desktop_w, DESKTOP_VIEWPORT[0])
        self.assertEqual(mobile_w, 411)
        self.assertEqual(
            findings.failures.count(
                "mobile horizontal overflow: 411px document width exceeds 390px viewport"
            ),
            1,
        )


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
                return ScreenshotSet(
                    desktop=desktop,
                    mobile=mobile,
                    desktop_metrics=_metrics(1440, 900),
                    mobile_metrics=_metrics(390, 844),
                )

            handle = RenderHandle(project_id="proj", port=5100, process=MagicMock(), url="http://127.0.0.1:5100/")
            mock_renderer.start.return_value = handle
            mock_capture.capture.side_effect = lambda url, qa_dir, attempt: _passing_screenshots(attempt)
            mock_adapter.vision_inspect.return_value = {
                "pass": False, "blocking": ["bad"], "observations": [], "summary": "x",
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
                side_effect=[(False, True, True), (True, True, True)],
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

        findings = VisionFindings(pass_=False, blocking_findings=[], observations=[], raw_error="provider timeout")
        self.assertTrue(findings.blocking)

    def test_vision_no_error_no_findings_not_blocking(self):
        from app.qa.findings import VisionFindings

        findings = VisionFindings(pass_=True, blocking_findings=[], observations=[])
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

        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
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

        with patch.object(orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
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

        with patch.object(orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
            result = orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 1)
        verify.assert_called_once()


# ---------------------------------------------------------------------------
# p5 regression (tg-6329821361-p5): VISION blocking vs. observation contract
# ---------------------------------------------------------------------------


class TestVisionBlockingVsObservationContract(_FixtureBase):
    """p5 regression: only concrete visible defects or explicit requirement
    violations may block QA or consume a repair attempt. Subjective VISION
    feedback is a non-blocking observation: it can never fail QA, never
    consume the repair budget, and never become a repair requirement.
    """

    def _run_with_vision(self, vision_responses):
        _queue_and_run_project(self.store, "proj")
        self._passing_render()
        self.mock_capture.capture.side_effect = (
            lambda url, qa_dir, attempt: self._passing_screenshots(attempt)
        )
        self.mock_adapter.vision_inspect.side_effect = vision_responses
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": self.design_dna,
        }
        with patch.object(self.orchestrator, "_run_rebuild_checks", return_value=(True, True, True)):
            return self.orchestrator.run("proj", self.workspace, self.brief, self.design_dna)

    def test_p5_case1_preferred_hex_color_is_non_blocking(self):
        """VISION: "Hero CTA would look better using #C2410C."
        -> NON-BLOCKING, PASS, no repair consumed."""
        result = self._run_with_vision([
            {"pass": True, "blocking": [],
             "observations": ["Hero CTA would look better using #C2410C."],
             "summary": "Healthy."},
        ])

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)
        # Recorded as observation, not acted on.
        self.assertEqual(
            state.deployment["qa"]["vision"]["observations"],
            ["Hero CTA would look better using #C2410C."],
        )

    def test_p5_case2_second_cta_is_non_blocking_without_explicit_prohibition(self):
        """VISION: "The page has a second CTA not explicitly described in
        Design DNA." -> NON-BLOCKING (absence of a spec is not a prohibition),
        PASS, no repair consumed."""
        result = self._run_with_vision([
            {"pass": True, "blocking": [],
             "observations": ["The page has a second CTA not explicitly described in Design DNA."],
             "summary": "Fine."},
        ])

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        self.assertEqual(self.store.load("proj").lifecycle, ProjectLifecycle.PREVIEW_READY.value)

    def test_p5_case3_aria_disabled_cannot_be_a_screenshot_blocker(self):
        """VISION: "CTA should use aria-disabled." -> implementation detail
        unobservable from screenshots; must not become a screenshot-based
        blocking requirement. As an observation: PASS, no repair consumed."""
        result = self._run_with_vision([
            {"pass": True, "blocking": [],
             "observations": ["CTA should use aria-disabled to look inert."],
             "summary": "Placeholder CTA looks active."},
        ])

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        self.assertEqual(self.store.load("proj").lifecycle, ProjectLifecycle.PREVIEW_READY.value)

    def test_p5_case4_placeholder_leakage_blocks_and_repairs(self):
        """VISION sees internal placeholder/debug text visibly exposed to the
        user -> BLOCKING, repair consumed, then pass."""
        result = self._run_with_vision([
            {"pass": False, "blocking": ["Internal placeholder text is visibly exposed to the user."],
             "observations": [], "summary": "Leak."},
            self._passing_vision(),
        ])

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 1)
        self.mock_adapter.frontend_build.assert_called_once()
        self.assertEqual(self.store.load("proj").lifecycle, ProjectLifecycle.PREVIEW_READY.value)

    def test_p5_case5_mobile_overlap_blocks_and_repairs(self):
        """VISION sees mobile content visibly overlapping / important content
        clipped -> BLOCKING, repair consumed."""
        result = self._run_with_vision([
            {"pass": False,
             "blocking": ["Mobile content visibly overlaps the hero; important content is clipped."],
             "observations": [], "summary": "Broken layout."},
            self._passing_vision(),
        ])

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 1)
        self.mock_adapter.frontend_build.assert_called_once()

    def test_p5_case6_observations_only_pass_consumes_zero_repairs(self):
        """blocking=[] + several subjective UI suggestions -> PASS, zero
        repair attempts consumed."""
        result = self._run_with_vision([
            {"pass": True, "blocking": [],
             "observations": [
                 "Feature icons could have more visual variety.",
                 "Spacing in the steps section could be slightly tighter.",
                 "The hero could feel more polished.",
             ],
             "summary": "Healthy, with polish suggestions."},
        ])

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 0)
        self.mock_adapter.frontend_build.assert_not_called()
        self.assertEqual(len(result.attempts), 1)

    def test_p5_case7_real_blocker_still_uses_bounded_repair(self):
        """A real blocking visual defect still drives the existing bounded
        repair loop; the budget remains exactly 2."""
        self.assertEqual(MAX_REPAIR_ATTEMPTS, 2)
        still_broken = {"pass": False,
                        "blocking": ["Mobile navigation visibly covers the hero content."],
                        "observations": [], "summary": "Still broken."}
        result = self._run_with_vision([still_broken, still_broken.copy(), still_broken.copy()])

        self.assertFalse(result.success)
        self.assertEqual(result.repair_attempts, MAX_REPAIR_ATTEMPTS)
        self.assertEqual(self.mock_adapter.frontend_build.call_count, MAX_REPAIR_ATTEMPTS)
        self.assertEqual(self.store.load("proj").lifecycle, ProjectLifecycle.FAILED.value)

    def test_p5_case8_observations_after_repair_still_pass(self):
        """After repair removes all blockers, remaining observations -> PASS."""
        result = self._run_with_vision([
            {"pass": False, "blocking": ["Important content is clipped on mobile."],
             "observations": ["Spacing could be tighter."], "summary": "Clipped."},
            {"pass": True, "blocking": [],
             "observations": ["These icons could be more varied.", "Hierarchy could be stronger."],
             "summary": "Fixed; polish suggestions remain."},
        ])

        self.assertTrue(result.success)
        self.assertEqual(result.repair_attempts, 1)
        self.mock_adapter.frontend_build.assert_called_once()
        state = self.store.load("proj")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)
        self.assertEqual(state.deployment["qa"]["vision"]["blocking"], [])
        self.assertEqual(
            len(state.deployment["qa"]["vision"]["observations"]), 2
        )


class TestVisionFindingsBlockingSemantics(unittest.TestCase):
    """Application-owned semantics on VisionFindings / QAAttempt: only
    blocking findings (or a VISION runtime failure) block; observations
    never do."""

    def test_observations_are_never_blocking(self):
        findings = VisionFindings(
            pass_=True,
            observations=["Spacing could be tighter", "Another CTA may be unnecessary"],
        )
        self.assertFalse(findings.blocking)
        attempt = QAAttempt(0, DeterministicFindings(), findings)
        self.assertFalse(attempt.repair_required)
        self.assertTrue(attempt.final_pass)

    def test_blocking_findings_block(self):
        findings = VisionFindings(
            pass_=False, blocking_findings=["Mobile content overlaps hero"]
        )
        self.assertTrue(findings.blocking)
        attempt = QAAttempt(0, DeterministicFindings(), findings)
        self.assertTrue(attempt.repair_required)
        self.assertFalse(attempt.final_pass)

    def test_raw_error_still_fails_closed(self):
        findings = VisionFindings(pass_=False, raw_error="provider timeout")
        self.assertTrue(findings.blocking)

    def test_to_dict_serializes_blocking_and_observations(self):
        findings = VisionFindings(
            pass_=False,
            blocking_findings=["b"],
            observations=["o"],
            summary="s",
        )
        d = findings.to_dict()
        self.assertEqual(d["blocking"], ["b"])
        self.assertEqual(d["observations"], ["o"])
        self.assertFalse(d["pass"])
        # Retired severity keys must not leak into the persisted contract.
        self.assertNotIn("critical", d)
        self.assertNotIn("major", d)
        self.assertNotIn("minor", d)


class TestRepairInstructionsUseBlockingOnly(_FixtureBase):
    """Repair instructions must carry BLOCKING findings only. Subjective
    observations must never reach FRONTEND as mandatory requirements."""

    def test_blocking_findings_are_included(self):
        failed = QAAttempt(
            attempt=0,
            deterministic=DeterministicFindings(failures=["npm run build failed"]),
            vision=VisionFindings(
                pass_=False,
                blocking_findings=["Mobile navigation visibly covers the hero content."],
                observations=[],
                summary="s",
            ),
        )
        instructions = self.orchestrator._build_repair_instructions(self.design_dna, failed)

        self.assertIn("VISION blocking findings", instructions)
        self.assertIn("Mobile navigation visibly covers the hero content.", instructions)

    def test_observations_are_not_sent_to_frontend(self):
        failed = QAAttempt(
            attempt=0,
            deterministic=DeterministicFindings(failures=[]),
            vision=VisionFindings(
                pass_=False,
                blocking_findings=["Mobile navigation visibly covers the hero content."],
                observations=["Spacing in the steps section could be tighter."],
                summary="s",
            ),
        )
        instructions = self.orchestrator._build_repair_instructions(self.design_dna, failed)

        self.assertIn("Mobile navigation visibly covers the hero content.", instructions)
        self.assertNotIn("Spacing in the steps section could be tighter.", instructions)
        self.assertIn("smallest targeted fix", instructions)
        self.assertIn("Do NOT invent business facts", instructions)
        self.assertIn("Preserve the existing Design DNA", instructions)


class TestVisionResponseContract(unittest.TestCase):
    """VISION JSON contract at the adapter seam: application-owned
    blocking/observation split, pass derived by the application, and a
    prompt that constrains VISION to visible-acceptance reviewing."""

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
        self.brief = {"name": "N", "what": "W", "why": "Y"}

    def tearDown(self):
        self.tmpdir_obj.cleanup()

    def test_parser_maps_blocking_and_observations(self):
        parsed = self.adapter._parse_vision_response(json.dumps({
            "pass": False,
            "blocking": ["CTA text is clipped and unreadable."],
            "observations": ["Spacing could be tighter."],
            "summary": "s",
        }))

        self.assertEqual(parsed["blocking"], ["CTA text is clipped and unreadable."])
        self.assertEqual(parsed["observations"], ["Spacing could be tighter."])
        self.assertFalse(parsed["pass"])

    def test_parser_derives_pass_from_blocking_not_model_prose(self):
        """The application owns pass semantics. VISION's own ``pass`` claim
        cannot make a page with blocking findings pass, nor fail a page VISION
        only had subjective observations about."""
        parsed = self.adapter._parse_vision_response(json.dumps({
            "pass": True, "blocking": ["Overlapping content on mobile."], "observations": [], "summary": "",
        }))
        self.assertFalse(parsed["pass"])

        parsed = self.adapter._parse_vision_response(json.dumps({
            "pass": False, "blocking": [], "observations": ["Hero CTA would look better using #C2410C."], "summary": "",
        }))
        self.assertTrue(parsed["pass"])

    def test_parser_legacy_shape_maps_without_dropping_findings(self):
        """If a model ignores the new schema and answers in the retired
        critical/major/minor shape, its findings must not be silently dropped
        (critical+major map to blocking; minor maps to observations)."""
        parsed = self.adapter._parse_vision_response(json.dumps({
            "pass": False, "critical": ["c"], "major": ["m"], "minor": ["n"], "summary": "s",
        }))

        self.assertEqual(parsed["blocking"], ["c", "m"])
        self.assertEqual(parsed["observations"], ["n"])
        self.assertFalse(parsed["pass"])

    def test_parser_failure_fails_closed(self):
        parsed = self.adapter._parse_vision_response("this is not json")

        self.assertFalse(parsed["pass"])
        self.assertIn("error", parsed)
        self.assertEqual(parsed["blocking"], [])
        self.assertEqual(parsed["observations"], [])

    def test_prompt_declares_blocking_observations_contract(self):
        prompt = self.adapter._build_vision_prompt(self.brief, {"version": 1})

        self.assertIn('"blocking"', prompt)
        self.assertIn('"observations"', prompt)
        self.assertIn("if and only if", prompt)
        # Retired contract must not be reintroduced in the schema section.
        self.assertNotIn('"critical"', prompt)
        self.assertNotIn('"major"', prompt)
        self.assertNotIn('"minor"', prompt)

    def test_prompt_constrains_vision_to_visible_acceptance_review(self):
        prompt = self.adapter._build_vision_prompt(self.brief, None)

        # VISION is a narrow acceptance reviewer, not a second designer.
        self.assertIn("visual acceptance reviewer", prompt)
        self.assertIn("NOT a\nsecond designer", prompt)
        self.assertIn("requirements author", prompt)
        # Absence of a specification is not a prohibition.
        self.assertIn("Absence of a specification is NOT a\nprohibition", prompt)
        # Implementation-detail findings are explicitly forbidden.
        self.assertIn("aria-disabled", prompt)
        self.assertIn("DOM attribute", prompt)
        self.assertIn("CSS class names", prompt)
        # Uncertainty resolves to observation, not failure.
        self.assertIn("classify it as an\nobservation", prompt)
        # Existing guard language is preserved.
        self.assertIn("attached to this message", prompt)
        self.assertNotIn("on disk", prompt)



if __name__ == "__main__":
    unittest.main()
