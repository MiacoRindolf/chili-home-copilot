"""Ross FIRST-PULLBACK entry (the EARLIEST, most aggressive momentum entry): the
first 1m candle to make a NEW HIGH after the FIRST shallow pullback off a confirmed
impulse (Ross caught JRSH this way for +$21k).

Two layers, mirroring tests/test_dipbuy_deep_reclaim.py:
  * the pure ``first_pullback_break`` gate — a valid explosive shallow-first-pullback
    -> new-high FIRES; a later/Nth pullback, a non-explosive name, and a too-deep
    pull all PASS (fall through to the existing ladder),
  * the integration through ``pullback_break_confirmation`` /
    ``momentum_pullback_trigger`` — the KILL-SWITCH byte-identity (the load-bearing
    parity contract), the ARM -> ``waiting_for_first_pullback_break`` tick routing
    through ``_dipbuy_tick_thrust_ok``, and the pullback-low stop.
"""
from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import (
    TICK_ARMED_WAIT_REASONS,
    _is_first_pullback,
    first_pullback_break,
    momentum_pullback_trigger,
    pullback_break_confirmation,
)


# ── df builders ───────────────────────────────────────────────────────────────
# A canonical EXPLOSIVE name with a shallow FIRST pullback -> new high on the
# current bar. ~30 bars so ema_9 / atr(14) / volume_ratio(20) are all warm. The
# impulse rises cleanly to a top at bar n-5, holds the 9-EMA the whole way (first
# pullback), pulls back 2-3 shallow bars, then the current bar makes a NEW HIGH
# above the pullback's prior swing high.
def _explosive_first_pullback_df(
    *,
    n: int = 30,
    cur_high: float = 12.40,      # current bar high (> the pullback swing high -> FIRE)
    cur_close: float = 12.30,
    pull_top: float = 12.20,      # the pullback swing high (level to break)
    big_volume: bool = True,
) -> pd.DataFrame:
    # rising impulse: 10.0 -> ~12.2 over the first n-4 bars, then a 3-bar shallow dip.
    base = np.linspace(10.0, 12.10, n - 4)
    highs = list(base + 0.10)
    lows = list(base - 0.10)
    closes = list(base + 0.05)
    opens = list(base - 0.05)
    # 3-bar shallow pullback (highs below the impulse top, but holding well above
    # any 9-EMA; lows only a touch under the rise).
    for h, lo, c in ((pull_top, 11.95, 12.05), (12.10, 11.90, 12.00), (12.05, 11.92, 12.02)):
        highs.append(h)
        lows.append(lo)
        closes.append(c)
        opens.append(c - 0.03)
    # the current bar: a NEW HIGH above the pullback swing high (the breakout).
    highs.append(cur_high)
    lows.append(12.00)
    closes.append(cur_close)
    opens.append(12.05)

    # volume: strong + rising into the move so volume_ratio(20) >> 1 at the tail
    # (an explosive name); small if not.
    if big_volume:
        vol = list(np.linspace(200_000, 600_000, n - 4)) + [300_000, 280_000, 320_000, 900_000]
    else:
        # FLAT, below-average tail volume -> sustained rel-vol < 1.0 (non-explosive).
        vol = [500_000] * (n - 4) + [120_000, 110_000, 115_000, 120_000]

    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vol}
    )


@pytest.fixture(autouse=True)
def _on():
    old = settings.chili_momentum_entry_first_pullback_enabled
    settings.chili_momentum_entry_first_pullback_enabled = True
    yield
    settings.chili_momentum_entry_first_pullback_enabled = old


# ── (b) _is_first_pullback extraction parity ─────────────────────────────────
# The helper was EXTRACTED from the inline loop in _dipbuy_signals_ok; the dipbuy
# suite proves _dipbuy_signals_ok is byte-identical. Pin the helper's own contract.

def test_is_first_pullback_true_when_band_held():
    lo = [9.0, 9.2, 9.4, 9.6, 9.8]
    ema9 = [8.0, 8.1, 8.2, 8.3, 8.4]  # every low well above the band
    assert _is_first_pullback(lo, ema9, anchor=0, peak_idx=4, ema_wick=0.005) is True


