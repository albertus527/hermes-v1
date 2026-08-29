"""R2.7 §11 news engine — deterministic mapping over cached classifications.

The LLM never decides trading effects. This module implements:

- FP-4 headline identity: NFKC -> casefold -> whitespace collapse -> strip
  -> Unicode punctuation strip (edges) -> SHA-256.
- §11.1 news_schema_v3 validation shape (category/direction/severity/
  ma_role/confidence conditional validity).
- §11.2 ordered total-function effect mapping with first-match exclusivity
  (MACRO/OTHER precedence; BEARISH-CRITICAL; M&A/TARGET; BEARISH HIGH;
  score branches) and the universal 24-hour NEWS_UNVERIFIED trigger window.
- FP-3 confidence < 0.85 behavior: 0 score contribution, veto/exit
  participation retained, NEWS_UNVERIFIED trigger.
- §11.3 keyword fallback (deterministic; forces BEARISH+CRITICAL) and the
  two-source CRITICAL confirmation rule (distinct headline_hash AND
  distinct source).
- P-4: effect fields are a function of normalized headline text + ticker
  only; same-(headline_hash, ticker) mismatch -> NewsCacheIntegrityFailure
  (halts under §19 item 6).

All windows are evaluated against the consuming scan's simulated decision
timestamp ``t`` (N-21); nothing here reads the wall clock.
"""

from __future__ import annotations

import datetime as _dt
import hashlib
import unicodedata
from dataclasses import dataclass, replace
from typing import Iterable, Mapping, Sequence

from trading_core.errors import NewsCacheIntegrityFailure

CONFIDENCE_THRESHOLD = 0.85          # ASSUMPTION pending calibration (§11.1)
KEYWORD_FALLBACK_TERMS = (
    "SEC investigation", "restatement", "withdraws guidance",
    "accounting fraud", "delisting",
)

CATEGORIES = ("EARNINGS", "GUIDANCE", "ANALYST", "M&A", "REGULATORY",
              "LEGAL", "PRODUCT", "MACRO", "INSIDER", "CAPITAL_RETURN",
              "OTHER")
DIRECTIONS = ("BULLISH", "BEARISH", "NEUTRAL")
SEVERITIES = ("LOW", "MEDIUM", "HIGH", "CRITICAL")
MA_ROLES = ("TARGET", "ACQUIRER", "NEITHER")

SCHEMA_VERSION_V3 = "news_schema_v3"

NEWS_UNVERIFIED_TRIGGER_WINDOW = _dt.timedelta(hours=24)  # universal (FP-3(c))
SCORING_WINDOW = _dt.timedelta(hours=24)
BEARISH_HIGH_WINDOW = _dt.timedelta(hours=24)
MA_TARGET_WINDOW = _dt.timedelta(days=30)


# ---------------------------------------------------------------------------
# Headline identity (FP-4)
# ---------------------------------------------------------------------------


def normalize_headline_text(text: str) -> str:
    """FP-4 normalization: NFKC -> casefold -> collapse whitespace runs to
    one ASCII space -> strip -> strip leading/trailing Unicode punctuation
    (General Category P*)."""
    t = unicodedata.normalize("NFKC", text).casefold()
    t = " ".join(t.split())
    t = t.strip()
    while t and unicodedata.category(t[0]).startswith("P"):
        t = t[1:].lstrip()
    while t and unicodedata.category(t[-1]).startswith("P"):
        t = t[:-1].rstrip()
    return t


