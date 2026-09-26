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
import inspect
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
    ReferenceSnapshot,
    compose_project_instructions,
    invalidate_artifact,
    prebuild_error,
    validate_composed_dna,
)
from app.core.lifecycle import ProjectLifecycle
from app.core.selfcontained import (
    EXTERNAL_RUNTIME_DEPENDENCY,
    SelfContainedReport,
    check_self_contained,
    normalize_artifact,
)
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


def run_fixed_checks(runner, project_id: str, workspace: Path) -> Dict[str, Any]:
    """Run the fixed Phase-7 cheap checks (npm ci / build / typecheck).

    Shared by the initial build AND the revision pipeline so a revision
    re-records the same ``checked`` binding Phase 9 preview requires. Each
    check runs at most once; ``npm_build``/``npm_typecheck`` are skipped when
    ``npm_ci`` fails (dependency installation is never repairable).
    """
    results: Dict[str, Any] = {}

    proc = runner.run_command(project_id, ["npm", "ci"], cwd=workspace, timeout=300)
    results["npm_ci"] = {
        "success": proc.returncode == 0,
        "stdout": _bounded_output(proc.stdout),
        "stderr": _bounded_output(proc.stderr),
    }
    if proc.returncode != 0:
        return results

    proc = runner.run_command(project_id, ["npm", "run", "build"], cwd=workspace, timeout=300)
    results["npm_build"] = {
        "success": proc.returncode == 0,
        "stdout": _bounded_output(proc.stdout),
        "stderr": _bounded_output(proc.stderr),
    }

    proc = runner.run_command(project_id, ["npm", "run", "typecheck"], cwd=workspace, timeout=120)
    results["npm_typecheck"] = {
        "success": proc.returncode == 0,
        "stdout": _bounded_output(proc.stdout),
        "stderr": _bounded_output(proc.stderr),
    }

    return results


# ---------------------------------------------------------------------------
# Self-contained artifact gate (build-time half of the same-origin invariant)
# ---------------------------------------------------------------------------
#
# FRONTEND keeps full design freedom; the FINAL artifact must render without
# third-party runtime assets. The build owns two deterministic steps that run
# AFTER npm build has produced ``dist/`` and BEFORE the artifact is ever
# bound as ``checked`` for QA/preview:
#
#   1. NORMALIZE supported external dependencies into local files (Phase 1:
#      Google Fonts web fonts) -- custom typography is preserved, never
#      replaced with a system font stack merely to pass a check.
#   2. VALIDATE that no unsupported external runtime dependency remains.
#      The ``checked`` binding carries the report so a later consumer never
#      has to re-derive it.
#
# This never touches PreviewSmokeTester's same-origin rule; smoke stays the
# independent RUNTIME verification boundary.

# The shared workspace-level helpers live in ``app.core.selfcontained`` so the
# QA post-repair rebuild boundary can enforce the exact same invariant without
# importing this module (avoids a build <-> qa import cycle).
from app.core.selfcontained import (  # noqa: E402  (imported with the gate block)
    normalize_and_check_self_contained,
    normalize_self_contained,
    self_contained_preflight,
    sync_vendored_assets,
)

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
        # Build-time self-contained gate. An external runtime dependency left
        # in the generated artifact is a deterministic SOURCE-level defect
        # that FRONTEND owns and can fix (replace a CDN script/stylesheet/asset
        # with a local copy). It reuses the EXISTING single compile-repair
        # budget -- no second, asset-specific repair system exists.
        r"\bEXTERNAL_RUNTIME_DEPENDENCY\b",
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
        (name for name in (*_CHEAP_CHECK_SEQUENCE, "self_contained")
         if checks.get(name, {}).get("success") is False),
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
    if entry.get("classification") == "infrastructure":
        return CompileRepairDecision(
            eligible=False,
            failed_check=failed_check,
            classification="infrastructure_failure",
            failed_check_output=((entry.get("stdout") or "") + "\n" + (entry.get("stderr") or "")).strip(),
        )
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
    # F4: True only once the pipeline has provably reached a REMOTE side
    # effect (the Vercel preview/deploy boundary). Persisted on the dispatch
    # claim so a crash/retry can distinguish a safe pre-remote re-run from an
    # unsafe post-remote replay. Absent on legacy results == UNKNOWN ==
    # fail-closed.
    reached_remote: bool = False
    diagnostics: Optional[Dict[str, Any]] = None
    # Stable application error code, when one applies. ``error`` may be free
    # text for genuinely variable failures; the dispatcher prefers this so the
    # user-facing reply can be mapped to specific copy. Absent == ``error`` is
    # already a stable code.
    error_code: Optional[str] = None
    # True when a NEW request may plausibly succeed (transient contention).
    retryable: bool = False


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
        return run_fixed_checks(self.runner, project_id, workspace)

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
        for name in (*_CHEAP_CHECK_SEQUENCE, "self_contained"):
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
- Work only inside the existing workspace source files. For TS6133, remove
  only unused declarations that are not part of a component's public API. If
  an unused prop belongs to the API, preserve and apply it to the component's
  root rendered element; for an icon component accepting className, forward it
  to the root SVG as
  <svg className={{className}}> instead of deleting the prop. The starter
  placeholder replacement step is already complete; do not repeat it.
