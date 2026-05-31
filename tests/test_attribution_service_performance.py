from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.trading import ScanPattern, Trade
from app.services.trading.attribution_service import (
    _scan_patterns_by_id,
    live_vs_research_by_pattern,
)


class _FakeQuery:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def all(self) -> list[SimpleNamespace]:
        return self.rows


class _FakeSession:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.query_calls = 0
        self.last_query: _FakeQuery | None = None

    def query(self, model: object) -> _FakeQuery:
        assert model is ScanPattern
        self.query_calls += 1
        self.last_query = _FakeQuery(self.rows)
        return self.last_query


def test_scan_patterns_by_id_batches_lookup() -> None:
    first = SimpleNamespace(id=2, name="breakout")
    duplicate = SimpleNamespace(id=2, name="duplicate")
    other = SimpleNamespace(id=5, name="pullback")
    db = _FakeSession([first, duplicate, other])

    result = _scan_patterns_by_id(db, {0, 2, 5})

    assert result == {2: duplicate, 5: other}
    assert db.query_calls == 1
    assert db.last_query is not None
    assert db.last_query.filter_calls == 1


def test_scan_patterns_by_id_skips_empty_lookup() -> None:
    db = _FakeSession([])

    assert _scan_patterns_by_id(db, {0}) == {}
    assert db.query_calls == 0


class _FakeAttributionSession:
    def __init__(
        self,
        *,
        trades: list[SimpleNamespace],
        patterns: list[SimpleNamespace],
    ) -> None:
        self.trades = trades
        self.patterns = patterns

    def query(self, model: object) -> _FakeQuery:
        if model is Trade:
            return _FakeQuery(self.trades)
        if model is ScanPattern:
            return _FakeQuery(self.patterns)
        raise AssertionError(f"unexpected model query: {model!r}")


def test_live_vs_research_reports_contract_aware_option_return_after_tca() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="option-alpha",
        promotion_status="pilot",
        win_rate=0.6,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    trade = SimpleNamespace(
        id=1001,
        user_id=7,
        status="closed",
        scan_pattern_id=42,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=716.0,  # deliberately underlying-like, should not drive return
        quantity=2.0,
        pnl=40.0,
        asset_kind="option",
        tags=None,
        indicator_snapshot={"asset_type": "options"},
        exit_date=datetime(2026, 5, 30, 15, 30),
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )
    db = _FakeAttributionSession(trades=[trade], patterns=[pattern])

    out = live_vs_research_by_pattern(db, 7, days=30, limit=10)

    row = out["patterns"][0]
    assert row["scan_pattern_id"] == 42
    assert row["live_return_sample_n"] == 1
    assert row["live_avg_return_pct"] == pytest.approx(16.0)
    assert row["live_avg_tca_cost_pct"] == pytest.approx(0.30)
    assert row["live_avg_net_return_pct"] == pytest.approx(15.70)
    assert row["live_avg_pnl"] == pytest.approx(40.0)
