"""Built-in fire handlers for action-layer mesh nodes.

``nm_action_signals`` is the single Telegram dispatch authority:
when it fires, it reads children's local_state, decides urgency/tier,
and calls dispatch_alert if warranted.
"""
from __future__ import annotations

import json
import logging
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....models.trading import BrainNodeState
from .schema import LOG_PREFIX

_log = logging.getLogger(__name__)

_CRITICAL_ACTIONS = frozenset({"exit_now", "STOP_HIT", "stop_hit", "TIME_EXIT"})
_WARNING_ACTIONS = frozenset({
    "tighten_stop", "STOP_TIGHTENED", "stop_tightened",
    "STOP_APPROACHING", "stop_approaching",
})
_INFO_ACTIONS = frozenset({
    "hold", "BREAKEVEN_REACHED", "breakeven_reached",
    "loosen_target",
})
DEFAULT_MESH_CRITICAL_DISPATCH_COOLDOWN_SECONDS = 4 * 3600
DEFAULT_MESH_CHILD_STATE_MAX_AGE_SECONDS = 15 * 60
_LAST_CRITICAL_DISPATCH_AT: dict[str, datetime] = {}


def _critical_dispatch_cooldown_seconds() -> int:
    try:
        from ....config import settings

        return int(
            getattr(
                settings,
                "chili_mesh_critical_alert_cooldown_seconds",
                DEFAULT_MESH_CRITICAL_DISPATCH_COOLDOWN_SECONDS,
            )
            or DEFAULT_MESH_CRITICAL_DISPATCH_COOLDOWN_SECONDS
        )
    except Exception:
        return DEFAULT_MESH_CRITICAL_DISPATCH_COOLDOWN_SECONDS


def _mesh_child_state_max_age_seconds() -> int:
    try:
        from ....config import settings

        return int(
            getattr(
                settings,
                "chili_mesh_child_state_max_age_seconds",
                DEFAULT_MESH_CHILD_STATE_MAX_AGE_SECONDS,
            )
            or DEFAULT_MESH_CHILD_STATE_MAX_AGE_SECONDS
        )
    except Exception:
        return DEFAULT_MESH_CHILD_STATE_MAX_AGE_SECONDS


def _child_state_updated_at(state: dict[str, Any]) -> datetime | None:
    raw = state.get("updated_at")
    if not raw:
        return None
    try:
        if isinstance(raw, datetime):
            parsed = raw
        else:
            parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        return None


def _child_state_stale_for_action(state: dict[str, Any], *, now: datetime) -> bool:
    updated_at = _child_state_updated_at(state)
    if updated_at is None:
        return False
    return (now - updated_at).total_seconds() > max(0, _mesh_child_state_max_age_seconds())


def _signature_value(value: Any) -> str:
    if value in (None, ""):
        return ""
    try:
        out = float(value)
    except (TypeError, ValueError):
        return str(value).strip()
    if not math.isfinite(out):
        return ""
    return f"{out:.8g}"


def _pending_exit_suppression_snapshot(trade: Any) -> dict[str, Any] | None:
    status = str(getattr(trade, "pending_exit_status", "") or "").strip().lower()
    reason = str(getattr(trade, "pending_exit_reason", "") or "").strip().lower()
    if status in {"submitted", "working", "queued", "pending"}:
        return {
            "id": int(getattr(trade, "id", 0) or 0),
            "ticker": getattr(trade, "ticker", None),
            "reason": "pending_exit_already_active",
            "broker_truth_status": "pending_exit",
            "stale_broker_position": False,
            "pending_exit_status": getattr(trade, "pending_exit_status", None),
            "pending_exit_reason": getattr(trade, "pending_exit_reason", None),
            "pending_exit_order_id": getattr(trade, "pending_exit_order_id", None),
        }
    if status == "deferred" and reason == "missing_broker_qty":
        backoff_meta = None
        try:
            from ..stop_engine import _crypto_missing_qty_backoff_active

            backoff_meta = _crypto_missing_qty_backoff_active(trade)
        except Exception:
            backoff_meta = None
        return {
            "id": int(getattr(trade, "id", 0) or 0),
            "ticker": getattr(trade, "ticker", None),
            "reason": (
                "crypto_missing_broker_qty_backoff"
                if backoff_meta is not None
                else "crypto_missing_broker_qty_deferred"
            ),
            "broker_truth_status": "pending_exit",
            "stale_broker_position": False,
            "pending_exit_status": getattr(trade, "pending_exit_status", None),
            "pending_exit_reason": getattr(trade, "pending_exit_reason", None),
            "pending_exit_order_id": getattr(trade, "pending_exit_order_id", None),
            "backoff_until": (
                backoff_meta.get("backoff_until") if isinstance(backoff_meta, dict) else None
            ),
            "missing_qty_streak": int(
                getattr(trade, "crypto_broker_zero_qty_streak", 0) or 0
            ),
        }
    return None


