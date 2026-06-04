from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

from app.models.trading import MarketSnapshot
from app.services.trading.learning import (
    _indicator_payload_dict,
    _load_recent_labeled_snapshots_for_mining,
)


def test_indicator_payload_accepts_jsonb_dict_and_string():
    payload = {"rsi": {"value": 55}}

    assert _indicator_payload_dict(payload) == payload
    assert _indicator_payload_dict(json.dumps(payload)) == payload
    assert _indicator_payload_dict("[1, 2, 3]") == {}
    assert _indicator_payload_dict("{bad") == {}


def test_labeled_snapshot_mining_loader_is_bounded_and_skinny():
    class _Query:
        def __init__(self):
            self.limit_value = None
            self.filtered = False
            self.ordered = False

        def filter(self, *args):
            self.filtered = True
            return self

        def order_by(self, *args):
            self.ordered = True
            return self

        def limit(self, value):
            self.limit_value = value
            return self

        def all(self):
            return [
                SimpleNamespace(
                    id=3,
                    ticker="NEW",
                    snapshot_date=datetime.utcnow(),
                    close_price=13.0,
                    indicator_data={"rsi": {"value": 60}},
                    future_return_5d=4.0,
                    future_return_10d=5.0,
                ),
                SimpleNamespace(
                    id=2,
                    ticker="NOPAY",
                    snapshot_date=datetime.utcnow(),
                    close_price=12.0,
                    indicator_data=None,
                    future_return_5d=3.0,
                    future_return_10d=None,
                ),
                SimpleNamespace(
                    id=1,
                    ticker="OLD",
                    snapshot_date=datetime.utcnow(),
                    close_price=10.0,
                    indicator_data={"rsi": {"value": 45}},
                    future_return_5d=1.2,
                    future_return_10d=2.4,
                ),
            ]

    class _Session:
        def __init__(self):
            self.query_cols = None
            self.query_obj = _Query()

        def query(self, *cols):
            self.query_cols = cols
            return self.query_obj

    db = _Session()
    rows = _load_recent_labeled_snapshots_for_mining(db, limit=10)
    ids = [row.id for row in rows]

    assert all(col is not MarketSnapshot for col in db.query_cols)
    assert any(getattr(col, "key", None) == "indicator_data" for col in db.query_cols)
    assert db.query_obj.filtered is True
    assert db.query_obj.ordered is True
    assert db.query_obj.limit_value == 10
    assert ids == [3, 1]
    assert rows[0].ticker == "NEW"
    assert rows[0].indicator_data == {"rsi": {"value": 60}}


def test_labeled_snapshot_mining_loader_rolls_back_and_degrades():
    class _BrokenSession:
        rolled_back = False

        def query(self, *args, **kwargs):
            raise RuntimeError("server closed the connection unexpectedly")

        def rollback(self):
            self.rolled_back = True

    db = _BrokenSession()

    assert _load_recent_labeled_snapshots_for_mining(db, limit=5000) == []
    assert db.rolled_back is True


def test_mine_patterns_releases_session_before_ohlcv_fetch(monkeypatch):
    from app.services.trading import learning

    events: list[str] = []

    class _Session:
        def rollback(self):
            events.append("rollback")

    class _Budget:
        def remaining_ohlcv(self):
            return 1

        def try_ohlcv(self, *args, **kwargs):
            return True

        def record_miner_error(self, *args, **kwargs):
            return None

    def _mine_from_history(*args, **kwargs):
        events.append("fetch")
        return []

    monkeypatch.setattr(learning, "provider_egress_available_for_brain_work", lambda: True)
    monkeypatch.setattr(learning, "_mine_from_history", _mine_from_history)
    monkeypatch.setattr(learning, "_load_recent_labeled_snapshots_for_mining", lambda *a, **k: [])
    monkeypatch.setattr(learning, "get_volatility_regime", lambda: {"regime": "unknown"})

    assert learning.mine_patterns(
        _Session(),
        user_id=None,
        ticker_universe=["SPY"],
        budget=_Budget(),
    ) == []
    assert events == ["rollback", "fetch"]
