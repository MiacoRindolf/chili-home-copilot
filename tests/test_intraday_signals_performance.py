from app.services.trading.intraday_signals import _top_intraday_signals


def test_top_intraday_signals_uses_bounded_ranking_with_stable_ties() -> None:
    signals = [
        {"ticker": "AAA", "score": 1.0},
        {"ticker": "BBB", "score": 9.0},
        {"ticker": "CCC", "score": 9.0},
        {"ticker": "DDD", "score": 4.0},
    ]

    assert _top_intraday_signals(
        signals,
        3,
        key=lambda signal: signal["score"],
    ) == [
        {"ticker": "BBB", "score": 9.0},
        {"ticker": "CCC", "score": 9.0},
        {"ticker": "DDD", "score": 4.0},
    ]


def test_top_intraday_signals_empty_for_non_positive_limit() -> None:
    assert _top_intraday_signals(
        [{"ticker": "AAA", "score": 1.0}],
        0,
        key=lambda signal: signal["score"],
    ) == []
