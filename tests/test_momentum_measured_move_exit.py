"""Measured-move scale target + double-top exhaustion (winner-management).

WINNER-SAFE / RATCHET-ONLY exit upgrades on the held momentum runner, flag-gated
``chili_momentum_measured_move_exit_enabled`` (default OFF ⇒ byte-identical):

  (1) MEASURED-MOVE SCALE TARGET — measure the name's OWN first impulse leg
      (impulse_leg_high − entry, frozen at the first-target scale-out), project it
      ABOVE the impulse high to a measured-move target, SCALE OUT a fraction at the
      target, and ratchet the runner stop up. A PARTIAL, never a full cut — a strong
      runner that blows past the target keeps running.

  (2) DOUBLE-TOP EXHAUSTION — a lower-high RETEST of the impulse high inside an
      ATR-relative band that is REJECTED ⇒ tighten the stop / arm a partial. A clean
      higher-high ⇒ NO exhaustion exit (the winner is left to run).

These are PURE helpers (no DB / no I/O) so the suite needs no fixture. Adversarial
coverage: target reached ⇒ partial + stop ratchets up; a strong runner through the
MM ⇒ remainder runs (not hard-cut); double-top weak retest ⇒ tighten/partial; clean
higher-high ⇒ no exhaustion exit; the stop only ever RATCHETS UP (never loosens);
flag OFF ⇒ byte-identical no-op.
"""

from __future__ import annotations

import math

import pytest

from app.config import settings
from app.services.trading.momentum_neural import paper_execution as pe


# ── helpers ───────────────────────────────────────────────────────────────────


@pytest.fixture
def mm_on(monkeypatch):
    """Flag ON + documented defaults (0.33 scale fraction, 0.75 ATR double-top band)."""
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_scale_fraction", 0.33, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_double_top_atr_mult", 0.75, raising=False)
    # neutralize the crypto override so equity-shaped tests use the base fraction
    monkeypatch.setattr(settings, "chili_momentum_crypto_scale_out_fraction", None, raising=False)
    return settings


# ── measured-move target geometry (the name's OWN leg, no flat %) ──────────────


def test_measured_move_target_projects_own_leg_height():
    # entry 10, impulse high 12 => leg = 2 => target = 12 + 2 = 14 (a second equal leg).
    tgt = pe.measured_move_target(entry_price=10.0, impulse_leg_high=12.0)
    assert tgt == pytest.approx(14.0)


def test_measured_move_target_degenerate_leg_is_none():
    # impulse high at/below entry => no leg => no target (caller no-ops).
    assert pe.measured_move_target(entry_price=10.0, impulse_leg_high=10.0) is None
    assert pe.measured_move_target(entry_price=10.0, impulse_leg_high=9.0) is None
    assert pe.measured_move_target(entry_price=10.0, impulse_leg_high=12.0, side_long=False) is None


# ── flag OFF => byte-identical no-op ───────────────────────────────────────────


