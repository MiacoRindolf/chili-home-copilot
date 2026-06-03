from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
import pytest
from sqlalchemy import create_engine
from sqlalchemy.dialects import postgresql
from sqlalchemy.orm import Session

from app.models.trading import (
    AutoTraderRun,
    BrainWorkEvent,
    BreakoutAlert,
    PaperTrade,
    ScanPattern,
    Trade,
)
from app.services.trading.brain_work.dispatcher import run_brain_work_dispatch_round
from app.services.trading.brain_work.handlers.profitability import (
    handle_recert_rescue_post_backtest,
    handle_recert_rescue_refresh,
)
from app.services.trading.brain_work.ledger import enqueue_work_event
from app.services.trading.edge_reliability import (
    EDGE_RELIABILITY_REFRESH,
    EDGE_RELIABILITY_SNAPSHOT,
    RECERT_RESCUE_REFRESH,
    RECERT_RESCUE_DIAGNOSTIC,
    _autotrader_run_summary_query,
    _canonical_asset_class,
    _asset_class_for_paper,
    _asset_class_for_trade,
    _expected_net_pct_from_run,
    _live_return_pct,
    _mean,
    _outcome_label_from_return,
    _paper_return_pct,
    _probability_or_none,
    compute_pattern_edge_reliability,
    edge_supply_rows,
    emit_edge_reliability_refresh_requested,
    emit_targeted_profitability_work,
    null_lineage_short_paper_candidates,
)


def test_edge_reliability_autotrader_run_query_is_summary_only() -> None:
    engine = create_engine("sqlite:///:memory:")
    with Session(engine) as db:
        query = _autotrader_run_summary_query(db).filter(
            AutoTraderRun.scan_pattern_id == 123
        )
        sql = str(
            query.statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )

    assert "trading_autotrader_runs.scan_pattern_id = 123" in sql
    assert "trading_autotrader_runs.rule_snapshot" in sql
    assert "trading_autotrader_runs.llm_snapshot" not in sql
    assert "trading_autotrader_runs.management_scope" not in sql


def _pattern(db, **kwargs) -> ScanPattern:
    if kwargs.get("promotion_gate_passed") and "cpcv_n_paths" not in kwargs:
        kwargs["cpcv_n_paths"] = 20
    pat = ScanPattern(
        name=kwargs.pop("name", "edge reliability pattern"),
        rules_json={},
        origin="test",
        asset_class=kwargs.pop("asset_class", "stocks"),
        timeframe="1d",
        active=True,
        lifecycle_stage=kwargs.pop("lifecycle_stage", "promoted"),
        **kwargs,
    )
    db.add(pat)
    db.flush()
    return pat


def _alert(db, pat: ScanPattern, ticker: str = "EDGE") -> BreakoutAlert:
    alert = BreakoutAlert(
        scan_pattern_id=pat.id,
        ticker=ticker,
        asset_type="stock",
        alert_tier="pattern_imminent",
        outcome="pending",
        score_at_alert=80.0,
        price_at_alert=100.0,
        entry_price=100.0,
        stop_loss=95.0,
        target_price=110.0,
        alerted_at=datetime.utcnow(),
        indicator_snapshot={
            "imminent_scorecard": {"signal_lane": "shadow_near_miss"}
        },
    )
    db.add(alert)
    db.flush()
    return alert


def _run(db, pat: ScanPattern, alert: BreakoutAlert, *, expected: float = 2.0):
    row = AutoTraderRun(
        breakout_alert_id=alert.id,
        scan_pattern_id=pat.id,
        ticker=alert.ticker,
        decision="blocked",
        reason="selector:shadow_observation_signal_lane",
        rule_snapshot={
            "paper_observation_signal_lane": "shadow_near_miss",
            "entry_edge": {
                "expected_net_pct": expected,
                "probability": 0.6,
                "breakeven_probability": 0.5,
                "probability_source": "pattern_regime_hit_rate",
            },
        },
    )
    db.add(row)
    db.flush()
    return row


def test_edge_reliability_asset_class_for_paper_uses_contract_identity() -> None:
    row = SimpleNamespace(
        ticker="SPY",
        signal_json='{"breakout_alert":{"asset_kind":"option"}}',
    )

    assert _asset_class_for_paper(row, alert=None, pattern=None) == "options"


def test_edge_reliability_asset_class_for_trade_uses_contract_identity() -> None:
    row = SimpleNamespace(
        ticker="SPY",
        asset_kind=None,
        tags=None,
        indicator_snapshot='{"breakout_alert":{"asset_kind":"option"}}',
    )

    assert _asset_class_for_trade(row, pattern=None) == "options"


def test_edge_reliability_label_prefers_partial_aware_return_over_pnl() -> None:
    assert _outcome_label_from_return(-10.0, 4.0) == 1


