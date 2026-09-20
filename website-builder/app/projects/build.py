"""Phase 7 frontend build for Website Builder R1.

Uses the existing Hermes FRONTEND logical role through the oneshot seam.
FRONTEND owns design/build decisions. Application owns workspace/lifecycle.
On success, hands off to Phase 8 QA within the same worker ownership.

If the deterministic cheap checks fail after initial generation for a
source/code-level reason, exactly ONE targeted FRONTEND compile-repair
attempt may run on the existing workspace (MAX_COMPILE_REPAIR_ATTEMPTS = 1),
followed by a full rerun of the fixed cheap-check sequence. No regeneration,
no second attempt. Infrastructure/runtime failures never enter this path and
never consume the attempt.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

from app.deploy.snapshot import source_fingerprint, record_checks
from app.core.composition import (
    compose_project_instructions,
    invalidate_artifact,
    prebuild_error,
    validate_composed_dna,
)
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

# Phase-7 compile repair: after initial FRONTEND generation, a single targeted
# FRONTEND repair attempt may run when the fixed cheap checks fail for a
# deterministic source/code-level reason. Exactly one attempt, never a second.
# This budget is SEPARATE from Phase-8 QA's own bounded repair budget.
MAX_COMPILE_REPAIR_ATTEMPTS = 1

# Deterministic execution order of the fixed cheap checks.
_CHEAP_CHECK_SEQUENCE = ("npm_ci", "npm_build", "npm_typecheck")

# Bounded diagnostic capture for cheap-check output. Long command output
# (e.g. a Tailwind/Vite stack trace) must not push the actual diagnostic
# line out of the captured text, so we preserve BOTH the head and the tail
# instead of a tail-only slice. Output stays bounded; unlimited command
# output is never persisted.
_DIAGNOSTIC_CAPTURE_LIMIT = 2000
_DIAGNOSTIC_TRUNCATION_MARKER = "\n...[truncated]...\n"

def _bounded_output(text: Optional[str], limit: int = _DIAGNOSTIC_CAPTURE_LIMIT) -> str:
    """Bound captured command output, preserving head + tail.

    Deterministic: short output passes through unchanged; long output keeps
    the first and last portions with a fixed truncation marker between them.
    The result never exceeds ``limit + len(marker)`` characters.
    """
    if not text:
        return ""
    if len(text) <= limit:
        return text
    head = limit // 2
    tail = limit - head
    return text[:head] + _DIAGNOSTIC_TRUNCATION_MARKER + text[-tail:]

# Deterministic source/code-level build failure signatures. Any match makes a
# failing npm_build / npm_typecheck check eligible for one targeted repair.
_REPAIRABLE_SOURCE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\berror\s+TS\d{3,5}\b",          # TypeScript diagnostics (TS6133, TS2304, TS2307, ...)
        r"SyntaxError:",
        r"Unexpected token\b",
        r"Parse error\b",
        r"Cannot find module\b",
        r"Module not found\b",
        r"Could not resolve\b",
        r"Failed to resolve import\b",
        r"Rollup failed to resolve\b",
        r"Transform failed\b",
        # Tailwind v4 generated-source diagnostic (real p5 failure). Narrow on
        # purpose: this exact @theme contract violation, not generic "Error:".
        r"@theme` blocks must only contain custom properties or `@keyframes`",
    )
)

# Infrastructure / toolchain / environment failure signatures. Any match makes
# a failing check INELIGIBLE for compile repair, regardless of other matches:
# repairing source cannot fix a broken runner, registry, or toolchain.
_INFRASTRUCTURE_PATTERNS = tuple(
    re.compile(pattern, re.IGNORECASE)
    for pattern in (
        r"\bEAI_AGAIN\b",
        r"\bENOTFOUND\b",
        r"\bETIMEDOUT\b",
        r"\bECONNREFUSED\b",
        r"\bECONNRESET\b",
        r"\bECONNABORTED\b",
        r"\bEACCES\b",
        r"\bEPERM\b",
        r"\bENOSPC\b",
        r"\bEMFILE\b",
        r"\bEBADENGINE\b",
        r"\bERESOLVE\b",
        r"command not found\b",
        r"not recognized as an internal or external command",
        r"out of memory",
        r"JavaScript heap out of memory",
        r"\btimed? ?out\b",
    )
)


@dataclass
class CompileRepairDecision:
    """Classification of a failed fixed cheap-check sequence.

    ``eligible`` is True only when the FIRST failing check is a deterministic
    source/code-level build failure that a targeted FRONTEND compile repair
    can legitimately fix. Infrastructure/runtime failures are never eligible
    and must never consume the single repair attempt.
    """

    eligible: bool
    failed_check: Optional[str]
    classification: str  # "no_failure" | "source_error" | "infrastructure_failure" | "unclassified"
    failed_check_output: str = ""


def classify_cheap_check_failure(checks: Dict[str, Any]) -> CompileRepairDecision:
    """Decide whether a failed cheap-check sequence is repairable in place.

    Rules (fail-closed):
    - npm_ci failures are NEVER repairable: dependency installation is a
      toolchain/registry concern, and the identity files it touches
      (package.json, package-lock.json) are protected from FRONTEND edits.
    - Infrastructure/runtime signatures ALWAYS win over source signatures.
      When in doubt the failure is not a proven source defect, so no repair.
    - A failing check without captured output is not provably a source
      defect, so it is not eligible.
    """
    failed_check = next(
        (name for name in _CHEAP_CHECK_SEQUENCE if checks.get(name, {}).get("success") is False),
        None,
    )
    if failed_check is None:
        return CompileRepairDecision(
            eligible=False, failed_check=None, classification="no_failure"
        )

    if failed_check == "npm_ci":
        return CompileRepairDecision(
            eligible=False,
            failed_check=failed_check,
            classification="infrastructure_failure",
            failed_check_output="",
        )

    entry = checks.get(failed_check) or {}
    output = ((entry.get("stdout") or "") + "\n" + (entry.get("stderr") or "")).strip()

    if any(pattern.search(output) for pattern in _INFRASTRUCTURE_PATTERNS):
        return CompileRepairDecision(
            eligible=False,
            failed_check=failed_check,
            classification="infrastructure_failure",
            failed_check_output=output,
        )

    if output and any(pattern.search(output) for pattern in _REPAIRABLE_SOURCE_PATTERNS):
        return CompileRepairDecision(
            eligible=True,
            failed_check=failed_check,
            classification="source_error",
            failed_check_output=output,
        )

    return CompileRepairDecision(
        eligible=False,
        failed_check=failed_check,
        classification="unclassified",
        failed_check_output=output,
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
            "stdout": _bounded_output(proc.stdout),
            "stderr": _bounded_output(proc.stderr),
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
            "stdout": _bounded_output(proc.stdout),
            "stderr": _bounded_output(proc.stderr),
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
            "stdout": _bounded_output(proc.stdout),
            "stderr": _bounded_output(proc.stderr),
        }

        return results

    def _build_compile_repair_instructions(
        self,
        state,
        checks: Dict[str, Any],
        decision: CompileRepairDecision,
    ) -> str:
        """Task text appended to the project instructions for the ONE targeted
        compile repair. FRONTEND gets the exact failing check, the bounded
        captured output, the existing brief/design context, and an explicit
        minimum-change mandate — never a regeneration request."""
        failed = decision.failed_check or "unknown"
        failing = checks.get(failed) or {}

        def _tail(value: Optional[str]) -> str:
            return (value or "").strip() or "(no output)"

        full_results = []
        for name in _CHEAP_CHECK_SEQUENCE:
            if name not in checks:
                continue
            entry = checks[name] or {}
            status = "PASS" if entry.get("success") else "FAIL"
            full_results.append(
                f"{name}: {status}\nexit output (bounded):\n{_tail(entry.get('stdout'))}\n"
                f"errors (bounded):\n{_tail(entry.get('stderr'))}"
            )
        results_text = "\n\n".join(full_results) or "(no checks captured)"

        dna = state.design_dna or {}
        dna_note = json.dumps(dna, indent=2) if dna else "(Design DNA persisted in workspace design-dna.json)"

        return f"""COMPILE REPAIR TASK (Phase 7 deterministic build verification FAILED).

