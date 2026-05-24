from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.services.trading.fast_path.scanner import (
    BPS_PER_UNIT,
    BOOK_PRESSURE_RECLAIM_LONG,
    MomentumScanner,
)


def _scanner() -> MomentumScanner:
    return MomentumScanner(
        emit_short_alerts=False,
        imbalance_long_threshold=0.99,
        book_pressure_window=3,
        book_pressure_min_avg_imbalance=0.65,
        book_pressure_min_microprice_bps=0.25,
        book_pressure_max_spread_bps=3.0,
        book_pressure_min_mid_move_bps=0.25,
        book_pressure_cooldown_s=0.0,
    )


def _book(
    *,
    best_bid: float,
    best_ask: float,
    bid_size: float = 9.0,
    ask_size: float = 1.0,
    imbalance: float = 0.90,
    at: datetime | None = None,
) -> dict:
    mid = (best_bid + best_ask) / 2.0
    return {
        "ticker": "TEST-USD",
        "snapshot_at": at or datetime.now(timezone.utc).replace(tzinfo=None),
        "bid_levels": [(best_bid, bid_size)],
        "ask_levels": [(best_ask, ask_size)],
        "bid_total_size": bid_size,
        "ask_total_size": ask_size,
        "imbalance": imbalance,
        "spread_bps": ((best_ask - best_bid) / mid) * BPS_PER_UNIT,
    }


def _pressure_alerts(alerts: list[dict]) -> list[dict]:
    return [a for a in alerts if a["alert_type"] == BOOK_PRESSURE_RECLAIM_LONG]


def test_book_pressure_requires_full_window_before_emitting():
    scanner = _scanner()
    start = datetime(2026, 5, 23, 18, 0, 0)

    first = scanner.on_book_emit(
        "TEST-USD",
        _book(best_bid=100.00, best_ask=100.02, at=start),
        now_monotonic=0.0,
    )
    second = scanner.on_book_emit(
        "TEST-USD",
        _book(best_bid=100.02, best_ask=100.04, at=start + timedelta(seconds=1)),
        now_monotonic=1.0,
    )

    assert _pressure_alerts(first) == []
    assert _pressure_alerts(second) == []
    assert scanner.stats()["suppressed_book_pressure_warmup"] == 2


def test_book_pressure_emits_after_persistent_pressure_and_mid_reclaim():
    scanner = _scanner()
    start = datetime(2026, 5, 23, 18, 0, 0)
    books = [
        _book(best_bid=100.00, best_ask=100.02, at=start),
        _book(best_bid=100.005, best_ask=100.025, at=start + timedelta(seconds=1)),
        _book(best_bid=100.010, best_ask=100.030, at=start + timedelta(seconds=2)),
    ]

    alerts: list[dict] = []
    for i, book in enumerate(books):
        alerts = scanner.on_book_emit("TEST-USD", book, now_monotonic=float(i))

    pressure = _pressure_alerts(alerts)
    assert len(pressure) == 1
    alert = pressure[0]
    assert alert["signal_score"] > 0
    assert alert["features"]["avg_imbalance"] == 0.90
    assert alert["features"]["avg_microprice_edge_bps"] >= 0.25
    assert alert["features"]["mid_move_bps"] >= 0.25
    assert alert["features"]["best_bid_move_bps"] >= 0.25
    assert alert["features"]["book_pressure_window"] == 3
    assert scanner.stats()["fired_book_pressure_reclaim_long"] == 1


def test_book_pressure_rejects_bid_wall_when_mid_deteriorates():
    scanner = _scanner()
    start = datetime(2026, 5, 23, 18, 0, 0)
    books = [
        _book(best_bid=100.04, best_ask=100.06, at=start),
        _book(best_bid=100.02, best_ask=100.04, at=start + timedelta(seconds=1)),
        _book(best_bid=100.00, best_ask=100.02, at=start + timedelta(seconds=2)),
    ]

    alerts: list[dict] = []
    for i, book in enumerate(books):
        alerts = scanner.on_book_emit("TEST-USD", book, now_monotonic=float(i))

    assert _pressure_alerts(alerts) == []
    stats = scanner.stats()
    assert stats["fired_book_pressure_reclaim_long"] == 0
    assert stats["suppressed_book_pressure_condition"] == 1
    assert stats["suppressed_book_pressure_reasons"]["mid_move_below"] == 1
    assert stats["suppressed_book_pressure_reasons"]["best_bid_move_below"] == 1


def test_book_pressure_rejects_when_current_microprice_pressure_fades():
    scanner = _scanner()
    start = datetime(2026, 5, 23, 18, 0, 0)
    books = [
        _book(best_bid=100.00, best_ask=100.02, at=start),
        _book(best_bid=100.005, best_ask=100.025, at=start + timedelta(seconds=1)),
        _book(
            best_bid=100.010,
            best_ask=100.030,
            bid_size=5.0,
            ask_size=5.0,
            at=start + timedelta(seconds=2),
        ),
    ]

    alerts: list[dict] = []
    for i, book in enumerate(books):
        alerts = scanner.on_book_emit("TEST-USD", book, now_monotonic=float(i))

    assert _pressure_alerts(alerts) == []
    stats = scanner.stats()
    assert stats["suppressed_book_pressure_condition"] == 1
    assert stats["suppressed_book_pressure_reasons"][
        "current_microprice_below"
    ] == 1


