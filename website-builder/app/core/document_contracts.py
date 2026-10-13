"""Canonical PRD.md / Design.md document contracts for Website Builder R2 (N1).

This module is the **N1 deliverable** that establishes the *document contract*
the later batches (N2–N4) compile, store, and consume. It is deliberately
additive: it defines and validates documents, and changes **no** existing
pipeline behaviour. Nothing in the FAST → FRONTEND → QA → publication path
imports this module in N1.

Scope of this module (N1 architecture & document contracts):

* **Versioned schemas** for the two canonical documents, ``PRD.md`` and
  ``Design.md``.
* **Stable identities** — a document id that is stable for a project across
  revisions, and a revision-bound id that changes with the release source.
* **Deterministic validation** — pure functions with stable error codes; no
  wall-clock, no randomness, no network, no subprocess.
* **Provenance** — every document records the project, the source revision, the
  requirements/Design-DNA versions, and the generator that produced it.
* **Legacy compatibility** — a project with no documents (every R1 project) is
  *valid*, not an error. Absence is a supported state, so N2 can adopt the
  documents without a migration flag day.

Design rules this module enforces (from the approved roadmap):

1. ``PRD.md`` and ``Design.md`` and the generated website source must belong to
   the **same exact Git release commit**. This module binds a document to a
   ``source_revision``; the Git commit binding happens at publication time
   (N2), so no second release state machine is created here.
2. FAST remains authoritative for requirements and scope. ``PRD.md`` is a
   *compilation* of FAST's decision, never a second authority.
3. Documents must be **deterministic**: the same inputs must serialise to
   byte-identical text, so a release commit is reproducible.

Front matter is intentionally a **restricted scalar format** (``key: value``
lines only). A general YAML loader coerces ``no`` → ``False``, ``1.0`` → ``1``,
etc., which makes a document's meaning depend on the parser; the restricted
parser fails closed on anything that is not a flat scalar instead.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Schema versions
# ---------------------------------------------------------------------------

#: Schema version of the ``PRD.md`` front matter + body contract.
PRD_SCHEMA_VERSION = 1

#: Schema version of the ``Design.md`` front matter + body contract.
DESIGN_SCHEMA_VERSION = 1

#: Schema version of the ``DesignGuidance.md`` front matter + body contract.
#:
#: DesignGuidance is the **pre-build** design document (N6): it is compiled from
#: FAST's brief plus the Decision Engine's selected, provenance-validated design
#: candidates, and is handed to FRONTEND *before* it runs. It is deliberately a
#: SEPARATE kind from ``Design.md`` (post-build, compiled from the Design DNA
#: FRONTEND produces), so the two can never be confused: one is an input the
#: application decided, the other is an output FRONTEND decided.
DESIGN_GUIDANCE_SCHEMA_VERSION = 1

#: The canonical document kinds. A closed set: a document is one of these.
DOCUMENT_KIND_PRD = "prd"
DOCUMENT_KIND_DESIGN = "design"
DOCUMENT_KIND_DESIGN_GUIDANCE = "design_guidance"
DOCUMENT_KINDS: Tuple[str, ...] = (
    DOCUMENT_KIND_PRD,
    DOCUMENT_KIND_DESIGN,
    DOCUMENT_KIND_DESIGN_GUIDANCE,
)

#: Canonical on-disk file names, relative to the generated website source.
PRD_FILE_NAME = "PRD.md"
DESIGN_FILE_NAME = "Design.md"
DESIGN_GUIDANCE_FILE_NAME = "DesignGuidance.md"

#: Map a document kind to its canonical file name.
DOCUMENT_FILE_NAMES: Mapping[str, str] = {
    DOCUMENT_KIND_PRD: PRD_FILE_NAME,
    DOCUMENT_KIND_DESIGN: DESIGN_FILE_NAME,
    DOCUMENT_KIND_DESIGN_GUIDANCE: DESIGN_GUIDANCE_FILE_NAME,
}

# ---------------------------------------------------------------------------
# Stable error codes
# ---------------------------------------------------------------------------
#
# Every violation carries a stable, machine-readable code so a caller (and a
# test) never has to match on human-readable prose. The prose may be reworded;
# the code may not.

ERR_MISSING_FRONT_MATTER = "MISSING_FRONT_MATTER"
ERR_UNTERMINATED_FRONT_MATTER = "UNTERMINATED_FRONT_MATTER"
ERR_MALFORMED_FRONT_MATTER = "MALFORMED_FRONT_MATTER"
ERR_UNKNOWN_FIELD = "UNKNOWN_FIELD"
ERR_MISSING_FIELD = "MISSING_FIELD"
ERR_INVALID_KIND = "INVALID_KIND"
ERR_INVALID_SCHEMA_VERSION = "INVALID_SCHEMA_VERSION"
ERR_INVALID_SOURCE_REVISION = "INVALID_SOURCE_REVISION"
ERR_INVALID_VERSION_COUNTER = "INVALID_VERSION_COUNTER"
ERR_INVALID_IDENTIFIER = "INVALID_IDENTIFIER"
ERR_MISSING_SECTION = "MISSING_SECTION"
ERR_EMPTY_SECTION = "EMPTY_SECTION"
ERR_LEGACY_CONTRADICTION = "LEGACY_CONTRADICTION"

#: N6: a recorded guidance snapshot does not match the documents actually used.
#: Raised by the QA/VISION parity check when the recorded evidence and the
#: workspace documents disagree — a hard inconsistency, never inferred away.
ERR_GUIDANCE_VERSION_MISMATCH = "GUIDANCE_VERSION_MISMATCH"

#: Front-matter fields every document must declare.
REQUIRED_FRONT_MATTER_FIELDS: Tuple[str, ...] = (
    "document",
    "schema_version",
    "project_id",
    "source_revision",
    "generator",
    "generator_version",
)

#: Optional front-matter fields, understood but not required.
OPTIONAL_FRONT_MATTER_FIELDS: Tuple[str, ...] = (
    "requirements_version",
    "design_dna_version",
    "design_guidance_version",
)

#: The complete set of recognised front-matter fields. An unknown field is a
#: hard error: silently ignoring a typo'd field is how a document validates as
#: something it is not.
RECOGNISED_FRONT_MATTER_FIELDS: Tuple[str, ...] = (
    REQUIRED_FRONT_MATTER_FIELDS + OPTIONAL_FRONT_MATTER_FIELDS
)

#: Required body sections per kind, as lower-case markdown headings (``##``).
#: The heading text is matched case-insensitively after trimming, and matched on
#: a normalised slug so ``## Acceptance Criteria`` and ``## acceptance_criteria``
#: are the same section.
PRD_REQUIRED_SECTIONS: Tuple[str, ...] = (
    "requirements",
    "scope",
    "acceptance_criteria",
)
DESIGN_REQUIRED_SECTIONS: Tuple[str, ...] = (
    "brand_personality",
    "palette",
    "typography",
    "layout",
)
#: N6: the pre-build Design Guidance sections. These mirror the useful, grounded
#: visual guidance the approved design requires (composition, typography, colour,
#: imagery, motion, brand character, and approved resources). Every section is
#: required to be PRESENT; a section the evidence does not support carries an
#: explicit UNRESOLVED marker rather than a fabricated value, so "present but
#: honest" is checkable and an empty section is a violation.
DESIGN_GUIDANCE_REQUIRED_SECTIONS: Tuple[str, ...] = (
    "brand_character",
    "composition",
    "typography",
    "color",
    "imagery",
    "motion",
    "approved_resources",
)

REQUIRED_SECTIONS_BY_KIND: Mapping[str, Tuple[str, ...]] = {
    DOCUMENT_KIND_PRD: PRD_REQUIRED_SECTIONS,
    DOCUMENT_KIND_DESIGN: DESIGN_REQUIRED_SECTIONS,
    DOCUMENT_KIND_DESIGN_GUIDANCE: DESIGN_GUIDANCE_REQUIRED_SECTIONS,
}

#: Project ids are the same slugs the sandbox runner accepts. Kept as a local
#: regex so this module has no import-time dependency on the runtime.
_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")

#: Generator identity: ``<component>/<role>``, lower-case slug segments.
_GENERATOR_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*(/[a-z0-9][a-z0-9_-]*)*$")


# ---------------------------------------------------------------------------
# Violations
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentViolation:
    """One deterministic validation failure.

    ``field`` names the offending front-matter field or section, or is ``None``
    for document-level failures. ``detail`` is human-readable and non-contractual.
    """

    code: str
    field: Optional[str] = None
    detail: str = ""

    def __str__(self) -> str:  # pragma: no cover - convenience only
        where = f" [{self.field}]" if self.field else ""
        return f"{self.code}{where}: {self.detail}" if self.detail else f"{self.code}{where}"


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentIdentity:
    """Provenance for one canonical document.

    ``document_id`` is stable for a (project, kind) pair across revisions;
    ``revision_id`` changes with the release source. Both are derived, never
    stored, so they cannot drift from the fields they are computed from.
    """

    kind: str
    project_id: str
    schema_version: int
    source_revision: int
    generator: str
    generator_version: int
    requirements_version: int = 0
    design_dna_version: int = 0
    #: N6: the pre-build Design Guidance version this document records. Distinct
    #: from ``design_dna_version`` (post-build) so the two can never be confused.
    design_guidance_version: int = 0

    @property
    def document_id(self) -> str:
        """Stable id for this document, invariant across revisions."""
        return f"{self.kind}:{self.project_id}"

    @property
    def revision_id(self) -> str:
        """Revision-bound id, changing with ``source_revision``."""
        return f"{self.document_id}:r{self.source_revision}"


def stable_document_id(kind: str, project_id: str) -> str:
    """Return the revision-invariant id for a document."""
    return f"{kind}:{project_id}"


# ---------------------------------------------------------------------------
# Front matter: restricted, deterministic parse / serialise
# ---------------------------------------------------------------------------

_FRONT_MATTER_FENCE = "---"


def _normalise_section(heading: str) -> str:
    """Normalise a markdown heading into a comparable section slug.

    ``"Acceptance Criteria"`` → ``"acceptance_criteria"``. Non-alphanumeric runs
    collapse to a single underscore so heading punctuation never matters.
    """
    slug = heading.strip().lower()
    slug = re.sub(r"[^a-z0-9]+", "_", slug)
    return slug.strip("_")


def parse_front_matter(text: str) -> Tuple[Dict[str, str], str]:
    """Split ``text`` into ``(front_matter, body)``.

    Front matter is the block between the first two ``---`` fences at the very
    start of the document. Values are returned as **raw strings** — no type
    coercion — so the caller decides what a value means.

    Raises :class:`DocumentContractError` on a missing or unterminated fence, or
    on a line that is not a flat ``key: value`` scalar. Fail-closed is the point:
    a document that cannot be parsed deterministically is not a valid document.
    """
    if not text.startswith(_FRONT_MATTER_FENCE):
        raise DocumentContractError(
            ERR_MISSING_FRONT_MATTER,
            "document must begin with a '---' front-matter fence",
        )
    lines = text.splitlines()
    # lines[0] is the opening fence.
    end_index = None
    for index in range(1, len(lines)):
        if lines[index].strip() == _FRONT_MATTER_FENCE:
            end_index = index
            break
    if end_index is None:
        raise DocumentContractError(
            ERR_UNTERMINATED_FRONT_MATTER,
            "front-matter fence opened but never closed",
        )

    front_matter: Dict[str, str] = {}
    for raw_line in lines[1:end_index]:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" not in line:
            raise DocumentContractError(
                ERR_MALFORMED_FRONT_MATTER,
                f"front-matter line is not 'key: value': {line!r}",
            )
        key, _, value = line.partition(":")
        key = key.strip()
        value = value.strip()
        if not key:
            raise DocumentContractError(
                ERR_MALFORMED_FRONT_MATTER,
                f"front-matter line has an empty key: {line!r}",
            )
        if key in front_matter:
            raise DocumentContractError(
                ERR_MALFORMED_FRONT_MATTER,
                f"duplicate front-matter key: {key!r}",
            )
        front_matter[key] = value

    body = "\n".join(lines[end_index + 1:])
    return front_matter, body


def serialise_document(
    front_matter: Mapping[str, Any],
    body: str,
) -> str:
    """Serialise a document deterministically (sorted keys, stable spacing).

    Determinism is a contract: the same logical document must produce
    byte-identical text so a release commit is reproducible. Keys are emitted in
    sorted order, and the body is stripped of trailing whitespace with exactly
    one trailing newline.
    """
    lines: List[str] = [_FRONT_MATTER_FENCE]
    for key in sorted(front_matter):
        lines.append(f"{key}: {front_matter[key]}")
    lines.append(_FRONT_MATTER_FENCE)
    lines.append("")
    lines.append(body.strip())
    lines.append("")
    return "\n".join(lines)


def document_digest(text: str) -> str:
    """Return the SHA-256 of a document's exact bytes (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Section extraction
