"""PRINCIPAL-LEVEL edge-case audit: FAIL-OPEN vs FAIL-CLOSED CONSISTENCY of the
Ross momentum-lane gates  ([failmode-consistency] class).

THE RULE this file enforces (an INCONSISTENCY is the bug we hunt):

  * A SAFETY-CRITICAL gate (the decision risks money on the ENTRY/ADD side) must
    FAIL-CLOSED on missing / None / garbage driving signal — no fire, or a smaller
    size.  e.g. ``tape_confirms_hold`` (REQUIRED tape confirmer), ``add_into_halt_ok``
    (the loss-sensitive halt-add) — a missing input must NEVER ADMIT a worse entry.

  * A RISK-REDUCING tilt / SIZE DERATE may FAIL-OPEN (permit / neutral 1.0) on a
    missing signal — it exists only to SHRINK or DEFER, so a missing input that
    falls back to "no shrink / keep waiting" is the SAFE direction AND avoids the
    documented 0-fills trap (a risk-reducer that fails into a HARD BLOCK would
    starve the lane).  e.g. ``spread_cost_veto`` derate, the catalyst / green-day /
    cushion size multipliers (missing => neutral 1.0, never a hard block).

Each test feeds a gate a MISSING / None / garbage version of its driving signal and
asserts the DOCUMENTED failmode direction is honoured AND is CONSISTENT with the
gate's safety role.  Tests are PURE-LOGIC: a fake ``settings`` object, a fake ``db``
whose ``.execute`` raises (so any real DB read fails -> exercises the error/empty
fail path), and synthetic OHLCV frames.  No ``db`` fixture, no truncate, fast.

If a test exposes a LIKELY SOURCE BUG it is written to FAIL (and flagged in the run
report) rather than asserting the buggy behaviour as "correct".
"""
from __future__ import annotations

import types
from unittest.mock import patch

import pandas as pd
import pytest

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import spread_cost_veto as scv
from app.services.trading.momentum_neural import risk_policy as rp
from app.services.trading.momentum_neural import catalyst as cat


# ──────────────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────────────
def _S(**overrides):
    """A bare settings stand-in.  getattr(default) is honoured for any key NOT set,
    so passing nothing yields the in-code defaults; pass only what a test pins."""
    ns = types.SimpleNamespace()
    for k, v in overrides.items():
        setattr(ns, k, v)
    return ns


class _RaisingDB:
    """A db whose every .execute() raises — forces the tape/percentile reads down
    their except->fail path so we exercise the documented error failmode."""

    def execute(self, *a, **k):  # noqa: D401
        raise RuntimeError("synthetic db failure")


class _EmptyDB:
    """A db that returns an empty result set (no rows) from .execute()."""

    class _R:
        def fetchall(self):
            return []

        def fetchone(self):
            return None

    def execute(self, *a, **k):
        return self._R()


def _rising_frame(n: int = 30, base: float = 10.0, step: float = 0.05) -> pd.DataFrame:
    """A clean front-side rising session frame (Open<Close, higher highs/lows, vol>0)."""
    idx = pd.date_range("2026-06-26 13:30", periods=n, freq="1min", tz="UTC")
    rows = []
    for i in range(n):
        o = base + step * i
        c = o + step * 0.6
        h = c + step * 0.2
        l = o - step * 0.2
        rows.append((o, h, l, c, 10_000 + 100 * i))
    return pd.DataFrame(rows, index=idx, columns=["Open", "High", "Low", "Close", "Volume"])


