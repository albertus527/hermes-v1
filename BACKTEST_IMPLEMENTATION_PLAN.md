# HERMES FABLE R2.7 — BACKTEST IMPLEMENTATION PLAN

**Scope of this document:** architecture and implementation plan for the R2.7
canonical backtest (docs/specs/HERMES_FABLE_R2_7_CANONICAL_BACKTEST_SPECIFICATION.md)
inside the Hermes Agent codebase. This is a PLAN ONLY. No code is written here,
no production behavior is changed, no strategy policy is invented. Every
proposed module references the existing Hermes file/module it integrates with.

**Date:** 2026-08-29, branch `feature/r27-backtest`.

---

## 0. Existing Hermes Agent structure (survey findings)

Application entry points:
- `hermes` (root launcher script) → `hermes_cli/main.py::main()` — argparse
  top-level parser (`build_top_level_parser()` in `hermes_cli/main.py`),
  subcommands in `hermes_cli/subcommands/*.py` (cron, tools, setup, gateway,
  model, skills, plugins, profile, …).
- `cli.py` — `HermesCLI` interactive loop; `load_cli_config()`.
- `run_agent.py` — `AIAgent` core conversation loop.
- `gateway/run.py` — messaging gateway (Telegram/Discord/… adapters under
  `gateway/platforms/` and `plugins/platforms/`).
- `mcp_serve.py`, `tui_gateway/`, `acp_adapter/`, `apps/desktop/` — other surfaces.

Tool/plugin architecture:
- `tools/registry.py` — central tool registry; `tools/*.py` self-register at
  import; `toolsets.py` gates exposure (`_HERMES_CORE_TOOLS` + named toolsets).
- `hermes_cli/plugins.py` — general plugin manager; `PluginContext` exposes
  `register_tool()`, `register_cli_command()`, lifecycle hooks. Plugins live in
  `plugins/<name>/` (bundled), `~/.hermes/plugins/` (user), or pip entry points.
- Memory/context-engine/image-gen plugins follow the "ABC + orchestrator +
  per-plugin directory" pattern (`agent/memory_provider.py` + `plugins/memory/`).
- Model providers: `providers/base.py::ProviderProfile` +
  `plugins/model-providers/<name>/`; `providers/__init__.py` lazy discovery.

Scheduling:
- `cron/jobs.py` (JSON job store at `~/.hermes/cron/jobs.json`) +
  `cron/scheduler.py::tick()/run_job()`; `hermes cron <verb>` CLI in
  `hermes_cli/subcommands/cron.py`; agent-facing `tools/cronjob_tools.py`.
- Cron supports `script` (pre-run data collection, stdout injected),
  `no_agent=True` (script-only jobs), `workdir`, per-job skills.

Configuration system:
- `hermes_cli/config.py` — `load_config()` (deep-merge of DEFAULT_CONFIG + user
  YAML); `hermes_cli/config_defaults.py` — `DEFAULT_CONFIG` (pure-data leaf,
  `_config_version: 39`) and `OPTIONAL_ENV_VARS` (secrets metadata).
- Three loaders: `load_cli_config()` (cli.py), `load_config()`
  (hermes_cli/config.py), direct YAML (gateway/run.py). New keys must consider
  all three.
- Policy: secrets in `.env` (`~/.hermes/.env`, registered in
  OPTIONAL_ENV_VARS); behavioral settings in `config.yaml` (config_defaults.py).
  Non-secret `HERMES_*` env vars are rejected by contribution policy.

Model/provider abstraction:
- `run_agent.py` AIAgent + `providers/` + `plugins/model-providers/`.
- Side-LLM work: `agent/auxiliary_client.py::call_llm(task=..., provider=...,
  model=..., temperature=..., response_format=...)` — shared resolution chain
  with per-task `auxiliary.<task>.*` config overrides; supports OpenAI
  structured-output `response_format: json_schema` (and Anthropic translation).

Clock/time dependencies:
- `hermes_time.py::now()` — timezone-aware wall clock (HERMES_TIMEZONE →
  config `timezone` → local). No injectable/simulated clock abstraction exists;
  every caller reads wall clock. `zoneinfo` is used for IANA zones.
- Cron timing derives from wall clock (`time.time`, `hermes_time.now`).

Persistence/storage:
- `hermes_state.py::SessionDB` — raw `sqlite3` (no SQLAlchemy ORM in core;
  SQLAlchemy 2.0.51 is present in uv.lock only as a transitive dep), WAL mode,
  schema in `hermes_state_schema.py`, profile-aware paths via
  `hermes_constants.py::get_hermes_home()`. Config section `database:
  journal_mode` exists.
- `utils.py::atomic_write_text/atomic_replace` for file durability.
- Cron store is JSON, not SQLite.

Logging:
- `hermes_logging.py::setup_logging()` — agent.log / errors.log / gateway.log,
  profile-aware, redacting formatter, `set_session_context()` correlation.

Existing data-provider abstractions:
- None for market data. The only market-data surface is the optional skill
  `optional-skills/finance/stocks/` (read-only Yahoo quote/history script,
  stdlib-only, no bars/feed/adjustment concepts). No Alpaca, Finnhub, FRED,
  corporate-actions, or exchange-calendar code exists in the repo.
- `pandas`, `sqlalchemy` (direct), `instructor`, `ta-lib`,
  `pandas_market_calendars` are NOT dependencies of Hermes (`pyproject.toml`
  checked; numpy 2.4.3 exists only in the `wake` extras; pydantic 2.13.4 and
  httpx 0.28.1 ARE base deps).

