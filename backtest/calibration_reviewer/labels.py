"""Four-field human-label validation for the §11.4 reviewer.

The human review label contains ONLY ``category``, ``direction``,
``severity`` and ``ma_role`` (§11.4 pre-registered label set). It is NOT an
LLM classification payload: there is no ``confidence``, ``published_at``,
``source``, ``model_version`` or ``keyword_override`` in a human label, and
none is fabricated to satisfy ``backtest.news.cache.validate_classification_payload``
(that validator targets the strict 12-field ``news_schema_v3`` payload).

The authoritative enumerations are imported from ``trading_core.news_effects``
so the reviewer's option lists can never drift from §11.1. The conditional
``ma_role`` rule mirrors §11.1: ``ma_role`` is required (TARGET | ACQUIRER |
NEITHER) when ``category == "M&A"`` and must equal ``NEITHER`` for every
non-M&A category.
"""

from __future__ import annotations

from dataclasses import dataclass

from trading_core.news_effects import (
    CATEGORIES,
    DIRECTIONS,
    MA_ROLES,
    SEVERITIES,
)

LABEL_FIELDS = ("category", "direction", "severity", "ma_role")

# Re-exported for the UI so the browser offers exactly the canonical sets.
ENUMS = {
    "category": list(CATEGORIES),
    "direction": list(DIRECTIONS),
    "severity": list(SEVERITIES),
    "ma_role": list(MA_ROLES),
}

_ALLOWED = {
    "category": set(CATEGORIES),
    "direction": set(DIRECTIONS),
    "severity": set(SEVERITIES),
    "ma_role": set(MA_ROLES),
}


class InvalidHumanLabelError(ValueError):
    """A human label violates §11.1 enum or conditional-validity rules."""


@dataclass(frozen=True)
class HumanLabel:
    """A validated four-field human label."""

    category: str
    direction: str
    severity: str
    ma_role: str

    def as_dict(self) -> dict[str, str]:
        return {f: getattr(self, f) for f in LABEL_FIELDS}


def validate_human_label(label: dict) -> HumanLabel:
    """Validate a four-field human label, fail-closed.

    Rejects: non-dict input, missing/extra fields, unknown enum values, and
    any ``ma_role`` that violates the §11.1 conditional rule. ``NEITHER`` is
    a legal ``ma_role`` for ``M&A`` (a non-TARGET M&A classification).
    """
    if not isinstance(label, dict):
        raise InvalidHumanLabelError("label must be an object")
    missing = [f for f in LABEL_FIELDS if f not in label]
    if missing:
        raise InvalidHumanLabelError(f"missing label fields: {sorted(missing)}")
    extra = sorted(set(label) - set(LABEL_FIELDS))
    if extra:
        raise InvalidHumanLabelError(f"unexpected label fields: {extra}")
    for field in LABEL_FIELDS:
        value = label[field]
        if not isinstance(value, str) or not value:
            raise InvalidHumanLabelError(f"{field} must be a non-empty string")
        if value not in _ALLOWED[field]:
            raise InvalidHumanLabelError(f"unknown {field} {value!r}")
    category = label["category"]
    ma_role = label["ma_role"]
    if category != "M&A" and ma_role != "NEITHER":
        raise InvalidHumanLabelError(
            f"ma_role must be NEITHER for category {category!r}")
    return HumanLabel(
        category=category,
        direction=label["direction"],
        severity=label["severity"],
        ma_role=ma_role,
    )


def is_complete_label(label: dict | None) -> bool:
    """True iff ``label`` is a complete, valid four-field human label."""
    if not isinstance(label, dict):
        return False
    try:
        validate_human_label(label)
    except InvalidHumanLabelError:
        return False
    return True
