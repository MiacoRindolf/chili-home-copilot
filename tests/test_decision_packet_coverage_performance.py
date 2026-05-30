from __future__ import annotations

from app.services.trading import decision_packet_coverage


def test_top_recommended_fixes_uses_bounded_heap_selection(monkeypatch) -> None:
    fixes = [
        {"surface": "low", "missing": 1},
        {"surface": "high", "missing": 10},
    ]
    calls: list[tuple[int, list[tuple[tuple[int, str], int, dict]]]] = []

    def fake_nsmallest(limit: int, items):
        materialized = list(items)
        calls.append((limit, materialized))
        return [min(materialized)]

    monkeypatch.setattr(decision_packet_coverage.heapq, "nsmallest", fake_nsmallest)

    assert decision_packet_coverage._top_recommended_fixes(fixes, limit=1) == [
        {"surface": "high", "missing": 10}
    ]
    assert calls == [
        (
            1,
            [
                ((-1, "low"), 0, {"surface": "low", "missing": 1}),
                ((-10, "high"), 1, {"surface": "high", "missing": 10}),
            ],
        )
    ]


def test_top_recommended_fixes_preserves_priority_and_tie_order() -> None:
    fixes = [
        {"surface": "zeta", "missing": 10, "key": "first"},
        {"surface": "alpha", "missing": 5},
        {"surface": "zeta", "missing": 10, "key": "second"},
        {"surface": "beta", "missing": 10},
    ]

    assert decision_packet_coverage._top_recommended_fixes(fixes, limit=3) == [
        {"surface": "beta", "missing": 10},
        {"surface": "zeta", "missing": 10, "key": "first"},
        {"surface": "zeta", "missing": 10, "key": "second"},
    ]


def test_top_recommended_fixes_handles_empty_and_nonpositive_limits() -> None:
    assert decision_packet_coverage._top_recommended_fixes([], limit=5) == []
    assert decision_packet_coverage._top_recommended_fixes(
        [{"surface": "alerts", "missing": 1}],
        limit=0,
    ) == []
