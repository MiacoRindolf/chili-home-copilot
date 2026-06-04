from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.config import settings
from app.models.trading import (
    AutoTraderRun,
    BrainWorkEvent,
    BreakoutAlert,
    TradingExecutionEvent,
    PaperTrade,
    ScanPattern,
    Trade,
    TradingPosition,
)
from app.services.trading.cash_deployment import (
    _allocation_score,
    cash_deployment_rows,
    cash_deployment_summary,
    cost_gate_execution_block_rollup,
    enqueue_cash_deployment_work,
    enqueue_imminent_edge_snapshot_coverage_work,
    low_confidence_exit_attribution_rollup,
)


def test_cash_deployment_allocation_score_preserves_zero_closed_evidence_floor(monkeypatch):
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 0)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.28)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_abs_paper_live_gap_pct", 3.0)

    score = _allocation_score(
        {
            "closed_evidence_count": 0,
            "brier_score": 0.0,
            "realized_ev_pct": 0.0,
            "live_realized_asset_closed_count": 0,
            "paper_live_gap_pct": None,
        },
        calibrated_ev_after_cost=0.0,
        venue_score=0.0,
        exposure_blocker=None,
    )

    assert score == pytest.approx(47.5)


def _pattern(
    db,
    *,
    name: str,
    lifecycle: str = "promoted",
    recert: bool = False,
    recert_reason: str | None = None,
    asset_class: str = "stock",
):
    pat = ScanPattern(
        name=name,
        rules_json={},
        origin="test",
        asset_class=asset_class,
        timeframe="1d",
        active=True,
        lifecycle_stage=lifecycle,
        recert_required=recert,
        recert_reason=recert_reason,
    )
    db.add(pat)
    db.flush()
    return pat


def _alert(db, pat: ScanPattern, *, ticker: str, asset_type: str = "stock"):
    alert = BreakoutAlert(
        scan_pattern_id=pat.id,
        ticker=ticker,
        asset_type=asset_type,
        alert_tier="pattern_imminent",
        outcome="pending",
        score_at_alert=85.0,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=95.0,
        target_price=110.0,
        alerted_at=datetime.utcnow(),
        indicator_snapshot={"imminent_scorecard": {"signal_lane": "standard"}},
    )
    db.add(alert)
    db.flush()
    return alert


def _run(
    db,
    pat: ScanPattern,
    alert: BreakoutAlert,
    *,
    expected: float,
    reason: str = "selector:secondary_gate_observation",
    decision: str = "skipped",
):
    row = AutoTraderRun(
        breakout_alert_id=alert.id,
        scan_pattern_id=pat.id,
        ticker=alert.ticker,
        decision=decision,
        reason=reason,
        rule_snapshot={
            "entry_edge": {
                "expected_net_pct": expected,
                "probability": 0.6,
                "breakeven_probability": 0.48,
                "probability_source": "pattern_regime_hit_rate",
            },
        },
    )
    db.add(row)
    db.flush()
    return row


def _closed_paper(db, pat: ScanPattern, alert: BreakoutAlert, *, count: int = 5, pnl_pct: float = 4.0):
    for idx in range(count):
        db.add(
            PaperTrade(
                scan_pattern_id=pat.id,
                paper_shadow_of_alert_id=alert.id,
                ticker=alert.ticker,
                direction="long",
                entry_price=100.0,
                stop_price=95.0,
                target_price=110.0,
                quantity=1.0,
                status="closed",
                entry_date=datetime.utcnow() - timedelta(hours=idx + 2),
                exit_date=datetime.utcnow() - timedelta(hours=idx + 1),
                exit_price=100.0 + pnl_pct,
                pnl=pnl_pct,
                pnl_pct=pnl_pct,
                signal_json={"paper_shadow": True},
            )
        )


def _closed_live(
    db,
    pat: ScanPattern,
    *,
    ticker: str,
    pnl: float,
    asset_kind: str = "equity",
    entry_price: float = 100.0,
    quantity: float = 1.0,
    indicator_snapshot: dict | None = None,
    exit_reason: str | None = None,
):
    trade = Trade(
        scan_pattern_id=pat.id,
        ticker=ticker,
        direction="long",
        entry_price=entry_price,
        quantity=quantity,
        status="closed",
        entry_date=datetime.utcnow() - timedelta(hours=2),
        exit_date=datetime.utcnow() - timedelta(hours=1),
        exit_price=entry_price + (pnl / quantity),
        pnl=pnl,
        asset_kind=asset_kind,
        indicator_snapshot=indicator_snapshot,
        exit_reason=exit_reason,
    )
    db.add(trade)
    db.flush()
    return trade


