"""[7] THE G4 LEVEL-1 NON-STRUCTURAL SUBSTITUTE FAILS OPEN ON MISSING DATA, SIZE-CONDITIONED.

Before (origin/main @ 42422964f, ``risk_policy.reentry_escalation_decision`` step 1)::

    _sub_ok = bool(
        _tape_positive()
        and _sub_req is not None
        and _sub_band is not None
        and _price_ge(_sub_req + _sub_band)
    )

Two ABSENCES of data were read as refusals, and both contradict the rest of the very
same function:

1. NO RECLAIM REFERENCE (``_sub_req is None`` -- no ``prior_high_print`` / ``prior_hwm``
   / ``prior_exit_price``).  The substitute is then UNSATISFIABLE, so in a session
   seeded at level 1 by #1252's cross-day rejection memory -- which by construction has
   no leg TODAY -- every non-structural fire is refused for the whole day.  Step 2 of
   the same function SKIPS the reclaim when the reference is missing ("partial raise
   rather than a starving block on absent bookkeeping"), and
   ``prior_day_rejection_seed``'s own docstring promised the opposite of the shipped
   code: "sa fresh session ay walang reclaim reference, kaya ang hinihingi lamang ay
   structural trigger + positibong tape".
2. AN UNREADABLE TAPE (``accel``, ``buy_share_delta`` and back buy share all None)
   makes ``_tape_positive()`` False -- while step 3 of the same function and level 0
   ([59]) both SKIP on an unreadable tape ("an unreadable tape never starves").

MEASURED on the live ``chili`` DB, 3 days, level 1, reason ``non_structural_trigger``
(4,223 blocks): 2,999 = 71.0% carry NO reference (WYHG 1320 / TNON 709 / SUNE 609 /
DPU 314 / BNC 47, every one traced to a cross-day seed -- 63 seed events, ZERO same-day),
1,180 = 27.9% are a genuinely low price, and only 44 = 1.0% are the row's original
premise.  All 2,999 carry a READABLE band with a NULL reference, so class A is exactly
"missing reference".

Now: both doors open, and neither opens at FULL size (mechanism, not binary) --
``substitute_fail_open_size_multiplier`` composes them multiplicatively inside
``[chili_momentum_frontside_size_floor, 1.0]``, never 0, so a door can never become a
new veto.  Derivations for both multipliers are in ``app/config.py``.

REVIEW FIXES (2026-09-11), each with a behavioural test below:

* **Door 1 is LEVEL-BOUNDED.**  It makes the ladder's ``(level-1) * prior_risk_dist``
  margin vacuous -- the margin is computed inside ``_reclaim_required()``, which returns
  ``(None, None)`` in exactly the case the door opens -- so without a bound the 5th
  stop-out of the day enters at the same 0.81 as the 1st.  The derivation and the whole
  measured population are level 1 (5,627 rows / 7 days); the level >= 2 no-reference
  population is ZERO rows over the full 60-day retention of
  ``trading_automation_events``.  Deeper rungs keep refusing and are NAMED
  (``substitute_no_reference_level_unmeasured``).
* **A door-1 pass no longer advertises a bar it never applied**: ``margin_r`` is None,
  ``reclaim_form`` is ``no_reference_unenforced``, and the value that did NOT run rides
  along as ``margin_r_unenforced``.
* **A REFUSAL is byte-identical.**  ``size_multiplier`` / ``size_multiplier_binding`` /
  ``substitute_form`` exist in ``dbg`` ONLY on a pass that actually opened a door, so
  the 1,141-2,061 rows/day ``g4_reentry_escalation_blocked`` event (emitted as ``**dbg``
  on both the trigger and the continuation path) does not grow by a byte.
* **Door 2 is NOT parity with step 3 / level 0** and is no longer described as such:
  those skip on ``tape_accel is None`` alone, door 2 needs all three tape reads absent.
* **The decision is PURE**: the two multipliers and the floor fall back to NAMED module
  constants, never to ``settings``.
* **The floor-of-the-floor literal is NAMED** as not-derived
  (``_G4_SUBSTITUTE_SIZE_FLOOR_GUARD``) and reported when it binds.

WHAT KEEPS REFUSING.  Class C -- reference present, price clean, tape readable but WEAK
(9 rows) -- earns: AHMA 2026-09-10 13:48:33Z accel -28,237 share 0.429 -> MFE +2.67% /
MAE -18.72%; FTFT 2026-09-09 16:39:42Z accel -16,092 share 0.254 -> 0.00% / -21.81%;
BIAF 2026-09-09 12:09:39Z accel -9,025 share 0.420 -> +8.00% / -2.07%.  Class B --
reference present, price genuinely below required (1,180 rows) -- is NOT measured to be
a knife: same sampling as 0.81 (one sample per 15-min bucket per symbol, forward 15-min
MFE >= 2%, n=29 buckets / 13 symbols) gives 14/29 = 0.483 against the 0.548 we trade at
full size => ratio 0.88, MAE p50 -3.38% with a tail to -20.29%.  It is left refusing
because the price half is the ladder's own contract (CLRO 07-02); opening it is its own
design with its own refuter, and 0.88 is written into planner row [7].
"""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.risk_policy import (
    _G4_SUBSTITUTE_NO_REFERENCE_MAX_LEVEL,
    _G4_SUBSTITUTE_NO_REFERENCE_SIZE_MULT,
    _G4_SUBSTITUTE_SIZE_FLOOR,
    _G4_SUBSTITUTE_SIZE_FLOOR_GUARD,
    _G4_SUBSTITUTE_UNREADABLE_TAPE_SIZE_MULT,
    reentry_escalation_decision,
    substitute_fail_open_size_multiplier,
)

# The shipped, derived values — passed explicitly so the tests pin the CONTRACT and
# not whatever the environment's settings happen to be.
NO_REF_MULT = 0.81
UNREADABLE_MULT = 0.48
FLOOR = 0.25

_MULTS = dict(
    substitute_no_reference_size_mult=NO_REF_MULT,
    substitute_unreadable_tape_size_mult=UNREADABLE_MULT,
    substitute_size_floor=FLOOR,
)

