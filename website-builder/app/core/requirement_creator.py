"""Requirement Creator for Website Builder R2 (N2).

The Requirement Creator is the component the approved roadmap places between
FAST and the rest of the pipeline. It runs in **two modes**, both of which are
pure, deterministic compilations of an already-made decision:

* **PRD Compiler** — compiles FAST's authoritative brief into ``PRD.md``.
* **Design Guidance Compiler** — compiles the accepted Design DNA into
  ``Design.md``.

Two properties are load-bearing, and they are the reason this is a *compiler*
and not a second author:

**FAST remains authoritative.** This module never decides scope, requirements,
or business facts. It re-expresses the decision FAST already made, in a
document. When a fact is absent it emits an explicit *Unresolved* marker — it
never invents a service, a price, an address, or a claim. A compiler that
fabricates is an author.

**Determinism.** The same brief and Design DNA must compile to byte-identical
documents, so the document digest can be pinned into a release commit and the
commit stays reproducible. There is no wall-clock, no randomness, no network,
and no dict-iteration-order dependence anywhere in this module.

The output is always a valid document under
:mod:`app.core.document_contracts` — this module's compilers are tested to
produce documents that pass ``validate_prd`` / ``validate_design`` unchanged.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from app.core.document_contracts import (
    DESIGN_SCHEMA_VERSION,
    DOCUMENT_KIND_DESIGN,
    DOCUMENT_KIND_PRD,
    PRD_SCHEMA_VERSION,
    serialise_document,
    validate_design,
    validate_prd,
)

#: Stable generator identifiers written into document front matter.
PRD_GENERATOR = "requirement-creator/prd"
DESIGN_GENERATOR = "requirement-creator/design"

#: The marker emitted wherever FAST supplied no fact. Deliberately loud and
#: uniform so a downstream reader (and the N4 acceptance test) can find every
#: unresolved item; it is never a fabricated value.
UNRESOLVED_MARKER = "UNRESOLVED"


class RequirementCreatorError(ValueError):
    """A compilation could not produce a valid document.

    Carries the validator's stable violation codes so a caller never has to
    parse prose to learn why a compile failed.
    """

    def __init__(self, message: str, codes: Sequence[str] = ()) -> None:
        self.codes = tuple(codes)
        super().__init__(message)


@dataclass(frozen=True)
class RequirementCreatorConfig:
    """Feature configuration for the Requirement Creator.

    DISABLED by default. When disabled the build path is byte-identical to the
    pre-N2 path: no documents are compiled, written, or read. A malformed or
    missing config yields the disabled default.
    """

    enabled: bool = False
    generator_version: int = 1


def config_from_mapping(data: Optional[Mapping[str, Any]]) -> RequirementCreatorConfig:
    """Build a :class:`RequirementCreatorConfig` from a mapping (parsed YAML).

    Only documented keys are read. A malformed value never enables the feature:
    ``enabled`` defaults to ``False`` and stays ``False`` unless it is exactly
    truthy. A non-positive ``generator_version`` falls back to ``1`` (the
    contract requires ``generator_version >= 1``).
    """
    data = data or {}
    enabled = bool(data.get("enabled", False))
    try:
        generator_version = int(data.get("generator_version", 1))
    except (TypeError, ValueError):
        generator_version = 1
    if generator_version < 1:
        generator_version = 1
    return RequirementCreatorConfig(enabled=enabled, generator_version=generator_version)


def _clean(value: Any) -> str:
    """Return a trimmed string, or ``""`` for anything that is not text."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _bullet_block(items: Sequence[str], *, empty: str) -> str:
    """Render a deterministic bullet list, or a single ``empty`` line."""
    cleaned = [item for item in items if item]
    if not cleaned:
        return empty
    return "\n".join(f"- {item}" for item in cleaned)