def test_edge_reliability_paper_return_prefers_realized_pnl_over_legacy_pct() -> None:
    row = SimpleNamespace(
        entry_price=100.0,
        exit_price=116.0,
        quantity=2.0,
        pnl=32.0,
        pnl_pct=-9999.0,
        direction="long",
        signal_json={"asset_type": "stock"},
    )

    assert _paper_return_pct(row) == pytest.approx(16.0)


def test_edge_reliability_live_return_uses_partial_aware_realized_pnl() -> None:
    row = SimpleNamespace(
        entry_price=100.0,
        exit_price=105.0,
        quantity=1.0,
        filled_quantity=None,
        pnl=5.0,
        direction="long",
        asset_kind="stock",
        tags=None,
        indicator_snapshot={},
        partial_taken=True,
        partial_taken_qty=1.0,
        partial_taken_price=110.0,
    )

    assert _live_return_pct(row) == pytest.approx(7.5)


def test_edge_reliability_numeric_helpers_reject_malformed_evidence() -> None:
    run = SimpleNamespace(rule_snapshot={"entry_edge": {"expected_net_pct": True}})

    assert _expected_net_pct_from_run(run) is None
    assert _probability_or_none(True) is None
    assert _probability_or_none(-0.01) is None
    assert _probability_or_none(1.01) is None
    assert _probability_or_none(0.55) == pytest.approx(0.55)
    assert _mean([True, float("nan"), 1.0, 3.0]) == pytest.approx(2.0)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("robinhood_options", "options"),
        ("option_contract", "options"),
        ("digital_asset", "crypto"),
        ("equities", "stock"),
    ],
)
def test_edge_reliability_canonical_asset_class_uses_shared_aliases(raw, expected) -> None:
    assert _canonical_asset_class(raw) == expected


def test_edge_reliability_attribution_from_runs_paper_and_live(db):
    pat = _pattern(db)
    alert = _alert(db, pat)
    _run(db, pat, alert, expected=2.0)
    db.add(
        PaperTrade(
            scan_pattern_id=pat.id,
            paper_shadow_of_alert_id=alert.id,
            ticker="EDGE",
            direction="long",
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            quantity=1.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=105.0,
            pnl=5.0,
            pnl_pct=5.0,
            signal_json={"paper_shadow": True},
        )
    )
    db.add(
        Trade(
            scan_pattern_id=pat.id,
            ticker="EDGE",
            direction="long",
            entry_price=100.0,
            quantity=1.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=102.0,
            pnl=2.0,
            exit_reason="target",
        )
    )
    db.commit()

    row = compute_pattern_edge_reliability(db, pat.id, window_days=7)

    assert row["edge_eval_count"] == 1
    assert row["expected_ev_pct"] == pytest.approx(2.0)
    assert row["realized_ev_pct"] == pytest.approx(3.5)
    assert row["ev_calibration_error"] == pytest.approx(1.5)
    assert row["closed_evidence_count"] == 2
    assert row["brier_score"] == pytest.approx(0.16)
    assert row["recommended_work_event"] == EDGE_RELIABILITY_REFRESH


def test_edge_reliability_separates_low_confidence_live_exits_from_clean_ev(db):
    pat = _pattern(db)
    alert = _alert(db, pat)
    _run(db, pat, alert, expected=2.0)
    db.add_all(
        [
            Trade(
                scan_pattern_id=pat.id,
                related_alert_id=alert.id,
                ticker="EDGE",
                direction="long",
                entry_price=100.0,
                quantity=1.0,
                status="closed",
                entry_date=datetime.utcnow(),
                exit_date=datetime.utcnow(),
                exit_price=104.0,
                pnl=4.0,
                exit_reason="target",
            ),
            Trade(
                scan_pattern_id=pat.id,
                related_alert_id=alert.id,
                ticker="EDGE",
                direction="long",
                entry_price=100.0,
                quantity=1.0,
                status="closed",
                entry_date=datetime.utcnow(),
                exit_date=datetime.utcnow(),
                exit_price=90.0,
                pnl=-10.0,
                exit_reason="broker_reconcile_position_gone",
            ),
            Trade(
                scan_pattern_id=pat.id,
                related_alert_id=alert.id,
                ticker="EDGE",
                direction="long",
                entry_price=100.0,
                quantity=1.0,
                status="closed",
                entry_date=datetime.utcnow(),
                exit_date=datetime.utcnow(),
                exit_price=95.0,
                pnl=-5.0,
                exit_reason=None,
            ),
        ]
    )
    db.commit()

    row = compute_pattern_edge_reliability(db, pat.id, window_days=7)

    assert row["closed_evidence_count"] == 1
    assert row["live_closed_count"] == 1
    assert row["live_clean_exit_count"] == 1
    assert row["live_total_closed_count"] == 3
    assert row["realized_ev_pct"] == pytest.approx(4.0)
    assert row["live_realized_ev_pct"] == pytest.approx(4.0)
    assert row["observed_win_rate"] == pytest.approx(1.0)
    assert row["brier_score"] == pytest.approx(0.16)
    assert row["live_low_confidence_exit_count"] == 2
    assert row["live_low_confidence_return_count"] == 2
    assert row["live_low_confidence_pnl_sample_n"] == 2
    assert row["live_low_confidence_total_pnl_usd"] == pytest.approx(-15.0)
    assert row["live_low_confidence_realized_ev_pct"] == pytest.approx(-7.5)
    assert row["live_low_confidence_win_rate"] == pytest.approx(0.0)
    assert row["live_low_confidence_exit_reasons"]["missing"] == 1
    assert row["live_low_confidence_exit_reasons"]["broker_reconcile_position_gone"] == 1