# ==============================================================================
# 1. tape_confirms_hold  — SAFETY-CRITICAL (REQUIRED tape confirmer) => FAIL-CLOSED
#    Missing symbol / db / empty-or-thin tape / garbage / error  =>  (False, ...)
#    A missing tape must NEVER produce an early fire.
# ==============================================================================
class TestTapeConfirmsHoldFailsClosed:
    def test_pattern_gate_rollback_flag_no_fire_and_no_io(self):
        # CAPTURE-G1(b): the ROLLBACK kill-switch chili_momentum_pattern_tape_gate_enabled=False
        # must short-circuit BEFORE any I/O (a raising db must not even be hit) — the legacy
        # hard-False (dark) behavior for a one-flag revert.
        ok, dbg = eg.tape_confirms_hold(
            "AAA", db=_RaisingDB(),
            settings=_S(chili_momentum_pattern_tape_gate_enabled=False),
        )
        assert ok is False
        assert dbg.get("reason") == "tape_hold_disabled"

    def test_decouple_early_fire_flag_off_still_evaluates_tape(self):
        # CAPTURE-G1(b) DECOUPLE PROOF: the FIX-C early-fire flag
        # chili_momentum_tape_hold_entry_enabled being OFF must NO LONGER disable the inline
        # pattern-trigger tape gate — with the (default-on) pattern gate, a genuinely lifting
        # tape CONFIRMS even though the early-fire flag is OFF. This is what un-darks the 12
        # tape-required pattern triggers in production.
        feats = {"signed_tape_accel": 0.8, "tick_rate": 9.0, "tick_rate_floor": 1.0, "n_ticks": 25}
        with patch.object(eg, "signed_tape_accel_features", return_value=feats):
            ok, dbg = eg.tape_confirms_hold(
                "AAA", db=_EmptyDB(),
                settings=_S(chili_momentum_tape_hold_entry_enabled=False),
            )
        assert ok is True
        assert dbg["reason"] == "tape_hold_confirmed"

    def test_missing_symbol_fails_closed(self):
        ok, _ = eg.tape_confirms_hold(
            None, db=_EmptyDB(),
            settings=_S(chili_momentum_tape_hold_entry_enabled=True),
        )
        assert ok is False  # no symbol -> NO early fire

    def test_missing_db_fails_closed(self):
        ok, _ = eg.tape_confirms_hold(
            "AAA", db=None,
            settings=_S(chili_momentum_tape_hold_entry_enabled=True),
        )
        assert ok is False

    def test_db_error_fails_closed(self):
        # A raising db -> signed_tape_accel_features returns None -> fail CLOSED.
        ok, dbg = eg.tape_confirms_hold(
            "AAA", db=_RaisingDB(),
            settings=_S(chili_momentum_tape_hold_entry_enabled=True),
        )
        assert ok is False
        assert dbg["reason"] in ("tape_hold_no_data", "tape_hold_error")

    def test_empty_tape_none_fails_closed(self):
        with patch.object(eg, "signed_tape_accel_features", return_value=None):
            ok, _ = eg.tape_confirms_hold(
                "AAA", db=_EmptyDB(),
                settings=_S(chili_momentum_tape_hold_entry_enabled=True),
            )
        assert ok is False

    def test_garbage_zero_accel_fails_closed(self):
        # accel == 0.0 is NOT > 0.0 -> not confirmed.  This is the boundary: a flat
        # tape (no net buying) must NOT manufacture a hold.
        feats = {"signed_tape_accel": 0.0, "tick_rate": 5.0, "tick_rate_floor": 1.0, "n_ticks": 9}
        with patch.object(eg, "signed_tape_accel_features", return_value=feats):
            ok, dbg = eg.tape_confirms_hold(
                "AAA", db=_EmptyDB(),
                settings=_S(chili_momentum_tape_hold_entry_enabled=True),
            )
        assert ok is False
        assert dbg["reason"] == "tape_hold_not_confirmed"

    def test_thin_tick_rate_below_floor_fails_closed(self):
        # Positive accel but tick_rate BELOW its own floor -> not active enough -> no fire.
        feats = {"signed_tape_accel": 0.5, "tick_rate": 0.5, "tick_rate_floor": 2.0, "n_ticks": 3}
        with patch.object(eg, "signed_tape_accel_features", return_value=feats):
            ok, dbg = eg.tape_confirms_hold(
                "AAA", db=_EmptyDB(),
                settings=_S(chili_momentum_tape_hold_entry_enabled=True),
            )
        assert ok is False
        assert dbg["reason"] == "tape_hold_not_confirmed"

    def test_nan_accel_does_not_confirm(self):
        # GARBAGE: a NaN accel must not pass the `accel > 0.0` test (NaN comparisons
        # are False) -> fail CLOSED.  Guards against a NaN sneaking an early fire.
        feats = {"signed_tape_accel": float("nan"), "tick_rate": 9.0, "tick_rate_floor": 1.0, "n_ticks": 20}
        with patch.object(eg, "signed_tape_accel_features", return_value=feats):
            ok, _ = eg.tape_confirms_hold(
                "AAA", db=_EmptyDB(),
                settings=_S(chili_momentum_tape_hold_entry_enabled=True),
            )
        assert ok is False

    def test_genuine_confirmation_fires(self):
        # The POSITIVE control: a genuinely lifting tape (accel>0 AND active) DOES fire,
        # so the fail-closed asserts above are not just "always False".
        feats = {"signed_tape_accel": 0.8, "tick_rate": 9.0, "tick_rate_floor": 1.0, "n_ticks": 25}
        with patch.object(eg, "signed_tape_accel_features", return_value=feats):
            ok, dbg = eg.tape_confirms_hold(
                "AAA", db=_EmptyDB(),
                settings=_S(chili_momentum_tape_hold_entry_enabled=True),
            )
        assert ok is True
        assert dbg["reason"] == "tape_hold_confirmed"


# ==============================================================================
# CAPTURE-G1(b) — the 12 tape-required pattern triggers become REACHABLE.
#   Every one of these triggers uses tape_confirms_hold as its fail-closed LAST gate.
#   Before the decouple, chili_momentum_tape_hold_entry_enabled=False (the deployed
#   default) hard-Falsed that gate ⇒ all 12 could NEVER fire live. After the decouple
#   the gate keys on TAPE AVAILABILITY: dense+healthy tape ⇒ confirmed (the trigger is
#   REACHABLE, still tape-gated); missing/thin/stale ⇒ the fail-CLOSED refusal STANDS.
# ==============================================================================
class TestTwelveTriggersReachableAfterDecouple:
    # The 12 triggers whose fail-closed LAST gate is tape_confirms_hold (doc/audit list).
    TWELVE = (
        "bull_flag_confirmation", "wedge_break_entry", "absorption_snap_entry",
        "false_break_reclaim_confirmation", "ask_thins_dip_entry", "sub_vwap_trap_entry",
        "pulling_away_roc_entry", "premarket_pivot_macd_entry",
        "inverse_head_shoulders_confirmation", "cup_and_handle_confirmation",
        "bottom_reversal_confirmation", "momentum_continuation",  # + the continuation entry
    )

    def test_all_named_triggers_exist(self):
        # The decoupled gate is the shared dependency of these triggers; guard the list
        # against a rename silently dropping a trigger off the tape gate.
        callables = {
            "bull_flag_confirmation", "wedge_break_entry", "absorption_snap_entry",
            "false_break_reclaim_confirmation", "ask_thins_dip_entry", "sub_vwap_trap_entry",
            "pulling_away_roc_entry", "premarket_pivot_macd_entry",
            "inverse_head_shoulders_confirmation", "cup_and_handle_confirmation",
            "bottom_reversal_confirmation", "momentum_continuation_trigger",
        }
        for name in callables:
            assert hasattr(eg, name), f"trigger {name} missing (tape-gate dependency changed?)"

    def test_dense_healthy_tape_confirms_regardless_of_early_fire_flag(self):
        # With DENSE healthy tape the shared gate CONFIRMS whether the FIX-C early-fire flag
        # is ON or OFF — proving the 12 triggers are reachable independent of that flag.
        feats = {"signed_tape_accel": 0.9, "tick_rate": 12.0, "tick_rate_floor": 1.0, "n_ticks": 40}
        for early_flag in (True, False):
            with patch.object(eg, "signed_tape_accel_features", return_value=feats):
                ok, dbg = eg.tape_confirms_hold(
                    "AAA", db=_EmptyDB(),
                    settings=_S(chili_momentum_tape_hold_entry_enabled=early_flag),
                )
            assert ok is True, f"dense tape must confirm (early_flag={early_flag})"
            assert dbg["reason"] == "tape_hold_confirmed"

    def test_thin_or_missing_tape_still_refuses(self):
        # The fail-CLOSED floor is UNCHANGED: a missing/thin tape (features None) refuses,
        # so a name with no buyers on tape can never fire — the discipline is preserved.
        with patch.object(eg, "signed_tape_accel_features", return_value=None):
            ok, dbg = eg.tape_confirms_hold(
                "AAA", db=_EmptyDB(),
                settings=_S(chili_momentum_tape_hold_entry_enabled=False),
            )
        assert ok is False
        assert dbg["reason"] == "tape_hold_no_data"

    def test_rollback_flag_darkens_all_twelve(self):
        # The one-flag rollback: pattern_tape_gate_enabled=False restores the hard-False
        # (dark) gate for ALL dependent triggers, even on a genuinely lifting tape.
        feats = {"signed_tape_accel": 0.9, "tick_rate": 12.0, "tick_rate_floor": 1.0, "n_ticks": 40}
        with patch.object(eg, "signed_tape_accel_features", return_value=feats):
            ok, dbg = eg.tape_confirms_hold(
                "AAA", db=_EmptyDB(),
                settings=_S(chili_momentum_pattern_tape_gate_enabled=False),
            )
        assert ok is False
        assert dbg["reason"] == "tape_hold_disabled"


