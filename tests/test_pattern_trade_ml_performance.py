from app.services.trading.pattern_trade_ml import _top_feature_importances


def test_top_feature_importances_uses_bounded_ranking_with_stable_ties() -> None:
    importances = {
        "first": 1.0,
        "second": 9.0,
        "third": 9.0,
        "fourth": 4.0,
    }

    assert _top_feature_importances(importances, 3) == {
        "second": 9.0,
        "third": 9.0,
        "fourth": 4.0,
    }


def test_top_feature_importances_empty_for_non_positive_limit() -> None:
    assert _top_feature_importances({"first": 1.0}, 0) == {}