def test_cash_deployment_categorizes_positive_blocks_without_live_shortcuts(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.28)

    ready = _pattern(db, name="cash ready")
    ready_alert = _alert(db, ready, ticker="READY")
    _run(db, ready, ready_alert, expected=2.0)
    _closed_paper(db, ready, ready_alert)

    recert = _pattern(
        db,
        name="cash recert",
        recert=True,
        recert_reason="negative_oos_recert",
    )
    recert_alert = _alert(db, recert, ticker="RECRT")
    _run(db, recert, recert_alert, expected=3.0)
    _closed_paper(db, recert, recert_alert)

    shadow = _pattern(db, name="cash shadow", lifecycle="shadow_promoted")
    shadow_alert = _alert(db, shadow, ticker="SHDW")
    _run(db, shadow, shadow_alert, expected=2.5)
    _closed_paper(db, shadow, shadow_alert)

    reject = _pattern(db, name="cash broker reject")
    reject_alert = _alert(db, reject, ticker="RJCT")
    _run(db, reject, reject_alert, expected=1.5, reason="broker:quantity_precision")
    _closed_paper(db, reject, reject_alert)

    negative = _pattern(db, name="cash negative")
    negative_alert = _alert(db, negative, ticker="NEG")
    _run(db, negative, negative_alert, expected=-0.2, reason="non_positive_expected_edge")
    db.commit()

    rows = cash_deployment_rows(db, window_days=7, limit=20)
    by_pattern = {row["scan_pattern_id"]: row for row in rows}

    assert by_pattern[ready.id]["cash_deployment_category"] == "live_deployable"
    assert by_pattern[ready.id]["cash_deployment_rank"] == 1
    assert by_pattern[ready.id]["calibrated_ev_after_cost_pct"] == pytest.approx(2.45)
    assert by_pattern[ready.id]["max_safe_notional"] > 0

    assert by_pattern[recert.id]["cash_deployment_category"] == "positive_ev_recert"
    assert by_pattern[recert.id]["live_deployable"] is False
    assert by_pattern[recert.id]["recommended_work_event"] == "recert_rescue_refresh"

    assert by_pattern[shadow.id]["cash_deployment_category"] == "positive_ev_shadow"
    assert by_pattern[shadow.id]["live_deployable"] is False
    assert by_pattern[shadow.id]["recommended_work_event"] == "exit_variant_refresh"

    assert by_pattern[reject.id]["cash_deployment_category"] == "positive_ev_execution_blocked"
    assert by_pattern[reject.id]["live_deployable"] is False
    assert by_pattern[reject.id]["execution_blocker"] == "broker_rejects"

    assert by_pattern[negative.id]["cash_deployment_category"] == "negative_ev"
    assert by_pattern[negative.id]["cash_deployment_rank"] is None

    summary = cash_deployment_summary(rows)
    assert summary["live_deployable"] == 1
    assert summary["positive_ev_recert"] >= 1
    assert summary["positive_ev_shadow"] >= 1
    assert summary["positive_ev_execution_blocked"] >= 1
    assert summary["negative_ev"] >= 1


def test_cash_deployment_blocks_positive_slippage_miss_as_execution_debt(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.28)

    pat = _pattern(db, name="cash slippage blocked")
    alert = _alert(db, pat, ticker="SLIP")
    _run(db, pat, alert, expected=2.0, reason="missed_entry_slippage")
    _closed_paper(db, pat, alert)
    db.commit()

    rows = cash_deployment_rows(db, window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)

    assert row["cash_deployment_category"] == "positive_ev_execution_blocked"
    assert row["graduation_blocker"] == "execution_blocked"
    assert row["execution_blocker"] == "missed_entry_slippage"
    assert row["slippage_miss_count"] == 1
    assert row["live_deployable"] is False
    assert row["max_safe_notional"] == 0.0


def test_cash_deployment_exposure_cap_blocks_sizing(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "brain_risk_max_positions", 1)

    db.add(
        Trade(
            ticker="HOLD",
            direction="long",
            entry_price=10.0,
            quantity=1.0,
            status="open",
            entry_date=datetime.utcnow(),
            stop_loss=9.5,
        )
    )
    pat = _pattern(db, name="cash exposure blocked")
    alert = _alert(db, pat, ticker="CAPD")
    _run(db, pat, alert, expected=2.0)
    _closed_paper(db, pat, alert)
    db.commit()

    rows = cash_deployment_rows(db, window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)

    assert row["cash_deployment_category"] == "positive_ev_execution_blocked"
    assert row["exposure_blocker"]
    assert row["max_safe_notional"] == 0.0
    assert row["live_deployable"] is False


def test_cash_deployment_option_heat_uses_premium_risk_not_underlying_stop(
    monkeypatch,
):
    from app.services.trading.cash_deployment import _trade_heat_pct

    monkeypatch.setattr(
        settings,
        "chili_autotrader_options_exit_stop_pct",
        50.0,
        raising=False,
    )
    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        stop_loss=700.0,
        asset_kind="option",
        tags=None,
        indicator_snapshot={
            "asset_type": "options",
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
                "price_domain": "option_premium",
            },
        },
    )

    assert _trade_heat_pct(trade, capital=10_000.0) == pytest.approx(1.25)


def test_cash_deployment_sparse_asset_class_option_uses_contract_return() -> None:
    from app.services.trading.cash_deployment import _trade_asset_class, _trade_return_pct

    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        pnl=40.0,
        asset_kind=None,
        tags=None,
        indicator_snapshot={"asset_class": "options"},
    )

    assert _trade_asset_class(trade) == "options"
    assert _trade_return_pct(trade) == pytest.approx(16.0)


def test_cash_deployment_nested_asset_class_option_uses_contract_return() -> None:
    from app.services.trading.cash_deployment import _trade_asset_class, _trade_return_pct

    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=2.0,
        pnl=40.0,
        asset_kind=None,
        tags=None,
        indicator_snapshot={"breakout_alert": {"asset_class": "option"}},
    )

    assert _trade_asset_class(trade) == "options"
    assert _trade_return_pct(trade) == pytest.approx(16.0)


def test_cash_deployment_live_return_uses_partial_aware_realized_pnl() -> None:
    from app.services.trading.cash_deployment import _trade_return_pct

    trade = SimpleNamespace(
        ticker="SPY",
        direction="long",
        entry_price=100.0,
        exit_price=105.0,
        quantity=1.0,
        filled_quantity=None,
        pnl=5.0,
        asset_kind="stock",
        indicator_snapshot={},
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=110.0,
    )

    assert _trade_return_pct(trade) == pytest.approx(7.5)


