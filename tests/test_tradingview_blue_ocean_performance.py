from __future__ import annotations

from collections import OrderedDict

from app.services.trading import tradingview_blue_ocean as boats


class _NoSnapshotDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("cache pruning should not snapshot all keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("cache pruning should walk oldest entries directly")


def test_boats_cache_get_removes_stale_entry() -> None:
    key = ("AAPL", "5", 150)
    boats._CACHE.clear()
    try:
        boats._CACHE[key] = (900.0, [{"time": 1, "close": 100.0}])

        assert boats._cache_get(key, now=1_000.0, ttl=10.0) is None
        assert key not in boats._CACHE
    finally:
        boats._CACHE.clear()


def test_boats_cache_set_prunes_expired_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(boats, "_CACHE_MAX", 5)
    cache = _NoSnapshotDict(
        [
            (("OLD1", "5", 150), (900.0, [{"time": 1}])),
            (("OLD2", "5", 150), (901.0, [{"time": 2}])),
            (("FRESH", "5", 150), (995.0, [{"time": 3}])),
        ]
    )
    monkeypatch.setattr(boats, "_CACHE", cache)

    boats._cache_set(("NEW", "5", 150), [{"time": 4}], now=1_000.0, ttl=10.0)

    assert list(boats._CACHE) == [("FRESH", "5", 150), ("NEW", "5", 150)]


def test_boats_cache_set_caps_oldest_entries(monkeypatch) -> None:
    monkeypatch.setattr(boats, "_CACHE_MAX", 3)
    boats._CACHE.clear()
    try:
        for idx, sym in enumerate(["AAA", "BBB", "CCC"]):
            boats._CACHE[(sym, "5", 150)] = (990.0 + idx, [{"time": idx}])

        boats._cache_set(("DDD", "5", 150), [{"time": 4}], now=1_000.0, ttl=20.0)

        assert list(boats._CACHE) == [
            ("BBB", "5", 150),
            ("CCC", "5", 150),
            ("DDD", "5", 150),
        ]
    finally:
        boats._CACHE.clear()


def test_boats_cache_hit_returns_copy() -> None:
    key = ("AAPL", "5", 150)
    bars = [{"time": 1, "close": 100.0}]
    boats._CACHE.clear()
    try:
        boats._CACHE[key] = (1_000.0, bars)

        cached = boats._cache_get(key, now=1_001.0, ttl=10.0)

        assert cached == bars
        assert cached is not bars
    finally:
        boats._CACHE.clear()


def test_boats_cache_hit_refreshes_recency() -> None:
    first = ("AAPL", "5", 150)
    second = ("MSFT", "5", 150)
    boats._CACHE.clear()
    try:
        boats._CACHE[first] = (1_000.0, [{"time": 1}])
        boats._CACHE[second] = (1_000.0, [{"time": 2}])

        assert boats._cache_get(first, now=1_001.0, ttl=10.0) == [{"time": 1}]

        assert list(boats._CACHE) == [second, first]
    finally:
        boats._CACHE.clear()