# The keys [7] adds to the decision's debug dict. THE INVARIANT: they exist if and only
# if a fail-open door actually opened on a PASS — never on a refusal, which is what the
# 1,141-2,061 rows/day blocked event is made of.
DOOR_KEYS = ("size_multiplier", "size_multiplier_binding", "substitute_form")
NEW_KEYS = DOOR_KEYS + ("margin_r_unenforced", "substitute_no_reference_level_unmeasured")


def _assert_no_door_keys(dbg: dict) -> None:
    present = sorted(k for k in DOOR_KEYS if k in dbg)
    assert present == [], present


def _decide(**kw):
    base = dict(
        enabled=True,
        escalation_level=1,
        structural_trigger=False,
        live_price=None,
        prior_hwm=None,
        prior_exit_price=None,
        prior_risk_dist=None,
        tape_accel=None,
        **_MULTS,
    )
    base.update(kw)
    return reentry_escalation_decision(**base)


# ── (1) DOOR 1: no reference + positive tape => allowed, tape-only, x0.81 ─────────
def test_no_reference_with_positive_tape_is_allowed_tape_only_and_derated() -> None:
    # The 2,999-row class A (71.0% of level-1 non-structural blocks). The price half is
    # VACUOUS — there is nothing to reclaim — so the tape alone decides, exactly what
    # #1252's own docstring promised.
    allowed, dbg = _decide(live_price=1.87, tape_accel=12_400.0, noise_abs=0.02)
    assert allowed is True
    assert dbg["reason"] == "non_structural_substitute_no_reference"
    assert dbg["substitute_form"] == "no_reference_tape_only"
    assert dbg["reclaim_structural_substitute"] is True
    # The reference was never proven, so the pass must not claim proof.
    assert dbg["reclaim_proven"] is False
    assert dbg["size_multiplier"] == pytest.approx(0.81)
    assert dbg["size_multiplier_binding"]["doors"] == ["no_reference"]
    assert dbg["size_multiplier_binding"]["floor_bound"] is False
    # the values came from the caller's contract, not from the process environment
    assert dbg["size_multiplier_binding"]["mult_basis"] == "argument"


# ── (2) NEGATIVE CONTROL: no reference + READABLE NEGATIVE tape => still blocked ──
def test_no_reference_with_readable_negative_tape_still_blocks() -> None:
    # 1,450 of the 2,999 class-A rows are tape-NEGATIVE. The missing reference makes the
    # price half vacuous; it does NOT excuse a tape that is readable and refusing.
    allowed, dbg = _decide(live_price=1.87, tape_accel=-28_237.0, noise_abs=0.02)
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"
    # REVIEW FIX: a refusal carries NO new key — the heavy event stays byte-identical.
    # The class is still readable from fields that predate [7] (`substitute_required`
    # null = no reference), which is exactly the split the derivation used.
    _assert_no_door_keys(dbg)
    assert dbg["substitute_required"] is None


# ── (3) BOTH DOORS: no reference + unreadable tape => allowed at 0.81 x 0.48 ─────
def test_no_reference_and_unreadable_tape_compose_multiplicatively() -> None:
    # 28 of the class-A rows carry NO readable tape at all (accel None AND back buy
    # share None). Step 3 and level 0 both skip on this; step 1 alone punished it.
    allowed, dbg = _decide(live_price=1.87, tape_accel=None, tape_back_buy_share=None)
    assert allowed is True
    assert dbg["substitute_form"] == "no_reference_tape_only+unreadable_tape"
    assert dbg["size_multiplier"] == pytest.approx(round(0.81 * 0.48, 4))
    assert dbg["size_multiplier"] == pytest.approx(0.3888)
    assert dbg["size_multiplier_binding"]["doors"] == ["no_reference", "unreadable_tape"]
    # 0.3888 is above the documented floor, so the floor does not bind today.
    assert dbg["size_multiplier_binding"]["floor_bound"] is False


# ── (4) CLASS B UNTOUCHED: reference present + price below required => blocked ───
def test_reference_present_but_price_below_required_still_blocks() -> None:
    # The 1,180-row class the substitute exists for (CLRO 07-02 loss-chase). required =
    # HWM 6.90 + (level-1)*R 0 + one R 0.05 = 6.95; 6.80 is below it.
    # MEASURED (review fix, same sampling as 0.81): class B hits 14/29 = 0.483 vs the
    # 0.548 full-size reference => 0.88, MAE p50 -3.38%, tail -20.29%. It is a size-down
    # and NOT a knife — but the price half is the ladder's own contract, so opening it
    # is its own design with its own refuter (planner row [7]), not this review.
    allowed, dbg = _decide(
        live_price=6.80, prior_hwm=6.90, prior_exit_price=6.70,
        prior_risk_dist=0.05, tape_accel=12_400.0,
    )
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"
    assert dbg["substitute_required"] == pytest.approx(6.95)
    _assert_no_door_keys(dbg)


# ── (5) DOOR 2 ALONE: reference + clean price + unreadable tape => x0.48 ─────────
def test_reference_and_clean_price_with_unreadable_tape_is_allowed_derated() -> None:
    # The TPET pocket (35 class-C rows, price >= required, tape unreadable). The reclaim
    # IS proven here, so the reason stays the informative step-2 one.
    allowed, dbg = _decide(
        live_price=7.01, prior_hwm=6.90, prior_exit_price=6.70,
        prior_risk_dist=0.05, tape_accel=None, tape_back_buy_share=None,
    )
    assert allowed is True
    assert dbg["substitute_form"] == "unreadable_tape"
    assert dbg["reason"] == "reclaim_met"
    assert dbg["reclaim_proven"] is True
    assert dbg["size_multiplier"] == pytest.approx(0.48)
    assert dbg["size_multiplier_binding"]["doors"] == ["unreadable_tape"]
    # door 2 does NOT touch the price half, so the margin really did decide here and
    # the receipt keeps saying so
    assert dbg["reclaim_form"] == "escalated_reclaim_with_margin"
    assert dbg["margin_r"] == 0
    assert "margin_r_unenforced" not in dbg


