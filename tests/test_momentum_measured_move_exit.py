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