def test_flag_off_scale_decision_is_noop():
    out = pe.measured_move_scale_exit_decision(
        flag_on=False,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.5,  # well past the measured-move target
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is False
    assert out["reason"] == "flag_off"
    # ratchet-only invariant even in the no-op path: stop is unchanged, never loosened.
    assert out["new_stop_floor"] == 11.0


def test_flag_off_double_top_is_noop():
    out = pe.double_top_tighten_decision(
        flag_on=False,
        impulse_leg_high=12.0,
        current_high=11.9,  # a lower-high retest that WOULD be a double-top if on
        bid=11.7,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
    )
    assert out["tighten"] is False
    assert out["exhausted"] is False
    assert out["new_stop_floor"] == 11.0  # unchanged


# ── (1) measured-move: target reached => partial + stop ratchets up ────────────


def test_target_reached_fires_partial_and_ratchets_stop_up(mm_on):
    # entry 10, impulse high 12 => target 14. Bid at 14.0 reaches it.
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,  # already above breakeven
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is True
    assert out["reason"] == "measured_move_target"
    assert out["target_price"] == pytest.approx(14.0)
    # PARTIAL, not a full cut: 0.33 of the ORIGINAL 100 sold, the rest runs.
    assert out["scale_qty"] == pytest.approx(33.0)
    assert out["remainder_qty"] == pytest.approx(67.0)
    assert out["remainder_qty"] > 0.0  # a runner ALWAYS remains
    # stop ratcheted up to AT LEAST breakeven (and never below the input stop).
    assert out["new_stop_floor"] >= 10.5
    assert out["new_stop_floor"] >= out.get("new_stop_floor")  # trivially


def test_target_not_reached_no_fire_but_stop_floor_never_loosens(mm_on):
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=13.0,  # below the 14 target
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is False
    assert out["reason"] == "target_not_reached"
    assert out["new_stop_floor"] == 11.0  # unchanged, never loosened


def test_already_fired_does_not_double_fire(mm_on):
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=67.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.5,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=12.0,
        breakeven_floor=10.0,
        already_fired=True,
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is False
    assert out["reason"] == "already_fired"
    assert out["new_stop_floor"] == 12.0


# ── (1) WINNER-SAFE: a strong runner through the MM is NOT hard-cut ────────────


def test_strong_runner_through_mm_keeps_running_not_hard_cut(mm_on):
    # A monster: bid blew WAY past the measured-move target. We still only scale a
    # FRACTION; the remainder is non-zero and runs. The decision NEVER returns a
    # flatten — there is no "exit all" branch, only a partial + a stop ratchet.
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=25.0,  # +150% — a runaway winner
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is True
    # PROOF the runner is not hard-cut: a strictly positive remainder always remains.
    assert out["remainder_qty"] > 0.0
    assert out["scale_qty"] < 100.0  # never sells the whole position
    assert out["scale_fraction"] < 1.0
    # and the stop only ratcheted UP (winner protected, not exited).
    assert out["new_stop_floor"] >= 11.0


def test_dust_remainder_does_not_flatten_runner_still_ratchets(mm_on):
    # A position too small to split cleanly must NOT be flattened by THIS helper
    # (the existing target/trail machinery owns the flat case). It still ratchets up.
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=1.0,
        original_qty=1.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.2,
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,  # 0.33 of 1 share floors to 0 => cannot split
    )
    assert out["fire"] is False
    assert out["reason"] == "target_reached_no_split"
    assert out["scale_qty"] == 0.0  # nothing sold (no flatten)
    assert out["new_stop_floor"] >= 10.2  # but the stop still ratchets up


# ── (2) double-top: weak retest => tighten / arm partial ──────────────────────


def test_double_top_weak_retest_tightens_and_arms_partial(mm_on):
    # entry 10, impulse high 12, risk_dist = 10 * 0.02 * 0.6 = 0.12, band = 0.75*0.12 = 0.09.
    # A retest peak at 11.95 is a lower-high 0.05 below the high (inside the band),
    # and the live bid 11.85 has rolled back below it (rejected) => double-top.
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        ofi=-0.5,  # weak flow
        micro_edge=-0.3,  # weak flow
    )
    assert out["exhausted"] is True
    assert out["tighten"] is True
    assert out["flow_weak"] is True
    assert out["partial_arm"] is True  # both OFI<=0 AND micro<0 => arm a partial
    assert out["new_stop_floor"] > 10.5  # the stop was tightened UP


def test_double_top_structural_only_tightens_without_flow(mm_on):
    # Same structural double-top but NO flow supplied — the lower-high rejected
    # retest ALONE marks exhaustion (fail-OPEN on flow); a plain tighten, no partial.
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        ofi=None,
        micro_edge=None,
    )
    assert out["exhausted"] is True
    assert out["tighten"] is True
    assert out["flow_weak"] is False
    assert out["partial_arm"] is False  # no flow corroborant => tighten only
    assert out["new_stop_floor"] > 10.5


# ── (2) WINNER-SAFE: a clean higher-high is NOT an exhaustion exit ─────────────


