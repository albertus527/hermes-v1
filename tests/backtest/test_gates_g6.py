"""§7.2 G6 earnings-session mapping (P-A-02 + I-1) tests."""

import datetime as dt

from trading_core.gates import EarningsEvent, gate_g6, map_earnings_event_session

# Fake calendar: Mon-Fri trading days on the week of 2026-01-05..09
SESSIONS = [dt.date(2026, 1, 5) + dt.timedelta(days=i) for i in range(5)]


def _is_trading(d):
    return d in SESSIONS


def _next_trading(d):
    for s in SESSIONS:
        if s > d:
            return s
    raise ValueError(d)


def _gate(decision, events, **kw):
    defaults = dict(
        ticker="T", asset_class="stock", decision_date=decision,
        trading_sessions=SESSIONS, events=events, coverage_enabled=True,
        manifest_version="m1", is_trading_day=_is_trading,
        next_trading_day=_next_trading)
    defaults.update(kw)
    return gate_g6(**defaults)


class TestEventSessionMapping:
    def test_before_market_open_maps_to_event_day(self):
        e = EarningsEvent(dt.date(2026, 1, 6), "before-market-open")
        assert map_earnings_event_session(
            e, is_trading_day=_is_trading, next_trading_day=_next_trading) == \
            dt.date(2026, 1, 6)

    def test_after_market_close_maps_to_next_trading_day(self):
        e = EarningsEvent(dt.date(2026, 1, 6), "after-market-close")
        assert map_earnings_event_session(
            e, is_trading_day=_is_trading, next_trading_day=_next_trading) == \
            dt.date(2026, 1, 7)

    def test_unspecified_maps_to_event_day(self):
        e = EarningsEvent(dt.date(2026, 1, 6), "unspecified")
        assert map_earnings_event_session(
            e, is_trading_day=_is_trading, next_trading_day=_next_trading) == \
            dt.date(2026, 1, 6)

    def test_i1_non_trading_date_fallback(self):
        """I-1: a before-market-open/unspecified event dated on a weekend
        maps to the NEXT trading day."""
        sat = dt.date(2026, 1, 10)  # Saturday
        sessions = SESSIONS + [dt.date(2026, 1, 12)]
        is_td = lambda d: d in sessions
        ntd = lambda d: min(s for s in sessions if s > d)
        e = EarningsEvent(sat, "before-market-open")
        assert map_earnings_event_session(
            e, is_trading_day=is_td, next_trading_day=ntd) == dt.date(2026, 1, 12)
        e2 = EarningsEvent(sat, "unspecified")
        assert map_earnings_event_session(
            e2, is_trading_day=is_td, next_trading_day=ntd) == dt.date(2026, 1, 12)


class TestG6Blackout:
    def test_blackout_T_T1_T2(self):
        # Event d(e) = Wed 1/7; decisions Mon/Tue/Wed fail, Thu passes
        e = EarningsEvent(dt.date(2026, 1, 7), "before-market-open")
        assert not _gate(dt.date(2026, 1, 5), [e]).passed   # T+2 in blackout
        assert not _gate(dt.date(2026, 1, 6), [e]).passed   # T+1
        assert not _gate(dt.date(2026, 1, 7), [e]).passed   # T
        assert _gate(dt.date(2026, 1, 8), [e]).passed       # T-1: outside

    def test_etf_passes(self):
        e = EarningsEvent(dt.date(2026, 1, 5), "before-market-open")
        r = _gate(dt.date(2026, 1, 5), [e], asset_class="etf")
        assert r.passed and r.reason_code == "G6_NA_ETF"

    def test_coverage_gap_passes_with_disabled_code(self):
        e = EarningsEvent(dt.date(2026, 1, 5), "before-market-open")
        r = _gate(dt.date(2026, 1, 5), [e], coverage_enabled=False)
        assert r.passed and r.reason_code == "G6_DISABLED_COVERAGE"

    def test_mapped_session_logged_in_inputs(self):
        sat = dt.date(2026, 1, 10)
        sessions = SESSIONS + [dt.date(2026, 1, 12)]
        r = gate_g6(
            ticker="T", asset_class="stock",
            decision_date=dt.date(2026, 1, 9), trading_sessions=sessions,
            events=[EarningsEvent(sat, "unspecified")],
            coverage_enabled=True, manifest_version="m9",
            is_trading_day=lambda d: d in sessions,
            next_trading_day=lambda d: min(s for s in sessions if s > d))
        mapped = r.inputs_json["events"][0]
        assert mapped["mapped_session"] == "2026-01-12"
        assert mapped["earnings_manifest_version"] == "m9"
        assert not r.passed  # d(e)=Mon 1/12 is T+1 for Fri 1/9