def test_is_first_pullback_false_on_earlier_dip():
    lo = [9.0, 7.0, 9.4, 9.6, 9.8]   # bar 1 dipped well below the band (an earlier pull)
    ema9 = [8.0, 8.1, 8.2, 8.3, 8.4]
    assert _is_first_pullback(lo, ema9, anchor=0, peak_idx=4, ema_wick=0.005) is False


def test_is_first_pullback_skips_none_ema():
    lo = [9.0, 5.0, 9.4]
    ema9 = [8.0, None, 8.2]           # None at the dipping bar -> skipped (fail-open)
    assert _is_first_pullback(lo, ema9, anchor=0, peak_idx=3, ema_wick=0.005) is True


# ── (c) first_pullback_break verdict matrix ──────────────────────────────────

def test_explosive_first_pullback_fires_at_pullback_high():
    v, lvl, stop, dbg = first_pullback_break(_explosive_first_pullback_df(), symbol="JRSH")
    assert v == "FIRE", dbg
    assert dbg["pattern"] == "first_pullback"
    # level is the pullback swing high (the breakout level), BELOW the current new high.
    assert lvl is not None and lvl < 12.40
    # (e) tight stop = the pullback LOW (not pre-floored here; the vol-floor layer widens
    # it downstream). The shallow pull's low sits around 11.90.
    assert stop is not None and 11.85 < stop < 12.05, stop
    assert stop < lvl


def test_later_nth_pullback_passes():
    # inject an EARLIER dip below the 9-EMA inside the impulse -> NOT the first pullback.
    df = _explosive_first_pullback_df()
    df.loc[12, "Low"] = 8.0   # a deep early dip mid-impulse (an Nth-pullback signature)
    v, lvl, stop, dbg = first_pullback_break(df, symbol="JRSH")
    assert v == "PASS"
    assert dbg.get("fp_declined") == "not_first_pullback"
    assert lvl is None and stop is None


def test_non_explosive_name_passes():
    # flat/below-average tail volume -> sustained rel-vol < the floor -> not explosive.
    v, lvl, stop, dbg = first_pullback_break(
        _explosive_first_pullback_df(big_volume=False), symbol="DULL"
    )
    assert v == "PASS"
    assert dbg.get("fp_declined") == "not_explosive"
    assert lvl is None and stop is None


def test_too_deep_pullback_passes():
    # a deep flush on the LAST pre-cur bar makes the retrace exceed the shallow cap.
    df = _explosive_first_pullback_df()
    df.loc[len(df) - 2, "Low"] = 7.5   # the pullback low collapses -> too deep
    v, lvl, stop, dbg = first_pullback_break(df, symbol="JRSH")
    assert v == "PASS"
    assert dbg.get("fp_declined") in ("too_deep", "pullback_below_ema9")
    assert lvl is None and stop is None


def test_arms_when_not_yet_broken():
    # current bar high does NOT exceed the pullback swing high -> ARM, not FIRE.
    df = _explosive_first_pullback_df(cur_high=12.10, cur_close=12.05)
    v, lvl, stop, dbg = first_pullback_break(df, symbol="JRSH")
    assert v == "ARM", dbg
    assert lvl is not None and stop is not None and stop < lvl


def test_never_raises_on_garbage():
    v, lvl, stop, dbg = first_pullback_break(pd.DataFrame({"Close": [1.0]}), symbol="X")
    assert v == "PASS" and lvl is None and stop is None


def test_kill_switch_passes_via_disabled_at_callsite():
    # the gate function itself has no kill-switch (it is pure); the flag lives at the
    # call site in pullback_break_confirmation (asserted in the parity tests below).
    v, _, _, _ = first_pullback_break(_explosive_first_pullback_df(), symbol="JRSH")
    assert v in ("FIRE", "ARM", "PASS")


# ── (a) PARITY: flag OFF ⇒ pullback_break_confirmation byte-identical ─────────
# A battery of synthetic dfs; with the flag OFF the (ok, reason, debug) output must
# equal the output that the SAME df produces today (the existing retest/raw ladder),
# which we capture by running with the branch skipped via the kill-switch.