# ==============================================================================
# 2. tape_confirmed_hold_trigger — STRUCTURE side of the early fire; FAIL-CLOSED.
#    A thin/degenerate frame / missing level / broken structure / backside-read
#    error => ok=False (keep waiting on the break).  Must NEVER fire early on an
#    extended/faded/rolled-over name.
# ==============================================================================
class TestTapeConfirmedHoldTriggerFailsClosed:
    def test_thin_frame_fails_closed(self):
        df = _rising_frame(n=5)  # < 10 bars
        ok, reason, dbg = eg.tape_confirmed_hold_trigger(
            df, pullback_high=10.5, pullback_low=10.0, live_price=10.6,
        )
        assert ok is False
        assert dbg["reason"] == "insufficient_bars"

    def test_missing_structural_low_fails_closed(self):
        df = _rising_frame(n=20)
        ok, _, dbg = eg.tape_confirmed_hold_trigger(
            df, pullback_high=11.0, pullback_low=None, live_price=11.0,
        )
        assert ok is False
        assert dbg["reason"] == "no_structural_low"

    def test_price_broke_structural_low_fails_closed(self):
        df = _rising_frame(n=20)
        # live_price WELL below the pullback low -> structure broke -> no early fire.
        ok, _, dbg = eg.tape_confirmed_hold_trigger(
            df, pullback_high=99.0, pullback_low=98.0, live_price=10.0,
        )
        assert ok is False
        assert dbg["reason"] == "broke_structural_low"

    def test_backside_read_exception_fails_closed(self):
        # If the backside read RAISES, the new (loss-sensitive) early-fire path must
        # fail CLOSED — we cannot prove the name is front-side, so do not enter early.
        df = _rising_frame(n=20)
        last_close = float(df["Close"].iloc[-1])
        real = eg.compute_all_from_df

        calls = {"n": 0}

        def _flaky(frame, *, needed):
            calls["n"] += 1
            # First call (ema_9/atr for the EMA-band) succeeds; the SECOND call (the
            # ema_9/ema_20/macd backside arrays) raises -> exercise the backside except.
            if needed == {"ema_9", "ema_20", "macd", "macd_signal"}:
                raise RuntimeError("synthetic backside-array failure")
            return real(frame, needed=needed)

        with patch.object(eg, "compute_all_from_df", side_effect=_flaky):
            ok, _, dbg = eg.tape_confirmed_hold_trigger(
                df, pullback_high=last_close + 5.0, pullback_low=last_close - 5.0,
                live_price=last_close,
            )
        assert ok is False
        assert dbg["reason"] == "backside_read_error"


# ==============================================================================
# 3. _entry_extension_veto — CHASE-GUARD (entry-side, safety-critical).
#    Behaviour: it is ADDITIVE and still FAILS OPEN on a bad/non-positive level or
#    any internal error ("never block an entry on a bug"), BUT a missing atr_pct now
#    FAILS SAFE — the cap collapses to the FLAT extension floor instead of disarming.
#
#    ✅ RESOLVED (HIGH-2 fix): the prior CONSISTENCY TENSION — a chase-guard that
#    fail-OPENED (admitted the entry) when atr_pct was MISSING — is fixed. A missing
#    ATR no longer disarms a safety-critical signal; it falls back to the floor cap
#    (max(floor, K*0) = floor) so a blow-off chase still VETOES. The tests below pin
#    the new fail-SAFE direction.
# ==============================================================================
class TestEntryExtensionVetoFailOpen:
    def test_blocks_a_real_chase(self):
        # POSITIVE control: a +20% extension over the break with a normal ATR must VETO.
        s = _S(chili_momentum_entry_extension_veto_enabled=True,
               chili_momentum_entry_extension_atr_mult=8.0,
               chili_momentum_entry_extension_floor_pct=0.08)
        assert eg._entry_extension_veto(12.0, 10.0, 0.015, s) is True  # +20% vs cap ~0.12

    def test_within_cap_does_not_veto(self):
        s = _S(chili_momentum_entry_extension_veto_enabled=True,
               chili_momentum_entry_extension_atr_mult=8.0,
               chili_momentum_entry_extension_floor_pct=0.08)
        assert eg._entry_extension_veto(10.5, 10.0, 0.015, s) is False  # +5% < cap

    def test_missing_atr_falls_back_to_floor_and_vetoes(self):
        # FIX HIGH-2: atr_pct None no longer DISARMS the chase-guard. A missing ATR (thin
        # low-float runner with no computable volatility) now falls back to the FLAT extension
        # floor (default 0.08), so a wildly-extended entry still VETOES (fail-SAFE on the entry
        # side) instead of admitting a blatant blow-off chase unguarded.
        s = _S(chili_momentum_entry_extension_veto_enabled=True)
        # entry is +900% over the break — a blatant blow-off — VETOED via the floor cap.
        assert eg._entry_extension_veto(100.0, 10.0, None, s) is True

    def test_garbage_level_fails_open(self):
        s = _S(chili_momentum_entry_extension_veto_enabled=True)
        assert eg._entry_extension_veto(100.0, 0.0, 0.02, s) is False   # non-positive level
        assert eg._entry_extension_veto(0.0, 10.0, 0.02, s) is False    # non-positive entry

    def test_internal_error_fails_open(self):
        # A settings object whose getattr raises inside the try -> the bare except
        # returns False (fail-open).  Garbage atr (string) hits the float() -> except.
        s = _S(chili_momentum_entry_extension_veto_enabled=True)
        assert eg._entry_extension_veto(100.0, 10.0, "not-a-number", s) is False


