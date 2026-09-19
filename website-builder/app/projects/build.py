"""Phase 7 frontend build for Website Builder R1.

Uses the existing Hermes FRONTEND logical role through the oneshot seam.
FRONTEND owns design/build decisions. Application owns workspace/lifecycle.
On success, hands off to Phase 8 QA within the same worker ownership.
"""

from __future__ import annotations

import hashlib
import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from app.deploy.snapshot import source_fingerprint, record_checks
from app.core.composition import compose_project_instructions, prebuild_error, validate_composed_dna
from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.qa.orchestrator import QAOrchestrator
from app.sandbox.runner import ProjectRunner, WorkspaceError

# Protected toolchain identity files that must NEVER be mutated by FRONTEND or repairs
PROTECTED_TOOLCHAIN_FILES = (
    ".nvmrc",
    ".npmrc",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "vite.config.ts",
)


def _file_sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _capture_toolchain_hashes(workspace: Path) -> Dict[str, str]:
    hashes = {}
    for filename in PROTECTED_TOOLCHAIN_FILES:
        target = workspace / filename
        if target.is_file():
            hashes[filename] = _file_sha256(target)
    return hashes


def _verify_toolchain_untouched(workspace: Path, expected_hashes: Dict[str, str]) -> Optional[str]:
    """Verify that protected toolchain identity files have not been modified or deleted.

    Returns the filename of the first modified/missing file, or None if all match.
    """
    for filename, expected in expected_hashes.items():
        target = workspace / filename
        if not target.is_file():
            return f"missing:{filename}"
        if _file_sha256(target) != expected:
            return f"modified:{filename}"
    return None


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
        preview_orchestrator=None,
        web3forms_access_key: Optional[str] = None,
    ):
        self.preview_orchestrator = preview_orchestrator
        self.runner = runner
        self.store = store
        self.hermes_adapter = hermes_adapter
        self.starter_path = starter_path or _STARTER_PATH
        # Phase 14: caller-injected verified destination secret, never
        # hardcoded. None selects the deterministic fallback-link path.
        self.web3forms_access_key = web3forms_access_key

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
            # Admission and first workspace mutation share the writer lock.
            with self.store.acquire_writer(project_id) as state:
                # Legacy direct callers may supply initial requirements once;
                # persisted requirements always win thereafter.
                if not state.brief:
                    state.brief = dict(brief)
                error = prebuild_error(state)
                if error:
                    return BuildResult(False, project_id, error=error)
                brief = dict(state.brief)
                combined_instructions = compose_project_instructions(
                    state, access_key=self.web3forms_access_key
                )
                policy_state = state
                workspace = self.runner.create_workspace(project_id)
                self._copy_starter(workspace)
                toolchain_hashes = _capture_toolchain_hashes(workspace)
                self.store.transition_lifecycle_locked(state, ProjectLifecycle.RUNNING)
                state.revisions.source_revision += 1
                self.store.save(state)

            # Use Hermes FRONTEND to derive design and build
            if self.hermes_adapter is not None:
                frontend_result = self.hermes_adapter.frontend_build(
                    project_id=project_id,
                    brief=brief,
                    workspace=workspace,
                    design_dna_instructions=combined_instructions,
                )

                # MEDIUM-5: verify the protected starter/toolchain identity
                # files after EVERY initial FRONTEND generation — regardless
                # of the FRONTEND result. Mutation or removal is a
                # deterministic toolchain-policy rejection, never a fallback
                # into generic FRONTEND error handling.
                toolchain_violation = _verify_toolchain_untouched(workspace, toolchain_hashes)
                if toolchain_violation:
                    logger.warning(
                        "Protected toolchain file mutated during initial FRONTEND "
                        "generation for project %s: %s",
                        project_id,
                        toolchain_violation,
                    )
                    frontend_result = {
                        "success": False,
                        "error": f"TOOLCHAIN_MUTATION_REJECTED ({toolchain_violation})",
                    }

                if frontend_result.get("success"):
                    try:
                        validate_composed_dna(frontend_result.get("design_dna"), policy_state)
                    except ValueError as exc:
                        frontend_result = {"success": False, "error": str(exc)}

                if not frontend_result.get("success"):
                    err_text = frontend_result.get("error", "Unknown FRONTEND error")
                    err_code = "TOOLCHAIN_MUTATION_REJECTED" if "TOOLCHAIN_MUTATION_REJECTED" in err_text else err_text
                    with self.store.acquire_writer(project_id) as state:
                        self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                        state.failure = {
                            "phase": "frontend_build",
                            "error": err_text,
                            "failed_at": time.time(),
                        }
                        self.store.save(state)

                    return BuildResult(
                        success=False,
                        project_id=project_id,
                        workspace=workspace,
                        error=err_code,
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
            before = source_fingerprint(workspace)
            checks = self._run_fixed_checks(project_id, workspace)
            build_success = all(r.get("success", False) for r in checks.values())
            if build_success:
                record_checks(self.store, project_id, workspace, before)

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
                failed_check = next((name for name, r in checks.items() if not r.get("success")), "unknown")
                return BuildResult(
                    success=False,
                    project_id=project_id,
                    workspace=workspace,
                    design_dna=design_dna,
                    build_output=json.dumps(checks, indent=2),
                    error=f"CHEAP_CHECKS_FAILED:{failed_check}",
                    duration_seconds=time.time() - start_time,
                )

            # Phase 7 succeeded — hand off to Phase 8 QA inside the same
            # worker ownership boundary. QAOrchestrator does not acquire or
            # release the project slot; that stays here.
            qa_orchestrator = QAOrchestrator(
                self.runner,
                self.store,
                hermes_adapter=self.hermes_adapter,
                web3forms_access_key=self.web3forms_access_key,
                # MEDIUM-5: QA repair must verify the same protected toolchain
                # files after every FRONTEND repair invocation.
                toolchain_verify=lambda ws: _verify_toolchain_untouched(ws, toolchain_hashes),
            )
            qa_result = qa_orchestrator.run(
                project_id=project_id,
                workspace=workspace,
                brief=brief,
                design_dna=design_dna,
            )

            if qa_result.success and self.preview_orchestrator is not None:
                preview = self.preview_orchestrator.run_owned(
                    project_id, workspace, slot_held=True
                )
                if not preview.success:
                    return BuildResult(False, project_id, workspace=workspace,
                                       error=preview.error or preview.error_code,
                                       duration_seconds=time.time() - start_time)

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
            # MEDIUM-3: unexpected exceptions are logged with full detail
            # operator-side; the returned BuildResult stays a stable,
            # sanitized application error code.
            logger.exception("Unexpected error during Phase 7 build of %s", project_id)
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
                error="UNEXPECTED_BUILD_ERROR",
                duration_seconds=time.time() - start_time,
            )

        finally:
            self.runner.release_project(project_id)