Existing tests:
- `tests/` pytest suite (~900 files), run ONLY via `scripts/run_tests.sh`
  (hermetic: per-file subprocess isolation, TZ=UTC, temp HERMES_HOME via
  `_isolate_hermes_home` autouse fixture in `tests/conftest.py`, credential
  env vars stripped). Source-reading and change-detector tests are banned;
  behavior-contract/invariant tests are the required style.

---

## 1. EXISTING HERMES COMPONENTS TO REUSE

| R2.7 need | Existing component | How it is reused |
|---|---|---|
| CLI surface for backtest runs | `hermes_cli/subcommands/*.py` pattern + `build_top_level_parser()` in `hermes_cli/main.py` | New `hermes backtest` (or plugin-registered) subcommand module, modeled on `hermes_cli/subcommands/cron.py` |
| Plugin CLI registration | `hermes_cli/plugins.py::PluginContext.register_cli_command()` | If the backtest ships as a plugin, its argparse tree wires into `hermes <name>` with zero core changes |
| Scheduling of Phase-2 news-cache job and (later) live scans | `cron/jobs.py` + `cron/scheduler.py` + `hermes_cli/subcommands/cron.py` | One-shot/recurring `no_agent=True` script jobs; no LLM in the loop for deterministic phases |
| Config plumbing (behavioral settings) | `hermes_cli/config_defaults.py::DEFAULT_CONFIG` + `hermes_cli/config.py::load_config()` | New `backtest:` / `trading:` config section; adding keys to an existing section needs no `_config_version` bump (deep-merge handles it) |
| Secrets registration | `OPTIONAL_ENV_VARS` in `hermes_cli/config_defaults.py` | `ALPACA_API_KEY/SECRET`, `FINNHUB_API_KEY`, `OPENROUTER_API_KEY` (OpenRouter likely already present — verify at implementation), FRED key if needed |
| Pinned side-LLM calls (Phase 2 news classifier, vision extractor) | `agent/auxiliary_client.py::call_llm()` with per-task `auxiliary.<task>.{provider,model,...}` overrides + `response_format` json_schema support | News classification pinned via `auxiliary.news_classification.*` config; backtest itself NEVER calls it (cache replay only, §11.5, §21 item 18) |
| LLM provider identity | `providers/base.py::ProviderProfile` + `plugins/model-providers/openrouter/` | Pinned `openrouter/<provider>/<model>@<version>` identifier resolved through the existing provider profile machinery |
| Timezone handling | `zoneinfo` usage + `hermes_time.py` | Strategy schedule is `America/New_York` via `zoneinfo.ZoneInfo("America/New_York")` directly — NOT via `hermes_time.now()` (wall clock is banned from decision paths, §21 item 27) |
| Profile-safe state paths | `hermes_constants.py::get_hermes_home()` / `display_hermes_home()` | All backtest DBs/artifacts under `$HERMES_HOME/backtest/` |
| Atomic artifact writes | `utils.py::atomic_write_text/atomic_replace` | Versioned artifacts (`universe.yaml`, `fee_schedule.yaml`, coverage manifests) written atomically |
| Logging | `hermes_logging.py` | Engine events go through standard loggers; the §16 event rows are DB records, not log lines |
| Test harness | `scripts/run_tests.sh` + `tests/conftest.py::_isolate_hermes_home` | New tests under `tests/backtest/`; never write to `~/.hermes/` in tests; invariant-style assertions only |
| Plugin discovery pattern | ABC + orchestrator precedent (`agent/memory_provider.py` + `agent/memory_manager.py` + `plugins/memory/`) | Template for the market-data-provider and corporate-actions-provider abstractions (§2 below) |
| Telegram delivery (live phase only) | `gateway/platforms/` + `plugins/platforms/telegram/` | Out of scope for the backtest itself; noted for live parity |

## 2. COMPONENTS REQUIRING ABSTRACTION