def test_cash_deployment_live_asset_pnl_uses_partial_aware_option_dollars() -> None:
    from app.services.trading.cash_deployment import _live_asset_performance

    class _Query:
        def filter(self, *args, **kwargs):
            return self

        def all(self):
            return [
                SimpleNamespace(
                    scan_pattern_id=42,
                    ticker="SPY",
                    direction="long",
                    entry_price=1.25,
                    exit_price=1.20,
                    quantity=1.0,
                    filled_quantity=None,
                    pnl=-5.0,
                    status="closed",
                    entry_date=datetime.utcnow() - timedelta(hours=3),
                    exit_date=datetime.utcnow() - timedelta(hours=1),
                    asset_kind="option",
                    tags=None,
                    indicator_snapshot={
                        "asset_type": "options",
                        "option_meta": {
                            "underlying": "SPY",
                            "expiration": "2026-06-19",
                            "strike": 729.0,
                            "option_type": "call",
                            "price_domain": "option_premium",
                        },
                    },
                    partial_taken=True,
                    partial_taken_qty=1.0,
                    partial_taken_price=1.45,
                )
            ]

    db = SimpleNamespace(query=lambda _model: _Query())

    perf = _live_asset_performance(
        db,
        scan_pattern_id=42,
        asset_class="options",
        user_id=None,
        window_days=7,
    )

    assert perf["live_realized_asset_closed_count"] == 1
    assert perf["live_realized_asset_pnl_usd"] == pytest.approx(15.0)
    assert perf["live_realized_asset_avg_return_pct"] == pytest.approx(6.0)
    assert perf["live_realized_asset_win_rate"] == pytest.approx(1.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("robinhood_options", "options"),
        ("option_contract", "options"),
        ("digital_asset", "crypto"),
        ("equities", "stock"),
    ],
)
def test_cash_deployment_canonical_asset_class_uses_shared_aliases(raw, expected) -> None:
    from app.services.trading.cash_deployment import _canonical_asset_class

    assert _canonical_asset_class(raw) == expected


def test_cash_deployment_row_asset_class_uses_alias_counters() -> None:
    from app.services.trading.cash_deployment import _asset_class_for_row

    row = {
        "asset_class": None,
        "asset_types": {"robinhood_options": 3, "stock": 1},
        "primary_symbol": "SPY",
    }

    assert _asset_class_for_row(row) == "options"


def test_cash_deployment_zero_ranks_stale_broker_local_open(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.28)

    pat = _pattern(db, name="cash stale broker")
    alert = _alert(db, pat, ticker="STAL")
    _run(db, pat, alert, expected=2.0)
    _closed_paper(db, pat, alert)
    pos = TradingPosition(
        user_id=None,
        broker_source="robinhood",
        account_type="cash",
        ticker="STAL",
        direction="long",
        asset_kind="equity",
        current_quantity=0.0,
        current_avg_price=100.0,
        state="closed",
        last_observed_at=datetime.utcnow() - timedelta(days=1),
        last_state_transition_at=datetime.utcnow() - timedelta(days=1),
    )
    db.add(pos)
    db.flush()
    db.add(
        Trade(
            ticker="STAL",
            direction="long",
            entry_price=100.0,
            quantity=1.0,
            status="open",
            broker_source="robinhood",
            broker_status="filled",
            broker_order_id="stale-entry",
            position_id=pos.id,
            entry_date=datetime.utcnow() - timedelta(days=2),
            stop_loss=95.0,
        )
    )
    db.commit()

    rows = cash_deployment_rows(db, window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)
    summary = cash_deployment_summary(rows)

    assert row["cash_deployment_category"] == "stale_broker_local_open"
    assert row["broker_truth_status"] == "stale"
    assert row["broker_truth_reason"] == "position_identity_closed"
    assert row["stale_broker_position"] is True
    assert row["live_deployable"] is False
    assert row["cash_deployment_rank"] is None
    assert row["max_safe_notional"] == 0.0
    assert summary["stale_broker_local_open"] >= 1


def test_cash_deployment_exposes_live_asset_slice_performance(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.28)

    pat = _pattern(db, name="cash live perf")
    alert = _alert(db, pat, ticker="LPERF")
    _run(db, pat, alert, expected=2.0)
    _closed_paper(db, pat, alert)
    _closed_live(db, pat, ticker="LPERF", pnl=4.0, asset_kind="equity")
    _closed_live(db, pat, ticker="BTC-USD", pnl=50.0, asset_kind="crypto")
    db.commit()

    rows = cash_deployment_rows(db, pattern_ids=[pat.id], window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)

    assert row["asset_class"] == "stock"
    assert row["cash_deployment_category"] == "live_deployable"
    assert row["live_realized_asset_closed_count"] == 1
    assert row["live_realized_asset_pnl_usd"] == pytest.approx(4.0)
    assert row["live_realized_asset_avg_return_pct"] == pytest.approx(4.0)
    assert row["live_realized_asset_win_rate"] == pytest.approx(1.0)
    assert row["live_realized_asset_last_exit_at"] is not None