# ── (6) THE REFUSAL THAT EARNS: reference + clean price + WEAK readable tape ─────
def test_reference_and_clean_price_with_weak_readable_tape_still_blocks() -> None:
    # AHMA 2026-09-10 13:48:33Z, the measured negative control: price 1.87 above the
    # required reclaim, accel -28,237, back buy share 0.429 => MFE +2.67% but MAE
    # -18.72% in the next 15 minutes. A readable tape that refuses is EVIDENCE.
    allowed, dbg = _decide(
        live_price=1.87, prior_hwm=1.80, prior_exit_price=1.75,
        prior_risk_dist=0.02, tape_accel=-28_237.0, tape_back_buy_share=0.429,
    )
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"
    _assert_no_door_keys(dbg)


# ── (7) FULL EVIDENCE => byte-identical sizing (no door, no keys) ────────────────
def test_full_evidence_pass_is_full_size() -> None:
    allowed, dbg = _decide(
        live_price=7.01, prior_hwm=6.90, prior_exit_price=6.70,
        prior_risk_dist=0.05, tape_accel=12_400.0, tape_back_buy_share=0.62,
    )
    assert allowed is True
    assert dbg["reason"] == "reclaim_met"
    _assert_no_door_keys(dbg)


# ── (8) THE FLOOR: a door can NEVER become a veto ────────────────────────────────
def test_multiplier_is_clamped_to_the_floor_and_is_never_zero() -> None:
    # Absurd settings (someone sets both doors to 0.01) still trade at the ONE
    # documented base floor — conditioning, never a hard veto.
    mult, binding = substitute_fail_open_size_multiplier(
        no_reference=True, tape_unreadable=True,
        no_reference_mult=0.01, unreadable_mult=0.01, floor=FLOOR,
    )
    assert mult == pytest.approx(FLOOR)
    assert mult > 0.0
    assert binding["floor_bound"] is True
    # and it is never lifted above full size either
    mult_hi, _ = substitute_fail_open_size_multiplier(
        no_reference=True, tape_unreadable=False,
        no_reference_mult=4.0, unreadable_mult=1.0, floor=FLOOR,
    )
    assert mult_hi == pytest.approx(1.0)
    # a garbage floor cannot zero the position either
    mult_z, binding_z = substitute_fail_open_size_multiplier(
        no_reference=True, tape_unreadable=True,
        no_reference_mult=0.0001, unreadable_mult=0.0001, floor=0.0,
    )
    assert mult_z > 0.0
    assert binding_z["floor"] > 0.0
    # no door at all => exactly 1.0, no derate
    mult_none, binding_none = substitute_fail_open_size_multiplier(
        no_reference=False, tape_unreadable=False,
        no_reference_mult=NO_REF_MULT, unreadable_mult=UNREADABLE_MULT, floor=FLOOR,
    )
    assert mult_none == pytest.approx(1.0)
    assert binding_none["doors"] == []


# ── (8b) REVIEW FIX: the floor-of-the-floor is a NAMED, not-derived guard ────────
def test_the_floor_guard_is_named_reachable_and_reported() -> None:
    # The first form hid a bare ``0.01`` inside ``min(max(_pos(floor, 0.25), 0.01), 1.0)``
    # in a change whose whole argument is that every value is derived and reported. It
    # binds only for a configured floor in (0, 0.01) — which ``floor=0.0`` never reaches,
    # because ``_pos`` substitutes 0.25 for a non-positive floor. So it was named
    # nowhere AND executed nowhere. It is a STRUCTURAL guard, not a measured value: a
    # floor configured at ~0 would turn conditioning into a de-facto veto, the one thing
    # the doors may never become.
    assert _G4_SUBSTITUTE_SIZE_FLOOR_GUARD == 0.01
    mult, binding = substitute_fail_open_size_multiplier(
        no_reference=True, tape_unreadable=True,
        no_reference_mult=0.0001, unreadable_mult=0.0001, floor=0.001,
    )
    assert mult == pytest.approx(_G4_SUBSTITUTE_SIZE_FLOOR_GUARD)
    assert binding["floor_guard"] == _G4_SUBSTITUTE_SIZE_FLOOR_GUARD
    assert binding["floor_guard_basis"] == "not_derived_structural_guard"
    assert binding["floor_configured"] == pytest.approx(0.001)
    # the shipped floor never reaches the guard, so it is reported only where it decided
    _, shipped = substitute_fail_open_size_multiplier(
        no_reference=True, tape_unreadable=True,
        no_reference_mult=NO_REF_MULT, unreadable_mult=UNREADABLE_MULT, floor=FLOOR,
    )
    assert "floor_guard" not in shipped


# ── (9) THE LEVEL-0 PATH ([59]) IS UNTOUCHED BY THE NEW BRANCHES ─────────────────
def test_level0_path_is_untouched() -> None:
    # no reference at level 0 => no_escalation, exactly as before (fail-open), and no
    # substitute form / derate is invented for it.
    allowed, dbg = _decide(escalation_level=0, live_price=1.87, tape_accel=12_400.0)
    assert allowed is True
    assert dbg["reason"] == "no_escalation"
    _assert_no_door_keys(dbg)
    # level 0 WITH a reference still runs the [59] bar, unchanged: a print below the
    # prior leg's high print waits, an unreadable tape is still skipped there.
    allowed, dbg = _decide(
        escalation_level=0, live_price=3.60, prior_high_print=4.39, tape_accel=12_400.0,
    )
    assert allowed is False
    assert dbg["reason"] == "reclaim_of_prior_leg_high_wait"
    _assert_no_door_keys(dbg)
    allowed, dbg = _decide(
        escalation_level=0, live_price=4.40, prior_high_print=4.39,
        tape_accel=None, tape_back_buy_share=None,
    )
    assert allowed is True
    assert dbg["reason"] == "reclaim_met_level0"
    assert dbg["tape_hold"] == "unreadable_skipped"
    _assert_no_door_keys(dbg)


# ── the structural path never reaches step 1's substitute at all ─────────────────
def test_structural_trigger_never_takes_the_substitute_path() -> None:
    allowed, dbg = _decide(
        structural_trigger=True, live_price=6.98, prior_hwm=6.90,
        prior_exit_price=6.80, prior_risk_dist=0.05, tape_accel=12_400.0,
    )
    assert allowed is True
    assert dbg["reason"] == "reclaim_met"
    _assert_no_door_keys(dbg)


