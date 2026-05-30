from app.services.trading.opportunity_board import _tier_strength_key, _top_tier_rows


def test_top_tier_rows_matches_full_sort_with_stable_ties() -> None:
    rows = [
        {"ticker": "A", "composite": None},
        {"ticker": "B", "composite": 0.8},
        {"ticker": "C", "composite": 0.8},
        {"ticker": "D", "composite": 0.9},
        {"ticker": "E", "composite": None},
    ]

    selected, more = _top_tier_rows(rows, 3)

    assert selected == sorted(rows, key=_tier_strength_key)[:3]
    assert more is True


def test_top_tier_rows_sorts_uncapped_rows() -> None:
    rows = [
        {"ticker": "A", "composite": None},
        {"ticker": "B", "composite": 0.7},
    ]

    selected, more = _top_tier_rows(rows, 5)

    assert selected == sorted(rows, key=_tier_strength_key)
    assert more is False


def test_top_tier_rows_handles_non_positive_limits() -> None:
    selected, more = _top_tier_rows([{"ticker": "A", "composite": 0.1}], 0)

    assert selected == []
    assert more is True