def test_low_confidence_exit_attribution_rollup_groups_noisy_exits(db):
    pat = _pattern(db, name="cash noisy exit attribution")
    _closed_live(db, pat, ticker="NOISY", pnl=-10.0, exit_reason=None)
    _closed_live(
        db,
        pat,
        ticker="NOISY",
        pnl=-5.0,
        exit_reason="broker_reconcile_position_gone",
    )
    _closed_live(db, pat, ticker="NOISY", pnl=2.0, exit_reason="sync_duplicate")
    _closed_live(db, pat, ticker="NOISY", pnl=8.0, exit_reason="target")
    db.commit()

    rollup = low_confidence_exit_attribution_rollup(
        db,
        window_days=7,
        limit=10,
    )

    row = next(x for x in rollup["rows"] if x["scan_pattern_id"] == pat.id)
    assert rollup["total_groups"] >= 1
    assert row["cash_deployment_category"] == "low_confidence_exit_attribution"
    assert row["recommended_work_event"] == "provenance_backfill"
    assert row["closed_trades"] == 4
    assert row["low_confidence_exit_count"] == 3
    assert row["low_confidence_exit_rate_pct"] == pytest.approx(75.0)
    assert row["missing_exit_reason_count"] == 1
    assert row["reconciler_exit_count"] == 2
    assert row["planned_exit_count"] == 1
    assert row["low_confidence_total_pnl_usd"] == pytest.approx(-13.0)
    assert row["total_pnl_usd"] == pytest.approx(-5.0)
    assert row["exit_reason_counts"]["missing"] == 1
    assert row["exit_reason_counts"]["broker_reconcile_position_gone"] == 1
    assert row["exit_reason_counts"]["sync_duplicate"] == 1
    assert row["tickers"] == ["NOISY"]


def test_cash_deployment_options_returns_use_contract_multiplier(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_autotrader_options_enabled", True)
    monkeypatch.setattr(settings, "chili_options_venue_robinhood_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_options_cost_pct", 1.0)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.28)

    pat = _pattern(db, name="cash option live perf", asset_class="options")
    alert = _alert(db, pat, ticker="SPY", asset_type="options")
    _run(db, pat, alert, expected=3.0)
    _closed_paper(db, pat, alert)
    _closed_live(
        db,
        pat,
        ticker="SPY",
        pnl=40.0,
        asset_kind="option",
        entry_price=1.25,
        quantity=2.0,
        indicator_snapshot={
            "asset_type": "options",
            "option_meta": {
                "underlying": "SPY",
                "expiration": "2026-06-19",
                "strike": 729.0,
                "option_type": "call",
                "price_domain": "option_premium",
            },
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
    )
    db.commit()

    rows = cash_deployment_rows(db, pattern_ids=[pat.id], window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)

    assert row["asset_class"] == "options"
    assert row["live_realized_asset_closed_count"] == 1
    assert row["live_realized_asset_avg_return_pct"] == pytest.approx(16.0)
    assert row["live_realized_asset_avg_return_pct"] < 100.0


def test_cash_deployment_live_asset_perf_uses_confirmed_option_return_without_pnl(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_autotrader_options_enabled", True)
    monkeypatch.setattr(settings, "chili_options_venue_robinhood_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_options_cost_pct", 1.0)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.28)
    monkeypatch.setattr(
        settings,
        "chili_cash_deployment_max_abs_paper_live_gap_pct",
        50.0,
    )

    pat = _pattern(db, name="cash option price fallback", asset_class="options")
    alert = _alert(db, pat, ticker="SPY", asset_type="options")
    _run(db, pat, alert, expected=3.0)
    _closed_paper(db, pat, alert)
    confirmed_snapshot = {
        "asset_type": "options",
        "option_meta": {
            "price_domain": "option_premium",
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
        },
        "price_domains": {
            "entry_price": "option_premium",
            "exit_price": "option_premium",
        },
    }
    db.add_all(
        [
            Trade(
                scan_pattern_id=pat.id,
                ticker="SPY",
                direction="long",
                entry_price=1.25,
                quantity=2.0,
                status="closed",
                entry_date=datetime.utcnow() - timedelta(hours=3),
                exit_date=datetime.utcnow() - timedelta(hours=2),
                exit_price=1.45,
                pnl=None,
                asset_kind="option",
                indicator_snapshot=confirmed_snapshot,
            ),
            Trade(
                scan_pattern_id=pat.id,
                ticker="SPY",
                direction="long",
                entry_price=4.01,
                quantity=1.0,
                status="closed",
                entry_date=datetime.utcnow() - timedelta(hours=2),
                exit_date=datetime.utcnow() - timedelta(hours=1),
                exit_price=716.0,
                pnl=None,
                asset_kind="option",
                indicator_snapshot={"asset_type": "options"},
            ),
        ]
    )
    db.commit()

    rows = cash_deployment_rows(db, pattern_ids=[pat.id], window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)

    assert row["asset_class"] == "options"
    assert row["live_realized_asset_closed_count"] == 1
    assert row["live_realized_asset_avg_return_pct"] == pytest.approx(16.0)
    assert row["live_realized_asset_win_rate"] == pytest.approx(1.0)
    assert row["live_realized_asset_pnl_usd"] == pytest.approx(0.0)


def test_cash_deployment_blocks_live_deployable_on_negative_live_asset_perf(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.28)

    pat = _pattern(db, name="cash negative live perf")
    alert = _alert(db, pat, ticker="NLPERF")
    _run(db, pat, alert, expected=2.0)
    _closed_paper(db, pat, alert, count=6, pnl_pct=4.0)
    _closed_live(db, pat, ticker="NLPERF", pnl=-2.0, asset_kind="equity")
    db.commit()

    rows = cash_deployment_rows(db, pattern_ids=[pat.id], window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)

    assert row["live_realized_asset_closed_count"] == 1
    assert row["live_realized_asset_avg_return_pct"] == pytest.approx(-2.0)
    assert row["cash_deployment_category"] == "needs_calibration"
    assert row["live_deployable"] is False
    assert row["max_safe_notional"] == 0.0


def test_cash_deployment_routes_positive_edge_noisy_live_exits_to_provenance(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.28)

    pat = _pattern(db, name="cash noisy live exits")
    alert = _alert(db, pat, ticker="NEXIT")
    _run(db, pat, alert, expected=2.0)
    _closed_paper(db, pat, alert, count=5, pnl_pct=4.0)
    _closed_live(
        db,
        pat,
        ticker="NEXIT",
        pnl=-10.0,
        asset_kind="equity",
        exit_reason="broker_reconcile_position_gone",
    )
    _closed_live(
        db,
        pat,
        ticker="NEXIT",
        pnl=-5.0,
        asset_kind="equity",
        exit_reason=None,
    )
    db.commit()

    rows = cash_deployment_rows(db, pattern_ids=[pat.id], window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)
    summary = cash_deployment_summary(rows)

    assert row["cash_deployment_category"] == "needs_exit_provenance"
    assert row["recommended_work_event"] == "provenance_backfill"
    assert row["exit_provenance_blocker"] == "low_confidence_live_exit_rate"
    assert row["live_deployable"] is False
    assert row["max_safe_notional"] == 0.0
    assert row["closed_evidence_count"] == 5
    assert row["live_total_closed_count"] == 2
    assert row["live_low_confidence_exit_count"] == 2
    assert row["live_low_confidence_exit_rate"] == pytest.approx(1.0)
    assert row["live_low_confidence_total_pnl_usd"] == pytest.approx(-15.0)
    assert summary["needs_exit_provenance"] == 1
    assert summary["live_low_confidence_exit_count"] == 2
    assert summary["live_low_confidence_pnl_usd"] == pytest.approx(-15.0)

    first = enqueue_cash_deployment_work(
        db,
        window_days=7,
        limit=10,
        include_null_lineage=False,
        include_snapshot_coverage=False,
    )
    db.commit()
    assert first["created"] == 1
    assert first["event_types"] == {"provenance_backfill": 1}
    work = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == "provenance_backfill")
        .one()
    )
    assert work.payload["cash_deployment_category"] == "needs_exit_provenance"
    assert work.payload["exit_provenance_blocker"] == "low_confidence_live_exit_rate"
    assert work.payload["live_low_confidence_exit_count"] == 2
    assert work.payload["live_low_confidence_total_pnl_usd"] == pytest.approx(-15.0)