# ─────────────────────────────────────────────────────────────────────────────────
# REVIEW FIX (1) — THE NO-REFERENCE DOOR IS BOUNDED BY THE MEASURED POPULATION.
# ─────────────────────────────────────────────────────────────────────────────────
def test_the_no_reference_door_is_bounded_to_the_measured_escalation_level() -> None:
    # The ladder's ENTIRE margin is ``max(0, lvl - 1) * prior_risk_dist``, computed
    # inside ``_reclaim_required()`` — which returns (None, None) in exactly the case
    # this door opens. So the margin is vacuous precisely where the door fires: without
    # a bound, a name that has stopped us out five times today enters on tape alone at
    # the SAME 0.81 as its first re-entry, where the shipped intent demands four extra
    # R of proof.
    # MEASURED (live `chili`, 60 days = the full retention of `trading_automation_events`,
    # `g4_reentry_escalation_blocked` / `non_structural_trigger`, no reference =
    # prior_high_print + prior_hwm + prior_exit_price ALL absent): level 1 = 5,627 rows
    # over 7 days; level >= 2 = ZERO rows. The bound costs nothing today and applies the
    # same standard this change used to leave the "reference present, band unreadable"
    # path alone (zero rows => nothing to derive a multiplier from).
    assert _G4_SUBSTITUTE_NO_REFERENCE_MAX_LEVEL == 1
    allowed, dbg = _decide(
        escalation_level=1, live_price=1.87, prior_risk_dist=0.20,
        tape_accel=12_400.0, tape_buy_share_delta=0.05, noise_abs=0.02,
    )
    assert allowed is True
    assert dbg["size_multiplier"] == pytest.approx(0.81)
    for lvl in (2, 3, 4, 5, 9):
        allowed, dbg = _decide(
            escalation_level=lvl, live_price=1.87, prior_risk_dist=0.20,
            tape_accel=12_400.0, tape_buy_share_delta=0.05, noise_abs=0.02,
        )
        assert allowed is False, lvl
        # byte-identical to origin/main: the same reason, and no [7] size keys
        assert dbg["reason"] == "non_structural_trigger", lvl
        _assert_no_door_keys(dbg)
        # ...but the (today empty) population is NAMED so it can be measured the day it
        # appears, instead of being silently admitted at the level-1 multiplier.
        assert dbg["substitute_no_reference_level_unmeasured"] == lvl
    # and the deeper rung is not refused for a MISSING price: with a reference and a
    # clean price it still passes at that level, so this is a bound on the door and not
    # a new lockout.
    allowed, dbg = _decide(
        escalation_level=3, live_price=8.00, prior_hwm=6.90, prior_exit_price=6.70,
        prior_risk_dist=0.05, tape_accel=12_400.0, tape_buy_share_delta=0.05,
    )
    assert allowed is True
    assert dbg["reason"] == "reclaim_met"
    assert dbg["margin_r"] == 2


# ─────────────────────────────────────────────────────────────────────────────────
# REVIEW FIX (2) — THE RECEIPT DOES NOT ADVERTISE A BAR THAT NEVER RAN.
# ─────────────────────────────────────────────────────────────────────────────────
def test_a_door1_pass_reports_no_margin_and_names_the_unenforced_one() -> None:
    # ``margin_r`` and ``reclaim_form`` are stamped BEFORE any door is evaluated and are
    # copied into the `binding` block the pass receipt carries. On a door-1 pass nothing
    # of the kind was checked: ``required_reclaim`` is None and the tape alone decided.
    # Pre-[7] that contradiction could only accompany a REFUSAL ("the bar we did not
    # meet"); it must never ride on a PASS.
    allowed, dbg = _decide(
        escalation_level=1, live_price=1.87, prior_risk_dist=0.20,
        tape_accel=12_400.0, tape_buy_share_delta=0.05, noise_abs=0.02,
    )
    assert allowed is True
    assert dbg["required_reclaim"] is None
    assert dbg["margin_r"] is None
    assert dbg["reclaim_form"] == "no_reference_unenforced"
    assert dbg["margin_r_unenforced"] == 0
    # every other rung keeps reporting the form that really decided
    _, dbg_ref = _decide(
        escalation_level=1, live_price=7.01, prior_hwm=6.90, prior_exit_price=6.70,
        prior_risk_dist=0.05, tape_accel=12_400.0, tape_buy_share_delta=0.05,
    )
    assert dbg_ref["reclaim_form"] == "escalated_reclaim_with_margin"
    assert dbg_ref["margin_r"] == 0
    assert "margin_r_unenforced" not in dbg_ref


# ─────────────────────────────────────────────────────────────────────────────────
# REVIEW FIX (3) — DOOR 2 IS NARROWER THAN STEP 3 / LEVEL 0, AND SAYS SO.
# ─────────────────────────────────────────────────────────────────────────────────
def test_an_accel_unreadable_but_readable_back_share_is_still_refused() -> None:
    # The first form claimed parity with step 3 and level 0 ("an unreadable tape never
    # starves") in three shipped documents. Step 3 skips on ``tape_accel is None`` ALONE
    # and level 0 on ``not _accel_readable()`` ALONE; door 2 needs accel AND
    # buy_share_delta AND back buy share all absent. So this pocket — accel unreadable,
    # back share readable and bearish — is STILL refused at step 1. That is the
    # conservative direction and it matches the measured population (`tape_accel IS NULL
    # AND tape_back_buy_share IS NULL`, n=28), so the narrowness is kept and NAMED
    # rather than the claim being kept and the behaviour widened.
    allowed, dbg = _decide(
        escalation_level=1, live_price=1.87, prior_hwm=None, prior_exit_price=None,
        tape_accel=None, tape_back_buy_share=0.30,
    )
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"
    _assert_no_door_keys(dbg)
    # level 0 on the SAME tape does skip it (the asymmetry, pinned)
    allowed0, dbg0 = _decide(
        escalation_level=0, live_price=4.40, prior_high_print=4.39,
        tape_accel=None, tape_back_buy_share=0.30,
    )
    assert allowed0 is True
    assert dbg0["tape_hold"] == "unreadable_skipped"