def test_edge_reliability_rejects_malformed_expected_probability_evidence(db):
    pat = _pattern(db)
    alert = _alert(db, pat)
    run = _run(db, pat, alert, expected=2.0)
    run.rule_snapshot = {
        "paper_observation_signal_lane": "shadow_near_miss",
        "entry_edge": {
            "expected_net_pct": True,
            "probability": 1.25,
            "breakeven_probability": False,
            "probability_source": "bad_model_payload",
        },
    }
    db.add(
        PaperTrade(
            scan_pattern_id=pat.id,
            paper_shadow_of_alert_id=alert.id,
            ticker="EDGE",
            direction="long",
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            quantity=1.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=105.0,
            pnl=5.0,
            pnl_pct=5.0,
            signal_json={"paper_shadow": True},
        )
    )
    db.commit()

    row = compute_pattern_edge_reliability(db, pat.id, window_days=7)

    assert row["edge_eval_count"] == 1
    assert row["expected_ev_pct"] is None
    assert row["avg_probability"] is None
    assert row["avg_breakeven_probability"] is None
    assert row["brier_score"] is None
    assert row["realized_ev_pct"] == pytest.approx(5.0)
    assert row["calibrated_ev_pct"] == pytest.approx(5.0)


def test_edge_reliability_slices_all_asset_patterns_by_asset(db):
    pat = _pattern(db, asset_class="all", lifecycle_stage="promoted")
    stock_alert = _alert(db, pat, "STKA")
    crypto_alert = _alert(db, pat, "BTC-USD")
    crypto_alert.asset_type = "crypto"
    _run(db, pat, stock_alert, expected=-1.0)
    _run(db, pat, crypto_alert, expected=3.0)
    db.add(
        PaperTrade(
            scan_pattern_id=pat.id,
            paper_shadow_of_alert_id=stock_alert.id,
            ticker="STKA",
            direction="long",
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            quantity=1.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=98.0,
            pnl=-2.0,
            pnl_pct=-2.0,
            signal_json={"paper_shadow": True, "asset_type": "stock"},
        )
    )
    db.add(
        PaperTrade(
            scan_pattern_id=pat.id,
            paper_shadow_of_alert_id=crypto_alert.id,
            ticker="BTC-USD",
            direction="long",
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            quantity=1.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=106.0,
            pnl=6.0,
            pnl_pct=6.0,
            signal_json={"paper_shadow": True, "asset_type": "crypto"},
        )
    )
    db.commit()

    stock = compute_pattern_edge_reliability(db, pat.id, asset_class="stock", window_days=7)
    crypto = compute_pattern_edge_reliability(db, pat.id, asset_class="crypto", window_days=7)
    blended = compute_pattern_edge_reliability(db, pat.id, window_days=7)

    assert stock["asset_class"] == "stock"
    assert stock["slice_asset_class"] == "stock"
    assert stock["edge_eval_count"] == 1
    assert stock["expected_ev_pct"] == pytest.approx(-1.0)
    assert stock["realized_ev_pct"] == pytest.approx(-2.0)
    assert stock["primary_symbol"] == "STKA"

    assert crypto["asset_class"] == "crypto"
    assert crypto["edge_eval_count"] == 1
    assert crypto["expected_ev_pct"] == pytest.approx(3.0)
    assert crypto["realized_ev_pct"] == pytest.approx(6.0)
    assert crypto["primary_symbol"] == "BTC-USD"

    assert blended["asset_class"] == "all"
    assert blended["edge_eval_count"] == 2
    assert blended["realized_ev_pct"] == pytest.approx(2.0)

    rows = edge_supply_rows(db, pattern_ids=[pat.id], window_days=7, limit=10)
    by_asset = {row["asset_class"]: row for row in rows}
    assert set(by_asset) == {"stock", "crypto"}
    assert by_asset["stock"]["primary_symbol"] == "STKA"
    assert by_asset["crypto"]["primary_symbol"] == "BTC-USD"