def test_provenance_backfill_handler_scopes_low_confidence_exit_debt(db):
    from app.services.trading.brain_work.handlers.profitability import (
        handle_provenance_backfill,
    )
    from app.services.trading.edge_reliability import PROVENANCE_BACKFILL_DIAGNOSTIC

    pat = _pattern(db, name="handler noisy live exits")
    _closed_live(db, pat, ticker="HNOISE", pnl=-6.0, exit_reason=None)
    _closed_live(
        db,
        pat,
        ticker="HNOISE",
        pnl=-4.0,
        exit_reason="broker_reconcile_position_gone",
    )
    other = _pattern(db, name="handler unrelated noisy exits")
    _closed_live(db, other, ticker="OTHER", pnl=-99.0, exit_reason=None)
    parent = BrainWorkEvent(
        event_type="provenance_backfill",
        event_kind="work",
        payload={
            "scan_pattern_id": pat.id,
            "asset_class": "stock",
            "window_days": 7,
            "cash_deployment_category": "needs_exit_provenance",
            "exit_provenance_blocker": "low_confidence_live_exit_rate",
            "evidence_fingerprint": "handler-exit-debt-fp",
        },
        dedupe_key="test:provenance:handler-exit-debt",
        lease_scope="edge",
    )
    db.add(parent)
    db.commit()

    handle_provenance_backfill(db, parent, user_id=None)

    outcome = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == PROVENANCE_BACKFILL_DIAGNOSTIC)
        .one()
    )
    payload = outcome.payload
    rows = payload["low_confidence_exit_attribution"]
    assert outcome.parent_event_id == parent.id
    assert payload["scan_pattern_id"] == pat.id
    assert payload["cash_deployment_category"] == "needs_exit_provenance"
    assert payload["exit_provenance_blocker"] == "low_confidence_live_exit_rate"
    assert payload["exit_attribution_debt_count"] == 1
    assert payload["edge_reliability_refresh_after_repair"] == {
        "queued": False,
        "event_id": None,
        "reason": "no_repairs",
    }
    assert payload["low_confidence_exit_attribution_summary"]["total_groups"] == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["scan_pattern_id"] == pat.id
    assert row["low_confidence_exit_count"] == 2
    assert row["low_confidence_total_pnl_usd"] == pytest.approx(-10.0)
    assert row["ticker_count"] == 1
    assert row["tickers"] == ["HNOISE"]
    assert len(set(row["low_confidence_trade_ids"])) == 2
    assert row["exit_reason_counts"]["missing"] == 1
    assert row["exit_reason_counts"]["broker_reconcile_position_gone"] == 1
    assert all(x["scan_pattern_id"] != other.id for x in rows)


