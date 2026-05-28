from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models.trading import AutoTraderRun, BreakoutAlert, PaperTrade, ScanPattern, Trade
from app.services.trading.cash_deployment import cash_deployment_rows, cash_deployment_summary


def _pattern(db, *, name: str, lifecycle: str = "promoted", recert: bool = False, recert_reason: str | None = None):
    pat = ScanPattern(
        name=name,
        rules_json={},
        origin="test",
        asset_class="stock",
        timeframe="1d",
        active=True,
        lifecycle_stage=lifecycle,
        recert_required=recert,
        recert_reason=recert_reason,
    )
    db.add(pat)
    db.flush()
    return pat


def _alert(db, pat: ScanPattern, *, ticker: str):
    alert = BreakoutAlert(
        scan_pattern_id=pat.id,
        ticker=ticker,
        asset_type="stock",
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
