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

from app.core.design_reference_safety import one_line
from app.core.document_contracts import (
    DESIGN_GUIDANCE_SCHEMA_VERSION,
    DOCUMENT_KIND_DESIGN_GUIDANCE,
    serialise_document,
    validate_design_guidance,
)
from app.core.design_intent import (
    clauses_by_axis,
    owner_intent as _owner_intent,
    recommendations as _recommendations,
)
from app.core.asset_strategy import (
    CONTENT_LAUNCH_CRITICAL,
    CONTENT_OPTIONAL,
    plan_assets,
)

#: Stable generator identifier written into the document front matter.
DESIGN_GUIDANCE_GENERATOR = "requirement-creator/design-guidance"

#: The marker emitted wherever the evidence does not support a value. Loud and
#: uniform so a downstream reader (and the acceptance tests) can find every
#: unresolved item; it is never a fabricated value.
UNRESOLVED_MARKER = "UNRESOLVED"

#: The section headings the contract requires, in a fixed order. Each is emitted
#: even when unresolved, so "present but honest" is a checkable property.
#: (The full, V2-B-extended table is defined below, after the marker constants.)


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


#: The section headings the contract requires, in a fixed order, followed by the
#: V2-B additions. Each required section is emitted even when unresolved, so
#: "present but honest" is a checkable property. The V2-B sections carry the
#: owner's OWN design intent (separated from inferred recommendations) and the
#: hierarchy/interaction axes the owner may have spoken to.
_GUIDANCE_SECTIONS: Tuple[Tuple[str, str], ...] = (
    ("Brand Character", "brand_character"),
    ("Design Intent", "design_intent"),
    ("Composition", "composition"),
    ("Typography", "typography"),
    ("Color", "color"),
    ("Imagery", "imagery"),
    ("Hierarchy", "hierarchy"),
    ("Interaction", "interaction"),
    ("Motion", "motion"),
    ("Approved Resources", "approved_resources"),
)

#: Marker appended to a line that carries the OWNER'S OWN words verbatim, so a
#: reader can always tell a user-specified instruction from an inferred one.
#: This is the ONLY mandatory design-provenance class.
USER_SPECIFIED = "user-specified"

#: Marker appended to an application implementation RECOMMENDATION derived from
#: the owner's stated terms + the brief's domain. Never presented as the owner's
#: words; never a fixed hex, ratio, count, or mandatory element.
#:
#: C (Design Autonomy Restoration): the marker now states explicitly that the
#: line is an OPTIONAL reference and that FRONTEND owns the final art-direction
#: choice. The requirement to keep the recommendation grounded is unchanged --
#: only its AUTHORITY is clarified, so a prescriptive reading can no longer
#: displace FRONTEND's own composition/typography/layout decisions.
DESIGN_RECOMMENDED = "design-recommended (optional reference — FRONTEND owns the final choice)"

#: Marker appended to guidance inferred from a retrieved reference, never from
#: the user. Kept distinct so the three classes can never be confused.
#: C: like DESIGN_RECOMMENDED this is an OPTIONAL reference, never an instruction.
REFERENCE_DERIVED = "reference-derived (optional reference, not user-specified)"

#: C: the explicit art-direction authority statement emitted as the guidance
#: document's PREAMBLE (before the first ``##`` section, so it never alters the
#: section contract). It names exactly what FRONTEND decides and what the
#: inferred lines mean, so a reader cannot mistake a recommendation for a rule.
ART_DIRECTION_PREAMBLE = (
    "> Art direction: FRONTEND owns the final design decisions — hero "
    "composition and visual focal point, typography pairing, imagery treatment, "
    "section rhythm, responsive layout, and colour-accent placement. "
    "Lines tagged [design-recommended] or [reference-derived] are OPTIONAL "
    "references offered for consideration, not execution instructions; FRONTEND "
    "may choose any coherent alternative that still satisfies the owner's "
    "requirements below. Lines tagged [user-specified] are the owner's own words "
    "and remain MANDATORY, as do the PRD requirements."
)

