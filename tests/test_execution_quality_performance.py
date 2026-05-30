from __future__ import annotations

from app.services.trading import execution_quality


def test_top_ticker_stats_uses_bounded_heap_for_large_inputs(monkeypatch) -> None:
    def fail_sort(*_args, **_kwargs):
        raise AssertionError("top ticker stats should not sort every ticker")

    monkeypatch.setattr(execution_quality, "sorted", fail_sort, raising=False)
    ticker_stats = {
        f"T{i:03d}": {
            "avg_slippage_pct": float(i),
            "max_slippage_pct": float(i),
            "trades": 3,
        }
        for i in range(100)
    }

    top = execution_quality._top_ticker_stats(ticker_stats, limit=20)

    assert list(top) == [f"T{i:03d}" for i in range(99, 79, -1)]
    assert all(row["avg_slippage_pct"] >= 80.0 for row in top.values())
