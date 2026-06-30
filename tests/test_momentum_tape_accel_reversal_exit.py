"""Tape-acceleration reversal exit — sell-into-strength climax lock (adversarial tests).

``tape_accel_reversal_exit`` is the equity-tape sibling of ``ofi_exhaustion_lock``: it locks
a WINNER at the spike's climax — the moment the executed-buy push (``signed_tape_accel`` off
``iqfeed_trade_ticks``) ENDS / turns while price is still NEAR the high — so the next tick
exits near the top BEFORE the giveback. It covers the broad set of equity names the OFI lock
misses (the L2 depth tape is starved on most names; the TRADE tape is not).

Safety contract MIRRORS the OFI lock EXACTLY:

  * RATCHET-ONLY (Invariant A): ``new_stop_floor = max(current_stop, breakeven_floor,
    candidate)`` — it can only ever RAISE the stop, never loosen, never write below the
    structural stop. It can therefore ONLY exit a winner near its top; never cut a loser
    early.
  * FAIL-SAFE: a short, any non-finite/missing input, or ``signed_tape_accel is None``
    (crypto / empty tape) ⇒ pure no-op (``new_stop_floor == current_stop``, ``fired == False``).
  * A/B counterfactual: ``counterfactual_fixed_stop == current_stop`` (the lock-OFF baseline)
    is ALWAYS returned so realized PnL can be measured against the live baseline.

These are PURE-LOGIC tests on crafted numeric inputs (no I/O). ``paper_execution.settings`` is
patched with a ``SimpleNamespace`` carrying the knob defaults so each gate can be regressed
independently: a clean WINNER with the tape turning <=0 near the high FIRES (tightens); flip
exactly ONE condition (still accelerating / not yet a winner / far from the high / None tape)
and it NO-OPs. A gate that silently stopped blocking would make its adversarial test fail.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from app.services.trading.momentum_neural import entry_gates
from app.services.trading.momentum_neural.paper_execution import tape_accel_reversal_exit

_PE = "app.services.trading.momentum_neural.paper_execution"


def _settings() -> SimpleNamespace:
    """The exit's knobs at their config defaults (REUSE the OFI lock's arm_frac +
    base_lock_bps; the only NEW knob is the giveback fraction)."""
    return SimpleNamespace(
        chili_momentum_exit_ofi_arm_frac=0.5,          # arm_r = 0.5 * rr
        chili_momentum_exit_ofi_base_lock_bps=120.0,   # climax cushion off the bid
        chili_momentum_exit_accel_reversal_giveback_frac=0.35,  # the one new knob
    )


# A clean WINNER geometry. entry=10.0, atr_pct=0.02, stop_atr_mult=0.60 -> risk_dist =
# 10 * max(0.003, 0.02*0.60) = 10 * 0.012 = 0.12. rr=2.0 -> arm_r = max(0.5, 0.5*2) = 1.0R.
# hwm=10.30 -> peak_r = (10.30-10.0)/0.12 = 2.5R (well past the 1.0R arm).
# giveback band = 0.35 * 0.12 = 0.042; a bid of 10.29 gives back 0.01 (< 0.042) => NEAR-HIGH.
_ENTRY = 10.0
_ATR_PCT = 0.02
_SM = 0.60
_RISK = 0.12
_HWM = 10.30
_NEAR_BID = 10.29   # giveback 0.01 < 0.042 -> near the high (into strength)
_FAR_BID = 10.20    # giveback 0.10 > 0.042 -> already gave back a lot (trail's job)
_RR = 2.0
# A loss-side current stop well below the bid so a fire can RAISE it.
_CUR_STOP = 9.90
_BE = 10.0


def _call(**over):
    base = dict(
        high_water_mark=_HWM,
        entry_price=_ENTRY,
        bid=_NEAR_BID,
        atr_pct=_ATR_PCT,
        stop_atr_mult=_SM,
        reward_risk=_RR,
        current_stop=_CUR_STOP,
        breakeven_floor=_BE,
        signed_tape_accel=-5.0,            # the push has TURNED net-negative
        prev_signed_tape_accel=20.0,       # was pushing up -> genuine TURN
        side_long=True,
    )
    base.update(over)
    with patch(f"{_PE}.settings", _settings()):
        return tape_accel_reversal_exit(**base)


# ─────────────────────────── (a) FIRES on a winner climax ───────────────────────────────

class TestFires:
    def test_winner_accel_turns_near_high_fires_and_tightens(self):
        """A WINNER (2.5R) with the tape turning <=0 (genuine TURN) NEAR the high FIRES and
        RAISES the stop toward the climax (bid - cushion), strictly above the current stop."""
        out = _call()
        assert out["fired"] is True, out
        assert out["armed"] is True
        assert out["trigger"] == "tape_accel_reversal"
        # candidate = bid - bid*(120bps) = 10.29 * (1 - 0.012) = 10.16652; > current stop.
        assert out["new_stop_floor"] > _CUR_STOP
        assert out["new_stop_floor"] == pytest.approx(_NEAR_BID * (1 - 120.0 / 10_000.0), abs=1e-6)
        # peak_r reported as ~2.5R
        assert out["peak_r"] == pytest.approx(2.5, abs=1e-3)

    def test_no_prev_accel_le_zero_alone_fires(self):
        """With NO prior sample, accel <= 0 alone qualifies as the reversal (cleaner-turn
        read only applies when prev is provided)."""
        out = _call(prev_signed_tape_accel=None, signed_tape_accel=0.0)
        assert out["fired"] is True, out
        assert out["trigger"] == "tape_accel_reversal"


# ─────────────────── (b) NO-OP while still accelerating (building spike) ─────────────────

class TestStillAccelerating:
    def test_accel_positive_no_op(self):
        """accel > 0 -> the executed push is STILL building -> do NOT sell into a rising spike."""
        out = _call(signed_tape_accel=30.0, prev_signed_tape_accel=10.0)
        assert out["fired"] is False
        assert out["new_stop_floor"] == _CUR_STOP
        assert out["reason"] == "still_accelerating"
        assert out["armed"] is True  # it IS a winner; it just hasn't reversed

    def test_no_genuine_turn_when_prev_was_already_negative(self):
        """prev <= 0 AND current <= 0 is NOT a TURN (was never pushing up) -> no fire when a
        prior sample is present."""
        out = _call(prev_signed_tape_accel=-3.0, signed_tape_accel=-5.0)
        assert out["fired"] is False
        assert out["reason"] == "still_accelerating"


# ───────────────────────── (c) NO-OP when not yet a winner ──────────────────────────────

class TestNotYetWinner:
    def test_below_arm_no_op(self):
        """peak_r < arm_r (= 1.0R) -> the trail/stop owns healthy pullbacks; the lock is inert."""
        # hwm just 0.05 above entry -> peak_r = 0.05/0.12 ~= 0.42R < 1.0R arm.
        out = _call(high_water_mark=_ENTRY + 0.05, bid=_ENTRY + 0.04)
        assert out["armed"] is False
        assert out["fired"] is False
        assert out["new_stop_floor"] == _CUR_STOP
        assert out["reason"] == "below_arm"


# ─────────────── (d) NO-OP when price already gave back a lot (trail's job) ──────────────

class TestGaveBackTooMuch:
    def test_far_from_high_no_op(self):
        """A winner that reversed but is now FAR below the high (giveback > band) is the
        TRAIL's exit, not a sell-into-strength. The lock declines (no fire)."""
        out = _call(bid=_FAR_BID)  # giveback 0.10 > 0.042 band
        assert out["armed"] is True
        assert out["fired"] is False
        assert out["new_stop_floor"] == _CUR_STOP
        assert out["reason"] == "gave_back_too_much"


