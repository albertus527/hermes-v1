"""Exchange-calendar abstraction tests (§3.1) + backtest DB schema smoke."""

import datetime as dt

import pytest

pmc = pytest.importorskip(
    "pandas_market_calendars", reason="pandas_market_calendars not installed")

from backtest import calendar as cal


class TestNYSECalendar:
    def test_weekday_trading_day(self):
        assert cal.is_trading_day(dt.date(2026, 1, 5))   # Monday

    def test_weekend_not_trading(self):
        assert not cal.is_trading_day(dt.date(2026, 1, 3))  # Saturday

    def test_new_years_day_holiday(self):
        assert not cal.is_trading_day(dt.date(2026, 1, 1))

    def test_thanksgiving_half_day_friday(self):
        # 2025-11-28 (day after Thanksgiving) is a 13:00 half-day
        assert cal.is_trading_day(dt.date(2025, 11, 28))
        assert cal.is_half_day(dt.date(2025, 11, 28))
        assert not cal.is_half_day(dt.date(2026, 1, 5))

    def test_next_prev_trading_day(self):
        assert cal.next_trading_day(dt.date(2026, 1, 2)) == dt.date(2026, 1, 5)
        assert cal.prev_trading_day(dt.date(2026, 1, 5)) == dt.date(2026, 1, 2)

    def test_session_close_tz_aware(self):
        close = cal.session_close(dt.date(2026, 1, 5))
        assert close is not None and close.tzinfo is not None
        assert (close.hour, close.minute) == (16, 0)

    def test_trading_sessions_range(self):
        days = cal.trading_sessions(dt.date(2026, 1, 5), dt.date(2026, 1, 9))
        assert len(days) == 5


class TestBacktestDBSchema:
    def test_schema_initializes(self, tmp_path):
        from backtest.db.schema import open_db
        conn = open_db(tmp_path / "bt.sqlite3")
        try:
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            required = {
                "run_history", "bars", "corp_actions", "news_headlines",
                "coverage_manifests", "regime_snapshots",
                "indicator_snapshots", "gate_results", "scores",
                "news_classifications", "recommendations", "sim_trades",
                "simulation_events", "fee_calculations",
            }
            assert required <= tables
            version = conn.execute(
                "SELECT value FROM schema_meta WHERE key='schema_version'"
            ).fetchone()
            assert version and version[0]
        finally:
            conn.close()

    def test_run_role_check_constraint(self, tmp_path):
        import sqlite3
        from backtest.db.schema import open_db
        conn = open_db(tmp_path / "bt.sqlite3")
        try:
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO run_history(run_id, run_role, config_version,"
                    " code_commit, started_at) VALUES('r1','BOGUS',1,'abc','t')")
        finally:
            conn.close()
