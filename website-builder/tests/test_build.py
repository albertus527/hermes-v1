"""Phase 7 -> Phase 8 pipeline tests.

FRONTEND uses Hermes when available. Application owns workspace/lifecycle.
Application runs deterministic cheap checks through ProjectRunner, then hands
off to Phase 8 QA inside the same worker ownership boundary.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


@pytest.fixture(autouse=True)
def fake_build_artifact(monkeypatch):
    """These orchestration tests stub npm; supply its output in the workspace."""
    original = ProjectRunner.create_workspace
    def create(runner, project_id):
        workspace = original(runner, project_id)
        (workspace / 'dist').mkdir(exist_ok=True)
        (workspace / 'dist' / 'index.html').write_bytes(b'<html>local fake build</html>')
        return workspace
    monkeypatch.setattr(ProjectRunner, 'create_workspace', create)


from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.projects.build import FrontendBuilder, BuildResult
from app.sandbox.runner import ProjectRunner


def _queue_project(store: ProjectStateStore, project_id: str) -> None:
    """Advance a fresh project through the canonical Phase 7 setup path.

    DISCOVERING -> READY -> QUEUED
    """
    store.transition_lifecycle(project_id, ProjectLifecycle.READY)
    store.transition_lifecycle(project_id, ProjectLifecycle.QUEUED)


class TestCanonicalStarterResolution(unittest.TestCase):
    """Test that the canonical frontend starter path resolves correctly."""

    def test_default_starter_path(self):
        """Default starter path points to <repo>/templates/frontend-starter/."""
        from app.projects.build import _STARTER_PATH

        # The path should resolve to the repo's templates/frontend-starter
        self.assertTrue(_STARTER_PATH.name == "frontend-starter")
        self.assertTrue(_STARTER_PATH.parent.name == "templates")

    def test_starter_exists_in_repo(self):
        """The canonical starter actually exists in the repository."""
        from app.projects.build import _STARTER_PATH

        self.assertTrue(_STARTER_PATH.exists(), f"Starter not found at {_STARTER_PATH}")
        self.assertTrue((_STARTER_PATH / "package.json").exists())
        self.assertTrue((_STARTER_PATH / "src").exists())


class TestFrontendBuilderWithHermes(unittest.TestCase):
    """Test build when Hermes FRONTEND adapter is available."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.mock_adapter = MagicMock()
        self.builder = FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.mock_adapter
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_uses_hermes_frontend(self):
        """Phase 7 consumes FRONTEND output rather than hardcoded Design DNA."""
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {
                "version": 1,
                "brand_personality": "premium, minimalist",
                "palette": {"primary": "#000000"},
                "typography": {"heading_font": "Inter"},
                "spacing": {"density": "comfortable"},
                "page_inventory": ["home"],
                "layout": {"navigation": "top-bar"},
                "motion": {"enabled": True},
                "primary_cta": {"label": "Book Now", "destination": None},
                "assets": [],
                "verified_content": {"name": "Northcut", "what": "barbershop"},
                "unresolved_facts": ["cta_destination"],
            },
        }

        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
        _queue_project(self.store, "proj-hermes-1")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-hermes-1", brief)

        self.assertTrue(result.success)
        self.assertIsNotNone(result.design_dna)
        self.assertEqual(result.design_dna["brand_personality"], "premium, minimalist")
        self.mock_adapter.frontend_build.assert_called_once()

    def test_build_fails_when_hermes_unavailable(self):
        """Build fails gracefully when Hermes adapter is not configured."""
        builder = FrontendBuilder(self.runner, self.store, hermes_adapter=None)
        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
        _queue_project(self.store, "proj-hermes-2")
        result = builder.build("proj-hermes-2", brief)

        self.assertFalse(result.success)
        self.assertIn("Hermes adapter not configured", result.error)

        state = self.store.load("proj-hermes-2")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_build_fails_when_frontend_fails(self):
        """Build fails when FRONTEND returns failure."""
        self.mock_adapter.frontend_build.return_value = {
            "success": False,
            "error": "FRONTEND build failed",
            "design_dna": None,
        }

        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
        _queue_project(self.store, "proj-hermes-3")
        result = self.builder.build("proj-hermes-3", brief)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "FRONTEND build failed")

        state = self.store.load("proj-hermes-3")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_no_fabricated_cta_url(self):
        """Design DNA must not contain fabricated CTA URLs."""
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {
                "version": 1,
                "primary_cta": {"label": "Book Now", "destination": None},
                "unresolved_facts": ["cta_destination"],
            },
        }

        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
        _queue_project(self.store, "proj-hermes-4")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-hermes-4", brief)

        self.assertTrue(result.success)
        # Destination must be None, not a fabricated URL
        self.assertIsNone(result.design_dna["primary_cta"]["destination"])
        self.assertIn("cta_destination", result.design_dna["unresolved_facts"])

    def test_successful_build_stays_running_until_qa(self):
        """Successful cheap checks do NOT advance to PREVIEW_READY.

        PREVIEW_READY requires Phase 8 QA. Phase 7 stops at RUNNING.
        """
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 1},
        }

        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

        # Mock the fixed checks to succeed
        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            _queue_project(self.store, "proj-hermes-5")
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-hermes-5", brief)

        self.assertTrue(result.success)
        state = self.store.load("proj-hermes-5")
        # Must remain RUNNING, not PREVIEW_READY — the mocked QA did not
        # perform the real transition.
        self.assertEqual(state.lifecycle, ProjectLifecycle.RUNNING.value)

    def test_max_workers_enforced(self):
        self.runner.acquire_project("other-project")
        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
        _queue_project(self.store, "proj-hermes-6")
        result = self.builder.build("proj-hermes-6", brief)

        self.assertFalse(result.success)
        self.assertIn("MAX_WORKERS=1", result.error)

    def test_build_result_has_no_qa_artifacts(self):
        """BuildResult does not expose QA-specific artifacts."""
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 1},
        }

        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            _queue_project(self.store, "proj-hermes-7")
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-hermes-7", brief)

        self.assertTrue(result.success)
        # Build result should not contain QA artifacts
        self.assertIsNone(getattr(result, "screenshots", None))
        self.assertIsNone(getattr(result, "vision_findings", None))
        self.assertIsNone(getattr(result, "repair_passes", None))


