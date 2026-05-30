from __future__ import annotations

from app.services.trading import portfolio_optimizer


def test_first_sorted_tickers_keeps_bounded_deterministic_selection() -> None:
    tickers = {f"TK{i:04d}" for i in range(100, 0, -1)}

    assert portfolio_optimizer._first_sorted_tickers(tickers, limit=5) == [
        "TK0001",
        "TK0002",
        "TK0003",
        "TK0004",
        "TK0005",
    ]


def test_first_sorted_tickers_uses_bounded_heap_selection(monkeypatch) -> None:
    calls: list[tuple[int, set[str]]] = []

    def fake_nsmallest(limit: int, tickers: set[str]) -> list[str]:
        calls.append((limit, tickers))
        return ["AAPL"]

    monkeypatch.setattr(portfolio_optimizer.heapq, "nsmallest", fake_nsmallest)

    assert portfolio_optimizer._first_sorted_tickers({"MSFT", "AAPL"}, limit=1) == ["AAPL"]
    assert calls == [(1, {"MSFT", "AAPL"})]


def test_first_sorted_tickers_handles_empty_and_nonpositive_limits() -> None:
    assert portfolio_optimizer._first_sorted_tickers(set(), limit=20) == []
    assert portfolio_optimizer._first_sorted_tickers({"AAPL"}, limit=0) == []