# ==============================================================================
# 4. add_into_halt_ok — LOSS-SENSITIVE pyramid-into-halt add (the RISKIEST gate).
#    EVERY input missing => FAIL-CLOSED (no add).  This is the canonical
#    fail-closed safety gate; we adversarially probe each leg.
# ==============================================================================
class TestAddIntoHaltFailsClosed:
    BASE = dict(
        avg_entry=10.0, original_stop=9.0, current_stop=9.0, bid=11.5,
        is_limit_up_halt=True, in_rth=True, tape_confirmed=True,
        breakout_level=10.0, atr_pct=0.03, consecutive_halt_up_count=1,
        halt_level=10.0, resumption_open=10.2,
    )

    def _S_on(self, **extra):
        base = dict(
            chili_momentum_add_into_halt_enabled=True,
            chili_momentum_add_into_halt_min_profit_r=1.0,
            chili_momentum_halt_chain_block_count=3,
            chili_momentum_entry_extension_veto_enabled=True,
            chili_momentum_entry_extension_atr_mult=8.0,
            chili_momentum_entry_extension_floor_pct=0.08,
        )
        base.update(extra)
        return _S(**base)

    def test_disabled_no_add(self):
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=_S(chili_momentum_add_into_halt_enabled=False), **self.BASE)
        assert ok is False and reason == "add_into_halt_disabled"

    def test_missing_tape_fails_closed(self):
        # tape_confirmed None (no tape read) => NO add — the REQUIRED tape leg.
        kw = {**self.BASE, "tape_confirmed": None}
        # use a df so we get past the structure gate IF tape passed (it won't).
        kw["df"] = _rising_frame(20)
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_no_tape"

    def test_tape_false_fails_closed(self):
        kw = {**self.BASE, "tape_confirmed": False, "df": _rising_frame(20)}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_no_tape"

    def test_missing_extension_inputs_fails_closed(self):
        # No breakout_level / atr_pct => cannot prove it is not parabolic => no add.
        kw = {**self.BASE, "breakout_level": None, "df": _rising_frame(20)}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_no_extension_inputs"

    def test_extended_add_price_fails_closed(self):
        # bid sits far above the breakout level for the ATR => extension veto fires => no add.
        kw = {**self.BASE, "bid": 13.0, "breakout_level": 10.0, "atr_pct": 0.01, "df": _rising_frame(20)}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_extended"

    def test_missing_structure_df_fails_closed(self):
        kw = {**self.BASE, "df": None}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_no_structure"

    def test_underwater_fails_closed(self):
        # bid below entry => negative profit_r => never add underwater.
        kw = {**self.BASE, "bid": 9.5, "df": _rising_frame(20)}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_insufficient_profit"

    def test_loosened_stop_fails_closed(self):
        kw = {**self.BASE, "current_stop": 8.0, "df": _rising_frame(20)}  # below original 9.0
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_stop_loosened"

    def test_limit_down_halt_fails_closed(self):
        kw = {**self.BASE, "is_limit_up_halt": False}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_not_limit_up"

    def test_missing_halt_level_fails_closed(self):
        # Past tape/extension/structure; a MISSING halt_level => cannot confirm the resume
        # direction => fail-closed.  Needs a front-side df so we reach the halt-context legs.
        kw = {**self.BASE, "halt_level": None, "df": _rising_frame(20)}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_no_halt_signal"

    def test_missing_resumption_fails_closed(self):
        kw = {**self.BASE, "resumption_open": None, "df": _rising_frame(20)}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_no_resumption"

    def test_unfavorable_resume_fails_closed(self):
        # resume below the halt level = false/weak halt => refuse.
        kw = {**self.BASE, "resumption_open": 9.0, "halt_level": 10.0, "df": _rising_frame(20)}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_unfavorable_resumption"

    def test_halt_chain_blocked_fails_closed(self):
        kw = {**self.BASE, "consecutive_halt_up_count": 5, "df": _rising_frame(20)}  # >= block 3
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_halt_chain_blocked"

    def test_garbage_current_stop_fails_closed(self):
        # A non-numeric current_stop must NOT silently pass — it should be caught as a
        # bad stop and refuse (loss-sensitive).  Guards the float(current_stop) except.
        kw = {**self.BASE, "current_stop": "garbage", "df": _rising_frame(20)}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=self._S_on(), **kw)
        assert ok is False and reason == "add_into_halt_bad_stop"

    def test_master_flag_enforces_halt_context_independently(self):
        # CONSISTENCY: the halt-chain / resume legs must self-enforce under the MASTER
        # flag EVEN when the standalone sub-flags are OFF (a sub-flag-OFF lane must not
        # silently fail-OPEN and add into a blow-off).  Here the sub-flags are explicitly
        # OFF but a 5-deep halt-chain must STILL block.
        s = self._S_on(
            chili_momentum_halt_chain_risk_gate_enabled=False,
            chili_momentum_halt_resumption_direction_enabled=False,
            chili_momentum_false_halt_avoid_enabled=False,
        )
        kw = {**self.BASE, "consecutive_halt_up_count": 5, "df": _rising_frame(20)}
        ok, reason, _ = eg.add_into_halt_ok(settings_obj=s, **kw)
        assert ok is False and reason == "add_into_halt_halt_chain_blocked"


