"""Paper trading simulation for promoted patterns (LEGACY system).

Auto-enters paper trades when a promoted pattern fires a signal,
auto-exits on stop/target/expiry, and tracks simulated P&L.

Supports ATR-based adaptive stops/targets, trailing stops, spread/slippage
modeling, and pattern-specific exit_config.

NOTE: This is the **legacy** paper trade system using ``PaperTrade`` rows
(table ``trading_paper_trades``).  The **momentum autopilot** system uses
``TradingAutomationSession`` rows instead (see ``momentum_neural/`` package).
The two systems are **independent**:

- Legacy: ``auto_enter_from_signals()`` + ``check_paper_exits()`` + scheduler
  ``paper_trade_check`` job.  Simpler, pattern-driven.
- Autopilot: ``paper_runner`` / ``live_runner`` FSMs with operator controls,
  decision ledger, viability pipeline, and venue adapters.

Both can run simultaneously.  Ensure you check the correct table when
inspecting P&L or open positions.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from types import SimpleNamespace
from typing import Any

from sqlalchemy.orm import Session

from ...config import settings
from ...models.trading import BreakoutAlert, PaperTrade, ScanPattern

logger = logging.getLogger(__name__)

DEFAULT_PAPER_CAPITAL = 100_000.0
MAX_OPEN_PAPER_TRADES = 20
PAPER_TRADE_EXPIRY_DAYS = 5
DEFAULT_SLIPPAGE_PCT = 0.05
DEFAULT_ATR_STOP_MULT = 2.0
DEFAULT_ATR_TARGET_MULT = 3.0
TRAILING_STOP_ACTIVATION_R = 1.0  # activate trailing after 1R move
OPTION_CONTRACT_MULTIPLIER = 100.0
PAPER_TRADE_CAPACITY_SCOPE_ALL = "all"
PAPER_TRADE_CAPACITY_SCOPE_AUTOTRADER_SHADOW = "autotrader_shadow"
PAPER_SHADOW_PRIORITY_UNKNOWN = 0
PAPER_SHADOW_PRIORITY_CANDIDATE = 10
PAPER_SHADOW_PRIORITY_CHALLENGED = 15
PAPER_SHADOW_PRIORITY_VALIDATED = 25
PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE = 30
PAPER_SHADOW_PRIORITY_SHADOW_PROMOTED = 45
PAPER_SHADOW_PRIORITY_PILOT_PROMOTED = 55
PAPER_SHADOW_PRIORITY_LIVE_READY = 65
PAPER_SHADOW_PRIORITY_RECERT = 75
PAPER_SHADOW_PRIORITY_NEAR_MISS_SIGNAL_LANE = 50
PAPER_SHADOW_STAGE_PRIORITY = {
    "candidate": PAPER_SHADOW_PRIORITY_CANDIDATE,
    "backtested": PAPER_SHADOW_PRIORITY_VALIDATED,
    "validated": PAPER_SHADOW_PRIORITY_VALIDATED,
    "challenged": PAPER_SHADOW_PRIORITY_CHALLENGED,
    "decayed": PAPER_SHADOW_PRIORITY_CHALLENGED,
    "shadow_promoted": PAPER_SHADOW_PRIORITY_SHADOW_PROMOTED,
    "pilot_promoted": PAPER_SHADOW_PRIORITY_PILOT_PROMOTED,
    "promoted": PAPER_SHADOW_PRIORITY_LIVE_READY,
    "live": PAPER_SHADOW_PRIORITY_LIVE_READY,
}


def _positive_float(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out > 0 else None


def _as_dict(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _truthy_option_marker(value: Any) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _paper_option_meta_from_signal(signal_json: Any) -> dict[str, Any]:
    sig = _as_dict(signal_json)
    meta = sig.get("option_meta")
    if isinstance(meta, dict) and meta:
        return meta
    breakout = _as_dict(sig.get("breakout_alert"))
    meta = breakout.get("option_meta")
    if isinstance(meta, dict) and meta:
        return meta
    return {}


def _is_option_signal(signal_json: Any) -> bool:
    sig = _as_dict(signal_json)
    if _paper_option_meta_from_signal(sig):
        return True
    if _truthy_option_marker(sig.get("options_path")):
        return True
    if str(sig.get("asset_type") or "").strip().lower() in {"option", "options"}:
        return True
    breakout = _as_dict(sig.get("breakout_alert"))
    if str(breakout.get("asset_type") or "").strip().lower() in {"option", "options"}:
        return True
    return _truthy_option_marker(breakout.get("options_path"))


def _is_option_paper_trade(pt: PaperTrade) -> bool:
    return _is_option_signal(getattr(pt, "signal_json", None))


def _paper_contract_multiplier(pt: PaperTrade) -> float:
    return OPTION_CONTRACT_MULTIPLIER if _is_option_paper_trade(pt) else 1.0


def _option_paper_levels(entry_price: float) -> tuple[float, float]:
    stop_pct = float(getattr(settings, "chili_autotrader_options_exit_stop_pct", 50.0) or 50.0)
    target_pct = float(getattr(settings, "chili_autotrader_options_exit_tp_pct", 100.0) or 100.0)
    stop_price = max(float(entry_price) * max(0.0, 1.0 - stop_pct / 100.0), 0.01)
    target_price = float(entry_price) * (1.0 + max(0.0, target_pct) / 100.0)
    return round(stop_price, 4), round(target_price, 4)


def _option_signal_quantity(signal_json: Any) -> int | None:
    meta = _paper_option_meta_from_signal(signal_json)
    qty = _positive_float(meta.get("quantity"))
    if qty is None:
        return None
    return max(1, int(qty))


def _option_premium_level(value: Any, entry_price: float) -> float | None:
    level = _positive_float(value)
    if level is None:
        return None
    # Underlying-shaped stops/targets can leak into option signals. Premium
    # levels should be in the same rough price domain as the option entry.
    return level if level <= entry_price * 10.0 else None


def _size_option_contracts(
    capital: float,
    entry_price: float,
    stop_price: float,
    *,
    risk_pct: float,
) -> int:
    risk_amount = float(capital or 0.0) * (float(risk_pct) / 100.0)
    risk_per_contract = abs(float(entry_price) - float(stop_price)) * OPTION_CONTRACT_MULTIPLIER
    if risk_amount <= 0 or risk_per_contract <= 0 or entry_price <= 0:
        return 0
    by_risk = int(risk_amount / risk_per_contract)
    by_notional = int((float(capital or 0.0) * 0.20) / (entry_price * OPTION_CONTRACT_MULTIPLIER))
    return max(0, min(by_risk, by_notional))


def _paper_current_mark_price(pt: PaperTrade, *, purpose: str = "display") -> float | None:
    if _is_option_paper_trade(pt):
        try:
            from .broker_quotes import broker_quote_for_trade

            proxy = SimpleNamespace(
                ticker=getattr(pt, "ticker", ""),
                direction=getattr(pt, "direction", "long"),
                broker_source="robinhood",
                indicator_snapshot=getattr(pt, "signal_json", None) or {},
            )
            quote = broker_quote_for_trade(proxy, purpose=purpose)
            return _positive_float(
                quote.get("price")
                or quote.get("mark_price")
                or quote.get("last_price")
            )
        except Exception:
            logger.debug(
                "[paper] option premium quote failed ticker=%s",
                getattr(pt, "ticker", None),
                exc_info=True,
            )
            return None
    try:
        from .market_data import fetch_quote

        quote = fetch_quote(pt.ticker)
        return _positive_float((quote or {}).get("price"))
    except Exception:
        return None
PAPER_SHADOW_DECISION_PRIORITY = {
    "blocked_recert_required": PAPER_SHADOW_PRIORITY_RECERT,
    "blocked_shadow_promoted": PAPER_SHADOW_PRIORITY_SHADOW_PROMOTED,
    "blocked_coinbase_cap": PAPER_SHADOW_PRIORITY_LIVE_READY,
    "blocked_max_concurrent_crypto": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
    "blocked_max_concurrent_equity": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
    "blocked_max_concurrent_global": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
    "blocked_max_concurrent_options": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
    "blocked_regime_gate": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
    "placed": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
    "skipped_non_positive_expected_edge": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
    "skipped_duplicate_pattern_already_open": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
    "skipped_synergy_disabled_second_signal": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
    "skipped_synergy_not_applicable": PAPER_SHADOW_PRIORITY_STANDARD_EVIDENCE,
}
PAPER_SHADOW_SIGNAL_LANE_PRIORITY = {
    "shadow_near_miss": PAPER_SHADOW_PRIORITY_NEAR_MISS_SIGNAL_LANE,
}


def _utc_iso(ts: datetime | None = None) -> str:
    return (ts or datetime.utcnow()).replace(microsecond=0).isoformat()


def _parse_utc_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", ""))
    except (TypeError, ValueError):
        return None


def _get_pattern_exit_config(db: Session, scan_pattern_id: int | None) -> dict:
    """Load exit_config from the pattern's ScanPattern row, with defaults."""
    defaults = {
        "atr_stop_mult": DEFAULT_ATR_STOP_MULT,
        "atr_target_mult": DEFAULT_ATR_TARGET_MULT,
        "trailing_enabled": True,
        "trailing_atr_mult": 1.5,
        "max_bars": None,
        "timeframe": "1d",
    }
    if not scan_pattern_id:
        return defaults
    try:
        pat = db.query(ScanPattern).filter(ScanPattern.id == scan_pattern_id).first()
        if pat and pat.exit_config:
            cfg = pat.exit_config if isinstance(pat.exit_config, dict) else json.loads(pat.exit_config)
            defaults.update({k: v for k, v in cfg.items() if v is not None})
        if pat and pat.timeframe:
            defaults["timeframe"] = pat.timeframe
    except Exception:
        pass
    return defaults


