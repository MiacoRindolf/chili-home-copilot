from __future__ import annotations

import collections

from app.services import yf_session


class _NoSnapshotOrderedDict(collections.OrderedDict):
    def items(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot items")

    def keys(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot keys")

    def values(self):  # pragma: no cover - fails the test if called
        raise AssertionError("cache pruning should not snapshot values")


def test_cache_set_prunes_fresh_overflow_in_batches(monkeypatch) -> None:
    monkeypatch.setattr(yf_session, "_MAX_CACHE_SIZE", 10)
    monkeypatch.setattr(yf_session.time, "time", lambda: 1_000.0)
    with yf_session._cache_lock:
        yf_session._cache.clear()
        for idx in range(12):
            yf_session._cache[f"old-{idx}"] = (900.0 + idx, idx)
    try:
        yf_session._cache_set("new-key", "new-value")

        with yf_session._cache_lock:
            assert len(yf_session._cache) <= 10
            assert "new-key" in yf_session._cache
            assert "old-0" not in yf_session._cache
            assert "old-1" not in yf_session._cache
    finally:
        with yf_session._cache_lock:
            yf_session._cache.clear()


def test_cache_set_prunes_oldest_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(yf_session, "_MAX_CACHE_SIZE", 10)
    monkeypatch.setattr(yf_session.time, "time", lambda: 1_000.0)
    cache = _NoSnapshotOrderedDict(
        (f"old-{idx}", (990.0 + idx, idx))
        for idx in range(12)
    )
    monkeypatch.setattr(yf_session, "_cache", cache)

    yf_session._cache_set("new-key", "new-value")

    with yf_session._cache_lock:
        assert len(yf_session._cache) == 9
        assert "new-key" in yf_session._cache
        assert "old-0" not in yf_session._cache
        assert "old-1" not in yf_session._cache
        assert "old-2" not in yf_session._cache
        assert "old-3" not in yf_session._cache


def test_dead_ticker_cache_is_capped_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(yf_session, "_MAX_DEAD_TICKERS", 3)
    monkeypatch.setattr(yf_session.time, "time", lambda: 1_000.0)
    cache = _NoSnapshotOrderedDict(
        (f"OLD{idx}", 990.0 + idx)
        for idx in range(4)
    )
    monkeypatch.setattr(yf_session, "_dead_tickers", cache)

    yf_session._mark_dead("NEW")

    assert list(yf_session._dead_tickers) == ["OLD2", "OLD3", "NEW"]


def test_empty_counts_cache_is_capped_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(yf_session, "_MAX_EMPTY_COUNTS", 3)
    cache = _NoSnapshotOrderedDict(
        (f"OLD{idx}", idx)
        for idx in range(4)
    )
    monkeypatch.setattr(yf_session, "_empty_counts", cache)

    assert yf_session._bump_empty("NEW") == 1

    assert list(yf_session._empty_counts) == ["OLD2", "OLD3", "NEW"]
