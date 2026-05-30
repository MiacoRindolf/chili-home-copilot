from __future__ import annotations

from app.services.trading import prescreener


def test_lowest_scored_tickers_matches_full_sort_with_stable_ties() -> None:
    scored = [
        ("AAA", 0.30),
        ("BBB", 0.10),
        ("CCC", 0.10),
        ("DDD", 0.20),
        ("EEE", 0.05),
    ]

    assert prescreener._lowest_scored_tickers(scored, 3) == ["EEE", "BBB", "CCC"]


def test_highest_scored_tickers_matches_full_sort_with_stable_ties() -> None:
    scored = [
        ("AAA", 0.30),
        ("BBB", 0.90),
        ("CCC", 0.90),
        ("DDD", 0.20),
        ("EEE", 0.95),
    ]

    assert prescreener._highest_scored_tickers(scored, 3) == ["EEE", "BBB", "CCC"]


def test_scored_ticker_helpers_handle_non_positive_limits() -> None:
    scored = [("AAA", 0.30), ("BBB", 0.10)]

    assert prescreener._lowest_scored_tickers(scored, 0) == []
    assert prescreener._highest_scored_tickers(scored, -1) == []
