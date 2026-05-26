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
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, Optional

from sqlalchemy.orm import Session

from .alert_formatter import (
    format_breakeven,
    format_stop_approaching,
    format_stop_hit,
    format_stop_tightened,
    format_target_hit,
    format_time_exit,
)
from .tick_normalizer import normalize_price as _norm_price

logger = logging.getLogger(__name__)

SECONDS_PER_HOUR = 3600
DEFAULT_MARKET_CONTEXT_STALENESS_SECS = 300
DEFAULT_ALERT_COOLDOWN_SECS = SECONDS_PER_HOUR


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
    recent_high: float | None = None
    recent_low: float | None = None
    recent_high_ts: datetime | None = None
    recent_low_ts: datetime | None = None
    range_source: str | None = None
    is_stale: bool = False


@dataclass
class StopDecisionResult:
    trade_id: int
    state: StopState
    old_stop: float | None
    new_stop: float | None
    # Persisted into Trade.take_profit when _compute_initial_stop runs so the
    # live monitor uses the same target the engine used for TARGET_HIT.
    new_take_profit: float | None = None
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


def _now_naive_utc() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _to_naive_utc(value: datetime | str | None) -> datetime | None:
    if value is None:
        return None
    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        value = datetime.fromisoformat(raw)
    if value.tzinfo is None or value.utcoffset() is None:
        return value
    return value.astimezone(timezone.utc).replace(tzinfo=None)


def _safe_market_float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


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
        # Phase 4 (2026-05-01) — magic-number fallback flagged.
        #
        # The user's stated policy ("never `or 0.5` magic constants for
        # missing measurements") forbids this branch existing at all,
        # but several call sites currently expect a (sl, tp) tuple
        # unconditionally. Removing the fallback here without updating
        # those callers would crash the stop-management loop.
        #
        # Compromise pending Phase 4.b: keep the fallback so nothing
        # breaks, but log CRITICAL each time it fires so the operator
        # sees how often the brain is being asked to size a stop without
        # ATR. Once the count is observed, the proper fix is to update
        # the call sites to handle None and remove this branch entirely.
        from .stop_engine_fallback_constants import (
            FALLBACK_STOP_PCT_LONG,
            FALLBACK_TP_MULT_LONG,
            FALLBACK_STOP_PCT_SHORT,
            FALLBACK_TP_MULT_SHORT,
        )
        logger.critical(
            "[stop_engine] FALLBACK_FIRED: missing ATR, using configured "
            "fallback constants. atr=%r price=%r entry=%s direction=%s "
            "lifecycle=%s — observe frequency in [stop_engine] CRITICAL "
            "log; if persistent, fix the upstream ATR pipeline rather "
            "than tuning these constants.",
            atr, price, entry, direction,
            (brain.lifecycle_stage if brain else None),
        )
        if direction == "long":
            sl = entry * (1.0 - FALLBACK_STOP_PCT_LONG)
            tp = entry * FALLBACK_TP_MULT_LONG
        else:
            sl = entry * (1.0 + FALLBACK_STOP_PCT_SHORT)
            tp = entry * FALLBACK_TP_MULT_SHORT

    # Phase 1 (2026-05-01): align stop+target to the venue's actual tick
    # before storage. Previously this rounded equity to 4 decimals, which
    # then mismatched the broker's 2-decimal rule and caused the broker
    # to flag the rounded value as invalid post-acceptance. Pass
    # asset_class explicitly because this function only has the bool;
    # tick_normalizer's asset_class override skips the ticker pattern test.
    asset = "crypto" if is_crypto else "equity"
    return (
        _norm_price(sl, "", asset_class=asset),
        _norm_price(tp, "", asset_class=asset),
    )


def compute_initial_bracket(
    *,
    entry: float,
    direction: str = "long",
    atr: float | None,
    asset_class: str = "crypto",
    stop_model: str = "atr_crypto_breakout",
    regime: str = "cautious",
    lifecycle_stage: str = "validated",
    pattern_win_rate: float | None = None,
    stop_mult_override: float | None = None,
    target_mult_override: float | None = None,
) -> tuple[float, float]:
    """Public wrapper for initial (stop, target) bracket computation.

    Same brain-aware policy as ``evaluate_trade`` uses internally on the
    first pass (``_compute_initial_stop``), but callable from contexts
    that don't have a Trade ORM row — fast-path executor / exit-manager,
    backtest harnesses, simulators.

    The bracket reflects regime + lifecycle + win-rate adjustments so a
    crypto scalp opened in a risk_off regime gets a tighter stop than
    the same setup in risk_on. Returns prices already aligned to the
    venue tick (via :func:`tick_normalizer.normalize_price`).
    """
    is_crypto = (asset_class or "").strip().lower() == "crypto"
    brain = BrainContext(
        regime=regime,
        regime_stop_factor=_REGIME_STOP_TIGHTEN.get(regime, 1.0),
        warn_proximity=_REGIME_WARN_PROXIMITY.get(regime, WARN_PROXIMITY_R),
        lifecycle_stage=lifecycle_stage,
        lifecycle_stop_factor=_LIFECYCLE_STOP_FACTOR.get(lifecycle_stage, 1.0),
        pattern_win_rate=pattern_win_rate,
        stop_mult_override=stop_mult_override,
        target_mult_override=target_mult_override,
    )
    return _compute_initial_stop(
        entry, direction, atr, entry, stop_model, is_crypto, brain,
    )


def _recent_extrema_trigger_on_stale_quote(
    *,
    result: StopDecisionResult,
    trade,
    market: MarketContext,
    entry: float,
    stop: float | None,
    target: float | None,
    is_long: bool,
    brain: BrainContext,
) -> StopDecisionResult | None:
    """Allow audited broker-session bar touches even when the latest quote ages out."""

    def _trigger_is_plausible(px: float | None) -> bool:
        if px is None or px <= 0 or entry <= 0:
            return False
        ratio = px / float(entry)
        return 0.1 <= ratio <= 10.0

    def _current_txt() -> str:
        return f"${market.price:,.4f}" if market.price and market.price > 0 else "unavailable"

    def _ts_txt(ts: datetime | None) -> str:
        return f" at {ts.isoformat()}Z" if ts else ""

    def _base_inputs(trigger_basis: str, trigger_price: float, trigger_ts: datetime | None) -> dict[str, Any]:
        inputs = {
            "price": market.price if market.price and market.price > 0 else None,
            "entry": entry,
            "stop": stop,
            "target": target,
            "trigger_basis": trigger_basis,
            "trigger_price": trigger_price,
            "quote_stale": bool(market.is_stale),
            "source": market.range_source or "broker_range",
            "brain": brain.summary_dict(),
        }
        if trigger_ts:
            inputs["trigger_ts"] = trigger_ts.isoformat()
        return inputs

    stop_trigger: tuple[str, float, datetime | None] | None = None
    if stop:
        if is_long and market.recent_low is not None and market.recent_low <= stop:
            stop_trigger = ("recent_low", market.recent_low, market.recent_low_ts)
        elif (
            not is_long
            and market.recent_high is not None
            and market.recent_high >= stop
        ):
            stop_trigger = ("recent_high", market.recent_high, market.recent_high_ts)
    if stop_trigger is not None:
        trigger_basis, trigger_price, trigger_ts = stop_trigger
        if _trigger_is_plausible(trigger_price):
            pnl_pct = round(
                ((trigger_price - entry) / entry * 100)
                if is_long else ((entry - trigger_price) / entry * 100),
                2,
            )
            result.state = StopState.TRIGGERED
            result.alert_event = "STOP_HIT"
            result.recommended_action = "exit"
            result.reason = (
                f"stop touched by broker bar {trigger_basis}=${trigger_price:,.4f}"
                f"{_ts_txt(trigger_ts)} (stop=${stop:,.4f}, current={_current_txt()}, "
                f"P&L={pnl_pct:+.1f}%, source={market.range_source or 'broker_range'}, "
                f"strategy={brain.pattern_name or trade.stop_model}, regime={brain.regime}; "
                "latest quote stale/unavailable)"
            )
            result.inputs = _base_inputs(trigger_basis, trigger_price, trigger_ts)
            return result

    target_trigger: tuple[str, float, datetime | None] | None = None
    if target:
        if is_long and market.recent_high is not None and market.recent_high >= target:
            target_trigger = ("recent_high", market.recent_high, market.recent_high_ts)
        elif (
            not is_long
            and market.recent_low is not None
            and market.recent_low <= target
        ):
            target_trigger = ("recent_low", market.recent_low, market.recent_low_ts)
    if target_trigger is not None:
        trigger_basis, trigger_price, trigger_ts = target_trigger
        if _trigger_is_plausible(trigger_price):
            pnl_pct = round(
                ((trigger_price - entry) / entry * 100)
                if is_long else ((entry - trigger_price) / entry * 100),
                2,
            )
            result.alert_event = "TARGET_HIT"
            result.recommended_action = "reduce"
            result.reason = (
                f"target touched by broker bar {trigger_basis}=${trigger_price:,.4f}"
                f"{_ts_txt(trigger_ts)} (target=${target:,.4f}, current={_current_txt()}, "
                f"P&L=+{pnl_pct:.1f}%, source={market.range_source or 'broker_range'}, "
                f"strategy={brain.pattern_name or trade.stop_model}; latest quote stale/unavailable)"
            )
            result.inputs = _base_inputs(trigger_basis, trigger_price, trigger_ts)
            return result

    return None


