"""``hermes backtest`` subcommand parser (R2.7 Phase 0).

Modeled on hermes_cli/subcommands/cron.py: the handler is injected so this
module does not import main (cycle avoidance). Only Phase-0 verbs exist;
data-fetch and later-phase verbs arrive with their phases.
"""

from __future__ import annotations

from typing import Callable


def build_backtest_parser(subparsers, *, cmd_backtest: Callable) -> None:
    """Attach the ``backtest`` subcommand (and its sub-actions)."""
    parser = subparsers.add_parser(
        "backtest",
        help="R2.7 canonical backtest (Phase 0/1/2 scaffolding)",
        description=(
            "R2.7 canonical backtest infrastructure. The deterministic core "
            "lives in trading_core/ and is shared live/backtest; this "
            "subcommand manages backtest storage, versioned artifacts, and "
            "Phase-gated jobs."
        ),
    )
    subs = parser.add_subparsers(dest="backtest_command")

    p_init = subs.add_parser(
        "init", help="Initialize the backtest store and seed artifacts")
    p_init.set_defaults(backtest_handler="init")

    p_smoke = subs.add_parser(
        "fee-smoke",
        help="Run the §1.4 six-branch fee smoke test (Phase-0 gate)")
    p_smoke.set_defaults(backtest_handler="fee-smoke")

    p_populate = subs.add_parser(
        "populate-news-cache",
        help="§20 Phase 2: classify all timed headlines in verified NEWS "
             "covered spans into the cache (the only authorized bulk "
             "live-LLM job; NOT a backtest)")
    p_populate.add_argument("--manifest-version", required=True,
                            help="run-pinned NEWS coverage manifest version (§11.6)")
    p_populate.add_argument("--llm-config-version", default="",
                            help="llm_config_version provenance label (§11.5)")
    p_populate.add_argument("--run-id", default="news-cache-population",
                            help="run_id stamped on cache rows")
    p_populate.set_defaults(backtest_handler="populate-news-cache")

    p_report = subs.add_parser(
        "news-cache-report",
        help="§20 Phase 2: cache-completeness + P-4 integrity report "
             "(deterministic reads over an existing cache)")
    p_report.add_argument("--manifest-version", required=True,
                          help="run-pinned NEWS coverage manifest version (§11.6)")
    p_report.set_defaults(backtest_handler="news-cache-report")

    p_calibrate = subs.add_parser(
        "calibrate-news",
        help="§11.4: evaluate the news classifier against a labeled set "
             "(framework runner; never claims PASS)")
    p_calibrate.add_argument("--labeled-set", default=None,
                             help="path to the labeled-headlines JSON set")
    p_calibrate.set_defaults(backtest_handler="calibrate-news")

    # -- Phase-0 data-ingestion jobs (§20 Phase 0) -------------------------

    p_bars = subs.add_parser(
        "fetch-alpaca-bars",
        help="Alpaca historical bars (SIP feed asserted; daily or 1-min; "
             "split=signal / raw=executable adjustment)")
    p_bars.add_argument("--tickers", required=True,
                        help="comma-separated ticker list")
    p_bars.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_bars.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    p_bars.add_argument("--timeframe", default="1Min",
                        choices=["1Min", "1Day"])
    p_bars.add_argument("--adjustment", default="split",
                        choices=["split", "raw"])
    p_bars.add_argument("--run-id", default="fetch-alpaca-bars")
    p_bars.set_defaults(backtest_handler="fetch-alpaca-bars")

    p_ca = subs.add_parser(
        "fetch-corp-actions",
        help="Alpaca Corporate Actions (§3.6 designated provider) + verified "
             "CORP_ACTIONS coverage-manifest attestations")
    p_ca.add_argument("--tickers", required=True,
                      help="comma-separated ticker list")
    p_ca.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_ca.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    p_ca.add_argument("--corp-actions-version", default="alpaca-ca-1")
    p_ca.add_argument("--manifest-version", default=None,
                      help="coverage manifest version (defaults to "
                           "corp-actions-version)")
    p_ca.add_argument("--run-id", default="fetch-corp-actions")
    p_ca.set_defaults(backtest_handler="fetch-corp-actions")

    p_news = subs.add_parser(
        "fetch-finnhub-news",
        help="Finnhub raw headline inventory + verified NEWS covered-span "
             "manifests (FP-5)")
    p_news.add_argument("--tickers", required=True,
                        help="comma-separated ticker list")
    p_news.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_news.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    p_news.add_argument("--manifest-version", default="finnhub-news-1")
    p_news.add_argument("--run-id", default="fetch-finnhub-news")
    p_news.set_defaults(backtest_handler="fetch-finnhub-news")

    p_earn = subs.add_parser(
        "fetch-finnhub-earnings",
        help="Finnhub earnings calendar (G6 inputs) + verified EARNINGS "
             "covered-span manifests")
    p_earn.add_argument("--tickers", required=True,
                        help="comma-separated ticker list")
    p_earn.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_earn.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    p_earn.add_argument("--manifest-version", default="finnhub-earnings-1")
    p_earn.add_argument("--run-id", default="fetch-finnhub-earnings")
    p_earn.set_defaults(backtest_handler="fetch-finnhub-earnings")

    p_eodhd_earn = subs.add_parser(
        "fetch-eodhd-earnings",
        help="EODHD historical earnings calendar (§3.7 substitutable G6 "
             "inputs) + verified EARNINGS covered-span manifests")
    p_eodhd_earn.add_argument("--tickers", required=True,
                              help="comma-separated ticker list")
    p_eodhd_earn.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_eodhd_earn.add_argument("--end", required=True,
                              help="YYYY-MM-DD (inclusive)")
    p_eodhd_earn.add_argument("--manifest-version",
                              default="eodhd-earnings-1")
    p_eodhd_earn.add_argument("--run-id", default="fetch-eodhd-earnings")
    p_eodhd_earn.set_defaults(backtest_handler="fetch-eodhd-earnings")

    p_av_news = subs.add_parser(
        "fetch-alphavantage-news",
        help="Alpha Vantage historical NEWS_SENTIMENT (§3.8 substitutable "
             "historical news source) + verified NEWS covered-span "
             "manifests")
    p_av_news.add_argument("--tickers", required=True,
                           help="comma-separated ticker list")
    p_av_news.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_av_news.add_argument("--end", required=True,
                           help="YYYY-MM-DD (inclusive)")
    p_av_news.add_argument("--manifest-version",
                           default="alphavantage-news-1")
    p_av_news.add_argument("--run-id", default="fetch-alphavantage-news")
    p_av_news.set_defaults(backtest_handler="fetch-alphavantage-news")

    p_vix = subs.add_parser(
        "fetch-fred-vix",
        help="FRED VIXCLS daily closes (§5.2 volatility state input)")
    p_vix.add_argument("--start", required=True, help="YYYY-MM-DD")
    p_vix.add_argument("--end", required=True, help="YYYY-MM-DD (inclusive)")
    p_vix.add_argument("--run-id", default="fetch-fred-vix")
    p_vix.set_defaults(backtest_handler="fetch-fred-vix")

    parser.set_defaults(func=cmd_backtest)