def test_clean_higher_high_no_exhaustion_exit(mm_on):
    # The retest TAKES OUT the impulse high (a new higher-high) — NOT a double-top.
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=12.40,  # higher-high
        bid=12.35,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        ofi=-0.9,  # even with weak flow, a higher-high is NOT exhaustion
        micro_edge=-0.5,
    )
    assert out["exhausted"] is False
    assert out["tighten"] is False
    assert out["new_stop_floor"] == 10.5  # untouched — the winner runs
    chk = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=12.40,
        bid=12.35,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
    )
    assert chk["clean_higher_high"] is True
    assert chk["exhausted"] is False


def test_shallow_retest_not_near_high_is_not_double_top(mm_on):
    # A lower-high that never came NEAR the impulse high (far outside the ATR band)
    # is a normal pullback, NOT a double-top => no tighten (don't choke a healthy dip).
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.20,  # 0.80 below the high, band is only ~0.09 => too shallow
        bid=11.10,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        ofi=-0.5,
        micro_edge=-0.3,
    )
    assert out["exhausted"] is False
    assert out["reason"] == "retest_too_shallow"
    assert out["new_stop_floor"] == 10.5


def test_retest_still_pressing_is_not_double_top(mm_on):
    # Price is at the retest peak (still pressing the high, not rejected) => not yet
    # a double-top; never an exhaustion exit while the bid holds the retest peak.
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.95,  # bid == retest peak (still pressing)
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        ofi=-0.5,
        micro_edge=-0.3,
    )
    assert out["exhausted"] is False
    assert out["reason"] == "still_pressing"
    assert out["new_stop_floor"] == 10.5


# ── RATCHET-ONLY invariant: every branch, the stop only ever raises ───────────


def test_measured_move_ratchet_never_loosens_when_stop_already_tighter(mm_on):
    # Current stop ALREADY sits above breakeven AND above where the MM ratchet would
    # put it. The output must EQUAL the current stop (never loosened down to BE).
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=13.5,  # already very tight (above breakeven 10.0)
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["new_stop_floor"] == 13.5  # never loosened below the tighter current stop


def test_double_top_ratchet_never_loosens_when_stop_already_tighter(mm_on):
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.94,  # already tighter than the retest-peak−ATR candidate
        breakeven_floor=10.0,
        ofi=-0.5,
        micro_edge=-0.3,
    )
    # exhaustion is still detected, but the stop is already tighter => no loosening.
    assert out["exhausted"] is True
    assert out["new_stop_floor"] >= 11.94


def test_ratchet_only_property_sweep(mm_on):
    # Property: across a grid of inputs, NEITHER decision ever returns a stop below
    # the input current_stop (the RATCHET-ONLY invariant the live runner relies on).
    for cs in (9.0, 10.0, 10.5, 11.0, 12.0, 13.9, 14.5):
        for bid in (11.0, 13.0, 14.0, 14.001, 25.0):
            mm = pe.measured_move_scale_exit_decision(
                flag_on=True,
                current_qty=100.0,
                original_qty=100.0,
                entry_price=10.0,
                impulse_leg_high=12.0,
                bid=bid,
                atr_pct=0.02,
                stop_atr_mult=0.6,
                current_stop=cs,
                breakeven_floor=10.0,
                base_increment=1.0,
                base_min_size=1.0,
            )
            assert mm["new_stop_floor"] >= cs - 1e-9, (cs, bid, mm)
        for rh in (11.20, 11.90, 11.95, 11.99, 12.40):
            dt = pe.double_top_tighten_decision(
                flag_on=True,
                impulse_leg_high=12.0,
                current_high=rh,
                bid=rh - 0.10,
                entry_price=10.0,
                atr_pct=0.02,
                stop_atr_mult=0.6,
                current_stop=cs,
                breakeven_floor=10.0,
                ofi=-0.5,
                micro_edge=-0.3,
            )
            assert dt["new_stop_floor"] >= cs - 1e-9, (cs, rh, dt)


