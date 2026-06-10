"""Broker-connectivity alarm — a configured broker must never be silently dead.

Robinhood's refresh token expired ~2026-04-19 and every refresh attempt logged a
quiet ``invalid_grant`` info line for ~7 WEEKS while live equity trading was
impossible (post-mortem 2026-06-10). Log spam is not an alarm: this watch raises a
LOUD, deduplicated alert when a configured broker stays disconnected past the
threshold, and an explicit all-clear when it reconnects.

Surfaces: ``logger.critical`` (operator log review) + the live-trading WebSocket
broadcast (UI toast/feed via ``alert_broadcast``). State is in-process per episode —
a scheduler restart re-arms the timer, which simply re-alerts after the threshold if
the broker is still down (a useful reminder, not a bug).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..broker_manager import get_all_broker_statuses

logger = logging.getLogger(__name__)

# broker -> epoch seconds when first observed configured-but-disconnected
_disconnect_since: dict[str, float] = {}
# broker -> True once the sustained-disconnect alert has fired for this episode
_alerted: dict[str, bool] = {}

_REAUTH_HINT = {
    "robinhood": "Re-authenticate Robinhood (login + MFA) from the broker settings page.",
    "coinbase": "Check the Coinbase Advanced API key (scope/expiry) in broker settings.",
}


def _alarm_threshold_seconds() -> float:
    from ...config import settings

    try:
        mins = float(getattr(settings, "chili_broker_disconnect_alarm_minutes", 15.0) or 15.0)
    except (TypeError, ValueError):
        mins = 15.0
    return max(60.0, mins * 60.0)


def _broadcast(payload: dict[str, Any]) -> None:
    try:
        from .alert_broadcast import broadcast_alert_sync

        broadcast_alert_sync(payload)
    except Exception:
        logger.debug("[broker_watch] broadcast failed", exc_info=True)


def run_broker_connectivity_watch(*, now: float | None = None) -> dict[str, Any]:
    """One watch pass. Returns a summary dict (also handy for tests)."""
    ts = time.time() if now is None else float(now)
    out: dict[str, Any] = {"checked": 0, "alerted": [], "resolved": [], "down": []}
    try:
        brokers = get_all_broker_statuses()
    except Exception:
        logger.warning("[broker_watch] status fetch failed", exc_info=True)
        return out

    for name in ("robinhood", "coinbase"):
        st = brokers.get(name) or {}
        if not st.get("configured"):
            continue
        out["checked"] += 1
        if st.get("connected"):
            if _disconnect_since.pop(name, None) is not None and _alerted.pop(name, False):
                logger.warning("[broker_watch] %s RECONNECTED — alarm cleared", name)
                _broadcast({
                    "type": "ops_alert",
                    "severity": "resolved",
                    "broker": name,
                    "message": f"{name.title()} reconnected — broker alarm cleared.",
                })
                out["resolved"].append(name)
            continue

        since = _disconnect_since.setdefault(name, ts)
        down_s = ts - since
        out["down"].append({"broker": name, "down_seconds": round(down_s, 0)})
        if down_s >= _alarm_threshold_seconds() and not _alerted.get(name):
            _alerted[name] = True
            mins = int(down_s // 60)
            msg = (
                f"{name.title()} is CONFIGURED but DISCONNECTED for {mins}+ min — live "
                f"trading on this venue is impossible. {_REAUTH_HINT.get(name, 'Re-authenticate the broker.')}"
            )
            logger.critical("[broker_watch] %s", msg)
            _broadcast({
                "type": "ops_alert",
                "severity": "critical",
                "alert_type": "broker_disconnect",
                "broker": name,
                "down_minutes": mins,
                "message": msg,
            })
            out["alerted"].append(name)
    return out


def reset_for_tests() -> None:
    _disconnect_since.clear()
    _alerted.clear()
