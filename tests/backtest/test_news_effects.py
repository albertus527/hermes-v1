"""News determinism: FP-4 headline-hash normalization vectors, §11.2 ordered
total-function mapping (incl. first-match exclusivity), FP-3 low-confidence
behavior, §11.3 two-source rule + keyword override, P-4 cache integrity.
"""

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from trading_core.errors import NewsCacheIntegrityFailure
from trading_core.news_effects import (
    RawHeadline,
    assert_cache_integrity,
    catalyst_score_points,
    classify_with_keyword_fallback,
    confirmed_bearish_critical_exit,
    dedupe_by_hash,
    g7_vetoed_at,
    headline_hash,
    is_news_unverified,
    map_effect_branch,
    normalize_headline_text,
)

ET = ZoneInfo("America/New_York")
PUB = dt.datetime(2026, 1, 5, 9, 0, tzinfo=ET)      # Monday 09:00 ET
SCAN_10 = dt.datetime(2026, 1, 5, 10, 0, tzinfo=ET)

# trading sessions Mon 1/5 .. Fri 1/9 with 16:00 ET official closes
SESSIONS = [dt.date(2026, 1, 5) + dt.timedelta(days=i) for i in range(5)]
CLOSES = {d: dt.datetime(d.year, d.month, d.day, 16, 0, tzinfo=ET)
          for d in SESSIONS}


def _cls(**kw):
    defaults = dict(ticker="T", category="PRODUCT", direction="BULLISH",
                    severity="MEDIUM", ma_role="NEITHER", confidence=0.95,
                    published_at=PUB, headline_hash="h1", source="finnhub")
    defaults.update(kw)
    from trading_core.news_effects import Classification
    return Classification(**defaults)


class TestHashNormalization:
    def test_nfkc_casefold_whitespace_punct(self):
        # full-width chars (NFKC), mixed case, whitespace runs, edge punct
        a = "ＡＢＣ  Corp   beats…!"
        b = "abc corp beats…"
        assert headline_hash(a) == headline_hash(b)

    def test_source_independent(self):
        assert headline_hash("Hello world") == headline_hash("Hello world")

    def test_distinct_texts_differ(self):
        assert headline_hash("A") != headline_hash("B")

    def test_normalization_strips_edge_punctuation(self):
        assert normalize_headline_text('"...Tesla recalls!"') == \
            "tesla recalls"


class TestOrderedMapping:
    def test_macro_precedence_over_bearish_critical(self):
        """First-match exclusivity: BEARISH-CRITICAL with category MACRO is
        consumed by step 1 — no veto, no exit, 0 points."""
        c = _cls(category="MACRO", direction="BEARISH", severity="CRITICAL")
        assert map_effect_branch(c) == "MACRO_OTHER"
        assert not g7_vetoed_at([c], SCAN_10, trading_sessions=SESSIONS,
                                official_closes=CLOSES)

    def test_bearish_critical_beats_ma_target(self):
        c = _cls(category="M&A", ma_role="TARGET", direction="BEARISH",
                 severity="CRITICAL")
        assert map_effect_branch(c) == "BEARISH_CRITICAL"

    def test_ma_target_branch(self):
        c = _cls(category="M&A", ma_role="TARGET", direction="NEUTRAL",
                 severity="LOW")
        assert map_effect_branch(c) == "MA_TARGET"
        assert g7_vetoed_at([c], SCAN_10, trading_sessions=SESSIONS,
                            official_closes=CLOSES)
        # 30-calendar-day window: still vetoed on day 20 (outside scoring)
        t20 = PUB + dt.timedelta(days=20)
        assert g7_vetoed_at([c], t20, trading_sessions=SESSIONS,
                            official_closes=CLOSES)
        assert catalyst_score_points([c], t20) == 0

    def test_score_points(self):
        bull = _cls(direction="BULLISH", severity="HIGH")
        bear = _cls(direction="BEARISH", severity="MEDIUM",
                    headline_hash="h2")
        assert catalyst_score_points([bull], SCAN_10) == 10
        assert catalyst_score_points([bull, bear], SCAN_10) == 0  # 10 - 10

    def test_score_clip(self):
        bulls = [_cls(direction="BULLISH", severity="HIGH",
                      headline_hash=f"h{i}") for i in range(3)]
        assert catalyst_score_points(bulls, SCAN_10) == 10  # clipped

    def test_outside_scoring_window_contributes_zero(self):
        bull = _cls(direction="BULLISH", severity="HIGH")
        t2 = PUB + dt.timedelta(hours=25)
        assert catalyst_score_points([bull], t2) == 0

    def test_bearish_critical_window_through_d4_close(self):
        """E-06: d0 = first session whose official close >= published_at;
        window active through official_close(d4) (5th counted session)."""
        c = _cls(direction="BEARISH", severity="CRITICAL")
        d4_close = CLOSES[SESSIONS[4]]
        assert g7_vetoed_at([c], d4_close, trading_sessions=SESSIONS,
                            official_closes=CLOSES)
        assert not g7_vetoed_at([c], d4_close + dt.timedelta(minutes=1),
                                trading_sessions=SESSIONS, official_closes=CLOSES)


