from __future__ import annotations

from types import SimpleNamespace

from app.services.code_brain import lenses


class _NoReplaceString(str):
    def replace(self, *_args, **_kwargs):  # type: ignore[override]
        raise AssertionError("all-files lens should not normalize file paths")


def test_filter_file_path_items_wildcard_returns_before_matching() -> None:
    items = [SimpleNamespace(file_path=_NoReplaceString("app\\services\\x.py"))]

    assert lenses._filter_file_path_items(items, ["*"]) is items


def test_filter_file_path_items_preserves_path_and_basename_matching() -> None:
    items = [
        SimpleNamespace(file_path="app\\services\\trading.py"),
        SimpleNamespace(file_path="tests/test_trading.py"),
    ]

    assert lenses._filter_file_path_items(items, ["app/services/*"]) == [items[0]]
    assert lenses._filter_file_path_items(items, ["trading.py"]) == [items[0]]
    assert lenses._filter_file_path_items(items, ["tests/*"]) == [items[1]]


def test_top_confidence_insights_uses_bounded_heap(monkeypatch) -> None:
    def fail_sort(*_args, **_kwargs):
        raise AssertionError("lens top insights should not sort every insight")

    monkeypatch.setattr(lenses, "sorted", fail_sort, raising=False)
    insights = [
        SimpleNamespace(id=i, confidence=float(i))
        for i in range(100)
    ]

    top = lenses._top_confidence_insights(insights, limit=5)

    assert [item.id for item in top] == [99, 98, 97, 96, 95]


def test_top_confidence_insights_handles_empty_and_non_positive_limits() -> None:
    assert lenses._top_confidence_insights([], limit=10) == []
    assert lenses._top_confidence_insights(
        [SimpleNamespace(id=1, confidence=0.8)],
        limit=0,
    ) == []
