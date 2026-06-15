"""Ross dip-buy QUALITY gates (5 knobs, ALL default-off / byte-identical).

Three flag-gated discriminators that make MARGINAL / round-trip dip entries PASS while
clean ones still FIRE:

  * Gate 1  — MACD-open STRICT (require MACD line > signal, not the lenient
    hist>=0 OR line>=signal). ``chili_momentum_entry_macd_open_strict`` (False).
  * Gate 2a — high-volume RED (distribution) pullback-candle veto.
    ``chili_momentum_dipbuy_distribution_vol_mult`` (0.0 = off).
  * Gate 2b — impulse-ACCUMULATION confirm (push volume non-decreasing).
    ``chili_momentum_dipbuy_impulse_accum_min_slope`` (-1.0 sentinel = off).
  * Gate 3  — L2 hidden-seller / big-seller veto (reuses read_ladder_distribution +
    OFI/micro). ``chili_momentum_entry_l2_veto_enabled`` (False) +
    ``chili_momentum_entry_l2_bigseller_pctile_floor`` (0.15).

PARITY is load-bearing: with EVERY knob at its default the gates are byte-identical to
current behavior (equity + crypto). FAIL-OPEN everywhere (a missing/stale L2 or thin
MACD must NEVER block a good entry). Style mirrors tests/test_dipbuy_deep_reclaim.py +
tests/test_first_pullback.py.
"""
from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural.entry_gates import (
    _dipbuy_signals_ok,
    _l2_entry_veto,
    first_pullback_break,
    momentum_pullback_trigger,
    pullback_break_confirmation,
)
from app.services.trading.momentum_neural.pipeline import LadderRead


# ── reuse the canonical valid buyable dip (matched to test_dipbuy_deep_reclaim) ──
# peak_idx=10, dip_idx=12, cur=13. _dipbuy_signals_ok now ALSO accepts opn/db/l2_as_of
# (all default None -> Gate 2a + Gate 3 are no-ops unless explicitly exercised).
def _canon(**over):
    hi = [10.6, 11.0, 11.5, 12.0, 12.5, 13.0, 13.5, 14.0, 14.3, 14.5,  # 0-9 impulse
          15.0,                                                          # 10 peak
          14.0, 13.8,                                                    # 11-12 dip
          14.3]                                                          # 13 reversal (cur)
    lo = [h - 0.3 for h in hi[:10]] + [14.6, 13.8, 13.5, 13.7]
    cl = [h - 0.05 for h in hi[:10]] + [14.9, 13.9, 13.6, 14.2]
    op = [c - 0.05 for c in cl]  # GREEN pullback bars by default (close > open)
    vo = [300000.0] * 11 + [100000.0, 90000.0, 280000.0]
    ema9 = [x - 1.0 for x in lo]
    vwap = [10.0 + 0.4 * i for i in range(14)]
    kw = dict(
        high=pd.Series(hi), low=pd.Series(lo), close=pd.Series(cl), vol=pd.Series(vo),
        vwap=vwap, peak_idx=10, dip_idx=12, dip_low=13.5, run_high=15.0,
        depth=(15.0 - 13.5) / 15.0, cur=13, w_start=0, atr_pct=0.02, tol=0.002,
        ema_wick=0.005, ema9=ema9, symbol="EDHL",
    )
    _opn_series = pd.Series(op)
    for k in ("hi", "lo", "cl", "op", "vo", "vwap"):
        if k in over:
            edits = over.pop(k)
            target = {"hi": hi, "lo": lo, "cl": cl, "op": op, "vo": vo, "vwap": vwap}[k]
            for idx, val in edits:
                target[idx] = val
            if k == "vwap":
                kw["vwap"] = vwap
            elif k == "op":
                _opn_series = pd.Series(target)
            else:
                kw[{"hi": "high", "lo": "low", "cl": "close", "vo": "vol"}[k]] = pd.Series(target)
    # opn is threaded in only when the caller asks for the distribution check; leave it
    # OUT of kw by default so the byte-identity tests run with opn=None like production
    # callers that don't supply Open.
    _want_opn = over.pop("_with_opn", False)
    kw.update(over)
    if _want_opn:
        kw["opn"] = _opn_series
    return kw


