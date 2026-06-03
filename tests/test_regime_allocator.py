from __future__ import annotations

from types import SimpleNamespace

import pytest


class _PatternQuery:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self) -> list[object]:
        return list(self._rows)


class _PatternDb:
    def __init__(self, rows: list[object]) -> None:
        self._rows = rows

    def query(self, *_args, **_kwargs):
        return _PatternQuery(self._rows)


def _pattern(**overrides):
    data = {
        "id": 1,
        "name": "Momentum Pattern",
        "rules_json": {},
        "regime_affinity_json": {},
        "confidence": 0.5,
        "oos_win_rate": None,
        "win_rate": None,
    }
    data.update(overrides)
    return SimpleNamespace(**data)


def test_regime_allocator_preserves_zero_oos_win_rate(monkeypatch):
    from app.services.trading import market_data
    from app.services.trading import regime_allocator

    monkeypatch.setattr(
        market_data,
        "get_market_regime",
        lambda: {"composite": "risk_on"},
    )
    pat = _pattern(
        id=7,
        confidence=1.0,
        oos_win_rate=0.0,
        win_rate=1.0,
    )

    out = regime_allocator.compute_regime_allocations(
        _PatternDb([pat]),
        capital=100_000,
    )

    assert out["allocations"][0]["pattern_id"] == 7
    assert out["allocations"][0]["confidence"] == 1.0
    assert out["allocations"][0]["score"] == 0.0
    assert out["total_deployed"] == 0


def test_regime_allocator_normalizes_percent_probability_inputs(monkeypatch):
    from app.services.trading import market_data
    from app.services.trading import regime_allocator

    monkeypatch.setattr(
        market_data,
        "get_market_regime",
        lambda: {"composite": "cautious"},
    )
    pat = _pattern(
        id=8,
        name="Mean Reversion Pattern",
        confidence=75.0,
        oos_win_rate=None,
        win_rate=60.0,
    )

    out = regime_allocator.compute_regime_allocations(
        _PatternDb([pat]),
        capital=100_000,
    )

    alloc = out["allocations"][0]
    assert alloc["pattern_id"] == 8
    assert alloc["confidence"] == 0.75
    assert alloc["score"] == pytest.approx(0.45)
    assert alloc["capital"] == 60_000