def test_edge_reliability_excludes_ambiguous_option_paper_pct(db):
    pat = _pattern(db, asset_class="option")
    alert = _alert(db, pat, "OPT")
    alert.asset_type = "option"
    db.add(
        PaperTrade(
            scan_pattern_id=pat.id,
            paper_shadow_of_alert_id=alert.id,
            ticker="OPT",
            direction="long",
            entry_price=4.01,
            stop_price=2.0,
            target_price=8.0,
            quantity=1.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=716.0,
            pnl=None,
            pnl_pct=17755.61,
            signal_json={"asset_type": "options", "option_meta": {"strike": 700.0}},
        )
    )
    db.commit()

    row = compute_pattern_edge_reliability(db, pat.id, window_days=7)

    assert row["closed_evidence_count"] == 0
    assert row["paper_closed_count"] == 0
    assert row["realized_ev_pct"] is None
    assert row["paper_realized_ev_pct"] is None


def test_edge_reliability_counts_option_paper_with_realized_pnl(db):
    pat = _pattern(db, asset_class="option")
    alert = _alert(db, pat, "OPT")
    alert.asset_type = "option"
    db.add(
        PaperTrade(
            scan_pattern_id=pat.id,
            paper_shadow_of_alert_id=alert.id,
            ticker="OPT",
            direction="long",
            entry_price=1.25,
            stop_price=0.75,
            target_price=2.0,
            quantity=2.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=1.45,
            pnl=40.0,
            pnl_pct=1600.0,
            signal_json={"asset_type": "options", "option_meta": {"strike": 500.0}},
        )
    )
    db.commit()

    row = compute_pattern_edge_reliability(db, pat.id, window_days=7)

    assert row["closed_evidence_count"] == 1
    assert row["paper_closed_count"] == 1
    assert row["realized_ev_pct"] == pytest.approx(16.0)
    assert row["paper_realized_ev_pct"] == pytest.approx(16.0)


def test_edge_reliability_labels_option_paper_with_confirmed_return_fallback(db):
    pat = _pattern(db, asset_class="option")
    alert = _alert(db, pat, "OPT")
    alert.asset_type = "option"
    _run(db, pat, alert, expected=2.0)
    db.add(
        PaperTrade(
            scan_pattern_id=pat.id,
            paper_shadow_of_alert_id=alert.id,
            ticker="OPT",
            direction="long",
            entry_price=1.25,
            stop_price=0.75,
            target_price=2.0,
            quantity=2.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=1.45,
            pnl=None,
            pnl_pct=9999.0,
            signal_json={
                "asset_type": "options",
                "option_meta": {"price_domain": "option_premium"},
                "price_domains": {
                    "entry_price": "option_premium",
                    "exit_price": "option_premium",
                },
            },
        )
    )
    db.commit()

    row = compute_pattern_edge_reliability(db, pat.id, window_days=7)

    assert row["closed_evidence_count"] == 1
    assert row["paper_closed_count"] == 1
    assert row["realized_ev_pct"] == pytest.approx(16.0)
    assert row["observed_win_rate"] == pytest.approx(1.0)
    assert row["brier_score"] == pytest.approx(0.16)


def test_edge_reliability_labels_option_live_with_confirmed_return_fallback(db):
    pat = _pattern(db, asset_class="option")
    alert = _alert(db, pat, "OPT")
    alert.asset_type = "option"
    _run(db, pat, alert, expected=2.0)
    db.add(
        Trade(
            scan_pattern_id=pat.id,
            related_alert_id=alert.id,
            ticker="OPT",
            direction="long",
            entry_price=1.25,
            quantity=2.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=1.45,
            pnl=None,
            asset_kind="option",
            exit_reason="target",
            indicator_snapshot={
                "asset_type": "options",
                "option_meta": {"price_domain": "option_premium"},
                "price_domains": {
                    "entry_price": "option_premium",
                    "exit_price": "option_premium",
                },
            },
        )
    )
    db.commit()

    row = compute_pattern_edge_reliability(db, pat.id, window_days=7)

    assert row["closed_evidence_count"] == 1
    assert row["live_closed_count"] == 1
    assert row["realized_ev_pct"] == pytest.approx(16.0)
    assert row["live_realized_ev_pct"] == pytest.approx(16.0)
    assert row["observed_win_rate"] == pytest.approx(1.0)
    assert row["brier_score"] == pytest.approx(0.16)


