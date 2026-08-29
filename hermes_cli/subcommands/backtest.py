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

    parser.set_defaults(func=cmd_backtest)
