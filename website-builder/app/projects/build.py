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
import os
import re
import shutil
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

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
from app.core.requirement_creator import (
    RequirementCreatorConfig,
    compile_design,
    compile_prd,
)
from app.core.requirement_artifact_builder import build_artifact_from_brief
from app.core.design_requirement_artifact import write_requirement_artifact
from app.core.document_store import (
    DEPLOYMENT_DOCUMENTS_KEY,
    document_records,
    invalidate_design,
    read_document,
    store_design_guidance,
    store_documents,
)
from app.core.design_guidance import compile_design_guidance
from app.core.decision_modes import DecisionEngineConfig, DecisionEngineMode
from app.core.document_guidance import guidance_for_workspace
from app.qa.orchestrator import QAOrchestrator
from app.sandbox.runner import ProjectRunner, WorkspaceError

# Protected toolchain identity files that must NEVER be mutated by FRONTEND or repairs
PROTECTED_TOOLCHAIN_FILES = (
    ".nvmrc",
    ".npmrc",
    "components.json",
    "package.json",
    "package-lock.json",
    "tsconfig.json",
    "tsconfig.app.json",
    "tsconfig.node.json",
    "vite.config.ts",
)

#: Config filenames a tool auto-discovers with PRECEDENCE, where the starter
#: ships only the one in ``PROTECTED_TOOLCHAIN_FILES``. Vite resolves
#: ``DEFAULT_CONFIG_FILES`` in order -- ``vite.config.js``, ``.mjs``, ``.ts``,
#: ``.cjs``, ``.mts``, ``.cts`` -- so a FRONTEND-created ``vite.config.js``
#: SHADOWS the platform-owned ``vite.config.ts`` and a by-name hash guard never
#: sees it. Reproduced live: planting ``vite.config.js`` with a different
#: ``build.outDir`` made ``npm run build`` write there while
#: ``_verify_toolchain_untouched`` reported ``None`` (untouched). These must be
#: ABSENT -- the platform owns exactly one config per resolver.
FORBIDDEN_TOOLCHAIN_FILES = (
    "vite.config.js",
    "vite.config.mjs",
    "vite.config.cjs",
    "vite.config.mts",
    "vite.config.cts",
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


def _frontend_failure_reason(result: Dict[str, Any]) -> Dict[str, Any]:
    """Bounded, sanitized reason a FRONTEND invocation failed.

    Derivation mirrors the initial-generation path in ``build()``: prefer the
    adapter's stable supervision code (e.g. ``FRONTEND_IDLE_TIMEOUT`` /
    ``FRONTEND_HARD_TIMEOUT``) over re-deriving one from free text, and keep
    the ``TOOLCHAIN_MUTATION_REJECTED`` substring test as the fallback for
    results that carry no code. Unlike the initial path (whose ``error`` is
    surfaced verbatim to the caller), the reason is bounded here because it is
    PERSISTED, not rendered.

    ``invocation`` is the adapter's bounded ``frontend_forensics/3`` receipt:
    activity counters, timestamps, normalized descriptions, stream frame
    shapes, and hashes. It never contains prompts, source, model output, tool
    arguments, or URLs.
    """
    raw_error = result.get("error") or "Unknown FRONTEND error"
    reason = {
        "error": _bounded_output(raw_error),
        "error_code": result.get("error_code")
        or ("TOOLCHAIN_MUTATION_REJECTED"
            if "TOOLCHAIN_MUTATION_REJECTED" in raw_error
            else _bounded_output(raw_error)),
    }
    if result.get("invocation"):
        reason["invocation"] = result["invocation"]
    return reason


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
    """Verify protected toolchain identity files are unchanged AND complete.

    Two independent dimensions:

    1. **Present protected files are unchanged.** Every captured file must still
       exist and hash the same, else ``missing:<name>`` / ``modified:<name>``.
    2. **A protected file that was ABSENT at capture must not APPEAR, and no
       shadowing config may exist.** The captured dict only names files present
       at capture, so an absent-at-capture protected file was previously
       unverified -- and, more sharply, a tool that auto-discovers a config by
       PRECEDENCE (Vite: ``vite.config.js`` > ``.mjs`` > ``.ts`` > …) would let a
       FRONTEND-created ``vite.config.js`` SHADOW the protected
       ``vite.config.ts``. Reproduced live: the shadow config's ``outDir`` was
       honored while the by-name guard reported untouched.

    Returns the first offending filename (``appeared:<name>`` / ``forbidden:`` /
    ``missing:`` / ``modified:``), or None if everything matches.
    """
    for filename, expected in expected_hashes.items():
        target = workspace / filename
        if not target.is_file():
            return f"missing:{filename}"
        if _file_sha256(target) != expected:
            return f"modified:{filename}"

    # A protected file that did not exist at capture must not have been created.
    for filename in PROTECTED_TOOLCHAIN_FILES:
        if filename not in expected_hashes and (workspace / filename).is_file():
            return f"appeared:{filename}"

    # A shadowing config the resolver would prefer must never exist.
    for filename in FORBIDDEN_TOOLCHAIN_FILES:
        if (workspace / filename).is_file():
            return f"forbidden:{filename}"

    return None


# ---------------------------------------------------------------------------
# Rebuild-safe starter merge (Phase 7 workspace provisioning)
#
# The starter is copied into the project workspace with MERGE semantics: every
# starter entry overwrites the same-named entry already present and unrelated
# generated files (PRD.md, Design.md, FRONTEND components, ...) are left in
# place. ``shutil.copytree(..., dirs_exist_ok=True)`` / ``shutil.copy2`` alone
# are not safe for a REBUILD: ``copy2`` opens the destination with ``'wb'`` and
# raises ``PermissionError`` when a previously copied starter file is read-only
# (e.g. the staging source refresh left the source ``chmod a-w``, so the first
# build's copy inherited read-only modes), and both follow a symlink planted at
# the destination -- which would let a link redirect the write outside the
# workspace. The helpers below replace the destination safely instead.
# ---------------------------------------------------------------------------

#: Entries the starter merge never propagates (build output / VCS metadata).
_STARTER_SKIP = ("node_modules", "dist", ".git")


def _assert_confined(path: Path, workspace: Path) -> None:
    """Fail closed unless the write target stays inside ``workspace``.

    Validates the REAL parent directory (``path.parent``), because a destination
    entry is written by unlinking any symlink at ``path`` and creating a fresh
    entry inside its parent -- so the parent chain is what a planted symlink
    would have to escape through. The workspace root itself is trivially
    confined. ``os.path.realpath`` resolves intermediate symlinks and ``..``, so
    a redirected parent is caught here before any mutation.
    """
    workspace_real = Path(os.path.realpath(workspace))
    if Path(os.path.realpath(path)) == workspace_real:
        return
    parent_real = Path(os.path.realpath(path.parent))
    if parent_real != workspace_real and workspace_real not in parent_real.parents:
        raise WorkspaceError(
            f"Starter copy destination escapes workspace: {path}"
        )


def _remove_path_safely(path: Path) -> None:
    """Remove ``path`` WITHOUT following a final symlink.

    A symlink is unlinked (never dereferenced, so a planted link cannot redirect
    the removal outside the workspace), a regular file is unlinked, and a
    directory is emptied bottom-up. Owner-write is added ONLY to the directories
    inside this exact subtree (scoped, additive) so a previously read-only
    starter directory can be emptied -- the rest of the workspace is never
    touched and there is no recursive chmod of the workspace.
    """
    if path.is_symlink() or not path.is_dir():
        os.unlink(path)
        return

    # Top-down: make each directory writable enough to empty, and unlink every
    # entry. ``files`` never contains a directory (symlinked dirs land in
    # ``dirs``), so each entry is a regular file or a symlink to unlink, not
    # follow.
    for root, dirs, files in os.walk(path, topdown=True, followlinks=False):
        root_path = Path(root)
        mode = stat.S_IMODE(os.stat(root_path).st_mode)
        if not mode & stat.S_IWUSR:
            os.chmod(root_path, mode | stat.S_IRWXU)
        for name in files:
            os.unlink(root_path / name)
        for name in dirs:
            target = root_path / name
            if target.is_symlink():
                os.unlink(target)
    # Bottom-up: remove now-empty directories (symlinked dirs already unlinked).
    for root, dirs, _ in os.walk(path, topdown=False, followlinks=False):
        for name in dirs:
            target = Path(root) / name
            if target.is_symlink():
                os.unlink(target)
            else:
                os.rmdir(target)
    os.rmdir(path)


def _ensure_dir(path: Path, workspace: Path) -> None:
    """Create ``path`` as a real directory, replacing a symlink or a file.

    Never follows a symlink at ``path``. Adds owner-write to an existing
    read-only directory so the merge can proceed (additive only -- no bits are
    removed, and only this specific directory is touched).
    """
    _assert_confined(path, workspace)
    if path.is_symlink() or (path.exists() and not path.is_dir()):
        _remove_path_safely(path)
    path.mkdir(parents=True, exist_ok=True)
    mode = stat.S_IMODE(os.stat(path).st_mode)
    if not mode & stat.S_IWUSR:
        os.chmod(path, mode | stat.S_IWUSR | stat.S_IXUSR)


def _replace_file(src: Path, dest: Path, workspace: Path) -> None:
    """Copy ``src`` over ``dest``, safely replacing a read-only/symlinked dest.

    The destination is unlinked first (never followed) so a read-only file is
    replaced instead of failing ``open(..., 'wb')`` with EACCES, and a planted
    symlink cannot redirect the write. ``shutil.copy2`` then preserves the
    source's content AND permission bits (including any executable mode).
    """
    _assert_confined(dest, workspace)
    _ensure_dir(dest.parent, workspace)
    if dest.is_symlink() or (dest.exists() and not dest.is_dir()):
        os.unlink(dest)
    elif dest.is_dir():
        _remove_path_safely(dest)
    shutil.copy2(src, dest)


def _copy_tree_merge(src: Path, dest: Path, workspace: Path) -> None:
    """Recursively merge ``src`` into ``dest`` (overwrite same names, keep the
    rest). Directories are ensured writable as needed; files are replaced via
    :func:`_replace_file`. Every destination is confined to ``workspace``.
    """
    _ensure_dir(dest, workspace)
    for entry in src.iterdir():
        if entry.name in _STARTER_SKIP:
            continue
        target = dest / entry.name
        if entry.is_dir():
            _copy_tree_merge(entry, target, workspace)
        else:
            _replace_file(entry, target, workspace)


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
        critic_scanner=None,
        requirement_creator_config: Optional[RequirementCreatorConfig] = None,
        decision_engine=None,
        decision_engine_config: Optional[DecisionEngineConfig] = None,
        design_capability_report=None,
        design_fidelity_enabled: bool = False,
        frontend_invocation_limit: Optional[int] = None,
    ):
        self.preview_orchestrator = preview_orchestrator
        self.runner = runner
        self.store = store
        self.hermes_adapter = hermes_adapter
        self.starter_path = starter_path or _STARTER_PATH
        # Phase 14: caller-injected verified destination secret, never
        # hardcoded. None selects the deterministic fallback-link path.
        self.web3forms_access_key = web3forms_access_key
        # D3b: the production Impeccable critic scanner, injected into every
        # QAOrchestrator this builder constructs so the bounded critic repair
        # stage runs on the real production path.
        self.critic_scanner = critic_scanner
        # N2: Requirement Creator feature config. DISABLED by default, so a
        # builder constructed without it compiles and writes no documents and
        # the build path is byte-identical to pre-N2.
        self.requirement_creator_config = (
            requirement_creator_config or RequirementCreatorConfig()
        )
        # N6: the optional Decision Engine + its mode config. When design mode is
        # active the builder compiles a pre-build DesignGuidance.md before the
        # initial FRONTEND call. Default: no engine / disabled, so the build path
        # is byte-identical to pre-N6.
        self.decision_engine = decision_engine
        self.decision_engine_config = decision_engine_config or DecisionEngineConfig()
        # N6-D: the AUTHORITATIVE design capability report (a
        # ``DesignCapabilityReport``), resolved once at composition and injected
        # here. The design context pack builder needs it to prove a reference is
        # actually reachable on THIS host; without it the selector assumes every
        # reference is unreachable and the pack degrades to ``resources=0``.
        # Injected (never re-resolved here) so capability truth stays
        # single-sourced and deterministic. ``None`` keeps the pre-fix behaviour
        # for callers that have no report (never invents availability).
        self.design_capability_report = design_capability_report
        # V4-A: the runtime design-fidelity gate switch, injected into every
        # QAOrchestrator this builder constructs. FALSE (default) keeps the
        # build path byte-identical to pre-V4-A.
        self.design_fidelity_enabled = bool(design_fidelity_enabled)
        # V4-B2: the GLOBAL per-operation FRONTEND invocation cap. None (default)
        # keeps production byte-identical (per-stage budgets only); the staging
        # E2E sets 2 so ONE budget spans the initial generation and every repair
        # stage of a single build.
        self.frontend_invocation_limit = frontend_invocation_limit

    def _copy_starter(self, workspace: Path) -> None:
        """Merge the fixed frontend starter into the project workspace.

        Rebuild-safe and symlink-safe: existing read-only starter files are
        REPLACED rather than opened for write, a planted symlink at a
        destination is unlinked (never followed), and every write is confined
        to ``workspace``. Unrelated generated files already in the workspace
        (PRD.md, Design.md, FRONTEND source) are preserved -- the merge
        overwrites only same-named starter entries.
        """
        if not self.starter_path.exists():
            raise FileNotFoundError(f"Frontend starter not found: {self.starter_path}")

        workspace = Path(workspace)
        for item in self.starter_path.iterdir():
            if item.name in _STARTER_SKIP:
                continue
            dest = workspace / item.name
            if item.is_dir():
                _copy_tree_merge(item, dest, workspace)
            else:
                _replace_file(item, dest, workspace)

    def _compile_and_store_documents(
        self,
        project_id: str,
        workspace: Path,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]],
        *,
        scope: Optional[str] = None,
        readiness: Optional[str] = None,
    ) -> None:
        """Compile BOTH documents and write them into the workspace (N2).

        Convenience wrapper used where both the brief and the accepted Design DNA
        are already available (revisions, repairs). The initial build uses the
        two split methods instead, because the Design DNA does not exist until
        FRONTEND has produced it.
        """
        self._compile_and_store_prd(project_id, workspace, brief, scope=scope, readiness=readiness)
        self._compile_and_store_design(project_id, workspace, brief, design_dna)

    def _compile_and_store_prd(
        self,
        project_id: str,
        workspace: Path,
        brief: Dict[str, Any],
        *,
        scope: Optional[str] = None,
        readiness: Optional[str] = None,
    ) -> None:
        """Compile ``PRD.md`` from FAST's brief and write it (N2).

        Called BEFORE the FRONTEND invocation, so the PRD is available to be
        handed to FRONTEND as guidance (N4). Feature-flagged and DISABLED by
        default: a no-op unless enabled, so the workspace is byte-identical to
        pre-N2.
        """
        if not self.requirement_creator_config.enabled:
            return

        with self.store.acquire_writer(project_id) as state:
            revision = state.revisions.source_revision
            requirements_version = state.revisions.requirements_version
            if not scope:
                scope = state.brief.get("scope")
            if not readiness:
                readiness = state.brief.get("readiness")

        try:
            prd_text = compile_prd(
                brief,
                project_id=project_id,
                source_revision=revision,
                requirements_version=requirements_version,
                generator_version=self.requirement_creator_config.generator_version,
                scope=scope,
                readiness=readiness,
            )
            prd, _ = store_documents(workspace, prd_text=prd_text)
        except (ValueError, OSError) as exc:
            self._record_document_failure(project_id, revision, exc, kind="prd")
            return

        # N9: emit the structured requirement artifact alongside the PRD.
        #
        # ORDER MATTERS. The PRD is written FIRST and the artifact SECOND. If the
        # process dies between the two writes the workspace holds a new PRD beside
        # a missing or stale artifact, which the consumer detects
        # (verify_artifact_matches_prd) and repairs by re-emitting from the brief.
        # The reverse order would leave a fresh artifact pointing at a stale PRD,
        # which is indistinguishable from a legitimate build.
        #
        # document_store guarantees atomicity PER FILE only (temp + os.replace);
        # store_documents writes each kind with its own replace, so this pair is
        # NOT a cross-file transaction. Recovery is detect-and-rewrite, and the
        # detection is what makes that safe.
        try:
            artifact = build_artifact_from_brief(
                brief,
                project_id=project_id,
                source_revision=revision,
                requirements_version=requirements_version,
            )
            write_requirement_artifact(workspace, artifact)
        except (ValueError, OSError) as exc:
            # The PRD is already durable; a missing artifact only means Fit will
            # abstain, which is the honest outcome and never a build failure.
            logger.warning(
                "requirement artifact emission failed for %s: %s",
                project_id, type(exc).__name__,
            )

        self._record_documents(project_id, revision, prd=prd)

    def _prepare_prebuild_design(
        self,
        project_id: str,
        workspace: Path,
        brief: Dict[str, Any],
    ):
        """Prepare the pre-build design artefacts (N6-C + N6-D).

        Runs the Decision Engine's Design job once, then:

        * compiles and stores ``DesignGuidance.md`` (N6-C), and
        * builds the bounded D2/D3a design context pack (N6-D), reusing the
          existing selector + retrieval against the same evidence.

        Returns ``(guidance_text_or_None, pack_or_None)``. Both are ``None`` when
        design mode is not ACTIVE, so the initial build is byte-identical to
        pre-N6 in ``disabled``/``shadow``. A Decision Engine abstention or outage
        degrades to brief-only guidance (honestly UNRESOLVED) rather than failing
        the build; a pack/compile failure never fails the build.
        """
        if not self.decision_engine_config.design_active:
            return None, None
        if not self.requirement_creator_config.enabled:
            # The guidance is a canonical document; without the Requirement
            # Creator enabled there is no document pipeline to carry it.
            return None, None

        with self.store.acquire_writer(project_id) as state:
            revision = state.revisions.source_revision
            guidance_version = state.revisions.design_guidance_version
            # N8 B5: the REQUIREMENT revision this guidance is scored against.
            # This is the brief's own revision counter -- NOT the OpenViking
            # reference revision -- so a suitability score can be tied to the
            # exact requirements it was judged against.
            requirements_version = state.revisions.requirements_version

        # V3-B: the retrieval query carries the project DOMAIN, the intended
        # AESTHETIC (the owner's own design-intent words), and the page type /
        # visual requirements — so retrieval is steered toward references that
        # match the requested look rather than a generic term. Application-owned
        # text only; nothing retrieved feeds back into it.
        from app.core.design_intent import retrieval_query as _retrieval_query

        query = _retrieval_query(brief)

        # N8 B7 calibration / N9: Fit must be judged against requirements the
        # native pipeline actually produced, never against a hardcoded list.
        #
        # The structured artifact is authoritative. If it is present and its
        # revision linkage matches this PRD, its screenshot-scorable entries are
        # used. If it is missing, stale, inconsistent or corrupt, the build does
        # NOT silently fall back to PRD bullets: those mix project requirements
        # with compiler boilerplate, and scoring Fit against boilerplate is the
        # exact failure this replaces. The fallback is abstention.
        #
        # Recovery: when the artifact is absent or mismatched, re-emit it from the
        # brief (idempotent -- the digest is deterministic) and use the result. A
        # crash between the PRD write and the artifact write is therefore repaired
        # on the next build rather than papered over.
        requirements: Tuple[str, ...] = ()
        try:
            from app.core.design_requirement_artifact import (
                REASON_OK,
                read_requirement_artifact,
                verify_artifact_matches_prd,
            )
            from app.core.design_requirements import requirements_for_fit
            from app.core.document_store import read_documents

            prd_doc, _design_doc = read_documents(workspace)
            prd_text = prd_doc.text if prd_doc is not None else ""

            artifact = read_requirement_artifact(workspace)
            needs_repair = artifact is None
            if artifact is not None and prd_text:
                linked, _why = verify_artifact_matches_prd(artifact, prd_text)
                needs_repair = not linked

            if needs_repair:
                artifact = build_artifact_from_brief(
                    brief,
                    project_id=project_id,
                    source_revision=revision,
                    requirements_version=requirements_version,
                )
                write_requirement_artifact(workspace, artifact)
                logger.info(
                    "requirement artifact re-emitted for %s (idempotent repair)",
                    project_id,
                )

            if artifact is not None and prd_text:
                linked, _why = verify_artifact_matches_prd(artifact, prd_text)
                if linked:
                    requirements, _reason = requirements_for_fit(workspace)
        except Exception:  # noqa: BLE001 - requirement lookup never breaks a build
            requirements = ()

        decision = None
        if self.decision_engine is not None:
            try:
                # N8 B5: retrieval happens ONCE, here, and the resulting
                # candidates are handed to whichever design provider is
                # configured. Before this, no caller populated "candidates", so
                # a Decisions Design provider abstained on every single call.
                candidates = self._retrieve_design_candidates(
                    query=query,
                    library_project_id=(
                        self.decision_engine_config.design_library_project_id
                    ),
                )
                decision = self.decision_engine.decide_design(
                    {
                        "query": query,
                        "design_dna": {},  # pre-build: no DNA exists yet
                        "source_revision": revision,
                        # The application-owned REVIEWED design-library scope,
                        # from configuration only — never from the brief or any
                        # user input, so a request can never redirect retrieval.
                        "library_project_id": (
                            self.decision_engine_config.design_library_project_id
                        ),
                        # N8 B5: one explicit candidate contract, shared by the
                        # native retrieval provider and the Decisions provider.
                        "candidates": candidates,
                        "brief": dict(brief),
                        # Ties requirement-fit to the exact brief revision.
                        "requirement_version": f"req-{requirements_version}",
                        # N8 B7 calibration: the native requirement set, read
                        # from this build's own PRD. Empty means "abstain on
                        # Fit", never "score Fit low".
                        "requirements": requirements,
                    }
                )
            except Exception:  # noqa: BLE001 - engine never breaks the build
                decision = None

        try:
            text = compile_design_guidance(
                brief,
                decision,
                project_id=project_id,
                source_revision=revision,
                design_guidance_version=guidance_version,
                generator_version=self.requirement_creator_config.generator_version,
            )
            stored = store_design_guidance(workspace, text)
        except (ValueError, OSError) as exc:
            self._record_document_failure(
                project_id, revision, exc, kind="design_guidance"
            )
            text = None
        else:
            self._record_documents(project_id, revision, design_guidance=stored)

        pack = self._build_design_context_pack(brief, decision, query)
        return text, pack

    def _build_design_context_pack(self, brief, decision, query):
        """Build the bounded D2/D3a design context pack (N6-D).

        Reuses the EXISTING selector + retrieval (``build_design_context_pack``)
        against the pre-build evidence (brief + the selected, provenance-validated
        candidate). The pack's ``design_dna`` input is the bounded evidence
        mapping, so the selector runs on exactly what the application supplied and
        never on anything it did not.

        Returns the pack, or ``None`` when it cannot be built (no hermes_home on
        the adapter, or any failure). A pack is an ENHANCEMENT: its absence never
        blocks or changes a build.
        """
        hermes_home = getattr(self.hermes_adapter, "hermes_home", None)
        if hermes_home is None:
            return None
        try:
            from app.core.design_context import build_design_context_pack
            from app.core.design_guidance import (
                guidance_evidence,
                guidance_selection_input,
            )

            evidence = guidance_evidence(brief, decision)
            selection_input = guidance_selection_input(evidence)
            return build_design_context_pack(
                hermes_home,
                design_dna=selection_input,
                query=query,
                include_guidance=True,
                retrieve=True,
                capability_report=self.design_capability_report,
            )
        except Exception:  # noqa: BLE001 - a pack failure never blocks a build
            return None

    def _invalidate_stale_design(self, project_id: str, workspace: Path) -> None:
        """Remove a stale post-build ``Design.md`` before the FRONTEND snapshot (N6-E).

        A workspace is REUSED across builds, and ``Design.md`` is compiled AFTER
        FRONTEND. On a rebuild the previous build's ``Design.md`` therefore still
        sits in the workspace; handed to FRONTEND it is recorded as a USED
        guidance input, and the post-FRONTEND recompile then changes its digest,
        producing a false ``GUIDANCE_VERSION_MISMATCH``. Removing it here — before
        the FRONTEND guidance snapshot is captured — restores the fresh-build
        invariant. Only ``Design.md`` is affected.

        Fail-safe: invalidation NEVER fails a build. A refusal (symlink, escape)
        or an OSError is logged and swallowed, exactly like the other document
        side-effects on this path, so a workspace problem degrades to the
        existing behaviour rather than losing the website.
        """
        try:
            removed = invalidate_design(workspace)
        except (ValueError, OSError):
            logger.warning(
                "N6-E: refusing to invalidate stale Design.md for %s; continuing",
                project_id,
                exc_info=True,
            )
            return
        if removed:
            logger.info(
                "N6-E: invalidated stale post-build Design.md for %s before FRONTEND",
                project_id,
            )

    def _retrieve_design_candidates(
        self,
        *,
        query: str,
        library_project_id: str,
    ) -> list:
        """Retrieve design candidates ONCE, for any configured design provider.

        N8 B5: this is the single retrieval seam. The Decisions Design provider
        does no retrieval of its own, so a build retrieves exactly once and every
        provider consumes the same list -- which is also what makes candidate ids
        and their provenance comparable across providers.

        Fail-safe: a retrieval problem yields an empty list. The engine then
        abstains honestly rather than failing the build, and a provider that
        receives no candidates returns ``()`` instead of inventing one.
        """
        provider = getattr(self.decision_engine, "provider", None)
        if provider is None:
            return []
        retrieve = getattr(provider, "propose_design_candidates", None)
        if not callable(retrieve):
            return []
        try:
            candidates = retrieve(
                {"query": query, "library_project_id": library_project_id},
                limit=self.decision_engine_config.max_candidates,
            )
        except Exception:  # noqa: BLE001 - retrieval never breaks a build
            logger.warning("design candidate retrieval failed", exc_info=True)
            return []
        return list(candidates or ())

    def _compile_and_store_design(
        self,
        project_id: str,
        workspace: Path,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]],
    ) -> None:
        """Compile ``Design.md`` from the accepted Design DNA and write it (N2).

        Called AFTER FRONTEND has produced the Design DNA, and BEFORE the checked
        source fingerprint is taken, so Design.md is part of the published bytes.
        Feature-flagged and DISABLED by default.
        """
        if not self.requirement_creator_config.enabled:
            return

        with self.store.acquire_writer(project_id) as state:
            revision = state.revisions.source_revision
            design_dna_version = state.revisions.design_dna_version

        try:
            design_text = compile_design(
                design_dna or {},
                project_id=project_id,
                source_revision=revision,
                design_dna_version=design_dna_version,
                generator_version=self.requirement_creator_config.generator_version,
                brief=brief,
            )
            _, design = store_documents(workspace, design_text=design_text)
        except (ValueError, OSError) as exc:
            self._record_document_failure(project_id, revision, exc, kind="design")
            return
        self._record_documents(
            project_id, revision, prd=read_document(workspace, "prd"), design=design
        )

    def _record_document_failure(
        self, project_id: str, revision: int, exc: Exception, *, kind: str
    ) -> None:
        """Record a compilation failure for an operator; never fail the build.

        The failure is merged into the existing per-revision record rather than
        replacing it, so a later successful compile of the *other* document can
        never silently erase a recorded failure (the N4 bug this method exists
        to prevent).
        """
        logger.warning(
            "Requirement Creator: %s document compilation for %s failed (%s); "
            "continuing without that document",
            kind,
            project_id,
            type(exc).__name__,
        )
        self._merge_document_record(
            project_id, revision, records=None, error_kind=kind, error_code=type(exc).__name__
        )

    def _record_documents(
        self, project_id: str, revision: int, *, prd=None, design=None,
        design_guidance=None,
    ) -> None:
        """Persist the document digest record into the existing deployment map.

        N6 adds the pre-build ``design_guidance`` document. It is recorded through
        the SAME per-kind merge as PRD/Design, so guidance evidence can never
        erase — or be erased by — the other documents.
        """
        records = document_records(prd, design)
        guidance_entry = (
            design_guidance.to_record() if design_guidance is not None else None
        )
        if guidance_entry is not None:
            if records is None:
                records = {"entries": [], "digests": {}}
            records["entries"] = list(records["entries"]) + [guidance_entry]
        self._merge_document_record(project_id, revision, records=records)

    def _merge_document_record(
        self,
        project_id: str,
        revision: int,
        *,
        records: Optional[Dict[str, Any]],
        error_kind: Optional[str] = None,
        error_code: Optional[str] = None,
    ) -> None:
        """Merge the current documents/errors into ``state.deployment['documents']``.

        Rules, all chosen so evidence is never lost:

        * A **new revision** resets the entry set — documents from an earlier
          revision are stale and must not be reported as this revision's.
        * A **successful** compile for a kind replaces that kind's entry and
          clears that kind's error (a genuine later fix).
        * A **failure** records that kind's error and never removes an entry for
          another kind, so PRD and Design evidence are independent.
        """
        with self.store.acquire_writer(project_id) as state:
            existing = state.deployment.get(DEPLOYMENT_DOCUMENTS_KEY)
            existing = existing if isinstance(existing, dict) else {}
            if existing.get("source_revision") != revision:
                # A new revision: prior entries belong to a different source.
                existing = {}

            entries = {
                e.get("kind"): e
                for e in (existing.get("entries") or [])
                if isinstance(e, dict) and e.get("kind")
            }
            errors = dict(existing.get("errors") or {})

            if records:
                for entry in records.get("entries") or []:
                    kind = entry.get("kind")
                    entries[kind] = entry
                    errors.pop(kind, None)
            if error_kind is not None:
                errors[error_kind] = error_code

            if not entries and not errors:
                state.deployment.pop(DEPLOYMENT_DOCUMENTS_KEY, None)
                self.store.save(state)
                return

            merged: Dict[str, Any] = {
                "entries": [entries[k] for k in sorted(entries)],
                "digests": {k: entries[k]["digest"] for k in sorted(entries)},
                "errors": errors,
                "source_revision": revision,
            }
            if errors:
                # Back-compat: the first failing kind's code, so an operator
                # reading only `error` still learns a compile failed.
                merged["error"] = errors[sorted(errors)[0]]
            state.deployment[DEPLOYMENT_DOCUMENTS_KEY] = merged
            self.store.save(state)

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
  components.json, package.json, package-lock.json, tsconfig.json,
  tsconfig.app.json, tsconfig.node.json, vite.config.ts.
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
        frontend_budget=None,
    ) -> Dict[str, Any]:
        """Invoke FRONTEND exactly once for the minimum compile fix.

        Returns {"executed": bool, "toolchain_violation": Optional[str],
        "design_dna": Optional[dict], "error": Optional[str],
        "error_code": Optional[str], "invocation": Optional[dict]}:
        - executed=True only when the repair call itself succeeded and any
          returned Design DNA passed the composed-policy validation. The
          deterministic checks are ALWAYS the authority after this; the
          model's success claim is never trusted.
        - toolchain_violation is set when a protected starter/toolchain
          identity file was mutated/removed during the repair — a
          deterministic TOOLCHAIN_MUTATION_REJECTED policy rejection.
        - error/error_code/invocation carry the BOUNDED, sanitized reason a
          non-executed repair failed, so the answer survives in persisted
          state instead of being dropped. They are None when the repair was
          never invoked (no adapter) or was rejected by toolchain policy.
        """
        if self.hermes_adapter is None:
            return {"executed": False, "toolchain_violation": None,
                    "design_dna": None, "error": None, "error_code": None,
                    "invocation": None}

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
            frontend_budget=frontend_budget,
            invocation_stage="compile_repair",
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
            return {"executed": False, "toolchain_violation": violation,
                    "design_dna": None, "error": None, "error_code": None,
                    "invocation": None}

        if not repair_result.get("success"):
            return {"executed": False, "toolchain_violation": None,
                    "design_dna": None, **_frontend_failure_reason(repair_result)}

        dna = repair_result.get("design_dna")
        if dna is None:
            # The repair did not return/persist a readable Design DNA —
            # fine: keep the generation-time DNA and let the deterministic
            # checks judge the workspace.
            return {"executed": True, "toolchain_violation": None,
                    "design_dna": None, "error": None, "error_code": None,
                    "invocation": None}

        try:
            validate_composed_dna(dna, policy_state)
        except ValueError as exc:
            logger.warning(
                "Phase-7 compile repair of %s returned invalid Design DNA",
                project_id,
            )
            return {"executed": False, "toolchain_violation": None,
                    "design_dna": None,
                    **_frontend_failure_reason({"error": str(exc)})}

        with self.store.acquire_writer(project_id) as locked:
            locked.design_dna = dna
            locked.revisions.design_dna_version = dna.get(
                "version", locked.revisions.design_dna_version + 1
            )
            self.store.save(locked)

        return {"executed": True, "toolchain_violation": None, "design_dna": dna,
                "error": None, "error_code": None, "invocation": None}

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

            # V4-B2: ONE durable FRONTEND budget for this whole build operation,
            # shared by the initial generation, the Phase-7 compile repair, and
            # every Phase-8 repair (deterministic QA, critic, design-fidelity).
            # ``None`` when no global cap is configured (production default), in
            # which case every frontend_build call below is unchanged.
            from app.core.frontend_budget import budget_for_operation

            frontend_budget = budget_for_operation(
                self.store,
                project_id,
                operation_token=f"build-r{frontend_operation_id}",
                limit=self.frontend_invocation_limit,
            )

            workspace = self.runner.create_workspace(project_id)
            self._copy_starter(workspace)
            toolchain_hashes = _capture_toolchain_hashes(workspace)

            # Use Hermes FRONTEND to derive design and build
            if self.hermes_adapter is not None:
                # N6-E: a REUSED workspace still holds the PREVIOUS build's
                # post-build ``Design.md``. Design.md is compiled AFTER FRONTEND
                # (see ``_compile_and_store_design`` below), so that stale copy
                # would be handed to FRONTEND and recorded as a USED guidance
                # input, and the post-FRONTEND recompile would then change its
                # digest — a false GUIDANCE_VERSION_MISMATCH. Invalidate it here,
                # at the build boundary and BEFORE the FRONTEND guidance snapshot
                # is captured, restoring the fresh-build invariant (Design.md
                # absent at the initial invocation). Only Design.md is removed;
                # PRD.md, DesignGuidance.md, design-dna.json, src/ and QA
                # evidence are untouched. A fresh workspace is a no-op.
                if self.requirement_creator_config.enabled:
                    self._invalidate_stale_design(project_id, workspace)
                # N4: compile the PRD from FAST's brief BEFORE FRONTEND, so the
                # validated requirement document can be handed to FRONTEND as
                # bounded guidance. No-op unless the Requirement Creator is
                # enabled; a compile failure never fails the build.
                self._compile_and_store_prd(project_id, workspace, brief)
                # N6: prepare the pre-build design artefacts — DesignGuidance.md
                # (N6-C) and the bounded D2/D3a design context pack (N6-D) — from
                # the brief plus the Decision Engine's selected,
                # provenance-validated candidates. ACTIVE design mode only; a
                # no-op in disabled/shadow, and a Decision Engine
                # abstention/outage degrades to brief-only guidance (honestly
                # UNRESOLVED) rather than failing the build.
                _prebuild_guidance, _design_pack = self._prepare_prebuild_design(
                    project_id, workspace, brief
                )
                # N4/N6: the adapter derives the bounded PRD/DesignGuidance
                # guidance from the workspace itself, so the documents written
                # just above reach FRONTEND with no extra plumbing. Design.md is
                # compiled AFTER FRONTEND has produced the Design DNA and
                # therefore is not available to the initial build's own FRONTEND
                # call — a real, documented ordering constraint, not an omission.
                frontend_result = self.hermes_adapter.frontend_build(
                    project_id=project_id,
                    brief=brief,
                    workspace=workspace,
                    design_dna_instructions=combined_instructions,
                    build_operation_id=frontend_operation_id,
                    design_context=_design_pack,
                    frontend_budget=frontend_budget,
                    invocation_stage="initial_generation",
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
                    # A result that merely *has* an ``error`` key is not
                    # enough: a blank/``None`` value is persisted verbatim as
                    # ``state.failure["error"]`` and would tell an operator
                    # nothing. Fall through the adapter's own classification
                    # before the fixed text, which also removes a latent
                    # ``TypeError`` in the substring test below.
                    err_text = (
                        frontend_result.get("error")
                        or frontend_result.get("error_code")
                        or "Unknown FRONTEND error"
                    )
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
            # N2/N4: compile Design.md from the accepted Design DNA and write it
            # BEFORE the source fingerprint is taken, so Design.md is part of the
            # checked and (later) published bytes. No-op unless enabled.
            self._compile_and_store_design(project_id, workspace, brief, design_dna)
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
            # Bounded, sanitized reason the repair invocation itself failed.
            # Persisted so the answer is recoverable from state; the reason was
            # previously dropped, leaving `repair_execution_failed` undiagnosable.
            repair_failure: Optional[Dict[str, Any]] = None

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
                        frontend_budget=frontend_budget,
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
                        repair_failure = {
                            "error": repair_outcome.get("error"),
                            "error_code": repair_outcome.get("error_code"),
                        }
                        if repair_outcome.get("invocation"):
                            repair_failure["invocation"] = repair_outcome["invocation"]
                        logger.warning(
                            "Phase-7 compile repair for %s did not execute "
                            "(error_code=%s); preserving initial cheap-check failure",
                            project_id,
                            repair_failure["error_code"],
                        )

            # Update state with results
            with self.store.acquire_writer(project_id) as state:
                if build_success:
                    # Successful Phase 7 keeps lifecycle RUNNING. Only Phase 8
                    # may advance RUNNING -> PREVIEW_READY.
                    state.revisions.design_dna_version = design_dna.get("version", 1) if design_dna else 0
                    state.design_dna = design_dna or {}
                    # N4: record the guidance digest FRONTEND was given, so QA
                    # and any later reader can prove they evaluate the same
                    # document version. Absent when there are no documents.
                    _guidance_block, guidance_digest = guidance_for_workspace(workspace)
                    if guidance_digest:
                        state.deployment["guidance_digest"] = guidance_digest
                    else:
                        state.deployment.pop("guidance_digest", None)
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
                    # Why the repair invocation itself failed, bounded and
                    # sanitized. Only the compile-repair path persists it; the
                    # QA-repair and revision paths keep their own reporting.
                    if repair_failure is not None:
                        failure["repair"] = repair_failure
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
                # D3b: the bounded Impeccable critic repair stage.
                critic_scanner=self.critic_scanner,
                # V4-A: the bounded design-fidelity gate (default off).
                design_fidelity_enabled=self.design_fidelity_enabled,
                # V4-B2: the SAME per-operation budget as the initial build, so
                # the QA / critic / fidelity repairs share the global cap.
                frontend_budget=frontend_budget,
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
