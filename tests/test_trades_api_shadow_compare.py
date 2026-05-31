from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

from app.routers.trading_sub.trades import (
    PHASE5AF_TRADES_API_ENV,
    _phase5af_trades_api_can_use_envelopes,
    _stable_trades_shadow_mismatches,
    _trade_like_public_response,
    _trades_api_envelope_response_rows,
)


def _route_row(**overrides):
    row = {
        "id": 42,
        "ticker": "ABC",
        "direction": "long",
        "entry_price": 10.25,
        "local_entry_price": 10.0,
        "exit_price": None,
        "quantity": 4.5,
        "local_quantity": 4.0,
        "entry_date": "2026-05-30T09:30:00",
        "exit_date": None,
        "status": "open",
        "pnl": None,
        "tags": "alpha",
        "notes": None,
        "broker_source": "robinhood",
        "broker_status": "filled",
        "broker_order_id": "ord-1",
        "filled_at": "2026-05-30T09:31:00",
        "avg_fill_price": 10.0,
        "tca_reference_entry_price": 9.95,
        "tca_entry_slippage_bps": 5.0,
        "tca_reference_exit_price": None,
        "tca_exit_slippage_bps": None,
        "strategy_proposal_id": 7,
        "scan_pattern_id": 585,
        "position_id": 99,
    }
    row.update(overrides)
    return row


def _envelope_row(**overrides):
    row = {
        "id": 42,
        "ticker": "ABC",
        "direction": "long",
        "entry_price": 10.0,
        "exit_price": None,
        "quantity": 4.0,
        "entry_date": datetime(2026, 5, 30, 9, 30),
        "exit_date": None,
        "status": "open",
        "pnl": None,
        "tags": "alpha",
        "notes": None,
        "broker_source": "robinhood",
        "broker_status": "filled",
        "broker_order_id": "ord-1",
        "filled_at": datetime(2026, 5, 30, 9, 31),
        "avg_fill_price": 10.0,
        "tca_reference_entry_price": 9.95,
        "tca_entry_slippage_bps": 5.0,
        "tca_reference_exit_price": None,
        "tca_exit_slippage_bps": None,
        "strategy_proposal_id": 7,
        "scan_pattern_id": 585,
        "position_id": 99,
    }
    row.update(overrides)
    return row


def test_shadow_compare_uses_local_values_not_broker_display_overlays():
    mismatches = _stable_trades_shadow_mismatches(
        [_route_row()],
        [_envelope_row()],
    )

    assert mismatches == []


def test_shadow_compare_flags_stable_field_mismatch():
    mismatches = _stable_trades_shadow_mismatches(
        [_route_row(local_entry_price=10.5)],
        [_envelope_row(entry_price=10.0)],
    )

    assert mismatches == [
        {
            "id": 42,
            "field": "entry_price",
            "current": 10.5,
            "envelope": 10.0,
        }
    ]


def test_envelope_response_rows_match_public_trade_shape():
    rows = _trades_api_envelope_response_rows([_envelope_row(status="closed")])

    assert rows == [
        {
            "id": 42,
            "ticker": "ABC",
            "direction": "long",
            "entry_price": 10.0,
            "exit_price": None,
            "quantity": 4.0,
            "local_entry_price": 10.0,
            "local_quantity": 4.0,
            "entry_date": "2026-05-30T09:30:00",
            "exit_date": None,
            "status": "closed",
            "pnl": None,
            "tags": "alpha",
            "notes": None,
            "broker_source": "robinhood",
            "broker_status": "filled",
            "broker_order_id": "ord-1",
            "filled_at": "2026-05-30T09:31:00",
            "avg_fill_price": 10.0,
            "tca_reference_entry_price": 9.95,
            "tca_entry_slippage_bps": 5.0,
            "tca_reference_exit_price": None,
            "tca_exit_slippage_bps": None,
            "strategy_proposal_id": 7,
            "scan_pattern_id": 585,
            "position_id": 99,
            "broker_truth_entry_price": None,
            "broker_truth_quantity": None,
            "broker_truth_position_id": None,
            "broker_truth_current_envelope_id": None,
            "broker_truth_metrics_source": None,
        }
    ]


def test_phase5af_cutover_refuses_open_rows():
    assert _phase5af_trades_api_can_use_envelopes(
        status="open",
        envelope_rows=[_envelope_row(status="open")],
    ) is False
    assert _phase5af_trades_api_can_use_envelopes(
        status=None,
        envelope_rows=[_envelope_row(status="open")],
    ) is False
    assert _phase5af_trades_api_can_use_envelopes(
        status="closed",
        envelope_rows=[_envelope_row(status="closed")],
    ) is True


def test_phase5ah_trade_like_response_applies_broker_truth_and_stale_filter(
    monkeypatch,
):
    open_trade = SimpleNamespace(**_envelope_row(status="open"))
    closed_trade = SimpleNamespace(**_envelope_row(
        id=43,
        ticker="XYZ",
        status="closed",
        exit_date=datetime(2026, 5, 30, 10, 30),
        pnl=12.5,
    ))
    stale_trade = SimpleNamespace(**_envelope_row(
        id=44,
        ticker="STALE",
        status="open",
    ))

    def _fake_filter(_db, rows):
        return [t for t in rows if t.id == 42], [{"id": 44, "ticker": "STALE"}]

    def _fake_metrics(_db, trade):
        if trade.id == 42:
            return {
                "entry_price": 10.25,
                "quantity": 4.5,
                "position_id": 99,
                "current_envelope_id": 42,
                "source": "broker_position_identity",
            }
        return {}

    monkeypatch.setattr(
        "app.routers.trading_sub.trades.filter_broker_stale_open_trades",
        _fake_filter,
    )
    monkeypatch.setattr(
        "app.routers.trading_sub.trades.broker_position_display_metrics",
        _fake_metrics,
    )

    rows, suppressed = _trade_like_public_response(
        SimpleNamespace(),
        [open_trade, closed_trade, stale_trade],
        apply_open_stale_filter=True,
    )

    assert [row["id"] for row in rows] == [42, 43]
    assert suppressed == [{"id": 44, "ticker": "STALE"}]
    assert rows[0]["entry_price"] == 10.25
    assert rows[0]["quantity"] == 4.5
    assert rows[0]["local_entry_price"] == 10.0
    assert rows[0]["local_quantity"] == 4.0
    assert rows[0]["broker_truth_metrics_source"] == "broker_position_identity"
    assert rows[1]["entry_price"] == 10.0
    assert rows[1]["broker_truth_metrics_source"] is None


def test_phase5af_trades_api_flag_is_typed_default_false(monkeypatch):
    from app.config import Settings

    monkeypatch.delenv(PHASE5AF_TRADES_API_ENV, raising=False)

    s = Settings(_env_file=None)

    assert s.chili_phase5af_trades_api_use_envelopes is False


def test_phase5af_trades_api_env_alias_flows_through_settings(monkeypatch):
    from app.config import Settings

    monkeypatch.setenv(PHASE5AF_TRADES_API_ENV, "true")

    s = Settings(_env_file=None)

    assert s.chili_phase5af_trades_api_use_envelopes is True
