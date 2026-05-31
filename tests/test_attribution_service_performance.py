from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.trading import PaperTrade, ScanPattern, Trade
from app.services.trading.attribution_service import (
    _paper_directional_outcome,
    _scan_patterns_by_id,
    _trade_directional_outcome,
    live_vs_research_by_pattern,
    post_trade_review,
)


class _FakeQuery:
    def __init__(self, rows: list[SimpleNamespace]) -> None:
        self.rows = rows
        self.filter_calls = 0

    def filter(self, *args: object) -> "_FakeQuery":
        self.filter_calls += 1
        return self

    def order_by(self, *args: object) -> "_FakeQuery":
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


def test_attribution_live_directional_outcome_prefers_partial_aware_return() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert _trade_directional_outcome(trade) == pytest.approx(4.0)


def test_attribution_paper_directional_outcome_prefers_partial_aware_return() -> None:
    trade = SimpleNamespace(
        entry_price=1.25,
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        pnl_pct=-8.0,
        direction="long",
        signal_json={"asset_type": "options", "option_meta": {"strike": 500.0}},
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
    )

    assert _paper_directional_outcome(trade) == pytest.approx(4.0)


class _FakeAttributionSession:
    def __init__(
        self,
        *,
        trades: list[SimpleNamespace],
        paper_trades: list[SimpleNamespace] | None = None,
        patterns: list[SimpleNamespace],
    ) -> None:
        self.trades = trades
        self.paper_trades = list(paper_trades or [])
        self.patterns = patterns

    def query(self, model: object) -> _FakeQuery:
        if model is Trade:
            return _FakeQuery(self.trades)
        if model is PaperTrade:
            return _FakeQuery(self.paper_trades)
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
    assert row["live_win_sample_n"] == 1
    assert row["live_win_rate_pct"] == pytest.approx(100.0)
    assert row["live_return_sample_n"] == 1
    assert row["live_avg_return_pct"] == pytest.approx(16.0)
    assert row["live_avg_tca_cost_pct"] == pytest.approx(0.30)
    assert row["live_avg_net_return_pct"] == pytest.approx(15.70)
    assert row["live_avg_pnl"] == pytest.approx(40.0)


