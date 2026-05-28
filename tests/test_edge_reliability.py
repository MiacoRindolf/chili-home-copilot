from __future__ import annotations

from datetime import datetime
import pytest

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
    handle_recert_rescue_refresh,
)
from app.services.trading.brain_work.ledger import enqueue_work_event
from app.services.trading.edge_reliability import (
    EDGE_RELIABILITY_REFRESH,
    EDGE_RELIABILITY_SNAPSHOT,
    RECERT_RESCUE_REFRESH,
    RECERT_RESCUE_DIAGNOSTIC,
    compute_pattern_edge_reliability,
    edge_supply_rows,
    emit_edge_reliability_refresh_requested,
)


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
    alert = _alert(db, pat)
    _run(db, pat, alert, expected=1.25)
    db.commit()

    first = emit_edge_reliability_refresh_requested(
        db,
        pat.id,
        source="test",
        window_days=7,
        evidence_fingerprint="same",
    )
    second = emit_edge_reliability_refresh_requested(
        db,
        pat.id,
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
    assert snapshot.payload["scan_pattern_id"] == pat.id
    assert snapshot.payload["expected_ev_pct"] == pytest.approx(1.25)