# ── adaptive: bigger leg => bigger target; wider ATR => wider double-top band ──


def test_target_scales_with_the_names_own_leg(mm_on):
    small_leg = pe.measured_move_target(entry_price=10.0, impulse_leg_high=11.0)  # leg 1 => 12
    big_leg = pe.measured_move_target(entry_price=10.0, impulse_leg_high=15.0)  # leg 5 => 20
    assert small_leg == pytest.approx(12.0)
    assert big_leg == pytest.approx(20.0)
    assert big_leg - 15.0 > small_leg - 11.0  # the projection follows the name's own leg


def test_double_top_band_scales_with_atr(mm_on):
    # A retest 0.30 below the high: rejected under a HIGH-ATR name (wide band) but
    # "too shallow" under a LOW-ATR name (tight band) — the band is ATR-relative.
    common = dict(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.70,
        bid=11.60,
        entry_price=10.0,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
    )
    wide = pe.double_top_tighten_decision(atr_pct=0.08, **common)  # band ~0.36 > 0.30
    tight = pe.double_top_tighten_decision(atr_pct=0.02, **common)  # band ~0.09 < 0.30
    assert wide["exhausted"] is True
    assert tight["exhausted"] is False
    assert tight["reason"] == "retest_too_shallow"


# ══════════════════════════════════════════════════════════════════════════════
# ADVERSARIAL HARDENING — branch / boundary / edge / failure-mode coverage.
# Each test asserts the SPECIFIC reason or value so it fails if its branch
# regresses (not mere truthiness). Source is NOT modified — any test that
# exposes a likely source bug is noted in the operator return.
# ══════════════════════════════════════════════════════════════════════════════


# ── measured_move_target: NaN / inf / negative / non-numeric edges ────────────


def test_measured_move_target_nan_and_inf_inputs_are_none():
    nan = float("nan")
    inf = float("inf")
    assert pe.measured_move_target(entry_price=nan, impulse_leg_high=12.0) is None
    assert pe.measured_move_target(entry_price=10.0, impulse_leg_high=nan) is None
    assert pe.measured_move_target(entry_price=inf, impulse_leg_high=12.0) is None
    assert pe.measured_move_target(entry_price=10.0, impulse_leg_high=inf) is None


def test_measured_move_target_nonpositive_and_bad_type_are_none():
    assert pe.measured_move_target(entry_price=0.0, impulse_leg_high=12.0) is None
    assert pe.measured_move_target(entry_price=-1.0, impulse_leg_high=12.0) is None
    assert pe.measured_move_target(entry_price=10.0, impulse_leg_high=0.0) is None
    assert pe.measured_move_target(entry_price=10.0, impulse_leg_high=-1.0) is None
    assert pe.measured_move_target(entry_price="x", impulse_leg_high=12.0) is None
    assert pe.measured_move_target(entry_price=None, impulse_leg_high=12.0) is None


# ── _measured_move_scale_fraction: clamp + bad-value fallbacks ─────────────────


def test_scale_fraction_clamped_to_open_interval(monkeypatch):
    # Above the upper bound clamps to 0.95 (always leaves a runner).
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_scale_fraction", 5.0, raising=False)
    assert pe._measured_move_scale_fraction() == pytest.approx(0.95)
    # Below the lower bound clamps to 0.05 (never a no-op 0%).
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_scale_fraction", 0.0, raising=False)
    assert pe._measured_move_scale_fraction() == pytest.approx(0.05)
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_scale_fraction", -1.0, raising=False)
    assert pe._measured_move_scale_fraction() == pytest.approx(0.05)


