from __future__ import annotations

from datetime import datetime, timezone

from app.services.trading.momentum_neural.ross_feed_health import evaluate_feed_health
from app.services.trading.momentum_neural.ross_feed_health import market_clock


def test_feed_health_fails_when_massive_snapshot_is_stale() -> None:
    out = evaluate_feed_health(
        iqfeed={"age_s": 10.0, "rows": 10},
        massive={"age_s": 400.0, "rows": 10},
        fresh_live_rows=0,
        clock={"in_hot_window": True},
    )

    assert out.ok is False
    assert out.reason == "massive_snapshot_tape_stale"


def test_feed_health_warns_when_massive_snapshot_is_stale_outside_hot_window() -> None:
    out = evaluate_feed_health(
        iqfeed={"age_s": 7200.0, "rows": 10},
        massive={"age_s": 400.0, "rows": 10},
        fresh_live_rows=0,
        clock={"in_hot_window": False},
    )

    assert out.ok is True
    assert out.severity == "warn"
    assert out.reason == "massive_snapshot_tape_stale_outside_hot_window"


def test_feed_health_fails_when_iqfeed_stale_during_hot_live_window() -> None:
    out = evaluate_feed_health(
        iqfeed={"age_s": 300.0, "rows": 10},
        massive={"age_s": 10.0, "rows": 10},
        fresh_live_rows=2,
        clock={"in_hot_window": True},
        max_iqfeed_age_hot_s=60.0,
    )

    assert out.ok is False
    assert out.reason == "iqfeed_l1_stale_during_hot_live_window"


def test_feed_health_warns_but_passes_when_iqfeed_stale_outside_hot_window() -> None:
    out = evaluate_feed_health(
        iqfeed={"age_s": 7200.0, "rows": 10},
        massive={"age_s": 10.0, "rows": 10},
        fresh_live_rows=0,
        clock={"in_hot_window": False},
    )

    assert out.ok is True
    assert out.severity == "warn"
    assert out.reason == "iqfeed_l1_stale_outside_hot_window"


def test_feed_health_passes_when_hot_window_has_fresh_iqfeed() -> None:
    out = evaluate_feed_health(
        iqfeed={"age_s": 5.0, "rows": 10},
        massive={"age_s": 10.0, "rows": 10},
        fresh_live_rows=1,
        clock={"in_hot_window": True},
    )

    assert out.ok is True
    assert out.reason == "ross_lane_feed_runtime_ok"


def test_market_clock_treats_independence_day_observed_as_closed() -> None:
    out = market_clock(datetime(2026, 7, 3, 8, 15, tzinfo=timezone.utc))

    assert out["equity_market_closed_full_day"] is True
    assert out["in_hot_window"] is False


def test_market_clock_keeps_regular_premarket_hot_window_open() -> None:
    out = market_clock(datetime(2026, 7, 10, 8, 15, tzinfo=timezone.utc))

    assert out["equity_market_closed_full_day"] is False
    assert out["in_hot_window"] is True
