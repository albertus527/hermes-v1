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
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple

from app.core.shadow_scoring import (
    SHADOW_CONTRACT_VERSION,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_INDETERMINATE,
    STATE_PENDING,
    TERMINAL_STATES,
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
STATUS_INDETERMINATE = "indeterminate_previous_outcome"
STATUS_STORAGE_FAILED = "storage_failed"
STATUS_NOT_ENABLED = "not_enabled"
STATUS_PENDING = "pending"

#: The statuses that mean the attempt degraded, i.e. something failed
#: rather than the measurement simply being inconclusive.
DEGRADED_STATUSES = frozenset({
    STATUS_PROVIDER_FAILED,
    STATUS_TIMEOUT,
    STATUS_MALFORMED,
    STATUS_STORAGE_FAILED,
})

#: The failures that happened AFTER the dispatch. The request was sent, so
#: it may already have been billed, so the outcome of that paid call is
#: UNKNOWN. These get the INDETERMINATE state, which blocks an ordinary
#: resume from re-dispatching.
POST_DISPATCH_STATUSES = frozenset({
    STATUS_PROVIDER_FAILED,
    STATUS_TIMEOUT,
    STATUS_MALFORMED,
})


class ShadowNotReady(Exception):
    """The preconditions for a shadow run are not met."""


@dataclass(frozen=True)
class ShadowEvaluationRequest:
    """Everything one shadow evaluation needs, and nothing more.

    The brief is passed in so the scoring call can be made, but it is never
    written into the record: the record carries scores and identifiers, not
    the owner's words.
    """

    project_id: str
    source_revision: str
    candidates: Sequence[Any]
    brief: Mapping[str, Any]
    requirements_version: str = ""
    requirement_set_id: str = ""


def preview_already_delivered(state: Any) -> bool:
    """Whether a preview for this revision has already been shown.

    This is the same durable fact the build consults to decide whether a
    preview may be buried: the lifecycle is PREVIEW_READY and the shown
    preview belongs to the current source revision.
    """
    lifecycle = str(getattr(state, "lifecycle", "") or "")
    deployment = getattr(state, "deployment", None) or {}
    shown = deployment.get("latest_shown_preview") or {}
    revisions = getattr(state, "revisions", None)
    current_revision = str(getattr(revisions, "source_revision", "") or "")
    return (
        lifecycle == "PREVIEW_READY"
        and bool(shown)
        and str(shown.get("source_revision") or "") == current_revision
    )


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


def _frozen_mapping(value: Any) -> Any:
    """Return an immutable, independently constructed view of a mapping.

    A plain ``dict`` is mutable, so sharing one between the live candidate
    and the scoring copy would let the scorer change the live object. A
    ``MappingProxyType`` is immutable but wraps the ORIGINAL dict, so
    mutating the original would still be visible through it. Neither is
    enough: the mapping has to be COPIED and then frozen.
    """
    if value is None:
        return None
    if isinstance(value, Mapping):
        return MappingProxyType(dict(value))
    return value


def _copy_asset(asset: Any) -> Any:
    """Rebuild an asset as an independent, frozen value.

    A candidate's assets are typed ``Tuple[Any, ...]``, so in principle an
    asset could be any object at all, including a mutable one. Rebuilding
    it as a frozen ``DesignAsset`` means the scorer can read it but cannot
    write to it, and cannot write through it to the live asset.
    """
    from app.core.design_reference import DesignAsset

    if isinstance(asset, DesignAsset):
        return DesignAsset(
            kind=asset.kind, uri=asset.uri, revision=asset.revision,
            digest=asset.digest, mime=asset.mime, width=asset.width,
            height=asset.height, bytes=asset.bytes,
        )
    # A duck-typed or mapping asset. Rebuild the fields that matter, and
    # carry the rest through as an immutable mapping so nothing mutable
    # survives.
    if isinstance(asset, Mapping):
        return MappingProxyType(dict(asset))
    fields = {}
    for name in ("kind", "uri", "revision", "digest", "mime", "width",
                 "height", "bytes"):
        if hasattr(asset, name):
            fields[name] = getattr(asset, name)
    if fields:
        return MappingProxyType(fields)
    return asset


def _copy_measurement(measurement: Any) -> Any:
    """Rebuild a measurement as an independent, frozen value.

    ``StructuralEvidence.counts`` is a ``Mapping[str, Any]`` and in
    practice a plain dict, which a hostile scorer could mutate to change
    the live measurement. The copy gets its own dict, frozen.
    """
    from app.core.design_reference import StructuralEvidence

    if not isinstance(measurement, StructuralEvidence):
        if isinstance(measurement, Mapping):
            return MappingProxyType(dict(measurement))
        return measurement
    return StructuralEvidence(
        candidate_id=measurement.candidate_id,
        source_revision=measurement.source_revision,
        viewport=measurement.viewport,
        screenshot_digest=measurement.screenshot_digest,
        measured_at_url=measurement.measured_at_url,
        probe_version=measurement.probe_version,
        font_families=tuple(measurement.font_families),
        type_scale=tuple(measurement.type_scale),
        spacing_scale=tuple(measurement.spacing_scale),
        contrast=tuple(measurement.contrast),
        component_count=measurement.component_count,
        counts=_frozen_mapping(measurement.counts),
        document_height=measurement.document_height,
        collected=measurement.collected,
        error=measurement.error,
    )


def _copy_candidate(candidate: Any) -> Any:
    """Return a DEEP, independent copy of a candidate that carries no
    suitability.

    A shallow copy is not enough. A frozen dataclass prevents rebinding its
    own attributes, but it does not prevent mutating a mutable object
    REACHABLE through one of them -- and a candidate's assets and a
    measurement's ``counts`` are exactly that. So every nested value the
    scorer can reach is rebuilt here as an independent, immutable value.

    The original is never handed to the provider, so the provider cannot
    mutate it even in principle.
    """
    try:
        assets = tuple(
            _copy_asset(a) for a in (getattr(candidate, "assets", ()) or ())
        )
        measurements = tuple(
            _copy_measurement(m)
            for m in (getattr(candidate, "measurements", ()) or ())
        )
        detail = getattr(candidate, "suitability_detail", ()) or ()
        evidence = getattr(candidate, "evidence", ()) or ()
        palette = getattr(candidate, "palette", ()) or ()
        return replace(
            candidate,
            suitability=None,
            suitability_detail=(),
            suitability_contract="",
            assets=assets,
            measurements=measurements,
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


def invocation_identity(
    project_id: str,
    source_revision: str,
    requirement_set_id: str,
    scoring_contract_version: str = SHADOW_CONTRACT_VERSION,
) -> str:
    """The durable identity of one shadow measurement.

    It is derived from the things that DEFINE the measurement, so:

      * the same measurement always has the same identity, which is what
        makes a duplicate detectable across process restarts;
      * a different revision, requirement set or contract can never share
        an identity, so an old revision can never impersonate the current
        one.
    """
    import hashlib

    material = "\x1f".join((
        project_id, str(source_revision), requirement_set_id,
        scoring_contract_version,
    ))
    return "inv-" + hashlib.sha256(material.encode("utf-8")).hexdigest()[:20]


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
                invocation_state=STATE_FAILED,
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
                invocation_state=STATE_FAILED,
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
                invocation_state=STATE_FAILED,
            ))
        return _store(ws, project_id, source_revision, attempts, notes)

    # --- The durable invocation identity. A record with this identity that
    # --- is in a TERMINAL state means the measurement was already made, so
    # --- this is a duplicate: refuse, and spend no call.
    # ---
    # --- A record with this identity that is still PENDING means a
    # --- previous process died between the dispatch and the completion
    # --- write. The outcome of that PAID call is UNKNOWN. It is NOT
    # --- retried: the runner reports it and refuses, because a silent
    # --- retry could spend a second call for a measurement whose first
    # --- call may already have been billed. The operator decides.
    identity = invocation_identity(
        project_id, source_revision, effective_set_id,
        config.scoring_contract_version,
    )
    for previous in _existing_attempts(ws):
        if previous.invocation_id != identity:
            continue
        if previous.invocation_state == STATE_COMPLETED:
            # The measurement was made. Refuse, and spend no call.
            return ShadowRunResult(
                project_id=project_id,
                source_revision=source_revision,
                attempts=(),
                stored=False,
                error=STATUS_DUPLICATE,
            )
        if previous.invocation_state in (STATE_PENDING, STATE_INDETERMINATE):
            # The previous call was SENT and its outcome is unknown. A
            # second call could be billed for a measurement whose first
            # call already happened. Refuse, and say so.
            return ShadowRunResult(
                project_id=project_id,
                source_revision=source_revision,
                attempts=(),
                stored=False,
                error=STATUS_INDETERMINATE,
            )
        # STATE_FAILED: the attempt failed BEFORE the dispatch, so nothing
        # was billed and a retry is safe. Fall through and continue.

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
            # A candidate that cannot be copied is not silently dropped: the
            # attempt is recorded, so a failure is never invisible.
            notes.append(str(exc))
            attempts.append(ShadowAttempt(
                project_id=project_id,
                candidate_id=str(getattr(candidate, "candidate_id", "")),
                source_revision=source_revision,
                requirement_set_id=effective_set_id,
                degraded=False,
                status=STATUS_NOT_ENABLED,
                error_code=STATUS_NOT_ENABLED,
                note=str(exc),
                invocation_state=STATE_FAILED,
            ))
            return _store(ws, project_id, source_revision, attempts, notes)

    if score_fn is None:
        attempts.append(_failed(
            project_id, pool, source_revision, effective_set_id,
            STATUS_NOT_ENABLED, "no shadow scorer was supplied", 0,
        ))
        return _store(ws, project_id, source_revision, attempts, notes)

    # --- THE PENDING WRITE. It happens BEFORE the dispatch, so a process
    # --- death during the paid call leaves a durable record saying the
    # --- outcome is unknown. Without this, a resume would re-dispatch a
    # --- call that may already have been billed.
    pending = [ShadowAttempt(
        project_id=project_id,
        candidate_id=str(getattr(candidate, "candidate_id", "")),
        source_revision=source_revision,
        requirement_set_id=effective_set_id,
        scoring_contract_version=config.scoring_contract_version,
        invocation_id=identity,
        invocation_state=STATE_PENDING,
        status=STATUS_PENDING,
        note="dispatched",
    ) for candidate in pool]
    try:
        write_shadow_record(
            ws, tuple(pending), project_id=project_id,
            source_revision=source_revision,
        )
    except (ShadowScoringError, OSError):
        # The pending marker could not be written, so the call is NOT
        # dispatched: without the marker, a crash would be invisible.
        attempts.append(_failed(
            project_id, pool, source_revision, effective_set_id,
            STATUS_STORAGE_FAILED, "the pending marker could not be written",
            0,
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
            invocation_id=identity,
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
            elapsed, invocation_id=identity,
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
                invocation_id=identity,
            ))
            continue
        suitability = getattr(result, "suitability", None)
        if suitability is None:
            attempts.append(_failed(
                project_id, [candidate], source_revision, effective_set_id,
                STATUS_MALFORMED, f"candidate {cid!r} carries no suitability",
                elapsed, invocation_id=identity,
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
            invocation_id=identity,
            invocation_state=STATE_COMPLETED,
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
    *,
    invocation_id: str = "",
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
        invocation_id=invocation_id,
        invocation_state=(
            STATE_INDETERMINATE if status in POST_DISPATCH_STATUSES
            else STATE_FAILED
        ),
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


def evaluate_after_preview(
    workspace: Any,
    *,
    state: Any,
    request: ShadowEvaluationRequest,
    config: ShadowConfig,
    score_fn: Optional[Callable[..., Sequence[Any]]] = None,
) -> ShadowRunResult:
    """Run one shadow evaluation, after the preview was delivered.

    The build never calls this. An operator does, after the user has their
    preview. It refuses unless the preview for the CURRENT source revision
    has already been delivered, because a shadow run that could delay a
    preview is a shadow run in the wrong place.
    """
    if not config.enabled:
        raise ShadowNotReady(
            "shadow scoring is not enabled; enable it explicitly for this run"
        )
    if not preview_already_delivered(state):
        raise ShadowNotReady(
            "the preview has not been delivered yet; a shadow run must not "
            "be able to delay a preview"
        )
    if not request.candidates:
        raise ShadowNotReady("there are no candidates to score")

    result = run_shadow_scoring(
        Path(workspace),
        project_id=request.project_id,
        config=config,
        candidates=request.candidates,
        source_revision=request.source_revision,
        requirement_set_id=request.requirement_set_id,
        score_fn=score_fn,
        brief=request.brief,
        requirement_version=request.requirements_version,
    )
    logger.info(
        "shadow evaluation finished",
        extra={
            "n8_role": "shadow",
            "n8_project_id": request.project_id,
            "n8_stored": result.stored,
            "n8_attempts": len(result.attempts),
            "n8_produced_suitability": result.produced_suitability,
        },
    )
    return result
