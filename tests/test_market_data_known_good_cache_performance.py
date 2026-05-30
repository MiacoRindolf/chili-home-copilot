from __future__ import annotations

from collections import OrderedDict

from app.services.trading import market_data


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("known-good cache pruning should not snapshot keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("known-good cache pruning should not snapshot items")


def test_known_good_cache_caps_oldest_entries(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_KNOWN_GOOD_CACHE_MAX", 3)
    market_data._KNOWN_GOOD_CACHE.clear()
    try:
        for ticker, price in [("A", 1.0), ("B", 2.0), ("C", 3.0)]:
            market_data._accept_known_good_price(ticker, price)

        market_data._accept_known_good_price("D", 4.0)

        assert list(market_data._KNOWN_GOOD_CACHE) == ["B", "C", "D"]
    finally:
        market_data._KNOWN_GOOD_CACHE.clear()


def test_known_good_anchor_lookup_moves_hit_to_newest(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_KNOWN_GOOD_CACHE_MAX", 2)
    market_data._KNOWN_GOOD_CACHE.clear()
    try:
        market_data._accept_known_good_price("A", 1.0)
        market_data._accept_known_good_price("B", 2.0)

        assert market_data._resolve_implausibility_anchor("A") == 1.0
        market_data._accept_known_good_price("C", 3.0)

        assert list(market_data._KNOWN_GOOD_CACHE) == ["A", "C"]
    finally:
        market_data._KNOWN_GOOD_CACHE.clear()


def test_known_good_cache_refreshes_existing_key(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_KNOWN_GOOD_CACHE_MAX", 2)
    market_data._KNOWN_GOOD_CACHE.clear()
    try:
        market_data._accept_known_good_price("A", 1.0)
        market_data._accept_known_good_price("B", 2.0)
        market_data._accept_known_good_price("A", 5.0)
        market_data._accept_known_good_price("C", 3.0)

        assert list(market_data._KNOWN_GOOD_CACHE) == ["A", "C"]
        assert market_data._KNOWN_GOOD_CACHE.get("A") == 5.0
    finally:
        market_data._KNOWN_GOOD_CACHE.clear()


def test_known_good_cache_prunes_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_KNOWN_GOOD_CACHE_MAX", 2)
    cache = _NoSnapshotOrderedDict([("A", 1.0), ("B", 2.0)])
    monkeypatch.setattr(market_data, "_KNOWN_GOOD_CACHE", cache)

    market_data._accept_known_good_price("C", 3.0)

    assert list(market_data._KNOWN_GOOD_CACHE) == ["B", "C"]