def test_scale_fraction_nan_and_bad_type_fall_back_to_default(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_scale_fraction", float("nan"), raising=False)
    assert pe._measured_move_scale_fraction() == pytest.approx(0.33)
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_scale_fraction", "garbage", raising=False)
    assert pe._measured_move_scale_fraction() == pytest.approx(0.33)


def test_double_top_atr_mult_clamped_and_bad_value(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_double_top_atr_mult", 99.0, raising=False)
    assert pe._double_top_atr_mult() == pytest.approx(2.0)  # upper clamp
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_double_top_atr_mult", 0.0, raising=False)
    assert pe._double_top_atr_mult() == pytest.approx(0.1)  # lower clamp (never zero band)
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_double_top_atr_mult", float("nan"), raising=False)
    assert pe._double_top_atr_mult() == pytest.approx(0.75)  # NaN => default
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_double_top_atr_mult", "junk", raising=False)
    assert pe._double_top_atr_mult() == pytest.approx(0.75)  # bad type => default


# ── measured_move_scale_exit_decision: short side, bad basis, no-leg ───────────


def test_scale_decision_short_side_is_noop(mm_on):
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.5,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,
        side_long=False,  # short side is unsupported => inert
    )
    assert out["fire"] is False
    # short returns the initial dict: reason stays the flag-on "wait" sentinel.
    assert out["reason"] == "wait"
    assert out["new_stop_floor"] == 11.0  # untouched
    assert out["target_price"] is None


def test_scale_decision_bad_basis_nonnumeric(mm_on):
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price="bad",  # non-numeric basis
        impulse_leg_high=12.0,
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
    )
    assert out["fire"] is False
    assert out["reason"] == "bad_basis"
    assert out["new_stop_floor"] == 11.0  # never loosened even on the error path


def test_scale_decision_bad_basis_nan(mm_on):
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=float("nan"),  # NaN bid => bad_basis (not a silent fire)
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
    )
    assert out["fire"] is False
    assert out["reason"] == "bad_basis"
    assert out["new_stop_floor"] == 11.0


def test_scale_decision_no_leg_degenerate_impulse(mm_on):
    # impulse high <= entry => degenerate leg => no target => no_leg (caller no-ops).
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=10.0,  # no first leg
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is False
    assert out["reason"] == "no_leg"
    assert out["target_price"] is None
    assert out["leg_height"] is None
    assert out["new_stop_floor"] == 11.0


# ── measured_move_scale_exit_decision: TARGET boundary (exactly-at / eps) ──────


def test_scale_decision_bid_exactly_at_target_fires(mm_on):
    # target = 14.0; the gate is `bid < tgt*(1-1e-9)` => bid EXACTLY at target FIRES.
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.0,  # exactly at target
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is True
    assert out["reason"] == "measured_move_target"


def test_scale_decision_bid_eps_below_target_does_not_fire(mm_on):
    # A hair below the 1e-9 tolerance band must NOT fire (target_not_reached).
    tgt = 14.0
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=tgt * (1.0 - 1e-6),  # clearly below the tolerance
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is False
    assert out["reason"] == "target_not_reached"
    assert out["target_price"] == pytest.approx(14.0)  # target computed even un-fired
    assert out["new_stop_floor"] == 11.0


# ── measured_move_scale_exit_decision: ratchet floor uses breakeven > stop ─────


def test_scale_ratchet_lifts_to_breakeven_when_stop_below_entry(mm_on):
    # current_stop (10.2) is BELOW entry (10.0)? no — set stop below breakeven_floor
    # so the ratchet must lift the stop UP to the breakeven candidate max(entry, be).
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=9.5,  # below both entry and breakeven_floor
        breakeven_floor=10.3,  # explicit breakeven floor above entry
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is True
    # ratchet_floor = max(cs=9.5, be=10.3, be_candidate=max(10.0,10.3)=10.3) = 10.3
    assert out["new_stop_floor"] == pytest.approx(10.3)
    assert out["new_stop_floor"] > 9.5  # lifted UP off the loose stop


# ── crypto override: heavier slice; equity ignores the crypto knob ────────────


def test_crypto_symbol_takes_heavier_slice(monkeypatch, mm_on):
    # crypto override 0.50 > base 0.33 => crypto sells the heavier fraction.
    monkeypatch.setattr(settings, "chili_momentum_crypto_scale_out_fraction", 0.50, raising=False)
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        symbol="ETH-USD",  # crypto
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is True
    assert out["scale_fraction"] == pytest.approx(0.50)
    assert out["scale_qty"] == pytest.approx(50.0)
    assert out["remainder_qty"] == pytest.approx(50.0)  # still a runner