# ─────────────────────────────────────────────────────────────────────────────────
# REVIEW FIX (4) — THE DECISION IS PURE: NAMED MODULE DEFAULTS, NEVER `settings`.
# ─────────────────────────────────────────────────────────────────────────────────
def test_the_decision_never_reads_process_settings_for_the_door_multipliers(
    monkeypatch,
) -> None:
    # The first form fell back to ``getattr(settings, ...)`` whenever the three kwargs
    # were omitted, while the docstring said "(PURE, no I/O)" — so a replay or bench
    # harness that omits them silently picked up the operator's environment instead of
    # the contract. The live caller still passes the config values.
    from app.services.trading.momentum_neural import risk_policy as RP

    monkeypatch.setattr(
        RP.settings, "chili_momentum_g4_substitute_no_reference_size_mult", 0.11,
        raising=False,
    )
    monkeypatch.setattr(
        RP.settings, "chili_momentum_g4_substitute_unreadable_tape_size_mult", 0.12,
        raising=False,
    )
    monkeypatch.setattr(
        RP.settings, "chili_momentum_frontside_size_floor", 0.99, raising=False,
    )
    allowed, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=False, live_price=1.87,
        prior_hwm=None, prior_exit_price=None, prior_risk_dist=None,
        tape_accel=12_400.0,
    )
    assert allowed is True
    assert dbg["size_multiplier"] == pytest.approx(_G4_SUBSTITUTE_NO_REFERENCE_SIZE_MULT)
    assert dbg["size_multiplier_binding"]["mult_basis"] == "module_default"
    assert dbg["size_multiplier_binding"]["floor"] == pytest.approx(
        _G4_SUBSTITUTE_SIZE_FLOOR
    )


def test_the_module_defaults_and_the_config_defaults_are_the_same_numbers() -> None:
    # Two homes for one derived value is only safe if they are pinned equal: the module
    # constant is the pure default, `app/config.py` is the operator's knob (and carries
    # the derivation prose).
    from app.config import Settings

    fields = Settings.model_fields
    assert fields["chili_momentum_g4_substitute_no_reference_size_mult"].default == (
        _G4_SUBSTITUTE_NO_REFERENCE_SIZE_MULT
    )
    assert fields["chili_momentum_g4_substitute_unreadable_tape_size_mult"].default == (
        _G4_SUBSTITUTE_UNREADABLE_TAPE_SIZE_MULT
    )
    assert fields["chili_momentum_frontside_size_floor"].default == (
        _G4_SUBSTITUTE_SIZE_FLOOR
    )


# ─────────────────────────────────────────────────────────────────────────────────
# THE DERATE IS A MECHANISM, NOT A RECEIPT: it must reach entry sizing, be cleared
# when the next leg proves itself, and be reported where it decided.
# ─────────────────────────────────────────────────────────────────────────────────

import json  # noqa: E402
from datetime import datetime  # noqa: E402
from types import SimpleNamespace  # noqa: E402

from app.services.trading.momentum_neural import entry_gates as EG  # noqa: E402
from app.services.trading.momentum_neural import live_runner as LR  # noqa: E402

# A cross-day-seeded session: level 1, NO leg today => no reclaim reference at all.
# This is the 2,999-row class A, and it is exactly the shape that used to be refused
# for the whole day.
POS_TAPE = {
    "signed_tape_accel": 5000.0, "back_buy_share": 0.58, "buy_share_delta": 0.12,
    "prints_since_high": 12, "n_ticks": 255,
    "last_print": 1.87, "last_bid": 1.86, "last_ask": 1.88,
}
UNREADABLE_TAPE = {
    "signed_tape_accel": None, "back_buy_share": None, "buy_share_delta": None,
    "prints_since_high": None, "n_ticks": 0,
    "last_print": 1.87, "last_bid": 1.86, "last_ask": 1.88,
}


def _lr_harness(monkeypatch, *, tape, prior=None, high_print=None, level=1):
    now = datetime(2026, 9, 10, 22, 23, 54)
    monkeypatch.setattr(LR, "_utcnow", lambda: now)
    monkeypatch.setattr(LR, "_replay_l2_as_of_or_none", lambda: None)
    emitted: list[tuple[str, dict]] = []
    committed: list[dict] = []
    monkeypatch.setattr(LR, "_emit", lambda db, sess, et, payload: emitted.append((et, payload)))
    monkeypatch.setattr(LR, "_commit_le", lambda sess, le: committed.append(dict(le)))
    monkeypatch.setattr(LR, "same_day_escalation_seed", lambda *a, **k: {
        "level": 0, "stopout_cycles": 0, "source_session_id": None,
        "prior_trade": None, "sessions_seen": 0})
    monkeypatch.setattr(EG, "signed_tape_accel_features", lambda *a, **k: tape)
    monkeypatch.setattr(EG, "prior_leg_high_print", lambda *a, **k: (high_print, 120, True))
    monkeypatch.setattr(EG, "prints_since_exceeds", lambda *a, **k: False)
    monkeypatch.setattr(LR, "_own_tape_noise_floor_pct", lambda db, s, entry_price: (0.0105, 9))
    le = {"g4_reentry_escalation": level, "g4_escalation_seed_checked": True}
    if prior is not None:
        le["g4_prior_trade"] = prior
    sess = SimpleNamespace(id=21605, symbol="TNON", execution_family="alpaca_spot")
    via = SimpleNamespace(viability_score=0.5)
    return le, sess, via, emitted, committed