- A parse/syntax error can mask later TypeScript diagnostics. After fixing
  the reported parser error, inspect sibling icon components in src/components
  for the same unused public-prop pattern and fix only identical defects.
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
            # Distinct operation id so this repair's watchdog can never be
            # refreshed by the initial generation's activity.
            repair_operation_id = str(state.revisions.source_revision)
            invalidate_artifact(state)
            self.store.save(state)

        repair_result = self.hermes_adapter.frontend_build(
            project_id=project_id,
            brief=repair_brief,
            workspace=workspace,
            design_dna_instructions=instructions,
            build_operation_id=repair_operation_id,
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

    def build(
        self,
        project_id: str,
        brief: Dict[str, Any],
        *,
        on_remote_boundary=None,
    ) -> BuildResult:
        """Execute the Phase 7 -> Phase 8 build pipeline for a project.

        Acquires the single worker slot once, runs Phase 7, and on success
        invokes Phase 8 QA before releasing the slot. Phase 8 owns the
        RUNNING -> PREVIEW_READY transition.

        ``on_remote_boundary`` (F4): invoked exactly once, IMMEDIATELY BEFORE
        the first remote (Vercel) side effect of the pipeline. The caller uses
        it to durably mark the operation as having reached remote state, so a
        crash between the remote effect and result persistence can never be
        mistaken for a safe pre-remote retry. It must be called before, never
        after, the remote call.
        """
        start_time = time.time()

        # Acquire the single worker slot
        if not self.runner.acquire_project(project_id):
            # A dedicated, mapped code (not free text): the dispatcher forwards
            # `error` to the user, and an unmapped string rendered as the
            # generic "Something went wrong" with no hint to retry.
            return BuildResult(
                success=False,
                project_id=project_id,
                error="WORKER_BUSY",
                error_code="WORKER_BUSY",
                retryable=True,
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
                # Validate Phase 7's Design DNA against the reference set the
                # instructions were composed from — NOT the live state, which
                # by now is post-lock-release and possibly mutated by a
                # concurrent reference upload. Same hazard the Phase 8 repair
                # path already guards; see app.core.composition.ReferenceSnapshot.
                policy_state = ReferenceSnapshot(state.design_references)
                # The workspace is created and the starter copied OUTSIDE the
                # lock: that is filesystem work proportional to the template
                # size, and the exclusive cross-process lock has a bounded
                # wait that it can exhaust for every other operation on this
                # project. Admission (prebuild_error) and the lifecycle
                # transition stay under the lock, which is what actually needs
                # to be atomic.
                self.store.transition_lifecycle_locked(state, ProjectLifecycle.RUNNING)
                state.revisions.source_revision += 1
                # Operation id for the supervised FRONTEND invocation below.
                frontend_operation_id = str(state.revisions.source_revision)
                self.store.save(state)

            workspace = self.runner.create_workspace(project_id)
            self._copy_starter(workspace)
            toolchain_hashes = _capture_toolchain_hashes(workspace)

            # Use Hermes FRONTEND to derive design and build
            if self.hermes_adapter is not None:
                frontend_result = self.hermes_adapter.frontend_build(
                    project_id=project_id,
                    brief=brief,
                    workspace=workspace,
                    design_dna_instructions=combined_instructions,
                    build_operation_id=frontend_operation_id,
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
                    # Prefer the adapter's stable supervision code (e.g.
                    # FRONTEND_IDLE_TIMEOUT / FRONTEND_HARD_TIMEOUT) over
                    # re-deriving one from free text. The existing
                    # TOOLCHAIN_MUTATION_REJECTED substring test stays as the
                    # fallback for results that carry no code.
                    err_code = (
                        frontend_result.get("error_code")
                        or (
                            "TOOLCHAIN_MUTATION_REJECTED"
                            if "TOOLCHAIN_MUTATION_REJECTED" in err_text
                            else err_text
                        )
                    )
                    with self.store.acquire_writer(project_id) as state:
                        self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                        failure: Dict[str, Any] = {
                            "phase": "frontend_build",
                            "error": err_text,
                            "failed_at": time.time(),
                        }
                        # Bounded supervision metadata for operators: which
                        # invocation ran, for how long, and why it ended. Never
                        # prompts, source, or model output.
                        if frontend_result.get("invocation"):
                            failure["invocation"] = frontend_result["invocation"]
                        state.failure = failure
                        self.store.save(state)

                    return BuildResult(
                        success=False,
                        project_id=project_id,
                        workspace=workspace,
                        error=err_code,
                        error_code=err_code if err_code != err_text else None,
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
            self_contained = None
            if build_success:
                # BUILD-TIME self-contained gate. normalize supported external
                # dependencies (Phase 1: Google Fonts) into local assets, then
                # reject any remaining unsupported external runtime dependency.
                # The normalized assets enter ``before``/``record_checks``
                # below, so the checked binding can never certify bytes that
                # still carry an external runtime dependency.
                self_contained = normalize_and_check_self_contained(project_id, workspace)
                if not self_contained.ok:
                    checks["self_contained"] = {
                        "success": False,
                        "stdout": "",
                        "stderr": _bounded_output(self_contained.error_text()),
                        "error_code": self_contained.error_code,
                        "classification": (
                            "infrastructure" if self_contained.infrastructure_error
                            else "artifact"
                        ),
                        "findings": [f.to_dict() for f in self_contained.findings],
                    }
                    build_success = False
                else:
                    checks["self_contained"] = {"success": True, "stdout": "", "stderr": ""}
                before = source_fingerprint(workspace)
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
                            # The repair may have INTRODUCED an external runtime
                            # dependency (or removed a vendored one): re-run the
                            # full self-contained gate before the checked
                            # binding is ever recorded.
                            self_contained = normalize_and_check_self_contained(
                                project_id, workspace)
                            if not self_contained.ok:
                                checks["self_contained"] = {
                                    "success": False,
                                    "stdout": "",
                                    "stderr": _bounded_output(self_contained.error_text()),
                                    "error_code": self_contained.error_code,
                                    "classification": (
                                        "infrastructure" if self_contained.infrastructure_error
                                        else "artifact"
                                    ),
                                    "findings": [f.to_dict() for f in self_contained.findings],
                                }
                                build_success = False
                            else:
                                checks["self_contained"] = {
                                    "success": True, "stdout": "", "stderr": ""}
                            before = source_fingerprint(workspace)
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
                failed_check = next(
                    (name for name in (*_CHEAP_CHECK_SEQUENCE, "self_contained")
                     if not (checks.get(name) or {}).get("success")),
                    "unknown",
                )
                error = f"CHEAP_CHECKS_FAILED:{failed_check}"
                if failed_check == "self_contained":
                    error = f"{error}:{(self_contained.error_code if self_contained else EXTERNAL_RUNTIME_DEPENDENCY)}"
                return BuildResult(
                    success=False,
                    project_id=project_id,
                    workspace=workspace,
                    design_dna=design_dna,
                    build_output=json.dumps(checks, indent=2),
                    error=error,
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

            remote_reached = False

            def mark_remote_boundary():
                nonlocal remote_reached
                if remote_reached:
                    return
                if on_remote_boundary is not None:
                    on_remote_boundary()
                remote_reached = True

            if qa_result.success and self.preview_orchestrator is not None:
                try:
                    params = inspect.signature(
                        self.preview_orchestrator.run_owned
                    ).parameters
                    accepts_boundary = (
                        'on_remote_boundary' in params
                        or any(p.kind == inspect.Parameter.VAR_KEYWORD
                               for p in params.values())
                    )
                except (TypeError, ValueError):
                    accepts_boundary = False
                if accepts_boundary:
                    preview = self.preview_orchestrator.run_owned(
                        project_id, workspace, slot_held=True,
                        on_remote_boundary=mark_remote_boundary,
                    )
                else:
                    preview = self.preview_orchestrator.run_owned(
                        project_id, workspace, slot_held=True
                    )
                if not preview.success:
                    return BuildResult(
                        False, project_id, workspace=workspace,
                        error=preview.error or preview.error_code,
                        reached_remote=remote_reached,
                        diagnostics=dict(getattr(preview, 'data', {}) or {}),
                        duration_seconds=time.time() - start_time,
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
                reached_remote=remote_reached,
            )

        except Exception as exc:
            # MEDIUM-3: unexpected exceptions are logged with full detail
            # operator-side; the returned BuildResult stays a stable,
            # sanitized application error code.
            logger.error(
                "Unexpected error during Phase 7 build of %s (type=%s)",
                project_id, type(exc).__name__,
            )
            with self.store.acquire_writer(project_id) as state:
                # A preview may already have been DEPLOYED, SMOKE-TESTED and
                # SENT to the user by the time something raises here (the
                # success return is still inside this try). Transitioning to
                # FAILED in that case contradicts durable state the user can
                # already see: PREVIEW_READY -> FAILED is a legal edge, so it
                # would silently bury a working preview and refuse follow-up
                # work. Record the failure for operators and keep the lifecycle
                # at PREVIEW_READY.
                shown = state.deployment.get("latest_shown_preview") or {}
                preview_delivered = (
                    state.lifecycle == ProjectLifecycle.PREVIEW_READY.value
                    and bool(shown)
                    and shown.get("source_revision") == state.revisions.source_revision
                )
                if not preview_delivered:
                    self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
                # ``error`` keeps the raw exception text operator-side on
                # purpose (asserted by test_build.py); ``error_code`` is the
                # stable, sanitized code for anything that surfaces a code.
                state.failure = {
                    "phase": "post_preview" if preview_delivered else "build",
                    "error": str(exc),
                    "error_code": "UNEXPECTED_BUILD_ERROR",
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