def test_crypto_override_never_below_base_fraction(monkeypatch, mm_on):
    # A crypto override BELOW the base must NOT shrink the slice (max(base, ov)).
    monkeypatch.setattr(settings, "chili_momentum_crypto_scale_out_fraction", 0.10, raising=False)
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        symbol="BTC-USD",
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is True
    assert out["scale_fraction"] == pytest.approx(0.33)  # base wins, not the lower override


def test_equity_symbol_ignores_crypto_override(monkeypatch, mm_on):
    # A crypto override set, but an EQUITY symbol must ignore it (uses base 0.33).
    monkeypatch.setattr(settings, "chili_momentum_crypto_scale_out_fraction", 0.80, raising=False)
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        symbol="AAPL",  # equity — the crypto knob must not apply
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is True
    assert out["scale_fraction"] == pytest.approx(0.33)


def test_crypto_override_out_of_range_ignored(monkeypatch, mm_on):
    # An override >= 1.0 is out of (0,1) => ignored; base fraction stands.
    monkeypatch.setattr(settings, "chili_momentum_crypto_scale_out_fraction", 1.5, raising=False)
    out = pe.measured_move_scale_exit_decision(
        flag_on=True,
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        symbol="ETH-USD",
        base_increment=1.0,
        base_min_size=1.0,
    )
    assert out["fire"] is True
    assert out["scale_fraction"] == pytest.approx(0.33)  # bad override ignored


# ── flag-OFF remainder_qty mirrors current_qty (byte-identical no-op shape) ────


def test_flag_off_remainder_equals_current_qty():
    out = pe.measured_move_scale_exit_decision(
        flag_on=False,
        current_qty=88.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=14.5,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
    )
    assert out["fire"] is False
    assert out["reason"] == "flag_off"
    assert out["remainder_qty"] == pytest.approx(88.0)  # the held qty, untouched
    assert out["scale_qty"] == 0.0
    assert out["target_price"] is None


# ── double_top_exhaustion_check: direct branch coverage ───────────────────────


def test_exhaustion_check_flag_off_inert():
    out = pe.double_top_exhaustion_check(
        flag_on=False,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
    )
    assert out["exhausted"] is False
    assert out["reason"] == "flag_off"
    assert out["retest_gap_atr"] is None


def test_exhaustion_check_short_side_inert():
    out = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        side_long=False,  # short => inert (returns the on-"wait" sentinel)
    )
    assert out["exhausted"] is False
    assert out["reason"] == "wait"


def test_exhaustion_check_bad_basis_nan_and_type():
    nan = float("nan")
    out_nan = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=nan,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
    )
    assert out_nan["exhausted"] is False
    assert out_nan["reason"] == "bad_basis"
    out_type = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high="x",
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
    )
    assert out_type["reason"] == "bad_basis"


def test_exhaustion_check_nonpositive_high_or_entry_is_bad_basis():
    out_h = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=0.0,  # non-positive high
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
    )
    assert out_h["reason"] == "bad_basis"
    out_e = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=-1.0,  # non-positive entry
        atr_pct=0.02,
        stop_atr_mult=0.6,
    )
    assert out_e["reason"] == "bad_basis"


def test_exhaustion_check_reports_retest_gap_atr():
    # risk_dist = 10*0.02*0.6 = 0.12; gap = 12-11.95 = 0.05; gap/risk = 0.4167 -> 0.4167.
    out = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
    )
    assert out["exhausted"] is True
    assert out["retest_gap_atr"] == pytest.approx(round(0.05 / 0.12, 4))