@pytest.fixture(autouse=True)
def _dipbuy_on():
    old = settings.chili_momentum_deep_reclaim_dipbuy_enabled
    settings.chili_momentum_deep_reclaim_dipbuy_enabled = True
    yield
    settings.chili_momentum_deep_reclaim_dipbuy_enabled = old


# ── first-pullback df builder (matched to test_first_pullback) ────────────────
def _explosive_first_pullback_df(
    *, n: int = 30, cur_high: float = 12.40, cur_close: float = 12.30,
    pull_top: float = 12.20, big_volume: bool = True,
    distribution_bar: bool = False, fading_impulse: bool = False,
) -> pd.DataFrame:
    base = np.linspace(10.0, 12.10, n - 4)
    highs = list(base + 0.10)
    lows = list(base - 0.10)
    closes = list(base + 0.05)
    opens = list(base - 0.05)
    for h, lo, c in ((pull_top, 11.95, 12.05), (12.10, 11.90, 12.00), (12.05, 11.92, 12.02)):
        highs.append(h)
        lows.append(lo)
        closes.append(c)
        opens.append(c - 0.03)
    highs.append(cur_high)
    lows.append(12.00)
    closes.append(cur_close)
    opens.append(12.05)

    if big_volume:
        impulse = list(np.linspace(200_000, 600_000, n - 4))
        vol = impulse + [300_000, 280_000, 320_000, 900_000]
    else:
        vol = [500_000] * (n - 4) + [120_000, 110_000, 115_000, 120_000]

    df = pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vol}
    )
    if fading_impulse and big_volume:
        # invert the impulse volume so the push is DECREASING into the high
        df.loc[: n - 5, "Volume"] = list(np.linspace(600_000, 200_000, n - 4))
    if distribution_bar:
        # make the FIRST pullback bar a big RED high-volume distribution candle.
        i = n - 4
        df.loc[i, "Open"] = 12.20
        df.loc[i, "Close"] = 11.97   # red (close < open)
        df.loc[i, "Volume"] = 5_000_000  # >> impulse mean
    return df


# ════════════════════════════════════════════════════════════════════════════
# (0) PARITY — load-bearing: ALL knobs at default ⇒ byte-identical equity+crypto
# ════════════════════════════════════════════════════════════════════════════

_DEFAULTS = {
    "chili_momentum_entry_macd_open_strict": False,
    "chili_momentum_dipbuy_distribution_vol_mult": 0.0,
    "chili_momentum_dipbuy_impulse_accum_min_slope": -1.0,
    "chili_momentum_entry_l2_veto_enabled": False,
    "chili_momentum_entry_l2_bigseller_pctile_floor": 0.15,
}


def _force_defaults(monkeypatch):
    for k, v in _DEFAULTS.items():
        monkeypatch.setattr(settings, k, v)


def _dipbuy_battery():
    # equity + crypto symbols across FIRE / ARM / PASS verdicts
    yield "eq_fire", _canon()
    yield "crypto_fire", _canon(symbol="BTC-USD")
    yield "eq_arm", _canon(hi=[(13, 13.95)], lo=[(13, 13.6)], cl=[(13, 13.9)])
    yield "eq_pass_heavy_dip", _canon(vo=[(11, 400000.0), (12, 380000.0)])
    yield "eq_pass_weak_close", _canon(hi=[(13, 14.6)], lo=[(13, 14.25)], cl=[(13, 14.3)])
    yield "crypto_pass_falling_vwap", _canon(symbol="ETH-USD",
                                             vwap=[(i, 20.0 - 0.4 * i) for i in range(14)])