def test_live_vs_research_live_win_rate_uses_confirmed_return_when_pnl_missing() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="option-alpha",
        promotion_status="pilot",
        win_rate=0.6,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    trade = SimpleNamespace(
        id=1002,
        user_id=7,
        status="closed",
        scan_pattern_id=42,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=None,
        pnl_pct=9999.0,
        asset_kind="option",
        tags=None,
        indicator_snapshot={
            "asset_type": "options",
            "option_meta": {"price_domain": "option_premium"},
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
        exit_date=datetime(2026, 5, 30, 15, 30),
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )
    db = _FakeAttributionSession(trades=[trade], patterns=[pattern])

    out = live_vs_research_by_pattern(db, 7, days=30, limit=10)

    row = out["patterns"][0]
    assert row["live_closed_trades"] == 1
    assert row["live_win_sample_n"] == 1
    assert row["live_win_rate_pct"] == pytest.approx(100.0)
    assert row["live_return_sample_n"] == 1
    assert row["live_avg_return_pct"] == pytest.approx(16.0)
    assert row["live_avg_tca_cost_pct"] == pytest.approx(0.30)
    assert row["live_avg_net_return_pct"] == pytest.approx(15.70)
    assert row["live_total_pnl"] == pytest.approx(0.0)
    assert row["live_avg_pnl"] == pytest.approx(0.0)


def test_post_trade_review_uses_contract_aware_outcomes_when_pnl_missing() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="option-alpha",
        promotion_status="pilot",
        win_rate=0.5,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    snapshot = {
        "asset_type": "options",
        "option_meta": {"price_domain": "option_premium"},
        "price_domains": {
            "entry_price": "option_premium",
            "exit_price": "option_premium",
        },
    }
    trades = [
        SimpleNamespace(
            id=1101,
            user_id=7,
            status="closed",
            scan_pattern_id=42,
            ticker="SPY",
            direction="long",
            entry_price=1.25,
            exit_price=1.45,
            quantity=2.0,
            pnl=None,
            asset_kind="option",
            tags=None,
            indicator_snapshot=snapshot,
            exit_date=datetime(2026, 5, 30, 15, 30),
            tca_entry_slippage_bps=5.0,
            tca_exit_slippage_bps=5.0,
        ),
        SimpleNamespace(
            id=1102,
            user_id=7,
            status="closed",
            scan_pattern_id=42,
            ticker="SPY",
            direction="long",
            entry_price=1.00,
            exit_price=0.90,
            quantity=2.0,
            pnl=None,
            asset_kind="option",
            tags=None,
            indicator_snapshot=snapshot,
            exit_date=datetime(2026, 5, 30, 15, 31),
            tca_entry_slippage_bps=5.0,
            tca_exit_slippage_bps=5.0,
        ),
        SimpleNamespace(
            id=1103,
            user_id=7,
            status="closed",
            scan_pattern_id=42,
            ticker="SPY",
            direction="long",
            entry_price=2.00,
            exit_price=2.40,
            quantity=1.0,
            pnl=None,
            asset_kind="option",
            tags=None,
            indicator_snapshot=snapshot,
            exit_date=datetime(2026, 5, 30, 15, 32),
            tca_entry_slippage_bps=5.0,
            tca_exit_slippage_bps=5.0,
        ),
    ]
    db = _FakeAttributionSession(trades=trades, patterns=[pattern])

    out = post_trade_review(db, 7, days=30)

    review = out["review"]
    assert review["total_trades"] == 3
    assert review["win_sample_n"] == 3
    assert review["wins"] == 2
    assert review["losses"] == 1
    assert review["live_win_rate_pct"] == pytest.approx(66.7)
    assert review["max_consecutive_losses"] == 1
    assert review["total_pnl"] == pytest.approx(0.0)
    assert review["avg_pnl"] == pytest.approx(0.0)

    outperformer = review["outperforming_patterns"][0]
    assert outperformer["scan_pattern_id"] == 42
    assert outperformer["live_trades"] == 3
    assert outperformer["live_win_sample_n"] == 3
    assert outperformer["live_win_rate_pct"] == pytest.approx(66.7)
    assert outperformer["research_win_rate_pct"] == pytest.approx(55.0)
    assert outperformer["delta_pct"] == pytest.approx(11.7)
    assert review["underperforming_patterns"] == []
    assert out["feedback_signals"][0]["signal"] == "upweight"


def test_live_vs_research_reports_contract_aware_paper_option_return_after_tca() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="option-alpha",
        promotion_status="shadow",
        win_rate=0.6,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    paper_trade = SimpleNamespace(
        id=2001,
        user_id=7,
        status="closed",
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=716.0,  # deliberately underlying-like, should not drive return
        quantity=2.0,
        pnl=40.0,
        pnl_pct=9999.0,
        signal_json={"asset_class": "robinhood_options"},
        exit_date=datetime(2026, 5, 30, 15, 30),
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )
    db = _FakeAttributionSession(
        trades=[],
        paper_trades=[paper_trade],
        patterns=[pattern],
    )

    out = live_vs_research_by_pattern(db, 7, days=30, limit=10)

    row = out["patterns"][0]
    assert row["scan_pattern_id"] == 42
    assert row["live_closed_trades"] == 0
    assert row["paper_closed_trades"] == 1
    assert row["paper_win_sample_n"] == 1
    assert row["paper_win_rate_pct"] == pytest.approx(100.0)
    assert row["paper_return_sample_n"] == 1
    assert row["paper_avg_return_pct"] == pytest.approx(16.0)
    assert row["paper_avg_tca_cost_pct"] == pytest.approx(0.30)
    assert row["paper_avg_net_return_pct"] == pytest.approx(15.70)
    assert row["paper_avg_pnl"] == pytest.approx(40.0)


def test_live_vs_research_paper_win_rate_uses_confirmed_return_when_pnl_missing() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="option-alpha",
        promotion_status="shadow",
        win_rate=0.6,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    paper_trade = SimpleNamespace(
        id=2002,
        user_id=7,
        status="closed",
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=1.45,
        quantity=2.0,
        pnl=None,
        pnl_pct=9999.0,
        signal_json={
            "asset_class": "contract-options",
            "option_meta": {"price_domain": "option_premium"},
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
        exit_date=datetime(2026, 5, 30, 15, 30),
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )
    db = _FakeAttributionSession(
        trades=[],
        paper_trades=[paper_trade],
        patterns=[pattern],
    )

    out = live_vs_research_by_pattern(db, 7, days=30, limit=10)

    row = out["patterns"][0]
    assert row["paper_closed_trades"] == 1
    assert row["paper_win_sample_n"] == 1
    assert row["paper_win_rate_pct"] == pytest.approx(100.0)
    assert row["paper_return_sample_n"] == 1
    assert row["paper_avg_return_pct"] == pytest.approx(16.0)
    assert row["paper_total_pnl"] is None
    assert row["paper_avg_pnl"] is None
