"""bracket-writer-respect-upside-targets (2026-05-04) -- tests for the
pending-decision surface that replaces the bracket writer's previous
auto-cancel-and-place-stop behavior on covered-by-existing-sell.

Six scenarios:
    A. existing limit + missing stop -> pending_decision row, no broker
       action.
    B. operator chooses keep_target -> intent transitions, no broker.
    C. operator chooses replace_with_stop -> cancel + place sequence.
    D. operator chooses convert_to_trailing_stop -> NOT_IMPLEMENTED.
    E. cancelled-limit replacement viability evaluator: above current
       -> pending_decision; below current -> None.

Tests use the chili_test conftest db fixture. Run with ``-p no:asyncio``.
"""
from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text

from app.services.trading.bracket_reconciler import (
    BrokerView,
    LocalView,
    ReconciliationDecision,
)


@dataclass
class _StubAction:
    ok: bool
    reason: str
    new_stop_order_id: str | None = None
    new_stop_qty: float | None = None
    new_stop_price: float | None = None


def _seed(db, *, trade_id: int, intent_id: int, ticker: str,
          qty: float = 10.0, entry: float = 5.0, stop: float = 4.5) -> None:
    db.execute(text("""
        INSERT INTO trading_trades (
            id, ticker, status, broker_source, direction, quantity,
            entry_price, entry_date
        ) VALUES (
            :id, :ticker, 'open', 'robinhood', 'long', :qty,
            :entry, NOW()
        ) ON CONFLICT (id) DO NOTHING
    """), {"id": trade_id, "ticker": ticker, "qty": qty, "entry": entry})

    db.execute(text("""
        INSERT INTO trading_bracket_intents (
            id, trade_id, ticker, direction, quantity, entry_price,
            stop_price, intent_state, shadow_mode, broker_source,
            created_at, updated_at, payload_json
        ) VALUES (
            :id, :tid, :ticker, 'long', :qty, :entry,
            :stop, 'intent', false, 'robinhood',
            NOW(), NOW(), '{}'::jsonb
        ) ON CONFLICT (id) DO NOTHING
    """), {"id": intent_id, "tid": trade_id, "ticker": ticker, "qty": qty,
           "entry": entry, "stop": stop})
    db.commit()


def _set_pending(db, intent_id: int, choice: str | None,
                 *, kind: str = "existing_sell_holds_all_shares") -> None:
    """Manually inject a pending_decision row to simulate an operator
    choice (or the unanswered state)."""
    import json as _j
    pending = {
        "kind": kind,
        "observed_at": "2026-05-04T20:00:00Z",
        "broker_state": {"qty": 10.0, "held_for_sells": 10.0,
                         "covering_orders": [
                             {"order_id": "abc", "type": "limit",
                              "side": "sell", "qty": 10.0,
                              "price": 6.0, "stop_price": None}
                         ]},
        "brain_state": {"target_price": 7.0, "stop_price": 4.5,
                        "current_price": 5.5, "regime": "cautious"},
        "options": [
            {"choice": "keep_target", "consequence": "no_downside_stop"},
            {"choice": "replace_with_stop",
             "consequence": "cancels_existing_limit_sell_and_places_stop_at_brain_price"},
        ],
        "operator_choice": choice,
    }
    db.execute(text(
        "UPDATE trading_bracket_intents "
        "SET payload_json = jsonb_build_object('pending_decision', "
        "                    CAST(:p AS JSONB)) "
        "WHERE id = :id"
    ), {"id": intent_id, "p": _j.dumps(pending)})
    db.commit()


def _local(*, trade_id: int, intent_id: int, ticker: str,
           intent_state: str = "intent") -> LocalView:
    return LocalView(
        trade_id=trade_id, bracket_intent_id=intent_id, ticker=ticker,
        direction="long", quantity=10.0, intent_state=intent_state,
        stop_price=4.5, target_price=None,
        broker_source="robinhood", trade_status="open",
    )