# ---------------------------------------------------------------------------


def extract_sections(body: str) -> Dict[str, str]:
    """Return ``{section_slug: section_text}`` for every ``##`` heading.

    Only level-2 headings (``##``) delimit contract sections; deeper headings
    stay inside their parent section. Text before the first ``##`` is ignored
    (it is a document preamble, not a contract section).
    """
    sections: Dict[str, str] = {}
    current: Optional[str] = None
    buffer: List[str] = []
    for line in body.splitlines():
        if line.startswith("## "):
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = _normalise_section(line[3:])
            buffer = []
        elif line.strip() == "##":
            if current is not None:
                sections[current] = "\n".join(buffer).strip()
            current = None
            buffer = []
        else:
            buffer.append(line)
    if current is not None:
        sections[current] = "\n".join(buffer).strip()
    return sections


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class DocumentContractError(ValueError):
    """A document is structurally unparseable or violates its contract.

    ``code`` is a stable machine-readable identifier from the ``ERR_*`` set.
    """

    def __init__(self, code: str, detail: str = "") -> None:
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}" if detail else code)


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _parse_int(value: Any, *, field: str, minimum: int = 0) -> int:
    """Parse a non-negative integer from a front-matter string, fail-closed."""
    if isinstance(value, bool):  # bool is an int subclass; reject explicitly
        raise DocumentContractError(ERR_INVALID_VERSION_COUNTER, f"{field} must be an integer")
    if isinstance(value, int):
        parsed = value
    else:
        text = str(value).strip()
        if not re.fullmatch(r"\d+", text):
            raise DocumentContractError(
                ERR_INVALID_VERSION_COUNTER,
                f"{field} must be a non-negative integer, got {value!r}",
            )
        parsed = int(text)
    if parsed < minimum:
        raise DocumentContractError(
            ERR_INVALID_VERSION_COUNTER,
            f"{field} must be >= {minimum}, got {parsed}",
        )
    return parsed


