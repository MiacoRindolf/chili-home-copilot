from __future__ import annotations

from app.services.trading.edge_reliability import _top_profitability_buckets


def test_top_profitability_buckets_matches_full_sort_with_stable_ties() -> None:
    rows = [
        {"ticker": "AAA", "total_pnl": 100.0, "closed_count": 4},
        {"ticker": "BBB", "total_pnl": 150.0, "closed_count": 2},
        {"ticker": "CCC", "total_pnl": 150.0, "closed_count": 2},
        {"ticker": "DDD", "total_pnl": 150.0, "closed_count": 5},
        {"ticker": "EEE", "total_pnl": 90.0, "closed_count": 100},
    ]

    assert _top_profitability_buckets(rows, 3) == [rows[3], rows[1], rows[2]]


def test_top_profitability_buckets_preserves_existing_minimum_limit_behavior() -> None:
    rows = [
        {"ticker": "AAA", "total_pnl": 100.0, "closed_count": 4},
        {"ticker": "BBB", "total_pnl": 150.0, "closed_count": 2},
    ]

    assert _top_profitability_buckets(rows, 0) == [rows[1]]