def _broker(*, available: bool = True, position_quantity: float = 10.0,
            stop_order_state: str | None = None,
            target_order_state: str | None = None) -> BrokerView:
    return BrokerView(
        available=available, ticker="TST", broker_source="robinhood",
        position_quantity=position_quantity,
        stop_order_state=stop_order_state,
        target_order_state=target_order_state,
    )


def _decision(kind: str = "missing_stop", severity: str = "warn") -> ReconciliationDecision:
    return ReconciliationDecision(kind=kind, severity=severity, delta_payload={})


def _enable_writer():
    from app.config import settings
    return [
        patch.object(settings, "chili_bracket_writer_g2_enabled", True, create=True),
        patch.object(settings, "chili_bracket_writer_g2_place_missing_stop", True,
                     create=True),
        patch.object(settings, "chili_bracket_missing_stop_repair_enabled", True,
                     create=True),
        # CANCEL flag stays OFF (the new policy).
        patch.object(settings, "chili_bracket_writer_cancel_covering_sell", False,
                     create=True),
    ]


# ── Scenarios ─────────────────────────────────────────────────────────


def test_a_covered_by_existing_sell_writes_pending_decision(db):
    """A: held_for_sells == broker_qty -> pending_decision row written
    with options list, no broker call."""
    _seed(db, trade_id=3001, intent_id=33001, ticker="ALPHA",
          qty=10.0, entry=5.0)
    fake_adapter = MagicMock()
    product = MagicMock()
    product.product_id = "ALPHA"
    product.raw = {"ticker": "ALPHA", "quantity": 10.0}
    fake_adapter.get_products.return_value = ([product], True)

    from app.services.trading.bracket_writer_g2 import place_missing_stop

    patches = _enable_writer()
    for p in patches:
        p.start()
    try:
        with (
            # held_for_sells == broker_qty triggers covered-by-sell branch.
            patch("app.services.broker_service.get_position_held_for_sells",
                  return_value=10.0),
            # list_open_sell_orders surfaces the covering order in JSON.
            patch("app.services.broker_service.list_open_sell_orders_for_ticker",
                  return_value=[{"order_id": "ord-abc", "type": "limit",
                                 "side": "sell", "quantity": 10.0,
                                 "price": 6.5, "stop_price": None}]),
            # Sentinel: broker SELL_STOP placement must NOT be called.
            patch(
                "app.services.broker_service.place_sell_stop_loss_order",
                side_effect=AssertionError(
                    "broker SELL_STOP must not be placed -- pending_decision flow"
                ),
            ),
            # Sentinel: cancel must NOT be called.
            patch(
                "app.services.broker_service.cancel_open_sell_orders_for_ticker",
                side_effect=AssertionError(
                    "cancel must not run on initial pending_decision write"
                ),
            ),
            # fetch_quote returns a price for the JSON payload.
            patch("app.services.trading.market_data.fetch_quote",
                  return_value={"last_price": 5.5}),
        ):
            result = place_missing_stop(
                db,
                trade_id=3001,
                bracket_intent_id=33001,
                ticker="ALPHA",
                broker_source="robinhood",
                decision=_decision("missing_stop"),
                local_quantity=10.0,
                stop_price=4.5,
                adapter_factory=lambda src: fake_adapter,
            )
    finally:
        for p in patches:
            p.stop()

    assert result.ok is False
    assert result.reason == "existing_target_present_no_stop"

    row = db.execute(text(
        "SELECT payload_json, last_diff_reason "
        "FROM trading_bracket_intents WHERE id=33001"
    )).first()
    payload = row[0] or {}
    pending = payload.get("pending_decision")
    assert pending is not None, "pending_decision must be written"
    assert pending.get("kind") == "existing_sell_holds_all_shares"
    assert pending.get("operator_choice") is None
    assert pending["broker_state"]["qty"] == 10.0
    assert pending["broker_state"]["held_for_sells"] == 10.0
    assert len(pending["broker_state"]["covering_orders"]) == 1
    # Default options: keep_target + replace_with_stop. trailing absent
    # because no broker helper exists.
    choices = {opt["choice"] for opt in pending["options"]}
    assert "keep_target" in choices
    assert "replace_with_stop" in choices
    # Last diff reason recorded.
    assert row[1] == "existing_target_present_no_stop"


