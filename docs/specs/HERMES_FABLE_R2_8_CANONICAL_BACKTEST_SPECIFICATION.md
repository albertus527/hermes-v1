# HERMES FABLE R2.8 — CANONICAL BACKTEST SPECIFICATION
**Revision:** R2.8 (R2.7 authoritative baseline with only the accepted targeted earnings-source clarifications A-1 through A-4, with corrections C-1 through C-3, integrated in place; P-1 through P-4, C-1, C-2, I-1, I-2, P-A-01 through P-A-05, FP-1 through FP-8, EB-01, TRAIN_SUBSTITUTED_ZERO, and all other R2.7 normative policy are preserved without reopening; R2.6 audit category-E findings E-1 through E-13 remain explicitly not applied)  
**Status:** **R2.8 — R2.7 TARGETED EARNINGS-SOURCE CLARIFICATION APPLIED; READY FOR INDEPENDENT PATCH VERIFICATION.** No final freeze, release approval, strategy validation, profitability validation, OOS pass, paper-trading pass, broker/provider confirmation, or live readiness is claimed.

---

## §0. Normative Determinism Conventions (Binding Index)

Every convention below is **DECIDED** and binding throughout this document. Where a convention resolves a previously ambiguous point, the choice is stated here once and propagated to every affected section.

**Citation convention:** members of numbered lists are cited as "§*S* item *n*" (e.g., "§21 item 18"). All section references in this document refer to R2.8 sections only.

| # | Convention | Binding choice |
|---|---|---|
| N-01 | Bar labeling | All intraday bars are labeled by **bar start time** (provider convention). A 1-min bar labeled `09:44` opens at 09:44:00 and closes at 09:45:00. |
| N-02 | Point-in-time availability | A bar is available at decision time `t` iff `bar_close_time ≤ t − 15 min` (inclusive). At 10:00 ET the last available 1-min bar is the bar **labeled 09:44** (closing 09:45:00). Backtest fills (§13) are simulator constructs exempt from this rule but may never precede the decision or detection that caused them (N-09). |
| N-03 | Fill-quantity invariance | **Notional is invariant** from decision to fill. Shares are re-derived at the actual fill price. |
| N-04 | Share precision | `shares_filled = truncate(notional / fill_price, 4 decimal places)`. Simulator default; broker fractional precision is **MUST CONFIRM WITH BROKER**. |
| N-05 | Fee basis | All fees are computed and debited on **actual fill notional and actual filled shares**. Gate-A fees are a pre-trade **screening estimate** (N-07, N-08). |
| N-06 | Stop/target anchor | Stop and target are anchored to **`entry_reference_price`** (the 09:44-bar close, executable series), fixed at decision time, published in Telegram, and used identically live and in backtest. Never re-anchored to the fill price. |
| N-07 | Risk quantities | `realised_risk = notional × stop_pct` is a **pre-trade screening quantity** (Gate-A denominator). `realised_risk_actual = shares_filled × (fill_price − stop_price)` is computed and logged post-fill, **signed** (may be ≤ 0 per §13.2 / §15.2). Divergence is disclosed, not hidden. |
| N-08 | Gate-A sell-side assumption | Screening `fees_rt(notional)` evaluates the sell side at **entry notional and entry share estimate** (`shares_est = notional / entry_reference_price`) — a flat-exit assumption. |
| N-09 | Exit fill precedence | §13.3 exit-fill timing governs **unconditionally**. Gap-through affects the fill **price only** (actual executable price at the §13.3 fill timestamp), never the fill time. No fill may precede its detection scan. No **exit** fill occurs at the stop level itself. |
| N-10 | Same-scan sequencing | If any exit trigger fires at a scan, **no new entry is evaluated that scan**; earliest new-entry evaluation is the next trading day's 10:00 scan. No entry fill may precede the corresponding exit fill (guaranteed by this rule). |
| N-11 | Series assignment | See §3.3 consumer table. `entry_reference_price`, evaluation prices, stop/target comparisons, voiding tests, opening-drop **both legs**, and all fills → **executable series**. Indicators, gates G4/G5, scores, ATR, `stop_pct`, trend-failure → **signal series**. |
| N-12 | Opening-drop instrument | **SPY**, global effect. The §5.5 return is computed from the 09:44 executable bar at the 10:00 scan whenever **S0.0 and/or S0.4** requires it; a pending NEXT_SESSION entry is resolved at S0.0 before S0.1, while ordinary same-day BUY suppression remains S0.4. Both legs are in executable (unadjusted) space (§5.5). |
| N-13 | Score arithmetic | Total score is rounded **half-up to the nearest integer** (`floor(x + 0.5)`) before cutoff, banding, and ranking. Rank tie-break: higher score, then **ascending ticker lexicographic**. |
| N-14 | `T−1` | The prior **trading day** per the exchange calendar. |
| N-15 | `initial_capital` basis | Canonical capitals ($61, $200) are **post-FX USD**. FX cost is a reporting-layer adjustment only (§9.9). |
| N-16 | Criterion-bearing run | The **$200** run bears the §15.4 acceptance criteria. The $61 run is a mandatory disclosure run and is structurally constrained (§1.3). |
| N-17 | Canonical evaluation cell | `entry = FAST, exit = EXIT_FAST, slippage = 10 bps, fee_rounding = CEIL_CENT_PER_COMPONENT, vat_on_regulatory = false, USD basis, news and earnings gates ENABLED where covered (coverage-disabled elsewhere per §11.6 / §7.2 G6)`. All other cells are reported-only. Applies to **both** train-window selection (§15.1) and acceptance evaluation (§15.4). **PRE-REGISTERED RESEARCH DECISION.** |
| N-18 | Zero-trade windows | A zero-trade test window counts as **non-positive** and remains in the denominator of the window-positivity criterion. |
| N-19 | News constants | Activation windows, integer catalyst points, aggregation, and the ingestion guard are fixed in §11. |
| N-20 | Scenario count | The scenario grid is **3 populated entry × 2 exit = 6 combinations, plus MISSED as one degenerate entry-only null run** ("6 + 1"). |
| N-21 | **News clock** | All news staleness and window tests are evaluated against the **simulated decision timestamp `t`** of the consuming scan, **never** against wall-clock fetch/classification time. A headline participates in an effect at scan `t` iff `published_at` is present, `published_at ≤ t`, and `t` lies within that effect's window (§11.2). For **NEWS_UNVERIFIED trigger scope**, every timed classification additionally carries the universal 24-hour trigger window `0 ≤ t − published_at ≤ 24 h`, irrespective of whether its mapped effect branch contributes points; any mapped G7 activation window is tested independently (§11.2). The Phase-2 cache-population job classifies **every** timed headline in the covered span irrespective of wall-clock age; staleness is applied at consumption, not at classification. Untimed headlines, future-dated-at-`t` headlines, and headlines outside both the universal 24-hour trigger window and any applicable mapped G7 activation window never raise NEWS_UNVERIFIED. |
| N-22 | **Official close / official open** | **"Official close"** = the **close of the daily executable bar** for that session. **"Official open"** = the **open of the daily executable bar**. The last intraday minute bar is never substituted for either. Slippage per §13.4 applies to fills at these prices. |
| N-23 | **Net expectancy** | `net_expectancy` = arithmetic mean, over trades, of **realised net P&L in USD per trade** (§15.1). All trades weighted equally; aggregates pool trades, not windows. R-multiple expectancy is reported-only and is never a selection objective or acceptance criterion. |
| N-24 | **Walk-forward equity basis** | Every train window and every test window is simulated as an **independent run** starting at `initial_capital` with `cash = initial_capital`; equity never carries across windows. Compounding is ON **within** a window only (§15.1). Window-end official-open/official-close resolution and daily equity-sample emission follow N-25. |
| N-25 | **Absent or non-trading official open/close** | Wherever a rule requires the official open or official close (N-22) of a nominal date, the value is taken from the daily executable bar of the **nearest exchange trading session at or before** that nominal date for which the bar exists; if no such session exists at or after the window start, the **nearest following** such session inside the window is used. If neither exists inside the window, the affected test window is excluded from the §15.1 verified-window count, from criterion 1's denominator, and from criteria 2–3 aggregates, exactly as §9.7 item 4 excludes partially fee-verified windows. A daily equity sample (§15.3 items 3–4) is **not emitted** for a session whose required daily executable bar is absent, and the drawdown series is computed over emitted samples only. Every substitution is logged with the nominal date, the substituted session, and the consuming rule. |

---

## §1. Executive Decision / Objective

**Objective.** Rank and recommend the highest expected net-edge long setup(s) in US equities/ETFs, subject to risk, fee, and data-quality constraints. Recommendations are delivered as Telegram messages for **manual execution**. Diversification is a **risk constraint**, not an objective.

**Scope.** This document defines the deterministic backtest baseline and the live recommendation pipeline it mirrors, with all live↔backtest divergences disclosed (§13.9). The strategy is **not validated** and is **not described as profitable**. This specification authorizes construction of the backtest that will test the strategy.

### §1.1 Baseline constants and structural decisions

- `max_positions = 1` — **DECIDED**.
- Long-only. No shorting, no options, no leverage.
- Starting live capital ≈ **$61** (`Rp 1,000,000`).
- Backtest initial capital is configurable; canonical required runs are **$61** and **$200** (both post-FX USD, N-15). The **$200 run is criterion-bearing** (N-16).
- Broker reality: execution via **Pluang** → **PALN routing** → **Alpaca Securities LLC**. Fractional shares are supported; minimum broker order is **$1**. (Fractional support and $1 minimum: KNOWN published behavior. Fractional **share precision** and **regulatory fee minimum applicability on fractional orders**: **MUST CONFIRM WITH BROKER**.)
- Recommendation-only operation is a **USER REQUIREMENT**, not a structural impossibility.
- A permissioned Pluang Agentic Trading / MCP channel is **KNOWN to exist** in some form; its scope/availability for this user is **MUST CONFIRM WITH BROKER**. It is **OPTIONAL / FUTURE** and **not used by the baseline**.
- Portfolio-state source for the baseline is **screenshot ingestion** — **DECIDED**.

### §1.2 System boundary

- No autonomous order placement, ever.
- All deterministic calculations — gates, scores, sizing, fees, stops, exits — execute in code.
- The LLM is confined to: (1) constrained news classification into a fixed JSON schema; (2) vision extraction of portfolio screenshots into a fixed JSON schema; (3) narrative text that explains, but cannot alter, pre-computed decisions.
- Stops and targets are advisory levels; the system cannot enforce them.
- Every BUY message carries the honest-risk disclaimer.
- FX hedging is out of scope. FX conversion cost is a real modeled cost (reporting layer, §9.9).

### §1.3 STRUCTURAL FEE-VIABILITY CONSTRAINTS — ARITHMETIC CONSEQUENCES, NOT ASSUMPTIONS

The following results are **derived arithmetic consequences** of §5.3, §9.1, §9.4, and §9.6 at the baseline parameters. They are not tunable and must appear in the report header of the runs to which they apply. **Train-window caveat:** on dates where §9.7 applies `TRAIN_SUBSTITUTED_ZERO`, the regulatory contribution used by G9 is deliberately zero for the affected component(s), so the §1.3.1/§1.3.2 derived admissibility bands do **not** describe the train-window G9 geometry on those substituted dates. This does not change the §1.4 verified-rate smoke-test targets.

#### §1.3.1 The $61 run

**NEUTRAL / VOLATILE regimes (multiplier 0.5):**

$$\text{risk\_dollars} = \$61 \times 1.0\% \times 0.5 = \$0.305 \quad\Rightarrow\quad \text{Gate A requires } \text{fees\_rt} \le 0.25 \times \$0.305 = \$0.07625$$

Because `realised_risk = notional × stop_pct = risk_dollars` identically whenever the 90% cap does not bind, the Gate-A fee cap is fixed while fees increase monotonically in notional; the G10 floor (`notional = $10.00`) therefore gives the global minimum burden:

| Branch | fees_rt at $10 notional | vs. $0.07625 cap |
|---|---:|---|
| `CEIL_CENT_PER_COMPONENT` | $0.13 | fails |
| `CEIL_CENT_PER_SIDE` | $0.11 | fails |
| `EXACT` | ≈ $0.1077 | fails |

If the 90% cap binds, `realised_risk` falls below $0.305 and the constraint tightens further. **G9 ∧ G10 is therefore jointly and totally unsatisfiable at $61 in NEUTRAL and VOLATILE regimes under all three rounding branches.**

**RISK_ON regime (multiplier 1.0):** `risk_dollars = $0.61`, fee cap $0.1525. G9 ∧ G10 admits only:

$$\text{stop\_pct} \in [\approx 3.87\%\text{–}4.06\%,\; 6.10\%] \quad\Longleftrightarrow\quad \text{ATR\%} \in [\approx 1.55\%\text{–}1.62\%,\; 2.44\%] \text{ at } \text{stop\_mult} = 2.5$$

(lower edge rounding-branch-dependent; upper edge is the G10 floor `0.61/6.1% = $10`).

**Disclosed consequences:** (1) the $61 run is a RISK_ON-only, narrow-ATR-band selector — because Stage 4 selects the first rank-order candidate passing G9/G10, **ATR becomes the de facto selection variable at $61**; (2) trade counts will be far below the §15.5 threshold and many test windows are expected to contain zero trades; (3) the $61 run is **not criterion-bearing** (N-16).

#### §1.3.2 The $200 criterion-bearing run

The same derivation at $200:

- **NEUTRAL/VOLATILE:** `risk = $1.00`, fee cap $0.25 ⇒ under the canonical `CEIL_CENT_PER_COMPONENT` branch the fee-edge occurs at `notional ≈ $27.03` (disclosure-level approximation: ≈ $27) ⇒ $\text{stop\_pct} \in [\approx 3.70\%, 10\%]$, i.e. $\text{ATR\%} \in [\approx 1.48\%, 4.0\%]$.
- **RISK_ON:** `risk = $2.00`, fee cap $0.50 ⇒ under the canonical `CEIL_CENT_PER_COMPONENT` branch the fee-edge occurs at `notional ≈ $57.06` (disclosure-level approximation: within ≈ $55–60) ⇒ $\text{stop\_pct} \in [\approx 3.51\%, 20\%]$, i.e. $\text{ATR\%} \in [\approx 1.40\%, 8.0\%]$.

**The $61 pathology does not propagate** — the $200 run is feasible in all admitting regimes — **but the run carries a hard minimum-ATR% admissibility floor of approximately 1.4–1.5% in the canonical branch**, so ATR remains a partial Stage-4 selection variable in the criterion-bearing run. **DISCLOSED STRUCTURAL CONSEQUENCE**; mandatory in the $200 report header (§13.9 item 12). The figures in this subsection are disclosure-level approximations; the actual G9 decision always uses the exact §9.6 fee computation.

### §1.4 Worked example — Portfolio = $61.00

Illustrative baseline assumptions unless otherwise noted:

```text
risk_per_trade = 1.0%                  ASSUMPTION / MUST TEST
regime_multiplier = 1.0 (RISK_ON)      ASSUMPTION / MUST TEST
risk_dollars = $61 × 1.0% × 1.0 = $0.61

ATR% = 2.0%                            ILLUSTRATIVE ASSUMPTION
stop_mult = 2.5                        ASSUMPTION / MUST TEST
stop_pct = 2.5 × 2.0% = 5.0%

raw_notional = $0.61 / 0.05 = $12.20
max deploy cap = 0.90 × $61 = $54.90
notional = min($12.20, $54.90) = $12.20
realised_risk (screening) = $12.20 × 5.0% = $0.61
```

Screening fee components at notional $12.20 (`shares_est ≈ 0.0674` at a $181 reference price; all three sell-side regulatory minimums bind at $0.01):

```text
transaction = 0.003 × 12.20 = $0.036600   → ×1.11 = $0.040626
JFX/KBI     = 0.0005 × 12.20 = $0.006100  → ×1.11 = $0.006771
SEC/TAF/CAT (sell only) = $0.01 + $0.01 + $0.01 = $0.030000
```

**Six-branch fee table (Phase-0 smoke-test targets — all six must be reproduced exactly):**

| `vat_on_regulatory` | Rounding branch | fees_buy | fees_sell | fees_rt | Gate A burden |
|---|---|---:|---:|---:|---:|
| false (canonical) | CEIL_CENT_PER_COMPONENT (canonical) | $0.06 | $0.09 | **$0.15** | **24.59%** |
| false | CEIL_CENT_PER_SIDE | $0.05 | $0.08 | $0.13 | 21.31% |
| false | EXACT | $0.047397 | $0.077397 | $0.124794 | 20.46% |
| true | CEIL_CENT_PER_COMPONENT | $0.06 | $0.12 | $0.18 | **29.51% — FAILS Gate A** |
| true | CEIL_CENT_PER_SIDE | $0.05 | $0.09 | $0.14 | 22.95% |
| true | EXACT | $0.047397 | $0.080697 | $0.128094 | 21.00% |

**Conditionality disclosure.** The canonical-branch Gate-A pass (24.59% vs 25%, a 0.41 pp margin) is conditional on **two unverified broker facts**: (1) fee rounding scope — **MUST CONFIRM WITH BROKER**; (2) applicability of the $0.01 SEC/TAF/CAT minimums to fractional orders — **MUST CONFIRM WITH BROKER**. If the minimums do not apply on fractional orders, burden ≈ 19.7%; if VAT applies to regulatory components under per-component ceiling, burden = 29.51% (fails). No element of this example is labeled KNOWN beyond the published rate formulas themselves.

---

## §2. Adjudication Record

<details>
<summary><strong>Full adjudication table (source-explicit items, RG-series R2 repairs, RP-series R2.1 patches, FP-series R2.2 release-gate closures)</strong></summary>

| Item | Canonical effect | Status |
|---|---|---|
| Conflict #1 | Baseline `max_positions = 1`; regime caps not active | DECIDED |
| D-02(src) | Pre-close scan restored and required | DECIDED |
| D-03(src) | Canonical rounding `CEIL_CENT_PER_COMPONENT`; sensitivity branches; receipt verification | DECIDED / MUST CONFIRM WITH BROKER |
| D-04(src) | SEC/TAF/CAT effective-dated; no backward projection | DECIDED |
| D-05(src) | Global minimum-viable-capital constructs removed; viability via G9/G10 | REMOVED / DECIDED |
| D-06(src) | Gate B deferred; no shadow logging of undefined quantity | DEFERRED / NOT IN EFFECT |
| D-07(src) | Slippage: sensitivity grid; MUST MEASURE in paper trading | ASSUMPTION |
| D-08(src) | Take-profit restored as 2R | ASSUMPTION / MUST TEST |
| D-10(src)/C5 | Three-series model: signal, executable, accounting | DECIDED |
| D-11(src) | One stop convention: fixed at entry decision, percentage form, ATR-based | DECIDED convention / parameter MUST TEST |
| D-12(src) | Gate-A denominator is post-clamp screening realised risk; $10 minimum is rejection, not clamp-up | DECIDED |
| D-13(src) | Pinned LLM config, temperature 0, schema versioning, cache-keyed replay | DECIDED |
| D-14(src) | Append-only reconstruction requirement | DECIDED |
| D-16(src) | Walk-forward pre-registered before OOS evaluation | PRE-REGISTERED RESEARCH DECISION |
| D-18(src) | FX hedging out of scope; FX cost modeled at deposit/withdrawal | DECIDED model / rate ASSUMPTION |
| D-20(src)/C1 | Opening-drop triggers on `≤ −0.02` | DECIDED measurement / threshold ASSUMPTION |
| D-23(src)/C3–C4 | Stage-4 rank-order iteration; exact staged pipeline | DECIDED |
| D-24(src) | Survivorship bias disclosed, upward, non-eliminable | DISCLOSED BIAS |
| C2 | Marginal band removed; Strong and Moderate only | CORRECTED |
| R1 | VAT canonical branch: transaction + JFX/KBI only | DECIDED / MUST CONFIRM WITH BROKER |
| R2 | No fee-rate backfill; unverified spans PROVISIONAL | CORRECTED |
| R3 | $10 minimum = rejection; only downward 90% cap | CORRECTED |
| R4 | Global min-viable-capital gate removed | REMOVED |
| R5 | Two-state exit scenarios crossed with entry scenarios | CORRECTED |
| R6 | Walk-forward criteria are pre-registered research decisions, not validated truths | CORRECTED |
| RG-01 | Notional-invariant fills; fees on actual fill quantities; screening vs actual risk split | DECIDED (N-03/05/07) |
| RG-02 | Stop/target anchored to `entry_reference_price` | DECIDED (N-06) |
| RG-03 | §13.3 governs exit timing unconditionally; gap-through is price-only | DECIDED (N-09) |
| RG-04 | Exit-day entry lockout; cash assertion | DECIDED (N-10) |
| RG-05 | Bar-start labeling convention | DECIDED (N-01) |
| RG-06 | Series-consumer assignment table | DECIDED (N-11) |
| RG-07 | Opening-drop instrument = SPY, global, Stage 0 | DECIDED (N-12) |
| RG-08 | Opening range 09:30–09:40 so the break component is satisfiable | DECIDED convention / window ASSUMPTION / MUST TEST |
| RG-09 | Closed-form score components; integer rounding; ticker tie-break | DECIDED (N-13) |
| RG-10 | News integer points, aggregation, two-source rule (whitelist deleted); Stage-5 no-resume | DECIDED |
| RG-11 | G6 coverage-gap rule mirroring §11.6; as-revised-dates bias disclosed | DECIDED |
| RG-12 | `applicable: false` verified-zero encoding; <8 verified windows → NOT EVALUABLE | DECIDED |
| RG-13 | Feed-parity (SIP) hard assertion | DECIDED |
| RG-14 | Walk-forward selection objective, tie-breaks, canonical cell, zero-trade rules | PRE-REGISTERED RESEARCH DECISION |
| RG-15 | Benchmark symmetry; drawdown sampling; CI estimator | PRE-REGISTERED RESEARCH DECISION |
| RG-16 | $200 criterion-bearing; $61 disclosure-only | DECIDED |
| RG-17 | VAT relabeled DECIDED/MUST CONFIRM; "mirroring" claim replaced by divergence disclosure; prior readiness status retired | DECIDED |
| RG-18 | News-cache population job; TA-Lib validation → Phase-1 exit gate, 1e−6 tolerance | DECIDED |
| **RP-01** | News clock bound to simulated decision timestamp; cache job classifies irrespective of wall-clock age; staleness never raises NEWS_UNVERIFIED; live 7-day rule reclassified as **live ingestion guard only** with disclosed live↔backtest divergence | DECIDED (N-21) |
| **RP-02** | Backtest NEWS_UNVERIFIED trigger set defined (low-confidence timed classifications within the universal 24-hour trigger or an applicable mapped G7 window; cache miss within a covered span uses the 24-hour trigger); cache miss ≠ coverage gap | DECIDED |
| **RP-03** | Walk-forward per-window independent-run equity basis; per-window criterion evaluation; per-window benchmarks | PRE-REGISTERED RESEARCH DECISION (N-24) |
| **RP-04** | `net_expectancy` = equal-weighted mean USD net P&L per trade; pooled aggregates | PRE-REGISTERED RESEARCH DECISION (N-23) |
| **RP-05** | Fee-verified window = all-days rule; partial windows excluded from count, denominator, aggregates | DECIDED |
| **RP-06** | Globally excluded days: entries suppressed, exits evaluated; `EXIT_EVAL_SUPPRESSED` fallback | DECIDED |
| **RP-07** | Corporate-actions data contract; designated provider = Alpaca Corporate Actions endpoint, coverage MUST CONFIRM; `CORP_ACTIONS_UNVERIFIED` fail-safe; ratio back-out prohibited | DECIDED contract / provider MUST CONFIRM |
| **RP-08** | "Official close/open" = daily executable bar close/open | DECIDED (N-22) |
| **RP-09** | Opening-drop both legs in executable space; ex-date ratio handling | DECIDED |
| **RP-10** | `stop_overshoot` and `risk_divergence` formulas; signed `realised_risk_actual`; `ENTERED_BEYOND_STOP` state | DECIDED |
| **RP-11** | Cross-reference sweep; citation convention in §0 | DECIDED (clerical) |
| **RP-12** | $200 ATR-floor disclosure (§1.3.2); volume-pace split artifact, voiding asymmetry, NEXT_SESSION overlap added to §13.9; N-17 news clause; accounting-series role clarified; zero-volume VWAP guard; bootstrap RNG pinned; Alpaca 15-min boundary added to confirmations | DECIDED (disclosure/determinism only; no policy change) |
| **FP-01** | `news_schema_v3`; `ma_role`; G7 M&A-target predicate made schema-expressible | DECIDED / APPLIED |
| **FP-02** | News-effect mapping made an ordered total function of `(category, direction, severity, ma_role)` with MACRO/OTHER precedence | DECIDED / APPLIED |
| **FP-03** | `confidence < 0.85`: zero score contribution, G7 veto and exit-trigger participation retained, NEWS_UNVERIFIED raised | DECIDED behavior / threshold remains ASSUMPTION pending calibration |
| **FP-04** | Source-independent `headline_hash`; source added to cache key; deterministic keyword match forces BEARISH+CRITICAL at classification time and persists `keyword_override` | DECIDED / APPLIED |
| **FP-05** | Raw news inventory + versioned coverage manifests; covered-span semantics; corporate-actions verified-zero attestation and all-days test-window verification | DECIDED / APPLIED; provider coverage MUST CONFIRM |
| **FP-06** | Prior trend-failure 10:00-only wording is **superseded only by accepted P-A-05**; NEXT_SESSION window-end expiry and half-day DELAYED sequencing remain preserved | DECIDED / APPLIED, with R2.5 P-A-05 supersession on trend-failure timing only |
| **FP-07** | Fractional max drawdown; pre-registered `coverage_end`; train-window verification treatment fixed | PRE-REGISTERED RESEARCH DECISION / APPLIED |
| **FP-08** | §13.9 disclosures for live-only BUY suppressions and backward-projected broker/JFX-KBI/VAT constants | DISCLOSED / APPLIED |
| **P-A-01 (R2.5)** | Dividend entitlement by ex-date; deterministic net-dividend credit/attribution; `dividends_net` trade reconstruction | DECIDED / APPLIED |
| **P-A-02 (R2.5)** | G6 earnings event→session mapping and explicit `{T,T+1,T+2}` blackout indices | DECIDED / APPLIED |
| **P-A-03 (R2.5)** | Symbol-change/delisting 10:00 detection; exit-scenario timestamp; last-available price + sell slippage; full sell fees and fill-date fee state | DECIDED / APPLIED |
| **P-A-04 (R2.5)** | Stage S0.0 NEXT_SESSION pending-entry resolution before S0.1; void/no-filter expiry semantics; same-day exit eligibility | DECIDED / APPLIED |
| **P-A-05 (R2.5)** | Trend-failure trigger per open position from entry fill; earliest qualifying non-suppressed exit-evaluating scan; suppressed scans do not disarm | DECIDED / APPLIED |
| **P-1 (R2.7)** | Required executable intraday bars fixed to 09:30–09:39 plus 09:44; 09:40–09:43 sparsity tolerated; VWAP/pace use returned bars; zero-volume is entry-side-only; exits use last available bar unless none exists at/before the N-02 boundary | DECIDED / APPLIED |
| **P-2 (R2.7)** | N-25 deterministic official-open/official-close substitution; absent daily-bar samples omitted; unresolvable test windows excluded from verified-window acceptance aggregates | DECIDED / APPLIED |
| **P-3 (R2.7)** | RISK_OFF advisory persists across suppressed exit scans to the next non-suppressed exit-evaluating scan of the same session; lapses at session close if unissued | DECIDED / APPLIED |
| **P-4 (R2.7)** | News effect fields depend only on normalized headline text + ticker; source is cache/confirmation metadata only; same-hash/ticker effect mismatch is a canonicality-halting cache-integrity failure | DECIDED / APPLIED |