def test_the_derate_is_stamped_on_the_ledger_for_sizing(monkeypatch) -> None:
    le, sess, via, emitted, _ = _lr_harness(monkeypatch, tape=POS_TAPE)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.88,
    )
    assert (ok, lvl) == (True, 1)
    assert dbg["reason"] == "non_structural_substitute_no_reference"
    # the value that will multiply the risk budget
    assert le["g4_reentry_size_mult"] == pytest.approx(
        float(settings_mult("chili_momentum_g4_substitute_no_reference_size_mult", 0.81))
    )
    assert le["g4_reentry_size_mult_form"] == "no_reference_tape_only"
    # ...and the pass is on the record even though this session has NO prior-leg stash
    # (the whole point: the class-A population is a cross-day seed with no leg today).
    receipts = [p for et, p in emitted if et == "g4_reentry_pass_unproven"]
    assert len(receipts) == 1
    assert receipts[0]["substitute_form"] == "no_reference_tape_only"
    assert receipts[0]["size_multiplier"] == le["g4_reentry_size_mult"]
    assert receipts[0]["size_multiplier_binding"]["doors"] == ["no_reference"]
    assert receipts[0]["reclaim_proven"] is False
    # the receipt reports the margin that did NOT run, under a name that says so
    assert receipts[0]["reclaim_form"] == "no_reference_unenforced"
    assert receipts[0]["margin_r_unenforced"] == 0
    # the high-volume binding block carries the deciding value where it decided
    assert dbg["binding"]["size_multiplier"] == le["g4_reentry_size_mult"]
    assert dbg["binding"]["substitute_form"] == "no_reference_tape_only"
    assert dbg["binding"]["margin_r"] is None
    assert dbg["binding"]["margin_r_unenforced"] == 0


def settings_mult(name: str, dflt: float) -> float:
    from app.config import settings as _s
    return float(getattr(_s, name, dflt) or dflt)


def test_a_fully_proven_pass_clears_a_stale_derate(monkeypatch) -> None:
    # A derate from an earlier fire must NEVER be worn by a leg that proved itself —
    # the frontside_size_tilt defect (2026-09-07) reached again.
    prior = {"exit_price": 1.70, "high_water_mark": 1.75, "risk_dist": 0.02,
             "was_loss": True, "exited_at_utc": "2026-09-10T21:00:00"}
    le, sess, via, emitted, _ = _lr_harness(
        monkeypatch, tape=POS_TAPE, prior=prior, high_print=1.75,
    )
    le["g4_reentry_size_mult"] = 0.81
    le["g4_reentry_size_mult_form"] = "no_reference_tape_only"
    ok, dbg, _lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.88,
    )
    assert ok is True
    assert dbg["reason"] == "reclaim_met" and dbg["reclaim_proven"] is True
    _assert_no_door_keys(dbg)
    assert "g4_reentry_size_mult" not in le
    assert "g4_reentry_size_mult_form" not in le
    # a fully proven pass reports no size keys in the lean binding block...
    assert "size_multiplier" not in dbg["binding"]
    # ...nor in the (deduped) pass receipt
    receipts = [p for et, p in emitted if et.startswith("g4_reentry_")]
    assert receipts, [et for et, _ in emitted]
    for r in receipts:
        for k in DOOR_KEYS:
            assert k not in r, (k, r)


def _emit_call_span(*, occurrence: int) -> tuple[int, int]:
    """Character span of the Nth shipped ``g4_reentry_escalation_blocked`` emit.

    0 = the trigger path, 1 = the momentum-continuation fire. The indentation is
    read off the file instead of being written into the test, so a reindent of the
    surrounding block cannot silently turn this into a no-op.
    """
    needle = '_emit(db, sess, "g4_reentry_escalation_blocked", {\n'
    pos = -1
    for _ in range(occurrence + 1):
        pos = _LR_SOURCE.index(needle, pos + 1)
    start = _LR_SOURCE.rindex("\n", 0, pos) + 1
    indent = " " * (pos - start)
    close = indent + "})\n"
    end = _LR_SOURCE.index(close, pos) + len(close)
    return start, end


def _blocked_emit_payload(dbg: dict, *, level: int, prev_reason: str) -> dict:
    """Run the SHIPPED ``g4_reentry_escalation_blocked`` payload construction.

    The claim under test is about the bytes that reach ``trading_automation_events``,
    and the payload is built at the CALL SITE (``**_g4e_dbg``), not inside the helper —
    so the guard has to execute the call site's own source, not look at
    ``dbg["binding"]``.
    """
    start, end = _emit_call_span(occurrence=0)
    captured: list[tuple[str, dict]] = []
    ns: dict[str, Any] = {
        "_emit": lambda db, sess, et, payload: captured.append((et, payload)),
        "db": None, "sess": None,
        "_prev_reason": prev_reason, "_g4e_level": level, "_g4e_dbg": dbg,
    }
    exec(compile(textwrap.dedent(_LR_SOURCE[start:end]), "<blocked_emit>", "exec"), ns, ns)  # noqa: S102
    assert len(captured) == 1
    return captured[0][1]


def test_a_refusal_emits_no_new_bytes_at_the_live_runner_seam(monkeypatch) -> None:
    # The [59] review fix put a size budget on this event because
    # ``g4_reentry_escalation_blocked`` runs 1,141-2,061 rows/day (re-counted: 09-08
    # 2,061, 09-09 1,635, 09-10 1,141). The first form of [7] put
    # ``size_multiplier`` / ``size_multiplier_binding`` / ``substitute_form`` in the BASE
    # debug dict, so they rode on every return — refusals included — and the emit is
    # ``**_g4e_dbg``: +80..+102 JSON bytes per row, ~91-206 KB/day of constant noise.
    # The guard that was supposed to catch this only looked at ``dbg["binding"]``, so it
    # passed while the claim it was named for was false. This one executes the SHIPPED
    # emit and inspects the payload that would be written.
    neg_tape = dict(POS_TAPE, signed_tape_accel=-28_237.0, buy_share_delta=-0.20,
                    back_buy_share=0.429)
    le, sess, via, _emitted, _ = _lr_harness(monkeypatch, tape=neg_tape)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.88,
    )
    assert ok is False and dbg["reason"] == "non_structural_trigger"
    payload = _blocked_emit_payload(dbg, level=lvl, prev_reason="momentum_ok_rel_vol")
    for k in NEW_KEYS:
        assert k not in payload, k
    assert "size_multiplier" not in payload["binding"]
    assert "substitute_form" not in payload["binding"]
    assert "margin_r_unenforced" not in payload["binding"]
    assert len(repr(dbg["binding"])) < 420, repr(dbg["binding"])
    assert "g4_reentry_size_mult" not in le
    # ...and the cost is MEASURED, not asserted in the abstract: the first form's
    # three constant keys on the same row are +80..+102 JSON bytes, which at
    # 1,141-2,061 rows/day is ~91-206 KB/day of noise.
    shipped_bytes = len(json.dumps(payload, default=str))
    first_form = dict(
        payload,
        size_multiplier=1.0,
        size_multiplier_binding=None,
        substitute_form="no_reference_tape_only",
    )
    first_form_bytes = len(json.dumps(first_form, default=str))
    assert first_form_bytes - shipped_bytes >= 80, first_form_bytes - shipped_bytes


