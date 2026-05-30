from __future__ import annotations

from app.services.diagnostics import mem_watcher


def test_top_count_items_uses_bounded_heap(monkeypatch) -> None:
    def fail_sort(*_args, **_kwargs):
        raise AssertionError("memory watcher top counts should not sort every item")

    monkeypatch.setattr(mem_watcher, "sorted", fail_sort, raising=False)
    counts = {f"T{i:03d}": i for i in range(100)}

    top = mem_watcher._top_count_items(counts, 5)

    assert top == [
        ("T099", 99),
        ("T098", 98),
        ("T097", 97),
        ("T096", 96),
        ("T095", 95),
    ]


def test_top_count_items_handles_empty_and_non_positive_limits() -> None:
    assert mem_watcher._top_count_items({}, 5) == []
    assert mem_watcher._top_count_items({"dict": 10}, 0) == []