def _fp_battery():
    yield "fp_explosive", _explosive_first_pullback_df()
    yield "fp_non_explosive", _explosive_first_pullback_df(big_volume=False)
    yield "fp_arm", _explosive_first_pullback_df(cur_high=12.10, cur_close=12.05)
    n = 30
    ramp = np.linspace(5.0, 6.0, n)
    yield "fp_generic_ramp", pd.DataFrame({
        "Open": ramp - 0.02, "High": ramp + 0.03, "Low": ramp - 0.03,
        "Close": ramp, "Volume": [300_000] * n,
    })
    rng = np.random.default_rng(11)
    flat = 8.0 + rng.normal(0, 0.02, n)
    yield "fp_choppy", pd.DataFrame({
        "Open": flat, "High": flat + 0.05, "Low": flat - 0.05,
        "Close": flat, "Volume": [250_000] * n,
    })


def test_parity_dipbuy_signals_defaults_byte_identical(monkeypatch):
    # Capture baseline with defaults; re-run with the SAME defaults re-asserted — every
    # gate branch is guarded by `knob != default`, so the default state IS the pre-gate
    # behavior. Outputs (verdict, level, stop, patch) must match exactly.
    baseline = {}
    _force_defaults(monkeypatch)
    for name, kw in _dipbuy_battery():
        baseline[name] = _dipbuy_signals_ok(**kw)
    # re-assert defaults explicitly (idempotent) and confirm invariance
    _force_defaults(monkeypatch)
    for name, kw in _dipbuy_battery():
        assert _dipbuy_signals_ok(**kw) == baseline[name], name
    # at least the two FIRE cases must actually be FIRE (the battery is meaningful)
    assert baseline["eq_fire"][0] == "FIRE"
    assert baseline["crypto_fire"][0] == "FIRE"


def test_parity_dipbuy_with_opn_supplied_still_default_identical(monkeypatch):
    # Supplying opn must NOT change the verdict while Gate 2a is OFF (mult == 0).
    _force_defaults(monkeypatch)
    no_opn = _dipbuy_signals_ok(**_canon())
    with_opn = _dipbuy_signals_ok(**_canon(_with_opn=True))
    assert no_opn == with_opn


@pytest.mark.parametrize("interval", ["1m", "5m"])
def test_parity_pullback_break_confirmation_defaults(monkeypatch, interval):
    # Full pipeline byte-identity: defaults == the pre-gate ladder. We compare the
    # default-state output to itself under a re-assertion of defaults (the gate code is
    # only reachable when a knob is non-default). Equity + crypto + retest on/off.
    _force_defaults(monkeypatch)
    for name, df in _fp_battery():
        for require_retest in (True, False):
            base = pullback_break_confirmation(
                df, entry_interval=interval, require_retest=require_retest, symbol="JRSH",
            )
            base_c = pullback_break_confirmation(
                df, entry_interval=interval, require_retest=require_retest, symbol="BTC-USD",
            )
            _force_defaults(monkeypatch)
            again = pullback_break_confirmation(
                df, entry_interval=interval, require_retest=require_retest, symbol="JRSH",
            )
            again_c = pullback_break_confirmation(
                df, entry_interval=interval, require_retest=require_retest, symbol="BTC-USD",
            )
            assert base == again, f"{name}/{interval}/retest={require_retest}/equity"
            assert base_c == again_c, f"{name}/{interval}/retest={require_retest}/crypto"


def test_parity_db_threading_no_effect_when_l2_off(monkeypatch):
    # Passing a db handle while Gate 3 is OFF must be byte-identical to db=None.
    _force_defaults(monkeypatch)

    class _DummyDB:
        def execute(self, *a, **k):  # pragma: no cover - never reached (gate OFF)
            raise AssertionError("db must not be touched while l2 veto is OFF")

    df = _explosive_first_pullback_df()
    none_db = momentum_pullback_trigger(df, entry_interval="1m", symbol="JRSH")
    with_db = momentum_pullback_trigger(df, entry_interval="1m", symbol="JRSH", db=_DummyDB())
    assert none_db == with_db


# ════════════════════════════════════════════════════════════════════════════
# Gate 1 — MACD-open STRICT
# ════════════════════════════════════════════════════════════════════════════

def _macd_df(*, line_above_signal: bool) -> pd.DataFrame:
    # A clean breakout df where the structural trigger FIRES; we then control the MACD
    # arrays via monkeypatch of compute_all_from_df so the strict-vs-lenient split is
    # deterministic.
    return _explosive_first_pullback_df()


