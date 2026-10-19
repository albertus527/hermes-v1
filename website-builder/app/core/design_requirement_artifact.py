"""Structured requirement artifact for N9 (contract v2 implementation).

WHY THIS MODULE EXISTS
----------------------
Design Fit scoring needs requirements that are individually assessable and
provably the owner's, not prose invented by a compiler. The native PRD already
carries a Requirements section, but on a real brief it holds one or two sentences
of free text plus compiler boilerplate, which cannot support a per-requirement
judgement.

This module defines the structured artifact that the Requirement Creator writes
alongside the PRD, and the coverage policy that decides whether the artifact can
support a Fit judgement at all.

WHAT IT REFUSES TO DO
---------------------
* invent a quantitative obligation the user never stated;
* promote an assumption or an unresolved question into a requirement;
* count template boilerplate as project-specific evidence;
* mark a behavioural requirement as failed because a screenshot cannot show it.

HONEST NOTE ON ATOMICITY
------------------------
``document_store.write_document`` is atomic PER FILE (temp file + os.replace).
``store_documents`` writes PRD and Design as two SEPARATE replaces, so it does
NOT guarantee cross-file atomicity. This module therefore does not claim it:
:func:`write_requirement_artifact` writes the artifact with its own atomic
replace, and :func:`verify_artifact_matches_prd` lets a reader detect and recover
from an interrupted write by comparing the three linkage fields. Recovery is
detection plus rewrite, not a distributed transaction.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence, Tuple

from app.core.design_reference_safety import one_line
from app.core.document_contracts import extract_sections, parse_front_matter

#: Bumped when the artifact shape changes incompatibly.
ARTIFACT_SCHEMA_VERSION = 2

#: File name written next to the PRD inside the project workspace.
ARTIFACT_FILE_NAME = "requirements.json"

#: Evidence kinds, in descending order of what a screenshot can establish.
EVIDENCE_VISUAL = "visual"
EVIDENCE_STRUCTURAL = "structural"
EVIDENCE_BEHAVIOURAL = "behavioural"
EVIDENCE_EXTERNAL = "external"
EVIDENCE_KINDS: Tuple[str, ...] = (
    EVIDENCE_VISUAL,
    EVIDENCE_STRUCTURAL,
    EVIDENCE_BEHAVIOURAL,
    EVIDENCE_EXTERNAL,
)

#: How the current pipeline can actually verify a requirement.
VERIFIABLE_SCREENSHOT = "screenshot"
VERIFIABLE_SOURCE = "source"
VERIFIABLE_RUNTIME = "runtime"
VERIFIABLE_NONE = "none"
VERIFIABLE_BY: Tuple[str, ...] = (
    VERIFIABLE_SCREENSHOT,
    VERIFIABLE_SOURCE,
    VERIFIABLE_RUNTIME,
    VERIFIABLE_NONE,
)

#: Requirement kinds.
KIND_CONTENT = "content"
KIND_BEHAVIOUR = "behaviour"
KIND_CONSTRAINT = "constraint"
KIND_STYLE = "style"
KINDS: Tuple[str, ...] = (KIND_CONTENT, KIND_BEHAVIOUR, KIND_CONSTRAINT, KIND_STYLE)

#: Origins. ``template`` marks compiler-emitted boilerplate.
ORIGIN_USER_BRIEF = "user_brief"
ORIGIN_ACCEPTED_CONTEXT = "accepted_context"
ORIGIN_ASSUMPTION = "assumption"
ORIGIN_TEMPLATE = "template"
ORIGINS: Tuple[str, ...] = (
    ORIGIN_USER_BRIEF,
    ORIGIN_ACCEPTED_CONTEXT,
    ORIGIN_ASSUMPTION,
    ORIGIN_TEMPLATE,
)

#: Reasons a coverage assessment can fail. Stable codes.
REASON_OK = ""
REASON_NO_ARTIFACT = "no_artifact"
REASON_MALFORMED = "malformed_artifact"
REASON_NO_SCORABLE = "no_screenshot_scorable_requirements"
REASON_NO_DISCRIMINATING = "no_discriminating_requirement"
REASON_REVISION_MISMATCH = "revision_mismatch"

#: Minimum text length for a requirement to carry a checkable predicate.
MIN_REQUIREMENT_CHARS = 12


class RequirementArtifactError(ValueError):
    """The artifact is structurally invalid."""


@dataclass(frozen=True)
class RequirementEntry:
    """One atomic, individually assessable requirement."""

    id: str
    text: str
    kind: str
    evidence_kind: str
    verifiable_by: str
    origin: str
    trace: str
    mandatory: bool = True
    discriminating: bool = True

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise RequirementArtifactError("empty requirement id")
        if len(one_line(self.text, 300)) < MIN_REQUIREMENT_CHARS:
            raise RequirementArtifactError(
                f"{self.id}: text too short to be assessable"
            )
        if self.kind not in KINDS:
            raise RequirementArtifactError(f"{self.id}: unknown kind {self.kind!r}")
        if self.evidence_kind not in EVIDENCE_KINDS:
            raise RequirementArtifactError(
                f"{self.id}: unknown evidence_kind {self.evidence_kind!r}"
            )
        if self.verifiable_by not in VERIFIABLE_BY:
            raise RequirementArtifactError(
                f"{self.id}: unknown verifiable_by {self.verifiable_by!r}"
            )
        if self.origin not in ORIGINS:
            raise RequirementArtifactError(
                f"{self.id}: unknown origin {self.origin!r}"
            )

    @property
    def is_screenshot_scorable(self) -> bool:
        """Whether a rendered screenshot could settle this requirement.

        Both halves must hold. ``evidence_kind == visual`` says appearance is what
        decides it; ``verifiable_by == screenshot`` says the current pipeline can
        actually produce that evidence. A behavioural requirement fails the first
        test even if a DOM handler exists, because a screenshot cannot prove a
        form submits.
        """
        return (
            self.evidence_kind == EVIDENCE_VISUAL
            and self.verifiable_by == VERIFIABLE_SCREENSHOT
            and self.mandatory
            and self.origin != ORIGIN_ASSUMPTION
            and self.origin != ORIGIN_TEMPLATE
        )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "text": self.text,
            "kind": self.kind,
            "evidence_kind": self.evidence_kind,
            "verifiable_by": self.verifiable_by,
            "origin": self.origin,
            "trace": self.trace,
            "mandatory": self.mandatory,
            "discriminating": self.discriminating,
        }


@dataclass(frozen=True)
class RequirementArtifact:
    """The structured requirements written beside ``PRD.md``."""

    project_id: str
    source_revision: int
    requirements_version: int
    entries: Tuple[RequirementEntry, ...] = ()
    assumptions: Tuple[Mapping[str, Any], ...] = ()
    unresolved: Tuple[Mapping[str, Any], ...] = ()
    schema_version: int = ARTIFACT_SCHEMA_VERSION

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "source_revision": self.source_revision,
            "requirements_version": self.requirements_version,
            "entries": [e.to_dict() for e in self.entries],
            "assumptions": [dict(a) for a in self.assumptions],
            "unresolved": [dict(u) for u in self.unresolved],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n"

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.to_json().encode()).hexdigest()


@dataclass(frozen=True)
class Coverage:
    """Whether a requirement set can support a Fit judgement.

    Replaces a bare count. ``sufficient`` is a judgement about evidence, not
    about size: one specific screenshot-verifiable requirement is enough, and any
    number of boilerplate entries is not.
    """

    sufficient: bool
    reason: str
    scorable: Tuple[str, ...] = ()
    unverified: Tuple[str, ...] = ()
    discriminating: Tuple[str, ...] = ()
    excluded_template: Tuple[str, ...] = ()

    @property
    def scorable_count(self) -> int:
        return len(self.scorable)


def parse_artifact(data: Any) -> RequirementArtifact:
    """Validate a decoded artifact mapping into a :class:`RequirementArtifact`."""
    if not isinstance(data, Mapping):
        raise RequirementArtifactError("artifact is not a mapping")

    schema = data.get("schema_version")
    if not isinstance(schema, int) or schema != ARTIFACT_SCHEMA_VERSION:
        raise RequirementArtifactError(f"unsupported schema_version {schema!r}")

    project_id = str(data.get("project_id") or "").strip()
    if not project_id:
        raise RequirementArtifactError("missing project_id")

    try:
        source_revision = int(str(data.get("source_revision")))
        requirements_version = int(str(data.get("requirements_version")))
    except (TypeError, ValueError):
        raise RequirementArtifactError(
            "source_revision / requirements_version must be integers"
        ) from None

    raw_entries = data.get("entries")
    if not isinstance(raw_entries, Sequence) or isinstance(raw_entries, (str, bytes)):
        raise RequirementArtifactError("entries must be a list")

    entries = []
    seen = set()
    for raw in raw_entries:
        if not isinstance(raw, Mapping):
            raise RequirementArtifactError("entry is not a mapping")
        entry = RequirementEntry(
            id=str(raw.get("id") or "").strip(),
            text=one_line(raw.get("text") or "", 300),
            kind=str(raw.get("kind") or "").strip(),
            evidence_kind=str(raw.get("evidence_kind") or "").strip(),
            verifiable_by=str(raw.get("verifiable_by") or "").strip(),
            origin=str(raw.get("origin") or "").strip(),
            trace=str(raw.get("trace") or "").strip(),
            mandatory=bool(raw.get("mandatory", True)),
            discriminating=bool(raw.get("discriminating", True)),
        )
        if entry.id in seen:
            raise RequirementArtifactError(f"duplicate requirement id {entry.id!r}")
        seen.add(entry.id)
        entries.append(entry)

    def _side(name: str) -> Tuple[Mapping[str, Any], ...]:
        value = data.get(name) or ()
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise RequirementArtifactError(f"{name} must be a list")
        return tuple(dict(v) for v in value if isinstance(v, Mapping))

    return RequirementArtifact(
        project_id=project_id,
        source_revision=source_revision,
        requirements_version=requirements_version,
        entries=tuple(entries),
        assumptions=_side("assumptions"),
        unresolved=_side("unresolved"),
    )


def assess_coverage(artifact: Optional[RequirementArtifact]) -> Coverage:
    """Decide whether the artifact can support a Fit judgement.

    Policy, in order:

    1. keep entries a screenshot could settle (visual + screenshot + mandatory
       + not an assumption + not template boilerplate)
    2. of those, require at least one that can DISCRIMINATE between two designs
    3. sufficient iff the scorable set is non-empty AND discriminating

    There is deliberately no minimum count. A project with one specific
    screenshot-verifiable requirement is assessable; a project with six template
    criteria is not.
    """
    if artifact is None:
        return Coverage(False, REASON_NO_ARTIFACT)

    scorable = []
    unverified = []
    discriminating = []
    template = []

    for entry in artifact.entries:
        if entry.origin == ORIGIN_TEMPLATE:
            template.append(entry.text)
            continue
        if entry.is_screenshot_scorable:
            scorable.append(entry.text)
            if entry.discriminating:
                discriminating.append(entry.text)
        else:
            unverified.append(entry.text)

    if not scorable:
        return Coverage(False, REASON_NO_SCORABLE, (), tuple(unverified),
                        (), tuple(template))
    if not discriminating:
        return Coverage(False, REASON_NO_DISCRIMINATING, tuple(scorable),
                        tuple(unverified), (), tuple(template))
    return Coverage(True, REASON_OK, tuple(scorable), tuple(unverified),
                    tuple(discriminating), tuple(template))


def verify_artifact_matches_prd(
    artifact: RequirementArtifact, prd_text: str
) -> Tuple[bool, str]:
    """Check the three linkage fields against a PRD's front matter.

    ``document_store`` gives per-file atomicity only, so an interrupted build can
    leave a new PRD beside an old artifact. This detects that: it compares
    project_id, source_revision and requirements_version. A mismatch is
    recoverable by rewriting the artifact, not by silently scoring against it.
    """
    if not isinstance(prd_text, str) or not prd_text.strip():
        return False, REASON_NO_ARTIFACT
    try:
        front, _body = parse_front_matter(prd_text)
    except Exception:  # noqa: BLE001
        return False, REASON_MALFORMED

    if str(front.get("project_id") or "").strip() != artifact.project_id:
        return False, REASON_REVISION_MISMATCH
    try:
        if int(str(front.get("source_revision"))) != artifact.source_revision:
            return False, REASON_REVISION_MISMATCH
        if int(str(front.get("requirements_version"))) != artifact.requirements_version:
            return False, REASON_REVISION_MISMATCH
    except (TypeError, ValueError):
        return False, REASON_REVISION_MISMATCH
    return True, REASON_OK


def artifact_path(workspace: Any) -> Any:
    """Path of the artifact inside a project workspace."""
    from pathlib import Path

    return Path(workspace) / ARTIFACT_FILE_NAME


def write_requirement_artifact(workspace: Any, artifact: RequirementArtifact) -> str:
    """Write the artifact atomically and return its digest.

    Atomic PER FILE (temp + ``os.replace``), matching ``write_document``. This is
    NOT a cross-file transaction with ``PRD.md``; see the module docstring.
    """
    path = artifact_path(workspace)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = artifact.to_json()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return hashlib.sha256(text.encode()).hexdigest()


def read_requirement_artifact(workspace: Any) -> Optional[RequirementArtifact]:
    """Read and validate the artifact, or return ``None`` if absent/invalid."""
    path = artifact_path(workspace)
    if not path.exists():
        return None
    try:
        return parse_artifact(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError):
        return None


def artifact_from_prd(prd_text: str) -> Optional[RequirementArtifact]:
    """Build a MINIMAL artifact from a PRD alone, for legacy projects.

    Every entry is derived from the PRD's own bullet text and is marked
    ``evidence_kind=visual`` / ``verifiable_by=screenshot`` / ``origin=template``
    so it cannot silently become Fit evidence. The practical effect is that a
    pre-N9 project produces an artifact with zero scorable entries and therefore
    abstains -- which is the honest outcome, since its PRD never distinguished
    project requirements from compiler boilerplate.
    """
    if not isinstance(prd_text, str) or not prd_text.strip():
        return None
    try:
        front, body = parse_front_matter(prd_text)
        sections = extract_sections(body)
    except Exception:  # noqa: BLE001
        return None

    entries = []
    for name in ("requirements", "acceptance_criteria"):
        for line in (sections.get(name) or "").splitlines():
            text = one_line(line.strip().lstrip("-*+ ").strip(), 300)
            if len(text) < MIN_REQUIREMENT_CHARS:
                continue
            if text.upper().startswith("UNRESOLVED"):
                continue
            entries.append(RequirementEntry(
                id=f"legacy-{len(entries) + 1}",
                text=text,
                kind=KIND_CONTENT,
                evidence_kind=EVIDENCE_VISUAL,
                verifiable_by=VERIFIABLE_SCREENSHOT,
                origin=ORIGIN_TEMPLATE,
                trace=f"prd.{name}",
                mandatory=True,
                discriminating=False,
            ))
    try:
        return RequirementArtifact(
            project_id=str(front.get("project_id") or "").strip() or "unknown",
            source_revision=int(front.get("source_revision") or 0),
            requirements_version=int(front.get("requirements_version") or 0),
            entries=tuple(entries),
        )
    except (TypeError, ValueError):
        return None


__all__ = [
    "ARTIFACT_FILE_NAME",
    "ARTIFACT_SCHEMA_VERSION",
    "Coverage",
    "EVIDENCE_KINDS",
    "EVIDENCE_VISUAL",
    "KINDS",
    "MIN_REQUIREMENT_CHARS",
    "ORIGINS",
    "ORIGIN_TEMPLATE",
    "REASON_MALFORMED",
    "REASON_NO_ARTIFACT",
    "REASON_NO_DISCRIMINATING",
    "REASON_NO_SCORABLE",
    "REASON_OK",
    "REASON_REVISION_MISMATCH",
    "RequirementArtifact",
    "RequirementArtifactError",
    "RequirementEntry",
    "artifact_from_prd",
    "artifact_path",
    "assess_coverage",
    "parse_artifact",
    "read_requirement_artifact",
    "verify_artifact_matches_prd",
    "write_requirement_artifact",
]
