"""Pre-build Design Guidance compiler for Website Builder R2 (N6).

The approved roadmap places **two** Requirement Creator jobs before FRONTEND:

* the PRD compiler (N2) — FAST's brief → ``PRD.md``;
* the Design Guidance compiler (this module) — FAST's brief plus the Decision
  Engine's selected, provenance-validated design candidates →
  ``DesignGuidance.md``.

This is the document the confirmed N1 handoff gap was missing: on the *initial*
build there is no Design DNA yet (FRONTEND produces it), so a pre-build design
document cannot be compiled from the DNA. It is compiled from what the
application has **already decided** instead.

Three properties are load-bearing.

**Grounded, never fabricated.** Every statement traces to the brief, to a
selected candidate's own provenance, or to the deterministic D2 selection. A
field with no evidence carries an explicit ``UNRESOLVED`` marker — the compiler
never invents a palette, a font, a reference, or a design fact. Fabricating a
reference is the failure mode this module exists to prevent.

**Deterministic.** The same brief and the same Decision Decision compile to
byte-identical text, so the document digest can be pinned into a release commit.

**Distinct from Design DNA / Design.md.** This document is an INPUT the
application decided *before* FRONTEND; ``design-dna.json`` and ``Design.md`` are
OUTPUTS FRONTEND decided *after*. They are separate kinds with separate version
fields, so they can never be confused.

The D2 selector is **reused, not re-implemented**: requirement derivation and
resource selection are the existing ``derive_design_requirements`` /
``select_design_resources`` functions, given the brief + candidate evidence.
"""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional, Sequence, Tuple

from app.core.document_contracts import (
    DESIGN_GUIDANCE_SCHEMA_VERSION,
    DOCUMENT_KIND_DESIGN_GUIDANCE,
    serialise_document,
    validate_design_guidance,
)

#: Stable generator identifier written into the document front matter.
DESIGN_GUIDANCE_GENERATOR = "requirement-creator/design-guidance"

#: The marker emitted wherever the evidence does not support a value. Loud and
#: uniform so a downstream reader (and the acceptance tests) can find every
#: unresolved item; it is never a fabricated value.
UNRESOLVED_MARKER = "UNRESOLVED"

#: The section headings the contract requires, in a fixed order. Each is emitted
#: even when unresolved, so "present but honest" is a checkable property.
_GUIDANCE_SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("Brand Character", "brand_character"),
    ("Composition", "composition"),
    ("Typography", "typography"),
    ("Color", "color"),
    ("Imagery", "imagery"),
    ("Motion", "motion"),
    ("Approved Resources", "approved_resources"),
)


class DesignGuidanceError(ValueError):
    """A compilation could not produce a valid guidance document."""

    def __init__(self, message: str, codes: Sequence[str] = ()) -> None:
        self.codes = tuple(codes)
        super().__init__(message)


