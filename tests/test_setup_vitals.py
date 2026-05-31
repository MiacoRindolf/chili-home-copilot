"""Unit tests for setup_vitals trajectory scoring."""
from __future__ import annotations

import inspect

from app.services.trading import setup_vitals
from app.services.trading.setup_vitals import (
    _compute_vitals_from_flats,
    _normalized_slope,
    monitored_tickers_for_vitals,
)


def test_normalized_slope_trend_up():
    vals = [10.0, 11.0, 12.0, 13.0]
    s = _normalized_slope(vals)
    assert s > 0


def test_vitals_from_synthetic_flats():
    flats = []
    for i in range(8):
        rsi = 40.0 + i * 2
        flats.append({
            "price": 100.0 + i,
            "rsi_14": rsi,
            "macd_hist": 0.01 * i,
            "ema_20": 99.0 + i * 0.5,
            "ema_50": 95.0 + i * 0.3,
            "stoch_k": 30 + i,
            "obv": 1e6 + i * 100,
            "bb_pct_b": 0.4 + i * 0.02,
        })
    v = _compute_vitals_from_flats(flats, source="test")
    assert -1.0 <= v.momentum_score <= 1.0
    assert 0.0 <= v.composite_health <= 1.0
    assert "rsi_14" in v.trajectory_details


class _PendingAlertQuery:
    def __init__(self, rows):
        self.rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def distinct(self):
        return self

    def limit(self, _limit):
        return self

    def __iter__(self):
        return iter(self.rows)


class _PendingAlertDb:
    def __init__(self, rows):
        self.rows = rows

    def query(self, *_args, **_kwargs):
        return _PendingAlertQuery(self.rows)


def test_monitored_tickers_for_vitals_uses_envelope_tickers(monkeypatch):
    monkeypatch.setattr(
        setup_vitals,
        "load_open_setup_vitals_envelope_tickers",
        lambda _db: ["abc", "xyz"],
    )
    db = _PendingAlertDb([("abc",), ("pnd",)])

    tickers = monitored_tickers_for_vitals(db)

    assert tickers == ["ABC", "PND", "XYZ"]


def test_monitored_tickers_for_vitals_has_no_trade_orm_reader():
    source = inspect.getsource(monitored_tickers_for_vitals)

    assert "db.query(Trade" not in source
    assert "from ...models.trading import BreakoutAlert, Trade" not in source