The website already generated in this workspace did NOT pass the fixed
deterministic checks (npm ci, npm run build, npm run typecheck). The FIRST
failing check is: {failed}

Failing check output (bounded, exactly as captured):
stdout:
{_tail(failing.get('stdout'))}

stderr:
{_tail(failing.get('stderr'))}

Full fixed-check results (bounded):
{results_text}

Existing Design DNA (context only — do not redesign):
{dna_note}

Repair rules (HARD constraints):
- This is a TARGETED COMPILE REPAIR, NOT a regeneration. Make the MINIMUM
  code change required for the project to pass npm ci, npm run build, and
  npm run typecheck.
- Do NOT rewrite, redesign, regenerate, or re-create the website. Do not
  touch unrelated files or components.
- Work only inside the existing workspace source files (e.g. remove or use
  an unused import/variable for TS6133). The starter placeholder replacement
  step is already complete; do not repeat it.
- Do NOT modify or delete protected toolchain files: .nvmrc, .npmrc,
  package.json, package-lock.json, tsconfig.json, tsconfig.app.json,
  tsconfig.node.json, vite.config.ts.
- Do NOT run npm ci, npm run build, or npm run typecheck yourself — the
  application reruns the entire fixed cheap-check sequence after you finish.
- Do NOT invent business facts.

