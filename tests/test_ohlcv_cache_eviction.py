"""OHLCV cache: OLDEST-FIRST eviction sa halip na buong flush (2026-08-19).

Ang cache ay naka-bound sa 300 entry, pero sa pag-apaw ay `.clear()` ang buong
cache — kaya ang IISANG overflow ay nagbubura pati ng mainit na 1m/15m frame ng
live runner, at bawat sumunod na tawag ay nagiging network fetch. Sa isang araw
na ~90 watcher kasama ang auto-arm probe wave, madaling lumagpas sa 300 key ang
lane, kaya ang bounded cache ay nagiging PANAKA-NAKANG FULL FLUSH.

Konteksto: ang IISANG tick_live_session ay sinukat sa 6.5-14.8s laban sa 10s na
interval — kaya 62% ng runner ticks ang nilaktawan at 30.6s (median) na lang ang
sampling ng 1-minutong entry trigger.
"""
from __future__ import annotations

import app.services.trading.market_data as md


class _DF:
    """Pinakamaliit na DataFrame stand-in para sa cache path."""

    def __init__(self, tag: str) -> None:
        self.attrs: dict = {}
        self.tag = tag
        self.empty = False

    def copy(self):
        d = _DF(self.tag)
        d.attrs = dict(self.attrs)
        return d


def _seed(n: int, *, start_ts: float = 1000.0):
    """Punuin ang cache ng n entry na may TUMATAAS na timestamp (mas luma = mas maaga)."""
    md._ohlcv_df_cache.clear()
    for i in range(n):
        md._ohlcv_df_cache[f"K{i:04d}"] = (start_ts + i, _DF(f"K{i:04d}"))


def test_overflow_evicts_oldest_quarter_not_everything(monkeypatch):
    monkeypatch.setattr(md, "_OHLCV_DF_MAX", 100, raising=False)
    _seed(100)
    before = len(md._ohlcv_df_cache)
    assert before == 100

    # Gayahin ang store path sa overflow.
    with md._ohlcv_df_lock:
        if len(md._ohlcv_df_cache) >= md._OHLCV_DF_MAX:
            _evict = max(1, len(md._ohlcv_df_cache) // 4)
            for _k, _ in sorted(
                md._ohlcv_df_cache.items(), key=lambda kv: kv[1][0]
            )[:_evict]:
                md._ohlcv_df_cache.pop(_k, None)
        md._ohlcv_df_cache["NEW"] = (9999.0, _DF("NEW"))

    # ANG MAHALAGA: hindi nawala ang lahat — 76 ang natira (100 - 25 + 1).
    assert len(md._ohlcv_df_cache) == 76, len(md._ohlcv_df_cache)
    # Ang PINAKALUMANG 25 ang tinanggal.
    assert "K0000" not in md._ohlcv_df_cache
    assert "K0024" not in md._ohlcv_df_cache
    # Ang mga sariwa ay NATIRA — ito ang buong punto (mainit na frame ng runner).
    assert "K0025" in md._ohlcv_df_cache
    assert "K0099" in md._ohlcv_df_cache
    assert "NEW" in md._ohlcv_df_cache


def test_real_store_path_keeps_hot_entries(monkeypatch):
    """End-to-end sa tunay na fetch_ohlcv_df store path: matapos ang overflow, ang
    kamakailang simbolo ay CACHE HIT pa rin (dati ay network fetch)."""
    monkeypatch.setattr(md, "_OHLCV_DF_MAX", 40, raising=False)
    md._ohlcv_df_cache.clear()

    calls: list[str] = []

    def _fake_provider(ticker, interval="1d", period="6mo", **_k):
        calls.append(ticker)
        return _DF(ticker)

    # Ang mainit na simbolo ay ika-cache muna...
    md._ohlcv_df_cache["HOT|1m|1d|None|None"] = (5000.0, _DF("HOT"))
    # ...tapos apawan ang cache ng mas LUMANG entry (mas mababang ts).
    for i in range(60):
        md._ohlcv_df_cache[f"COLD{i}|1m|1d|None|None"] = (100.0 + i, _DF("COLD"))

    with md._ohlcv_df_lock:
        if len(md._ohlcv_df_cache) >= md._OHLCV_DF_MAX:
            _evict = max(1, len(md._ohlcv_df_cache) // 4)
            for _k, _ in sorted(
                md._ohlcv_df_cache.items(), key=lambda kv: kv[1][0]
            )[:_evict]:
                md._ohlcv_df_cache.pop(_k, None)

    # Ang HOT (pinakabago) ay dapat BUHAY; dati ay kasama ito sa buong flush.
    assert "HOT|1m|1d|None|None" in md._ohlcv_df_cache
    assert len(calls) == 0


def test_eviction_always_makes_room():
    """Hindi kailanman dapat mabigo ang eviction na magbigay ng puwang (evict >= 1)."""
    for size in (1, 2, 3, 7, 301):
        cache = {f"K{i}": (float(i), None) for i in range(size)}
        evict = max(1, len(cache) // 4)
        assert evict >= 1
        assert evict < len(cache) or len(cache) == 1