def test_provenance_backfill_repairs_only_unambiguous_exit_reasons(db):
    from app.services.trading.brain_work.handlers.profitability import (
        handle_provenance_backfill,
    )
    from app.services.trading.edge_reliability import (
        EDGE_RELIABILITY_REFRESH,
        PROVENANCE_BACKFILL_DIAGNOSTIC,
    )

    pat = _pattern(db, name="handler repairable noisy exits")
    pending = _closed_live(db, pat, ticker="RPAIR", pnl=3.0, exit_reason=None)
    pending.pending_exit_reason = "pattern_exit_now"
    pending.pending_exit_status = "filled"
    from_event = _closed_live(
        db,
        pat,
        ticker="RPAIR",
        pnl=5.0,
        exit_reason="broker_reconcile_position_gone",
    )
    from_reconcile_event = _closed_live(
        db,
        pat,
        ticker="RPAIR",
        pnl=4.0,
        exit_reason="broker_reconcile_position_gone",
    )
    duplicate = _closed_live(
        db,
        pat,
        ticker="RPAIR",
        pnl=-1.0,
        exit_reason="sync_duplicate",
    )
    duplicate.pending_exit_reason = "target"
    ambiguous = _closed_live(db, pat, ticker="RPAIR", pnl=1.0, exit_reason=None)
    ambiguous.pending_exit_reason = "target"
    ambiguous.pending_exit_status = "filled"
    db.flush()
    db.add_all(
        [
            TradingExecutionEvent(
                trade_id=from_event.id,
                scan_pattern_id=pat.id,
                ticker="RPAIR",
                event_type="exit_fill",
                status="filled",
                payload_json={"side": "sell", "exit_reason": "target"},
            ),
            TradingExecutionEvent(
                trade_id=ambiguous.id,
                scan_pattern_id=pat.id,
                ticker="RPAIR",
                event_type="exit_fill",
                status="filled",
                payload_json={"side": "sell", "exit_reason": "stop"},
            ),
            TradingExecutionEvent(
                trade_id=from_reconcile_event.id,
                scan_pattern_id=pat.id,
                ticker="RPAIR",
                event_type="broker_reconcile_gone_close",
                status="filled",
                payload_json={
                    "side": "sell",
                    "exit_reason": "broker_reconcile_position_gone",
                    "pending_exit_reason": "pattern_exit_now",
                },
            ),
        ]
    )
    parent = BrainWorkEvent(
        event_type="provenance_backfill",
        event_kind="work",
        payload={
            "scan_pattern_id": pat.id,
            "asset_class": "stock",
            "window_days": 7,
            "cash_deployment_category": "needs_exit_provenance",
            "exit_provenance_blocker": "low_confidence_live_exit_rate",
            "evidence_fingerprint": "handler-exit-repair-fp",
        },
        dedupe_key="test:provenance:handler-exit-repair",
        lease_scope="edge",
    )
    db.add(parent)
    db.commit()

    handle_provenance_backfill(db, parent, user_id=None)
    db.flush()
    for trade in (pending, from_event, from_reconcile_event, duplicate, ambiguous):
        db.refresh(trade)

    outcome = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == PROVENANCE_BACKFILL_DIAGNOSTIC)
        .one()
    )
    repair = outcome.payload["exit_provenance_repair_summary"]
    refresh = outcome.payload["edge_reliability_refresh_after_repair"]

    assert pending.exit_reason == "pattern_exit_now"
    assert from_event.exit_reason == "target"
    assert from_reconcile_event.exit_reason == "pattern_exit_now"
    assert duplicate.exit_reason == "sync_duplicate"
    assert ambiguous.exit_reason is None
    assert outcome.payload["repair_applied"] is True
    assert outcome.payload["research_only"] is False
    assert repair["repaired_count"] == 3
    assert {row["trade_id"] for row in repair["repaired"]} == {
        pending.id,
        from_event.id,
        from_reconcile_event.id,
    }
    skipped = {row["trade_id"]: row["reason"] for row in repair["skipped"]}
    assert skipped[duplicate.id] == "unrepairable_current_exit_reason"
    assert skipped[ambiguous.id] == "ambiguous_or_missing_repair_reason"
    assert refresh["queued"] is True
    assert refresh["event_type"] == EDGE_RELIABILITY_REFRESH
    assert refresh["scan_pattern_id"] == pat.id
    assert refresh["asset_class"] == "stock"
    assert refresh["repaired_count"] == 3
    work = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == EDGE_RELIABILITY_REFRESH)
        .one()
    )
    assert refresh["event_id"] == work.id
    assert work.payload["scan_pattern_id"] == pat.id
    assert work.payload["asset_class"] == "stock"
    assert work.payload["source"] == "provenance_backfill_exit_repair"


def test_cash_deployment_work_producer_enqueues_targeted_deduped_work(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)

    recert = _pattern(
        db,
        name="cash work recert",
        recert=True,
        recert_reason="negative_oos_recert",
    )
    recert_alert = _alert(db, recert, ticker="WRCRT")
    _run(db, recert, recert_alert, expected=3.0)
    _closed_paper(db, recert, recert_alert)

    shadow = _pattern(db, name="cash work shadow", lifecycle="shadow_promoted")
    shadow_alert = _alert(db, shadow, ticker="WSHDW")
    _run(db, shadow, shadow_alert, expected=2.5)
    _closed_paper(db, shadow, shadow_alert)
    db.commit()

    first = enqueue_cash_deployment_work(
        db,
        window_days=7,
        limit=10,
        include_null_lineage=False,
        include_snapshot_coverage=False,
    )
    db.commit()
    second = enqueue_cash_deployment_work(
        db,
        window_days=7,
        limit=10,
        include_null_lineage=False,
        include_snapshot_coverage=False,
    )
    db.commit()

    rows = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type.in_(("recert_rescue_refresh", "exit_variant_refresh")))
        .all()
    )
    by_type = {row.event_type: row for row in rows}

    assert first["created"] == 2
    assert second["created"] == 0
    assert set(first["event_types"]) == {"recert_rescue_refresh", "exit_variant_refresh"}
    assert set(by_type) == {"recert_rescue_refresh", "exit_variant_refresh"}
    assert by_type["recert_rescue_refresh"].payload["scan_pattern_id"] == recert.id
    assert by_type["recert_rescue_refresh"].payload["asset_class"] == "stock"
    assert by_type["exit_variant_refresh"].payload["scan_pattern_id"] == shadow.id
    assert by_type["exit_variant_refresh"].payload["cash_deployment_category"] == "positive_ev_shadow"


