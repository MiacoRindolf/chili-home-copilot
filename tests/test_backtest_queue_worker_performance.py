from __future__ import annotations

from app.services.trading import backtest_queue_worker


def test_request_priority_tickers_mapping_uses_bounded_heap(monkeypatch) -> None:
    calls: list[int] = []

    def fake_nlargest(limit: int, items, *, key):
        calls.append(limit)
        return sorted(items, key=key, reverse=True)[:limit]

    monkeypatch.setattr(backtest_queue_worker.heapq, "nlargest", fake_nlargest)

    payload = {
        "priority_tickers": {
            "AAA": 10,
            "BBB": 30,
            "CCC": 30,
            "DDD": 5,
        }
    }

    assert backtest_queue_worker.request_priority_tickers_from_payload(
        payload,
        max_tickers=3,
    ) == ["BBB", "CCC", "AAA"]
    assert calls == [3]
