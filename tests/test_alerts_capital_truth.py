from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.services.trading import alerts


class _FakeQuery:
    def __init__(self, row):
        self.row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self.row


class _FakeDb:
    def __init__(self, pattern=None):
        self.pattern = pattern
        self.commits = 0
        self.rollbacks = 0

    def query(self, _model):
        return _FakeQuery(self.pattern)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def _promoted_pattern(pattern_id: int = 101):
    return SimpleNamespace(
        id=pattern_id,
        name="Capital truth pattern",
        active=True,
        promotion_status="promoted",
        lifecycle_stage="promoted",
    )


def _pick(pattern_id: int, ticker: str = "CAPT") -> dict:
    return {
        "ticker": ticker,
        "signal": "buy",
        "combined_score": 9.5,
        "price": 100.0,
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "brain_stop": 95.0,
        "take_profit": 115.0,
        "brain_target": 115.0,
        "scan_pattern_id": pattern_id,
        "signals": [],
        "indicators": {},
        "timeframe": "swing",
    }


def _proposal(**overrides):
    fields = {
        "user_id": None,
        "ticker": "CAPT",
        "direction": "long",
        "status": "approved",
        "entry_price": 100.0,
        "stop_loss": 95.0,
        "take_profit": 115.0,
        "quantity": 2,
        "position_size_pct": 5.0,
        "projected_profit_pct": 15.0,
        "projected_loss_pct": 5.0,
        "risk_reward_ratio": 3.0,
        "confidence": 95.0,
        "timeframe": "swing",
        "thesis": "Capital truth regression",
        "signals_json": None,
    }
    fields.update(overrides)
    return SimpleNamespace(**fields)


def test_get_buying_power_returns_none_when_disconnected():
    with patch("app.services.broker_service.is_connected", return_value=False), patch(
        "app.services.broker_service.get_portfolio"
    ) as get_portfolio:
        assert alerts._get_buying_power() is None

    get_portfolio.assert_not_called()


def test_get_buying_power_returns_none_when_fetch_fails():
    with patch("app.services.broker_service.is_connected", return_value=True), patch(
        "app.services.broker_service.get_portfolio",
        side_effect=RuntimeError("portfolio unavailable"),
    ):
        assert alerts._get_buying_power() is None


def test_get_buying_power_returns_none_when_portfolio_value_is_unparseable():
    with patch("app.services.broker_service.is_connected", return_value=True), patch(
        "app.services.broker_service.get_portfolio",
        return_value={"buying_power": "not-a-number"},
    ):
        assert alerts._get_buying_power() is None


def test_safe_float_rejects_nonfinite_and_boolean_values():
    assert alerts._safe_float("150.25") == 150.25
    assert alerts._safe_float("NaN") == 0.0
    assert alerts._safe_float("Infinity") == 0.0
    assert alerts._safe_float("-Infinity") == 0.0
    assert alerts._safe_float(True) == 0.0


def test_safe_order_state_normalizes_malformed_broker_values():
    assert alerts._safe_order_state(" Filled ") == "filled"
    assert alerts._safe_order_state("") == "queued"
    assert alerts._safe_order_state(None) == "queued"
    assert alerts._safe_order_state(True) == "queued"
    assert alerts._safe_order_state(1) == "queued"


def test_execute_proposal_does_not_zero_fill_proven_quantity_fields():
    source = Path(alerts.__file__).read_text()
    start = source.index("def _execute_proposal(")
    end = source.index("\ndef _safe_float", start)
    assert "float(quantity or 0)" not in source[start:end]


def test_generate_strategy_proposals_skips_unknown_buying_power_without_superseding():
    pattern = _promoted_pattern()
    db = _FakeDb(pattern=pattern)

    with patch("app.services.trading.scanner.generate_top_picks", return_value=[_pick(pattern.id)]), patch(
        "app.services.trading.alerts._get_buying_power", return_value=None
    ), patch(
        "app.services.trading.alerts._proposal_passes_sector_cap", return_value=True
    ), patch(
        "app.services.trading.market_data.fetch_quote"
    ) as fetch_quote, patch(
        "app.services.trading.alerts._supersede_proposals"
    ) as supersede, patch(
        "app.services.brain_worker_signals.persist_last_proposal_skips_json"
    ):
        created = alerts.generate_strategy_proposals(db, user_id=None)

    assert created == []
    fetch_quote.assert_not_called()
    supersede.assert_not_called()
    assert db.commits == 1


