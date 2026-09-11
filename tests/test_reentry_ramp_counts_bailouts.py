"""THE RE-ENTRY RAMP MUST BIND (A): a loss is a loss.

Two rules said a bailout was free, and the ledger paid for both.

  * ``reentry_escalation_level_update`` (ffc00b673): a NON-stop loss incremented
    the level only when the name was ALREADY at level >= 1 — "a fresh name's
    bailout is a choice". Result on 2026-09-10: 6 bailouts, 0 increments; TNON
    re-entered 4x in 12 minutes at level 0.
  * the terminal stop-out cap (2026-08-27, XPON): ``bailout`` is not stop-class,
    so ``stopout_cycles`` never advanced on one
    (``stopout_cap_skipped_non_stop_class`` x20 in 7 days).

MEASURED LIVE, 7 days to 2026-09-10 (momentum_fill_outcomes, mode=live, 54
closed legs): first legs 18 = +$9.09; RE-ENTRIES 36 = -$502.40; red bailouts
18 = -$661.29 of which 14 were re-entries = -$466.28. The doctrine that a fresh
name's bailout is free is refuted by the ledger it was meant to protect.

The XPON case (three bailouts terminalised a +58% mover) stays covered by the
day-leader exemption of the cap, which recycles PAST the cap at the escalated
bar — not by making the bailout free for every name.

DB-free: both rules are pure. Runnable: pytest tests/test_reentry_ramp_counts_bailouts.py -v
"""
from __future__ import annotations

import pathlib

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as LR
from app.services.trading.momentum_neural.risk_policy import (
    bailout_class_exit_reason,
    reentry_after_stop_allowed,
    reentry_escalation_level_update as upd,
    reentry_ramp_loss_counts,
    reentry_ramp_strike_class,
    stop_class_exit_reason,
    stopout_cycles_after_recycle,
)

_SRC = pathlib.Path(LR.__file__)

# The measurement that justifies the change, pinned so a future reader can see
# what the code was answering to (7 days to 2026-09-10, mode=live).
MEASURED_7D = {
    "closed_legs": 54,
    "first_legs": 18,
    "first_legs_pnl": 9.09,
    "reentries": 36,
    "reentries_pnl": -502.40,
    "red_bailouts": 18,
    "red_bailouts_pnl": -661.29,
    "red_bailouts_on_reentries": 14,
    "red_bailouts_on_reentries_pnl": -466.28,
}


def test_the_measurement_is_net_negative_on_reentries():
    """The premise of the change: re-entries lose, first legs do not."""
    assert MEASURED_7D["reentries_pnl"] < 0 < MEASURED_7D["first_legs_pnl"]
    assert MEASURED_7D["red_bailouts_on_reentries_pnl"] < -400


# ── the level rule ───────────────────────────────────────────────────────────


def test_a_fresh_names_bailout_now_counts():
    """The ffc00b673 exception is gone: level 0 + red bailout => level 1."""
    assert upd(current_level=0, was_loss=True, exit_reason="bailout",
               green_banked=False) == (1, "non_stop_loss_increment")


@pytest.mark.parametrize("reason", ["bailout", "max_hold", "operator_flatten",
                                    "kill_switch_flatten", "governance_exit",
                                    "momentum_break_stop", None, ""])
def test_every_red_exit_is_a_rung(reason):
    lvl, why = upd(current_level=0, was_loss=True, exit_reason=reason, green_banked=False)
    assert lvl == 1
    lvl2, _ = upd(current_level=2, was_loss=True, exit_reason=reason, green_banked=False)
    assert lvl2 == 3


def test_stop_class_semantics_are_untouched():
    assert upd(current_level=0, was_loss=True, exit_reason="stop",
               green_banked=False) == (1, "stop_class_loss_increment")
    assert upd(current_level=2, was_loss=True, exit_reason="trail_stop",
               green_banked=False, rapid_stopout=True) == (4, "rapid_whipsaw_double_increment")


def test_the_rapid_double_increment_stays_stop_class_only():
    """A rapid BAILOUT is one rung, not two: the whipsaw cadence is a stop-class
    contract on both ends (L4) and this change does not widen it."""
    assert upd(current_level=1, was_loss=True, exit_reason="bailout",
               green_banked=False, rapid_stopout=True) == (2, "non_stop_loss_increment")


def test_profit_and_green_paths_are_untouched():
    assert upd(current_level=5, was_loss=False, exit_reason="target",
               green_banked=False) == (4, "profit_recycle_decay")
    assert upd(current_level=7, was_loss=False, exit_reason="target",
               green_banked=True) == (0, "green_banked_reset")


def test_the_revert_knob_restores_ffc00b673_verbatim():
    assert upd(current_level=0, was_loss=True, exit_reason="bailout",
               green_banked=False, count_every_loss=False) == (0, "non_stop_loss_unchanged")
    assert upd(current_level=1, was_loss=True, exit_reason="bailout",
               green_banked=False, count_every_loss=False) == (2, "non_stop_loss_on_escalated_name_increment")


