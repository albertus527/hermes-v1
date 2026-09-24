"""Phase 8 QA + bounded repair orchestrator for Website Builder R1.

Application-owned orchestration only. VISION is evidence-only. FRONTEND
performs repairs. Deterministic checks and lifecycle authority stay in
application code. Maximum 2 repair attempts, never a third.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

from app.deploy.snapshot import TestedSnapshot, source_fingerprint, record_checks
from app.core.lifecycle import ProjectLifecycle
from app.core.composition import compose_project_instructions, validate_composed_dna, invalidate_artifact
from app.core.selfcontained import (
    EXTERNAL_RUNTIME_DEPENDENCY,
    normalize_and_check_self_contained,
)
from app.core.state import ProjectStateStore
from app.qa.deterministic import run_deterministic_checks
from app.qa.findings import DeterministicFindings, QAAttempt, QAResult, VisionFindings
from app.qa.render import LocalRenderer, RenderError
from app.qa.screenshot import (
    ScreenshotCapture,
    ScreenshotSet,
    validate_screenshot_dimensions,
)
from app.sandbox.runner import ProjectRunner

# Bounded repair loop: attempt 0 (initial QA) + up to 2 repairs.
MAX_REPAIR_ATTEMPTS = 2


class _ReferenceSnapshot:
    """Immutable view of ``state.design_references`` captured under the lock.

    ``validate_composed_dna`` only reads ``.design_references`` from its state
    argument. Passing the live ``ProjectState`` after the writer lock has been
    released validates against whatever is current at call time rather than
    the reference set the repair instructions were composed against.
    """

    __slots__ = ("design_references",)

    def __init__(self, design_references):
        # design_references is a role -> record mapping; shallow-copy it so
        # later mutation of the live state cannot change the validated set.
        self.design_references = dict(design_references or {})


class QAOrchestrator:
    """Phase 8 orchestrator: render, screenshot, VISION, deterministic QA,
    bounded FRONTEND repair, final verification.
    """

    def __init__(
        self,
        runner: ProjectRunner,
        store: ProjectStateStore,
        hermes_adapter=None,
        renderer: Optional[LocalRenderer] = None,
        screenshot_capture: Optional[ScreenshotCapture] = None,
        web3forms_access_key: Optional[str] = None,
        toolchain_verify=None,
    ):
        self.runner = runner
        self.store = store
        self.hermes_adapter = hermes_adapter
        self.web3forms_access_key = web3forms_access_key
        self.renderer = renderer or LocalRenderer(runner)
        self.screenshot_capture = screenshot_capture or ScreenshotCapture()
        # MEDIUM-5: optional callable(workspace) -> Optional[str] verifying
        # protected starter/toolchain files after every FRONTEND repair.
        self._toolchain_verify = toolchain_verify
        self._toolchain_error: Optional[str] = None
        # Latest self-contained report produced by a post-repair rebuild; read
        # by the repair loop to attach an explicit, FRONTEND-actionable
        # blocking finding. Never persisted as evidence (the artifact itself
        # is the evidence).
        self._self_contained_report = None

    # ------------------------------------------------------------------
    # Public entry point
    # ------------------------------------------------------------------

    def run(
        self,
        project_id: str,
        workspace: Path,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]] = None,
    ) -> QAResult:
        """Run Phase 8 QA + bounded repair for a project in RUNNING state.

        Transitions RUNNING -> PREVIEW_READY on final pass, or
        RUNNING -> FAILED when the repair budget is exhausted without a
        passing QA attempt.
        """
        qa_dir = workspace / "qa"
        attempts: list = []
        repair_count = 0
        self._toolchain_error = None

        # Monotonic attempt counter: every entry appended to ``attempts``
        # (whether a real QA pass or a synthetic failing entry recording a
        # broken post-repair rebuild) consumes the next sequential number.
        # This guarantees no two attempts ever share a number — the prior
        # scheme reused a repair's number for both the synthetic rebuild-
        # failure entry AND the following retry's fresh QA attempt, which
        # produced duplicate ``attempt`` values.
        next_attempt_num = 0

        try:
            rebuild_failure = None
            while True:
                # Never render stale dist after a failed rebuild. Feed the
                # deterministic failure directly into the next bounded repair.
                qa_attempt = rebuild_failure or self._run_one_attempt(
                    project_id, workspace, qa_dir, next_attempt_num, brief, design_dna
                )
                rebuild_failure = None
                next_attempt_num += 1
                attempts.append(qa_attempt)

                if qa_attempt.infrastructure_error:
                    # Infrastructure error (render start, browser capture, or VISION provider failure).
                    # This is NOT a defect produced by FRONTEND — do NOT consume repair attempts
                    # or mutate source code.
                    self._finalize_failure(
                        project_id, attempts, repair_count, error=qa_attempt.infrastructure_error
                    )
                    return QAResult(
                        project_id=project_id,
                        success=False,
                        attempts=attempts,
                        repair_attempts=repair_count,
                        error=qa_attempt.infrastructure_error,
                    )

                if qa_attempt.final_pass:
                    state = self.store.load(project_id)
                    checked = state.deployment.get('checked') or {}
                    if checked:
                        snapshot = TestedSnapshot.capture(workspace, checked['source_sha256'],
                                                          checked['artifact_sha256'])
                        if checked['source_revision'] != state.revisions.source_revision:
                            raise ValueError('STALE_QA_BINDING')
                        if qa_attempt.vision is None or qa_attempt.vision.pass_ is not True:
                            raise ValueError('VISION_REQUIRED')
                        with self.store.acquire_writer(project_id) as locked:
                            locked.deployment['tested_snapshot'] = snapshot.to_dict()
                            self.store.save(locked)
                    self._finalize_success(project_id, qa_attempt)
                    return QAResult(
                        project_id=project_id,
                        success=True,
                        attempts=attempts,
                        repair_attempts=repair_count,
                    )

                if repair_count >= MAX_REPAIR_ATTEMPTS:
                    # Repair budget exhausted — no third repair.
                    break

                # Repair required and budget remains — invoke FRONTEND.
                repair_count += 1
                repaired = self._repair(
                    project_id, workspace, brief, design_dna, qa_attempt
                )
                if not repaired:
                    # Repair itself failed to execute; stop the loop and
                    # fail with what we have rather than silently retrying.
                    if self._toolchain_error is not None:
                        # MEDIUM-5: a protected toolchain mutation during a
                        # repair is a deterministic policy rejection that must
                        # surface as TOOLCHAIN_MUTATION_REJECTED — never as a
                        # generic "budget exhausted" error.
                        self._finalize_failure(
                            project_id, attempts, repair_count,
                            error=self._toolchain_error,
                        )
                        return QAResult(
                            project_id=project_id,
                            success=False,
                            attempts=attempts,
                            repair_attempts=repair_count,
                            error=self._toolchain_error,
                        )
                    break

                design_dna = self.store.load(project_id).design_dna

                # Deterministic rebuild checks after repair.
                build_ok, typecheck_ok, self_contained_ok = self._run_rebuild_checks(
                    project_id, workspace)
                if not build_ok or not typecheck_ok or not self_contained_ok:
                    # Record a synthetic failing attempt reflecting the
                    # broken rebuild. A failed deterministic rebuild is
                    # metadata about this repair, not a fresh QA pass — but
                    # it still consumes the next sequential attempt number
                    # so the history stays strictly increasing and never
                    # corrupts the repair budget or attempt numbering.
                    failing = DeterministicFindings(
                        render_ok=False,
                        desktop_screenshot_ok=False,
                        mobile_screenshot_ok=False,
                        design_dna_valid=False,
                        source_present=False,
                        build_ok=build_ok,
                        typecheck_ok=typecheck_ok,
                    )
                    if not build_ok:
                        failing.failures.append("npm run build failed after repair")
                    if not typecheck_ok:
                        failing.failures.append("npm run typecheck failed after repair")
                    if build_ok and typecheck_ok and not self_contained_ok:
                        report = self._self_contained_report
                        failing.failures.append(
                            "External runtime dependency remained after repair:\n"
                            + (report.error_text() if report is not None else EXTERNAL_RUNTIME_DEPENDENCY)
                        )
                    rebuild_failure = QAAttempt(
                        attempt=next_attempt_num,
                        deterministic=failing,
                        vision=None,
                    )
                    continue

            # Exhausted repair budget without a passing attempt -> FAILED.
            self._finalize_failure(project_id, attempts, repair_count)
            return QAResult(
                project_id=project_id,
                success=False,
                attempts=attempts,
                repair_attempts=repair_count,
                error="QA blocking findings remained after repair budget exhausted",
            )

        except Exception as exc:
            # MEDIUM-3: log full detail operator-side; the QAResult and the
            # persisted failure carry a stable, sanitized application code.
            logger.exception("Unexpected error during Phase 8 QA for %s", project_id)
            self._finalize_failure(project_id, attempts, repair_count, error="UNEXPECTED_QA_ERROR")
            return QAResult(
                project_id=project_id,
                success=False,
                attempts=attempts,
                repair_attempts=repair_count,
                error="UNEXPECTED_QA_ERROR",
            )

    # ------------------------------------------------------------------
    # One QA attempt: render -> screenshots -> VISION -> deterministic
    # ------------------------------------------------------------------

    def _run_one_attempt(
        self,
        project_id: str,
        workspace: Path,
        qa_dir: Path,
        attempt_num: int,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]],
    ) -> QAAttempt:
        render_ok = True
        handle = None
        infra_error: Optional[str] = None
        screenshots = ScreenshotSet(desktop=None, mobile=None)

        try:
            handle = self.renderer.start(project_id, workspace)
        except RenderError as exc:
            render_ok = False
            infra_error = f"INFRASTRUCTURE_ERROR:render_failed:{exc}"

        try:
            if render_ok and handle is not None:
                screenshots = self.screenshot_capture.capture(
                    handle.url, qa_dir, attempt_num
                )
        except Exception as exc:
            infra_error = f"INFRASTRUCTURE_ERROR:capture_failed:{exc}"
        finally:
            if handle is not None:
                self.renderer.stop(handle)

        # Evidence-integrity guard: verify the actual PNG pixel dimensions
        # match the intended viewports BEFORE VISION consumes the evidence.
        # Wrong-dimension evidence (e.g. the browser's default 1280px width
        # when the viewport command silently failed) is a capture
        # infrastructure error, not a visual QA finding — it must never
        # reach VISION or consume a FRONTEND repair attempt.
        if not infra_error and render_ok:
            try:
                validate_screenshot_dimensions(screenshots)
            except Exception as exc:
                infra_error = f"INFRASTRUCTURE_ERROR:capture_failed:{exc}"

        vision_findings: Optional[VisionFindings] = None
        if not infra_error and screenshots.complete and self.hermes_adapter is not None:
            try:
                vision_raw = self.hermes_adapter.vision_inspect(
                    screenshots.desktop, screenshots.mobile, brief, design_dna
                )
            except Exception as exc:
                infra_error = f"INFRASTRUCTURE_ERROR:vision_failed:{exc}"
                vision_raw = {"error": str(exc)}

            if not infra_error:
                raw_err = vision_raw.get("error")
                if raw_err:
                    infra_error = f"INFRASTRUCTURE_ERROR:vision_failed:{raw_err}"
                # Application-owned pass semantics: VISION passes iff it
                # reported no blocking findings and VISION itself ran.
                # Subjective observations never affect pass/fail and never
                # consume the repair budget.
                blocking_findings = list(vision_raw.get("blocking") or [])
                vision_findings = VisionFindings(
                    pass_=not blocking_findings and not raw_err,
                    blocking_findings=blocking_findings,
                    observations=list(vision_raw.get("observations") or []),
                    summary=vision_raw.get("summary", ""),
                    raw_error=raw_err,
                )

        # Build/typecheck are already known-good from Phase 7 on attempt 0;
        # subsequent attempts re-verify via _run_rebuild_checks before this
        # method is called again, so we treat them as passing here unless
        # this is the very first attempt post-repair path (handled by the
        # caller recording a synthetic failing attempt instead).
        deterministic = run_deterministic_checks(
            workspace,
            screenshots,
            render_ok=render_ok,
            build_ok=True,
            typecheck_ok=True,
        )

        return QAAttempt(
            attempt=attempt_num,
            deterministic=deterministic,
            vision=vision_findings,
            desktop_screenshot=str(screenshots.desktop) if screenshots.desktop else None,
            mobile_screenshot=str(screenshots.mobile) if screenshots.mobile else None,
            infrastructure_error=infra_error,
        )

    # ------------------------------------------------------------------
    # Repair
    # ------------------------------------------------------------------

    def _repair(
        self,
        project_id: str,
        workspace: Path,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]],
        failed_attempt: QAAttempt,
    ) -> bool:
        """Invoke FRONTEND to perform the smallest targeted fix.

        Returns True if the repair call itself succeeded (regardless of
        whether the resulting rebuild passes — that is checked separately).
        """
        if self.hermes_adapter is None:
            return False

        # Invalidate before invoking a mutating tool: even a failed or timed
        # out invocation may have changed source on disk.
        with self.store.acquire_writer(project_id) as state:
            instructions = compose_project_instructions(
                state, access_key=self.web3forms_access_key,
                task=self._build_repair_instructions(state.design_dna or design_dna, failed_attempt),
            )
            brief = dict(state.brief)
            # Snapshot the reference set UNDER the lock. validate_composed_dna
            # (below, after the FRONTEND call) must validate the new DNA
            # against the SAME design_references that were current when the
            # repair instructions were composed — validating against a
            # post-lock-release, possibly-mutated `state` object would
            # validate against stale references.
            design_references = dict(state.design_references or {})
            state.revisions.source_revision += 1
            invalidate_artifact(state)
            self.store.save(state)
        result = self.hermes_adapter.frontend_build(
            project_id=project_id,
            brief=brief,
            workspace=workspace,
            design_dna_instructions=instructions,
        )

        # MEDIUM-5: verify protected starter/toolchain files after EVERY
        # FRONTEND repair invocation — regardless of the repair's own result.
        if self._toolchain_verify is not None:
            violation = self._toolchain_verify(workspace)
            if violation:
                logger.warning(
                    "Protected toolchain file mutated during QA repair of %s: %s",
                    project_id,
                    violation,
                )
                self._toolchain_error = f"TOOLCHAIN_MUTATION_REJECTED ({violation})"
                return False

        if not result.get("success"):
            return False
        dna = result.get("design_dna")
        try:
            validate_composed_dna(dna, _ReferenceSnapshot(design_references))
        except ValueError:
            return False
        with self.store.acquire_writer(project_id) as locked:
            locked.design_dna = dna
            locked.revisions.design_dna_version = dna.get("version", locked.revisions.design_dna_version + 1)
            invalidate_artifact(locked)
            self.store.save(locked)
        return True

    def _build_repair_instructions(
        self, design_dna: Optional[Dict[str, Any]], failed_attempt: QAAttempt
    ) -> str:
        det_failures = "\n".join(f"- {f}" for f in failed_attempt.deterministic.failures) or "(none)"
        vision_blockers = ""
        if failed_attempt.vision is not None:
            # Only BLOCKING findings are repair requirements. Subjective
            # VISION observations are deliberately excluded — they are not
            # mandatory and must not drive changes.
            blockers = "\n".join(f"- {b}" for b in failed_attempt.vision.blocking_findings) or "(none)"
            vision_blockers = f"""
