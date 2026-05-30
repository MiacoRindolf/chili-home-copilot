from __future__ import annotations

from app.services.trading.fast_path import order_book
from app.services.trading.fast_path.order_book import OrderBookAggregator


def test_order_book_emit_selects_top_levels_without_full_sort(monkeypatch) -> None:
    def fail_sort(*_args, **_kwargs):
        raise AssertionError("book emission should not sort every price level")

    monkeypatch.setattr(order_book, "sorted", fail_sort, raising=False)
    agg = OrderBookAggregator(output_levels=3, emit_interval_s=0.0, max_levels_per_side=1_000)
    updates = [
        {"side": "bid", "price_level": str(100.0 + i), "new_quantity": "1.0"}
        for i in range(20)
    ] + [
        {"side": "offer", "price_level": str(120.0 + i), "new_quantity": "1.0"}
        for i in range(20)
    ]
    agg.apply_event({"type": "snapshot", "product_id": "BTC-USD", "updates": updates})

    emitted = agg.maybe_emit("BTC-USD", now_monotonic=1.0)

    assert emitted is not None
    assert [price for price, _size in emitted["bid_levels"]] == [119.0, 118.0, 117.0]
    assert [price for price, _size in emitted["ask_levels"]] == [120.0, 121.0, 122.0]