class TestLowConfidence:
    def test_zero_score_but_veto_retained_and_unverified(self):
        c = _cls(direction="BULLISH", severity="HIGH", confidence=0.50)
        assert catalyst_score_points([c], SCAN_10) == 0  # FP-3 zero score
        v = _cls(direction="BEARISH", severity="CRITICAL", confidence=0.50)
        assert g7_vetoed_at([v], SCAN_10, trading_sessions=SESSIONS,
                            official_closes=CLOSES)  # veto retained
        assert is_news_unverified(
            ticker="T", t=SCAN_10, classifications=[v], raw_headlines=[],
            covered=True, schema_version="news_schema_v3", model_version="",
            trading_sessions=SESSIONS, official_closes=CLOSES)

    def test_universal_24h_trigger_for_macro_other(self):
        c = _cls(category="MACRO", confidence=0.10)
        assert is_news_unverified(
            ticker="T", t=SCAN_10, classifications=[c], raw_headlines=[],
            covered=True, schema_version="news_schema_v3", model_version="",
            trading_sessions=SESSIONS, official_closes=CLOSES)
        # outside 24h and no mapped G7 window -> not unverified
        t2 = PUB + dt.timedelta(hours=25)
        assert not is_news_unverified(
            ticker="T", t=t2, classifications=[c], raw_headlines=[],
            covered=True, schema_version="news_schema_v3", model_version="",
            trading_sessions=SESSIONS, official_closes=CLOSES)

    def test_future_dated_never_unverified(self):
        c = _cls(confidence=0.10,
                 published_at=SCAN_10 + dt.timedelta(hours=1))
        assert not is_news_unverified(
            ticker="T", t=SCAN_10, classifications=[c], raw_headlines=[],
            covered=True, schema_version="news_schema_v3", model_version="",
            trading_sessions=SESSIONS, official_closes=CLOSES)

    def test_cache_miss_inside_covered_span_triggers(self):
        h = RawHeadline(ticker="T", headline_hash="hx", source="finnhub",
                        published_at=PUB)
        assert is_news_unverified(
            ticker="T", t=SCAN_10, classifications=[], raw_headlines=[h],
            covered=True, schema_version="news_schema_v3", model_version="",
            trading_sessions=SESSIONS, official_closes=CLOSES)

    def test_coverage_gap_never_unverified(self):
        h = RawHeadline(ticker="T", headline_hash="hx", source="finnhub",
                        published_at=PUB)
        assert not is_news_unverified(
            ticker="T", t=SCAN_10, classifications=[], raw_headlines=[h],
            covered=False, schema_version="news_schema_v3", model_version="",
            trading_sessions=SESSIONS, official_closes=CLOSES)


class TestTwoSourceRule:
    def test_confirmed_requires_distinct_hash_and_source(self):
        base = dict(direction="BEARISH", severity="CRITICAL")
        a = _cls(headline_hash="h1", source="finnhub", **base)
        same_diff_src = _cls(headline_hash="h1", source="rss", **base)
        diff_same_src = _cls(headline_hash="h2", source="finnhub", **base)
        two = _cls(headline_hash="h2", source="rss", **base)
        assert not confirmed_bearish_critical_exit(
            [a, same_diff_src], SCAN_10,
            trading_sessions=SESSIONS, official_closes=CLOSES)
        assert not confirmed_bearish_critical_exit(
            [a, diff_same_src], SCAN_10,
            trading_sessions=SESSIONS, official_closes=CLOSES)
        assert confirmed_bearish_critical_exit(
            [a, two], SCAN_10,
            trading_sessions=SESSIONS, official_closes=CLOSES)


class TestKeywordOverride:
    def test_forces_bearish_critical_and_persists(self):
        c = classify_with_keyword_fallback(
            ticker="T", headline_text="Company under SEC investigation",
            category="PRODUCT", direction="BULLISH", severity="LOW",
            ma_role="NEITHER", confidence=0.99, published_at=PUB,
            source="finnhub")
        assert c.keyword_override
        assert c.direction == "BEARISH" and c.severity == "CRITICAL"
        assert map_effect_branch(c) == "BEARISH_CRITICAL"

    def test_case_insensitive_nfkc_substring(self):
        c = classify_with_keyword_fallback(
            ticker="T", headline_text="Firm announces RESTATEMENT of results",
            category="EARNINGS", direction="NEUTRAL", severity="MEDIUM",
            ma_role="NEITHER", confidence=0.99, published_at=PUB,
            source="finnhub")
        assert c.keyword_override


class TestP4Integrity:
    def test_same_hash_ticker_mismatch_halts(self):
        a = _cls(headline_hash="h1", source="finnhub")
        b = _cls(headline_hash="h1", source="rss", severity="LOW")
        with pytest.raises(NewsCacheIntegrityFailure):
            assert_cache_integrity([a, b])

    def test_same_hash_identical_effects_dedupes(self):
        a = _cls(headline_hash="h1", source="finnhub")
        b = _cls(headline_hash="h1", source="rss")
        deduped = dedupe_by_hash([a, b])
        assert len(deduped) == 1
