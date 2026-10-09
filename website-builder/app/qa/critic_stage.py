"""The bounded critic repair stage (D3b).

One application-owned state machine that runs AFTER the existing browser/VISION
QA passes, and BEFORE acceptance:

    Impeccable scan -> classification -> [bounded FRONTEND repair ->
    build+typecheck -> browser/VISION QA -> re-scan -> convergence]*

Every decision is application-owned. The critic supplies evidence; FRONTEND
performs repairs through the EXISTING authorized project-writing mechanism
(injected here as ``repair_fn``). Impeccable never modifies a source file --
there is no write path in :mod:`app.core.design_critic` at all.

This class holds NO second pipeline, NO second lock manager, and NO second
revision lifecycle: it calls back into the orchestrator's existing primitives
(the writer lock, ``_run_rebuild_checks``, ``_run_one_attempt``, the FRONTEND
adapter) and only owns the DECISION logic, which is pure and in
:mod:`app.core.critic_policy`.

Stop conditions (never unbounded):

* acceptance gates pass -> ACCEPTED / REPAIRED / FULLY_VALIDATED
* a blocking finding became more severe, or a new blocker appeared -> REJECTED
* build/typecheck or browser QA regressed -> REJECTED
* the same actionable findings persist unchanged -> STALLED
* the attempt limit is reached -> EXHAUSTED
* a security/dependency/workspace boundary is violated -> REJECTED / FAILED
* a missing/degraded/failed critic -> explicit DEGRADED outcome
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from app.core import critic_policy
from app.core.critic_policy import (
    ClassifiedFinding,
    ConvergenceReport,
    RepairBudget,
    acceptance_allowed,
    blocking_findings,
    bound_repair_findings,
    classify_findings,
    evaluate_convergence,
    repairable_findings,
    unrepaired_blockers,
)
from app.core.critic_repair import CriticScanResult

logger = logging.getLogger(__name__)

#: Bound on the retained per-attempt history (operator evidence only).
MAX_HISTORY = 8


@dataclass
class CriticStageResult:
    """Outcome of the bounded critic repair stage."""

    #: Final critic state (CRITIC_STATES).
    state: str
    #: Terminal repair outcome (CRITIC_REPAIR_OUTCOMES).
    outcome: str
    authoritative: bool = False
    #: True when acceptance proceeded on a missing/degraded/failed critic.
    degraded_record: bool = False
    #: True => the QA run must FAIL (no acceptance, no publication).
    failed: bool = False
    error: Optional[str] = None
    attempts_used: int = 0
    reason: str = ""
    history: List[Dict[str, Any]] = field(default_factory=list)
    scan: Optional[CriticScanResult] = None
    classification: Tuple[ClassifiedFinding, ...] = ()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "state": self.state,
            "outcome": self.outcome,
            "authoritative": self.authoritative,
            "degraded_record": self.degraded_record,
            "failed": self.failed,
            "error": self.error,
            "attempts_used": self.attempts_used,
            "reason": self.reason,
            "history": list(self.history),
            "scan": self.scan.to_dict() if self.scan is not None else None,
            "classification": [f.to_dict() for f in self.classification],
        }


#: A repair callable: (findings, previous_results, budget, attempt_index) -> bool.
RepairFn = Callable[[Sequence[ClassifiedFinding], Sequence[Dict[str, Any]], RepairBudget, int], bool]
#: A rebuild callable: () -> (build_ok, typecheck_ok, self_contained_ok).
RebuildFn = Callable[[], Tuple[bool, bool, bool]]
#: A browser/VISION QA callable: () -> attempt (object exposing ``final_pass`` /
#: ``infrastructure_error``).
BrowserQAFn = Callable[[], Any]


class CriticStage:
    """Runs the bounded critic repair loop for ONE project revision.

    ``repair_fn`` performs the FRONTEND repair through the orchestrator's
    EXISTING mechanism. The stage never writes a source file itself.
    """

    def __init__(
        self,
        *,
        scanner: Any,
        repair_fn: RepairFn,
        rebuild_fn: RebuildFn,
        browser_qa_fn: BrowserQAFn,
        budget: Optional[RepairBudget] = None,
        protected_terms: Sequence[str] = (),
        initial_attempts: int = 0,
        record_attempt: Optional[Callable[[Dict[str, Any]], None]] = None,
        revision_of: Optional[Callable[[], int]] = None,
        expected_start_revision: Optional[int] = None,
    ):
        self.scanner = scanner
        self.repair_fn = repair_fn
        self.rebuild_fn = rebuild_fn
        self.browser_qa_fn = browser_qa_fn
        # Durable resume: a re-run after a crash MUST NOT restart the attempt
        # count, or recovery could silently consume unlimited attempts.
        self.budget = budget or RepairBudget(attempts_used=max(0, int(initial_attempts)))
        self.protected_terms = tuple(protected_terms)
        # Optional durable sink invoked after each attempt, so the attempt
        # identity survives a crash and recovery can reconcile.
        self._record_attempt = record_attempt
        # Optional revision observer: proves the critic result belongs to the
        # revision it was produced for, and that a stale result cannot modify a
        # newer revision.
        self._revision_of = revision_of
        self._expected_start_revision = expected_start_revision

    # -- helpers -------------------------------------------------------

    def _classify(self, scan: CriticScanResult) -> Tuple[ClassifiedFinding, ...]:
        return classify_findings(scan.findings, protected_terms=self.protected_terms)

    def _degraded(self, scan: CriticScanResult, reason: str) -> CriticStageResult:
        """A missing/degraded/failed critic -> explicit degraded outcome.

        Acceptance is policy-controlled (``CRITIC_FAILURE_POLICY``). The record
        always carries the TRUE state; degraded quality is never labelled as
        fully verified.
        """
        allowed, why = acceptance_allowed(
            critic_state=scan.state,
            authoritative=False,
            unresolved_blockers=(),
            repairable_remaining=(),
            degraded_record=True,
        )
        return CriticStageResult(
            state=scan.state,
            outcome=critic_policy.OUTCOME_DEGRADED_ACCEPTED,
            authoritative=False,
            degraded_record=True,
            failed=not allowed,
            error=None if allowed else f"CRITIC_{scan.state}",
            attempts_used=self.budget.attempts_used,
            reason=why or reason,
            scan=scan,
        )

    # -- the loop ------------------------------------------------------

    def _revision_ok(self) -> bool:
        """Whether the workspace still belongs to the revision we last recorded.

        A stale critic result must never modify a newer revision: if the
        project's source revision moved since this stage last observed it (i.e.
        some OTHER operation bumped it), the stage refuses to continue. The
        stage's OWN repair advances the expected value via
        :meth:`_note_revision`, so a repair this stage performed is not mistaken
        for staleness.
        """
        if self._revision_of is None or self._expected_start_revision is None:
            return True
        try:
            current = int(self._revision_of())
        except Exception:
            return False
        return current == int(self._expected_start_revision)

    def _note_revision(self) -> None:
        """Record the revision the stage currently owns (after its own repair)."""
        if self._revision_of is None:
            return
        try:
            self._expected_start_revision = int(self._revision_of())
        except Exception:
            self._expected_start_revision = None

    def run(self, workspace) -> CriticStageResult:
        scan = self.scanner.scan(workspace)
        classified = self._classify(scan)

        # --- a critic that could not produce authoritative evidence -------
        if scan.state in (critic_policy.NOT_RUN, critic_policy.DEGRADED, critic_policy.FAILED):
            return self._degraded(scan, f"critic_{scan.state.lower()}")

        # A revision that moved out from under us is an uncertain workspace
        # identity, never a licence to repair.
        if not self._revision_ok():
            return CriticStageResult(
                state=scan.state,
                outcome=critic_policy.OUTCOME_REJECTED,
                authoritative=False,
                failed=True,
                error="CRITIC_STALE_REVISION",
                attempts_used=self.budget.attempts_used,
                reason="stale_revision",
                scan=scan,
                classification=classified,
            )

        history: List[Dict[str, Any]] = []

        while True:
            repairable = repairable_findings(classified)
            unresolvable = unrepaired_blockers(classified)

            # A blocking finding the application may NOT auto-repair (a
            # requirement conflict, or an unknown severity) is an explicit
            # acceptance failure. The critic cannot override the requirement.
            if unresolvable:
                allowed, why = acceptance_allowed(
                    critic_state=scan.state,
                    authoritative=scan.authoritative,
                    unresolved_blockers=unresolvable,
                    repairable_remaining=repairable,
                    degraded_record=False,
                )
                return CriticStageResult(
                    state=scan.state,
                    outcome=critic_policy.OUTCOME_REJECTED,
                    authoritative=scan.authoritative,
                    failed=not allowed,
                    error="CRITIC_REQUIREMENT_CONFLICT" if not allowed else None,
                    attempts_used=self.budget.attempts_used,
                    reason=why,
                    history=history,
                    scan=scan,
                    classification=classified,
                )

            if not repairable:
                # No actionable finding remains -> acceptance gates pass.
                allowed, why = acceptance_allowed(
                    critic_state=scan.state,
                    authoritative=scan.authoritative,
                    unresolved_blockers=(),
                    repairable_remaining=(),
                    degraded_record=False,
                )
                if allowed:
                    outcome = (
                        critic_policy.OUTCOME_REPAIRED if history
                        else critic_policy.OUTCOME_ACCEPTED
                    )
                    return CriticStageResult(
                        state=scan.state,
                        outcome=outcome,
                        authoritative=scan.authoritative,
                        attempts_used=self.budget.attempts_used,
                        reason=why,
                        history=history,
                        scan=scan,
                        classification=classified,
                    )
                # Defensive: a non-authoritative CLEAN that reached here.
                return self._degraded(scan, "clean_without_authority")

            # --- budget gate: never unbounded ---------------------------
            if self.budget.exhausted:
                return CriticStageResult(
                    state=scan.state,
                    outcome=critic_policy.OUTCOME_EXHAUSTED,
                    authoritative=scan.authoritative,
                    failed=True,
                    error="CRITIC_REPAIR_BUDGET_EXHAUSTED",
                    attempts_used=self.budget.attempts_used,
                    reason="repair_budget_exhausted",
                    history=history,
                    scan=scan,
                    classification=classified,
                )

            # --- one bounded repair attempt ----------------------------
            attempt_index = self.budget.attempts_used + 1
            self.budget = self.budget.consume()

            # Durable attempt identity: record the attempt BEFORE the mutating
            # FRONTEND call, so a crash mid-repair is visible to recovery and
            # cannot be silently re-consumed from a stale attempt count.
            if self._record_attempt is not None:
                try:
                    self._record_attempt({
                        "attempt": attempt_index,
                        "max_attempts": self.budget.max_attempts,
                        "phase": "repair_started",
                    })
                except Exception:
                    logger.warning("critic repair attempt record failed", exc_info=True)

            request_findings = bound_repair_findings(classified)

            logger.info(
                "Critic repair attempt %d/%d (%d repairable findings)",
                attempt_index, self.budget.max_attempts, len(request_findings),
            )
            repaired = self.repair_fn(request_findings, tuple(history), self.budget, attempt_index)
            if not repaired:
                # The repair itself failed to execute (or was rejected by
                # toolchain/identity policy). Fail closed; never retry blindly.
                return CriticStageResult(
                    state=scan.state,
                    outcome=critic_policy.OUTCOME_REJECTED,
                    authoritative=scan.authoritative,
                    failed=True,
                    error="CRITIC_REPAIR_EXECUTION_FAILED",
                    attempts_used=self.budget.attempts_used,
                    reason="repair_execution_failed",
                    history=history,
                    scan=scan,
                    classification=classified,
                )

            # A repair bumps the source revision. The critic result that drove
            # it belonged to the PREVIOUS revision; if the workspace identity is
            # now ambiguous, stop rather than validating against the wrong one.
            # A repair this stage performed is recorded first so it is not
            # mistaken for staleness.
            self._note_revision()
            if not self._revision_ok():
                return CriticStageResult(
                    state=scan.state,
                    outcome=critic_policy.OUTCOME_REJECTED,
                    authoritative=False,
                    failed=True,
                    error="CRITIC_STALE_REVISION",
                    attempts_used=self.budget.attempts_used,
                    reason="stale_revision",
                    history=history,
                    scan=scan,
                    classification=classified,
                )

            # --- revalidation: build + typecheck (deterministic authority) --
            build_ok, typecheck_ok, self_contained_ok = self.rebuild_fn()
            deterministic_ok = bool(build_ok and typecheck_ok and self_contained_ok)

            # --- browser / VISION QA -----------------------------------
            browser_attempt = self.browser_qa_fn()
            infra = getattr(browser_attempt, "infrastructure_error", None)
            browser_ok = bool(getattr(browser_attempt, "final_pass", False))

            # --- re-scan ------------------------------------------------
            new_scan = self.scanner.scan(workspace)
            new_classified = self._classify(new_scan)

            convergence: ConvergenceReport = evaluate_convergence(
                classified,
                new_classified,
                build_ok=deterministic_ok,
                typecheck_ok=True,
                browser_qa_ok=browser_ok and not infra,
            )

            record = {
                "attempt": attempt_index,
                "outcome": convergence.state,
                "critic_state": new_scan.state,
                "resolved": list(convergence.resolved),
                "persistent": list(convergence.persistent),
                "introduced": list(convergence.introduced),
                "increased_severity": list(convergence.increased_severity),
                "new_blockers": list(convergence.new_blockers),
                "build_ok": bool(build_ok),
                "typecheck_ok": bool(typecheck_ok),
                "self_contained_ok": bool(self_contained_ok),
                "browser_qa_ok": browser_ok,
            }
            history.append(record)
            if len(history) > MAX_HISTORY:
                history = history[-MAX_HISTORY:]

            # --- stop conditions ---------------------------------------
            if infra:
                return CriticStageResult(
                    state=new_scan.state,
                    outcome=critic_policy.OUTCOME_REJECTED,
                    authoritative=new_scan.authoritative,
                    failed=True,
                    error=f"CRITIC_STAGE_INFRASTRUCTURE:{infra}",
                    attempts_used=self.budget.attempts_used,
                    reason="browser_qa_regression",
                    history=history,
                    scan=new_scan,
                    classification=new_classified,
                )

            if not deterministic_ok:
                return CriticStageResult(
                    state=new_scan.state,
                    outcome=critic_policy.OUTCOME_REJECTED,
                    authoritative=new_scan.authoritative,
                    failed=True,
                    error="CRITIC_REPAIR_BUILD_REGRESSION",
                    attempts_used=self.budget.attempts_used,
                    reason="build_regression",
                    history=history,
                    scan=new_scan,
                    classification=new_classified,
                )

            if not browser_ok:
                # Browser QA regressed after the repair. Terminate (the spec's
                # "browser QA regresses" stop condition); the last known-good
                # validated snapshot is never overwritten.
                return CriticStageResult(
                    state=new_scan.state,
                    outcome=critic_policy.OUTCOME_REJECTED,
                    authoritative=new_scan.authoritative,
                    failed=True,
                    error="CRITIC_REPAIR_BROWSER_QA_REGRESSION",
                    attempts_used=self.budget.attempts_used,
                    reason="browser_qa_regression",
                    history=history,
                    scan=new_scan,
                    classification=new_classified,
                )

            # A critic that degrades or fails after a repair cannot certify the
            # result: stop with an explicit degraded failure rather than
            # accepting an unverifiable state.
            if new_scan.state in (critic_policy.DEGRADED, critic_policy.FAILED, critic_policy.NOT_RUN):
                return CriticStageResult(
                    state=new_scan.state,
                    outcome=critic_policy.OUTCOME_REJECTED,
                    authoritative=False,
                    failed=True,
                    error="CRITIC_DEGRADED_AFTER_REPAIR",
                    attempts_used=self.budget.attempts_used,
                    reason="critic_degraded_after_repair",
                    history=history,
                    scan=new_scan,
                    classification=new_classified,
                )

            if convergence.state == critic_policy.CONVERGENCE_WORSENED:
                return CriticStageResult(
                    state=new_scan.state,
                    outcome=critic_policy.OUTCOME_WORSENED,
                    authoritative=new_scan.authoritative,
                    failed=True,
                    error="CRITIC_REPAIR_WORSENED",
                    attempts_used=self.budget.attempts_used,
                    reason=(
                        "new_blocking_finding" if convergence.new_blockers
                        else "increased_severity"
                    ),
                    history=history,
                    scan=new_scan,
                    classification=new_classified,
                )

            if convergence.state == critic_policy.CONVERGENCE_STALLED:
                return CriticStageResult(
                    state=new_scan.state,
                    outcome=critic_policy.OUTCOME_STALLED,
                    authoritative=new_scan.authoritative,
                    failed=True,
                    error="CRITIC_REPAIR_STALLED",
                    attempts_used=self.budget.attempts_used,
                    reason="no_meaningful_improvement",
                    history=history,
                    scan=new_scan,
                    classification=new_classified,
                )

            # FULLY_VALIDATED: the re-scan found nothing actionable.
            if convergence.state == critic_policy.CONVERGENCE_FULLY_VALIDATED:
                if new_scan.authoritative:
                    return CriticStageResult(
                        state=new_scan.state,
                        outcome=critic_policy.OUTCOME_REPAIRED,
                        authoritative=True,
                        attempts_used=self.budget.attempts_used,
                        reason="acceptance_gates_passed",
                        history=history,
                        scan=new_scan,
                        classification=new_classified,
                    )
                # Findings cleared but the scan is not authoritative -> the
                # result cannot be certified. Continue only if budget remains;
                # otherwise it is an explicit exhausted failure.
                if self.budget.exhausted:
                    return CriticStageResult(
                        state=new_scan.state,
                        outcome=critic_policy.OUTCOME_EXHAUSTED,
                        authoritative=False,
                        failed=True,
                        error="CRITIC_REPAIR_BUDGET_EXHAUSTED",
                        attempts_used=self.budget.attempts_used,
                        reason="critic_degraded_after_repair",
                        history=history,
                        scan=new_scan,
                        classification=new_classified,
                    )

            # IMPROVED (or a non-authoritative clean with budget left):
            # continue to the next bounded attempt.
            classified = new_classified
            scan = new_scan


__all__ = ["CriticStage", "CriticStageResult", "MAX_HISTORY"]
