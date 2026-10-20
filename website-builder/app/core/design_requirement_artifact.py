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
ARTIFACT_SCHEMA_VERSION = 3

#: Schema versions this reader understands.
#:
#: Schema 2 artifacts were NOT decomposed. Reading one as if it had been would
#: silently change the requirement set of a saved project, so a schema-2
#: artifact is read as schema 2: its entries stand alone, and each becomes a
#: single-part group. Decomposition happens only when an artifact is REBUILT
#: from the accepted brief, which is the existing staleness-repair path.
ARTIFACT_SCHEMA_VERSION_LEGACY = 2
SUPPORTED_SCHEMA_VERSIONS: Tuple[int, ...] = (
    ARTIFACT_SCHEMA_VERSION_LEGACY,
    ARTIFACT_SCHEMA_VERSION,
)

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

#: Minimum text length for a PART of a decomposed requirement.
#:
#: A part is shorter than a whole requirement because it is one obligation out
#: of a group. "tentang" and "layanan" are seven characters and are perfectly
#: checkable on their own; refusing them would merge the group back together
#: and defeat the decomposition.
MIN_PART_CHARS = 4


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
    #: The group this part belongs to. Empty for a whole (undecomposed)
    #: requirement, and for every entry of a legacy schema-2 artifact.
    group_id: str = ""
    #: The ORIGINAL clause, verbatim. A part is a substring of it, so a reader
    #: can always find the part inside the requirement the owner wrote.
    group_text: str = ""

    def __post_init__(self) -> None:
        if not self.id.strip():
            raise RequirementArtifactError("empty requirement id")
        minimum = MIN_PART_CHARS if self.group_id else MIN_REQUIREMENT_CHARS
        if len(one_line(self.text, 300)) < minimum:
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
            "group_id": self.group_id,
            "group_text": self.group_text,
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
    #: sha256 of the exact PRD text this artifact was derived from.
    #:
    #: The three linkage fields above are not sufficient on their own: a PRD can
    #: be recompiled with different content while the revision counter happens
    #: to be unchanged (a retried build, a manual re-run, a restored workspace).
    #: In that case project_id/revision/version all match and the artifact is
    #: silently stale. The digest closes that hole. Empty means "not recorded",
    #: which is treated as unverifiable rather than as a match.
    prd_digest: str = ""
    schema_version: int = ARTIFACT_SCHEMA_VERSION
    #: The decomposition groups. Each records the ORIGINAL clause, where it
    #: came from, and the ids of the parts it was split into. A group with one
    #: part is a requirement that was not decomposable.
    groups: Tuple[Mapping[str, Any], ...] = ()
    #: EXCLUSIONS the owner stated: things the site must NOT have. They are
    #: constraints on the design, not obligations to satisfy, so they never
    #: enter screenshot Fit. Keeping them recorded means the brief's negative
    #: half is not lost.
    constraints: Tuple[Mapping[str, Any], ...] = ()
    #: The weighting policy for this set: which parent group carries which
    #: importance, and which priorities the owner stated. It is RECORDED here
    #: and passed to the evaluator as context; it is not enforced by the
    #: provider, which has no weighting parameter.
    weighting: Optional[Mapping[str, Any]] = None
    #: A stable identity for the REQUIREMENT SET -- derived from its content,
    #: group structure, schema version, mandatory flags, evidence modality and
    #: the weighting policy. Two sets that differ in any of those have
    #: different ids, so a suitability computed against one can never be
    #: compared with one computed against the other.
    requirement_set_id: str = ""

    def to_dict(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "project_id": self.project_id,
            "source_revision": self.source_revision,
            "requirements_version": self.requirements_version,
            "prd_digest": self.prd_digest,
            "entries": [e.to_dict() for e in self.entries],
            "groups": [dict(g) for g in self.groups],
            "constraints": [dict(c) for c in self.constraints],
            "weighting": dict(self.weighting) if self.weighting else None,
            "requirement_set_id": self.requirement_set_id,
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
    if not isinstance(schema, int) or schema not in SUPPORTED_SCHEMA_VERSIONS:
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
            group_id=str(raw.get("group_id") or "").strip(),
            group_text=one_line(raw.get("group_text") or "", 300),
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

    # A LEGACY artifact has no groups: each entry stands alone, and each
    # becomes a single-part group whose text is its own text. It is NOT
    # decomposed on read -- reinterpreting a saved project silently is
    # exactly what the version binding exists to prevent.
    groups: list = []
    if schema == ARTIFACT_SCHEMA_VERSION:
        raw_groups = data.get("groups")
        if raw_groups is not None:
            if not isinstance(raw_groups, Sequence) or isinstance(raw_groups, (str, bytes)):
                raise RequirementArtifactError("groups must be a list")
            for raw in raw_groups:
                if not isinstance(raw, Mapping):
                    raise RequirementArtifactError("group is not a mapping")
                groups.append({
                    "group_id": str(raw.get("group_id") or "").strip(),
                    "text": one_line(raw.get("text") or "", 300),
                    "origin": str(raw.get("origin") or "").strip(),
                    "trace": str(raw.get("trace") or "").strip(),
                    "parts": [str(p) for p in (raw.get("parts") or ())],
                })
    else:
        for entry in entries:
            groups.append({
                "group_id": entry.id,
                "text": entry.text,
                "origin": entry.origin,
                "trace": entry.trace,
                "parts": [entry.id],
            })

    return RequirementArtifact(
        project_id=project_id,
        source_revision=source_revision,
        requirements_version=requirements_version,
        prd_digest=str(data.get("prd_digest") or ""),
        schema_version=schema,
        entries=tuple(entries),
        groups=tuple(groups),
        constraints=_side("constraints"),
        weighting=(
            dict(data["weighting"])
            if isinstance(data.get("weighting"), Mapping) else None
        ),
        requirement_set_id=str(data.get("requirement_set_id") or ""),
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

    # The linkage fields are necessary but not sufficient: a recompiled PRD can
    # carry the same three values while its content has changed. Compare the
    # digest when the artifact recorded one. An empty digest means the artifact
    # predates this check, so it is unverifiable -- not a match.
    if artifact.prd_digest:
        expected = hashlib.sha256(prd_text.encode("utf-8")).hexdigest()
        if artifact.prd_digest != expected:
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