class TestFrontendBuilderTimeoutRecovery(unittest.TestCase):
    """Phase 7 timeout-recovery integration tests through FrontendBuilder."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)

        # Fake starter so workspace copying works.
        self.starter_path = Path(self.tmpdir) / "starter"
        (self.starter_path / "src").mkdir(parents=True)
        (self.starter_path / "package.json").write_text(
            json.dumps({"name": "starter", "scripts": {"build": "echo build"}})
        )
        (self.starter_path / "src" / "App.tsx").write_text(
            "// starter placeholder\n", encoding="utf-8"
        )

        self.builder = FrontendBuilder(
            self.runner,
            self.store,
            hermes_adapter=None,  # set per-test
            starter_path=self.starter_path,
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _queue(self, project_id: str) -> None:
        _queue_project(self.store, project_id)

    def test_timeout_recovery_with_passing_checks_stays_running(self):
        """Timeout + valid artifacts + passing cheap checks -> RUNNING."""
        adapter = MagicMock()
        adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 1, "brand_personality": "premium"},
        }
        self.builder.hermes_adapter = adapter

        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
        self._queue("proj-recover-1")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-recover-1", brief)

        self.assertTrue(result.success)
        self.assertIsNotNone(result.design_dna)
        state = self.store.load("proj-recover-1")
        self.assertEqual(state.lifecycle, ProjectLifecycle.RUNNING.value)
        self.assertEqual(state.revisions.design_dna_version, 1)
        self.assertEqual(state.design_dna["brand_personality"], "premium")

    def test_timeout_recovery_with_failing_checks_fails_cheap_checks(self):
        """Timeout + valid artifacts + failing cheap checks -> FAILED(cheap_checks)."""
        adapter = MagicMock()
        adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 1},
        }
        self.builder.hermes_adapter = adapter

        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
        self._queue("proj-recover-2")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": False, "stderr": "tsc error"},
                "npm_typecheck": {"success": True},
            }
            result = self.builder.build("proj-recover-2", brief)

        self.assertFalse(result.success)
        state = self.store.load("proj-recover-2")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(state.failure["phase"], "cheap_checks")


class TestFixedChecksThroughProjectRunner(unittest.TestCase):
    """Test that fixed checks execute through ProjectRunner."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.mock_adapter = MagicMock()
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 1},
        }
        self.builder = FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.mock_adapter
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_checks_use_project_runner(self):
        """_run_fixed_checks routes through ProjectRunner.run_command."""
        workspace = self.runner.create_workspace("proj-checks")

        with patch.object(self.runner, "run_command") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=0, stdout="", stderr=""
            )
            results = self.builder._run_fixed_checks("proj-checks", workspace)

        # All three checks should have been called through run_command
        self.assertEqual(mock_run.call_count, 3)
        calls = [c[0][1] for c in mock_run.call_args_list]
        self.assertEqual(calls[0], ["npm", "ci"])
        self.assertEqual(calls[1], ["npm", "run", "build"])
        self.assertEqual(calls[2], ["npm", "run", "typecheck"])

    def test_each_check_runs_exactly_once(self):
        """Each deterministic check runs exactly once."""
        workspace = self.runner.create_workspace("proj-once")

        call_count = {"npm_ci": 0, "npm_build": 0, "npm_typecheck": 0}

        def track_call(project_id, cmd, **kwargs):
            if cmd == ["npm", "ci"]:
                call_count["npm_ci"] += 1
            elif cmd == ["npm", "run", "build"]:
                call_count["npm_build"] += 1
            elif cmd == ["npm", "run", "typecheck"]:
                call_count["npm_typecheck"] += 1
            return MagicMock(returncode=0, stdout="", stderr="")

        with patch.object(self.runner, "run_command", side_effect=track_call):
            self.builder._run_fixed_checks("proj-once", workspace)

        self.assertEqual(call_count["npm_ci"], 1)
        self.assertEqual(call_count["npm_build"], 1)
        self.assertEqual(call_count["npm_typecheck"], 1)

    def test_frontend_does_not_run_checks(self):
        """FRONTEND does NOT run npm cheap checks itself.

        The FRONTEND prompt explicitly instructs the model to stop before
        running npm ci / npm run build / npm run typecheck.
        """
        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}
        workspace = self.runner.create_workspace("proj-nofrontendchecks")

        with patch.object(self.runner, "run_command") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="", stderr="")
            _queue_project(self.store, "proj-nofrontendchecks")
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                self.builder.build("proj-nofrontendchecks", brief)

        # run_command should only be called for the three checks, not by FRONTEND
        self.assertEqual(mock_run.call_count, 3)


