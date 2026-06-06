"""Coinbase stale-close exit-price resolution (data-first: partial-coverage VWAP).

When a Coinbase position is broker-gone but only PARTIAL real sell fills are
recoverable, ``_coinbase_stale_close_fill`` now prices the whole close at the
observed-fill VWAP (a real traded price) instead of discarding it to pnl=NULL /
broker_reconcile_no_exit_price. Below the coverage floor the exit price stays
unknown -- never fabricated.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch


def _trade(qty: float = 10.0, ticker: str = "ETH-USD") -> SimpleNamespace:
    return SimpleNamespace(
        ticker=ticker,
        quantity=qty,
        entry_price=100.0,
        entry_date=datetime.utcnow() - timedelta(hours=2),
        pending_exit_order_id=None,
        direction="long",
    )


def _sell(px: float, size: float, mins_ago: int = 5) -> dict:
    return {
        "product_id": "ETH-USD",
        "side": "SELL",
        "price": px,
        "size": size,
        "trade_time": (datetime.utcnow() - timedelta(minutes=mins_ago)).isoformat(),
    }


def _client(rows: list[dict]) -> SimpleNamespace:
    return SimpleNamespace(get_fills=lambda **kw: {"fills": rows})


def test_partial_coverage_uses_vwap_priced_on_full_position():
    from app.services import coinbase_service as cs

    trade = _trade(qty=10.0)
    # 7 of 10 units sold (70% coverage, above the 0.5 floor) at VWAP 110.
    with patch.object(cs, "_get_client", return_value=_client([_sell(110.0, 7.0)])):
        out = cs._coinbase_stale_close_fill(trade)
    assert out is not None
    assert abs(out["price"] - 110.0) < 1e-9
    assert abs(out["quantity"] - 10.0) < 1e-9            # full position, not the 7 covered
    assert out["partial_coverage"] is True
    assert out["source"] == "recent_sell_fills_partial"


def test_below_coverage_floor_returns_none_no_fabrication():
    from app.services import coinbase_service as cs

    trade = _trade(qty=10.0)
    # only 3 of 10 (30% < 0.5 floor) -> too thin -> unknown (NULL), never fabricated.
    with patch.object(cs, "_get_client", return_value=_client([_sell(110.0, 3.0)])):
        out = cs._coinbase_stale_close_fill(trade)
    assert out is None


def test_full_coverage_not_flagged_partial():
    from app.services import coinbase_service as cs

    trade = _trade(qty=10.0)
    with patch.object(cs, "_get_client", return_value=_client([_sell(110.0, 10.0)])):
        out = cs._coinbase_stale_close_fill(trade)
    assert out is not None
    assert out["partial_coverage"] is False
    assert out["source"] == "recent_sell_fills"
    assert abs(out["quantity"] - 10.0) < 1e-9
