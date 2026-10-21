"""The operator entry point for a shadow evaluation.

This is a THIN operator surface over ``shadow_evaluation``. It exists so the
run is explicit and auditable: an operator names a project, and the command
either performs one shadow evaluation or refuses with a reason.

It is NOT wired into the build, NOT scheduled, and NOT on by default. It
takes the candidate set from the caller, because the candidate set is the
output of the native evidence pipeline and re-deriving it here would mean
this command had a second, divergent way of finding candidates.

USAGE

    python -m app --shadow-evaluate <project_id> \\
        --workspace <path> --revision <n> --config <config.json>

The config file is a JSON object matching ``ShadowConfig``. It must contain
``"enabled": true``; the command refuses to run without it, so a stray
invocation cannot start a shadow run by accident.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from app.core.shadow_evaluation import (
    ShadowEvaluationRequest,
    ShadowNotReady,
    evaluate_after_preview,
)
from app.core.shadow_runner import ShadowConfig

logger = logging.getLogger(__name__)


class ShadowEvaluationError(Exception):
    """The shadow evaluation could not be performed."""


def load_shadow_config(path: Any) -> ShadowConfig:
    """Load the shadow configuration from a JSON file.

    The file must set ``enabled`` explicitly. Absent means absent -- the
    default is disabled, and an operator has to say yes on purpose.
    """
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ShadowEvaluationError(
            f"could not read the shadow config: {exc}"
        ) from exc
    if not isinstance(raw, Mapping):
        raise ShadowEvaluationError("the shadow config is not a JSON object")
    if "enabled" not in raw:
        raise ShadowEvaluationError(
            "the shadow config must state 'enabled' explicitly"
        )
    if raw.get("enabled") is not True:
        raise ShadowEvaluationError(
            "the shadow config does not enable shadow scoring"
        )
    known = {"enabled", "deadline_seconds", "max_candidates", "cost_ceiling",
             "scoring_contract_version"}
    unknown = set(raw) - known
    if unknown:
        raise ShadowEvaluationError(
            f"the shadow config has unknown keys: {sorted(unknown)}"
        )
    return ShadowConfig(**dict(raw))


def run_shadow_evaluation_command(
    *,
    project_id: str,
    workspace: Any,
    state: Any,
    source_revision: str,
    candidates: Sequence[Any],
    brief: Mapping[str, Any],
    config: ShadowConfig,
    score_fn: Optional[Any] = None,
    requirements_version: str = "",
    requirement_set_id: str = "",
) -> dict:
    """Perform one shadow evaluation and return a report.

    The report is the operator-facing summary. It never contains the brief
    or the requirement texts.
    """
    request = ShadowEvaluationRequest(
        project_id=project_id,
        source_revision=source_revision,
        candidates=candidates,
        brief=brief,
        requirements_version=requirements_version,
        requirement_set_id=requirement_set_id,
    )
    try:
        result = evaluate_after_preview(
            workspace,
            state=state,
            request=request,
            config=config,
            score_fn=score_fn,
        )
    except ShadowNotReady as exc:
        raise ShadowEvaluationError(str(exc)) from exc

    return {
        "project_id": project_id,
        "source_revision": source_revision,
        "stored": result.stored,
        "error": result.error,
        "produced_suitability": result.produced_suitability,
        "attempts": [
            {
                "candidate_id": a.candidate_id,
                "status": a.status,
                "degraded": a.degraded,
                "suitability": a.suitability,
                "requirement_set_id": a.requirement_set_id,
                "scoring_contract_version": a.scoring_contract_version,
                "latency_ms": a.latency_ms,
                "cost": a.cost,
            }
            for a in result.attempts
        ],
    }


def shadow_config_help() -> str:
    """The example config an operator can copy."""
    return json.dumps(asdict(ShadowConfig(enabled=True)), indent=2)
