from __future__ import annotations

from collections import OrderedDict

from app.services.trading import ai_context


class _NoSnapshotOrderedDict(OrderedDict):
    def items(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot items")

    def keys(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot keys")

    def values(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot values")


def test_thesis_cache_get_removes_stale_entry() -> None:
    cache = OrderedDict({"thesis:1": (900.0, {"stance": "old"})})

    assert ai_context._thesis_cache_get(cache, "thesis:1", now=5_000.0) is None

    assert "thesis:1" not in cache


def test_thesis_cache_get_refreshes_hit_recency() -> None:
    cache = OrderedDict(
        {
            "thesis:1": (1_000.0, {"stance": "one"}),
            "thesis:2": (1_000.0, {"stance": "two"}),
        }
    )

    assert ai_context._thesis_cache_get(cache, "thesis:1", now=1_001.0) == {"stance": "one"}

    assert list(cache) == ["thesis:2", "thesis:1"]


def test_thesis_cache_set_prunes_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(ai_context, "_THESIS_CACHE_MAX", 3)
    cache = _NoSnapshotOrderedDict(
        (f"thesis:{idx}", (990.0 + idx, {"idx": idx}))
        for idx in range(4)
    )

    ai_context._thesis_cache_set(cache, "thesis:new", {"idx": 5}, now=1_000.0)

    assert list(cache) == ["thesis:2", "thesis:3", "thesis:new"]
