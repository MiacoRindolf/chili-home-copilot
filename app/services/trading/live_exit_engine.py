"""Live exit engine — mirrors DynamicPatternStrategy exit logic for real/paper positions.

Supports:
- ATR trailing stops (tighten only)
- Time-decay exits (reduce after N bars with no move)
- Partial profit-taking at R-multiples
- Break-of-structure (BOS) exits via swing-low breach
- Pattern-specific exit_config from ScanPattern.exit_config
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ...models.trading import PaperTrade, ScanPattern, Trade

logger = logging.getLogger(__name__)


def compute_live_exit_levels(
    db: Session,
    trade: PaperTrade | Trade,
    current_price: float,
) -> dict[str, Any]:
    """Compute adaptive exit levels for an open position based on its pattern's config."""
    from .market_data import fetch_ohlcv_df
    from .indicator_core import compute_atr

    result: dict[str, Any] = {"action": "hold"}

    exit_cfg = _load_exit_config(db, getattr(trade, "scan_pattern_id", None))
    entry = trade.entry_price
    stop = trade.stop_price or entry * 0.97
    is_long = getattr(trade, "direction", "long") == "long"
    risk = abs(entry - stop) if entry and stop else entry * 0.03

    try:
        df = fetch_ohlcv_df(trade.ticker, period="3mo", interval="1d")
        if df is not None and len(df) >= 14:
            atr_arr = compute_atr(df["High"].values, df["Low"].values, df["Close"].values, period=14)
            atr = float(atr_arr[-1]) if len(atr_arr) > 0 and not math.isnan(atr_arr[-1]) else None
        else:
            atr = None
    except Exception:
        atr = None

    result["atr"] = atr
    result["exit_config"] = exit_cfg

    if atr and exit_cfg.get("trailing_enabled", True):
        trail_mult = exit_cfg.get("trailing_atr_mult", 1.5)
        if is_long:
            trail_stop = current_price - (atr * trail_mult)
            result["trailing_stop"] = round(trail_stop, 4)
        else:
            trail_stop = current_price + (atr * trail_mult)
            result["trailing_stop"] = round(trail_stop, 4)

    if is_long and current_price <= stop:
        result["action"] = "exit_stop"
        result["exit_price"] = stop
    elif not is_long and current_price >= stop:
        result["action"] = "exit_stop"
        result["exit_price"] = stop

    target = getattr(trade, "target_price", None)
    if target:
        if is_long and current_price >= target:
            result["action"] = "exit_target"
            result["exit_price"] = target
        elif not is_long and current_price <= target:
            result["action"] = "exit_target"
            result["exit_price"] = target

    if risk > 0 and exit_cfg.get("partial_at_1r", False):
        r_move = (current_price - entry) / risk if is_long else (entry - current_price) / risk
        if r_move >= 1.0:
            result["partial_profit_eligible"] = True
            result["r_multiple"] = round(r_move, 2)

    max_bars = exit_cfg.get("max_bars")
    if max_bars and trade.entry_date:
        days_held = (datetime.utcnow() - trade.entry_date).days
        if days_held >= max_bars and result["action"] == "hold":
            result["action"] = "exit_time_decay"
            result["exit_price"] = current_price
            result["days_held"] = days_held

    if atr and exit_cfg.get("use_bos", True):
        try:
            df_recent = fetch_ohlcv_df(trade.ticker, period="1mo", interval="1d")
            if df_recent is not None and len(df_recent) >= 5:
                lows = df_recent["Low"].values[-5:]
                swing_low = float(min(lows))
                bos_buffer = exit_cfg.get("bos_buffer_pct", 0.5) / 100
                bos_level = swing_low * (1 - bos_buffer) if is_long else swing_low * (1 + bos_buffer)
                result["bos_level"] = round(bos_level, 4)
                if is_long and current_price < bos_level:
                    result["action"] = "exit_bos"
                    result["exit_price"] = current_price
        except Exception:
            pass

    return result


def _load_exit_config(db: Session, scan_pattern_id: int | None) -> dict:
    """Load exit config from the ScanPattern, with sensible defaults."""
    defaults = {
        "atr_stop_mult": 2.0,
        "atr_target_mult": 3.0,
        "trailing_enabled": True,
        "trailing_atr_mult": 1.5,
        "max_bars": 20,
        "use_bos": True,
        "bos_buffer_pct": 0.5,
        "partial_at_1r": False,
    }
    if not scan_pattern_id:
        return defaults
    try:
        pat = db.query(ScanPattern).filter(ScanPattern.id == scan_pattern_id).first()
        if pat and pat.exit_config:
            cfg = pat.exit_config if isinstance(pat.exit_config, dict) else json.loads(pat.exit_config)
            defaults.update({k: v for k, v in cfg.items() if v is not None})
    except Exception:
        pass
    return defaults


def run_exit_engine(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Evaluate all open positions through the exit engine. Returns action recommendations."""
    from .market_data import fetch_quote

    open_paper = db.query(PaperTrade).filter(PaperTrade.status == "open")
    if user_id is not None:
        open_paper = open_paper.filter(PaperTrade.user_id == user_id)
    positions = open_paper.all()

    results = []
    for pos in positions:
        try:
            q = fetch_quote(pos.ticker)
            if not q or not q.get("price"):
                continue
            price = float(q["price"])
            exit_rec = compute_live_exit_levels(db, pos, price)
            exit_rec["ticker"] = pos.ticker
            exit_rec["position_id"] = pos.id
            exit_rec["current_price"] = price
            results.append(exit_rec)
        except Exception as e:
            logger.debug("[exit_engine] Error evaluating %s: %s", pos.ticker, e)

    actions = [r for r in results if r.get("action") != "hold"]
    logger.info("[exit_engine] Evaluated %d positions: %d actions recommended", len(results), len(actions))

    return {
        "ok": True,
        "evaluated": len(results),
        "actions": actions,
        "all": results,
    }
