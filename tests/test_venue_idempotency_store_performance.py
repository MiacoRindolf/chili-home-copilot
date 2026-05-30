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