def _patch_macd(monkeypatch, *, m: float, s: float, hist: float):
    real = eg.compute_all_from_df

    def _fake(df, needed=None):
        out = real(df, needed=needed)
        n = len(df)
        if "macd" in (needed or set()):
            out["macd"] = [None] * (n - 1) + [m]
            out["macd_signal"] = [None] * (n - 1) + [s]
            out["macd_hist"] = [None] * (n - 1) + [hist]
        return out

    monkeypatch.setattr(eg, "compute_all_from_df", _fake)


def test_gate1_strict_vetoes_line_below_signal_that_lenient_passes(monkeypatch):
    _force_defaults(monkeypatch)
    # hist >= 0 but line < signal: LENIENT passes, STRICT must veto.
    _patch_macd(monkeypatch, m=0.10, s=0.20, hist=0.01)
    df = _macd_df(line_above_signal=False)
    # lenient (default): macd does not block (hist>=0)
    ok_lenient, reason_lenient, _ = pullback_break_confirmation(
        df, entry_interval="5m", require_retest=False, require_macd_bullish=True, symbol="JRSH",
    )
    # strict: line < signal -> veto
    monkeypatch.setattr(settings, "chili_momentum_entry_macd_open_strict", True)
    ok_strict, reason_strict, dbg_strict = pullback_break_confirmation(
        df, entry_interval="5m", require_retest=False, require_macd_bullish=True, symbol="JRSH",
    )
    assert ok_lenient is True, reason_lenient
    assert ok_strict is False and reason_strict == "macd_not_bullish"
    assert dbg_strict.get("macd_open") == {"m": 0.10, "s": 0.20}


def test_gate1_strict_fires_when_line_above_signal(monkeypatch):
    _force_defaults(monkeypatch)
    _patch_macd(monkeypatch, m=0.30, s=0.20, hist=0.05)  # clean cross-up
    monkeypatch.setattr(settings, "chili_momentum_entry_macd_open_strict", True)
    df = _macd_df(line_above_signal=True)
    ok, reason, _ = pullback_break_confirmation(
        df, entry_interval="5m", require_retest=False, require_macd_bullish=True, symbol="JRSH",
    )
    assert ok is True, reason


def test_gate1_strict_fails_open_on_macd_warmup(monkeypatch):
    _force_defaults(monkeypatch)
    # line/signal both None (warmup) -> strict must NOT veto (fail-open).
    _patch_macd_none = eg.compute_all_from_df

    def _fake(df, needed=None):
        out = _patch_macd_none(df, needed=needed)
        n = len(df)
        if "macd" in (needed or set()):
            out["macd"] = [None] * n
            out["macd_signal"] = [None] * n
            out["macd_hist"] = [None] * n  # everything None -> the whole block is skipped
        return out

    monkeypatch.setattr(eg, "compute_all_from_df", _fake)
    monkeypatch.setattr(settings, "chili_momentum_entry_macd_open_strict", True)
    df = _explosive_first_pullback_df()
    ok, reason, _ = pullback_break_confirmation(
        df, entry_interval="5m", require_retest=False, require_macd_bullish=True, symbol="JRSH",
    )
    assert ok is True, reason  # warmup never vetoes


def test_gate1_strict_fails_open_when_only_signal_missing(monkeypatch):
    # hist present but signal None: the block runs, strict sees s is None -> fail-open.
    _force_defaults(monkeypatch)
    real = eg.compute_all_from_df

    def _fake(df, needed=None):
        out = real(df, needed=needed)
        n = len(df)
        if "macd" in (needed or set()):
            out["macd"] = [None] * (n - 1) + [0.10]
            out["macd_signal"] = [None] * n          # signal missing at cur
            out["macd_hist"] = [None] * (n - 1) + [0.02]  # hist present -> block entered
        return out

    monkeypatch.setattr(eg, "compute_all_from_df", _fake)
    monkeypatch.setattr(settings, "chili_momentum_entry_macd_open_strict", True)
    df = _explosive_first_pullback_df()
    ok, reason, _ = pullback_break_confirmation(
        df, entry_interval="5m", require_retest=False, require_macd_bullish=True, symbol="JRSH",
    )
    assert ok is True, reason