def identity_from_front_matter(
    front_matter: Mapping[str, str],
    *,
    expected_kind: Optional[str] = None,
) -> DocumentIdentity:
    """Build a :class:`DocumentIdentity` from parsed front matter.

    Raises :class:`DocumentContractError` on any missing/invalid required field,
    an unknown field, or a kind that does not match ``expected_kind``.
    """
    for field in REQUIRED_FRONT_MATTER_FIELDS:
        if field not in front_matter:
            raise DocumentContractError(ERR_MISSING_FIELD, f"missing required field {field!r}")
    for field in front_matter:
        if field not in RECOGNISED_FRONT_MATTER_FIELDS:
            raise DocumentContractError(ERR_UNKNOWN_FIELD, f"unknown front-matter field {field!r}")

    kind = str(front_matter["document"]).strip().lower()
    if kind not in DOCUMENT_KINDS:
        raise DocumentContractError(
            ERR_INVALID_KIND,
            f"document kind must be one of {DOCUMENT_KINDS}, got {kind!r}",
        )
    if expected_kind is not None and kind != expected_kind:
        raise DocumentContractError(
            ERR_INVALID_KIND,
            f"expected kind {expected_kind!r}, got {kind!r}",
        )

    project_id = str(front_matter["project_id"]).strip()
    if not _PROJECT_ID_RE.match(project_id):
        raise DocumentContractError(
            ERR_INVALID_IDENTIFIER,
            f"project_id is not a valid slug: {project_id!r}",
        )

    generator = str(front_matter["generator"]).strip()
    if not _GENERATOR_RE.match(generator):
        raise DocumentContractError(
            ERR_INVALID_IDENTIFIER,
            f"generator is not a valid identifier: {generator!r}",
        )

    schema_version = _parse_int(
        front_matter["schema_version"], field="schema_version", minimum=1
    )
    expected_schema = {
        DOCUMENT_KIND_PRD: PRD_SCHEMA_VERSION,
        DOCUMENT_KIND_DESIGN: DESIGN_SCHEMA_VERSION,
        DOCUMENT_KIND_DESIGN_GUIDANCE: DESIGN_GUIDANCE_SCHEMA_VERSION,
    }[kind]
    if schema_version != expected_schema:
        raise DocumentContractError(
            ERR_INVALID_SCHEMA_VERSION,
            f"{kind} schema_version must be {expected_schema}, got {schema_version}",
        )

    return DocumentIdentity(
        kind=kind,
        project_id=project_id,
        schema_version=schema_version,
        source_revision=_parse_int(
            front_matter["source_revision"], field="source_revision", minimum=1
        ),
        generator=generator,
        generator_version=_parse_int(
            front_matter["generator_version"], field="generator_version", minimum=1
        ),
        requirements_version=_parse_int(
            front_matter.get("requirements_version", "0"),
            field="requirements_version",
        ),
        design_dna_version=_parse_int(
            front_matter.get("design_dna_version", "0"),
            field="design_dna_version",
        ),
        design_guidance_version=_parse_int(
            front_matter.get("design_guidance_version", "0"),
            field="design_guidance_version",
        ),
    )


