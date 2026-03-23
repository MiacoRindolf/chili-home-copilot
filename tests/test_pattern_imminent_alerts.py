"""Tests for ScanPattern imminent breakout alert helpers."""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.trading.pattern_imminent_alerts import (
    estimate_breakout_eta_hours,
    evaluate_imminent_readiness,
    format_eta_range,
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