def test_b_keep_target_clears_pending_no_broker_action(db):
    """B: operator chose keep_target -> reconciler resolves, no broker
    action, intent_state -> reconciled, pending_decision cleared."""
    _seed(db, trade_id=3002, intent_id=33002, ticker="BETA")
    _set_pending(db, 33002, "keep_target")

    from app.services.trading.bracket_reconciliation_service import (
        _resolve_pending_bracket_decision,
    )

    local = _local(trade_id=3002, intent_id=33002, ticker="BETA")
    broker = _broker()

    with (
        patch("app.services.broker_service.cancel_open_sell_orders_for_ticker",
              side_effect=AssertionError(
                  "cancel must not be called for keep_target"
              )),
        patch("app.services.trading.bracket_writer_g2.place_missing_stop",
              side_effect=AssertionError(
                  "place_missing_stop must not be called for keep_target"
              )),
    ):
        result = _resolve_pending_bracket_decision(
            db, local=local, broker=broker, sweep_id="test-sweep",
        )

    assert result is not None
    assert result["writer"] == "pending_decision_resolved"
    assert result["ok"] is True
    assert result["reason"] == "keep_target"

    row = db.execute(text(
        "SELECT payload_json, intent_state, last_diff_reason "
        "FROM trading_bracket_intents WHERE id=33002"
    )).first()
    payload = row[0] or {}
    assert payload.get("pending_decision") is None
    assert row[1] == "reconciled"
    assert row[2] == "pending_decision_resolved:keep_target"


def test_c_replace_with_stop_cancels_then_places(db):
    """C: operator chose replace_with_stop -> cancel called, place_stop
    called with brain stop_price, pending_decision cleared."""
    _seed(db, trade_id=3003, intent_id=33003, ticker="GAMMA")
    _set_pending(db, 33003, "replace_with_stop")

    from app.services.trading.bracket_reconciliation_service import (
        _resolve_pending_bracket_decision,
    )

    local = _local(trade_id=3003, intent_id=33003, ticker="GAMMA")
    broker = _broker()

    cancel_calls: list[str] = []
    place_calls: list[dict] = []

    def _stub_cancel(ticker):
        cancel_calls.append(ticker)
        return 1  # cancelled 1 covering order

    def _stub_place(db_, **kw):
        place_calls.append(kw)
        return _StubAction(
            ok=True, reason="ok", new_stop_order_id="new-stop-id",
            new_stop_qty=10.0, new_stop_price=4.5,
        )

    with (
        patch("app.services.broker_service.cancel_open_sell_orders_for_ticker",
              side_effect=_stub_cancel),
        patch("app.services.trading.bracket_writer_g2.place_missing_stop",
              side_effect=_stub_place),
        # No real time.sleep for the test
        patch("time.sleep", return_value=None),
    ):
        result = _resolve_pending_bracket_decision(
            db, local=local, broker=broker, sweep_id="test-sweep",
        )

    assert result is not None
    assert result["ok"] is True
    assert result["reason"] == "ok"
    assert cancel_calls == ["GAMMA"]
    assert len(place_calls) == 1
    # Brain stop_price from _set_pending was 4.5
    assert place_calls[0]["stop_price"] == 4.5

    payload = db.execute(text(
        "SELECT payload_json FROM trading_bracket_intents WHERE id=33003"
    )).scalar() or {}
    assert payload.get("pending_decision") is None


