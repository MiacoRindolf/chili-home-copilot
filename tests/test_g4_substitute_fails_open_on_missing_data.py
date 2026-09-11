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

WHAT MUST KEEP REFUSING (both measured to earn): class B (price genuinely below the
required reclaim, 1,180 rows) and class C weak-but-READABLE tape (9 rows: AHMA
2026-09-10 13:48:33Z accel -28,237 share 0.429 -> MFE +2.67% / MAE -18.72%; FTFT
2026-09-09 16:39:42Z accel -16,092 share 0.254 -> 0.00% / -21.81%; BIAF 2026-09-09
12:09:39Z accel -9,025 share 0.420 -> +8.00% / -2.07%).
"""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.risk_policy import (
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


# ── (2) NEGATIVE CONTROL: no reference + READABLE NEGATIVE tape => still blocked ──
def test_no_reference_with_readable_negative_tape_still_blocks() -> None:
    # 1,450 of the 2,999 class-A rows are tape-NEGATIVE. The missing reference makes the
    # price half vacuous; it does NOT excuse a tape that is readable and refusing.
    allowed, dbg = _decide(live_price=1.87, tape_accel=-28_237.0, noise_abs=0.02)
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"
    assert dbg["substitute_form"] == "no_reference_tape_only"
    assert dbg["size_multiplier"] == 1.0  # no door opened => nothing to derate


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
    allowed, dbg = _decide(
        live_price=6.80, prior_hwm=6.90, prior_exit_price=6.70,
        prior_risk_dist=0.05, tape_accel=12_400.0,
    )
    assert allowed is False
    assert dbg["reason"] == "non_structural_trigger"
    assert dbg["substitute_form"] == "price_reclaim_and_tape"
    assert dbg["substitute_required"] == pytest.approx(6.95)
    assert dbg["size_multiplier"] == 1.0


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
    assert dbg["substitute_form"] == "price_reclaim_and_tape"
    assert dbg["size_multiplier"] == 1.0


# ── (7) FULL EVIDENCE => byte-identical sizing (mult 1.0, no door) ───────────────
def test_full_evidence_pass_is_full_size() -> None:
    allowed, dbg = _decide(
        live_price=7.01, prior_hwm=6.90, prior_exit_price=6.70,
        prior_risk_dist=0.05, tape_accel=12_400.0, tape_back_buy_share=0.62,
    )
    assert allowed is True
    assert dbg["reason"] == "reclaim_met"
    assert dbg["substitute_form"] == "price_reclaim_and_tape"
    assert dbg["size_multiplier"] == 1.0
    assert dbg["size_multiplier_binding"] is None


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


# ── (9) THE LEVEL-0 PATH ([59]) IS UNTOUCHED BY THE NEW BRANCHES ─────────────────
def test_level0_path_is_untouched() -> None:
    # no reference at level 0 => no_escalation, exactly as before (fail-open), and no
    # substitute form / derate is invented for it.
    allowed, dbg = _decide(escalation_level=0, live_price=1.87, tape_accel=12_400.0)
    assert allowed is True
    assert dbg["reason"] == "no_escalation"
    assert dbg["substitute_form"] is None
    assert dbg["size_multiplier"] == 1.0
    # level 0 WITH a reference still runs the [59] bar, unchanged: a print below the
    # prior leg's high print waits, an unreadable tape is still skipped there.
    allowed, dbg = _decide(
        escalation_level=0, live_price=3.60, prior_high_print=4.39, tape_accel=12_400.0,
    )
    assert allowed is False
    assert dbg["reason"] == "reclaim_of_prior_leg_high_wait"
    assert dbg["size_multiplier"] == 1.0
    allowed, dbg = _decide(
        escalation_level=0, live_price=4.40, prior_high_print=4.39,
        tape_accel=None, tape_back_buy_share=None,
    )
    assert allowed is True
    assert dbg["reason"] == "reclaim_met_level0"
    assert dbg["tape_hold"] == "unreadable_skipped"
    assert dbg["size_multiplier"] == 1.0


# ── the structural path never reaches step 1's substitute at all ─────────────────
def test_structural_trigger_never_takes_the_substitute_path() -> None:
    allowed, dbg = _decide(
        structural_trigger=True, live_price=6.98, prior_hwm=6.90,
        prior_exit_price=6.80, prior_risk_dist=0.05, tape_accel=12_400.0,
    )
    assert allowed is True
    assert dbg["reason"] == "reclaim_met"
    assert dbg["substitute_form"] is None
    assert dbg["size_multiplier"] == 1.0


# ─────────────────────────────────────────────────────────────────────────────────
# THE DERATE IS A MECHANISM, NOT A RECEIPT: it must reach entry sizing, be cleared
# when the next leg proves itself, and be reported where it decided.
# ─────────────────────────────────────────────────────────────────────────────────

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
    # the high-volume binding block carries the deciding value where it decided
    assert dbg["binding"]["size_multiplier"] == le["g4_reentry_size_mult"]
    assert dbg["binding"]["substitute_form"] == "no_reference_tape_only"


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
    assert dbg["size_multiplier"] == 1.0
    assert "g4_reentry_size_mult" not in le
    assert "g4_reentry_size_mult_form" not in le
    # a fully proven pass reports no size keys in the lean binding block
    assert "size_multiplier" not in dbg["binding"]


def test_a_refusal_keeps_the_binding_block_byte_identical(monkeypatch) -> None:
    # The [59] review fix put a size budget on this block because
    # `g4_reentry_escalation_blocked` runs 1,141-2,061 rows/day. A refusal can never
    # open a door, so the heavy event must not grow at all.
    neg_tape = dict(POS_TAPE, signed_tape_accel=-28_237.0, buy_share_delta=-0.20,
                    back_buy_share=0.429)
    le, sess, via, _emitted, _ = _lr_harness(monkeypatch, tape=neg_tape)
    ok, dbg, _lvl = LR._g4_reentry_escalation_check(
        None, sess, le, via, trigger_reason="momentum_ok_rel_vol", tick_px=1.88,
    )
    assert ok is False and dbg["reason"] == "non_structural_trigger"
    assert "size_multiplier" not in dbg["binding"]
    assert "substitute_form" not in dbg["binding"]
    assert len(repr(dbg["binding"])) < 420, repr(dbg["binding"])
    assert "g4_reentry_size_mult" not in le


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
# THE DERATE BINDS ON THE PAPER LANE TOO ([62]'s lesson, and day_open_ramp's before
# it): a multiplier that lives only in the product is RESTORED by
# `paper_full_size_floor` and is therefore a RECEIPT, not a mechanism, on the lane
# that is actually running.
# ─────────────────────────────────────────────────────────────────────────────────

import pathlib  # noqa: E402
import textwrap  # noqa: E402
from typing import Any  # noqa: E402

_LR_SOURCE = pathlib.Path(LR.__file__).read_text(encoding="utf-8")


def _g4_post_floor_block() -> str:
    start = _LR_SOURCE.index("        # [7] G4 SUBSTITUTE DERATE BINDS ON PAPER TOO.")
    end = _LR_SOURCE.index("        # CYCLE-EXHAUSTION BINDS ON PAPER TOO", start)
    return textwrap.dedent(_LR_SOURCE[start:end])


def _run_g4_post_floor(*, paper_floor_fired: bool, mult: float, eff: float) -> dict[str, Any]:
    le: dict[str, Any] = {"g4_reentry_size_mult_form": "no_reference_tape_only"}
    ns: dict[str, Any] = {
        "_paper_floor_fired": paper_floor_fired,
        "_g4_reentry_mult": mult,
        "_eff_max_loss": eff,
        "le": le,
    }
    exec(compile(_g4_post_floor_block(), "<g4_post_floor>", "exec"), ns, ns)  # noqa: S102
    return {"eff": ns["_eff_max_loss"], "le": le}


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
    # mult bites, so it must be cleared at the top of EVERY sizing pass.
    block = _g4_post_floor_block()
    assert block.index('le.pop("g4_reentry_size_post_floor", None)') < block.index("if _paper_floor_fired")


def test_the_multiplier_is_in_the_product_and_in_the_receipt() -> None:
    assert "_safe_mult(_g4_reentry_mult)" in _LR_SOURCE
    assert '"g4_reentry": round(float(_safe_mult(_g4_reentry_mult)), 4)' in _LR_SOURCE
    i_evt = _LR_SOURCE.index('"live_entry_filled"')
    assert '"g4_reentry_size_mult": le.get("g4_reentry_size_mult")' in _LR_SOURCE[i_evt:]
    assert '"g4_reentry_size_post_floor": le.get("g4_reentry_size_post_floor")' in _LR_SOURCE[i_evt:]