def _critical_trade_broker_live(
    db: Session,
    child_state: dict[str, Any],
) -> tuple[bool, dict[str, Any] | None]:
    trade_id = child_state.get("trade_id")
    if not trade_id:
        return True, None
    try:
        from ....models.trading import Trade
        from ..broker_position_truth import broker_stale_open_trade_snapshot

        trade = db.get(Trade, int(trade_id))
        if trade is None:
            return False, {
                "id": int(trade_id),
                "reason": "local_trade_missing",
                "broker_truth_status": "stale",
                "stale_broker_position": True,
            }
        if getattr(trade, "status", None) != "open":
            return False, {
                "id": int(trade_id),
                "ticker": getattr(trade, "ticker", None),
                "reason": "local_trade_not_open",
                "broker_truth_status": "stale",
                "stale_broker_position": True,
            }
        stale = broker_stale_open_trade_snapshot(db, trade)
        if stale:
            return False, stale
        pending_exit = _pending_exit_suppression_snapshot(trade)
        if pending_exit:
            return False, pending_exit
        return True, None
    except Exception:
        _log.debug("%s critical broker-live revalidation failed", LOG_PREFIX, exc_info=True)
        return True, None


def _critical_dispatch_signature(action: str, child_state: dict[str, Any]) -> str:
    return "|".join(
        [
            "trade_critical",
            str(child_state.get("trade_id") or ""),
            str(child_state.get("ticker") or "").upper(),
            str(action or "").strip().lower(),
            _signature_value(child_state.get("stop_level") or child_state.get("new_stop")),
        ]
    )


def _critical_dispatch_in_cooldown(
    db: Session,
    signature: str,
    now: datetime,
) -> bool:
    cooldown = _critical_dispatch_cooldown_seconds()
    last = _LAST_CRITICAL_DISPATCH_AT.get(signature)
    if last is not None and (now - last).total_seconds() < max(0, cooldown):
        return True
    try:
        from ....models.trading import AlertHistory

        now_utc = now.astimezone(timezone.utc).replace(tzinfo=None)
        cutoff = now_utc - timedelta(seconds=max(0, cooldown))
        exists = (
            db.query(AlertHistory.id)
            .filter(AlertHistory.content_signature == signature)
            .filter(AlertHistory.created_at >= cutoff)
            .order_by(AlertHistory.created_at.desc())
            .first()
        )
        return exists is not None
    except Exception:
        _log.debug("%s critical cooldown lookup failed", LOG_PREFIX, exc_info=True)
        return False


def _classify_urgency(children: dict[str, dict[str, Any]]) -> tuple[str, str, dict[str, Any]]:
    """Scan children's local_state for the highest-urgency signal.

    Returns (urgency, action, best_child_state).
    urgency is one of: critical, warning, info, none.
    """
    best_urgency = "none"
    best_action = "hold"
    best_child: dict[str, Any] = {}
    urgency_rank = {"critical": 3, "warning": 2, "info": 1, "none": 0}
    now = datetime.now(timezone.utc)

    for child_id, state in children.items():
        if not state:
            continue
        if _child_state_stale_for_action(state, now=now):
            continue

        child_urgency = str(state.get("urgency", "none")).lower()
        child_action = str(state.get("action", state.get("alert_event", "hold"))).strip()

        if child_action in _CRITICAL_ACTIONS:
            child_urgency = "critical"
        elif child_action in _WARNING_ACTIONS and child_urgency != "critical":
            child_urgency = "warning"
        elif child_action in _INFO_ACTIONS and child_urgency not in ("critical", "warning"):
            child_urgency = "info"

        if urgency_rank.get(child_urgency, 0) > urgency_rank.get(best_urgency, 0):
            best_urgency = child_urgency
            best_action = child_action
            best_child = state

    return best_urgency, best_action, best_child


def _format_mesh_alert_message(
    urgency: str,
    action: str,
    child_state: dict[str, Any],
    children: dict[str, Any],
) -> str:
    """Build a Telegram message from aggregated mesh context."""
    ticker = child_state.get("ticker", "???")
    reason = child_state.get("reason", child_state.get("reasoning", ""))
    price = child_state.get("price", child_state.get("current_price", 0))

    header = {
        "critical": f"🚨 CRITICAL: {ticker}",
        "warning": f"⚠️ {ticker}",
        "info": f"ℹ️ {ticker}",
    }.get(urgency, f"📊 {ticker}")

    lines = [header]
    lines.append(f"Action: {action}")
    if price:
        lines.append(f"Price: ${price}")

    stop = child_state.get("stop_level", child_state.get("new_stop"))
    if stop:
        lines.append(f"Stop: ${stop}")

    health = child_state.get("health_score")
    if health is not None:
        lines.append(f"Health: {float(health):.0%}")

    if reason:
        lines.append(f"Reason: {str(reason)[:200]}")

    source_nodes = [
        nid for nid, s in children.items()
        if s and (s.get("action") or s.get("alert_event"))
    ]
    if source_nodes:
        lines.append(f"Sources: {', '.join(source_nodes)}")

    lines.append(f"[Mesh Decision @ {datetime.now(timezone.utc).strftime('%H:%M UTC')}]")
    return "\n".join(lines)


