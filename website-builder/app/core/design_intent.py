"""V3-A: decompose the owner's design intent into per-facet actionable guidance.

The owner's ORIGINAL design-intent statement stays the authoritative
requirement and is preserved **verbatim** (that is the Design Guidance
compiler's ``design_intent`` section). This module does not replace it. It does
two bounded, deterministic things:

1. **Splits** the intent into clauses and assigns each clause to the facet(s)
   it actually speaks to, so Composition / Typography / Color carry *their own*
   words instead of the whole directive repeated three times (the V3-A defect).
2. **Translates** the owner's stated design terms into implementable
   recommendations, grounded in the brief's own domain.

Three provenance classes are kept strictly apart and never conflated:

* ``USER_SPECIFIED``      — the owner's own words, verbatim.
* ``DESIGN_RECOMMENDED``  — an application implementation suggestion, derived
  from the owner's stated terms + the brief's domain. Never presented as the
  owner's words, and never a fixed hex, palette ratio, component count, or
  mandatory element.
* ``REFERENCE_DERIVED``   — a pattern from a retrieved, provenanced reference.

No model call, no network, no state. Pure functions over strings and mappings.
"""

from __future__ import annotations

import re
from typing import Any, Mapping, Optional, Sequence, Tuple

#: The design facets, in the fixed order the guidance renders them.
AXES: Tuple[str, ...] = (
    "composition",
    "typography",
    "color",
    "imagery",
    "hierarchy",
    "interaction",
    "motion",
)

#: Bounded, lower-case vocabulary used ONLY to route a clause to a facet. Each
#: term appears in at most one facet so routing is unambiguous. The vocabulary
#: never styles anything and is never counted — the owner's clause is carried
#: verbatim regardless of how many terms it matches.
AXIS_TERMS: Tuple[Tuple[str, Tuple[str, ...]], ...] = (
    (
        "composition",
        (
            "komposisi", "composition", "layout", "tata letak", "grid",
            "editorial", "minimalis", "minimal", "premium", "modern",
            "asimetris", "asymmetric", "whitespace", "white space", "bersih",
            "clean", "kolom", "column", "bento", "mosaic", "magazine",
        ),
    ),
    (
        "typography",
        (
            "tipografi", "typography", "font", "huruf", "typeface", "serif",
            "sans", "elegan", "elegant",
        ),
    ),
    (
        "color",
        (
            "warna", "color", "colour", "palet", "palette", "terracotta",
            "aksen", "accent", "off-white", "off white", "charcoal",
            "monokrom", "monochrome", "netral", "neutral",
        ),
    ),
    (
        "imagery",
        (
            "gambar", "foto", "photo", "imagery", "image", "visual",
            "ilustrasi", "illustration", "render", "video",
        ),
    ),
    (
        "hierarchy",
        (
            "hierarki", "hierarchy", "prioritas", "penekanan", "emphasis",
            "kontras", "contrast", "urutan", "fokus", "focal",
        ),
    ),
    (
        "interaction",
        (
            "interaksi", "interaction", "hover", "klik", "click",
            "microinteraction", "micro-interaction", "gesture",
        ),
    ),
    (
        "motion",
        (
            "animasi", "animation", "motion", "transisi", "transition",
            "scroll", "gerak", "parallax", "easing",
        ),
    ),
)

_AXIS_TERMS: dict = {axis: terms for axis, terms in AXIS_TERMS}

#: Sentence terminators, then coordinating separators, split the intent into
#: clauses. Both are applied so "off-white, charcoal, serta aksen terracotta"
#: becomes three clauses the Color facet can carry verbatim.
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+")
_CLAUSE_SPLIT = re.compile(
    r"\s*[,;]\s*|\s+(?:dan|serta|and|or)\s+", re.IGNORECASE
)
#: A leading/trailing coordinating conjunction left over from a comma split
#: ("premium, dan modern" → "dan modern"). Stripped for readability; the clause
#: body itself is never altered.
_LEADING_CONJUNCTION = re.compile(
    r"^(?:dan|serta|and|or|atau)\s+", re.IGNORECASE
)
_TRAILING_CONJUNCTION = re.compile(
    r"\s+(?:dan|serta|and|or|atau)$", re.IGNORECASE
)