def test_edge_reliability_option_slice_keeps_alias_runs_and_realized_paper(db):
    pat = _pattern(db, asset_class="all")
    alert = _alert(db, pat, "SPY")
    run = _run(db, pat, alert, expected=2.0)
    run.rule_snapshot = {**run.rule_snapshot, "asset_class": "robinhood_options"}
    db.add(
        PaperTrade(
            scan_pattern_id=pat.id,
            paper_shadow_of_alert_id=alert.id,
            ticker="SPY",
            direction="long",
            entry_price=1.25,
            stop_price=0.75,
            target_price=2.0,
            quantity=2.0,
            status="closed",
            entry_date=datetime.utcnow(),
            exit_date=datetime.utcnow(),
            exit_price=1.45,
            pnl=40.0,
            pnl_pct=1600.0,
            signal_json={
                "asset_class": "robinhood_options",
                "option_meta": {"strike": 500.0},
            },
        )
    )
    db.commit()

    row = compute_pattern_edge_reliability(
        db,
        pat.id,
        asset_class="options",
        window_days=7,
    )

    assert row["asset_class"] == "options"
    assert row["slice_asset_class"] == "options"
    assert row["edge_eval_count"] == 1
    assert row["asset_types"] == {"options": 1}
    assert row["closed_evidence_count"] == 1
    assert row["paper_closed_count"] == 1
    assert row["realized_ev_pct"] == pytest.approx(16.0)
    assert row["paper_realized_ev_pct"] == pytest.approx(16.0)
    assert row["expected_ev_pct"] == pytest.approx(2.0)


def test_null_lineage_short_candidates_skip_unpriced_option_legacy_pct(db):
    good = PaperTrade(
        scan_pattern_id=None,
        ticker="SPY",
        direction="short",
        entry_price=1.25,
        stop_price=2.0,
        target_price=0.75,
        quantity=1.0,
        status="closed",
        entry_date=datetime.utcnow(),
        exit_date=datetime.utcnow(),
        exit_price=1.05,
        pnl=None,
        pnl_pct=-9999.0,
        signal_json={
            "asset_type": "options",
            "strategy": "short_call_reject",
            "price_domains": {
                "entry_price": "option_premium",
                "exit_price": "option_premium",
            },
        },
    )
    ambiguous = PaperTrade(
        scan_pattern_id=None,
        ticker="SPY",
        direction="short",
        entry_price=4.01,
        stop_price=8.0,
        target_price=2.0,
        quantity=1.0,
        status="closed",
        entry_date=datetime.utcnow(),
        exit_date=datetime.utcnow(),
        exit_price=716.0,
        pnl=None,
        pnl_pct=17755.61,
        signal_json={
            "asset_type": "options",
            "strategy": "short_call_reject",
        },
    )
    db.add_all([good, ambiguous])
    db.commit()

    rows = null_lineage_short_paper_candidates(
        db,
        window_days=7,
        min_total_pnl=0.0,
    )

    assert len(rows) == 1
    row = rows[0]
    assert row["closed_count"] == 1
    assert row["avg_pnl_pct"] == pytest.approx(16.0)
    assert row["paper_trade_ids"] == [good.id]


def test_edge_supply_prefers_recent_positive_edge_over_arbitrary_distinct_order(db):
    for idx in range(12):
        pat = _pattern(db, name=f"low value pattern {idx}")
        alert = _alert(db, pat, f"LOW{idx}")
        _run(db, pat, alert, expected=-1.0)

    target = _pattern(db, name="high value edge pattern")
    target_alert = _alert(db, target, "HIGH")
    _run(db, target, target_alert, expected=7.5)
    db.commit()

    rows = edge_supply_rows(db, window_days=7, limit=2)
    ids = [row["scan_pattern_id"] for row in rows]

    assert target.id in ids
    assert ids[0] == target.id


def test_recert_rescue_diagnostic_never_clears_hard_recert(db):
    pat = _pattern(
        db,
        recert_required=True,
        recert_reason="negative_oos_recert,thin_realized_ev",
        cpcv_median_sharpe=2.0,
        promotion_gate_passed=True,
    )
    alert = _alert(db, pat, "RECRT")
    _run(db, pat, alert, expected=5.0)
    db.commit()

    row = compute_pattern_edge_reliability(db, pat.id, window_days=7)
    assert row["graduation_blocker"] == "hard_recert_blocked"

    ev_id = enqueue_work_event(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        dedupe_key=f"test:recert:{pat.id}",
        payload={"scan_pattern_id": pat.id, "window_days": 7},
        lease_scope="edge",
    )
    db.commit()
    ev = db.get(BrainWorkEvent, ev_id)
    assert ev is not None
    handle_recert_rescue_refresh(db, ev, user_id=None)
    db.commit()
    db.refresh(pat)

    assert pat.recert_required is True
    outcome = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == RECERT_RESCUE_DIAGNOSTIC)
        .one()
    )
    assert outcome.payload["safe_to_bypass_live"] is False
    assert outcome.payload["graduation_blocker"] == "hard_recert_blocked"
    assert outcome.payload["recert_rescue_status"] == "hard_blocked"
    assert outcome.payload["hard_recert_reasons"] == ["negative_oos_recert"]
    assert outcome.payload["soft_recert_reasons"] == ["thin_realized_ev"]
    assert outcome.payload["recommended_next_action"] == (
        "keep_live_blocked_until_hard_recert_clears"
    )