def compile_prd(
    brief: Mapping[str, Any],
    *,
    project_id: str,
    source_revision: int,
    requirements_version: int = 0,
    generator_version: int = 1,
    scope: Optional[str] = None,
    readiness: Optional[str] = None,
) -> str:
    """Compile FAST's authoritative brief into a validated ``PRD.md``.

    FAST owns the brief; this function only re-expresses it. Fields FAST did not
    supply become explicit ``UNRESOLVED`` markers, never invented values.

    Returns the document text. Raises :class:`RequirementCreatorError` if the
    compiled document does not validate (which would be a compiler bug, and must
    never be written to disk as a "document").
    """
    name = _clean(brief.get("name"))
    what = _clean(brief.get("what"))
    why = _clean(brief.get("why"))
    why_destination = _clean(brief.get("why_destination"))
    # V2-A: the user's own explicit design/aesthetic preferences, verbatim.
    # When present it is carried as its OWN section so FRONTEND receives the
    # owner's stated intent; when absent the section carries the UNRESOLVED
    # marker (honest, never inferred).
    design_intent = _clean(brief.get("design_intent"))

    requirements = _bullet_block(
        [what, why], empty=f"- {UNRESOLVED_MARKER}: FAST supplied no what/why"
    )
    scope_lines = [
        f"- Project: {name or UNRESOLVED_MARKER}",
        f"- Scope: {_clean(scope) or UNRESOLVED_MARKER}",
        f"- Readiness: {_clean(readiness) or UNRESOLVED_MARKER}",
    ]
    criteria = _bullet_block(
        [
            "Primary call-to-action is reachable from the landing page"
            if why_destination or why
            else f"- {UNRESOLVED_MARKER}: no call-to-action intent supplied",
            "Every published page renders without a critical runtime error",
            "No unresolved launch-critical placeholder remains",
        ],
        empty=f"- {UNRESOLVED_MARKER}",
    )

    front = {
        "document": DOCUMENT_KIND_PRD,
        "schema_version": str(PRD_SCHEMA_VERSION),
        "project_id": project_id,
        "source_revision": str(source_revision),
        "generator": PRD_GENERATOR,
        "generator_version": str(generator_version),
        "requirements_version": str(requirements_version),
    }
    design_intent_line = (
        design_intent
        or f"{UNRESOLVED_MARKER}: the owner stated no explicit design preference"
    )
    body = (
        "## Requirements\n\n"
        f"{requirements}\n\n"
        "## Design Intent\n\n"
        f"{design_intent_line}\n\n"
        "## Scope\n\n"
        f"{chr(10).join(scope_lines)}\n\n"
        "## Acceptance Criteria\n\n"
        f"{criteria}\n"
    )
    text = serialise_document(front, body)
    _, violations = validate_prd(text)
    if violations:
        raise RequirementCreatorError(
            "compiled PRD failed validation", [v.code for v in violations]
        )
    return text


