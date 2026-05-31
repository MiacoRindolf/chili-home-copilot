"""Compact attribution payload for live/broker trade-close ledger outcomes."""

from __future__ import annotations

from typing import Any

from ....models.trading import Trade
from ..return_math import trade_return_pct


def _finite_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out and out not in (float("inf"), float("-inf")) else None


def _tca_cost_pct(trade: Trade) -> float | None:
    entry_bps = _finite_float(getattr(trade, "tca_entry_slippage_bps", None))
    exit_bps = _finite_float(getattr(trade, "tca_exit_slippage_bps", None))
    if entry_bps is None or exit_bps is None:
        return None
    return round((entry_bps + exit_bps) / 100.0, 6)


def trade_close_attribution_dict(trade: Trade) -> dict[str, Any]:
    """Fields for execution feedback and operator visibility (bounded strings)."""
    oid = getattr(trade, "broker_order_id", None) or ""
    realized_return_pct = trade_return_pct(trade)
    tca_cost_pct = _tca_cost_pct(trade)
    return {
        "scan_pattern_id": getattr(trade, "scan_pattern_id", None),
        "strategy_proposal_id": getattr(trade, "strategy_proposal_id", None),
        "pnl": trade.pnl,
        "realized_return_pct": (
            round(realized_return_pct, 6)
            if realized_return_pct is not None
            else None
        ),
        "tca_cost_pct": tca_cost_pct,
        "net_return_pct": (
            round(realized_return_pct - tca_cost_pct, 6)
            if realized_return_pct is not None and tca_cost_pct is not None
            else None
        ),
        "exit_price": trade.exit_price,
        "entry_price": trade.entry_price,
        "quantity": trade.quantity,
        "direction": (trade.direction or "").strip() or None,
        "broker_source": (trade.broker_source or "").strip() or None,
        "broker_order_id": (str(oid)[:96] if oid else None),
        "tca_entry_slippage_bps": getattr(trade, "tca_entry_slippage_bps", None),
        "tca_exit_slippage_bps": getattr(trade, "tca_exit_slippage_bps", None),
    }
