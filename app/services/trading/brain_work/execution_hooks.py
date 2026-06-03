"""Authoritative ledger hooks for execution feedback (paper / live / broker)."""

from __future__ import annotations

import logging
import math
from datetime import UTC, datetime, timedelta
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
from .execution_attribution import (
    paper_trade_close_attribution_dict,
    trade_close_attribution_dict,
)
from .ledger import enqueue_or_refresh_debounced_work

logger = logging.getLogger(__name__)


def _exec_feedback_debounce_s() -> int:
    return int(getattr(settings, "brain_work_exec_feedback_debounce_seconds", 45))


TIME_DECAY_EXIT_VARIANT_SOURCE = "paper_time_decay_edge_miss"
TIME_DECAY_EXIT_VARIANT_REASONS = frozenset({
    "exit_engine_time_decay",
    "time_decay",
    "exit_time_decay",
})
TIME_DECAY_EXIT_VARIANT_SWEEP_DEFAULT_LOOKBACK_HOURS = 48.0
TIME_DECAY_EXIT_VARIANT_SWEEP_DEFAULT_LIMIT = 25
TIME_DECAY_EXIT_VARIANT_SWEEP_FETCH_MULTIPLIER = 6


def _json_dict(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _finite_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return out if math.isfinite(out) else None


def _paper_expected_net_pct(pt: PaperTrade) -> float | None:
    sig = _json_dict(getattr(pt, "signal_json", None))
    edge = _json_dict(sig.get("entry_edge"))
    expected = _finite_float(edge.get("expected_net_pct"))
    if expected is not None:
        return expected
    return _finite_float(sig.get("entry_edge_expected_net_pct"))


def _paper_asset_class(pt: PaperTrade) -> str:
    sig = _json_dict(getattr(pt, "signal_json", None))
    raw = str(sig.get("asset_class") or sig.get("asset_type") or "").strip().lower()
    if raw in {"crypto", "coin", "coinbase_spot"}:
        return "crypto"
    if raw in {"option", "options", "robinhood_options"}:
        return "options"
    ticker = str(getattr(pt, "ticker", "") or "").strip().upper()
    if ticker.endswith("-USD"):
        return "crypto"
    return "stock"


def _emit_time_decay_exit_variant_work(
    db: Session,
    pt: PaperTrade,
    *,
    close_extra: dict[str, Any],
) -> int | None:
    reason = str(getattr(pt, "exit_reason", "") or "").strip().lower()
    if reason not in TIME_DECAY_EXIT_VARIANT_REASONS:
        return None
    pattern_id = getattr(pt, "scan_pattern_id", None)
    if pattern_id is None:
        return None
    expected_net_pct = _paper_expected_net_pct(pt)
    if expected_net_pct is None or expected_net_pct <= 0.0:
        return None
    realized_return_pct = _finite_float(close_extra.get("realized_return_pct"))
    pnl = _finite_float(getattr(pt, "pnl", None))
    if (realized_return_pct is None or realized_return_pct >= 0.0) and (
        pnl is None or pnl >= 0.0
    ):
        return None

    sig = _json_dict(getattr(pt, "signal_json", None))
    paper_meta = _json_dict(sig.get("_paper_meta"))
    exit_config = _json_dict(paper_meta.get("exit_config"))
    asset_class = _paper_asset_class(pt)
    edge_bucket = max(0, min(50, int(math.floor(expected_net_pct))))
    return_shortfall = abs(realized_return_pct) if realized_return_pct is not None else 0.0
    expected_value = round(max(expected_net_pct, expected_net_pct + return_shortfall), 6)

    from ..edge_reliability import EXIT_VARIANT_REFRESH, emit_targeted_profitability_work

    return emit_targeted_profitability_work(
        db,
        event_type=EXIT_VARIANT_REFRESH,
        scan_pattern_id=int(pattern_id),
        source=TIME_DECAY_EXIT_VARIANT_SOURCE,
        asset_class=asset_class,
        evidence_fingerprint=f"td_loss_e{edge_bucket}_{asset_class}_v1",
        payload={
            "cash_deployment_category": "positive_ev_time_decay_loss",
            "recommended_work_event": EXIT_VARIANT_REFRESH,
            "paper_trade_id": int(getattr(pt, "id", 0) or 0),
            "ticker": str(getattr(pt, "ticker", "") or "").strip().upper(),
            "exit_reason": reason,
            "expected_net_pct": round(float(expected_net_pct), 6),
            "realized_return_pct": (
                round(float(realized_return_pct), 6)
                if realized_return_pct is not None
                else None
            ),
            "pnl": pnl,
            "expected_evidence_value": expected_value,
            "graduation_blocker": "exit_thesis_mismatch",
            "paper_shadow": bool(
                getattr(pt, "paper_shadow_of_alert_id", None)
                or sig.get("paper_shadow")
                or sig.get("shadow_of_alert_id")
            ),
            "paper_shadow_of_alert_id": getattr(pt, "paper_shadow_of_alert_id", None),
            "entry_price": getattr(pt, "entry_price", None),
            "exit_price": getattr(pt, "exit_price", None),
            "quantity": getattr(pt, "quantity", None),
            "timeframe": exit_config.get("timeframe"),
            "max_bars": exit_config.get("max_bars"),
            "target_reward_fraction": exit_config.get("target_reward_fraction")
            or exit_config.get("reward_fraction")
            or exit_config.get("target_fraction"),
            "stop_loss_fraction": exit_config.get("stop_loss_fraction")
            or exit_config.get("hard_stop_loss_fraction")
            or exit_config.get("loss_fraction"),
            "exit_defaults_source": exit_config.get("exit_defaults_source"),
            "dynamic_monitor_reason": _json_dict(
                paper_meta.get("dynamic_monitor")
            ).get("last_reason"),
        },
    )


def _is_autotrader_or_shadow_paper(pt: PaperTrade) -> bool:
    sig = _json_dict(getattr(pt, "signal_json", None))
    return bool(
        getattr(pt, "paper_shadow_of_alert_id", None)
        or sig.get("paper_shadow")
        or sig.get("shadow_of_alert_id")
        or sig.get("auto_trader_v1")
    )


def enqueue_recent_time_decay_exit_variant_work(
    db: Session,
    *,
    lookback_hours: float | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Backfill exit-variant work for recent positive-edge time-decay losses.

    The close hook covers future rows. This bounded sweep catches already
    closed paper/shadow rows without scanning the full paper-trade table.
    """
    enabled = bool(
        getattr(settings, "brain_work_time_decay_exit_variant_sweep_enabled", True)
    )
    if not enabled:
        return {"ok": True, "skipped": True, "reason": "disabled_by_setting"}

    lookback = _finite_float(
        lookback_hours
        if lookback_hours is not None
        else getattr(
            settings,
            "brain_work_time_decay_exit_variant_sweep_lookback_hours",
            TIME_DECAY_EXIT_VARIANT_SWEEP_DEFAULT_LOOKBACK_HOURS,
        )
    )
    lookback = max(1.0, lookback or TIME_DECAY_EXIT_VARIANT_SWEEP_DEFAULT_LOOKBACK_HOURS)
    max_items = int(
        limit
        if limit is not None
        else getattr(
            settings,
            "brain_work_time_decay_exit_variant_sweep_limit",
            TIME_DECAY_EXIT_VARIANT_SWEEP_DEFAULT_LIMIT,
        )
    )
    if max_items <= 0:
        return {"ok": True, "skipped": True, "reason": "limit_not_positive"}

    cutoff = datetime.now(UTC).replace(tzinfo=None) - timedelta(hours=lookback)
    fetch_limit = max_items * TIME_DECAY_EXIT_VARIANT_SWEEP_FETCH_MULTIPLIER
    rows = (
        db.query(PaperTrade)
        .filter(PaperTrade.status == "closed")
        .filter(PaperTrade.exit_reason.in_(tuple(TIME_DECAY_EXIT_VARIANT_REASONS)))
        .filter(PaperTrade.exit_date >= cutoff)
        .filter(PaperTrade.pnl < 0)
        .filter(PaperTrade.scan_pattern_id.isnot(None))
        .order_by(PaperTrade.exit_date.desc(), PaperTrade.id.desc())
        .limit(fetch_limit)
        .all()
    )

    result: dict[str, Any] = {
        "ok": True,
        "skipped": False,
        "lookback_hours": lookback,
        "limit": max_items,
        "candidate_rows": len(rows),
        "checked": 0,
        "queued": 0,
        "deduped_or_existing": 0,
        "skipped_not_shadow": 0,
        "skipped_no_positive_edge": 0,
        "skipped_no_loss": 0,
    }
    for row in rows:
        if int(result["checked"]) >= max_items:
            break
        if not _is_autotrader_or_shadow_paper(row):
            result["skipped_not_shadow"] += 1
            continue
        expected_net_pct = _paper_expected_net_pct(row)
        if expected_net_pct is None or expected_net_pct <= 0.0:
            result["skipped_no_positive_edge"] += 1
            continue
        close_extra = paper_trade_close_attribution_dict(row)
        realized_return_pct = _finite_float(close_extra.get("realized_return_pct"))
        pnl = _finite_float(getattr(row, "pnl", None))
        if (realized_return_pct is None or realized_return_pct >= 0.0) and (
            pnl is None or pnl >= 0.0
        ):
            result["skipped_no_loss"] += 1
            continue
        result["checked"] += 1
        event_id = _emit_time_decay_exit_variant_work(
            db,
            row,
            close_extra=close_extra,
        )
        if event_id is None:
            result["deduped_or_existing"] += 1
        else:
            result["queued"] += 1
    return result


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
        close_extra = paper_trade_close_attribution_dict(pt)
        emit_paper_trade_closed_outcome(
            db,
            paper_trade_id=int(pt.id),
            user_id=pt.user_id,
            scan_pattern_id=pt.scan_pattern_id,
            ticker=(pt.ticker or "").strip(),
            pnl=pt.pnl,
            exit_reason=(pt.exit_reason or "").strip(),
            extra=close_extra,
        )
        try:
            _emit_time_decay_exit_variant_work(db, pt, close_extra=close_extra)
        except Exception:
            logger.debug(
                "[execution_hooks] time-decay exit variant work enqueue failed",
                exc_info=True,
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