Source adjudication IDs D-01, D-09, D-15, D-17, D-19, D-21, D-22 are **absent from the source record**; the gaps are ID gaps, not dropped adjudications.

[MISSING SOURCE CONTENT — CANNOT CANONICALIZE] A complete adjudication history beyond the items above is not available.

</details>

---

## §3. Data Stack

### §3.1 Providers

| Role | Provider | Status |
|---|---|---|
| Price bars, daily + 1-min | Alpaca free tier; historical queries with `end ≥ 15 minutes old` return consolidated SIP; history from 2016; 200 calls/min | KNOWN documented; **hard feed-parity assertion required** (§3.5); exact behavior of requests at the precise 15-minute boundary **MUST CONFIRM** (see §3.2) |
| **Corporate actions & dividends** | **Designated provider: Alpaca Corporate Actions endpoint** (consistent with the surviving bar-data architecture). Coverage, history depth, and field completeness for the 2018+ window are **MUST CONFIRM**. Data contract in §3.6. | DECIDED contract / provider availability MUST CONFIRM; fail-safe `CORP_ACTIONS_UNVERIFIED` (§3.6) |
| News headlines | Finnhub free tier | KNOWN available; history depth MUST CONFIRM |
| Earnings calendar | **Baseline provider: Finnhub free tier** (A-1). Finnhub is the **baseline**, not an exclusive mandatory provider: an alternative earnings source MAY be used iff it satisfies the §3.7 earnings-source data contract. | Baseline KNOWN available; earnings calendar history depth MUST CONFIRM; alternative sources per §3.7 |
| VIX | FRED series `VIXCLS`, prior daily close | KNOWN available; gap rule §5.2 |
| LLM | OpenRouter, pinned model | KNOWN availability; exact pinned identifier must be set in config (MISSING — blocks Phase 2) |
| Exchange calendar | `pandas_market_calendars`, community package, not authoritative; fallback manual override table | DECIDED |

### §3.2 Bar labeling and point-in-time availability (NORMATIVE)

- **Bars are labeled by start time** (N-01).
- Availability rule (N-02), inclusive boundary:

```text
data_available(t) = { bars : bar_close_time ≤ t − 15 minutes }
```

- Last-available 1-min bar at each decision point:

| Decision time | Last available 1-min bar (label) | Its close |
|---|---|---|
| 10:00 ET scan | 09:44 | 09:45:00 |
| 15:30 ET scan | 15:14 | 15:15:00 |
| 12:00 ET half-day scan | 11:44 | 11:45:00 |

- No decision may consume any datum with availability time after the decision timestamp.
- **Live boundary confirmation:** a live request at exactly `t` for bars through `t − 15:00` sits precisely on the free-tier delay boundary. Backtest behavior is deterministic regardless; live behavior at the boundary is **MUST CONFIRM** (external-confirmation list, §20 Phase 0). If live requests at the boundary fail, the live scan retries once at `t + 60 s` with the same bar set (deterministic; the consumed bar set is unchanged).

### §3.3 Price-series semantics and consumer assignment (NORMATIVE)

Three distinct series:

| Series | Source / adjustment | Role |
|---|---|---|
| Signal series | Split-adjusted daily + 1-min bars. **OHLCV, including volume, is consumed exactly as returned by the provider under the same split-adjusted request; Hermes performs no independent volume adjustment or ratio back-out.** | indicators, gates, scores, stops-as-percent |
| Executable series | Unadjusted, as-traded prices | all reference prices, evaluation prices, opening-drop legs, and simulated fills |
| Accounting series | Total-return, all-adjusted | **reporting context only.** It is **not** an input to benchmark construction (the §15.3 benchmark is built from executable prices plus explicit net dividends) and is never a fill price. Retained solely for report-level total-return context displays. |

**Consumer assignment table (N-11):**

| Consumer | Series |
|---|---|
| EMA/ATR/RSI/RS/dollar-volume, G4, G5, trend-failure exit, `stop_pct` computation | Signal (daily) |
| Session VWAP, opening range, session volume pace, intraday evaluation prices | Executable 1-min; required-bar and last-available-bar semantics follow §19 item 2 |
| `entry_reference_price` | Executable (09:44-bar close) |
| `stop_price`, `target_price` (levels), stop/target comparisons, voiding tests | Executable (unadjusted space) |
| All simulated fills; "official open"/"official close" (N-22) | Executable |
| Opening-drop numerator **and denominator** (§5.5) | Executable (unadjusted), with explicit ex-date ratio handling |
| Benchmark and performance reporting | Per §15.3 construction (executable + explicit net dividends); accounting series for context displays only |

**P-1 intraday availability semantics:** entry-side ticker processing requires all ten executable 1-min bars labeled 09:30–09:39 and the executable 09:44 bar (§19 item 2). Session VWAP and session pace use the executable bars actually returned within 09:30–09:44, subject to the zero-volume guard; exit evaluation for an open position instead uses §8.6's last-available-bar rule and is suppressed only under the explicit §19 item 2 boundary test.

**Disclosed artifact (volume pace):** session volume pace (§6.1) uses executable (unadjusted) volumes over a 20-session lookback; a split inside the lookback inflates pace by approximately the split factor for up to 20 sessions, affecting the +10 opening-range-break and +5 pace score components. This is deterministic and computable identically by all implementations; it is **DISCLOSED** in §13.9 item 13 and is intentionally **not** repaired to avoid altering scoring policy.

Mid-position splits adjust `shares`, `stop_price`, and `target_price` mechanically by the split ratio on ex-date (§13.6), using §3.6 data only.

### §3.4 Market-data staleness kill switch

If the latest expected **global/provider freshness** bar is missing, the provider errors, or the latest available bar is older than required delay + 20 minutes at scan time, emit `DATA DEGRADED — no recommendations` and block all BUY output. Distinct from portfolio-staleness warnings, which never block a scan. **Ticker-specific intraday bar absence does not by itself invoke this global kill switch; required-bar entry-side exclusion and open-position exit suppression follow §19 item 2.**

**Parity disclosure:** DATA DEGRADED and provider-failure days are **not simulated**; the backtest may trade on days live would not (§13.9 item 2).

### §3.5 Feed parity (NORMATIVE)

Live and backtest bars must both be **consolidated SIP**. `bars.feed` must equal `sip` for every decision-consumed bar. A Phase-0/Phase-1 hard assertion fails the run on any mismatch (§19 item 8). IEX-only bars must never feed G8, VWAP, opening range, or volume pace.

### §3.6 Corporate-actions data contract (NORMATIVE)

All split ratios, ex-dates, dividend amounts, record dates, and pay dates consumed anywhere in this specification (strategy accounting §13.6–§13.7, stop/target/share adjustments, benchmark construction §15.3, opening-drop ex-date handling §5.5) are taken **exclusively** from the designated corporate-actions source (§3.1).

**Required fields per event:** `ticker, event_type ∈ {SPLIT, CASH_DIVIDEND}, ex_date, split_ratio (SPLIT only), cash_amount_per_share (DIVIDEND only), record_date, pay_date`.

**Rules:**

1. The dataset is versioned as `corp_actions_version`, logged on every simulated trade and every benchmark run.
2. Values are **as-revised** (fetched current, not point-in-time). **DISCLOSED BIAS** (§13.9 item 14); a point-in-time corporate-actions snapshot does not exist in the baseline.
3. **Deriving split ratios or dividends from adjusted ÷ unadjusted price quotients is PROHIBITED.**
4. **Coverage-manifest verification (FP-5 — NORMATIVE):** corporate-actions coverage is attested in `coverage_manifests` (§16), with `source_kind = CORP_ACTIONS`. A `(ticker, span)` is verified iff the run-pinned `manifest_version` contains a `verified = true` attestation whose `[span_start, span_end]` covers that span. A verified attestation may cover a span with **no corporate-action events**; this is the corporate-actions **verified-zero** state. Absence of an attestation — not absence of events — yields `CORP_ACTIONS_UNVERIFIED`.
5. **Window-level verification granularity (FP-5 — NORMATIVE):** a test window is **corp-actions-verified** iff **every trading day within it** is covered by a verified corporate-actions attestation for **every universe ticker plus QQQ/SCHG/SPY**. Partially verified test windows are excluded from the §15.1 verified-window count, from criterion 1's denominator, and from criteria 2–3 aggregates exactly as §9.7 item 4 excludes partially fee-verified windows. No partial-window trade subsetting is permitted.
6. **Fail-safe / scope:** criterion-bearing **OOS/test-window** execution refuses unverified corporate-actions spans and the affected test windows are excluded. Train-window simulation is explicitly **not criterion-bearing** and follows §15.1: `CORP_ACTIONS_UNVERIFIED` spans inside train windows do not block selection, but are logged per window and disclosed in the report header. Live BUY output is blocked for an affected ticker when required corporate-actions coverage is unattested. Provider coverage confirmation remains a Phase-0 prerequisite before criterion-bearing execution (§20).
7. **Dividend provenance (P-A-01):** `ex_date` is the entitlement date consumed by strategy and benchmark accounting; `record_date` is stored for provenance only and is never a decision or entitlement input. Strategy dividend attribution/credit timing is §13.6; benchmark handling is §15.3 item 2.

### §3.7 Earnings-source data contract (NORMATIVE)

This section is the provider-neutral contract an earnings source must satisfy. **A-1 (provider substitutability):** Finnhub free tier (§3.1) is the **baseline** earnings provider, not an exclusive mandatory provider. An alternative earnings source MAY be used only when it satisfies this §3.7 contract. **A-2 (backtest/live divergence):** the earnings source used for BACKTEST MAY differ from the earnings source used for LIVE operation; each source must independently satisfy the same canonical G6 semantics (§7.2) and this §3.7 data-contract requirement. Any such divergence is disclosed at report level (§13.9 item 21).

**A-3 — Provider-neutral minimum earnings data contract.** A compliant earnings source must provide sufficient semantic information for existing G6 evaluation (§7.2), and no more:

- `ticker`;
- the earnings event calendar date; and
- a provider/source timing value sufficient to **deterministically map into an existing canonical G6 timing branch** (§7.2: before-market-open, after-market-close, unspecified).

**No new G6 timing branch may be created** by this contract.

**C-1 — Timing semantics.** A provider value MAY map to the **before-market-open** branch only when its semantics unambiguously mean before market open. A provider value MAY map to the **after-market-close** branch only when its semantics unambiguously mean after market close. A provider value MAY map to the existing **unspecified** branch only when the provider value itself represents missing, unknown, unspecified, or an equivalent semantic state compatible with existing §7.2. A provider value with **known but different semantics — for example, "during market hours" — MUST NOT automatically map to the unspecified branch.** If the provider timing semantics cannot be represented by an existing canonical G6 timing branch without semantic invention, the source/event is **NON-COMPLIANT for canonical G6 evaluation for the affected event/span**. No fourth timing branch is created; §7.2 G6 mapping is not modified by this contract.

**A-4 / C-3 — Static historical earnings datasets and input reproducibility.** A **static historical earnings dataset** MAY be used for BACKTEST when it satisfies the approved coverage, provenance, reproducibility, deterministic-replay, and as-revised requirements, i.e. it MUST:

- contain `ticker`;
- contain the event calendar date;
- contain timing semantics sufficient for the existing §7.2 G6 mapping (C-1 above);
- have verified EARNINGS coverage spans under the existing `coverage_manifests` mechanism (§11.6, §16);
- be sufficiently immutable, version-pinned, or reconstructible for deterministic replay (below);
- preserve existing G6 provenance (§16);
- retain the existing as-revised earnings-data bias (§7.2 DISCLOSED BIAS, §13.9 item 14); and
- preserve existing run-pinned EARNINGS `manifest_version` requirements (§11.6, §16).

It is **NOT required** to reconstruct historical scheduled-calendar revisions or what was knowable at simulated timestamp T — point-in-time scheduled-calendar history remains NOT REQUIRED.

**Input reproducibility is distinct from coverage-manifest run-pinning.** The earnings source — whether live API or static dataset — MUST be sufficiently immutable, version-pinned, or reconstructible to permit deterministic replay of every G6 decision for each run. This requirement applies to the earnings **input itself**. Existing run-pinning of the EARNINGS coverage manifest under §16, including `manifest_version` logged per run, remains intact and unchanged. `manifest_version` alone does **not** uniquely identify the provider, does not version the source dataset, does not hash the source dataset, and does not reconstruct the source dataset; no dataset-hash field or other new persistence field is added by this contract.

**C-2 — Provider identity.** No per-row provider identity, provider metadata schema, or provider ID field is introduced by this contract; existing §16 provenance remains unchanged. Source identification needed by the §13.9 disclosure (item 21) is **descriptive report-level metadata only** and creates no persistence-schema requirement.

**Coverage semantics are not weakened by this contract:** unverified or missing coverage MUST NOT become verified-zero coverage; the §11.6/§16 coverage-manifest rules govern EARNINGS coverage exactly as before.

---

## §4. Instrument Universe

The universe is an explicit versioned artifact: `universe.yaml`. `universe_version` is logged with every decision-bearing database row (§16).

### §4.1 Baseline universe v1.0 (validation date 2026-08-25)

All `pluang_confirmed` statuses are **UNCONFIRMED** until the user verifies each ticker in the Pluang app. Unconfirmed tickers are excluded from live recommendations and included in backtest only with the exclusion disclosed.

| Tickers | Class | Leveraged | History continuity | Pluang status |
|---|---|---|---|---|
| QQQ, VUG, SCHG, VGT, SMH, IWF | ETF | No | 2016+; SMH structure change predates window | UNCONFIRMED |
| AAPL, MSFT, NVDA, GOOGL, AMZN, META, TSLA, AVGO, JPM, V, MA, UNH, HD, PG, COST, ORCL, NFLX, AMD, CRM, ADBE | Stock | No | 2016+ | UNCONFIRMED |

### §4.2 Universe rules

- Leveraged and inverse products categorically excluded; each entry asserts `leveraged: false`.
- `history_continuous_since` required per entry; discontinuities exclude the affected span.
- User confirmation increments the universe version and stamps `pluang_confirmed: <date>`.

### §4.3 Survivorship bias

Curated list of currently surviving mega-caps. **DISCLOSED BIAS — upward, non-eliminable in baseline.** Every backtest report header carries this disclosure (§13.9 item 1).

---

## §5. Market Regime Engine

Computed once daily at **08:15 ET** from prior trading-day closes (`T−1`, N-14), signal series. Deterministic.

### §5.1 Trend score

EMA convention (all EMAs): recursive EMA with $\alpha = 2/(N+1)$, seeded with the SMA of the first $N$ closes.

```text
trend_score = count of TRUE among:
  SPY close(T−1) > SPY EMA200
  SPY EMA50 > SPY EMA200
  QQQ close(T−1) > QQQ EMA200
  QQQ EMA50 > QQQ EMA200
```

### §5.2 Volatility state

```text
vol_state = HIGH if VIXCLS(T−1) > 25 else NORMAL
```

`25` is **ASSUMPTION / MUST TEST**.

**VIX gap rule (DECIDED):** `T−1` is the prior exchange trading day. If `VIXCLS` is missing for that date, use the most recent `VIXCLS` observation within the preceding **5 calendar days**. If none exists, regime computation fails → global exclusion per **§19 item 2** (excluded-day behavior defined there: entries suppressed, exits still evaluated). Adequacy of this rule **MUST TEST**.

### §5.3 Regime mapping

| trend_score | vol_state | Regime | regime_multiplier | New longs |
|---|---|---|---|---|
| 4 | NORMAL | RISK_ON | 1.0 | Allowed |
| 2–3 | NORMAL | NEUTRAL | 0.5 | Allowed, cutoff raised |
| 2–4 | HIGH | VOLATILE | 0.5 | Allowed, cutoff raised |
| 0–1 | any | RISK_OFF | — | Blocked; exit/trim advisories only |

All multipliers and bands **ASSUMPTION / MUST TEST**. Per §1.3.1: at $61, multiplier 0.5 renders G9 ∧ G10 unsatisfiable — arithmetic, restated wherever $61 results appear.

### §5.4 Regime position-count caps

Caps `5/3/2` are **NON-NORMATIVE ANNOTATIONS — NOT IN EFFECT**. Normative only after an aggregate-risk admission rule for positions ≥ 2 is specified and tested (**UNRESOLVED / DEFERRED**).

### §5.5 Opening-drop filter (instrument, series, scope — NORMATIVE)

- **Instrument: SPY** (N-12). Effect: **global** — suppresses ordinary same-day BUY output for the day, all tickers; it also resolves a pending NEXT_SESSION entry under §7.1 S0.0 / §13.2.
- Computed from 09:44 data at the **10:00 ET scan** whenever **S0.0 or S0.4 requires it**. If both require it on the same day, compute once and reuse the identical result. The computation is not skipped merely because S0.3 would otherwise end the pipeline.
- **S0.0 scope:** S0.0 requires the computation only when a NEXT_SESSION fill is scheduled for that day. S0.4 requires it for the ordinary same-day entry pipeline. This preserves FAST and DELAYED entry semantics.
- **Both legs in executable (unadjusted) space** (RP-09). Adjusted series are prohibited here: a retro-adjusted denominator embeds the cumulative factor of all future splits — a fetch-date artifact in a decision input.

```text
SPY price_0944            = close of SPY executable 1-min bar labeled 09:44
SPY prior_official_close  = SPY official close (N-22, executable daily bar) of T−1

if an SPY split ex-date (per §3.6) falls on day T:
    prior_official_close ← prior_official_close / split_ratio
(no other adjustment is applied)

opening_return = (price_0944 − prior_official_close) / prior_official_close

if opening_return ≤ −0.02:
    suppress all ordinary same-day BUY output for the day (global); a pending NEXT_SESSION candidate is voided at S0.0 (§7.1)
```

Threshold −2% **ASSUMPTION / MUST TEST**. Exit logic is unaffected except that an S0.0-resolved NEXT_SESSION position that stands becomes exit-eligible at that same day's 10:00 exit evaluation (§7.1, §13.2).

---

## §6. Indicator Set

All indicators implemented once, in a single pinned pandas/numpy module; identical code path live and backtest.

- Wilder smoothing for ATR and RSI. Seed: simple average of the first 14 periods, then Wilder recursion.
- EMA: recursive, $\alpha = 2/(N+1)$, SMA seed.
- **TA-Lib cross-validation is a Phase-1 exit gate**: for EMA, RSI, ATR, maximum relative error ≤ $10^{-6}$ per value after a warm-up of 5 × period bars — **DECIDED tolerance**.

### §6.1 Indicator table

| Indicator | Parameters | Series / timeframe | Role |
|---|---|---|---|
| EMA | 20, 50, 200 | Signal daily | Trend gates |
| ATR | 14, Wilder | Signal daily | Stop/sizing only; never directional |
| RSI | 14, Wilder | Signal daily | Momentum ranking + overextension gate |
| Session VWAP | anchored 09:30 ET; per-bar price input = **typical price** $(H+L+C)/3$, volume-weighted; computed over the executable bars **actually returned** with labels 09:30–09:44. **Zero-volume guard (RP-12/P-1):** if cumulative volume over those returned bars is zero, VWAP is undefined → exclude the ticker from **ENTRY-SIDE evaluation only (Stages 1–5)** for the day, logged `VWAP_UNDEFINED_ZERO_VOLUME`; this guard never suppresses exit evaluation (§19 item 2). | Executable 1-min | Intraday bias gate at 10:00 scan |
| Volume | daily vs 20-day average; **session pace** = (cumulative session volume over executable bars **actually returned** with labels 09:30–09:44) / (mean over the prior 20 sessions of cumulative volume over the executable bars actually returned with labels 09:30–09:44). Executable volumes; split artifact disclosed (§3.3, §13.9 item 13). | Executable 1-min + signal daily | Confirmation component |
| Opening range | **09:30–09:40** high/low = max high / min low over bars labeled **09:30 through 09:39** (10 bars; **all ten required — §19 item 2**) | Executable 1-min | Confirmation component (window strictly before the 09:44 evaluation bar so the break test is satisfiable; window ASSUMPTION / MUST TEST) |
| 20-day relative strength | `close(T−1)/close(T−21) − 1` minus the same for SPY | Signal daily | Ranking component |

All indicator parameters **ASSUMPTION / MUST TEST**.

### §6.2 Excluded non-signal tools

MACD, Bollinger Bands, Fibonacci levels, trendlines: excluded from the deterministic path; LLM narrative context only, labeled non-signal. The manual TradingView chart card (EMA 20/50/200, RSI 14, ATR 14, VWAP, volume vs 20-day, MACD 12/26/9 visual, Fib 38.2/50/61.8 visual) is a user deliverable, non-normative.

---

## §7. Signal Engine

### §7.1 Canonical pipeline order (NORMATIVE, exact)

```text
Stage 0 (global preconditions, in order):
  S0.0  Pending-entry resolution (NEXT_SESSION only when scheduled today):
        compute §5.5 from SPY 09:44 executable bar vs T−1 official close.
        If required SPY filter data is unavailable under §19 item 2 →
          pending candidate expires unfilled; log ENTRY_UNFILLABLE_NO_FILTER.
        Else if opening_return ≤ −0.02 →
          candidate is voided; no position, no accounting, no exit evaluation
          for that candidate; log NEXT_SESSION_VOIDED.
        Else →
          the previously scheduled 09:30 official-open fill stands; the
          resulting position is exit-eligible from this day's 10:00 S0.2.
        The §5.5 computation required by S0.0/S0.4 occurs irrespective of
        whether S0.3 would otherwise end the pipeline.
  S0.1  G2 data freshness (global) — failure → DATA DEGRADED, halt
  S0.2  Exit evaluation on the open position (§8.6).
        If ANY exit trigger fires → no new entry is evaluated this scan
        (N-10); pipeline ends after exit processing.
  S0.3  max_positions check: if a position is open (and remains open) →
        no new entry; pipeline ends.
  S0.4  Opening-drop filter (SPY, global, §5.5): compute or reuse the day's
        §5.5 result; if tripped → suppress all ordinary same-day BUY output
        for the day; pipeline ends.
  S0.5  Regime check: RISK_OFF → no new entries; exit/trim advisories only.

Stage 1 (per ticker, no sizing needed):
  G3, G4, G5, G6, G7, G8

Stage 2:
  score all Stage-1 survivors (integer scores, N-13)
  discard scores < active regime cutoff

Stage 3:
  rank by integer score descending; tie-break ascending ticker (N-13)

Stage 4 (sizing-dependent gates, in rank order):
  for each candidate:
    compute sizing (§9.1): risk_dollars, raw_notional,
    G10: raw_notional ≥ $10.00 (reject if not)
    notional, shares_est, realised_risk (screening)
    G9:  fees_rt(notional) / realised_risk ≤ 0.25   (per N-08, §9.4)
    first candidate passing G10 and G9 → selected BUY CANDIDATE; stop
  if none passes → WATCH / NO ACTION

Stage 5 (publication):
  if the selected candidate's ticker is NEWS_UNVERIFIED at this scan
  (trigger sets: §11.2) → downgrade to WATCH / DATA-UNVERIFIED; firm BUY
  blocked. Stage 4 is NOT resumed for lower-ranked candidates (DECIDED):
  the day produces no BUY.
```

### §7.2 Hard gates

Binary; any failure rejects; no compensation by score.