def _compute_atr_levels(ticker: str, entry_price: float, exit_cfg: dict) -> tuple[float | None, float | None, float | None]:
    """Compute ATR-based stop, target, and ATR value for a ticker."""
    try:
        from .market_data import fetch_ohlcv_df
        from .indicator_core import compute_atr

        df = fetch_ohlcv_df(ticker, period="3mo", interval="1d")
        if df is None or len(df) < 14:
            return None, None, None

        atr_arr = compute_atr(df["High"].values, df["Low"].values, df["Close"].values, period=14)
        atr_val = float(atr_arr[-1]) if len(atr_arr) > 0 else None
        if not atr_val or atr_val <= 0:
            return None, None, None

        stop_dist = atr_val * exit_cfg.get("atr_stop_mult", DEFAULT_ATR_STOP_MULT)
        target_dist = atr_val * exit_cfg.get("atr_target_mult", DEFAULT_ATR_TARGET_MULT)
        return round(entry_price - stop_dist, 4), round(entry_price + target_dist, 4), atr_val
    except Exception:
        return None, None, None


def _apply_slippage(price: float, direction: str, is_entry: bool) -> float:
    """Apply simulated slippage to a fill price."""
    spread_pct = float(getattr(settings, "backtest_spread", DEFAULT_SLIPPAGE_PCT / 100) or 0.0) * 100
    slip = price * spread_pct / 100
    if is_entry:
        return price + slip if direction == "long" else price - slip
    else:
        return price - slip if direction == "long" else price + slip


def _expiry_days_for_timeframe(timeframe: str) -> int:
    """Adaptive expiry based on pattern timeframe."""
    tf_map = {"5m": 1, "15m": 2, "1h": 3, "4h": 5, "1d": 10, "1wk": 30}
    return tf_map.get(timeframe, PAPER_TRADE_EXPIRY_DAYS)


def open_paper_trade(
    db: Session,
    user_id: int | None,
    ticker: str,
    entry_price: float,
    *,
    scan_pattern_id: int | None = None,
    stop_price: float | None = None,
    target_price: float | None = None,
    direction: str = "long",
    quantity: int = 100,
    signal_json: dict | None = None,
    paper_shadow_of_alert_id: int | None = None,
    max_open_trades: int | None = None,
    capacity_scope: str = PAPER_TRADE_CAPACITY_SCOPE_ALL,
    allow_duplicate_open: bool = False,
) -> PaperTrade | None:
    """Open a simulated paper trade with ATR-based adaptive levels."""
    open_limit = MAX_OPEN_PAPER_TRADES if max_open_trades is None else int(max_open_trades)
    open_q = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.status == "open",
    )
    normalized_capacity_scope = (
        capacity_scope or PAPER_TRADE_CAPACITY_SCOPE_ALL
    ).strip().lower()
    if normalized_capacity_scope == PAPER_TRADE_CAPACITY_SCOPE_AUTOTRADER_SHADOW:
        open_count = sum(
            1 for pt in open_q.all() if _is_autotrader_paper_shadow_row(pt)
        )
    else:
        open_count = open_q.count()
    if open_count >= open_limit:
        logger.debug("[paper] Max open paper trades (%d) reached", open_limit)
        return None

    if not allow_duplicate_open:
        existing = db.query(PaperTrade).filter(
            PaperTrade.user_id == user_id,
            PaperTrade.ticker == ticker.upper(),
            PaperTrade.status == "open",
            PaperTrade.scan_pattern_id == scan_pattern_id,
        ).first()
        if existing:
            logger.debug(
                "[paper] Already have open paper trade for %s pattern %s",
                ticker,
                scan_pattern_id,
            )
            return None

    exit_cfg = _get_pattern_exit_config(db, scan_pattern_id)
    atr_val = None
    is_option_paper = _is_option_signal(signal_json)

    if is_option_paper and (stop_price is None or target_price is None):
        option_stop, option_target = _option_paper_levels(float(entry_price))
        if stop_price is None:
            stop_price = option_stop
        if target_price is None:
            target_price = option_target
    elif stop_price is None or target_price is None:
        atr_stop, atr_target, atr_val = _compute_atr_levels(ticker, entry_price, exit_cfg)
        if stop_price is None:
            stop_price = atr_stop if atr_stop else entry_price * 0.97
        if target_price is None:
            target_price = atr_target if atr_target else entry_price + abs(entry_price - stop_price) * 2

    fill_price = _apply_slippage(entry_price, direction, is_entry=True)

    meta = dict(_as_dict(signal_json))
    meta["_paper_meta"] = {
        "original_entry": entry_price,
        "fill_price": fill_price,
        "slippage_applied": round(abs(fill_price - entry_price), 4),
        "atr_value": atr_val,
        "exit_config": exit_cfg,
        "trailing_enabled": exit_cfg.get("trailing_enabled", True),
        "trailing_atr_mult": exit_cfg.get("trailing_atr_mult", 1.5),
        "trailing_stop": None,
        "highest_price": fill_price if direction == "long" else None,
        "lowest_price": fill_price if direction == "short" else None,
        "expiry_days": _expiry_days_for_timeframe(exit_cfg.get("timeframe", "1d")),
    }
    if is_option_paper:
        meta["_paper_meta"]["asset_type"] = "options"
        meta["_paper_meta"]["contract_multiplier"] = OPTION_CONTRACT_MULTIPLIER
        meta["_paper_meta"]["premium_stop_price"] = stop_price
        meta["_paper_meta"]["premium_target_price"] = target_price

    pt = PaperTrade(
        user_id=user_id,
        scan_pattern_id=scan_pattern_id,
        ticker=ticker.upper(),
        direction=direction,
        entry_price=round(fill_price, 4),
        stop_price=round(stop_price, 4),
        target_price=round(target_price, 4),
        quantity=quantity,
        status="open",
        entry_date=datetime.utcnow(),
        signal_json=meta,
        paper_shadow_of_alert_id=paper_shadow_of_alert_id,
    )
    db.add(pt)
    db.flush()

    # Phase A shadow hook: record entry fill in the canonical economic-truth
    # ledger. Legacy PaperTrade.pnl remains authoritative. Any failure is
    # swallowed so the paper path never breaks due to ledger bugs.
    try:
        from . import economic_ledger as _ledger
        if _ledger.mode_is_active():
            _ledger.record_entry_fill(
                db,
                source="paper",
                paper_trade_id=int(pt.id),
                user_id=user_id,
                scan_pattern_id=scan_pattern_id,
                ticker=pt.ticker,
                direction=direction,
                quantity=float(quantity),
                fill_price=float(fill_price),
                fee=0.0,
                event_ts=pt.entry_date,
                provenance={"legacy_path": "open_paper_trade", "atr_value": atr_val},
            )
    except Exception:
        logger.debug("[paper] economic_ledger entry hook failed", exc_info=True)

    logger.info("[paper] Opened paper trade: %s %s @ %.4f (fill=%.4f, stop=%.4f, target=%.4f, atr=%.4f)",
                direction, ticker, entry_price, fill_price, stop_price, target_price, atr_val or 0)
    return pt