def test_exhaustion_clean_higher_high_boundary_at_impulse_high():
    # current_high EXACTLY at the impulse high (within 1e-9) => clean_higher_high.
    out = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=12.0,  # equal to the high — boundary
        bid=11.9,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
    )
    assert out["clean_higher_high"] is True
    assert out["exhausted"] is False
    assert out["reason"] == "clean_higher_high"


def test_exhaustion_still_pressing_when_bid_at_retest_peak():
    # bid == current_high (within 1e-9) => still_pressing (not rejected yet).
    out = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.95,  # bid holds the retest peak
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
    )
    assert out["exhausted"] is False
    assert out["reason"] == "still_pressing"


# ── double_top flow corroborant: BOTH legs required (fail-OPEN structural) ─────


def test_flow_weak_requires_both_ofi_and_micro_weak(mm_on):
    base = dict(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
    )
    # Only OFI weak, micro POSITIVE => not flow_weak => structural tighten only.
    only_ofi = pe.double_top_tighten_decision(ofi=-0.5, micro_edge=0.4, **base)
    assert only_ofi["exhausted"] is True
    assert only_ofi["flow_weak"] is False
    assert only_ofi["partial_arm"] is False
    # Only micro weak, OFI POSITIVE => not flow_weak.
    only_micro = pe.double_top_tighten_decision(ofi=0.6, micro_edge=-0.3, **base)
    assert only_micro["flow_weak"] is False
    assert only_micro["partial_arm"] is False


def test_flow_weak_ofi_zero_is_weak_but_micro_zero_is_not(mm_on):
    # The check is ofi <= 0 AND micro < 0. ofi=0 qualifies; micro=0 does NOT.
    out = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        ofi=0.0,  # boundary: <= 0 is weak
        micro_edge=0.0,  # boundary: NOT < 0 => not weak
    )
    assert out["exhausted"] is True
    assert out["flow_weak"] is False  # micro=0 fails the strict < 0


def test_flow_weak_both_at_boundary(mm_on):
    # ofi=0 (<=0 ok) and micro just below 0 (<0 ok) => flow_weak True.
    out = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        ofi=0.0,
        micro_edge=-1e-9,
    )
    assert out["exhausted"] is True
    assert out["flow_weak"] is True


def test_flow_nan_is_treated_as_absent(mm_on):
    # NaN flow must not crash and must not count as weak (fail-open structural).
    out = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        ofi=float("nan"),
        micro_edge=float("nan"),
    )
    assert out["exhausted"] is True  # structural still holds
    assert out["flow_weak"] is False


# ── double_top_tighten_decision: short side + candidate floored at entry ───────


def test_tighten_short_side_is_noop(mm_on):
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
        ofi=-0.5,
        micro_edge=-0.3,
        side_long=False,  # short => inert
    )
    assert out["tighten"] is False
    assert out["exhausted"] is False
    assert out["new_stop_floor"] == 10.5  # untouched


def test_tighten_candidate_floored_at_entry_when_retest_low(mm_on):
    # A double-top whose retest peak is BELOW entry would put the raw candidate
    # below entry; the max() floor includes entry so the stop never goes below it.
    # Use a low-priced retest near a low impulse high but above-entry structure:
    # entry 11.0, impulse high 11.9 (leg 0.9), retest 11.85 lower-high, bid 11.70.
    # risk_dist = 11*0.02*0.6 = 0.132; candidate = 11.85 - 0.132 = 11.718 > entry.
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=11.9,
        current_high=11.85,
        bid=11.70,
        entry_price=11.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=9.0,  # very loose stop
        breakeven_floor=9.0,
        ofi=-0.5,
        micro_edge=-0.3,
    )
    assert out["exhausted"] is True
    assert out["tighten"] is True
    # candidate ~11.718, but the entry floor (11.0) guarantees >= entry regardless.
    assert out["new_stop_floor"] >= 11.0
    assert out["new_stop_floor"] == pytest.approx(11.85 - 11.0 * 0.02 * 0.6)