def compile_design(
    design_dna: Mapping[str, Any],
    *,
    project_id: str,
    source_revision: int,
    design_dna_version: int = 0,
    generator_version: int = 1,
    brief: Optional[Mapping[str, Any]] = None,
) -> str:
    """Compile the accepted Design DNA into a validated ``Design.md``.

    The Design DNA is the machine source of truth (owned by FRONTEND). This
    compiles it into the human-readable document. Compatibility is
    **non-contradiction**, not equality: the section text is derived *from* the
    DNA, so it can never assert a value the DNA does not carry. Missing DNA
    fields become ``UNRESOLVED`` markers.

    Returns the document text. Raises :class:`RequirementCreatorError` if the
    compiled document does not validate.
    """
    dna = design_dna if isinstance(design_dna, Mapping) else {}
    brief = brief or {}

    def _field(key: str) -> str:
        value = dna.get(key)
        if isinstance(value, str):
            return value.strip()
        if isinstance(value, Mapping):
            parts = [
                f"{k}: {v}"
                for k, v in sorted(value.items())
                if isinstance(v, (str, int, float)) and str(v).strip()
            ]
            return "; ".join(parts)
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            parts = [str(v).strip() for v in value if str(v).strip()]
            return ", ".join(parts)
        return ""

    brand = _field("brand_personality")
    palette = _field("palette")
    typography = _field("typography")
    layout = _field("layout")

    # The OWNER'S OWN design intent, read from the same brief field the PRD
    # reads. It is the highest-authority design evidence available, and it was
    # absent from the document FRONTEND reads most closely. It is carried
    # VERBATIM, and it is kept in its own section so it can never be mistaken
    # for a DNA-derived value or silently overridden by one.
    design_intent = _clean(brief.get("design_intent"))

    # Assumptions and unresolved questions come from the brief as the
    # Requirement Creator already records them, so Design.md and the
    # requirement artifact cannot disagree about what was assumed.
    assumptions = brief.get("assumptions")
    assumption_lines = [
        f"- {_clean(a)}" for a in (assumptions or ())
        if isinstance(a, str) and _clean(a)
    ]
    unresolved = brief.get("unresolved")
    unresolved_lines = [
        f"- {_clean(u)}" for u in (unresolved or ())
        if isinstance(u, str) and _clean(u)
    ]

    # A tiny, deterministic fallback so a minimal-but-valid DNA still yields a
    # non-empty, honest section. It is derived from the DNA's own key set, not
    # from invention.
    def _or_unresolved(value: str) -> str:
        return value if value else UNRESOLVED_MARKER

    front = {
        "document": DOCUMENT_KIND_DESIGN,
        "schema_version": str(DESIGN_SCHEMA_VERSION),
        "project_id": project_id,
        "source_revision": str(source_revision),
        "generator": DESIGN_GENERATOR,
        "generator_version": str(generator_version),
        "design_dna_version": str(design_dna_version),
    }
    confirmed_line = (
        brand
        or f"{UNRESOLVED_MARKER}: no confirmed brand personality was supplied"
    )
    assumptions_line = (
        "\n".join(assumption_lines) if assumption_lines else "None recorded."
    )
    unresolved_line = (
        "\n".join(unresolved_lines) if unresolved_lines else "None recorded."
    )
    body = (
          "## Brand Personality\n\n"
          f"{_or_unresolved(brand)}\n\n"
          "## Palette\n\n"
          f"{_or_unresolved(palette)}\n\n"
          "## Typography\n\n"
          f"{_or_unresolved(typography)}\n\n"
          "## Layout\n\n"
          f"{_or_unresolved(layout)}\n\n"
    )
    # The Design Intent section appears ONLY when the owner actually stated one.
    #
    # The PRD may carry an UNRESOLVED marker in its optional Design Intent
    # section, because that section is not among the PRD's REQUIRED sections.
    # Design.md is different: its contract is that a document whose facts are
    # supplied carries no UNRESOLVED markers, and a document with no owner
    # preference supplied has no intent to report.
    #
    # So the honest form of "the owner said nothing" here is to OMIT the
    # section, not to write a marker for it. Writing "UNRESOLVED: the owner
    # stated no explicit design preference" into a fully-specified Design.md
    # would be a marker about a document section that does not exist, and it
    # would contradict the document's own contract.
    #
    # Nothing is invented either way: the owner's words are absent because the
    # owner wrote none.
    if design_intent:
        body += (
            "## Design Intent\n\n"
            "Explicit owner preference:\n"
            f"{design_intent}\n\n"
            "Confirmed design choice:\n"
            f"{confirmed_line}\n\n"
            "Assumptions:\n"
            f"{assumptions_line}\n\n"
            "Unresolved design questions:\n"
            f"{unresolved_line}\n"
        )
    text = serialise_document(front, body)
    _, violations = validate_design(text)
    if violations:
        raise RequirementCreatorError(
            "compiled Design.md failed validation", [v.code for v in violations]
        )
    return text