def validate_document(
    text: str,
    *,
    expected_kind: Optional[str] = None,
) -> Tuple[Optional[DocumentIdentity], List[DocumentViolation]]:
    """Validate ``text`` against the canonical document contract.

    Returns ``(identity, violations)``. ``identity`` is ``None`` only when the
    document could not be parsed far enough to establish one. A non-empty
    ``violations`` list means the document is invalid; an empty list means it is
    valid.

    This function never raises on invalid *content* — it reports. It is the
    deterministic, side-effect-free validator the N1 gate tests exercise.
    """
    violations: List[DocumentViolation] = []

    try:
        front_matter, body = parse_front_matter(text)
    except DocumentContractError as exc:
        return None, [DocumentViolation(exc.code, detail=exc.detail)]

    try:
        identity = identity_from_front_matter(front_matter, expected_kind=expected_kind)
    except DocumentContractError as exc:
        return None, [DocumentViolation(exc.code, detail=exc.detail)]

    sections = extract_sections(body)
    for required in REQUIRED_SECTIONS_BY_KIND[identity.kind]:
        if required not in sections:
            violations.append(
                DocumentViolation(
                    ERR_MISSING_SECTION,
                    field=required,
                    detail=f"required section '## {required}' is absent",
                )
            )
        elif not sections[required]:
            violations.append(
                DocumentViolation(
                    ERR_EMPTY_SECTION,
                    field=required,
                    detail=f"required section '## {required}' is present but empty",
                )
            )

    return identity, violations


