"""Built-in fire handlers for action-layer mesh nodes.

``nm_action_signals`` is the single Telegram dispatch authority:
when it fires, it reads children's local_state, decides urgency/tier,
and calls dispatch_alert if warranted.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
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


def _classify_urgency(children: dict[str, dict[str, Any]]) -> tuple[str, str, dict[str, Any]]:
    """Scan children's local_state for the highest-urgency signal.

    Returns (urgency, action, best_child_state).
    urgency is one of: critical, warning, info, none.
    """
    best_urgency = "none"
    best_action = "hold"
    best_child: dict[str, Any] = {}
    urgency_rank = {"critical": 3, "warning": 2, "info": 1, "none": 0}

    for child_id, state in children.items():
        if not state:
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
        if s and s.get("action") or s.get("alert_event")
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
        ticker = best_child.get("ticker")
        user_id = best_child.get("user_id")
        alert_type = _map_action_to_alert_type(action)
        msg = _format_mesh_alert_message(urgency, action, best_child, children)

        try:
            from ..alerts import dispatch_alert
            dispatch_alert(
                db,
                user_id=user_id,
                alert_type=alert_type,
                ticker=ticker,
                message=msg,
                skip_throttle=True,
            )
            decision["dispatched"] = True
            decision["alert_type"] = alert_type
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
