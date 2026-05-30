from __future__ import annotations

from collections import OrderedDict

import pandas as pd

from app.services.trading import market_data


class _NoSnapshotOrderedDict(OrderedDict):
    def keys(self):  # type: ignore[override]
        raise AssertionError("OHLCV cache pruning should not snapshot keys")

    def items(self):  # type: ignore[override]
        raise AssertionError("OHLCV cache pruning should walk oldest entries")


def _df(value: float) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": [value],
            "High": [value],
            "Low": [value],
            "Close": [value],
            "Volume": [100],
        }
    )


def test_ohlcv_df_cache_get_removes_stale_entry(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_OHLCV_DF_TTL", 10)
    market_data._ohlcv_df_cache.clear()
    try:
        market_data._ohlcv_df_cache["OLD|1d|5d|None|None"] = (900.0, _df(1.0))

        assert market_data._get_ohlcv_df_cache("OLD|1d|5d|None|None", 1_000.0) is None
        assert "OLD|1d|5d|None|None" not in market_data._ohlcv_df_cache
    finally:
        market_data._ohlcv_df_cache.clear()


def test_ohlcv_df_cache_store_prunes_expired_without_snapshot(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_OHLCV_DF_TTL", 10)
    monkeypatch.setattr(market_data, "_OHLCV_DF_MAX", 5)
    cache = _NoSnapshotOrderedDict(
        [
            ("EXPIRED1|1d|5d|None|None", (900.0, _df(1.0))),
            ("EXPIRED2|1d|5d|None|None", (901.0, _df(2.0))),
            ("FRESH|1d|5d|None|None", (995.0, _df(3.0))),
        ]
    )
    monkeypatch.setattr(market_data, "_ohlcv_df_cache", cache)

    market_data._store_ohlcv_df_cache("NEW|1d|5d|None|None", _df(4.0), 1_000.0)

    assert list(market_data._ohlcv_df_cache) == [
        "FRESH|1d|5d|None|None",
        "NEW|1d|5d|None|None",
    ]


def test_ohlcv_df_cache_caps_oldest_entries(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_OHLCV_DF_TTL", 100)
    monkeypatch.setattr(market_data, "_OHLCV_DF_MAX", 3)
    market_data._ohlcv_df_cache.clear()
    try:
        for idx, symbol in enumerate(["A", "B", "C"]):
            market_data._ohlcv_df_cache[f"{symbol}|1d|5d|None|None"] = (
                990.0 + idx,
                _df(float(idx)),
            )

        market_data._store_ohlcv_df_cache("D|1d|5d|None|None", _df(4.0), 1_000.0)

        assert list(market_data._ohlcv_df_cache) == [
            "B|1d|5d|None|None",
            "C|1d|5d|None|None",
            "D|1d|5d|None|None",
        ]
    finally:
        market_data._ohlcv_df_cache.clear()


def test_ohlcv_df_cache_hit_returns_copy_and_moves_to_newest(monkeypatch) -> None:
    monkeypatch.setattr(market_data, "_OHLCV_DF_TTL", 100)
    market_data._ohlcv_df_cache.clear()
    try:
        original = _df(1.0)
        market_data._ohlcv_df_cache["A|1d|5d|None|None"] = (990.0, original)
        market_data._ohlcv_df_cache["B|1d|5d|None|None"] = (991.0, _df(2.0))

        cached = market_data._get_ohlcv_df_cache("A|1d|5d|None|None", 1_000.0)

        assert cached is not None
        assert cached is not original
        cached.loc[0, "Close"] = 99.0
        assert float(original.loc[0, "Close"]) == 1.0
        assert list(market_data._ohlcv_df_cache) == [
            "B|1d|5d|None|None",
            "A|1d|5d|None|None",
        ]
    finally:
        market_data._ohlcv_df_cache.clear()
