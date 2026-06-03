from __future__ import annotations

import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from app.models.trading import AutoTraderRun, PaperTrade, ScanPattern, Trade
from app.services.trading.attribution_service import (
    _execution_drag_report_from_rows,
    _paper_directional_outcome,
    _scan_patterns_by_id,
    _trade_directional_outcome,
    execution_alpha_drag_report,
    execution_alpha_drag_followup_candidates,
    live_vs_research_by_pattern,
    post_trade_review,
    queue_execution_alpha_drag_followups,
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
        autotrader_runs: list[SimpleNamespace] | None = None,
        patterns: list[SimpleNamespace],
    ) -> None:
        self.trades = trades
        self.paper_trades = list(paper_trades or [])
        self.autotrader_runs = list(autotrader_runs or [])
        self.patterns = patterns

    def query(self, model: object) -> _FakeQuery:
        if model is Trade:
            return _FakeQuery(self.trades)
        if model is PaperTrade:
            return _FakeQuery(self.paper_trades)
        if model is AutoTraderRun:
            return _FakeQuery(self.autotrader_runs)
        if model is ScanPattern:
            return _FakeQuery(self.patterns)
        raise AssertionError(f"unexpected model query: {model!r}")


def test_execution_drag_report_groups_positive_edge_no_order_id_shadow() -> None:
    audit = SimpleNamespace(
        id=1,
        user_id=7,
        breakout_alert_id=100,
        scan_pattern_id=42,
        ticker="BTC-USD",
        decision="blocked",
        reason="broker:place_no_order_id",
        rule_snapshot={
            "entry_edge_expected_net_pct": 2.5,
            "broker_reject_missing_order_id": True,
            "broker_reject_venue": "coinbase",
            "broker_reject_order_hint": "limit_post_only",
        },
        created_at=datetime(2026, 6, 2, 12, 0),
    )
    shadow = SimpleNamespace(
        id=10,
        paper_shadow_of_alert_id=100,
        scan_pattern_id=42,
        ticker="BTC-USD",
        direction="long",
        entry_price=100.0,
        exit_price=103.0,
        quantity=1.0,
        pnl=3.0,
        pnl_pct=3.0,
        status="closed",
        signal_json={
            "paper_shadow": True,
            "shadow_decision": "blocked_no_order_id",
            "asset_class": "crypto",
            "entry_edge_expected_net_pct": 2.5,
        },
    )

    out = _execution_drag_report_from_rows([audit], [shadow], days=7, limit=10)

    assert out["summary"] == {
        "execution_drag_events": 1,
        "positive_edge_events": 1,
        "penalized_positive_drag_events": 1,
        "paper_shadow_confirmed_missed_alpha_events": 1,
        "paper_shadow_spared_loss_events": 0,
        "groups": 1,
        "paper_shadow_count": 1,
    }
    row = out["groups"][0]
    assert row["asset_class"] == "crypto"
    assert row["scan_pattern_id"] == 42
    assert row["broker_venue"] == "coinbase"
    assert row["order_hint"] == "limit_post_only"
    assert row["reason_family"] == "place_no_order_id"
    assert row["order_status"] == "no_order_id"
    assert row["recommended_work_event"] == "edge_reliability_refresh"
    assert (
        row["recommended_next_action"]
        == "refresh_edge_reliability_and_review_entry_execution_geometry"
    )
    assert row["avg_expected_net_pct"] == pytest.approx(2.5)
    assert row["shadow_adjusted_avg_expected_net_pct"] == pytest.approx(2.5)
    assert row["raw_positive_drag_rate_pct"] == pytest.approx(100.0)
    assert row["shadow_adjusted_positive_drag_rate_pct"] == pytest.approx(100.0)
    assert row["net_edge_execution_drag_cost_fraction_estimate"] == pytest.approx(0.025)
    assert row["net_edge_execution_drag_cost_pct_estimate"] == pytest.approx(2.5)
    assert row["penalized_positive_drag_events"] == 1
    assert row["paper_shadow_sample_n"] == 1
    assert row["paper_shadow_confirmed_missed_alpha_events"] == 1
    assert row["paper_shadow_spared_loss_events"] == 0
    assert row["paper_shadow_unknown_outcome_events"] == 0
    assert row["unobserved_positive_drag_events"] == 0
    assert row["paper_shadow_avg_return_pct"] == pytest.approx(3.0)
    assert row["paper_shadow_win_rate_pct"] == pytest.approx(100.0)
    candidates = execution_alpha_drag_followup_candidates(out)
    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate["event_type"] == "edge_reliability_refresh"
    assert candidate["scan_pattern_id"] == 42
    assert candidate["asset_class"] == "crypto"
    assert candidate["evidence_fingerprint"] == row["evidence_fingerprint"]
    assert candidate["expected_evidence_value"] == pytest.approx(2.5)
    assert candidate["positive_edge_events"] == 1
    assert candidate["events"] == 1
    assert candidate["reason_family"] == "place_no_order_id"
    assert candidate["order_status"] == "no_order_id"
    assert candidate["broker_venue"] == "coinbase"
    assert candidate["order_hint"] == "limit_post_only"
    assert (
        candidate["recommended_next_action"]
        == "refresh_edge_reliability_and_review_entry_execution_geometry"
    )