# ==============================================================================
# 5. _detect_back_side — a BACKSIDE DETECTOR (returns True=on-the-backside).
#    DOCUMENTED: fails OPEN (returns False / not-backside) on missing/short data,
#    so a thin series can never *veto*.  CONSISTENCY note: this is a *detector
#    primitive*; its callers wrap it fail-CLOSED for loss-sensitive paths (see the
#    add_into_halt and tape_confirmed_hold_trigger except branches above).  Here we
#    pin the primitive's documented fail-open and its positive detection.
# ==============================================================================
class TestDetectBackSidePrimitiveFailsOpen:
    def test_empty_arrays_not_backside(self):
        bs, reason = eg._detect_back_side([], [], [], [], 0)
        assert bs is False and reason == ""

    def test_index_out_of_range_not_backside(self):
        bs, _ = eg._detect_back_side([1.0], [1.0], [0.1], [0.0], 99)
        assert bs is False

    def test_none_values_not_backside(self):
        bs, _ = eg._detect_back_side([None, None], [None, None], [None, None], [None, None], 1)
        assert bs is False

    def test_structural_flip_detected(self):
        # ema9 < ema20 at cur => the dominant structural backside signal => True.
        bs, reason = eg._detect_back_side([9.0, 8.0], [9.0, 9.5], [0.0, 0.0], [0.0, 0.0], 1)
        assert bs is True and reason == "ema9_below_ema20"

    def test_macd_cross_below_detected(self):
        # ema front-side (e9>e20) but MACD crossed below signal within lookback AND still below.
        ema9 = [10.0, 10.1, 10.2]
        ema20 = [9.0, 9.0, 9.0]
        macd = [0.5, 0.4, -0.1]        # was above, now below
        sig = [0.3, 0.45, 0.0]         # crosses: at i=2 macd(-0.1) < sig(0.0), at i=1 macd(0.4)<sig(0.45)
        bs, reason = eg._detect_back_side(ema9, ema20, macd, sig, 2)
        assert bs is True and reason == "macd_crossed_below_signal"


# ==============================================================================
# 6. _doji_trigger_veto — CANDLE-QUALITY veto (entry-side).  DOCUMENTED fail-SAFE:
#    a zero-range / unreadable bar => veto=False (never block on unreadable data).
#    CONSISTENCY note: this is the fail-OPEN direction on a candle-quality gate; we
#    pin it and the positive doji detection, and check the strong-body override.
# ==============================================================================
class TestDojiTriggerVetoFailSafe:
    def test_zero_range_bar_no_veto(self):
        veto, _ = eg._doji_trigger_veto(10.0, 10.0, 10.0, 10.0, atr_pct=0.02, base_body_frac=0.25)
        assert veto is False  # unreadable bar -> never block

    def test_real_doji_vetoes(self):
        # A genuine doji: tiny body in a wide range, RED (close < open) and closing in the
        # lower half -> NOT a strong-bull commitment candle -> veto.  (A green bar closing
        # in its upper half would trip the strong-body override; we avoid that here.)
        veto, dbg = eg._doji_trigger_veto(10.15, 10.5, 9.5, 10.0, atr_pct=0.0, base_body_frac=0.25)
        assert veto is True
        assert dbg["doji_body_frac"] < dbg["doji_threshold"]

    def test_strong_full_body_overrides_veto(self):
        # A green bar closing in the upper half with a small upper wick passes even with a
        # body that is a smallish FRACTION of a tall range (the conviction-candle override).
        veto, _ = eg._doji_trigger_veto(10.0, 11.0, 9.95, 10.92, atr_pct=0.0, base_body_frac=0.25)
        assert veto is False

    def test_garbage_inputs_fail_open(self):
        veto, _ = eg._doji_trigger_veto("x", 1.0, 0.5, 0.9, atr_pct=0.02, base_body_frac=0.25)  # type: ignore[arg-type]
        assert veto is False


