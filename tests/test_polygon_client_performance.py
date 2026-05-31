from __future__ import annotations

from collections import OrderedDict

from app.services import polygon_client


class _NoSnapshotOrderedDict(OrderedDict):
    def items(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot items")

    def keys(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot keys")

    def values(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot values")


def test_cache_set_prunes_fresh_overflow_in_batches(monkeypatch) -> None:
    monkeypatch.setattr(polygon_client, "_MAX_CACHE", 10)
    monkeypatch.setattr(polygon_client.time, "time", lambda: 1_000.0)
    polygon_client._cache.clear()
    try:
        for idx in range(12):
            polygon_client._cache[f"old-{idx}"] = (900.0 + idx, idx)

        polygon_client._cache_set("new-key", "new-value")

        assert len(polygon_client._cache) <= 10
        assert "new-key" in polygon_client._cache
        assert "old-0" not in polygon_client._cache
        assert "old-1" not in polygon_client._cache
    finally:
        polygon_client._cache.clear()


def test_cache_set_prunes_oldest_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(polygon_client, "_MAX_CACHE", 10)
    monkeypatch.setattr(polygon_client.time, "time", lambda: 1_000.0)
    cache = _NoSnapshotOrderedDict(
        (f"old-{idx}", (990.0 + idx, idx))
        for idx in range(12)
    )
    monkeypatch.setattr(polygon_client, "_cache", cache)

    polygon_client._cache_set("new-key", "new-value")

    assert len(polygon_client._cache) == 9
    assert "new-key" in polygon_client._cache
    assert "old-0" not in polygon_client._cache
    assert "old-1" not in polygon_client._cache
    assert "old-2" not in polygon_client._cache
    assert "old-3" not in polygon_client._cache