def test_the_continuation_fire_blocked_emit_is_equally_clean(monkeypatch) -> None:
    # The continuation path spreads the same dbg (``**{k: v for k, v in _mcg_dbg...}``),
    # so it inherits any growth. Same shipped-source execution, same assertion.
    neg_tape = dict(POS_TAPE, signed_tape_accel=-28_237.0, buy_share_delta=-0.20,
                    back_buy_share=0.429)
    le, sess, via, _emitted, _ = _lr_harness(monkeypatch, tape=neg_tape)
    ok, dbg, lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_continuation", tick_px=1.88,
    )
    assert ok is False
    start, end = _emit_call_span(occurrence=1)
    captured: list[tuple[str, dict]] = []
    ns: dict[str, Any] = {
        "_emit": lambda db, sess, et, payload: captured.append((et, payload)),
        "db": None, "sess": None,
        "_mcg_level": lvl, "_mcg_dbg": dbg, "_mc_reason": "momentum_continuation",
        "_mc_tape_dbg": {"signed_tape_accel": -28_237.0, "tick_rate": 3.0, "n_ticks": 255},
    }
    exec(compile(textwrap.dedent(_LR_SOURCE[start:end]), "<cont_emit>", "exec"), ns, ns)  # noqa: S102
    assert len(captured) == 1
    payload = captured[0][1]
    for k in NEW_KEYS:
        assert k not in payload, k


def test_the_unreadable_tape_door_composes_at_the_live_runner_seam(monkeypatch) -> None:
    le, sess, via, emitted, _ = _lr_harness(monkeypatch, tape=UNREADABLE_TAPE)
    ok, dbg, _lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.88,
    )
    assert ok is True
    assert dbg["substitute_form"] == "no_reference_tape_only+unreadable_tape"
    expected = round(
        settings_mult("chili_momentum_g4_substitute_no_reference_size_mult", 0.81)
        * settings_mult("chili_momentum_g4_substitute_unreadable_tape_size_mult", 0.48),
        4,
    )
    assert le["g4_reentry_size_mult"] == pytest.approx(expected)
    assert le["g4_reentry_size_mult"] > 0.0


def test_the_derate_keys_are_cleared_per_leg_on_recycle() -> None:
    # per-LEG, not per-session: the escalation LEVEL survives a recycle by design, the
    # SIZE of one admission does not.
    for key in ("g4_reentry_size_mult", "g4_reentry_size_mult_form",
                "g4_reentry_size_post_floor"):
        assert key in LR._RECYCLE_ENTRY_STATE_KEYS, key
    assert "g4_reentry_escalation" not in LR._RECYCLE_ENTRY_STATE_KEYS


# ─────────────────────────────────────────────────────────────────────────────────
# THE DERATE REACHES THE BUDGET, AND BINDS ON THE PAPER LANE TOO ([62]'s lesson, and
# day_open_ramp's before it): a multiplier that lives only in the product is RESTORED
# by `paper_full_size_floor` and is therefore a RECEIPT, not a mechanism, on the lane
# that is actually running.
#
# REVIEW FIX: these used to assert that substrings were PRESENT in live_runner's
# source (`"_safe_mult(_g4_reentry_mult)" in _LR_SOURCE`) and that one index was below
# another — which passes if the term is multiplied into a dead local, if the factor is
# hard-wired to 1.0, or if the live copy of a duplicated block loses it. The shipped
# statements are now EXECUTED, the same treatment `_run_g4_post_floor` already gave the
# post-floor block. (The comment at live_runner.py:41060 asks for exactly this: "TINATAWAG
# na function, hindi nakabaon na bloke, para may MAPAPATAKBONG pagsusuri ng UGALI".)
# ─────────────────────────────────────────────────────────────────────────────────

import pathlib  # noqa: E402
import re  # noqa: E402
import textwrap  # noqa: E402
from typing import Any  # noqa: E402

_LR_SOURCE = pathlib.Path(LR.__file__).read_text(encoding="utf-8")


def _g4_post_floor_block() -> str:
    start = _LR_SOURCE.index("        # [7] G4 SUBSTITUTE DERATE BINDS ON PAPER TOO.")
    end = _LR_SOURCE.index("        # CYCLE-EXHAUSTION BINDS ON PAPER TOO", start)
    return textwrap.dedent(_LR_SOURCE[start:end])


