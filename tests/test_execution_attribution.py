from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.brain_work.execution_attribution import (
    trade_close_attribution_dict,
)


def test_trade_close_attribution_uses_contract_aware_option_return() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        strategy_proposal_id=99,
        pnl=40.0,
        entry_price=1.25,
        exit_price=716.0,
        quantity=2.0,
        direction="long",
        broker_source="robinhood",
        broker_order_id="order-1",
        asset_kind="option",
        tags=None,
        indicator_snapshot={"asset_type": "options"},
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    out = trade_close_attribution_dict(trade)

    assert out["pnl"] == pytest.approx(40.0)
    assert out["realized_return_pct"] == pytest.approx(16.0)
    assert out["tca_cost_pct"] == pytest.approx(0.30)
    assert out["net_return_pct"] == pytest.approx(15.70)
    assert out["exit_price"] == pytest.approx(716.0)


def test_trade_close_attribution_rejects_ambiguous_option_price_return() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        strategy_proposal_id=None,
        pnl=None,
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        direction="long",
        broker_source="robinhood",
        broker_order_id="order-2",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    out = trade_close_attribution_dict(trade)

    assert out["realized_return_pct"] is None
    assert out["net_return_pct"] is None
    assert out["tca_cost_pct"] == pytest.approx(0.30)