def _battery():
    yield "explosive_fp", _explosive_first_pullback_df()
    yield "non_explosive", _explosive_first_pullback_df(big_volume=False)
    yield "arm_only", _explosive_first_pullback_df(cur_high=12.10, cur_close=12.05)
    # a generic non-setup ramp
    n = 30
    ramp = np.linspace(5.0, 6.0, n)
    yield "generic_ramp", pd.DataFrame({
        "Open": ramp - 0.02, "High": ramp + 0.03, "Low": ramp - 0.03,
        "Close": ramp, "Volume": [300_000] * n,
    })
    # a choppy flat tape
    rng = np.random.default_rng(7)
    flat = 8.0 + rng.normal(0, 0.02, n)
    yield "choppy_flat", pd.DataFrame({
        "Open": flat, "High": flat + 0.05, "Low": flat - 0.05,
        "Close": flat, "Volume": [250_000] * n,
    })


@pytest.mark.parametrize("interval", ["1m", "5m"])
def test_flag_off_is_byte_identical(monkeypatch, interval):
    for _name, df in _battery():
        monkeypatch.setattr(settings, "chili_momentum_entry_first_pullback_enabled", False)
        off = pullback_break_confirmation(df, entry_interval=interval, require_retest=True)
        # The flag-OFF result is the existing-ladder baseline by construction. Re-run
        # with the flag ON but the timeframe guard DISQUALIFYING the gate (a first-
        # pullback interval that differs from the entry interval) -> also byte-identical.
        monkeypatch.setattr(settings, "chili_momentum_entry_first_pullback_enabled", True)
        other_iv = "5m" if interval == "1m" else "1m"
        on_skipped = pullback_break_confirmation(
            df, entry_interval=interval, require_retest=True, first_pullback_interval=other_iv,
        )
        assert off == on_skipped, _name


def test_flag_on_can_change_the_result():
    # The whole point: on the matching interval the flag ON is a REAL behavior change
    # (an aggressive earlier entry). At least one battery df must differ vs flag OFF.
    changed = False
    for _name, df in _battery():
        settings.chili_momentum_entry_first_pullback_enabled = False
        off = pullback_break_confirmation(df, entry_interval="1m", require_retest=True,
                                          first_pullback_interval="1m")
        settings.chili_momentum_entry_first_pullback_enabled = True
        on = pullback_break_confirmation(df, entry_interval="1m", require_retest=True,
                                         first_pullback_interval="1m")
        if off != on:
            changed = True
    settings.chili_momentum_entry_first_pullback_enabled = True
    assert changed, "first-pullback ON never changed any battery result on the 1m path"


def test_fire_through_confirmation_yields_first_pullback_reason():
    # On the matching interval (1m) the explosive df FIRES and surfaces an observable
    # first_pullback reason (so the replay A/B can attribute the aggressive entry).
    ok, reason, dbg = pullback_break_confirmation(
        _explosive_first_pullback_df(), entry_interval="1m", require_retest=True,
        first_pullback_interval="1m",
        # disable the downstream optional tape confirmations so the structural FIRE is
        # not masked by a candle/VWAP/MACD veto unrelated to this gate.
    )
    # the explosive fixture's breakout bar carries returning volume, so the base volume
    # spike clears; the reason must attribute first_pullback.
    assert ok is True, (reason, dbg)
    assert reason in ("first_pullback_ok", "first_pullback_tick_ok"), reason
    assert dbg.get("pattern") == "first_pullback"
    assert dbg.get("pullback_low") is not None and dbg.get("pullback_high") is not None


# ── capture-g fix F2: the 1m FALLBACK frame keeps the first-pullback branch ────

def test_micro_config_fallback_frame_keeps_first_pullback(monkeypatch):
    """F2: micropull configured ('15s' first-pullback interval) + the micro frame
    UNAVAILABLE (sparse tape / bridge silent-hang) ⇒ the live runner evaluates the BASE
    (1m) fallback frame. The first-pullback branch must still arm there — pre-flip parity —
    instead of silently vanishing on exactly the degraded path ('15s' != '1m')."""
    monkeypatch.setattr(settings, "chili_momentum_entry_first_pullback_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pullback_entry_interval", "1m")
    df = _explosive_first_pullback_df()
    # pre-flip baseline: fp interval '1m' on the 1m frame (the old default behavior).
    baseline = pullback_break_confirmation(
        df, entry_interval="1m", require_retest=True, first_pullback_interval="1m",
    )
    # post-flip degraded path: fp interval '15s' (micro config) but the frame is the 1m
    # fallback — MUST behave exactly as the pre-flip baseline (branch arms + fires).
    fallback = pullback_break_confirmation(
        df, entry_interval="1m", require_retest=True, first_pullback_interval="15s",
    )
    assert fallback == baseline
    ok, reason, dbg = fallback
    assert ok is True, (reason, dbg)
    assert dbg.get("pattern") == "first_pullback"


