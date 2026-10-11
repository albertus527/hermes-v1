"""Proposed labeling rubric for the §11.4 human calibration reviewer.

STATUS: PROPOSED — PENDING HUMAN APPROVAL.

This rubric is guidance for a human reviewer. It does NOT change §11.2
trading-effect mapping, G7 windows, confidence semantics, or §11.3 keyword
fallback. It invents no numerical financial-materiality thresholds.

Canonical locking gate: labels may be *drafted* at any time, but a label can
only be counted toward canonical §11.4 progress once the reviewer has
recorded an explicit, auditable rubric approval for this exact
``rubric_version``. Until then, locked labels are recorded as
``provisional`` (still fully auditable) and are excluded from the canonical
count.
"""

from __future__ import annotations

import hashlib

RUBRIC_VERSION = "r281-reviewer-rubric-0.1"
RUBRIC_STATUS = "PROPOSED — PENDING HUMAN APPROVAL"

# Concise, per-field guidance shown in the UI. Kept short for fast review.
RUBRIC = {
    "status": RUBRIC_STATUS,
    "version": RUBRIC_VERSION,
    "category": (
        "Pick the single dominant event type from the canonical news_schema_v3 "
        "list. Use OTHER only when no company-specific category applies "
        "(market-wide or industry commentary, listicles, generic round-ups)."
    ),
    "entity_relevance": (
        "The ticker must be the subject — or a clearly named affected party — "
        "of the headline. Competitor or read-through headlines that merely "
        "mention the company are not company-specific. ETF/leveraged-product "
        "tickers (QQQ, VUG, SCHG, VGT, SMH, IWF) are valid review subjects, "
        "but do NOT infer a company-specific event for them without direct "
        "evidence in the headline."
    ),
    "direction": (
        "BULLISH / BEARISH only when the headline states a company-level "
        "positive / negative event. Otherwise NEUTRAL."
    ),
    "severity": (
        "LOW: routine or minor mention. MEDIUM: clear but non-extreme "
        "company impact. HIGH: severe, company-wide, likely material. "
        "CRITICAL: catastrophic company-level event (e.g. operations halted, "
        "fraud, delisting). Judge severity from the event described, not from "
        "how strongly the headline is worded. No numeric thresholds are "
        "canonical."
    ),
    "confirmation_and_evidence": (
        "Distinguish FIVE independent questions and do not collapse them: "
        "(1) has the underlying event been CONFIRMED to have occurred (a filed "
        "suit, an announced deal, a reported figure) versus merely alleged, "
        "rumored, under investigation, or a potential/could future event; "
        "(2) are the underlying ALLEGATIONS proven (liability established) — "
        "usually NOT at the headline stage even for a confirmed filing; "
        "(3) company-level RELEVANCE (is the ticker the affected entity); "
        "(4) potential financial/operational MATERIALITY; (5) STRENGTH of the "
        "evidence actually available in the headline. A confirmed filing of a "
        "major lawsuit is NOT the same as an unverified rumor, even though "
        "liability is not yet established. Do not apply a blanket rule that "
        "unproven matters must be at most MEDIUM — assess each dimension."
    ),
    "ma_role": (
        "TARGET: the ticker is the acquired party. ACQUIRER: the ticker is the "
        "buyer. NEITHER otherwise. ma_role MUST be NEITHER for every non-M&A "
        "category."
    ),
    "other_vs_specific": (
        "OTHER only for genuinely non-company-specific or unclassifiable "
        "items; prefer the specific category when a company-specific event is "
        "present."
    ),
    "unresolved": [
        "Whether a confirmed CRITICAL event affecting only one division is "
        "HIGH or CRITICAL.",
        "Whether a confirmed major lawsuit filing is HIGH or CRITICAL before "
        "liability is established.",
        "Whether analyst rating changes that reference earnings are ANALYST "
        "or OTHER.",
        "Whether CAPITAL_RETURN requires an executed action or an "
        "announced/considered one.",
        "Whether §11.3 keyword-eligible headlines should be visually "
        "pre-flagged in the rubric (they must NEVER prepopulate fields).",
        "Canonical target: Dataset A (240) alone as the §11.4 set, with "
        "Datasets B and C reported separately.",
    ],
}


def rubric_help() -> dict:
    """The rubric payload served to the browser (no sample data)."""
    return RUBRIC


def rubric_sha256() -> str:
    """SHA-256 of the exact rubric content behind ``RUBRIC_VERSION``.

    Approvals are bound to (version, digest): if a later edit changes any
    wording, the digest changes and the old approval no longer matches —
    the reviewer must approve the new content explicitly. Version strings
    alone cannot detect an unversioned content edit; the digest can.
    """
    return hashlib.sha256(
        repr(sorted(RUBRIC.items())).encode("utf-8")).hexdigest()