def _is_autotrader_paper_shadow_row(pt: PaperTrade) -> bool:
    sig = pt.signal_json if isinstance(pt.signal_json, dict) else {}
    return bool(
        pt.paper_shadow_of_alert_id
        or sig.get("auto_trader_v1")
        or sig.get("paper_shadow")
        or sig.get("shadow_of_alert_id")
    )


def _paper_shadow_signal_json(pt: PaperTrade) -> dict[str, Any]:
    return pt.signal_json if isinstance(pt.signal_json, dict) else {}


def _paper_shadow_pattern_stage_map(
    db: Session,
    rows: list[PaperTrade],
) -> dict[int, str]:
    pattern_ids = {
        int(pt.scan_pattern_id)
        for pt in rows
        if getattr(pt, "scan_pattern_id", None) is not None
    }
    if not pattern_ids:
        return {}
    try:
        stage_rows = (
            db.query(ScanPattern.id, ScanPattern.lifecycle_stage)
            .filter(ScanPattern.id.in_(pattern_ids))
            .all()
        )
    except Exception:
        logger.debug("[paper_shadow_janitor] stage lookup failed", exc_info=True)
        return {}
    out: dict[int, str] = {}
    for row in stage_rows:
        try:
            pattern_id = int(row[0])
            stage = str(row[1] or "").strip().lower()
        except Exception:
            continue
        out[pattern_id] = stage
    return out


def _paper_shadow_evidence_priority(
    pt: PaperTrade,
    *,
    pattern_stage_by_id: dict[int, str],
) -> dict[str, Any]:
    sig = _paper_shadow_signal_json(pt)
    pattern_id = int(pt.scan_pattern_id or 0)
    stage = pattern_stage_by_id.get(pattern_id, "")
    stage_priority = PAPER_SHADOW_STAGE_PRIORITY.get(
        stage,
        PAPER_SHADOW_PRIORITY_UNKNOWN,
    )
    decision = str(
        sig.get("shadow_decision")
        or sig.get("paper_shadow_decision")
        or sig.get("paper_shadow_reject_decision")
        or ""
    ).strip().lower()
    decision_priority = PAPER_SHADOW_DECISION_PRIORITY.get(
        decision,
        PAPER_SHADOW_PRIORITY_UNKNOWN,
    )
    lane = str(
        sig.get("paper_observation_signal_lane")
        or sig.get("paper_shadow_signal_lane")
        or sig.get("signal_lane")
        or ""
    ).strip().lower()
    lane_priority = PAPER_SHADOW_SIGNAL_LANE_PRIORITY.get(
        lane,
        PAPER_SHADOW_PRIORITY_UNKNOWN,
    )
    priority = max(stage_priority, decision_priority, lane_priority)
    return {
        "priority": priority,
        "stage": stage or None,
        "stage_priority": stage_priority,
        "decision": decision or None,
        "decision_priority": decision_priority,
        "signal_lane": lane or None,
        "signal_lane_priority": lane_priority,
    }


def _paper_shadow_evict_key(
    pt: PaperTrade,
    *,
    pattern_stage_by_id: dict[int, str],
) -> tuple[int, datetime]:
    evidence = _paper_shadow_evidence_priority(
        pt,
        pattern_stage_by_id=pattern_stage_by_id,
    )
    return (
        int(evidence["priority"]),
        pt.entry_date or datetime.min,
    )


def _paper_close_ledger_safe(db: Session, pt: PaperTrade) -> None:
    try:
        from .brain_work.execution_hooks import on_paper_trade_closed

        on_paper_trade_closed(db, pt)
    except Exception:
        pass