| Gate | Rule |
|---|---|
| G2 Data freshness | §3.4; global |
| G3 Universe/tradeability | Ticker in current `universe.yaml`; live requires `pluang_confirmed`; backtest may include unconfirmed tickers only with disclosed bias |
| G4 Trend | `close(T−1) > EMA50(T−1)` AND `EMA50(T−1) > EMA200(T−1)`, signal daily |
| G5 Overextension | `RSI14(T−1) ≤ 75` — ASSUMPTION / MUST TEST |
| G6 Earnings blackout | Let `T` be the decision date. Map each earnings event `e` to trading session `d(e)`: provider timing `before-market-open` → trading day of the event calendar date; `after-market-close` → next trading day; `unspecified` → trading day of the event calendar date. **If an event calendar date used by the before-market-open or unspecified branch is not a trading day, `d(e)` is the next trading day.** G6 **fails iff** any event satisfies `d(e) ∈ {T, T+1, T+2}` in trading-day-index terms. ETFs: N/A, passes. **Coverage-gap rule unchanged:** earnings coverage is determined from the run-pinned `coverage_manifests` records with `source_kind = EARNINGS` (§16). Where no verified manifest-recorded covered span contains the simulated date for a ticker, G6 **passes** and `G6_DISABLED_COVERAGE` is logged per ticker-bar; mandatory sensitivity pair (earnings-gate-on vs off over manifest-recorded covered spans). **DISCLOSED BIAS:** historical earnings dates are as-finally-revised, not as knowable at simulated time T. |
| G7 News veto (consolidated; per-ticker scope; windows per N-21 against scan time `t`) | Fails iff any of the following is **active** for the ticker at `t` after the ordered §11.2 mapping: (a) BEARISH-CRITICAL — active under the explicit §11.2 trading-session inequality from `published_at` through the official close of its fifth counted trading session; (b) `category = M&A AND ma_role = TARGET` — active 30 calendar days from `published_at`, refreshed by any new headline that **maps to the M&A-target branch** under §11.2; (c) BEARISH-HIGH — active 24 hours from `published_at`. `confidence < 0.85` does **not** suppress these vetoes (FP-3). All scopes **per-ticker**; no global news block exists (the only global suppressions are DATA DEGRADED and the opening-drop filter). |
| G8 Intraday bias | Evaluated only after the ticker survives §19 item 2's entry-side required-bar rule; close of executable 09:44 bar > session VWAP computed over executable bars actually returned within 09:30–09:44 (zero-volume guard: §6.1) |
| G9 Fee Gate A | `fees_rt(notional) / realised_risk ≤ 0.25`, per §9.4 and N-08 — threshold MUST TEST |
| G10 Sizing viability | `raw_notional ≥ $10.00` — rejection, not clamp-up |

The global `min_viable_capital` gate has been **REMOVED** and must not be resurrected.

### §7.3 Gate B

**REMOVED from the active baseline; DEFERRED.** No defensible estimator of `expected_gross_edge` exists at current sample sizes without same-window fitting. Reintroduction requires a pre-registered estimator trained strictly on prior walk-forward windows. No shadow-mode logging of an undefined quantity.

### §7.4 Score components (closed forms — NORMATIVE)

Let `P = close of executable 09:44 bar`, `V = session VWAP over executable bars actually returned within 09:30–09:44`, `a = ATR14(T−1)/close(T−1)` (signal series). **P-1 availability rule:** Intraday/Volume components are evaluated only for tickers that survived the §19 item 2 entry-side required-bar exclusion; opening range uses all ten required 09:30–09:39 bars, while VWAP and session pace use returned bars in 09:30–09:44.

| Component | Points | Closed-form rule |
|---|---|---|
| Trend quality | 30 | `EMA20(T−1) > EMA50(T−1) > EMA200(T−1)` → +10; `close(T−1) > EMA20(T−1)` → +10 (signal daily); 20-day RS vs SPY > 0 (§6.1) → +10 |
| Momentum | 25 | `RSI14(T−1) ∈ [50, 70]` → +15; `RSI14(T−1) − RSI14(T−6) > 0` → +10 |
| Intraday | 25 | VWAP distance: $15 \times \min\!\big(1, \max(0, \tfrac{P-V}{V}) / a\big)$; opening-range break: `P > OR_high(09:30–09:40)` AND session pace > 1.2 → +10 |
| Volume | 10 | mean of `close×volume` over days T−10..T−1 > mean over T−20..T−11 (signal daily) → +5; session pace ≥ 1.0 → +5 |
| Catalyst modifier | ±10 | Deterministic sum per §11.2 over source-independent distinct `headline_hash` values after the same-`(headline_hash, ticker)` effect-field integrity assertion; clipped to $[-10, +10]$ |

Maximum attainable score is 100. All weights **ASSUMPTION / MUST TEST**; walk-forward perturbation check required.

**Rounding (N-13):** `score_final = floor(raw_score + 0.5)`. All cutoffs, bands, and ranking use `score_final`.

### §7.5 Entry cutoffs

```text
RISK_ON:          score_final ≥ 70
NEUTRAL/VOLATILE: score_final ≥ 80
```

Both **ASSUMPTION / MUST TEST**. No candidate below the active cutoff is ever published.

### §7.6 Display bands

```text
Strong:   score_final ≥ 85
Moderate: active cutoff ≤ score_final ≤ 84
```

Integer scores make bands exhaustive above the cutoff. Marginal band removed. Raw and final scores logged; Telegram shows band only.

---

## §8. Action Vocabulary & Exit Logic

### §8.1 Action vocabulary

`BUY CANDIDATE · WATCH · HOLD · TRIM · EXIT · NO ACTION`

### §8.2 Entry conditions

A BUY CANDIDATE requires: all applicable gates pass; `score_final ≥` active cutoff; Stage-4 viability; canonical sizing; position count allows entry (Stage 0). Baseline: `max_positions = 1`; if a position is open, no new BUY; one BUY CANDIDATE maximum per day; **no new entry on any scan at which an exit trigger fires** (N-10).

### §8.3 Adopted positions

Holdings appearing in a screenshot without a system entry are tagged `ADOPTED`, receive the synthetic stop of §8.7, and are disclosed as synthetic in every message.

### §8.4 Canonical stop (reference-anchored — NORMATIVE)

Anchor: **`entry_reference_price`** (N-06) — close of the executable 09:44 bar at the decision scan.

```text
ATR        = ATR14 (Wilder), signal series, last completed daily bar (T−1)
stop_pct   = stop_mult × ATR / close(T−1)           [signal-series ratio]
stop_price = entry_reference_price × (1 − stop_pct)  [executable space]
```

- `stop_mult = 2.5` — **ASSUMPTION / MUST TEST**.
- Fixed at decision time; not trailing; percentage form everywhere; no absolute-dollar variant.
- The stop **exists before any fill** — it is what Telegram publishes, what the live user is given, and what all voiding tests (§13.2) and exit comparisons use. Level parity live↔backtest is exact by construction.
- Never re-anchored to the fill price. `realised_risk_actual` therefore diverges from screening `realised_risk` whenever fill ≠ reference (N-07); both logged; divergence distribution reported (§15.2).
- A fill at or beyond the stop level is a defined state (`ENTERED_BEYOND_STOP`, §13.2).

### §8.5 Take-profit

```text
target_price = entry_reference_price × (1 + 2 × stop_pct)
```

2× initial risk distance, same anchor. **ASSUMPTION / MUST TEST**.

### §8.6 Exit conditions

Evaluated only at scheduled scans. For an open position, §19 item 2 first determines whether exit evaluation is suppressed: if at least one executable 1-min bar exists in that session with a label at or before the scan's N-02 availability boundary, the **evaluation price** is the close of the **last available** such bar and evaluation proceeds; otherwise log `EXIT_EVAL_SUPPRESSED`. The normal boundary bars are 09:44 at 10:00, 15:14 at 15:30, and 11:44 at half-day 12:00, but an earlier last-available bar is valid when the boundary bar is absent. Priority when simultaneous, highest first:

1. **Stop breach** — evaluation price ≤ `stop_price` → EXIT.
2. **Regime → RISK_OFF** — detected at 08:15 → EXIT advisory at the **next non-suppressed exit-evaluating scan of that session** (§19 item 2) (10:00 / 15:30 / half-day 12:00). (Unavailable on globally excluded days — §19 item 2.)
3. **BEARISH-CRITICAL news** — for a classification that reaches the BEARISH-CRITICAL branch after §11.2's ordered category precedence, EXIT advisory subject to the two-source rule (§11.3); flagged for human confirmation live. In backtest, only a **confirmed** (two-source) CRITICAL triggers this exit. `confidence < 0.85` retains exit-trigger participation (FP-3).
4. **Target reached** — evaluation price ≥ `target_price` → EXIT.
5. **Trend failure** — evaluated **per open position**, with origin at that position's **entry fill**. It fires at the **earliest scheduled exit-evaluating scan** (10:00 / 15:30 / half-day 12:00) at which (a) the position is open, (b) exit evaluation is not suppressed under §19 item 2, and (c) the **two most recent completed daily signal bars** both satisfy `close < EMA50`. That qualifying scan is the detection scan for §13.3. The trigger is **not** a once-per-ticker event, and a suppressed scan does **not** disarm it; the condition is reevaluated at the next scheduled non-suppressed exit-evaluating scan while the position remains open. **ASSUMPTION / MUST TEST**.
6. **BEARISH-HIGH news** — TRIM advisory only; no-op in the single-position baseline backtest. **DECIDED**, disclosed simplification.

Exits close the **entire position** (no partial exits in baseline).

**Detection semantics:** exit evaluation is **close-based at scan times only**. An intra-scan breach that fully recovers before the next evaluation bar close is **never detected**, live or backtest — a disclosed structural property (§13.9 item 11). Stops are checked exactly twice per trading day: the 10:00 scan and the 15:30 scan (12:00 on half-days). Gap-through **price** handling: §13.5.

### §8.7 Adopted-position synthetic stop

```text
synthetic_stop  = reference_price × (1 − stop_mult × ATR14(T−1) / close(T−1))
reference_price = extracted average cost if available, else close(T−1) (executable)
```

Tagged `SYNTHETIC` everywhere. **ASSUMPTION / MUST TEST**.

### §8.8 Fee-derived minimum stop

None exists. Gate A fully serves the purpose: a stop too tight relative to fees fails G9. **REMOVED / DECIDED.**

---

## §9. Position Sizing / Fee & Risk Logic

### §9.1 Canonical sizing (decision time)

```text
risk_dollars = portfolio_value × risk_per_trade × regime_multiplier
raw_notional = risk_dollars / stop_pct

if raw_notional < $10.00 → REJECT at G10
   ("position too small to be fee-viable at current risk settings")

notional      = min(raw_notional, 0.90 × portfolio_value)
shares_est    = notional / entry_reference_price     [screening only]
realised_risk = notional × stop_pct                  [screening quantity, N-07]
```

- `risk_per_trade = 1.0%`, `regime_multiplier` (§5), 90% deploy cap, $10 rejection threshold — all **ASSUMPTION / MUST TEST**.
- Fractional shares allowed. The only clamp is the downward 90% cap. No clamp-up exists.

### §9.2 Fill semantics (NORMATIVE — N-03/N-04/N-05)

```text
shares_filled        = truncate(notional / fill_price, 4 dp)   [precision MUST CONFIRM WITH BROKER]
notional_actual      = shares_filled × fill_price               (≤ notional; residual stays in cash)
fees                 = fee model applied to notional_actual and shares_filled
realised_risk_actual = shares_filled × (fill_price − stop_price)  [signed; logged post-fill]
```

