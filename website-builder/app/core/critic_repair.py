"""Production seam: run Impeccable against a project and report its STATE.

This is the thin adapter between the verified Impeccable execution contract
(:mod:`app.core.design_critic`) and the D3b orchestration policy
(:mod:`app.core.critic_policy`). It answers ONE question the orchestrator must
not answer for itself: **what state is the critic in for THIS project, and was
the intended project actually scanned at full quality?**

The critical distinction it exists to preserve::

    ok=True, findings=[]                          # NOT a certification
    authoritative=True, intended_project_scanned=True, findings=[]   # a certification

A degraded (regex-fallback) scan, a scan of a project the engine never opened,
and a failed scan are all reported as their true state -- never collapsed into
a clean pass. The engine is invoked through the EXISTING, unmodified
:func:`app.core.design_critic.run_critic_scan`; the argv stays fixed
(``detect --json --quiet .``), ``shell=False``, bounded, and interpreter-explicit.

The scanner never installs anything. A missing engine is a reported absence.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Tuple

from app.core import critic_policy
from app.core.design_activation import (
    engine_quality,
    resolve_engine_path,
)
from app.core.design_critic import (
    CriticOutcome,
    run_critic_scan,
    scan_is_authoritative,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class CriticScanResult:
    """The critic's state for ONE project at ONE revision.

    ``state`` is one of :data:`app.core.critic_policy.CRITIC_STATES`.
    ``authoritative`` is True ONLY for a full-quality scan of the intended
    project with a clean result or real findings -- never for a degraded,
    unscanned, or failed run.
    """

    state: str
    authoritative: bool
    intended_project_scanned: bool
    engine_quality: str
    findings: Tuple[Any, ...] = ()
    reasons: Tuple[str, ...] = ()
    degraded: bool = False

    @property
    def is_clean_certification(self) -> bool:
        return (
            self.state == critic_policy.CLEAN
            and self.authoritative
            and self.intended_project_scanned
        )

    def to_dict(self) -> dict:
        return {
            "state": self.state,
            "authoritative": self.authoritative,
            "intended_project_scanned": self.intended_project_scanned,
            "engine_quality": self.engine_quality,
            "findings": [f.to_dict() for f in self.findings],
            "reasons": list(self.reasons),
            "degraded": self.degraded,
        }


def _state_from_outcome(
    outcome: CriticOutcome,
    *,
    quality: str,
    intended_project_scanned: bool,
) -> CriticScanResult:
    """Map a :class:`CriticOutcome` + engine quality onto a critic STATE.

    Rules, in order:

    * A scan that did not succeed is ``FAILED`` (never an empty pass).
    * A degraded scan with no findings is ``DEGRADED`` -- an undercount, not a
      clean certification.
    * A degraded scan WITH findings is still ``FINDINGS`` (the findings are
      real and actionable) but is never authoritative.
    * A full-quality clean scan is ``CLEAN`` only when the intended project was
      actually scanned.
    """
    authoritative = scan_is_authoritative(outcome) and intended_project_scanned

    if not outcome.ok:
        return CriticScanResult(
            state=critic_policy.FAILED,
            authoritative=False,
            intended_project_scanned=intended_project_scanned,
            engine_quality=quality,
            findings=(),
            reasons=tuple(outcome.reasons) or ("critic_scan_failed",),
            degraded=outcome.degraded,
        )

    if outcome.degraded and not outcome.findings:
        return CriticScanResult(
            state=critic_policy.DEGRADED,
            authoritative=False,
            intended_project_scanned=intended_project_scanned,
            engine_quality=quality,
            findings=(),
            reasons=tuple(outcome.reasons),
            degraded=True,
        )

    if outcome.findings:
        return CriticScanResult(
            state=critic_policy.FINDINGS,
            authoritative=authoritative,
            intended_project_scanned=intended_project_scanned,
            engine_quality=quality,
            findings=tuple(outcome.findings),
            reasons=tuple(outcome.reasons),
            degraded=outcome.degraded,
        )

    # ok, no findings.
    if not authoritative:
        return CriticScanResult(
            state=critic_policy.DEGRADED,
            authoritative=False,
            intended_project_scanned=intended_project_scanned,
            engine_quality=quality,
            findings=(),
            reasons=tuple(outcome.reasons) or ("critic_not_authoritative",),
            degraded=outcome.degraded,
        )
    return CriticScanResult(
        state=critic_policy.CLEAN,
        authoritative=True,
        intended_project_scanned=True,
        engine_quality=quality,
        findings=(),
        reasons=tuple(outcome.reasons),
        degraded=False,
    )


class ImpeccableScanner:
    """Runs the provisioned Impeccable engine against a project workspace.

    ``skill_root`` is the provisioned profile skill directory
    (``$HERMES_HOME/skills/impeccable``). ``node_executable`` is the declared
    Node interpreter, resolved by the APPLICATION (``compose``); this class
    never searches ``PATH`` itself and never installs anything.

    ``runner`` is injectable so offline tests can drive every branch without
    executing the engine.
    """

    def __init__(
        self,
        *,
        skill_root: Optional[Path],
        node_executable: Optional[str],
        timeout_seconds: int = 120,
        runner: Any = None,
    ):
        self.skill_root = Path(skill_root) if skill_root is not None else None
        self.node_executable = node_executable
        self.timeout_seconds = timeout_seconds
        self._runner = runner

    def scan(self, workspace: Path) -> CriticScanResult:
        """Scan ``workspace`` as the critic's fixed target (``cwd``).

        The engine's argv is unchanged: the fixed ``.`` target means the scan
        reads the project at ``workspace`` (the process cwd), never STDIN.
        """
        workspace = Path(workspace)

        if self.skill_root is None:
            return CriticScanResult(
                state=critic_policy.NOT_RUN,
                authoritative=False,
                intended_project_scanned=False,
                engine_quality="missing",
                reasons=("engine_unavailable",),
            )

        quality = engine_quality(self.skill_root)
        engine = resolve_engine_path(self.skill_root)

        if engine is None or quality == "missing":
            # A reported absence, never a pass. No download, no PATH search.
            return CriticScanResult(
                state=critic_policy.DEGRADED,
                authoritative=False,
                intended_project_scanned=False,
                engine_quality="missing",
                reasons=("engine_unavailable",),
            )

        if not isinstance(self.node_executable, str) or not self.node_executable.strip():
            return CriticScanResult(
                state=critic_policy.DEGRADED,
                authoritative=False,
                intended_project_scanned=False,
                engine_quality=quality,
                reasons=("engine_unavailable",),
            )

        if not workspace.is_dir():
            # The intended project is not where we think it is: an uncertain
            # workspace identity is NOT a clean scan.
            return CriticScanResult(
                state=critic_policy.FAILED,
                authoritative=False,
                intended_project_scanned=False,
                engine_quality=quality,
                reasons=("workspace_identity_uncertain",),
            )

        outcome = run_critic_scan(
            engine,
            node_executable=self.node_executable,
            # ``skill_root`` is the scan's cwd: the fixed ``.`` target resolves
            # to the PROJECT workspace, not the skill directory.
            skill_root=workspace,
            timeout_seconds=self.timeout_seconds,
            runner=self._runner,
        )

        # The scan ran against the intended project by construction: the cwd
        # was the workspace and the target was the fixed ``.``.
        intended = True
        result = _state_from_outcome(
            outcome, quality=quality, intended_project_scanned=intended
        )
        logger.info(
            "Impeccable critic scan: state=%s authoritative=%s quality=%s findings=%d",
            result.state, result.authoritative, quality, len(result.findings),
        )
        return result


def scanner_from_config(
    *,
    hermes_home: Optional[Path],
    node_executable: Optional[str],
    timeout_seconds: int = 120,
    runner: Any = None,
) -> Optional[ImpeccableScanner]:
    """Build the production scanner from runtime config.

    Returns ``None`` when there is no profile home to resolve the skill from --
    the caller then records an explicit ``NOT_RUN`` degraded state rather than
    pretending a scan happened.
    """
    if hermes_home is None:
        return None
    from app.core.design_resources import design_profile_skills_dir

    skill_root = design_profile_skills_dir(hermes_home) / "impeccable"
    return ImpeccableScanner(
        skill_root=skill_root,
        node_executable=node_executable,
        timeout_seconds=timeout_seconds,
        runner=runner,
    )


__all__ = [
    "CriticScanResult",
    "ImpeccableScanner",
    "scanner_from_config",
]