def test_d_convert_to_trailing_stop_returns_not_implemented(db):
    """D: convert_to_trailing_stop -> NOT_IMPLEMENTED outcome, leaves
    pending_decision in place for operator to revise."""
    _seed(db, trade_id=3004, intent_id=33004, ticker="DELTA")
    _set_pending(db, 33004, "convert_to_trailing_stop")

    from app.services.trading.bracket_reconciliation_service import (
        _resolve_pending_bracket_decision,
    )

    local = _local(trade_id=3004, intent_id=33004, ticker="DELTA")
    broker = _broker()

    with (
        patch("app.services.broker_service.cancel_open_sell_orders_for_ticker",
              side_effect=AssertionError("cancel must not be called for trailing-stop NOT_IMPLEMENTED")),
        patch("app.services.trading.bracket_writer_g2.place_missing_stop",
              side_effect=AssertionError("place must not be called for trailing-stop NOT_IMPLEMENTED")),
    ):
        result = _resolve_pending_bracket_decision(
            db, local=local, broker=broker, sweep_id="test-sweep",
        )

    assert result is not None
    assert result["ok"] is False
    assert result["reason"] == "convert_to_trailing_stop_not_implemented"

    # pending_decision STAYS so the operator can pick a different option.
    payload = db.execute(text(
        "SELECT payload_json FROM trading_bracket_intents WHERE id=33004"
    )).scalar() or {}
    assert payload.get("pending_decision") is not None


def test_e1_evaluator_above_current_price_writes_pending(db):
    """E (above): brain target above current price -> pending_decision
    row appears with kind='cancelled_limit_replacement_candidate'."""
    _seed(db, trade_id=3005, intent_id=33005, ticker="EVAL", entry=5.0)

    from app.services.trading.bracket_writer_g2 import (
        evaluate_target_replacement,
    )
    from app.services.trading import bracket_intent as bi_mod

    # Brain returns target=8 (above current 5.5 and entry 5.0).
    fake_result = MagicMock()
    fake_result.target_price = 8.0
    fake_result.stop_price = 4.5

    with (
        patch.object(bi_mod, "compute_bracket_intent",
                     return_value=fake_result),
        patch("app.services.trading.market_data.fetch_quote",
              return_value={"last_price": 5.5}),
    ):
        result = evaluate_target_replacement(
            db, bracket_intent_id=33005, trade_id=3005, ticker="EVAL",
            broker_source="robinhood", entry_price=5.0, quantity=10.0,
        )

    assert result is not None
    assert result["kind"] == "cancelled_limit_replacement_candidate"

    payload = db.execute(text(
        "SELECT payload_json FROM trading_bracket_intents WHERE id=33005"
    )).scalar() or {}
    pending = payload.get("pending_decision")
    assert pending is not None
    assert pending["kind"] == "cancelled_limit_replacement_candidate"
    assert pending["brain_state"]["target_price"] == 8.0


def test_e2_evaluator_below_current_price_returns_none(db):
    """E (below): brain target at-or-below current price -> None, no
    pending_decision row."""
    _seed(db, trade_id=3006, intent_id=33006, ticker="EVAL2", entry=5.0)

    from app.services.trading.bracket_writer_g2 import (
        evaluate_target_replacement,
    )
    from app.services.trading import bracket_intent as bi_mod

    # Brain target=5.5 == current_price=5.5 -> not viable.
    fake_result = MagicMock()
    fake_result.target_price = 5.5
    fake_result.stop_price = 4.5

    with (
        patch.object(bi_mod, "compute_bracket_intent",
                     return_value=fake_result),
        patch("app.services.trading.market_data.fetch_quote",
              return_value={"last_price": 5.5}),
    ):
        result = evaluate_target_replacement(
            db, bracket_intent_id=33006, trade_id=3006, ticker="EVAL2",
            broker_source="robinhood", entry_price=5.0, quantity=10.0,
        )

    assert result is None

    payload = db.execute(text(
        "SELECT payload_json FROM trading_bracket_intents WHERE id=33006"
    )).scalar() or {}
    assert payload.get("pending_decision") is None