def test_execution_alpha_drag_report_exposes_option_no_fill_groups() -> None:
    audit = SimpleNamespace(
        id=2,
        user_id=7,
        breakout_alert_id=101,
        scan_pattern_id=77,
        ticker="AAPL",
        decision="blocked",
        reason="broker:option_entry_no_fill:cancelled",
        rule_snapshot={
            "entry_edge": {"expected_net_pct": 3.1},
            "asset_type": "options",
            "options_path": True,
            "option_meta": {"limit_price": 1.25, "expiration": "2026-06-19"},
            "option_entry_terminal_state": "cancelled",
        },
        created_at=datetime(2026, 6, 2, 12, 5),
    )
    shadow = SimpleNamespace(
        id=11,
        user_id=7,
        paper_shadow_of_alert_id=101,
        scan_pattern_id=77,
        ticker="AAPL",
        direction="long",
        entry_price=1.25,
        exit_price=1.10,
        quantity=1.0,
        pnl=-15.0,
        pnl_pct=-12.0,
        status="closed",
        entry_date=datetime(2026, 6, 2, 12, 5),
        signal_json={
            "paper_shadow": True,
            "shadow_decision": "blocked_option_entry_no_fill",
            "asset_class": "options",
        },
    )
    db = _FakeAttributionSession(
        trades=[],
        paper_trades=[shadow],
        autotrader_runs=[audit],
        patterns=[],
    )

    out = execution_alpha_drag_report(db, 7, days=7, limit=10)

    row = out["groups"][0]
    assert row["asset_class"] == "options"
    assert row["broker_venue"] == "robinhood_options"
    assert row["order_hint"] == "option_limit"
    assert row["reason_family"] == "option_entry_no_fill"
    assert row["order_status"] == "no_fill"
    assert row["positive_edge_events"] == 1
    assert row["penalized_positive_drag_events"] == 0
    assert row["paper_shadow_sample_n"] == 1
    assert row["paper_shadow_confirmed_missed_alpha_events"] == 0
    assert row["paper_shadow_spared_loss_events"] == 1
    assert row["paper_shadow_unknown_outcome_events"] == 0
    assert row["unobserved_positive_drag_events"] == 0
    assert row["recommended_work_event"] == "edge_reliability_refresh"
    assert (
        row["recommended_next_action"]
        == "refresh_edge_reliability_and_review_entry_execution_geometry"
    )
    assert row["avg_expected_net_pct"] == pytest.approx(3.1)
    assert row["shadow_adjusted_avg_expected_net_pct"] is None
    assert row["raw_positive_drag_rate_pct"] == pytest.approx(100.0)
    assert row["shadow_adjusted_positive_drag_rate_pct"] == pytest.approx(0.0)
    assert row["net_edge_execution_drag_cost_fraction_estimate"] == pytest.approx(0.0)
    assert row["net_edge_execution_drag_cost_pct_estimate"] == pytest.approx(0.0)
    assert row["paper_shadow_avg_return_pct"] == pytest.approx(-12.0)
    assert row["paper_shadow_win_rate_pct"] == pytest.approx(0.0)


def test_queue_execution_alpha_drag_followups_requests_edge_refresh(monkeypatch) -> None:
    audit = SimpleNamespace(
        id=3,
        user_id=7,
        breakout_alert_id=102,
        scan_pattern_id=88,
        ticker="AAPL",
        decision="blocked",
        reason="broker:option_entry_no_fill:cancelled",
        rule_snapshot={
            "entry_edge_expected_net_pct": 2.4,
            "asset_type": "options",
            "options_path": True,
            "option_meta": {"limit_price": 1.35, "expiration": "2026-06-19"},
            "option_entry_terminal_state": "cancelled",
        },
        created_at=datetime(2026, 6, 2, 12, 5),
    )
    db = _FakeAttributionSession(
        trades=[],
        paper_trades=[],
        autotrader_runs=[audit],
        patterns=[],
    )
    calls: list[dict] = []

    from app.services.trading import edge_reliability

    def _emit(db_arg, scan_pattern_id, **kwargs):
        calls.append({"db": db_arg, "scan_pattern_id": scan_pattern_id, **kwargs})
        return 123

    monkeypatch.setattr(
        edge_reliability,
        "emit_edge_reliability_refresh_requested",
        _emit,
    )

    out = queue_execution_alpha_drag_followups(db, 7, days=7, limit=10)

    assert out["created"] == 1
    assert out["event_ids"] == [123]
    assert out["report_summary"]["positive_edge_events"] == 1
    assert out["report_summary"]["penalized_positive_drag_events"] == 1
    assert out["report_summary"]["paper_shadow_spared_loss_events"] == 0
    assert calls == [
        {
            "db": db,
            "scan_pattern_id": 88,
            "source": "execution_alpha_drag_report",
            "asset_class": "options",
            "window_days": 7,
            "evidence_fingerprint": out["candidates"][0]["evidence_fingerprint"],
        }
    ]


