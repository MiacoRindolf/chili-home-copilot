"""Adaptive order-flow EXHAUSTION LOCK (crypto runner) — unit tests for the pure
helper ``ofi_exhaustion_lock``.

Covers the red-team-mandated invariants:
  * never-loosen-the-floor (Invariant A): the returned stop is ALWAYS
    ``>= max(current_stop, breakeven_floor)`` for any input (incl NaN/negative
    bps, hwm < entry).
  * false-exhaustion: below the profit-arm, the lock is inert (the trail/stop
    owns healthy pullbacks).
  * graceful degradation: ``(None, None)`` signals -> no-op.
  * adaptivity: lock tightness scales with move strength + flow magnitude, and
    is clamped no looser than the cushion band already is.
  * the A/B counterfactual (fixed-R:R stop, lock OFF) is always present.

The crypto-scoping + equity-byte-identical + no-double-exit behavior is enforced
at the live_runner CALL SITE (gated on ``-USD`` + ``not scale_limit_order_id``)
and is asserted by the live-runner parity tests; the helper itself is pure and
side-effect-free, so these are direct value assertions.
"""

from __future__ import annotations

import math

import pytest

from app.services.trading.momentum_neural.paper_execution import ofi_exhaustion_lock


# entry 1.00, atr 0.02, mult 0.60 -> risk_dist = 0.012 (1.2% of price).
# With reward_risk=3.0 (crypto) and arm_frac default 0.5 -> arm_r = 1.5R.
_ENTRY = 1.00
_ATR = 0.02
_MULT = 0.60
_RD = _ENTRY * max(0.003, _ATR * _MULT)  # 0.012


def _call(**over):
    kw = dict(
        high_water_mark=_ENTRY + 2.0 * _RD,   # +2.0R peak (armed: >= 1.5R)
        entry_price=_ENTRY,
        bid=(_ENTRY + 2.0 * _RD) - 0.6 * _RD,  # 0.6R giveback (> arm corroborant)
        atr_pct=_ATR,
        stop_atr_mult=_MULT,
        ofi=-0.8,
        micro_edge=-30.0,
        hidden_seller=None,
        reward_risk=3.0,
        current_stop=0.99,
        breakeven_floor=1.00,
        current_band_bps=800.0,
        side_long=True,
    )
    kw.update(over)
    return ofi_exhaustion_lock(**kw)


# ---------------------------------------------------------------- fire path ---

def test_confluence_fires_and_tightens_above_breakeven() -> None:
    r = _call()
    assert r["fired"] is True
    assert r["trigger"] == "ofi_micro_confluence"
    # ratchet only ever raises and never below the breakeven floor
    assert r["new_stop_floor"] >= 1.00
    # the lock is tighter than the cushion band counterfactual it replaces
    assert r["new_stop_floor"] > r["counterfactual_fixed_stop"]


def test_counterfactual_is_the_fixed_band_stop_lock_off() -> None:
    # band 800bps below hwm, floored at BE -> that is the fixed-R:R baseline.
    hwm = _ENTRY + 2.0 * _RD
    r = _call(current_band_bps=800.0, breakeven_floor=1.00, current_stop=0.99)
    expected_band = hwm * (1.0 - 800.0 / 10_000.0)
    assert abs(r["counterfactual_fixed_stop"] - max(0.99, 1.00, expected_band)) < 1e-9


# --------------------------------------------------------- false exhaustion ---

def test_below_profit_arm_is_inert() -> None:
    # +1.0R peak is below the 1.5R arm -> never fires even with hard reversed flow
    hwm = _ENTRY + 1.0 * _RD
    r = _call(high_water_mark=hwm, bid=hwm - 0.6 * _RD)
    assert r["armed"] is False
    assert r["fired"] is False
    assert r["new_stop_floor"] == 0.99  # untouched


def test_brief_ofi_dip_without_micro_rollover_does_not_fire() -> None:
    # OFI flipped negative but micro-price still above mid (book hasn't turned)
    r = _call(ofi=-0.8, micro_edge=+5.0)
    assert r["armed"] is True
    assert r["fired"] is False


def test_giveback_corroborant_required() -> None:
    # decisive flow but price still pinned at the high (no give-back) -> no fire
    hwm = _ENTRY + 2.0 * _RD
    r = _call(high_water_mark=hwm, bid=hwm)  # zero give-back
    assert r["fired"] is False


# ------------------------------------------------ graceful degradation ---

def test_none_signals_is_noop() -> None:
    r = _call(ofi=None, micro_edge=None, hidden_seller=None)
    assert r["fired"] is False
    assert r["new_stop_floor"] == 0.99
    # the counterfactual is still computed (telemetry never goes blind)
    assert math.isfinite(r["counterfactual_fixed_stop"])


def test_nan_signals_is_noop() -> None:
    r = _call(ofi=float("nan"), micro_edge=float("nan"))
    assert r["fired"] is False
    assert r["new_stop_floor"] == 0.99


# --------------------------------------------- never-loosen-the-floor (A) ---