Respond with a JSON summary:
{{
  "success": true|false,
  "design_dna_path": "path to design-dna.json",
  "error": "error message if failed"
}}
"""

    def _attempt_compile_repair(
        self,
        project_id: str,
        workspace: Path,
        checks: Dict[str, Any],
        decision: CompileRepairDecision,
        toolchain_hashes: Dict[str, str],
    ) -> Dict[str, Any]:
        """Invoke FRONTEND exactly once for the minimum compile fix.

        Returns {"executed": bool, "toolchain_violation": Optional[str],
        "design_dna": Optional[dict]}:
        - executed=True only when the repair call itself succeeded and any
          returned Design DNA passed the composed-policy validation. The
          deterministic checks are ALWAYS the authority after this; the
          model's success claim is never trusted.
        - toolchain_violation is set when a protected starter/toolchain
          identity file was mutated/removed during the repair — a
          deterministic TOOLCHAIN_MUTATION_REJECTED policy rejection.
        """
        if self.hermes_adapter is None:
            return {"executed": False, "toolchain_violation": None, "design_dna": None}

        # Invalidate before invoking the mutating repair call: even a failed
        # or timed-out invocation may have changed source on disk. Compose
        # the repair instructions from persisted state (brief + policy).
        with self.store.acquire_writer(project_id) as state:
            instructions = compose_project_instructions(
                state,
                access_key=self.web3forms_access_key,
                task=self._build_compile_repair_instructions(state, checks, decision),
            )
            repair_brief = dict(state.brief)
            policy_state = state
            state.revisions.source_revision += 1
            invalidate_artifact(state)
            self.store.save(state)

        repair_result = self.hermes_adapter.frontend_build(
            project_id=project_id,
            brief=repair_brief,
            workspace=workspace,
            design_dna_instructions=instructions,
        )

        # Toolchain protection stays mandatory: verify protected
        # starter/toolchain files after the repair exactly as after initial
        # FRONTEND generation and after every Phase-8 QA repair.
        violation = _verify_toolchain_untouched(workspace, toolchain_hashes)
        if violation:
            logger.warning(
                "Protected toolchain file mutated during Phase-7 compile "
                "repair of %s: %s",
                project_id,
                violation,
            )
            return {"executed": False, "toolchain_violation": violation, "design_dna": None}

        if not repair_result.get("success"):
            return {"executed": False, "toolchain_violation": None, "design_dna": None}

        dna = repair_result.get("design_dna")
        if dna is None:
            # The repair did not return/persist a readable Design DNA —
            # fine: keep the generation-time DNA and let the deterministic
            # checks judge the workspace.
            return {"executed": True, "toolchain_violation": None, "design_dna": None}

        try:
            validate_composed_dna(dna, policy_state)
        except ValueError:
            logger.warning(
                "Phase-7 compile repair of %s returned invalid Design DNA",
                project_id,
            )
            return {"executed": False, "toolchain_violation": None, "design_dna": None}

        with self.store.acquire_writer(project_id) as locked:
            locked.design_dna = dna
            locked.revisions.design_dna_version = dna.get(
                "version", locked.revisions.design_dna_version + 1
            )
            self.store.save(locked)

        return {"executed": True, "toolchain_violation": None, "design_dna": dna}

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

            initial_checks: Optional[Dict[str, Any]] = None
            compile_repair_attempts = 0
            final_stage = "initial"
            rejection_error: Optional[str] = None

            if not build_success:
                decision = classify_cheap_check_failure(checks)
                logger.info(
                    "Phase-7 cheap checks failed for %s: failed_check=%s classification=%s",
                    project_id,
                    decision.failed_check,
                    decision.classification,
                )
                if decision.eligible:
                    # Exactly ONE targeted FRONTEND compile-repair attempt on
                    # the EXISTING generated workspace. No regeneration, no
                    # second attempt, no new agents/services. This budget is
                    # independent from Phase-8 QA's own repair budget.
                    compile_repair_attempts += 1
                    initial_checks = checks
                    logger.info(
                        "Phase-7 compile repair attempt %d/%d for %s (failed_check=%s)",
                        compile_repair_attempts,
                        MAX_COMPILE_REPAIR_ATTEMPTS,
                        project_id,
                        decision.failed_check,
                    )
                    repair_outcome = self._attempt_compile_repair(
                        project_id=project_id,
                        workspace=workspace,
                        checks=checks,
                        decision=decision,
                        toolchain_hashes=toolchain_hashes,
                    )
                    if repair_outcome["toolchain_violation"]:
                        # Deterministic toolchain-policy rejection — fail
                        # closed, never fall through to generic handling.
                        final_stage = "repair_rejected"
                        rejection_error = (
                            f"TOOLCHAIN_MUTATION_REJECTED ({repair_outcome['toolchain_violation']})"
                        )
                    elif repair_outcome["executed"]:
                        if repair_outcome["design_dna"] is not None:
                            design_dna = repair_outcome["design_dna"]
                        logger.info(
                            "Phase-7 compile repair completed for %s; rerunning fixed cheap checks",
                            project_id,
                        )
                        final_stage = "post_repair"
                        # Authoritative: rerun the ENTIRE fixed cheap-check
                        # sequence. The model's fix claim is never trusted.
                        before = source_fingerprint(workspace)
                        checks = self._run_fixed_checks(project_id, workspace)
                        build_success = all(r.get("success", False) for r in checks.values())
                        if build_success:
                            record_checks(self.store, project_id, workspace, before)
                        logger.info(
                            "Phase-7 cheap checks rerun for %s: final result=%s",
                            project_id,
                            "PASS" if build_success else "FAIL",
                        )
                    else:
                        # Repair itself failed to execute: keep the initial
                        # failing checks as the authoritative failure and
                        # fail closed. The attempt is consumed.
                        final_stage = "repair_execution_failed"
                        checks = initial_checks
                        logger.warning(
                            "Phase-7 compile repair for %s did not execute; "
                            "preserving initial cheap-check failure",
                            project_id,
                        )

            # Update state with results
            with self.store.acquire_writer(project_id) as state:
                if build_success:
                    # Successful Phase 7 keeps lifecycle RUNNING. Only Phase 8
                    # may advance RUNNING -> PREVIEW_READY.
                    state.revisions.design_dna_version = design_dna.get("version", 1) if design_dna else 0
                    state.design_dna = design_dna or {}
                else:
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                    failure: Dict[str, Any] = {
                        "phase": "cheap_checks",
                        "checks": checks,
                        "compile_repair_attempts": compile_repair_attempts,
                        "final_stage": final_stage,
                        "failed_at": time.time(),
                    }
                    if rejection_error is not None:
                        failure["error"] = rejection_error
                    # Persist BOTH the initial cheap-check failure and the
                    # post-repair cheap-check failure whenever a repair ran,
                    # so operators can diagnose each independently.
                    if initial_checks is not None and initial_checks is not checks:
                        failure["initial_checks"] = initial_checks
                    state.failure = failure
                self.store.save(state)

            if not build_success:
                if rejection_error is not None:
                    return BuildResult(
                        success=False,
                        project_id=project_id,
                        workspace=workspace,
                        design_dna=design_dna,
                        build_output=json.dumps(checks, indent=2),
                        error="TOOLCHAIN_MUTATION_REJECTED",
                        duration_seconds=time.time() - start_time,
                    )
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
