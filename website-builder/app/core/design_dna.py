"""Design DNA validation helpers for Website Builder R1.

Small, focused, deterministic validators. Design DNA itself is owned by
FRONTEND (see .hermes/skills/website-builder-design-dna/SKILL.md); this
module only checks the persisted document against the fixed contract
constraints that application code is responsible for enforcing.
"""
from __future__ import annotations

from typing import Any, Dict, Optional


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
