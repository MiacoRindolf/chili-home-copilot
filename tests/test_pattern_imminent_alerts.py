"""Tests for ScanPattern imminent breakout alert helpers."""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from app.models.trading import AlertHistory
from app.services.trading.alerts import PATTERN_BREAKOUT_IMMINENT
from app.services.trading.pattern_imminent_alerts import (
    _cooldown_active,
    estimate_breakout_eta_hours,
    evaluate_imminent_readiness,
    format_eta_range,
    run_pattern_imminent_scan,
    timeframe_to_hours_per_step,
    us_stock_session_open,
)


def test_timeframe_to_hours_per_step_defaults() -> None:
    assert timeframe_to_hours_per_step("1h") == 1.0
    assert timeframe_to_hours_per_step("15m") == 0.25
    assert timeframe_to_hours_per_step("unknown") == 6.5


def test_estimate_breakout_eta_hours_clamped() -> None:
    lo, hi = estimate_breakout_eta_hours(0.9, "1h", k=1.5, max_eta_hours=4.0)
    assert 5 / 60 <= lo <= hi <= 4.0
    lo2, hi2 = estimate_breakout_eta_hours(0.2, "1d", k=1.5, max_eta_hours=4.0)
    assert lo2 <= hi2 <= 4.0


def test_format_eta_range_minutes() -> None:
    s = format_eta_range(0.08, 0.2)
    assert "min" in s


def test_evaluate_imminent_readiness_all_pass_excluded_by_caller() -> None:
    """When every evaluable condition passes strictly, readiness is high; caller skips all_pass."""
    conditions = [
        {"indicator": "rsi_14", "op": ">", "value": 50},
    ]
    flat = {"rsi_14": 60.0, "price": 100.0}
    readiness, all_pass, ratio = evaluate_imminent_readiness(
        conditions, flat, evaluable_ratio_floor=0.5,
    )
    assert readiness is not None
    assert readiness > 0
    assert all_pass is True
    assert ratio == 1.0


def test_evaluate_imminent_readiness_partial() -> None:
    conditions = [
        {"indicator": "rsi_14", "op": ">", "value": 40},
        {"indicator": "rsi_14", "op": ">", "value": 95},
    ]
    flat = {"rsi_14": 60.0, "price": 100.0}
    readiness, all_pass, ratio = evaluate_imminent_readiness(
        conditions, flat, evaluable_ratio_floor=0.5,
    )
    assert readiness is not None
    assert all_pass is False
    assert 0.0 < readiness < 1.0


def test_evaluate_imminent_readiness_low_evaluable_ratio() -> None:
    conditions = [
        {"indicator": "rsi_14", "op": ">", "value": 50},
        {"indicator": "bb_squeeze", "op": "==", "value": True},
    ]
    flat = {"rsi_14": 60.0, "price": 100.0}
    readiness, _all_pass, ratio = evaluate_imminent_readiness(
        conditions, flat, evaluable_ratio_floor=0.99,
    )
    assert readiness is None
    assert ratio < 0.99


def test_evaluate_imminent_two_evaluable_low_ratio_ok() -> None:
    """Two evaluable clauses suffice even when coverage ratio is below floor."""
    conditions = [
        {"indicator": "rsi_14", "op": ">", "value": 50},
        {"indicator": "adx", "op": ">", "value": 20},
        {"indicator": "bb_squeeze", "op": "==", "value": True},
        {"indicator": "vwap_reclaim", "op": "==", "value": True},
    ]
    flat = {"rsi_14": 55.0, "adx": 18.0, "price": 100.0}
    readiness, all_pass, ratio = evaluate_imminent_readiness(
        conditions, flat, evaluable_ratio_floor=0.5,
    )
    assert readiness is not None
    assert ratio == 0.5
    assert all_pass is False


def test_us_stock_session_open_saturday_utc() -> None:
    sat = datetime(2026, 3, 21, 14, 0, 0, tzinfo=timezone.utc)
    assert us_stock_session_open(sat) is False


def test_cooldown_ignores_failed_imminent_delivery(db) -> None:
    row = AlertHistory(
        user_id=1,
        alert_type=PATTERN_BREAKOUT_IMMINENT,
        ticker="SPY",
        message="failed delivery",
        scan_pattern_id=52,
        sent_via="sms_failed",
        success=False,
    )
    db.add(row)
    db.commit()

    assert _cooldown_active(db, 1, "SPY", 52, 3.0) is False

    row.success = True
    db.commit()

    assert _cooldown_active(db, 1, "SPY", 52, 3.0) is True


def test_run_pattern_imminent_scan_does_not_count_failed_delivery_as_sent(
    db,
    monkeypatch,
) -> None:
    inserted: list[tuple[str, int]] = []

    pattern = SimpleNamespace(
        id=52,
        name="Promoted VCP",
        description="Test imminent pattern",
    )
    candidate = {
        "pattern": pattern,
        "ticker": "AAOI",
        "eta_lo": 0.5,
        "eta_hi": 1.0,
        "score": {
            "price": 100.0,
            "entry_price": 101.0,
            "stop_loss": 97.5,
            "take_profit": 110.0,
            "signals": ["Tight range", "Volume building"],
        },
        "trade_type": "swing",
        "duration_estimate": "2-5 days",
        "hold_label": "2-5 days",
        "composite": 0.71,
        "readiness": 0.82,
        "flat": {"price": 100.0},
        "score_breakdown": {"quality": 0.7},
        "coverage_ratio": 0.75,
    }

    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.gather_imminent_candidate_rows",
        lambda *args, **kwargs: ([candidate], {"patterns_active": 1, "tickers_scored": 1}),
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._cooldown_active",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts.dispatch_alert",
        lambda *args, **kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.trading.pattern_imminent_alerts._insert_imminent_breakout_alert",
        lambda db, user_id, pat, ticker, *args, **kwargs: inserted.append((ticker, pat.id)),
    )

    result = run_pattern_imminent_scan(db, user_id=1)

    assert result["candidates"] == 1
    assert result["alerts_sent"] == 0
    assert result["delivery_failed"] == 1
    assert inserted == []