def test_cost_gate_execution_block_rollup_groups_by_pattern_venue_edge_source(db):
    pat = _pattern(db, name="cost gate blocked")
    now = datetime.utcnow()
    shared = {
        "source": "autotrader_cost_gate_execution_blocked",
        "scan_pattern_id": pat.id,
        "asset_class": "stock",
        "broker_venue": "robinhood",
        "cost_gate_edge_pct_source": "entry_execution.entry_edge_expected_net_pct",
        "cash_deployment_category": "positive_ev_execution_blocked",
        "graduation_blocker": "execution_blocked",
        "recommended_work_event": "edge_reliability_refresh",
    }
    for i, (ticker, expected, gap, threshold) in enumerate(
        (("CGB", 1.2, 0.9, 210), ("CGB2", 1.6, 1.1, 230)),
        start=1,
    ):
        db.add(
            BrainWorkEvent(
                domain="trading",
                event_type="exit_variant_refresh",
                event_kind="work",
                dedupe_key=f"cost-gate-rollup-{i}",
                status="pending" if i == 1 else "done",
                payload={
                    **shared,
                    "ticker": ticker,
                    "expected_net_pct": expected,
                    "cost_gate_edge_gap_pct": gap,
                    "cost_gate_edge_bps": int(expected * 100),
                    "cost_gate_threshold_bps": threshold,
                    "cost_gate_tca_cost_bps": 180,
                    "cost_gate_fee_bps": 0,
                },
                created_at=now - timedelta(minutes=i),
            )
        )
    db.add(
        BrainWorkEvent(
            domain="trading",
            event_type="edge_reliability_refresh",
            event_kind="work",
            dedupe_key="cost-gate-rollup-direct-edge-refresh",
            status="pending",
            payload={
                **shared,
                "ticker": "CGB3",
                "expected_net_pct": 1.4,
                "cost_gate_edge_gap_pct": 0.7,
                "cost_gate_edge_bps": 140,
                "cost_gate_threshold_bps": 210,
                "cost_gate_tca_cost_bps": 180,
                "cost_gate_fee_bps": 0,
            },
            created_at=now - timedelta(minutes=3),
        )
    )
    db.add(
        BrainWorkEvent(
            domain="trading",
            event_type="exit_variant_refresh",
            event_kind="work",
            dedupe_key="cost-gate-rollup-other-source",
            status="pending",
            payload={**shared, "source": "cash_deployment", "ticker": "IGNORED"},
            created_at=now,
        )
    )
    db.commit()

    out = cost_gate_execution_block_rollup(db, window_days=7, limit=10)

    assert out["total_groups"] == 1
    assert out["returned_groups"] == 1
    assert out["total_blocked_events_returned"] == 3
    assert out["venues"] == {"robinhood": 1}
    assert out["edge_sources"] == {
        "entry_execution.entry_edge_expected_net_pct": 1,
    }
    row = out["rows"][0]
    assert row["scan_pattern_id"] == pat.id
    assert row["asset_class"] == "stock"
    assert row["broker_venue"] == "robinhood"
    assert row["blocked_count"] == 3
    assert row["ticker_count"] == 3
    assert row["tickers"] == ["CGB", "CGB2", "CGB3"]
    assert row["statuses"] == {"pending": 2, "done": 1}
    assert row["avg_expected_net_pct"] == pytest.approx(1.4)
    assert row["max_expected_net_pct"] == pytest.approx(1.6)
    assert row["avg_cost_gate_edge_gap_pct"] == pytest.approx(0.9)
    assert row["max_cost_gate_threshold_bps"] == pytest.approx(230)
    assert row["max_cost_gate_tca_cost_bps"] == pytest.approx(180)
    assert row["cash_deployment_category"] == "positive_ev_execution_blocked"
    assert row["recommended_work_event"] == "edge_reliability_refresh"


def test_imminent_snapshot_coverage_enqueues_missing_asset_slices(db):
    mixed = _pattern(db, name="cash coverage mixed")
    mixed.asset_class = "all"
    stock_alert = _alert(db, mixed, ticker="COVS")
    crypto_alert = _alert(db, mixed, ticker="COVC-USD")
    crypto_alert.asset_type = "crypto"
    db.commit()

    first = enqueue_imminent_edge_snapshot_coverage_work(
        db,
        window_days=7,
        limit=10,
        lookback_minutes=120,
        max_snapshot_age_minutes=60,
    )
    db.commit()
    second = enqueue_imminent_edge_snapshot_coverage_work(
        db,
        window_days=7,
        limit=10,
        lookback_minutes=120,
        max_snapshot_age_minutes=60,
    )
    db.commit()

    rows = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_kind == "work")
        .filter(BrainWorkEvent.event_type == "edge_reliability_refresh")
        .all()
    )
    assets = {row.payload["asset_class"] for row in rows}

    assert first["considered_slices"] == 2
    assert first["created"] == 2
    assert first["missing_snapshot"] == 2
    assert second["created"] == 0
    assert second["skipped_deduped"] == 2
    assert assets == {"stock", "crypto"}
    assert all(row.payload["scan_pattern_id"] == mixed.id for row in rows)
    assert all(row.payload["source"] == "imminent_snapshot_coverage" for row in rows)


