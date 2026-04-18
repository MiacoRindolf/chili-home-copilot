"""Shared Autopilot desk / monitor eligibility helpers."""
from __future__ import annotations

from sqlalchemy import or_

from ...models.trading import Trade


def live_autopilot_trade_filter():
    """SQLAlchemy filter for live trades surfaced on the Autopilot desk.

    The desk and live execution monitor should agree on the same scope:

    * AutoTrader v1 rows
    * Pattern-linked rows (scan pattern or breakout alert)
    * AI/manual plan-level rows with a saved stop or target
    """
    return or_(
        Trade.auto_trader_version == "v1",
        Trade.scan_pattern_id.isnot(None),
        Trade.related_alert_id.isnot(None),
        Trade.stop_loss.isnot(None),
        Trade.take_profit.isnot(None),
    )


def classify_live_autopilot_trade_scope(trade: Trade) -> str:
    """Return the operator-facing scope label for a live trade."""
    if trade.related_alert_id is not None or trade.scan_pattern_id is not None:
        return "pattern_linked"
    if (trade.auto_trader_version or "") == "v1":
        return "autotrader_v1"
    if trade.stop_loss is not None or trade.take_profit is not None:
        return "plan_levels"
    return "other"


def is_live_autopilot_trade(trade: Trade) -> bool:
    return classify_live_autopilot_trade_scope(trade) != "other"