def handle_action_signals(
    db: Session,
    node_id: str,
    state: BrainNodeState,
    context: dict[str, Any],
) -> Optional[dict[str, Any]]:
    """nm_action_signals fire handler — the single Telegram dispatch authority.

    Reads children's local_state, classifies urgency, dispatches alert
    only for critical signals. Writes decision to own local_state.
    """
    children = context.get("children_state", {})
    urgency, action, best_child = _classify_urgency(children)

    decision = {
        "urgency": urgency,
        "action": action,
        "ticker": best_child.get("ticker"),
        "decided_at": datetime.now(timezone.utc).isoformat(),
        "children_summary": {
            nid: {
                "action": s.get("action", s.get("alert_event", "none")),
                "urgency": s.get("urgency", "none"),
                "ticker": s.get("ticker"),
            }
            for nid, s in children.items()
            if s
        },
    }

    state.local_state = decision
    state.updated_at = datetime.now(timezone.utc)

    if urgency == "critical":
        broker_live, stale_snapshot = _critical_trade_broker_live(db, best_child)
        if not broker_live:
            is_stale = bool((stale_snapshot or {}).get("stale_broker_position"))
            decision.update(
                {
                    "urgency": "none",
                    "action": (
                        "suppressed_stale_broker_position"
                        if is_stale
                        else "suppressed_non_actionable_trade_state"
                    ),
                    "dispatched": False,
                    "suppressed_reason": (
                        "stale_broker_position"
                        if is_stale
                        else (stale_snapshot or {}).get("reason")
                    ),
                    "broker_truth": stale_snapshot,
                }
            )
            state.local_state = decision
            _log.warning(
                "%s critical alert suppressed for non-actionable trade state: %s",
                LOG_PREFIX,
                stale_snapshot,
            )
            return decision

        ticker = best_child.get("ticker")
        user_id = best_child.get("user_id")
        alert_type = _map_action_to_alert_type(action)
        msg = _format_mesh_alert_message(urgency, action, best_child, children)
        now = datetime.now(timezone.utc)
        dispatch_sig = _critical_dispatch_signature(action, best_child)
        if _critical_dispatch_in_cooldown(db, dispatch_sig, now):
            decision.update(
                {
                    "dispatched": False,
                    "suppressed_reason": "critical_dispatch_cooldown",
                    "dispatch_signature": dispatch_sig,
                }
            )
            state.local_state = decision
            return decision

        try:
            from ..alerts import dispatch_alert
            dispatch_alert(
                db,
                user_id=user_id,
                alert_type=alert_type,
                ticker=ticker,
                message=msg,
                skip_throttle=True,
                content_signature=dispatch_sig,
            )
            decision["dispatched"] = True
            decision["alert_type"] = alert_type
            decision["dispatch_signature"] = dispatch_sig
            _LAST_CRITICAL_DISPATCH_AT[dispatch_sig] = now
            state.local_state = decision
        except Exception:
            _log.exception("%s alert dispatch failed for %s", LOG_PREFIX, ticker)
            decision["dispatched"] = False
            decision["dispatch_error"] = True
            state.local_state = decision

        _log.info(
            "%s CRITICAL alert dispatched: %s %s (%s)",
            LOG_PREFIX, ticker, action, urgency,
        )
    else:
        _log.debug(
            "%s action_signals: urgency=%s action=%s — no Telegram",
            LOG_PREFIX, urgency, action,
        )

    return decision


def _map_action_to_alert_type(action: str) -> str:
    """Map mesh action to alerts.py alert type constant."""
    action_lower = action.lower().replace(" ", "_")
    mapping = {
        "exit_now": "pattern_monitor",
        "stop_hit": "stop_hit",
        "time_exit": "stop_hit",
        "stop_tightened": "stop_tightened",
        "stop_approaching": "stop_approaching",
        "target_hit": "target_hit",
        "breakeven_reached": "breakeven_reached",
    }
    return mapping.get(action_lower, "pattern_monitor")


def register_builtin_handlers() -> None:
    """Register all built-in fire handlers. Called once at startup."""
    from .handlers import register_handler
    register_handler("nm_action_signals", handle_action_signals)

    try:
        from .trade_context_aggregator import register_trade_context_handler
        register_trade_context_handler()
    except Exception:
        _log.debug("%s trade_context handler deferred", LOG_PREFIX, exc_info=True)

    _log.info("%s built-in handlers registered", LOG_PREFIX)
