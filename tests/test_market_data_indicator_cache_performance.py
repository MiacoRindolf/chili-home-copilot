from __future__ import annotations

from collections import OrderedDict

from app.services.trading import market_data


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("indicator cache pruning should not snapshot keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("indicator cache pruning should walk oldest entries")


def _key(symbol: str) -> tuple[str, str, str, frozenset[str], bool]:
    return (symbol, "1d", "6mo", frozenset({"rsi"}), True)


def test_indicator_cache_get_removes_stale_entry(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_IND_CACHE_TTL", 10)
    market_data._ind_cache.clear()
    try:
        market_data._ind_cache[_key("OLD")] = (900.0, {"rsi": [1.0]})

        assert market_data._get_ind_cache(_key("OLD"), 1_000.0) is None
        assert _key("OLD") not in market_data._ind_cache
    finally:
        market_data._ind_cache.clear()


def test_indicator_cache_store_prunes_expired_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_IND_CACHE_TTL", 10)
    monkeypatch.setattr(market_data, "_IND_CACHE_MAX", 5)
    cache = _NoSnapshotOrderedDict(
        [
            (_key("EXPIRED1"), (900.0, {"rsi": [1.0]})),
            (_key("EXPIRED2"), (901.0, {"rsi": [2.0]})),
            (_key("FRESH"), (995.0, {"rsi": [3.0]})),
        ]
    )
    monkeypatch.setattr(market_data, "_ind_cache", cache)

    market_data._store_ind_cache(_key("NEW"), {"rsi": [4.0]}, 1_000.0)

    assert list(market_data._ind_cache) == [_key("FRESH"), _key("NEW")]


def test_indicator_cache_caps_oldest_entries(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_IND_CACHE_TTL", 100)
    monkeypatch.setattr(market_data, "_IND_CACHE_MAX", 3)
    market_data._ind_cache.clear()
    try:
        for idx, symbol in enumerate(["A", "B", "C"]):
            market_data._ind_cache[_key(symbol)] = (990.0 + idx, {"rsi": [float(idx)]})

        market_data._store_ind_cache(_key("D"), {"rsi": [4.0]}, 1_000.0)

        assert list(market_data._ind_cache) == [_key("B"), _key("C"), _key("D")]
    finally:
        market_data._ind_cache.clear()


def test_indicator_cache_hit_returns_original_value_and_moves_to_newest(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_IND_CACHE_TTL", 100)
    market_data._ind_cache.clear()
    try:
        original = {"rsi": [1.0]}
        market_data._ind_cache[_key("A")] = (990.0, original)
        market_data._ind_cache[_key("B")] = (991.0, {"rsi": [2.0]})

        cached = market_data._get_ind_cache(_key("A"), 1_000.0)

        assert cached is original
        assert list(market_data._ind_cache) == [_key("B"), _key("A")]
    finally:
        market_data._ind_cache.clear()