def prune_autotrader_paper_shadow_capacity(
    db: Session,
    user_id: int | None,
    *,
    max_open: int,
    max_age_hours: int,
    buffer: int = 5,
    reserve_new_slot: bool = True,
) -> dict[str, Any]:
    """Close stale autotrader paper-shadow rows before opening more evidence.

    This janitor is intentionally scoped to rows tagged as autotrader/shadow
    observations. It never touches the user's ordinary paper-trading positions.
    """
    open_limit = max(1, int(max_open or 1))
    target_open = max(0, open_limit - max(0, int(buffer or 0)))
    age_limit_h = max(1.0, float(max_age_hours or 1))
    now = datetime.utcnow()

    q = db.query(PaperTrade).filter(PaperTrade.status == "open")
    if user_id is not None:
        q = q.filter(PaperTrade.user_id == user_id)
    rows = [pt for pt in q.all() if _is_autotrader_paper_shadow_row(pt)]
    if not rows:
        return {
            "checked": 0,
            "closed": 0,
            "stale_closed": 0,
            "capacity_closed": 0,
            "max_open": open_limit,
            "target_open": target_open,
            "reserve_new_slot": bool(reserve_new_slot),
            "eviction_policy": "priority_evidence_buffer",
        }

    stale: list[PaperTrade] = []
    for pt in rows:
        entry_dt = pt.entry_date or now
        age_h = max(0.0, (now - entry_dt).total_seconds() / 3600.0)
        if age_h >= age_limit_h:
            stale.append(pt)

    to_close: list[tuple[PaperTrade, str]] = [(pt, "stale") for pt in stale]
    selected_ids = {int(pt.id) for pt, _ in to_close if pt.id is not None}
    remaining_open = len(rows) - len(selected_ids)
    capacity_trigger = (
        max(1, target_open)
        if bool(reserve_new_slot)
        else open_limit
    )
    if remaining_open >= capacity_trigger:
        desired_open = (
            max(0, target_open - 1)
            if bool(reserve_new_slot)
            else target_open
        )
        excess = max(0, remaining_open - desired_open)
        pattern_stage_by_id = _paper_shadow_pattern_stage_map(db, rows)
        capacity_pool = sorted(
            [pt for pt in rows if int(pt.id or 0) not in selected_ids],
            key=lambda pt: _paper_shadow_evict_key(
                pt,
                pattern_stage_by_id=pattern_stage_by_id,
            ),
        )
        for pt in capacity_pool[:excess]:
            to_close.append((pt, "capacity"))
            if pt.id is not None:
                selected_ids.add(int(pt.id))

    if not to_close:
        return {
            "checked": len(rows),
            "closed": 0,
            "stale_closed": 0,
            "capacity_closed": 0,
            "max_open": open_limit,
            "target_open": target_open,
            "reserve_new_slot": bool(reserve_new_slot),
            "eviction_policy": "priority_evidence_buffer",
        }

    stale_closed = 0
    capacity_closed = 0
    for pt, kind in to_close:
        try:
            raw_exit = float(_paper_current_mark_price(pt, purpose="exit") or pt.entry_price)
        except Exception:
            raw_exit = float(pt.entry_price)
        exit_p = _apply_slippage(raw_exit, pt.direction or "long", is_entry=False)
        _close_paper_trade(pt, exit_p, "shadow_capacity_janitor")
        _paper_close_ledger_safe(db, pt)
        if kind == "stale":
            stale_closed += 1
        else:
            capacity_closed += 1

    db.commit()
    result = {
        "checked": len(rows),
        "closed": len(to_close),
        "stale_closed": stale_closed,
        "capacity_closed": capacity_closed,
        "max_open": open_limit,
        "target_open": target_open,
        "reserve_new_slot": bool(reserve_new_slot),
        "eviction_policy": "priority_evidence_buffer",
    }
    logger.info("[paper_shadow_janitor] %s", result)
    return result


def _paper_dynamic_monitor_candidate(pt: PaperTrade) -> bool:
    """True for paper rows that should mimic live dynamic monitoring."""
    if not bool(getattr(settings, "chili_autotrader_paper_dynamic_monitor_enabled", True)):
        return False
    sig = pt.signal_json if isinstance(pt.signal_json, dict) else {}
    return bool(
        sig.get("auto_trader_v1")
        or sig.get("paper_shadow")
        or pt.paper_shadow_of_alert_id
    )


def _paper_dynamic_near_stop_exit(pt: PaperTrade, price: float) -> dict[str, Any] | None:
    """Mirror the live plan-level monitor's near-stop risk trigger."""
    stop = float(pt.stop_price or 0.0)
    if stop <= 0 or price <= 0:
        return None
    side = (pt.direction or "long").lower()
    if side == "short":
        if price < stop:
            dist_pct = (stop - price) / stop * 100.0
        else:
            return None
    else:
        if price > stop:
            dist_pct = (price - stop) / stop * 100.0
        else:
            return None
    if 0 < dist_pct <= 2.0:
        return {
            "action": "exit_now",
            "reason": "plan_levels_near_stop",
            "confidence": 0.85,
            "decision_source": "plan_levels",
            "detail": {"distance_to_stop_pct": round(dist_pct, 4)},
        }
    return None


def _update_paper_dynamic_monitor_meta(
    pt: PaperTrade,
    *,
    action: str,
    source: str,
    reason: str | None,
    price: float,
    extra: dict[str, Any] | None = None,
) -> None:
    sig = dict(pt.signal_json or {})
    paper_meta = dict(sig.get("_paper_meta") or {})
    monitor_meta = dict(paper_meta.get("dynamic_monitor") or {})
    now_s = _utc_iso()
    event = {
        "checked_at": now_s,
        "action": action,
        "source": source,
        "reason": reason,
        "price": round(float(price), 8),
    }
    if extra:
        event.update(extra)
    history = list(monitor_meta.get("history") or [])
    history.append(event)
    monitor_meta["history"] = history[-20:]
    monitor_meta["last_checked_at"] = now_s
    monitor_meta["last_action"] = action
    monitor_meta["last_source"] = source
    monitor_meta["last_reason"] = reason
    monitor_meta["last_price"] = round(float(price), 8)
    for key in (
        "health_score",
        "health_delta",
        "plan_health_score",
        "static_health_score",
        "vitals_composite",
        "signal_signature",
    ):
        if extra and key in extra:
            monitor_meta[f"last_{key}"] = extra[key]
    paper_meta["dynamic_monitor"] = monitor_meta
    sig["_paper_meta"] = paper_meta
    pt.signal_json = sig


