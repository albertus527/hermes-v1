"""R2.7 §16 backtest SQLite schema (raw sqlite3; separate from SessionDB).

Append-only tables. Every row carries run_id, config_version, code_commit;
decision-bearing rows additionally carry universe_version. Journal mode
follows the database.journal_mode / backtest.journal_mode config.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 2

DDL = """
CREATE TABLE IF NOT EXISTS run_history (
    run_id TEXT NOT NULL,
    run_role TEXT NOT NULL CHECK (run_role IN ('TRAIN','TEST','LIVE')),
    config_version INTEGER NOT NULL,
    code_commit TEXT NOT NULL,
    started_at TEXT NOT NULL,
    coverage_manifest_versions TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'RUNNING',
    exception_json TEXT
);
CREATE TABLE IF NOT EXISTS bars (
    ticker TEXT NOT NULL, ts_label_start TEXT NOT NULL,
    o TEXT NOT NULL, h TEXT NOT NULL, l TEXT NOT NULL, c TEXT NOT NULL,
    v TEXT NOT NULL,
    feed TEXT NOT NULL, timeframe TEXT NOT NULL, adjustment TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS corp_actions (
    ticker TEXT NOT NULL, event_type TEXT NOT NULL, ex_date TEXT NOT NULL,
    split_ratio TEXT, cash_amount_per_share TEXT,
    record_date TEXT, pay_date TEXT,
    corp_actions_version TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS news_headlines (
    headline_hash TEXT NOT NULL, source TEXT NOT NULL, ticker TEXT NOT NULL,
    published_at TEXT, headline_text_normalized TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS coverage_manifests (
    source_kind TEXT NOT NULL
        CHECK (source_kind IN ('NEWS','EARNINGS','CORP_ACTIONS','FEE_SCHEDULE')),
    ticker TEXT NOT NULL, span_start TEXT NOT NULL, span_end TEXT NOT NULL,
    verified INTEGER NOT NULL, manifest_version TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS regime_snapshots (
    date TEXT NOT NULL, trend_score INTEGER, vix REAL, vix_date_used TEXT,
    regime TEXT, multiplier REAL, opening_return_spy REAL,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS indicator_snapshots (
    ticker TEXT NOT NULL, ts TEXT NOT NULL,
    ema20 REAL, ema50 REAL, ema200 REAL, atr14 REAL, rsi14 REAL,
    vwap TEXT, vol_metrics TEXT,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS gate_results (
    candidate_id TEXT NOT NULL, gate_id TEXT NOT NULL, pass INTEGER NOT NULL,
    inputs_json TEXT NOT NULL, stage TEXT NOT NULL,
    universe_version TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS scores (
    candidate_id TEXT NOT NULL, component_points_json TEXT NOT NULL,
    raw_total TEXT NOT NULL, final_total_int INTEGER NOT NULL,
    rank INTEGER, tiebreak_applied INTEGER NOT NULL DEFAULT 0,
    cutoff INTEGER NOT NULL, regime TEXT NOT NULL,
    universe_version TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS news_classifications (
    headline_hash TEXT NOT NULL, ticker TEXT NOT NULL, source TEXT NOT NULL,
    ma_role TEXT NOT NULL, keyword_override INTEGER NOT NULL DEFAULT 0,
    json_payload TEXT NOT NULL,
    model_version TEXT NOT NULL, schema_version TEXT NOT NULL,
    classified_at_wallclock TEXT, published_at TEXT,
    activation_start TEXT, activation_end TEXT,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS recommendations (
    id TEXT NOT NULL, ts TEXT NOT NULL, type TEXT NOT NULL, ticker TEXT,
    notional TEXT, shares_est TEXT, stop TEXT, target TEXT,
    realised_risk TEXT, fees_rt_screening TEXT, gateA_burden TEXT,
    template_hash TEXT, universe_version TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS sim_trades (
    trade_id TEXT NOT NULL, recommendation_id TEXT,
    entry_scenario TEXT, exit_scenario TEXT,
    entry_fill_ts TEXT, entry_fill_price TEXT, shares_filled TEXT,
    notional_actual TEXT,
    exit_detection_ts TEXT, exit_fill_ts TEXT, exit_fill_price TEXT,
    exit_reason TEXT, dividends_net TEXT,
    realised_risk_actual TEXT, stop_overshoot TEXT, risk_divergence TEXT,
    flags TEXT, train_fee_substitutions_json TEXT,
    fee_schedule_version TEXT, corp_actions_version TEXT,
    coverage_manifest_versions TEXT,
    fee_rounding TEXT, vat_on_regulatory INTEGER, slippage_bps INTEGER,
    config_version INTEGER NOT NULL DEFAULT 0, universe_version TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '', code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS simulation_events (
    event_id TEXT NOT NULL, candidate_id TEXT, trade_id TEXT,
    event_ts TEXT NOT NULL, event_code TEXT NOT NULL, details_json TEXT,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS fee_calculations (
    fee_calc_id TEXT NOT NULL, candidate_id TEXT, trade_id TEXT,
    side TEXT NOT NULL, component TEXT NOT NULL, fee_context TEXT NOT NULL,
    fee_computation_date TEXT NOT NULL,
    base_amount TEXT NOT NULL, vat_amount TEXT NOT NULL,
    rounded_amount TEXT NOT NULL, rounding_branch TEXT NOT NULL,
    fee_schedule_version TEXT NOT NULL, fee_input_status TEXT NOT NULL,
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS vix_observations (
    -- FRED VIXCLS daily close (§3.1, §5.2). observation_date is the
    -- series date (already a trading-day close); value is the raw close.
    -- '.'-encoded FRED missing values are NOT stored.
    observation_date TEXT NOT NULL PRIMARY KEY,
    value TEXT NOT NULL,
    series_id TEXT NOT NULL DEFAULT 'VIXCLS',
    run_id TEXT NOT NULL DEFAULT '', config_version INTEGER NOT NULL DEFAULT 0,
    code_commit TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS schema_meta (key TEXT PRIMARY KEY, value TEXT);
CREATE INDEX IF NOT EXISTS idx_bars_ticker_ts ON bars(ticker, ts_label_start);
CREATE INDEX IF NOT EXISTS idx_gate_candidate ON gate_results(candidate_id);
CREATE INDEX IF NOT EXISTS idx_events_code ON simulation_events(event_code);
CREATE INDEX IF NOT EXISTS idx_fee_trade ON fee_calculations(trade_id);
"""


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(DDL)
    conn.execute(
        "INSERT OR REPLACE INTO schema_meta(key, value) VALUES('schema_version', ?)",
        (str(SCHEMA_VERSION),))
    conn.commit()


def open_db(path: str | Path, *, journal_mode: str = "wal") -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path))
    conn.execute(f"PRAGMA journal_mode={journal_mode}")
    init_db(conn)
    return conn
