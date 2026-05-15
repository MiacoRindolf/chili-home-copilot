"""Authoritative ledger hooks for execution feedback (paper / live / broker)."""

from __future__ import annotations

import logging
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....models.trading import PaperTrade, Trade
from ....config import settings
from .emitters import (
    emit_broker_fill_closed_outcome,
    emit_live_trade_closed_outcome,
    emit_paper_trade_closed_outcome,
)
from .execution_attribution import trade_close_attribution_dict
from .ledger import enqueue_or_refresh_debounced_work

logger = logging.getLogger(__name__)


def _exec_feedback_debounce_s() -> int:
    return int(getattr(settings, "brain_work_exec_feedback_debounce_seconds", 45))


# ── Phase B (f-execution-truth-wiring): venue-truth + cost-estimate refresh ──
#
# Wire-point for record_fill_observation. Approved per plan.response.md
# (2026-05-15). All three close hooks below call _record_venue_truth +
# _refresh_rolling_cost_estimate inside try/except so a write failure can
# NEVER block the legacy emitter chain. record_fill_observation honours
# settings.brain_venue_truth_mode ("shadow" default); when "off" the
# function returns False and writes nothing.

def _broker_side_for(trade_or_pt: Any) -> str:
    return "short" if (getattr(trade_or_pt, "direction", "long") or "long").strip().lower() == "short" else "long"


def _fee_bps_for_broker(broker_source: str | None) -> float:
    """Round-trip fee assumption in bps; lives here so paper / live agree."""
    b = (broker_source or "").strip().lower()
    if b == "coinbase":
        return float(getattr(settings, "chili_coinbase_taker_fee_bps_round_trip", 120))
    # Robinhood crypto is fee-free; RH equity is sub-bps. Use a 1bps floor
    # so the cost fraction is not artificially zero for non-Coinbase brokers.
    return 1.0


