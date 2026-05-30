from __future__ import annotations

from app.services import polygon_client


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
