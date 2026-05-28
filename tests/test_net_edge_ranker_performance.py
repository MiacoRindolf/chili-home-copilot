from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import PaperTrade, ScanPattern, Trade
from app.services.trading.net_edge_ranker import _load_training_pairs


class _FakeQuery:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.filter_calls = 0
        self.order_by_calls = 0
        self.limit_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def order_by(self, *args: object) -> "_FakeQuery":
        self.order_by_calls += 1
        return self

    def limit(self, *args: object) -> "_FakeQuery":
        self.limit_calls += 1
        return self

    def all(self) -> list[SimpleNamespace]:
        return self.rows


class _FakeSession:
    def __init__(
        self,
        *,
        trades: list[SimpleNamespace],
        paper_trades: list[SimpleNamespace],
        patterns: list[SimpleNamespace],
    ) -> None:
        self.rows_by_model = {
            Trade: trades,
            PaperTrade: paper_trades,
            ScanPattern: patterns,
        }
        self.query_calls: dict[object, int] = {Trade: 0, PaperTrade: 0, ScanPattern: 0}
        self.last_query_by_model: dict[object, _FakeQuery] = {}

    def query(self, model: object) -> _FakeQuery:
        self.query_calls[model] = self.query_calls.get(model, 0) + 1
        query = _FakeQuery(self.rows_by_model.get(model, []))
        self.last_query_by_model[model] = query
        return query


def test_load_training_pairs_batches_scan_pattern_lookup() -> None:
    db = _FakeSession(
        trades=[
            SimpleNamespace(scan_pattern_id=1, pnl=25.0),
            SimpleNamespace(scan_pattern_id=2, pnl=-5.0),
            SimpleNamespace(scan_pattern_id=3, pnl=None),
        ],
        paper_trades=[
            SimpleNamespace(scan_pattern_id=2, entry_price=10.0, exit_price=12.0),
            SimpleNamespace(scan_pattern_id=4, entry_price=10.0, exit_price=None),
        ],
        patterns=[
            SimpleNamespace(id=1, oos_win_rate=0.6, win_rate=None, asset_class="stocks"),
            SimpleNamespace(id=2, oos_win_rate=None, win_rate=55.0, asset_class="stocks"),
        ],
    )

    pairs = _load_training_pairs(db, asset_class=None, regime_bucket="risk_on", lookback_days=1)

    assert pairs == [(0.6, 1), (0.55, 0), (0.55, 1)]
    assert db.query_calls[Trade] == 1
    assert db.query_calls[PaperTrade] == 1
    assert db.query_calls[ScanPattern] == 1
    assert db.last_query_by_model[ScanPattern].filter_calls == 1


def test_load_training_pairs_skips_pattern_lookup_when_no_pattern_ids() -> None:
    db = _FakeSession(
        trades=[SimpleNamespace(scan_pattern_id=None, pnl=25.0)],
        paper_trades=[SimpleNamespace(scan_pattern_id=None, entry_price=10.0, exit_price=12.0)],
        patterns=[],
    )

    assert _load_training_pairs(db, asset_class=None, regime_bucket="risk_on") == []
    assert db.query_calls[Trade] == 1
    assert db.query_calls[PaperTrade] == 1
    assert db.query_calls[ScanPattern] == 0