def _latest_event_spread_bps(db: Session, trade_id: int | None) -> float | None:
    """Most recent TradingExecutionEvent.spread_bps for trade_id, or None."""
    if trade_id is None:
        return None
    try:
        row = db.execute(
            text("""
                SELECT spread_bps
                  FROM trading_execution_events
                 WHERE trade_id = :tid
                   AND spread_bps IS NOT NULL
              ORDER BY recorded_at DESC
                 LIMIT 1
            """),
            {"tid": int(trade_id)},
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None
    except Exception:
        return None


def _rolling_estimate_row(
    db: Session, *, ticker: str, side: str, window_days: int = 30,
) -> dict[str, Any] | None:
    """Lookup the rolling ExecutionCostEstimate row; None when absent."""
    try:
        row = db.execute(
            text("""
                SELECT median_spread_bps, p90_spread_bps,
                       median_slippage_bps, p90_slippage_bps,
                       avg_daily_volume_usd, sample_trades
                  FROM trading_execution_cost_estimates
                 WHERE ticker = :tkr
                   AND side = :side
                   AND window_days = :win
                 LIMIT 1
            """),
            {"tkr": ticker, "side": side, "win": int(window_days)},
        ).fetchone()
        if not row:
            return None
        return {
            "median_spread_bps": row[0],
            "p90_spread_bps": row[1],
            "median_slippage_bps": row[2],
            "p90_slippage_bps": row[3],
            "avg_daily_volume_usd": row[4],
            "sample_trades": row[5],
        }
    except Exception:
        return None


def _compute_fill_observation(
    db: Session, trade_or_pt: Any, *, paper_bool: bool,
):
    """Build a FillObservation for venue_truth.record_fill_observation.

    Returns None when there's not enough signal to record anything
    meaningful (no ticker, zero notional). Each Optional field falls back
    to None on missing data; the dataclass tolerates partial fills.
    """
    from ..venue_truth import FillObservation
    from ..execution_cost_model import estimate_cost_fraction

    ticker = (getattr(trade_or_pt, "ticker", "") or "").strip()
    if not ticker:
        return None

    qty = float(getattr(trade_or_pt, "quantity", None) or 0.0)
    entry_px = float(getattr(trade_or_pt, "entry_price", None) or 0.0)
    notional = abs(entry_px * qty)
    if notional <= 0:
        return None

    side = _broker_side_for(trade_or_pt)
    broker = getattr(trade_or_pt, "broker_source", None)
    fee_bps = _fee_bps_for_broker(broker)

    # Realized fields
    realized_slippage_bps: float | None = None
    raw_slip = getattr(trade_or_pt, "tca_entry_slippage_bps", None)
    if raw_slip is not None:
        try:
            realized_slippage_bps = abs(float(raw_slip))
        except (TypeError, ValueError):
            realized_slippage_bps = None

    realized_spread_bps = _latest_event_spread_bps(db, getattr(trade_or_pt, "id", None))

    realized_cost_fraction: float | None = None
    if realized_spread_bps is not None or realized_slippage_bps is not None:
        spread_f = (realized_spread_bps or 0.0) / 10_000.0
        slip_f = (realized_slippage_bps or 0.0) / 10_000.0
        realized_cost_fraction = spread_f + slip_f + (fee_bps / 10_000.0)

    # Expected fields from rolling estimate
    expected_spread_bps: float | None = None
    expected_slippage_bps: float | None = None
    expected_cost_fraction: float | None = None
    est = _rolling_estimate_row(db, ticker=ticker, side=side)
    if est is not None and (est.get("sample_trades") or 0) > 0:
        try:
            expected_spread_bps = float(est.get("p90_spread_bps") or 0.0)
            expected_slippage_bps = float(est.get("p90_slippage_bps") or 0.0)
            breakdown = estimate_cost_fraction(
                ticker=ticker, side=side, notional_usd=notional,
                estimate_row=est,
                fee_bps=fee_bps,
                impact_cap_bps=float(getattr(settings, "brain_execution_cost_impact_cap_bps", 50.0)),
                use_p90=True,
            )
            expected_cost_fraction = float(breakdown.total)
        except Exception:
            pass

    return FillObservation(
        ticker=ticker,
        side=side,
        notional_usd=notional,
        expected_spread_bps=expected_spread_bps,
        realized_spread_bps=realized_spread_bps,
        expected_slippage_bps=expected_slippage_bps,
        realized_slippage_bps=realized_slippage_bps,
        expected_cost_fraction=expected_cost_fraction,
        realized_cost_fraction=realized_cost_fraction,
        trade_id=int(getattr(trade_or_pt, "id", None) or 0) or None,
        paper_bool=bool(paper_bool),
    )


def _record_venue_truth(db: Session, trade_or_pt: Any, *, paper_bool: bool) -> None:
    """Best-effort write to trading_venue_truth_log. Never raises."""
    try:
        from ..venue_truth import record_fill_observation, mode_is_active

        if not mode_is_active():
            return
        obs = _compute_fill_observation(db, trade_or_pt, paper_bool=paper_bool)
        if obs is None:
            return
        record_fill_observation(db, obs)
    except Exception:
        logger.debug("[execution_hooks] record_fill_observation failed", exc_info=True)


def _refresh_rolling_cost_estimate(db: Session, trade_or_pt: Any) -> None:
    """Lazy refresh of trading_execution_cost_estimates for this (ticker, side)."""
    try:
        from ..execution_cost_builder import (
            compute_rolling_estimate, upsert_estimate, mode_is_active,
        )

        if not mode_is_active():
            return
        ticker = (getattr(trade_or_pt, "ticker", "") or "").strip()
        if not ticker:
            return
        side = _broker_side_for(trade_or_pt)
        est = compute_rolling_estimate(db, ticker=ticker, side=side, window_days=30)
        if est is None:
            return
        upsert_estimate(db, est)
    except Exception:
        logger.debug("[execution_hooks] refresh rolling estimate failed", exc_info=True)


def on_paper_trade_closed(db: Session, pt: PaperTrade) -> None:
    """Call in the same transaction as the paper close (before commit)."""
    try:
        emit_paper_trade_closed_outcome(
            db,
            paper_trade_id=int(pt.id),
            user_id=pt.user_id,
            scan_pattern_id=pt.scan_pattern_id,
            ticker=(pt.ticker or "").strip(),
            pnl=pt.pnl,
            exit_reason=(pt.exit_reason or "").strip(),
        )
        uid = pt.user_id
        if uid is not None:
            enqueue_or_refresh_debounced_work(
                db,
                event_type="execution_feedback_digest",
                dedupe_key=f"exec_fb_digest:user:{int(uid)}",
                payload={"user_id": int(uid), "trigger": "paper_trade_closed"},
                debounce_seconds=_exec_feedback_debounce_s(),
                lease_scope="execution_feedback",
            )
    except Exception:
        logger.debug("[execution_hooks] on_paper_trade_closed failed", exc_info=True)

    _record_venue_truth(db, pt, paper_bool=True)
    _refresh_rolling_cost_estimate(db, pt)


def _phase_a_economic_ledger_live_shadow(
    db: Session, trade: Trade, *, ledger_source: str
) -> None:
    """Phase A shadow hook: record entry + exit fills against the canonical
    economic ledger and reconcile against ``trade.pnl``.

    Shadow-only — does not modify the legacy Trade row. Swallows all errors
    so the execution feedback path never breaks on ledger bugs.
    """
    try:
        from ....services.trading import economic_ledger as _ledger
        if not _ledger.mode_is_active():
            return
    except Exception:
        return

    # LLL -- skip option trades. The economic ledger assumes share-based
    # positions where cash_delta = price * qty. For an option, the row
    # uses trade.entry_price (premium, e.g. $4) but trade.exit_price may
    # be the underlying spot price (e.g. $715), yielding a phantom +$711
    # P&L. Option exits should be tracked via the options-specific cash
    # math (premium * 100 multiplier * qty), not this hook. Until the
    # ledger gains options-aware shape, do not emit option events.
    try:
        from ..autopilot_scope import is_option_trade
        if is_option_trade(trade):
            return
    except Exception:
        pass

    try:
        ticker = (trade.ticker or "").strip().upper()
        if not ticker:
            return
        entry_qty = float(
            getattr(trade, "filled_quantity", None)
            or getattr(trade, "quantity", None)
            or 0.0
        )
        entry_price = float(
            getattr(trade, "avg_fill_price", None)
            or getattr(trade, "entry_price", None)
            or 0.0
        )
        if entry_qty > 0 and entry_price > 0:
            _ledger.record_entry_fill(
                db,
                source="live",
                trade_id=int(trade.id),
                user_id=trade.user_id,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                ticker=ticker,
                direction=(trade.direction or "long"),
                quantity=entry_qty,
                fill_price=entry_price,
                fee=0.0,
                broker_source=getattr(trade, "broker_source", None),
                event_ts=getattr(trade, "filled_at", None) or getattr(trade, "entry_date", None),
                provenance={"legacy_path": f"on_{ledger_source}", "lazy_emit": True},
            )

        exit_qty = float(
            getattr(trade, "filled_quantity", None)
            or getattr(trade, "quantity", None)
            or 0.0
        )
        exit_price = float(getattr(trade, "exit_price", None) or 0.0)
        if exit_qty > 0 and exit_price > 0 and entry_price > 0:
            _ledger.record_exit_fill(
                db,
                source="live",
                trade_id=int(trade.id),
                user_id=trade.user_id,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                ticker=ticker,
                direction=(trade.direction or "long"),
                quantity=exit_qty,
                fill_price=exit_price,
                entry_price=entry_price,
                fee=0.0,
                broker_source=getattr(trade, "broker_source", None),
                event_ts=getattr(trade, "exit_date", None),
                provenance={"legacy_path": f"on_{ledger_source}", "exit_reason": getattr(trade, "exit_reason", None)},
            )

        legacy_pnl = getattr(trade, "pnl", None)
        if legacy_pnl is not None:
            _ledger.reconcile_trade(
                db,
                source="live",
                trade_id=int(trade.id),
                user_id=trade.user_id,
                scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                ticker=ticker,
                legacy_pnl=float(legacy_pnl),
                provenance={"legacy_path": f"on_{ledger_source}"},
            )
    except Exception:
        logger.debug("[execution_hooks] economic_ledger live hook failed", exc_info=True)


def on_live_trade_closed(
    db: Session,
    trade: Trade,
    *,
    source: str,
    extra: Optional[dict[str, Any]] = None,
) -> None:
    """Portfolio or operator-initiated close of a live ``Trade`` row (before commit)."""
    try:
        merged = {**trade_close_attribution_dict(trade), **(extra or {})}
        emit_live_trade_closed_outcome(
            db,
            trade_id=int(trade.id),
            user_id=trade.user_id,
            ticker=(trade.ticker or "").strip(),
            source=source,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            extra=merged,
        )
        _phase_a_economic_ledger_live_shadow(db, trade, ledger_source="live_trade_closed")
        uid = trade.user_id
        if uid is not None:
            enqueue_or_refresh_debounced_work(
                db,
                event_type="execution_feedback_digest",
                dedupe_key=f"exec_fb_digest:user:{int(uid)}",
                payload={"user_id": int(uid), "trigger": "live_trade_closed", "source": source},
                debounce_seconds=_exec_feedback_debounce_s(),
                lease_scope="execution_feedback",
            )
    except Exception:
        logger.debug("[execution_hooks] on_live_trade_closed failed", exc_info=True)

    _record_venue_truth(db, trade, paper_bool=False)
    _refresh_rolling_cost_estimate(db, trade)


def on_broker_reconciled_close(
    db: Session,
    trade: Trade,
    *,
    source: str,
) -> None:
    """Broker sync inferred close (position vanished, manual cleanup during RH sync, etc.)."""
    try:
        att = trade_close_attribution_dict(trade)
        emit_broker_fill_closed_outcome(
            db,
            trade_id=int(trade.id),
            user_id=trade.user_id,
            ticker=(trade.ticker or "").strip(),
            broker_source=(getattr(trade, "broker_source", None) or "") or "unknown",
            source=source,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            extra=att,
        )
        _phase_a_economic_ledger_live_shadow(db, trade, ledger_source="broker_reconciled_close")
        uid = trade.user_id
        if uid is not None:
            enqueue_or_refresh_debounced_work(
                db,
                event_type="execution_feedback_digest",
                dedupe_key=f"exec_fb_digest:user:{int(uid)}",
                payload={"user_id": int(uid), "trigger": "broker_fill_closed", "source": source},
                debounce_seconds=_exec_feedback_debounce_s(),
                lease_scope="execution_feedback",
            )
    except Exception:
        logger.debug("[execution_hooks] on_broker_reconciled_close failed", exc_info=True)

    _record_venue_truth(db, trade, paper_bool=False)
    _refresh_rolling_cost_estimate(db, trade)