def _run_g4_post_floor(
    *, paper_floor_fired: bool, mult: float, eff: float, le: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if le is None:
        le = {"g4_reentry_size_mult_form": "no_reference_tape_only"}
    ns: dict[str, Any] = {
        "_paper_floor_fired": paper_floor_fired,
        "_g4_reentry_mult": mult,
        "_eff_max_loss": eff,
        "le": le,
    }
    exec(compile(_g4_post_floor_block(), "<g4_post_floor>", "exec"), ns, ns)  # noqa: S102
    return {"eff": ns["_eff_max_loss"], "le": le}


def _sizing_product_stmt() -> str:
    start = _LR_SOURCE.index(
        "        _eff_max_loss = min(\n            float(_base_max_loss) * _safe_mult(_streak_mult)"
    )
    end = _LR_SOURCE.index("\n        )\n", start) + len("\n        )\n")
    return textwrap.dedent(_LR_SOURCE[start:end])


def _risk_mults_stmt() -> str:
    start = _LR_SOURCE.index('            le["risk_mults"] = {\n')
    end = _LR_SOURCE.index("\n            }\n", start) + len("\n            }\n")
    return textwrap.dedent(_LR_SOURCE[start:end])


def _run_sizing(*, g4_mult: float, base: float = 390.0, extra: dict | None = None):
    """Execute the SHIPPED budget statement + receipt block with a 1.0 namespace."""
    stmt = _sizing_product_stmt()
    receipt = _risk_mults_stmt()
    names = set(re.findall(r"_safe_mult\((_[A-Za-z0-9_]+)\)", stmt + receipt))
    assert "_g4_reentry_mult" in names, sorted(names)
    le: dict[str, Any] = {}
    ns: dict[str, Any] = {n: 1.0 for n in names}
    ns.update({
        "_safe_mult": LR._safe_mult,
        "_base_max_loss": base,
        "le": le,
        "_g4_reentry_mult": g4_mult,
    })
    if extra:
        ns.update(extra)
    exec(compile(stmt + receipt, "<sizing>", "exec"), ns, ns)  # noqa: S102
    return ns["_eff_max_loss"], le


def test_the_multiplier_is_in_the_executed_product_and_in_the_receipt() -> None:
    base = 390.0
    # full size => byte-identical budget
    eff_full, le_full = _run_sizing(g4_mult=1.0, base=base)
    assert eff_full == pytest.approx(base)
    assert le_full["risk_mults"]["g4_reentry"] == pytest.approx(1.0)
    # the door's derate actually SHRINKS the risk budget (not a dead local)
    eff_derate, le_derate = _run_sizing(g4_mult=0.81, base=base)
    assert eff_derate == pytest.approx(base * 0.81)
    assert eff_derate < eff_full
    assert le_derate["risk_mults"]["g4_reentry"] == pytest.approx(0.81)
    assert le_derate["risk_mults"]["eff_max_loss"] == pytest.approx(round(base * 0.81, 4))
    # the composed 0.81 x 0.48 door pair too
    eff_both, _ = _run_sizing(g4_mult=0.3888, base=base)
    assert eff_both == pytest.approx(base * 0.3888)
    # ...and it can never lift the budget past the 3x combined-multiplier ceiling
    eff_clamped, _ = _run_sizing(g4_mult=0.81, base=base, extra={"_streak_mult": 10.0})
    assert eff_clamped == pytest.approx(base * 3.0)


def test_the_derate_is_reapplied_after_the_paper_floor() -> None:
    i_floor = _LR_SOURCE.index('"paper_full_size_floor"')
    i_g4 = _LR_SOURCE.index('le["g4_reentry_size_post_floor"]', i_floor)
    i_shelf = _LR_SOURCE.index('"shelf_registration_damper"')
    assert i_floor < i_g4 < i_shelf
    block = _g4_post_floor_block()
    assert "_paper_floor_fired" in block  # no double-apply on the real-money path
    base = 390.0
    out = _run_g4_post_floor(paper_floor_fired=True, mult=0.81, eff=base)
    assert out["eff"] == pytest.approx(base * 0.81)
    rec = out["le"]["g4_reentry_size_post_floor"]
    assert rec["mult"] == pytest.approx(0.81)
    assert rec["effective_usd"] == pytest.approx(round(base * 0.81, 2))
    assert rec["substitute_form"] == "no_reference_tape_only"


def test_no_reapply_at_full_size_or_without_the_floor() -> None:
    out = _run_g4_post_floor(paper_floor_fired=True, mult=1.0, eff=390.0)
    assert out["eff"] == pytest.approx(390.0)
    assert "g4_reentry_size_post_floor" not in out["le"]
    out = _run_g4_post_floor(paper_floor_fired=False, mult=0.81, eff=195.0)
    assert out["eff"] == pytest.approx(195.0)
    assert "g4_reentry_size_post_floor" not in out["le"]


def test_the_post_floor_receipt_never_survives_into_a_full_size_leg() -> None:
    # the frontside_size_tilt defect (2026-09-07): the key is written only when the
    # mult bites, so it must be cleared at the top of EVERY sizing pass. Run the block
    # TWICE on the same ledger — derated, then full size — and watch the receipt go.
    le: dict[str, Any] = {"g4_reentry_size_mult_form": "no_reference_tape_only"}
    out = _run_g4_post_floor(paper_floor_fired=True, mult=0.81, eff=390.0, le=le)
    assert out["le"]["g4_reentry_size_post_floor"]["mult"] == pytest.approx(0.81)
    out = _run_g4_post_floor(paper_floor_fired=True, mult=1.0, eff=390.0, le=le)
    assert "g4_reentry_size_post_floor" not in out["le"]
    assert out["eff"] == pytest.approx(390.0)


def test_the_fill_event_carries_the_size_that_was_staked() -> None:
    # measuring the door against the outcome must not need a snapshot join, so the
    # entry-fill payload carries the stake. Execute the SHIPPED payload fragment.
    i_evt = _LR_SOURCE.index('"live_entry_filled"')
    frag_start = _LR_SOURCE.index(
        '                        "g4_reentry_size_mult": le.get("g4_reentry_size_mult"),',
        i_evt,
    )
    frag_end = _LR_SOURCE.index(
        '"g4_reentry_size_post_floor": le.get("g4_reentry_size_post_floor"),', frag_start
    ) + len('"g4_reentry_size_post_floor": le.get("g4_reentry_size_post_floor"),')
    frag = textwrap.dedent(_LR_SOURCE[frag_start:frag_end])
    le = {
        "g4_reentry_size_mult": 0.81,
        "g4_reentry_size_mult_form": "no_reference_tape_only",
        "g4_reentry_size_post_floor": {"mult": 0.81, "effective_usd": 315.9},
    }
    ns: dict[str, Any] = {"le": le}
    exec(compile("_payload = {\n" + frag + "\n}\n", "<fill_payload>", "exec"), ns, ns)  # noqa: S102
    assert ns["_payload"] == {
        "g4_reentry_size_mult": 0.81,
        "g4_reentry_size_mult_form": "no_reference_tape_only",
        "g4_reentry_size_post_floor": {"mult": 0.81, "effective_usd": 315.9},
    }
