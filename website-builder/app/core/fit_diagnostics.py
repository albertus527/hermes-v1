"""Per-requirement Requirement Fit diagnostics (N9).

WHY THIS EXISTS
---------------
The aggregate Fit score is one number for a whole page. When a candidate
scored badly, there was no way to tell WHICH requirement drove it, so the
result could not be acted on and could not be checked against a human's
reading of the page.

This module adds a diagnostic layer. It does NOT replace the aggregate score:
the aggregate F remains authoritative for the gate, exactly as before. The
diagnostics are a per-requirement account that sits alongside it.

WHAT THE DIAGNOSTICS ARE NOT
-----------------------------
* They are not a recomputation of F. Nothing here changes the aggregate.
* They are not a second score. A diagnostic is a STATUS, not a number.
* They are not explanations the model did not give. A diagnostic is derived
  from a model answer, and where the model said nothing the diagnostic is
  ``unverified`` -- never a guess.

THE THREE SOURCES OF TRUTH, KEPT APART
--------------------------------------
Every diagnostic row carries its basis explicitly:

  ``model``        the model judged this requirement and said which status
  ``deterministic`` a measurement in the evidence settles it (for example a
                   measured contrast ratio, or the absence of a section in
                   the rendered DOM)
  ``unresolved``   the evidence could not settle it

A deterministic basis is only used when a MEASUREMENT exists. The absence of a
requirement is not a model judgment, and it is reported as such.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

#: The four diagnostic states.
DIAG_SATISFIED = "satisfied"
DIAG_PARTIAL = "partial"
DIAG_ABSENT = "absent"
DIAG_UNVERIFIED = "unverified"

DIAGNOSTIC_STATUSES: Tuple[str, ...] = (
    DIAG_SATISFIED,
    DIAG_PARTIAL,
    DIAG_ABSENT,
    DIAG_UNVERIFIED,
)

#: The basis a diagnostic rests on. Kept explicit so a reader never has to
#: guess whether a status came from a measurement, a model judgment, or
#: nothing at all.
BASIS_MODEL = "model"
BASIS_DETERMINISTIC = "deterministic"
BASIS_UNRESOLVED = "unresolved"

#: The choice-option labels a diagnostic question uses.
OPTION_SATISFIED = "satisfied"
OPTION_PARTIAL = "partial"
OPTION_ABSENT = "absent"
OPTION_UNVERIFIED = "unverified"

#: The complete set of options a diagnostic question may offer. It is a
#: CLOSED set: a validator compares against this rather than against a
#: hand-written list, so the question builder and the contract checker can
#: never drift apart.
FIT_REQUIRED_OPTIONS: Tuple[str, ...] = (
    OPTION_SATISFIED,
    OPTION_PARTIAL,
    OPTION_ABSENT,
    OPTION_UNVERIFIED,
)

#: Maps a model's chosen option to a diagnostic status. An unknown option is
#: NOT mapped to a status: it becomes ``unverified``, because guessing which
#: status the model meant would be inventing a judgment.
_OPTION_TO_STATUS: Dict[str, str] = {
    OPTION_SATISFIED: DIAG_SATISFIED,
    OPTION_PARTIAL: DIAG_PARTIAL,
    OPTION_ABSENT: DIAG_ABSENT,
    OPTION_UNVERIFIED: DIAG_UNVERIFIED,
}

#: Question-key prefix for a diagnostic question. Kept separate from the
#: scoring question namespace so a diagnostic answer can never be mistaken for
#: a dimension score.
DIAGNOSTIC_PREFIX = "fitdiag"


class FitDiagnosticsError(ValueError):
    """The diagnostic request or its response is malformed."""


@dataclass(frozen=True)
class RequirementDiagnostic:
    """One requirement, judged for one candidate.

    ``status`` is the diagnostic verdict. ``basis`` says where it came from.
    ``evidence`` records what was actually available when the verdict was
    reached -- including, importantly, when it was NOT enough.
    """

    requirement_id: str
    text: str
    trace: str
    requirement_version: str
    evidence_modality: str
    status: str
    basis: str
    evidence: str = ""
    candidate_id: str = ""
    model_option: str = ""

    def __post_init__(self) -> None:
        if self.status not in DIAGNOSTIC_STATUSES:
            raise FitDiagnosticsError(f"unknown diagnostic status {self.status!r}")
        if self.basis not in (BASIS_MODEL, BASIS_DETERMINISTIC, BASIS_UNRESOLVED):
            raise FitDiagnosticsError(f"unknown diagnostic basis {self.basis!r}")

    @property
    def is_resolved(self) -> bool:
        """True when the requirement was actually decided."""
        return self.status != DIAG_UNVERIFIED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "requirement_id": self.requirement_id,
            "text": self.text,
            "trace": self.trace,
            "requirement_version": self.requirement_version,
            "evidence_modality": self.evidence_modality,
            "status": self.status,
            "basis": self.basis,
            "evidence": self.evidence,
            "candidate_id": self.candidate_id,
            "model_option": self.model_option,
        }


@dataclass(frozen=True)
class FitDiagnosticReport:
    """Diagnostics for one candidate, plus the aggregate they sit beside.

    The aggregate F is carried through UNCHANGED. It stays authoritative: no
    consumer may recompute or replace it from the statuses here.
    """

    candidate_id: str
    requirement_version: str
    aggregate_fit: Optional[float]
    diagnostics: Tuple[RequirementDiagnostic, ...] = ()
    source_revision: str = ""
    notes: Tuple[str, ...] = field(default_factory=tuple)

    def by_id(self, requirement_id: str) -> Optional[RequirementDiagnostic]:
        for d in self.diagnostics:
            if d.requirement_id == requirement_id:
                return d
        return None

    def counts(self) -> Dict[str, int]:
        out = {s: 0 for s in DIAGNOSTIC_STATUSES}
        for d in self.diagnostics:
            out[d.status] += 1
        return out

    def to_dict(self) -> Dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "requirement_version": self.requirement_version,
            "source_revision": self.source_revision,
            "aggregate_fit": self.aggregate_fit,
            "aggregate_is_authoritative": True,
            "counts": self.counts(),
            "diagnostics": [d.to_dict() for d in self.diagnostics],
            "notes": list(self.notes),
        }


# ---------------------------------------------------------------------------
# building the diagnostic questions
# ---------------------------------------------------------------------------


def diagnostic_question_key(candidate_id: str, requirement_id: str) -> str:
    """The question key for one (candidate, requirement) diagnostic.

    The candidate id is NOT used: an untrusted id must never enter a question
    key namespace, exactly as in the scoring path.
    """
    safe = "".join(ch for ch in str(requirement_id) if ch.isalnum() or ch in "-_")
    if not safe:
        raise FitDiagnosticsError("requirement id has no usable characters")
    return f"{DIAGNOSTIC_PREFIX}_{safe}"


def build_diagnostic_questions(
    candidates: Sequence[Any],
    requirements: Sequence[Mapping[str, Any]],
) -> Dict[str, dict]:
    """One choice question per (candidate, requirement).

    ``requirements`` must be the STRUCTURED entries from the N9 artifact, each
    carrying at least ``id``, ``text`` and ``trace``. Plain strings are not
    accepted: a diagnostic without an id and a trace cannot be reported
    honestly, and that is the whole point of this layer.
    """
    questions: Dict[str, dict] = {}
    for index, candidate in enumerate(candidates):
        cid = str(getattr(candidate, "candidate_id", "") or "")
        for requirement in requirements:
            rid = str(requirement.get("id", "") or "")
            text = str(requirement.get("text", "") or "")
            trace = str(requirement.get("trace", "") or "")
            if not rid or not text:
                raise FitDiagnosticsError(
                    "a diagnostic requirement needs an id and text"
                )
            key = f"{DIAGNOSTIC_PREFIX}{index}_{rid}"
            questions[key] = {
                "type": "choice",
                "instructions": (
                    f"For requirement {rid} of candidate {index}, decide from the "
                    f"attached screenshot(s) only: is this requirement "
                    f"SATISFIED, PARTIAL, ABSENT, or UNVERIFIABLE?\n"
                    f"Requirement {rid}: {text}\n"
                    f"Choose UNVERIFIABLE if the screenshot cannot settle it. "
                    f"Do not guess, and do not judge anything other than this "
                    f"one requirement."
                ),
                "criteria": {
                    OPTION_SATISFIED: f"{rid} is satisfied by this candidate",
                    OPTION_PARTIAL: f"{rid} is partially satisfied",
                    OPTION_ABSENT: f"{rid} is absent",
                    OPTION_UNVERIFIED: f"{rid} cannot be judged from the screenshot",
                },
            }
    return questions


# ---------------------------------------------------------------------------
# parsing the model's answers
# ---------------------------------------------------------------------------


def parse_diagnostic_answers(
    answers: Mapping[str, Any],
    *,
    candidates: Sequence[Any],
    requirements: Sequence[Mapping[str, Any]],
    requirement_version: str,
    aggregate_fit: Optional[float] = None,
    source_revision: str = "",
) -> Tuple[FitDiagnosticReport, ...]:
    """Turn raw answers into one report per candidate.

    A missing, malformed or unrecognised answer becomes ``unverified`` with
    basis ``unresolved``. It never becomes a status the model did not choose.
    """
    reports = []
    for index, candidate in enumerate(candidates):
        cid = str(getattr(candidate, "candidate_id", "") or "")
        rows = []
        for requirement in requirements:
            rid = str(requirement.get("id", "") or "")
            key = f"{DIAGNOSTIC_PREFIX}{index}_{rid}"
            answer = answers.get(key) if isinstance(answers, Mapping) else None
            option = ""
            if isinstance(answer, Mapping):
                raw = answer.get("choice")
                if isinstance(raw, str):
                    option = raw.strip().lower()
            status = _OPTION_TO_STATUS.get(option)
            if status is None:
                status = DIAG_UNVERIFIED
                basis = BASIS_UNRESOLVED
                evidence = (
                    "the model did not return a usable status for this requirement"
                    if option else
                    "no diagnostic answer was returned for this requirement"
                )
            else:
                basis = BASIS_MODEL
                evidence = (
                    f"the model judged this requirement {option} from the "
                    f"attached screenshot"
                )
            rows.append(RequirementDiagnostic(
                requirement_id=rid,
                text=str(requirement.get("text", "") or ""),
                trace=str(requirement.get("trace", "") or ""),
                requirement_version=requirement_version,
                evidence_modality=str(
                    requirement.get("evidence_kind", "") or "unknown"),
                status=status,
                basis=basis,
                evidence=evidence,
                candidate_id=cid,
                model_option=option,
            ))
        reports.append(FitDiagnosticReport(
            candidate_id=cid,
            requirement_version=requirement_version,
            aggregate_fit=aggregate_fit,
            diagnostics=tuple(rows),
            source_revision=source_revision,
        ))
    return tuple(reports)


def deterministic_diagnostic(
    *,
    requirement_id: str,
    text: str,
    trace: str,
    requirement_version: str,
    evidence_modality: str,
    status: str,
    evidence: str,
    candidate_id: str = "",
) -> RequirementDiagnostic:
    """Build a diagnostic whose basis is a MEASUREMENT, not a model judgment.

    This is the only sanctioned way to produce a deterministic diagnostic: the
    caller must supply the measurement that justifies the status. A status
    without evidence cannot be constructed.
    """
    if not evidence.strip():
        raise FitDiagnosticsError(
            "a deterministic diagnostic needs the measurement that justifies it"
        )
    return RequirementDiagnostic(
        requirement_id=requirement_id,
        text=text,
        trace=trace,
        requirement_version=requirement_version,
        evidence_modality=evidence_modality,
        status=status,
        basis=BASIS_DETERMINISTIC,
        evidence=evidence,
        candidate_id=candidate_id,
    )


def assert_aggregate_intact(
    report: FitDiagnosticReport,
    aggregate_fit: Optional[float],
) -> None:
    """Guard: the aggregate must not have been changed by the diagnostics.

    Called wherever diagnostics are attached to a scored candidate. It raises
    rather than silently correcting, because a mismatch here means something
    recomputed the aggregate from the statuses -- which is exactly the thing
    this layer must never do.
    """
    if report.aggregate_fit != aggregate_fit:
        raise FitDiagnosticsError(
            "the aggregate Fit score must be carried through unchanged: "
            f"report has {report.aggregate_fit!r}, scored has {aggregate_fit!r}"
        )


__all__ = [
    "BASIS_DETERMINISTIC",
    "BASIS_MODEL",
    "BASIS_UNRESOLVED",
    "DIAG_ABSENT",
    "DIAG_PARTIAL",
    "DIAG_SATISFIED",
    "DIAG_UNVERIFIED",
    "DIAGNOSTIC_PREFIX",
    "DIAGNOSTIC_STATUSES",
    "FIT_REQUIRED_OPTIONS",
    "FitDiagnosticReport",
    "FitDiagnosticsError",
    "RequirementDiagnostic",
    "assert_aggregate_intact",
    "build_diagnostic_questions",
    "deterministic_diagnostic",
    "diagnostic_question_key",
    "parse_diagnostic_answers",
]