def validate_prd(text: str) -> Tuple[Optional[DocumentIdentity], List[DocumentViolation]]:
    """Validate a ``PRD.md`` document (kind pinned to ``prd``)."""
    return validate_document(text, expected_kind=DOCUMENT_KIND_PRD)


def validate_design(text: str) -> Tuple[Optional[DocumentIdentity], List[DocumentViolation]]:
    """Validate a ``Design.md`` document (kind pinned to ``design``)."""
    return validate_document(text, expected_kind=DOCUMENT_KIND_DESIGN)


def validate_design_guidance(
    text: str,
) -> Tuple[Optional[DocumentIdentity], List[DocumentViolation]]:
    """Validate a ``DesignGuidance.md`` document (N6, kind pinned to ``design_guidance``)."""
    return validate_document(text, expected_kind=DOCUMENT_KIND_DESIGN_GUIDANCE)


def is_valid(text: str, *, expected_kind: Optional[str] = None) -> bool:
    """Convenience predicate: ``True`` iff ``text`` validates with no violations."""
    _, violations = validate_document(text, expected_kind=expected_kind)
    return not violations


# ---------------------------------------------------------------------------
# Legacy compatibility
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DocumentPresence:
    """Whether a project carries the canonical documents.

    R1 projects have no documents. That is a **supported state**, not an error:
    ``has_documents`` is ``False`` and ``legacy`` is ``True``. N2 can therefore
    adopt documents without a migration flag day, and every downstream consumer
    reads one boolean instead of re-deriving presence from the filesystem.
    """

    prd_present: bool
    design_present: bool

    @property
    def has_documents(self) -> bool:
        return self.prd_present and self.design_present

    @property
    def legacy(self) -> bool:
        """A project with neither document is a legacy (R1) project."""
        return not self.prd_present and not self.design_present

    @property
    def partial(self) -> bool:
        """Exactly one document present: an inconsistent, unsupported state."""
        return self.prd_present != self.design_present


