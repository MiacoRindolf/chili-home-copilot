from __future__ import annotations

from collections import OrderedDict

from app.services.trading import strategy_parameter


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("strategy-parameter cache pruning should not snapshot keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("strategy-parameter cache pruning should walk oldest entries")


def _key(symbol: str) -> tuple[str, str, str, str]:
    return ("family", "param", "symbol", symbol)


def test_strategy_parameter_cache_get_removes_stale_entry(monkeypatch) -> None:
    monkeypatch.setattr(strategy_parameter, "_CACHE_TTL_SEC", 10.0)
    strategy_parameter._CACHE.clear()
    try:
        key = _key("OLD")
        strategy_parameter._CACHE[key] = (900.0, 1.25)
        monkeypatch.setattr(strategy_parameter.time, "monotonic", lambda: 1_000.0)

        assert strategy_parameter._cache_get(key) is None
        assert key not in strategy_parameter._CACHE
    finally:
        strategy_parameter._CACHE.clear()


def test_strategy_parameter_cache_put_prunes_expired_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(strategy_parameter, "_CACHE_TTL_SEC", 10.0)
    monkeypatch.setattr(strategy_parameter, "_CACHE_MAX", 5)
    cache = _NoSnapshotOrderedDict(
        [
            (_key("EXPIRED1"), (900.0, 1.0)),
            (_key("EXPIRED2"), (901.0, 2.0)),
            (_key("FRESH"), (1_005.0, 3.0)),
        ]
    )
    monkeypatch.setattr(strategy_parameter, "_CACHE", cache)
    monkeypatch.setattr(strategy_parameter.time, "monotonic", lambda: 1_000.0)

    strategy_parameter._cache_put(_key("NEW"), 4.0)

    assert list(strategy_parameter._CACHE) == [_key("FRESH"), _key("NEW")]


def test_strategy_parameter_cache_caps_oldest_entries(monkeypatch) -> None:
    monkeypatch.setattr(strategy_parameter, "_CACHE_TTL_SEC", 100.0)
    monkeypatch.setattr(strategy_parameter, "_CACHE_MAX", 3)
    strategy_parameter._CACHE.clear()
    try:
        for idx, symbol in enumerate(["A", "B", "C"]):
            strategy_parameter._CACHE[_key(symbol)] = (1_100.0 + idx, float(idx))
        monkeypatch.setattr(strategy_parameter.time, "monotonic", lambda: 1_000.0)

        strategy_parameter._cache_put(_key("D"), 4.0)

        assert list(strategy_parameter._CACHE) == [_key("B"), _key("C"), _key("D")]
    finally:
        strategy_parameter._CACHE.clear()


def test_strategy_parameter_cache_get_moves_hit_to_newest(monkeypatch) -> None:
    strategy_parameter._CACHE.clear()
    try:
        strategy_parameter._CACHE[_key("A")] = (1_100.0, 1.0)
        strategy_parameter._CACHE[_key("B")] = (1_100.0, 2.0)
        monkeypatch.setattr(strategy_parameter.time, "monotonic", lambda: 1_000.0)

        assert strategy_parameter._cache_get(_key("A")) == 1.0
        assert list(strategy_parameter._CACHE) == [_key("B"), _key("A")]
    finally:
        strategy_parameter._CACHE.clear()
