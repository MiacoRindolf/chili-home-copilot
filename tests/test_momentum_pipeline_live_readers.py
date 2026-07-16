from __future__ import annotations

from datetime import datetime

from app.config import settings
from app.services.trading.momentum_neural.pipeline import (
    _live_book_imbalance,
    _live_flow_slope,
    _live_ofi_microprice,
    _live_realized_vol,
    _live_trade_flow,
)


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Db:
    def __init__(self) -> None:
        self.calls = []
        self.depth_rows = [
            {
                "observed_at": datetime(2026, 7, 1, 13, 0, 2),
                "bid_top": 4.98,
                "ask_top": 5.00,
                "bid5_size": 9000.0,
                "ask5_size": 3000.0,
                "imbalance5": 0.50,
            },
            {
                "observed_at": datetime(2026, 7, 1, 13, 0, 1),
                "bid_top": 4.97,
                "ask_top": 4.99,
                "bid5_size": 5000.0,
                "ask5_size": 5000.0,
                "imbalance5": 0.0,
            },
            {
                "observed_at": datetime(2026, 7, 1, 13, 0, 0),
                "bid_top": 4.96,
                "ask_top": 4.98,
                "bid5_size": 4000.0,
                "ask5_size": 6000.0,
                "imbalance5": -0.20,
            },
        ]
        self.flow_rows = [
            {"observed_at": datetime(2026, 7, 1, 13, 0, 0), "price": 4.97, "size": 100.0, "bid": 4.97, "ask": 4.99},
            {"observed_at": datetime(2026, 7, 1, 13, 0, 1), "price": 5.00, "size": 300.0, "bid": 4.98, "ask": 5.00},
            {"observed_at": datetime(2026, 7, 1, 13, 0, 2), "price": 5.01, "size": 300.0, "bid": 4.99, "ask": 5.01},
        ]

    def execute(self, sql, _params):
        text = str(sql)
        self.calls.append((text, dict(_params)))
        if "iqfeed_depth_snapshots" in text and "ORDER BY observed_at ASC" in text:
            return _Rows(list(reversed(self.depth_rows)))
        if "iqfeed_depth_snapshots" in text:
            return _Rows(self.depth_rows)
        if "iqfeed_trade_ticks" in text:
            return _Rows(self.flow_rows)
        return _Rows([])


def test_live_ofi_readers_return_fresh_iqfeed_metrics() -> None:
    db = _Db()
    as_of = datetime(2026, 7, 1, 13, 0, 3)

    ofi, micro = _live_ofi_microprice("JEM", db=db, as_of=as_of)
    book = _live_book_imbalance("JEM", db=db, as_of=as_of)
    slope = _live_flow_slope("JEM", db=db, as_of=as_of)
    flow = _live_trade_flow("JEM", db=db, as_of=as_of)
    vol = _live_realized_vol("JEM", db=db, as_of=as_of)

    assert ofi == 0.7
    assert micro is not None
    assert book is not None
    assert slope == {"ofi_level": 0.5, "ofi_slope": 0.7}
    assert flow == 5 / 7
    assert vol is not None and vol > 0


def test_live_ofi_reader_missing_l2_stays_missing_not_confirming() -> None:
    class EmptyDb:
        def execute(self, _sql, _params):
            return _Rows([])

    ofi, micro = _live_ofi_microprice("JEM", db=EmptyDb(), as_of=datetime(2026, 7, 1, 13, 0, 3))

    assert ofi is None
    assert micro is None


def test_live_trade_readers_use_first_class_settings(monkeypatch) -> None:
    db = _Db()
    as_of = datetime(2026, 7, 1, 13, 0, 3)
    monkeypatch.setattr(settings, "chili_momentum_trade_flow_window_seconds", 7.0)
    monkeypatch.setattr(settings, "chili_momentum_trade_flow_tick_limit", 11)
    monkeypatch.setattr(settings, "chili_momentum_flow_slope_window_seconds", 9.0)
    monkeypatch.setattr(settings, "chili_momentum_flow_slope_snapshot_limit", 13)
    monkeypatch.setattr(settings, "chili_momentum_realized_vol_window_seconds", 17.0)
    monkeypatch.setattr(settings, "chili_momentum_realized_vol_tick_limit", 19)
    monkeypatch.setattr(settings, "chili_momentum_realized_vol_min_ticks", 3)

    assert _live_trade_flow("JEM", db=db, as_of=as_of) is not None
    trade_params = db.calls[-1][1]
    assert trade_params["window_s"] == 7.0
    assert trade_params["limit"] == 11

    assert _live_flow_slope("JEM", db=db, as_of=as_of) is not None
    slope_params = db.calls[-1][1]
    assert slope_params["window_s"] == 9.0
    assert slope_params["limit"] == 13

    assert _live_realized_vol("JEM", db=db, as_of=as_of) is not None
    vol_params = db.calls[-1][1]
    assert vol_params["window_s"] == 17.0
    assert vol_params["limit"] == 19
