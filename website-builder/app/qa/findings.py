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
    """

    pass_: bool
    critical: List[str] = field(default_factory=list)
    major: List[str] = field(default_factory=list)
    minor: List[str] = field(default_factory=list)
    summary: str = ""
    raw_error: Optional[str] = None

    @property
    def blocking(self) -> bool:
        """VISION findings are blocking when critical/major findings are
        present, OR when VISION itself failed to run (raw_error set).

        A VISION runtime failure (image attach failure, unsupported vision
        model, provider error, malformed JSON, missing screenshot) must
        never be silently treated as "no findings" — that would let a QA
        attempt pass without VISION ever having actually inspected the
        pixels. Fail closed.
        """
        return bool(self.critical) or bool(self.major) or bool(self.raw_error)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "pass": self.pass_,
            "critical": self.critical,
            "major": self.major,
            "minor": self.minor,
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

    @property
    def repair_required(self) -> bool:
        det_blocking = self.deterministic.blocking
        vis_blocking = bool(self.vision and self.vision.blocking)
        return det_blocking or vis_blocking

    @property
    def final_pass(self) -> bool:
        return not self.repair_required

    def to_dict(self) -> Dict[str, Any]:
        return {
            "attempt": self.attempt,
            "deterministic": self.deterministic.to_dict(),
            "vision": self.vision.to_dict() if self.vision else None,
            "desktop_screenshot": self.desktop_screenshot,
            "mobile_screenshot": self.mobile_screenshot,
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