VISION blocking findings (concrete visible defects / explicit requirement violations):
{blockers}
"""
        dna_note = json.dumps(design_dna, indent=2) if design_dna else "(none)"

        return f"""REPAIR TASK (Phase 8 QA found blocking issues).

Existing Design DNA (do not redesign; make the smallest targeted fix):
{dna_note}

Deterministic QA failures:
{det_failures}
{vision_blockers}

Instructions:
- Make the smallest targeted fix that resolves the blocking findings above.
- Do NOT redesign unrelated areas of the site.
- Do NOT invent business facts.
- Preserve the existing Design DNA unless it directly caused a blocking finding.
- Only the blocking findings above are mandatory. VISION also reviews the
  site subjectively, but those observations are NOT part of this repair task
  and must not drive changes.
"""

    def _run_rebuild_checks(self, project_id: str, workspace: Path) -> tuple:
        """Run build + typecheck once after a repair. Does not re-run npm ci.

        ``self_contained`` holds a THIRD, independent outcome: the same
        build-time artifact gate Phase 7 applies. A repair is free to fix a
        blocking finding any way it likes, but it must not leave the artifact
        with an unsupported external runtime dependency. ``record_checks`` (and
        therefore the ``checked`` binding QA/preview trust) only happens when
        all three are clean.
        """
        self_contained = True
        before = source_fingerprint(workspace)
        build_proc = self.runner.run_command(
            project_id, ["npm", "run", "build"], cwd=workspace, timeout=300,
        )
        typecheck_proc = self.runner.run_command(
            project_id, ["npm", "run", "typecheck"], cwd=workspace, timeout=120,
        )
        if build_proc.returncode == 0 and typecheck_proc.returncode == 0:
            # Normalize supported external dependencies into local assets,
            # then reject whatever unsupported external runtime dependency the
            # repair left behind. The report is carried on the orchestrator so
            # the caller can turn it into an explicit blocking finding instead
            # of an opaque "npm run build failed after repair".
            report = normalize_and_check_self_contained(project_id, workspace)
            self._self_contained_report = report
            self_contained = report.ok
            if self_contained:
                before = source_fingerprint(workspace)
                record_checks(self.store, project_id, workspace, before)
        return build_proc.returncode == 0, typecheck_proc.returncode == 0, self_contained

    # ------------------------------------------------------------------
    # Lifecycle finalization
    # ------------------------------------------------------------------

    def _finalize_success(self, project_id: str, qa_attempt: QAAttempt) -> None:
        with self.store.acquire_writer(project_id) as state:
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.PREVIEW_READY)
            state.revisions.qa_revision = state.revisions.source_revision
            # External preview identity belongs to Phase 9, not local QA.
            state.revisions.preview_revision = 0
            state.deployment["qa"] = qa_attempt.to_dict()
            self.store.save(state)

    def _finalize_failure(
        self,
        project_id: str,
        attempts: list,
        repair_attempts: int,
        error: Optional[str] = None,
    ) -> None:
        with self.store.acquire_writer(project_id) as state:
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.FAILED)
            state.failure = {
                "phase": "qa",
                "repair_attempts": repair_attempts,
                "deterministic_findings": [a.deterministic.to_dict() for a in attempts],
                "vision_findings": [
                    a.vision.to_dict() for a in attempts if a.vision is not None
                ],
                "error": error,
                "failed_at": time.time(),
            }
            self.store.save(state)