def _paper_dynamic_monitor_decision(
    db: Session,
    pt: PaperTrade,
    *,
    price: float,
    quote_source: str,
) -> dict[str, Any] | None:
    """Evaluate the live-style dynamic monitor for an autotrader paper row.

    The live monitor often exits on pattern health deterioration, not only on
    the original bracket. Paper-shadow learning should therefore be labeled by
    this dynamic policy too. This helper deliberately uses deterministic
    monitor paths only: plan-level near-stop, learned mechanical rules, and the
    heuristic pre-filter. It does not call the premium LLM from the paper loop.
    """
    if not _paper_dynamic_monitor_candidate(pt):
        return None

    sig = pt.signal_json if isinstance(pt.signal_json, dict) else {}
    paper_meta = sig.get("_paper_meta") if isinstance(sig.get("_paper_meta"), dict) else {}
    monitor_meta = (
        paper_meta.get("dynamic_monitor")
        if isinstance(paper_meta.get("dynamic_monitor"), dict)
        else {}
    )

    near_stop = _paper_dynamic_near_stop_exit(pt, price)
    cooldown_min = int(
        getattr(settings, "chili_autotrader_paper_dynamic_monitor_cooldown_minutes", 5)
        or 0
    )
    last_checked = _parse_utc_iso(monitor_meta.get("last_checked_at"))
    if cooldown_min > 0 and last_checked is not None and near_stop is None:
        age_s = (datetime.utcnow() - last_checked).total_seconds()
        if age_s < cooldown_min * 60:
            return None

    decision: dict[str, Any] | None = None
    alert_id = (
        pt.paper_shadow_of_alert_id
        or sig.get("breakout_alert_id")
        or sig.get("shadow_of_alert_id")
    )
    alert: BreakoutAlert | None = None
    pattern: ScanPattern | None = None
    if alert_id:
        try:
            alert = db.get(BreakoutAlert, int(alert_id))
        except Exception:
            alert = None
    pattern_id = pt.scan_pattern_id or (alert.scan_pattern_id if alert else None)
    if pattern_id:
        try:
            pattern = db.get(ScanPattern, int(pattern_id))
        except Exception:
            pattern = None

    if pattern is not None and getattr(pattern, "rules_json", None):
        try:
            from .market_data import get_indicator_snapshot
            from .monitor_rules_engine import (
                apply_level_ratios,
                build_signal_snapshot,
                compute_signal_signature,
                get_graduation_status,
                heuristic_adjustment,
                is_pattern_simple,
                lookup_rule,
            )
            from .pattern_condition_monitor import evaluate_pattern_health, evaluate_trade_plan
            from .pattern_position_monitor import (
                _effective_monitor_health_score,
                _flatten_indicators,
            )
            from .scanner import get_adaptive_weight
            from .setup_vitals import get_or_compute_ticker_vitals

            timeframe = (
                getattr(pattern, "timeframe", None)
                or (paper_meta.get("exit_config") or {}).get("timeframe")
                or "1d"
            )
            indicators = get_indicator_snapshot(pt.ticker, str(timeframe))
            flat = _flatten_indicators(indicators or {})
            trade_plan = None
            if alert is not None:
                trade_plan = alert.trade_plan or alert.trade_plan_mechanical

            vitals = None
            try:
                vitals = get_or_compute_ticker_vitals(db, pt.ticker, str(timeframe))
            except Exception:
                vitals = None

            prev_health = monitor_meta.get("last_health_score")
            try:
                prev_health_f = float(prev_health) if prev_health is not None else None
            except (TypeError, ValueError):
                prev_health_f = None

            health = evaluate_pattern_health(
                pattern.rules_json,
                flat,
                previous_health=prev_health_f,
            )
            plan_health = evaluate_trade_plan(trade_plan, flat, price, vitals=vitals)
            static_health_score = float(health.health_score)
            effective_score, health_source = _effective_monitor_health_score(
                condition_health=health,
                plan_health=plan_health,
                vitals=vitals,
            )
            health.health_score = effective_score
            health.health_delta = (
                round(effective_score - prev_health_f, 4)
                if prev_health_f is not None
                else None
            )

            delta_urgent = float(get_adaptive_weight("monitor_delta_urgent"))
            health_weakening = float(get_adaptive_weight("monitor_health_weakening"))
            health_healthy = float(get_adaptive_weight("monitor_health_healthy"))
            needs_action = bool(
                plan_health.has_critical_invalidation
                or plan_health.has_any_invalidation
                or plan_health.caution_signals_changed
                or (
                    health.health_delta is not None
                    and float(health.health_delta) <= delta_urgent
                )
                or float(health.health_score) < health_weakening
            )

            bearish_div = False
            overextended_fading = False
            if vitals is not None:
                try:
                    bearish_div = any(
                        isinstance(d, dict) and d.get("type") == "bearish"
                        for d in (getattr(vitals, "divergences", None) or [])
                    )
                    overextended_fading = (
                        float(getattr(vitals, "overextension_risk", 0) or 0) > 0.8
                        and float(getattr(vitals, "momentum_score", 0) or 0) < -0.2
                    )
                except Exception:
                    bearish_div = False
                    overextended_fading = False
            needs_action = bool(needs_action or bearish_div or overextended_fading)

            sig_snap = build_signal_snapshot(
                plan_health=plan_health,
                condition_health=health,
                pnl_pct=(
                    ((price - float(pt.entry_price)) / float(pt.entry_price) * 100.0)
                    if pt.entry_price
                    else None
                ),
                current_price=price,
                stop_price=pt.stop_price or (alert.stop_loss if alert else None),
                target_price=pt.target_price or (alert.target_price if alert else None),
                vitals=vitals,
            )
            signal_sig = compute_signal_signature(sig_snap)
            pattern_type = (pattern.name or f"pattern_{pattern.id}")[:120]

            primary = None
            decision_source = "dynamic_monitor_hold"
            if needs_action:
                mech = lookup_rule(db, pattern_type, signal_sig)
                if mech and mech.rule_id:
                    mech = apply_level_ratios(
                        mech,
                        mech.rule_id,
                        price,
                        pt.stop_price or (alert.stop_loss if alert else None),
                        db,
                    )
                simple = is_pattern_simple(
                    pattern.rules_json if isinstance(pattern.rules_json, dict) else None
                )
                grad_status = get_graduation_status(db, pattern_type, signal_sig)
                if (
                    grad_status == "graduated"
                    and mech
                ) or (simple and mech and grad_status == "shadow"):
                    primary = mech
                    decision_source = "mechanical"
                else:
                    primary = heuristic_adjustment(
                        plan_health=plan_health,
                        condition_health=health,
                        pnl_pct=(
                            ((price - float(pt.entry_price)) / float(pt.entry_price) * 100.0)
                            if pt.entry_price
                            else None
                        ),
                        current_price=price,
                        current_stop=pt.stop_price,
                        current_target=pt.target_price,
                        pattern_stop=pt.stop_price or (alert.stop_loss if alert else None),
                        delta_urgent=delta_urgent,
                        health_healthy=health_healthy,
                        trade_direction=pt.direction or "long",
                        vitals=vitals,
                        vitals_degradation={},
                    )
                    decision_source = "heuristic" if primary else "dynamic_monitor_no_llm"

            extra = {
                "health_score": round(float(health.health_score), 4),
                "health_delta": health.health_delta,
                "plan_health_score": round(float(plan_health.plan_health_score), 4),
                "static_health_score": round(static_health_score, 4),
                "health_source": health_source,
                "signal_signature": signal_sig,
                "quote_source": quote_source,
            }
            if vitals is not None:
                try:
                    extra["vitals_composite"] = round(float(vitals.composite_health), 4)
                except Exception:
                    pass

            if primary is not None and primary.action in {
                "exit_now",
                "tighten_stop",
                "loosen_target",
            }:
                decision = {
                    "action": primary.action,
                    "reason": primary.reasoning or primary.action,
                    "confidence": float(primary.confidence or 0.0),
                    "decision_source": decision_source,
                    "new_stop": primary.new_stop,
                    "new_target": getattr(primary, "new_target", None),
                    "detail": extra,
                }
            else:
                _update_paper_dynamic_monitor_meta(
                    pt,
                    action="hold",
                    source=decision_source,
                    reason="no_dynamic_exit",
                    price=price,
                    extra=extra,
                )
        except Exception:
            logger.debug(
                "[paper_dynamic_monitor] evaluation failed paper_trade=%s ticker=%s",
                getattr(pt, "id", None),
                pt.ticker,
                exc_info=True,
            )

    if (decision is None or decision.get("action") == "hold") and near_stop is not None:
        decision = near_stop

    if decision is None:
        return None

    action = str(decision.get("action") or "hold")
    if action == "tighten_stop" and decision.get("new_stop") is not None:
        try:
            new_stop = float(decision["new_stop"])
            side = (pt.direction or "long").lower()
            current_stop = float(pt.stop_price or 0.0)
            tightens = (
                current_stop <= 0
                or (side != "short" and current_stop < new_stop < price)
                or (side == "short" and price < new_stop < current_stop)
            )
            if tightens:
                pt.stop_price = round(new_stop, 4)
        except (TypeError, ValueError):
            pass
    elif action == "loosen_target" and decision.get("new_target") is not None:
        try:
            new_target = float(decision["new_target"])
            side = (pt.direction or "long").lower()
            current_target = float(pt.target_price or 0.0)
            loosens = (
                current_target <= 0
                or (side != "short" and new_target > current_target)
                or (side == "short" and 0 < new_target < current_target)
            )
            if loosens:
                pt.target_price = round(new_target, 4)
        except (TypeError, ValueError):
            pass

    _update_paper_dynamic_monitor_meta(
        pt,
        action=action,
        source=str(decision.get("decision_source") or "dynamic_monitor"),
        reason=str(decision.get("reason") or action),
        price=price,
        extra={
            **(decision.get("detail") or {}),
            "confidence": decision.get("confidence"),
            "new_stop": decision.get("new_stop"),
            "new_target": decision.get("new_target"),
        },
    )
    return decision


