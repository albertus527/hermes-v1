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
        help="R2.7 canonical backtest (Phase 0/1 scaffolding)",
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

    parser.set_defaults(func=cmd_backtest)