def _clean(value: Any) -> str:
    """Return a trimmed string, or ``""`` for anything that is not text."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def _line(value: str) -> str:
    """Render one honest section line: the value, or an explicit UNRESOLVED note."""
    return value if value else f"{UNRESOLVED_MARKER}: no supporting evidence supplied"


def guidance_evidence(
    brief: Mapping[str, Any],
    decision: Optional[Any],
) -> dict:
    """Derive the bounded, grounded evidence the compiler renders from.

    The evidence is the *intersection* of what the brief states and what the
    Decision Engine's selected candidate actually carries. Nothing is inferred
    beyond those two sources, so the result can never assert a design fact the
    application did not authorise.

    ``decision`` is a :class:`~app.core.decision_engine.DesignDecision` (or any
    object exposing ``selected`` with ``descriptor`` / ``palette`` / ``label`` /
    ``source_uri`` / ``source_revision``). ``None`` or an abstention yields
    brief-only evidence.
    """
    brief = brief if isinstance(brief, Mapping) else {}
    what = _clean(brief.get("what"))
    why = _clean(brief.get("why"))
    name = _clean(brief.get("name"))

    # Brand character: the brief's own words, plus the candidate's descriptor.
    brand_parts = [p for p in (name, what, why) if p]
    brand = "; ".join(brand_parts)

    candidate = getattr(decision, "selected", None) if decision is not None else None
    descriptor = _clean(getattr(candidate, "descriptor", "")) if candidate else ""
    label = _clean(getattr(candidate, "label", "")) if candidate else ""
    palette: Tuple[str, ...] = ()
    if candidate is not None:
        raw_palette = getattr(candidate, "palette", ()) or ()
        palette = tuple(str(p).strip() for p in raw_palette if str(p).strip())

    source_uri = _clean(getattr(candidate, "source_uri", "")) if candidate else ""
    source_revision = _clean(getattr(candidate, "source_revision", "")) if candidate else ""

    return {
        "name": name,
        "what": what,
        "why": why,
        "brand": brand,
        "descriptor": descriptor,
        "label": label,
        "palette": palette,
        "source_uri": source_uri,
        "source_revision": source_revision,
        "has_candidate": candidate is not None,
    }


def guidance_selection_input(evidence: Mapping[str, Any]) -> dict:
    """Build the D2 selector's requirement input from the evidence.

    The selector's requirement derivation keys off a ``design_dna``-shaped
    mapping. Pre-build there is no DNA, so we pass a *bounded* mapping carrying
    only what the brief and the selected candidate actually state. This keeps the
    selector's authority precedence intact (it still decides) while never letting
    it see a value the application did not supply.
    """
    mapping: dict = {}
    brand = evidence.get("brand")
    if brand:
        mapping["brand_personality"] = brand
    descriptor = evidence.get("descriptor")
    if descriptor:
        # The candidate's own descriptor is legitimate evidence for layout/motion
        # language (e.g. "editorial grid, smooth scroll"). It is DATA, and the
        # selector reads it exactly as it reads a DNA field.
        mapping["layout"] = descriptor
    palette = evidence.get("palette")
    if palette:
        mapping["palette"] = {f"color_{i}": v for i, v in enumerate(palette)}
    return mapping


def _requirements_mapping(evidence: Mapping[str, Any]) -> dict:
    return guidance_selection_input(evidence)


def _select_resources(evidence: Mapping[str, Any], *, requested_resources):
    """Reuse the existing D2 selector on the pre-build evidence.

    Returns the selection plan. Imported lazily so this module does not hard-depend
    on the resource manifest at import time, and so a missing manifest degrades to
    "no resources selected" rather than failing the whole compile.
    """
    from app.core.design_selection import select_design_resources

    mapping = _requirements_mapping(evidence)
    try:
        return select_design_resources(
            mapping,
            requested=tuple(requested_resources or ()),
        )
    except Exception:
        # A selector failure (e.g. an undeclared requested id) must never lose the
        # build; the guidance simply carries no approved resources, honestly.
        return None


def _render_approved_resources(plan, evidence: Mapping[str, Any]) -> str:
    """Render the approved-resources section, grounded in real provenance."""
    lines = []
    if plan is not None:
        for entry in plan.selected_resources:
            lines.append(f"- {entry.resource_id} ({entry.resource_kind}): {entry.reason}")
    if evidence.get("has_candidate") and evidence.get("source_uri"):
        # The candidate itself is an approved input, and it travels with the
        # provenance that makes it trustworthy. A candidate without provenance
        # never reaches here (the engine drops it), so this line is always real.
        lines.append(
            f"- reference {evidence['source_uri']}"
            f"@{evidence['source_revision']}"
            + (f": {evidence['label']}" if evidence.get("label") else "")
        )
    if not lines:
        return f"{UNRESOLVED_MARKER}: no approved resource was selected from the evidence"
    return "\n".join(lines)


def compile_design_guidance(
    brief: Mapping[str, Any],
    decision: Optional[Any],
    *,
    project_id: str,
    source_revision: int,
    design_guidance_version: int = 0,
    requested_resources: Sequence[str] = (),
    generator_version: int = 1,
) -> str:
    """Compile the pre-build ``DesignGuidance.md`` from brief + Decision Decision.

    Returns the validated document text. Raises :class:`DesignGuidanceError` if
    the compiled document does not validate (a compiler bug — never written to
    disk as a "document").

    The compiler is deliberately conservative: every section is present, and a
    section the evidence does not support carries an explicit ``UNRESOLVED``
    marker rather than an invented value.
    """
    evidence = guidance_evidence(brief, decision)
    plan = _select_resources(evidence, requested_resources=requested_resources)

    brand = evidence["brand"]
    descriptor = evidence["descriptor"]
    palette = evidence["palette"]

    brand_line = _line(brand)
    # Composition is grounded in the candidate descriptor (layout/motion language
    # the decision engine actually selected) or the brief's own "what".
    composition = _line(descriptor or evidence["what"])
    typography = f"{UNRESOLVED_MARKER}: no typography evidence supplied (FRONTEND owns the final choice)"
    color = _line(", ".join(palette))
    imagery = f"{UNRESOLVED_MARKER}: no imagery evidence supplied"
    motion = _line(descriptor if descriptor and any(
        kw in descriptor.lower() for kw in ("scroll", "motion", "animation", "transition", "timeline")
    ) else "")
    approved = _render_approved_resources(plan, evidence)

    body_parts = []
    rendered = {
        "brand_character": brand_line,
        "composition": composition,
        "typography": typography,
        "color": color,
        "imagery": imagery,
        "motion": motion,
        "approved_resources": approved,
    }
    for heading, slug in _GUIDANCE_SECTIONS:
        body_parts.append(f"## {heading}\n\n{rendered[slug]}\n")

    front = {
        "document": DOCUMENT_KIND_DESIGN_GUIDANCE,
        "schema_version": str(DESIGN_GUIDANCE_SCHEMA_VERSION),
        "project_id": project_id,
        "source_revision": str(source_revision),
        "generator": DESIGN_GUIDANCE_GENERATOR,
        "generator_version": str(generator_version),
        "design_guidance_version": str(design_guidance_version),
    }
    text = serialise_document(front, "\n".join(body_parts))
    _, violations = validate_design_guidance(text)
    if violations:
        raise DesignGuidanceError(
            "compiled DesignGuidance.md failed validation",
            [v.code for v in violations],
        )
    return text


def guidance_digest(text: str) -> str:
    """Return the SHA-256 of a guidance document's exact bytes."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


__all__ = [
    "DESIGN_GUIDANCE_GENERATOR",
    "UNRESOLVED_MARKER",
    "DesignGuidanceError",
    "guidance_evidence",
    "guidance_selection_input",
    "compile_design_guidance",
    "guidance_digest",
]
