from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.brain_work import emitters, execution_hooks
from app.services.trading.brain_work.execution_attribution import (
    paper_trade_close_attribution_dict,
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


def test_paper_trade_close_attribution_uses_contract_aware_option_return() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        pnl=40.0,
        pnl_pct=9999.0,
        entry_price=1.25,
        exit_price=716.0,
        quantity=2.0,
        direction="long",
        exit_reason="target",
        signal_json={"asset_class": "robinhood_options"},
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    out = paper_trade_close_attribution_dict(trade)

    assert out["pnl"] == pytest.approx(40.0)
    assert out["paper_shadow_of_alert_id"] == 77
    assert out["realized_return_pct"] == pytest.approx(16.0)
    assert out["tca_cost_pct"] == pytest.approx(0.30)
    assert out["net_return_pct"] == pytest.approx(15.70)
    assert out["exit_price"] == pytest.approx(716.0)


def test_paper_trade_close_attribution_rejects_legacy_option_pct_without_pnl() -> None:
    trade = SimpleNamespace(
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        pnl=None,
        pnl_pct=17755.61,
        entry_price=4.01,
        exit_price=716.0,
        quantity=1.0,
        direction="long",
        exit_reason="target",
        signal_json={"asset_type": "options"},
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    out = paper_trade_close_attribution_dict(trade)

    assert out["realized_return_pct"] is None
    assert out["net_return_pct"] is None
    assert out["tca_cost_pct"] == pytest.approx(0.30)


def test_emit_paper_trade_closed_outcome_preserves_core_fields_with_extra(
    monkeypatch,
) -> None:
    captured = {}

    def _fake_enqueue(_db, **kwargs):
        captured.update(kwargs)
        return 123

    monkeypatch.setattr(emitters, "enqueue_outcome_event", _fake_enqueue)

    out = emitters.emit_paper_trade_closed_outcome(
        None,
        paper_trade_id=5,
        user_id=9,
        scan_pattern_id=42,
        ticker="SPY",
        pnl=40.0,
        exit_reason="target",
        extra={
            "paper_trade_id": 999,
            "scan_pattern_id": 999,
            "pnl": -1.0,
            "realized_return_pct": 16.0,
        },
    )

    assert out == 123
    assert captured["event_type"] == "paper_trade_closed"
    payload = captured["payload"]
    assert payload["paper_trade_id"] == 5
    assert payload["scan_pattern_id"] == 42
    assert payload["pnl"] == pytest.approx(40.0)
    assert payload["realized_return_pct"] == pytest.approx(16.0)


def test_on_paper_trade_closed_emits_contract_aware_extra(monkeypatch) -> None:
    captured = {}

    def _fake_emit(_db, **kwargs):
        captured.update(kwargs)
        return 123

    monkeypatch.setattr(execution_hooks, "emit_paper_trade_closed_outcome", _fake_emit)
    monkeypatch.setattr(execution_hooks, "enqueue_or_refresh_debounced_work", lambda *a, **k: 1)
    monkeypatch.setattr(execution_hooks, "_record_venue_truth", lambda *a, **k: None)
    monkeypatch.setattr(
        execution_hooks,
        "_refresh_rolling_cost_estimate",
        lambda *a, **k: None,
    )
    paper_trade = SimpleNamespace(
        id=5,
        user_id=9,
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        ticker="SPY",
        pnl=40.0,
        pnl_pct=9999.0,
        entry_price=1.25,
        exit_price=716.0,
        quantity=2.0,
        direction="long",
        exit_reason="target",
        signal_json={"asset_class": "robinhood_options"},
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )

    execution_hooks.on_paper_trade_closed(None, paper_trade)

    assert captured["paper_trade_id"] == 5
    assert captured["scan_pattern_id"] == 42
    assert captured["pnl"] == pytest.approx(40.0)
    assert captured["extra"]["realized_return_pct"] == pytest.approx(16.0)
    assert captured["extra"]["net_return_pct"] == pytest.approx(15.70)
