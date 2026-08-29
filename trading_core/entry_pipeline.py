"""R2.7 §7.1 main-scan orchestration: Stage 1 → Stage 5 entry pipeline.

Separated from pipeline.py to keep modules small. All inputs are explicit
point-in-time snapshots; no wall clock, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping, Sequence

from trading_core import gates as g
from trading_core.fees import (
    RegulatoryRates,
    RoundingBranch,
    compute_round_trip_fees,
)
from trading_core.indicators import (
    atr,
    ema,
    evaluation_price_0944,
    relative_strength_20,
    rsi,
    session_volume_pace,
    session_vwap,
)
from trading_core.news_effects import catalyst_score_points, g7_vetoed_at
from trading_core.pipeline import (
    PipelineEvents,
    Stage1Survivor,
    Stage4Evaluation,
)
from trading_core.scoring import (
    ScoreInputs,
    active_cutoff,
    rank_candidates,
    score_candidate,
)
from trading_core.sizing import compute_sizing
from trading_core.stops import compute_stop_pct
from trading_core.types import SymbolSnapshot


def run_stage1(
    *,
    snapshots: Sequence[SymbolSnapshot],
    universe: set[str],
    run_surface: str,                      # "live" | "backtest"
    trading_sessions: Sequence[Any],
    official_closes: Mapping[Any, Any],
    scan_t: Any,
    events: PipelineEvents,
    is_trading_day: Callable,
    next_trading_day: Callable,
) -> list[Stage1Survivor]:
    """Stage 1: per-ticker hard gates G3, G4, G5, G6, G7, G8 (no sizing).

    Applies the §19 item 2 / P-1 entry-side required-bar rule and the §6.1
    zero-volume VWAP guard before G8 (emits TICKER_REQUIRED_BAR_EXCLUSION /
    VWAP_UNDEFINED_ZERO_VOLUME).
    """
    survivors: list[Stage1Survivor] = []
    for snap in snapshots:
        # P-1 required-bar entry-side rule
        required = g.check_required_bars(snap.exec_bars_today.keys())
        if required.excluded:
            events.emit("TICKER_REQUIRED_BAR_EXCLUSION",
                        ticker=snap.ticker, session=str(snap.session_date),
                        missing_labels=list(required.missing_labels),
                        scope="entry-side")
            continue
        vwap = session_vwap(snap.exec_bars_today)
        if vwap is None:
            events.emit("VWAP_UNDEFINED_ZERO_VOLUME",
                        ticker=snap.ticker, session=str(snap.session_date),
                        returned_labels=sorted(snap.exec_bars_today.keys()))
            continue

        closes = [float(c) for c in snap.signal_closes]
        if len(closes) < 200:
            events.emit("INSUFFICIENT_SIGNAL_HISTORY", ticker=snap.ticker,
                        sessions=len(closes))
            continue
        ema50 = float(ema(closes, 50)[-1])
        ema200 = float(ema(closes, 200)[-1])
        rsi14 = float(rsi(closes, 14)[-1])

        results = [
            g.gate_g3(ticker=snap.ticker, universe=universe,
                      pluang_confirmed=snap.pluang_confirmed,
                      run_surface=run_surface),
            g.gate_g4(close_t_minus_1=closes[-1], ema50_t_minus_1=ema50,
                      ema200_t_minus_1=ema200),
            g.gate_g5(rsi14_t_minus_1=rsi14),
            g.gate_g6(
                ticker=snap.ticker, asset_class=snap.asset_class,
                decision_date=snap.session_date,
                trading_sessions=trading_sessions,
                events=snap.earnings_events,
                coverage_enabled=snap.earnings_coverage_enabled,
                manifest_version=snap.earnings_manifest_version,
                is_trading_day=is_trading_day,
                next_trading_day=next_trading_day,
            ),
        ]
        vetoed = g7_vetoed_at(snap.classifications, scan_t,
                              trading_sessions=trading_sessions,
                              official_closes=official_closes)
        results.append(g.GateResult(
            gate_id="G7", passed=not vetoed, stage="1",
            inputs_json={"ticker": snap.ticker, "vetoed": vetoed,
                         "news_coverage_enabled": snap.news_coverage_enabled},
            reason_code="G7_NEWS_VETO" if vetoed else "",
        ))
        results.append(g.gate_g8(exec_bars_today=snap.exec_bars_today))

        if all(r.passed for r in results):
            survivors.append(Stage1Survivor(snapshot=snap, gate_results=results))
    return survivors


def run_stages_2_to_5(
    *,
    survivors: Sequence[Stage1Survivor],
    regime: Any,
    portfolio_value: Decimal,
    risk_per_trade: Decimal,
    stop_mult: Decimal,
    fee_regulatory: RegulatoryRates,
    vat_on_regulatory: bool,
    fee_rounding: RoundingBranch,
    run_role: str,
    screening_date: Any,
    scan_t: Any,
    trading_sessions: Sequence[Any],
    official_closes: Mapping[Any, Any],
    events: PipelineEvents,
) -> tuple[str | None, list[str], list[Stage4Evaluation], int, str]:
    """Stage 2 (score + cutoff) → Stage 3 (rank) → Stage 4 (G10/G9 in rank
    order) → Stage 5 (NEWS_UNVERIFIED downgrade; no Stage-4 resumption).

    Returns (selected_ticker, ranking, stage4_evaluations, cutoff, action).
    """
    from trading_core.news_effects import is_news_unverified

    cutoff = active_cutoff(regime.regime)

    scored = []
    for s in survivors:
        snap = s.snapshot
        closes = [float(c) for c in snap.signal_closes]
        highs = [float(h) for h in snap.signal_highs]
        lows = [float(l) for l in snap.signal_lows]
        ema20_v = float(ema(closes, 20)[-1])
        ema50_v = float(ema(closes, 50)[-1])
        ema200_v = float(ema(closes, 200)[-1])
        atr14_v = float(atr(highs, lows, closes, 14)[-1])
        rsi_series = rsi(closes, 14)
        rsi14_v = float(rsi_series[-1])
        rsi14_m6 = float(rsi_series[-6]) if len(closes) >= 20 else None
        spy_closes = [float(c) for c in snap.spy_signal_closes]
        rs20 = relative_strength_20(closes, spy_closes)

        vols = [float(v) for v in snap.signal_volumes]
        dv = [c * v for c, v in zip(closes, vols)]
        recent = sum(dv[-10:]) / 10 if len(dv) >= 10 else 0.0
        prior = sum(dv[-20:-10]) / 10 if len(dv) >= 20 else 0.0

        catalyst = catalyst_score_points(snap.classifications, scan_t)
        price_0944 = evaluation_price_0944(snap.exec_bars_today)
        vwap = session_vwap(snap.exec_bars_today)
        assert price_0944 is not None and vwap is not None  # Stage 1 enforced
        inp = ScoreInputs(
            ticker=snap.ticker,
            ema20=ema20_v, ema50=ema50_v, ema200=ema200_v,
            close_t_minus_1=closes[-1], atr14=atr14_v,
            rsi14_t_minus_1=rsi14_v, rsi14_t_minus_6=rsi14_m6,
            rs20_vs_spy=rs20,
            daily_dollar_volume_recent=recent,
            daily_dollar_volume_prior=prior,
            price_0944=price_0944,
            session_vwap=vwap,
            exec_bars_today=snap.exec_bars_today,
            session_pace=session_volume_pace(
                snap.exec_bars_today,
                [snap.prior_session_window_volumes[d]
                 for d in sorted(snap.prior_session_window_volumes)][-20:]),
            catalyst_points=catalyst,
        )
        scored.append(score_candidate(inp))

    ranked = rank_candidates(scored, cutoff)
    ranking = [r.ticker for r in ranked]
    discarded = [r.ticker for r in scored if r.final_total_int < cutoff]
    for t in discarded:
        events.emit("STAGE2_BELOW_CUTOFF", ticker=t, cutoff=cutoff)

    evaluations: list[Stage4Evaluation] = []
    selected: str | None = None
    for cand in ranked:
        snap = next(s.snapshot for s in survivors if s.snapshot.ticker == cand.ticker)
        closes = [float(c) for c in snap.signal_closes]
        highs = [float(h) for h in snap.signal_highs]
        lows = [float(l) for l in snap.signal_lows]
        atr14_v = Decimal(str(float(atr(highs, lows, closes, 14)[-1])))
        stop_pct = compute_stop_pct(
            atr14=atr14_v, close_t_minus_1=Decimal(str(closes[-1])),
            stop_mult=stop_mult)
        ref_price = evaluation_price_0944(snap.exec_bars_today)
        assert ref_price is not None  # Stage 1 enforced
        sizing = compute_sizing(
            portfolio_value=portfolio_value, risk_per_trade=risk_per_trade,
            regime_multiplier=Decimal(str(regime.multiplier)),
            stop_pct=stop_pct,
            entry_reference_price=ref_price,
        )
        if not sizing.g10_pass:
            evaluations.append(Stage4Evaluation(
                ticker=cand.ticker, g10_pass=False, g9_pass=None,
                sizing=sizing))
            events.emit("G10_REJECT", ticker=cand.ticker,
                        raw_notional=str(sizing.raw_notional))
            continue
        assert sizing.notional is not None and sizing.shares_est is not None
        assert sizing.realised_risk is not None
        fees = compute_round_trip_fees(
            notional=sizing.notional, shares_est=sizing.shares_est,
            screening_date=screening_date, regulatory=fee_regulatory,
            vat_on_regulatory=vat_on_regulatory, rounding=fee_rounding,
            run_role=run_role)
        from trading_core.fees import gate_a_burden
        burden = gate_a_burden(fees.fees_rt, sizing.realised_risk)
        g9_pass = burden is not None and burden <= Decimal("0.25")
        evaluations.append(Stage4Evaluation(
            ticker=cand.ticker, g10_pass=True, g9_pass=g9_pass,
            sizing=sizing, fee_computation=fees, burden=burden))
        if not g9_pass:
            events.emit("G9_REJECT", ticker=cand.ticker,
                        burden=str(burden))
            continue
        # First candidate passing G10 and G9 -> selected BUY CANDIDATE; stop.
        if is_news_unverified(
                ticker=cand.ticker, t=scan_t,
                classifications=snap.classifications,
                raw_headlines=snap.raw_headlines,
                covered=snap.news_coverage_enabled,
                schema_version="news_schema_v3", model_version="",
                trading_sessions=trading_sessions,
                official_closes=official_closes):
            # Stage 5: downgrade; Stage 4 NOT resumed (DECIDED).
            events.emit("STAGE5_NEWS_UNVERIFIED_DOWNGRADE", ticker=cand.ticker)
            return None, ranking, evaluations, cutoff, "WATCH"
        selected = cand.ticker
        return selected, ranking, evaluations, cutoff, "BUY CANDIDATE"

    return None, ranking, evaluations, cutoff, (
        "NO ACTION" if not evaluations or all(
            e.g9_pass is False or not e.g10_pass for e in evaluations)
        else "WATCH")
