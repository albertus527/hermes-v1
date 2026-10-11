"""R2.8.1 §11.4 human calibration reviewer — offline, loopback-only.

A self-contained, minimal local web application for one authorized human
reviewer to independently label historical news headlines for the §11.4
calibration requirement.

Design boundaries (this batch):

* READ-ONLY against every production artifact. The published news cache
  (``~/.hermes/backtest/backtest.sqlite3``) is opened only in SQLite
  read-only URI mode; the source calibration CSVs are opened ``rb`` only.
* Reviewer state lives in a NEW SQLite database under
  ``~/.hermes/backtest/calibration/reviewer/`` and never touches the
  backtest store, existing classification tables, or consensus CSVs.
* Strict server-side blind review: no prior consensus label, notes, model
  prediction, confidence, or selection cue is ever serialized to the
  browser before a sample is explicitly locked.
* No outbound network calls, no CDN assets, no LLM/API calls, no persistent
  service, no public bind.

This package deliberately does NOT modify §11.2 trading-effect mapping,
G7 windows, confidence semantics, or keyword fallback. It produces a
separate, auditable human-review record only.
"""

from __future__ import annotations

__all__ = [
    "REVIEWER_SCHEMA_VERSION",
    "RUBRIC_VERSION",
]

# Reviewer-DB schema version (independent of news_schema_v3).
REVIEWER_SCHEMA_VERSION = 1

# Rubric is PROPOSED until an explicit, auditable human approval is recorded.
RUBRIC_VERSION = "r281-reviewer-rubric-0.1"