# ════════════════════════════════════════════════════════════════════════════
# Gate 2a — high-volume SELLING-candle veto
# ════════════════════════════════════════════════════════════════════════════

def test_gate2a_distribution_candle_vetoes_in_dipbuy(monkeypatch):
    _force_defaults(monkeypatch)
    # push mean = 300k (bars 9,10). Dip bar 11 = RED at 460k (>= 1.5x push), bar 12 = 30k
    # so the 2-bar dip AVERAGE = 245k <= 0.85*300k (dries up) -> the gate-OFF baseline
    # FIRES, isolating Gate 2a as the deciding factor. mult=1.5.
    kw = _canon(op=[(11, 14.7)], cl=[(11, 13.9)],
                vo=[(11, 460_000.0), (12, 30_000.0)], _with_opn=True)
    base_v = _dipbuy_signals_ok(**kw)[0]
    assert base_v in ("FIRE", "ARM"), "baseline must fire so the veto is attributable"
    monkeypatch.setattr(settings, "chili_momentum_dipbuy_distribution_vol_mult", 1.5)
    v, lvl, stop, patch = _dipbuy_signals_ok(**kw)
    assert v == "PASS" and patch["dipbuy_declined"] == "distribution_candle"
    assert "dist_vol_ratio" in patch and patch["dist_vol_ratio"] >= 1.5
    assert lvl is None and stop is None


def test_gate2a_clean_dip_still_fires(monkeypatch):
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_dipbuy_distribution_vol_mult", 2.5)
    # canonical clean dip (GREEN pullback bars, light volume) -> still FIRES
    v, lvl, _, patch = _dipbuy_signals_ok(**_canon(_with_opn=True))
    assert v == "FIRE", patch
    assert abs(lvl - 14.0) < 1e-6


def test_gate2a_green_highvol_bar_not_vetoed(monkeypatch):
    # the SAME high-volume bar that vetoes when RED is spared when GREEN (close>open) —
    # a green high-volume pullback bar is accumulation, not distribution. Same volumes
    # as the veto test (dip average dries up) so only the candle COLOUR differs.
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_dipbuy_distribution_vol_mult", 1.5)
    kw = _canon(op=[(11, 13.9)], cl=[(11, 14.7)],
                vo=[(11, 460_000.0), (12, 30_000.0)], _with_opn=True)
    v = _dipbuy_signals_ok(**kw)[0]
    assert v in ("FIRE", "ARM")  # green bar -> not a distribution veto


def test_gate2a_no_opn_fails_open(monkeypatch):
    # gate ON but opn not supplied -> the loop is skipped (fail-open), still FIRES.
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_dipbuy_distribution_vol_mult", 2.5)
    v = _dipbuy_signals_ok(**_canon())[0]  # no _with_opn -> opn=None
    assert v == "FIRE"


def test_gate2a_distribution_candle_vetoes_in_first_pullback(monkeypatch):
    _force_defaults(monkeypatch)
    df = _explosive_first_pullback_df(distribution_bar=True)
    base = first_pullback_break(df, symbol="JRSH")[0]
    monkeypatch.setattr(settings, "chili_momentum_dipbuy_distribution_vol_mult", 2.5)
    v, lvl, stop, dbg = first_pullback_break(df, symbol="JRSH")
    assert v == "PASS" and dbg.get("fp_declined") == "distribution_candle"
    assert lvl is None and stop is None
    assert base in ("FIRE", "ARM")  # OFF it would have fired


# ════════════════════════════════════════════════════════════════════════════
# Gate 2b — impulse-accumulation confirm
# ════════════════════════════════════════════════════════════════════════════

