from app.services.trading.momentum_neural.opportunities import _top_opportunity_rows


def test_top_opportunity_rows_uses_bounded_ranking_with_stable_ties() -> None:
    rows = [
        {"symbol": "AAA", "score": 1.0},
        {"symbol": "BBB", "score": 9.0},
        {"symbol": "CCC", "score": 9.0},
        {"symbol": "DDD", "score": 4.0},
    ]

    assert _top_opportunity_rows(rows, 3, key=lambda row: row["score"]) == [
        {"symbol": "BBB", "score": 9.0},
        {"symbol": "CCC", "score": 9.0},
        {"symbol": "DDD", "score": 4.0},
    ]


def test_top_opportunity_rows_empty_for_non_positive_limit() -> None:
    assert _top_opportunity_rows(
        [{"symbol": "AAA", "score": 1.0}],
        0,
        key=lambda row: row["score"],
    ) == []
