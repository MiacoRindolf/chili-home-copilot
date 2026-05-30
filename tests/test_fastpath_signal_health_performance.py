from __future__ import annotations

from app.services.trading.fast_path.signal_health import (
    _attempt_health_sort_key,
    _signal_row_sort_key,
    _sort_attempt_health_rows,
    _sort_signal_rows,
    _top_attempt_health_rows,
    _top_signal_rows,
)


def _row(
    idx: int,
    *,
    pain_points: int,
    drift: float | None,
    favorable: float,
    attempts: int,
) -> dict:
    return {
        "ticker": f"T{idx:03d}-USD",
        "alert_type": "imbalance",
        "score_bucket": f"b{idx % 3}",
        "pain_points": [f"p{i}" for i in range(pain_points)],
        "filled_avg_side_mid_drift_bps": drift,
        "unfilled_favorable_rate": favorable,
        "attempts": attempts,
    }


def test_top_attempt_health_rows_matches_full_sort_with_stable_ties() -> None:
    rows = [
        _row(5, pain_points=1, drift=1.5, favorable=0.2, attempts=10),
        _row(2, pain_points=3, drift=None, favorable=0.1, attempts=20),
        _row(1, pain_points=3, drift=None, favorable=0.1, attempts=20),
        _row(4, pain_points=2, drift=-0.5, favorable=0.4, attempts=7),
        _row(3, pain_points=2, drift=-0.5, favorable=0.6, attempts=7),
    ]

    assert _top_attempt_health_rows(rows, limit=3) == sorted(
        rows,
        key=_attempt_health_sort_key,
    )[:3]


def test_top_attempt_health_rows_sorts_uncapped_rows() -> None:
    rows = [
        _row(2, pain_points=1, drift=2.0, favorable=0.1, attempts=3),
        _row(1, pain_points=2, drift=1.0, favorable=0.1, attempts=3),
    ]

    assert _top_attempt_health_rows(rows, limit=5) == _sort_attempt_health_rows(rows)


def test_top_attempt_health_rows_handles_non_positive_limit() -> None:
    rows = [_row(1, pain_points=1, drift=1.0, favorable=0.1, attempts=3)]

    assert _top_attempt_health_rows(rows, limit=0) == []


def _signal_row(
    idx: int,
    *,
    verdict: str,
    mean_net: float,
    rank: int | None,
) -> dict:
    return {
        "ticker": f"T{idx:03d}-USD",
        "alert_type": "imbalance",
        "score_bucket": f"b{idx % 3}",
        "verdict": verdict,
        "best_mean_net": {"mean_net_bps": mean_net},
        "rank": rank,
    }


def test_top_signal_rows_matches_full_sort_with_stable_ties() -> None:
    rows = [
        _signal_row(5, verdict="uncertain", mean_net=4.0, rank=2),
        _signal_row(2, verdict="negative_edge", mean_net=1.0, rank=1),
        _signal_row(1, verdict="negative_edge", mean_net=1.0, rank=1),
        _signal_row(4, verdict="below_cost", mean_net=8.0, rank=None),
        _signal_row(3, verdict="below_cost", mean_net=10.0, rank=3),
    ]

    assert _top_signal_rows(rows, limit=3) == sorted(
        rows,
        key=_signal_row_sort_key,
    )[:3]


def test_top_signal_rows_sorts_uncapped_rows() -> None:
    rows = [
        _signal_row(2, verdict="uncertain", mean_net=2.0, rank=2),
        _signal_row(1, verdict="negative_edge", mean_net=1.0, rank=1),
    ]

    assert _top_signal_rows(rows, limit=5) == _sort_signal_rows(rows)


def test_top_signal_rows_handles_non_positive_limit() -> None:
    rows = [_signal_row(1, verdict="uncertain", mean_net=1.0, rank=1)]

    assert _top_signal_rows(rows, limit=0) == []
