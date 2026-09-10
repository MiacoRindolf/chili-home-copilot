"""A bailout on a name that has already failed must raise the re-entry bar.

The escalation level is the right mechanism: each failure demands one more full R
of proof before the name may be re-entered. It only counted STOP-class exits, on
the reasoning that a bailout is "we chose to leave", not "the level broke".

That reasoning is right for ONE trade and wrong for a SEQUENCE.

WYHG, live 2026-09-08: eight losing entries in 31 minutes for -$212.83 — 78% of
the day's loss. The exits were 1 burst, 3 stop and 4 BAILOUT, so half the losses
incremented nothing: the level reached 2 instead of 8, and 509 of 512 blocks were
still level 1. The ramp never ramped.

DB-free — the update rule is pure.

2026-09-10 UPDATE (tests/test_reentry_ramp_counts_bailouts.py): the "fresh
name's bailout is a choice" exception below was itself refuted on the live
ledger — 7 days to 2026-09-10: 18 red bailouts -$661.29, 14 on re-entries
-$466.28, TNON 4x in 12 minutes at level 0. EVERY loss is now a rung; the
level-0 semantics tested here are what the revert knob restores.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.risk_policy import (
    reentry_escalation_level_update as upd,
)


def test_a_fresh_names_bailout_counts_since_2026_09_10():
    """At level 0 a bailout used to be "a choice" — measured, it was the first
    of a series (TNON 09-09: four in twelve minutes, level never left 0)."""
    lvl, why = upd(current_level=0, was_loss=True, exit_reason="bailout",
                   green_banked=False)
    assert (lvl, why) == (1, "non_stop_loss_increment")
    # the pre-2026-09-10 semantics survive behind the revert knob
    assert upd(current_level=0, was_loss=True, exit_reason="bailout",
               green_banked=False, count_every_loss=False) == (0, "non_stop_loss_unchanged")


def test_a_bailout_on_an_already_failed_name_now_counts():
    lvl, why = upd(current_level=1, was_loss=True, exit_reason="bailout",
                   green_banked=False)
    assert lvl == 2
    assert why == "non_stop_loss_increment"


def test_a_stop_still_increments_the_same_way():
    assert upd(current_level=0, was_loss=True, exit_reason="stop",
               green_banked=False) == (1, "stop_class_loss_increment")
    assert upd(current_level=3, was_loss=True, exit_reason="stop",
               green_banked=False) == (4, "stop_class_loss_increment")


def test_the_rapid_whipsaw_double_increment_is_untouched():
    assert upd(current_level=2, was_loss=True, exit_reason="stop",
               green_banked=False, rapid_stopout=True) == (4, "rapid_whipsaw_double_increment")


def test_a_green_banked_round_still_resets():
    assert upd(current_level=7, was_loss=False, exit_reason="target",
               green_banked=True) == (0, "green_banked_reset")


def test_a_profit_recycle_still_decays_by_one():
    assert upd(current_level=5, was_loss=False, exit_reason="target",
               green_banked=False) == (4, "profit_recycle_decay")


def test_the_wyhg_ladder_now_escalates():
    """The eight exits of 2026-09-08, in order, through the real rule."""
    exits = ["burst_window_exit", "stop", "bailout", "stop",
             "bailout", "bailout", "bailout", "stop"]
    lvl = 0
    path = []
    for reason in exits:
        lvl, _ = upd(current_level=lvl, was_loss=True, exit_reason=reason,
                     green_banked=False)
        path.append(lvl)
    # Before ffc00b673: burst 0, stop 1, then the four bailouts held it flat.
    # ffc00b673: [0, 1, 2, 3, 4, 5, 6, 7].
    #
    # UPDATED 2026-09-08 (local lane tree, merged 2026-09-10) — the FIRST rung moved:
    # burst_window_exit splits to {burst, window, exit} and carried no ``stop`` token,
    # so level 0 treated it as a choice rather than a break. It is a protective exit,
    # and risk_policy._STOP_CLASS_EXIT_REASONS_WITHOUT_TOKEN now says so.
    # 2026-09-10 (#1376): every loss counts at every level (count_every_loss), which
    # reaches the same ladder by a second route. Eight losses, eight rungs.
    assert path == [1, 2, 3, 4, 5, 6, 7, 8], path
    # By the sixth entry the name owes five extra R of proof — unreachable in chop.
    assert path[5] >= 5


def test_the_old_behaviour_would_have_stalled_at_two():
    """What actually happened, kept as the regression this fixes."""
    exits = ["burst_window_exit", "stop", "bailout", "stop",
             "bailout", "bailout", "bailout", "stop"]
    lvl = 0
    for reason in exits:                       # the pre-fix rule, inlined
        if reason in ("stop", "trail_stop", "stop_loss"):
            lvl += 1
    assert lvl == 3          # only the exits on THAT LIST ever counted
    assert lvl < 8           # against 8 under the fix
    # NOTE: the inlined list above is a HISTORICAL SNAPSHOT of the pre-fix rule and
    # is deliberately not re-derived from risk_policy. Under the current definition
    # burst_window_exit is also stop-class, so a faithful "stop-class only" count
    # today would be 4, not 3. Keeping the literal list preserves what actually ran
    # on 2026-09-08 rather than silently re-scoring history.


@pytest.mark.parametrize("reason", ["bailout", "max_hold", "operator_flatten",
                                    "kill_switch_flatten", "governance_exit"])
def test_every_non_stop_reason_behaves_the_same_way(reason):
    assert upd(current_level=0, was_loss=True, exit_reason=reason,
               green_banked=False)[0] == 1
    assert upd(current_level=2, was_loss=True, exit_reason=reason,
               green_banked=False)[0] == 3


def test_an_unusable_level_is_treated_as_zero():
    assert upd(current_level=None, was_loss=True, exit_reason="bailout",
               green_banked=False) == (1, "non_stop_loss_increment")