def assess_presence(
    *,
    prd_text: Optional[str] = None,
    design_text: Optional[str] = None,
) -> DocumentPresence:
    """Assess which canonical documents a project carries."""
    return DocumentPresence(
        prd_present=bool(prd_text and prd_text.strip()),
        design_present=bool(design_text and design_text.strip()),
    )


def design_dna_compatible(
    design_sections: Mapping[str, str],
    design_dna: Optional[Mapping[str, Any]],
) -> List[DocumentViolation]:
    """Check a ``Design.md`` body against an existing ``design-dna.json``.

    Compatibility with the existing Design DNA contract is a **non-contradiction**
    rule, not an equality rule: ``Design.md`` is the human-readable design
    document and ``design-dna.json`` remains the machine Design DNA. This checks
    only that the two do not assert conflicting values for the fields they share
    (``brand_personality``, ``palette``, ``typography``, ``layout``).

    A ``None`` Design DNA is compatible with everything — that is the legacy
    case. When the DNA names a value, the document's section must either be
    absent or contain that value, case-insensitively; a document that asserts a
    *different* palette is a contradiction, not a refinement.
    """
    violations: List[DocumentViolation] = []
    if not design_dna:
        return violations

    for field in DESIGN_REQUIRED_SECTIONS:
        dna_value = design_dna.get(field)
        if dna_value is None:
            continue
        section_text = (design_sections.get(field) or "").lower()
        if not section_text:
            continue
        if isinstance(dna_value, str):
            candidates = [dna_value]
        elif isinstance(dna_value, Mapping):
            candidates = [str(v) for v in dna_value.values() if isinstance(v, (str, int, float))]
        elif isinstance(dna_value, Sequence) and not isinstance(dna_value, (str, bytes)):
            candidates = [str(v) for v in dna_value if isinstance(v, (str, int, float))]
        else:
            candidates = [str(dna_value)]

        candidates = [c.strip().lower() for c in candidates if str(c).strip()]
        if candidates and not any(c in section_text for c in candidates):
            violations.append(
                DocumentViolation(
                    ERR_LEGACY_CONTRADICTION,
                    field=field,
                    detail=(
                        f"Design.md section '{field}' does not reflect the persisted "
                        f"design-dna.json value(s) {candidates}"
                    ),
                )
            )
    return violations


__all__ = [
    "PRD_SCHEMA_VERSION",
    "DESIGN_SCHEMA_VERSION",
    "DESIGN_GUIDANCE_SCHEMA_VERSION",
    "DOCUMENT_KIND_PRD",
    "DOCUMENT_KIND_DESIGN",
    "DOCUMENT_KIND_DESIGN_GUIDANCE",
    "DOCUMENT_KINDS",
    "PRD_FILE_NAME",
    "DESIGN_FILE_NAME",
    "DESIGN_GUIDANCE_FILE_NAME",
    "DOCUMENT_FILE_NAMES",
    "REQUIRED_FRONT_MATTER_FIELDS",
    "OPTIONAL_FRONT_MATTER_FIELDS",
    "RECOGNISED_FRONT_MATTER_FIELDS",
    "PRD_REQUIRED_SECTIONS",
    "DESIGN_REQUIRED_SECTIONS",
    "DESIGN_GUIDANCE_REQUIRED_SECTIONS",
    "REQUIRED_SECTIONS_BY_KIND",
    "ERR_GUIDANCE_VERSION_MISMATCH",
    "DocumentViolation",
    "DocumentIdentity",
    "DocumentPresence",
    "DocumentContractError",
    "stable_document_id",
    "parse_front_matter",
    "serialise_document",
    "document_digest",
    "extract_sections",
    "identity_from_front_matter",
    "validate_document",
    "validate_prd",
    "validate_design",
    "validate_design_guidance",
    "is_valid",
    "assess_presence",
    "design_dna_compatible",
]
