"""Native requirement extraction for N8 B7 Design Fit scoring.

**Why this module exists.** The V2 controlled run scored Requirement Fit at
~0.50 for every candidate because the brief handed to the model contained no
requirements at all -- only a name, a content inventory, a purpose statement and
a design intent. The model was asked to judge compliance against nothing, so it
returned the scale midpoint.

The fix is NOT to invent requirements. Hermes already compiles a PRD from FAST's
accepted brief, and that PRD already carries a ``## Requirements`` section and a
``## Acceptance Criteria`` section, both as bullet lists, both validated by
``document_contracts.validate_prd``. This module reads that native document and
returns the requirement strings it actually contains.

**What this module refuses to do:**

* invent a requirement that is not written in the PRD;
* promote a ``UNRESOLVED`` marker into a requirement;
* fall back to a hardcoded list when the PRD is missing or thin.

When the native PRD cannot supply a usable set, :func:`extract_requirements`
returns an empty tuple with a reason. The caller then abstains
(``insufficient_requirements``) rather than scoring Fit against a fabricated
standard.

**N9 dependency, stated plainly.** FAST's brief is free text. The PRD compiler
re-expresses it faithfully but cannot add structure that was never there: if
FAST produced "layanan, harga, portofolio, lokasi" then the PRD's Requirements
section holds that sentence, not four separately checkable requirements. Turning
free-text briefs into an enumerated, testable requirement set is N9 Requirement
Creator work. Until N9 lands, Fit scoring will legitimately abstain on most real
projects, and that abstention is the correct behaviour rather than a bug.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence, Tuple

from app.core.design_reference_safety import one_line
from app.core.document_contracts import extract_sections, parse_front_matter

#: Section names the native PRD contract guarantees are present.
SECTION_REQUIREMENTS = "requirements"
SECTION_ACCEPTANCE = "acceptance_criteria"

#: A requirement string shorter than this cannot carry a checkable predicate.
MIN_REQUIREMENT_CHARS = 12

#: Leading markers the PRD compiler emits when FAST supplied nothing.
UNRESOLVED_REQUIREMENT_MARKERS: Tuple[str, ...] = ("UNRESOLVED",)

#: Bullet prefixes the PRD compiler emits.
_BULLET_PREFIXES = ("- ", "* ", "+ ")


class RequirementSourceError(ValueError):
    """The native requirement source could not be read."""


def _is_unresolved_marker(text: str) -> bool:
    """Whether a PRD entry is the compiler's "nothing was supplied" placeholder.

    The check is deliberately anchored to the START of the entry. The native PRD
    also contains a genuine acceptance criterion -- "No unresolved
    launch-critical placeholder remains" -- which contains the word
    "unresolved" in the middle. A substring test would discard a real
    requirement, so only a leading marker counts.
    """
    head = text.strip().lstrip("-*+ ").strip()
    upper = head.upper()
    return any(upper.startswith(marker) for marker in UNRESOLVED_REQUIREMENT_MARKERS)


def _bullets(block: str) -> Tuple[str, ...]:
    """Pull bullet-list entries out of one PRD section body."""
    entries: list = []
    for raw_line in (block or "").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        for prefix in _BULLET_PREFIXES:
            if line.startswith(prefix):
                line = line[len(prefix):].strip()
                break
        else:
            # A non-bullet line inside a section is prose; keep it only if it is
            # long enough to be a statement rather than a heading fragment.
            pass
        if len(line) < MIN_REQUIREMENT_CHARS:
            continue
        entries.append(line)
    return tuple(entries)


def extract_requirements(prd_text: str) -> Tuple[Tuple[str, ...], str]:
    """Return ``(requirements, reason)`` from a native ``PRD.md``.

    ``requirements`` is empty when the document cannot support a Fit judgement,
    and ``reason`` then carries one of the stable codes below. When requirements
    are returned, ``reason`` is ``""``.

    Codes:
        ``no_document``          empty or non-string input
        ``unparseable``          front matter / section extraction failed
        ``no_requirements``      neither section yielded a usable entry
    """
    if not isinstance(prd_text, str) or not prd_text.strip():
        return (), "no_document"

    try:
        _front, body = parse_front_matter(prd_text)
        sections = extract_sections(body)
    except Exception:  # noqa: BLE001 - any parse fault is an unreadable source
        return (), "unparseable"

    if not isinstance(sections, Mapping):
        return (), "unparseable"

    collected: list = []
    for name in (SECTION_REQUIREMENTS, SECTION_ACCEPTANCE):
        collected.extend(_bullets(sections.get(name, "")))

    # Sanitise every entry at this boundary: the PRD is owner-influenced text and
    # must never carry a newline or control character into a prompt.
    cleaned: list = []
    seen: set = set()
    for entry in collected:
        text = one_line(entry, 300)
        if len(text) < MIN_REQUIREMENT_CHARS:
            continue
        if _is_unresolved_marker(text):
            # The compiler's honest "FAST supplied nothing" marker. Counting it
            # as an unmet requirement would blame the design for a data gap.
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(text)

    if not cleaned:
        return (), "no_requirements"
    return tuple(cleaned), ""


def requirements_from_state(state: Any) -> Tuple[Tuple[str, ...], str]:
    """Best-effort native lookup: a compiled PRD stashed on project state.

    The build path may carry the compiled PRD text (``prd_text``) or the raw
    requirement list (``requirements``) on the project state. This reads either,
    preferring an explicit list because it is already structured.

    It deliberately does NOT read the brief's free-text ``what``/``why`` fields.
    Those are prose, and promoting prose into "requirements" is exactly the
    fabrication this module exists to prevent.
    """
    if state is None:
        return (), "no_document"

    explicit = getattr(state, "requirements", None)
    if isinstance(explicit, Sequence) and not isinstance(explicit, (str, bytes)):
        values = tuple(str(x) for x in explicit if str(x).strip())
        if values:
            return values, ""

    prd_text = getattr(state, "prd_text", None)
    if isinstance(prd_text, str) and prd_text.strip():
        return extract_requirements(prd_text)

    return (), "no_document"


def requirements_for_fit(workspace: Any) -> Tuple[Tuple[str, ...], str]:
    """N9: requirements a SCREENSHOT-BASED Fit judgement may use.

    Reads the structured artifact written beside ``PRD.md`` and returns only the
    entries a rendered screenshot can actually settle. Returns
    ``(texts, reason)``; an empty tuple with a non-empty reason means the caller
    must abstain rather than score Fit against something unverifiable.

    This is the replacement for a hard ``MIN_FIT_REQUIREMENTS`` count. Sufficiency
    is a coverage judgement (see :func:`assess_coverage`), not a size test: one
    specific screenshot-verifiable requirement is enough, six template entries
    are not.

    Behavioural requirements are deliberately EXCLUDED here. They remain in the
    artifact so their own runtime/functional QA path can use them; a screenshot
    must never mark one satisfied.
    """
    from app.core.design_requirement_artifact import (
        REASON_NO_ARTIFACT,
        assess_coverage,
        read_requirement_artifact,
    )

    artifact = read_requirement_artifact(workspace)
    if artifact is None:
        return (), REASON_NO_ARTIFACT

    coverage = assess_coverage(artifact)
    if not coverage.sufficient:
        return (), coverage.reason
    return coverage.scorable, ""


__all__ = [
    "MIN_REQUIREMENT_CHARS",
    "RequirementSourceError",
    "SECTION_ACCEPTANCE",
    "SECTION_REQUIREMENTS",
    "extract_requirements",
    "requirements_for_fit",
    "requirements_from_state",
]