#: Composition terms → an implementation MECHANISM. Mechanism only: no hex, no
#: ratio, no count, no mandatory element. Emitted only for terms the owner used.
_COMPOSITION_MECHANISMS: Tuple[Tuple[str, str], ...] = (
    (
        "editorial",
        "organise the page as an editorial sequence: a strong opening "
        "statement, content on a considered grid, and deliberate variation in "
        "emphasis between items",
    ),
    (
        "minimalis",
        "keep decoration to a minimum and let whitespace and alignment carry "
        "the composition",
    ),
    (
        "minimal",
        "keep decoration to a minimum and let whitespace and alignment carry "
        "the composition",
    ),
    (
        "premium",
        "convey quality through restraint: generous spacing, precise "
        "alignment, and a disciplined type scale rather than ornament",
    ),
    (
        "modern",
        "use a clear responsive grid that reflows to a single column on small "
        "screens",
    ),
    (
        "asymmetric",
        "introduce asymmetry: vary column spans and item sizes instead of a "
        "uniform grid",
    ),
    (
        "asimetris",
        "introduce asymmetry: vary column spans and item sizes instead of a "
        "uniform grid",
    ),
    (
        "bento",
        "use a modular grid with varied cell sizes to give the showcase rhythm",
    ),
    (
        "mosaic",
        "use a mosaic arrangement so the showcase reads as a body of work "
        "rather than a uniform list",
    ),
    (
        "magazine",
        "use a magazine-style multi-column rhythm with a clear lead item",
    ),
)

#: Typography terms → an implementation MECHANISM.
_TYPOGRAPHY_MECHANISMS: Tuple[Tuple[str, str], ...] = (
    (
        "elegan",
        "pair a refined, high-contrast display face for headings with a "
        "restrained neutral face for body text; keep to at most two families",
    ),
    (
        "elegant",
        "pair a refined, high-contrast display face for headings with a "
        "restrained neutral face for body text; keep to at most two families",
    ),
    (
        "serif",
        "use a serif or transitional display face for headings to carry the "
        "editorial character",
    ),
    (
        "sans",
        "use a neutral sans for body text so long-form reading stays "
        "comfortable",
    ),
)

#: Colour term → the ROLE it should play. Role only: the owner's own colour
#: names are preserved and never converted to a fixed hex.
_COLOR_ROLES: Tuple[Tuple[str, str], ...] = (
    ("off-white", "surface / background"),
    ("off white", "surface / background"),
    ("charcoal", "primary text / ink"),
    ("terracotta", "accent"),
)

#: Terms that mean the site will SHOW visual content (a showcase, gallery, or
#: portfolio). Imagery guidance is only relevant then — otherwise an imagery
#: strategy would be an invented requirement, not grounded guidance.
_VISUAL_CONTENT_TERMS: Tuple[str, ...] = (
    "showcase", "portfolio", "gallery", "galeri", "proyek", "project",
    "karya", "work", "case study", "lookbook", "catalog", "katalog",
    "gambar", "foto", "photo", "image", "imagery", "visual", "render",
)

#: Terms that mean "an accent colour exists" without naming one.
_ACCENT_TERMS = ("aksen", "accent")


def _clean(value: Any) -> str:
    """Return a trimmed string, or ``""`` for anything that is not text."""
    if not isinstance(value, str):
        return ""
    return value.strip()


def split_clauses(intent: str) -> Tuple[str, ...]:
    """Split ``intent`` into clause fragments, deterministically.

    Sentence terminators first, then coordinating separators. Punctuation-only
    fragments are dropped. The fragments are substrings of the original text,
    so nothing is paraphrased.
    """
    text = _clean(intent)
    if not text:
        return ()
    out = []
    for sentence in _SENTENCE_SPLIT.split(text):
        sentence = sentence.strip()
        if not sentence:
            continue
        for fragment in _CLAUSE_SPLIT.split(sentence):
            fragment = fragment.strip(" .,;:!?")
            fragment = _LEADING_CONJUNCTION.sub("", fragment)
            fragment = _TRAILING_CONJUNCTION.sub("", fragment)
            fragment = fragment.strip(" .,;:!?")
            if fragment:
                out.append(fragment)
    return tuple(out)


def clauses_by_axis(intent: str) -> dict:
    """Return ``{axis: (verbatim clause, ...)}`` for the clauses that speak to it.

    A clause is assigned to every axis whose vocabulary it contains. It is
    carried VERBATIM; routing never rewrites, extends, or drops a requirement.
    """
    result: dict = {axis: [] for axis in AXES}
    for clause in split_clauses(intent):
        lowered = clause.lower()
        for axis, terms in AXIS_TERMS:
            if any(term in lowered for term in terms):
                result[axis].append(clause)
    return {axis: tuple(values) for axis, values in result.items()}


def _domain_phrase(brief: Mapping[str, Any]) -> str:
    """The brief's own domain wording, for a context-specific recommendation."""
    for key in ("what", "why", "name"):
        value = _clean((brief or {}).get(key))
        if value:
            return value
    return "this website"


