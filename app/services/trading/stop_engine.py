"""
Stop Engine — brain-decided stop-loss management for open positions.

Evaluates every open trade against current market data and applies a
state-machine for stop management:
    INITIAL -> BREAKEVEN (at +1R) -> TRAILING (at +2R)
    Any state -> WARN (within 0.25R of stop) -> TRIGGERED (stop breach)

Stop policies use the same ATR multiples as scanner.py adaptive weights.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


# ── Stop state machine ──────────────────────────────────────────────

class StopState(str, Enum):
    INITIAL = "initial"
    BREAKEVEN = "breakeven"
    TRAILING = "trailing"
    WARN = "warn"
    TRIGGERED = "triggered"


# ── ATR-based stop policies per strategy type ────────────────────────

STOP_POLICIES: dict[str, dict[str, float]] = {
    "atr_swing": {
        "stop_mult_normal": 2.0,
        "stop_mult_volatile": 2.5,
        "target_mult": 3.0,
        "trail_k": 2.5,
        "volatility_threshold": 3.0,
    },
    "atr_breakout": {
        "stop_mult_normal": 2.5,
        "stop_mult_volatile": 2.5,
        "target_mult": 5.0,
        "trail_k": 2.5,
        "volatility_threshold": 3.0,
    },
    "atr_intraday": {
        "stop_mult_normal": 1.5,
        "stop_mult_volatile": 1.5,
        "target_mult": 2.5,
        "trail_k": 1.5,
        "volatility_threshold": 3.0,
    },
    "atr_crypto_breakout": {
        "stop_mult_normal": 2.0,
        "stop_mult_volatile": 2.5,
        "target_mult": 5.0,
        "trail_k": 2.0,
        "volatility_threshold": 3.0,
    },
    "snapshot": {
        "stop_mult_normal": 2.0,
        "stop_mult_volatile": 2.5,
        "target_mult": 3.0,
        "trail_k": 2.5,
        "volatility_threshold": 3.0,
    },
    "pct_fallback": {
        "stop_mult_normal": 2.0,
        "stop_mult_volatile": 2.5,
        "target_mult": 3.0,
        "trail_k": 2.5,
        "volatility_threshold": 3.0,
    },
}

# R-multiple thresholds for state transitions
BREAKEVEN_R = 1.0
TRAILING_R = 2.0
WARN_PROXIMITY_R = 0.25
TIME_STOP_MIN_R = 0.5
TIME_STOP_BARS_DEFAULT = 50


@dataclass
class MarketContext:
    price: float
    bid: float | None = None
    ask: float | None = None
    atr: float | None = None
    volume: float | None = None
    quote_ts: datetime | None = None
    spread_bps: float | None = None
    is_stale: bool = False


@dataclass
class StopDecisionResult:
    trade_id: int
    state: StopState
    old_stop: float | None
    new_stop: float | None
    alert_event: str | None = None  # None / STOP_APPROACHING / STOP_HIT / BREAKEVEN_REACHED / STOP_TIGHTENED / DATA_STALE / TIME_STOP_WARN
    recommended_action: str = "hold"  # hold / reduce / exit
    reason: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    watermark_updated: bool = False
    new_watermark: float | None = None
    new_trail_stop: float | None = None


def _is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith("-USD")


def _get_policy(stop_model: str | None) -> dict[str, float]:
    return STOP_POLICIES.get(stop_model or "snapshot", STOP_POLICIES["snapshot"])


def _compute_initial_stop(
    entry: float, direction: str, atr: float | None, price: float,
    stop_model: str | None, is_crypto: bool,
) -> tuple[float, float]:
    """Compute initial stop and target when none exist on the trade."""
    policy = _get_policy(stop_model)
    if atr and atr > 0 and price > 0:
        vol_pct = (atr / price) * 100
        mult = policy["stop_mult_volatile"] if vol_pct > policy["volatility_threshold"] else policy["stop_mult_normal"]
        tgt_mult = policy["target_mult"]
        if direction == "long":
            sl = entry - mult * atr
            tp = entry + tgt_mult * atr
        else:
            sl = entry + mult * atr
            tp = entry - tgt_mult * atr
    else:
        if direction == "long":
            sl = entry * 0.92
            tp = entry * 1.15
        else:
            sl = entry * 1.08
            tp = entry * 0.85

    rd = 8 if is_crypto else 4
    return round(sl, rd), round(tp, rd)


def evaluate_trade(
    trade,
    market: MarketContext,
    db: Session | None = None,
) -> StopDecisionResult:
    """
    Evaluate a single open trade against current market conditions.
    Returns a StopDecisionResult with any stop changes and alert events.

    `trade` is a SQLAlchemy Trade ORM object.
    """
    result = StopDecisionResult(
        trade_id=trade.id,
        state=StopState.INITIAL,
        old_stop=trade.stop_loss,
        new_stop=trade.stop_loss,
    )

    entry = trade.entry_price
    direction = trade.direction or "long"
    is_long = direction == "long"
    crypto = _is_crypto(trade.ticker)

    if not entry or entry <= 0:
        result.reason = "no entry price"
        return result

    if market.price <= 0:
        result.reason = "no valid price"
        return result

    # Data staleness check
    if market.is_stale:
        result.alert_event = "DATA_STALE"
        result.reason = "quote is stale — no stop changes"
        return result

    stop = trade.stop_loss
    target = trade.take_profit
    policy = _get_policy(trade.stop_model)

    # If trade has no stop/target yet, compute initial values
    if not stop or not target:
        stop, target = _compute_initial_stop(
            entry, direction, market.atr, market.price,
            trade.stop_model, crypto,
        )
        result.new_stop = stop
        result.alert_event = "STOP_TIGHTENED"
        result.reason = "initial stop computed"
        result.inputs = {"entry": entry, "atr": market.atr, "model": trade.stop_model}

    price = market.price
    R = abs(entry - stop)
    if R <= 0:
        R = entry * 0.02  # safeguard

    # ── Update high watermark ──
    hwm = trade.high_watermark or entry
    if is_long and price > hwm:
        hwm = price
        result.watermark_updated = True
    elif not is_long and price < hwm:
        hwm = price
        result.watermark_updated = True
    result.new_watermark = hwm

    # ── Determine current R-multiple ──
    if is_long:
        current_r = (price - entry) / R if R > 0 else 0
    else:
        current_r = (entry - price) / R if R > 0 else 0

    result.inputs = {
        "price": price, "entry": entry, "stop": stop, "target": target,
        "R": round(R, 6), "current_r": round(current_r, 2),
        "atr": market.atr, "hwm": hwm, "model": trade.stop_model,
    }

    # ── Break-even check (+1R) ──
    fees_buffer = entry * 0.002  # 0.2% covers typical commission + spread
    if current_r >= BREAKEVEN_R:
        if is_long:
            be_stop = entry + fees_buffer
        else:
            be_stop = entry - fees_buffer

        if (is_long and (not stop or be_stop > stop)) or (not is_long and (not stop or be_stop < stop)):
            old = stop
            stop = round(be_stop, 8 if crypto else 4)
            result.new_stop = stop
            result.state = StopState.BREAKEVEN
            if old != stop:
                result.alert_event = "BREAKEVEN_REACHED"
                result.reason = f"moved stop to break-even at +{current_r:.1f}R"

    # ── Chandelier trailing check (+2R) ──
    if current_r >= TRAILING_R and market.atr and market.atr > 0:
        k = policy["trail_k"]
        if is_long:
            trail = hwm - k * market.atr
        else:
            trail = hwm + k * market.atr
        trail = round(trail, 8 if crypto else 4)

        # Monotonic: stop only tightens
        if (is_long and trail > stop) or (not is_long and trail < stop):
            old = stop
            stop = trail
            result.new_stop = stop
            result.new_trail_stop = trail
            result.state = StopState.TRAILING
            if old != stop:
                result.alert_event = "STOP_TIGHTENED"
                result.reason = f"chandelier trail at +{current_r:.1f}R (k={k})"
    elif current_r >= BREAKEVEN_R:
        result.state = StopState.BREAKEVEN
    else:
        result.state = StopState.INITIAL

    # ── Stop breach check ──
    stop_breached = False
    if is_long and price <= stop:
        stop_breached = True
    elif not is_long and price >= stop:
        stop_breached = True

    if stop_breached:
        if is_long:
            pnl_pct = round((price - entry) / entry * 100, 2)
        else:
            pnl_pct = round((entry - price) / entry * 100, 2)
        result.state = StopState.TRIGGERED
        result.alert_event = "STOP_HIT"
        result.recommended_action = "exit"
        result.reason = f"stop breached at ${price:,.4f} (stop=${stop:,.4f}, P&L={pnl_pct:+.1f}%)"
        return result

    # ── Proximity warning ──
    if is_long:
        distance_to_stop = price - stop
    else:
        distance_to_stop = stop - price

    if distance_to_stop > 0 and R > 0 and (distance_to_stop / R) <= WARN_PROXIMITY_R:
        if result.alert_event not in ("BREAKEVEN_REACHED", "STOP_TIGHTENED"):
            result.alert_event = "STOP_APPROACHING"
            pct_from_stop = round(distance_to_stop / price * 100, 2)
            result.reason = f"within {pct_from_stop:.1f}% of stop (${stop:,.4f})"
            result.state = StopState.WARN

    # ── Target hit check ──
    target_hit = False
    if target:
        if is_long and price >= target:
            target_hit = True
        elif not is_long and price <= target:
            target_hit = True

    if target_hit:
        if is_long:
            pnl_pct = round((price - entry) / entry * 100, 2)
        else:
            pnl_pct = round((entry - price) / entry * 100, 2)
        result.alert_event = "TARGET_HIT"
        result.recommended_action = "reduce"
        result.reason = f"target reached at ${price:,.4f} (target=${target:,.4f}, P&L=+{pnl_pct:.1f}%)"

    return result


def _record_stop_decision(db: Session, trade_id: int, result: StopDecisionResult) -> None:
    """Persist a stop decision to the audit table."""
    from ...models.trading import StopDecision as SDModel
    record = SDModel(
        trade_id=trade_id,
        as_of_ts=datetime.utcnow(),
        state=result.state.value,
        old_stop=result.old_stop,
        new_stop=result.new_stop,
        trigger=result.alert_event,
        inputs_json=result.inputs,
        reason=result.reason,
        executed=False,
    )
    db.add(record)


def _apply_stop_to_trade(db: Session, trade, result: StopDecisionResult) -> None:
    """Update the Trade row with any stop/watermark changes."""
    changed = False
    if result.new_stop is not None and result.new_stop != trade.stop_loss:
        trade.stop_loss = result.new_stop
        changed = True
    if result.new_trail_stop is not None and result.new_trail_stop != trade.trail_stop:
        trade.trail_stop = result.new_trail_stop
        changed = True
    if result.watermark_updated and result.new_watermark is not None:
        trade.high_watermark = result.new_watermark
        changed = True
    if changed:
        db.add(trade)


def _fetch_market_context(ticker: str, staleness_secs: int = 300) -> MarketContext:
    """Build a MarketContext from the market_data service."""
    from .market_data import fetch_quote
    try:
        q = fetch_quote(ticker)
    except Exception:
        return MarketContext(price=0, is_stale=True)

    if not q:
        return MarketContext(price=0, is_stale=True)

    price = q.get("price", 0) or 0
    if not price or price <= 0:
        return MarketContext(price=0, is_stale=True)

    # ATR from the indicator snapshot cache or compute
    atr = None
    try:
        from .market_data import get_indicator_snapshot
        snap = get_indicator_snapshot(ticker, interval="1d")
        if snap:
            atr_block = snap.get("atr") or {}
            atr = atr_block.get("value") if isinstance(atr_block, dict) else None
    except Exception:
        pass

    return MarketContext(
        price=float(price),
        bid=q.get("bid"),
        ask=q.get("ask"),
        atr=float(atr) if atr else None,
        volume=q.get("volume"),
        quote_ts=datetime.utcnow(),
    )


def evaluate_all(
    db: Session,
    user_id: int | None = None,
) -> dict[str, Any]:
    """
    Evaluate all open trades for a user (or all users if None).
    Returns summary dict with counts and alert list.
    """
    from ...models.trading import Trade

    filters = [Trade.status == "open"]
    if user_id is not None:
        filters.append(Trade.user_id == user_id)

    trades = db.query(Trade).filter(*filters).all()

    summary: dict[str, Any] = {
        "total_checked": 0,
        "stops_hit": 0,
        "targets_hit": 0,
        "stops_tightened": 0,
        "breakevens": 0,
        "warnings": 0,
        "data_stale": 0,
        "alerts": [],
    }

    for trade in trades:
        try:
            market = _fetch_market_context(trade.ticker)
            result = evaluate_trade(trade, market, db)
            summary["total_checked"] += 1

            if result.alert_event:
                summary["alerts"].append({
                    "trade_id": trade.id,
                    "ticker": trade.ticker,
                    "event": result.alert_event,
                    "state": result.state.value,
                    "reason": result.reason,
                    "price": market.price,
                    "old_stop": result.old_stop,
                    "new_stop": result.new_stop,
                    "action": result.recommended_action,
                })

            if result.alert_event == "STOP_HIT":
                summary["stops_hit"] += 1
            elif result.alert_event == "TARGET_HIT":
                summary["targets_hit"] += 1
            elif result.alert_event == "STOP_TIGHTENED":
                summary["stops_tightened"] += 1
            elif result.alert_event == "BREAKEVEN_REACHED":
                summary["breakevens"] += 1
            elif result.alert_event == "STOP_APPROACHING":
                summary["warnings"] += 1
            elif result.alert_event == "DATA_STALE":
                summary["data_stale"] += 1

            # Persist stop changes and audit record
            if result.alert_event and result.alert_event != "DATA_STALE":
                _record_stop_decision(db, trade.id, result)
                _apply_stop_to_trade(db, trade, result)

        except Exception as e:
            logger.warning("[stop_engine] Error evaluating %s (id=%s): %s", trade.ticker, trade.id, e)

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error("[stop_engine] Failed to commit stop updates", exc_info=True)

    return summary


def dispatch_stop_alerts(
    db: Session,
    user_id: int | None,
    summary: dict[str, Any],
) -> int:
    """
    Turn stop engine alerts into user-facing dispatched alerts.
    Returns count of alerts dispatched.
    """
    from .alerts import dispatch_alert

    STOP_HIT = "stop_hit"
    TARGET_HIT = "target_hit"
    STOP_APPROACHING = "stop_approaching"
    BREAKEVEN_REACHED = "breakeven_reached"
    STOP_TIGHTENED = "stop_tightened"

    dispatched = 0
    for alert in summary.get("alerts", []):
        event = alert["event"]
        ticker = alert["ticker"]
        price = alert.get("price", 0)
        reason = alert.get("reason", "")

        if event == "STOP_HIT":
            msg = f"STOP HIT: {ticker} at ${price:,.2f} — {reason}"
            dispatch_alert(
                db, user_id, STOP_HIT, ticker, msg,
                skip_throttle=True,
            )
            dispatched += 1

        elif event == "TARGET_HIT":
            msg = f"TARGET HIT: {ticker} at ${price:,.2f} — {reason}"
            dispatch_alert(
                db, user_id, TARGET_HIT, ticker, msg,
                skip_throttle=True,
            )
            dispatched += 1

        elif event == "STOP_APPROACHING":
            msg = f"STOP APPROACHING: {ticker} at ${price:,.2f} — {reason}"
            dispatch_alert(
                db, user_id, STOP_APPROACHING, ticker, msg,
                skip_throttle=False,
            )
            dispatched += 1

        elif event == "BREAKEVEN_REACHED":
            msg = f"BREAKEVEN: {ticker} stop moved to entry — {reason}"
            dispatch_alert(
                db, user_id, BREAKEVEN_REACHED, ticker, msg,
                skip_throttle=True,
            )
            dispatched += 1

        elif event == "STOP_TIGHTENED":
            old_s = alert.get("old_stop")
            new_s = alert.get("new_stop")
            msg = f"STOP TIGHTENED: {ticker} stop ${old_s:,.2f}→${new_s:,.2f} — {reason}"
            dispatch_alert(
                db, user_id, STOP_TIGHTENED, ticker, msg,
                skip_throttle=True,
            )
            dispatched += 1

    return dispatched
