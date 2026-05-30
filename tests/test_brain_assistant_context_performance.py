from __future__ import annotations

from collections import OrderedDict

from app.services.trading import brain_assistant_context as bac


class _NoSnapshotDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("snapshot-cache pruning should not snapshot all keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("snapshot-cache pruning should walk oldest entries directly")


def test_snapshot_cache_get_removes_stale_entry(monkeypatch) -> None:
    monkeypatch.setattr(bac, "_SNAPSHOT_CACHE_TTL", 10)
    bac._snapshot_cache.clear()
    try:
        key = (1, "AAPL")
        bac._snapshot_cache[key] = (900.0, {"ticker": "AAPL"})

        assert bac._snapshot_cache_get(key, now=1_000.0) is None
        assert key not in bac._snapshot_cache
    finally:
        bac._snapshot_cache.clear()


def test_snapshot_cache_set_prunes_expired_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(bac, "_SNAPSHOT_CACHE_TTL", 10)
    monkeypatch.setattr(bac, "_SNAPSHOT_CACHE_MAX", 5)
    cache = _NoSnapshotDict(
        [
            ((1, "old-1"), (900.0, {"v": 1})),
            ((1, "old-2"), (901.0, {"v": 2})),
            ((1, "fresh"), (995.0, {"v": 3})),
        ]
    )
    monkeypatch.setattr(bac, "_snapshot_cache", cache)

    bac._snapshot_cache_set((1, "new"), {"v": 4}, now=1_000.0)

    assert list(bac._snapshot_cache) == [(1, "fresh"), (1, "new")]


def test_snapshot_cache_set_caps_oldest_entries(monkeypatch) -> None:
    monkeypatch.setattr(bac, "_SNAPSHOT_CACHE_TTL", 100)
    monkeypatch.setattr(bac, "_SNAPSHOT_CACHE_MAX", 3)
    bac._snapshot_cache.clear()
    try:
        for idx, key in enumerate([(1, "a"), (1, "b"), (1, "c")]):
            bac._snapshot_cache[key] = (990.0 + idx, {"v": idx})

        bac._snapshot_cache_set((1, "d"), {"v": 4}, now=1_000.0)

        assert list(bac._snapshot_cache) == [(1, "b"), (1, "c"), (1, "d")]
    finally:
        bac._snapshot_cache.clear()


def test_snapshot_cache_get_moves_hit_to_newest_and_returns_copy(monkeypatch) -> None:
    monkeypatch.setattr(bac, "_SNAPSHOT_CACHE_TTL", 100)
    bac._snapshot_cache.clear()
    try:
        snapshot = {"ticker": "AAPL"}
        bac._snapshot_cache[(1, "a")] = (1_000.0, snapshot)
        bac._snapshot_cache[(1, "b")] = (1_000.0, {"ticker": "MSFT"})

        cached = bac._snapshot_cache_get((1, "a"), now=1_001.0)

        assert cached == snapshot
        assert cached is not snapshot
        assert list(bac._snapshot_cache) == [(1, "b"), (1, "a")]
    finally:
        bac._snapshot_cache.clear()