#: Axis vocabulary now lives in ``app.core.design_intent`` (V3-A), which routes
#: the owner's clauses to the facet that speaks to them. This module no longer
#: keeps a second copy, so there is a single routing authority.

#: Descriptors that describe a DOCUMENT or DIRECTORY rather than a design. They
#: are uninformative as composition instruction — the exact failure V2-B fixes,
#: where an OpenViking "this directory contains a craft details guide …" string
#: was presented as Composition guidance. Their provenance is still recorded.
_META_DESCRIPTOR_MARKERS: Tuple[str, ...] = (
    "this directory",
    "this file",
    "this document",
    "this folder",
    "readme",
    "guide for",
    "craft details guide",
    "implementation patterns",
    "documentation",
    "the following file",
)

#: Language that indicates a descriptor actually carries design/composition
#: vocabulary (so it is usable as composition guidance). A descriptor with NONE
#: of these is treated as uninformative and excluded from Composition.
_COMPOSITION_LANGUAGE: Tuple[str, ...] = (
    "grid", "layout", "composition", "editorial", "asymmetric", "whitespace",
    "white space", "bento", "mosaic", "magazine", "column", "typograph",
    "serif", "sans", "minimal", "swiss", "brutalist", "geometric", "spacious",
    "hierarchy", "rhythm", "alignment", "showcase", "immersive", "motion",
    "scroll", "transition", "hover", "palette", "colour", "color", "tone",
)

#: Motion-specific language, for the Motion section's reference-derived branch.
_MOTION_LANGUAGE: Tuple[str, ...] = (
    "scroll", "motion", "animation", "transition", "timeline", "hover",
    "parallax", "easing",
)

#: The Asset Strategy section (V3-B) is emitted but NOT required by the
#: document contract. It is optional precisely so an OLD DesignGuidance.md
#: (compiled before V3-B) still validates and reads back unchanged.
_ASSET_STRATEGY_HEADING = "Asset Strategy"
_ASSET_STRATEGY_SLUG = "asset_strategy"


def _render_asset_strategy(brief: Mapping[str, Any]) -> str:
    """Render the V3-B Asset Strategy section for the pre-build contract.

    Actionable, provenance-honest instructions derived from the brief: the
    showcase asset state, what to do when real assets are absent, and the
    launch-critical vs optional content still missing. It never invents a
    project, client, service, location, award, or testimonial, and it never
    creates an external image dependency.
    """
    plan = plan_assets(brief)
    state = plan["states"].get("showcase", "")
    lines = [f"- Showcase asset state: {state}" if state else
             f"- {UNRESOLVED_MARKER}: no visual showcase is described"]
    for instruction in plan["instructions"]:
        lines.append(f"- {instruction}")
    if plan["owner_assets"]:
        lines.append(
            "- Owner-supplied assets: " + ", ".join(plan["owner_assets"])
        )
    for item in plan["missing_launch_critical"]:
        lines.append(f"- MISSING ({CONTENT_LAUNCH_CRITICAL}): {item}")
    for item in plan["missing_optional"]:
        lines.append(f"- MISSING ({CONTENT_OPTIONAL}): {item}")
    return "\n".join(lines)


def _intent_axes(intent: str) -> dict:
    """Return ``{axis: bool}`` for which facets the OWNER's text mentions.

    Thin wrapper over the V3-A clause router so callers that only need a
    boolean keep working. It never authors or styles anything.
    """
    by_axis = clauses_by_axis(intent)
    return {axis: bool(clauses) for axis, clauses in by_axis.items()}


