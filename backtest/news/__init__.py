"""R2.7 Phase 2 — deterministic historical news classification cache (§11, §16, §20).

Strict separation of the two news clocks (N-21):

- **Classification / cache-population time** — ``backtest.news.classifier`` +
  ``backtest.news.cache_populate``. The ONLY authorized live-LLM context
  (§20 Phase 2). Never runs during a backtest replay.
- **Replay time** — ``backtest.news.cache`` (this store) serves cached
  ``news_schema_v3`` classifications deterministically from SQLite. A
  backtest NEVER issues a live LLM call (§21 item 18); nothing under
  ``backtest/news/`` except ``classifier.py`` may even import
  ``agent.auxiliary_client``, and ``classifier.py`` imports it lazily
  inside a classification call.

The deterministic core (``trading_core/news_effects.py``) owns the effect
mapping; this package owns persistence, cache identity, validation, and the
population/calibration infrastructure. The pinned model identifier remains
externally configured (``backtest.pinned_model`` in config.yaml) and BLOCKED
until supplied — no value is invented here.
"""
