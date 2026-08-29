"""R2.7 canonical backtest package (backtest-only adapters + storage).

Lives beside the shared deterministic core (trading_core/). Per the R2.7
implementation plan, the backtest store is a SEPARATE SQLite database
(raw sqlite3, no SQLAlchemy) under $HERMES_HOME/backtest/ — hermes_state.py
SessionDB is generic agent infrastructure and is not extended.

Nothing in this package changes existing generic Hermes behavior; the only
core touchpoints are the `hermes backtest` subcommand wiring and the
config/secret registrations.
"""
