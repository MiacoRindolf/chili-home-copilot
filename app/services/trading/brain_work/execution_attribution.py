"""Compact attribution payloads for trade-close ledger outcomes."""

from __future__ import annotations

from typing import Any

from ....models.trading import PaperTrade, Trade
from ..execution_cost_builder import _usable_tca_bps
from ..return_math import paper_trade_return_pct, trade_return_pct


def _tca_cost_pct(trade: Trade) -> float | None:
    entry_bps = _usable_tca_bps(trade, "tca_entry_slippage_bps")
    exit_bps = _usable_tca_bps(trade, "tca_exit_slippage_bps")
    if entry_bps is None or exit_bps is None:
        return None
    return round((entry_bps + exit_bps) / 100.0, 6)


def _tca_bps(trade: Trade, attr: str) -> float | None:
    return _usable_tca_bps(trade, attr)


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
        "tca_entry_slippage_bps": _tca_bps(trade, "tca_entry_slippage_bps"),
        "tca_exit_slippage_bps": _tca_bps(trade, "tca_exit_slippage_bps"),
    }


def paper_trade_close_attribution_dict(paper_trade: PaperTrade) -> dict[str, Any]:
    """Contract-aware paper/shadow close fields for promotion feedback."""
    realized_return_pct = paper_trade_return_pct(paper_trade)
    tca_cost_pct = _tca_cost_pct(paper_trade)
    return {
        "scan_pattern_id": getattr(paper_trade, "scan_pattern_id", None),
        "paper_shadow_of_alert_id": getattr(
            paper_trade,
            "paper_shadow_of_alert_id",
            None,
        ),
        "pnl": getattr(paper_trade, "pnl", None),
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
        "exit_price": getattr(paper_trade, "exit_price", None),
        "entry_price": getattr(paper_trade, "entry_price", None),
        "quantity": getattr(paper_trade, "quantity", None),
        "direction": (getattr(paper_trade, "direction", "") or "").strip() or None,
        "exit_reason": (
            getattr(paper_trade, "exit_reason", "") or ""
        ).strip() or None,
        "tca_entry_slippage_bps": _tca_bps(
            paper_trade,
            "tca_entry_slippage_bps",
        ),
        "tca_exit_slippage_bps": _tca_bps(
            paper_trade,
            "tca_exit_slippage_bps",
        ),
    }
