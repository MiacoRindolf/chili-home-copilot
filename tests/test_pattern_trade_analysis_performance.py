from app.services.trading.pattern_trade_analysis import (
    _top_score_items,
    _top_ticker_return_items,
)


def test_top_ticker_return_items_uses_bounded_count_ranking_with_stable_ties() -> None:
    by_ticker = {
        "AAA": [1.0],
        "BBB": [1.0, 2.0, 3.0],
        "CCC": [1.0, 2.0, 3.0],
        "DDD": [1.0, 2.0],
    }

    assert _top_ticker_return_items(by_ticker, 3) == [
        ("BBB", [1.0, 2.0, 3.0]),
        ("CCC", [1.0, 2.0, 3.0]),
        ("DDD", [1.0, 2.0]),
    ]


def test_top_score_items_uses_bounded_score_ranking_with_stable_ties() -> None:
    scores = [
        ("AAA", 1.0),
        ("BBB", 5.0),
        ("CCC", 5.0),
        ("DDD", 3.0),
    ]

    assert _top_score_items(scores, 3) == [
        ("BBB", 5.0),
        ("CCC", 5.0),
        ("DDD", 3.0),
    ]


def test_top_helpers_empty_for_non_positive_limits() -> None:
    assert _top_ticker_return_items({"AAA": [1.0]}, 0) == []
    assert _top_score_items([("AAA", 1.0)], 0) == []
