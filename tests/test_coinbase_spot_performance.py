from __future__ import annotations

from dataclasses import dataclass

from app.services.trading.venue import coinbase_spot


@dataclass(frozen=True)
class _Level:
    price: float


def test_top_book_levels_uses_bounded_heap_selection(monkeypatch) -> None:
    levels = [_Level(100.0), _Level(101.0)]
    calls: list[tuple[str, int, list[_Level]]] = []

    def fake_nlargest(limit: int, items: list[_Level], *, key):
        calls.append(("bid", limit, items))
        return [max(items, key=key)]

    def fake_nsmallest(limit: int, items: list[_Level], *, key):
        calls.append(("ask", limit, items))
        return [min(items, key=key)]

    monkeypatch.setattr(coinbase_spot.heapq, "nlargest", fake_nlargest)
    monkeypatch.setattr(coinbase_spot.heapq, "nsmallest", fake_nsmallest)

    assert coinbase_spot._top_book_levels(levels, limit=1, bid_side=True) == [_Level(101.0)]
    assert coinbase_spot._top_book_levels(levels, limit=1, bid_side=False) == [_Level(100.0)]
    assert calls == [("bid", 1, levels), ("ask", 1, levels)]


def test_top_book_levels_selects_best_bid_and_ask_prices() -> None:
    levels = [_Level(100.0), _Level(103.0), _Level(101.0), _Level(102.0)]

    assert [level.price for level in coinbase_spot._top_book_levels(levels, limit=2, bid_side=True)] == [
        103.0,
        102.0,
    ]
    assert [level.price for level in coinbase_spot._top_book_levels(levels, limit=2, bid_side=False)] == [
        100.0,
        101.0,
    ]


def test_top_book_levels_handles_empty_and_nonpositive_limits() -> None:
    assert coinbase_spot._top_book_levels([], limit=20, bid_side=True) == []
    assert coinbase_spot._top_book_levels([_Level(1.0)], limit=0, bid_side=False) == []
