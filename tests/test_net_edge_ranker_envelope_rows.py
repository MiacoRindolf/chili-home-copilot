from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import PaperTrade
from app.services.trading import net_edge_ranker


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def limit(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _Db:
    def __init__(self, paper_rows=None):
        self.paper_rows = list(paper_rows or [])

    def query(self, model):
        if model is PaperTrade:
            return _Query(self.paper_rows)
        raise AssertionError(f"unexpected query model: {model!r}")


def test_load_training_pairs_uses_envelope_helper_for_live_rows(monkeypatch):
    monkeypatch.setattr(
        net_edge_ranker,
        "load_net_edge_training_envelope_rows",
        lambda _db, *, since, limit: [
            SimpleNamespace(scan_pattern_id=1, pnl=5.0),
            SimpleNamespace(scan_pattern_id=2, pnl=-1.0),
            SimpleNamespace(scan_pattern_id=3, pnl=9.0),
            SimpleNamespace(scan_pattern_id=None, pnl=99.0),
            SimpleNamespace(scan_pattern_id=4, pnl=None),
        ],
    )
    monkeypatch.setattr(
        net_edge_ranker,
        "_scan_patterns_by_id",
        lambda _db, ids: {
            1: SimpleNamespace(id=1, oos_win_rate=0.7, win_rate=None, asset_class="stocks"),
            2: SimpleNamespace(id=2, oos_win_rate=None, win_rate=45.0, asset_class="stocks"),
            3: SimpleNamespace(id=3, oos_win_rate=0.9, win_rate=None, asset_class="crypto"),
        },
    )

    pairs = net_edge_ranker._load_training_pairs(
        _Db(),
        asset_class="stocks",
        regime_bucket="risk_on",
        lookback_days=30,
    )

    assert pairs == [(0.7, 1), (0.45, 0)]


def test_net_edge_ranker_no_longer_reads_trade_orm_source():
    source = net_edge_ranker.__loader__.get_source(net_edge_ranker.__name__)

    assert "db.query(Trade)" not in source
    assert "from ...models.trading import (\n" in source
    assert "    Trade,\n" not in source
    assert "load_net_edge_training_envelope_rows" in source
