"""Bulk position plan generator: LLM-powered evaluation of all open positions."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...models.trading import BreakoutAlert, PatternMonitorDecision, ScanPattern, Trade
from ..llm_caller import call_llm
from .market_data import fetch_quotes_batch, get_market_regime

logger = logging.getLogger(__name__)

_PROMPT_PATH = Path(__file__).resolve().parents[2] / "prompts" / "position_plan.txt"
PLAN_STALE_HOURS = 4


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
) -> list[dict[str, Any]]:
    """Build per-position context dicts for the LLM."""
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

        days_held = (datetime.utcnow() - trade.entry_date).days if trade.entry_date else None

        pos = {
            "trade_id": trade.id,
            "ticker": trade.ticker,
            "direction": trade.direction or "long",
            "entry_price": entry,
            "current_price": cur_price,
            "pnl_pct": pnl_pct,
            "quantity": float(trade.quantity) if trade.quantity else 1.0,
            "stop_loss": float(trade.stop_loss) if trade.stop_loss else None,
            "take_profit": float(trade.take_profit) if trade.take_profit else None,
            "entry_date": trade.entry_date.isoformat() if trade.entry_date else None,
            "days_held": days_held,
            "sector": trade.sector,
            "trade_type": trade.trade_type,
            "notes": (trade.notes or "")[:200],
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

    tickers = list({t.ticker.upper() for t in trades})
    quotes: dict[str, dict[str, Any]] = {}
    try:
        quotes = fetch_quotes_batch(tickers, allow_provider_fallback=True)
    except Exception:
        logger.warning("[position_plan] fetch_quotes_batch failed", exc_info=True)

    regime = {}
    try:
        regime = get_market_regime()
    except Exception:
        logger.warning("[position_plan] get_market_regime failed", exc_info=True)

    positions = _build_position_context(db, trades, quotes)
    portfolio_ctx = _build_portfolio_context(positions, regime)

    user_msg = json.dumps({
        "portfolio": portfolio_ctx,
        "positions": positions,
    }, default=str, separators=(",", ":"))

    system_prompt = _load_system_prompt()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    max_tokens = min(8192, 400 * len(trades) + 600)
    raw = call_llm(messages, max_tokens=max_tokens, trace_id="position-plan-generator")

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

    _persist_plans(db, user_id, [t.id for t in trades], result, generated_at)

    return {
        "ok": True,
        "portfolio_summary": result.get("portfolio_summary", portfolio_ctx),
        "position_plans": result.get("position_plans", []),
        "generated_at": generated_at.isoformat(),
        "trade_ids": [t.id for t in trades],
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

    plan_json, gen_at, cached_tids = row
    if isinstance(plan_json, str):
        plan_json = json.loads(plan_json)
    if isinstance(cached_tids, str):
        cached_tids = json.loads(cached_tids)

    if set(cached_tids or []) != set(trade_ids):
        return None

    return {
        "ok": True,
        "portfolio_summary": plan_json.get("portfolio_summary", {}),
        "position_plans": plan_json.get("position_plans", []),
        "generated_at": gen_at.isoformat() if hasattr(gen_at, "isoformat") else str(gen_at),
        "trade_ids": cached_tids,
        "cached": True,
    }


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

    return {
        "ok": True,
        "portfolio_summary": plan_json.get("portfolio_summary", {}),
        "position_plans": plan_json.get("position_plans", []),
        "generated_at": gen_at.isoformat() if hasattr(gen_at, "isoformat") else str(gen_at),
        "trade_ids": cached_tids,
        "stale": stale,
    }
