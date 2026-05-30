from __future__ import annotations

from types import SimpleNamespace

from app.services.code_brain import lenses


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
