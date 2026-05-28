"""Bulk position plan generator: LLM-powered evaluation of all open positions."""
from __future__ import annotations

import hashlib
import json
import logging
import math
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Mapping

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import BreakoutAlert, PatternMonitorDecision, ScanPattern, Trade
from ..llm_caller import call_llm
from .market_data import fetch_quotes_batch, get_market_regime
from .options.contracts import (
    OPTION_CONTRACT_MULTIPLIER,
    PRICE_DOMAIN_OPTION_PREMIUM,
    PRICE_DOMAIN_UNDERLYING_SPOT,
    parse_contract_quantity,
)

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "position_plan.txt"
PLAN_STALE_HOURS = 4
MATERIAL_SIGNATURE_VERSION = 1


def _positive_float_or_none(value: Any) -> float | None:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= 0:
        return None
    return out


def _bucket_number(value: Any, *, step: float, digits: int = 4) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if step <= 0:
        return round(number, digits)
    return round(round(number / step) * step, digits)


def _bucket_price(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    abs_number = abs(number)
    if abs_number >= 100:
        step = 0.25
    elif abs_number >= 20:
        step = 0.1
    elif abs_number >= 5:
        step = 0.05
    else:
        step = 0.01
    return _bucket_number(number, step=step)


def _bucket_bars(value: Any) -> int | None:
    try:
        bars = int(value)
    except (TypeError, ValueError):
        return None
    if bars < 0:
        return None
    if bars < 20:
        return bars
    return int(round(bars / 5) * 5)


def _context_quantity(trade: Trade, *, trade_is_option: bool) -> float | None:
    if trade_is_option:
        qty = parse_contract_quantity(getattr(trade, "quantity", None))
        return float(qty) if qty is not None else None
    return _positive_float_or_none(getattr(trade, "quantity", None))


def _as_mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except Exception:
            return None
        return parsed if isinstance(parsed, Mapping) else None
    return None


def _nested_mapping(source: Mapping[str, Any] | None, key: str) -> Mapping[str, Any] | None:
    if not isinstance(source, Mapping):
        return None
    return _as_mapping(source.get(key))


def _trade_price_domains(trade: Trade) -> dict[str, str]:
    snap = _as_mapping(getattr(trade, "indicator_snapshot", None))
    domains = _nested_mapping(snap, "price_domains")
    if not domains:
        breakout = _nested_mapping(snap, "breakout_alert")
        domains = _nested_mapping(breakout, "price_domains")
    if not domains:
        return {}
    return {
        str(k): str(v).strip().lower()
        for k, v in domains.items()
        if str(k or "").strip() and str(v or "").strip()
    }


def _signed_price_pnl(
    *,
    entry: float | None,
    current: float | None,
    quantity: float | None,
    direction: Any,
    multiplier: float,
) -> float | None:
    if entry is None or current is None or quantity is None:
        return None
    if entry <= 0.0 or current <= 0.0 or quantity <= 0.0 or multiplier <= 0.0:
        return None
    per_unit = current - entry
    if str(direction or "").strip().lower() == "short":
        per_unit = -per_unit
    return round(per_unit * quantity * multiplier, 2)


def _load_system_prompt() -> str:
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _parse_llm_json(raw: str) -> dict[str, Any] | None:
    """Parse LLM JSON with recovery for truncated responses."""
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[1] if "\n" in cleaned else cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    repaired = _repair_truncated_json(cleaned)
    if repaired:
        try:
            result = json.loads(repaired)
            logger.info("[position_plan] Recovered truncated JSON (%d → %d chars)", len(cleaned), len(repaired))
            return result
        except json.JSONDecodeError:
            pass

    logger.warning("[position_plan] JSON repair failed, len=%d", len(cleaned))
    return None


def _repair_truncated_json(s: str) -> str | None:
    """Attempt to close a truncated JSON object so it parses.

    Strategy: walk the string tracking open braces/brackets/strings,
    then append the necessary closing tokens.  Also trims any trailing
    incomplete key-value pair to keep the structure valid.
    """
    if not s or s[0] != "{":
        return None

    stack: list[str] = []
    in_string = False
    escape = False
    last_complete_idx = 0

    for i, ch in enumerate(s):
        if escape:
            escape = False
            continue
        if ch == "\\":
            if in_string:
                escape = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if ch in ("{", "["):
            stack.append(ch)
            last_complete_idx = i
        elif ch == "}":
            if stack and stack[-1] == "{":
                stack.pop()
                last_complete_idx = i
        elif ch == "]":
            if stack and stack[-1] == "[":
                stack.pop()
                last_complete_idx = i
        elif ch == ",":
            last_complete_idx = i

    if not stack:
        return None

    truncated = s[:last_complete_idx + 1].rstrip().rstrip(",")

    closers = ""
    for opener in reversed(stack):
        if opener == "{":
            closers += "}"
        elif opener == "[":
            closers += "]"

    candidate = truncated + closers
    try:
        json.loads(candidate)
        return candidate
    except json.JSONDecodeError:
        pass

    trim_targets = [
        truncated.rfind(","),
        truncated.rfind("{") + 1,
        truncated.rfind("[") + 1,
    ]
    for pos in sorted(set(trim_targets), reverse=True):
        if pos <= 0:
            continue
        attempt = truncated[:pos].rstrip().rstrip(",") + closers
        try:
            json.loads(attempt)
            return attempt
        except json.JSONDecodeError:
            continue

    return None


def _build_position_context(
    db: Session,
    trades: list[Trade],
    quotes: dict[str, dict[str, Any]],
    trade_quotes: dict[int, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build per-position context dicts for the LLM."""
    try:
        from .autopilot_scope import is_option_trade
    except Exception:
        def is_option_trade(_trade: Trade) -> bool:  # type: ignore[no-redef]
            return False
    try:
        from .options.exit_monitor import _opt_meta
    except Exception:
        def _opt_meta(_trade: Trade) -> dict[str, Any]:  # type: ignore[no-redef]
            return {}

    trade_quotes = trade_quotes or {}
    trade_ids = [t.id for t in trades]
    alert_ids = [t.related_alert_id for t in trades if t.related_alert_id]
    pattern_ids = list({t.scan_pattern_id for t in trades if t.scan_pattern_id})

    alerts_by_id: dict[int, BreakoutAlert] = {}
    if alert_ids:
        for ba in db.query(BreakoutAlert).filter(BreakoutAlert.id.in_(alert_ids)).all():
            alerts_by_id[ba.id] = ba

    patterns_by_id: dict[int, ScanPattern] = {}
    if pattern_ids:
        for p in db.query(ScanPattern).filter(ScanPattern.id.in_(pattern_ids)).all():
            patterns_by_id[p.id] = p

    latest_decisions: dict[int, PatternMonitorDecision] = {}
    if trade_ids:
        for d in (
            db.query(PatternMonitorDecision)
            .filter(PatternMonitorDecision.trade_id.in_(trade_ids))
            .order_by(PatternMonitorDecision.created_at.desc())
            .all()
        ):
            if d.trade_id not in latest_decisions:
                latest_decisions[d.trade_id] = d

    positions = []
    for trade in trades:
        trade_is_option = is_option_trade(trade)
        q = trade_quotes.get(int(trade.id)) or {}
        if not q and not trade_is_option:
            q = quotes.get(trade.ticker.upper()) or quotes.get(trade.ticker) or {}
        cur_price = q.get("price") or q.get("last_price")
        try:
            cur_price = float(cur_price) if cur_price is not None else None
        except (TypeError, ValueError):
            cur_price = None

        entry = float(trade.entry_price) if trade.entry_price else 0
        pnl_pct = None
        if cur_price and entry:
            if trade.direction == "short":
                pnl_pct = round((entry - cur_price) / entry * 100, 2)
            else:
                pnl_pct = round((cur_price - entry) / entry * 100, 2)

        pat = patterns_by_id.get(trade.scan_pattern_id) if trade.scan_pattern_id else None
        alert = alerts_by_id.get(trade.related_alert_id) if trade.related_alert_id else None
        dec = latest_decisions.get(trade.id)
        quantity = _context_quantity(trade, trade_is_option=trade_is_option)
        multiplier = OPTION_CONTRACT_MULTIPLIER if trade_is_option else 1.0
        entry_value_usd = (
            round(entry * quantity * multiplier, 2)
            if entry > 0.0 and quantity is not None and quantity > 0.0
            else None
        )
        current_value_usd = (
            round(cur_price * quantity * multiplier, 2)
            if cur_price is not None
            and cur_price > 0.0
            and quantity is not None
            and quantity > 0.0
            else None
        )
        unrealized_pnl_usd = _signed_price_pnl(
            entry=entry,
            current=cur_price,
            quantity=quantity,
            direction=trade.direction,
            multiplier=multiplier,
        )
        raw_stop_loss = _positive_float_or_none(getattr(trade, "stop_loss", None))
        raw_take_profit = _positive_float_or_none(getattr(trade, "take_profit", None))
        price_domains = _trade_price_domains(trade)
        stop_loss = raw_stop_loss
        take_profit = raw_take_profit

        # Migration 227: bars-held is unit-aware via the pattern's
        # timeframe. Pre-fix this was wall-clock ``.days``, which lied
        # by 24x at 1h, 1440x at 1m -- the LLM saw a 5-minute scalper as
        # "0 days held" indefinitely. Falls back to 1d for orphan trades.
        bars_held = None
        if trade.entry_date:
            tf = (pat.timeframe if pat and pat.timeframe else "1d")
            try:
                from .timeframe_utils import timeframe_to_seconds
                tf_s = timeframe_to_seconds(tf)
            except ValueError:
                tf_s = 86400
            elapsed_s = (datetime.utcnow() - trade.entry_date).total_seconds()
            bars_held = max(0, int(elapsed_s // tf_s))

        pos = {
            "trade_id": trade.id,
            "ticker": trade.ticker,
            "asset_type": "options" if trade_is_option else (
                "crypto" if (trade.ticker or "").upper().endswith("-USD") else "stock"
            ),
            "direction": trade.direction or "long",
            "entry_price": entry,
            "current_price": cur_price,
            "pnl_pct": pnl_pct,
            "quantity": quantity,
            "entry_value_usd": entry_value_usd,
            "current_value_usd": current_value_usd,
            "unrealized_pnl_usd": unrealized_pnl_usd,
            "stop_loss": stop_loss,
            "take_profit": take_profit,
            "entry_date": trade.entry_date.isoformat() if trade.entry_date else None,
            "bars_held": bars_held,
            "sector": trade.sector,
            "trade_type": trade.trade_type,
            "notes": (trade.notes or "")[:200],
        }
        if q.get("source"):
            pos["quote_source"] = q.get("source")
        if q.get("quote_ts"):
            pos["quote_ts"] = q.get("quote_ts")

        if trade_is_option:
            opt_meta = _opt_meta(trade)
            stop_domain = price_domains.get("stop_loss")
            target_domain = price_domains.get("take_profit")
            if stop_domain == PRICE_DOMAIN_UNDERLYING_SPOT:
                pos["stop_loss"] = None
                pos["underlying_stop_loss"] = raw_stop_loss
            elif stop_domain == PRICE_DOMAIN_OPTION_PREMIUM:
                pos["premium_stop_loss"] = raw_stop_loss
            elif raw_stop_loss is not None:
                pos["untrusted_stop_loss"] = raw_stop_loss
                pos["stop_loss"] = None
            if target_domain == PRICE_DOMAIN_UNDERLYING_SPOT:
                pos["take_profit"] = None
                pos["underlying_take_profit"] = raw_take_profit
            elif target_domain == PRICE_DOMAIN_OPTION_PREMIUM:
                pos["premium_take_profit"] = raw_take_profit
            elif raw_take_profit is not None:
                pos["untrusted_take_profit"] = raw_take_profit
                pos["take_profit"] = None
            pos["contract_multiplier"] = OPTION_CONTRACT_MULTIPLIER
            pos["price_domain"] = PRICE_DOMAIN_OPTION_PREMIUM
            pos["price_domains"] = {
                "entry_price": PRICE_DOMAIN_OPTION_PREMIUM,
                "current_price": PRICE_DOMAIN_OPTION_PREMIUM,
                "stop_loss": stop_domain or "unknown",
                "take_profit": target_domain or "unknown",
            }
            pos["max_premium_at_risk_usd"] = entry_value_usd
            if quantity is None:
                pos["quantity_error"] = "invalid_option_contract_quantity"
            pos["option_meta"] = {
                "underlying": opt_meta.get("underlying") or trade.ticker,
                "expiration": opt_meta.get("expiration"),
                "strike": opt_meta.get("strike"),
                "option_type": opt_meta.get("option_type"),
            }

        if pat:
            pos["pattern_name"] = pat.name
            pos["pattern_timeframe"] = pat.timeframe
            pos["pattern_win_rate"] = float(pat.win_rate) if pat.win_rate else None

        if alert and alert.trade_plan:
            tp = alert.trade_plan
            pos["trade_plan_summary"] = {
                "entry_validation": tp.get("entry_validation", {}).get("method"),
                "key_levels": tp.get("key_levels"),
                "invalidation_count": len(tp.get("invalidation_conditions", [])),
            }

        if dec:
            pos["latest_monitor"] = {
                "action": dec.action,
                "health_score": round(float(dec.health_score) * 100, 1) if dec.health_score else None,
                "reasoning": (dec.llm_reasoning or "")[:150],
                "when": dec.created_at.isoformat() if dec.created_at else None,
            }

        positions.append(pos)

    return positions


def _position_plan_material_signature(
    portfolio_ctx: Mapping[str, Any],
    positions: list[dict[str, Any]],
) -> str:
    """Hash the material state that should drive a position-plan advisory.

    The signature ignores quote timestamps and prose while bucketing prices
    and PnL, so forced refreshes can reuse an advisory when the portfolio has
    not materially changed.
    """
    normalized_positions = []
    for pos in sorted(
        positions,
        key=lambda p: (str(p.get("ticker") or ""), int(p.get("trade_id") or 0)),
    ):
        latest_monitor = pos.get("latest_monitor") if isinstance(pos.get("latest_monitor"), Mapping) else {}
        option_meta = pos.get("option_meta") if isinstance(pos.get("option_meta"), Mapping) else {}
        normalized_positions.append({
            "trade_id": pos.get("trade_id"),
            "ticker": str(pos.get("ticker") or "").upper(),
            "asset_type": pos.get("asset_type"),
            "direction": pos.get("direction"),
            "entry_price": _bucket_price(pos.get("entry_price")),
            "current_price": _bucket_price(pos.get("current_price")),
            "pnl_pct": _bucket_number(pos.get("pnl_pct"), step=0.5),
            "quantity": _bucket_number(pos.get("quantity"), step=0.01),
            "stop_loss": _bucket_price(pos.get("stop_loss")),
            "take_profit": _bucket_price(pos.get("take_profit")),
            "premium_stop_loss": _bucket_price(pos.get("premium_stop_loss")),
            "premium_take_profit": _bucket_price(pos.get("premium_take_profit")),
            "underlying_stop_loss": _bucket_price(pos.get("underlying_stop_loss")),
            "underlying_take_profit": _bucket_price(pos.get("underlying_take_profit")),
            "bars_held": _bucket_bars(pos.get("bars_held")),
            "pattern_name": pos.get("pattern_name"),
            "pattern_timeframe": pos.get("pattern_timeframe"),
            "latest_monitor_action": latest_monitor.get("action"),
            "option_expiration": option_meta.get("expiration"),
            "option_strike": _bucket_price(option_meta.get("strike")),
            "option_type": option_meta.get("option_type"),
        })

    material = {
        "version": MATERIAL_SIGNATURE_VERSION,
        "portfolio": {
            "total_positions": portfolio_ctx.get("total_positions"),
            "regime": portfolio_ctx.get("regime"),
            "spy_direction": portfolio_ctx.get("spy_direction"),
            "vix": _bucket_number(portfolio_ctx.get("vix"), step=0.5),
            "vix_regime": portfolio_ctx.get("vix_regime"),
            "avg_pnl_pct": _bucket_number(portfolio_ctx.get("avg_pnl_pct"), step=0.5),
            "winning_count": portfolio_ctx.get("winning_count"),
            "losing_count": portfolio_ctx.get("losing_count"),
            "sector_breakdown": portfolio_ctx.get("sector_breakdown") or {},
        },
        "positions": normalized_positions,
    }
    payload = json.dumps(material, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _build_portfolio_context(
    positions: list[dict[str, Any]],
    regime: dict[str, Any],
) -> dict[str, Any]:
    """Build portfolio-level context for the LLM."""
    sectors: dict[str, int] = {}
    total_pnl_items = []
    for p in positions:
        s = p.get("sector") or "Unknown"
        sectors[s] = sectors.get(s, 0) + 1
        if p.get("pnl_pct") is not None:
            total_pnl_items.append(p["pnl_pct"])

    return {
        "total_positions": len(positions),
        "regime": regime.get("regime", "unknown"),
        "spy_direction": regime.get("spy_direction"),
        "vix": regime.get("vix"),
        "vix_regime": regime.get("vix_regime"),
        "sector_breakdown": sectors,
        "avg_pnl_pct": round(sum(total_pnl_items) / len(total_pnl_items), 2) if total_pnl_items else None,
        "winning_count": sum(1 for x in total_pnl_items if x > 0),
        "losing_count": sum(1 for x in total_pnl_items if x < 0),
    }


def _call_position_plan_llm(
    messages: list[dict[str, Any]],
    *,
    max_tokens: int,
    system_prompt: str,
) -> str:
    """Route deterministic position-plan prompts through cache + single-flight."""
    raw = call_llm(
        messages,
        max_tokens=max_tokens,
        trace_id="position-plan-generator",
        cacheable=True,
        purpose="position_plan_generator",
        system_prompt=system_prompt,
    )
    return raw if isinstance(raw, str) else str(raw or "")


def generate_position_plans(
    db: Session,
    user_id: int | None,
    *,
    force_refresh: bool = False,
) -> dict[str, Any]:
    """Evaluate all open positions and generate comprehensive plans.

    Returns dict with portfolio_summary and position_plans (from LLM),
    plus metadata (generated_at, trade_ids, stale flag).
    """
    trades = (
        db.query(Trade)
        .filter(Trade.status == "open", Trade.entry_price > 0)
    )
    if user_id is not None:
        trades = trades.filter(Trade.user_id == user_id)
    else:
        trades = trades.filter(Trade.user_id.is_(None))
    trades = trades.order_by(Trade.entry_date.desc()).all()

    if not trades:
        return {
            "ok": True,
            "portfolio_summary": {
                "total_positions": 0,
                "regime": "unknown",
                "overall_assessment": "No open positions to evaluate.",
                "concentration_warnings": [],
                "portfolio_heat": "low",
            },
            "position_plans": [],
            "generated_at": datetime.utcnow().isoformat(),
            "trade_ids": [],
        }

    if not force_refresh:
        cached = _get_cached_plans(db, user_id, [t.id for t in trades])
        if cached is not None:
            return cached

    try:
        from .autopilot_scope import is_option_trade
    except Exception:
        def is_option_trade(_trade: Trade) -> bool:  # type: ignore[no-redef]
            return False

    tickers = list({t.ticker.upper() for t in trades if not is_option_trade(t)})
    quotes: dict[str, dict[str, Any]] = {}
    if tickers:
        try:
            quotes = fetch_quotes_batch(tickers, allow_provider_fallback=True)
        except Exception:
            logger.warning("[position_plan] fetch_quotes_batch failed", exc_info=True)

    trade_quotes: dict[int, dict[str, Any]] = {}
    option_trades = [t for t in trades if is_option_trade(t)]
    if option_trades:
        try:
            from .broker_quotes import broker_quote_for_trade

            for trade in option_trades:
                q = broker_quote_for_trade(trade, purpose="display")
                if q and q.get("price") is not None:
                    trade_quotes[int(trade.id)] = q
        except Exception:
            logger.warning("[position_plan] option quote lookup failed", exc_info=True)

    regime = {}
    try:
        regime = get_market_regime()
    except Exception:
        logger.warning("[position_plan] get_market_regime failed", exc_info=True)

    positions = _build_position_context(db, trades, quotes, trade_quotes)
    portfolio_ctx = _build_portfolio_context(positions, regime)
    material_signature = _position_plan_material_signature(portfolio_ctx, positions)
    material_cached = _get_cached_plans_by_material_signature(
        db,
        user_id,
        [t.id for t in trades],
        material_signature,
    )
    if material_cached is not None:
        return material_cached

    user_msg = json.dumps({
        "portfolio": portfolio_ctx,
        "positions": positions,
    }, default=str, separators=(",", ":"))

    system_prompt = _load_system_prompt()
    messages = [{"role": "user", "content": user_msg}]

    max_tokens = min(8192, 400 * len(trades) + 600)
    raw = _call_position_plan_llm(
        messages,
        max_tokens=max_tokens,
        system_prompt=system_prompt,
    )

    if not raw:
        logger.error("[position_plan] LLM returned empty response")
        return {
            "ok": False,
            "error": "LLM returned empty response",
            "portfolio_summary": portfolio_ctx,
            "position_plans": [],
            "generated_at": datetime.utcnow().isoformat(),
            "trade_ids": [t.id for t in trades],
        }

    result = _parse_llm_json(raw)
    if result is None:
        logger.error("[position_plan] Failed to parse LLM JSON — raw[:500]: %s", raw[:500])
        return {
            "ok": False,
            "error": "Failed to parse LLM response (truncated or malformed)",
            "raw_response": raw[:2000],
            "portfolio_summary": portfolio_ctx,
            "position_plans": [],
            "generated_at": datetime.utcnow().isoformat(),
            "trade_ids": [t.id for t in trades],
        }

    generated_at = datetime.utcnow()

    ticker_to_trade = {t.ticker.upper(): t.id for t in trades}
    plans = result.get("position_plans", [])
    for p in plans:
        if not p.get("trade_id"):
            matched_id = ticker_to_trade.get((p.get("ticker") or "").upper())
            if matched_id:
                p["trade_id"] = matched_id

    result_for_persist = dict(result)
    result_for_persist["_chili_material_state"] = {
        "signature": material_signature,
        "version": MATERIAL_SIGNATURE_VERSION,
    }
    _persist_plans(db, user_id, [t.id for t in trades], result_for_persist, generated_at)

    return {
        "ok": True,
        "portfolio_summary": result.get("portfolio_summary", portfolio_ctx),
        "position_plans": plans,
        "generated_at": generated_at.isoformat(),
        "trade_ids": [t.id for t in trades],
    }


def _backfill_trade_ids_on_plans(db: Session, plans: list[dict], user_id: int | None) -> None:
    """Ensure every plan dict has trade_id by matching ticker to open trades."""
    missing = [p for p in plans if not p.get("trade_id")]
    if not missing:
        return
    try:
        q = db.query(Trade.id, Trade.ticker).filter(Trade.status == "open")
        if user_id is not None:
            q = q.filter(Trade.user_id == user_id)
        else:
            q = q.filter(Trade.user_id.is_(None))
        ticker_map = {row.ticker.upper(): row.id for row in q.all()}
    except Exception:
        return
    for p in missing:
        tid = ticker_map.get((p.get("ticker") or "").upper())
        if tid:
            p["trade_id"] = tid


def _cached_plan_from_row(
    db: Session,
    row: Any,
    user_id: int | None,
    *,
    cached_reason: str,
) -> dict[str, Any] | None:
    if not row:
        return None
    plan_json, gen_at, cached_tids = row
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    if isinstance(cached_tids, str):
        cached_tids = json.loads(cached_tids)
    if not isinstance(plan_json, Mapping):
        return None

    plans = plan_json.get("position_plans", [])
    if not isinstance(plans, list):
        plans = []
    _backfill_trade_ids_on_plans(db, plans, user_id)

    return {
        "ok": True,
        "portfolio_summary": plan_json.get("portfolio_summary", {}),
        "position_plans": plans,
        "generated_at": gen_at.isoformat() if hasattr(gen_at, "isoformat") else str(gen_at),
        "trade_ids": cached_tids,
        "cached": True,
        "cache_reason": cached_reason,
    }


def _get_cached_plans(
    db: Session,
    user_id: int | None,
    trade_ids: list[int],
) -> dict[str, Any] | None:
    """Return cached plans if fresh enough, else None."""
    cutoff = datetime.utcnow() - timedelta(hours=PLAN_STALE_HOURS)
    try:
        uid_clause = "user_id = :uid" if user_id is not None else "user_id IS NULL"
        row = db.execute(
            text(
                f"SELECT plan_json, generated_at, trade_ids FROM trading_position_plans "
                f"WHERE {uid_clause} AND generated_at > :cutoff "
                f"ORDER BY generated_at DESC LIMIT 1"
            ),
            {"uid": user_id, "cutoff": cutoff},
        ).fetchone()
    except Exception:
        return None

    if not row:
        return None

    plan_json, _gen_at, cached_tids = row
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    if isinstance(cached_tids, str):
        cached_tids = json.loads(cached_tids)

    if set(cached_tids or []) != set(trade_ids):
        return None

    return _cached_plan_from_row(db, row, user_id, cached_reason="fresh_trade_set")


def _get_cached_plans_by_material_signature(
    db: Session,
    user_id: int | None,
    trade_ids: list[int],
    material_signature: str,
) -> dict[str, Any] | None:
    """Return cached plans when a forced refresh sees unchanged material state."""
    if not material_signature:
        return None
    cutoff = datetime.utcnow() - timedelta(hours=PLAN_STALE_HOURS)
    try:
        uid_clause = "user_id = :uid" if user_id is not None else "user_id IS NULL"
        row = db.execute(
            text(
                f"SELECT plan_json, generated_at, trade_ids FROM trading_position_plans "
                f"WHERE {uid_clause} AND generated_at > :cutoff "
                f"ORDER BY generated_at DESC LIMIT 1"
            ),
            {"uid": user_id, "cutoff": cutoff},
        ).fetchone()
    except Exception:
        return None

    if not row:
        return None

    plan_json, _gen_at, cached_tids = row
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    if isinstance(cached_tids, str):
        cached_tids = json.loads(cached_tids)
    if set(cached_tids or []) != set(trade_ids):
        return None

    meta = plan_json.get("_chili_material_state") if isinstance(plan_json, Mapping) else None
    if not isinstance(meta, Mapping):
        return None
    if meta.get("signature") != material_signature:
        return None

    return _cached_plan_from_row(db, row, user_id, cached_reason="material_state_unchanged")


def _persist_plans(
    db: Session,
    user_id: int | None,
    trade_ids: list[int],
    plan_result: dict[str, Any],
    generated_at: datetime,
) -> None:
    """Upsert the latest position plans into the DB."""
    try:
        uid_clause = "user_id = :uid" if user_id is not None else "user_id IS NULL"
        db.execute(
            text(f"DELETE FROM trading_position_plans WHERE {uid_clause}"),
            {"uid": user_id},
        )
        db.execute(
            text(
                "INSERT INTO trading_position_plans (user_id, trade_ids, plan_json, generated_at) "
                "VALUES (:uid, :tids, :plan, :gen_at)"
            ),
            {
                "uid": user_id,
                "tids": json.dumps(trade_ids),
                "plan": json.dumps(plan_result, default=str),
                "gen_at": generated_at,
            },
        )
        db.commit()
    except Exception:
        logger.warning("[position_plan] Failed to persist plans", exc_info=True)
        try:
            db.rollback()
        except Exception:
            pass


def get_latest_plans(db: Session, user_id: int | None) -> dict[str, Any] | None:
    """Retrieve the most recent plans without generating new ones."""
    try:
        uid_clause = "user_id = :uid" if user_id is not None else "user_id IS NULL"
        row = db.execute(
            text(
                f"SELECT plan_json, generated_at, trade_ids FROM trading_position_plans "
                f"WHERE {uid_clause} ORDER BY generated_at DESC LIMIT 1"
            ),
            {"uid": user_id},
        ).fetchone()
    except Exception:
        return None

    if not row:
        return None

    plan_json, gen_at, cached_tids = row
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    if isinstance(cached_tids, str):
        cached_tids = json.loads(cached_tids)

    stale = (datetime.utcnow() - gen_at).total_seconds() > PLAN_STALE_HOURS * 3600 if gen_at else True

    plans = plan_json.get("position_plans", [])
    _backfill_trade_ids_on_plans(db, plans, user_id)

    return {
        "ok": True,
        "portfolio_summary": plan_json.get("portfolio_summary", {}),
        "position_plans": plans,
        "generated_at": gen_at.isoformat() if hasattr(gen_at, "isoformat") else str(gen_at),
        "trade_ids": cached_tids,
        "stale": stale,
    }