# ==============================================================================
# 7. halt-context gates — _dip_buy_in_rth_window (RTH-only dip-buy gate) and
#    halt_chain_risk_gate (size de-rate).  Both DOCUMENTED fail-OPEN on missing
#    clock / on a bug.  CONSISTENCY check: a missing clock must NEVER turn a
#    would-fire into a no-fire (that is the 0-fills trap for a RISK-REDUCING gate).
# ==============================================================================
class TestHaltContextGatesFailOpen:
    def test_rth_gate_disabled_passes(self):
        ok, reason = eg._dip_buy_in_rth_window(
            now=None, bar_ts=None, symbol="AAA",
            settings_obj=_S(chili_momentum_dip_buy_rth_only_enabled=False),
        )
        assert ok is True and reason == "rth_gate_disabled"

    def test_no_clock_fails_open(self):
        # Gate ON, equity name, but NO usable clock => must FAIL OPEN (never block on a miss).
        ok, reason = eg._dip_buy_in_rth_window(
            now=None, bar_ts=None, symbol="AAA",
            settings_obj=_S(chili_momentum_dip_buy_rth_only_enabled=True),
        )
        assert ok is True and reason == "rth_no_clock"

    def test_crypto_exempt_fails_open(self):
        ok, reason = eg._dip_buy_in_rth_window(
            now=pd.Timestamp("2026-06-26 02:00", tz="UTC"), bar_ts=None, symbol="BTC-USD",
            settings_obj=_S(chili_momentum_dip_buy_rth_only_enabled=True),
        )
        assert ok is True and reason == "rth_crypto_exempt"

    def test_garbage_clock_fails_open(self):
        # An unparseable clock must FAIL OPEN (never block on a bug).
        ok, reason = eg._dip_buy_in_rth_window(
            now="not-a-timestamp", bar_ts=None, symbol="AAA",
            settings_obj=_S(chili_momentum_dip_buy_rth_only_enabled=True),
        )
        assert ok is True and reason == "rth_clock_error"

    def test_outside_window_blocks(self):
        # POSITIVE control: a real premarket clock (04:00 ET) with the gate ON DOES block.
        ok, reason = eg._dip_buy_in_rth_window(
            now=pd.Timestamp("2026-06-26 08:00", tz="UTC"),  # 04:00 ET
            bar_ts=None, symbol="AAA",
            settings_obj=_S(chili_momentum_dip_buy_rth_only_enabled=True,
                            chili_momentum_dip_buy_rth_start_hour=9.5,
                            chili_momentum_dip_buy_rth_end_hour=16.0),
        )
        assert ok is False and reason == "rth_only_outside_window"

    def test_halt_chain_gate_missing_count_fails_open_neutral(self):
        # consecutive count None => treated as 0 => NO block, size_mult 1.0 (neutral).
        block, mult, reason, _ = eg.halt_chain_risk_gate(
            consecutive_halt_up_count=None,
            settings_obj=_S(chili_momentum_halt_chain_risk_gate_enabled=True,
                            chili_momentum_halt_chain_block_count=3),
        )
        assert block is False and mult == 1.0

    def test_halt_chain_gate_only_ever_shrinks(self):
        # RISK-REDUCING invariant: size_mult is ALWAYS in [0.5, 1.0] — it can never
        # boost size.  Probe the whole chain range incl. garbage.
        for cnt in (None, 0, 1, 2, 3, 7, -4):
            block, mult, _, _ = eg.halt_chain_risk_gate(
                consecutive_halt_up_count=cnt,
                settings_obj=_S(chili_momentum_halt_chain_risk_gate_enabled=True,
                                chili_momentum_halt_chain_block_count=4),
            )
            assert 0.5 <= mult <= 1.0, f"mult out of [0.5,1.0] for count={cnt}: {mult}"

    def test_halt_band_trapped_fail_open(self):
        # bad inputs (zero risk / non-positive band) => False (no veto).
        assert eg.halt_band_trapped(10.0, 10.0) is False     # zero risk
        assert eg.halt_band_trapped(0.0, -1.0) is False      # bad band/price
        # POSITIVE control: a stop sitting right at the LULD band IS trapped.
        band = eg.luld_down_band(10.0)
        assert eg.halt_band_trapped(10.0, band) is True


# ==============================================================================
# 8. _entry_flow_veto — entry-TIMING DEFER gate (risk-reducing).  DOCUMENTED:
#    fails OPEN (no veto) when the relevant flow is None or the flag is OFF.  A
#    missing live-flow signal must NOT block (it is a defer-when-selling gate, not
#    a REQUIRED confirmer — the REQUIRED confirmer is tape_confirms_hold).
# ==============================================================================
class TestEntryFlowVetoFailOpen:
    S = _S(chili_momentum_entry_flow_veto_enabled=True,
           chili_momentum_entry_flow_veto_ofi=-0.6,
           chili_momentum_entry_flow_veto_trade_flow=-0.25,
           chili_momentum_entry_flow_veto_trade_flow_strong=-0.5)

    def test_both_none_fails_open(self):
        assert eg._entry_flow_veto(None, None, self.S) is False

    def test_flag_off_fails_open(self):
        assert eg._entry_flow_veto(-1.0, -1.0, _S(chili_momentum_entry_flow_veto_enabled=False)) is False

    def test_strong_selling_tape_vetoes(self):
        # POSITIVE control: a strongly-selling executed tape vetoes even with mild +OFI.
        assert eg._entry_flow_veto(0.5, -0.63, self.S) is True

    def test_both_bearish_vetoes(self):
        assert eg._entry_flow_veto(-0.7, -0.3, self.S) is True

    def test_only_ofi_missing_uses_strong_leg(self):
        # OFI None disables the AND-leg, but a strong-selling tape still vetoes via OR-leg.
        assert eg._entry_flow_veto(None, -0.6, self.S) is True
        # ...and a mildly-negative tape with OFI None does NOT veto (AND-leg dead, OR-leg quiet).
        assert eg._entry_flow_veto(None, -0.3, self.S) is False


# ==============================================================================
# 9. _overhead_supply_veto — entry-side veto.  DOCUMENTED fail-OPEN to None on
#    flag-off / no DailyContext / no usable ATR / any error.  CONSISTENCY note:
#    this is the fail-OPEN direction on an entry-side veto, justified as "never
#    over-block a clean breakout on missing daily data".
# ==============================================================================
class TestOverheadSupplyVetoFailOpen:
    def test_flag_off_passes(self):
        with patch.object(eg.settings, "chili_momentum_overhead_veto_enabled", False, create=True):
            assert eg._overhead_supply_veto(object(), entry=10.0) is None

    def test_no_daily_ctx_passes(self):
        with patch.object(eg.settings, "chili_momentum_overhead_veto_enabled", True, create=True):
            assert eg._overhead_supply_veto(None, entry=10.0) is None

    def test_bad_entry_passes(self):
        with patch.object(eg.settings, "chili_momentum_overhead_veto_enabled", True, create=True):
            assert eg._overhead_supply_veto(object(), entry=0.0) is None
            assert eg._overhead_supply_veto(object(), entry=None) is None