@pytest.mark.parametrize("band", [float("nan"), -50.0, 0.0, 5.0, 50_000.0])
@pytest.mark.parametrize("cs", [0.5, 0.99, 1.05, 1.20])
def test_ratchet_never_below_current_stop_or_breakeven(band, cs) -> None:
    r = _call(current_stop=cs, breakeven_floor=1.00, current_band_bps=band)
    assert r["new_stop_floor"] >= cs - 1e-12
    # breakeven floor only applies once armed+fired; current_stop floor is absolute
    assert r["new_stop_floor"] >= cs - 1e-12


def test_hwm_below_entry_is_noop() -> None:
    r = _call(high_water_mark=0.5, entry_price=1.0, bid=0.5, current_stop=0.9, breakeven_floor=0.9)
    assert r["fired"] is False
    assert r["new_stop_floor"] == 0.9


def test_negative_and_zero_inputs_never_crash_and_hold_floor() -> None:
    for bad in (
        dict(entry_price=0.0),
        dict(entry_price=-1.0),
        dict(atr_pct=0.0, stop_atr_mult=0.0),
        dict(high_water_mark=float("nan")),
        dict(bid=float("nan")),
        dict(reward_risk=0.0),
        dict(reward_risk=float("nan")),
    ):
        r = _call(current_stop=1.05, breakeven_floor=1.00, **bad)
        assert r["new_stop_floor"] >= 1.05 - 1e-9, bad


def test_short_side_is_noop() -> None:
    r = _call(side_long=False)
    assert r["fired"] is False
    assert r["partial_arm"] is False


# ------------------------------------------------------------- adaptivity ---

def test_harder_flow_locks_tighter() -> None:
    weak = _call(ofi=-0.30, micro_edge=-5.0)
    strong = _call(ofi=-0.95, micro_edge=-40.0)
    assert weak["fired"] and strong["fired"]
    # tighter lock = SMALLER bps = stop closer to the high-water mark
    assert strong["lock_bps"] <= weak["lock_bps"]
    assert strong["new_stop_floor"] >= weak["new_stop_floor"]


def test_lock_never_looser_than_cushion_band() -> None:
    # a very tight band must clamp the lock no looser than itself
    hwm = _ENTRY + 2.0 * _RD
    r = _call(current_band_bps=30.0)
    band_stop = hwm * (1.0 - 30.0 / 10_000.0)
    # lock stop is >= the band stop (equal or tighter), never below it
    assert r["new_stop_floor"] >= band_stop - 1e-9


def test_arm_derives_from_reward_risk_not_fixed() -> None:
    # rr=2.0 (equity-style) -> arm_r = 0.5*2 = 1.0R; +1.2R peak now ARMS,
    # whereas under rr=3.0 (arm 1.5R) the same peak would not.
    hwm = _ENTRY + 1.2 * _RD
    armed_rr2 = _call(high_water_mark=hwm, bid=hwm - 0.6 * _RD, reward_risk=2.0)
    armed_rr3 = _call(high_water_mark=hwm, bid=hwm - 0.6 * _RD, reward_risk=3.0)
    assert armed_rr2["armed"] is True
    assert armed_rr3["armed"] is False


# ------------------------------------------------------------- Action B ---

def test_strong_flow_arms_partial_weak_does_not() -> None:
    strong = _call(ofi=-0.8, micro_edge=-30.0)   # ofi < -2*thr (-0.5)
    weak = _call(ofi=-0.30, micro_edge=-5.0)      # ofi only just past -thr
    assert strong["partial_arm"] is True
    assert weak["partial_arm"] is False


def test_hidden_seller_accelerant_off_by_default() -> None:
    # absorption alone (no OFI flip / no giveback) must NOT fire while the
    # hidden-seller flag ships OFF (default).
    hwm = _ENTRY + 2.0 * _RD
    r = ofi_exhaustion_lock(
        high_water_mark=hwm, entry_price=_ENTRY, bid=hwm,  # zero giveback
        atr_pct=_ATR, stop_atr_mult=_MULT,
        ofi=+0.5, micro_edge=-30.0, hidden_seller=5.0,  # strong absorption + micro roll
        reward_risk=3.0, current_stop=0.99, breakeven_floor=1.00,
        current_band_bps=800.0, side_long=True,
    )
    assert r["fired"] is False  # accelerant gated behind its flag


def test_hidden_seller_accelerant_fires_when_enabled(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_exit_ofi_hidden_seller_enabled", True, raising=False
    )
    hwm = _ENTRY + 2.0 * _RD
    r = ofi_exhaustion_lock(
        high_water_mark=hwm, entry_price=_ENTRY, bid=hwm,  # zero giveback (OR-bypassed)
        atr_pct=_ATR, stop_atr_mult=_MULT,
        ofi=+0.5, micro_edge=-30.0, hidden_seller=5.0,  # OFI still POSITIVE (leading)
        reward_risk=3.0, current_stop=0.99, breakeven_floor=1.00,
        current_band_bps=800.0, side_long=True,
    )
    assert r["fired"] is True
    assert r["trigger"] == "absorption"
    assert r["partial_arm"] is True  # absorption is high-conviction
    assert r["new_stop_floor"] >= 1.00