def test_tnon_2026_09_09_now_climbs_the_ladder():
    """TNON re-entered four times in twelve minutes, every exit a bailout, and the
    level never left zero. Under the fix the fourth entry owes three extra R."""
    lvl = 0
    path = []
    for _ in range(4):
        lvl, _why = upd(current_level=lvl, was_loss=True, exit_reason="bailout", green_banked=False)
        path.append(lvl)
    assert path == [1, 2, 3, 4]


def test_wyhg_2026_09_08_reaches_eight():
    exits = ["burst_window_exit", "stop", "bailout", "stop",
             "bailout", "bailout", "bailout", "stop"]
    lvl = 0
    for r in exits:
        lvl, _ = upd(current_level=lvl, was_loss=True, exit_reason=r, green_banked=False)
    assert lvl == 8


# ── the cap ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", ["bailout", "bailout_broker_zero_reconcile", "BAILOUT"])
def test_bailout_token_classifies(reason):
    assert bailout_class_exit_reason(reason) is True
    assert reentry_ramp_loss_counts(reason) is True
    # [23]: the class is named on the counted receipt
    assert reentry_ramp_strike_class(reason) == "bailout"


@pytest.mark.parametrize("reason", ["stop", "trail_stop", "stop_broker_zero_reconcile"])
def test_stop_class_still_counts_for_the_ramp(reason):
    assert reentry_ramp_loss_counts(reason) is True
    assert reentry_ramp_strike_class(reason) == "stop"


@pytest.mark.parametrize("reason", ["kill_switch_flatten", "max_hold", "target",
                                    "scale_out_limit"])
def test_named_non_strike_reasons_still_skip_the_cap(reason):
    assert reentry_ramp_loss_counts(reason) is False
    assert bailout_class_exit_reason(reason) is False
    assert reentry_ramp_strike_class(reason) is None


@pytest.mark.parametrize("reason", [None, "", "tape_accel_rollover", "tape_sellers_took_it"])
def test_since_23_an_unnamed_red_reason_is_a_strike(reason):
    """[23] (2026-09-11) INVERTED the cap's predicate: it used to be a list of what to
    count, so a name it had never seen — the #1385 verdict exits that REPLACED the
    bailout (LBGJ 22135, -358 bps, stopout_cycles 0) — was free. Now a red exit is a
    strike unless it is a NAMED non-strike; a missing reason is a loss all the same
    (the level rule above already counted None / '' as a rung)."""
    assert reentry_ramp_loss_counts(reason) is True
    assert bailout_class_exit_reason(reason) is False


def test_the_one_stop_class_classifier_is_not_widened():
    """The L4 whipsaw cadence keys on stop_class_exit_reason on BOTH ends; the
    ramp composes a second predicate rather than redefining the first."""
    assert stop_class_exit_reason("bailout") is False


def _cycles(reasons):
    n = 0
    for r in reasons:
        counts = reentry_ramp_loss_counts(r)
        n = stopout_cycles_after_recycle(
            prev_stopout_cycles=n, recycle_was_stopout=counts,
            recycle_holds_streak=(not counts),
        )
    return n


def test_three_red_bailouts_now_reach_the_cap():
    n = _cycles(["bailout", "bailout", "bailout"])
    assert n == 3
    ok, why = reentry_after_stop_allowed(
        stopout_cycles=n,
        max_stopout_reentries=int(settings.chili_momentum_max_stopout_reentries),
        enabled=True,
    )
    assert ok is False and why == "max_stopout_reentries_reached"


def test_a_max_hold_between_bailouts_holds_the_streak():
    assert _cycles(["bailout", "max_hold", "bailout", "kill_switch_flatten", "bailout"]) == 3


# ── the runner wiring and the knob ───────────────────────────────────────────


def _cooldown_region() -> str:
    src = _SRC.read_text(encoding="utf-8")
    i = src.index('le["last_recycle_was_stopout"]')
    return src[max(0, i - 6000): i + 400]


def test_the_cap_input_counts_a_bailout_with_a_receipt():
    region = _cooldown_region()
    # [23]: ONE predicate names the class; the stop-class classifier survives only as
    # the named revert path (count_every_loss=False).
    assert "reentry_ramp_strike_class(_recycle_reason)" in region
    assert "stop_class_exit_reason(_recycle_reason)" in region
    assert "stopout_cap_counts_bailout" in region, "a counted bailout must leave a receipt"
    assert '"strike_class": _strike_class' in region, "and the receipt names the class"
    assert "stopout_cap_skipped_non_stop_class" in region, "the other skips still leave theirs"


def test_the_level_update_passes_the_knob_and_emits_a_receipt():
    src = _SRC.read_text(encoding="utf-8")
    i = src.index("_g4_esc, _g4_esc_why = reentry_escalation_level_update(")
    region = src[i: i + 1500]
    assert "count_every_loss=" in region
    assert "g4_reentry_escalation_level_update" in region, "every level change is visible"
    assert '"level_before"' in region and '"level_after"' in region


def test_the_knob_ships_on_with_the_measurement_in_its_description():
    name = "chili_momentum_reentry_ramp_counts_every_loss"
    assert getattr(settings, name) is True
    fields = type(settings).model_fields
    assert name in fields
    desc = str(fields[name].description or "")
    assert "2026-09-10" in desc
    assert "661.29" in desc, "the measured bailout ledger must be on the knob"
    assert "TNON" in desc
