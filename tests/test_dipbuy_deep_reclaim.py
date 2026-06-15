"""Dip-buy evolution of deep_reclaim — buy the FIRST reversal off the dip (Ross),
EARLIER than the recovery-high reclaim, behind a 3-signal knife gate.

The pure helper ``_dipbuy_signals_ok`` carries all the numeric discipline, so it is
unit-tested in isolation (a valid buyable dip FIRES; each falling-knife signature
PASSes → the caller falls through to the existing recovery-high reclaim BYTE-
IDENTICALLY). A small integration layer pins the observable reason + the kill-switch
byte-identity through the full ``pullback_break_confirmation``.
"""
from __future__ import annotations

import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import (
    TICK_ARMED_WAIT_REASONS,
    _dipbuy_signals_ok,
    _dipbuy_tick_thrust_ok,
)


# ── a canonical VALID buyable dip ────────────────────────────────────────────
# 10-bar rising impulse → a 2-bar shallow dip on DRYING volume → a strong-close
# reversal bar that ticks the dip bar's own high back on RETURNING volume.
# peak_idx=10, dip_idx=12, cur=13; deep enough that the dip-low stop is real, with a
# measured-move continuation runway that clears 2:1.
def _canon(**over):
    hi = [10.6, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.3, 14.5,  # 0-9 impulse
          15.0,                                                          # 10 peak
          14.0, 13.8,                                                    # 11-12 dip (light vol)
          14.3]                                                          # 13 reversal (cur)
    lo = [h - 0.3 for h in hi[:10]] + [14.6, 13.8, 13.5, 13.7]
    cl = [h - 0.05 for h in hi[:10]] + [14.9, 13.9, 13.6, 14.2]
    vo = [300000.0] * 11 + [100000.0, 90000.0, 280000.0]
    ema9 = [x - 1.0 for x in lo]  # well below every low → "first pullback" clean
    vwap = [10.0 + 0.4 * i for i in range(14)]  # strictly rising proxy
    kw = dict(
        high=pd.Series(hi), low=pd.Series(lo), close=pd.Series(cl), vol=pd.Series(vo),
        vwap=vwap, peak_idx=10, dip_idx=12, dip_low=13.5, run_high=15.0,
        depth=(15.0 - 13.5) / 15.0, cur=13, w_start=0, atr_pct=0.02, tol=0.002,
        ema_wick=0.005, ema9=ema9, symbol="EDHL",
    )
    # series overrides come as (index, value) lists under e.g. hi=[(13, 14.1)]
    for k in ("hi", "lo", "cl", "vo", "vwap"):
        if k in over:
            edits = over.pop(k)
            target = {"hi": hi, "lo": lo, "cl": cl, "vo": vo, "vwap": vwap}[k]
            for idx, val in edits:
                target[idx] = val
            if k == "vwap":
                kw["vwap"] = vwap
            else:
                kw[{"hi": "high", "lo": "low", "cl": "close", "vo": "vol"}[k]] = pd.Series(target)
    kw.update(over)
    return kw


@pytest.fixture(autouse=True)
def _on():
    old = settings.chili_momentum_deep_reclaim_dipbuy_enabled
    settings.chili_momentum_deep_reclaim_dipbuy_enabled = True
    yield
    settings.chili_momentum_deep_reclaim_dipbuy_enabled = old


# ── the FIRE / ARM core ──────────────────────────────────────────────────────

def test_valid_dip_fires_at_pullback_high_not_recovery_high():
    v, lvl, stop, patch = _dipbuy_signals_ok(**_canon())
    assert v == "FIRE", patch
    # level is the dip bar's OWN high (14.0), well BELOW the recovery swing high / peak
    # (15.0) — the earliness gain (enter near the dip, not the chase).
    assert abs(lvl - 14.0) < 1e-6, lvl
    assert lvl < 15.0
    # stop is the dip-low anchor (just under 13.5), NOT pre-floored by ATR here
    assert 13.3 < stop < 13.5, stop
    assert patch["dipbuy_runway_rr"] >= 2.0


def test_not_broken_yet_arms_tick_watch():
    # current bar high has NOT exceeded the dip-bar high (14.0) → ARM, not FIRE
    v, lvl, stop, _ = _dipbuy_signals_ok(**_canon(hi=[(13, 13.95)], lo=[(13, 13.6)], cl=[(13, 13.9)]))
    assert v == "ARM"
    assert abs(lvl - 14.0) < 1e-6


# ── falling-knife signatures must PASS (fall through to the legacy reclaim) ───

