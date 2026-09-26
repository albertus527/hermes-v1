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


from app.core.contracts import OperationResult
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.projects.build import FrontendBuilder, BuildResult
from app.qa.orchestrator import QAResult
from app.sandbox.runner import ProjectRunner


def _valid_dna() -> dict:
    """A minimal Design DNA that passes the real composition validation."""
    return {
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
    }


def _passing_checks() -> dict:
    """The cheap-check result shape FrontendBuilder expects when all pass."""
    return {
        "npm_ci": {"success": True},
        "npm_build": {"success": True},
        "npm_typecheck": {"success": True},
    }


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
        # A stable, mapped application code, not free text: the dispatcher
        # forwards `error` to the user, and an unmapped string rendered as the
        # generic "Something went wrong" with no hint that this is transient.
        self.assertEqual(result.error, "WORKER_BUSY")
        self.assertEqual(result.error_code, "WORKER_BUSY")
        self.assertTrue(result.retryable)

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
            toolchain_verify=unittest.mock.ANY,
        )
        # MEDIUM-5: the toolchain verifier closes over this build's hashes.
        verify_fn = mock_qa_cls.call_args.kwargs["toolchain_verify"]
        self.assertTrue(callable(verify_fn))
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

    def test_preview_failure_preserves_sanitized_diagnostics_and_remote_boundary(self):
        self._passing_frontend()
        self._queue("proj-preview-failure")
        boundary = MagicMock()
        preview = MagicMock()
        diagnostics = {
            "operation_id": "op-1",
            "deployment_id": "dpl-1",
            "failure_classification": "artifact_defect",
            "failure_records": [],
        }

        def run_preview(project_id, workspace, *, slot_held=True,
                        on_remote_boundary=None):
            on_remote_boundary()
            return OperationResult(
                success=False, error="SMOKE_FAILED", error_code="SMOKE_FAILED",
                data=dict(diagnostics),
            )

        preview.run_owned.side_effect = run_preview
        builder = FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.mock_adapter,
            preview_orchestrator=preview,
        )
        with patch.object(builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._passing_checks()
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()

                def run_qa(project_id, workspace, brief, design_dna):
                    with self.store.acquire_writer(project_id) as state:
                        self.store.transition_lifecycle_locked(
                            state, ProjectLifecycle.PREVIEW_READY
                        )
                        self.store.save(state)
                    return MagicMock(success=True, error=None)

                mock_qa.run.side_effect = run_qa
                mock_qa_cls.return_value = mock_qa
                result = builder.build(
                    "proj-preview-failure", self._brief(),
                    on_remote_boundary=boundary,
                )

        self.assertFalse(result.success)
        self.assertTrue(result.reached_remote)
        self.assertEqual(result.diagnostics["operation_id"], "op-1")
        self.assertEqual(result.diagnostics["failure_classification"], "artifact_defect")
        boundary.assert_called_once()

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


# ---------------------------------------------------------------------------
# MEDIUM-3: build failure observability / stable error propagation
# ---------------------------------------------------------------------------


class TestBuildErrorPropagation(unittest.TestCase):
    """Cheap-check failures return stable non-null application errors;
    unexpected exceptions are logged operator-side while the returned error
    stays sanitized."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.starter_path = Path(self.tmpdir) / "starter"
        (self.starter_path / "src").mkdir(parents=True)
        (self.starter_path / "package.json").write_text(
            json.dumps({"name": "starter", "scripts": {"build": "echo build"}})
        )
        (self.starter_path / "src" / "App.tsx").write_text(
            "// starter placeholder\n", encoding="utf-8"
        )
        self.adapter = MagicMock()
        self.builder = FrontendBuilder(
            self.runner,
            self.store,
            hermes_adapter=self.adapter,
            starter_path=self.starter_path,
        )
        self.brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def test_cheap_check_failure_returns_stable_error_code(self):
        self.adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 1, "brand_personality": "premium"},
        }
        _queue_project(self.store, "proj-cheap")
        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = {
                "npm_ci": {"success": True},
                "npm_build": {"success": False},
                "npm_typecheck": {"success": True},
            }
            result = self.builder.build("proj-cheap", self.brief)

        self.assertFalse(result.success)
        self.assertIsNotNone(result.error)
        self.assertTrue(result.error.startswith("CHEAP_CHECKS_FAILED:"))
        self.assertIn("npm_build", result.error)
        state = self.store.load("proj-cheap")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(state.failure["phase"], "cheap_checks")

    def test_unexpected_exception_returns_sanitized_error(self):
        self.adapter.frontend_build.side_effect = RuntimeError("kaboom-internal-detail")
        _queue_project(self.store, "proj-boom")
        result = self.builder.build("proj-boom", self.brief)

        self.assertFalse(result.success)
        # User-facing error is a stable, sanitized code…
        self.assertEqual(result.error, "UNEXPECTED_BUILD_ERROR")
        # …while the full detail is preserved operator-side in state.
        state = self.store.load("proj-boom")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertIn("kaboom-internal-detail", state.failure["error"])

    def test_exception_after_a_delivered_preview_does_not_mark_the_project_failed(self):
        """A preview the user has ALREADY been sent must not be buried.

        PREVIEW_READY -> FAILED is a legal edge, so an exception raised after
        the preview was deployed, smoke-tested and delivered used to silently
        contradict state the user can see, and follow-up work was refused.
        """
        _queue_project(self.store, "proj-delivered")

        def _pid(args, kwargs):
            """The project id, whether passed positionally or by keyword."""
            return args[0] if args else kwargs["project_id"]

        def _qa_succeeds(*args, **kwargs):
            # Model the REAL QA outcome: a passing run transitions the project
            # to PREVIEW_READY (that is what _finalize_success does), which is
            # the lifecycle the outer handler observes in the post-preview
            # window.
            pid = _pid(args, kwargs)
            with self.store.acquire_writer(pid) as state:
                self.store.transition_lifecycle_locked(
                    state, ProjectLifecycle.PREVIEW_READY)
                state.revisions.qa_revision = state.revisions.source_revision
                self.store.save(state)
            return QAResult(project_id=pid, success=True, attempts=[],
                            repair_attempts=0)

        def _delivered_then_raises(*args, **kwargs):
            pid = _pid(args, kwargs)
            # The preview was already delivered for THIS revision...
            with self.store.acquire_writer(pid) as state:
                state.deployment["latest_shown_preview"] = {
                    "operation_id": "op-1",
                    "source_revision": state.revisions.source_revision,
                    "preview_url": "https://x.vercel.app",
                    "photo_attempted": True, "photo_outcome": "SENT",
                    "text_attempted": True, "text_outcome": "SENT",
                }
                self.store.save(state)
            # ...and only then does something go wrong.
            raise RuntimeError("raised after delivery")

        self.adapter.frontend_build.return_value = {
            "success": True, "design_dna": _valid_dna()}
        # Use the same builder shape as the passing-build tests in this file so
        # the run reaches Phase 9 instead of failing earlier in Phase 7.
        builder = FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.adapter)
        builder.preview_orchestrator = MagicMock()
        builder.preview_orchestrator.run_owned.side_effect = _delivered_then_raises

        with patch.object(builder, "_run_fixed_checks",
                          return_value=_passing_checks()), \
             patch("app.projects.build.QAOrchestrator") as qa_cls:
            qa_cls.return_value.run.side_effect = _qa_succeeds
            result = builder.build("proj-delivered", self.brief)

        self.assertFalse(result.success)
        state = self.store.load("proj-delivered")
        self.assertEqual(state.lifecycle, ProjectLifecycle.PREVIEW_READY.value)
        self.assertIsNotNone(state.failure)
        self.assertEqual(state.failure["phase"], "post_preview")
        # The delivered preview is untouched and still the latest one.
        self.assertEqual(state.deployment["latest_shown_preview"]["preview_url"],
                         "https://x.vercel.app")

    def test_exception_with_a_stale_preview_still_marks_failed(self):
        """A shown preview for a DIFFERENT revision is not a delivered preview
        for this build, so the project must still fail closed."""
        _queue_project(self.store, "proj-stale-preview")

        def _pid(args, kwargs):
            return args[0] if args else kwargs["project_id"]

        def _qa_succeeds(*args, **kwargs):
            pid = _pid(args, kwargs)
            with self.store.acquire_writer(pid) as state:
                self.store.transition_lifecycle_locked(
                    state, ProjectLifecycle.PREVIEW_READY)
                self.store.save(state)
            return QAResult(project_id=pid, success=True, attempts=[],
                            repair_attempts=0)

        def _delivered_then_raises(*args, **kwargs):
            pid = _pid(args, kwargs)
            with self.store.acquire_writer(pid) as state:
                # Shown for an EARLIER revision than the one being built.
                state.deployment["latest_shown_preview"] = {
                    "operation_id": "op-old",
                    "source_revision": state.revisions.source_revision - 1,
                    "preview_url": "https://old.vercel.app",
                }
                self.store.save(state)
            raise RuntimeError("boom")

        self.adapter.frontend_build.return_value = {
            "success": True, "design_dna": _valid_dna()}
        builder = FrontendBuilder(
            self.runner, self.store, hermes_adapter=self.adapter)
        builder.preview_orchestrator = MagicMock()
        builder.preview_orchestrator.run_owned.side_effect = _delivered_then_raises

        with patch.object(builder, "_run_fixed_checks",
                          return_value=_passing_checks()), \
             patch("app.projects.build.QAOrchestrator") as qa_cls:
            qa_cls.return_value.run.side_effect = _qa_succeeds
            result = builder.build("proj-stale-preview", self.brief)

        self.assertFalse(result.success)
        state = self.store.load("proj-stale-preview")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(state.failure["phase"], "build")

    def test_exception_before_any_preview_still_marks_failed(self):
        """Regression: with no delivered preview, the handler must still fail
        the project closed."""
        _queue_project(self.store, "proj-nopreview")
        self.adapter.frontend_build.side_effect = RuntimeError("boom")

        result = self.builder.build("proj-nopreview", self.brief)

        self.assertFalse(result.success)
        state = self.store.load("proj-nopreview")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(state.failure["phase"], "build")


# ---------------------------------------------------------------------------
# MEDIUM-5: protected toolchain files verified after initial FRONTEND
# ---------------------------------------------------------------------------


class TestToolchainProtectionInitialBuild(unittest.TestCase):
    """Mutation/removal of a protected starter/toolchain identity file during
    initial FRONTEND generation deterministically fails with
    TOOLCHAIN_MUTATION_REJECTED."""

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)
        self.starter_path = Path(self.tmpdir) / "starter"
        (self.starter_path / "src").mkdir(parents=True)
        (self.starter_path / ".nvmrc").write_text("26.5.0\n", encoding="utf-8")
        (self.starter_path / "package.json").write_text(
            json.dumps({"name": "starter", "scripts": {"build": "echo build"}})
        )
        (self.starter_path / "src" / "App.tsx").write_text(
            "// starter placeholder\n", encoding="utf-8"
        )
        self.adapter = MagicMock()
        self.builder = FrontendBuilder(
            self.runner,
            self.store,
            hermes_adapter=self.adapter,
            starter_path=self.starter_path,
        )
        self.brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    def _dna(self):
        return {"version": 1, "brand_personality": "premium"}

    def test_toolchain_modification_fails_build(self):
        def mutate(**kwargs):
            (kwargs["workspace"] / "package.json").write_text(
                json.dumps({"name": "evil"}), encoding="utf-8"
            )
            return {"success": True, "design_dna": self._dna()}

        self.adapter.frontend_build.side_effect = mutate
        _queue_project(self.store, "proj-mutate")
        result = self.builder.build("proj-mutate", self.brief)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "TOOLCHAIN_MUTATION_REJECTED")
        state = self.store.load("proj-mutate")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertIn("TOOLCHAIN_MUTATION_REJECTED", state.failure["error"])
        self.assertIn("package.json", state.failure["error"])

    def test_toolchain_removal_fails_build(self):
        def remove(**kwargs):
            (kwargs["workspace"] / ".nvmrc").unlink()
            return {"success": True, "design_dna": self._dna()}

        self.adapter.frontend_build.side_effect = remove
        _queue_project(self.store, "proj-remove")
        result = self.builder.build("proj-remove", self.brief)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "TOOLCHAIN_MUTATION_REJECTED")
        state = self.store.load("proj-remove")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertIn("missing:.nvmrc", state.failure["error"])

    def test_toolchain_mutation_rejected_even_when_frontend_fails(self):
        """A failed FRONTEND that still mutated protected files fails with the
        deterministic policy code, never a generic FRONTEND error."""

        def mutate_and_fail(**kwargs):
            (kwargs["workspace"] / "package.json").write_text("{}", encoding="utf-8")
            return {"success": False, "error": "FRONTEND build failed"}

        self.adapter.frontend_build.side_effect = mutate_and_fail
        _queue_project(self.store, "proj-mutate-fail")
        result = self.builder.build("proj-mutate-fail", self.brief)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "TOOLCHAIN_MUTATION_REJECTED")
        state = self.store.load("proj-mutate-fail")
        self.assertIn("TOOLCHAIN_MUTATION_REJECTED", state.failure["error"])


# ---------------------------------------------------------------------------
# Phase-7 bounded compile repair after initial FRONTEND generation
# ---------------------------------------------------------------------------


class TestCheapCheckClassification(unittest.TestCase):
    """Eligibility rules for the single Phase-7 compile-repair attempt.

    Only deterministic source/build failures may enter the repair path;
    infrastructure/runtime failures and unprovable failures must not.
    """

    def test_ts_diagnostic_is_eligible_source_error(self):
        from app.projects.build import classify_cheap_check_failure

        decision = classify_cheap_check_failure({
            "npm_ci": {"success": True},
            "npm_build": {
                "success": False,
                "stderr": "src/components/BookingCta.tsx(1,1): error TS6133: "
                          "'Icon' is declared but its value is never read.",
            },
            "npm_typecheck": {"success": True},
        })
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.failed_check, "npm_build")
        self.assertEqual(decision.classification, "source_error")

    def test_network_failure_is_not_eligible(self):
        from app.projects.build import classify_cheap_check_failure

        decision = classify_cheap_check_failure({
            "npm_ci": {"success": True},
            "npm_build": {
                "success": False,
                "stderr": "npm ERR! code EAI_AGAIN\nnetwork request to "
                          "registry.npmjs.org failed",
            },
            "npm_typecheck": {"success": True},
        })
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.classification, "infrastructure_failure")

    def test_infrastructure_signature_wins_over_source_signature(self):
        from app.projects.build import classify_cheap_check_failure

        decision = classify_cheap_check_failure({
            "npm_ci": {"success": True},
            "npm_build": {
                "success": False,
                "stderr": "error TS6133: unused\nENOSPC: no space left on device",
            },
            "npm_typecheck": {"success": True},
        })
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.classification, "infrastructure_failure")

    def test_npm_ci_failure_is_never_eligible(self):
        from app.projects.build import classify_cheap_check_failure

        decision = classify_cheap_check_failure({
            "npm_ci": {"success": False, "stderr": "npm ERR! code EBADENGINE"},
        })
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.failed_check, "npm_ci")

    def test_empty_output_is_not_eligible(self):
        from app.projects.build import classify_cheap_check_failure

        decision = classify_cheap_check_failure({
            "npm_ci": {"success": True},
            "npm_build": {"success": False},
            "npm_typecheck": {"success": True},
        })
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.classification, "unclassified")

    def test_no_failure_is_not_eligible(self):
        from app.projects.build import classify_cheap_check_failure

        decision = classify_cheap_check_failure({
            "npm_ci": {"success": True},
            "npm_build": {"success": True},
            "npm_typecheck": {"success": True},
        })
        self.assertFalse(decision.eligible)
        self.assertIsNone(decision.failed_check)
        self.assertEqual(decision.classification, "no_failure")

    def test_tailwind_theme_diagnostic_is_eligible_source_error(self):
        """The exact real p5 Tailwind @theme failure is repairable."""
        from app.projects.build import classify_cheap_check_failure

        decision = classify_cheap_check_failure({
            "npm_ci": {"success": True},
            "npm_build": {
                "success": False,
                "stderr": "[plugin @tailwindcss/vite:generate:build] "
                          "/workspace/src/index.css\n"
                          "Error: `@theme` blocks must only contain custom "
                          "properties or `@keyframes`.",
            },
            "npm_typecheck": {"success": True},
        })
        self.assertTrue(decision.eligible)
        self.assertEqual(decision.failed_check, "npm_build")
        self.assertEqual(decision.classification, "source_error")

    def test_generic_error_prefix_remains_unclassified(self):
        """A generic 'Error:' line is NOT a repairable source signature."""
        from app.projects.build import classify_cheap_check_failure

        decision = classify_cheap_check_failure({
            "npm_ci": {"success": True},
            "npm_build": {
                "success": False,
                "stderr": "Error: something unexpected happened in a plugin",
            },
            "npm_typecheck": {"success": True},
        })
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.classification, "unclassified")

    def test_infrastructure_still_wins_over_tailwind_source_text(self):
        """Infrastructure signatures win even when source-looking text is present."""
        from app.projects.build import classify_cheap_check_failure

        decision = classify_cheap_check_failure({
            "npm_ci": {"success": True},
            "npm_build": {
                "success": False,
                "stderr": "Error: `@theme` blocks must only contain custom "
                          "properties or `@keyframes`.\n"
                          "ENOSPC: no space left on device",
            },
            "npm_typecheck": {"success": True},
        })
        self.assertFalse(decision.eligible)
        self.assertEqual(decision.classification, "infrastructure_failure")


class TestBoundedDiagnosticCapture(unittest.TestCase):
    """Bounded head+tail capture for cheap-check stdout/stderr.

    Real p5 failure: the Tailwind @theme diagnostic sat at the BEGINNING of
    a long stderr whose >2000-char stack trace pushed it out of the old
    tail-only capture, so the failure classified as 'unclassified' and the
    eligible compile repair was skipped.
    """

    _TAILWIND_DIAGNOSTIC = (
        "[plugin @tailwindcss/vite:generate:build] /workspace/src/index.css\n"
        "Error: `@theme` blocks must only contain custom properties or "
        "`@keyframes`."
    )

    def test_bounded_output_short_text_unchanged(self):
        from app.projects.build import _bounded_output

        self.assertEqual(_bounded_output("short"), "short")
        self.assertEqual(_bounded_output(""), "")
        self.assertEqual(_bounded_output(None), "")

    def test_bounded_output_preserves_head_and_tail(self):
        from app.projects.build import _bounded_output

        head = "HEAD-DIAGNOSTIC " + "h" * 100
        tail = "t" * 100 + " TAIL-END"
        middle = "m" * 5000
        captured = _bounded_output(head + middle + tail)

        self.assertIn("HEAD-DIAGNOSTIC", captured)
        self.assertIn("TAIL-END", captured)
        self.assertIn("...[truncated]...", captured)
        # Bounded: well under the full input length.
        self.assertLess(len(captured), 2200)

    def test_run_fixed_checks_preserves_leading_diagnostic(self):
        """A long stderr with the diagnostic at the START and a >2000-char
        stack trace at the END still preserves the diagnostic."""
        from app.projects.build import classify_cheap_check_failure

        tmpdir = tempfile.mkdtemp()
        try:
            workspace_root = Path(tmpdir) / "workspaces"
            state_root = Path(tmpdir) / "state"
            store = ProjectStateStore(state_root)
            runner = ProjectRunner(workspace_root, store)
            builder = FrontendBuilder(runner, store, hermes_adapter=None)
            workspace = runner.create_workspace("proj-bounded")

            stack_trace = "\n" + "\n".join(
                f"    at chunk{i} (node_modules/vite/dist/node/chunks/dep-{i}.js:{i}:13)"
                for i in range(300)
            )
            long_stderr = self._TAILWIND_DIAGNOSTIC + stack_trace
            self.assertGreater(len(long_stderr), 2000)

            def fake_run(project_id, cmd, **kwargs):
                if cmd == ["npm", "ci"]:
                    return MagicMock(returncode=0, stdout="", stderr="")
                if cmd == ["npm", "run", "build"]:
                    return MagicMock(returncode=1, stdout="", stderr=long_stderr)
                return MagicMock(returncode=0, stdout="", stderr="")

            with patch.object(runner, "run_command", side_effect=fake_run):
                results = builder._run_fixed_checks("proj-bounded", workspace)

            captured = results["npm_build"]["stderr"]
            self.assertIn("`@theme` blocks must only contain", captured)
            self.assertLess(len(captured), 2200)

            decision = classify_cheap_check_failure(results)
            self.assertEqual(decision.classification, "source_error")
            self.assertTrue(decision.eligible)
            self.assertEqual(decision.failed_check, "npm_build")
        finally:
            import shutil

            shutil.rmtree(tmpdir, ignore_errors=True)


class TestPhase7CompileRepair(unittest.TestCase):
    """A–H: the single bounded Phase-7 compile-repair path.

    Real E2E class: npm_build fails with TS6133 (unused import) and the
    existing recovery regenerated the whole site (~7–9 min). One targeted
    repair on the existing workspace must fix it instead.
    """

    def setUp(self):
        self.tmpdir = tempfile.mkdtemp()
        self.workspace_root = Path(self.tmpdir) / "workspaces"
        self.state_root = Path(self.tmpdir) / "state"
        self.store = ProjectStateStore(self.state_root)
        self.runner = ProjectRunner(self.workspace_root, self.store)

        # Fake starter with the protected toolchain identity files.
        self.starter_path = Path(self.tmpdir) / "starter"
        (self.starter_path / "src" / "components").mkdir(parents=True)
        (self.starter_path / ".nvmrc").write_text("26.5.0\n", encoding="utf-8")
        (self.starter_path / "package.json").write_text(
            json.dumps({"name": "starter", "scripts": {"build": "echo build"}}),
            encoding="utf-8",
        )
        (self.starter_path / "src" / "App.tsx").write_text(
            "// starter placeholder\n", encoding="utf-8"
        )

        self.adapter = MagicMock()
        self.builder = FrontendBuilder(
            self.runner,
            self.store,
            hermes_adapter=self.adapter,
            starter_path=self.starter_path,
        )
        self.brief = {"name": "Northcut", "what": "barbershop", "why": "booking WA"}

    def tearDown(self):
        import shutil

        shutil.rmtree(self.tmpdir, ignore_errors=True)

    # -- fixtures / helpers -------------------------------------------------

    def _all_passing(self):
        return {
            "npm_ci": {"success": True, "stdout": "", "stderr": ""},
            "npm_build": {"success": True, "stdout": "", "stderr": ""},
            "npm_typecheck": {"success": True, "stdout": "", "stderr": ""},
        }

    def _ts6133_npm_build_failure(self):
        return {
            "npm_ci": {"success": True, "stdout": "", "stderr": ""},
            "npm_build": {
                "success": False,
                "stdout": "",
                "stderr": (
                    "src/components/BookingCta.tsx(1,1): error TS6133: "
                    "'Icon' is declared but its value is never read."
                ),
            },
            "npm_typecheck": {"success": True, "stdout": "", "stderr": ""},
        }

    def _passing_frontend(self):
        self.adapter.frontend_build.return_value = {
            "success": True,
            "design_dna": {"version": 1, "brand_personality": "premium"},
        }

    def _run_build_with_checks(self, project_id, checks_effect):
        """Run build() with a scripted `_run_fixed_checks` sequence.

        Returns (result, checks_mock, qa_mock_or_None).
        """
        self._passing_frontend()
        _queue_project(self.store, project_id)
        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.side_effect = checks_effect
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build(project_id, self.brief)
                return result, mock_checks, mock_qa

    # -- A: exact real failure class ---------------------------------------

    def test_a_ts6133_repairs_once_then_passes_toward_qa(self):
        result, mock_checks, mock_qa = self._run_build_with_checks(
            "proj-repair-a",
            [self._ts6133_npm_build_failure(), self._all_passing()],
        )

        self.assertTrue(result.success)
        # The ENTIRE fixed cheap-check sequence is rerun (not trusted).
        self.assertEqual(mock_checks.call_count, 2)
        # Exactly one initial generation + exactly ONE targeted repair.
        self.assertEqual(self.adapter.frontend_build.call_count, 2)

        repair_instructions = self.adapter.frontend_build.call_args_list[1].kwargs[
            "design_dna_instructions"
        ]
        self.assertIn("COMPILE REPAIR", repair_instructions)
        self.assertIn("npm_build", repair_instructions)
        self.assertIn("error TS6133", repair_instructions)
        self.assertIn("MINIMUM", repair_instructions)
        self.assertIn("Do NOT rewrite, redesign, regenerate", repair_instructions)

        # Proceeds toward QA (Phase 8 owns PREVIEW_READY, so it stays RUNNING
        # under the mocked QA).
        mock_qa.run.assert_called_once()
        state = self.store.load("proj-repair-a")
        self.assertEqual(state.lifecycle, ProjectLifecycle.RUNNING.value)
        self.assertIsNone(state.failure)

    def test_masked_syntax_failure_preserves_unused_icon_prop_api(self):
        syntax_output = (
            "src/components/Catalog.tsx(29,14): error TS1005: '>' expected."
        )
        unused_prop_output = (
            "src/components/icons.tsx(54,35): error TS6133: "
            "'className' is declared but its value is never read."
        )
        initial = {
            "npm_ci": {"success": True, "stdout": "", "stderr": ""},
            "npm_build": {
                "success": False,
                "stdout": syntax_output,
                "stderr": "",
            },
            "npm_typecheck": {
                "success": False,
                "stdout": syntax_output,
                "stderr": "",
            },
        }
        post_repair = {
            "npm_ci": {"success": True, "stdout": "", "stderr": ""},
            "npm_build": {
                "success": False,
                "stdout": unused_prop_output,
                "stderr": "",
            },
            "npm_typecheck": {
                "success": False,
                "stdout": unused_prop_output,
                "stderr": "",
            },
        }

        result, mock_checks, mock_qa = self._run_build_with_checks(
            "proj-repair-masked-icon-prop",
            [initial, post_repair],
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "CHEAP_CHECKS_FAILED:npm_build")
        self.assertEqual(self.adapter.frontend_build.call_count, 2)
        self.assertEqual(mock_checks.call_count, 2)
        mock_qa.run.assert_not_called()

        repair_instructions = self.adapter.frontend_build.call_args_list[1].kwargs[
            "design_dna_instructions"
        ]
        self.assertIn("error TS1005", repair_instructions)
        self.assertIn(
            "parse/syntax error can mask later TypeScript diagnostics",
            repair_instructions,
        )
        self.assertIn("part of a component's public API", repair_instructions)
        self.assertIn("component's\n  root rendered element", repair_instructions)
        self.assertIn("<svg className={className}>", repair_instructions)
        self.assertIn(
            "inspect sibling icon components in src/components", repair_instructions
        )
        self.assertIn(
            "Do NOT modify or delete protected toolchain files", repair_instructions
        )

        state = self.store.load("proj-repair-masked-icon-prop")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(state.failure["compile_repair_attempts"], 1)
        self.assertEqual(state.failure["final_stage"], "post_repair")
        self.assertIn(
            "TS1005", state.failure["initial_checks"]["npm_build"]["stdout"]
        )
        self.assertIn("TS6133", state.failure["checks"]["npm_typecheck"]["stdout"])

    # -- B: repair runs, build still fails, NO second repair ---------------

    def test_b_repair_still_failing_has_no_second_repair(self):
        result, mock_checks, _mock_qa = self._run_build_with_checks(
            "proj-repair-b",
            [self._ts6133_npm_build_failure(), self._ts6133_npm_build_failure()],
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "CHEAP_CHECKS_FAILED:npm_build")
        # Initial generation + exactly one repair; never a third FRONTEND call.
        self.assertEqual(self.adapter.frontend_build.call_count, 2)
        self.assertEqual(mock_checks.call_count, 2)
        state = self.store.load("proj-repair-b")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertEqual(state.failure["phase"], "cheap_checks")
        self.assertEqual(state.failure["compile_repair_attempts"], 1)

    # -- C: typecheck source error is eligible -----------------------------

    def test_c_typecheck_ts_error_is_repaired(self):
        typecheck_failure = {
            "npm_ci": {"success": True, "stdout": "", "stderr": ""},
            "npm_build": {"success": True, "stdout": "", "stderr": ""},
            "npm_typecheck": {
                "success": False,
                "stdout": "",
                "stderr": "src/App.tsx(12,5): error TS2304: Cannot find name 'ctaUrl'.",
            },
        }
        result, mock_checks, mock_qa = self._run_build_with_checks(
            "proj-repair-c", [typecheck_failure, self._all_passing()]
        )

        self.assertTrue(result.success)
        self.assertEqual(self.adapter.frontend_build.call_count, 2)
        self.assertEqual(mock_checks.call_count, 2)
        repair_instructions = self.adapter.frontend_build.call_args_list[1].kwargs[
            "design_dna_instructions"
        ]
        self.assertIn("npm_typecheck", repair_instructions)
        self.assertIn("error TS2304", repair_instructions)
        mock_qa.run.assert_called_once()

    # -- D: infrastructure / runner failures never repair ------------------

    def test_d_network_failure_never_repairs_or_consumes_attempt(self):
        network_failure = {
            "npm_ci": {"success": True, "stdout": "", "stderr": ""},
            "npm_build": {
                "success": False,
                "stdout": "",
                "stderr": (
                    "npm ERR! code EAI_AGAIN\nnpm ERR! network request to "
                    "registry.npmjs.org failed"
                ),
            },
            "npm_typecheck": {"success": True, "stdout": "", "stderr": ""},
        }
        result, mock_checks, mock_qa = self._run_build_with_checks(
            "proj-repair-d1", [network_failure]
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "CHEAP_CHECKS_FAILED:npm_build")
        # Repair NOT invoked, rerun NOT attempted.
        self.assertEqual(self.adapter.frontend_build.call_count, 1)
        self.assertEqual(mock_checks.call_count, 1)
        mock_qa.run.assert_not_called()
        state = self.store.load("proj-repair-d1")
        self.assertEqual(state.failure["compile_repair_attempts"], 0)
        self.assertEqual(state.failure["final_stage"], "initial")

    def test_d_npm_ci_failure_never_repairs(self):
        ci_failure = {
            "npm_ci": {
                "success": False,
                "stdout": "",
                "stderr": "npm ERR! code EBADENGINE engine unsupported",
            },
        }
        result, mock_checks, _qa = self._run_build_with_checks(
            "proj-repair-d2", [ci_failure]
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "CHEAP_CHECKS_FAILED:npm_ci")
        self.assertEqual(self.adapter.frontend_build.call_count, 1)
        self.assertEqual(mock_checks.call_count, 1)
        state = self.store.load("proj-repair-d2")
        self.assertEqual(state.failure["compile_repair_attempts"], 0)

    def test_d_unclassified_failure_never_repairs(self):
        unclassified = {
            "npm_ci": {"success": True, "stdout": "", "stderr": ""},
            "npm_build": {"success": False, "stdout": "", "stderr": ""},
            "npm_typecheck": {"success": True, "stdout": "", "stderr": ""},
        }
        result, _mock_checks, _qa = self._run_build_with_checks(
            "proj-repair-d3", [unclassified]
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "CHEAP_CHECKS_FAILED:npm_build")
        self.assertEqual(self.adapter.frontend_build.call_count, 1)

    # -- E: protected toolchain mutation during repair ---------------------

    def test_e_toolchain_mutation_during_repair_fails_closed(self):
        frontend_calls = {"count": 0}

        def frontend_dispatch(**kwargs):
            frontend_calls["count"] += 1
            if frontend_calls["count"] == 2:
                # The compile repair mutates a protected toolchain identity file.
                (kwargs["workspace"] / "package.json").write_text(
                    json.dumps({"name": "evil"}), encoding="utf-8"
                )
            return {
                "success": True,
                "design_dna": {"version": 1, "brand_personality": "premium"},
            }

        self.adapter.frontend_build.side_effect = frontend_dispatch
        _queue_project(self.store, "proj-repair-e")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._ts6133_npm_build_failure()
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-repair-e", self.brief)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "TOOLCHAIN_MUTATION_REJECTED")
        self.assertEqual(self.adapter.frontend_build.call_count, 2)
        # Policy rejection happens BEFORE any cheap-check rerun or QA handoff.
        self.assertEqual(mock_checks.call_count, 1)
        mock_qa.run.assert_not_called()
        state = self.store.load("proj-repair-e")
        self.assertEqual(state.lifecycle, ProjectLifecycle.FAILED.value)
        self.assertIn("TOOLCHAIN_MUTATION_REJECTED", state.failure["error"])
        self.assertIn("package.json", state.failure["error"])

    # -- F: successful initial checks never repair -------------------------

    def test_f_passing_initial_checks_never_invoke_repair(self):
        result, mock_checks, mock_qa = self._run_build_with_checks(
            "proj-repair-f", [self._all_passing()]
        )

        self.assertTrue(result.success)
        self.assertEqual(self.adapter.frontend_build.call_count, 1)
        self.assertEqual(mock_checks.call_count, 1)
        mock_qa.run.assert_called_once()

    # -- G: Phase-7 repair budget is independent from Phase-8 QA -----------

    def test_g_compile_repair_budget_independent_from_phase8_qa(self):
        self._passing_frontend()
        _queue_project(self.store, "proj-repair-g")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.side_effect = [
                self._ts6133_npm_build_failure(),
                self._all_passing(),
            ]
            with patch("app.projects.build.QAOrchestrator") as mock_qa_cls:
                mock_qa = MagicMock()
                mock_qa.run.return_value = MagicMock(success=True, error=None)
                mock_qa_cls.return_value = mock_qa
                result = self.builder.build("proj-repair-g", self.brief)

        self.assertTrue(result.success)
        # Phase-7 consumed its one repair...
        self.assertEqual(self.adapter.frontend_build.call_count, 2)
        # ...and Phase-8 QA is then handed off with exactly the standard
        # constructor/handoff surface — no compile-repair budget or state is
        # threaded into it, so its own bounded repair budget is untouched.
        self.assertEqual(
            set(mock_qa_cls.call_args.kwargs.keys()),
            {"hermes_adapter", "web3forms_access_key", "toolchain_verify"},
        )
        mock_qa.run.assert_called_once_with(
            project_id="proj-repair-g",
            workspace=result.workspace,
            brief=self.brief,
            design_dna={"version": 1, "brand_personality": "premium"},
        )

    # -- Repair execution failure fails closed on the initial checks -------

    def test_repair_execution_failure_fails_closed_with_initial_error(self):
        self.adapter.frontend_build.side_effect = [
            {
                "success": True,
                "design_dna": {"version": 1, "brand_personality": "premium"},
            },
            {"success": False, "error": "FRONTEND repair failed"},
        ]
        _queue_project(self.store, "proj-repair-x")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._ts6133_npm_build_failure()
            result = self.builder.build("proj-repair-x", self.brief)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "CHEAP_CHECKS_FAILED:npm_build")
        self.assertEqual(self.adapter.frontend_build.call_count, 2)
        # No rerun after a failed repair invocation; the attempt is consumed.
        self.assertEqual(mock_checks.call_count, 1)
        state = self.store.load("proj-repair-x")
        self.assertEqual(state.failure["compile_repair_attempts"], 1)
        self.assertEqual(state.failure["final_stage"], "repair_execution_failed")
        # The reason the REPAIR failed is persisted, not dropped.
        self.assertEqual(state.failure["repair"]["error"], "FRONTEND repair failed")
        self.assertEqual(state.failure["repair"]["error_code"],
                         "FRONTEND repair failed")
        self.assertNotIn("invocation", state.failure["repair"])

    def test_repair_failure_reason_prefers_the_adapter_supervision_code(self):
        self.adapter.frontend_build.side_effect = [
            {
                "success": True,
                "design_dna": {"version": 1, "brand_personality": "premium"},
            },
            {"success": False, "error": "child exited 1", "error_code":
                "FRONTEND_IDLE_TIMEOUT"},
        ]
        _queue_project(self.store, "proj-repair-code")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._ts6133_npm_build_failure()
            result = self.builder.build("proj-repair-code", self.brief)

        self.assertFalse(result.success)
        # The user-facing contract is unchanged.
        self.assertEqual(result.error, "CHEAP_CHECKS_FAILED:npm_build")
        state = self.store.load("proj-repair-code")
        self.assertEqual(state.failure["repair"]["error_code"],
                         "FRONTEND_IDLE_TIMEOUT")
        self.assertEqual(state.failure["repair"]["error"], "child exited 1")

    def test_repair_returning_invalid_design_dna_is_reported_not_dropped(self):
        self.adapter.frontend_build.side_effect = [
            {
                "success": True,
                "design_dna": {"version": 1, "brand_personality": "premium"},
            },
            {"success": True, "design_dna": {}},
        ]
        _queue_project(self.store, "proj-repair-bad-dna")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._ts6133_npm_build_failure()
            result = self.builder.build("proj-repair-bad-dna", self.brief)

        self.assertFalse(result.success)
        self.assertEqual(result.error, "CHEAP_CHECKS_FAILED:npm_build")
        self.assertEqual(mock_checks.call_count, 1)
        state = self.store.load("proj-repair-bad-dna")
        self.assertEqual(state.failure["final_stage"], "repair_execution_failed")
        self.assertEqual(state.failure["repair"]["error"], "MISSING_DESIGN_DNA")
        self.assertEqual(state.failure["repair"]["error_code"],
                         "MISSING_DESIGN_DNA")
        self.assertNotIn("invocation", state.failure["repair"])

    def test_repair_failure_persists_bounded_invocation_forensics(self):
        receipt = {
            "schema": "frontend_forensics/1",
            "returncode": 1,
            "elapsed_seconds": 12.0,
            "counters": {"model_started": 0, "tool_started": 0},
            "activity": {"last_phase": "startup"},
        }
        # A long child stderr, with a sentinel deep inside the middle: the
        # bounded capture must keep head+tail and drop the middle.
        noise = "x" * 6000
        self.adapter.frontend_build.side_effect = [
            {
                "success": True,
                "design_dna": {"version": 1, "brand_personality": "premium"},
            },
            {"success": False, "error": f"head-marker{noise}tail-marker",
             "error_code": "FRONTEND_CHILD_FAILED", "invocation": receipt},
        ]
        _queue_project(self.store, "proj-repair-receipt")

        with patch.object(self.builder, "_run_fixed_checks") as mock_checks:
            mock_checks.return_value = self._ts6133_npm_build_failure()
            result = self.builder.build("proj-repair-receipt", self.brief)

        self.assertFalse(result.success)
        state = self.store.load("proj-repair-receipt")
        repair = state.failure["repair"]
        self.assertEqual(repair["error_code"], "FRONTEND_CHILD_FAILED")
        self.assertLess(len(repair["error"]), 2100)
        self.assertIn("head-marker", repair["error"])
        self.assertIn("tail-marker", repair["error"])
        # The persisted invocation is the bounded forensics receipt verbatim.
        self.assertEqual(repair["invocation"], receipt)
        # Nothing unbounded leaked into the serialized failure.
        serialized = json.dumps(state.failure)
        self.assertNotIn(noise, serialized)

    # -- H: persisted diagnostics distinguish the two failures -------------

    def test_h_persisted_diagnostics_distinguish_initial_and_post_repair(self):
        initial = self._ts6133_npm_build_failure()
        post_repair = {
            "npm_ci": {"success": True, "stdout": "", "stderr": ""},
            "npm_build": {
                "success": False,
                "stdout": "",
                "stderr": (
                    "src/pages/Home.tsx(5,3): error TS2322: Type 'string' is "
                    "not assignable to type 'number'."
                ),
            },
            "npm_typecheck": {"success": True, "stdout": "", "stderr": ""},
        }
        result, _mock_checks, _qa = self._run_build_with_checks(
            "proj-repair-h", [initial, post_repair]
        )

        self.assertFalse(result.success)
        self.assertEqual(result.error, "CHEAP_CHECKS_FAILED:npm_build")
        state = self.store.load("proj-repair-h")
        failure = state.failure
        self.assertEqual(failure["phase"], "cheap_checks")
        self.assertEqual(failure["compile_repair_attempts"], 1)
        self.assertEqual(failure["final_stage"], "post_repair")
        # Both failures are persisted and independently diagnosable.
        self.assertIn("initial_checks", failure)
        self.assertIn("TS6133", failure["initial_checks"]["npm_build"]["stderr"])
        self.assertIn("TS2322", failure["checks"]["npm_build"]["stderr"])


if __name__ == "__main__":
    unittest.main()
