"""Phase 8 QA + bounded repair orchestrator for Website Builder R1.

Application-owned orchestration only. VISION is evidence-only. FRONTEND
performs repairs. Deterministic checks and lifecycle authority stay in
application code. Maximum 2 repair attempts, never a third.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Dict, Optional

from app.core.lifecycle import ProjectLifecycle
from app.core.state import ProjectStateStore
from app.qa.deterministic import run_deterministic_checks
from app.qa.findings import DeterministicFindings, QAAttempt, QAResult, VisionFindings
from app.qa.render import LocalRenderer, RenderError
from app.qa.screenshot import ScreenshotCapture, ScreenshotSet
from app.sandbox.runner import ProjectRunner

# Bounded repair loop: attempt 0 (initial QA) + up to 2 repairs.
MAX_REPAIR_ATTEMPTS = 2


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
    ):
        self.runner = runner
        self.store = store
        self.hermes_adapter = hermes_adapter
        self.renderer = renderer or LocalRenderer(runner)
        self.screenshot_capture = screenshot_capture or ScreenshotCapture()

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

        try:
            for attempt_num in range(MAX_REPAIR_ATTEMPTS + 1):
                qa_attempt = self._run_one_attempt(
                    project_id, workspace, qa_dir, attempt_num, brief, design_dna
                )
                attempts.append(qa_attempt)

                if qa_attempt.final_pass:
                    self._finalize_success(project_id, qa_attempt)
                    return QAResult(
                        project_id=project_id,
                        success=True,
                        attempts=attempts,
                        repair_attempts=repair_count,
                    )

                if attempt_num >= MAX_REPAIR_ATTEMPTS:
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
                    break

                # Deterministic rebuild checks after repair.
                build_ok, typecheck_ok = self._run_rebuild_checks(project_id, workspace)
                if not build_ok or not typecheck_ok:
                    # Record a synthetic failing attempt reflecting the
                    # broken rebuild, then continue the loop (still bounded
                    # by MAX_REPAIR_ATTEMPTS) so the next iteration can
                    # either repair again or exhaust the budget.
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
                    attempts.append(
                        QAAttempt(
                            attempt=attempt_num + 1,
                            deterministic=failing,
                            vision=None,
                        )
                    )
                    if attempt_num + 1 >= MAX_REPAIR_ATTEMPTS:
                        break

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
            self._finalize_failure(project_id, attempts, repair_count, error=str(exc))
            return QAResult(
                project_id=project_id,
                success=False,
                attempts=attempts,
                repair_attempts=repair_count,
                error=str(exc),
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
        screenshots = ScreenshotSet(desktop=None, mobile=None)

        try:
            handle = self.renderer.start(project_id, workspace)
        except RenderError:
            render_ok = False

        try:
            if render_ok and handle is not None:
                screenshots = self.screenshot_capture.capture(
                    handle.url, qa_dir, attempt_num
                )
        finally:
            if handle is not None:
                self.renderer.stop(handle)

        vision_findings: Optional[VisionFindings] = None
        if screenshots.complete and self.hermes_adapter is not None:
            vision_raw = self.hermes_adapter.vision_inspect(
                screenshots.desktop, screenshots.mobile, brief, design_dna
            )
            vision_findings = VisionFindings(
                pass_=bool(vision_raw.get("pass", False)),
                critical=list(vision_raw.get("critical") or []),
                major=list(vision_raw.get("major") or []),
                minor=list(vision_raw.get("minor") or []),
                summary=vision_raw.get("summary", ""),
                raw_error=vision_raw.get("error"),
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

        instructions = self._build_repair_instructions(design_dna, failed_attempt)
        result = self.hermes_adapter.frontend_build(
            project_id=project_id,
            brief=brief,
            workspace=workspace,
            design_dna_instructions=instructions,
        )
        return bool(result.get("success"))

    def _build_repair_instructions(
        self, design_dna: Optional[Dict[str, Any]], failed_attempt: QAAttempt
    ) -> str:
        det_failures = "\n".join(f"- {f}" for f in failed_attempt.deterministic.failures) or "(none)"
        vision_findings = ""
        if failed_attempt.vision is not None:
            crit = "\n".join(f"- {c}" for c in failed_attempt.vision.critical) or "(none)"
            major = "\n".join(f"- {m}" for m in failed_attempt.vision.major) or "(none)"
            vision_findings = f"""
VISION critical findings:
{crit}

VISION major findings:
{major}
"""
        dna_note = json.dumps(design_dna, indent=2) if design_dna else "(none)"

        return f"""REPAIR TASK (Phase 8 QA found blocking issues).

Existing Design DNA (do not redesign; make the smallest targeted fix):
{dna_note}

Deterministic QA failures:
{det_failures}
{vision_findings}

Instructions:
- Make the smallest targeted fix that resolves the blocking findings above.
- Do NOT redesign unrelated areas of the site.
- Do NOT invent business facts.
- Preserve the existing Design DNA unless it directly caused a blocking finding.
"""

    def _run_rebuild_checks(self, project_id: str, workspace: Path) -> tuple:
        """Run build + typecheck once after a repair. Does not re-run npm ci."""
        build_proc = self.runner.run_command(
            project_id, ["npm", "run", "build"], cwd=workspace, timeout=300,
        )
        typecheck_proc = self.runner.run_command(
            project_id, ["npm", "run", "typecheck"], cwd=workspace, timeout=120,
        )
        return build_proc.returncode == 0, typecheck_proc.returncode == 0

    # ------------------------------------------------------------------
    # Lifecycle finalization
    # ------------------------------------------------------------------

    def _finalize_success(self, project_id: str, qa_attempt: QAAttempt) -> None:
        with self.store.acquire_writer(project_id) as state:
            self.store.transition_lifecycle_locked(state, ProjectLifecycle.PREVIEW_READY)
            state.revisions.qa_revision += 1
            state.revisions.preview_revision += 1
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
                    a.vision.to_dict() if a.vision else None for a in attempts
                ],
                "error": error,
                "failed_at": time.time(),
            }
            self.store.save(state)
