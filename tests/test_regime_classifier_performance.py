from __future__ import annotations

import sys
from datetime import date, datetime
from types import SimpleNamespace

from app.models.trading import RegimeSnapshot, ScanPattern, Trade
from app.services.trading.regime_classifier import (
    _latest_regime_snapshots_by_date,
    _scan_patterns_by_id,
    build_regime_scanner_sharpe_heatmap,
)


class _FakeQuery:
    def __init__(self, rows: list[SimpleNamespace], first_row: SimpleNamespace | None = None) -> None:
        self.rows = rows
        self.first_row = first_row
        self.filter_calls = 0
        self.order_by_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def order_by(self, *args: object) -> "_FakeQuery":
        self.order_by_calls += 1
        return self

    def all(self) -> list[SimpleNamespace]:
        return self.rows

    def first(self) -> SimpleNamespace | None:
        return self.first_row


class _FakeSession:
    def __init__(
        self,
        *,
        trades: list[SimpleNamespace] | None = None,
        patterns: list[SimpleNamespace] | None = None,
        regimes: list[SimpleNamespace] | None = None,
        latest_regime: SimpleNamespace | None = None,
    ) -> None:
        self.trades = trades or []
        self.patterns = patterns or []
        self.regimes = regimes or []
        self.latest_regime = latest_regime
        self.queries: list[tuple[object, _FakeQuery]] = []
        self.regime_query_count = 0

    def query(self, model: object) -> _FakeQuery:
        if model is Trade:
            query = _FakeQuery(self.trades)
        elif model is ScanPattern:
            query = _FakeQuery(self.patterns)
        elif model is RegimeSnapshot:
            self.regime_query_count += 1
            if self.regime_query_count == 1:
                query = _FakeQuery(self.regimes)
            else:
                query = _FakeQuery([], first_row=self.latest_regime)
        else:
            raise AssertionError(f"unexpected query: {model!r}")
        self.queries.append((model, query))
        return query


def test_scan_patterns_by_id_batches_lookup() -> None:
    first = SimpleNamespace(id=3, name="Breakout")
    duplicate = SimpleNamespace(id=3, name="Duplicate")
    other = SimpleNamespace(id=5, name="Momentum")
    db = _FakeSession(patterns=[first, duplicate, other])

    result = _scan_patterns_by_id(db, {3, 5})  # type: ignore[arg-type]

    assert result == {3: duplicate, 5: other}
    assert len(db.queries) == 1
    assert db.queries[0][0] is ScanPattern
    assert db.queries[0][1].filter_calls == 1


def test_latest_regime_snapshots_by_date_batches_lookup() -> None:
    newer = SimpleNamespace(as_of=datetime(2026, 5, 28, 15), regime="bull")
    older = SimpleNamespace(as_of=datetime(2026, 5, 28, 9), regime="bear")
    other = SimpleNamespace(as_of=datetime(2026, 5, 27, 15), regime="chop")
    db = _FakeSession(regimes=[newer, older, other])

    result = _latest_regime_snapshots_by_date(
        db,  # type: ignore[arg-type]
        {date(2026, 5, 28), date(2026, 5, 27)},
    )

    assert result == {date(2026, 5, 28): newer, date(2026, 5, 27): other}
    assert len(db.queries) == 1
    assert db.queries[0][0] is RegimeSnapshot
    assert db.queries[0][1].filter_calls == 1
    assert db.queries[0][1].order_by_calls == 1


def test_regime_scanner_heatmap_batches_trade_inputs(monkeypatch) -> None:
    day = datetime(2026, 5, 28, 14)
    trade_a = SimpleNamespace(
        scan_pattern_id=3,
        entry_date=day,
        entry_price=100.0,
        exit_price=110.0,
        direction="long",
    )
    trade_b = SimpleNamespace(
        scan_pattern_id=3,
        entry_date=day,
        entry_price=100.0,
        exit_price=90.0,
        direction="short",
    )
    pattern = SimpleNamespace(
        id=3,
        name="Opening Range Breakout",
        timeframe="1d",
        hypothesis_family=None,
        origin="builtin",
    )
    regime = SimpleNamespace(as_of=day, regime="bull", model_version="v1")
    db = _FakeSession(
        trades=[trade_a, trade_b],
        patterns=[pattern],
        regimes=[regime],
        latest_regime=regime,
    )
    monkeypatch.setitem(
        sys.modules,
        "app.services.trading.promotion_gate",
        SimpleNamespace(
            SCANNER_BUCKETS=("swing", "day", "breakout", "momentum", "patterns"),
            infer_scanner_bucket=lambda _pattern: "breakout",
        ),
    )

    result = build_regime_scanner_sharpe_heatmap(db)  # type: ignore[arg-type]

    queried_models = [model for model, _query in db.queries]
    assert queried_models == [Trade, ScanPattern, RegimeSnapshot, RegimeSnapshot]
    assert result["ok"] is True
    assert result["n_trades_matrix"][0][2] == 2
