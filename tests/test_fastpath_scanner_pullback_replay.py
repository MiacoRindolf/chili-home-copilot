"""Regression tests for stale pullback replay suppression."""
from __future__ import annotations

from datetime import datetime, timedelta
from unittest.mock import patch

from app.services.trading.fast_path.gates import ALERT_RECENCY_MAX_AGE_S
from app.services.trading.fast_path.scanner import MomentumScanner


def _bar_at(ts: datetime, *, volume: float, close: float = 100.0) -> dict:
    return {
        "ticker": "TEST-USD",
        "bar_close_at": ts,
        "open": 100.0,
        "close": close,
        "high": max(100.0, close),
        "low": min(100.0, close),
        "volume": volume,
    }


def _book_at(ts: datetime) -> dict:
    return {
        "ticker": "TEST-USD",
        "snapshot_at": ts,
        "bid_levels": [[100.0, 1.0]],
        "ask_levels": [[100.05, 1.0]],
        "imbalance": 0.5,
        "spread_bps": 1.0,
    }


def _schedule(s: MomentumScanner, *, original: datetime, delay_s: float) -> None:
    with patch(
        "app.services.trading.fast_path.scanner.get_pullback_delay_s",
        return_value=delay_s,
    ):
        s._schedule_pullback_deferred(
            ticker="TEST-USD",
            original_fired_at=original,
            signal_score=1.0,
            original_features={
                "volume": 10.0,
                "mean_vol": 1.0,
                "vol_ratio": 10.0,
                "close": 100.0,
                "ret_pct": 0.01,
            },
        )


def test_stale_pullback_deferred_emit_is_dropped() -> None:
    scanner = MomentumScanner()
    original = datetime(2026, 5, 23, 12, 0, 0)
    delay_s = 30.0
    _schedule(scanner, original=original, delay_s=delay_s)

    now = original + timedelta(
        seconds=delay_s + ALERT_RECENCY_MAX_AGE_S + 1.0,
    )
    alerts = scanner._drain_pullback_due(
        now_wall=now,
        triggering_ticker="TEST-USD",
        triggering_book=_book_at(now),
    )

    assert alerts == []
    assert scanner.deferred_dropped_stale == 1
    assert scanner.stats()["pullback_deferred_dropped_stale"] == 1


def test_due_pullback_inside_recency_window_still_emits() -> None:
    scanner = MomentumScanner()
    original = datetime(2026, 5, 23, 12, 0, 0)
    delay_s = 30.0
    _schedule(scanner, original=original, delay_s=delay_s)

    now = original + timedelta(seconds=delay_s + ALERT_RECENCY_MAX_AGE_S)
    alerts = scanner._drain_pullback_due(
        now_wall=now,
        triggering_ticker="TEST-USD",
        triggering_book=_book_at(now),
    )

    assert len(alerts) == 1
    assert alerts[0]["features"]["best_ask"] == 100.05
    assert scanner.deferred_dropped_stale == 0


def test_pullback_deferred_cap_drops_new_arrivals() -> None:
    scanner = MomentumScanner(max_pending_deferred=1)
    original = datetime(2026, 5, 23, 12, 0, 0)

    _schedule(scanner, original=original, delay_s=30.0)
    _schedule(scanner, original=original + timedelta(seconds=1), delay_s=30.0)

    stats = scanner.stats()
    assert stats["pullback_pending_heap"] == 1
    assert scanner.deferred_scheduled == 1
    assert scanner.deferred_dropped_overcap == 1
    assert stats["pullback_deferred_dropped_overcap"] == 1
    assert stats["config"]["max_pending_deferred"] == 1


def test_pullback_deferred_cap_rejects_bad_constructor_value() -> None:
    scanner = MomentumScanner(max_pending_deferred="bad")
    assert scanner.stats()["config"]["max_pending_deferred"] == 1000


def test_bar_close_emit_disabled_warms_without_alert_or_deferred() -> None:
    scanner = MomentumScanner()
    start = datetime(2026, 5, 23, 12, 0, 0)
    for i in range(20):
        scanner.on_bar_close(_bar_at(start + timedelta(minutes=i), volume=1.0))

    stale_spike = _bar_at(
        start + timedelta(minutes=20),
        volume=3.0,
        close=101.0,
    )
    alerts = scanner.on_bar_close(stale_spike, emit_alerts=False)

    assert alerts == []
    assert scanner.fired_volume_breakout_long == 0
    assert scanner.deferred_scheduled == 0
    assert scanner.stats()["suppressed_bar_close_alerts_disabled"] == 1

    fresh_spike = _bar_at(
        start + timedelta(minutes=21),
        volume=3.0,
        close=101.0,
    )
    alerts = scanner.on_bar_close(fresh_spike, emit_alerts=True)

    assert [a["alert_type"] for a in alerts] == ["volume_breakout_long"]
    assert scanner.fired_volume_breakout_long == 1
    assert scanner.deferred_scheduled == 1
