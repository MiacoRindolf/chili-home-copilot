"""OCO sa mock broker — para masukat ng replay ang protected partials (2026-08-27).

Ang replay ang measurement instrument ng capture rate; nang walang OCO sa mock,
ang bagong partial machinery ay bumabagsak sa suppression sa simulasyon at ang
sukat ay understated. Ang mga test ay sumusubok sa PAREHONG hugis na binabasa
ng live adopt path (raw.legs, #1204).

Runnable: pytest tests/test_mock_broker_oco.py -v
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedQuote,
)


def _adapter():
    a = MockBrokerAdapter()
    a.set_clock(datetime(2026, 8, 27, 14, 0, tzinfo=timezone.utc))
    return a


def _q(bid, ask):
    return RecordedQuote(bid=bid, ask=ask)


def _place(a, tp=2.60, stop=2.10, qty="100"):
    a.set_quote("MIMI", _q(2.30, 2.32))
    return a.place_protected_partial_oco(
        product_id="MIMI", base_size=qty, take_profit_price=tp,
        stop_price=stop, client_order_id="chili_ml_toco_test_1",
    )


def test_placement_returns_the_real_adapter_shape():
    a = _adapter()
    res = _place(a)
    assert res["ok"] is True and res["order_id"]
    assert res["legs"] and res["legs"][0]["order_type"] == "stop"
    assert res["order_request"]["order_class"] == "oco"


def test_tp_touch_fills_the_parent_and_cancels_the_leg():
    """ANG MASAYANG LANDAS: umabot ang bid sa TP — parent fills, leg canceled."""
    a = _adapter()
    res = _place(a, tp=2.60, stop=2.10)
    a.set_quote("MIMI", _q(2.61, 2.63))
    o, _ = a.get_order(res["order_id"])
    assert o.status == "filled"
    assert float(o.filled_size) == 100.0
    assert o.raw["legs"][0]["status"] == "canceled"


def test_stop_touch_fills_the_LEG_and_cancels_the_parent():
    """ANG KRITIKAL NA KASO (ang binabasa ng adopt path): bumagsak sa stop —
    ang LEG ang may fill sa SARILING presyo; parent canceled na zero fill."""
    a = _adapter()
    res = _place(a, tp=2.60, stop=2.10)
    a.set_quote("MIMI", _q(2.05, 2.07))
    o, _ = a.get_order(res["order_id"])
    assert o.status == "canceled"
    assert float(o.filled_size) == 0.0
    leg = o.raw["legs"][0]
    assert leg["status"] == "filled"
    assert float(leg["filled_qty"]) == 100.0
    assert float(leg["filled_avg_price"]) == 2.05, (
        "konserbatibo: sa mas masama sa stop/bid (bid 2.05 < stop 2.10)"
    )
    assert o.raw["order_class"] == "oco"


def test_the_leg_fill_lands_in_the_fills_ledger():
    a = _adapter()
    res = _place(a)
    a.set_quote("MIMI", _q(2.05, 2.07))
    truth = a.get_position_quantity_truth("MIMI")
    assert truth["quantity"] == -100.0, "ang leg sell ay dapat nasa position math"


def test_cancel_parent_cancels_both_legs():
    """⚠️ OVERSELL INVARIANT: ang clamp ay kinakansela ang parent at inaasahan
    na kasama ang leg — linked cancel."""
    a = _adapter()
    res = _place(a)
    a.cancel_order(res["order_id"])
    o, _ = a.get_order(res["order_id"])
    assert o.raw["legs"][0]["status"] == "canceled"


def test_validation_matches_the_real_adapter():
    a = _adapter()
    assert _place(a, qty="100.5")["ok"] is False, "fractional refuse"
    assert _place(a, tp=2.00, stop=2.10)["ok"] is False, "tp<=stop refuse"
