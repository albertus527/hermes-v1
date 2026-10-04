"""R2.8.1 §11.1 — deterministic canonical normalization of a model
classification BEFORE strict validation.

THE STRUCTURAL FAILURE MODE THIS EXISTS FOR
------------------------------------------
``NEWS_SCHEMA_V3_JSON_SCHEMA`` (and therefore any strict ``json_schema``
structured output) enumerates ``ma_role`` INDEPENDENTLY of ``category``.
It can express "``ma_role`` is one of TARGET|ACQUIRER|NEITHER" but NOT
"``ma_role`` is NEITHER unless ``category`` is M&A". The cross-field
invariant lives only in :func:`backtest.news.cache.validate_classification_payload`,
which FAILS CLOSED on a non-M&A row carrying TARGET/ACQUIRER.

Measured on the 99-row R2.8.1 safety-challenge benchmark (2026-10-03),
every single fail-closed candidate failure across all three candidates
was exactly that violation, and every one of them repeated IDENTICALLY
across all 5 bounded retries — a strict-schema cross-field rule is
deterministic, so retrying cannot fix it.

THE CANONICAL RULE (deterministic, total, idempotent)
-----------------------------------------------------
IF ``category`` is a valid category OTHER than ``"M&A"`` AND ``ma_role``
is a valid non-``NEITHER`` role:
    canonical ``ma_role`` := ``"NEITHER"``
and the normalization is RECORDED.

IF ``category == "M&A"``:
    ``ma_role`` is model-authored and is NEVER touched — it passes normal
    canonical validation (including the legal M&A + NEITHER case, §11.1).

EVERYTHING ELSE IS LEFT EXACTLY AS THE MODEL EMITTED IT. This module may
modify ``ma_role`` and NOTHING else: never ``category`` (no invalid-category
conversion), never ``direction``, never ``severity`` (no softening),
never ``confidence``. It never infers M&A, never flips TARGET<->ACQUIRER,
and never rewrites an M&A row. Every other contract violation — malformed
JSON, unknown enum, missing field, bad confidence, extra field — is NOT
this module's business and still fails closed in
``validate_classification_payload`` on the NORMALIZED payload.

Normalization is a REPAIR OF ONE deterministic cross-field inconsistency,
never a guess about content: the repaired value is entailed by the
category the model itself emitted. It is NOT a second opinion, and it is
NOT a license to accept anything else.

OBSERVABILITY (the patch must not hide model quality)
-----------------------------------------------------
Every call returns a :class:`ClassificationNormalization` carrying BOTH the
raw model fields and the canonical fields plus
``normalization_applied`` / ``normalization_reason``. The canonical
``news_schema_v3`` payload itself is UNCHANGED — these are provenance /
audit fields, deliberately NOT new payload fields, because
``_REQUIRED_PAYLOAD_FIELDS`` rejects extra fields and the §16 cache schema
is frozen. Callers surface the metadata in their existing audit
structures (the population report, the benchmark harness report).

IDEMPOTENCE
-----------
The function is a pure function of its input and touches ``ma_role`` only
when it is out of canonical form, so applying it twice yields the same
canonical payload and the second application reports
``normalization_applied=False`` — no duplicate audit event, no
double-counting. Feeding an already-canonical payload through is a no-op.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from trading_core.news_effects import CATEGORIES, MA_ROLES

__all__ = [
    "MA_CATEGORY",
    "NEITHER_MA_ROLE",
    "NORMALIZATION_REASON_NON_MA_MA_ROLE",
    "ClassificationNormalization",
    "normalize_classification_payload",
]

#: The one category for which a non-NEITHER ``ma_role`` is legitimate.
MA_CATEGORY = "M&A"

#: The canonical ``ma_role`` for every non-M&A category.
NEITHER_MA_ROLE = "NEITHER"

#: The exact, stable reason string recorded when a non-M&A row's ``ma_role``
#: was forced to NEITHER. Part of the audit contract — do not reword it.
NORMALIZATION_REASON_NON_MA_MA_ROLE = "NON_MA_ROLE_FORCED_NEITHER"

#: Effect fields the normalization is allowed to read. It may WRITE only
#: ``ma_role``; the rest are copied through byte-identically.
_EFFECT_FIELDS = ("category", "direction", "severity", "confidence")

_NO_REASON = None


@dataclass(frozen=True)
class ClassificationNormalization:
    """Outcome of one normalization pass: canonical payload + full audit.

    ``canonical`` is a NEW dict; the input mapping is never mutated.
    ``raw_*`` are the model-authored values (unchanged even when they are
    absent/unusable — then they are ``None`` and validation still fails
    closed downstream). ``canonical_*`` are the values that were handed to
    ``validate_classification_payload``.
    """

    canonical: dict
    raw_category: Any
    raw_direction: Any
    raw_severity: Any
    raw_ma_role: Any
    canonical_category: Any
    canonical_direction: Any
    canonical_severity: Any
    canonical_ma_role: Any
    normalization_applied: bool = False
    normalization_reason: str | None = _NO_REASON

    def audit_record(self) -> dict:
        """Flat, JSON-serializable provenance/audit row (sort-key stable)."""
        return {
            "raw_category": self.raw_category,
            "raw_direction": self.raw_direction,
            "raw_severity": self.raw_severity,
            "raw_ma_role": self.raw_ma_role,
            "canonical_category": self.canonical_category,
            "canonical_direction": self.canonical_direction,
            "canonical_severity": self.canonical_severity,
            "canonical_ma_role": self.canonical_ma_role,
            "normalization_applied": self.normalization_applied,
            "normalization_reason": self.normalization_reason,
        }


def normalize_classification_payload(payload: dict) -> ClassificationNormalization:
    """Apply the canonical §11.1 ``ma_role`` rule to a model classification.

    ``payload`` is the effect-fields mapping (the strict news_schema_v3
    payload, or just its ``category``/``direction``/``severity``/
    ``ma_role``/``confidence`` subset). Every key other than ``ma_role``
    is copied through UNCHANGED, and the input mapping is not mutated.

    Normalization is applied ONLY when the rule is unambiguous — i.e. both
    the category and the ma_role are known enum members. A missing or
    unknown ``category``/``ma_role`` is left alone so that
    ``validate_classification_payload`` rejects it exactly as before
    (normalization never rescues a malformed record).

    Idempotent: an already-canonical payload returns with
    ``normalization_applied=False`` and an identical canonical payload.
    """
    if not isinstance(payload, dict):
        # Not our failure class — hand it back untouched so the strict
        # validator raises its own "payload is not a JSON object".
        return ClassificationNormalization(
            canonical=payload,
            raw_category=None, raw_direction=None, raw_severity=None,
            raw_ma_role=None,
            canonical_category=None, canonical_direction=None,
            canonical_severity=None, canonical_ma_role=None,
        )

    raw_category = payload.get("category")
    raw_direction = payload.get("direction")
    raw_severity = payload.get("severity")
    raw_ma_role = payload.get("ma_role")

    canonical = dict(payload)
    applied = False
    reason = _NO_REASON

    if (raw_category in CATEGORIES
            and raw_category != MA_CATEGORY
            and raw_ma_role in MA_ROLES
            and raw_ma_role != NEITHER_MA_ROLE):
        canonical["ma_role"] = NEITHER_MA_ROLE
        applied = True
        reason = NORMALIZATION_REASON_NON_MA_MA_ROLE

    return ClassificationNormalization(
        canonical=canonical,
        raw_category=raw_category,
        raw_direction=raw_direction,
        raw_severity=raw_severity,
        raw_ma_role=raw_ma_role,
        canonical_category=canonical.get("category"),
        canonical_direction=canonical.get("direction"),
        canonical_severity=canonical.get("severity"),
        canonical_ma_role=canonical.get("ma_role"),
        normalization_applied=applied,
        normalization_reason=reason,
    )