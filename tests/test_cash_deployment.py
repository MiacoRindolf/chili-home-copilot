from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models.trading import (
    AutoTraderRun,
    BrainWorkEvent,
    BreakoutAlert,
    PaperTrade,
    ScanPattern,
    Trade,
    TradingPosition,
)
from app.services.trading.cash_deployment import (
    cash_deployment_rows,
    cash_deployment_summary,
    enqueue_cash_deployment_work,
)


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


def _closed_live(
    db,
    pat: ScanPattern,
    *,
    ticker: str,
    pnl: float,
    asset_kind: str = "equity",
    entry_price: float = 100.0,
    quantity: float = 1.0,
):
    db.add(
        Trade(
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

    first = enqueue_cash_deployment_work(db, window_days=7, limit=10, include_null_lineage=False)
    db.commit()
    second = enqueue_cash_deployment_work(db, window_days=7, limit=10, include_null_lineage=False)
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

    out = enqueue_cash_deployment_work(db, window_days=7, limit=10, include_null_lineage=False)
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

    out = enqueue_cash_deployment_work(db, window_days=7, limit=10, include_null_lineage=False)
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

    out = enqueue_cash_deployment_work(db, window_days=7, limit=10, include_null_lineage=False)
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

    out = enqueue_cash_deployment_work(db, window_days=7, limit=10, include_null_lineage=False)
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