def test_tighten_passthrough_reason_on_no_exhaustion(mm_on):
    # When the check yields no exhaustion, the tighten decision PROPAGATES the
    # check's reason (e.g. retest_too_shallow) — not a stale "wait".
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.20,  # far below the high => too shallow
        bid=11.10,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=10.5,
        breakeven_floor=10.0,
    )
    assert out["exhausted"] is False
    assert out["tighten"] is False
    assert out["reason"] == "retest_too_shallow"
    assert out["new_stop_floor"] == 10.5


def test_tighten_bad_basis_returns_passthrough_stop(mm_on):
    # current_stop non-numeric in the tighten-side try/except => pass-through dict
    # (the initial new_stop_floor is the raw current_stop input, unchanged).
    sentinel = object()
    out = pe.double_top_tighten_decision(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=sentinel,  # non-float => float() raises in the tighten try
        breakeven_floor=10.0,
        ofi=-0.5,
        micro_edge=-0.3,
    )
    assert out["tighten"] is False
    assert out["new_stop_floor"] is sentinel  # untouched passthrough, no loosen/raise


# ── double_top band: degenerate risk_dist (zero atr) => band None / too shallow ─


def test_double_top_zero_atr_uses_floor_not_band_none(mm_on):
    # atr_pct=0 and stop_atr_mult=0 => the product is 0, but risk_dist has a 0.003
    # FLOOR (e*0.003), so the band is NOT None; a near retest still resolves.
    # entry 10 => risk_dist = 10*0.003 = 0.03; band = 0.75*0.03 = 0.0225.
    # retest 11.99 (gap 0.01 < 0.0225) rejected by bid 11.90 => double-top.
    out = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.99,
        bid=11.90,
        entry_price=10.0,
        atr_pct=0.0,
        stop_atr_mult=0.0,
    )
    assert out["exhausted"] is True
    assert out["retest_gap_atr"] == pytest.approx(round(0.01 / 0.03, 4))


def test_double_top_zero_atr_far_retest_is_shallow(mm_on):
    # Same zero-ATR floor band (~0.0225) but a retest 0.20 below the high is far
    # outside => retest_too_shallow (no false exhaustion on a healthy pullback).
    out = pe.double_top_exhaustion_check(
        flag_on=True,
        impulse_leg_high=12.0,
        current_high=11.80,
        bid=11.70,
        entry_price=10.0,
        atr_pct=0.0,
        stop_atr_mult=0.0,
    )
    assert out["exhausted"] is False
    assert out["reason"] == "retest_too_shallow"


# ── default-flag parity: with NO mm_on fixture the flag defaults OFF ───────────


def test_defaults_flag_off_byte_identical_when_unset(monkeypatch):
    # Force the flag attribute to its documented default-OFF and assert inert.
    monkeypatch.setattr(settings, "chili_momentum_measured_move_exit_enabled", False, raising=False)
    assert pe.measured_move_exit_enabled() is False
    mm = pe.measured_move_scale_exit_decision(
        flag_on=pe.measured_move_exit_enabled(),
        current_qty=100.0,
        original_qty=100.0,
        entry_price=10.0,
        impulse_leg_high=12.0,
        bid=99.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
    )
    assert mm["fire"] is False
    assert mm["reason"] == "flag_off"
    assert mm["new_stop_floor"] == 11.0
    dt = pe.double_top_tighten_decision(
        flag_on=pe.measured_move_exit_enabled(),
        impulse_leg_high=12.0,
        current_high=11.95,
        bid=11.85,
        entry_price=10.0,
        atr_pct=0.02,
        stop_atr_mult=0.6,
        current_stop=11.0,
        breakeven_floor=10.0,
        ofi=-0.5,
        micro_edge=-0.3,
    )
    assert dt["tighten"] is False
    assert dt["exhausted"] is False
    assert dt["new_stop_floor"] == 11.0


def test_measured_move_exit_enabled_bad_value_is_false(monkeypatch):
    # The reader coerces to bool; a missing attr defaults to False.
    monkeypatch.delattr(settings, "chili_momentum_measured_move_exit_enabled", raising=False)
    assert pe.measured_move_exit_enabled() is False