def test_heavy_dip_volume_rejected():
    # dip bars heavier than the push bars = sellers in control (the knife)
    v, _, _, patch = _dipbuy_signals_ok(**_canon(vo=[(11, 400000.0), (12, 380000.0)]))
    assert v == "PASS" and patch["dipbuy_declined"] == "no_dryup_heavy_dip"


def test_insufficient_dryup_rejected():
    # dip volume only marginally below the push mean → not a real dry-up
    v, _, _, patch = _dipbuy_signals_ok(**_canon(vo=[(11, 290000.0), (12, 285000.0)]))
    assert v == "PASS" and patch["dipbuy_declined"] == "insufficient_dryup"


def test_low_volume_bounce_rejected():
    # dry-up holds but the trigger bar has NO returning volume (anemic bounce)
    v, _, _, patch = _dipbuy_signals_ok(**_canon(vo=[(13, 50000.0)]))
    assert v == "PASS" and patch["dipbuy_declined"] == "weak_completed_break"


def test_falling_vwap_rejected():
    v, _, _, patch = _dipbuy_signals_ok(**_canon(vwap=[(i, 20.0 - 0.4 * i) for i in range(14)]))
    assert v == "PASS" and patch["dipbuy_declined"] == "vwap_not_rising"


def test_lower_high_structure_rejected():
    # the "peak" is NOT a higher high than the prior window → broken uptrend
    v, _, _, patch = _dipbuy_signals_ok(**_canon(run_high=14.4))
    assert v == "PASS" and patch["dipbuy_declined"] == "hh_hl_broken"


def test_weak_reversal_close_rejected():
    # the trigger bar pokes the level but closes weak (top-heavy, not a strong reclaim)
    v, _, _, patch = _dipbuy_signals_ok(
        **_canon(hi=[(13, 14.6)], lo=[(13, 14.25)], cl=[(13, 14.3)])
    )
    assert v == "PASS" and patch["dipbuy_declined"] == "weak_completed_break"


def test_not_shallow_grind_rejected():
    # a long grind down (dip many bars after the peak) is not a shallow first pullback
    v, _, _, patch = _dipbuy_signals_ok(**_canon(peak_idx=5))
    assert v == "PASS" and patch["dipbuy_declined"] == "not_shallow"


# ── fail-open / kill-switch / class-aware ────────────────────────────────────

def test_vwap_warmup_fails_open():
    # too few non-NaN VWAP points (the 20-bar rolling-proxy warm-up) → PASS, never fire blind
    v, _, _, patch = _dipbuy_signals_ok(**_canon(vwap=[(i, None) for i in range(11)]))
    assert v == "PASS" and patch["dipbuy_declined"] == "vwap_warmup"


def test_missing_volume_fails_open():
    v, _, _, patch = _dipbuy_signals_ok(**_canon(vo=[(i, float("nan")) for i in range(14)]))
    assert v == "PASS" and patch["dipbuy_declined"] in ("volume_nan", "thin_volume_window")


def test_kill_switch_off_passes(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_deep_reclaim_dipbuy_enabled", False)
    v, lvl, stop, patch = _dipbuy_signals_ok(**_canon())
    assert v == "PASS" and patch["dipbuy_declined"] == "disabled"
    assert lvl is None and stop is None


def test_crypto_fires_with_class_reward_risk():
    # the same valid dip on a -USD symbol uses class_aware_reward_risk(crypto); the
    # canonical runway clears it comfortably (>2:1 measured move)
    v, lvl, _, _ = _dipbuy_signals_ok(**_canon(symbol="BTC-USD"))
    assert v == "FIRE"
    assert abs(lvl - 14.0) < 1e-6


def test_never_raises_on_garbage():
    # bad indices must degrade to PASS, never raise (the fail-open contract)
    v, lvl, stop, _ = _dipbuy_signals_ok(**_canon(peak_idx=99, dip_idx=2))
    assert v == "PASS" and lvl is None and stop is None


# ── tick thrust guard + shared tuple ─────────────────────────────────────────

def test_dipbuy_tick_arm_in_shared_tuple():
    assert "waiting_for_dipbuy_break" in TICK_ARMED_WAIT_REASONS


def test_dipbuy_tick_thrust_buffer_blocks_1tick_poke():
    # the dip bar's high is the tightest level: a 1-tick poke must clear the ATR/floor
    # buffer in EVERY session (the premarket guard is a no-op in RTH/crypto)
    level = 14.0
    assert _dipbuy_tick_thrust_ok(live_price=14.001, level=level, atr_pct=0.02) is False
    assert _dipbuy_tick_thrust_ok(live_price=14.30, level=level, atr_pct=0.02) is True
    # fail-open on a missing volatility read
    assert _dipbuy_tick_thrust_ok(live_price=14.001, level=level, atr_pct=None) is True