def test_hidden_seller_accelerant_still_requires_profit_arm(monkeypatch) -> None:
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_exit_ofi_hidden_seller_enabled", True, raising=False
    )
    # below the profit arm, even strong absorption must not fire (the one gate
    # that prevents a loss-side sell).
    hwm = _ENTRY + 1.0 * _RD  # +1.0R < 1.5R arm
    r = ofi_exhaustion_lock(
        high_water_mark=hwm, entry_price=_ENTRY, bid=hwm,
        atr_pct=_ATR, stop_atr_mult=_MULT,
        ofi=+0.5, micro_edge=-30.0, hidden_seller=9.0,
        reward_risk=3.0, current_stop=0.99, breakeven_floor=0.99,
        current_band_bps=800.0, side_long=True,
    )
    assert r["armed"] is False
    assert r["fired"] is False


# ------------------------------------------ 1m candle confirmer (AND-gate) ---
# The candle confirmer can only ever RESTRICT the FLOW confluence; it never
# creates a fire and never loosens a stop (INVARIANT A). Default is observe-
# first: candle_gate_live=False leaves the live decision byte-identical and
# only records the would-suppress A/B.

def test_candle_default_unset_is_byte_identical_fire() -> None:
    # No candle args at all (default None/False) -> exactly the legacy fire.
    r = _call()
    assert r["fired"] is True
    assert r["candle_ok"] is True            # fail-open
    assert r["candle_exhaustion"] is None
    assert r["candle_gate_live"] is False
    assert r["candle_would_suppress"] is False


def test_candle_observe_first_does_not_change_live_fire() -> None:
    # candle says NO exhaustion but the gate is NOT live -> the lock still fires
    # (byte-identical) and only flags the would-suppress counterfactual.
    base = _call()
    r = _call(candle_exhaustion=False, candle_gate_live=False)
    assert r["fired"] is True
    assert r["new_stop_floor"] == base["new_stop_floor"]   # decision unchanged
    assert r["candle_ok"] is False
    assert r["candle_would_suppress"] is True              # the A/B is recorded


def test_candle_gate_live_suppresses_when_candle_says_no() -> None:
    r = _call(candle_exhaustion=False, candle_gate_live=True, current_stop=0.99)
    assert r["fired"] is False
    assert r["candle_would_suppress"] is True
    # INVARIANT A: suppression NEVER loosens the stop (held at current_stop).
    assert r["new_stop_floor"] == 0.99
    assert r["partial_arm"] is False


def test_candle_gate_live_preserves_capture_when_candle_confirms() -> None:
    base = _call()
    r = _call(candle_exhaustion=True, candle_gate_live=True)
    assert r["fired"] is True
    assert r["candle_ok"] is True
    assert r["candle_would_suppress"] is False
    assert r["new_stop_floor"] == base["new_stop_floor"]   # full capture preserved


def test_candle_gate_fail_open_on_none_even_when_live() -> None:
    # Missing 1m df (None) must never restrict -> the lock behaves as today.
    base = _call()
    r = _call(candle_exhaustion=None, candle_gate_live=True)
    assert r["fired"] is True
    assert r["candle_ok"] is True
    assert r["candle_would_suppress"] is False
    assert r["new_stop_floor"] == base["new_stop_floor"]


def test_candle_gate_never_creates_a_fire() -> None:
    # The flow confluence does NOT fire (micro still positive); a confirming
    # candle must NOT manufacture a fire — the gate only ever restricts.
    r = _call(micro_edge=+5.0, candle_exhaustion=True, candle_gate_live=True)
    assert r["fired"] is False
    assert r["candle_would_suppress"] is False  # nothing to suppress (didn't fire)


def test_candle_gate_does_not_block_absorption(monkeypatch) -> None:
    # The absorption OR-bypass (leading distribution signal) is intentionally
    # NOT candle-gated: it fires even with candle_exhaustion=False + gate live.
    from app.config import settings

    monkeypatch.setattr(
        settings, "chili_momentum_exit_ofi_hidden_seller_enabled", True, raising=False
    )
    hwm = _ENTRY + 2.0 * _RD
    r = ofi_exhaustion_lock(
        high_water_mark=hwm, entry_price=_ENTRY, bid=hwm,  # zero giveback (OR-bypass)
        atr_pct=_ATR, stop_atr_mult=_MULT,
        ofi=+0.5, micro_edge=-30.0, hidden_seller=5.0,
        reward_risk=3.0, current_stop=0.99, breakeven_floor=1.00,
        current_band_bps=800.0,
        candle_exhaustion=False, candle_gate_live=True, side_long=True,
    )
    assert r["fired"] is True
    assert r["trigger"] == "absorption"
    assert r["candle_would_suppress"] is False  # absorption isn't a pure-confluence fire
