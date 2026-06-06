"""Ross-style reward:risk on the momentum target.

stop_target_prices now anchors the TARGET to the ACTUAL stop distance x a
reward:risk multiple (default 2.0), fixing the old ~1.3-1.5:1 that sat below
Ross's strict 2:1. The R:R is the single documented, learnable knob.
"""
from __future__ import annotations

import pytest

from app.config import settings
from app.services.trading.momentum_neural.paper_execution import stop_target_prices


def test_target_is_2to1_of_actual_stop_distance(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0)
    entry = 100.0
    stop, target = stop_target_prices(entry, atr_pct=0.02, side_long=True)
    stop_dist = entry - stop
    assert stop_dist > 0
    assert (target - entry) == pytest.approx(2.0 * stop_dist)  # exactly 2:1


def test_2to1_holds_even_at_the_stop_floor(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0)
    entry = 100.0
    # tiny ATR -> stop hits the 0.3% floor; target must still be 2x that distance
    stop, target = stop_target_prices(entry, atr_pct=0.0001, side_long=True)
    stop_dist = entry - stop
    assert stop_dist == pytest.approx(0.003 * entry)  # 0.3% floor
    assert (target - entry) == pytest.approx(2.0 * stop_dist)


def test_reward_risk_override_raises_target(monkeypatch) -> None:
    entry = 100.0
    stop, target = stop_target_prices(entry, atr_pct=0.02, reward_risk=3.0)
    assert (target - entry) == pytest.approx(3.0 * (entry - stop))


def test_bad_reward_risk_falls_back_to_2(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0)
    entry = 100.0
    for bad in (0.0, -1.0, float("nan"), float("inf")):
        stop, target = stop_target_prices(entry, atr_pct=0.02, reward_risk=bad)
        assert (target - entry) == pytest.approx(2.0 * (entry - stop))


def test_short_side_2to1(monkeypatch) -> None:
    monkeypatch.setattr(settings, "chili_momentum_risk_reward_risk_ratio", 2.0)
    entry = 100.0
    stop, target = stop_target_prices(entry, atr_pct=0.02, side_long=False)
    risk = stop - entry  # short: stop is above entry
    assert risk > 0
    assert (entry - target) == pytest.approx(2.0 * risk)
