from __future__ import annotations

from app.services.trading import signal_explainability


def test_top_importance_contribs_uses_bounded_heap_selection(monkeypatch) -> None:
    contribs = [
        {"indicator": "low", "importance": 0.1},
        {"indicator": "high", "importance": 0.9},
    ]
    calls: list[tuple[int, list[dict]]] = []

    def fake_nlargest(limit: int, items: list[dict], *, key):
        calls.append((limit, items))
        return [max(items, key=key)]

    monkeypatch.setattr(signal_explainability.heapq, "nlargest", fake_nlargest)

    assert signal_explainability._top_importance_contribs(contribs, limit=1) == [
        {"indicator": "high", "importance": 0.9}
    ]
    assert calls == [(1, contribs)]


def test_top_importance_contribs_keeps_descending_importance() -> None:
    contribs = [
        {"indicator": "mid", "importance": 0.5},
        {"indicator": "low", "importance": 0.1},
        {"indicator": "high", "importance": 0.9},
    ]

    assert signal_explainability._top_importance_contribs(contribs, limit=2) == [
        {"indicator": "high", "importance": 0.9},
        {"indicator": "mid", "importance": 0.5},
    ]


def test_top_importance_contribs_handles_empty_and_nonpositive_limits() -> None:
    assert signal_explainability._top_importance_contribs([], limit=10) == []
    assert signal_explainability._top_importance_contribs(
        [{"indicator": "a", "importance": 0.1}],
        limit=0,
    ) == []