def test_execution_alpha_drag_followups_endpoint_queues_work(monkeypatch) -> None:
    from app.routers.trading_sub import trades as trades_router
    from app.services.trading import public_api

    db = object()
    calls: list[dict] = []

    def _identity(request, db_arg):
        return {"user_id": 7}

    def _queue(db_arg, user_id, **kwargs):
        calls.append({"db": db_arg, "user_id": user_id, **kwargs})
        return {
            "ok": True,
            "window_days": kwargs["days"],
            "considered": 1,
            "created": 1,
            "skipped": 0,
            "event_ids": [123],
        }

    monkeypatch.setattr(trades_router, "get_identity_ctx", _identity)
    monkeypatch.setattr(public_api, "queue_execution_alpha_drag_followups", _queue)

    resp = trades_router.api_attribution_execution_alpha_drag_followups(
        request=object(),
        db=db,
        days=5,
        limit=20,
        min_positive_edge_events=2,
    )

    data = json.loads(resp.body)
    assert resp.status_code == 200
    assert data["ok"] is True
    assert data["created"] == 1
    assert data["event_ids"] == [123]
    assert calls == [
        {
            "db": db,
            "user_id": 7,
            "days": 5,
            "limit": 20,
            "min_positive_edge_events": 2,
        }
    ]


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
    assert row["live_pnl_sample_n"] == 1
    assert row["live_avg_pnl"] == pytest.approx(40.0)


def test_live_vs_research_ignores_unverified_extreme_tca_costs() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="crypto-alpha",
        promotion_status="pilot",
        win_rate=0.6,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    normal = SimpleNamespace(
        id=1004,
        user_id=7,
        status="closed",
        scan_pattern_id=42,
        ticker="POND-USD",
        direction="long",
        entry_price=100.0,
        exit_price=110.0,
        quantity=1.0,
        pnl=10.0,
        exit_date=datetime(2026, 5, 30, 15, 30),
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
        avg_fill_price=None,
        broker_order_id="",
        broker_status="",
    )
    unverified_outlier = SimpleNamespace(
        id=1005,
        user_id=7,
        status="closed",
        scan_pattern_id=42,
        ticker="POND-USD",
        direction="long",
        entry_price=100.0,
        exit_price=110.0,
        quantity=1.0,
        pnl=10.0,
        exit_date=datetime(2026, 5, 30, 15, 31),
        tca_entry_slippage_bps=1426.0,
        tca_exit_slippage_bps=1361.0,
        avg_fill_price=None,
        broker_order_id="",
        broker_status="",
    )
    db = _FakeAttributionSession(
        trades=[normal, unverified_outlier],
        patterns=[pattern],
    )

    out = live_vs_research_by_pattern(db, 7, days=30, limit=10)

    row = out["patterns"][0]
    assert row["live_return_sample_n"] == 2
    assert row["live_avg_return_pct"] == pytest.approx(10.0)
    assert row["live_avg_tca_cost_pct"] == pytest.approx(0.30)
    assert row["live_avg_net_return_pct"] == pytest.approx(9.70)
    assert row["live_avg_entry_slippage_bps"] == pytest.approx(12.0)
    assert row["live_avg_exit_slippage_bps"] == pytest.approx(18.0)