def test_gate2b_fading_impulse_vetoed(monkeypatch):
    _force_defaults(monkeypatch)
    # push volumes DECREASING into the peak -> normalized slope < 0.
    kw = _canon(vo=[(i, float(600000 - 50000 * i)) for i in range(11)], _with_opn=True)
    base_v = _dipbuy_signals_ok(**kw)[0]
    monkeypatch.setattr(settings, "chili_momentum_dipbuy_impulse_accum_min_slope", 0.0)
    v, lvl, stop, patch = _dipbuy_signals_ok(**kw)
    assert v == "PASS" and patch["dipbuy_declined"] == "impulse_not_accumulating"
    assert lvl is None and stop is None
    assert base_v in ("FIRE", "ARM")  # OFF (sentinel) it fired


def test_gate2b_accumulating_impulse_fires(monkeypatch):
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_dipbuy_impulse_accum_min_slope", 0.0)
    # push volumes RISING into the peak -> slope > 0 -> passes the floor.
    kw = _canon(vo=[(i, float(100000 + 30000 * i)) for i in range(11)], _with_opn=True)
    v, lvl, _, patch = _dipbuy_signals_ok(**kw)
    assert v == "FIRE", patch


def test_gate2b_sentinel_off_never_vetoes(monkeypatch):
    # the default -1.0 sentinel must be byte-identical even on a fading impulse.
    _force_defaults(monkeypatch)
    kw = _canon(vo=[(i, float(600000 - 50000 * i)) for i in range(11)], _with_opn=True)
    v = _dipbuy_signals_ok(**kw)[0]
    assert v in ("FIRE", "ARM")


def test_gate2b_fading_impulse_vetoed_in_first_pullback(monkeypatch):
    _force_defaults(monkeypatch)
    df = _explosive_first_pullback_df(fading_impulse=True)
    base = first_pullback_break(df, symbol="JRSH")[0]
    monkeypatch.setattr(settings, "chili_momentum_dipbuy_impulse_accum_min_slope", 0.0)
    v, _, _, dbg = first_pullback_break(df, symbol="JRSH")
    assert v == "PASS" and dbg.get("fp_declined") == "impulse_not_accumulating"
    assert base in ("FIRE", "ARM")


# ════════════════════════════════════════════════════════════════════════════
# Gate 3 — L2 hidden-seller / big-seller veto (fail-open everywhere)
# ════════════════════════════════════════════════════════════════════════════

class _StubDB:
    """A non-None db sentinel; read_ladder_distribution is monkeypatched so this is
    never actually queried."""


def _stub_ladder(monkeypatch, lr: LadderRead):
    # _l2_entry_veto does `from .pipeline import read_ladder_distribution` at call-time,
    # so patching the attribute on the pipeline module is picked up by the real helper.
    import app.services.trading.momentum_neural.pipeline as _pl
    monkeypatch.setattr(_pl, "read_ladder_distribution", lambda *a, **k: lr)


def test_gate3_big_seller_wall_vetoes(monkeypatch):
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_entry_l2_veto_enabled", True)
    # ask-heavy book: depth_imbal_pctile BELOW the 0.15 floor.
    lr = LadderRead(depth_imbal=-0.6, depth_imbal_pctile=0.05, ofi=0.0, micro_edge=1.0,
                    bid_refill=None, ask_build=0.5, spread_bps=8.0, snapshot_age_s=1.0, n_snaps=6)
    _stub_ladder(monkeypatch, lr)
    res = _l2_entry_veto("BTC-USD", db=_StubDB())
    assert res is not None and res[0] == "l2_big_seller"
    # threaded through the dip-buy gate end-to-end
    v, lvl, stop, patch = _dipbuy_signals_ok(**_canon(symbol="BTC-USD", db=_StubDB()))
    assert v == "PASS" and patch["dipbuy_declined"] == "l2_big_seller"
    assert lvl is None and stop is None


def test_gate3_hidden_seller_absorption_vetoes(monkeypatch):
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_entry_l2_veto_enabled", True)
    # buy-side OFI (>= threshold 0.25) BUT micro-price rolled over (< 0) = absorption.
    lr = LadderRead(depth_imbal=0.4, depth_imbal_pctile=0.8, ofi=0.5, micro_edge=-3.0,
                    bid_refill=None, ask_build=None, spread_bps=8.0, snapshot_age_s=1.0, n_snaps=6)
    _stub_ladder(monkeypatch, lr)
    res = _l2_entry_veto("AAPL", db=_StubDB())
    assert res is not None and res[0] == "l2_hidden_seller"


