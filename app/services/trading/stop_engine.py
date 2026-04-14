"""
Stop Engine — brain-decided stop-loss management for open positions.

Before making any stop decision, the engine consults the CHILI trading brain:
  1. Linked ScanPattern → exit_config (ATR mult, max bars, BOS settings)
  2. Pattern lifecycle stage → decayed/retired = tighter stops
  3. Market regime → risk_off = tighter stops, risk_on = more room
  4. Pattern performance → low win-rate patterns get less leash

State machine:
    INITIAL -> BREAKEVEN (at +1R) -> TRAILING (at +2R)
    Any state -> WARN (within proximity of stop) -> TRIGGERED (stop breach)
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


# ── ATR-based stop policies per strategy type (defaults) ──────────

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

# R-multiple thresholds for state transitions (adjusted by brain context)
BREAKEVEN_R = 1.0
TRAILING_R = 2.0
WARN_PROXIMITY_R = 0.25
TIME_STOP_MIN_R = 0.5
TIME_STOP_BARS_DEFAULT = 50

# Regime adjustment multipliers applied to stop distance
_REGIME_STOP_TIGHTEN = {
    "risk_off": 0.80,   # 20% tighter stops
    "cautious": 1.00,
    "risk_on": 1.10,    # 10% more room
}
_REGIME_WARN_PROXIMITY = {
    "risk_off": 0.40,   # warn further from stop
    "cautious": 0.25,
    "risk_on": 0.20,    # only warn very close
}

# Lifecycle adjustments to stop multiplier
_LIFECYCLE_STOP_FACTOR = {
    "decayed": 0.75,    # 25% tighter — pattern losing edge
    "retired": 0.70,    # 30% tighter — should be closing
    "candidate": 0.90,  # 10% tighter — unproven
    "backtested": 0.95,
    "validated": 1.00,
    "challenged": 0.85,
    "promoted": 1.05,   # 5% more room — trusted
    "live": 1.05,
}


@dataclass
class BrainContext:
    """Strategy and brain context consulted before each stop decision."""
    pattern_name: str | None = None
    pattern_id: int | None = None
    lifecycle_stage: str | None = None
    exit_config: dict[str, Any] | None = None
    pattern_win_rate: float | None = None
    pattern_timeframe: str | None = None
    regime: str = "cautious"
    regime_vix: str | None = None
    regime_numeric: int = 0
    stop_mult_override: float | None = None
    target_mult_override: float | None = None
    lifecycle_stop_factor: float = 1.0
    regime_stop_factor: float = 1.0
    warn_proximity: float = WARN_PROXIMITY_R
    breakeven_r: float = BREAKEVEN_R
    trailing_r: float = TRAILING_R

    def effective_stop_mult(self, base_mult: float) -> float:
        """Apply brain adjustments to the base ATR stop multiplier."""
        if self.stop_mult_override:
            base_mult = self.stop_mult_override
        return base_mult * self.lifecycle_stop_factor * self.regime_stop_factor

    def summary_dict(self) -> dict[str, Any]:
        """Compact summary for audit logging."""
        return {
            k: v for k, v in {
                "pattern": self.pattern_name,
                "pattern_id": self.pattern_id,
                "lifecycle": self.lifecycle_stage,
                "regime": self.regime,
                "regime_vix": self.regime_vix,
                "win_rate": self.pattern_win_rate,
                "stop_mult_override": self.stop_mult_override,
                "lifecycle_factor": self.lifecycle_stop_factor,
                "regime_factor": self.regime_stop_factor,
                "warn_proximity": self.warn_proximity,
                "breakeven_r": self.breakeven_r,
            }.items() if v is not None
        }


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
    alert_event: str | None = None
    recommended_action: str = "hold"
    reason: str = ""
    inputs: dict[str, Any] = field(default_factory=dict)
    watermark_updated: bool = False
    new_watermark: float | None = None
    new_trail_stop: float | None = None
    brain_context: dict[str, Any] = field(default_factory=dict)


def _is_crypto(ticker: str) -> bool:
    return ticker.upper().endswith("-USD")


def _get_policy(stop_model: str | None) -> dict[str, float]:
    return STOP_POLICIES.get(stop_model or "snapshot", STOP_POLICIES["snapshot"])


def _build_brain_context(trade, db: Session | None) -> BrainContext:
    """Consult the trading brain: pattern strategy, lifecycle, and regime."""
    ctx = BrainContext()

    # 1. Linked ScanPattern → exit_config + lifecycle + performance
    if db and getattr(trade, "scan_pattern_id", None):
        try:
            from ...models.trading import ScanPattern
            pattern = db.query(ScanPattern).filter(
                ScanPattern.id == trade.scan_pattern_id
            ).first()
            if pattern:
                ctx.pattern_name = pattern.name
                ctx.pattern_id = pattern.id
                ctx.lifecycle_stage = getattr(pattern, "lifecycle_stage", None)
                ctx.pattern_win_rate = pattern.win_rate
                ctx.pattern_timeframe = pattern.timeframe

                ec = getattr(pattern, "exit_config", None)
                if ec:
                    if isinstance(ec, str):
                        try:
                            ec = json.loads(ec)
                        except (json.JSONDecodeError, TypeError):
                            ec = None
                    if isinstance(ec, dict):
                        ctx.exit_config = ec
                        if ec.get("atr_mult") is not None:
                            ctx.stop_mult_override = float(ec["atr_mult"])
                        if ec.get("target_mult") is not None:
                            ctx.target_mult_override = float(ec["target_mult"])
        except Exception as e:
            logger.debug("[stop_engine] Failed to load pattern for trade %s: %s", trade.id, e)

    # 2. Lifecycle adjustment
    stage = ctx.lifecycle_stage or "candidate"
    ctx.lifecycle_stop_factor = _LIFECYCLE_STOP_FACTOR.get(stage, 1.0)

    if stage in ("decayed", "retired"):
        ctx.breakeven_r = 0.75
        ctx.trailing_r = 1.5

    # 3. Market regime
    try:
        from .regime import get_regime_indicators
        regime = get_regime_indicators()
        ctx.regime = regime.get("regime_composite", "cautious")
        ctx.regime_vix = regime.get("regime_vix")
        ctx.regime_numeric = regime.get("regime_numeric", 0)
    except Exception as e:
        logger.debug("[stop_engine] Failed to get regime: %s", e)

    ctx.regime_stop_factor = _REGIME_STOP_TIGHTEN.get(ctx.regime, 1.0)
    ctx.warn_proximity = _REGIME_WARN_PROXIMITY.get(ctx.regime, WARN_PROXIMITY_R)

    # 4. Low win-rate patterns get tighter stops
    if ctx.pattern_win_rate is not None and ctx.pattern_win_rate < 0.40:
        ctx.lifecycle_stop_factor *= 0.90
        logger.debug(
            "[stop_engine] Pattern %s win_rate=%.0f%% — tightening stops 10%%",
            ctx.pattern_name, ctx.pattern_win_rate * 100,
        )

    return ctx


def _compute_initial_stop(
    entry: float, direction: str, atr: float | None, price: float,
    stop_model: str | None, is_crypto: bool, brain: BrainContext | None = None,
) -> tuple[float, float]:
    """Compute initial stop and target, incorporating brain context."""
    policy = _get_policy(stop_model)
    if atr and atr > 0 and price > 0:
        vol_pct = (atr / price) * 100
        base_mult = policy["stop_mult_volatile"] if vol_pct > policy["volatility_threshold"] else policy["stop_mult_normal"]
        tgt_base = policy["target_mult"]

        if brain:
            mult = brain.effective_stop_mult(base_mult)
            tgt_mult = brain.target_mult_override or tgt_base
        else:
            mult = base_mult
            tgt_mult = tgt_base

        if direction == "long":
            sl = entry - mult * atr
            tp = entry + tgt_mult * atr
        else:
            sl = entry + mult * atr
            tp = entry - tgt_mult * atr
    else:
        pct_stop = 0.08 if (brain and brain.lifecycle_stage in ("decayed", "retired")) else 0.08
        if direction == "long":
            sl = entry * (1.0 - pct_stop)
            tp = entry * 1.15
        else:
            sl = entry * (1.0 + pct_stop)
            tp = entry * 0.85

    rd = 8 if is_crypto else 4
    return round(sl, rd), round(tp, rd)


def evaluate_trade(
    trade,
    market: MarketContext,
    db: Session | None = None,
    brain: BrainContext | None = None,
) -> StopDecisionResult:
    """
    Evaluate a single open trade against current market conditions
    with full brain context (pattern strategy, lifecycle, regime).

    `trade` is a SQLAlchemy Trade ORM object.
    """
    if brain is None:
        brain = _build_brain_context(trade, db)

    result = StopDecisionResult(
        trade_id=trade.id,
        state=StopState.INITIAL,
        old_stop=trade.stop_loss,
        new_stop=trade.stop_loss,
        brain_context=brain.summary_dict(),
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

    if market.is_stale:
        result.alert_event = "DATA_STALE"
        result.reason = "quote is stale — no stop changes"
        return result

    stop = trade.stop_loss
    target = trade.take_profit
    policy = _get_policy(trade.stop_model)

    if not stop or not target:
        stop, target = _compute_initial_stop(
            entry, direction, market.atr, market.price,
            trade.stop_model, crypto, brain,
        )
        result.new_stop = stop
        result.alert_event = "STOP_TIGHTENED"
        result.reason = f"initial stop computed (strategy={brain.pattern_name or trade.stop_model}, regime={brain.regime})"
        result.inputs = {
            "entry": entry, "atr": market.atr, "model": trade.stop_model,
            "brain": brain.summary_dict(),
        }

    price = market.price
    R = abs(entry - stop)
    if R <= 0:
        R = entry * 0.02

    hwm = trade.high_watermark or entry
    if is_long and price > hwm:
        hwm = price
        result.watermark_updated = True
    elif not is_long and price < hwm:
        hwm = price
        result.watermark_updated = True
    result.new_watermark = hwm

    if is_long:
        current_r = (price - entry) / R if R > 0 else 0
    else:
        current_r = (entry - price) / R if R > 0 else 0

    result.inputs = {
        "price": price, "entry": entry, "stop": stop, "target": target,
        "R": round(R, 6), "current_r": round(current_r, 2),
        "atr": market.atr, "hwm": hwm, "model": trade.stop_model,
        "brain": brain.summary_dict(),
    }

    # ── Break-even check (brain-adjusted R threshold) ──
    be_threshold = brain.breakeven_r
    fees_buffer = entry * 0.002
    if current_r >= be_threshold:
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
                result.reason = (
                    f"moved stop to break-even at +{current_r:.1f}R "
                    f"(threshold={be_threshold}R, regime={brain.regime}, lifecycle={brain.lifecycle_stage})"
                )

    # ── Chandelier trailing (brain-adjusted R threshold and trail k) ──
    trail_threshold = brain.trailing_r
    if current_r >= trail_threshold and market.atr and market.atr > 0:
        base_k = policy["trail_k"]
        k = base_k * brain.regime_stop_factor * brain.lifecycle_stop_factor
        if is_long:
            trail = hwm - k * market.atr
        else:
            trail = hwm + k * market.atr
        trail = round(trail, 8 if crypto else 4)

        if (is_long and trail > stop) or (not is_long and trail < stop):
            old = stop
            stop = trail
            result.new_stop = stop
            result.new_trail_stop = trail
            result.state = StopState.TRAILING
            if old != stop:
                result.alert_event = "STOP_TIGHTENED"
                result.reason = (
                    f"chandelier trail at +{current_r:.1f}R (k={k:.2f}, "
                    f"regime={brain.regime}, lifecycle={brain.lifecycle_stage})"
                )
    elif current_r >= be_threshold:
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
        result.reason = (
            f"stop breached at ${price:,.4f} (stop=${stop:,.4f}, P&L={pnl_pct:+.1f}%, "
            f"strategy={brain.pattern_name or trade.stop_model}, regime={brain.regime})"
        )
        return result

    # ── Proximity warning (brain-adjusted) ──
    if is_long:
        distance_to_stop = price - stop
    else:
        distance_to_stop = stop - price

    if distance_to_stop > 0 and R > 0 and (distance_to_stop / R) <= brain.warn_proximity:
        if result.alert_event not in ("BREAKEVEN_REACHED", "STOP_TIGHTENED"):
            result.alert_event = "STOP_APPROACHING"
            pct_from_stop = round(distance_to_stop / price * 100, 2)
            result.reason = (
                f"within {pct_from_stop:.1f}% of stop (${stop:,.4f}, "
                f"regime={brain.regime}, lifecycle={brain.lifecycle_stage})"
            )
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
        result.reason = (
            f"target reached at ${price:,.4f} (target=${target:,.4f}, P&L=+{pnl_pct:.1f}%, "
            f"strategy={brain.pattern_name or trade.stop_model})"
        )

    return result


def _record_stop_decision(db: Session, trade_id: int, result: StopDecisionResult) -> None:
    """Persist a stop decision to the audit table, including brain context."""
    from ...models.trading import StopDecision as SDModel
    inputs = dict(result.inputs)
    if result.brain_context:
        inputs["brain"] = result.brain_context
    record = SDModel(
        trade_id=trade_id,
        as_of_ts=datetime.utcnow(),
        state=result.state.value,
        old_stop=result.old_stop,
        new_stop=result.new_stop,
        trigger=result.alert_event,
        inputs_json=inputs,
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
    Consults brain context (pattern strategy, lifecycle, regime) per trade.
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
        "regime": "cautious",
        "alerts": [],
    }

    # Cache regime for the batch (same for all trades in this evaluation)
    batch_regime = "cautious"
    try:
        from .regime import get_regime_indicators
        ri = get_regime_indicators()
        batch_regime = ri.get("regime_composite", "cautious")
    except Exception:
        pass
    summary["regime"] = batch_regime

    for trade in trades:
        try:
            brain = _build_brain_context(trade, db)
            market = _fetch_market_context(trade.ticker)
            result = evaluate_trade(trade, market, db, brain=brain)
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
                    "brain": brain.summary_dict(),
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

    logger.info(
        "[stop_engine] Evaluated %d trades (regime=%s): %d stops hit, %d targets, %d tightened, %d warnings",
        summary["total_checked"], batch_regime,
        summary["stops_hit"], summary["targets_hit"],
        summary["stops_tightened"], summary["warnings"],
    )

    return summary


def dispatch_stop_alerts(
    db: Session,
    user_id: int | None,
    summary: dict[str, Any],
) -> int:
    """
    Turn stop engine alerts into user-facing dispatched alerts.
    Each message includes brain context so the user sees the reasoning.
    Returns count of alerts dispatched.
    """
    from .alerts import dispatch_alert

    STOP_HIT = "stop_hit"
    TARGET_HIT = "target_hit"
    STOP_APPROACHING = "stop_approaching"
    BREAKEVEN_REACHED = "breakeven_reached"
    STOP_TIGHTENED = "stop_tightened"

    regime = summary.get("regime", "cautious")

    dispatched = 0
    for alert in summary.get("alerts", []):
        event = alert["event"]
        ticker = alert["ticker"]
        price = alert.get("price", 0)
        reason = alert.get("reason", "")
        brain = alert.get("brain", {})
        strategy_tag = brain.get("pattern") or alert.get("model", "")
        lifecycle_tag = brain.get("lifecycle", "")

        ctx_line = ""
        if strategy_tag or lifecycle_tag:
            parts = []
            if strategy_tag:
                parts.append(f"strategy: {strategy_tag}")
            if lifecycle_tag:
                parts.append(f"lifecycle: {lifecycle_tag}")
            parts.append(f"regime: {regime}")
            ctx_line = f" [{', '.join(parts)}]"

        if event == "STOP_HIT":
            msg = f"STOP HIT: {ticker} at ${price:,.2f}{ctx_line} — {reason}"
            dispatch_alert(db, user_id, STOP_HIT, ticker, msg, skip_throttle=True)
            dispatched += 1

        elif event == "TARGET_HIT":
            msg = f"TARGET HIT: {ticker} at ${price:,.2f}{ctx_line} — {reason}"
            dispatch_alert(db, user_id, TARGET_HIT, ticker, msg, skip_throttle=True)
            dispatched += 1

        elif event == "STOP_APPROACHING":
            msg = f"STOP APPROACHING: {ticker} at ${price:,.2f}{ctx_line} — {reason}"
            dispatch_alert(db, user_id, STOP_APPROACHING, ticker, msg, skip_throttle=False)
            dispatched += 1

        elif event == "BREAKEVEN_REACHED":
            msg = f"BREAKEVEN: {ticker} stop moved to entry{ctx_line} — {reason}"
            dispatch_alert(db, user_id, BREAKEVEN_REACHED, ticker, msg, skip_throttle=True)
            dispatched += 1

        elif event == "STOP_TIGHTENED":
            old_s = alert.get("old_stop")
            new_s = alert.get("new_stop")
            msg = f"STOP TIGHTENED: {ticker} ${old_s:,.2f}->${new_s:,.2f}{ctx_line} — {reason}"
            dispatch_alert(db, user_id, STOP_TIGHTENED, ticker, msg, skip_throttle=True)
            dispatched += 1

    return dispatched
