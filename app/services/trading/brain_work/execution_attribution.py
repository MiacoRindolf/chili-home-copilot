"""Compact attribution payload for live/broker trade-close ledger outcomes."""

from __future__ import annotations

from typing import Any

from ....models.trading import Trade


def trade_close_attribution_dict(trade: Trade) -> dict[str, Any]:
    """Fields for execution feedback and operator visibility (bounded strings)."""
    oid = getattr(trade, "broker_order_id", None) or ""
    return {
        "scan_pattern_id": getattr(trade, "scan_pattern_id", None),
        "strategy_proposal_id": getattr(trade, "strategy_proposal_id", None),
        "pnl": trade.pnl,
        "exit_price": trade.exit_price,
        "entry_price": trade.entry_price,
        "quantity": trade.quantity,
        "direction": (trade.direction or "").strip() or None,
        "broker_source": (trade.broker_source or "").strip() or None,
        "broker_order_id": (str(oid)[:96] if oid else None),
        "tca_entry_slippage_bps": getattr(trade, "tca_entry_slippage_bps", None),
        "tca_exit_slippage_bps": getattr(trade, "tca_exit_slippage_bps", None),
    }
