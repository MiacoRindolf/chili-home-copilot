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
