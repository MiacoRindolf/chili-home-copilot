from __future__ import annotations

from app.services.trading.fast_path.universe_rotator import (
    _PairCandidate,
    _adaptive_range_floor_bps,
    _dynamic_range_floor_component,
)


def _candidate_with_range(ticker: str, range_bps: float) -> _PairCandidate:
    return _PairCandidate(
        ticker=ticker,
        volume_24h_base=1_000_000.0,
        last_price=100.0,
        bid=99.95,
        ask=100.05,
        trades_24h=10_000,
        low_24h=100.0,
        high_24h=100.0 + (range_bps / 100.0),
        _bid_size_usd=20_000.0,
        _ask_size_usd=20_000.0,
    )


def test_dynamic_range_floor_component_matches_full_sort_slot() -> None:
    candidates = [
        _candidate_with_range("A-USD", 80.0),
        _candidate_with_range("B-USD", 120.0),
        _candidate_with_range("C-USD", 60.0),
        _candidate_with_range("D-USD", 100.0),
        _candidate_with_range("E-USD", 40.0),
    ]

    expected = sorted(
        (float(c.range_24h_bps) for c in candidates),
        reverse=True,
    )[2]

    assert _dynamic_range_floor_component(candidates, target_count=3) == expected


def test_dynamic_range_floor_component_preserves_short_pool_middle_value() -> None:
    candidates = [
        _candidate_with_range("A-USD", 80.0),
        _candidate_with_range("B-USD", 120.0),
        _candidate_with_range("C-USD", 60.0),
        _candidate_with_range("D-USD", 100.0),
    ]

    expected = sorted(
        (float(c.range_24h_bps) for c in candidates),
        reverse=True,
    )[len(candidates) // 2]

    assert _dynamic_range_floor_component(candidates, target_count=10) == expected


def test_dynamic_range_floor_component_ignores_non_positive_ranges() -> None:
    candidates = [
        _candidate_with_range("ZERO-USD", 0.0),
        _candidate_with_range("NEG-USD", -20.0),
    ]

    assert _dynamic_range_floor_component(candidates, target_count=3) is None


def test_adaptive_range_floor_uses_bounded_dynamic_component() -> None:
    candidates = [
        _candidate_with_range("A-USD", 80.0),
        _candidate_with_range("B-USD", 120.0),
        _candidate_with_range("C-USD", 60.0),
        _candidate_with_range("D-USD", 100.0),
    ]

    floor, dynamic = _adaptive_range_floor_bps(
        candidates,
        static_floor_bps=50.0,
        target_count=2,
        enabled=True,
    )

    assert dynamic == sorted((float(c.range_24h_bps) for c in candidates), reverse=True)[1]
    assert floor == dynamic
