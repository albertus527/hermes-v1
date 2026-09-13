"""Phase 7 frontend build for Website Builder R1.

Uses the existing Hermes FRONTEND logical role through the oneshot seam.
FRONTEND owns design/build decisions. Application owns workspace/lifecycle.
On success, hands off to Phase 8 QA within the same worker ownership.
"""

from __future__ import annotations

import json
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.qa.orchestrator import QAOrchestrator
from app.sandbox.runner import ProjectRunner, WorkspaceError


# Canonical frontend starter location (relative to repo root)
# Repository layout:
#   <repo>/templates/frontend-starter/
#   <repo>/website-builder/app/projects/build.py
_STARTER_PATH = Path(__file__).parent.parent.parent.parent / "templates" / "frontend-starter"


@dataclass
class BuildResult:
    """Result of the Phase 7 -> Phase 8 build pipeline."""

    success: bool
    project_id: str
    workspace: Optional[Path] = None
    design_dna: Optional[Dict[str, Any]] = None
    build_output: Optional[str] = None
    error: Optional[str] = None
    duration_seconds: float = 0.0


class FrontendBuilder:
    """Builds the first frontend using the fixed starter and Hermes FRONTEND.

    FRONTEND is the existing logical role through Hermes + 9Router.
    This class automates the build boundary — it does not create a new agent.
    """

    def __init__(
        self,
        runner: ProjectRunner,
        store: ProjectStateStore,
        hermes_adapter=None,
        starter_path: Optional[Path] = None,
    ):
        self.runner = runner
        self.store = store
        self.hermes_adapter = hermes_adapter
        self.starter_path = starter_path or _STARTER_PATH

    def _copy_starter(self, workspace: Path) -> None:
        """Copy the fixed frontend starter into the project workspace."""
        if not self.starter_path.exists():
            raise FileNotFoundError(f"Frontend starter not found: {self.starter_path}")

        for item in self.starter_path.iterdir():
            if item.name in ("node_modules", "dist", ".git"):
                continue
            dest = workspace / item.name
            if item.is_dir():
                shutil.copytree(item, dest, dirs_exist_ok=True)
            else:
                shutil.copy2(item, dest)

    def _run_fixed_checks(self, project_id: str, workspace: Path) -> Dict[str, Any]:
        """Run the fixed cheap checks that belong to Phase 7.

        Checks: npm ci, npm run build, npm run typecheck
        Each check runs exactly once through ProjectRunner.
        """
        results: Dict[str, Any] = {}

        # npm ci
        proc = self.runner.run_command(
            project_id,
            ["npm", "ci"],
            cwd=workspace,
            timeout=300,
        )
        results["npm_ci"] = {
            "success": proc.returncode == 0,
            "stdout": proc.stdout[-2000:] if proc.stdout else "",
            "stderr": proc.stderr[-2000:] if proc.stderr else "",
        }
        if proc.returncode != 0:
            return results

        # npm run build
        proc = self.runner.run_command(
            project_id,
            ["npm", "run", "build"],
            cwd=workspace,
            timeout=300,
        )
        results["npm_build"] = {
            "success": proc.returncode == 0,
            "stdout": proc.stdout[-2000:] if proc.stdout else "",
            "stderr": proc.stderr[-2000:] if proc.stderr else "",
        }

        # npm run typecheck
        proc = self.runner.run_command(
            project_id,
            ["npm", "run", "typecheck"],
            cwd=workspace,
            timeout=120,
        )
        results["npm_typecheck"] = {
            "success": proc.returncode == 0,
            "stdout": proc.stdout[-2000:] if proc.stdout else "",
            "stderr": proc.stderr[-2000:] if proc.stderr else "",
        }

        return results

    def build(self, project_id: str, brief: Dict[str, Any]) -> BuildResult:
        """Execute the Phase 7 -> Phase 8 build pipeline for a project.

        Acquires the single worker slot once, runs Phase 7, and on success
        invokes Phase 8 QA before releasing the slot. Phase 8 owns the
        RUNNING -> PREVIEW_READY transition.
        """
        start_time = time.time()

        # Acquire the single worker slot
        if not self.runner.acquire_project(project_id):
            return BuildResult(
                success=False,
                project_id=project_id,
                error="Another project is currently being built (MAX_WORKERS=1)",
            )

        try:
            # Create isolated workspace
            workspace = self.runner.create_workspace(project_id)

            # Copy fixed starter
            self._copy_starter(workspace)

            # Update project state: QUEUED -> RUNNING
            with self.store.acquire_writer(project_id) as state:
                self.store.transition_lifecycle_locked(state, ProjectLifecycle.RUNNING)
                state.revisions.source_revision += 1
                self.store.save(state)

            # Use Hermes FRONTEND to derive design and build
            if self.hermes_adapter is not None:
                frontend_result = self.hermes_adapter.frontend_build(
                    project_id=project_id,
                    brief=brief,
                    workspace=workspace,
                )

                if not frontend_result.get("success"):
                    with self.store.acquire_writer(project_id) as state:
                        self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                        state.failure = {
                            "phase": "frontend_build",
                            "error": frontend_result.get("error", "Unknown FRONTEND error"),
                            "failed_at": time.time(),
                        }
                        self.store.save(state)

                    return BuildResult(
                        success=False,
                        project_id=project_id,
                        workspace=workspace,
                        error=frontend_result.get("error"),
                        duration_seconds=time.time() - start_time,
                    )

                design_dna = frontend_result.get("design_dna")
            else:
                # No Hermes adapter — cannot run FRONTEND. This is a configuration error.
                with self.store.acquire_writer(project_id) as state:
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                    state.failure = {
                        "phase": "frontend_build",
                        "error": "Hermes adapter not configured — FRONTEND role unavailable",
                        "failed_at": time.time(),
                    }
                    self.store.save(state)

                return BuildResult(
                    success=False,
                    project_id=project_id,
                    workspace=workspace,
                    error="Hermes adapter not configured",
                    duration_seconds=time.time() - start_time,
                )

            # Run fixed cheap checks (deterministic, application-owned)
            # FRONTEND does NOT run these. Application code does.
            checks = self._run_fixed_checks(project_id, workspace)
            build_success = all(r.get("success", False) for r in checks.values())

            # Update state with results
            with self.store.acquire_writer(project_id) as state:
                if build_success:
                    # Successful Phase 7 keeps lifecycle RUNNING. Only Phase 8
                    # may advance RUNNING -> PREVIEW_READY.
                    state.revisions.design_dna_version = design_dna.get("version", 1) if design_dna else 0
                    state.design_dna = design_dna or {}
                else:
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                    state.failure = {
                        "phase": "cheap_checks",
                        "checks": checks,
                        "failed_at": time.time(),
                    }
                self.store.save(state)

            if not build_success:
                return BuildResult(
                    success=False,
                    project_id=project_id,
                    workspace=workspace,
                    design_dna=design_dna,
                    build_output=json.dumps(checks, indent=2),
                    duration_seconds=time.time() - start_time,
                )

            # Phase 7 succeeded — hand off to Phase 8 QA inside the same
            # worker ownership boundary. QAOrchestrator does not acquire or
            # release the project slot; that stays here.
            qa_orchestrator = QAOrchestrator(
                self.runner,
                self.store,
                hermes_adapter=self.hermes_adapter,
            )
            qa_result = qa_orchestrator.run(
                project_id=project_id,
                workspace=workspace,
                brief=brief,
                design_dna=design_dna,
            )

            duration = time.time() - start_time

            return BuildResult(
                success=qa_result.success,
                project_id=project_id,
                workspace=workspace,
                design_dna=design_dna,
                build_output=json.dumps(checks, indent=2),
                error=qa_result.error,
                duration_seconds=duration,
            )

        except Exception as exc:
            with self.store.acquire_writer(project_id) as state:
                self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                state.failure = {
                    "phase": "build",
                    "error": str(exc),
                    "failed_at": time.time(),
                }
                self.store.save(state)

            return BuildResult(
                success=False,
                project_id=project_id,
                error=str(exc),
                duration_seconds=time.time() - start_time,
            )

        finally:
            self.runner.release_project(project_id)
