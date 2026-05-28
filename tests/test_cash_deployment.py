from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models.trading import AutoTraderRun, BreakoutAlert, PaperTrade, ScanPattern, Trade
from app.services.trading.cash_deployment import cash_deployment_rows, cash_deployment_summary
from app.services.trading.edge_reliability import persist_edge_reliability_snapshot


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
                signal_json={"paper_shadow": True, "asset_type": alert.asset_type},
            )
        )


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

    assert by_pattern[negative.id]["cash_deployment_category"] == "negative_ev"
    assert by_pattern[negative.id]["cash_deployment_rank"] is None

    summary = cash_deployment_summary(rows)
    assert summary["live_deployable"] == 1
    assert summary["positive_ev_recert"] >= 1
    assert summary["positive_ev_shadow"] >= 1
    assert summary["positive_ev_execution_blocked"] >= 1
    assert summary["negative_ev"] >= 1


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


def test_cash_deployment_stale_edge_evidence_needs_refresh(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 1)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.5)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_data_age_hours", 1.0)

    old = datetime.utcnow() - timedelta(hours=2)
    pat = _pattern(db, name="cash stale evidence")
    alert = _alert(db, pat, ticker="STALE")
    alert.alerted_at = old
    run = _run(db, pat, alert, expected=2.0)
    run.created_at = old
    _closed_paper(db, pat, alert, count=2, pnl_pct=4.0)
    db.commit()

    rows = cash_deployment_rows(db, pattern_ids=[pat.id], window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)

    assert row["cash_deployment_category"] == "needs_calibration"
    assert row["freshness_blocker"] == "stale_data"
    assert row["recommended_work_event"] == "edge_reliability_refresh"
    assert row["max_safe_notional"] == 0.0
    assert row["live_deployable"] is False


def test_cash_deployment_prefers_materialized_asset_slices(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_crypto_cost_pct", 0.25)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 1)
    monkeypatch.setattr(settings, "chili_cash_deployment_max_brier_score", 0.5)

    pat = _pattern(db, name="all asset slice cash", asset_class="all")
    stock_alert = _alert(db, pat, ticker="SLICE", asset_type="stock")
    crypto_alert = _alert(db, pat, ticker="SLICE-USD", asset_type="crypto")
    _run(db, pat, stock_alert, expected=2.0)
    _run(db, pat, crypto_alert, expected=-2.0, reason="non_positive_expected_edge")
    _closed_paper(db, pat, stock_alert, count=5, pnl_pct=4.0)
    _closed_paper(db, pat, crypto_alert, count=5, pnl_pct=-2.0)
    db.commit()

    snapshot = persist_edge_reliability_snapshot(
        db,
        pat.id,
        window_days=7,
        source="test_materialized_slice",
    )
    db.commit()

    assert snapshot["asset_slice_count"] == 2
    rows = cash_deployment_rows(db, pattern_ids=[pat.id], window_days=7, limit=10)
    by_asset = {row["asset_class"]: row for row in rows}

    assert set(by_asset) == {"stock", "crypto"}
    assert by_asset["stock"]["snapshot_granularity"] == "asset_slice"
    assert by_asset["stock"]["calibrated_ev_after_cost_pct"] > 0
    assert by_asset["stock"]["cash_deployment_category"] == "live_deployable"
    assert by_asset["crypto"]["snapshot_granularity"] == "asset_slice"
    assert by_asset["crypto"]["calibrated_ev_after_cost_pct"] < 0
    assert by_asset["crypto"]["cash_deployment_category"] == "negative_ev"
    assert by_asset["crypto"]["cash_deployment_rank"] is None


def test_repeated_broker_rejects_degrade_venue_readiness(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_autotrader_live_enabled", True)
    monkeypatch.setattr(settings, "chili_cash_deployment_equity_cost_pct", 0.05)
    monkeypatch.setattr(settings, "chili_cash_deployment_min_closed_evidence", 1)
    monkeypatch.setattr(settings, "chili_cash_deployment_venue_degrade_min_rejects", 2)
    monkeypatch.setattr(settings, "chili_cash_deployment_venue_degrade_reject_rate", 0.1)

    pat = _pattern(db, name="cash repeated reject")
    alert = _alert(db, pat, ticker="RJCT2")
    _run(db, pat, alert, expected=2.0, reason="broker:Robinhood returned no order_id")
    _run(db, pat, alert, expected=2.1, reason="broker:Robinhood returned no order_id")
    _closed_paper(db, pat, alert, count=1, pnl_pct=4.0)
    db.commit()

    rows = cash_deployment_rows(db, pattern_ids=[pat.id], window_days=7, limit=10)
    row = next(x for x in rows if x["scan_pattern_id"] == pat.id)

    assert row["cash_deployment_category"] == "positive_ev_execution_blocked"
    assert row["execution_blocker"] == "venue_degraded_repeated_broker_rejects"
    assert "degraded_repeated_rejects" in row["venue_readiness"]
    assert row["live_deployable"] is False