def check_paper_exits(
    db: Session,
    user_id: int | None = None,
    *,
    skip_trade_ids: set[int] | None = None,
) -> dict[str, Any]:
    """Check all open paper trades for stop/target/trailing-stop/expiry exits.

    Supports ATR trailing stops: once price moves >= 1R in profit, trail the
    stop at trailing_atr_mult * ATR behind the best price seen. The trailing
    stop only tightens, never loosens.

    ``skip_trade_ids`` lets the caller hold specific rows past their stop/target
    (used by AutoTrader v1 per-position monitor pause from the Autopilot desk).
    """
    open_trades = db.query(PaperTrade).filter(
        PaperTrade.status == "open",
    )
    if user_id is not None:
        open_trades = open_trades.filter(PaperTrade.user_id == user_id)
    open_trades = open_trades.all()

    if skip_trade_ids:
        open_trades = [pt for pt in open_trades if pt.id not in skip_trade_ids]

    if not open_trades:
        return {"checked": 0, "closed": 0, "trailing_updated": 0}

    def _paper_close_ledger(db_sess: Session, ptx: PaperTrade) -> None:
        try:
            from .brain_work.execution_hooks import on_paper_trade_closed
            on_paper_trade_closed(db_sess, ptx)
        except Exception:
            pass

    closed = 0
    trailing_updated = 0
    for pt in open_trades:
        try:
            meta = (pt.signal_json or {}).get("_paper_meta", {})
            expiry = meta.get("expiry_days", PAPER_TRADE_EXPIRY_DAYS)

            price = _paper_current_mark_price(pt)
            if price is None:
                if pt.entry_date and (datetime.utcnow() - pt.entry_date).days >= expiry:
                    exit_p = _apply_slippage(pt.entry_price, pt.direction, is_entry=False)
                    _close_paper_trade(pt, exit_p, "expired")
                    _paper_close_ledger(db, pt)
                    closed += 1
                continue

            quote_source = "robinhood_options" if _is_option_paper_trade(pt) else "market_data"
            is_long = pt.direction == "long"

            dynamic_decision = _paper_dynamic_monitor_decision(
                db,
                pt,
                price=price,
                quote_source=quote_source,
            )
            if dynamic_decision and dynamic_decision.get("action") == "exit_now":
                exit_p = _apply_slippage(price, pt.direction, is_entry=False)
                _close_paper_trade(pt, exit_p, "pattern_exit_now")
                _paper_close_ledger(db, pt)
                closed += 1
                continue
            meta = (pt.signal_json or {}).get("_paper_meta", {})

            # --- Trailing stop logic ---
            trail_enabled = meta.get("trailing_enabled", False)
            atr_val = meta.get("atr_value")
            trail_mult = meta.get("trailing_atr_mult", 1.5)
            if trail_enabled and atr_val and atr_val > 0:
                risk = abs(pt.entry_price - (pt.stop_price or pt.entry_price * 0.97))
                if is_long:
                    best = max(meta.get("highest_price") or pt.entry_price, price)
                    meta["highest_price"] = best
                    profit_r = (best - pt.entry_price) / risk if risk > 0 else 0
                    if profit_r >= TRAILING_STOP_ACTIVATION_R:
                        new_trail = best - (atr_val * trail_mult)
                        old_trail = meta.get("trailing_stop")
                        if old_trail is None or new_trail > old_trail:
                            meta["trailing_stop"] = round(new_trail, 4)
                            trailing_updated += 1
                else:
                    best = min(meta.get("lowest_price") or pt.entry_price, price)
                    meta["lowest_price"] = best
                    profit_r = (pt.entry_price - best) / risk if risk > 0 else 0
                    if profit_r >= TRAILING_STOP_ACTIVATION_R:
                        new_trail = best + (atr_val * trail_mult)
                        old_trail = meta.get("trailing_stop")
                        if old_trail is None or new_trail < old_trail:
                            meta["trailing_stop"] = round(new_trail, 4)
                            trailing_updated += 1

                sj = dict(pt.signal_json or {})
                sj["_paper_meta"] = meta
                pt.signal_json = sj

            effective_stop = pt.stop_price
            trail_stop = meta.get("trailing_stop")
            if trail_stop is not None:
                if is_long:
                    effective_stop = max(effective_stop or 0, trail_stop)
                else:
                    effective_stop = min(effective_stop or float("inf"), trail_stop)

            exit_price_with_slip = _apply_slippage(price, pt.direction, is_entry=False)

            # Stop hit (includes trailing)
            if is_long and effective_stop and price <= effective_stop:
                reason = "trailing_stop" if trail_stop and trail_stop >= (pt.stop_price or 0) else "stop"
                _close_paper_trade(pt, _apply_slippage(effective_stop, pt.direction, is_entry=False), reason)
                _paper_close_ledger(db, pt)
                closed += 1
            elif not is_long and effective_stop and price >= effective_stop:
                reason = "trailing_stop" if trail_stop and trail_stop <= (pt.stop_price or float("inf")) else "stop"
                _close_paper_trade(pt, _apply_slippage(effective_stop, pt.direction, is_entry=False), reason)
                _paper_close_ledger(db, pt)
                closed += 1
            # Target hit
            elif is_long and pt.target_price and price >= pt.target_price:
                _close_paper_trade(pt, _apply_slippage(pt.target_price, pt.direction, is_entry=False), "target")
                _paper_close_ledger(db, pt)
                closed += 1
            elif not is_long and pt.target_price and price <= pt.target_price:
                _close_paper_trade(pt, _apply_slippage(pt.target_price, pt.direction, is_entry=False), "target")
                _paper_close_ledger(db, pt)
                closed += 1
            # Time-based forced exit for day trades / scalps
            elif pt.entry_date and meta.get("trade_type") in ("scalp", "daytrade", "breakout", "momentum"):
                from .scanner import _MAX_HOLD_HOURS
                _max_h = _MAX_HOLD_HOURS.get(meta["trade_type"])
                if _max_h is not None:
                    _held_h = (datetime.utcnow() - pt.entry_date).total_seconds() / 3600
                    if _held_h >= _max_h:
                        _close_paper_trade(pt, exit_price_with_slip, f"time_exit_{meta['trade_type']}")
                        _paper_close_ledger(db, pt)
                        closed += 1
                        continue
            # Expiry
            elif pt.entry_date and (datetime.utcnow() - pt.entry_date).days >= expiry:
                _close_paper_trade(pt, exit_price_with_slip, "expired")
                _paper_close_ledger(db, pt)
                closed += 1
        except Exception as e:
            logger.debug("[paper] Error checking %s: %s", pt.ticker, e)

    if closed > 0 or trailing_updated > 0:
        db.commit()

    return {"checked": len(open_trades), "closed": closed, "trailing_updated": trailing_updated}