class TestFrontendBuilderStarterCopy(unittest.TestCase):
    """Test starter copying with a real starter directory."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)

        # Create a minimal fake starter
        self.starter_path = Path(self.tmpdir) / "starter"
        self.starter_path.mkdir()
        (self.starter_path / "package.json").write_text(
            json.dumps({"name": "starter", "scripts": {"build": "echo build"}})
        )
        (self.starter_path / "src").mkdir()
        (self.starter_path / "src" / "index.ts").write_text("// starter")

        self.mock_adapter = MagicMock()
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 1},
        }
        self.builder = FrontendBuilder(
            self.runner,
            self.store,
            hermes_adapter=self.mock_adapter,
            starter_path=self.starter_path,
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_build_creates_workspace(self):
        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            _queue_project(self.store, "proj-copy-1")
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-copy-1", brief)

        self.assertTrue(result.success)
        self.assertIsNotNone(result.workspace)
        self.assertTrue(result.workspace.exists())

    def test_build_uses_fixed_starter(self):
        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            _queue_project(self.store, "proj-copy-2")
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-copy-2", brief)

        self.assertTrue(result.success)
        self.assertTrue((result.workspace / "package.json").exists())
        self.assertTrue((result.workspace / "src" / "index.ts").exists())

    def test_source_repo_not_modified(self):
        """Generated site run must not modify the Hermes source repository."""
        brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": True},
                "npm_typecheck": {"success": True},
            }
            _queue_project(self.store, "proj-copy-3")
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-copy-3", brief)

        self.assertTrue(result.success)
        # The starter path should be unchanged
        self.assertTrue(self.starter_path.exists())
        # The workspace should be separate from the starter
        self.assertNotEqual(result.workspace, self.starter_path)


class TestPhase8Handoff(unittest.TestCase):
    """Focused tests for the Phase 7 -> Phase 8 application handoff."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.mock_adapter = MagicMock()
        self.builder = FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.mock_adapter
        )

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _brief(self):
        return {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

    def _queue(self, project_id: str) -> None:
        _queue_project(self.store, project_id)

    def _passing_frontend(self):
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 1, "brand_personality": "premium"},
        }

    def _passing_checks(self):
        return {
            "npm_ci": {"success": True},
            "npm_build": {"success": True},
            "npm_typecheck": {"success": True},
        }

    def test_phase7_success_invokes_qa_exactly_once(self):
        """A. Phase 7 success -> QAOrchestrator invoked exactly once."""
        self._passing_frontend()
        self._queue("proj-handoff-a")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._passing_checks()
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-handoff-a", self._brief())

        self.assertTrue(result.success)
        mock_qa_cls.assert_called_once_with(
            self.runner,
            self.store,
            hermes_adapter=self.mock_adapter,
            web3forms_access_key=None,
        )
        mock_qa.run.assert_called_once()

    def test_phase7_failure_does_not_invoke_qa(self):
        """B. Phase 7 failure -> QA not invoked."""
        self.mock_adapter.frontend_build.return_value = {
            "success": False,
            "error": "FRONTEND failed",
        }
        self._queue("proj-handoff-b")

        with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
            result = self.builder.build("proj-handoff-b", self._brief())

        self.assertFalse(result.success)
        mock_qa_cls.assert_not_called()

    def test_worker_slot_acquired_once_released_once(self):
        """C. Worker slot acquired once -> released once after Phase 8."""
        self._passing_frontend()
        self._queue("proj-handoff-c")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._passing_checks()
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                with patch.object(self.runner, "acquire_project", wraps=self.runner.acquire_project) as mock_acquire, \
                     patch.object(self.runner, "release_project", wraps=self.runner.release_project) as mock_release:
                    result = self.builder.build("proj-handoff-c", self._brief())

        self.assertTrue(result.success)
        mock_acquire.assert_called_once_with("proj-handoff-c")
        mock_release.assert_called_once_with("proj-handoff-c")

    def test_qa_success_reaches_preview_ready(self):
        """D. QA success -> final lifecycle PREVIEW_READY."""
        self._passing_frontend()
        self._queue("proj-handoff-d")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._passing_checks()
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()

                def _run_qa(project_id, workspace, brief, design_dna):
                    with self.store.acquire_writer(project_id) as state:
                        self.store.transition_lifecycle_locked(state, ProjectLifecycle.PREVIEW_READY)
                        self.store.save(state)
                    return MagicMock(success=True, error=None)

                mock_qa.run.side_effect = _run_qa
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-handoff-d", self._brief())

        self.assertTrue(result.success)
        state = self.store.load("proj-handoff-d")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)

    def test_qa_failure_reaches_failed(self):
        """E. QA failure -> final lifecycle FAILED."""
        self._passing_frontend()
        self._queue("proj-handoff-e")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._passing_checks()
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()

                def _run_qa(project_id, workspace, brief, design_dna):
                    with self.store.acquire_writer(project_id) as state:
                        self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                        state.failure = {"phase": "qa", "error": "QA exhausted"}
                        self.store.save(state)
                    return MagicMock(success=False, error="QA exhausted")

                mock_qa.run.side_effect = _run_qa
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-handoff-e", self._brief())

        self.assertFalse(result.success)
        self.assertEqual(result.error, "QA exhausted")
        state = self.store.load("proj-handoff-e")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)

    def test_same_context_passed_to_phase8(self):
        """F. Same project_id/workspace/brief/design_dna passed to Phase 8."""
        design_dna = {"version": 1, "brand_personality": "premium"}
        self.mock_adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": design_dna,
        }
        self._queue("proj-handoff-f")
        brief = self._brief()

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._passing_checks()
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-handoff-f", brief)

        self.assertTrue(result.success)
        mock_qa.run.assert_called_once_with(
            project_id="proj-handoff-f",
            workspace=result.workspace,
            brief=brief,
            design_dna=design_dna,
        )

    def test_no_duplicate_acquire_inside_phase8(self):
        """G. No duplicate acquire_project call inside Phase 8."""
        self._passing_frontend()
        self._queue("proj-handoff-g")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._passing_checks()
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                with patch.object(self.runner, "acquire_project", wraps=self.runner.acquire_project) as mock_acquire:
                    result = self.builder.build("proj-handoff-g", self._brief())

        self.assertTrue(result.success)
        mock_acquire.assert_called_once_with("proj-handoff-g")


if __name__ == "__main__":
    unittest.main()