def test_generate_strategy_proposals_rejects_inverted_long_levels_without_superseding():
    pattern = _promoted_pattern()
    db = _FakeDb(pattern=pattern)
    pick = _pick(pattern.id)
    pick.update({"stop_loss": 105.0, "brain_stop": 105.0})
    persisted = {}

    def _persist(_db, payload):
        persisted.update(payload)

    with patch("app.services.trading.scanner.generate_top_picks", return_value=[pick]), patch(
        "app.services.trading.alerts._get_buying_power", return_value=10_000.0
    ), patch(
        "app.services.trading.alerts._proposal_passes_sector_cap", return_value=True
    ), patch(
        "app.services.trading.market_data.fetch_quote", return_value={"price": 100.0}
    ), patch(
        "app.services.trading.alerts._supersede_proposals"
    ) as supersede, patch(
        "app.services.trading.alerts._compute_position_size"
    ) as compute_position_size, patch(
        "app.services.brain_worker_signals.persist_last_proposal_skips_json",
        side_effect=_persist,
    ):
        created = alerts.generate_strategy_proposals(db, user_id=None)

    assert created == []
    supersede.assert_not_called()
    compute_position_size.assert_not_called()
    assert persisted["skips"]["invalid_long_levels"] == 1
    assert db.commits == 1


def test_create_proposal_from_pick_refuses_unknown_buying_power():
    pattern = _promoted_pattern()
    db = _FakeDb(pattern=pattern)

    with patch("app.services.trading.market_data.fetch_quote", return_value={"price": 100.0}), patch(
        "app.services.trading.alerts._get_buying_power", return_value=None
    ), patch("app.services.trading.alerts._supersede_proposals") as supersede:
        proposal, reason = alerts.create_proposal_from_pick(
            db,
            user_id=None,
            ticker="CAPT",
            pick=_pick(pattern.id),
        )

    assert proposal is None
    assert reason == "buying_power_unavailable"
    supersede.assert_not_called()


def test_create_proposal_from_pick_rejects_inverted_long_levels_before_capital():
    pattern = _promoted_pattern()
    db = _FakeDb(pattern=pattern)
    pick = _pick(pattern.id)
    pick.update({"take_profit": 90.0, "brain_target": 90.0})

    with patch("app.services.trading.market_data.fetch_quote", return_value={"price": 100.0}), patch(
        "app.services.trading.alerts._get_buying_power"
    ) as get_buying_power, patch(
        "app.services.trading.alerts._supersede_proposals"
    ) as supersede:
        proposal, reason = alerts.create_proposal_from_pick(
            db,
            user_id=None,
            ticker="CAPT",
            pick=pick,
        )

    assert proposal is None
    assert reason == (
        "Invalid long proposal levels: stop loss must be below entry and "
        "take profit must be above entry."
    )
    get_buying_power.assert_not_called()
    supersede.assert_not_called()


def test_create_proposal_from_pick_sizing_unavailable_does_not_supersede():
    pattern = _promoted_pattern()
    db = _FakeDb(pattern=pattern)

    with patch("app.services.trading.market_data.fetch_quote", return_value={"price": 100.0}), patch(
        "app.services.trading.alerts._get_buying_power", return_value=10_000.0
    ), patch(
        "app.services.trading.alerts._compute_position_size",
        return_value=(None, None),
    ), patch(
        "app.services.trading.alerts._supersede_proposals"
    ) as supersede:
        proposal, reason = alerts.create_proposal_from_pick(
            db,
            user_id=None,
            ticker="CAPT",
            pick=_pick(pattern.id),
        )

    assert proposal is None
    assert reason == "sizing_unavailable"
    supersede.assert_not_called()