# ==============================================================================
# 10. spread_cost_veto.adaptive_spread_cost_veto_derate — SIZE DERATE (NOT a hard
#     block, GLOBALLY derate-only).  DOCUMENTED: fail-OPEN to (True, 1.0, ...) on
#     any unusable basis / thin history / flag OFF; allow is ALWAYS True; the mult
#     is always in [floor, 1.0].  CONSISTENCY: a missing spread/price/R must give
#     NEUTRAL 1.0 (no shrink) — it must never block (the 0-fills trap) AND never
#     return allow=False.
# ==============================================================================
class TestSpreadCostDerateFailOpenNeutral:
    def test_flag_off_pass_through(self):
        allow, mult, reason, _ = scv.adaptive_spread_cost_veto_derate(
            symbol="AAA", entry_price=10.0, current_spread_bps=300.0, stop_distance=0.5,
            db=_RaisingDB(), flag_enabled=False,
        )
        assert allow is True and mult == 1.0 and reason == "flag_off"

    def test_missing_spread_neutral(self):
        allow, mult, reason, _ = scv.adaptive_spread_cost_veto_derate(
            symbol="AAA", entry_price=10.0, current_spread_bps=None, stop_distance=0.5,
            db=_EmptyDB(), flag_enabled=True,
        )
        assert allow is True and mult == 1.0 and reason == "no_spread"

    def test_missing_entry_price_neutral(self):
        allow, mult, reason, _ = scv.adaptive_spread_cost_veto_derate(
            symbol="AAA", entry_price=0.0, current_spread_bps=300.0, stop_distance=0.5,
            db=_EmptyDB(), flag_enabled=True,
        )
        assert allow is True and mult == 1.0 and reason == "no_entry_price"

    def test_missing_stop_distance_neutral(self):
        allow, mult, reason, _ = scv.adaptive_spread_cost_veto_derate(
            symbol="AAA", entry_price=10.0, current_spread_bps=300.0, stop_distance=0.0,
            db=_EmptyDB(), flag_enabled=True,
        )
        assert allow is True and mult == 1.0 and reason == "no_stop_distance"

    def test_nan_spread_treated_as_missing_neutral(self):
        allow, mult, _, _ = scv.adaptive_spread_cost_veto_derate(
            symbol="AAA", entry_price=10.0, current_spread_bps=float("nan"), stop_distance=0.5,
            db=_EmptyDB(), flag_enabled=True,
        )
        assert allow is True and mult == 1.0

    def test_db_error_never_blocks_and_floors_size(self):
        # A raising db -> name_spread_percentiles returns None (insufficient history) ->
        # the anomaly leg cannot engage, but the cost-of-R leg still derates.  CRITICAL
        # invariant: allow is ALWAYS True and mult in [floor, 1.0] — never a block.
        floor = 0.5
        with patch.object(scv.settings, "chili_momentum_spread_cost_derate_floor", floor, create=True), \
             patch.object(scv.settings, "chili_momentum_spread_cost_max_fraction_of_r", 0.25, create=True):
            # A toxic spread (eats > max_frac of R) with NO name history -> derate (not block).
            allow, mult, _, meta = scv.adaptive_spread_cost_veto_derate(
                symbol="AAA", entry_price=10.0, current_spread_bps=2000.0, stop_distance=0.1,
                db=_RaisingDB(), flag_enabled=True,
            )
        assert allow is True
        assert floor <= mult <= 1.0
        assert meta.get("name_dist") == "insufficient_history"

    def test_typical_wide_spread_with_good_R_passes_at_1x(self):
        # The NO-0-FILLS guarantee: a wide-but-cheap-vs-R spread with no name history must
        # PASS at mult=1.0 (a normal low-float Ross trade is never derated).
        with patch.object(scv.settings, "chili_momentum_spread_cost_max_fraction_of_r", 0.25, create=True), \
             patch.object(scv.settings, "chili_momentum_spread_cost_derate_engage_frac", 0.5, create=True):
            allow, mult, reason, _ = scv.adaptive_spread_cost_veto_derate(
                symbol="AAA", entry_price=10.0, current_spread_bps=50.0, stop_distance=1.0,
                db=_EmptyDB(), flag_enabled=True,
            )
        # cost_of_r = (50/1e4 * 10)/1.0 = 0.05  << engage 0.125 -> no derate
        assert allow is True and mult == 1.0 and reason == "pass"

    def test_reclaim_derates_no_more_than_nonreclaim(self):
        # CONSISTENCY: the reclaim carve-out must NEVER make the gate STRICTER than the
        # non-reclaim base (a misconfigured smaller reclaim base must be clamped up).  At
        # the SAME extreme spread, the reclaim mult must be >= the non-reclaim mult.
        with patch.object(scv.settings, "chili_momentum_spread_cost_max_fraction_of_r", 0.25, create=True), \
             patch.object(scv.settings, "chili_momentum_spread_cost_reclaim_max_fraction_of_r", 0.10, create=True), \
             patch.object(scv.settings, "chili_momentum_spread_cost_derate_floor", 0.5, create=True):
            kw = dict(symbol="AAA", entry_price=10.0, current_spread_bps=900.0,
                      stop_distance=0.2, db=_EmptyDB(), flag_enabled=True)
            _, nonreclaim_mult, _, _ = scv.adaptive_spread_cost_veto_derate(entry_trigger_reason="hod_break", **kw)
            _, reclaim_mult, _, _ = scv.adaptive_spread_cost_veto_derate(entry_trigger_reason="vwap_reclaim", **kw)
        assert reclaim_mult >= nonreclaim_mult - 1e-9, (
            "reclaim carve-out made the gate STRICTER than non-reclaim — the documented "
            "max(std, reclaim) clamp must prevent this"
        )