def _is_informative_descriptor(text: str) -> bool:
    """Whether ``text`` is usable as design/composition guidance.

    ``False`` for an empty string, a document/directory meta-description, or any
    text carrying no design vocabulary at all. This is a RELEVANCE gate on a
    retrieved string, not a styling rule: it never changes the site, only
    whether an uninformative reference string is quoted as guidance.
    """
    candidate = (text or "").strip().lower()
    if not candidate:
        return False
    if any(marker in candidate for marker in _META_DESCRIPTOR_MARKERS):
        return False
    return any(term in candidate for term in _COMPOSITION_LANGUAGE)


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
    # N8 B7: reference text is untrusted and reaches the Design Guidance
    # DOCUMENT here. `_clean` only strips, so control characters and newlines
    # would survive into a file that other systems read. one_line() collapses
    # them to a single inert line. Rendering normalises; stored evidence is
    # never mutated.
    descriptor = one_line(getattr(candidate, "descriptor", ""), 600) if candidate else ""
    label = one_line(getattr(candidate, "label", ""), 160) if candidate else ""
    palette: Tuple[str, ...] = ()
    if candidate is not None:
        raw_palette = getattr(candidate, "palette", ()) or ()
        palette = tuple(
            one_line(p, 40) for p in raw_palette if one_line(p, 40)
        )

    source_uri = one_line(getattr(candidate, "source_uri", ""), 300) if candidate else ""
    source_revision = (
        one_line(getattr(candidate, "source_revision", ""), 64) if candidate else ""
    )

    # V2-A/V2-B: the OWNER'S OWN design-intent words, verbatim. This is the
    # primary, highest-authority design evidence when present.
    design_intent = _clean(brief.get("design_intent"))

    # N8 B7: a selection is NOT a quality verification. When no suitability
    # score exists, the evidence says so explicitly, so a reader of the guidance
    # cannot mistake "selected" for "visually assessed".
    suitability = getattr(candidate, "suitability", None) if candidate else None
    has_suitability = suitability is not None
    suitability_value = float(suitability) if has_suitability else 0.0

    # N8 B8: the audit trail a reader needs to judge the assessment. Only the
    # contract and requirement revision are carried -- never a confidence, which
    # is deliberately absent from the candidate entirely.
    suitability_contract = (
        one_line(getattr(candidate, "suitability_contract", "") or "", 64)
        if candidate else ""
    )
    requirement_version = (
        one_line(getattr(candidate, "requirement_version", "") or "", 64)
        if candidate else ""
    )
    raw_detail = getattr(candidate, "suitability_detail", ()) or () if candidate else ()
    suitability_detail: Tuple[Tuple[str, float], ...] = ()
    if has_suitability and raw_detail:
        try:
            suitability_detail = tuple(
                (one_line(name, 32), float(value))
                for name, value in dict(raw_detail).items()
            )
        except (TypeError, ValueError):
            suitability_detail = ()

    return {
        "name": name,
        "what": what,
        "why": why,
        "design_intent": design_intent,
        "brand": brand,
        "descriptor": descriptor,
        "label": label,
        "palette": palette,
        "source_uri": source_uri,
        "source_revision": source_revision,
        "has_candidate": candidate is not None,
        "has_suitability": has_suitability,
        "suitability": suitability_value,
        "suitability_contract": suitability_contract,
        "suitability_detail": suitability_detail,
        "requirement_version": requirement_version,
        "selection_basis": (
            "assessed_suitability" if has_suitability else "grounding_only"
        ),
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
    # V2-B: only a descriptor that actually carries design language is offered as
    # layout evidence. A document/directory meta-description ("this directory
    # contains a craft details guide …") is uninformative and is EXCLUDED here,
    # so it can never steer selection either.
    if descriptor and _is_informative_descriptor(descriptor):
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


def _render_suitability_line(evidence: Mapping[str, Any]) -> str:
    """Render the suitability assessment, or an explicit NOT-ASSESSED note.

    N8 B8. This is the honesty line. A selection made on grounding alone must
    never read as a quality verification, so the two cases are rendered in
    visibly different language rather than the same field with a different
    number.
    """
    if not evidence.get("has_candidate"):
        return ""
    if not evidence.get("has_suitability"):
        return (
            f"{UNRESOLVED_MARKER}: visual design quality was NOT assessed. "
            "The reference was selected on grounding and provenance only "
            "(no multimodal suitability score exists for this build)."
        )

    lines = [
        f"Suitability: {float(evidence.get('suitability') or 0.0):.3f} "
        f"(selection basis: {evidence.get('selection_basis') or 'assessed_suitability'})"
    ]
    contract = str(evidence.get("suitability_contract") or "").strip()
    if contract:
        lines.append(f"Suitability contract: {contract}")
    requirement = str(evidence.get("requirement_version") or "").strip()
    if requirement:
        lines.append(f"Requirement version: {requirement}")

    detail = evidence.get("suitability_detail") or ()
    try:
        pairs = [(str(k), float(v)) for k, v in dict(detail).items()]
    except (TypeError, ValueError):
        pairs = []
    if pairs:
        rendered = ", ".join(f"{name} {value:.2f}" for name, value in pairs)
        lines.append(f"Dimension scores: {rendered}")
    return "\n".join(lines)


def _render_approved_resources(plan, evidence: Mapping[str, Any]) -> str:
    """Render the approved-resources section, grounded in real provenance.

    N8 B8: the selected reference now travels with the evidence that justifies
    it -- its revision, the requirement version it was judged against, and its
    suitability only when a score legitimately exists.
    """
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
        assessment = _render_suitability_line(evidence)
        if assessment:
            lines.append(assessment)
    if not lines:
        return f"{UNRESOLVED_MARKER}: no approved resource was selected from the evidence"
    return "\n".join(lines)


def _tag(text: str, marker: str) -> str:
    """Append an explicit provenance tag to a guidance line.

    The tag is how a reader (and a test) tells a line carrying the OWNER'S OWN
    words from a line inferred from a retrieved reference. It never changes the
    substance of the line.
    """
    return f"{text} [{marker}]"


def _render_facet(
    intent: str,
    by_axis: Mapping[str, Sequence[str]],
    axis: str,
    recommendation: str,
    reference_line: str,
    unresolved_note: str,
) -> str:
    """Render one facet as an ordered, provenance-tagged block (see above).

    ``recommendation`` may carry several newline-separated recommendations; each
    is rendered as its own tagged bullet so a reader can tell them apart and no
    recommendation inherits another's provenance.
    """
    parts = []
    for clause in by_axis.get(axis, ()):
        parts.append(_tag(clause, USER_SPECIFIED))
    for line in str(recommendation or "").splitlines():
        if line.strip():
            parts.append(_tag(line.strip(), DESIGN_RECOMMENDED))
    if reference_line:
        parts.append(_tag(reference_line, REFERENCE_DERIVED))
    if not parts:
        return f"{UNRESOLVED_MARKER}: {unresolved_note}"
    return "\n".join(f"- {part}" for part in parts)


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

    V2-B: the OWNER'S OWN design intent (``brief["design_intent"]``) is the
    highest-authority design evidence and is carried VERBATIM, tagged
    ``user-specified``, in its own section and — where the owner's words speak to
    an axis — in that axis's section. Guidance INFERRED from a retrieved
    reference is tagged ``reference-derived`` and never presented as the user's
    own instruction. A retrieved descriptor that is really a document/directory
    description is EXCLUDED from Composition (its provenance is still recorded).
    """
    evidence = guidance_evidence(brief, decision)
    plan = _select_resources(evidence, requested_resources=requested_resources)

    brand = evidence["brand"]
    descriptor = evidence["descriptor"]
    palette = evidence["palette"]
    intent = _owner_intent(brief)
    by_axis = clauses_by_axis(intent)

    brand_line = _line(brand)
    # The owner's own words, verbatim, in their own section. This is the
    # authoritative requirement and is NEVER replaced by a facet.
    design_intent_line = (
        _tag(intent, USER_SPECIFIED)
        if intent
        else f"{UNRESOLVED_MARKER}: the owner stated no explicit design preference"
    )

    # Application recommendations, grounded in the owner's stated terms + the
    # brief's domain. No fixed hex, ratio, count, or mandatory element.
    recs = _recommendations(brief, intent)

    # A retrieved descriptor is usable as composition guidance ONLY when it is
    # genuinely design language; a document/directory meta-description is never
    # presented as composition (its provenance is still recorded below).
    informative_descriptor = descriptor if _is_informative_descriptor(descriptor) else ""
    motion_descriptor = (
        descriptor
        if descriptor and any(kw in descriptor.lower() for kw in _MOTION_LANGUAGE)
        else ""
    )

    composition = _render_facet(
        intent, by_axis, "composition",
        recs.get("composition", ""), informative_descriptor,
        "no composition evidence supplied",
    )
    typography = _render_facet(
        intent, by_axis, "typography",
        recs.get("typography", ""), "",
        "no typography evidence supplied (FRONTEND owns the final choice)",
    )
    # Colour: the owner's clauses, an application role recommendation, and a
    # retrieved palette (provenanced) all coexist, each tagged.
    palette_line = ", ".join(palette)
    color = _render_facet(
        intent, by_axis, "color",
        recs.get("color", ""), palette_line,
        "no supporting evidence supplied",
    )
    imagery = _render_facet(
        intent, by_axis, "imagery",
        recs.get("imagery", ""), "",
        "no imagery evidence supplied",
    )
    # P1-A: the action-affordance recommendation rides in the EXISTING
    # Hierarchy section rather than a new one, so the ten-section document
    # contract is unchanged. Both lines are independently tagged; either can
    # be absent without affecting the other.
    hierarchy_parts = [p for p in (recs.get("hierarchy", ""), recs.get("action", "")) if p]
    hierarchy = _render_facet(
        intent, by_axis, "hierarchy",
        "\n".join(hierarchy_parts), "",
        "no supporting evidence supplied",
    )
    interaction = _render_facet(
        intent, by_axis, "interaction",
        "", "",
        "no interaction evidence supplied (only added where relevant)",
    )
    motion = _render_facet(
        intent, by_axis, "motion",
        "", motion_descriptor,
        "no supporting evidence supplied",
    )
    approved = _render_approved_resources(plan, evidence)
    asset_strategy = _render_asset_strategy(brief)

    rendered = {
        "brand_character": brand_line,
        "design_intent": design_intent_line,
        "composition": composition,
        "typography": typography,
        "color": color,
        "imagery": imagery,
        "hierarchy": hierarchy,
        "interaction": interaction,
        "motion": motion,
        "approved_resources": approved,
    }
    # V3-B: emit the Asset Strategy section after the required sections. It is
    # optional in the contract (not in REQUIRED_SECTIONS), so old documents stay
    # readable.
    #
    # C: the art-direction authority statement is the document PREAMBLE (before
    # the first ``##``), so it never changes the section contract while still
    # telling every reader — and FRONTEND — exactly what the inferred lines mean.
    body_parts = [
        f"{ART_DIRECTION_PREAMBLE}\n",
        *[
            f"## {heading}\n\n{rendered[slug]}\n"
            for heading, slug in _GUIDANCE_SECTIONS
        ],
    ]
    body_parts.append(
        f"## {_ASSET_STRATEGY_HEADING}\n\n{asset_strategy}\n"
    )

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
    "USER_SPECIFIED",
    "DESIGN_RECOMMENDED",
    "REFERENCE_DERIVED",
    "DesignGuidanceError",
    "guidance_evidence",
    "guidance_selection_input",
    "compile_design_guidance",
    "guidance_digest",
]
