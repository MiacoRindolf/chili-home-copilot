"""Streak-adaptive risk dial — self-relative multiplier on per-trade max loss."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural.risk_policy import streak_risk_multiplier


class _Q:
    def __init__(self, pnls):
        self._p = pnls

    def filter(self, *a, **k): return self
    def order_by(self, *a, **k): return self
    def limit(self, n): self._p = self._p[:n]; return self
    def all(self): return [(p,) for p in self._p]


def _db(pnls):
    return SimpleNamespace(query=lambda *a, **k: _Q(list(pnls)))


def test_neutral_with_insufficient_history():
    m, meta = streak_risk_multiplier(_db([100, -50]))
    assert m == 1.0 and meta["reason"] == "insufficient_history"


def test_hot_streak_sizes_up():
    m, meta = streak_risk_multiplier(_db([200, 150, -50, 300, 120, 90, -30, 250, 180, 60]))  # 8/10 wins
    assert m == pytest.approx(1.3)
    assert meta["win_rate"] == pytest.approx(0.8)


def test_cold_streak_sizes_down():
    m, _ = streak_risk_multiplier(_db([-50, 100, -30, -80, -20, 110, -40, -60, 90, -10]))  # 3/10 wins
    assert m == pytest.approx(0.8)


def test_three_consecutive_losses_hard_floor():
    # newest-first: 3 straight losses despite decent overall rate -> 0.5
    m, meta = streak_risk_multiplier(_db([-50, -30, -80, 200, 150, 120, 90, 250, 180, 60]))
    assert m == 0.5 and meta["consecutive_losses"] == 3


def test_bounds_never_exceeded():
    m_hi, _ = streak_risk_multiplier(_db([10] * 10))
    m_lo, _ = streak_risk_multiplier(_db([-10] * 10))
    assert m_hi == 1.5 and m_lo == 0.5


def test_error_fails_neutral():
    class _Boom:
        def query(self, *a, **k): raise RuntimeError("db down")
    m, meta = streak_risk_multiplier(_Boom())
    assert m == 1.0 and meta["reason"] == "error_fail_neutral"
