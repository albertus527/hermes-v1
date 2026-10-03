"""Design DNA validation helpers for Website Builder R1.

Small, focused, deterministic validators. Design DNA itself is owned by
FRONTEND (see .hermes/skills/website-builder-design-dna/SKILL.md); this
module only checks the persisted document against the fixed contract
constraints that application code is responsible for enforcing.

This module is also the single place a persisted ``design-dna.json`` is turned
into an in-memory document. There is exactly ONE canonical shape — a flat object
whose contract fields live at the top level — because every consumer reads
top-level keys (``typography``, ``reference_synthesis``, ``version``). A document
read any other way silently fails those reads: a nested document reads as
"no typography", which is indistinguishable from "typography is fine".

:func:`load_persisted_design_dna` is therefore the only supported reader.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: The key a nested/legacy document puts the real Design DNA under.
DESIGN_DNA_WRAPPER_KEY = "design_dna"

#: The contract fields defined at the TOP LEVEL of the canonical document. This
#: is the persisted shape FRONTEND is told to produce and the only shape
#: validators, persisted state, and QA ever see.
CANONICAL_DESIGN_DNA_KEYS: Tuple[str, ...] = (
    "version",
    "brand_personality",
    "palette",
    "typography",
    "spacing",
    "page_inventory",
    "layout",
    "motion",
    "primary_cta",
    "assets",
    "verified_content",
    "unresolved_facts",
)


def unwrap_design_dna(raw: Any) -> Tuple[Any, bool]:
    """Return ``(document, was_wrapped)`` for a parsed Design DNA file.

    A canonical document is returned unchanged with ``was_wrapped=False``. A
    legacy/nested document — the whole document under a ``design_dna`` key — is
    unwrapped at this boundary and nowhere else, so no downstream consumer has
    to know the shape ever varied.

    Contract fields sitting BESIDE the wrapper are folded in, but only where the
    unwrapped body does not already define them: an observed nested artifact
    hoisted ``verified_content``/``unresolved_facts`` out of the wrapper, and
    dropping them would silently lose recorded facts. The wrapped body wins,
    because that is the copy the document is actually about.

    Anything else is returned untouched rather than reinterpreted — a non-dict,
    an empty document, or a ``design_dna`` key that is not a mapping. Guessing at
    those is how a malformed document starts validating as a valid one.
    """
    if not isinstance(raw, dict) or not raw:
        return raw, False
    body = raw.get(DESIGN_DNA_WRAPPER_KEY)
    if not isinstance(body, dict) or not body:
        return raw, False
    merged = dict(body)
    for key in CANONICAL_DESIGN_DNA_KEYS:
        if key not in merged and key in raw:
            merged[key] = raw[key]
    return merged, True


def load_persisted_design_dna(path: Path) -> Optional[Dict[str, Any]]:
    """Read ``path`` as a canonical Design DNA document, or return ``None``.

    ``None`` covers every reason the document is unusable — absent, unreadable,
    not JSON, not an object — so callers have one branch instead of re-deriving
    what "valid" means. An empty object is ``None`` too: it carries no design
    and must not pass as one.
    """
    try:
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = json.load(handle)
    except (OSError, ValueError):
        return None
    document, _ = unwrap_design_dna(raw)
    if not isinstance(document, dict) or not document:
        return None
    return document


def _font_families(design_dna: Dict[str, Any]) -> set:
    """Return the set of distinct, non-empty font family names in use."""
    typography = design_dna.get("typography") or {}
    if not isinstance(typography, dict):
        return set()
    families = set()
    for key in ("heading_font", "body_font"):
        value = typography.get(key)
        if isinstance(value, str) and value.strip():
            families.add(value.strip())
    return families


def validate_typography(design_dna: Optional[Dict[str, Any]]) -> bool:
    """Return True when Design DNA typography uses at most 2 font families.

    A missing/empty typography block is valid (nothing to violate). Only a
    concrete document naming 3+ distinct font families fails.
    """
    if not design_dna:
        return True
    return len(_font_families(design_dna)) <= 2


def typography_violation_message(design_dna: Dict[str, Any]) -> str:
    families = sorted(_font_families(design_dna))
    return (
        "Design DNA typography must use at most 2 font families, "
        f"found {len(families)}: {families}"
    )