def place_partial_close(
    db: Session,
    trade: PaperTrade,
    fraction: float,
    *,
    current_price: float | None = None,
) -> dict[str, Any]:
    """Submit a partial close on a paper position at ``fraction`` of size.

    Reduces ``trade.quantity`` by ``fraction`` of the current quantity,
    populates the four ``partial_taken_*`` bookkeeping columns (migration
    226), and commits. Slippage applied on the fill price the same way
    full closes do (``_apply_slippage``). The remaining position keeps
    running for trail / target / BOS / time-decay; the partial-taken bit
    prevents the same trade from re-firing.

    Returns ``{"ok": True, "quantity": qty_closed, "price": fill_price}``
    on success, ``{"ok": False, "error": reason}`` on validation failure
    or quote miss. NEVER raises into the caller (consistent with other
    paper helpers).

    Live (Trade ORM) partial closes are intentionally NOT supported here:
    they need a separate fast-path safety-belt review per the brief and
    are out of scope. ``broker_service.place_sell_order`` already accepts
    a partial qty if/when that wiring lands.
    """
    if not isinstance(trade, PaperTrade):
        return {"ok": False, "error": "live_partial_not_yet_supported"}
    if getattr(trade, "partial_taken", False):
        return {"ok": False, "error": "already_partialed"}
    if not (0.0 < fraction < 1.0):
        return {"ok": False, "error": f"invalid_fraction:{fraction}"}

    qty_to_close = float(trade.quantity) * float(fraction)
    if qty_to_close <= 0:
        return {"ok": False, "error": f"computed_qty_non_positive:{qty_to_close}"}

    if current_price is None:
        mark_price = _paper_current_mark_price(trade, purpose="exit")
        if mark_price is None:
            return {"ok": False, "error": "no_quote"}
        current_price = float(mark_price)

    fill_price = _apply_slippage(float(current_price), trade.direction, is_entry=False)

    trade.quantity = float(trade.quantity) - qty_to_close
    trade.partial_taken = True
    trade.partial_taken_at = datetime.utcnow()
    trade.partial_taken_qty = qty_to_close
    trade.partial_taken_price = fill_price
    db.add(trade)
    db.commit()

    logger.info(
        "[paper] Partial close %s %s qty=%.4f @ %.4f (frac=%.2f, remaining=%.4f)",
        trade.direction, trade.ticker,
        qty_to_close, fill_price, fraction, trade.quantity,
    )
    return {"ok": True, "quantity": qty_to_close, "price": fill_price}


def _close_paper_trade(pt: PaperTrade, exit_price: float, reason: str) -> None:
    """Close a paper trade with P&L calculation."""
    pt.status = "closed"
    pt.exit_date = datetime.utcnow()
    pt.exit_price = exit_price
    pt.exit_reason = reason

    multiplier = _paper_contract_multiplier(pt)
    if pt.direction == "long":
        gross_pnl = (exit_price - pt.entry_price) * pt.quantity * multiplier
        gross_pct = (exit_price - pt.entry_price) / pt.entry_price * 100
    else:
        gross_pnl = (pt.entry_price - exit_price) * pt.quantity * multiplier
        gross_pct = (pt.entry_price - exit_price) / pt.entry_price * 100

    commission_rate = float(getattr(settings, "backtest_commission", 0.0) or 0.0)
    commission_cost = (pt.entry_price + exit_price) * pt.quantity * multiplier * commission_rate
    net_pnl = gross_pnl - commission_cost
    notional = max(pt.entry_price * pt.quantity * multiplier, 1e-9)
    net_pct = (net_pnl / notional) * 100
    pt.pnl = round(net_pnl, 2)
    pt.pnl_pct = round(net_pct, 2)

    logger.info("[paper] Closed %s %s @ %.2f (%s) P&L: $%.2f (%.2f%%)",
                pt.direction, pt.ticker, exit_price, reason, pt.pnl, pt.pnl_pct)

    # Phase A shadow hook: record exit fill + reconcile legacy pnl against
    # the canonical ledger's realized_pnl_delta sum. Shadow-only; legacy
    # pt.pnl remains authoritative. Any failure is swallowed.
    try:
        from sqlalchemy import inspect as _sa_inspect
        from . import economic_ledger as _ledger

        if _ledger.mode_is_active():
            sess = _sa_inspect(pt).session
            if sess is not None:
                # Entry-leg + exit-leg fees are both folded into commission_cost
                # at legacy close time; ledger rows express them on the exit leg
                # only so the sum of realized_pnl_delta matches pt.pnl exactly.
                fee_total = float(commission_cost)
                _ledger.record_exit_fill(
                    sess,
                    source="paper",
                    paper_trade_id=int(pt.id),
                    user_id=pt.user_id,
                    scan_pattern_id=pt.scan_pattern_id,
                    ticker=pt.ticker or "",
                    direction=pt.direction or "long",
                    quantity=float(pt.quantity),
                    fill_price=float(exit_price),
                    entry_price=float(pt.entry_price),
                    fee=fee_total,
                    event_ts=pt.exit_date,
                    provenance={"legacy_path": "_close_paper_trade", "reason": reason},
                )
                _ledger.reconcile_trade(
                    sess,
                    source="paper",
                    paper_trade_id=int(pt.id),
                    user_id=pt.user_id,
                    scan_pattern_id=pt.scan_pattern_id,
                    ticker=pt.ticker or "",
                    legacy_pnl=float(pt.pnl) if pt.pnl is not None else None,
                    provenance={"legacy_path": "_close_paper_trade", "reason": reason},
                )
    except Exception:
        logger.debug("[paper] economic_ledger exit/reconcile hook failed", exc_info=True)