def test_live_vs_research_live_pnl_total_includes_partial_option_leg() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="option-alpha",
        promotion_status="pilot",
        win_rate=0.6,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    trade = SimpleNamespace(
        id=1003,
        user_id=7,
        status="closed",
        scan_pattern_id=42,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        asset_kind="option",
        tags=None,
        indicator_snapshot=None,
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
        exit_date=datetime(2026, 5, 30, 15, 30),
        tca_entry_slippage_bps=12.0,
        tca_exit_slippage_bps=18.0,
    )
    db = _FakeAttributionSession(trades=[trade], patterns=[pattern])

    out = live_vs_research_by_pattern(db, 7, days=30, limit=10)

    row = out["patterns"][0]
    assert row["live_win_rate_pct"] == pytest.approx(100.0)
    assert row["live_avg_return_pct"] == pytest.approx(4.0)
    assert row["live_pnl_sample_n"] == 1
    assert row["live_total_pnl"] == pytest.approx(10.0)
    assert row["live_avg_pnl"] == pytest.approx(10.0)


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
    assert row["live_pnl_sample_n"] == 0
    assert row["live_total_pnl"] is None
    assert row["live_avg_pnl"] is None


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
    assert review["pnl_sample_n"] == 0
    assert review["total_pnl"] is None
    assert review["avg_pnl"] is None

    outperformer = review["outperforming_patterns"][0]
    assert outperformer["scan_pattern_id"] == 42
    assert outperformer["live_trades"] == 3
    assert outperformer["live_win_sample_n"] == 3
    assert outperformer["live_win_rate_pct"] == pytest.approx(66.7)
    assert outperformer["live_pnl_sample_n"] == 0
    assert outperformer["live_total_pnl"] is None
    assert outperformer["research_win_rate_pct"] == pytest.approx(55.0)
    assert outperformer["delta_pct"] == pytest.approx(11.7)
    assert review["underperforming_patterns"] == []
    assert out["feedback_signals"][0]["signal"] == "upweight"


def test_post_trade_review_pnl_totals_include_partial_option_leg() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="option-alpha",
        promotion_status="pilot",
        win_rate=0.5,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    trades = [
        SimpleNamespace(
            id=1201,
            user_id=7,
            status="closed",
            scan_pattern_id=42,
            ticker="SPY",
            direction="long",
            entry_price=1.25,
            exit_price=1.15,
            quantity=1.0,
            pnl=-10.0,
            asset_kind="option",
            tags=None,
            indicator_snapshot=None,
            partial_taken=True,
            partial_taken_qty=1.0,
            partial_taken_price=1.45,
            exit_date=datetime(2026, 5, 30, 15, 30),
            tca_entry_slippage_bps=40.0,
            tca_exit_slippage_bps=20.0,
        )
    ]
    db = _FakeAttributionSession(trades=trades, patterns=[pattern])

    out = post_trade_review(db, 7, days=30)

    review = out["review"]
    assert review["wins"] == 1
    assert review["pnl_sample_n"] == 1
    assert review["total_pnl"] == pytest.approx(10.0)
    assert review["avg_pnl"] == pytest.approx(10.0)
    assert review["high_slippage_trades"][0]["pnl"] == pytest.approx(10.0)


def test_post_trade_review_excludes_unverified_extreme_slippage_outliers() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="crypto-alpha",
        promotion_status="pilot",
        win_rate=0.5,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    trades = [
        SimpleNamespace(
            id=1202,
            user_id=7,
            status="closed",
            scan_pattern_id=42,
            ticker="POND-USD",
            direction="long",
            entry_price=100.0,
            exit_price=110.0,
            quantity=1.0,
            pnl=10.0,
            exit_date=datetime(2026, 5, 30, 15, 30),
            tca_entry_slippage_bps=1426.0,
            tca_exit_slippage_bps=1361.0,
            avg_fill_price=None,
            broker_order_id="",
            broker_status="",
        )
    ]
    db = _FakeAttributionSession(trades=trades, patterns=[pattern])

    out = post_trade_review(db, 7, days=30)

    assert out["review"]["high_slippage_trades"] == []
    assert not any("high slippage" in item for item in out["review"]["takeaways"])


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


def test_live_vs_research_paper_pnl_total_includes_partial_option_leg() -> None:
    pattern = SimpleNamespace(
        id=42,
        name="option-alpha",
        promotion_status="shadow",
        win_rate=0.6,
        oos_win_rate=0.55,
        oos_avg_return_pct=3.2,
    )
    paper_trade = SimpleNamespace(
        id=2003,
        user_id=7,
        status="closed",
        scan_pattern_id=42,
        paper_shadow_of_alert_id=77,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        exit_price=1.15,
        quantity=1.0,
        pnl=-10.0,
        pnl_pct=-8.0,
        signal_json={"asset_type": "options"},
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=1.45,
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
    assert row["paper_win_rate_pct"] == pytest.approx(100.0)
    assert row["paper_avg_return_pct"] == pytest.approx(4.0)
    assert row["paper_total_pnl"] == pytest.approx(10.0)
    assert row["paper_avg_pnl"] == pytest.approx(10.0)


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
