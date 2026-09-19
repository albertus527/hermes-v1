"""QA findings data structures for Website Builder R1 Phase 8.

One small application-owned QA result structure. No second state machine.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class VisionFindings:
    """Structured VISION evidence-only result.

    VISION never mutates lifecycle or files. This is pure evidence.

    VISION is a narrow visual acceptance reviewer, not a second designer or
    a requirements author. Its findings are split into two disjoint classes:

    - ``blocking_findings``: concrete visible defects or violations of an
      EXPLICIT requirement supplied to VISION (brief / Design DNA). Only
      these may consume a FRONTEND repair attempt.
    - ``observations``: subjective feedback, suggestions, or inferences not
      explicitly required by the supplied intent. These never block, never
      consume a repair attempt, and are never promoted into requirements.
    """

    pass_: bool
    blocking_findings: List[str] = field(default_factory=list)
    observations: List[str] = field(default_factory=list)
    summary: str = ""
    raw_error: Optional[str] = None

    @property
    def blocking(self) -> bool:
        """VISION blocks only on concrete visible defects / explicit
        requirement violations, OR when VISION itself failed to run
        (raw_error set).

        A VISION runtime failure (image attach failure, unsupported vision
        model, provider error, malformed JSON, missing screenshot) must
        never be silently treated as "no findings" — that would let a QA
        attempt pass without VISION ever having actually inspected the
        pixels. Fail closed on infrastructure; fail open (non-blocking) on
        subjective design interpretation.
        """
        return bool(self.blocking_findings) or bool(self.raw_error)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pass": self.pass_,
            "blocking": self.blocking_findings,
            "observations": self.observations,
            "summary": self.summary,
            "raw_error": self.raw_error,
        }


@dataclass
class DeterministicFindings:
    """Deterministic, application-owned QA findings."""

    render_ok: bool = False
    desktop_screenshot_ok: bool = False
    mobile_screenshot_ok: bool = False
    design_dna_valid: bool = False
    source_present: bool = False
    build_ok: bool = False
    typecheck_ok: bool = False
    failures: List[str] = field(default_factory=list)

    @property
    def blocking(self) -> bool:
        return bool(self.failures)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "render_ok": self.render_ok,
            "desktop_screenshot_ok": self.desktop_screenshot_ok,
            "mobile_screenshot_ok": self.mobile_screenshot_ok,
            "design_dna_valid": self.design_dna_valid,
            "source_present": self.source_present,
            "build_ok": self.build_ok,
            "typecheck_ok": self.typecheck_ok,
            "failures": self.failures,
        }


@dataclass
class QAAttempt:
    """Result of a single QA attempt (initial pass or post-repair verification)."""

    attempt: int
    deterministic: DeterministicFindings
    vision: Optional[VisionFindings]
    desktop_screenshot: Optional[str] = None
    mobile_screenshot: Optional[str] = None
    infrastructure_error: Optional[str] = None

    @property
    def repair_required(self) -> bool:
        if self.infrastructure_error:
            return False
        det_blocking = self.deterministic.blocking
        vis_blocking = bool(self.vision and self.vision.blocking)
        return det_blocking or vis_blocking

    @property
    def final_pass(self) -> bool:
        return not self.infrastructure_error and not self.repair_required

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt": self.attempt,
            "deterministic": self.deterministic.to_dict(),
            "vision": self.vision.to_dict() if self.vision else None,
            "desktop_screenshot": self.desktop_screenshot,
            "mobile_screenshot": self.mobile_screenshot,
            "infrastructure_error": self.infrastructure_error,
            "repair_required": self.repair_required,
            "final_pass": self.final_pass,
        }


@dataclass
class QAResult:
    """Overall Phase 8 QA result for a project."""

    project_id: str
    success: bool
    attempts: List[QAAttempt] = field(default_factory=list)
    repair_attempts: int = 0
    error: Optional[str] = None

    @property
    def final_attempt(self) -> Optional[QAAttempt]:
        return self.attempts[-1] if self.attempts else None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "project_id": self.project_id,
            "success": self.success,
            "attempts": [a.to_dict() for a in self.attempts],
            "repair_attempts": self.repair_attempts,
            "error": self.error,
        }
