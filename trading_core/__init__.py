"""R2.7 shared deterministic trading core (live + backtest).

Implements docs/specs/HERMES_FABLE_R2_7_CANONICAL_BACKTEST_SPECIFICATION.md
(the normative source of truth; this package never redefines policy).

Rules that bind every module in this package:

- Pure functions over point-in-time snapshots (spec §14.2). Decision
  timestamps ``t`` are explicit parameters; NOTHING here reads the wall
  clock (§21 item 27), so ``hermes_time``/``time.time`` must never appear.
- No LLM calls, no network I/O, no Telegram/gateway logic, and no imports
  from ``run_agent.py``, ``cli.py``, ``gateway/``, ``tools/``, or any
  other Hermes-core module.
- Live and backtest share THIS single implementation of gates, scoring,
  sizing, fees, stops, indicators, regime, and exits (§6, §14.2).
- Fee, sizing, and level arithmetic uses ``decimal.Decimal`` so that the
  §1.4 smoke-test targets and every gate decision are exact.
"""

from trading_core.errors import (
    DeterministicEngineHalt,
    FeedParityViolation,
    NewsCacheIntegrityFailure,
    TrainFeeSubstitutionViolation,
)

__all__ = [
    "DeterministicEngineHalt",
    "FeedParityViolation",
    "NewsCacheIntegrityFailure",
    "TrainFeeSubstitutionViolation",
]
