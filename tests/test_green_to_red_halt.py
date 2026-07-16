"""Ross gap #8: green-to-red session breaker (videos 37/38). Going from green on the day
back to <= $0 is the emotional-hijack walk-away trigger. The profit-giveback halt's floor
(peak * (1 - frac)) sits ABOVE $0, so a true round-trip into the red on a smaller green
day isn't caught — this stricter complement is. Pure-logic unit tests: the realized-PnL
peak/current and the equity-relative cap are monkeypatched so no DB/broker is needed.
"""
from __future__ import annotations

from app.services.trading.momentum_neural import risk_evaluator
from app.services.trading.momentum_neural.risk_evaluator import evaluate_green_to_red_halt


def _patch(monkeypatch, peak, current, cap=200.0):
    # activation = 0.5 * cap = 100.0 with the default cap
    monkeypatch.setattr(risk_evaluator, "equity_relative_daily_loss_cap", lambda *a, **k: cap)
    monkeypatch.setattr(
        risk_evaluator,
        "_daily_realized_pnl_peak_and_current",
        lambda db, uid, **kwargs: (peak, current),
    )


def test_halts_on_full_roundtrip_into_red(monkeypatch):
    _patch(monkeypatch, peak=150.0, current=-5.0)   # peaked +150 (>=100), now red
    r = evaluate_green_to_red_halt(None, user_id=1)
    assert r["armed"] is True and r["halted"] is True
    assert r["activation_threshold_usd"] == 100.0


def test_catches_what_giveback_misses(monkeypatch):
    # peak 150 / current +80: giveback (frac .5 -> floor 75) would NOT halt, and isn't even
    # armed (its activation is the FULL cap 200 > 150). Green-to-red also not halted yet
    # (current 80 > 0). Drop current to -1 -> green-to-red halts where giveback stays silent.
    _patch(monkeypatch, peak=150.0, current=80.0)
    assert evaluate_green_to_red_halt(None, user_id=1)["halted"] is False
    _patch(monkeypatch, peak=150.0, current=-1.0)
    assert evaluate_green_to_red_halt(None, user_id=1)["halted"] is True


def test_breakeven_is_a_full_giveback(monkeypatch):
    _patch(monkeypatch, peak=150.0, current=0.0)   # gave it ALL back to flat
    assert evaluate_green_to_red_halt(None, user_id=1)["halted"] is True


def test_tiny_green_does_not_arm(monkeypatch):
    _patch(monkeypatch, peak=40.0, current=-10.0)  # peak 40 < 100 activation -> not armed
    r = evaluate_green_to_red_halt(None, user_id=1)
    assert r["armed"] is False and r["halted"] is False


def test_still_green_not_halted(monkeypatch):
    _patch(monkeypatch, peak=150.0, current=120.0)
    assert evaluate_green_to_red_halt(None, user_id=1)["halted"] is False


def test_straight_red_day_not_halted(monkeypatch):
    _patch(monkeypatch, peak=0.0, current=-50.0)   # never green -> green-to-red N/A
    r = evaluate_green_to_red_halt(None, user_id=1)
    assert r["armed"] is False and r["halted"] is False


def test_disabled_when_cap_zero(monkeypatch):
    _patch(monkeypatch, peak=150.0, current=-5.0, cap=0.0)  # activation 0 -> never arms
    assert evaluate_green_to_red_halt(None, user_id=1)["armed"] is False