def _mechanisms(intent_lower: str, table: Sequence[Tuple[str, str]]) -> Tuple[str, ...]:
    """The de-duplicated mechanisms whose term the owner actually used."""
    seen: list = []
    for term, mechanism in table:
        if term in intent_lower and mechanism not in seen:
            seen.append(mechanism)
    return tuple(seen)


def _imagery_is_relevant(brief: Mapping[str, Any], intent: str) -> bool:
    """Whether the site will actually SHOW visual content (grounded check).

    True when the brief or the owner's intent speaks of a showcase, gallery,
    portfolio, or images. Otherwise an imagery strategy would be an invented
    requirement rather than grounded guidance, so it stays UNRESOLVED.
    """
    haystack = " ".join(
        _clean((brief or {}).get(k)) for k in ("name", "what", "why")
    ).lower()
    haystack = f"{haystack} {_clean(intent).lower()}"
    return any(term in haystack for term in _VISUAL_CONTENT_TERMS)


def recommendations(
    brief: Mapping[str, Any],
    intent: str,
    *,
    imagery_is_relevant: Optional[bool] = None,
) -> dict:
    """Return ``{axis: recommendation}`` — implementable, tagged, non-imposing.

    A recommendation is emitted ONLY where it is grounded: composition /
    typography / color fire on the owner's own stated terms; hierarchy fires
    from a stated typography preference; imagery fires only when the brief
    actually calls for visual content, as a conditional anti-fabrication
    strategy. Interaction and motion never auto-fire (the decision says "only
    where relevant"); they stay UNRESOLVED unless the owner or a reference
    supplies evidence. No fixed hex, ratio, or count is emitted.
    """
    intent_lower = _clean(intent).lower()
    domain = _domain_phrase(brief)
    if imagery_is_relevant is None:
        imagery_is_relevant = _imagery_is_relevant(brief, intent)
    out: dict = {}

    composition = _mechanisms(intent_lower, _COMPOSITION_MECHANISMS)
    if composition:
        out["composition"] = f"For {domain}: " + "; ".join(composition)

    typography = _mechanisms(intent_lower, _TYPOGRAPHY_MECHANISMS)
    if typography:
        out["typography"] = "; ".join(typography)

    roles = [(term, role) for term, role in _COLOR_ROLES if term in intent_lower]
    if roles:
        rendered = ", ".join(f"{term} as {role}" for term, role in roles)
        out["color"] = (
            "assign the named colours distinct, consistent roles so they are "
            f"not used interchangeably ({rendered})"
        )
    elif any(term in intent_lower for term in _ACCENT_TERMS):
        out["color"] = (
            "keep the accent to a small number of high-signal elements "
            "(primary action, active state) so it reads as intentional"
        )

    if typography:
        out["hierarchy"] = (
            "establish a clear typographic hierarchy: one dominant display "
            "level, one secondary level, and a quiet body level, with "
            "consistent scale steps"
        )

    if imagery_is_relevant:
        out["imagery"] = (
            f"plan an imagery strategy for {domain}: favour licensed project "
            "or architectural photography where assets exist; where none "
            "exist, use clearly-labelled conceptual visuals or placeholders, "
            "and never fabricate project names, clients, or photographs"
        )

    return out


def owner_intent(brief: Mapping[str, Any]) -> str:
    """The owner's OWN design-intent words, verbatim, or ``""``."""
    return _clean((brief or {}).get("design_intent"))


def retrieval_query(brief: Mapping[str, Any]) -> str:
    """Build the design-retrieval query from the brief, deterministically.

    V3-B: the query carries the project's DOMAIN (name/what/why), the intended
    AESTHETIC (the owner's own design-intent words), and the PAGE TYPE / visual
    requirements implied by the brief — so retrieval is steered toward
    references that match the requested look instead of a generic term. It is
    still application-owned text; no retrieved content and no model output feeds
    back into it. Returns ``""`` only when the brief carries nothing at all.
    """
    brief = brief if isinstance(brief, Mapping) else {}
    parts: list = []
    for key in ("name", "what", "why"):
        value = _clean(brief.get(key))
        if value:
            parts.append(value)
    intent = owner_intent(brief)
    if intent:
        # The intended aesthetic is a first-class retrieval signal.
        parts.append(intent)
    return " ".join(parts).strip()


__all__ = [
    "AXES",
    "AXIS_TERMS",
    "owner_intent",
    "retrieval_query",
    "split_clauses",
    "clauses_by_axis",
    "recommendations",
]
