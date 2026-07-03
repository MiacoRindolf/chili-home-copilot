"""Liquidity-bias selection — prefer FILLABLE (high-dollar-volume) Ross small-caps.

The live spread gate blocks wide-spread entries, so a trigger on an illiquid name
never fills (06-09: 13 clean triggers, 0 fills — all wide-spread-blocked). Dollar-
volume is the cleanest selection-time liquidity proxy; preferring it converts
triggers to fills (spread sweep: 06-08 5m liquid ~100bps = +$12,818 vs wide ~200bps
= +$634). These pin the pure helpers; no DB.
"""
from __future__ import annotations

from types import SimpleNamespace

import app.services.trading.momentum_neural.auto_arm as aa
import app.services.trading.momentum_neural.universe as uni
from app.services.trading.momentum_neural.universe import snapshot_dollar_volumes


def _snap(rows: dict[str, tuple]) -> list[dict]:
    """Build a snapshot from {ticker: (last_price, day_volume)}."""
    return [{"ticker": t, "lastTrade": {"p": px}, "day": {"v": v}} for t, (px, v) in rows.items()]


# ── universe.snapshot_dollar_volumes ──────────────────────────────────────────
def test_dollar_volume_is_price_times_volume():
    snap = _snap({"CCTG": (2.0, 100_000_000), "AHMA": (1.5, 10_000_000)})
    dv = snapshot_dollar_volumes(["CCTG", "AHMA"], snapshot=snap)
    assert dv["CCTG"] == 200_000_000.0   # $2 * 100M
    assert dv["AHMA"] == 15_000_000.0    # $1.5 * 10M


def test_dollar_volume_missing_symbol_absent():
    dv = snapshot_dollar_volumes(["CCTG", "GHOST"], snapshot=_snap({"CCTG": (2.0, 100_000_000)}))
    assert "CCTG" in dv and "GHOST" not in dv  # absent -> caller treats as 0.0


def test_dollar_volume_empties_and_zero_vol():
    assert snapshot_dollar_volumes([], snapshot=[]) == {}
    assert snapshot_dollar_volumes(["X"], snapshot=_snap({"X": (5.0, 0)})) == {}  # 0 vol -> absent
    assert snapshot_dollar_volumes(["X"], snapshot=[]) == {}


def test_dollar_volume_uses_day_close_when_no_last_trade():
    snap = [{"ticker": "NPT", "day": {"c": 4.0, "v": 1_000_000}}]
    assert snapshot_dollar_volumes(["NPT"], snapshot=snap)["NPT"] == 4_000_000.0


def test_dollar_volume_uses_min_av_when_day_volume_zero():
    snap = [{"ticker": "PREGAP", "lastTrade": {"p": 2.50}, "day": {"v": 0}, "min": {"av": 900_000}}]
    assert snapshot_dollar_volumes(["PREGAP"], snapshot=snap)["PREGAP"] == 2_250_000.0


# ── auto_arm._liquidity_rerank ────────────────────────────────────────────────
def _rows(*symbols):
    return [SimpleNamespace(symbol=s) for s in symbols]


def test_rerank_prefers_fillable_over_illiquid_top(monkeypatch):
    # Viability order A,B,C (A=top). But A is illiquid; B is very liquid.
    # blend(viability_rank + dvol_rank): A=0+2=2, B=1+0=1, C=2+1=3 -> B armed FIRST.
    monkeypatch.setattr(uni, "snapshot_dollar_volumes",
                        lambda syms: {"A": 10e6, "B": 300e6, "C": 50e6}, raising=True)
    out = aa._liquidity_rerank(_rows("A", "B", "C"))
    assert [r.symbol for r in out] == ["B", "A", "C"]  # liquid B beats illiquid viability-top A


def test_rerank_flag_off_keeps_viability_order(monkeypatch):
    monkeypatch.setattr(aa.settings, "chili_momentum_auto_arm_liquidity_bias", False, raising=False)
    monkeypatch.setattr(uni, "snapshot_dollar_volumes",
                        lambda syms: {"A": 10e6, "B": 300e6}, raising=True)
    out = aa._liquidity_rerank(_rows("A", "B"))
    assert [r.symbol for r in out] == ["A", "B"]  # unchanged


def test_rerank_no_liquidity_data_fails_open(monkeypatch):
    monkeypatch.setattr(uni, "snapshot_dollar_volumes", lambda syms: {}, raising=True)
    out = aa._liquidity_rerank(_rows("A", "B", "C"))
    assert [r.symbol for r in out] == ["A", "B", "C"]  # fail-open -> viability order


def test_rerank_single_or_empty_noop(monkeypatch):
    monkeypatch.setattr(uni, "snapshot_dollar_volumes", lambda syms: {"A": 1e6}, raising=True)
    assert [r.symbol for r in aa._liquidity_rerank(_rows("A"))] == ["A"]
    assert aa._liquidity_rerank([]) == []


def test_rerank_errored_helper_fails_open(monkeypatch):
    def _boom(syms):
        raise RuntimeError("snapshot exploded")
    monkeypatch.setattr(uni, "snapshot_dollar_volumes", _boom, raising=True)
    out = aa._liquidity_rerank(_rows("A", "B"))
    assert [r.symbol for r in out] == ["A", "B"]  # error -> viability order
