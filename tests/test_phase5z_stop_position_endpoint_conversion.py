from __future__ import annotations

import inspect

from app.routers import trading


def test_stop_positions_uses_envelope_runtime_helper_not_trade_query() -> None:
    source = inspect.getsource(trading.api_stop_positions)

    assert "load_open_stop_position_envelope_objects" in source
    assert "db.query(Trade)" not in source
    assert "from ..models.trading import Trade" not in source
    assert "filter_broker_stale_open_trades" in source
    assert "broker_position_display_metrics" in source
    assert "broker_quote_for_trade" in source
    assert "_build_brain_context" in source


def test_stop_positions_public_payload_contract_stays_pinned() -> None:
    source = inspect.getsource(trading.api_stop_positions)

    for field in (
        '"positions": result',
        '"suppressed_stale_trades": suppressed_stale_trades',
        '"suppressed_stale_count": len(suppressed_stale_trades)',
        '"id": t.id',
        '"ticker": t.ticker',
        '"asset_type": "options"',
        '"current_price": price',
        '"broker_truth_entry_price": broker_metrics.get("entry_price")',
        '"state": state',
        '"brain": brain_ctx',
    ):
        assert field in source