Live instruction parity: the user is instructed to place a **dollar-notional** order for `notional` (consistent with Pluang's $1-minimum fractional dollar orders).

Gate A remains a **pre-trade screen** on screening quantities (N-08). Post-fill actuals are logged and reported; they are not re-gated.

### §9.3 Realised-risk semantics

Gate A always uses screening `realised_risk` after all applied clamps, never nominal `risk_dollars`. If the 90% cap binds, `realised_risk < risk_dollars` — acceptable. If `raw_notional < $10`, reject. Actual loss at the stop is `realised_risk_actual` and can exceed screening risk when the fill is above the reference price, and further under gap-through (§13.5). `realised_risk_actual ≤ 0` occurs iff the fill lands at or below the stop (`ENTERED_BEYOND_STOP`, §13.2). All disclosed in reporting.

### §9.4 Gate A

```text
burden = fees_rt(notional) / realised_risk       [screening, N-08]
pass:  burden ≤ 0.25                              [threshold ASSUMPTION / MUST TEST]
```

The Telegram fee metric is the identical quantity.

### §9.5 Candidate viability; removed global minimum capital

The `$25` constant and the global `min_viable_capital` gate remain **REMOVED**. Viability is candidate-specific via G10 and G9. If all candidates fail:

```text
NO FEE-VIABLE CANDIDATES at current capital —
reasons per candidate: [G9/G10];
accumulate capital or await setups with wider viability.
```

An informational capital estimate may appear, labeled `ILLUSTRATIVE`; it never gates. **§1.3.1 applies verbatim**: at $61 this message is the *expected* output on most days.

### §9.6 Canonical fee model

**Pluang components, per side (buy and sell):**

```text
Transaction fee = 0.30% × notional_side
JFX & KBI fee   = min(0.05% × notional_side, $0.10)
```

**Sell-side regulatory pass-throughs:**

```text
SEC = max($0.0000206 × sell_notional, $0.01)
TAF = max($0.000166 × shares, $0.01), cap $8.30
CAT = max($0.0000265 × shares, $0.01)
```

Published rate formulas: **KNOWN for the current schedule**. Receipt-level behavior, rounding scope, and **fractional-order applicability of the $0.01 minimums**: **MUST CONFIRM WITH BROKER**. For historical spans, SEC/TAF/CAT are effective-dated per §9.7; the broker transaction fee, JFX/KBI fee, and VAT rate are **not** effective-dated in this baseline and are backward-projected as current constants across the full backtest window (**MUST CONFIRM for historical applicability; DISCLOSED in §13.9 item 18**).

**VAT branch:** VAT 11% applies to the transaction fee and JFX/KBI fee only; not to SEC/TAF/CAT, in the canonical branch:

```text
vat_on_regulatory = false   (canonical)
vat_on_regulatory ∈ {false canonical, true sensitivity}
```

Status: **DECIDED interpretation / MUST CONFIRM WITH BROKER** (explicitly not KNOWN).

**Rounding:**

```text
fee_rounding = CEIL_CENT_PER_COMPONENT   (canonical)
fee_rounding ∈ {CEIL_CENT_PER_COMPONENT, CEIL_CENT_PER_SIDE, EXACT}
```

Rounding applies per component (or per side) **after** VAT where VAT applies; schedule-level $0.01 minimums and the TAF cap apply **before** rounding (schedule terms, not rounding artifacts) **only when the regulatory component is actually evaluated from a verified rate**. A §9.7 `TRAIN_SUBSTITUTED_ZERO` component is omitted from the schedule formula entirely and therefore cannot be lifted above $0.00 by a minimum, cap, VAT, or rounding branch. Sensitivity run across all three branches required. **DECIDED canonical branch / MUST CONFIRM WITH BROKER.** The §1.4 six-branch table is the Phase-0 smoke-test target set.

### §9.7 Effective-dated regulatory fee rates

`fee_schedule.yaml` stores effective-dated entries. **Three historical entry states:**

```yaml
- component: CAT
  rate: 0.0000265        # verified rate
  effective: [from, to]
  verified: true

- component: CAT
  applicable: false      # component verifiably did not apply / was not passed through
  effective: [from, to]
  verified: true         # VERIFIED ZERO, not a gap
# absence of any entry for a date = UNVERIFIED → PROVISIONAL
```

Rules:

1. **Fee-computation date (Audit A P1 — NORMATIVE).** The fee-computation date for a regulatory component is the trading date of the fill being priced — entry fill date for buy-side components, exit fill date for sell-side components — and the **decision date** for the Gate-A/G9 screening estimate (N-08). Rate selection and verification-state determination use the **same fee-computation date**. `fee_schedule_version` is recorded for every resulting simulated fee computation/trade. There is no entry-date substitution for an exit-side fee computation.
2. Only verified, dated entries from SEC/FINRA/CAT fee-rate notices; no backward projection; component pass-through **start dates** are **MUST CONFIRM** from public notices and must be encoded as `applicable: false` verified-zero spans once confirmed.
3. **Unverified-state treatment and train-selection-only substitution (R2.3 EB-01 + Audit A P2/P3/P5 — NORMATIVE).** A fee-computation date lacking either (a) a verified dated rate or (b) a verified `applicable: false` entry for a required SEC/TAF/CAT component remains `PROVISIONAL / FEE-SCHEDULE-UNVERIFIED`. The absence is never interpreted as a historical fact, never backfilled with another period's rate, and never written into `fee_schedule.yaml` as a verified entry. `coverage_manifests` records the corresponding `FEE_SCHEDULE` research coverage for reconstruction (§16), but a coverage attestation **never substitutes** for a verified rate or verified `applicable: false` entry in `fee_schedule.yaml`.

   During §15.1 **train-window simulation only**, whenever a required SEC/TAF/CAT component has no verified entry for its fee-computation date, that component is assigned fee-input provenance `TRAIN_SUBSTITUTED_ZERO` and contributes **exactly $0.00** solely for that train-window fee computation. This applies to every affected train-window regulatory-fee computation, including:
   - the Gate-A/G9 round-trip screening estimate on the decision-date fee-computation date; and
   - actual sell-side regulatory fees for any train-window exit, including normal exits, window-end force-closes, and delisting/symbol-change force-closes, using the exit fill date as the fee-computation date.

   A `TRAIN_SUBSTITUTED_ZERO` component is **omitted entirely from the §9.6 schedule formula**: the $0.01 schedule minimum does not apply; the TAF cap does not apply; VAT does not apply; and no `fee_rounding` branch may turn the substituted zero into a non-zero fee. The corresponding `fee_calculations` row records `base_amount = 0`, `vat_amount = 0`, `rounded_amount = 0`, the affected component, fee-computation date, fee context, `fee_schedule_version`, and `fee_input_status = TRAIN_SUBSTITUTED_ZERO`.

   The substitution:
   - does **not** modify `fee_schedule.yaml`;
   - does **not** create or imply a verified-zero historical record;
   - does **not** change the date/span from PROVISIONAL to verified;
   - does **not** change `fee_schedule_version`;
   - is a **research-selection convenience**, not an assertion that the component was historically zero;
   - may bias train-window parameter selection because a historically nonzero regulatory component may have been modeled as zero;
   - is a property of the **simulation run role**, never of the calendar date; and
   - is permitted only when `run_role = TRAIN`.

   Because train windows overlap prior test windows under the §15.1 24/6/6 schedule, the same calendar date may legitimately use `TRAIN_SUBSTITUTED_ZERO` in a train run while a separate test run treats that date according to its own verified fee state. A test-window or live run must never write `TRAIN_SUBSTITUTED_ZERO`; any attempted write is a deterministic engine exception and halts under §19 item 6. Gate-level and trade-level provenance is recorded per §16, and every resulting simulated trade whose screening or actual exit fee computation used substitution carries `TRAIN_FEE_SUBSTITUTED`.
4. **Window-level verification granularity (RP-05 + Audit A P4 — NORMATIVE):** a test window is **fee-verified** iff **every trading day within it** has a verified `fee_schedule.yaml` entry (rate or verified `applicable: false`) for **every applicable component**. Partially fee-verified test windows are excluded from the §15.1 verified-window count, from criterion 1's denominator, and from criteria 2–3 aggregates. They are reported separately as PROVISIONAL by **enumerating the affected test window and its unverified spans/days only; no simulated metrics are produced for that window**. `TRAIN_SUBSTITUTED_ZERO` is never applied to a test-window run. No partial-window trade subsetting is permitted. In §13.8 / §20 Phase 3, **"provisional fee-span segregation" means this enumeration**.
5. **Coverage-vs-windows resolution:** if fee-verified coverage (jointly with §3.6 corporate-actions verification and the N-25 resolvability filter) yields fewer than 8 eligible test windows, the acceptance verdict is **NOT EVALUABLE** — not pass, not fail — and progression to paper trading is blocked pending completion of the fee/corporate-actions history research or resolution of the required daily executable-bar availability. This is the same verified-window count consumed by §15.1/§15.4.
6. **Live:** a fee-computation date with no verified entry blocks BUY output with reason `FEE_SCHEDULE_UNVERIFIED`. `TRAIN_SUBSTITUTED_ZERO` is categorically unavailable in live operation.
### §9.8 Dividend withholding

`dividend_withholding_rate` default 30%; treaty rate **MUST CONFIRM**. Applied to dividend cash in strategy accounting **and to the benchmark identically** (§15.3). Dividend events per §3.6.

### §9.9 FX conversion cost (basis fixed — N-15)

- `initial_capital` ($61 / $200) is **post-FX USD**. The trading simulation is FX-invariant.
- FX enters as a **reporting layer**: IDR-basis performance applies `fx_cost_pct` once on deposit and once on withdrawal:

$$\text{IDR-basis multiple} = \frac{\text{equity}_T}{\text{initial\_capital}} \times (1 - \text{fx\_cost\_pct})^2$$

- `fx_cost_pct` default 0.5% one-way; grid `{0.25%, 0.5%, 1.0%}` mandatory. **ASSUMPTION / MUST MEASURE DURING PAPER TRADING.** FX hedging out of scope.

---

## §10. Correlation / Portfolio Risk

`max_positions = 1`. Regime caps `5/3/2` **NOT ACTIVE**. Correlation logic recorded for future use, **DORMANT**:

```text
Pearson r of daily log returns; lookback 20 days; min obs 15; reject r > 0.7
```

All parameters **MUST TEST**. Activates only after an aggregate portfolio-risk admission rule for positions ≥ 2 is specified and validated — **UNRESOLVED / DEFERRED**. Correlation must not substitute for that rule. Capital feasibility alone never becomes risk permission.

---

## §11. News Engine

### §11.1 Schema, clocks, and ingestion (NORMATIVE)

Schema `news_schema_v3`; strict JSON:

```json
{
  "ticker": "...",
  "category": "EARNINGS | GUIDANCE | ANALYST | M&A | REGULATORY | LEGAL | PRODUCT | MACRO | INSIDER | CAPITAL_RETURN | OTHER",
  "direction": "BULLISH | BEARISH | NEUTRAL",
  "severity": "LOW | MEDIUM | HIGH | CRITICAL",
  "ma_role": "TARGET | ACQUIRER | NEITHER",
  "confidence": 0.0,
  "published_at": "...",
  "headline_hash": "...",
  "source": "...",
  "keyword_override": false
}
```

`ma_role` is part of the classification label set. When `category = M&A`, `ma_role` is required and must be one of `TARGET | ACQUIRER | NEITHER`; for every non-M&A category it must equal `NEITHER`. `keyword_override` is a deterministic boolean set by §11.3; it is `false` unless the keyword fallback fires.

**Headline identity (FP-4 — NORMATIVE):**

```text
normalized_headline_text =
    NFKC(headline_text)
    → Unicode casefold
    → collapse each Unicode-whitespace run to one ASCII space
    → strip leading/trailing whitespace
    → strip leading/trailing Unicode punctuation (General Category P*)

headline_hash = SHA-256(UTF-8(normalized_headline_text))
```

`headline_hash` is **source-independent**. **P-4 classification-input rule:** the effect fields `category`, `direction`, `severity`, `ma_role`, `confidence`, and `keyword_override` are determined from **normalized headline text and `ticker` only**; `source` is never a classification input. Source identity remains a separate field used only for the LLM cache identity (§11.5) and the two-source confirmation rule (§11.3). The normalized text and raw-headline inventory are persisted per §16.

**Two clocks exist and are never interchangeable (N-21):**

- **Classification time** — wall-clock time at which a headline is classified (live fetch, or the Phase-2 cache-population job). Classification time has **no effect on any trading decision**.
- **Consumption timestamp `t`** — the simulated (or live) decision timestamp of the scan consuming the classification. **All window and staleness tests are evaluated against `t`.**

**Ingestion validity:**

- A headline with **missing `published_at`** is dropped at ingestion, logged `HEADLINE_UNTIMED`, produces no effect, and **never** raises NEWS_UNVERIFIED.
- **Backtest cache population:** the Phase-2 job classifies **every timed headline in the run-pinned, manifest-recorded covered Finnhub span (§11.6), irrespective of wall-clock age**. No age-based exclusion exists at classification time.
- **Live ingestion guard (live only):** a headline first observed live with `fetch_time − published_at > 7 calendar days` is not classified (guard against provider backfill floods); it produces no effect and never raises NEWS_UNVERIFIED. **Disclosed live↔backtest divergence** (§13.9 item 15): such late-delivered headlines are effect-less live but effect-bearing in backtest via their §11.2 windows.

**Consumption rule:** a headline participates in a **mapped trading effect** at scan `t` iff `published_at ≤ t` and `t` lies within that effect's window (§11.2). There is **no generic wall-clock staleness test at consumption**; mapped effect windows are the only time bounds for effect behavior. **NEWS_UNVERIFIED has its own trigger-scope rule:** every timed classification carries a 24-hour trigger window `0 ≤ t − published_at ≤ 24 h` in addition to any mapped G7 activation window, irrespective of whether the mapped branch contributes score points (§11.2).

Confidence threshold **0.85** — **ASSUMPTION pending calibration**. Its operative behavior is **DECIDED**: for any timed classification with `confidence < 0.85`, (a) its score contribution is forced to **0**; (b) any G7 veto and any §8.6 / §11.3 exit-trigger participation assigned by the ordered mapping are **retained**; and (c) the ticker raises NEWS_UNVERIFIED whenever the universal 24-hour NEWS_UNVERIFIED trigger window or an applicable mapped G7 activation window overlaps `t` (§11.2). The universal 24-hour trigger applies even to `category ∈ {MACRO, OTHER}`, BULLISH LOW, and all NEUTRAL classifications. Low confidence changes neither the mapped veto branch nor deterministic exit participation.

### §11.2 Deterministic effect mapping and NEWS_UNVERIFIED trigger sets (NORMATIVE)

The LLM never decides trading effects. Code applies the mapping below, with all windows evaluated against `t` per N-21 and all blocking scopes **per-ticker**.

**Ordered total-function rule (FP-2 — NORMATIVE):** the effect mapping is evaluated as an ordered decision procedure and is a total function of `(category, direction, severity, ma_role)`:

1. If `category ∈ {MACRO, OTHER}` → **0 points, no G7 veto, and no news-driven existing-position action**. This category branch takes precedence over every `(direction, severity)` branch.
2. Otherwise evaluate the three veto branches in the order shown below: BEARISH CRITICAL; `category = M&A AND ma_role = TARGET`; BEARISH HIGH. The first matching branch is the mapped effect.
3. Otherwise evaluate the score branches keyed on `(direction, severity)`; BULLISH LOW and all NEUTRAL classifications map to 0.

**BEARISH-CRITICAL five-trading-session window (E-06 — NORMATIVE clarification).** Let `d0` be the first exchange trading session whose **official close timestamp (N-22) is at or after `published_at`**; for a publication after a session's official close, `d0` is therefore the next trading session. Let `d4` be the session four trading-day indices after `d0`. The BEARISH-CRITICAL G7 window is active at scan `t` iff `published_at ≤ t ≤ official_close(d4)`. Thus `d0` is counted as trading day 1 and `d4` as trading day 5. N-21 still independently requires `published_at ≤ t` for consumption.

**First-match exclusivity is intentional (Audit B CP-1 — NORMATIVE clarification).** Where branches overlap, only the first matching branch governs effect behavior; effects do not union. Therefore: (a) a classification that is both BEARISH CRITICAL and `M&A/TARGET` maps only to BEARISH CRITICAL, so its G7 veto runs through the close of the 5th trading day and its existing-position behavior is the two-source-gated EXIT advisory, not the 30-calendar-day M&A-target HOLD behavior; and (b) a BEARISH-CRITICAL classification with `category ∈ {MACRO, OTHER}` is consumed by step 1 and therefore has no G7 veto or news-driven existing-position EXIT. This clarification changes no ordered mapping; it documents its intended overlap consequences.

| Ordered branch | Window (relative to `published_at`) | New BUY | Existing position | Score points |
|---|---|---|---|---:|
| `category ∈ {MACRO, OTHER}` — any direction/severity | — | — | none | 0 |
| BEARISH CRITICAL | `published_at ≤ t ≤ official_close(d4)` with `d0/d4` defined above | G7 veto | EXIT advisory per two-source rule (§11.3) | — (vetoed) |
| `category = M&A AND ma_role = TARGET` | 30 calendar days, refreshed by a new headline that maps to this branch | G7 veto | HOLD advisory with decoupling disclosure | — (vetoed) |
| BEARISH HIGH | 24 hours | G7 veto | TRIM advisory (backtest no-op) | — (vetoed) |
| BEARISH MEDIUM | scoring window | — | none | **−10** |
| BEARISH LOW | scoring window | — | none | **−5** |
| BULLISH HIGH or CRITICAL | scoring window | — | none | **+10** |
| BULLISH MEDIUM | scoring window | — | none | **+5** |
| BULLISH LOW / NEUTRAL | — | — | none | 0 |

**Scoring window:** `0 ≤ t − published_at ≤ 24 h`. **Aggregation:** sum points over qualifying headlines by distinct, source-independent `headline_hash`; duplicates counted once; clip to $[-10, +10]$. The 24-hour scoring window and the longer G7 activation windows are **independent tests against `t`**; a headline may be outside the scoring window yet inside its G7 window (e.g., a day-20 M&A-target veto with zero score contribution). **For NEWS_UNVERIFIED trigger purposes only, every timed classification carries this same 24-hour window whether or not its mapped branch contributes score points. A `—` in the effect table's Window column governs mapped effect behavior only and never suppresses the 24-hour NEWS_UNVERIFIED trigger scope.**

**P-4 same-hash classification consistency (NORMATIVE).** A classification's **effect fields** (`category`, `direction`, `severity`, `ma_role`, `confidence`, `keyword_override`) are a function **only** of normalized headline text and `ticker`. `source` is **never** an input to classification; it exists only for §11.5 cache identity and §11.3 two-source confirmation. Therefore, all cache entries sharing the same `(headline_hash, ticker)` must carry identical effect fields, and source-independent deduplication by `headline_hash` is deterministic. If a run observes two cache entries with the same `(headline_hash, ticker)` but differing effect fields, classify this as a **cache-integrity failure**, mark the run **non-canonical**, and halt under §19 item 6.

`confidence < 0.85` is **not** a mapping discriminator. The ordered branch above is still determined normally; FP-3 forces any nonzero score contribution from the affected classification to 0 while retaining the branch's G7 veto and §8.6 / §11.3 exit-trigger participation. FP-3(c) trigger scope is independent of whether the mapped branch itself has a score-bearing window.

**NEWS_UNVERIFIED trigger sets (RP-02 + FP-3/FP-5 — NORMATIVE, exhaustive):**

- **Backtest:** ticker `T` is NEWS_UNVERIFIED at scan `t` **iff** (a) any cached timed classification for `T` has `confidence < 0.85` and either its universal 24-hour trigger window `0 ≤ t − published_at ≤ 24 h` or an applicable mapped G7 activation window overlaps `t`; **or** (b) a timed headline for `T` present in `news_headlines` within a run-pinned verified `NEWS` covered span (§11.6) has **no cache entry** under `(headline_hash, source, schema_version, model_version)` and lies within the universal 24-hour trigger window. For trigger (b), no mapped branch exists yet, so the 24-hour trigger window is the **sole** applicable window test. A cache miss is a population-job failure requiring cache repair before the run is canonical.
- **Live:** additionally, malformed JSON or LLM call failure at classification of a headline whose scoring or G7 window overlaps `t`.
- **Never NEWS_UNVERIFIED:** untimed headlines; headlines with `published_at > t`; headlines outside both the universal 24-hour trigger window and every applicable mapped G7 activation window; live-guard-excluded headlines; **coverage gaps** — a span lacking a verified NEWS coverage attestation is handled solely by §11.6 neutral-disable and is never NEWS_UNVERIFIED.

**Effects of NEWS_UNVERIFIED:** new BUY → Stage-5 downgrade to WATCH / DATA-UNVERIFIED, no Stage-4 resumption, no BUY that day. Existing position → deterministic exit logic proceeds unaffected. For low-confidence classifications, score contribution = 0 while mapped G7 vetoes and mapped exit-trigger participation remain active.

### §11.3 Catastrophic keyword fallback and confirmation rule

Deterministic keyword list (`"SEC investigation"`, `"restatement"`, `"withdraws guidance"`, `"accounting fraud"`, `"delisting"`). Matching is **case-insensitive substring matching against NFKC-normalised headline text**: NFKC-normalise the headline and keyword, casefold both, then test substring presence. A match forces `severity = CRITICAL` **and `direction = BEARISH`** regardless of LLM output (false-negative guard). The override is applied **at classification time before §11.2 mapping** and persisted in the cached payload as `keyword_override: true`; cache-only backtest replay therefore reproduces it without headline-time LLM calls (§21 item 18).

**False-positive guard (whitelist branch deleted):** a CRITICAL-triggered **EXIT advisory** requires **two independent classifications that each reach the BEARISH-CRITICAL branch after §11.2's ordered category precedence**: distinct `headline_hash` **AND** distinct `source`, same ticker, both windows overlapping `t`. Because `headline_hash` is source-independent, identical syndicated headline text does not become independent confirmation merely by appearing at two sources. **`source` participates only in this confirmation rule and cache identity; it never changes the classification effect fields (§11.2 P-4).** If the rule is unmet: no EXIT advisory; message `CRITICAL — UNCONFIRMED (single source) — manual review advised`. The **G7 BUY-veto applies regardless** of confirmation status. `confidence < 0.85` does not remove either G7-veto or exit-trigger participation; the two-source rule still governs whether the exit fires. In backtest, only confirmed CRITICALs trigger the §8.6 priority-3 exit.

### §11.4 Calibration requirement

Before the news gate is live: validation against ≥ 200 manually labeled historical headlines; accuracy and confidence-calibration report; Phase-2 exit gate. The **pre-registered label set includes `category`, `direction`, `severity`, and `ma_role`**; `ma_role` must obey the §11.1 conditional validity rule. Threshold 0.85 remains **ASSUMPTION pending calibration**. `keyword_override` is deterministic code behavior and is not a human classification label.

### §11.5 LLM reproducibility

Pinned identifier pattern `openrouter/<provider>/<model>@<version>`; fixed in config; `llm_config_version` tracked; single-provider routing; temperature 0; strict JSON with `schema_version`. Cache key: **`(headline_hash, source, schema_version, model_version)`**. **`source` is cache-identity metadata only, not classifier input; all source-keyed entries sharing `(headline_hash, ticker)` must satisfy the §11.2 P-4 effect-field equality assertion.** **Backtests replay from cache only; a backtest never issues a live LLM call.** The cache is populated by the Phase-2 cache-population job (§20), the only authorized bulk live-call context, which classifies all timed headlines in run-pinned verified NEWS covered spans irrespective of wall-clock age (N-21). Cached payloads persist `ma_role` and `keyword_override`; model upgrades increment `llm_config_version`; historical caches are never overwritten.

[MISSING SOURCE CONTENT — CANNOT CANONICALIZE] Exact pinned model identifier — must be set in config; blocks Phase 2 completion.

### §11.6 Historical news / earnings coverage and covered-span semantics

`coverage_manifests` (§16) is the sole coverage-attestation source for NEWS and EARNINGS. A **covered span** is a `[span_start, span_end]` span recorded for the relevant ticker and `source_kind` in the **run-pinned `manifest_version`** with `verified = true`. Each run logs the manifest version(s) used. Absence of a verified manifest attestation is a **coverage gap**; absence of a headline or earnings event inside a verified covered span is not a coverage gap.

Where a verified NEWS covered span does not include a backtest date for a ticker: G7 passes, news score modifier = 0, and the disabled state is logged per ticker-bar. **A coverage gap is never NEWS_UNVERIFIED** (§11.2). Mandatory sensitivity pair: news-on vs news-off over verified NEWS covered spans.

The identical coverage pattern governs G6 earnings coverage (§7.2): outside verified EARNINGS covered spans G6 passes with `G6_DISABLED_COVERAGE`; the mandatory sensitivity pair is earnings-gate-on vs off over verified EARNINGS covered spans.

---

## §12. Schedule

All times `America/New_York`, DST-aware (`zoneinfo`); trading days from the exchange calendar.

| Time ET | Step | Output |
|---|---|---|
| 08:15 | Regime computation | Internal + logged |
| 09:00 | Pre-open scan: regime, watchlist from prior daily bars, **earnings blackouts using the §7.2 G6 `d(e)` session mapping (including the next-trading-day fallback when a before-market-open or unspecified event calendar date is non-trading) and `{T,T+1,T+2}` index window**, overnight news classification | Pre-open brief |
| 10:00 | Main scan: full pipeline (§7.1), exit evaluation #1 (Stage 0) | BUY CANDIDATE / WATCH / EXIT / NO ACTION |
| 15:30 | Pre-close scan: exit evaluation #2 on data through the 15:14 bar; **no new entries** | HOLD / TRIM / EXIT advisory |
| 17:30 | Post-close review: screenshot window closes, reconciliation, portfolio review | Portfolio review + staleness warning if applicable |

Half-days (13:00 ET close): pre-close scan at **12:00** (data through the 11:44 bar); post-close review at 14:30; 10:00 scan unchanged. "Stops checked twice daily" means exactly the 10:00 and 15:30 (or 12:00) scans. A half-day DELAYED entry scheduled for 12:00 is sequenced **after** this 12:00 exit scan and is not exit-evaluated at that scan; its first exit evaluation is the next trading day's 10:00 scan (§13.2, FP-6).

---

## §13. Backtest Engine

### §13.1 Series usage

Per §3.3 consumer table. Fills always executable. Mid-position splits: `shares`, `stop_price`, `target_price` adjusted by split ratio on ex-date, using §3.6 data exclusively; deterministic; logged. Total-return prices never used as fills.

### §13.2 Entry latency scenarios (3 populated + 1 null; non-weighted)

Probabilities **MUST MEASURE DURING PAPER TRADING**; no weighting invented. All fills executable series with buy slippage (§13.4). Stop/target levels for all scenarios are the decision-time reference-anchored levels (§8.4/§8.5) — they exist before any fill, so every voiding test is well-defined and non-circular.

| Scenario | Entry fill | Voiding rule |
|---|---|---|
| FAST | open of 1-min bar labeled **10:05** | — |
| DELAYED | open of 1-min bar labeled **13:00** (half-day: **12:00**) | Voided if any executable 1-min bar with label ≥ 09:45 and close ≤ fill_time − 15 min (full day: labels 09:45–12:44; half-day: 09:45–11:44) has `low ≤ stop_price` or `high ≥ target_price` |
| MISSED | no fill; candidate expires | degenerate entry-only null (N-20) |
| NEXT_SESSION | next trading day **official open** (N-22; 09:30 fill timestamp) | If the scheduled fill date lies **outside the current walk-forward window**, the candidate expires unfilled and is logged `ENTRY_UNFILLABLE_WINDOW_END`. Otherwise resolution occurs at that day's **S0.0 before S0.1** (§7.1): compute §5.5 using SPY 09:44 executable bar vs T−1 official close. If the filter trips, the candidate is voided — **no position is created, no accounting occurs, and no exit evaluation occurs for that candidate** — log `NEXT_SESSION_VOIDED`. If required SPY filter data is unavailable under §19 item 2, the candidate expires unfilled and logs `ENTRY_UNFILLABLE_NO_FILTER`. Otherwise the 09:30 fill stands and the position is exit-eligible from that day's 10:00 S0.2. |

**NEXT_SESSION S0.0 sequencing (P-A-04 — NORMATIVE):** the §5.5 computation required for a scheduled NEXT_SESSION fill is performed at S0.0 even if S0.3 would otherwise terminate the same scan. The candidate does not create a position or accounting state until S0.0 resolves it. A standing 09:30 fill is then visible to the same day's 10:00 exit evaluation. FAST and DELAYED semantics are unchanged.

**Half-day DELAYED sequencing (FP-6 — NORMATIVE):** on a half-day, the 12:00 DELAYED entry fill is processed **after** the 12:00 exit scan. It is not eligible for exit evaluation at that 12:00 scan, whose 11:44 evaluation bar predates the entry. Its first exit evaluation is the next trading day's 10:00 scan.

**Entered-beyond-stop state (RP-10 — NORMATIVE):** because the DELAYED voiding window is availability-bounded (a breach inside the final 15 minutes before the fill is never scanned) and a filled NEXT_SESSION candidate is not voided for stop/target state at S0.0, an entry fill may land **at or below `stop_price`** (or at/above `target_price`). In every such case the position **is entered**; `realised_risk_actual` is recorded **signed** (may be ≤ 0); the trade is tagged `ENTERED_BEYOND_STOP`; and **normal exit evaluation applies at the next scheduled scan** (no same-instant exit is synthesized). On a half-day 12:00 DELAYED fill, that next scheduled scan is the next trading day's 10:00 scan.

**Missing-bar fallback (entry):** if the scheduled fill bar is absent, fill at the open of the first available executable 1-min bar after the scheduled label within the session; if none exists before the close, the candidate expires (logged `ENTRY_UNFILLABLE`). The NEXT_SESSION walk-forward-window-end rule above takes precedence and logs `ENTRY_UNFILLABLE_WINDOW_END` rather than searching outside the window.

### §13.3 Exit latency scenarios (govern timing unconditionally — N-09)

Detection time (scan) and execution time are distinct. Exit scenarios cross with entry scenarios. For §8.6 item 5 trend failure, the **earliest qualifying exit-evaluating scan per §8.6 item 5** is the detection scan. For §8.6 item 2 RISK_OFF, the **next non-suppressed exit-evaluating scan of the same session** under §19 item 2 is the detection scan while the advisory remains pending. For a §13.6 symbol-change/delisting force-close, the **10:00 detection scan defined in §13.6** is the detection scan here. EXIT_FAST and EXIT_DELAYED then use the table below; the corporate-event path retains §13.6's special last-available-executable-price rule while preserving this section's fill timestamp.

| Exit scenario | Simulated exit fill |
|---|---|
| EXIT_FAST | open of executable 1-min bar labeled detection + 5 min: **10:05**, **15:35**, half-day **12:05**; sell slippage |
| EXIT_DELAYED | detection + 3 h, or official close (N-22; resolve any absent/non-trading nominal-date value under N-25) if sooner: 10:00 detection → open of bar labeled **13:00** (half-day: official close); 15:30 detection → **official close**; half-day 12:00 detection → official close; sell slippage |

**Missing-bar fallback (exit):** first available 1-min bar after the scheduled label; else official close (N-22), with any absent/non-trading nominal-date official-close resolution governed by N-25. If N-25 finds no qualifying daily executable bar inside an affected test window, that window follows N-25 test-window exclusion rather than inventing a fill price.

No `EXIT_MISSED` state exists in the baseline; the user is assumed to execute advised exits same-session. Failure-to-exit risk **DISCLOSED / MUST MEASURE DURING PAPER TRADING**.

**Scenario grid (N-20):** `3 populated entry × 2 exit = 6 combinations, plus MISSED as one degenerate null run` — reported as **6 + 1**, all non-weighted.

### §13.4 Slippage

```text
slippage_bps ∈ {0, 5, 10, 25}    — grid mandatory
Buy fill  = price × (1 + slippage_bps / 10000)
Sell fill = price × (1 − slippage_bps / 10000)
```

**ASSUMPTION / MUST MEASURE DURING PAPER TRADING.**

### §13.5 Gap-through stops (price semantics only — NORMATIVE)

§13.3 fixes the exit fill **timestamp** unconditionally. If the executable price at that timestamp is beyond the stop (or target), the fill occurs at that **actual price** (plus slippage) — never at the stop level, never earlier than the detection scan. Losses may exceed both `realised_risk` and `realised_risk_actual`. The stop-overshoot distribution (§15.2) is a mandatory report.

### §13.6 Corporate actions

All event data exclusively from §3.6 (`corp_actions_version` logged; as-revised **DISCLOSED BIAS**; price-quotient derivation prohibited).

- Splits: mechanical adjustment of `shares`, `stop_price`, `target_price` by ratio on ex-date; logged.
- **Dividends (P-A-01 — NORMATIVE).** Entitlement is determined by the **ex-date** from §3.6: a simulated position is entitled iff `shares > 0` immediately before the opening of the ex-date session. Use the ex-date share count **post-split adjustment where applicable**. `net_dividend = shares_held_at_ex_date × cash_amount_per_share × (1 − dividend_withholding_rate)`. Credit the net dividend to cash at the **earliest** of (i) the pay date, (ii) the position's exit fill timestamp, or (iii) the window's final official close (N-22), and attribute the **full** net dividend to the trade that held the entitlement. `record_date` is stored for provenance only and is not a decision or entitlement input. Signal series remains split-adjusted only; the dividend cash flow is explicit accounting. **P-2/N-25:** when branch (i) the pay date is the selected nominal credit date, or when branch (iii) consumes the window-final official close, resolve any required official-close value under N-25. No synthetic daily equity sample is emitted solely because the nominal credit date lacks its required daily executable bar; the cash ledger retains the credit and the next emitted equity sample reflects it.
- **Symbol changes / delistings (P-A-03 — NORMATIVE).** Detection scan = the **10:00 scan of the first trading day on which both** (a) the open position's ticker has **no executable bar**, and (b) universe metadata (§4.2) records a delisting or symbol change. That scan is the detection scan for §13.3. Fill timestamp follows the run's exit scenario: `EXIT_FAST` → 10:05; `EXIT_DELAYED` → the existing §13.3 rule. N-09 is preserved. Fill price = the **last available executable price for the ticker**, with sell slippage under §13.4. Apply full sell-side fees; regulatory fee-computation date = the **trading date of the fill timestamp** under §9.7 item 1. Log `CORP_EVENT_FORCE_CLOSE`; count/report it per §15.2.

### §13.7 Portfolio and equity accounting

```text
initial_capital: configurable (post-FX USD, N-15); canonical runs $61 and $200
equity(t) = cash + shares × executable_price(t)
```

- Single USD cash ledger. Compounding **ON within a run**; `portfolio_value` input to sizing is current simulated equity. Walk-forward windows are independent runs (N-24, §15.1).
- **Divergence disclosure:** backtest sizing input is exact same-instant equity; live sizing input is a **stale, vision-extracted, IDR-app-rendered value** with ±1.5% tolerated reconciliation divergence and age up to the last post-close screenshot. Not the same quantity; disclosed (§13.9 item 9); magnitude MUST MEASURE.
- Fee debits: buy fees at entry fill timestamp; sell + regulatory fees at exit fill timestamp; all on **actual** fill quantities (N-05).
- Dividend cash credits and trade attribution follow §13.6 exactly. A dividend may be credited at the exit fill timestamp or window-final official close before its pay date; this is accrual timing required by P-A-01, not pay-date receipt. The full attributed amount is persisted as `sim_trades.dividends_net` (§16).
- **Daily equity emission (P-2/N-25):** do **not** emit a daily equity sample for a session whose required daily executable bar is absent. Do not carry forward or synthesize an equity value for that missing-bar session. Drawdown consumers operate only on emitted samples (§15.3 item 4). Any explicit rule that requires an official open/close of a nominal date uses N-25 and logs the substitution.
- **Cash-availability assertion (N-10):** at every entry fill, assert `cash ≥ notional_actual + fees_buy`; at all times `cash ≥ 0`. Violation → HERMES CRITICAL HALT (§19 item 6).
- **T+1 settlement is not modeled** — disclosed (§13.9 item 4).
- If no candidate passes G9/G10, no trade; per-candidate reasons recorded. Absence of fee-viable candidates is itself a result (expected at $61 — §1.3.1).
- Every simulated trade record carries: `fee_schedule_version, corp_actions_version, coverage_manifest_versions, fee_rounding, vat_on_regulatory, slippage_bps, entry_scenario, exit_scenario, config_version, universe_version`, plus fills, `shares_filled`, `notional_actual`, `dividends_net`, `realised_risk` (screening), `realised_risk_actual` (signed), `stop_overshoot`, `risk_divergence`, and flags (`ENTERED_BEYOND_STOP`, `ENTRY_UNFILLABLE_WINDOW_END`, `CORP_EVENT_FORCE_CLOSE` where applicable).

### §13.8 Mandatory sensitivity branches

Entry×exit scenario grid (6+1); slippage grid; fee-rounding branches; VAT branches; FX reporting grid; news-on vs news-off (run-pinned verified NEWS covered spans); earnings-gate-on vs off (run-pinned verified EARNINGS covered spans); provisional fee-span and corp-actions-span segregation. **For partially fee-verified test windows, "provisional fee-span segregation" means enumeration of the affected test window plus its unverified spans/days only; no simulated metrics are produced for that window, no partial-window trade subsetting is permitted, and `TRAIN_SUBSTITUTED_ZERO` is never applied to a test-window run (§9.7 item 4).** Canonical OOS results remain valid only over fee-schedule-verified **and** corporate-actions-verified test windows (§9.7 item 4, §3.6 item 5).

### §13.9 Mandatory report-header disclosure block

Every backtest report carries, verbatim topics:

1. Survivorship bias (§4.3).
2. DATA DEGRADED / provider-failure days not simulated.
3. Fills are Alpaca consolidated (SIP) prices; actual Pluang execution price basis unmeasured (MUST MEASURE); slippage grid is a proxy, not a basis model.
4. T+1 settlement and unsettled-proceeds constraints not modeled.
5. Share precision assumed 4 dp truncation (MUST CONFIRM WITH BROKER).
6. Spans with news gate and/or earnings gate coverage-disabled, enumerated; globally excluded days (§19 item 2), enumerated.
7. Unconfirmed-ticker inclusion (backtest-only).
8. `initial_capital` is post-FX USD; IDR-basis results are a reporting layer (§9.9).
9. Live sizing input diverges from backtest equity (stale vision extraction, ±1.5% tolerance).
10. Exit advisories assumed executed same-session; no EXIT_MISSED state.
11. Close-based exit evaluation: intra-scan breaches that recover are never detected; stops advisory only. Voiding tests (§13.2) use intrabar high/low while exits are close-based — a deliberate, disclosed asymmetry.
12. Structural fee-viability constraints: for $61 runs, the §1.3.1 text; for $200 runs, the §1.3.2 ATR-floor text.
13. Session volume pace uses unadjusted volumes; splits inside the 20-session lookback inflate pace ≈ by the split factor for up to 20 sessions (§3.3).
14. Corporate-action and earnings data are as-revised, not point-in-time (§3.6, §7.2 G6).
15. Live news ingestion guard (7-day, §11.1) has no backtest counterpart; late-delivered headlines are effect-less live but effect-bearing in backtest.
16. NEXT_SESSION is a reported-only, non-canonical scenario with a deliberate **09:30→10:00 conditional-resolution overlap**: the official-open fill timestamp is 09:30, but S0.0 at 10:00 uses the 09:44 SPY bar to decide whether that fill stands or is voided. If it stands, the position is immediately exit-eligible at the 10:00 scan. This scenario-model artifact is disclosed and is not used by the canonical evaluation cell.
17. Live BUY-suppression paths with **no backtest counterpart**: vision-extraction failure, >3% screenshot↔portfolio reconciliation divergence (§18 item 3), and the live `FEE_SCHEDULE_UNVERIFIED` block (§9.7 item 6, §19 item 7). Backtest may trade on dates where one of these live-only paths would suppress a BUY. **For train-window fee gaps specifically, §9.7 item 3 also permits `TRAIN_SUBSTITUTED_ZERO`, so the divergence includes both admission and fee/P&L treatment; see item 20.**
18. Broker transaction fee (0.30%), JFX/KBI fee (0.05% with $0.10 cap), and the 11% VAT rate are applied as **current constants across the full backtest window** and are **not effective-dated**; this differs from the SEC/TAF/CAT components in §9.7. Historical broker-fee/VAT applicability is **MUST CONFIRM**; the baseline is an explicit backward projection. `fee_calculations.fee_input_status = BACKWARD_PROJECTED_CONSTANT` records this provenance (§16).
19. Train-window simulation is **not criterion-bearing**: PROVISIONAL fee spans and `CORP_ACTIONS_UNVERIFIED` spans inside train windows do not block parameter selection. Corporate-actions gaps remain logged/disclosed under §3.6/§15.1. For fee gaps, the deterministic train-only computation is the §9.7 `TRAIN_SUBSTITUTED_ZERO` mechanism described in item 20; affected train windows/spans remain enumerated and disclosed. Criterion-bearing OOS/test-window acceptance still uses verified windows only (§15.1, §15.4).
20. **Train-only regulatory-fee substitution:** during `run_role = TRAIN`, a missing SEC/TAF/CAT historical input may be computationally represented by `TRAIN_SUBSTITUTED_ZERO` exactly as §9.7 item 3 defines. This is a **research-selection convenience, not a historical verified-zero claim**; each affected candidate/trade, component, fee-computation date, and fee context is provenance-logged (§16), and parameter selection may be biased because a historically nonzero fee may have been modeled as zero. The §1.3.1/§1.3.2 derived G9 admissibility bands therefore **do not describe train-window G9 geometry on substituted dates**. No such substitution is permitted in test-window or live runs, and partially fee-verified test windows are enumeration-only with no simulated metrics (§9.7 item 4, §13.8).
21. **Backtest/live earnings-source divergence (A-2):** the earnings source used for BACKTEST MAY differ from the earnings source used for LIVE operation (§3.7). Each source must independently satisfy the canonical G6 semantics (§7.2) and the §3.7 earnings-source data contract; if the backtest and live earnings sources differ, that divergence is disclosed here at **report level**. This disclosure is descriptive report-level metadata only and introduces no persistence-schema requirement (§3.7 C-2).

---

## §14. Hermes Architecture

### §14.1 Stack

`Python 3.11+ · pydantic · pandas/numpy · sqlalchemy · instructor + OpenRouter (pinned)`. LLM calls: pinned config, temperature 0, versioned schemas, cache-keyed replay for backtests.

[MISSING SOURCE CONTENT — CANNOT CANONICALIZE] Concrete `openrouter/<provider>/<model>@<version>` identifier.

### §14.2 Engine functions

```text
HermesEngine: compute_regime() · scan_pre_open() · scan_main() ·
              scan_pre_close() · post_close_review() · process_screenshot()
```

Pure functions over point-in-time snapshots; all outputs written before message dispatch.

### §14.3 Governance

Deterministic calculations outside the LLM; LLM output structured/classificatory only; code applies gates; NEWS_UNVERIFIED policy per §11.2; LLM cannot execute trades or alter pre-computed decisions.

### §14.4 Optional broker integration

Pluang MCP/Agentic read-only channel: **FUTURE / OPTIONAL**; scope **MUST CONFIRM WITH BROKER**; never authorizes autonomous execution.

---

## §15. Backtest Success / Validation Criteria

### §15.1 Walk-forward protocol — PRE-REGISTERED RESEARCH DECISION

```text
Type: rolling walk-forward
Train window: 24 months | Test window: 6 months | Step: 6 months, non-overlapping tests
Coverage: 2018-01-01 → `coverage_end` (fixed in the pre-registration manifest)
Minimum required: ≥ 8 fee-verified AND corp-actions-verified AND N-25-resolvable test windows (§9.7 item 4, §3.6 item 5, N-25)
```

**Coverage-end pre-registration (FP-7 — NORMATIVE):** `coverage_end` is a fixed, inclusive calendar date recorded in `pre_registration_manifest.yaml` at the same git commit that freezes the acceptance criteria and search space. Only complete train/test windows fully contained within `2018-01-01 … coverage_end` are constructed. `coverage_end` is never derived from the backtest run date. Its exact value is a pre-registration artifact and must be committed before any OOS evaluation.

**Equity basis (N-24 — NORMATIVE):** every train window and every test window is simulated as an **independent run** with `initial_capital` ($200 for the criterion-bearing run) and `cash = initial_capital` at window start. Equity, cash, and open positions **never carry across windows**; any position still open at a window's final trading day is force-closed at that day's official close (N-22), **resolved under N-25 when the nominal date is non-trading or its daily executable bar is absent**, with sell slippage and full sell-side fees, logged `WINDOW_END_FORCE_CLOSE`, and its P&L belongs to that window. If N-25 finds no qualifying daily executable bar inside an affected **test** window, that window is excluded from the verified-window count and acceptance aggregates exactly as N-25 specifies; no force-close price is invented. Compounding is ON within each window only. This convention is chosen because G9/G10 are not scale-invariant (fixed minimums, ceil-cent rounding, the $10 floor, the 90% cap), so the equity basis is trade-determining; per-window reset makes windows independent, as the pre-registration structure already assumes.

- Refit rule: parameters selected on each train window only; frozen through its test window; no test-window feedback. No same-window fitting anywhere. Gate B remains deferred.
- Search space: finite grid over MUST TEST parameters, enumerated in `search_space.yaml`, versioned; every evaluated configuration logged (count, seed, results); no silent discards.

**Selection objective (NORMATIVE):** performed on the **canonical evaluation cell** (N-17, which fixes the news/earnings-gate setting for selection as well) of the $200 basis over the train window:

1. **Objective:** maximize `net_expectancy` (N-23) — the arithmetic mean over the train window's trades of **realised net P&L in USD per trade**, where trade P&L = sell proceeds − buy cost − all modeled fees − slippage effects + **net dividends attributed to the trade under §13.6**. All trades weighted equally.
2. **Tie-breaks, in order:** (a) greater trade count; (b) smaller within-window **fractional max drawdown as defined in §15.3 item 4**; (c) earliest position in `search_space.yaml` enumeration order.
3. **Zero-trade configurations** are excluded from selection. If **all** configurations produce zero train-window trades: carry forward the previous window's selection; for the first window, use the documented baseline defaults. All such events logged.
4. R-multiple expectancy is reported but is **never** the selection objective or an acceptance criterion (N-23).

**Train-window verification treatment (FP-7 + R2.3 EB-01 + Audit A P3 — NORMATIVE):** train-window simulation is **not criterion-bearing**. PROVISIONAL / `FEE-SCHEDULE-UNVERIFIED` spans and `CORP_ACTIONS_UNVERIFIED` spans inside a train window do **not** block configuration evaluation or parameter selection.

For every train-window regulatory-fee computation on an unverified SEC/TAF/CAT fee-computation date, each missing component receives the §9.7 `TRAIN_SUBSTITUTED_ZERO` treatment. The resulting deterministic fee values feed the existing computation chain without further special handling: `fees_rt`; G9; realised net trade P&L; train-window `net_expectancy`; and parameter selection with its existing tie-breaks. Every affected resulting trade carries `TRAIN_FEE_SUBSTITUTED`; the candidate/trade, substituted component(s), fee-computation date, and fee context are recorded per §16.

The substitution is a property of the **simulation run role**, never of the calendar date. Because the rolling 24/6/6 schedule causes later train windows to overlap calendar dates that previously appeared in test windows, the same date may receive train-only substitution in a `TRAIN` run and ordinary verified-state treatment in a separate `TEST` run. Every test-window run asserts zero `TRAIN_SUBSTITUTED_ZERO` fee inputs; any violation halts under §19 item 6. Live runs are subject to the same prohibition.

This train treatment is a **research-selection convenience** that may bias parameter selection; it never changes coverage verification and never enters test-window fee arithmetic. Existing `CORP_ACTIONS_UNVERIFIED` train-window treatment is unchanged: such spans remain logged/disclosed and do not block train selection. The existing zero-trade/no-selection fallback remains unchanged. Report-header disclosures are §13.9 items 19–20; canonical OOS/test-window aggregates and acceptance criteria remain restricted to fully fee-verified, corp-actions-verified, and N-25-resolvable test windows (§15.4).

**Pre-registration completeness rule:** pre-registration is effective only when this document, the fully enumerated `search_space.yaml`, and `pre_registration_manifest.yaml` containing a concrete `coverage_end` value are committed in the same pre-registration commit. **OOS evaluation must not begin before that commit.** Its git commit date is the pre-registration timestamp, at which the acceptance criteria, search space, and window-set endpoint are frozen.

[MISSING SOURCE CONTENT — CANNOT CANONICALIZE] Exact grid values of `search_space.yaml`; **blocks Phase 4** until committed.

[MISSING PRE-REGISTRATION ARTIFACT] Concrete `coverage_end` value in `pre_registration_manifest.yaml`; **blocks Phase 4 / any OOS evaluation** until committed.

### §15.2 Reported metrics

Per scenario cell and aggregated across test windows: `net_expectancy` (N-23, USD/trade) and R-multiple expectancy (reported-only); win rate; profit factor; Sharpe (daily, rf = 0); **fractional max drawdown** (§15.3 item 4); CAGR; trade count; MFE/MAE distributions; stop-overshoot distribution; risk-divergence distribution; per-window expectancy table; count of `ENTERED_BEYOND_STOP`, `ENTRY_UNFILLABLE_WINDOW_END`, `NEXT_SESSION_VOIDED`, `ENTRY_UNFILLABLE_NO_FILTER`, `WINDOW_END_FORCE_CLOSE`, and `CORP_EVENT_FORCE_CLOSE` events.

**Audit-metric formulas (RP-10 — NORMATIVE):**

$$\text{stop\_overshoot} = \frac{\text{stop\_price} - \text{exit\_fill\_price}}{\text{stop\_price}} \quad \text{(reported only for trades with } \text{exit\_reason} = \text{STOP; negative values = favourable fills)}$$

$$\text{risk\_divergence} = \frac{\text{realised\_risk\_actual}}{\text{realised\_risk}} - 1 \quad \text{(signed; } \text{realised\_risk\_actual} \le 0 \text{ possible per §13.2)}$$

**Uncertainty reporting (NORMATIVE):** nonparametric i.i.d. bootstrap on per-trade net USD P&L; `B = 10,000`; percentile 95% interval (2.5/97.5). **RNG pinned (RP-12):** numpy `default_rng(42)` (PCG64); each replicate's resample indices drawn as `rng.integers(0, n, size=n)`; replicates generated sequentially from the single generator instance. Mandatory at every trade count. **E-02 disclosed reproducibility limitation:** the ordering of the pooled per-trade net-USD-P&L vector is **not normatively pinned** in R2.7; therefore `default_rng(42)`/PCG64 and the stated resample call pin the RNG sequence only **conditional on a given input-vector ordering** and do not guarantee bit-identical confidence intervals across implementations that pool the same trades in different orders. The CI is reporting-only and bears no acceptance criterion. **Other disclosed limitation:** the estimator ignores serial dependence and window structure.

### §15.3 Benchmarks (per-window construction — NORMATIVE; terminal-liquidation asymmetry disclosed)

Benchmarks: buy-and-hold **QQQ** (criterion-bearing) and **SCHG** (reported). **Constructed independently per test window**, mirroring N-24:

1. Single purchase at the **official open (N-22)** of the first trading day of the test window, with absent/non-trading nominal-date resolution under **N-25**, executable price, canonical fee model, canonical-cell slippage (10 bps), same `initial_capital` ($200), share precision N-04.
2. Dividend entitlement uses the **§13.6 ex-date rule** on benchmark shares: the benchmark is entitled iff shares are held immediately before the ex-date session open; `record_date` is provenance only. Net dividend uses the same `dividend_withholding_rate` as the strategy. Because the benchmark has no ordinary exit fill, credit occurs at the earlier of the **pay date** or the **window's final official close**; the credited amount is reinvested at that nominal credit date's official close (N-22), **resolved under N-25**, executable price, with no additional fee — disclosed simplification. **Scope note: the benchmark credit rule is extended beyond accepted patch P-A-01, which amended strategy §13.6/§15.1 only. Under R2.4 a benchmark dividend whose pay date followed the window end was not credited; under N-24 window independence it is now credited at the window's final official close. This changes benchmark terminal equity and may change the §15.3 item 4 benchmark maxDD_max used by §15.4 criterion 3.**
3. Daily benchmark equity = shares × official close (N-22) + residual cash; USD basis (no FX layer on either side). **P-2/N-25 emission rule:** if the benchmark's required daily executable bar for a session is absent, emit **no** daily benchmark equity sample for that session; do not carry forward a prior value. Any official-close substitution required by another consuming rule is logged under N-25 and does not create a missing session's daily equity sample.
4. **Max drawdown (strategy and benchmark identically; FP-7 — NORMATIVE):** on the **emitted daily official-close (N-22) equity samples within each window**, with missing required-daily-bar sessions omitted under N-25, define `peak_t = max_{u≤t}(equity_u)` and  
   `max_drawdown = max_t ((peak_t − equity_t) / peak_t)`.  
   This is a **fractional, dimensionless** peak-to-trough decline. The same definition is used for the §15.1 train-window tie-break and the §15.4 criterion-3 strategy/QQQ comparison.

**E-07 disclosed asymmetry:** despite identical per-window starting capital, dividend withholding, and max-drawdown definition, the benchmark is **not terminally liquidated**, while the strategy force-closes an open position at window end with sell slippage and full sell-side fees under N-24/§15.1. The benchmark construction is therefore not fully symmetric to strategy terminal accounting. This disclosure changes no benchmark rule or acceptance criterion.

### §15.4 Pre-registered acceptance criteria (frozen at pre-registration commit)

**Evaluation scope (NORMATIVE):** exactly one cell — the **canonical evaluation cell** (N-17) — of the **$200 run**, over **fee-verified, corp-actions-verified, and N-25-resolvable test windows only** (§9.7 item 4; §3.6 item 5; N-25; partially fee-verified or N-25-unresolvable test windows produce no simulated acceptance metrics), each window simulated per N-24. A window excluded by N-25 is excluded from criterion 1's denominator and from criteria 2–3 aggregates. A test-window run may never write or consume `TRAIN_SUBSTITUTED_ZERO`; any attempted write is a deterministic engine exception under §19 item 6.

```text
1. positive OOS net_expectancy (N-23 point estimate, per-window trade set)
   in ≥ 60% of verified, N-25-resolvable test windows;
   a zero-trade window counts as NON-POSITIVE and remains in the denominator (N-18)

2. AND aggregate OOS net_expectancy > 0, computed by POOLING ALL TRADES
   across verified, N-25-resolvable test windows with equal per-trade weight (N-23)

3. AND strategy maxDD_max ≤ 1.5 × QQQ maxDD_max, where maxDD_max is the
   MAXIMUM over verified, N-25-resolvable test windows of the within-window FRACTIONAL
   max drawdown (§15.3 item 4), computed identically for strategy and benchmark.
   (Per-window statistic chosen to avoid artificial drawdowns at
   window-reset seams under N-24.)
```

- Fewer than 8 fee-verified, corp-actions-verified, N-25-resolvable test windows → verdict **NOT EVALUABLE**; progression blocked (§9.7 item 5).
- The **$61 run is not evaluated** against these criteria (N-16, §1.3.1); it is reported with the structural-constraint disclosure.
- These criteria are pass/fail for proceeding to paper trading. They are **not proof of edge**. They are **PRE-REGISTERED RESEARCH DECISIONS**, not validated strategy truths.

### §15.5 Trade-count warning

`aggregate trade count < 100 → report flagged INSUFFICIENT SAMPLE`. Clearing the flag does **not** imply sufficiency or validation. No fixed Sharpe/win-rate/profit-factor/CAGR/drawdown threshold is a validated truth. The $61 run is expected to carry this flag permanently (§1.3.1).

---

## §16. Database Design

Normative reconstruction requirement:

> What did Hermes know, calculate, classify, recommend, and believe the portfolio contained at timestamp T, and what happened afterward?

Tables append-only. All rows carry `run_id, config_version, code_commit`. All decision-bearing rows (`gate_results`, `scores`, `recommendations`, `sim_trades`) additionally carry `universe_version`. `run_history.run_role` is mandatory and belongs to the run, not to a calendar date.

```text
run_history(run_id, run_role, config_version, code_commit, started_at,
            coverage_manifest_versions, status, exception_json)
     -- run_role ∈ {TRAIN, TEST, LIVE}
     -- TRAIN_SUBSTITUTED_ZERO is legal only when run_role = TRAIN

bars(ticker, ts_label_start, ohlcv, feed, timeframe, adjustment)
     -- feed MUST equal 'sip' for decision-consumed bars (§3.5)

corp_actions(ticker, event_type, ex_date, split_ratio, cash_amount_per_share,
             record_date, pay_date, corp_actions_version)     -- §3.6

news_headlines(headline_hash, source, ticker, published_at,
               headline_text_normalized, fetched_at)          -- FP-4/FP-5

coverage_manifests(source_kind, ticker, span_start, span_end,
                   verified, manifest_version)
     -- source_kind ∈ {NEWS, EARNINGS, CORP_ACTIONS, FEE_SCHEDULE}
     -- "covered span" = run-pinned manifest record with verified=true (§11.6, §3.6)
     -- FEE_SCHEDULE coverage attestation is reconstruction provenance only;
     -- it never substitutes for a verified fee_schedule.yaml rate/verified-zero (§9.7)

regime_snapshots(date, trend_score, vix, vix_date_used, regime, multiplier,
                 opening_return_spy)

indicator_snapshots(ticker, ts, ema20/50/200, atr14, rsi14, vwap, vol_metrics)

gate_results(candidate_id, gate_id, pass, inputs_json, stage, universe_version)
     -- For G6, inputs_json preserves each considered earnings event's
     -- event calendar date, provider timing value, mapped trading session d(e),
     -- decision date T, and run-pinned EARNINGS manifest version (§7.2).
     -- For before-market-open or unspecified events dated on a non-trading day,
     -- mapped d(e) is the next trading day under §7.2 and that resolved session is logged.
     -- For train-window G9 screening that uses §9.7 substitution, inputs_json
     -- preserves fee-computation provenance, including:
     -- fee_context = SCREENING_RT
     -- fee_computation_date
     -- substituted_components = [SEC | TAF | CAT, ...]
     -- per-component fee_input_status, including TRAIN_SUBSTITUTED_ZERO

scores(candidate_id, component_points_json, raw_total, final_total_int,
       rank, tiebreak_applied, cutoff, regime, universe_version)

news_classifications(headline_hash, ticker, source, ma_role, keyword_override,
                     json_payload, model_version, schema_version,
                     classified_at_wallclock, published_at,
                     activation_start, activation_end)
     -- cache identity = (headline_hash, source, schema_version, model_version)
     -- P-4: source is never a classification input; effect fields
     -- {category,direction,severity,ma_role,confidence,keyword_override}
     -- must be identical across all rows sharing (headline_hash, ticker).
     -- A mismatch is NEWS_CACHE_INTEGRITY_FAILURE and halts under §19 item 6.
     -- classified_at_wallclock has no decision effect (N-21)

recommendations(id, ts, type, ticker, notional, shares_est, stop, target,
                realised_risk, fees_rt_screening, gateA_burden,
                template_hash, universe_version)

sim_trades(trade_id, recommendation_id, entry_scenario, exit_scenario,
           entry_fill_ts, entry_fill_price, shares_filled, notional_actual,
           exit_detection_ts, exit_fill_ts, exit_fill_price, exit_reason,
           dividends_net,                   -- full net dividends attributed under §13.6
           realised_risk_actual,            -- signed (N-07, §13.2)
           stop_overshoot,                  -- formula §15.2; NULL unless exit_reason = STOP
           risk_divergence,                 -- formula §15.2
           flags,                           -- ENTERED_BEYOND_STOP, ENTRY_UNFILLABLE_WINDOW_END,
                                            -- WINDOW_END_FORCE_CLOSE, CORP_EVENT_FORCE_CLOSE,
                                            -- TRAIN_FEE_SUBSTITUTED, ...
           train_fee_substitutions_json,
           fee_schedule_version, corp_actions_version, coverage_manifest_versions,
           fee_rounding, vat_on_regulatory, slippage_bps,
           config_version, universe_version)
     -- train_fee_substitutions_json is NULL unless substitution occurred.
     -- When populated, it preserves one or more objects containing:
     -- { component, fee_computation_date,
     --   fee_context: SCREENING_RT | EXIT_ACTUAL,
     --   fee_input_status: TRAIN_SUBSTITUTED_ZERO }
     -- Existing sim_trades flags/annotations remain additive and are never replaced.
     -- dividends_net is the trade-level amount used by §15.1 net-P&L reconstruction.

simulation_events(event_id, candidate_id, trade_id, event_ts, event_code, details_json)
     -- candidate_id and trade_id are nullable when not applicable.
     -- event_code includes NEXT_SESSION_VOIDED, ENTRY_UNFILLABLE_NO_FILTER,
     -- ENTRY_UNFILLABLE, ENTRY_UNFILLABLE_WINDOW_END, CORP_EVENT_FORCE_CLOSE,
     -- WINDOW_END_FORCE_CLOSE, EXIT_EVAL_SUPPRESSED, TICKER_REQUIRED_BAR_EXCLUSION,
     -- VWAP_UNDEFINED_ZERO_VOLUME, RISK_OFF_ADVISORY_PENDING, RISK_OFF_ADVISORY_LAPSED,
     -- OFFICIAL_PRICE_SUBSTITUTION,
     -- WINDOW_EXCLUDED_OFFICIAL_PRICE_UNAVAILABLE, NEWS_CACHE_INTEGRITY_FAILURE,
     -- plus pre-existing reason codes.
     -- NEXT_SESSION_VOIDED / ENTRY_UNFILLABLE_NO_FILTER must preserve the scheduled
     -- 09:30 fill date, §5.5 inputs/result or missing-input reason, and S0.0 timestamp.
     -- CORP_EVENT_FORCE_CLOSE preserves metadata event type, 10:00 detection date,
     -- exit scenario, fill timestamp, last-available-price provenance, and fee date.
     -- TICKER_REQUIRED_BAR_EXCLUSION preserves session, missing required labels,
     -- and entry-side scope; EXIT_EVAL_SUPPRESSED preserves scan timestamp,
     -- N-02 boundary label, and absence of any executable bar at/before that boundary.
     -- RISK_OFF_ADVISORY_PENDING preserves the 08:15 detection session and each
     -- suppressed exit-evaluating scan; RISK_OFF_ADVISORY_LAPSED records a same-session
     -- close lapse when no non-suppressed issuance occurred while the position stayed open.
     -- OFFICIAL_PRICE_SUBSTITUTION preserves nominal date, substituted session,
     -- consuming rule, and whether OPEN or CLOSE was consumed (N-25).
     -- NEWS_CACHE_INTEGRITY_FAILURE preserves headline_hash, ticker, source-keyed
     -- cache identities, and the conflicting effect-field payloads.

fee_calculations(fee_calc_id, candidate_id, trade_id, side, component,
                 fee_context, fee_computation_date,
                 base_amount, vat_amount, rounded_amount, rounding_branch,
                 fee_schedule_version, fee_input_status)
     -- fee_context ∈ {SCREENING_RT, ENTRY_ACTUAL, EXIT_ACTUAL}
     -- SCREENING_RT rows require candidate_id; trade_id may be NULL.
     -- ENTRY_ACTUAL / EXIT_ACTUAL rows require trade_id.
     -- For SEC/TAF/CAT, fee_input_status ∈
     --   {VERIFIED_RATE, VERIFIED_ZERO, TRAIN_SUBSTITUTED_ZERO}
     -- For backward-projected broker transaction/JFX-KBI/VAT inputs,
     --   fee_input_status = BACKWARD_PROJECTED_CONSTANT.
     -- TRAIN_SUBSTITUTED_ZERO rows must have rounded_amount = 0 and may occur
     -- only in run_role = TRAIN; for regulatory substitution the only legal
     -- fee_context values are SCREENING_RT and EXIT_ACTUAL (§9.6, §9.7).
```

**Normative provenance/assertion rules:**

1. `TRAIN_SUBSTITUTED_ZERO` never creates or implies a verified `fee_schedule.yaml` entry. `fee_schedule_version` identifies the actual source schedule that lacked the component; substitution provenance is separate.
2. Every substitution occurrence must be reconstructible to the run, candidate and/or trade, component, fee-computation date, and fee context. A G9-screened candidate that never becomes a trade is still reconstructible from `gate_results` / `fee_calculations`.
3. `fee_input_status` is non-null and truthful for every `fee_calculations` row: effective-dated regulatory inputs use `VERIFIED_RATE` or `VERIFIED_ZERO`; train-only missing regulatory inputs use `TRAIN_SUBSTITUTED_ZERO`; backward-projected broker transaction/JFX-KBI/VAT constants use `BACKWARD_PROJECTED_CONSTANT`.
4. A `TEST` or `LIVE` run must assert **zero** `TRAIN_SUBSTITUTED_ZERO` rows/writes. Any attempted write is a deterministic engine exception and halts under §19 item 6.
5. If train-window G9 screening or an actual train exit uses substitution and a simulated trade results, that trade must carry `TRAIN_FEE_SUBSTITUTED` while retaining every other pre-existing flag and annotation.
6. `sim_trades.dividends_net` must equal the sum of full net dividends attributed to that trade under §13.6. `CORP_EVENT_FORCE_CLOSE`, `NEXT_SESSION_VOIDED`, and `ENTRY_UNFILLABLE_NO_FILTER` must be reconstructible from `simulation_events` plus the cited source inputs; a voided/unfilled pending candidate creates no `sim_trades` row.
7. **P-1 bar provenance:** every `TICKER_REQUIRED_BAR_EXCLUSION` logs the session and missing subset of `{09:30,…,09:39,09:44}`; `VWAP_UNDEFINED_ZERO_VOLUME` logs the returned 09:30–09:44 bar set and cumulative volume; every `EXIT_EVAL_SUPPRESSED` logs the scan's N-02 boundary and proves that no executable ticker bar existed in-session at or before it. If exit evaluation proceeds on an earlier last-available bar, that bar label and close are persisted in the exit-evaluation provenance.
8. **P-2 official-price provenance:** every N-25 substitution logs the nominal date, substituted session, consuming rule, and OPEN/CLOSE side. If neither qualifying session exists inside an affected test window, log `WINDOW_EXCLUDED_OFFICIAL_PRICE_UNAVAILABLE`; no daily equity row/sample is emitted for a missing required daily-bar session, and the window is omitted from the §15.4 acceptance aggregates as N-25 requires.
9. **P-4 cache-integrity assertion:** before any news effect, NEWS_UNVERIFIED decision, catalyst aggregation, or two-source confirmation consumes cached classifications, assert identical effect fields for all entries sharing `(headline_hash, ticker)`. Any mismatch logs `NEWS_CACHE_INTEGRITY_FAILURE`, marks the run non-canonical, and halts under §19 item 6.
10. **P-3 advisory provenance:** when a RISK_OFF advisory detected at 08:15 encounters a suppressed exit-evaluating scan, persist `RISK_OFF_ADVISORY_PENDING` with the detection session and suppressed scan. If it reaches a later non-suppressed scan, the ordinary exit detection/fill provenance identifies that scan; if it remains unissued through session close while the position remains open, persist `RISK_OFF_ADVISORY_LAPSED`. Re-detection on a later session is a new 08:15 session event under §8.6 item 2.

`run_history` pins and logs the `manifest_version` used for each coverage source kind; decision reconstruction must use only those pinned coverage-manifest versions. Deterministic engine exceptions log stack trace and inputs to `run_history`.

**E-03 reconstruction disclosure:** complete Phase-4 walk-forward reconstruction also requires persistent **run↔window boundaries**, each run's **`initial_capital` and scenario cell**, and the §15.1 **configuration-evaluation log (`count`, `seed`, `results`)**. R2.7 does not invent exact table/column layouts that were absent from the accepted source; those exact Phase-4 persistence schemas remain a documented implementation artifact that must be committed before Phase 4 can be considered fully reconstructible.

[MISSING SOURCE CONTENT — CANNOT CANONICALIZE] Exact execution-event schema for user trade confirmations (§18) remains a live-phase completion item. Exact persistence schemas for the Phase-4 reconstruction artifacts identified above are also missing; they are **not** live-phase-only items.

---

## §17. Telegram Format

### §17.1 BUY CANDIDATE template

Deterministic template; LLM narrative appended, clearly separated, cannot alter fields. **Published stop/target are the canonical reference-anchored levels (N-06) — identical to backtest.**

```text
[BUY CANDIDATE] TICKER — band (Strong/Moderate)
Regime: X | Score band only
Entry ref: $A (09:44-bar close basis) | Notional: $N (place as dollar order) | Est. shares: s
Stop: $S (−p%) | Target: $T (+2p%)   [anchored to entry ref — fixed, not adjusted to your fill]
Risk: $R (screening realised risk) | Round-trip fees: $F (= Gate A burden b% of risk)
Gates: passed | News: classification summary
⚠ Stops are advisory — you must place/execute manually.
⚠ Your actual risk depends on your fill price relative to the entry ref.
⚠ Data is 15-min delayed. [staleness warning if portfolio data old]
```

Fee metric shown = `fees_rt / realised_risk` = Gate A quantity (§9.4).

### §17.2 Display rules

Band only (`Strong ≥ 85`, `Moderate = cutoff..84`, integer scores). Nothing below the active cutoff is published. Raw and final scores logged only.

### §17.3 Other message types

Pre-open brief, pre-close advisory, post-close review, WATCH, EXIT, NO ACTION: analogous fixed templates with reason codes.

[MISSING SOURCE CONTENT — CANNOT CANONICALIZE] Full template text for non-BUY messages beyond stated requirements; live-phase completion item.

---

## §18. Portfolio State / Screenshot Pipeline & Confirmation Loop

1. **Submission:** user sends post-close Pluang screenshot via Telegram.
2. **Vision extraction:** pinned LLM, temperature 0, `screenshot_schema_v1` (`total_portfolio_value` mandatory; `cash`; `positions[]` of `ticker/qty/avg_cost/market_value`; `extraction_confidence`). If `extraction_confidence < 0.90` → manual confirmation required; unconfirmed extraction not written to state. **ASSUMPTION / MUST TEST.**
3. **Reconciliation:** per-position market values vs Alpaca last close; tolerance ±1.5% → `PLUANG_PRICE_DIVERGENCE` flag + warning; divergence > 3% → BUY output suppressed until resolved. Thresholds **ASSUMPTION / MUST TEST**; divergence statistics **MUST MEASURE DURING PAPER TRADING**.
4. **Staleness:** if no fresh screenshot before a sizing calculation → use last known `portfolio_value` with explicit age-in-days warning; never skip the scan. **DECIDED.** (Live side of the §13.7 divergence disclosure.)
5. **Confirmation loop:** user trade confirmations (e.g., `bought 0.4 NVDA @ 181.20`) parsed and written as execution events; unconfirmed recommendations auto-expire at day end; adopted positions per §8.3/§8.7; CRITICAL EXIT advisories flagged for human confirmation.

[MISSING SOURCE CONTENT — CANNOT CANONICALIZE] Exact execution-event schema and confirmation parsing grammar; live-phase completion item.

---

## §19. Failure Modes

Deterministic; every trip logged with a reason code.

1. **Market-data staleness kill switch** — bar older than delay + 20 min at scan → `DATA DEGRADED`, no BUY output; exits evaluated only if exit-relevant data is fresh, else `MANUAL REVIEW ADVISED`. (Not simulated in backtest — §13.9 item 2.)
2. **Missing bars / calendar mismatch / VIX failure — excluded-day rule (RP-06 + P-A-03/P-A-04/P-A-05 + P-1/P-2/P-3 — NORMATIVE).** Ticker-level **entry-side** exclusion for the day on missing required ticker bars; the §6.1 zero-volume VWAP guard is a separate entry-side-only exclusion described below. **Required-bar definition (NORMATIVE).** For a scan on session `D`, a ticker's required executable bars are the ten 1-min bars labeled **09:30 through 09:39** (§6.1 opening range) and the bar labeled **09:44**. **Ticker-level exclusion for the day occurs iff any required bar is absent**; absence of a bar labeled 09:40–09:43 is not an exclusion. Session VWAP and session pace (§6.1) are computed over the bars the provider returns within 09:30–09:44, subject to the §6.1 zero-volume guard. The §6.1 `VWAP_UNDEFINED_ZERO_VOLUME` guard excludes the ticker from **entry-side** evaluation (Stages 1–5) only and never suppresses exit evaluation. **Exit evaluation at a scheduled exit-evaluating scan is suppressed (`EXIT_EVAL_SUPPRESSED`) iff the open position's ticker has no executable 1-min bar in that session labeled at or before the scan's N-02 availability boundary**; where such a bar exists, §8.6's last-available-bar evaluation price applies and exit evaluation proceeds irrespective of any entry-side ticker-level exclusion. **Daily executable-bar absence for a rule consuming an official open/close is governed by N-25; it is not converted into an intraday `EXIT_EVAL_SUPPRESSED` rule.** **Global exclusion** if index (SPY/QQQ) or VIX data is missing after the §5.2 gap rule. On a **globally excluded day**, ordinary new-entry evaluation is suppressed (no Stage 1–5), while exit evaluation proceeds at each scheduled exit-evaluating scan using the open position's own executable bars and the preceding P-1 boundary rule; the RISK_OFF trigger (§8.6 item 2) is unavailable and is not evaluated that day. **Pending NEXT_SESSION exception:** S0.0 resolves before S0.1. If the SPY data required by §5.5 is unavailable, the pending candidate expires unfilled as `ENTRY_UNFILLABLE_NO_FILTER`. If SPY filter inputs are available and the filter does not trip, the 09:30 fill stands; a later global exclusion caused by non-SPY inputs does not retroactively void that S0.0-resolved fill, although ordinary new-entry stages remain suppressed. **Open-position missing-bar exception:** if the position ticker has **no executable bar in the session** and universe metadata records a symbol change/delisting, §13.6's 10:00 `CORP_EVENT_FORCE_CLOSE` detection rule applies and uses its special last-available-price rule. Otherwise ordinary exit evaluation follows the P-1 boundary test above: at least one in-session executable bar at or before the boundary → evaluate on the last available bar; none → log `EXIT_EVAL_SUPPRESSED`. A suppressed scan does **not** disarm the per-position trend-failure trigger **or a RISK_OFF EXIT advisory detected at that session's 08:15 regime computation (§8.6 item 2)**. §8.6 item 5 reevaluates trend failure, **and the RISK_OFF advisory is issued, at the next scheduled non-suppressed exit-evaluating scan of the same session while the position remains open**; **an unissued RISK_OFF advisory lapses at that session's close and is re-detected at the next session's 08:15 computation if the regime remains RISK_OFF.** Excluded days are enumerated in the report header (§13.9 item 6), with P-1/P-2 provenance logged per §16.
3. **News classifier failure** — per the §11.2 trigger sets. For FP-3(c), every **timed** classification with `confidence < 0.85` carries the universal 24-hour NEWS_UNVERIFIED trigger window `0 ≤ t − published_at ≤ 24 h` in addition to any mapped G7 activation window, irrespective of score contribution; this includes `MACRO/OTHER`, BULLISH LOW, and all NEUTRAL classifications. For a backtest cache miss, where no mapped branch exists yet, the 24-hour trigger window is the sole window test. Effect: per-ticker NEWS_UNVERIFIED for that scan; Stage-5 downgrade, no resumption. For `confidence < 0.85`, score contribution is 0 but mapped G7 vetoes and §8.6 / §11.3 exit-trigger participation remain active; deterministic exit logic is unaffected. **Untimed headlines, future-dated-at-`t` headlines, trigger-window-excluded headlines, and coverage gaps never trigger NEWS_UNVERIFIED (N-21, §11.6).**
4. **Vision extraction failure / price divergence** — no state write; divergence > 3% suppresses BUY until resolved.
5. **Portfolio staleness** — never blocks a scan; age warning mandatory.
6. **Deterministic engine exception** (including §13.7 cash assertions, any `TRAIN_SUBSTITUTED_ZERO` write from a `TEST` or `LIVE` run, any run-role/provenance assertion failure in §16, **or a P-4 `NEWS_CACHE_INTEGRITY_FAILURE` caused by differing effect fields for the same `(headline_hash, ticker)`**) — full pipeline halt, mark the run non-canonical, `HERMES CRITICAL HALT`, stack trace + inputs to `run_history`, manual recovery only, no auto-restart.
7. **Fee-schedule gap at runtime** — live BUY blocked with reason `FEE_SCHEDULE_UNVERIFIED`; train-window simulation follows §9.7 item 3 instead. `TRAIN_SUBSTITUTED_ZERO` is never a live fallback.
8. **Feed-parity violation** — any decision-consumed bar with `feed ≠ sip` → run assertion failure (backtest) / DATA DEGRADED (live).
9. **Corporate-actions gap** — absence of a run-pinned verified `CORP_ACTIONS` coverage-manifest attestation for the required `(ticker, span)` → `CORP_ACTIONS_UNVERIFIED`; absence of events inside a verified no-event span is **not** a gap. Criterion-bearing OOS/test-window execution refuses unverified spans and excludes affected test windows; train-window simulation remains allowed but logged/disclosed per §15.1. Live BUY output is blocked for affected tickers.

---

## §20. Implementation Roadmap

**Phase 0 — Infrastructure and data.** Credentials (Alpaca, OpenRouter, Telegram); local SQL store; versioned universe file with Pluang-confirmation statuses; historical SEC/TAF/CAT effective-dated tables **including verified `applicable: false` spans and pass-through start dates** from public notices; **corporate-actions dataset fetched, with run-pinnable `CORP_ACTIONS` coverage-manifest attestations (including verified-zero/no-event spans) covering the required 2018+ universe-ticker + QQQ/SCHG/SPY history, and versioned (`corp_actions_version`, `manifest_version`)** — coverage confirmation is a Phase-0 prerequisite (§3.6); NEWS/EARNINGS raw coverage inventories and versioned coverage manifests captured where available; canonical OOS spans limited to fee-verified ∩ corp-actions-verified ∩ N-25-resolvable test windows; exchange calendar incl. half-days; **smoke test: exact reproduction of the §1.4 six-branch fee table**; feed-parity assertion wired; live 15-minute-boundary behavior confirmation task registered (§3.2); pre-registered news-classification label set (including `ma_role`) and vision-extraction validation sets. Required fee-schedule and corporate-actions coverage research must complete before criterion-bearing execution. **Incomplete historical SEC/TAF/CAT coverage does not prevent train-window selection computation because §9.7 item 3 defines the train-only substitution; that substitution does not satisfy, weaken, or bypass the fee-history prerequisite for criterion-bearing test-window execution.**

**Phase 1 — Deterministic engine.** Data stack, universe, regime, indicators, gates, ranking, sizing, fee model, schedule, exit logic, excluded-day rule (§19 item 2), including **P-1 required executable bars / sparse 09:40–09:43 handling / entry-only zero-volume guard / last-available exit evaluation**, **P-2 N-25 official-open/official-close substitution and daily-equity emission semantics**, **P-3 RISK_OFF same-session advisory persistence**, the explicit G6 earnings-session mapping and its non-trading-date next-session fallback (§7.2), S0.0 pending-entry resolution (§7.1), per-position trend-failure evaluation (§8.6), and symbol-change/delisting detection path (§13.6). **Exit gate:** smoke tests pass **and TA-Lib cross-validation passes at the §6 tolerance (rel. error ≤ 1e−6 after 5×period warm-up)**.

**Phase 2 — LLM layers.** **Blocked until the exact pinned `openrouter/<provider>/<model>@<version>` identifier is set.** News classifier (`news_schema_v3`, including `ma_role`) and vision extractor are validated against pre-registered sets with calibration reports. **Plus the news-cache population job:** a one-time, dedicated bulk classification pass over the run-pinned verified NEWS covered spans — the only authorized bulk live-LLM context — classifying **every timed headline irrespective of wall-clock age (N-21)** and producing the immutable cache keyed `(headline_hash, source, schema_version, model_version)`, with `keyword_override` persisted in cached payloads and its own reproducibility record (model id, `llm_config_version`, run manifest, headline count, coverage `manifest_version`, and a **cache-completeness report** verifying zero cache misses over covered spans per §11.2, plus a **P-4 cache-integrity assertion/report** proving identical effect fields for every shared `(headline_hash, ticker)` across source-keyed entries and proving that `source` was not a classifier input). The §21 item 18 prohibition on live LLM calls *during backtests* stands; this job is not a backtest.

**Phase 3 — Backtest engine.** Executable-series mechanics including P-1 bar-availability provenance, P-2 N-25 official-price substitutions and no-sample daily equity handling, and P-3 suppressed-scan advisory persistence; 6+1 scenario grid; slippage grid; fee/rounding/VAT branches; FX reporting grid; corporate actions per §3.6 including **ex-date dividend entitlement/attribution and `dividends_net`**, `CORP_EVENT_FORCE_CLOSE` timing/price/fees, S0.0 NEXT_SESSION void/unfillable states, gap-through price semantics, per-position trend-failure detection, `ENTERED_BEYOND_STOP` handling, provisional fee- and corp-actions-span segregation, cash assertions, event logging/provenance (§16), and disclosure block (§13.9). **For partially fee-verified test windows, provisional fee-span segregation is enumeration-only: record the affected window and each unverified span/day; produce no simulated metrics, perform no partial-window trade subsetting, and never apply `TRAIN_SUBSTITUTED_ZERO` in a test-window run (§9.7 item 4, §13.8).**

**Phase 4 — Walk-forward run.** Blocked until the fully enumerated `search_space.yaml` **and** `pre_registration_manifest.yaml` with concrete `coverage_end` are committed at the pre-registration commit (§15.1). N-24 per-window harness; `run_role` is explicit. Train-window selection follows §15.1 and applies §9.7 `TRAIN_SUBSTITUTED_ZERO` wherever a required SEC/TAF/CAT input is unverified; affected gate calculations, trades, components, fee-computation dates, and contexts are logged per §16 and disclosed per §13.9 items 19–20. Frozen criteria are evaluated only on fully fee-verified ∩ corp-actions-verified ∩ N-25-resolvable test windows per §15.4. Every test-window run asserts zero `TRAIN_SUBSTITUTED_ZERO` writes. Criteria decide progression only; they do not prove edge.

**Phase 5 — Paper trading.** (1) **Mechanism validation:** latency and failure-to-exit measurement; broker receipt reconciliation (fee rounding scope, VAT applicability, fractional minimums, share precision); Pluang↔Alpaca divergence; FX cost; live 15-minute-boundary behavior. (2) **Long-run edge validation:** open-ended; weeks of paper trading do **not** validate profitability. Optional broker-state integration remains future/optional and never autonomous.

---

## §21. What We Should Not Build

1. No autonomous order placement.
2. No LLM authority over deterministic decisions; classification/extraction/narrative only.
3. No LLM direct trade execution.
4. No active Gate B; no shadow logging of undefined expected-edge quantities.
5. No active multi-position admission (`max_positions = 1`).
6. No active correlation logic; correlation is not a substitute for the missing aggregate-risk admission rule.
7. No invented aggregate portfolio-risk cap.
8. No invented per-ticker cap.
9. No weighted execution probabilities; measure them in paper trading.
10. No global minimum-viable-capital gate; viability is G9/G10 only.
11. No min-notional clamp-up; `raw_notional < $10` rejects.
12. No fee-derived minimum stop; Gate A serves the purpose.
13. No VAT on SEC/TAF/CAT in the canonical branch.
14. No backward projection of **SEC/TAF/CAT regulatory pass-through rates**; unverified spans are provisional and excluded from canonical OOS. The train-only `TRAIN_SUBSTITUTED_ZERO` mechanism (§9.7 item 3) is a non-factual research-selection convenience, **not** a projected historical rate and not a verified-zero schedule entry. This does not negate the explicitly disclosed FP-8 baseline choice to backward-project current broker transaction/JFX-KBI fees and VAT (§13.9 item 18).
15. No total-return prices as fill prices.
16. No future paper-trading observations inside historical backtests.
17. No same-window fitting.
18. No live LLM calls during backtest (cache replay only; the Phase-2 cache-population job is the sole bulk-call context).
19. No silent discards in walk-forward search.
20. No auto-restart after a deterministic engine exception.
21. No presentation of research thresholds as validated strategy truths.
22. No **exit** fills before detection, and no **exit** fills at the stop level itself — gap-through exit pricing is price-at-scheduled-timestamp only. **Entry** fills at or below `stop_price` remain permitted under §13.2 `ENTERED_BEYOND_STOP`.
23. No re-anchoring of stops/targets to fill prices; notional invariance is fixed.
24. No same-scan exit-plus-entry, and no negative-cash states.
25. No source whitelist in the CRITICAL confirmation path; two independent sources or no EXIT advisory.
26. No treatment of the §1.3 structural constraints as tunable — they are arithmetic and must be disclosed wherever the affected runs appear.
27. **No wall-clock staleness in any decision path** — news windows are evaluated against the simulated decision timestamp only (N-21).
28. **No derivation of splits/dividends from adjusted÷unadjusted price quotients** — corporate actions come exclusively from the §3.6 contract.
29. **No equity carry-over across walk-forward windows** — each window is an independent run (N-24).
30. **No entry-side exclusion solely for absent 09:40–09:43 bars; no exit suppression from `VWAP_UNDEFINED_ZERO_VOLUME`** — P-1 required bars and exit boundary semantics are exactly §19 item 2.
31. **No synthetic/carry-forward daily equity sample when the required daily executable bar is absent** — N-25 governs official-price substitution and emitted-sample drawdown semantics.
32. **No disarming a same-session RISK_OFF EXIT advisory merely because an exit-evaluating scan was suppressed** — it persists to the next non-suppressed exit-evaluating scan of that session and otherwise lapses at session close (§19 item 2).
33. **No source-dependent news effect classification and no arbitrary same-hash duplicate selection** — effect fields depend only on normalized headline text + ticker; a same-`(headline_hash,ticker)` mismatch is a halting cache-integrity failure (§11.2, §19 item 6).

---

## §22. FULL Consolidated Epistemic Table

<details>
<summary><strong>Complete epistemic ledger (expand)</strong></summary>

| Item | Epistemic status | Canonical note |
|---|---|---|
| Strategy edge / profitability | NOT VALIDATED | No claim of profit, safety, or live readiness |
| Objective: build backtest | DECIDED | Authorizes construction only |
| `max_positions = 1`; long-only; no leverage | DECIDED | Baseline structure |
| Starting live capital ≈ $61 | DECIDED baseline / user constraint | Post-FX USD basis (N-15) |
| Canonical capitals $61 and $200 | DECIDED | $200 criterion-bearing; $61 disclosure-only (N-16) |
| $61 G9∧G10 infeasibility outside RISK_ON; RISK_ON ATR band | STRUCTURAL ARITHMETIC CONSEQUENCE — DISCLOSED | §1.3.1; not tunable |
| $200 minimum-ATR% admissibility floor (≈1.4–1.5% canonical disclosure) | STRUCTURAL ARITHMETIC CONSEQUENCE — DISCLOSED | §1.3.2; not tunable; header-mandatory |
| Broker route; fractional support; $1 minimum | KNOWN | Published broker behavior |
| Fractional share precision | MUST CONFIRM WITH BROKER | Simulator default 4 dp truncation |
| Fractional regulatory fee minimum applicability | MUST CONFIRM WITH BROKER | Load-bearing for §1.4 margin — disclosed |
| Recommendation-only requirement | DECIDED | User requirement |
| Pluang MCP existence / scope | KNOWN exists / MUST CONFIRM scope | Optional, future |
| Screenshot ingestion baseline | DECIDED | Sizing divergence DISCLOSED |
| Alpaca free-tier SIP behavior | KNOWN documented | Hard feed-parity assertion required |
| Live behavior at exact 15-min boundary | MUST CONFIRM | Deterministic retry fallback defined (§3.2) |
| **Corporate-actions data contract** | DECIDED | §3.6; ratio back-out prohibited |
| **Corporate-actions provider (Alpaca endpoint) coverage/depth** | MUST CONFIRM | Verification predicate DECIDED via run-pinned `coverage_manifests`; verified no-event attestations allowed; unattested spans → `CORP_ACTIONS_UNVERIFIED` |
| Corporate-action / earnings dates as-revised | DISCLOSED BIAS | No PIT snapshot in baseline |
| Finnhub news & earnings history depth | MUST CONFIRM | Coverage rules §11.6 / §7.2 G6 |
| VIX source FRED VIXCLS | KNOWN available | Gap rule DECIDED (§5.2); adequacy MUST TEST |
| Exact pinned LLM identifier | MISSING / UNSPECIFIED | Phase-2 prerequisite; E-10: not itself a blocker to R2.7 independent patch verification |
| Exchange calendar package | DECIDED | Manual override fallback |
| Bar-start labeling; inclusive 15-min rule | DECIDED (N-01/N-02) | Normative |
| Three-series model + consumer assignment | DECIDED (N-11) | Accounting series = reporting context only (no consumer ambiguity) |
| **"Official close/open" definition** | DECIDED (N-22/N-25) | Daily executable bar close/open; absent/non-trading nominal dates resolve by N-25; missing required-daily-bar sessions emit no daily equity sample |
| Staleness kill switch | DECIDED | Not simulated — DISCLOSED |
| Universe v1.0; leveraged exclusion; continuity metadata | DECIDED | Pluang confirmation MUST CONFIRM BY USER |
| Survivorship bias | DISCLOSED BIAS | Upward, non-eliminable |
| Regime engine rules | DECIDED | Parameters ASSUMPTION / MUST TEST |
| VIX threshold 25; multipliers 1.0/0.5 | ASSUMPTION / MUST TEST | — |
| Regime caps 5/3/2 | NOT IN EFFECT | Non-normative annotation |
| Opening-drop: SPY, global, Stage 0 S0.0/S0.4, **both legs executable** | DECIDED (N-12, RP-09, P-A-04) | Threshold −2% ASSUMPTION / MUST TEST; pending NEXT_SESSION resolved before S0.1 |
| Indicator parameters | ASSUMPTION / MUST TEST | Closed forms DECIDED |
| Zero-volume VWAP guard | DECIDED (P-1) | Entry-side Stages 1–5 exclusion only; never suppresses exit evaluation; deterministic |
| Volume-pace split artifact | DISCLOSED | §3.3 / §13.9 item 13; deliberately not repaired |
| EMA/Wilder seeding | DECIDED | Convention |
| TA-Lib validation, Phase-1 gate, 1e−6 | DECIDED | — |
| Hard gates binary | DECIDED | Structural |
| Earnings blackout 3 trading days | DECIDED rule (P-A-02; I-1 deterministic cleanup) | `d(e)` timing mapping explicit; before-market-open/unspecified events dated on a non-trading day map to the next trading day; G6 fails on `{T,T+1,T+2}` trading-day indices; ETF behavior and coverage rule unchanged; as-revised DISCLOSED BIAS |
| Gate A 25%; $10 floor; 90% cap; `risk_per_trade` 1% | ASSUMPTION / MUST TEST | — |
| Gate B | DEFERRED / REMOVED | No shadow logging |
| Score weights; cutoffs 70/80 | ASSUMPTION / MUST TEST | Perturbation check required |
| Integer rounding; ticker tie-break | DECIDED (N-13) | — |
| Bands; raw score hidden | DECIDED display | — |
| Pipeline order incl. Stage 0 | DECIDED | Normative |
| Stage-5 no-resumption | DECIDED | — |
| Stop anchor = reference; fixed; non-trailing | DECIDED (N-06) | `stop_mult 2.5` ASSUMPTION / MUST TEST |
| Take-profit 2R | ASSUMPTION / MUST TEST | — |
| Exit priority; twice-daily close-based checks | DECIDED | Non-detection DISCLOSED |
| Trend-failure 2 closes < EMA50 | ASSUMPTION / MUST TEST; trigger semantics DECIDED (P-A-05) | Per open position from entry fill; earliest non-suppressed exit-evaluating scan; suppressed scan does not disarm |
| TRIM backtest no-op | DECIDED simplification | Disclosed |
| Notional-invariant fills; fees on actuals; screening vs actual risk | DECIDED (N-03/05/07) | Divergence distribution reported |
| **Signed `realised_risk_actual`; `ENTERED_BEYOND_STOP`** | DECIDED (RP-10) | §13.2; deterministic |
| **`stop_overshoot`, `risk_divergence` formulas** | DECIDED (RP-10) | §15.2 |
| Gate-A flat-exit assumption | DECIDED (N-08) | Screening convention |
| Gap-through = price-only | DECIDED (N-09) | Overshoot reported |
| Same-scan lockout; cash assertions | DECIDED (N-10) | Violations halt engine |
| **Excluded-day / required-bar rule: entry-side exclusion, exits evaluated on last available bar where possible** | DECIDED (RP-06, P-1, P-3) | Required bars = 09:30–09:39 + 09:44; 09:40–09:43 gaps alone do not exclude; `EXIT_EVAL_SUPPRESSED` only when no in-session bar exists at/before the N-02 boundary; RISK_OFF unavailable only on globally excluded days and otherwise persists across suppressed scans within-session |
| Global min-viable-capital constructs | REMOVED | Do not resurrect |
| Fee formulas / rates | Current published formulas KNOWN; historical broker/JFX-KBI/VAT applicability MUST CONFIRM | SEC/TAF/CAT effective-dated; broker transaction/JFX-KBI/VAT current constants backward-projected and DISCLOSED (§13.9 item 18) |
| VAT interpretation | DECIDED / MUST CONFIRM WITH BROKER | Not KNOWN |
| Rounding canonical + branches | DECIDED / MUST CONFIRM WITH BROKER | Six-branch targets §1.4 |
| Historical rates & pass-through start dates | MUST CONFIRM (public notices) | Verified-zero encoding DECIDED |
| Provisional / unverified SEC/TAF/CAT spans | **PROVISIONAL historical state / TEST EXCLUDED** | No historical rate or verified zero is inferred or backward-projected. Train-window computation may use `TRAIN_SUBSTITUTED_ZERO` only under §9.7 item 3; partially fee-verified test windows are enumeration-only with no metrics. |
| **Fee-verified window = all-days rule; partial windows excluded** | DECIDED (RP-05) | §9.7 item 4 |
| <8 verified windows → NOT EVALUABLE | DECIDED | Jointly with corp-actions verification |
| Dividend withholding 30% default | ASSUMPTION | Treaty rate MUST CONFIRM; ex-date entitlement DECIDED (P-A-01); benchmark uses same rate but terminal liquidation remains asymmetric (§15.3). **C-1 scope disclosure:** accepted P-A-01 amended strategy §13.6/§15.1 only; under R2.4 a benchmark dividend whose pay date followed window end was not credited, while the retained N-24 benchmark rule credits it at the window-final official close. This can change benchmark terminal equity and the §15.3 item 4 benchmark `maxDD_max` used by §15.4 criterion 3. |
| FX cost 0.5%; grid; post-FX basis | ASSUMPTION / MUST MEASURE; basis DECIDED (N-15) | Reporting layer only |
| Schedule & half-days | DECIDED | DST-aware |
| Scenario grid 6+1; entry/exit definitions | DECIDED (N-20) | Probabilities MUST MEASURE |
| DELAYED voiding availability-bounded; intrabar-vs-close asymmetry | DECIDED / DISCLOSED | §13.2, §13.9 item 11 |
| Half-day DELAYED sequencing | DECIDED (FP-6) | 12:00 fill after 12:00 exit scan; first exit evaluation next trading day 10:00 |
| NEXT_SESSION S0.0 resolution + window-end behavior | DECIDED (P-A-04) / disclosed reported-only overlap | Resolve before S0.1; `NEXT_SESSION_VOIDED` / `ENTRY_UNFILLABLE_NO_FILTER`; standing 09:30 fill exit-eligible at 10:00; outside-window → `ENTRY_UNFILLABLE_WINDOW_END` |
| No EXIT_MISSED | DISCLOSED / MUST MEASURE | — |
| Slippage grid | ASSUMPTION / MUST MEASURE | — |
| Corporate-action mechanics; dividend attribution; delisting/symbol-change force-close; compounding ON within run | DECIDED (P-A-01/P-A-03) | Ex-date dividend entitlement; deterministic force-close detection/timestamp/price/slippage/fees; §13.6, §13.7 |
| Live-vs-backtest sizing-input divergence | DISCLOSED | §13.7 / §13.9 item 9 |
| T+1; Pluang price basis; DEGRADED days | DISCLOSED parity gaps | §13.9 |
| Live BUY-suppression paths absent from backtest | DISCLOSED (FP-8; R2.4 cross-reference cleanup) | Vision failure, >3% reconciliation divergence, live fee-schedule block; train runs may additionally price missing SEC/TAF/CAT inputs as `TRAIN_SUBSTITUTED_ZERO`; §13.9 items 17/20 |
| **News clock = decision timestamp** | DECIDED (N-21) | Wall clock never decision-effective |
| **Live 7-day ingestion guard** | DECIDED (live only) | Live↔backtest divergence DISCLOSED (§13.9 item 15) |
| News schema/effect mapping/windows/integer points/aggregation | DECIDED (P-4 consistency added) | `news_schema_v3`; `ma_role`; ordered total function; **first-match exclusivity intentional where branches overlap** (§11.2, Audit B CP-1); effect fields are a function only of normalized headline text + ticker; source-independent headline de-duplication; same-hash/ticker mismatches halt as cache-integrity failures |
| **Backtest NEWS_UNVERIFIED trigger set; cache miss ≠ coverage gap** | DECIDED / IMPLEMENTABLE (RP-02, FP-5) | Raw `news_headlines` inventory + run-pinned NEWS coverage manifests; §11.2 |
| News confidence 0.85 | ASSUMPTION pending calibration | ≥200-headline validation required; **effect scope DECIDED**: <0.85 → 0 score, veto/exit participation retained; every timed classification carries the universal 24-hour NEWS_UNVERIFIED trigger window plus any mapped G7 window (§11.2) |
| Keyword fallback; two-source CRITICAL rule | DECIDED | NFKC+case-insensitive substring; forces BEARISH+CRITICAL at classification; `keyword_override` cached; source-independent hash + distinct-source confirmation |
| Coverage neutral-disable + sensitivity pairs | DECIDED | News/earnings covered spans come from run-pinned verified `coverage_manifests` |
| Raw headline inventory + coverage manifests | DECIDED (FP-5) | `news_headlines`; `coverage_manifests` for NEWS/EARNINGS/CORP_ACTIONS/FEE_SCHEDULE; versions pinned/logged |
| LLM cache replay only; cache-population job + completeness/integrity report | DECIDED (P-4) | Cache key `(headline_hash, source, schema_version, model_version)`; `source` is cache/confirmation metadata only, never classifier input; same-`(headline_hash,ticker)` effect equality asserted; sole bulk-call context |
| Walk-forward 24m/6m/6m; 2018+; fixed `coverage_end`; ≥8 eligible verified windows; train-only refit | PRE-REGISTERED RESEARCH DECISION | `coverage_end` committed in pre-registration manifest; eligible test-window count additionally excludes N-25-unresolvable windows; never run-date-derived |
| Train-window fee/corp-actions verification treatment | PRE-REGISTERED RESEARCH DECISION (FP-7; R2.3 EB-01 consolidated) | Train is not criterion-bearing. `CORP_ACTIONS_UNVERIFIED` remains logged/disclosed and non-blocking for train selection; missing SEC/TAF/CAT inputs use §9.7 `TRAIN_SUBSTITUTED_ZERO` by run role, with provenance. Test windows remain verified-only / enumeration-only when partially fee-verified; N-25-unresolvable test windows use the same acceptance-exclusion semantics. |
| `TRAIN_SUBSTITUTED_ZERO` | **DECIDED RESEARCH-SELECTION CONVENIENCE / NOT A HISTORICAL FACT** | Exactly $0.00; train-window fee computation only; may bias selected parameters; logged per candidate/trade/component/date/context; never changes coverage verification and never appears in TEST or LIVE fee rows. |
| **Per-window independent-run equity basis; window-end force-close** | PRE-REGISTERED RESEARCH DECISION (N-24; P-2/N-25 resolution) | Trade-determining; official-price substitution and missing daily-sample handling follow N-25 |
| Selection objective / tie-breaks / zero-trade carry-forward | PRE-REGISTERED RESEARCH DECISION | §15.1 |
| **`net_expectancy` = equal-weighted mean USD/trade; pooled aggregates** | PRE-REGISTERED RESEARCH DECISION (N-23) | R-multiple reported-only |
| Canonical evaluation cell incl. news setting | PRE-REGISTERED RESEARCH DECISION (N-17) | Selection and acceptance |
| Acceptance criteria + zero-trade rule + **per-window fractional maxDD_max statistic** | PRE-REGISTERED RESEARCH DECISION | Fractional peak-to-trough definition §15.3; gates paper trading only |
| Benchmark per-window construction | PRE-REGISTERED RESEARCH DECISION | §15.3; E-07 terminal-liquidation asymmetry disclosed |
| Bootstrap CI (iid, B=10k, `default_rng(42)`/PCG64) | PRE-REGISTERED RESEARCH DECISION | E-02: input-vector ordering not normatively pinned, so bit-identical CI replay is not guaranteed across different pooling orders; CI is reporting-only |
| `search_space.yaml` grid values | MISSING | Phase-4 prerequisite; pre-registration incomplete until committed; E-10: not itself a blocker to R2.7 independent patch verification |
| `pre_registration_manifest.yaml` concrete `coverage_end` | MISSING PRE-REGISTRATION ARTIFACT | Phase-4 / OOS prerequisite; E-10: not itself a blocker to R2.7 independent patch verification |
| <100 trades → INSUFFICIENT SAMPLE | PRE-REGISTERED RESEARCH DECISION | ≥100 ≠ validation |
| Screenshot thresholds 0.90 / ±1.5% / 3% | ASSUMPTION / MUST TEST | Divergence stats MUST MEASURE |
| Staleness uses last known value + warning | DECIDED | Never blocks scan |
| Confirmation parsing; day-end expiry | DECIDED behavior | Schema MISSING (live phase) |
| Failure-mode priorities | DECIDED | Deterministic |
| Architecture stack; roadmap | DECIDED | Build sequence |
| Correlation parameters | DORMANT / MUST TEST | Not active |
| Aggregate-risk admission for ≥2 positions | UNRESOLVED / DEFERRED | Correlation not a substitute |
| Fee-derived minimum stop | REMOVED | — |
| Telegram fee metric = Gate A; published stop = canonical stop | DECIDED | Parity by construction |
| Paper trading ≠ profitability validation | DECIDED epistemic rule | Open-ended |

</details>

---

## §23. Specification Status

**Current status:** `R2.8 — R2.7 TARGETED EARNINGS-SOURCE CLARIFICATION APPLIED; READY FOR INDEPENDENT PATCH VERIFICATION`

R2.8 preserves the R2.7 authoritative baseline normatively unchanged except for the accepted targeted earnings-source clarifications **A-1 through A-4, with corrections C-1 through C-3** (§3.1 earnings-provider row split, new §3.7 earnings-source data contract, §13.9 item 21 divergence disclosure). The universe, regime logic, G1–G5, existing G6 `d(e)` mapping and timing branches and blackout window, G7–G10, scoring, news classification and News Score, sizing, stop/target/exit logic, fee logic and regulatory fees, portfolio accounting, walk-forward structure, TRAIN/TEST definitions, PASS/FAIL/NOT EVALUABLE criteria, benchmarks, drawdown rules, market-data / corporate-actions / VIX / LLM-classifier provider policy, search space, `coverage_end`, and execution assumptions are not reopened. P-1 through P-4, C-1, C-2, I-1, I-2, P-A-01 through P-A-05, FP-1 through FP-8, EB-01, TRAIN_SUBSTITUTED_ZERO, fee/train-substitution design, news effect mapping policy, coverage/PIT design, walk-forward architecture, acceptance criteria, thresholds, scoring weights, sizing, fee rates, and benchmark policy are preserved without reopening. **R2.6 audit category-E findings E-1 through E-13 are explicitly not applied in R2.8 and remain reserved for a later editorial pass. No final freeze is claimed.**

All previously recorded R2/R2.1/R2.2/R2.3/R2.4/R2.5/R2.6/R2.7 readiness or blocker statuses are **RETIRED / SUPERSEDED** as current-document statuses by this R2.8 status.

**Missing / prerequisite register** (these are gated artifacts or external confirmations, not reopened strategy policy):

| Missing / pending item | Blocks |
|---|---|
| `search_space.yaml` exact grid values | Phase 4; pre-registration completeness; **no OOS evaluation before commit** (§15.1) |
| `pre_registration_manifest.yaml` concrete `coverage_end` | Phase 4 / any OOS evaluation; fixes the walk-forward window set (§15.1) |
| Pinned `openrouter/<provider>/<model>@<version>` identifier | Phase 2 completion |
| Historical SEC/TAF/CAT fee-schedule and pass-through coverage research | Criterion-bearing OOS/test-window execution over unverified fee spans (§9.7, §20 Phase 0); does **not** block train-window selection computation because train runs use the explicitly non-factual §9.7 substitution |
| Corporate-actions provider coverage confirmation and verified coverage manifests | Criterion-bearing OOS/test-window execution over unattested spans (`CORP_ACTIONS_UNVERIFIED`) |
| Execution-event schema / confirmation grammar | Live-phase completion only |
| Full non-BUY Telegram templates | Live-phase completion only |
| Complete adjudication history beyond §2 | Documentation completeness only |
| Exact Phase-4 persistence schemas for run↔window boundaries, per-run `initial_capital`/scenario cell, and configuration-evaluation log | Phase-4 reconstruction completeness; E-03 disclosure; no strategy-policy change |

**E-10 status clarification:** `search_space.yaml` grid values, the concrete `coverage_end`, and the exact pinned OpenRouter model identifier remain **phase prerequisites** exactly as stated above. Their absence does not itself prevent producing or independently patch-verifying this R2.8 targeted revision; it continues to block the phases/OOS work already identified. No missing value is invented here. R2.6 audit category-E findings E-1 through E-13 remain unapplied.

**Standing caveats:** canonical OOS results are valid only over fee-verified ∩ corp-actions-verified ∩ **N-25-resolvable test windows**; partially fee-verified test windows are enumeration-only, produce no simulated metrics, and are excluded entirely from acceptance calculations; fewer than 8 verified windows → NOT EVALUABLE. Train-window simulation is not criterion-bearing: `CORP_ACTIONS_UNVERIFIED` spans remain logged/disclosed and non-blocking, while missing SEC/TAF/CAT inputs use the run-role-scoped `TRAIN_SUBSTITUTED_ZERO` mechanism (§9.7, §15.1). The $61 run is structurally constrained per §1.3.1 and not criterion-bearing; the $200 run carries the §1.3.2 ATR floor, and neither §1.3 band describes train-window G9 geometry on dates using substituted regulatory components. Broker transaction/JFX-KBI fees and VAT are current constants backward-projected across the backtest and disclosed (§13.9 item 18). No claim is made that the strategy is validated, profitable, safe, OOS-passed, paper-trading-passed, broker-confirmed, provider-confirmed, production-ready, or live-ready.

---

## HISTORICAL R2.4 RELEASE-GATE SELF-CHECK — NON-NORMATIVE

This table is retained solely as a **NON-NORMATIVE HISTORICAL AUTHOR SELF-CHECK** from R2.4. Its `PRESERVED`, `APPLIED`, `PASS`, and readiness labels are historical author claims, not evidence and not current R2.8 status. Where inherited R2.5 P-A-01 through P-A-05 or E-01 through E-10 supersede a statement below, the R2.8 normative sections control.

| Finding / closure | Historical R2.4 author resolution | Historical status |
|---|---|---|
| D-01 — news staleness clock | N-21 remains binding: all news time tests use simulated decision timestamp `t`; cache population is wall-clock-age-independent; live 7-day ingestion guard remains live-only and disclosed (§11.1, §13.9 item 15). | PRESERVED |
| D-02 / A-2 / A-6 — NEWS_UNVERIFIED implementability | §11.2 retains raw `news_headlines`, run-pinned NEWS covered-span manifests, source-qualified cache identity, and explicit low-confidence behavior; Audit B FP-3(c) now makes the universal 24-hour NEWS_UNVERIFIED trigger scope explicit for every timed classification and for cache misses. | R2.4 CORRECTION APPLIED |
| A-1 — effect mapping not total / M&A role missing | `news_schema_v3`, `ma_role`, and the ordered total function remain unchanged; Audit B CP-1 now explicitly documents intentional first-match exclusivity and its overlap consequences. | PRESERVED + DISCLOSURE CLARIFIED |
| A-3 / A-5 — headline identity and keyword fallback | Source-independent SHA-256 headline hash, source-qualified cache key, and deterministic BEARISH+CRITICAL keyword override remain unchanged. | PRESERVED |
| A-4 / D-07 — corporate-actions verification predicate | Versioned attestations, verified no-event spans, `CORP_ACTIONS_UNVERIFIED`, and all-days test-window rule remain unchanged. | PRESERVED; provider coverage MUST CONFIRM |
| A-8 / E-8 / E-9 — exit/fill timing gaps | R2.4 stated that trend-failure timing, NEXT_SESSION outside-window expiry, and half-day DELAYED sequencing remained unchanged. | HISTORICAL; trend-failure timing and NEXT_SESSION resolution are superseded where required by R2.5 P-A-05/P-A-04; window-end expiry and half-day DELAYED sequencing remain preserved |
| D-03 / A-7 / A-9 — walk-forward drawdown/window set/train verification | N-24, fractional drawdown, and fixed pre-registered `coverage_end` remain unchanged; train fee gaps now have deterministic, run-role-scoped §9.7 substitution while test windows remain verified-only. | R2.4 CORRECTION APPLIED |
| D-04 — net expectancy unit/estimator | N-23 equal-weighted realised net USD P&L/trade, pooled aggregates, R-multiple reported-only. | PRESERVED |
| D-05 — fee-verification granularity | §9.7 all-days rule retained; partially fee-verified test windows are now explicitly enumeration-only with no simulated metrics and no partial-window trade subsetting. | R2.4 CLARIFICATION APPLIED |
| D-06 — globally excluded days | §19 item 2 retained: entries suppressed, exits evaluated where exit data are available. | PRESERVED |
| D-08 — official close/open | N-22 retained: daily executable bar close/open. | PRESERVED |
| D-09 — opening-drop price spaces | §5.5 / §3.3 retained: both legs executable, ex-date handling from §3.6. | PRESERVED |
| D-10 — metric formulas / signed actual risk | §15.2 formulas, N-07 signed `realised_risk_actual`, and `ENTERED_BEYOND_STOP` retained. | PRESERVED |
| E-1 / D-1 — disclosures | §13.9 items 17–20 now reconcile live-only BUY suppressions, backward-projected broker/JFX-KBI/VAT constants, train-window verification, train fee substitution, and the §1.3 train-geometry caveat. | R2.4 CLEANUP APPLIED |
| D-11 — stale references | R2.4 stated that revision identity, schema/provenance, NEWS_UNVERIFIED scope, fee semantics, walk-forward role semantics, epistemic rows, and status language had been swept for its then-accepted corrections. | HISTORICAL AUTHOR CLAIM; superseded as evidence by the independent R2.4 release-gate audit and the R2.5 patch |
| Non-blocking 1 — $200 ATR floor | Disclosed as §1.3.2 structural consequence; header-mandatory (§13.9 item 12), with the train-substitution caveat in §1.3/§13.9 item 20. | PRESERVED / CLARIFIED |
| Non-blocking 2 — volume-pace split artifact | Disclosed (§3.3, §13.9 item 13); series assignment unchanged. | PRESERVED |
| Non-blocking 3 — N-17 news setting | News/earnings gates enabled where covered for selection and acceptance. | PRESERVED |
| Non-blocking 4 — accounting-series role | Accounting series remains reporting context only; not a benchmark input. | PRESERVED |
| Non-blocking 5 — voiding intrabar vs close-based exits | Disclosed (§13.9 item 11). | PRESERVED |
| Non-blocking 6 — NEXT_SESSION overlap | Disclosed (§13.9 item 16). | PRESERVED |
| Non-blocking 7 — zero-volume VWAP | §6.1 ticker-level exclusion retained. | PRESERVED |
| Non-blocking 8 — bootstrap RNG identity | §15.2 `default_rng(42)` / PCG64 retained. | PRESERVED |
| Non-blocking 9 — 15-min boundary | §3.2 MUST CONFIRM + deterministic live retry fallback retained; Phase-0/5 tasks retained. | PRESERVED |

**Historical classification (superseded):** `R2.4 — CONSOLIDATED AUDIT REPAIRS APPLIED; READY FOR INDEPENDENT RELEASE-GATE RE-AUDIT`

This classification is retained only as historical provenance. It is **not** the current R2.6 status and is not evidence that R2.4 had no remaining contradiction. It did not assert final freeze, release approval, strategy validation, profitability, OOS success, paper-trading success, broker confirmation, provider confirmation, or live readiness.

---

## HISTORICAL R2.4 CONSOLIDATION VERIFICATION — NON-NORMATIVE

This retained block is a **NON-NORMATIVE HISTORICAL AUTHOR SELF-CHECK** from R2.4; none of its labels is current release-gate evidence.

- Historical author claim: R2.2 mechanisms not implicated by Audit A or Audit B were preserved normatively.
- Historical author claim: the intended R2.3 EB-01 train-selection repair was integrated subject to Audit A P1–P6 corrections.
- Historical author claim: Audit A P1–P6 were applied.
- Historical author claim: Audit B FP-3(c) was applied.
- Historical author claim: Audit B CP-1 was applied with ordered mapping unchanged.
- Historical author claim: the then-targeted stale-status/cross-reference cleanup had passed its author sweep.
- Historical provenance only: R2.4's author considered Audit A and Audit B mutually consolidatable. This statement does not address the later independent R2.4 release-gate findings A-01 through A-05.

---

## HISTORICAL R2.4 CONSOLIDATED REPAIR RECORD — NON-NORMATIVE

| Source audit | Finding / patch identifier | Historical R2.4 section(s) changed | Historical applied correction |
|---|---|---|---|
| Audit A (adjudicating R2.3) | EB-01 accepted mechanism | §9.7, §13.9, §15.1, §16, §20, §22, §23 | Integrated the train-selection-only, explicitly non-factual zero-substitution mechanism for missing SEC/TAF/CAT inputs while preserving verified-only test-window acceptance and live fee-gap blocking. |
| Audit A | P1 — fee-computation date | §9.7 item 1; §16 | Defined the regulatory fee-computation date by fee context: decision date for Gate-A/G9 screening and fill trading date for actual fee pricing; rate selection and verification-state determination use the same date. |
| Audit A | P2 — true zero substitution | §9.6; §9.7 item 3; §16 | `TRAIN_SUBSTITUTED_ZERO` is exactly $0.00; the component is omitted from schedule minima/cap/rounding and records a zero-valued provenance row. |
| Audit A | P3 — run-role scope | §9.7 item 3; §15.1; §15.4; §16; §19; §20 | Made substitution a `TRAIN` run-role property rather than a date property; TEST/LIVE writes are forbidden and halt deterministically. |
| Audit A | P4 — provisional test-window branch | §9.7 item 4; §13.8; §15.4; §20 Phase 3 | Defined provisional fee-span segregation as enumeration of partially fee-verified test windows/spans/days only; no simulated metrics, no partial-window trade subsetting, no test substitution. |
| Audit A | P5 — schema/provenance consistency | §9.7; §13.9 item 18; §16 | Restored FEE_SCHEDULE coverage-attestation semantics; added truthful `BACKWARD_PROJECTED_CONSTANT`; preserved existing `sim_trades` flags/annotations additively; added candidate/trade/component/date/context provenance and TEST/LIVE assertions. |
| Audit A | P6 — disclosure/epistemic/status cleanup | §1.3; §13.9 items 17–20; §22; §23; trailing continuity records | Reconciled train-fee disclosures and structural-band caveat; retired duplicate/stale fee-treatment and blocker/status text; aligned the epistemic table and final status. |
| Audit B | FP-3(c) deterministic correction | N-21; §11.1; §11.2; §19; §22; release-gate continuity record | Made the 24-hour NEWS_UNVERIFIED trigger window universal for every timed classification regardless of score-bearing effect; mapped G7 windows remain additional tests; cache miss uses the 24-hour test alone. |
| Audit B | CP-1 disclosure/documentation cleanup | §11.2; §22 | Made intentional first-match exclusivity explicit, including BEARISH-CRITICAL∩M&A/TARGET and MACRO/OTHER∩BEARISH-CRITICAL consequences, without changing mapping order or behavior. |

---

## HISTORICAL R2.4 EDITORIAL CONSISTENCY SWEEP — NON-NORMATIVE

This retained table is a **NON-NORMATIVE HISTORICAL AUTHOR SELF-CHECK** from R2.4. Its `PASS` / `NO DIRECT CONFLICT` cells are historical claims only and were explicitly rejected as sufficient evidence by the independent R2.4 release-gate audit. It is retained for provenance, not as a current assertion.

| Directly affected surface | Result |
|---|---|
| §9.6 / §9.7 fee semantics | HISTORICAL AUTHOR CLAIM —  true-zero semantics, fee-computation date, three historical states, coverage-attestation rule, train-only substitution, and test/live prohibitions are mutually consistent. |
| G9 / `fees_rt` consumers | HISTORICAL AUTHOR CLAIM —  train screening uses decision-date fee state; substituted regulatory components are exactly zero; gate formula/threshold unchanged. |
| §13.7 fee debit behavior | HISTORICAL AUTHOR CLAIM —  debit timing/accounting is unchanged; actual sell-side regulatory rate state is selected by exit fill date and flows through the existing debit path. |
| §13.8 sensitivity / provisional-span behavior | HISTORICAL AUTHOR CLAIM —  partially fee-verified test windows are enumeration-only; no undefined test-window fee computation remains. |
| §13.9 disclosures | HISTORICAL AUTHOR CLAIM —  items 17, 19, and 20 are reconciled; backward-projected constants and §1.3 train-geometry caveat are explicit. |
| §15.1 train/test role semantics | HISTORICAL AUTHOR CLAIM —  substitution is run-role-scoped; the same calendar date may differ across independent TRAIN and TEST runs. |
| §15.4 criterion-bearing scope | HISTORICAL AUTHOR CLAIM —  only fully verified test windows carry acceptance metrics; `TRAIN_SUBSTITUTED_ZERO` is prohibited. |
| §16 schemas and provenance | HISTORICAL AUTHOR CLAIM —  fee-input status is total/truthful; existing trade flags survive; candidate/trade/component/date/context reconstruction is defined; TEST/LIVE assertion is explicit. |
| §19 failure modes | HISTORICAL AUTHOR CLAIM —  illegal substitution writes halt; live fee gap remains BUY-blocking; FP-3(c) scope is explicit. |
| §20 implementation phases | HISTORICAL AUTHOR CLAIM —  train computation may proceed with substitution; criterion-bearing fee-history prerequisite remains; provisional test-window reporting is enumeration-only. |
| §21 invariants | HISTORICAL AUTHOR CLAIM —  no historical SEC/TAF/CAT backward projection is introduced; train substitution is explicitly non-factual. |
| §22 epistemic table | HISTORICAL AUTHOR CLAIM —  provisional historical state, train convenience, NEWS trigger scope, and first-match clarification are aligned. |
| §23 / trailing status text | HISTORICAL AUTHOR CLAIM —  no stale R2.2 `NOT FROZEN / EDITORIAL BLOCKER / FAIL` status remains. |
| NEWS_UNVERIFIED cross-references (§7.1, §7.2, §8.6, §11.1–§11.3, §19) | HISTORICAL AUTHOR CLAIM —  Stage-5 behavior and retained veto/exit participation are unchanged; only FP-3(c) trigger scope is clarified. |
| Audit A vs Audit B | HISTORICAL AUTHOR CLAIM —  deterministic consolidation is possible without inventing a resolution. |

**Historical note:** R2.4's author sweep did not surface a stale contradiction in the surfaces it examined. That historical statement is superseded as release-gate evidence and is **not** a claim about R2.7.

---

## R2.6 PATCH APPLICATION RECORD

| Source finding | Affected R2.6 section(s) | Applied correction |
|---|---|---|
| **A-01 / P-A-01 — Dividends** | §3.6 item 7; §13.6; §13.7; §15.1 item 1; §15.3 item 2; §16; §22 | Dividend entitlement is ex-date-based; the entitled share count is the ex-date count after applicable split adjustment; `net_dividend` uses the stated withholding formula; cash credit occurs at the earliest of pay date, exit fill timestamp, or window-final official close; the full amount is attributed to the entitled trade; `record_date` is provenance only; `sim_trades.dividends_net` is the trade-P&L reconstruction field. Benchmark entitlement is also ex-date-based while preserving its existing reinvestment simplification. **C-1 scope disclosure:** accepted P-A-01 amended strategy §13.6/§15.1 only; R2.5 additionally changed benchmark credit timing so an entitled benchmark dividend whose pay date follows window end is credited at the window-final official close under N-24. R2.6 retains that benchmark-only extension; it can change benchmark terminal equity and the §15.3 item 4 benchmark `maxDD_max` used by §15.4 criterion 3. |
| **A-02 / P-A-02 — G6 earnings blackout** | §7.2 G6; §12; §16 G6 provenance; §20; §22 | Added the exact event-session mapping: before-market-open/unspecified → event-date trading session, after-market-close → next trading session; **I-1 adds only that when a before-market-open/unspecified event calendar date is not a trading day, `d(e)` is the next trading day**. G6 still fails iff `d(e) ∈ {T,T+1,T+2}`. ETF behavior, coverage-gap neutral-disable, sensitivity pairing, and three-trading-day policy are unchanged. |
| **A-03 / P-A-03 — Symbol change / delisting force-close** | N-09; §13.3; §13.6; §13.7; §15.2; §16; §19 item 2; §20; §22 | Detection is the first 10:00 scan with no executable ticker bar plus universe metadata recording a delisting/symbol change. Fill timestamp follows EXIT_FAST/EXIT_DELAYED; price uses the last available executable ticker price with sell slippage; full sell-side fees apply using the fill trading date for §9.7; `CORP_EVENT_FORCE_CLOSE` is logged and counted/reported. |
| **A-04 / P-A-04 — NEXT_SESSION void sequencing** | N-12; §5.5; §7.1 S0.0/S0.4; §13.2; §13.9 item 16; §15.2; §16; §19 item 2; §20; §22 | Added S0.0 before S0.1. Pending NEXT_SESSION fills are resolved with the SPY 09:44-vs-prior-close opening-drop return before data freshness, exit evaluation, and max-position handling. Trip → `NEXT_SESSION_VOIDED` with no position/accounting/exit evaluation; missing required SPY filter data → `ENTRY_UNFILLABLE_NO_FILTER`; otherwise the 09:30 fill stands and is exit-eligible at 10:00. §5.5 is computed whenever S0.0/S0.4 requires it irrespective of S0.3. FAST/DELAYED semantics are unchanged. |
| **A-05 / P-A-05 — Trend-failure exit** | §2 FP-06 provenance row; §8.6 item 5; §13.3; §19 item 2; §20; §22 | Trend failure is per open position, originating at that position's entry fill. It fires at the earliest scheduled non-suppressed exit-evaluating scan whose two most recent completed daily signal bars both close below EMA50. The qualifying scan is the §13.3 detection scan; the trigger is not once-per-ticker and suppressed scans do not disarm it. |
| **E-01 — Stop-level wording** | N-09; §21 item 22 | Scoped “no fills at the stop level itself” to **exit** fills only; §13.2 `ENTERED_BEYOND_STOP` entry fills remain permitted. |
| **E-02 — Bootstrap vector ordering** | §15.2; §22 | Added the smallest disclosure: the pooled per-trade P&L vector ordering is not normatively pinned, so RNG pinning is conditional on input ordering and does not guarantee bit-identical CIs across different pooling orders. No acceptance criterion is changed. |
| **E-03 — Phase-4 reconstruction artifacts** | §16; §23 | Disclosed that Phase-4 reconstruction additionally requires run↔window boundaries, per-run `initial_capital` and scenario cell, and the §15.1 configuration-evaluation log (`count`, `seed`, `results`). Exact missing persistence schemas are identified as Phase-4, not live-phase-only, without inventing a new schema policy. |
| **E-04 — RISK_OFF exit wording** | §8.6 item 2 | Replaced “next scan” with “next exit-evaluating scan” (10:00 / 15:30 / half-day 12:00). |
| **E-05 — Signal-series volume adjustment disclosure** | §3.3 | Stated that signal-series OHLCV, including volume, is consumed exactly as returned by the provider under the same split-adjusted request; Hermes performs no independent volume adjustment/back-out. |
| **E-06 — BEARISH-CRITICAL G7 endpoint** | §7.2 G7; §11.2 | Replaced the ambiguous prose endpoint with an explicit trading-session definition: `d0` is the first session whose official close is at or after `published_at`; the window is active iff `published_at ≤ t ≤ official_close(d4)`, with `d4` four trading indices after `d0`. This is the minimum endpoint clarification; mapping order and effect policy are unchanged. |
| **E-07 — Benchmark symmetry wording** | §15.3; §22 | Removed the blanket “symmetric construction” label and disclosed the existing terminal-accounting asymmetry: strategy positions are force-closed with sell slippage/fees at window end while the benchmark is not terminally liquidated. No benchmark or acceptance behavior is changed. |
| **E-08 — Historical self-check claims** | Trailing historical R2.4 self-check / consolidation-verification / repair-record / editorial-sweep sections | Marked retained R2.4 author checks explicitly **NON-NORMATIVE / HISTORICAL AUTHOR SELF-CHECKS**, superseded their evidentiary status, and removed the prior blanket current-style assertion that no contradiction remained. |
| **E-09 — §1.3.2 $200 fee-edge approximations** | §1.3.2; §22 | Corrected disclosure-level canonical per-component fee edges to approximately `$27.03` for the $0.25 cap and `$57.06` for the $0.50 cap, with conforming stop/ATR floor approximations. G9 still uses the exact §9.6 computation; strategy behavior is unchanged. |
| **E-10 — Gated artifact status** | §22; §23 | Clarified that the exact `search_space.yaml` grid, concrete `coverage_end`, and pinned OpenRouter identifier remain their existing phase prerequisites and are not themselves blockers to R2.5 patch verification. No missing value is invented. |

## R2.6 MINOR PATCH RECORD

| Finding | Source finding | Affected R2.6 sections | Applied correction |
|---|---|---|---|
| **C-1** | Independent patch verification S-1: §15.3 item 2 retained a deterministic benchmark dividend credit-timing extension beyond accepted P-A-01 scope, but the extension and its criterion-3 consequence were under-disclosed. | §15.3 item 2; §22 dividend epistemic row; R2.6 P-A-01 patch-application record | Kept the existing R2.5 benchmark behavior exactly. Added the normative scope note that P-A-01 amended strategy §13.6/§15.1 only; under R2.4 a benchmark dividend with pay date after window end was not credited, while under N-24 the retained rule credits it at the window-final official close. Disclosed that benchmark terminal equity and the §15.3 item 4 benchmark `maxDD_max` used by §15.4 criterion 3 may change. No strategy dividend accounting, benchmark entitlement, reinvestment simplification, criterion 3, or other benchmark construction was changed. |
| **C-2** | Independent patch verification S-2: §5.5 pseudocode retained the cosmetic phrase “suppress all BUY output for the day (global)” despite the accepted S0.0/S0.4 split. | §5.5 | Conformed the pseudocode to “suppress all ordinary same-day BUY output for the day (global); a pending NEXT_SESSION candidate is voided at S0.0 (§7.1)”. NEXT_SESSION sequencing and FAST/DELAYED semantics are unchanged. |
| **I-1** | Independent patch verification inherited residual I-1: before-market-open/unspecified G6 mapping was undefined when the provider event calendar date was a weekend/holiday. | §7.2 G6; §12; §16 G6 provenance; §20 Phase 1; §22 earnings-blackout row; R2.6 P-A-02 patch-application record | Preserved the accepted event-session branches and added only: if a before-market-open or unspecified event calendar date is not a trading day, `d(e)` is the next trading day. The `{T,T+1,T+2}` blackout set, ETF behavior, coverage-gap rule, and earnings-gate sensitivity policy are unchanged. |

## R2.6 TARGETED CONSISTENCY SWEEP

This is a targeted editorial consistency sweep only across surfaces directly affected by C-1, C-2, and I-1. It is **not** a new release-gate audit, strategy audit, profitability assessment, OOS result, paper-trading result, broker/provider confirmation, freeze, or release approval.

| Check | Result | R2.6 consistency result |
|---|---|---|
| 1. §15.3 benchmark dividend language and §15.4 criterion-3 cross-reference | **PASS** | §15.3 item 2 keeps the R2.5 credit rule and now discloses its scope and possible effect on benchmark `maxDD_max`; §15.4 criterion 3 itself is unchanged and still consumes §15.3 item 4. |
| 2. §22 benchmark/dividend epistemic disclosure | **PASS** | The dividend row now records that the benchmark credit-timing extension was outside accepted P-A-01 strategy scope and can affect benchmark terminal equity / criterion-3 comparator. |
| 3. R2.6 patch-application record | **PASS** | P-A-01 records C-1 scope; P-A-02 records the I-1 non-trading-date fallback without reopening accepted P-A behavior. |
| 4. §5.5 / §7.1 S0.0 / S0.4 wording consistency | **PASS** | §5.5 now distinguishes ordinary same-day BUY suppression from S0.0 pending NEXT_SESSION voiding; existing S0.0/S0.4 sequencing is unchanged. |
| 5. §7.2 G6 event→session mapping | **PASS** | Before-market-open/unspecified retain event-date-session mapping with the new next-trading-day fallback only when that calendar date is non-trading; after-market-close remains next trading day. |
| 6. §12 earnings-blackout references | **PASS** | Schedule text points to the same §7.2 mapping, explicitly including the non-trading-date fallback, and retains `{T,T+1,T+2}`. |
| 7. §16 G6 provenance fields | **PASS** | Existing event date, provider timing, mapped `d(e)`, decision date, and manifest provenance are retained; the resolved next-trading-day `d(e)` is explicitly logged for the new fallback case. |
| 8. §20 implementation-phase references | **PASS** | Phase 1 now names the non-trading-date fallback as part of the existing deterministic G6 mapping; no other implementation policy changed. |
| 9. §22 earnings-blackout row | **PASS** | The row records I-1 while preserving ETF behavior, coverage-gap policy, and the `{T,T+1,T+2}` blackout. |
| 10. No stale text contradicting the new non-trading-date rule | **PASS** | Directly affected normative, schedule, provenance, roadmap, epistemic, and patch-record restatements are aligned; no contrary weekend/holiday mapping remains in those surfaces. |
| I-2 non-modification check | **PASS** | S0.0 remains before S0.1 exactly as inherited from accepted P-A-04; I-2 was not reopened or modified. |

R2.6 — R2.5 MINOR VERIFICATION PATCH APPLIED;
READY FOR FINAL STANDALONE RELEASE-GATE AUDIT

---

## R2.7 PATCH APPLICATION RECORD

| Patch | Source finding | Affected R2.7 sections | Concise correction applied |
|---|---|---|---|
| **P-1** | **A-1 — contradictory/undefined intraday bar-availability semantics** | §3.3; §3.4; §6.1; §7.2 G8; §7.4; §8.6; §16; §19 item 2; §20; §21; §22 | Defined required entry-side executable bars as all ten 09:30–09:39 bars plus 09:44; missing 09:40–09:43 alone does not exclude. VWAP/pace use bars actually returned in 09:30–09:44; `VWAP_UNDEFINED_ZERO_VOLUME` is entry-side-only. Open-position exits proceed on the last available in-session executable bar at/before the N-02 boundary and are `EXIT_EVAL_SUPPRESSED` only when none exists. Added conforming provenance/invariants without changing thresholds, score weights, formulas, or exit triggers. |
| **P-2** | **A-2 — absent/non-trading official open/close undefined** | §0 N-24/N-25; §9.7 item 5; §13.3; §13.6; §13.7; §15.1; §15.3 items 1–4; §15.4; §16; §19 item 2; §20; §21; §22; §23 | Added N-25 exactly: nearest existing daily executable bar at-or-before the nominal date, else nearest following inside the window when none exists at/after window start; if neither exists in an affected test window, exclude it from the verified-window count, criterion-1 denominator, and criteria-2/3 aggregates. Missing required-daily-bar sessions emit no daily equity sample and drawdown uses emitted samples only. Every substitution logs nominal date, substituted session, and consuming rule. N-22 and benchmark policy/thresholds remain unchanged. |
| **P-3** | **A-3 — RISK_OFF advisory persistence undefined across suppressed scan** | §8.6 item 2; §13.3; §16; §19 item 2; §20; §21; §22 | RISK_OFF detected at 08:15 is issued at the next non-suppressed exit-evaluating scan of the same session while the position remains open. Suppression does not disarm it; pending/lapse state is logged for reconstruction; if never issued that session it lapses at close and is re-detected next session if still RISK_OFF. Exit priority, scan schedule, trend-failure behavior, fill timing, slippage, and fees are unchanged. |
| **P-4** | **A-4 — same-hash source-keyed classifications could disagree** | §2; §7.4; §11.1; §11.2; §11.3; §11.5; §16; §19 item 6; §20; §21; §22 | Bound effect fields (`category`, `direction`, `severity`, `ma_role`, `confidence`, `keyword_override`) to normalized headline text + ticker only. `source` is never classifier input and remains only cache identity / two-source confirmation metadata. All entries sharing `(headline_hash, ticker)` must have identical effect fields; a mismatch is `NEWS_CACHE_INTEGRITY_FAILURE`, marks the run non-canonical, and halts. Effect mapping, score points, confirmation rule, hash normalization, and cache key are unchanged. |

## R2.7 TARGETED CONSISTENCY SWEEP

This sweep is limited to contradictions created or exposed by applying P-1 through P-4. It is not a new audit, strategy redesign, profitability assessment, OOS result, paper-trading result, broker/provider confirmation, or category-E editorial pass.

| Patch / directly affected reference | Result | R2.7 consistency result |
|---|---|---|
| **P-1 — §3.3 / §3.4** | **PASS** | Executable 1-min consumers point to §19 item 2; global/provider staleness remains distinct from ticker-specific required-bar exclusion. |
| **P-1 — §6.1** | **PASS** | All ten 09:30–09:39 opening-range bars are explicitly required; VWAP and pace use returned 09:30–09:44 bars; zero-volume is entry-side-only. |
| **P-1 — §7.2 G8 / §7.4** | **PASS** | G8 and Intraday/Volume scoring consume the P-1 entry-side availability semantics; formulas, thresholds, and weights are unchanged. |
| **P-1 — §8.6 / §19 item 2 / §21** | **PASS** | Open-position exit suppression is exactly the no-bar-at-or-before-N-02-boundary condition; otherwise last-available-bar evaluation proceeds even on an entry-excluded ticker. Invariants prohibit the rejected interpretations. |
| **P-1 — logging/provenance** | **PASS** | Missing required labels, returned-bar/zero-volume inputs, exit-suppression boundary, and last-available exit bar are reconstructible in §16. |
| **P-2 — N-22 / N-24 / N-25** | **PASS** | N-22 remains unchanged; N-25 supplies the sole substitution/emission rule; N-24 cross-references it without changing per-window independence. |
| **P-2 — §13.3 / §13.6 / §13.7** | **PASS** | Terminal official-close fallback, dividend-related official-close consumption, and daily strategy equity sampling conform to N-25; no synthetic missing-session sample is introduced. |
| **P-2 — §9.7 item 5 / §15.1 / §15.3 / §15.4** | **PASS** | The eligible verified-window count, window-end force-close, benchmark open/dividend/daily-equity/maxDD, criterion-1 denominator, and criteria-2/3 aggregates all use the N-25 semantics; fee rules, thresholds, and benchmark construction are otherwise unchanged. |
| **P-2 — §19 item 2 / provenance** | **PASS** | Intraday suppression is kept separate from daily official-price substitution; every substitution and unresolvable-window exclusion is logged. |
| **P-3 — §8.6 item 2 / §13.3 / §19 item 2 / §21** | **PASS** | RISK_OFF persists only within the same session to the next non-suppressed exit scan, lapses at close if unissued, and is re-detected next 08:15 when applicable; trend-failure behavior and priority remain unchanged. |
| **P-3 — §16 / logging provenance** | **PASS** | Pending suppressed-scan state and same-session lapse are reconstructible through `RISK_OFF_ADVISORY_PENDING` / `RISK_OFF_ADVISORY_LAPSED`; an issued advisory retains the ordinary exit detection/fill provenance. |
| **P-4 — §7.4 / §11.1 / §11.2 / §11.3 / §11.5** | **PASS** | Source-independent classifier inputs and same-hash effect equality make catalyst dedup total and deterministic while preserving source-qualified cache identity and distinct-source confirmation. |
| **P-4 — §16 / §19 item 6 / §20 / §21** | **PASS** | Cache-integrity mismatch is persisted, marks the run non-canonical, halts deterministically, is asserted during cache population/consumption, and is captured by invariants. |
| **Preservation — category-E E-1 through E-13** | **PASS** | No R2.6 audit category-E finding was applied as part of R2.7. |
| **Preservation — C-1/C-2/I-1/I-2, P-A-01…05, FP-1…08, EB-01, TRAIN_SUBSTITUTED_ZERO, thresholds/weights/sizing/fees/acceptance** | **PASS** | No policy in these preserved areas was reopened; only direct P-1–P-4 conforming references were added. |

R2.7 — R2.6 TARGETED RELEASE-GATE PATCH APPLIED;
READY FOR INDEPENDENT PATCH VERIFICATION

---

## R2.8 PATCH APPLICATION RECORD

| Clarification | Source | Affected R2.8 sections | Concise clarification applied |
|---|---|---|---|
| **A-1** | Accepted earnings-source clarification | §3.1; §3.7 | Finnhub free tier is the **baseline** earnings provider, not an exclusive mandatory provider. An alternative earnings source MAY be used only when it satisfies the new §3.7 provider-neutral earnings-source contract. The §3.1 combined "News + earnings calendar" row is split into "News headlines" (Finnhub, unchanged) and "Earnings calendar" (baseline Finnhub, substitutable per §3.7). |
| **A-2** | Accepted earnings-source clarification | §3.7; §13.9 item 21 | The earnings source used for BACKTEST MAY differ from the earnings source used for LIVE operation; each source must independently satisfy the same canonical G6 semantics and §3.7 data-contract requirements. The divergence is disclosed at report level (§13.9 item 21) as descriptive report-level metadata only, with no persistence-schema requirement. |
| **A-3** | Accepted earnings-source clarification | §3.7 | Provider-neutral minimum earnings data contract: a compliant source must provide `ticker`, the earnings event calendar date, and a provider/source timing value sufficient to deterministically map into an **existing** canonical G6 timing branch. No new G6 timing branch is created. |
| **A-4** | Accepted earnings-source clarification | §3.7 | A static historical earnings dataset MAY be used for BACKTEST when it satisfies the approved coverage, provenance, reproducibility, deterministic-replay, and as-revised requirements. Point-in-time scheduled-calendar history remains NOT REQUIRED. |
| **C-1** | Corrected timing semantics | §3.7 | Before-market-open / after-market-close / unspecified mappings require unambiguous or missing/unknown/unspecified provider semantics respectively. A provider value with known but different semantics (e.g., "during market hours") MUST NOT automatically map to unspecified; such a source/event is NON-COMPLIANT for canonical G6 evaluation for the affected event/span. No fourth timing branch; §7.2 unchanged. |
| **C-2** | Corrected provider identity | §3.7 | No per-row provider identity, provider metadata schema, or provider ID field is introduced. Existing §16 provenance remains unchanged; §13.9 item 21 disclosure is descriptive report-level metadata only. |
| **C-3** | Corrected reproducibility distinction | §3.7 | Earnings-input reproducibility is NOT the same mechanism as EARNINGS coverage-manifest run-pinning. The earnings input (live API or static dataset) must be sufficiently immutable, version-pinned, or reconstructible for deterministic replay of every G6 decision per run. Existing §16 run-pinned EARNINGS `manifest_version` logging remains intact; `manifest_version` alone does not identify the provider, version, hash, or reconstruct the source dataset. No dataset-hash field or new persistence field is added. |

R2.8 does not modify §7.2 G6 semantics, §11.6 coverage semantics, or §16 persistence schemas beyond adding this record and the sections listed above; no section-number/reference repair was required by the insertion of §3.7. No strategy policy listed in the R2.8 scope lock (universe, regime, G1–G10, scoring, sizing, exits, fees, walk-forward, criteria, benchmarks, provider policies, search space, `coverage_end`, execution assumptions) is changed.

R2.8 — R2.7 TARGETED EARNINGS-SOURCE CLARIFICATION APPLIED;
READY FOR INDEPENDENT PATCH VERIFICATION