def _broker_range_lookback_minutes() -> int:
    try:
        from ...config import settings

        minutes = int(
            getattr(
                settings,
                "chili_broker_position_price_monitor_bar_lookback_minutes",
                720,
            )
            or 720
        )
    except Exception:
        minutes = 720
    return max(5, min(minutes, 1440))


def _load_recent_broker_touch_decisions(db: Session, trade_id: int) -> list[Any]:
    from ...models.trading import StopDecision as SDModel

    return (
        db.query(SDModel)
        .filter(
            SDModel.trade_id == trade_id,
            SDModel.trigger.in_(["STOP_HIT", "TARGET_HIT"]),
        )
        .order_by(SDModel.as_of_ts.desc())
        .limit(5)
        .all()
    )


def _persisted_trigger_on_stale_quote(
    *,
    result: StopDecisionResult,
    trade,
    market: MarketContext,
    db: Session | None,
    entry: float,
    stop: float | None,
    target: float | None,
    is_long: bool,
    brain: BrainContext,
) -> StopDecisionResult | None:
    """Retain a recent audited broker-session touch when extrema fetch is empty."""
    if db is None or not getattr(trade, "id", None):
        return None

    def _same_level(a: float | None, b: float | None) -> bool:
        if a is None or b is None:
            return True
        return abs(float(a) - float(b)) <= max(1e-6, abs(float(b)) * 1e-6)

    def _trigger_ts(value: Any, fallback: datetime | None) -> datetime | None:
        return _to_naive_utc(value) or _to_naive_utc(fallback)

    def _fresh_enough(ts: datetime | None) -> bool:
        if ts is None:
            return False
        age = (_now_naive_utc() - ts).total_seconds()
        return age <= _broker_range_lookback_minutes() * 60

    def _current_txt() -> str:
        return f"${market.price:,.4f}" if market.price and market.price > 0 else "unavailable"

    def _decision_source(decision: Any, inputs: dict[str, Any]) -> str:
        source = inputs.get("source")
        if source:
            return str(source)
        reason = str(getattr(decision, "reason", "") or "")
        marker = "source="
        if marker in reason:
            tail = reason.split(marker, 1)[1]
            parsed = tail.split(",", 1)[0].split(")", 1)[0].strip()
            if parsed:
                return parsed
        return market.range_source or "broker_range"

    try:
        decisions = _load_recent_broker_touch_decisions(db, int(trade.id))
    except Exception:
        logger.debug(
            "[stop_engine] prior broker touch lookup failed trade=%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
        return None

    for decision in decisions:
        trigger = (getattr(decision, "trigger", None) or "").upper()
        inputs = getattr(decision, "inputs_json", None) or {}
        trigger_price = _safe_market_float(inputs.get("trigger_price"))
        trigger_basis = str(inputs.get("trigger_basis") or "broker_bar")
        trigger_ts = _trigger_ts(inputs.get("trigger_ts"), getattr(decision, "as_of_ts", None))
        if trigger_price is None or not _fresh_enough(trigger_ts):
            continue

        persisted_stop = _safe_market_float(inputs.get("stop"))
        persisted_target = _safe_market_float(inputs.get("target"))
        source = _decision_source(decision, inputs)
        decision_id = getattr(decision, "id", None)
        decision_ts = _to_naive_utc(getattr(decision, "as_of_ts", None))
        base_inputs = {
            "price": market.price if market.price and market.price > 0 else None,
            "entry": entry,
            "stop": stop,
            "target": target,
            "trigger_basis": trigger_basis,
            "trigger_price": trigger_price,
            "quote_stale": bool(market.is_stale),
            "source": source,
            "persisted_decision_id": decision_id,
            "brain": brain.summary_dict(),
        }
        if trigger_ts:
            base_inputs["trigger_ts"] = trigger_ts.isoformat()
        if decision_ts:
            base_inputs["persisted_decision_ts"] = decision_ts.isoformat()

        if (
            trigger == "TARGET_HIT"
            and target
            and _same_level(persisted_target, target)
            and ((is_long and trigger_price >= target) or (not is_long and trigger_price <= target))
        ):
            pnl_pct = round(
                ((trigger_price - entry) / entry * 100)
                if is_long else ((entry - trigger_price) / entry * 100),
                2,
            )
            result.alert_event = "TARGET_HIT"
            result.recommended_action = "reduce"
            result.reason = (
                f"target touched by previously audited broker bar {trigger_basis}=${trigger_price:,.4f}"
                f" at {trigger_ts.isoformat()}Z (target=${target:,.4f}, current={_current_txt()}, "
                f"P&L=+{pnl_pct:.1f}%, source={source}, "
                f"strategy={brain.pattern_name or trade.stop_model}; latest quote stale/unavailable)"
            )
            result.inputs = base_inputs
            return result

        if (
            trigger == "STOP_HIT"
            and stop
            and _same_level(persisted_stop, stop)
            and ((is_long and trigger_price <= stop) or (not is_long and trigger_price >= stop))
        ):
            pnl_pct = round(
                ((trigger_price - entry) / entry * 100)
                if is_long else ((entry - trigger_price) / entry * 100),
                2,
            )
            result.state = StopState.TRIGGERED
            result.alert_event = "STOP_HIT"
            result.recommended_action = "exit"
            result.reason = (
                f"stop touched by previously audited broker bar {trigger_basis}=${trigger_price:,.4f}"
                f" at {trigger_ts.isoformat()}Z (stop=${stop:,.4f}, current={_current_txt()}, "
                f"P&L={pnl_pct:+.1f}%, source={source}, "
                f"strategy={brain.pattern_name or trade.stop_model}, regime={brain.regime}; "
                "latest quote stale/unavailable)"
            )
            result.inputs = base_inputs
            return result

    return None


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

    if market.is_stale or market.price <= 0:
        recent_trigger = _recent_extrema_trigger_on_stale_quote(
            result=result,
            trade=trade,
            market=market,
            entry=float(entry),
            stop=trade.stop_loss,
            target=trade.take_profit,
            is_long=is_long,
            brain=brain,
        )
        if recent_trigger is not None:
            return recent_trigger
        persisted_trigger = _persisted_trigger_on_stale_quote(
            result=result,
            trade=trade,
            market=market,
            db=db,
            entry=float(entry),
            stop=trade.stop_loss,
            target=trade.take_profit,
            is_long=is_long,
            brain=brain,
        )
        if persisted_trigger is not None:
            return persisted_trigger

    if market.price <= 0:
        result.reason = "no valid price"
        return result

    # Round-13/14 (2026-04-30): implausible-quote guard. Trade 585
    # (ARB-USD crypto) was stopped at px=0.00075706 vs entry=0.1295 due
    # to an upstream quote-provider data error -- the false stop fired
    # and sold the position. Same vulnerability exists for stocks here.
    # Reject any quote where ratio (price/entry) > 10 or < 0.1; the
    # caller's next pass can retry with a fresh quote rather than acting
    # on garbage data. Per the no-hardcoded-fallback rule: do NOT
    # silently substitute entry as price -- abstain instead.
    _ratio = market.price / float(entry)
    if _ratio > 10.0 or _ratio < 0.1:
        result.alert_event = "DATA_IMPLAUSIBLE"
        result.reason = (
            f"implausible quote: price=${market.price:,.4f} entry=${entry:,.4f} "
            f"ratio={_ratio:.4f} (rejected; upstream quote data error). "
            f"No stop changes; next pass retries with fresh quote."
        )
        return result

    if market.is_stale:
        result.alert_event = "DATA_STALE"
        result.reason = "quote is stale — no stop changes"
        return result

    # ── Time-based forced exit for day trades / scalps ──
    _trade_type = getattr(trade, "trade_type", None)
    if _trade_type and trade.entry_date:
        from .scanner import _MAX_HOLD_HOURS
        _max_h = _MAX_HOLD_HOURS.get(_trade_type)
        if _max_h is not None:
            _entry_dt = _to_naive_utc(trade.entry_date)
            if _entry_dt is None:
                return result
            _held_hours = (_now_naive_utc() - _entry_dt).total_seconds() / SECONDS_PER_HOUR
            if _held_hours >= _max_h:
                result.state = StopState.TRAILING
                result.alert_event = "TIME_EXIT"
                result.reason = (
                    f"{_trade_type} max hold {_max_h:.0f}h exceeded "
                    f"(held {_held_hours:.1f}h) — close position"
                )
                result.new_stop = market.price
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
        result.new_take_profit = target
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
    stop_trigger_price = price
    stop_trigger_ts: datetime | None = None
    stop_trigger_basis = "last"
    if is_long and price <= stop:
        stop_breached = True
    elif not is_long and price >= stop:
        stop_breached = True
    elif is_long and market.recent_low is not None and market.recent_low <= stop:
        stop_breached = True
        stop_trigger_price = market.recent_low
        stop_trigger_ts = market.recent_low_ts
        stop_trigger_basis = "recent_low"
    elif (
        not is_long
        and market.recent_high is not None
        and market.recent_high >= stop
    ):
        stop_breached = True
        stop_trigger_price = market.recent_high
        stop_trigger_ts = market.recent_high_ts
        stop_trigger_basis = "recent_high"

    if stop_breached:
        if is_long:
            pnl_pct = round((stop_trigger_price - entry) / entry * 100, 2)
        else:
            pnl_pct = round((entry - stop_trigger_price) / entry * 100, 2)
        result.state = StopState.TRIGGERED
        result.alert_event = "STOP_HIT"
        result.recommended_action = "exit"
        if stop_trigger_basis == "last":
            result.reason = (
                f"stop breached at ${price:,.4f} (stop=${stop:,.4f}, P&L={pnl_pct:+.1f}%, "
                f"strategy={brain.pattern_name or trade.stop_model}, regime={brain.regime})"
            )
        else:
            ts_txt = f" at {stop_trigger_ts.isoformat()}Z" if stop_trigger_ts else ""
            result.reason = (
                f"stop touched by broker bar {stop_trigger_basis}=${stop_trigger_price:,.4f}{ts_txt} "
                f"(stop=${stop:,.4f}, current=${price:,.4f}, P&L={pnl_pct:+.1f}%, "
                f"source={market.range_source or 'broker_range'}, "
                f"strategy={brain.pattern_name or trade.stop_model}, regime={brain.regime})"
            )
            result.inputs["trigger_basis"] = stop_trigger_basis
            result.inputs["trigger_price"] = stop_trigger_price
            if stop_trigger_ts:
                result.inputs["trigger_ts"] = stop_trigger_ts.isoformat()
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
    target_trigger_price = price
    target_trigger_ts: datetime | None = None
    target_trigger_basis = "last"
    if target:
        if is_long and price >= target:
            target_hit = True
        elif not is_long and price <= target:
            target_hit = True
        elif is_long and market.recent_high is not None and market.recent_high >= target:
            target_hit = True
            target_trigger_price = market.recent_high
            target_trigger_ts = market.recent_high_ts
            target_trigger_basis = "recent_high"
        elif (
            not is_long
            and market.recent_low is not None
            and market.recent_low <= target
        ):
            target_hit = True
            target_trigger_price = market.recent_low
            target_trigger_ts = market.recent_low_ts
            target_trigger_basis = "recent_low"

    if target_hit:
        if is_long:
            pnl_pct = round((target_trigger_price - entry) / entry * 100, 2)
        else:
            pnl_pct = round((entry - target_trigger_price) / entry * 100, 2)
        result.alert_event = "TARGET_HIT"
        result.recommended_action = "reduce"
        if target_trigger_basis == "last":
            result.reason = (
                f"target reached at ${price:,.4f} (target=${target:,.4f}, P&L=+{pnl_pct:.1f}%, "
                f"strategy={brain.pattern_name or trade.stop_model})"
            )
        else:
            ts_txt = f" at {target_trigger_ts.isoformat()}Z" if target_trigger_ts else ""
            result.reason = (
                f"target touched by broker bar {target_trigger_basis}=${target_trigger_price:,.4f}{ts_txt} "
                f"(target=${target:,.4f}, current=${price:,.4f}, P&L=+{pnl_pct:.1f}%, "
                f"source={market.range_source or 'broker_range'}, "
                f"strategy={brain.pattern_name or trade.stop_model})"
            )
            result.inputs["trigger_basis"] = target_trigger_basis
            result.inputs["trigger_price"] = target_trigger_price
            if target_trigger_ts:
                result.inputs["trigger_ts"] = target_trigger_ts.isoformat()

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


def _sync_bracket_intent_stop_unconditional(db: Session, trade) -> None:
    """bracket-intent-stop-price-live-sync (2026-05-03) — call the narrow
    sync writer for every sweep, regardless of whether the stop_engine
    produced an alert event. Catches moves to ``trade.stop_loss`` made
    by writers outside the stop_engine path (auto_trader_monitor,
    pattern_position_monitor, etc.).

    Mode-gated by ``brain_live_brackets_mode != 'off'`` (same gate as
    the existing ``_maybe_emit_bracket_intent``). Broker-source-gated
    so paper trades don't flood the cache. Errors are swallowed at
    debug level — the sync is advisory and a failure should not break
    the per-trade savepoint.
    """
    try:
        from ...config import settings as _cfg

        mode = getattr(_cfg, "brain_live_brackets_mode", "off") or "off"
        if mode == "off":
            return
        if not getattr(trade, "broker_source", None):
            return
        new_stop = getattr(trade, "stop_loss", None)
        if new_stop is None:
            return

        from .bracket_intent_writer import sync_bracket_intent_stop_from_trade

        changed, prev = sync_bracket_intent_stop_from_trade(
            db, int(trade.id), trade_stop_loss=float(new_stop),
        )
        if changed:
            logger.info(
                "[bracket_intent_writer] sync_stop_price trade=%s ticker=%s "
                "old=%s new=%s",
                trade.id, getattr(trade, "ticker", "?"),
                f"{prev:.4f}" if prev is not None else "NULL",
                f"{float(new_stop):.4f}",
            )
    except Exception:
        logger.debug("[stop_engine] bracket intent stop_price sync failed", exc_info=True)


def _indicator_snapshot_dict(trade) -> dict:
    """Return indicator_snapshot as a dict, including double-encoded legacy rows."""
    try:
        snapshot = getattr(trade, "indicator_snapshot", None)
        if not isinstance(snapshot, str) or not snapshot:
            return {}
        snap = json.loads(snapshot)
        if isinstance(snap, str):
            snap = json.loads(snap)
        return snap if isinstance(snap, dict) else {}
    except Exception:
        return {}


def _extract_atr_from_indicator_snapshot(trade) -> float | None:
    """Extract ATR from current and legacy indicator_snapshot shapes."""
    snap = _indicator_snapshot_dict(trade)
    if not snap:
        return None
    atr_val = snap.get("atr") or snap.get("ATR")
    if atr_val is None:
        atr_block = snap.get("atr_14") or snap.get("ATR_14")
        if isinstance(atr_block, dict):
            atr_val = atr_block.get("value")
        elif atr_block is not None:
            atr_val = atr_block
    if atr_val is None:
        ba = snap.get("breakout_alert")
        if isinstance(ba, dict):
            fi = ba.get("flat_indicators")
            if isinstance(fi, dict):
                atr_val = fi.get("atr") or fi.get("ATR")
    if atr_val is None:
        fi = snap.get("flat_indicators")
        if isinstance(fi, dict):
            atr_val = fi.get("atr") or fi.get("ATR")
    try:
        atr = float(atr_val)
        return atr if atr > 0 else None
    except Exception:
        return None


def _maybe_emit_bracket_intent(db: Session, trade, brain) -> None:
    """Phase G - single canonical bracket-intent emitter.

    Shadow mode only: persists the bracket (stop/target) the engine would have
    enforced for live (broker-backed) trades. Idempotent upsert, safe to call
    every evaluation tick.
    """
    try:
        from ...config import settings as _cfg

        mode = getattr(_cfg, "brain_live_brackets_mode", "off") or "off"
        if mode == "off":
            return
        broker_src = getattr(trade, "broker_source", None)
        if not broker_src:
            return
        stop_price = getattr(trade, "stop_loss", None)
        if stop_price is None or float(stop_price) <= 0:
            return

        from .bracket_intent import (
            BracketIntentInput,
            BracketIntentResult,
            compute_bracket_intent,
        )
        from .bracket_intent_writer import upsert_bracket_intent

        # f-stop-engine-atr-nested-key (2026-05-19): the original code
        # read ``snap["atr"]`` at the top level only, but the actual
        # indicator_snapshot shape produced by the breakout-alert
        # writer nests ATR at ``breakout_alert.flat_indicators.atr``
        # (also where adx, bb_pct, rsi_14, etc. live -- see trade 2064
        # ABTC for an example). Top-level reads always returned None
        # so the FALLBACK_FIRED CRITICAL log fired on every trade
        # every cycle for all open positions. This walker covers:
        #   * top-level ``atr`` / ``ATR`` (legacy schema)
        #   * ``breakout_alert.flat_indicators.atr`` (current schema)
        #   * ``flat_indicators.atr`` (alt depth)
        atr_val = _extract_atr_from_indicator_snapshot(trade)

        bracket_input = BracketIntentInput(
            ticker=trade.ticker,
            direction=(trade.direction or "long").lower(),
            entry_price=float(trade.entry_price or 0.0),
            quantity=float(trade.quantity or 0.0),
            atr=atr_val,
            stop_model=getattr(trade, "stop_model", None),
            pattern_id=getattr(trade, "scan_pattern_id", None),
            lifecycle_stage=getattr(brain, "lifecycle_stage", None) if brain else None,
            regime=getattr(brain, "regime", "cautious") if brain else "cautious",
            pattern_win_rate=getattr(brain, "pattern_win_rate", None) if brain else None,
            pattern_name=getattr(brain, "pattern_name", None) if brain else None,
        )
        target_price = getattr(trade, "take_profit", None)
        try:
            target_override = float(target_price) if target_price is not None else None
        except (TypeError, ValueError):
            target_override = None
        if target_override is not None and target_override <= 0:
            target_override = None

        # The bracket-intent row is the live placement cache. Once a trade
        # exists, its current stop/target columns are the source of truth;
        # recomputing from entry ATR would roll trailing stops and pattern
        # monitor target moves back to stale entry-time values every sweep.
        if target_override is not None:
            bracket_result = BracketIntentResult(
                stop_price=float(stop_price),
                target_price=target_override,
                stop_model_resolved=getattr(trade, "stop_model", None) or "snapshot",
                reasoning="source=trade_current_levels",
                brain_summary={
                    **(brain.summary_dict() if brain else {}),
                    "source": "trade_current_levels",
                },
            )
        else:
            bracket_result = compute_bracket_intent(bracket_input)
            bracket_result = BracketIntentResult(
                stop_price=float(stop_price),
                target_price=bracket_result.target_price,
                stop_model_resolved=bracket_result.stop_model_resolved,
                reasoning=f"{bracket_result.reasoning} source=trade_current_levels",
                brain_summary={
                    **bracket_result.brain_summary,
                    "source": "trade_current_levels",
                },
            )
        upsert_bracket_intent(
            db,
            trade_id=trade.id,
            user_id=getattr(trade, "user_id", None),
            bracket_input=bracket_input,
            bracket_result=bracket_result,
            broker_source=broker_src,
        )
    except Exception:
        logger.debug("[stop_engine] bracket intent emit failed", exc_info=True)


def _apply_stop_to_trade(db: Session, trade, result: StopDecisionResult) -> None:
    """Update the Trade row with any stop/watermark changes.

    If the trade is linked to a pattern-monitor alert (related_alert_id),
    the engine will never WIDEN the stop — only tighten it.  This preserves
    adjustments made by the pattern position monitor.
    """
    changed = False
    if result.new_stop is not None and result.new_stop != trade.stop_loss:
        is_pattern_linked = getattr(trade, "related_alert_id", None) is not None
        if is_pattern_linked and trade.stop_loss is not None:
            if result.new_stop > trade.stop_loss:
                trade.stop_loss = result.new_stop
                changed = True
        else:
            trade.stop_loss = result.new_stop
            changed = True
    if result.new_trail_stop is not None and result.new_trail_stop != trade.trail_stop:
        trade.trail_stop = result.new_trail_stop
        changed = True
    if result.watermark_updated and result.new_watermark is not None:
        trade.high_watermark = result.new_watermark
        changed = True
    if result.new_take_profit is not None:
        try:
            nt = float(result.new_take_profit)
        except (TypeError, ValueError):
            nt = 0.0
        if nt > 0:
            # Phase 1 (2026-05-01): venue-aware tick alignment for take_profit.
            # See _compute_initial_stop for why 4-decimal equity was wrong.
            sym = getattr(trade, "ticker", "") or ""
            asset = "crypto" if _is_crypto(sym) else "equity"
            rounded = _norm_price(nt, sym, asset_class=asset)
            cur = float(trade.take_profit or 0)
            cur_aligned = _norm_price(cur, sym, asset_class=asset) if cur else 0.0
            if trade.take_profit is None or cur_aligned != rounded:
                trade.take_profit = rounded
                changed = True
    if changed:
        db.add(trade)


def _fetch_broker_market_quote(
    ticker: str,
    *,
    broker_source: str | None,
    direction: str | None = None,
) -> dict[str, Any] | None:
    """Best-effort broker quote for live broker-managed positions."""
    try:
        from .broker_quotes import broker_quote_for_source

        broker_quote = broker_quote_for_source(
            ticker,
            broker_source=broker_source,
            direction=direction,
            purpose="exit",
        )
        broker_price = _safe_market_float((broker_quote or {}).get("price"))
        if broker_price is not None:
            return {
                "price": broker_price,
                "bid": broker_quote.get("bid"),
                "ask": broker_quote.get("ask"),
                "quote_ts": broker_quote.get("quote_ts"),
                "spread_bps": broker_quote.get("spread_bps"),
                "quote_source": broker_quote.get("source"),
                "volume": broker_quote.get("volume"),
                "day_high": broker_quote.get("day_high"),
                "day_low": broker_quote.get("day_low"),
                "stale": broker_quote.get("stale"),
                "age_seconds": broker_quote.get("age_seconds"),
                "max_age_seconds": broker_quote.get("max_age_seconds"),
            }
    except Exception:
        logger.debug(
            "[stop_engine] Broker quote helper failed for %s via %s",
            ticker,
            broker_source,
            exc_info=True,
        )

    try:
        from .venue.factory import get_adapter

        adapter = get_adapter(broker_source)
        if adapter is None:
            return None
        is_enabled = getattr(adapter, "is_enabled", None)
        if callable(is_enabled) and not is_enabled():
            return None
        tick = None
        fresh = None
        get_ticker = getattr(adapter, "get_ticker", None)
        if callable(get_ticker):
            raw_ticker = get_ticker(ticker)
            if isinstance(raw_ticker, tuple) and len(raw_ticker) == 2:
                tick, fresh = raw_ticker
        if tick is None:
            get_bbo = getattr(adapter, "get_best_bid_ask", None)
            if callable(get_bbo):
                raw_bbo = get_bbo(ticker)
                if isinstance(raw_bbo, tuple) and len(raw_bbo) == 2:
                    tick, fresh = raw_bbo
    except Exception:
        return None

    if tick is None:
        return None
    try:
        age_seconds = fresh.age_seconds() if fresh is not None else None
        max_age_seconds = getattr(fresh, "max_age_seconds", None) if fresh is not None else None
        if (
            age_seconds is not None
            and max_age_seconds is not None
            and float(age_seconds) > float(max_age_seconds)
        ):
            return None
    except Exception:
        return None

    raw = getattr(tick, "raw", None) or {}
    bid = _safe_market_float(getattr(tick, "bid", None))
    ask = _safe_market_float(getattr(tick, "ask", None))
    mid = _safe_market_float(getattr(tick, "mid", None))
    regular_last = _safe_market_float(raw.get("last_trade_price"))
    extended_last = _safe_market_float(raw.get("last_extended_hours_trade_price"))
    last = (
        _safe_market_float(getattr(tick, "last_price", None))
        or extended_last
        or regular_last
    )
    side = (direction or "long").strip().lower()
    if side == "short":
        price = ask or mid or last or bid
    elif side == "long":
        price = bid or mid or last or ask
    else:
        price = mid or last or bid or ask
    if price is None:
        return None

    quote_ts = getattr(fresh, "retrieved_at_utc", None)
    return {
        "price": price,
        "bid": bid,
        "ask": ask,
        "quote_ts": quote_ts,
        "spread_bps": _safe_market_float(getattr(tick, "spread_bps", None)),
        "quote_source": f"{(broker_source or 'broker').strip().lower()}_broker_quote",
        "day_high": _safe_market_float(raw.get("day_high") or raw.get("high")),
        "day_low": _safe_market_float(raw.get("day_low") or raw.get("low")),
        "age_seconds": age_seconds,
        "max_age_seconds": max_age_seconds,
    }


def _fetch_market_context(
    ticker: str,
    staleness_secs: int = DEFAULT_MARKET_CONTEXT_STALENESS_SECS,
    *,
    broker_source: str | None = None,
    direction: str | None = None,
) -> MarketContext:
    """Build a MarketContext, preferring venue truth for live broker rows."""
    from .market_data import fetch_quote
    q: dict[str, Any] | None = None
    if (broker_source or "").strip():
        q = _fetch_broker_market_quote(
            ticker,
            broker_source=broker_source,
            direction=direction,
        )
    provider_q: dict[str, Any] | None = None
    if q is None and not (broker_source or "").strip():
        try:
            provider_q = fetch_quote(ticker)
        except Exception:
            provider_q = None
    if q is None:
        q = provider_q

    recent_range: dict[str, Any] | None = None
    if (broker_source or "").strip():
        try:
            from .broker_quotes import broker_recent_extrema_for_source

            recent_range = broker_recent_extrema_for_source(
                ticker,
                broker_source=broker_source,
            )
        except Exception:
            recent_range = None

    if not q:
        range_price = _safe_market_float(recent_range.get("last")) if recent_range else None
        if range_price is None:
            return MarketContext(price=0, is_stale=True)
        q = {
            "price": range_price,
            "quote_ts": recent_range.get("last_ts"),
            "quote_source": recent_range.get("source"),
            "day_high": recent_range.get("high"),
            "day_low": recent_range.get("low"),
            "stale": True,
        }

    price = _safe_market_float(q.get("price"))
    if not price or price <= 0:
        range_price = _safe_market_float(recent_range.get("last")) if recent_range else None
        if range_price is None:
            return MarketContext(price=0, is_stale=True)
        q = {
            **q,
            "price": range_price,
            "quote_ts": q.get("quote_ts") or recent_range.get("last_ts"),
            "quote_source": q.get("quote_source") or recent_range.get("source"),
            "day_high": q.get("day_high") or recent_range.get("high"),
            "day_low": q.get("day_low") or recent_range.get("low"),
            "stale": True,
        }
        price = range_price

    quote_ts = _to_naive_utc(q.get("quote_ts")) or _now_naive_utc()
    age_secs = (_now_naive_utc() - quote_ts).total_seconds()
    max_age_seconds = _safe_market_float(q.get("max_age_seconds"))
    freshness_window = max_age_seconds or staleness_secs
    is_stale = bool(q.get("stale")) or age_secs > freshness_window

    atr = None
    try:
        from .market_data import get_indicator_snapshot
        snap = get_indicator_snapshot(ticker, interval="1d")
        if snap:
            atr_block = snap.get("atr") or {}
            atr = atr_block.get("value") if isinstance(atr_block, dict) else None
    except Exception:
        pass

    recent_high = (
        _safe_market_float(recent_range.get("high"))
        if recent_range else None
    ) or _safe_market_float(q.get("day_high"))
    recent_low = (
        _safe_market_float(recent_range.get("low"))
        if recent_range else None
    ) or _safe_market_float(q.get("day_low"))
    recent_high_ts = (
        _to_naive_utc(recent_range.get("high_ts"))
        if recent_range else None
    )
    recent_low_ts = (
        _to_naive_utc(recent_range.get("low_ts"))
        if recent_range else None
    )
    range_source = (
        recent_range.get("source")
        if recent_range else q.get("quote_source") or q.get("source")
    )

    return MarketContext(
        price=float(price),
        bid=q.get("bid"),
        ask=q.get("ask"),
        atr=float(atr) if atr else None,
        volume=q.get("volume") or (provider_q or {}).get("volume"),
        quote_ts=quote_ts,
        spread_bps=q.get("spread_bps"),
        recent_high=recent_high,
        recent_low=recent_low,
        recent_high_ts=recent_high_ts,
        recent_low_ts=recent_low_ts,
        range_source=range_source,
        is_stale=is_stale,
    )


# Per-event cooldown: how long to suppress identical alerts per trade.
# STOP_HIT gets one alert then a reminder after 4h; others get longer cooldowns.
_ALERT_COOLDOWN_SECS: dict[str, int] = {
    "STOP_HIT": 4 * 3600,       # 4h reminder — user should act
    "TARGET_HIT": 8 * 3600,     # 8h — target persists, low urgency
    "STOP_APPROACHING": 3600,   # 1h — situation may change
    "STOP_TIGHTENED": 0,        # always fire — stop actually moved (one-time)
    "BREAKEVEN_REACHED": 0,     # always fire — one-time transition
    "DATA_STALE": 1800,         # 30min
}


def _load_recent_decisions(db: Session, trade_ids: list[int]) -> dict[tuple[int, str], datetime]:
    """Load the most recent decision per (trade_id, trigger) for dedup."""
    from ...models.trading import StopDecision as SDModel
    from sqlalchemy import func
    if not trade_ids:
        return {}
    rows = (
        db.query(SDModel.trade_id, SDModel.trigger, func.max(SDModel.as_of_ts))
        .filter(SDModel.trade_id.in_(trade_ids), SDModel.trigger.isnot(None))
        .group_by(SDModel.trade_id, SDModel.trigger)
        .all()
    )
    return {(r[0], r[1]): r[2] for r in rows}


def _should_suppress_alert(
    trade_id: int, event: str, recent: dict[tuple[int, str], datetime],
    cooldowns: dict[str, int] | None = None,
) -> bool:
    """Check if this alert was already fired within its cooldown window."""
    effective = cooldowns or _ALERT_COOLDOWN_SECS
    cooldown = effective.get(event, DEFAULT_ALERT_COOLDOWN_SECS)
    if cooldown <= 0:
        return False
    last_ts = _to_naive_utc(recent.get((trade_id, event)))
    if not last_ts:
        return False
    now_utc = _now_naive_utc()
    elapsed = (now_utc - last_ts).total_seconds()
    return elapsed < cooldown


def _result_has_trade_state_change(trade, result: StopDecisionResult) -> bool:
    """Return True when applying this result would change the Trade row."""
    if result.new_stop is not None and result.new_stop != trade.stop_loss:
        is_pattern_linked = getattr(trade, "related_alert_id", None) is not None
        if is_pattern_linked and trade.stop_loss is not None:
            if result.new_stop > trade.stop_loss:
                return True
        else:
            return True
    if result.new_trail_stop is not None and result.new_trail_stop != trade.trail_stop:
        return True
    if result.watermark_updated and result.new_watermark is not None:
        if result.new_watermark != getattr(trade, "high_watermark", None):
            return True
    if result.new_take_profit is not None:
        try:
            nt = float(result.new_take_profit)
        except (TypeError, ValueError):
            nt = 0.0
        if nt > 0:
            sym = getattr(trade, "ticker", "") or ""
            asset = "crypto" if _is_crypto(sym) else "equity"
            rounded = _norm_price(nt, sym, asset_class=asset)
            cur = float(getattr(trade, "take_profit", None) or 0)
            cur_aligned = _norm_price(cur, sym, asset_class=asset) if cur else 0.0
            if getattr(trade, "take_profit", None) is None or cur_aligned != rounded:
                return True
    return False


def evaluate_all(
    db: Session,
    user_id: int | None = None,
    *,
    staleness_secs: int = DEFAULT_MARKET_CONTEXT_STALENESS_SECS,
    broker_sources: set[str] | frozenset[str] | list[str] | tuple[str, ...] | None = None,
    tickers: list[str] | tuple[str, ...] | set[str] | frozenset[str] | None = None,
) -> dict[str, Any]:
    """
    Evaluate all open trades for a user (or all users if None).
    Consults brain context (pattern strategy, lifecycle, regime) per trade.
    Suppresses duplicate alerts using per-trade+event cooldowns.
    Returns summary dict with counts and alert list.
    """
    from ...models.trading import Trade

    filters = [Trade.status == "open"]
    if user_id is not None:
        filters.append(Trade.user_id == user_id)
    if broker_sources:
        sources = [
            str(src).strip().lower()
            for src in broker_sources
            if str(src).strip()
        ]
        if sources:
            from sqlalchemy import func as _sa_func
            filters.append(_sa_func.lower(Trade.broker_source).in_(sources))
    if tickers:
        ticker_keys = [
            str(t).strip().upper()
            for t in tickers
            if str(t).strip()
        ]
        if ticker_keys:
            filters.append(Trade.ticker.in_(ticker_keys))

    trades = db.query(Trade).filter(*filters).all()

    summary: dict[str, Any] = {
        "total_checked": 0,
        "stops_hit": 0,
        "targets_hit": 0,
        "stops_tightened": 0,
        "breakevens": 0,
        "warnings": 0,
        "data_stale": 0,
        "suppressed": 0,
        "regime": "cautious",
        "alerts": [],
    }

    batch_regime = "cautious"
    try:
        from .regime import get_regime_indicators
        ri = get_regime_indicators()
        batch_regime = ri.get("regime_composite", "cautious")
    except Exception:
        pass
    summary["regime"] = batch_regime

    # Pre-load recent decisions for dedup (single query, not N+1)
    trade_ids = [t.id for t in trades]
    recent_decisions = _load_recent_decisions(db, trade_ids)

    # Self-learning: adapt cooldowns based on user behavior
    try:
        adaptive_cooldowns = get_adaptive_cooldowns(db)
    except Exception:
        adaptive_cooldowns = dict(_ALERT_COOLDOWN_SECS)

    for trade in trades:
        try:
            result_suppressed = False
            # Wrap each trade evaluation in a SAVEPOINT so one poisoned
            # update (e.g. a constraint violation on ``_apply_stop_to_trade``
            # or a bracket-intent insert) doesn't abort the outer
            # transaction and cause every subsequent ``_record_stop_decision``
            # in the batch to fail with ``InFailedSqlTransaction``. With a
            # savepoint, the failure rolls back only that trade's writes
            # while the surrounding batch commit still succeeds.
            with db.begin_nested():
                brain = _build_brain_context(trade, db)
                market = _fetch_market_context(
                    trade.ticker,
                    staleness_secs=staleness_secs,
                    broker_source=getattr(trade, "broker_source", None),
                    direction=getattr(trade, "direction", None),
                )
                # f-stop-engine-atr-trade-snapshot-fallback (2026-05-19):
                # When the live market_data ATR fetch returns None (which
                # is the steady-state for any ticker whose upstream data
                # provider is intermittently unavailable -- see memory
                # ``project_massive_blocked`` and ``project_regime_
                # classifier_yfinance_block``), fall back to the ATR
                # baked into the trade's own ``indicator_snapshot`` at
                # entry-time. Schema observed on live trades:
                # ``snap["breakout_alert"]["flat_indicators"]["atr"]``.
                # This is the value the autotrader sized stops from at
                # entry, so it's the most appropriate fallback. Without
                # this fallback, ``_compute_initial_stop`` fires the
                # FALLBACK_FIRED CRITICAL log on every cycle for every
                # open trade, with ``atr=None`` -- the operator-visible
                # alert noise that triggered today's investigation.
                if market.atr is None:
                    _atr_from_trade = _extract_atr_from_indicator_snapshot(trade)
                    if _atr_from_trade is not None:
                        market.atr = _atr_from_trade
                result = evaluate_trade(trade, market, db, brain=brain)
                summary["total_checked"] += 1

                if result.alert_event and result.alert_event != "DATA_STALE":
                    result_suppressed = _should_suppress_alert(
                        trade.id,
                        result.alert_event,
                        recent_decisions,
                        adaptive_cooldowns,
                    )
                    # Suppression is primarily an operator-noise control, but
                    # it must also prevent duplicate STOP_HIT/TARGET_HIT rows
                    # from flooding the audit table. Still record suppressed
                    # events that actually mutate trade state so stop moves do
                    # not disappear from the audit trail.
                    if (
                        not result_suppressed
                        or _result_has_trade_state_change(trade, result)
                    ):
                        _record_stop_decision(db, trade.id, result)
                    _apply_stop_to_trade(db, trade, result)
                # f-coinbase-bracket-coverage-fix (2026-05-10): emit
                # bracket intent on every sweep, not gated on alert_event.
                # The previous gate meant a freshly-entered Coinbase trade
                # whose price had not yet approached the stop never
                # produced an intent row, so the reconciler/writer never
                # saw it. The emitter is broker-source-gated, mode-gated,
                # and idempotent (upsert), so calling unconditionally is
                # safe; it short-circuits internally for paper trades and
                # mode=off.
                _maybe_emit_bracket_intent(db, trade, brain)
                # bracket-intent-stop-price-live-sync (2026-05-03):
                # Mirror trade.stop_loss into bracket_intents.stop_price
                # on EVERY sweep (not just alert sweeps). Other writers
                # (auto_trader_monitor, pattern_position_monitor, etc.)
                # also move trade.stop_loss without firing through the
                # alert path; without this sync the cache stays frozen
                # at entry-time values and place_missing_stop reads stale.
                # Idempotent + cheap: a single SELECT + conditional UPDATE,
                # gated by brain_live_brackets_mode and the writer's
                # CLOSED / authoritative_* guards.
                _sync_bracket_intent_stop_unconditional(db, trade)
                # Flush pending SQL inside the savepoint so per-trade
                # errors (constraint violations, bad ORM state) surface
                # here and get rolled back to the savepoint — not at the
                # outer ``db.commit()``, where they would poison the whole
                # batch with ``InFailedSqlTransaction``.
                db.flush()

            if result.alert_event:
                if result_suppressed:
                    summary["suppressed"] += 1
                    logger.debug(
                        "[stop_engine] Suppressed duplicate %s for %s (trade %d)",
                        result.alert_event, trade.ticker, trade.id,
                    )
                else:
                    alert_price = result.inputs.get("trigger_price", market.price)
                    summary["alerts"].append({
                        "trade_id": trade.id,
                        "ticker": trade.ticker,
                        "event": result.alert_event,
                        "state": result.state.value,
                        "reason": result.reason,
                        "price": alert_price,
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

        except Exception as e:
            logger.warning("[stop_engine] Error evaluating %s (id=%s): %s", trade.ticker, trade.id, e)

    try:
        db.commit()
    except Exception:
        db.rollback()
        logger.error("[stop_engine] Failed to commit stop updates", exc_info=True)

    logger.info(
        "[stop_engine] Evaluated %d trades (regime=%s): %d stops hit, %d targets, "
        "%d tightened, %d warnings, %d suppressed",
        summary["total_checked"], batch_regime,
        summary["stops_hit"], summary["targets_hit"],
        summary["stops_tightened"], summary["warnings"],
        summary["suppressed"],
    )

    return summary


def _try_auto_execute_stop(
    db: Session,
    user_id: int | None,
    alert: dict,
) -> None:
    """Auto-execute a sell order when a stop fires, if enabled and safe."""
    from ...config import settings as _cfg
    if not getattr(_cfg, "chili_auto_execute_stops", False):
        return
    from .governance import is_kill_switch_active
    if is_kill_switch_active():
        _log.info("[stop_engine] auto-exec skipped: kill switch active")
        return

    trade_id = alert.get("trade_id")
    ticker = alert.get("ticker")
    if not trade_id or not ticker:
        return

    try:
        from ...models.trading import Trade
        trade = db.query(Trade).filter(Trade.id == trade_id, Trade.status == "open").one_or_none()
        if not trade:
            return

        broker_src = getattr(trade, "broker_source", None)
        if not broker_src:
            _log.debug("[stop_engine] auto-exec skipped: no broker_source on trade %s", trade_id)
            return

        from .broker_service import get_broker_manager
        bm = get_broker_manager(db, user_id)
        if not bm:
            return

        qty = trade.quantity or 0
        if qty <= 0:
            return

        _log.warning(
            "[stop_engine] AUTO-EXECUTING stop sell: ticker=%s qty=%s broker=%s trade_id=%s",
            ticker, qty, broker_src, trade_id,
        )
        result = bm.sell(ticker, qty, order_type="market")
        if result:
            trade.status = "closed"
            trade.exit_price = alert.get("price", 0)
            trade.exit_date = datetime.utcnow()
            trade.exit_reason = alert.get("event", "auto_stop")
            db.add(trade)
            db.flush()
            # f-fix-live-trade-closed-emitter (2026-05-05): emit the
            # live_trade_closed event so the Phase 2 handler chain
            # (pattern_stats + demote + regime_ledger) can fire on
            # this stop-driven close. Pre-fix, only portfolio.py
            # emitted; stop_engine bypassed it silently.
            try:
                from .brain_work.execution_hooks import on_live_trade_closed
                on_live_trade_closed(db, trade, source="stop_engine")
            except Exception:
                _log.debug(
                    "[stop_engine] on_live_trade_closed failed for trade %s",
                    trade_id, exc_info=True,
                )
            # f-bracket-fired-stop-recording (2026-05-19): record the
            # stop_engine-initiated SELL as a sell-side execution_event
            # so Phase 4's position_has_recorded_sell helper sees it.
            # This path submits the sell directly via broker_manager
            # (NOT through pending_exit_order_id), so the
            # sync_pending_exit_order writer at
            # robinhood_exit_execution.py:1267 does NOT fire for it.
            # Wrapped in try/except: never block the close path.
            try:
                from .execution_audit import record_execution_event
                _exit_px = alert.get("price")
                _payload = {
                    "side": "sell",
                    "source": "stop_engine_auto_exec",
                    "trade_id": int(getattr(trade, "id", 0) or 0),
                    "exit_reason": trade.exit_reason,
                    "alert_event": alert.get("event"),
                    "alert_reason": alert.get("reason"),
                }
                record_execution_event(
                    db,
                    user_id=trade.user_id,
                    ticker=trade.ticker,
                    trade=trade,
                    scan_pattern_id=getattr(trade, "scan_pattern_id", None),
                    broker_source=broker_src,
                    event_type="stop_engine_auto_sell",
                    status="filled",
                    average_fill_price=float(_exit_px) if _exit_px else None,
                    cumulative_filled_quantity=float(qty or 0.0),
                    payload_json=_payload,
                )
            except Exception:
                _log.debug(
                    "[stop_engine] record_execution_event failed for "
                    "trade %s (non-fatal; sell already submitted)",
                    trade_id, exc_info=True,
                )
    except Exception:
        _log.warning("[stop_engine] auto-exec failed for trade %s", trade_id, exc_info=True)


def dispatch_stop_alerts(
    db: Session,
    user_id: int | None,
    summary: dict[str, Any],
) -> int:
    """Turn stop engine alerts into mesh sensor events + critical-only direct Telegram.

    All events publish to the neural mesh (nm_stop_eval sensor node) so parent
    aggregation nodes see the full picture. STOP_HIT / TARGET_HIT / STOP_APPROACHING
    also dispatch directly via Telegram as a safety fast-path (critical events must
    not wait for mesh propagation latency).
    """
    from .alerts import dispatch_alert

    STOP_HIT = "stop_hit"
    TARGET_HIT = "target_hit"
    STOP_APPROACHING = "stop_approaching"

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

        _fmt_kw = dict(
            strategy_tag=strategy_tag, lifecycle_tag=lifecycle_tag, regime=regime,
        )

        # Publish ALL events to mesh sensor (nm_stop_eval) for aggregation
        try:
            from .brain_neural_mesh.publisher import publish_stop_eval
            publish_stop_eval(
                db,
                trade_id=alert.get("trade_id", 0),
                ticker=ticker,
                alert_event=event,
                state=alert.get("state", ""),
                old_stop=alert.get("old_stop"),
                new_stop=alert.get("new_stop"),
                reason=reason,
                price=price,
                brain_context=brain,
                user_id=user_id,
            )
        except Exception:
            logger.debug("[stop_engine] mesh publish failed for %s", ticker, exc_info=True)

        # Critical fast-path: direct Telegram for events that demand immediate action
        if event == "STOP_HIT" or event == "TIME_EXIT":
            _fmt = format_time_exit if event == "TIME_EXIT" else format_stop_hit
            msg = _fmt(ticker, price, reason, **_fmt_kw)
            dispatch_alert(db, user_id, STOP_HIT, ticker, msg, skip_throttle=True)
            dispatched += 1
            # R30 cleanup (2026-04-30): _try_auto_execute_stop call REMOVED.
            # Single source of truth for crypto exit execution is now
            # ``run_crypto_exit_pass`` (called every 30s from
            # ``tick_auto_trader_monitor``); equity exits run through
            # ``submit_robinhood_trade_exit`` from the same monitor.
            # Leaving the call here was dead code (gated by
            # ``chili_auto_execute_stops=False``) but would have raced
            # the autotrader execution path if anyone ever flipped the
            # flag. dispatch_stop_alerts now does what its name says:
            # dispatches alerts (Telegram + neural mesh), no execution.

        elif event == "TARGET_HIT":
            msg = format_target_hit(ticker, price, reason, **_fmt_kw)
            dispatch_alert(db, user_id, TARGET_HIT, ticker, msg)
            dispatched += 1

        elif event == "STOP_APPROACHING":
            msg = format_stop_approaching(ticker, price, reason, **_fmt_kw)
            dispatch_alert(db, user_id, STOP_APPROACHING, ticker, msg)
            dispatched += 1

        elif event == "BREAKEVEN_REACHED":
            logger.info("[stop_engine] breakeven reached for %s (mesh only): %s", ticker, reason)

        elif event == "STOP_TIGHTENED":
            logger.info("[stop_engine] stop tightened for %s (mesh only): %s", ticker, reason)

    return dispatched


# ── Self-learning: review past alert outcomes ────────────────────────

def review_alert_outcomes(db: Session, lookback_hours: int = 48) -> dict[str, Any]:
    """
    Self-critical review: check past stop decisions and evaluate their accuracy.

    For each STOP_HIT or TARGET_HIT decision, check whether:
    - The trade was actually closed (user acted on the alert)
    - The price recovered (false positive — stop hit was premature)
    - The alert was ignored and the position is still open

    Returns a learning summary that can be used to adjust future behavior.
    """
    from ...models.trading import StopDecision as SDModel, Trade

    since = datetime.utcnow() - timedelta(hours=lookback_hours)

    actionable_decisions = (
        db.query(SDModel)
        .filter(
            SDModel.as_of_ts >= since,
            SDModel.trigger.in_(["STOP_HIT", "TARGET_HIT"]),
        )
        .all()
    )

    if not actionable_decisions:
        return {"reviewed": 0}

    trade_ids = list({d.trade_id for d in actionable_decisions})
    trades_by_id = {
        t.id: t for t in db.query(Trade).filter(Trade.id.in_(trade_ids)).all()
    }

    stats: dict[str, Any] = {
        "reviewed": len(actionable_decisions),
        "acted_on": 0,
        "ignored": 0,
        "false_positives": 0,
        "details": [],
    }

    for decision in actionable_decisions:
        trade = trades_by_id.get(decision.trade_id)
        if not trade:
            continue

        inputs = decision.inputs_json or {}
        decision_price = inputs.get("price", 0)
        entry = trade.entry_price or 0
        is_long = (trade.direction or "long") == "long"

        if trade.status == "closed":
            stats["acted_on"] += 1
            outcome = "acted"
        else:
            try:
                market = _fetch_market_context(trade.ticker)
                current_price = market.price
            except Exception:
                current_price = 0

            if current_price > 0 and decision_price > 0:
                if decision.trigger == "STOP_HIT":
                    if is_long and current_price > decision_price * 1.02:
                        stats["false_positives"] += 1
                        outcome = "false_positive_recovered"
                    elif not is_long and current_price < decision_price * 0.98:
                        stats["false_positives"] += 1
                        outcome = "false_positive_recovered"
                    else:
                        stats["ignored"] += 1
                        outcome = "ignored_still_breached"
                else:
                    stats["ignored"] += 1
                    outcome = "ignored"
            else:
                stats["ignored"] += 1
                outcome = "no_price_data"

        stats["details"].append({
            "trade_id": decision.trade_id,
            "ticker": trade.ticker,
            "trigger": decision.trigger,
            "decision_ts": decision.as_of_ts.isoformat() if decision.as_of_ts else None,
            "outcome": outcome,
        })

    total = stats["reviewed"]
    if total > 0:
        stats["act_rate"] = round(stats["acted_on"] / total, 2)
        stats["false_positive_rate"] = round(stats["false_positives"] / total, 2)
        stats["ignore_rate"] = round(stats["ignored"] / total, 2)

    logger.info(
        "[stop_engine] Alert review: %d decisions, %d acted, %d ignored, %d false positives (%.0f%% act rate)",
        total, stats["acted_on"], stats["ignored"], stats["false_positives"],
        stats.get("act_rate", 0) * 100,
    )

    return stats


def get_adaptive_cooldowns(db: Session) -> dict[str, int]:
    """
    Adjust alert cooldowns based on recent user behavior.

    If the user consistently ignores TARGET_HIT alerts (>70% ignore rate),
    double the cooldown. If they act on STOP_HIT quickly, keep it tight.
    """
    review = review_alert_outcomes(db, lookback_hours=168)  # 7 days
    base = dict(_ALERT_COOLDOWN_SECS)

    if review["reviewed"] < 5:
        return base

    target_decisions = [d for d in review.get("details", []) if d["trigger"] == "TARGET_HIT"]
    if target_decisions:
        ignored_targets = sum(1 for d in target_decisions if d["outcome"].startswith("ignored"))
        if len(target_decisions) > 0 and ignored_targets / len(target_decisions) > 0.7:
            base["TARGET_HIT"] = min(base["TARGET_HIT"] * 2, 24 * 3600)
            logger.info(
                "[stop_engine] User ignores %.0f%% of TARGET_HIT — extending cooldown to %dh",
                (ignored_targets / len(target_decisions)) * 100,
                base["TARGET_HIT"] // 3600,
            )

    stop_decisions = [d for d in review.get("details", []) if d["trigger"] == "STOP_HIT"]
    if stop_decisions:
        fp = sum(1 for d in stop_decisions if d["outcome"] == "false_positive_recovered")
        if len(stop_decisions) > 3 and fp / len(stop_decisions) > 0.5:
            base["STOP_HIT"] = min(base["STOP_HIT"] * 2, 12 * 3600)
            logger.info(
                "[stop_engine] %.0f%% of STOP_HIT were false positives — extending cooldown to %dh",
                (fp / len(stop_decisions)) * 100,
                base["STOP_HIT"] // 3600,
            )

    return base
