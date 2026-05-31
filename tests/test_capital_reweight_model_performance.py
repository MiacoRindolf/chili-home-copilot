from __future__ import annotations

from app.services.trading import capital_reweight_model as model


class _NoSortList(list):
    def __iter__(self):  # type: ignore[override]
        raise AssertionError("single-value percentile should not sort")


def test_percentile_single_value_skips_sorting() -> None:
    assert model._percentile(_NoSortList([42.0]), 90.0) == 42.0


def test_percentile_preserves_empty_and_multi_value_behavior() -> None:
    assert model._percentile([], 90.0) == 0.0
    assert model._percentile([10.0, 40.0, 20.0, 30.0], 90.0) == 40.0
