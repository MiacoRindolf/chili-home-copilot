from __future__ import annotations

from collections import OrderedDict

from app.services.trading.venue import idempotency_store


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("TTL pruning should not snapshot all cache keys")


def test_mem_remember_prunes_expired_entries_without_key_snapshot(monkeypatch) -> None:
    cache = _NoSnapshotOrderedDict(
        [
            ("expired-1", 100.0),
            ("expired-2", 110.0),
            ("fresh", 700.0),
        ]
    )
    monkeypatch.setattr(idempotency_store, "_mem_cache", cache)
    monkeypatch.setattr(idempotency_store.time, "monotonic", lambda: 1_000.0)

    idempotency_store._mem_remember("new")

    assert list(cache) == ["fresh", "new"]
    assert cache["new"] == 1_000.0


def test_mem_duplicate_hit_refreshes_recency(monkeypatch) -> None:
    cache = OrderedDict(
        [
            ("hot", 990.0),
            ("cold", 991.0),
        ]
    )
    monkeypatch.setattr(idempotency_store, "_mem_cache", cache)
    monkeypatch.setattr(idempotency_store.time, "monotonic", lambda: 1_000.0)

    assert idempotency_store._mem_is_duplicate("hot") is True

    assert list(cache) == ["cold", "hot"]


def test_ttl_settings_updates_remain_live(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(idempotency_store, "_settings_obj", None)
    monkeypatch.setattr(settings, "chili_venue_idempotency_ttl_hours_crypto", 12.0, raising=False)
    assert idempotency_store._venue_ttl_hours("coinbase") == 12.0

    monkeypatch.setattr(settings, "chili_venue_idempotency_ttl_hours_crypto", 24.0, raising=False)
    assert idempotency_store._venue_ttl_hours("coinbase") == 24.0