# ==============================================================================
# 11. catalyst & green-day & cushion SIZE multipliers — RISK-ADDING/NEUTRAL.
#     DOCUMENTED: missing/none/error => NEUTRAL 1.0; these NEVER hard-block and
#     NEVER shrink below 1.0 (a missing catalyst must not derate, and a present
#     catalyst only ADDS).  CONSISTENCY: a missing catalyst => 1.0, never a block.
# ==============================================================================
class TestSizeMultipliersFailNeutral:
    def test_catalyst_score_missing_set_is_neutral(self):
        assert cat.catalyst_score("AAA", None) == 0.5     # no set -> neutral
        assert cat.catalyst_score("AAA", set()) == 0.5    # empty set -> neutral

    def test_catalyst_viability_delta_missing_is_zero(self):
        assert cat.catalyst_viability_delta("AAA", None) == 0.0   # no tilt when no catalyst data

    def test_catalyst_score_crypto_neutral(self):
        assert cat.catalyst_score("BTC-USD", {"BTC"}) == 0.5

    def test_green_day_graduation_disabled_neutral(self):
        with patch.object(rp.settings, "chili_momentum_green_day_graduation_enabled", False, create=True):
            mult, meta = rp.green_day_graduation_multiplier(_EmptyDB(), execution_family="momentum_neural")
        assert mult == 1.0 and meta["graduation_mult"] == 1.0

    def test_green_day_graduation_error_fails_neutral(self):
        # Enabled but the streak read RAISES => fail-NEUTRAL 1.0, never a block.
        # (consecutive_green_days swallows a raising db on its own and returns 0, so to
        # exercise the OUTER except we force the streak helper itself to raise.)
        with patch.object(rp.settings, "chili_momentum_green_day_graduation_enabled", True, create=True), \
             patch.object(rp, "consecutive_green_days", side_effect=RuntimeError("boom")):
            mult, meta = rp.green_day_graduation_multiplier(_EmptyDB(), execution_family="momentum_neural")
        assert mult == 1.0
        assert meta.get("reason") == "error_fail_neutral"

    def test_green_day_graduation_db_failure_is_still_neutral(self):
        # Even a raising db (which consecutive_green_days handles gracefully -> streak 0)
        # must yield mult 1.0 — a missing/failed streak read NEVER changes sizing.
        with patch.object(rp.settings, "chili_momentum_green_day_graduation_enabled", True, create=True):
            mult, _ = rp.green_day_graduation_multiplier(_RaisingDB(), execution_family="momentum_neural")
        assert mult == 1.0

    def test_green_day_graduation_never_below_one(self):
        # INVARIANT: the graduation mult is clamped to [1.0, max] — it can only ADD size.
        with patch.object(rp.settings, "chili_momentum_green_day_graduation_enabled", True, create=True), \
             patch.object(rp, "consecutive_green_days", return_value=(0, {})):
            mult, _ = rp.green_day_graduation_multiplier(_EmptyDB(), execution_family="momentum_neural")
        assert mult >= 1.0

    def test_cushion_multiplier_no_base_neutral(self):
        # base_loss<=0 must short-circuit to neutral 1.0.  (Pin the pnl read — the function
        # imports it from ..governance — to a clean value so we isolate the no_base_loss
        # branch, not the governance read path.)
        with patch("app.services.trading.governance.global_realized_pnl_today_et",
                   return_value={"total_usd": 0.0}):
            mult, meta = rp.cushion_risk_multiplier(_EmptyDB(), base_loss_usd=0.0)
        assert mult == 1.0 and meta["reason"] == "no_base_loss"

    def test_cushion_multiplier_error_fails_neutral(self):
        # The pnl read (imported from ..governance) RAISES -> except -> neutral 1.0.
        with patch("app.services.trading.governance.global_realized_pnl_today_et",
                   side_effect=RuntimeError("boom")):
            mult, meta = rp.cushion_risk_multiplier(_EmptyDB(), base_loss_usd=100.0)
        assert mult == 1.0 and meta["reason"] == "error_fail_neutral"

    def test_cushion_multiplier_never_below_one(self):
        # INVARIANT: cushion mult is clamped to [1.0, 2.0] — a NEGATIVE day (loss) must NOT
        # shrink the next trade below base (the floor was deliberately raised 0.5 -> 1.0).
        with patch("app.services.trading.governance.global_realized_pnl_today_et",
                   return_value={"total_usd": -500.0}):
            mult, _ = rp.cushion_risk_multiplier(_EmptyDB(), base_loss_usd=100.0)
        assert 1.0 <= mult <= 2.0


# ==============================================================================
# 12. CROSS-CUTTING CONSISTENCY MATRIX — assert each gate's missing-input behaviour
#     matches its safety ROLE in ONE place, so a future edit that flips a direction
#     (e.g. makes a chase-guard fail-CLOSED into a hard block, or a size-derate
#     fail-CLOSED) trips here loudly.
# ==============================================================================
class TestFailmodeConsistencyMatrix:
    def test_safety_critical_gates_fail_closed_on_missing(self):
        # tape_confirms_hold: missing everything -> no fire.
        ok, _ = eg.tape_confirms_hold(None, db=None, settings=_S(chili_momentum_tape_hold_entry_enabled=True))
        assert ok is False
        # add_into_halt_ok: missing core inputs -> no add.
        ok2, _, _ = eg.add_into_halt_ok(
            avg_entry=None, original_stop=None, current_stop=None, bid=None,
            is_limit_up_halt=True, in_rth=True,
            settings_obj=_S(chili_momentum_add_into_halt_enabled=True),
        )
        assert ok2 is False

    def test_size_derates_fail_open_neutral_never_block(self):
        # spread derate, halt-chain de-rate, cushion, graduation, catalyst — each missing
        # its driver returns a NEUTRAL/permit, NEVER a hard block.
        allow, mult, _, _ = scv.adaptive_spread_cost_veto_derate(
            symbol="AAA", entry_price=10.0, current_spread_bps=None, stop_distance=0.5,
            db=_EmptyDB(), flag_enabled=True,
        )
        assert allow is True and mult == 1.0
        block, hmult, _, _ = eg.halt_chain_risk_gate(
            consecutive_halt_up_count=None,
            settings_obj=_S(chili_momentum_halt_chain_risk_gate_enabled=True),
        )
        assert block is False and hmult == 1.0
        assert cat.catalyst_score("AAA", None) == 0.5

    def test_chase_guard_extension_now_fails_safe(self):
        # HIGH-2 FIX: the chase-guard (_entry_extension_veto) now fails SAFE when atr_pct
        # is missing — a missing ATR collapses the cap to the FLAT extension floor
        # (max(floor, K*0) = floor) instead of disarming the guard. A wildly extended
        # entry (1000.0 vs a 10.0 break = +9900%) is far above the floor and VETOES.
        # (Previously this FAILED OPEN and admitted the chase — the flagged inconsistency,
        # now resolved to fail-safe.)
        s = _S(chili_momentum_entry_extension_veto_enabled=True)
        assert eg._entry_extension_veto(1000.0, 10.0, None, s) is True


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