def headline_hash(text: str) -> str:
    """Source-independent SHA-256 over the normalized headline text."""
    return hashlib.sha256(normalize_headline_text(text).encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Classification (cached payload replay shape)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Classification:
    """A cached news_schema_v3 classification (§11.1). ``published_at`` is
    an aware datetime; untimed headlines are dropped at ingestion and never
    reach this type (HEADLINE_UNTIMED)."""

    ticker: str
    category: str
    direction: str
    severity: str
    ma_role: str
    confidence: float
    published_at: _dt.datetime
    headline_hash: str
    source: str
    keyword_override: bool = False

    def __post_init__(self) -> None:
        if self.category not in CATEGORIES:
            raise ValueError(f"unknown category {self.category!r}")
        if self.direction not in DIRECTIONS:
            raise ValueError(f"unknown direction {self.direction!r}")
        if self.severity not in SEVERITIES:
            raise ValueError(f"unknown severity {self.severity!r}")
        if self.ma_role not in MA_ROLES:
            raise ValueError(f"unknown ma_role {self.ma_role!r}")
        # §11.1 conditional validity: ma_role is required (TARGET |
        # ACQUIRER | NEITHER) when category = M&A; NEITHER everywhere else.
        if self.category != "M&A" and self.ma_role != "NEITHER":
            raise ValueError(
                f"ma_role must be NEITHER for category {self.category!r}")

    @property
    def effect_fields(self) -> tuple:
        """P-4 effect fields: the only fields that may carry decision
        effect; must be identical across all cache entries sharing
        (headline_hash, ticker)."""
        return (self.category, self.direction, self.severity, self.ma_role,
                self.confidence, self.keyword_override)


def apply_keyword_override(c: Classification) -> Classification:
    raise NotImplementedError(
        "keyword override requires headline text; use "
        "classify_with_keyword_fallback (§11.3 is applied at classification "
        "time, before the §11.2 mapping, and persisted as keyword_override)")


def classify_with_keyword_fallback(
    *,
    ticker: str,
    headline_text: str,
    category: str,
    direction: str,
    severity: str,
    ma_role: str,
    confidence: float,
    published_at: _dt.datetime,
    source: str,
) -> Classification:
    """Build a Classification, applying the §11.3 deterministic keyword
    fallback: NFKC-normalise headline and keyword, casefold both, substring
    test; a match forces severity=CRITICAL and direction=BEARISH regardless
    of LLM output."""
    norm_text = unicodedata.normalize("NFKC", headline_text).casefold()
    hit = any(unicodedata.normalize("NFKC", kw).casefold() in norm_text
              for kw in KEYWORD_FALLBACK_TERMS)
    if hit:
        direction, severity = "BEARISH", "CRITICAL"
    return Classification(
        ticker=ticker, category=category, direction=direction,
        severity=severity, ma_role=ma_role, confidence=confidence,
        published_at=published_at,
        headline_hash=headline_hash(headline_text),
        source=source, keyword_override=hit,
    )


# ---------------------------------------------------------------------------
# P-4 integrity assertion
# ---------------------------------------------------------------------------


def assert_cache_integrity(classifications: Sequence[Classification]) -> None:
    """P-4 / §16 rule 9: all entries sharing (headline_hash, ticker) must
    carry identical effect fields. A mismatch halts the run."""
    seen: dict[tuple[str, str], tuple] = {}
    for c in classifications:
        key = (c.headline_hash, c.ticker)
        if key in seen and seen[key] != c.effect_fields:
            raise NewsCacheIntegrityFailure(
                f"(headline_hash={c.headline_hash}, ticker={c.ticker}) has "
                f"conflicting effect fields across sources",
                {"headline_hash": c.headline_hash, "ticker": c.ticker,
                 "first": seen[key], "second": c.effect_fields},
            )
        seen[key] = c.effect_fields


def dedupe_by_hash(classifications: Sequence[Classification]) -> list[Classification]:
    """Source-independent dedup by (headline_hash, ticker) AFTER the P-4
    integrity assertion — any representative carries identical effects."""
    assert_cache_integrity(classifications)
    seen: set[tuple[str, str]] = set()
    out: list[Classification] = []
    for c in classifications:
        key = (c.headline_hash, c.ticker)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out


# ---------------------------------------------------------------------------
# §11.2 ordered total-function effect mapping
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class MappedEffect:
    branch: str                 # MACRO_OTHER | BEARISH_CRITICAL | MA_TARGET |
                                # BEARISH_HIGH | SCORE | NONE
    score_points: int           # after FP-3 low-confidence zeroing
    g7_veto: bool
    position_action: str        # NONE | EXIT | HOLD | TRIM


def map_effect_branch(c: Classification) -> str:
    """The ordered decision procedure (§11.2 / FP-2), first-match
    exclusivity. Returns the branch key; window tests are separate."""
    if c.category in ("MACRO", "OTHER"):
        return "MACRO_OTHER"
    if c.direction == "BEARISH" and c.severity == "CRITICAL":
        return "BEARISH_CRITICAL"
    if c.category == "M&A" and c.ma_role == "TARGET":
        return "MA_TARGET"
    if c.direction == "BEARISH" and c.severity == "HIGH":
        return "BEARISH_HIGH"
    if (c.direction, c.severity) in (
            ("BEARISH", "MEDIUM"), ("BEARISH", "LOW"),
            ("BULLISH", "HIGH"), ("BULLISH", "CRITICAL"),
            ("BULLISH", "MEDIUM")):
        return "SCORE"
    return "NONE"  # BULLISH LOW / all NEUTRAL


_SCORE_POINTS = {
    ("BEARISH", "MEDIUM"): -10,
    ("BEARISH", "LOW"): -5,
    ("BULLISH", "HIGH"): 10,
    ("BULLISH", "CRITICAL"): 10,
    ("BULLISH", "MEDIUM"): 5,
}


def bearish_critical_d0(published_at: _dt.datetime,
                        official_closes: Mapping[_dt.date, _dt.datetime]
                        ) -> _dt.date | None:
    """E-06: d0 = first exchange trading session whose official close
    timestamp (N-22) is at or after published_at."""
    candidates = [(d, c) for d, c in official_closes.items() if c >= published_at]
    if not candidates:
        return None
    return min(candidates, key=lambda kv: kv[1])[0]


def g7_veto_active(
    c: Classification,
    branch: str,
    t: _dt.datetime,
    *,
    trading_sessions: Sequence[_dt.date],
    official_closes: Mapping[_dt.date, _dt.datetime],
) -> bool:
    """Whether the mapped G7 veto branch is active at scan ``t``.

    - BEARISH_CRITICAL: published_at <= t <= official_close(d4), d4 four
      trading-day indices after d0 (E-06).
    - MA_TARGET: 30 calendar days from published_at.
    - BEARISH_HIGH: 24 hours from published_at.
    """
    if t < c.published_at:
        return False
    if branch == "BEARISH_CRITICAL":
        d0 = bearish_critical_d0(c.published_at, official_closes)
        if d0 is None:
            return False
        sessions = sorted(trading_sessions)
        try:
            d0_idx = sessions.index(d0)
        except ValueError:
            return False
        if d0_idx + 4 >= len(sessions):
            return False
        d4 = sessions[d0_idx + 4]
        close_d4 = official_closes.get(d4)
        if close_d4 is None:
            return False
        return t <= close_d4
    if branch == "MA_TARGET":
        return t <= c.published_at + MA_TARGET_WINDOW
    if branch == "BEARISH_HIGH":
        return t <= c.published_at + BEARISH_HIGH_WINDOW
    return False


def catalyst_score_points(
    classifications: Sequence[Classification],
    t: _dt.datetime,
) -> int:
    """§7.4 catalyst modifier: deterministic sum over source-independent
    distinct headline_hash values whose scoring window contains ``t``,
    clipped to [-10, +10]. FP-3: confidence < 0.85 contributes 0.

    Runs the P-4 integrity assertion first (§16 rule 9).
    """
    deduped = dedupe_by_hash(classifications)
    total = 0
    for c in deduped:
        if not (c.published_at <= t <= c.published_at + SCORING_WINDOW):
            continue
        if map_effect_branch(c) != "SCORE":
            continue
        if c.confidence < CONFIDENCE_THRESHOLD:
            continue  # FP-3: score contribution forced to 0
        total += _SCORE_POINTS.get((c.direction, c.severity), 0)
    return max(-10, min(10, total))


def g7_vetoed_at(
    classifications: Sequence[Classification],
    t: _dt.datetime,
    *,
    trading_sessions: Sequence[_dt.date],
    official_closes: Mapping[_dt.date, _dt.datetime],
) -> bool:
    """§7.2 G7: any mapped veto branch active for the ticker at ``t``.

    confidence < 0.85 does NOT suppress vetoes (FP-3)."""
    for c in dedupe_by_hash(classifications):
        branch = map_effect_branch(c)
        if branch in ("BEARISH_CRITICAL", "MA_TARGET", "BEARISH_HIGH") and \
                g7_veto_active(c, branch, t,
                               trading_sessions=trading_sessions,
                               official_closes=official_closes):
            return True
    return False


def confirmed_bearish_critical_exit(
    classifications: Sequence[Classification],
    t: _dt.datetime,
    *,
    trading_sessions: Sequence[_dt.date],
    official_closes: Mapping[_dt.date, _dt.datetime],
) -> bool:
    """§11.3 two-source rule: a CRITICAL EXIT advisory requires two
    classifications that each reach the BEARISH-CRITICAL branch with
    distinct headline_hash AND distinct source, both windows overlapping
    ``t``. In backtest only confirmed CRITICALs trigger the §8.6
    priority-3 exit. confidence < 0.85 retains participation (FP-3)."""
    qualifying: set[tuple[str, str]] = set()
    for c in dedupe_by_hash(classifications):
        if map_effect_branch(c) != "BEARISH_CRITICAL":
            continue
        if not g7_veto_active(c, "BEARISH_CRITICAL", t,
                              trading_sessions=trading_sessions,
                              official_closes=official_closes):
            continue
        qualifying.add((c.headline_hash, c.source))
    hashes = {h for h, _ in qualifying}
    sources = {s for _, s in qualifying}
    return len(hashes) >= 2 and len(sources) >= 2


# ---------------------------------------------------------------------------
# NEWS_UNVERIFIED trigger sets (RP-02 + FP-3/FP-5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RawHeadline:
    """Timed raw headline from the news_headlines inventory (FP-5)."""

    ticker: str
    headline_hash: str
    source: str
    published_at: _dt.datetime


def is_news_unverified(
    *,
    ticker: str,
    t: _dt.datetime,
    classifications: Sequence[Classification],
    raw_headlines: Sequence[RawHeadline],
    covered: bool,
    schema_version: str,
    model_version: str,
    trading_sessions: Sequence[_dt.date],
    official_closes: Mapping[_dt.date, _dt.datetime],
) -> bool:
    """§11.2 backtest trigger sets for ticker NEWS_UNVERIFIED at scan ``t``:

    (a) any cached timed classification with confidence < 0.85 whose
        universal 24-hour trigger window OR an applicable mapped G7
        activation window overlaps ``t``;
    (b) a timed headline inside a run-pinned verified NEWS covered span
        with no cache entry, inside the universal 24-hour trigger window.

    Never NEWS_UNVERIFIED: untimed headlines (dropped upstream),
    future-dated-at-t headlines, headlines outside both windows, and
    coverage gaps (§11.6 neutral-disable).
    """
    deduped = dedupe_by_hash([c for c in classifications if c.ticker == ticker])
    for c in deduped:
        if c.published_at > t:
            continue
        if c.confidence < CONFIDENCE_THRESHOLD:
            in_universal = (t - c.published_at) <= NEWS_UNVERIFIED_TRIGGER_WINDOW
            if in_universal:
                return True
            branch = map_effect_branch(c)
            if branch in ("BEARISH_CRITICAL", "MA_TARGET", "BEARISH_HIGH") and \
                    g7_veto_active(c, branch, t,
                                   trading_sessions=trading_sessions,
                                   official_closes=official_closes):
                return True
    if covered:
        cached_keys = {(c.headline_hash, c.source) for c in classifications
                       if c.ticker == ticker}
        for h in raw_headlines:
            if h.ticker != ticker or h.published_at > t:
                continue
            if (t - h.published_at) > NEWS_UNVERIFIED_TRIGGER_WINDOW:
                continue
            if (h.headline_hash, h.source) not in cached_keys:
                return True  # cache miss inside a covered span (RP-02)
    return False
