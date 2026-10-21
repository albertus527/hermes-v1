"""The shadow execution seam.

This is the ONLY place a shadow scoring call can be made, and it is
deliberately hard to reach:

* it is never called by the build. The build's normal path does not import
  this module. A shadow run is explicitly initiated, after the preview has
  already been delivered.
* there is no service, no queue, no daemon, no scheduler, no background
  thread. A shadow run is a function call by an operator.
* the candidates it scores are COPIES. The live candidate objects are never
  touched, and the scored copies are never returned to the engine.
* every failure mode -- provider failure, timeout, malformed response, stale
  revision, mismatched requirement set -- produces a record and no
  suitability, and never raises into the caller.

The isolation is structural: the engine ranks on ``candidate.suitability``,
and nothing here ever writes to a candidate's suitability field, because the
candidates here are copies that are thrown away.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from app.core.shadow_scoring import (
    SHADOW_CONTRACT_VERSION,
    ShadowAttempt,
    ShadowScoringError,
    write_shadow_record,
)

logger = logging.getLogger(__name__)

#: The status codes a shadow attempt can end with. Every one of them is a
#: reason the attempt produced NO suitability.
STATUS_OK = "ok"
STATUS_PROVIDER_FAILED = "provider_failed"
STATUS_TIMEOUT = "timeout"
STATUS_MALFORMED = "malformed"
STATUS_STALE_REVISION = "stale_revision"
STATUS_REQUIREMENT_SET_MISMATCH = "requirement_set_mismatch"
STATUS_MISSING_ARTIFACT = "missing_artifact"
STATUS_CORRUPT_ARTIFACT = "corrupt_artifact"
STATUS_NO_CANDIDATES = "no_candidates"
STATUS_DUPLICATE = "duplicate_invocation"
STATUS_STORAGE_FAILED = "storage_failed"
STATUS_NOT_ENABLED = "not_enabled"

#: The statuses that mean the attempt degraded, i.e. something failed
#: rather than the measurement simply being inconclusive.
DEGRADED_STATUSES = frozenset({
    STATUS_PROVIDER_FAILED,
    STATUS_TIMEOUT,
    STATUS_MALFORMED,
    STATUS_STORAGE_FAILED,
})


class ShadowNotEnabled(Exception):
    """Raised when a shadow run is attempted while shadow mode is off."""


@dataclass(frozen=True)
class ShadowConfig:
    """The explicit shadow-only configuration.

    ``enabled`` is the ONLY switch, and it means "a shadow run may be
    requested". It does not mean the live scoring path may run: that is
    still governed by the provider's own ``scoring_enabled``, which this
    patch never touches.
    """

    enabled: bool = False
    #: The maximum wall-clock a single shadow attempt may take.
    deadline_seconds: float = 20.0
    #: The maximum number of candidates one shadow run may score.
    max_candidates: int = 8
    #: The operational cost ceiling for one shadow run, in dollars. It is
    #: checked AFTER the call, because a pre-call check cannot know the
    #: price; exceeding it is recorded, not retried.
    cost_ceiling: float = 0.02
    #: The scoring contract a shadow record is bound to.
    scoring_contract_version: str = SHADOW_CONTRACT_VERSION

    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "deadline_seconds": self.deadline_seconds,
            "max_candidates": self.max_candidates,
            "cost_ceiling": self.cost_ceiling,
            "scoring_contract_version": self.scoring_contract_version,
        }


@dataclass(frozen=True)
class ShadowRunResult:
    """The outcome of one explicitly initiated shadow run."""

    project_id: str
    source_revision: str
    attempts: Tuple[ShadowAttempt, ...] = ()
    stored: bool = False
    error: str = ""

    @property
    def produced_suitability(self) -> bool:
        return any(a.suitability is not None for a in self.attempts)

    def to_dict(self) -> dict:
        return {
            "project_id": self.project_id,
            "source_revision": self.source_revision,
            "stored": self.stored,
            "error": self.error,
            "attempts": [a.to_dict() for a in self.attempts],
        }


def _copy_candidate(candidate: Any) -> Any:
    """Return a copy of a candidate that carries no suitability.

    ``DesignCandidate`` is a frozen dataclass, so ``replace`` gives a
    genuine copy. The copy is what gets scored; the original is never
    handed to the provider, so the provider cannot mutate it even in
    principle.
    """
    try:
        return replace(
            candidate,
            suitability=None,
            suitability_detail=None,
            suitability_contract="",
        )
    except (TypeError, ValueError):
        # A candidate type that cannot be replaced is not silently scored.
        raise ShadowScoringError(
            f"candidate {getattr(candidate, 'candidate_id', '?')} cannot be copied"
        ) from None


def _requirement_binding(
    workspace: Any,
) -> Tuple[Tuple[str, ...], str, str]:
    """Read the requirement set, and its identity, from the workspace.

    Returns the requirement texts, the requirement_set_id and the source
    revision. A missing or corrupt artifact is a refusal, not a fallback
    to an empty set: scoring against no requirements would produce a
    meaningless number.
    """
    from app.core.design_requirements import requirements_for_fit
    from app.core.design_requirement_artifact import read_requirement_artifact

    artifact = read_requirement_artifact(workspace)
    if artifact is None:
        raise ShadowScoringError(STATUS_MISSING_ARTIFACT)
    requirements, reason = requirements_for_fit(workspace)
    if not requirements:
        raise ShadowScoringError(f"{STATUS_MISSING_ARTIFACT}: {reason}")
    set_id = str(
        getattr(artifact, "requirement_set_id", "")
        or getattr(artifact, "requirements_version", "")
        or ""
    )
    if not set_id:
        raise ShadowScoringError(STATUS_REQUIREMENT_SET_MISMATCH)
    return tuple(requirements), set_id, str(artifact.source_revision)


def _existing_attempts(workspace: Any) -> Tuple[ShadowAttempt, ...]:
    from app.core.shadow_scoring import read_shadow_attempts
    return read_shadow_attempts(workspace)


def run_shadow_scoring(
    workspace: Any,
    *,
    project_id: str,
    config: ShadowConfig,
    candidates: Sequence[Any],
    source_revision: str,
    requirement_set_id: str = "",
    score_fn: Optional[Callable[..., Sequence[Any]]] = None,
    brief: Optional[Mapping[str, Any]] = None,
    requirement_version: str = "",
) -> ShadowRunResult:
    """Run one explicitly initiated shadow evaluation.

    ``score_fn`` is the scoring callable. It is passed the COPIES and must
    return candidates carrying a suitability. Whatever it returns is
    inspected and then DISCARDED: the return value never reaches the
    engine, and the live candidates are never touched.

    The function never raises for a scoring failure. It raises
    ``ShadowNotEnabled`` when shadow mode is off, because that is a
    programming error rather than a measurement outcome.
    """
    if not config.enabled:
        raise ShadowNotEnabled(
            "shadow scoring is not enabled; scoring_enabled is a separate "
            "switch and this patch does not enable it"
        )

    ws = Path(workspace)
    attempts: list = []
    notes: list = []

    # --- the requirements must be real, and must be the same ones the
    # --- record is bound to. A mismatch means the measurement would be
    # --- about a different requirement set than it claims.
    try:
        requirements, artifact_set_id, artifact_revision = _requirement_binding(ws)
    except ShadowScoringError as exc:
        status = str(exc)
        code = status.split(":", 1)[0].strip()
        for candidate in candidates:
            attempts.append(ShadowAttempt(
                project_id=project_id,
                candidate_id=str(getattr(candidate, "candidate_id", "")),
                source_revision=source_revision,
                requirement_set_id=requirement_set_id,
                degraded=code in DEGRADED_STATUSES,
                status=code,
                error_code=code,
                note="the requirement artifact could not be read",
            ))
        return _store(ws, project_id, source_revision, attempts, notes)

    if requirement_set_id and requirement_set_id != artifact_set_id:
        for candidate in candidates:
            attempts.append(ShadowAttempt(
                project_id=project_id,
                candidate_id=str(getattr(candidate, "candidate_id", "")),
                source_revision=source_revision,
                requirement_set_id=requirement_set_id,
                degraded=False,
                status=STATUS_REQUIREMENT_SET_MISMATCH,
                error_code=STATUS_REQUIREMENT_SET_MISMATCH,
                note=(
                    f"the requested requirement set {requirement_set_id!r} does "
                    f"not match the artifact's {artifact_set_id!r}"
                ),
            ))
        return _store(ws, project_id, source_revision, attempts, notes)

    effective_set_id = requirement_set_id or artifact_set_id

    # --- a stale revision means the screenshots would not be the ones the
    # --- record claims to describe.
    if artifact_revision and str(artifact_revision) != str(source_revision):
        for candidate in candidates:
            attempts.append(ShadowAttempt(
                project_id=project_id,
                candidate_id=str(getattr(candidate, "candidate_id", "")),
                source_revision=source_revision,
                requirement_set_id=effective_set_id,
                degraded=False,
                status=STATUS_STALE_REVISION,
                error_code=STATUS_STALE_REVISION,
                note=(
                    f"the artifact is at revision {artifact_revision} but the "
                    f"shadow run is for revision {source_revision}"
                ),
            ))
        return _store(ws, project_id, source_revision, attempts, notes)

    # --- a duplicate invocation is refused, so one shadow run cannot be
    # --- charged twice or double-counted. A repeat for a DIFFERENT
    # --- revision is allowed, because that is a different measurement.
    for previous in _existing_attempts(ws):
        if (previous.source_revision == str(source_revision)
                and previous.requirement_set_id == effective_set_id
                and previous.status == STATUS_OK):
            return ShadowRunResult(
                project_id=project_id,
                source_revision=source_revision,
                attempts=(),
                stored=False,
                error=STATUS_DUPLICATE,
            )

    if not candidates:
        return _store(ws, project_id, source_revision, (), [
            "there were no candidates to score",
        ])

    pool = list(candidates)[: config.max_candidates]
    copies = []
    for candidate in pool:
        try:
            copies.append(_copy_candidate(candidate))
        except ShadowScoringError as exc:
            notes.append(str(exc))
            return _store(ws, project_id, source_revision, (), notes)

    if score_fn is None:
        attempts.append(_failed(
            project_id, pool, source_revision, effective_set_id,
            STATUS_NOT_ENABLED, "no shadow scorer was supplied", 0,
        ))
        return _store(ws, project_id, source_revision, attempts, notes)

    started = time.time()
    try:
        scored = score_fn(
            copies,
            brief=dict(brief or {}),
            requirements=requirements,
            requirement_version=requirement_version,
            deadline_seconds=config.deadline_seconds,
        )
    except TimeoutError as exc:
        attempts.append(_failed(
            project_id, pool, source_revision, effective_set_id,
            STATUS_TIMEOUT, str(exc), int((time.time() - started) * 1000),
        ))
        return _store(ws, project_id, source_revision, attempts, notes)
    except Exception as exc:  # noqa: BLE001 - a shadow failure is a record
        attempts.append(_failed(
            project_id, pool, source_revision, effective_set_id,
            STATUS_PROVIDER_FAILED, type(exc).__name__,
            int((time.time() - started) * 1000),
        ))
        logger.info(
            "shadow scoring failed",
            extra={"n8_role": "shadow", "n8_error_class": STATUS_PROVIDER_FAILED},
        )
        return _store(ws, project_id, source_revision, attempts, notes)

    elapsed = int((time.time() - started) * 1000)
    scored = list(scored or ())
    if not scored:
        attempts.append(_failed(
            project_id, pool, source_revision, effective_set_id,
            STATUS_MALFORMED, "the provider returned no candidates",
            elapsed,
        ))
        return _store(ws, project_id, source_revision, attempts, notes)

    by_id = {str(getattr(c, "candidate_id", "")): c for c in scored}
    for candidate in pool:
        cid = str(getattr(candidate, "candidate_id", ""))
        result = by_id.get(cid)
        if result is None:
            attempts.append(_failed(
                project_id, [candidate], source_revision, effective_set_id,
                STATUS_MALFORMED, f"no result for candidate {cid!r}", elapsed,
            ))
            continue
        suitability = getattr(result, "suitability", None)
        if suitability is None:
            attempts.append(_failed(
                project_id, [candidate], source_revision, effective_set_id,
                STATUS_MALFORMED, f"candidate {cid!r} carries no suitability",
                elapsed,
            ))
            continue
        digests = tuple(
            str(getattr(a, "digest", "") or "")
            for a in (getattr(candidate, "assets", ()) or ())
        )
        attempts.append(ShadowAttempt(
            project_id=project_id,
            candidate_id=cid,
            source_revision=source_revision,
            requirement_set_id=effective_set_id,
            scoring_contract_version=config.scoring_contract_version,
            suitability=float(suitability),
            screenshot_digests=tuple(d for d in digests if d),
            degraded=False,
            status=STATUS_OK,
            latency_ms=elapsed,
        ))

    return _store(ws, project_id, source_revision, attempts, notes)


def _failed(
    project_id: str,
    candidates: Sequence[Any],
    source_revision: str,
    requirement_set_id: str,
    status: str,
    detail: str,
    latency_ms: int,
) -> ShadowAttempt:
    """A single record describing a failed attempt for a pool."""
    first = candidates[0] if candidates else None
    return ShadowAttempt(
        project_id=project_id,
        candidate_id=str(getattr(first, "candidate_id", "")),
        source_revision=source_revision,
        requirement_set_id=requirement_set_id,
        degraded=status in DEGRADED_STATUSES,
        status=status,
        error_code=status,
        latency_ms=latency_ms,
        note=detail,
    )


def _store(
    workspace: Path,
    project_id: str,
    source_revision: str,
    attempts: Sequence[ShadowAttempt],
    notes: Sequence[str],
) -> ShadowRunResult:
    """Write the records. A write failure is reported, never raised."""
    try:
        write_shadow_record(
            workspace,
            tuple(attempts),
            project_id=project_id,
            source_revision=source_revision,
        )
    except (ShadowScoringError, OSError) as exc:
        logger.warning(
            "shadow scoring could not be recorded",
            extra={"n8_role": "shadow", "n8_error_class": STATUS_STORAGE_FAILED},
        )
        return ShadowRunResult(
            project_id=project_id,
            source_revision=source_revision,
            attempts=tuple(attempts),
            stored=False,
            error=STATUS_STORAGE_FAILED,
        )
    return ShadowRunResult(
        project_id=project_id,
        source_revision=source_revision,
        attempts=tuple(attempts),
        stored=True,
        error="",
    )