def get_paper_dashboard(db: Session, user_id: int | None = None) -> dict[str, Any]:
    """Get paper trading performance summary."""
    open_trades = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.status == "open",
    ).all()

    closed_trades = db.query(PaperTrade).filter(
        PaperTrade.user_id == user_id,
        PaperTrade.status == "closed",
    ).all()

    total_pnl = sum(t.pnl or 0 for t in closed_trades)
    wins = [t for t in closed_trades if (t.pnl or 0) > 0]
    losses = [t for t in closed_trades if (t.pnl or 0) <= 0]
    win_rate = len(wins) / len(closed_trades) * 100 if closed_trades else 0

    stops_hit = sum(1 for t in closed_trades if t.exit_reason == "stop")
    targets_hit = sum(1 for t in closed_trades if t.exit_reason == "target")
    expired = sum(1 for t in closed_trades if t.exit_reason == "expired")

    # Per-pattern attribution
    sp_pnl: dict[int, list[float]] = {}
    for t in closed_trades:
        if t.scan_pattern_id:
            sp_pnl.setdefault(t.scan_pattern_id, []).append(t.pnl or 0)

    sp_ids = list(sp_pnl.keys())
    sp_names = {}
    if sp_ids:
        for sp in db.query(ScanPattern).filter(ScanPattern.id.in_(sp_ids)).all():
            sp_names[sp.id] = sp.name

    pattern_stats = sorted([
        {
            "pattern_id": sp_id,
            "pattern_name": sp_names.get(sp_id, f"#{sp_id}"),
            "trades": len(pnls),
            "pnl": round(sum(pnls), 2),
            "win_rate": round(sum(1 for p in pnls if p > 0) / len(pnls) * 100, 1),
        }
        for sp_id, pnls in sp_pnl.items()
    ], key=lambda x: x["pnl"], reverse=True)

    return {
        "ok": True,
        "open_trades": len(open_trades),
        "closed_trades": len(closed_trades),
        "total_pnl": round(total_pnl, 2),
        "win_rate": round(win_rate, 1),
        "wins": len(wins),
        "losses": len(losses),
        "stops_hit": stops_hit,
        "targets_hit": targets_hit,
        "expired": expired,
        "pattern_stats": pattern_stats[:10],
        "open": [
            {
                "id": t.id, "ticker": t.ticker, "direction": t.direction,
                "entry": t.entry_price, "stop": t.stop_price, "target": t.target_price,
                "pattern_id": t.scan_pattern_id,
                "entry_date": t.entry_date.isoformat() if t.entry_date else None,
            }
            for t in open_trades
        ],
    }


def auto_enter_from_signals(
    db: Session,
    user_id: int | None,
    signals: list[dict[str, Any]],
    capital: float = DEFAULT_PAPER_CAPITAL,
) -> int:
    """Automatically open paper trades from high-confidence signals.

    Each signal dict should have: ticker, entry_price, stop_price, target_price,
    scan_pattern_id, confidence.

    Checks portfolio risk gate before each entry to enforce position limits,
    heat caps, sector concentration, correlation risk, and circuit breakers.
    """
    from .portfolio_risk import size_position, check_new_trade_allowed

    # NetEdgeRanker (Phase E, shadow-only). Imported lazily so the paper path
    # never breaks if the ranker module has an issue; the heuristic sizing
    # below is the single source of truth until Phase E becomes authoritative.
    try:
        from . import net_edge_ranker as _net_edge
    except Exception as _exc:  # pragma: no cover - defensive
        _net_edge = None  # type: ignore[assignment]
        logger.debug("[paper] net_edge_ranker unavailable: %s", _exc)

    entered = 0
    blocked = 0
    for sig in signals:
        conf = sig.get("confidence", 0)
        if conf < 0.6:
            continue

        ticker = sig.get("ticker", "")
        entry = sig.get("entry_price") or sig.get("price")
        stop = sig.get("stop_price") or sig.get("stop")
        target = sig.get("target_price") or sig.get("target")
        if not entry or entry <= 0:
            continue

        is_option_sig = _is_option_signal(sig)
        asset_type = (
            "options"
            if is_option_sig
            else ("crypto" if str(ticker).upper().endswith("-USD") else "stock")
        )
        allowed, reason = check_new_trade_allowed(
            db,
            user_id,
            ticker,
            capital,
            asset_type=asset_type,
        )
        if not allowed:
            logger.info("[paper] Trade blocked for %s: %s", ticker, reason)
            blocked += 1
            continue

        if is_option_sig:
            stop = _option_premium_level(stop, float(entry))
            target = _option_premium_level(target, float(entry))
            sizing_stop = stop
            if sizing_stop is None:
                sizing_stop, _default_target = _option_paper_levels(float(entry))
                stop = sizing_stop
                if target is None:
                    target = _default_target
            qty = _option_signal_quantity(sig) or _size_option_contracts(
                capital,
                float(entry),
                float(sizing_stop),
                risk_pct=0.5,
            )
            if qty <= 0:
                qty = 1
        else:
            if not stop:
                stop = entry * 0.97

            qty = size_position(capital, entry, stop, risk_pct=0.5)
            if qty <= 0:
                qty = 10

        # SHADOW HOOK: Compute NetEdgeRanker score for measurement only. The
        # qty above is and remains the authoritative sizing decision. The
        # ranker is not allowed to skip or resize entries until
        # ``brain_net_edge_ranker_mode == "authoritative"`` (future phase).
        _net_edge_score = None
        if _net_edge is not None and _net_edge.mode_is_active():
            try:
                _ctx = _net_edge.NetEdgeSignalContext(
                    ticker=ticker,
                    asset_class=asset_type,
                    scan_pattern_id=sig.get("scan_pattern_id"),
                    raw_prob=float(conf),
                    entry_price=float(entry),
                    stop_price=float(stop),
                    target_price=float(target) if target else None,
                    regime=sig.get("regime"),
                    timeframe=sig.get("timeframe"),
                    heuristic_score=sig.get("heuristic_score"),
                )
                _net_edge_score = _net_edge.score(db, _ctx)  # logged + persisted
            except Exception as _exc:  # pragma: no cover - defensive
                logger.debug("[paper] net_edge shadow score failed: %s", _exc)

        # Phase H shadow hook: log a canonical sizing proposal so the
        # paper path's ``qty`` can be compared against the Kelly +
        # portfolio-capped size. Shadow-only; qty above is unchanged.
        try:
            from .position_sizer_emitter import EmitterSignal, emit_shadow_proposal
            from .position_sizer_writer import LegacySizing, mode_is_active

            if mode_is_active():
                _legacy_notional = (
                    float(qty)
                    * float(entry)
                    * (OPTION_CONTRACT_MULTIPLIER if is_option_sig else 1.0)
                    if qty > 0 and entry > 0
                    else None
                )
                emit_shadow_proposal(
                    db,
                    signal=EmitterSignal(
                        source="paper_trading.auto_open",
                        ticker=ticker,
                        direction="long",
                        entry_price=float(entry),
                        stop_price=float(stop),
                        capital=float(capital or 0.0),
                        target_price=float(target) if target else None,
                        asset_class=asset_type,
                        user_id=user_id,
                        pattern_id=sig.get("scan_pattern_id"),
                        regime=sig.get("regime"),
                        confidence=float(conf) if conf is not None else None,
                    ),
                    legacy=LegacySizing(
                        notional=_legacy_notional,
                        quantity=float(qty) if qty else None,
                        source="paper_trading.size_position",
                    ),
                    net_edge_score=_net_edge_score,
                )
        except Exception as _exc:  # pragma: no cover - defensive
            logger.debug("[paper] phase H shadow emit failed: %s", _exc)

        pt = open_paper_trade(
            db, user_id,
            ticker=ticker,
            entry_price=entry,
            scan_pattern_id=sig.get("scan_pattern_id"),
            stop_price=stop,
            target_price=target,
            quantity=qty,
            signal_json=sig,
        )
        if pt:
            entered += 1

    if entered > 0:
        db.commit()

    if blocked > 0:
        logger.info("[paper] %d signals blocked by risk gate, %d entered", blocked, entered)

    return entered