def test_recert_rescue_hard_oos_positive_edge_enqueues_backtest_refresh(db):
    pat = _pattern(
        db,
        recert_required=True,
        recert_reason="negative_oos_recert",
        cpcv_median_sharpe=2.0,
        promotion_gate_passed=True,
        oos_evaluated_at=datetime.utcnow(),
        oos_trade_count=20,
        oos_win_rate=0.55,
        oos_avg_return_pct=-0.3,
        raw_realized_trade_count=8,
        raw_realized_avg_return_pct=1.0,
    )
    for idx in range(6):
        alert = _alert(db, pat, f"REBT{idx}")
        _run(db, pat, alert, expected=2.5)
    db.commit()

    ev_id = enqueue_work_event(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        dedupe_key=f"test:recert-bt:{pat.id}",
        payload={"scan_pattern_id": pat.id, "window_days": 7, "asset_class": "stock"},
        lease_scope="edge",
    )
    db.commit()
    ev = db.get(BrainWorkEvent, ev_id)
    assert ev is not None
    handle_recert_rescue_refresh(db, ev, user_id=None)
    db.commit()
    db.refresh(pat)

    assert pat.recert_required is True
    assert pat.recert_reason == "negative_oos_recert"
    outcome = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == RECERT_RESCUE_DIAGNOSTIC)
        .one()
    )
    refresh = outcome.payload["recert_backtest_refresh"]
    assert refresh["requested"] is True
    assert refresh["reason"] == "positive_edge_supply_needs_asset_sliced_oos_refresh"
    assert refresh["asset_class"] == "stock"
    assert outcome.payload["safe_to_bypass_live"] is False
    assert outcome.payload["recert_rescue_status"] == "hard_blocked"
    assert outcome.payload["recommended_next_action"] == (
        "run_recert_backtest_refresh_keep_live_blocked"
    )

    queued = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == "backtest_requested")
        .one()
    )
    assert queued.payload["source"] == "recert_rescue_refresh"
    assert queued.payload["scan_pattern_id"] == pat.id
    assert queued.lease_scope == "backtest"


def test_recert_rescue_reuses_open_backtest_refresh_for_pattern_asset(db):
    pat = _pattern(
        db,
        recert_required=True,
        recert_reason="negative_oos_recert",
        cpcv_median_sharpe=2.0,
        promotion_gate_passed=True,
        oos_evaluated_at=datetime.utcnow(),
        oos_trade_count=20,
        oos_win_rate=0.55,
        oos_avg_return_pct=-0.3,
        raw_realized_trade_count=8,
        raw_realized_avg_return_pct=1.0,
    )
    pat_id = pat.id
    open_id = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key=f"test:open-recert-bt:{pat_id}:old",
        payload={
            "scan_pattern_id": pat_id,
            "source": "recert_rescue_refresh",
            "asset_class": "stock",
            "evidence_fingerprint": "old-fingerprint",
        },
        lease_scope="backtest",
    )
    for idx in range(6):
        alert = _alert(db, pat, f"REOPEN{idx}")
        _run(db, pat, alert, expected=2.5)
    db.commit()
    assert open_id is not None

    ev_id = enqueue_work_event(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        dedupe_key=f"test:recert-bt-open:{pat_id}",
        payload={"scan_pattern_id": pat_id, "window_days": 7, "asset_class": "stock"},
        lease_scope="edge",
    )
    db.commit()
    ev = db.get(BrainWorkEvent, ev_id)
    assert ev is not None

    handle_recert_rescue_refresh(db, ev, user_id=None)
    db.commit()

    backtests = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == "backtest_requested")
        .all()
    )
    assert len(backtests) == 1
    outcome = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == RECERT_RESCUE_DIAGNOSTIC)
        .one()
    )
    refresh = outcome.payload["recert_backtest_refresh"]
    assert refresh["requested"] is False
    assert refresh["event_id"] == open_id
    assert refresh["reason"] == "recert_backtest_refresh_already_open"
    assert outcome.payload["safe_to_bypass_live"] is False