These are the seams where live and backtest MUST share one deterministic core
(spec: "identical code path live and backtest", §6; §14.2 "pure functions over
point-in-time snapshots"). None of these abstractions exist today.

### 2.1 Simulated clock / decision-time injection
- **Need:** every deterministic decision consumes a simulated timestamp `t`
  (N-02, N-21, §12). Wall clock is banned from decision paths (§21 item 27).
- **Conflict with existing code:** `hermes_time.now()` and `time.time()` are
  global wall-clock reads with no injection point; cron/scheduler assume wall
  clock throughout.
- **Proposed:** the deterministic core takes `t` (and the exchange calendar)
  as explicit function parameters — a "snapshot in, decisions out" design per
  §14.2. No global clock abstraction is introduced into Hermes core; the
  live adapter passes `now()`, the backtest adapter passes simulated `t`.
  This avoids touching `hermes_time.py` at all.

### 2.2 Market-data provider abstraction (bars, three-series)
- **Need:** split-adjusted (signal), unadjusted (executable), total-return
  (accounting, reporting-only) series with feed-parity (`feed == 'sip'`)
  assertion (§3.3, §3.5); point-in-time availability (N-02); P-1 required-bar
  semantics (§19 item 2).
- **Existing integration point:** none exists. New ABC, following the
  `agent/memory_provider.py` (ABC) + `agent/memory_manager.py` (orchestrator)
  precedent. Proposal: `backtest/data/market_data_provider.py` (ABC:
  `get_bars(ticker, timeframe, adjustment, start, end) -> DataFrame/rows` with
  `feed` and `adjustment` columns preserved per §16 `bars` table).
- **Backtest adapter:** SQLite-backed historical store populated by a Phase-0
  fetch job. **Live adapter (future):** Alpaca REST with the §3.2 15-minute
  boundary retry.

### 2.3 Corporate-actions provider abstraction
- **Need:** §3.6 contract — designated Alpaca Corporate Actions endpoint,
  versioned dataset (`corp_actions_version`), verified-zero attestations via
  `coverage_manifests`, `CORP_ACTIONS_UNVERIFIED` fail-safe, ratio back-out
  PROHIBITED (§3.6 rule 3, §21 item 28).
- **Existing integration point:** none. New ABC beside the market-data ABC.

### 2.4 News classification cache (LLM boundary)
- **Need:** cache-keyed replay `(headline_hash, source, schema_version,
  model_version)` (§11.5); backtest NEVER issues live LLM calls (§21 item 18);
  P-4 cache-integrity assertion on `(headline_hash, ticker)` effect-field
  equality.
- **Existing integration point:** `agent/auxiliary_client.py::call_llm()` for
  the Phase-2 population job ONLY. The deterministic core consumes a
  `ClassificationStore` interface with two implementations: `CachedStore`
  (SQLite, backtest + live replay) and the Phase-2 job's writer. The core
  never sees `call_llm`.

### 2.5 Portfolio state source
- **Backtest:** exact simulated equity (§13.7).
- **Live (future):** vision-extracted screenshot state (§18) — different
  quantity, disclosed divergence (§13.9 item 9).
- **Proposed:** core sizing functions take `portfolio_value: Decimal/float`
  as a parameter. No abstraction layer needed beyond a plain function
  argument — the divergence is a data-provenance issue, not an interface issue.

### 2.6 Exchange calendar
- **Need:** trading days, half-days, `T−1` (N-14), N-25 substitution semantics.
- **Existing integration point:** none (`pandas_market_calendars` not
  installed). Spec designates `pandas_market_calendars` with a manual override
  table fallback (§3.1). Proposal: thin `backtest/calendar.py` wrapper
  exposing `is_trading_day`, `next_trading_day`, `is_half_day`,
  `session_close` — so the dependency stays behind one module and can be
  replaced by the manual table if the package misbehaves.

## 3. NEW R2.7 DETERMINISTIC CORE COMPONENTS (shared live/backtest)

Proposed location: a new top-level package, e.g. `trading_core/` (name TBD at
implementation; must NOT live under `plugins/` if it is the canonical core —
see §14 conflict note). Every module below is pure functions over point-in-time
snapshots (§14.2); no I/O, no wall clock, no LLM.

| Module | Spec sections | Contents |
|---|---|---|
| `trading_core/indicators.py` | §6 | EMA (recursive α=2/(N+1), SMA seed), Wilder ATR14/RSI14, session VWAP (typical price, zero-volume guard), session volume pace, opening range, 20-day RS. **TA-Lib cross-validation at rel. err ≤ 1e−6 after 5×period warm-up is the Phase-1 exit gate.** Single pinned module — THE shared live/backtest path. |
| `trading_core/regime.py` | §5 | trend_score (SPY/QQQ vs EMA50/200), VIXCLS vol state with §5.2 gap rule, §5.3 regime mapping, §5.5 opening-drop filter (SPY, executable space, ex-date ratio handling) |
| `trading_core/gates.py` | §7.1–§7.2 | G2–G8 as pure predicates with full `inputs_json` capture for §16 `gate_results`; G6 `d(e)` session mapping incl. I-1 non-trading-day fallback; G7 windows per §11.2/E-06 |
| `trading_core/scoring.py` | §7.4–§7.6 | Closed-form score components, N-13 half-up rounding, tie-break (score desc, ticker asc), cutoffs 70/80, bands |
| `trading_core/pipeline.py` | §7.1 | Exact Stage 0 (S0.0→S0.5) → Stage 1 → 2 → 3 → 4 → 5 ordering, incl. N-10 same-scan lockout, Stage-5 no-resumption |
| `trading_core/sizing.py` | §9.1–§9.5 | risk_dollars → raw_notional → G10 → 90% cap → shares_est; screening realised_risk (N-07/N-08) |
| `trading_core/fees.py` | §9.6–§9.7 | Pluang transaction + JFX/KBI, SEC/TAF/CAT with effective-dated schedule, VAT branch, three rounding branches, `TRAIN_SUBSTITUTED_ZERO` (run-role-scoped, TEST/LIVE write = engine exception), `BACKWARD_PROJECTED_CONSTANT` provenance. **Phase-0 smoke test: exact reproduction of the §1.4 six-branch table.** |
| `trading_core/stops.py` | §8.4–§8.5, §8.7 | reference-anchored stop/target (N-06), adopted-position synthetic stop |
| `trading_core/exits.py` | §8.6, P-A-05, P-3 | priority-ordered exit evaluation, per-position trend failure, RISK_OFF same-session persistence |
| `trading_core/news_effects.py` | §11.1–§11.3, FP-2, P-4 | Ordered total-function effect mapping, scoring windows, NEWS_UNVERIFIED trigger sets (RP-02), keyword fallback (NFKC + casefold substring), two-source CRITICAL confirmation, headline_hash normalization (NFKC/casefold/punct-strip → SHA-256) |
| `trading_core/corporate_actions.py` | §13.6, P-A-01, P-A-03 | split adjustment mechanics, ex-date dividend entitlement/attribution, symbol-change/delisting force-close detection |
| `trading_core/official_prices.py` | N-22, N-25, P-2 | official open/close resolution with substitution logging |

Integration references: these consume the ABCs from §2 (market data,
corporate actions, calendar, classification store) and return plain data
structures that the caller persists via §16 stores. They must not import from
`run_agent.py`, `cli.py`, `gateway/`, or `tools/` — Hermes core stays generic.

## 4. NEW BACKTEST-ONLY ADAPTERS

| Adapter | Spec | Notes |
|---|---|---|
| `backtest/engine/simulator.py` | §13 | 6+1 scenario grid (FAST/DELAYED/NEXT_SESSION + MISSED null; EXIT_FAST/EXIT_DELAYED), N-03 notional-invariant fills, N-04 4-dp truncation, gap-through price-only semantics (N-09), missing-bar fallbacks, `ENTERED_BEYOND_STOP`, half-day DELAYED sequencing (FP-6), NEXT_SESSION S0.0 resolution (P-A-04), window-end force-close (N-24), cash assertions → `HERMES CRITICAL HALT` (§19 item 6) |
| `backtest/engine/slippage.py` | §13.4 | grid {0,5,10,25} bps, buy up / sell down |
| `backtest/engine/accounting.py` | §13.7 | USD cash ledger, daily equity emission with P-2/N-25 no-sample rule, dividend credit timing, `stop_overshoot`/`risk_divergence` (RP-10) |
| `backtest/engine/benchmarks.py` | §15.3 | per-window QQQ (criterion-bearing) + SCHG (reported) buy-and-hold, ex-date dividends, reinvestment simplification, fractional maxDD |
| `backtest/engine/metrics.py` | §15.2, §15.5 | net_expectancy (N-23), win rate, profit factor, Sharpe (daily rf=0), CAGR, MFE/MAE, bootstrap CI (numpy `default_rng(42)`, B=10,000, `rng.integers(0,n,size=n)`, sequential), INSUFFICIENT SAMPLE flag |
| `backtest/walkforward/scheduler.py` | §15.1 | 24/6/6 rolling windows over 2018-01-01→`coverage_end`, per-window independent runs (N-24), train-only refit, selection objective + tie-breaks, zero-trade carry-forward |
| `backtest/walkforward/selection.py` | §15.1 | search-space enumeration, evaluation log (count, seed, results — no silent discards, §21 item 19), canonical-cell pinning (N-17) |
| `backtest/walkforward/acceptance.py` | §15.4 | three criteria over verified ∩ corp-verified ∩ N-25-resolvable windows; ≥8 windows else NOT EVALUABLE; window exclusion bookkeeping |
| `backtest/data/fetch_alpaca.py` | §3.1–§3.5, §20 Phase 0 | historical bar ingestion (daily + 1-min, both adjustments, SIP feed assertion), corporate-actions ingestion, rate-limit handling (200 calls/min) |
| `backtest/data/fetch_finnhub.py` | §3.1, §11.6 | raw headline inventory + earnings calendar ingestion, coverage manifest capture |
| `backtest/data/fetch_fred.py` | §3.1, §5.2 | VIXCLS daily series |
| `backtest/news/cache_populate.py` | §20 Phase 2, §11.5 | the ONLY authorized bulk live-LLM context; classifies every timed headline in verified NEWS covered spans; writes cache + completeness report + P-4 integrity report. Uses `agent/auxiliary_client.py::call_llm` with pinned config. NOT a backtest. |
| `backtest/report.py` | §13.9, §15.2 | report generation incl. the verbatim 20-topic disclosure block and §1.3 structural-constraint headers |
| `backtest/cli.py` + `hermes_cli/subcommands/backtest.py` (or plugin CLI) | — | `hermes backtest run/fetch/populate-news-cache/report` verbs |

## 5. DATA SOURCES AND HISTORICAL DATA REQUIREMENTS

| Data | Source | Depth needed | Status per spec |
|---|---|---|---|
| Daily bars, split-adjusted + unadjusted (+ total-return for reporting) | Alpaca free tier, SIP feed | 2016+ (history for indicators) through `coverage_end`; window 2018-01-01+ | KNOWN documented; feed-parity hard assertion (§3.5); 15-min boundary MUST CONFIRM (live only) |
| 1-min bars, both adjustments | Alpaca free tier | 2018+; required labels 09:30–09:39 + 09:44 per session per ticker (P-1) | same as above |
| Corporate actions (splits, dividends; ex/record/pay dates) | Alpaca Corporate Actions endpoint | 2018+ for all universe tickers + QQQ/SCHG/SPY | DECIDED contract / coverage MUST CONFIRM (Phase-0 blocker for criterion-bearing runs) |
| News headlines (raw inventory) | Finnhub free tier | 2018+ per covered span | available; depth MUST CONFIRM |
| Earnings calendar | Finnhub free tier | 2018+ | depth MUST CONFIRM |
| VIX daily close | FRED `VIXCLS` | 2016+ (T−1 with 5-calendar-day gap rule) | KNOWN available |
| Exchange calendar incl. half-days | `pandas_market_calendars` + manual override | 2016+ | DECIDED; package not yet a dependency |
| LLM classifications | OpenRouter pinned model | per verified NEWS covered spans | identifier MISSING — blocks Phase 2 |
| Historical SEC/TAF/CAT rates | SEC/FINRA/CAT public notices | effective-dated incl. verified `applicable: false` spans | research task — Phase-0 prerequisite for criterion-bearing execution |

Artifacts (versioned, committed to the repo or pinned by hash):
`universe.yaml` (§4), `fee_schedule.yaml` (§9.7), `search_space.yaml`
(MISSING), `pre_registration_manifest.yaml` with concrete `coverage_end`
(MISSING), coverage manifests (§16).

## 6. DATABASE / STORAGE REQUIREMENTS

New SQLite database(s) under `$HERMES_HOME/backtest/`, opened with the same
journal-mode conventions as `hermes_state.py` (WAL default; respect
`database.journal_mode` config). Append-only tables, every row carrying
`run_id, config_version, code_commit`; decision-bearing rows additionally
`universe_version` (§16).

Tables required by §16 (normative):
`run_history` (with `run_role ∈ {TRAIN, TEST, LIVE}`), `bars` (feed must equal
'sip' for decision-consumed bars), `corp_actions`, `news_headlines`,
`coverage_manifests` (source_kind ∈ NEWS/EARNINGS/CORP_ACTIONS/FEE_SCHEDULE),
`regime_snapshots`, `indicator_snapshots`, `gate_results`, `scores`,
`news_classifications`, `recommendations`, `sim_trades` (incl.
`dividends_net`, `train_fee_substitutions_json`, flags), `simulation_events`
(all P-1..P-4 event codes), `fee_calculations` (with truthful
`fee_input_status` per §16 rule 3).

Missing persistence schemas to be designed and committed before Phase 4
(E-03 disclosure, §16/§23): run↔window boundaries, per-run `initial_capital`
and scenario cell, §15.1 configuration-evaluation log.

Do NOT extend `hermes_state.py::SessionDB` — that is the agent session store
and is generic infrastructure. The backtest store is a separate database file
with its own schema module (e.g. `backtest/db/schema.py`), reusing only the
connection/journal-mode helpers pattern from `hermes_state.py`.

## 7. PHASE-0 PREREQUISITES FROM R2.7 (§20 Phase 0)

1. Credentials: Alpaca, OpenRouter, (Telegram is live-phase) — register in
   `OPTIONAL_ENV_VARS` (`hermes_cli/config_defaults.py`); user stores in
   `~/.hermes/.env`.
2. Local SQL store (§6 above).
3. Versioned `universe.yaml` with UNCONFIRMED Pluang statuses (§4.1).
4. Historical SEC/TAF/CAT effective-dated `fee_schedule.yaml` incl. verified
   `applicable: false` spans — external research task; blocks
   criterion-bearing OOS, NOT train-window computation (§9.7 item 3).
5. Corporate-actions dataset + run-pinnable `CORP_ACTIONS` coverage-manifest
   attestations incl. verified-zero spans, 2018+ — coverage confirmation is a
   Phase-0 prerequisite (§3.6).
6. NEWS/EARNINGS raw inventories + versioned coverage manifests where
   available.
7. Exchange calendar incl. half-days; add `pandas_market_calendars` to
   `pyproject.toml` per the dependency-pinning policy (`>=cur,<next`) +
   `uv lock`.
8. Smoke test: exact reproduction of the §1.4 six-branch fee table (all six
   rows must match exactly).
9. Feed-parity assertion wired (§3.5, §19 item 8).
10. Live 15-min-boundary confirmation task registered (§3.2) — live-phase.
11. Pre-registered news-classification label set (incl. `ma_role`) and vision
    validation sets.
12. Add `pandas` and `numpy` as base dependencies (spec §14.1 stack names
    pandas/numpy/sqlalchemy/pydantic/instructor; pydantic and httpx exist;
    numpy exists only in the `wake` extra — see §14 conflicts).

## 8. PHASE-1 DETERMINISTIC ENGINE REQUIREMENTS (§20 Phase 1)

- All `trading_core/` modules from §3: data stack adapters, universe, regime,
  indicators, gates, ranking, sizing, fee model, schedule, exit logic.
- P-1: required-bar semantics (ten 09:30–09:39 + 09:44), sparse 09:40–09:43
  tolerated, entry-only zero-volume VWAP guard, last-available-bar exit
  evaluation.
- P-2: N-25 official-price substitution + daily-equity emission semantics.
- P-3: RISK_OFF same-session advisory persistence.
- G6 earnings session mapping incl. I-1 non-trading-day fallback.
- S0.0 pending-entry resolution (P-A-04); per-position trend failure
  (P-A-05); symbol-change/delisting detection (P-A-03).
- **Exit gate: smoke tests pass AND TA-Lib cross-validation at rel. err ≤
  1e−6 after 5×period warm-up** — this makes `ta-lib` (or `TA-Lib` python
  wrapper) a dev/validation dependency at minimum.

## 9. PHASE-2 NEWS CACHE REQUIREMENTS (§20 Phase 2, §11)

- **BLOCKED until the exact pinned `openrouter/<provider>/<model>@<version>`
  identifier is set in config** (§11.5, §14.1 — MISSING).
- `news_schema_v3` strict JSON (incl. `ma_role`, `keyword_override`),
  temperature 0, `llm_config_version` tracked.
- Cache key `(headline_hash, source, schema_version, model_version)`;
  historical caches never overwritten; model upgrades bump
  `llm_config_version`.
- Cache-population job: one-time bulk pass over run-pinned verified NEWS
  covered spans; classifies EVERY timed headline irrespective of wall-clock
  age (N-21); the only authorized bulk live-LLM context; uses
  `agent/auxiliary_client.py::call_llm`; produces cache-completeness report
  (zero misses over covered spans) + P-4 integrity report (identical effect
  fields per `(headline_hash, ticker)`).
- Calibration: ≥200 manually labeled headlines; accuracy + confidence
  calibration report; Phase-2 exit gate; threshold 0.85 remains ASSUMPTION
  pending calibration.
- Backtests replay from cache only (§21 item 18).

## 10. PHASE-3 SIMULATION REQUIREMENTS (§20 Phase 3, §13)

- Executable-series mechanics with P-1 bar-availability provenance, P-2 N-25
  substitution + no-sample daily equity, P-3 suppressed-scan advisory
  persistence.
- 6+1 scenario grid; slippage grid {0,5,10,25}; three fee-rounding branches;
  two VAT branches; FX reporting grid {0.25%,0.5%,1.0%}; news-on/off and
  earnings-on/off sensitivity pairs over verified covered spans.
- Corporate actions per §3.6: ex-date dividend entitlement/attribution +
  `dividends_net`; `CORP_EVENT_FORCE_CLOSE`; S0.0 NEXT_SESSION void/unfillable
  states; gap-through price-only; per-position trend failure;
  `ENTERED_BEYOND_STOP`; cash assertions.
- Provisional fee-span and corp-actions-span segregation = ENUMERATION ONLY
  for partially verified test windows (no simulated metrics, no
  partial-window subsetting, never `TRAIN_SUBSTITUTED_ZERO` in TEST).
- Full §16 event/provenance logging; §13.9 20-topic disclosure block in every
  report header.

## 11. PHASE-4 WALK-FORWARD REQUIREMENTS (§20 Phase 4, §15)

- **BLOCKED until `search_space.yaml` (full grid) AND
  `pre_registration_manifest.yaml` (concrete `coverage_end`) are committed at
  the pre-registration commit** (§15.1). OOS evaluation must not begin before
  that commit; its commit date is the pre-registration timestamp.
- N-24 per-window independent runs ($200 criterion-bearing, $61 disclosure);
  window-end force-close with N-25 resolution.
- Train-window selection: maximize `net_expectancy` (N-23) on the canonical
  evaluation cell (N-17); tie-breaks (trade count → fractional maxDD →
  enumeration order); zero-trade carry-forward; `TRAIN_SUBSTITUTED_ZERO`
  applied by run role with full provenance.
- Acceptance (§15.4): criteria evaluated only on fully verified ∩
  N-25-resolvable test windows; ≥8 windows else NOT EVALUABLE; every TEST run
  asserts zero `TRAIN_SUBSTITUTED_ZERO` writes (halt under §19 item 6).
- Phase-4 persistence schemas (run↔window boundaries, per-run
  initial_capital + scenario cell, configuration-evaluation log) must be
  committed before Phase 4 is fully reconstructible (E-03, §23).

## 12. TEST PLAN

All tests under `tests/backtest/` (new directory), run via
`scripts/run_tests.sh` only; stdlib + pytest + unittest.mock; no live
network; `_isolate_hermes_home` gives each test a temp HERMES_HOME.
Invariant/behavior-contract style per AGENTS.md (no source-reading tests, no
change-detector snapshots).

1. **Fee smoke test (Phase 0 gate):** reproduce the §1.4 six-branch table
   exactly (fees_buy/fees_sell/fees_rt/burden per row).
2. **Indicator cross-validation (Phase 1 gate):** EMA/RSI/ATR vs TA-Lib,
   rel. err ≤ 1e−6 after 5×period warm-up, on synthetic series.
3. **Fee-schedule state machine:** verified rate / verified-zero
   (`applicable: false`) / unverified → PROVISIONAL; `TRAIN_SUBSTITUTED_ZERO`
   legal only in TRAIN runs; TEST/LIVE attempted write raises the
   deterministic engine exception (§19 item 6); $0.01 minimum never lifts a
   substituted zero; fee-computation-date rules (decision date for G9
   screening, fill date for actuals).
4. **G9/G10 structural arithmetic:** §1.3.1 $61 unsatisfiability outside
   RISK_ON; §1.3.2 $200 ATR floor — assert the gate geometry, not the
   disclosure approximations.
5. **Pipeline order (§7.1):** S0.0 before S0.1; N-10 same-scan lockout;
   Stage-5 no-resumption; opening-drop computed even when S0.3 would end the
   pipeline.
6. **N-02 availability:** at 10:00 the last consumable bar is 09:44; at 15:30
   it is 15:14; half-day 12:00 → 11:44; boundary inclusivity.
7. **P-1 required bars:** missing one of 09:30–09:39/09:44 →
   `TICKER_REQUIRED_BAR_EXCLUSION`; 09:40–09:43 absence alone never excludes;
   zero-volume → `VWAP_UNDEFINED_ZERO_VOLUME` entry-side only; exit eval
   proceeds on last-available bar, `EXIT_EVAL_SUPPRESSED` only when no
   in-session bar exists at/before the boundary.
8. **N-25 substitution:** nearest-at-or-before, nearest-following fallback,
   window exclusion when unresolvable; every substitution logged
   (`OFFICIAL_PRICE_SUBSTITUTION`); no daily equity sample for missing-bar
   sessions; drawdown over emitted samples only.
9. **N-24 window independence:** no equity carry-over; window-end force-close
   with slippage + full sell fees; `WINDOW_END_FORCE_CLOSE` logged.
10. **Scenario grid mechanics:** FAST/DELAYED/NEXT_SESSION fills, DELAYED
    voiding window, half-day DELAYED sequencing, NEXT_SESSION S0.0
    void/unfillable paths, gap-through price-only (never fill at stop, never
    before detection), `ENTERED_BEYOND_STOP` with signed
    `realised_risk_actual`, missing-bar fallbacks,
    `ENTRY_UNFILLABLE_WINDOW_END` precedence.
11. **Cash assertions:** `cash ≥ notional_actual + fees_buy` at entry;
    `cash ≥ 0` always; violations halt.
12. **Dividends (P-A-01):** ex-date entitlement, withholding, earliest-of
    credit timing, `dividends_net` equals sum of attributed net dividends
    (§16 rule 6); benchmark credit/reinvestment rule incl. C-1 scope.
13. **Splits:** mechanical share/stop/target adjustment on ex-date; opening
    drop ex-date denominator handling; ratio back-out never used.
14. **Delisting/symbol change (P-A-03):** 10:00 detection, scenario-timed
    fill, last-available price, full sell fees at fill date.
15. **News determinism:** headline_hash normalization vectors (NFKC, casefold,
    whitespace/punct strip, SHA-256); ordered total-function mapping incl.
    first-match exclusivity cases (BEARISH-CRITICAL ∩ M&A/TARGET;
    MACRO/OTHER precedence); FP-3 low-confidence behavior (0 score, veto/exit
    retained, universal 24h NEWS_UNVERIFIED trigger); two-source rule with
    source-independent hashes; keyword override forces BEARISH+CRITICAL and
    persists; P-4 same-hash/ticker mismatch → `NEWS_CACHE_INTEGRITY_FAILURE`
    halt; cache-miss vs coverage-gap distinction (RP-02).
16. **Walk-forward selection:** objective + tie-breaks, zero-trade
    carry-forward, no silent discards (every evaluated configuration logged),
    canonical-cell pinning.
17. **Acceptance criteria:** window-positivity ≥60% with zero-trade windows
    as non-positive (N-18); pooled aggregate; maxDD_max ≤ 1.5× QQQ; <8
    windows → NOT EVALUABLE.
18. **Metrics:** bootstrap with `default_rng(42)` reproduces identical CI for
    a fixed input ordering (E-02 caveat acknowledged); fractional maxDD
    definition shared by strategy and benchmark.
19. **Feed parity:** any decision-consumed bar with `feed ≠ 'sip'` fails the
    run.
20. **Provenance:** all §16 rules 1–10 reconstructible from the DB;
    `fee_input_status` truthful for every row.

## 13. IMPLEMENTATION ORDER

0. **This plan** (current task) — review sign-off.
1. **Phase 0 scaffolding:** dependencies (pandas, numpy base, ta-lib
   validation extra, pandas_market_calendars) per pinning policy + `uv lock`;
   `backtest/` package skeleton; DB schema (§6); config section +
   OPTIONAL_ENV_VARS entries; `hermes backtest` subcommand wiring;
   `universe.yaml`, `fee_schedule.yaml` seed; fee module + §1.4 smoke test
   FIRST (it is the Phase-0 gate); feed-parity assertion.
2. **Phase 0 data jobs:** Alpaca bars ingest (both adjustments, SIP),
   corporate-actions ingest + coverage manifests, Finnhub raw inventory +
   manifests, FRED VIX, exchange calendar. External research: SEC/TAF/CAT
   history; Alpaca corporate-actions coverage confirmation.
3. **Phase 1 core:** indicators (+ TA-Lib gate), regime, opening drop, gates,
   scoring, sizing, stops, exits, pipeline — with the §12 tests.
4. **Phase 2:** pinned LLM config (BLOCKER — identifier missing);
   classification store + schema v3; cache-population job; calibration report;
   completeness + P-4 integrity reports.
5. **Phase 3:** simulator, slippage, accounting, benchmarks, metrics,
   disclosures, sensitivity branches.
6. **Phase 4 (BLOCKED on pre-registration commit):** `search_space.yaml` +
   `pre_registration_manifest.yaml`; walk-forward harness; Phase-4 persistence
   schemas; acceptance evaluation.
7. **Live adapters (separate later task, not this one):** live market-data
   adapter with §3.2 retry, screenshot/vision pipeline, Telegram templates.

## 14. CONFLICTS BETWEEN CURRENT HERMES BEHAVIOR AND R2.7

1. **Wall-clock coupling.** `hermes_time.now()` and `time.time()` are the
   only clocks; R2.7 bans wall clock from decision paths (§21 item 27) and
   binds the news clock to simulated decision time (N-21). Resolution:
   deterministic core takes `t` as a parameter (§2.1); NO change to
   `hermes_time.py`. No conflict if the core never imports it.
2. **Missing dependencies.** R2.7 §14.1 names `pandas`, `sqlalchemy`,
   `instructor`; the repo has none as base deps (numpy only in the `wake`
   extra; SQLAlchemy only transitive). pandas/numpy are genuinely needed by
   §6 ("single pinned pandas/numpy module"). SQLAlchemy is NOT needed —
   existing storage is raw sqlite3 (`hermes_state.py`); use raw sqlite3 for
   the backtest store too and document the deviation, or justify adding it.
   `instructor` is not needed — `agent/auxiliary_client.py::call_llm` already
   supports `response_format: json_schema` structured output. Each new
   dependency needs the pinning policy (`>=floor,<ceiling`) + `uv lock`.
3. **Cron assumes wall clock and agent sessions.** Backtest phases are batch
   jobs, not conversational turns. Use `no_agent=True` script jobs for the
   Phase-2 cache population and ingest jobs; do NOT route deterministic
   phases through `AIAgent` (LLM must have no authority, §21 items 2–3).
4. **Contribution-policy tension: where the code lives.** AGENTS.md steers
   new capability to plugins/skills and keeps the core narrow; but the R2.7
   deterministic core is the canonical shared live/backtest engine, and the
   repo's own plugin policies prohibit special-casing. Recommended
   resolution: `trading_core/` + `backtest/` as first-party packages in the
   fork (this IS the fork's product), with generic Hermes files
   (`run_agent.py`, `cli.py`, `tools/`, `toolsets.py`, `gateway/`) untouched.
   If upstream-mergeability of this fork matters, the alternative is a plugin
   (`plugins/backtest/`) using `register_cli_command` — but the deterministic
   core is not a "third-party product" and the fork exists for this purpose;
   document the choice rather than forcing the plugin shape. (No production
   Hermes behavior changes either way.)
5. **New env vars.** API keys belong in OPTIONAL_ENV_VARS (allowed — secrets).
   Everything else (thresholds, grids, schedule) lives in `config.yaml` or
   versioned YAML artifacts, NOT new `HERMES_*` env vars (policy).
6. **Test isolation.** Tests must never touch `~/.hermes/`; backtest tests
   must use temp HERMES_HOME and synthetic bar series (no network, no real
   Alpaca data in unit tests; recorded fixtures under `tests/backtest/fixtures/`
   are acceptable if license-clean — prefer synthetic).
7. **SQLite concurrency model.** SessionDB has heavy WAL/locking machinery
   for concurrent agent access. Backtest runs are single-writer batch
   processes; reuse the journal-mode config but do not inherit SessionDB's
   session-scoped locking assumptions.
8. **No existing market-data or trading code.** The `optional-skills/finance/stocks`
   skill (Yahoo, unofficial, no SIP/adjustments) does NOT satisfy §3.1/§3.5
   and must not be used as a data source for the backtest. Noted to prevent
   accidental reuse.
9. **Auxiliary-client fallback chains.** `call_llm`'s auto fallback across
   providers conflicts with §11.5's single-provider pinned routing
   requirement. The Phase-2 job must invoke it with explicit
   `provider=`, `model=`, `temperature=0` (pinned route), and must verify the
   resolved route matches the pin — never rely on `auto` resolution.

## 15. R2.7 REQUIRED ARTIFACTS STILL MISSING

From §23's missing/prerequisite register (plus repo check):

1. `search_space.yaml` — exact grid values. **Blocks Phase 4.** Not in repo.
2. `pre_registration_manifest.yaml` — concrete `coverage_end`. **Blocks
   Phase 4 / any OOS evaluation.** Not in repo.
3. Pinned `openrouter/<provider>/<model>@<version>` identifier. **Blocks
   Phase 2 completion.** Not set anywhere in this repo.
4. Historical SEC/TAF/CAT fee-schedule research + `fee_schedule.yaml` entries
   (incl. verified `applicable: false` spans). **Blocks criterion-bearing OOS
   over unverified spans** (train windows proceed via §9.7 item 3).
5. Alpaca Corporate Actions coverage confirmation + verified coverage
   manifests (2018+, universe + QQQ/SCHG/SPY). **Blocks criterion-bearing OOS
   over unattested spans.**
6. Finnhub news + earnings history-depth confirmation.
7. Phase-4 persistence schemas (run↔window boundaries, per-run
   initial_capital + scenario cell, configuration-evaluation log). **Blocks
   Phase-4 reconstruction completeness** (E-03).
8. Execution-event schema / confirmation grammar; full non-BUY Telegram
   templates — live-phase only, do not block the backtest.
9. Broker confirmations (fee rounding scope, fractional minimums, share
   precision, VAT, Pluang MCP scope, 15-min boundary) — external; shape
   disclosures and Phase 5, do not block backtest construction.
10. §11.4 calibration label set (≥200 manually labeled headlines) — Phase-2
    exit gate artifact.
11. Pluang ticker confirmations in `universe.yaml` (`pluang_confirmed`) —
    live-gating; backtest includes unconfirmed tickers with disclosure (§7.2
    G3, §13.9 item 7).

---

## READY FOR IMPLEMENTATION?

**YES WITH BLOCKERS**

Blockers (phase-scoped; none block Phase 0/1 construction of the
deterministic core):

- Phase 2: pinned OpenRouter model identifier missing (§11.5/§14.1); ≥200
  headline calibration set not yet produced.
- Phase 4: `search_space.yaml` grid and `pre_registration_manifest.yaml`
  `coverage_end` missing — pre-registration commit required before any OOS
  evaluation.
- Criterion-bearing test-window execution: historical SEC/TAF/CAT verified
  fee schedule and Alpaca corporate-actions coverage attestations are
  unconfirmed external prerequisites (train-window computation may proceed
  under `TRAIN_SUBSTITUTED_ZERO`).
- Repo-level decisions to confirm before writing code: (a) add pandas, numpy
  (base), pandas_market_calendars, and a TA-Lib validation dependency to
  `pyproject.toml` under the pinning policy; (b) confirm raw-sqlite3 (no
  SQLAlchemy) for the backtest store, consistent with `hermes_state.py`;
  (c) confirm package placement (`trading_core/` + `backtest/` first-party in
  this fork vs. plugin layout) per §14 item 4.
