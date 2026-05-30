from __future__ import annotations

from app.services.code_brain import agent


def test_top_relevant_files_uses_bounded_heap_selection(monkeypatch) -> None:
    files = [
        {"file": "low.py", "relevance": 1},
        {"file": "high.py", "relevance": 9},
    ]
    calls: list[tuple[int, list[dict]]] = []

    def fake_nlargest(limit: int, items: list[dict], *, key):
        calls.append((limit, items))
        return [max(items, key=key)]

    monkeypatch.setattr(agent.heapq, "nlargest", fake_nlargest)

    assert agent._top_relevant_files(files, limit=1) == [{"file": "high.py", "relevance": 9}]
    assert calls == [(1, files)]


def test_top_relevant_files_keeps_descending_relevance_and_tie_order() -> None:
    files = [
        {"file": "first-tie.py", "relevance": 7},
        {"file": "low.py", "relevance": 1},
        {"file": "winner.py", "relevance": 9},
        {"file": "second-tie.py", "relevance": 7},
    ]

    assert agent._top_relevant_files(files, limit=3) == [
        {"file": "winner.py", "relevance": 9},
        {"file": "first-tie.py", "relevance": 7},
        {"file": "second-tie.py", "relevance": 7},
    ]


def test_top_relevant_files_handles_empty_and_nonpositive_limits() -> None:
    assert agent._top_relevant_files([], limit=20) == []
    assert agent._top_relevant_files([{"file": "a.py", "relevance": 1}], limit=0) == []


def test_top_language_stats_uses_bounded_heap_selection(monkeypatch) -> None:
    stats = {"python": 100, "typescript": 20}
    calls: list[tuple[int, list[tuple[str, int]]]] = []

    def fake_nlargest(limit: int, items, *, key):
        materialized = list(items)
        calls.append((limit, materialized))
        return [max(materialized, key=key)]

    monkeypatch.setattr(agent.heapq, "nlargest", fake_nlargest)

    assert agent._top_language_stats(stats, limit=1) == [("python", 100)]
    assert calls == [(1, [("python", 100), ("typescript", 20)])]


def test_top_language_stats_keeps_descending_values() -> None:
    assert agent._top_language_stats(
        {"go": 15, "python": 100, "typescript": 20},
        limit=2,
    ) == [("python", 100), ("typescript", 20)]


def test_top_language_stats_handles_empty_and_nonpositive_limits() -> None:
    assert agent._top_language_stats({}, limit=5) == []
    assert agent._top_language_stats({"python": 1}, limit=0) == []