def test_recert_rescue_post_backtest_reconciles_without_requeue(db):
    pat = _pattern(
        db,
        recert_required=True,
        recert_reason="missing_oos_recert,missing_quality_composite_score,thin_realized_ev",
        cpcv_median_sharpe=2.0,
        promotion_gate_passed=True,
        quality_composite_score=0.72,
        oos_evaluated_at=datetime.utcnow(),
        oos_trade_count=35,
        oos_win_rate=0.58,
        oos_avg_return_pct=1.2,
        raw_realized_trade_count=9,
        raw_realized_avg_return_pct=1.1,
    )
    alert = _alert(db, pat, "REPOST")
    _run(db, pat, alert, expected=1.5)
    parent_id = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key=f"test:post-bt-parent:{pat.id}",
        payload={
            "scan_pattern_id": pat.id,
            "source": "recert_rescue_refresh",
            "asset_class": "stock",
            "window_days": 7,
            "recert_refresh_reason": "soft_recert_needs_oos_quality_refresh",
        },
        lease_scope="backtest",
    )
    db.commit()
    ev = BrainWorkEvent(
        event_type="backtest_completed",
        event_kind="outcome",
        status="processing",
        domain="trading",
        dedupe_key=f"test:post-bt-done:{pat.id}",
        payload={
            "scan_pattern_id": pat.id,
            "parent_work_event_id": parent_id,
            "backtests_run": 1,
        },
        parent_event_id=parent_id,
    )
    db.add(ev)
    db.commit()

    assert handle_recert_rescue_post_backtest(db, ev, user_id=None) is True
    db.commit()
    db.refresh(pat)

    assert pat.recert_required is False
    assert pat.recert_reason is None
    outcome = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == RECERT_RESCUE_DIAGNOSTIC)
        .one()
    )
    assert outcome.payload["recert_rescue_status"] == "not_recert_required"
    assert outcome.payload["safe_to_bypass_live"] is False
    assert outcome.payload["recert_backtest_refresh"]["requested"] is False
    assert outcome.payload["recert_backtest_refresh"]["reason"] == (
        "post_backtest_reconcile_only"
    )
    assert (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == "backtest_requested")
        .count()
        == 1
    )


def test_recert_rescue_refresh_removes_stale_soft_reason_but_preserves_hard_oos(
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trading.brain_work.handlers.quality_score._recompute_for_pattern",
        lambda *args, **kwargs: (0.62, 0.62, False),
    )
    pat = _pattern(
        db,
        recert_required=True,
        recert_reason="negative_oos_recert,missing_quality_composite_score",
        cpcv_median_sharpe=2.0,
        promotion_gate_passed=True,
        quality_composite_score=0.62,
        oos_evaluated_at=datetime.utcnow(),
        oos_trade_count=20,
        oos_win_rate=0.55,
        oos_avg_return_pct=-0.25,
        raw_realized_trade_count=8,
        raw_realized_avg_return_pct=2.0,
    )
    alert = _alert(db, pat, "RECLEAN")
    _run(db, pat, alert, expected=4.0)
    db.commit()

    ev_id = enqueue_work_event(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        dedupe_key=f"test:recert-clean:{pat.id}",
        payload={"scan_pattern_id": pat.id, "window_days": 7},
        lease_scope="edge",
    )
    db.commit()
    ev = db.get(BrainWorkEvent, ev_id)
    assert ev is not None
    handle_recert_rescue_refresh(db, ev, user_id=None)
    db.commit()
    db.refresh(pat)

    assert pat.recert_required is True
    assert pat.recert_reason == "negative_oos_recert"
    outcome = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == RECERT_RESCUE_DIAGNOSTIC)
        .one()
    )
    reconcile = outcome.payload["recert_reconcile"]
    assert reconcile["changed"] is True
    assert reconcile["cleared_recert_reasons"] == ["missing_quality_composite_score"]
    assert reconcile["persisted_recert_reasons"] == ["negative_oos_recert"]
    assert outcome.payload["recert_rescue_status"] == "hard_blocked"


def test_recert_rescue_refresh_clears_stale_soft_recert_when_current_evidence_passes(
    db,
    monkeypatch,
):
    monkeypatch.setattr(
        "app.services.trading.brain_work.handlers.quality_score._recompute_for_pattern",
        lambda *args, **kwargs: (0.71, 0.71, False),
    )
    pat = _pattern(
        db,
        recert_required=True,
        recert_reason="missing_oos_recert,missing_quality_composite_score,thin_realized_ev",
        cpcv_median_sharpe=2.0,
        promotion_gate_passed=True,
        quality_composite_score=0.71,
        oos_evaluated_at=datetime.utcnow(),
        oos_trade_count=30,
        oos_win_rate=0.57,
        oos_avg_return_pct=1.4,
        raw_realized_trade_count=9,
        raw_realized_avg_return_pct=1.1,
    )
    alert = _alert(db, pat, "RECLEAR")
    _run(db, pat, alert, expected=2.0)
    db.commit()

    ev_id = enqueue_work_event(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        dedupe_key=f"test:recert-clear:{pat.id}",
        payload={"scan_pattern_id": pat.id, "window_days": 7},
        lease_scope="edge",
    )
    db.commit()
    ev = db.get(BrainWorkEvent, ev_id)
    assert ev is not None
    handle_recert_rescue_refresh(db, ev, user_id=None)
    db.commit()
    db.refresh(pat)

    assert pat.recert_required is False
    assert pat.recert_reason is None
    outcome = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == RECERT_RESCUE_DIAGNOSTIC)
        .one()
    )
    reconcile = outcome.payload["recert_reconcile"]
    assert reconcile["changed"] is True
    assert reconcile["persisted_recert_reasons"] == []
    assert set(reconcile["cleared_recert_reasons"]) == {
        "missing_oos_recert",
        "missing_quality_composite_score",
        "thin_realized_ev",
    }
    assert outcome.payload["recert_rescue_status"] == "not_recert_required"


