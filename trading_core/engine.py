"""R2.7 §14.2 engine surface — scan orchestration over explicit snapshots.

The 10:00 main scan implements the exact §7.1 Stage-0 ordering:

    S0.0 pending-entry resolution (P-A-04)  [pipeline.resolve_pending_next_session]
    S0.1 G2 data freshness (global)         [DATA DEGRADED halts the scan]
    S0.2 exit evaluation on the open position (§8.6; N-10 lockout)
    S0.3 max_positions check
    S0.4 opening-drop filter (SPY global; compute-or-reuse the day's §5.5)
    S0.5 regime check (RISK_OFF blocks new entries)

then Stages 1–5 via entry_pipeline. The 15:30 (half-day 12:00) pre-close
scan evaluates exits only (§12: no new entries).

No wall clock: the caller supplies every decision timestamp.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence

from trading_core.entry_pipeline import run_stage1, run_stages_2_to_5
from trading_core.exits import (
    ExitEvaluationContext,
    RiskOffAdvisoryState,
    evaluate_exit,
)
from trading_core.pipeline import (
    MainScanResult,
    OpenPosition,
    PendingNextSessionEntry,
    PipelineEvents,
    S0Resolution,
    resolve_pending_next_session,
)
from trading_core.types import REGIME_RISK_OFF, SymbolSnapshot


@dataclass(frozen=True)
class GlobalInputs:
    """Global (non-ticker) scan inputs."""

    regime: Any                          # RegimeResult
    data_fresh: bool                     # S0.1 G2 (§3.4); backtest: always True
    globally_excluded: bool              # §19 item 2 global exclusion
    spy_0944_price: Any = None           # Decimal | None
    spy_prior_official_close: Any = None
    spy_split_ratio_on_ex_date: float | None = None
    confirmed_critical_by_ticker: Mapping[str, bool] | None = None
    bearish_high_by_ticker: Mapping[str, bool] | None = None
    position_bars: Mapping[str, Any] | None = None   # ticker -> bars for exits
    position_signal_closes: Mapping[str, Sequence[float]] | None = None


def scan_main_1000(
    *,
    session_date,
    scan_datetime,
    snapshots: Sequence[SymbolSnapshot],
    universe: set[str],
    run_surface: str,
    gbl: GlobalInputs,
    pending_next_session: PendingNextSessionEntry | None,
    open_position: OpenPosition | None,
    risk_off_state: RiskOffAdvisoryState | None,
    window_end,
    portfolio_value,
    risk_per_trade,
    stop_mult,
    fee_regulatory,
    vat_on_regulatory: bool,
    fee_rounding,
    run_role: str,
    trading_sessions: Sequence,
    official_closes: Mapping,
    compute_opening_drop: Callable,
    is_trading_day: Callable,
    next_trading_day: Callable,
) -> MainScanResult:
    """The 10:00 main scan (§7.1 exact ordering)."""
    events = PipelineEvents()
    opening_drop = None

    # --- S0.0 pending NEXT_SESSION resolution (before S0.1) ---
    s0 = resolve_pending_next_session(
        candidate=pending_next_session, session_date=session_date,
        window_end=window_end,
        spy_0944_price=gbl.spy_0944_price,
        spy_prior_official_close=gbl.spy_prior_official_close,
        spy_split_ratio_on_ex_date=gbl.spy_split_ratio_on_ex_date,
        compute_opening_drop=compute_opening_drop, events=events)
    opening_drop = s0[1] if isinstance(s0, tuple) else None
    resolution: S0Resolution = s0[0] if isinstance(s0, tuple) else s0
    if resolution.kind in ("NEXT_SESSION_VOIDED", "ENTRY_UNFILLABLE_NO_FILTER",
                           "ENTRY_UNFILLABLE_WINDOW_END"):
        # A voided/unfilled pending candidate creates no position, no
        # accounting, and no exit evaluation for that candidate (P-A-04);
        # the ordinary pipeline continues.
        open_position = None

    # --- S0.1 G2 data freshness (global) ---
    if not gbl.data_fresh:
        events.emit("DATA_DEGRADED", scan="10:00")
        return MainScanResult(action="NO ACTION", events=events,
                              s0_resolution=resolution,
                              opening_drop=opening_drop,
                              reason_code="DATA_DEGRADED")

    # §5.5: the opening-drop filter computes whenever S0.0 or S0.4 requires
    # it, irrespective of whether S0.3 would otherwise end the pipeline.
    # When S0.0 already computed the day's result, reuse it (N-12).
    if opening_drop is None and gbl.spy_0944_price is not None and \
            gbl.spy_prior_official_close is not None:
        opening_drop = compute_opening_drop(
            spy_price_0944=float(gbl.spy_0944_price),
            spy_prior_official_close=float(gbl.spy_prior_official_close),
            spy_split_ratio_on_ex_date=gbl.spy_split_ratio_on_ex_date)

    # --- S0.2 exit evaluation on the open position (N-10 lockout) ---
    # §19 item 2: exit evaluation proceeds at each scheduled scan using the
    # position's own executable bars even on globally excluded days; only
    # the RISK_OFF trigger (§8.6 item 2) is unavailable on such days.
    exit_eval = None
    position_open = open_position is not None
    if resolution.kind == "FILLED_STANDS":
        # The standing 09:30 fill is exit-eligible from this day's 10:00
        # S0.2 (P-A-04). The fill's stop/target live in the candidate's
        # decision context; its OpenPosition is constructed by the caller's
        # accounting layer (Phase 3). Here it marks the position open.
        position_open = True
    if open_position is not None:
        bars = (gbl.position_bars or {}).get(open_position.ticker, {})
        ctx = ExitEvaluationContext(
            scan_label="10:00", session_date=session_date,
            scan_datetime=scan_datetime, exec_bars_today=bars,
            stop_price=open_position.stop_price,
            target_price=open_position.target_price,
            risk_off_advisory_pending=bool(
                risk_off_state and risk_off_state.pending),
            globally_excluded=gbl.globally_excluded,
            confirmed_critical_news=bool(
                (gbl.confirmed_critical_by_ticker or {}).get(
                    open_position.ticker, False)),
            bearish_high_active=bool(
                (gbl.bearish_high_by_ticker or {}).get(
                    open_position.ticker, False)),
            signal_closes=(gbl.position_signal_closes or {}).get(
                open_position.ticker, ()),
        )
        exit_eval = evaluate_exit(ctx)
        if exit_eval.suppressed:
            events.emit("EXIT_EVAL_SUPPRESSED", ticker=open_position.ticker,
                        scan="10:00", boundary_label=exit_eval.boundary_label)
        if risk_off_state is not None:
            outcome = risk_off_state.on_scan(
                "10:00", suppressed=exit_eval.suppressed, position_open=True)
            if outcome == "RISK_OFF_ADVISORY_PENDING":
                events.emit("RISK_OFF_ADVISORY_PENDING",
                            ticker=open_position.ticker, scan="10:00",
                            detection_session=str(risk_off_state.detection_session))
        if exit_eval.fired is not None:
            events.emit("EXIT_TRIGGER", ticker=open_position.ticker,
                        reason=exit_eval.fired.value, scan="10:00")
            return MainScanResult(  # N-10: no new entry this scan
                action="EXIT" if exit_eval.fired.value != "TRIM_ADVISORY"
                else "TRIM",
                events=events, s0_resolution=resolution,
                opening_drop=opening_drop, exit_evaluation=exit_eval,
                reason_code=exit_eval.fired.value)

    # --- S0.3 max_positions (baseline max_positions = 1) ---
    if position_open:
        return MainScanResult(action="NO ACTION", events=events,
                              s0_resolution=resolution,
                              opening_drop=opening_drop,
                              exit_evaluation=exit_eval,
                              reason_code="MAX_POSITIONS")

    # --- S0.4 opening-drop filter (reuse the day's §5.5 result) ---
    if opening_drop is not None and opening_drop.tripped:
        events.emit("OPENING_DROP_FILTER", opening_return=opening_drop.opening_return)
        return MainScanResult(action="NO ACTION", events=events,
                              s0_resolution=resolution,
                              opening_drop=opening_drop,
                              exit_evaluation=exit_eval,
                              reason_code="OPENING_DROP")

    # --- S0.5 regime check ---
    if gbl.regime.regime == REGIME_RISK_OFF:
        events.emit("RISK_OFF_NO_NEW_ENTRIES", regime=gbl.regime.regime)
        return MainScanResult(action="NO ACTION", events=events,
                              s0_resolution=resolution,
                              opening_drop=opening_drop,
                              exit_evaluation=exit_eval,
                              reason_code="RISK_OFF")
    if gbl.globally_excluded:
        events.emit("GLOBAL_EXCLUSION_NO_ENTRIES")
        return MainScanResult(action="NO ACTION", events=events,
                              s0_resolution=resolution,
                              opening_drop=opening_drop,
                              exit_evaluation=exit_eval,
                              reason_code="GLOBAL_EXCLUSION")

    # --- Stages 1–5 ---
    survivors = run_stage1(
        snapshots=snapshots, universe=universe, run_surface=run_surface,
        trading_sessions=trading_sessions, official_closes=official_closes,
        scan_t=scan_datetime, events=events,
        is_trading_day=is_trading_day, next_trading_day=next_trading_day)
    selected, ranking, s4, cutoff, action = run_stages_2_to_5(
        survivors=survivors, regime=gbl.regime,
        portfolio_value=portfolio_value, risk_per_trade=risk_per_trade,
        stop_mult=stop_mult, fee_regulatory=fee_regulatory,
        vat_on_regulatory=vat_on_regulatory, fee_rounding=fee_rounding,
        run_role=run_role, screening_date=session_date, scan_t=scan_datetime,
        trading_sessions=trading_sessions, official_closes=official_closes,
        events=events)
    return MainScanResult(
        action=action, events=events, s0_resolution=resolution,
        opening_drop=opening_drop, exit_evaluation=exit_eval,
        stage1_results=list(survivors), stage3_ranking=ranking,
        stage4_evaluations=s4, selected_candidate=selected,
        cutoff=cutoff,
        reason_code="" if action == "BUY CANDIDATE" else "NO_FEE_VIABLE_CANDIDATES",
    )