def test_execute_proposal_blocks_unknown_buying_power_before_risk_or_broker():
    proposal = _proposal()
    db = _FakeDb()

    with patch("app.services.trading.alerts._get_buying_power", return_value=None), patch(
        "app.services.trading.alerts.dispatch_alert"
    ) as dispatch_alert, patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed"
    ) as risk_gate, patch(
        "app.services.broker_manager.place_buy_order"
    ) as place_buy_order:
        result = alerts._execute_proposal(db, proposal, user_id=None)

    assert result == {"status": "blocked", "error": "buying_power_unavailable"}
    dispatch_alert.assert_called_once()
    risk_gate.assert_not_called()
    place_buy_order.assert_not_called()
    assert proposal.status == "approved"


def test_execute_proposal_blocks_inverted_long_levels_before_risk_or_broker():
    proposal = _proposal(stop_loss=105.0)
    db = _FakeDb()

    with patch("app.services.trading.alerts._get_buying_power", return_value=10_000.0), patch(
        "app.services.trading.alerts.dispatch_alert"
    ) as dispatch_alert, patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed"
    ) as risk_gate, patch(
        "app.services.broker_manager.place_buy_order"
    ) as place_buy_order:
        result = alerts._execute_proposal(db, proposal, user_id=None)

    assert result == {"status": "blocked", "error": "invalid_long_levels"}
    dispatch_alert.assert_called_once()
    risk_gate.assert_not_called()
    place_buy_order.assert_not_called()
    assert proposal.status == "approved"


def test_execute_proposal_blocks_when_stale_quantity_cannot_be_resized():
    proposal = _proposal(quantity=None)
    db = _FakeDb()

    with patch("app.services.trading.alerts._get_buying_power", return_value=10_000.0), patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, None),
    ) as risk_gate, patch(
        "app.services.trading.alerts._compute_position_size",
        return_value=(None, None),
    ), patch(
        "app.services.trading.alerts.dispatch_alert"
    ), patch(
        "app.services.broker_manager.place_buy_order"
    ) as place_buy_order:
        result = alerts._execute_proposal(db, proposal, user_id=None)

    assert result == {"status": "blocked", "error": "sizing_unavailable"}
    risk_gate.assert_called_once()
    place_buy_order.assert_not_called()
    assert proposal.quantity is None


def test_execute_proposal_blocks_stale_quantity_when_capital_shrinks():
    proposal = _proposal(stop_loss=99.0)
    db = _FakeDb()

    with patch("app.services.trading.alerts._get_buying_power", return_value=1_000.0), patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, None),
    ) as risk_gate, patch(
        "app.services.trading.alerts.dispatch_alert"
    ) as dispatch_alert, patch(
        "app.services.broker_manager.get_best_broker_for"
    ) as get_best_broker_for, patch(
        "app.services.broker_manager.place_buy_order"
    ) as place_buy_order:
        result = alerts._execute_proposal(db, proposal, user_id=None)

    assert result == {
        "status": "blocked",
        "error": "proposal_quantity_exceeds_current_capital",
    }
    risk_gate.assert_called_once()
    dispatch_alert.assert_called_once()
    get_best_broker_for.assert_not_called()
    place_buy_order.assert_not_called()
    assert proposal.status == "approved"


def test_execute_proposal_blocks_quantity_above_current_risk_budget_without_plan_pct():
    proposal = _proposal(quantity=3, position_size_pct=None)
    db = _FakeDb()

    with patch("app.services.trading.alerts._get_buying_power", return_value=1_000.0), patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, None),
    ) as risk_gate, patch(
        "app.services.trading.alerts.dispatch_alert"
    ) as dispatch_alert, patch(
        "app.services.broker_manager.get_best_broker_for"
    ) as get_best_broker_for, patch(
        "app.services.broker_manager.place_buy_order"
    ) as place_buy_order:
        result = alerts._execute_proposal(db, proposal, user_id=None)

    assert result == {
        "status": "blocked",
        "error": "proposal_quantity_exceeds_current_risk",
    }
    risk_gate.assert_called_once()
    dispatch_alert.assert_called_once()
    get_best_broker_for.assert_not_called()
    place_buy_order.assert_not_called()
    assert proposal.status == "approved"