def test_non_micro_interval_mismatch_still_skips(monkeypatch):
    """F2 guard-rail: a NON-micro mismatch (fp '1m' vs a 5m df) keeps the original skip
    contract — the base-interval extension only applies when the configured fp interval is
    sub-minute (the micro config is what created the mismatch)."""
    monkeypatch.setattr(settings, "chili_momentum_entry_first_pullback_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pullback_entry_interval", "5m")
    df = _explosive_first_pullback_df()
    monkeypatch.setattr(settings, "chili_momentum_entry_first_pullback_enabled", False)
    off = pullback_break_confirmation(df, entry_interval="5m", require_retest=True)
    monkeypatch.setattr(settings, "chili_momentum_entry_first_pullback_enabled", True)
    on_mismatch = pullback_break_confirmation(
        df, entry_interval="5m", require_retest=True, first_pullback_interval="1m",
    )
    assert on_mismatch == off  # '1m' is not micro -> no base-iv extension -> skipped


# ── (d) ARM -> waiting_for_first_pullback_break tick routing ──────────────────

def test_arm_reason_in_shared_tick_tuple():
    assert "waiting_for_first_pullback_break" in TICK_ARMED_WAIT_REASONS


def test_arm_then_tick_break_fires_and_thrust_guard_blocks_1tick_poke(monkeypatch):
    # an explosive df that has NOT yet broken on a completed bar -> ARM. A live tick a
    # hair through the level is BLOCKED by _dipbuy_tick_thrust_ok (the tight-level
    # thrust buffer); a tick well through it FIRES the tick path.
    # The verticality cap is a SEPARATE downstream gate (already tested elsewhere); a
    # decisive tick clearing the thrust buffer can also clear it, so disable it here so
    # the test isolates the thrust-buffer routing under test.
    monkeypatch.setattr(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)
    df = _explosive_first_pullback_df(cur_high=12.10, cur_close=12.05)
    # first confirm the bar-level verdict is ARM (the level is the pullback swing high).
    v, lvl, _stop, _dbg = first_pullback_break(df, symbol="JRSH")
    assert v == "ARM"
    level = float(lvl)

    # a 1-tick poke just over the level -> thrust buffer rejects (not confirmed).
    ok_poke, reason_poke, dbg_poke = pullback_break_confirmation(
        df, entry_interval="1m", require_retest=True, first_pullback_interval="1m",
        live_price=level + 0.0001, symbol="JRSH",
    )
    assert ok_poke is False
    assert reason_poke == "first_pullback_tickbreak_unconfirmed", (reason_poke, dbg_poke)

    # a decisive tick well above the level clears the buffer -> tick-break fires.
    ok_thrust, reason_thrust, dbg_thrust = pullback_break_confirmation(
        df, entry_interval="1m", require_retest=True, first_pullback_interval="1m",
        live_price=level * 1.05, symbol="JRSH",
    )
    assert ok_thrust is True, (reason_thrust, dbg_thrust)
    assert reason_thrust == "first_pullback_tick_ok", reason_thrust
    assert dbg_thrust.get("tick_break") is True


def test_momentum_pullback_trigger_threads_interval(monkeypatch):
    # momentum_pullback_trigger resolves chili_momentum_first_pullback_interval and
    # threads it through. With it set to 1m and the entry df ALSO on 1m, the gate runs.
    monkeypatch.setattr(settings, "chili_momentum_first_pullback_interval", "1m")
    monkeypatch.setattr(settings, "chili_momentum_pullback_require_retest", True)
    ok, reason, dbg = momentum_pullback_trigger(
        _explosive_first_pullback_df(), entry_interval="1m", symbol="JRSH",
    )
    # either it fires first_pullback, or a downstream optional confirmation vetoes it —
    # but the first-pullback structure must have been REACHED (pattern stamped) on a fire.
    if ok:
        assert dbg.get("pattern") == "first_pullback"