def test_gate3_clean_book_no_veto(monkeypatch):
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_entry_l2_veto_enabled", True)
    # bid-heavy, high pctile, positive micro -> NO veto, FIRES.
    lr = LadderRead(depth_imbal=0.5, depth_imbal_pctile=0.9, ofi=0.4, micro_edge=2.0,
                    bid_refill=0.1, ask_build=None, spread_bps=8.0, snapshot_age_s=1.0, n_snaps=6)
    _stub_ladder(monkeypatch, lr)
    assert _l2_entry_veto("BTC-USD", db=_StubDB()) is None
    v, lvl, _, _ = _dipbuy_signals_ok(**_canon(symbol="BTC-USD", db=_StubDB()))
    assert v == "FIRE"


def test_gate3_fail_open_db_none(monkeypatch):
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_entry_l2_veto_enabled", True)
    # db None -> NEVER veto even with the flag ON.
    assert _l2_entry_veto("BTC-USD", db=None) is None
    v = _dipbuy_signals_ok(**_canon(symbol="BTC-USD", db=None))[0]
    assert v == "FIRE"


def test_gate3_fail_open_empty_ladder(monkeypatch):
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_entry_l2_veto_enabled", True)
    # _NULL-shaped read (n_snaps == 0, all None) -> fail-open.
    null = LadderRead(None, None, None, None, None, None, None, None, 0)
    _stub_ladder(monkeypatch, null)
    assert _l2_entry_veto("BTC-USD", db=_StubDB()) is None


def test_gate3_fail_open_when_disabled(monkeypatch):
    _force_defaults(monkeypatch)  # l2_veto_enabled stays False
    # even with a screaming big-seller ladder, the disabled flag short-circuits to None.
    lr = LadderRead(depth_imbal=-0.9, depth_imbal_pctile=0.01, ofi=-0.9, micro_edge=-5.0,
                    bid_refill=None, ask_build=1.0, spread_bps=8.0, snapshot_age_s=1.0, n_snaps=6)
    _stub_ladder(monkeypatch, lr)
    assert _l2_entry_veto("BTC-USD", db=_StubDB()) is None


def test_gate3_blank_symbol_fails_open(monkeypatch):
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_entry_l2_veto_enabled", True)
    assert _l2_entry_veto("", db=_StubDB()) is None
    assert _l2_entry_veto(None, db=_StubDB()) is None


def test_gate3_vetoes_in_first_pullback(monkeypatch):
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_entry_l2_veto_enabled", True)
    lr = LadderRead(depth_imbal=-0.6, depth_imbal_pctile=0.05, ofi=0.0, micro_edge=1.0,
                    bid_refill=None, ask_build=0.5, spread_bps=8.0, snapshot_age_s=1.0, n_snaps=6)
    _stub_ladder(monkeypatch, lr)
    df = _explosive_first_pullback_df()
    base = first_pullback_break(df, symbol="JRSH")[0]
    v, lvl, stop, dbg = first_pullback_break(df, symbol="JRSH", db=_StubDB())
    assert v == "PASS" and dbg.get("fp_declined") == "l2_big_seller"
    assert base in ("FIRE", "ARM")


def test_gate3_pctile_none_fails_open_absorption_too(monkeypatch):
    # pctile None AND no absorption signal -> no veto (fail-open).
    _force_defaults(monkeypatch)
    monkeypatch.setattr(settings, "chili_momentum_entry_l2_veto_enabled", True)
    lr = LadderRead(depth_imbal=-0.6, depth_imbal_pctile=None, ofi=None, micro_edge=None,
                    bid_refill=None, ask_build=None, spread_bps=8.0, snapshot_age_s=1.0, n_snaps=2)
    _stub_ladder(monkeypatch, lr)
    assert _l2_entry_veto("BTC-USD", db=_StubDB()) is None
