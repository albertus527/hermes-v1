"""Deterministic engine exceptions (R2.7 §19).

A ``DeterministicEngineHalt`` is the §19 item 6 'HERMES CRITICAL HALT'
condition: the full pipeline halts, the run is marked non-canonical, and
there is no auto-restart. These exceptions are raised only for genuine
spec violations — never for ordinary gate rejections or data exclusions,
which are modeled as result/event codes instead.
"""

from __future__ import annotations


class DeterministicEngineHalt(Exception):
    """§19 item 6 — full pipeline halt; run marked non-canonical."""

    def __init__(self, reason_code: str, message: str, details: dict | None = None):
        self.reason_code = reason_code
        self.details = details or {}
        super().__init__(f"[{reason_code}] {message}")


class FeedParityViolation(DeterministicEngineHalt):
    """§19 item 8 — a decision-consumed bar has feed != 'sip' (§3.5)."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__("FEED_PARITY_VIOLATION", message, details)


class NewsCacheIntegrityFailure(DeterministicEngineHalt):
    """P-4 — same (headline_hash, ticker) with differing effect fields."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__("NEWS_CACHE_INTEGRITY_FAILURE", message, details)


class TrainFeeSubstitutionViolation(DeterministicEngineHalt):
    """§9.7 item 3 — TRAIN_SUBSTITUTED_ZERO attempted outside a TRAIN run."""

    def __init__(self, message: str, details: dict | None = None):
        super().__init__("TRAIN_SUBSTITUTED_ZERO_ILLEGAL_ROLE", message, details)


class FeeScheduleUnverified(Exception):
    """§9.7 item 6 / §19 item 7 — live fee-schedule gap blocks BUY output.

    Raised only in LIVE runs; TRAIN runs substitute per §9.7 item 3 and
    TEST runs exclude unverified windows before simulation begins.
    """
