"""The post-preview shadow evaluation entry point.

Preview delivery must not wait for scoring. So the shadow run is NOT part of
the build: it is a separate, explicitly initiated step that an operator
starts AFTER the preview has already been delivered and shown to the user.

This module is the operator-facing surface. It:

* refuses to run unless shadow mode is explicitly enabled;
* refuses to run unless the preview has already been delivered, because a
  shadow run that could delay a preview is a shadow run in the wrong place;
* never touches the live candidate state, and never returns anything the
  engine could rank;
* records the outcome, whether it succeeded, failed, timed out or was
  refused.

There is no service, no queue, no daemon and no scheduler. A shadow run is a
call.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from app.core.shadow_runner import (
    ShadowConfig,
    ShadowRunResult,
    run_shadow_scoring,
)

logger = logging.getLogger(__name__)


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
    preview.
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