def test_cash_deployment_skips_recent_noop_exit_variant_same_evidence(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)

    shadow = _pattern(db, name="cash work noop shadow", lifecycle="shadow_promoted")
    alert = _alert(db, shadow, ticker="WNOOP")
    _run(db, shadow, alert, expected=2.5)
    _closed_paper(db, shadow, alert)
    db.commit()

    row = cash_deployment_rows(db, window_days=7, limit=10)[0]
    assert row["scan_pattern_id"] == shadow.id
    assert row["recommended_work_event"] == "exit_variant_refresh"
    fingerprint = str(row["evidence_fingerprint"])
    db.add(
        BrainWorkEvent(
            domain="trading",
            event_type="exit_variant_diagnostic",
            event_kind="outcome",
            dedupe_key=f"noop-exit-variant:{shadow.id}:{fingerprint}",
            status="done",
            payload={
                "scan_pattern_id": shadow.id,
                "evidence_fingerprint": fingerprint,
                "created_count": 0,
            },
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    out = enqueue_cash_deployment_work(
        db,
        window_days=7,
        limit=10,
        include_null_lineage=False,
        include_snapshot_coverage=False,
    )
    db.commit()

    assert out["created"] == 0
    assert out["skipped_noop_cooldown"] == 1
    assert (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_kind == "work")
        .filter(BrainWorkEvent.event_type == "exit_variant_refresh")
        .count()
        == 0
    )


def test_cash_deployment_skips_structural_noop_exit_variant_with_new_fingerprint(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)

    shadow = _pattern(db, name="cash work structural noop", lifecycle="shadow_promoted")
    alert = _alert(db, shadow, ticker="WSTRU")
    _run(db, shadow, alert, expected=2.5)
    _closed_paper(db, shadow, alert)
    db.add(
        BrainWorkEvent(
            domain="trading",
            event_type="exit_variant_diagnostic",
            event_kind="outcome",
            dedupe_key=f"structural-noop-exit-variant:{shadow.id}",
            status="done",
            payload={
                "scan_pattern_id": shadow.id,
                "evidence_fingerprint": "older-fingerprint",
                "created_count": 0,
                "skip_reason": "missing_parent_payoff_geometry",
            },
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    out = enqueue_cash_deployment_work(
        db,
        window_days=7,
        limit=10,
        include_null_lineage=False,
        include_snapshot_coverage=False,
    )
    db.commit()

    assert out["created"] == 0
    assert out["skipped_noop_cooldown"] == 1
    assert (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_kind == "work")
        .filter(BrainWorkEvent.event_type == "exit_variant_refresh")
        .count()
        == 0
    )


def test_cash_deployment_skips_too_negative_exit_debt_with_new_fingerprint(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)

    shadow = _pattern(db, name="cash work too negative debt noop", lifecycle="shadow_promoted")
    alert = _alert(db, shadow, ticker="WDEBT")
    _run(db, shadow, alert, expected=2.5)
    _closed_paper(db, shadow, alert)
    db.add(
        BrainWorkEvent(
            domain="trading",
            event_type="exit_variant_diagnostic",
            event_kind="outcome",
            dedupe_key=f"too-negative-exit-debt:{shadow.id}",
            status="done",
            payload={
                "scan_pattern_id": shadow.id,
                "evidence_fingerprint": "older-fingerprint",
                "created_count": 0,
                "skip_reason": "edge_debt_too_negative_for_exit_child:-0.793",
            },
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    out = enqueue_cash_deployment_work(
        db,
        window_days=7,
        limit=10,
        include_null_lineage=False,
        include_snapshot_coverage=False,
    )
    db.commit()

    assert out["created"] == 0
    assert out["skipped_noop_cooldown"] == 1
    assert (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_kind == "work")
        .filter(BrainWorkEvent.event_type == "exit_variant_refresh")
        .count()
        == 0
    )


def test_cash_deployment_skips_duplicate_exit_child_with_new_fingerprint(
    db,
    monkeypatch,
):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 5)
    monkeypatch.setattr(settings, "brain_work_cash_deployment_noop_cooldown_minutes", 360)

    shadow = _pattern(db, name="cash work duplicate exit child noop", lifecycle="shadow_promoted")
    alert = _alert(db, shadow, ticker="WDUP")
    _run(db, shadow, alert, expected=2.5)
    _closed_paper(db, shadow, alert)
    db.add(
        BrainWorkEvent(
            domain="trading",
            event_type="exit_variant_diagnostic",
            event_kind="outcome",
            dedupe_key=f"duplicate-exit-child:{shadow.id}",
            status="done",
            payload={
                "scan_pattern_id": shadow.id,
                "evidence_fingerprint": "older-fingerprint",
                "created_count": 0,
                "skip_reason": "duplicate_learned_exit_label",
                "existing_child_id": shadow.id + 1000,
            },
            created_at=datetime.utcnow(),
        )
    )
    db.commit()

    out = enqueue_cash_deployment_work(
        db,
        window_days=7,
        limit=10,
        include_null_lineage=False,
        include_snapshot_coverage=False,
    )
    db.commit()

    assert out["created"] == 0
    assert out["skipped_noop_cooldown"] == 1
    assert (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_kind == "work")
        .filter(BrainWorkEvent.event_type == "exit_variant_refresh")
        .count()
        == 0
    )