def test_recert_rescue_diagnostic_explains_soft_recert_next_action(db):
    pat = _pattern(
        db,
        recert_required=True,
        recert_reason="missing_oos_recert,missing_quality_composite_score",
        cpcv_median_sharpe=2.0,
        promotion_gate_passed=True,
    )
    alert = _alert(db, pat, "SOFTR")
    _run(db, pat, alert, expected=1.2)
    db.commit()

    row = compute_pattern_edge_reliability(db, pat.id, window_days=7)
    assert row["graduation_blocker"] == "recert_blocked"

    ev_id = enqueue_work_event(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        dedupe_key=f"test:soft-recert:{pat.id}",
        payload={"scan_pattern_id": pat.id, "window_days": 7},
        lease_scope="edge",
    )
    db.commit()
    ev = db.get(BrainWorkEvent, ev_id)
    assert ev is not None
    handle_recert_rescue_refresh(db, ev, user_id=None)
    db.commit()

    outcome = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == RECERT_RESCUE_DIAGNOSTIC)
        .one()
    )
    assert outcome.payload["safe_to_bypass_live"] is False
    assert outcome.payload["recert_rescue_status"] == "soft_blocked"
    assert outcome.payload["hard_recert_reasons"] == []
    assert outcome.payload["soft_recert_reasons"] == [
        "missing_oos_recert",
        "missing_quality_composite_score",
    ]
    assert outcome.payload["recommended_next_action"] == (
        "complete_oos_recert_and_quality_refresh"
    )


def test_edge_reliability_work_dedupe_and_dispatch(db):
    pat = _pattern(db)
    pat_id = pat.id
    alert = _alert(db, pat)
    _run(db, pat, alert, expected=1.25)
    db.commit()

    first = emit_edge_reliability_refresh_requested(
        db,
        pat_id,
        source="test",
        window_days=7,
        evidence_fingerprint="same",
    )
    second = emit_edge_reliability_refresh_requested(
        db,
        pat_id,
        source="test",
        window_days=7,
        evidence_fingerprint="same",
    )
    db.commit()

    assert first is not None
    assert second is None

    out = run_brain_work_dispatch_round(
        db,
        max_edge_reliability=1,
        max_recert_rescue=0,
        max_exit_variant=0,
        max_provenance=0,
        max_exec_feedback=0,
        max_mine=0,
        max_backtest=0,
        max_cpcv_gate=0,
        max_promote=0,
        max_trade_close=0,
        run_thin_evidence_sweep=False,
        run_market_snapshots_watchdog=False,
    )

    assert out["processed"] == 1
    snapshot = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.event_type == EDGE_RELIABILITY_SNAPSHOT)
        .one()
    )
    assert snapshot.payload["scan_pattern_id"] == pat_id
    assert snapshot.payload["expected_ev_pct"] == pytest.approx(1.25)

    recent_repeat = emit_edge_reliability_refresh_requested(
        db,
        pat_id,
        source="test",
        window_days=7,
        evidence_fingerprint="same",
    )
    db.commit()
    assert recent_repeat is None


def test_profitability_work_dedupe_skips_recent_done_same_fingerprint(db):
    pat = _pattern(db)
    pat_id = pat.id

    first = emit_targeted_profitability_work(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        scan_pattern_id=pat_id,
        source="test",
        asset_class="stock",
        evidence_fingerprint="same-fingerprint",
        payload={"expected_evidence_value": 10.0},
    )
    db.commit()
    assert first is not None

    row = db.get(BrainWorkEvent, first)
    row.status = "done"
    row.processed_at = datetime.utcnow()
    db.commit()

    repeat = emit_targeted_profitability_work(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        scan_pattern_id=pat_id,
        source="test",
        asset_class="stock",
        evidence_fingerprint="same-fingerprint",
        payload={"expected_evidence_value": 11.0},
    )
    fresh = emit_targeted_profitability_work(
        db,
        event_type=RECERT_RESCUE_REFRESH,
        scan_pattern_id=pat_id,
        source="test",
        asset_class="stock",
        evidence_fingerprint="new-fingerprint",
        payload={"expected_evidence_value": 12.0},
    )
    db.commit()

    assert repeat is None
    assert fresh is not None
