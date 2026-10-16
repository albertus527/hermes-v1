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
from app.core.design_retrieval import CriticFinding
from app.core import font_audit

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

    ``font_audit_enabled`` (C, default True) additionally runs the
    application-owned CSS font audit, which closes the engine's confirmed
    ``var()``/``@font-face`` blind spot for the ``overused-font`` rule. It is a
    pure, in-process check over the workspace's own CSS — no model, no network,
    no execution — and its findings are ordinary ``overused-font`` findings, so
    the existing classification/repair policy applies unchanged.
    """

    def __init__(
        self,
        *,
        skill_root: Optional[Path],
        node_executable: Optional[str],
        timeout_seconds: int = 120,
        runner: Any = None,
        font_audit_enabled: bool = True,
    ):
        self.skill_root = Path(skill_root) if skill_root is not None else None
        self.node_executable = node_executable
        self.timeout_seconds = timeout_seconds
        self._runner = runner
        self._font_audit_enabled = bool(font_audit_enabled)

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
        result = self._merge_font_audit(result, workspace)
        logger.info(
            "Impeccable critic scan: state=%s authoritative=%s quality=%s findings=%d",
            result.state, result.authoritative, quality, len(result.findings),
        )
        return result

    def _merge_font_audit(
        self, result: CriticScanResult, workspace: Path
    ) -> CriticScanResult:
        """Add the application-owned CSS font audit to an engine result (C).

        The audit only ever ADDS ``overused-font`` findings the engine missed
        through ``var()``/``@font-face`` indirection. It never removes an engine
        finding, and it never turns a FAILED/DEGRADED scan into a CLEAN one: a
        scan the engine could not certify stays uncertified, so a font audit
        cannot be used to launder a broken critic.

        Findings already reported by the engine (matched on the lower-cased font
        name in the finding text) are not duplicated.
        """
        if not self._font_audit_enabled:
            return result
        if result.state not in (critic_policy.CLEAN, critic_policy.FINDINGS):
            return result

        try:
            audited = font_audit.audit_workspace(workspace)
        except Exception:  # noqa: BLE001 - an audit must never break a scan
            logger.warning("CSS font audit failed", exc_info=True)
            return result

        if not audited:
            return result

        engine_text = " ".join(
            f"{f.finding} {f.evidence} {f.suggested_action}".lower()
            for f in result.findings
        )
        added = []
        for entry in audited:
            font = str(entry.get("font") or "").lower()
            if not font or font in engine_text:
                continue
            added.append(
                CriticFinding(
                    rule_id=font_audit.RULE_ID,
                    category="slop",
                    severity="warning",
                    finding=(
                        f"Overused font '{font}' is applied to the page "
                        "(detected through CSS variable/@font-face indirection, "
                        "which the engine's literal-text scan does not resolve)."
                    ),
                    evidence=(
                        f"{font} (used via {entry.get('source') or 'CSS'}"
                        + ("; referenced through var()" if entry.get("via_var") else "")
                        + ")"
                    ),
                    suggested_action=(
                        "Choose a face that gives the interface personality; "
                        "the application does not prescribe a specific pairing."
                    ),
                )
            )

        if not added:
            return result

        logger.info(
            "CSS font audit added %d overused-font finding(s) the engine missed",
            len(added),
        )
        return CriticScanResult(
            state=critic_policy.FINDINGS,
            authoritative=result.authoritative,
            intended_project_scanned=result.intended_project_scanned,
            engine_quality=result.engine_quality,
            findings=tuple(result.findings) + tuple(added),
            reasons=result.reasons,
            degraded=result.degraded,
        )


def scanner_from_config(
    *,
    hermes_home: Optional[Path],
    node_executable: Optional[str],
    timeout_seconds: int = 120,
    runner: Any = None,
    font_audit_enabled: bool = True,
) -> Optional[ImpeccableScanner]:
    """Build the production scanner from runtime config.

    Returns ``None`` when there is no profile home to resolve the skill from --
    the caller then records an explicit ``NOT_RUN`` degraded state rather than
    pretending a scan happened.

    ``font_audit_enabled`` (C) is on by default and only ever ADDS overused-font
    findings the engine's literal scan misses; setting it off restores the
    pre-C scanner byte-for-byte.
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
        font_audit_enabled=font_audit_enabled,
    )


__all__ = [
    "CriticScanResult",
    "ImpeccableScanner",
    "scanner_from_config",
]