def test_execute_proposal_rolls_back_when_decision_packet_create_fails():
    proposal = _proposal()
    db = _FakeDb()

    with patch("app.services.trading.alerts._get_buying_power", return_value=10_000.0), patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, None),
    ), patch(
        "app.services.trading.decision_ledger.record_strategy_proposal_decision",
        side_effect=RuntimeError("ledger unavailable"),
    ), patch(
        "app.services.trading.alerts.dispatch_alert"
    ), patch(
        "app.services.broker_manager.place_buy_order"
    ) as place_buy_order:
        result = alerts._execute_proposal(db, proposal, user_id=None, broker="manual")

    assert result == {"status": "blocked", "error": "decision_packet_create_failed"}
    assert db.rollbacks == 1
    place_buy_order.assert_not_called()
    assert proposal.status == "approved"


def test_execute_proposal_blocks_when_nonfinite_quantity_cannot_be_resized():
    proposal = _proposal(quantity=float("nan"))
    db = _FakeDb()

    with patch("app.services.trading.alerts._get_buying_power", return_value=10_000.0), patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, None),
    ) as risk_gate, patch(
        "app.services.trading.alerts._compute_position_size",
        return_value=(None, None),
    ), patch(
        "app.services.trading.alerts.dispatch_alert"
    ), patch(
        "app.services.broker_manager.place_buy_order"
    ) as place_buy_order:
        result = alerts._execute_proposal(db, proposal, user_id=None)

    assert result == {"status": "blocked", "error": "sizing_unavailable"}
    risk_gate.assert_called_once()
    place_buy_order.assert_not_called()
    assert proposal.quantity != proposal.quantity


def test_execute_proposal_resizes_nonfinite_quantity_before_broker(monkeypatch):
    proposal = _proposal(quantity=float("nan"))
    db = _FakeDb()

    monkeypatch.setattr(
        alerts.settings,
        "brain_decision_packet_required_for_proposals",
        False,
        raising=False,
    )

    with patch("app.services.trading.alerts._get_buying_power", return_value=10_000.0), patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, None),
    ), patch(
        "app.services.trading.alerts._compute_position_size",
        return_value=(3, 3.0),
    ), patch(
        "app.services.broker_manager.get_best_broker_for",
        return_value="robinhood",
    ), patch(
        "app.services.trading.alerts.dispatch_alert"
    ), patch(
        "app.services.broker_manager.place_buy_order",
        return_value={"ok": False, "broker": "robinhood", "error": "unit reject"},
    ) as place_buy_order:
        result = alerts._execute_proposal(db, proposal, user_id=None)

    assert result == {
        "status": "failed",
        "error": "unit reject",
        "decision_packet_id": None,
    }
    place_buy_order.assert_called_once()
    assert place_buy_order.call_args.kwargs["quantity"] == 3.0
    assert proposal.quantity == 3.0


def test_execute_proposal_blocks_auto_manual_fallback_before_local_record():
    proposal = _proposal()
    db = _FakeDb()

    with patch("app.services.trading.alerts._get_buying_power", return_value=10_000.0), patch(
        "app.services.trading.portfolio_risk.check_new_trade_allowed",
        return_value=(True, None),
    ) as risk_gate, patch(
        "app.services.broker_manager.get_best_broker_for",
        return_value="manual",
    ), patch(
        "app.services.trading.alerts.dispatch_alert"
    ) as dispatch_alert, patch(
        "app.services.broker_manager.place_buy_order"
    ) as place_buy_order:
        result = alerts._execute_proposal(db, proposal, user_id=None)

    assert result == {"status": "blocked", "error": "broker_unavailable"}
    risk_gate.assert_called_once()
    dispatch_alert.assert_called_once()
    place_buy_order.assert_not_called()
    assert proposal.status == "approved"
    assert not hasattr(proposal, "trade_id")
