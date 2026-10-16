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
from app.core.composition import (
    ReferenceSnapshot,
    compose_project_instructions,
    invalidate_artifact,
    validate_composed_dna,
)
from app.core.selfcontained import (
    EXTERNAL_RUNTIME_DEPENDENCY,
    normalize_and_check_self_contained,
)
from app.core.state import ProjectStateStore
from app.qa.critic_stage import CriticStage
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


class QABlockingError(Exception):
    """A deliberate, named QA precondition rejected this run.

    Distinct from an unexpected exception: the code is already the correct
    user-facing action ("the preview is outdated", "vision did not run"), so
    it must survive to ``QAResult.error`` and to the persisted failure record
    instead of being erased into the opaque ``UNEXPECTED_QA_ERROR``.
    """

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


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
        critic_scanner=None,
        design_fidelity_enabled: bool = False,
        frontend_budget=None,
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
        # D3b: the production Impeccable critic scanner. When None, the critic
        # stage records an explicit NOT_RUN degraded outcome instead of
        # pretending a scan happened.
        self._critic_scanner = critic_scanner
        # Set by the critic stage so the caller can persist an honest degraded
        # record instead of claiming full verification.
        self._critic_stage_result = None
        # V4-A: the runtime design-fidelity gate switch. FALSE (the DEFAULT)
        # keeps the QA path byte-identical to pre-V4-A -- the gate never runs,
        # no extra read, no repair, no model call. TRUE (staging E2E) enables
        # the bounded evaluate + AT-MOST-ONE-repair + re-verify stage in
        # :meth:`_run_design_fidelity_stage`.
        self._design_fidelity_enabled = bool(design_fidelity_enabled)
        self._design_fidelity_result = None
        # V4-B2: the ONE durable per-operation FRONTEND budget shared with the
        # build's initial generation and compile repair. None (the production
        # default) leaves every repair path byte-identical to pre-V4-B2.
        self._frontend_budget = frontend_budget
        # V4-B2: the honest VISION-vs-fidelity consistency classification for a
        # blocking VISION verdict. Set only when a blocking verdict actually
        # stops acceptance; read by :meth:`_finalize_failure`.
        self._vision_consistency: Optional[Dict[str, Any]] = None

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
                    # --- D3b: the bounded critic repair stage -----------------
                    # Runs AFTER the existing browser/VISION QA passes and
                    # BEFORE acceptance. It may perform further bounded FRONTEND
                    # repairs; if it fails, this QA run FAILS and no preview or
                    # publication is reached.
                    #
                    # It runs BEFORE the tested snapshot is captured, because a
                    # critic repair invalidates the ``checked`` binding and the
                    # browser QA evidence: the snapshot must bind the bytes that
                    # FINALLY passed, not the pre-critic ones.
                    critic_stage = self._run_critic_stage(
                        project_id=project_id,
                        workspace=workspace,
                        brief=brief,
                        design_dna=design_dna,
                        attempts=attempts,
                        next_attempt_num=next_attempt_num,
                        qa_dir=qa_dir,
                    )
                    if critic_stage is not None:
                        attempts = critic_stage["attempts"]
                        next_attempt_num = critic_stage["next_attempt_num"]
                        repair_count += critic_stage["extra_repairs"]
                        if critic_stage["failed"]:
                            self._finalize_failure(
                                project_id, attempts, repair_count,
                                error=critic_stage["error"],
                                critic=critic_stage["record"],
                            )
                            return QAResult(
                                project_id=project_id,
                                success=False,
                                attempts=attempts,
                                repair_attempts=repair_count,
                                error=critic_stage["error"],
                            )

                    # --- V3-C/V4-A: the bounded design-fidelity gate ---------
                    # Runs after the browser/VISION QA (and after the critic
                    # stage) and BEFORE the tested snapshot, so a fidelity
                    # repair invalidates nothing that has already been bound.
                    # DISABLED by default: byte-identical to pre-V4-A.
                    fidelity = self._run_design_fidelity_stage(
                        project_id=project_id,
                        workspace=workspace,
                        brief=brief,
                        design_dna=design_dna,
                        attempts=attempts,
                        next_attempt_num=next_attempt_num,
                        qa_dir=qa_dir,
                    )
                    if fidelity is not None:
                        attempts = fidelity["attempts"]
                        next_attempt_num = fidelity["next_attempt_num"]
                        repair_count += fidelity["extra_repairs"]
                        if fidelity["failed"]:
                            self._finalize_failure(
                                project_id, attempts, repair_count,
                                error=fidelity["error"],
                                fidelity=fidelity["record"],
                            )
                            return QAResult(
                                project_id=project_id,
                                success=False,
                                attempts=attempts,
                                repair_attempts=repair_count,
                                error=fidelity["error"],
                            )

                    # The attempt that FINALLY passed QA. After a critic repair
                    # this is the stage's fresh browser/VISION attempt; without
                    # one it is the original passing attempt.
                    final_attempt = attempts[-1] if attempts else qa_attempt

                    state = self.store.load(project_id)
                    checked = state.deployment.get('checked') or {}
                    tested_snapshot = None

                    # N6-E: verify the RECORDED guidance snapshot the last
                    # FRONTEND invocation was actually given. This runs
                    # UNCONDITIONALLY at acceptance, independently of the
                    # ``checked`` source binding: parity of the specification
                    # version is a distinct property. A document that was used
                    # and has since changed (or vanished) is a true mismatch and
                    # fails closed. Parity is proven against the RECORD, never
                    # inferred from whatever the workspace currently holds. A
                    # legacy project (no recorded invocation) verifies trivially.
                    from app.core.guidance_snapshot import (
                        latest_invocation, verify_invocation,
                    )

                    _recorded = latest_invocation(state)
                    _mismatch = verify_invocation(workspace, _recorded)
                    if _mismatch:
                        logger.warning(
                            "N6-E guidance parity rejected for %s: %s",
                            project_id, _mismatch,
                        )
                        raise QABlockingError('GUIDANCE_VERSION_MISMATCH')

                    if checked:
                        try:
                            snapshot = TestedSnapshot.capture(workspace, checked['source_sha256'],
                                                              checked['artifact_sha256'])
                        except ValueError as exc:
                            # STALE_SOURCE / STALE_ARTIFACT are the same
                            # condition as the revision check below -- the
                            # tested artifact is no longer the current one --
                            # raised one layer earlier by the snapshot itself.
                            # Report it as the same honest code instead of
                            # letting it be erased into UNEXPECTED_QA_ERROR.
                            if str(exc) in ("STALE_SOURCE", "STALE_ARTIFACT"):
                                raise QABlockingError('STALE_QA_BINDING') from None
                            raise
                        if checked['source_revision'] != state.revisions.source_revision:
                            raise QABlockingError('STALE_QA_BINDING')
                        # V4-B2: separate "VISION did not run" from "VISION ran and
                        # returned blocking findings". The former is an
                        # infrastructure/absence condition (retryable copy); the
                        # latter is a genuine visual-quality block that a retry
                        # of the same artifact cannot clear.
                        if final_attempt.vision is None:
                            raise QABlockingError('VISION_REQUIRED')
                        if final_attempt.vision.pass_ is not True:
                            self._vision_consistency = self._classify_vision_consistency(
                                attempts
                            )
                            raise QABlockingError('VISION_BLOCKING_FINDINGS')
                        tested_snapshot = snapshot.to_dict()

                    # ONE writer block commits the tested snapshot AND the
                    # RUNNING -> PREVIEW_READY transition together. Writing
                    # them separately left a window in which a crash left a
                    # fully passing QA result persisted under a RUNNING
                    # lifecycle, and nothing ever drove it forward.
                    self._finalize_success(
                        project_id, final_attempt, tested_snapshot=tested_snapshot
                    )
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

        except QABlockingError as exc:
            # A named precondition rejected this run. Its code is already a
            # stable application code with existing user-facing copy, so it
            # propagates verbatim instead of being collapsed into
            # UNEXPECTED_QA_ERROR (which would tell the user to retry a
            # condition that cannot be retried away).
            logger.info("Phase 8 QA precondition rejected for %s: %s", project_id, exc.code)
            self._finalize_failure(project_id, attempts, repair_count, error=exc.code)
            return QAResult(
                project_id=project_id,
                success=False,
                attempts=attempts,
                repair_attempts=repair_count,
                error=exc.code,
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

        # Evidence-integrity guard: verify live browser metrics against the
        # intended viewports before VISION consumes the full-page PNGs.
        # Mismatch or malformed metrics is capture infrastructure failure;
        # a legitimately wider full-page PNG is accepted and later classified
        # as deterministic horizontal overflow by the existing repair loop.
        if not infra_error and render_ok:
            try:
                validate_screenshot_dimensions(screenshots)
            except Exception as exc:
                infra_error = f"INFRASTRUCTURE_ERROR:capture_failed:{exc}"

        vision_findings: Optional[VisionFindings] = None
        if not infra_error and screenshots.complete and self.hermes_adapter is not None:
            try:
                # N6-E: hand VISION the pre-build DesignGuidance (when present) so
                # it can judge design COMPLIANCE alongside specification parity.
                # Absent documents leave the prompt unchanged.
                from app.core.document_store import read_design_guidance

                _guidance_doc = read_design_guidance(workspace)
                vision_raw = self.hermes_adapter.vision_inspect(
                    screenshots.desktop, screenshots.mobile, brief, design_dna,
                    design_guidance=(
                        _guidance_doc.text if _guidance_doc is not None else None
                    ),
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

    @staticmethod
    def _screenshot_digests(attempt: QAAttempt) -> Dict[str, Optional[str]]:
        """Content digests of the screenshots VISION was shown (V4-B2).

        Used ONLY to detect that two attempts were judged on byte-identical
        evidence — the basis for the "contradictory verdicts" consistency
        record. A missing/unreadable file yields None (never a false match).
        """
        import hashlib

        out: Dict[str, Optional[str]] = {"desktop": None, "mobile": None}
        for key in ("desktop", "mobile"):
            path = getattr(attempt, f"{key}_screenshot", None)
            if not path:
                continue
            try:
                out[key] = hashlib.sha256(Path(path).read_bytes()).hexdigest()
            except OSError:
                out[key] = None
        return out

    def _classify_vision_consistency(self, attempts: list) -> Optional[Dict[str, Any]]:
        """Record an honest VISION-vs-fidelity consistency classification.

        QA stays AUTHORITATIVE: this NEVER downgrades or bypasses the blocking
        verdict — it only records, for diagnostics, whether the VISION blocking
        finding is corroborated by the gate's OWN evidence, contradicted by an
        identical-evidence PASS, or merely reported-but-unconfirmed.

        Uses the last fidelity report when the gate ran; otherwise the gate
        simply did not evaluate, so every blocking finding is
        reported-but-unconfirmed (never an invented defect).
        """
        try:
            from app.core.design_fidelity_runtime import build_vision_consistency

            entries = [
                {
                    "attempt": a.attempt,
                    "pass": (a.vision.pass_ if a.vision is not None else None),
                    "blocking": list(a.vision.blocking_findings) if a.vision else [],
                    "observations": list(a.vision.observations) if a.vision else [],
                    "screenshots_sha": self._screenshot_digests(a),
                }
                for a in attempts
            ]
            record = self._design_fidelity_result
            # The recorded fidelity record is the gate result's to_dict(); its
            # findings live under the nested "report". The pure classifier
            # reads findings from either a report object or its to_dict() shape.
            report = record.get("report") if isinstance(record, dict) else record
            return build_vision_consistency(entries, report)
        except Exception:  # noqa: BLE001 - recording must never crash QA
            logger.warning(
                "V4-B2 could not classify VISION consistency", exc_info=True
            )
            return None

    # ------------------------------------------------------------------
    # D3b: the bounded critic repair stage
    # ------------------------------------------------------------------

    def _critic_repair(
        self,
        project_id: str,
        workspace: Path,
        findings,
        previous_results,
        budget,
        attempt_index: int,
    ) -> bool:
        """One critic-driven FRONTEND repair, through the EXISTING mechanism.

        Uses the same writer lock, the same ``frontend_build`` adapter call, the
        same toolchain verification, and the same Design DNA validation as every
        other repair. No second pipeline, no second lock manager.

        Returns True only when the repair call itself succeeded; the resulting
        rebuild is verified separately (never trusted from the model's claim).
        """
        if self.hermes_adapter is None:
            return False

        # V4-B2: cheap, non-authoritative peek. The FRONTEND boundary remains
        # the authoritative check; this only avoids a pointless attempt.
        if (
            self._frontend_budget is not None
            and not self._frontend_budget.has_capacity()
        ):
            logger.info(
                "V4-B2: skipping critic repair for %s — FRONTEND budget exhausted",
                project_id,
            )
            return False

        from app.core.critic_policy import build_repair_request

        with self.store.acquire_writer(project_id) as state:
            request = build_repair_request(
                requirements=state.brief or {},
                design_dna=state.design_dna or {},
                findings=findings,
                browser_findings=(),
                build_errors="",
                project_id=project_id,
                source_revision=state.revisions.source_revision,
                previous_results=previous_results,
                budget=budget,
            )
            instructions = compose_project_instructions(
                state, access_key=self.web3forms_access_key, task=request,
            )
            brief = dict(state.brief)
            design_references = dict(state.design_references or {})
            state.revisions.source_revision += 1
            repair_operation_id = str(state.revisions.source_revision)
            invalidate_artifact(state)
            self.store.save(state)

        result = self.hermes_adapter.frontend_build(
            project_id=project_id,
            brief=brief,
            workspace=workspace,
            design_dna_instructions=instructions,
            build_operation_id=repair_operation_id,
            frontend_budget=self._frontend_budget,
            invocation_stage="critic_repair",
        )

        # MEDIUM-5: verify protected starter/toolchain files after EVERY
        # FRONTEND repair invocation — regardless of the repair's own result.
        if self._toolchain_verify is not None:
            violation = self._toolchain_verify(workspace)
            if violation:
                logger.warning(
                    "Protected toolchain file mutated during QA repair of %s: %s",
                    project_id, violation,
                )
                self._toolchain_error = f"TOOLCHAIN_MUTATION_REJECTED ({violation})"
                return False

        if not result.get("success"):
            return False
        dna = result.get("design_dna")
        try:
            validate_composed_dna(dna, ReferenceSnapshot(design_references))
        except ValueError:
            return False
        with self.store.acquire_writer(project_id) as locked:
            locked.design_dna = dna
            locked.revisions.design_dna_version = dna.get(
                "version", locked.revisions.design_dna_version + 1
            )
            invalidate_artifact(locked)
            self.store.save(locked)
        return True

    def _run_critic_stage(
        self,
        *,
        project_id: str,
        workspace: Path,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]],
        attempts: list,
        next_attempt_num: int,
        qa_dir: Path,
    ) -> Optional[Dict[str, Any]]:
        """Run the bounded critic repair stage; return the updated bookkeeping.

        Returns None only when no scanner is configured AND no record should be
        produced -- which does not happen: a missing scanner is itself an
        explicit NOT_RUN degraded outcome. The returned dict always carries a
        durable ``record`` so the caller persists the honest critic state.
        """
        from app.core import critic_policy
        from app.core.critic_repair import CriticScanResult

        # --- a missing scanner is an explicit NOT_RUN degraded state --------
        if self._critic_scanner is None:
            record = {
                "state": critic_policy.NOT_RUN,
                "outcome": critic_policy.OUTCOME_DEGRADED_ACCEPTED,
                "authoritative": False,
                "degraded_record": True,
                "failed": False,
                "attempts_used": 0,
                "reason": "critic_not_run",
            }
            self._critic_stage_result = record
            self._record_critic_record(project_id, record)
            return {
                "attempts": attempts,
                "next_attempt_num": next_attempt_num,
                "extra_repairs": 0,
                "failed": False,
                "error": None,
                "record": record,
            }

        # A durable attempt sink: the attempt identity survives a crash.
        def _record_attempt(entry: Dict[str, Any]) -> None:
            with self.store.acquire_writer(project_id) as state:
                bag = state.deployment.setdefault("critic_repair", {})
                bag["last_attempt"] = dict(entry)
                bag["attempts_started"] = int(bag.get("attempts_started", 0)) + 1
                self.store.save(state)

        def _revision_of() -> int:
            st = self.store.load(project_id)
            return int(st.revisions.source_revision) if st is not None else -1

        start_revision = _revision_of()

        # The stage's browser QA produces a fresh QAAttempt that the caller must
        # keep in its history (and which consumes an attempt number).
        captured: Dict[str, Any] = {"attempts": attempts, "next": next_attempt_num}

        def _browser_qa():
            attempt = self._run_one_attempt(
                project_id, workspace, qa_dir, captured["next"], brief, design_dna
            )
            captured["next"] += 1
            captured["attempts"].append(attempt)
            return attempt

        def _rebuild():
            return self._run_rebuild_checks(project_id, workspace)

        stage = CriticStage(
            scanner=self._critic_scanner,
            repair_fn=lambda findings, prev, budget, idx: self._critic_repair(
                project_id, workspace, findings, prev, budget, idx
            ),
            rebuild_fn=_rebuild,
            browser_qa_fn=_browser_qa,
            initial_attempts=0,
            record_attempt=_record_attempt,
            revision_of=_revision_of,
            expected_start_revision=start_revision,
            protected_terms=self._critic_protected_terms(project_id),
        )
        result = stage.run(workspace)
        self._critic_stage_result = result.to_dict()

        record = result.to_dict()
        self._record_critic_record(project_id, record)

        extra_repairs = result.attempts_used
        error = result.error
        if error is None and result.failed:
            error = f"CRITIC_{result.outcome}"

        return {
            "attempts": captured["attempts"],
            "next_attempt_num": captured["next"],
            "extra_repairs": extra_repairs,
            "failed": bool(result.failed),
            "error": error,
            "record": record,
        }

    def _run_design_fidelity_stage(
        self,
        *,
        project_id: str,
        workspace: Path,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]],
        attempts: list,
        next_attempt_num: int,
        qa_dir: Path,
    ) -> Optional[Dict[str, Any]]:
        """Run the bounded V4-A design-fidelity gate; return bookkeeping.

        Returns ``None`` when the gate is DISABLED (the default), so the QA path
        is byte-identical to pre-V4-A. When enabled it:

        1. builds evidence from the SAME workspace/revision (persisted brief,
           PRD, DesignGuidance + Asset Strategy, generated source, VISION),
        2. runs the bounded gate: evaluate, AT MOST ONE repair through the
           EXISTING validated FRONTEND repair executor, then re-verify,
        3. records the honest verdict durably (tied to the source revision) and
           surfaces ``failed`` so the caller refuses acceptance.

        Post-repair it re-runs the required checks (build/typecheck,
        self-contained, screenshot/VISION, deterministic, and the re-evaluated
        fidelity) before acceptance (C4). It never creates a tested snapshot for
        an unverified artifact and never triggers publication (C5/C6).
        """
        if not self._design_fidelity_enabled:
            return None

        from app.core.design_fidelity import (
            FidelityRepairBudget,
            run_bounded_fidelity_gate,
        )
        from app.core.design_fidelity_runtime import make_fidelity_evaluate

        # --- evidence bound to the CURRENT source revision (A3) -------------
        persisted = self.store.load(project_id)
        if persisted is None:
            return None
        revision = int(persisted.revisions.source_revision)
        persisted_brief = dict(persisted.brief or {}) or dict(brief or {})

        # Mutable evidence state the gate re-reads on its post-repair pass.
        evidence: Dict[str, Any] = {
            "vision": attempts[-1].vision if attempts else None,
            "technical_ok": True,
        }
        captured: Dict[str, Any] = {
            "attempts": attempts,
            "next": next_attempt_num,
            "repaired": False,
        }

        # A validated FRONTEND repair executor exists (self._repair). When no
        # adapter is available there is NO executor, so we run detection-only
        # and report repair_unavailable rather than simulating a repair (C3).
        repair_fn = None
        if self.hermes_adapter is not None:
            repair_fn = lambda findings: self._fidelity_repair(  # noqa: E731
                project_id=project_id,
                workspace=workspace,
                brief=persisted_brief,
                design_dna=design_dna,
                findings=findings,
                evidence=evidence,
                captured=captured,
                qa_dir=qa_dir,
            )

        evaluate = make_fidelity_evaluate(
            workspace,
            persisted_brief,
            vision_provider=lambda: evidence["vision"],
            technical_ok_provider=lambda: evidence["technical_ok"],
        )

        # --- NO validated executor -> DETECTION-ONLY (C3) -------------------
        # We never simulate a repair or claim repair success. The gate runs its
        # evaluation, records the honest verdict, and reports repair_unavailable.
        if repair_fn is None:
            from app.core.design_fidelity import overall_terminal_outcome

            report = evaluate()
            outcome = overall_terminal_outcome(
                report, repair_used=False, repair_verified=False
            )
            record = {
                "outcome": outcome,
                "attempts_used": 0,
                "repair_used": False,
                "repair_verified": False,
                "reason": "repair_unavailable_no_executor",
                "failed": outcome in ("FIDELITY_FAILED", "TECHNICAL_FAILED"),
                "history": [],
                "report": report.to_dict(),
                "source_revision": revision,
            }
            self._design_fidelity_result = record
            self._record_fidelity_record(project_id, record)
            failed = bool(record["failed"])
            return {
                "attempts": captured["attempts"],
                "next_attempt_num": captured["next"],
                "extra_repairs": 0,
                "failed": failed,
                "error": f"DESIGN_FIDELITY_{outcome}" if failed else None,
                "record": record,
            }

        try:
            result = run_bounded_fidelity_gate(
                evaluate=evaluate,
                repair=repair_fn,
                budget=FidelityRepairBudget(),
                technical_ok=True,
            )
        except Exception:  # noqa: BLE001 - a gate error never crashes QA
            logger.warning(
                "V4-A design-fidelity gate raised for %s; recording degraded",
                project_id, exc_info=True,
            )
            record = {
                "outcome": "FIDELITY_UNKNOWN",
                "failed": False,
                "degraded": True,
                "error": "design_fidelity_gate_raised",
                "source_revision": revision,
            }
            self._design_fidelity_result = record
            self._record_fidelity_record(project_id, record)
            return {
                "attempts": captured["attempts"],
                "next_attempt_num": captured["next"],
                "extra_repairs": 1 if captured["repaired"] else 0,
                "failed": False,
                "error": None,
                "record": record,
            }

        record = result.to_dict()
        # A4: bind the verdict to the EXACT source revision it judged. A repair
        # bumps the source revision (and invalidates the old verdict), so read
        # the revision FRESH here — the verdict describes the FINAL artifacts.
        current = self.store.load(project_id)
        if current is not None:
            revision = int(current.revisions.source_revision)
        record["source_revision"] = revision
        self._design_fidelity_result = record
        self._record_fidelity_record(project_id, record)

        failed = bool(result.failed)
        error = None
        if failed:
            error = f"DESIGN_FIDELITY_{result.outcome}"
        return {
            "attempts": captured["attempts"],
            "next_attempt_num": captured["next"],
            "extra_repairs": int(result.attempts_used),
            "failed": failed,
            "error": error,
            "record": record,
        }

    def _fidelity_repair(
        self,
        *,
        project_id: str,
        workspace: Path,
        brief: Dict[str, Any],
        design_dna: Optional[Dict[str, Any]],
        findings: list,
        evidence: Dict[str, Any],
        captured: Dict[str, Any],
        qa_dir: Path,
    ) -> bool:
        """One fidelity-driven repair through the EXISTING validated executor.

        Reuses :meth:`_repair` (same writer lock, same FRONTEND adapter call,
        same toolchain verification, same Design DNA validation) — no second
        pipeline, no second lock manager. Afterwards it re-runs the required
        post-repair checks and a fresh browser/VISION attempt, updating
        ``evidence`` so the gate's re-evaluation reflects the ACTUAL repaired
        artifacts (B5/B6/C4). Returns True only when the repair call itself ran.
        """
        # The fidelity findings are handed to FRONTEND as VISION BLOCKING
        # findings, which is exactly the channel _build_repair_instructions
        # renders as mandatory. Subjective observations stay non-blocking.
        blockers = [
            f"{f.get('rule_id')}: {f.get('finding')} @ {f.get('location')} "
            f"-> {f.get('suggested_action')}"
            for f in findings
        ]
        synthetic = QAAttempt(
            attempt=captured["next"],
            deterministic=DeterministicFindings(
                render_ok=True, desktop_screenshot_ok=True, mobile_screenshot_ok=True,
                design_dna_valid=True, source_present=True,
                build_ok=True, typecheck_ok=True,
            ),
            vision=VisionFindings(pass_=False, blocking_findings=blockers),
        )
        repaired = self._repair(
            project_id, workspace, brief, design_dna, synthetic,
            invocation_stage="fidelity_repair",
        )
        if not repaired:
            return False
        captured["repaired"] = True

        # C4: rebuild + self-contained checks on the changed artifacts.
        build_ok, typecheck_ok, self_contained_ok = self._run_rebuild_checks(
            project_id, workspace
        )
        if not (build_ok and typecheck_ok and self_contained_ok):
            # Honest: the repair broke the build. The gate's re-evaluation will
            # see DIM_TECHNICAL = FAIL and refuse to verify the repair.
            evidence["technical_ok"] = False
            return True

        # C4: a fresh browser/VISION attempt so the re-evaluation reflects the
        # repaired pixels, not the pre-repair evidence.
        fresh = self._run_one_attempt(
            project_id, workspace, qa_dir, captured["next"], brief, design_dna
        )
        captured["next"] += 1
        captured["attempts"].append(fresh)
        evidence["vision"] = fresh.vision
        evidence["technical_ok"] = (
            not bool(fresh.infrastructure_error)
            and not bool(fresh.deterministic.failures)
        )
        return True

    def _record_fidelity_record(self, project_id: str, record: Dict[str, Any]) -> None:
        """Persist the fidelity verdict as honest evidence (never as a claim)."""
        try:
            with self.store.acquire_writer(project_id) as state:
                state.deployment["design_fidelity"] = record
                self.store.save(state)
        except Exception:  # noqa: BLE001 - recording must never crash QA
            logger.warning(
                "V3-C could not record design-fidelity result for %s",
                project_id, exc_info=True,
            )

    def _critic_protected_terms(self, project_id: str):
        """Terms a critic finding must not propose to change.

        Drawn from the project's OWN accepted requirements and Design DNA, so a
        finding that proposes removing or replacing a required element is
        classified as a requirement conflict and never auto-repaired.
        """
        terms = []
        try:
            state = self.store.load(project_id)
        except Exception:
            return tuple(terms)
        if state is None:
            return tuple(terms)
        brief = state.brief or {}
        for key in ("name", "what", "why"):
            value = brief.get(key)
            if isinstance(value, str) and value.strip():
                terms.append(value.strip())
        dna = state.design_dna or {}
        if isinstance(dna, dict):
            for key in ("brand_personality", "brand_name", "font_family", "palette"):
                value = dna.get(key)
                if isinstance(value, str) and value.strip():
                    terms.append(value.strip())
        # Deduplicate while preserving order; bound each term.
        seen = set()
        out = []
        for term in terms:
            t = term[:80]
            if t and t.lower() not in seen:
                seen.add(t.lower())
                out.append(t)
        return tuple(out[:12])

    def _record_critic_record(self, project_id: str, record: Dict[str, Any]) -> None:
        """Persist the critic stage record durably (never a source mutation)."""
        try:
            with self.store.acquire_writer(project_id) as state:
                state.deployment["critic"] = record
                self.store.save(state)
        except Exception:
            logger.warning("could not persist critic record for %s", project_id, exc_info=True)

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
        invocation_stage: str = "qa_repair",
    ) -> bool:
        """Invoke FRONTEND to perform the smallest targeted fix.

        Returns True if the repair call itself succeeded (regardless of
        whether the resulting rebuild passes — that is checked separately).
        """
        if self.hermes_adapter is None:
            return False

        # V4-B2: cheap, non-authoritative peek. If the global budget is already
        # spent, skip this repair without composing instructions or touching the
        # workspace. The FRONTEND boundary (adapter.frontend_build) remains the
        # AUTHORITATIVE check; this only avoids a pointless attempt.
        if (
            self._frontend_budget is not None
            and not self._frontend_budget.has_capacity()
        ):
            logger.info(
                "V4-B2: skipping QA repair for %s — FRONTEND budget exhausted",
                project_id,
            )
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
            # Operation id for the supervised FRONTEND invocation, so a QA
            # repair's watchdog is never refreshed by the build's activity.
            repair_operation_id = str(state.revisions.source_revision)
            invalidate_artifact(state)
            self.store.save(state)
        result = self.hermes_adapter.frontend_build(
            project_id=project_id,
            brief=brief,
            workspace=workspace,
            design_dna_instructions=instructions,
            build_operation_id=repair_operation_id,
            frontend_budget=self._frontend_budget,
            invocation_stage=invocation_stage,
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
            validate_composed_dna(dna, ReferenceSnapshot(design_references))
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

    def _finalize_success(
        self, project_id: str, qa_attempt: QAAttempt,
        *, tested_snapshot: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Commit the passing QA result atomically.

        ``tested_snapshot`` (when present) and the ``RUNNING ->
        PREVIEW_READY`` transition are written in the SAME writer block, so
        no crash can leave a passing result persisted under an in-flight
        lifecycle with nothing left to drive it forward.
        """
        with self.store.acquire_writer(project_id) as state:
            if tested_snapshot is not None:
                state.deployment['tested_snapshot'] = tested_snapshot
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
        critic: Optional[Dict[str, Any]] = None,
        fidelity: Optional[Dict[str, Any]] = None,
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
            if critic is not None:
                # D3b: the critic stage's own evidence for the failure, so an
                # operator can see WHICH finding/regression ended the loop.
                state.failure["critic"] = critic
            if fidelity is not None:
                # V3-C: the fidelity gate's own evidence for the failure.
                state.failure["design_fidelity"] = fidelity
            if self._vision_consistency is not None:
                # V4-B2: the honest VISION-vs-fidelity consistency record. It
                # NEVER overrides the blocking verdict — it only records whether
                # the VISION finding was corroborated by the gate's OWN
                # evidence, contradicted by an identical-evidence PASS, or
                # merely reported-but-unconfirmed.
                state.failure["vision_consistency"] = self._vision_consistency
                if isinstance(state.failure.get("design_fidelity"), dict):
                    state.failure["design_fidelity"]["vision_consistency"] = (
                        self._vision_consistency
                    )
                # Also attach to the DURABLE fidelity diagnostic record, so the
                # VISION findings (location/severity/evidence) and the
                # corroboration verdict live beside the gate's own verdict.
                dep_fid = state.deployment.get("design_fidelity")
                if isinstance(dep_fid, dict):
                    dep_fid["vision_consistency"] = self._vision_consistency
            self.store.save(state)
