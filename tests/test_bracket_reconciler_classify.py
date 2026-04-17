"""Phase G - unit tests for ``classify_discrepancy``.

Covers every ``kind`` branch exhaustively. No DB, no broker.
"""
from __future__ import annotations

import pytest

from app.services.trading.bracket_reconciler import (
    BrokerView,
    LocalView,
    Tolerances,
    classify_discrepancy,
)


def _local(**over) -> LocalView:
    defaults = dict(
        trade_id=1,
        bracket_intent_id=10,
        ticker="AAPL",
        direction="long",
        quantity=10.0,
        intent_state="shadow_logged",
        stop_price=96.0,
        target_price=106.0,
        broker_source="robinhood",
        trade_status="open",
    )
    defaults.update(over)
    return LocalView(**defaults)


def _broker(**over) -> BrokerView:
    defaults = dict(
        available=True,
        ticker="AAPL",
        broker_source="robinhood",
        position_quantity=10.0,
        stop_order_id="stop-1",
        stop_order_state="open",
        stop_order_price=96.0,
        target_order_id="tgt-1",
        target_order_state="open",
        target_order_price=106.0,
    )
    defaults.update(over)
    return BrokerView(**defaults)


def test_agree_when_local_and_broker_match():
    d = classify_discrepancy(_local(), _broker())
    assert d.kind == "agree"
    assert d.severity == "info"


def test_broker_down_when_snapshot_unavailable():
    d = classify_discrepancy(_local(), _broker(available=False))
    assert d.kind == "broker_down"
    assert d.severity == "warn"


def test_orphan_stop_when_local_closed_but_broker_has_working_stop():
    d = classify_discrepancy(
        _local(trade_status="closed"),
        _broker(),
    )
    assert d.kind == "orphan_stop"
    assert d.severity == "error"
    assert d.delta_payload["broker_stop_order_id"] == "stop-1"


def test_missing_stop_when_open_trade_but_no_broker_stop():
    d = classify_discrepancy(
        _local(),
        _broker(stop_order_state=None, stop_order_id=None, stop_order_price=None,
                target_order_state=None, target_order_id=None, target_order_price=None),
    )
    assert d.kind == "missing_stop"


def test_missing_stop_severity_warn_for_intent_state():
    d = classify_discrepancy(
        _local(intent_state="intent"),
        _broker(stop_order_state=None, stop_order_id=None,
                target_order_state=None),
    )
    assert d.kind == "missing_stop"
    assert d.severity == "warn"


def test_missing_stop_severity_error_when_authoritative():
    d = classify_discrepancy(
        _local(intent_state="authoritative_submitted"),
        _broker(stop_order_state=None, stop_order_id=None,
                target_order_state=None),
    )
    assert d.kind == "missing_stop"
    assert d.severity == "error"


def test_qty_drift_detected():
    d = classify_discrepancy(
        _local(quantity=10.0),
        _broker(position_quantity=9.0),
    )
    assert d.kind == "qty_drift"
    assert d.delta_payload["abs_diff"] == pytest.approx(1.0)


def test_qty_drift_within_tolerance_still_agree():
    d = classify_discrepancy(
        _local(quantity=10.0),
        _broker(position_quantity=10.0 + 1e-9),
    )
    assert d.kind == "agree"


def test_state_drift_authoritative_vs_cancelled():
    d = classify_discrepancy(
        _local(intent_state="authoritative_submitted"),
        _broker(stop_order_state="cancelled"),
    )
    assert d.kind == "state_drift"
    assert d.severity == "error"


def test_price_drift_stop_leg_beyond_tolerance():
    # 25 bps tolerance default => 96.0 -> 96.30 is ~31 bps away
    d = classify_discrepancy(
        _local(stop_price=96.0),
        _broker(stop_order_price=96.30),
    )
    assert d.kind == "price_drift"
    assert d.delta_payload["leg"] == "stop"
    assert d.delta_payload["drift_bps"] > 25.0


def test_price_drift_stop_within_tolerance_is_agree():
    d = classify_discrepancy(
        _local(stop_price=96.0),
        _broker(stop_order_price=96.05),  # ~5 bps
    )
    assert d.kind == "agree"


def test_price_drift_target_leg():
    d = classify_discrepancy(
        _local(target_price=106.0),
        _broker(target_order_price=107.0),  # ~94 bps
    )
    assert d.kind == "price_drift"
    assert d.delta_payload["leg"] == "target"


def test_tighter_tolerance_flags_smaller_drift():
    tight = Tolerances(price_drift_bps=5.0)
    d = classify_discrepancy(
        _local(stop_price=96.0),
        _broker(stop_order_price=96.10),  # ~10 bps
        tolerances=tight,
    )
    assert d.kind == "price_drift"


def test_qty_tolerance_respected():
    d = classify_discrepancy(
        _local(quantity=10.0),
        _broker(position_quantity=10.0 + 1e-4),
        tolerances=Tolerances(qty_drift_abs=1e-3),
    )
    assert d.kind == "agree"


def test_orphan_stop_beats_missing_stop_priority():
    # Closed local trade + broker working stop => orphan_stop even if
    # other flags could fire.
    d = classify_discrepancy(
        _local(trade_status="closed", bracket_intent_id=None),
        _broker(),
    )
    assert d.kind == "orphan_stop"