def test_book_pressure_rejects_when_mid_has_already_run_too_far():
    scanner = _scanner()
    start = datetime(2026, 5, 23, 18, 0, 0)
    books = [
        _book(best_bid=100.00, best_ask=100.02, at=start),
        _book(best_bid=100.05, best_ask=100.07, at=start + timedelta(seconds=1)),
        _book(best_bid=100.10, best_ask=100.12, at=start + timedelta(seconds=2)),
    ]

    alerts: list[dict] = []
    for i, book in enumerate(books):
        alerts = scanner.on_book_emit("TEST-USD", book, now_monotonic=float(i))

    assert _pressure_alerts(alerts) == []
    stats = scanner.stats()
    assert stats["suppressed_book_pressure_condition"] == 1
    assert stats["suppressed_book_pressure_reasons"]["mid_move_overextended"] == 1
    assert stats["suppressed_book_pressure_reasons"][
        "best_bid_move_overextended"
    ] == 1


def test_book_pressure_rejects_dust_touch_liquidity():
    scanner = _scanner()
    start = datetime(2026, 5, 23, 18, 0, 0)
    books = [
        _book(best_bid=100.00, best_ask=100.02, at=start),
        _book(best_bid=100.005, best_ask=100.025, at=start + timedelta(seconds=1)),
        _book(
            best_bid=100.010,
            best_ask=100.030,
            bid_size=9.0,
            ask_size=0.01,
            at=start + timedelta(seconds=2),
        ),
    ]

    alerts: list[dict] = []
    for i, book in enumerate(books):
        alerts = scanner.on_book_emit("TEST-USD", book, now_monotonic=float(i))

    assert _pressure_alerts(alerts) == []
    stats = scanner.stats()
    assert stats["suppressed_book_pressure_condition"] == 1
    assert stats["suppressed_book_pressure_reasons"]["min_touch_below"] == 1


def test_book_pressure_uses_depth_weighted_microprice_not_dust_top_level():
    scanner = _scanner()
    start = datetime(2026, 5, 23, 18, 0, 0)
    books = [
        _book(best_bid=100.00, best_ask=100.02, at=start),
        _book(best_bid=100.005, best_ask=100.025, at=start + timedelta(seconds=1)),
        {
            **_book(
                best_bid=100.010,
                best_ask=100.030,
                bid_size=9.0,
                ask_size=1.0,
                at=start + timedelta(seconds=2),
            ),
            "bid_total_size": 5.0,
            "ask_total_size": 5.0,
        },
    ]

    alerts: list[dict] = []
    for i, book in enumerate(books):
        alerts = scanner.on_book_emit("TEST-USD", book, now_monotonic=float(i))

    assert _pressure_alerts(alerts) == []
    stats = scanner.stats()
    assert stats["suppressed_book_pressure_condition"] == 1
    assert stats["suppressed_book_pressure_reasons"][
        "current_microprice_below"
    ] == 1


def test_book_pressure_stats_break_down_multiple_condition_reasons():
    scanner = _scanner()
    start = datetime(2026, 5, 23, 18, 0, 0)
    books = [
        _book(
            best_bid=100.00,
            best_ask=100.02,
            bid_size=1.0,
            ask_size=9.0,
            imbalance=0.10,
            at=start,
        ),
        _book(
            best_bid=100.00,
            best_ask=100.02,
            bid_size=1.0,
            ask_size=9.0,
            imbalance=0.10,
            at=start + timedelta(seconds=1),
        ),
        _book(
            best_bid=100.00,
            best_ask=100.02,
            bid_size=1.0,
            ask_size=9.0,
            imbalance=0.10,
            at=start + timedelta(seconds=2),
        ),
    ]

    alerts: list[dict] = []
    for i, book in enumerate(books):
        alerts = scanner.on_book_emit("TEST-USD", book, now_monotonic=float(i))

    assert _pressure_alerts(alerts) == []
    reasons = scanner.stats()["suppressed_book_pressure_reasons"]
    assert reasons["avg_imbalance_below"] == 1
    assert reasons["avg_microprice_below"] == 1
    assert reasons["current_microprice_below"] == 1
    assert reasons["mid_move_below"] == 1
    assert reasons["best_bid_move_below"] == 1


def test_book_pressure_knobs_surface_in_stats_config():
    scanner = MomentumScanner(
        book_pressure_enabled=False,
        book_pressure_window=7,
        book_pressure_min_avg_imbalance=0.7,
        book_pressure_min_microprice_bps=0.5,
        book_pressure_max_spread_bps=2.0,
        book_pressure_min_mid_move_bps=0.4,
        book_pressure_cooldown_s=19.0,
        book_pressure_min_touch_notional_usd=12.5,
    )

    cfg = scanner.stats()["config"]
    assert cfg["book_pressure_enabled"] is False
    assert cfg["book_pressure_window"] == 7
    assert cfg["book_pressure_min_avg_imbalance"] == 0.7
    assert cfg["book_pressure_min_microprice_bps"] == 0.5
    assert cfg["book_pressure_max_spread_bps"] == 2.0
    assert cfg["book_pressure_min_mid_move_bps"] == 0.4
    assert cfg["book_pressure_cooldown_s"] == 19.0
    assert cfg["book_pressure_min_touch_notional_usd"] == 12.5