# ───────────────────── (e) FAIL-SAFE: None / bad tape -> no-op ──────────────────────────

class TestFailSafe:
    def test_none_tape_no_op(self):
        """signed_tape_accel is None (crypto / empty tape / any read miss) -> pure no-op."""
        out = _call(signed_tape_accel=None)
        assert out["fired"] is False
        assert out["new_stop_floor"] == _CUR_STOP
        assert out["reason"] == "no_tape"
        assert out["counterfactual_fixed_stop"] == _CUR_STOP

    def test_non_finite_inputs_no_op(self):
        """A NaN/inf price input -> no-op (never acts on garbage)."""
        out = _call(high_water_mark=float("nan"))
        assert out["fired"] is False
        assert out["new_stop_floor"] == _CUR_STOP

    def test_short_side_no_op(self):
        """side_long=False -> immediate no-op (this is a long-only winner exit)."""
        out = _call(side_long=False)
        assert out["fired"] is False
        assert out["new_stop_floor"] == _CUR_STOP
        assert out["reason"] == "not_long"


# ───────────── (f) RATCHET-ONLY (Invariant A): never below current/BE, even on fire ──────

class TestRatchetOnly:
    def test_never_below_current_stop_even_when_candidate_lower(self):
        """If the climax candidate (bid - cushion) sits BELOW the current stop, Invariant A
        clamps new_stop_floor to the current stop and does NOT fire (no raise) — never loosen."""
        # current stop already ABOVE the candidate (10.20 > 10.16652) -> candidate cannot raise.
        out = _call(current_stop=10.25, breakeven_floor=10.25)
        assert out["new_stop_floor"] == pytest.approx(10.25, abs=1e-9)
        assert out["new_stop_floor"] >= 10.25
        assert out["fired"] is False  # ratchet produced no raise
        assert out["reason"] == "ratchet_no_raise"

    def test_breakeven_floor_respected_on_fire(self):
        """A fire never returns a stop below the breakeven floor. With a high BE floor the
        floor wins via the max()."""
        out = _call(current_stop=9.90, breakeven_floor=10.20)
        # candidate 10.16652 < BE 10.20 -> floor clamps to 10.20 (still a RAISE vs 9.90).
        assert out["new_stop_floor"] == pytest.approx(10.20, abs=1e-9)
        assert out["new_stop_floor"] >= 10.20
        assert out["fired"] is True  # 10.20 > 9.90 current

    def test_counterfactual_is_lock_off_baseline_always(self):
        """counterfactual_fixed_stop == current_stop on every path (the A/B baseline)."""
        fired = _call()
        noop = _call(signed_tape_accel=None)
        assert fired["counterfactual_fixed_stop"] == _CUR_STOP
        assert noop["counterfactual_fixed_stop"] == _CUR_STOP


# ───────── (g) crypto path: signed_tape_accel_features returns None -> helper no-ops ─────

class TestCryptoPath:
    def test_signed_tape_accel_features_none_for_crypto(self):
        """The live wiring fetches signed_tape_accel via signed_tape_accel_features, which
        returns None for a -USD symbol (no equity tick tape). That None flows into the helper
        as signed_tape_accel=None -> no-op -> crypto byte-identical."""
        # signed_tape_accel_features short-circuits on a -USD symbol BEFORE any db I/O.
        feats = entry_gates.signed_tape_accel_features("BTC-USD", db=MagicMock())
        assert feats is None
        # The helper, fed that None, is a pure no-op.
        out = _call(signed_tape_accel=None)
        assert out["fired"] is False
        assert out["new_stop_floor"] == _CUR_STOP
        assert out["reason"] == "no_tape"
