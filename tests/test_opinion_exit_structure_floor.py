"""An OPINION exit may not fire before the tape has produced anything to read (2026-09-06).

MEASURED, clean gate-15 baseline, 86 Alpaca + 82 Robinhood symbol-days replayed against the
Ross ledger (scratchpad/exit_census.py and scratchpad/bailout_cadence.py):

  the opinion bailouts ended 189 Ross-WINNER legs
    realised          -$4,627
    left on the table  $41,575  (max bid in the 30 min after the exit, times the shares)
    inside 60 s of the fill   163 of 189
    inside 30 s of the fill   138 of 189, carrying $31,841 of that $41,575
    median hold               22-25 s, two bars in

and over the same baseline the tick-cadence exit (#1261) ended ZERO legs.

The operator's doctrine, stated repeatedly for months: hold while the 10-second candles are
green, exit on the first red bar that breaks the prior bar's low. Testing that rule against
each of those 189 exits says the cadence was still saying HOLD at 101 of them (53%), worth
$24,123 of what was left behind. So the doctrine is right about those 101 -- but it is not
the whole answer either, because at the other 88 the rule agreed and the price still came
back. What separates them is not the rule, it is WHEN the rule is allowed to speak: two
completed bars into a small-cap breakout, any routine pullback satisfies "a red bar broke
the prior low".

Hence a floor, derived from that hold distribution rather than chosen: below it, the three
opinion exits may not fire. The structural stop, the #769 max-loss circuit and the
burst-window exit are evaluated ABOVE those blocks on every tick and are deliberately NOT
gated, so a genuinely collapsing position exits on the same tick it always did.

Runnable: pytest tests/test_opinion_exit_structure_floor.py -v   (DB-free)
"""
from __future__ import annotations

import inspect

from app.config import Settings
from app.services.trading.momentum_neural import live_runner as lr

FLOOR = 30.0


def test_the_floor_is_the_measured_p75_and_is_on():
    field = Settings.model_fields["chili_momentum_opinion_exit_min_hold_seconds"]
    assert field.default == FLOOR
    assert "p75" in (field.description or "")
    assert "189" in (field.description or "")


def test_below_the_floor_is_blocked_and_above_it_is_not():
    for held in (0.0, 0.5, 12.0, 29.999):
        blocked, dbg = lr.opinion_exit_structure_floor(held)
        assert blocked is True and dbg["reason"] == "below_structure_floor", held
        assert dbg["min_hold_seconds"] == FLOOR
    for held in (30.0, 31.0, 600.0):
        blocked, dbg = lr.opinion_exit_structure_floor(held)
        assert blocked is False and dbg["reason"] == "structure_present", held


def test_it_fails_open_because_a_floor_it_cannot_measure_must_not_suppress_an_exit():
    """The failure mode this avoids is the expensive one: a stop-shaped exit silently
    held back forever by a guard that could not read the clock."""
    for bad in (None, "x", float("nan"), float("inf"), -5.0):
        blocked, _ = lr.opinion_exit_structure_floor(bad)
        assert blocked is False, bad
    # and a floor of zero is the pre-2026-09-06 behaviour, exactly
    for held in (0.0, 1.0, 900.0):
        assert lr.opinion_exit_structure_floor(held, min_hold_seconds=0.0)[0] is False


def test_all_three_opinion_exits_are_gated():
    src = inspect.getsource(lr.tick_live_session)
    for trigger in ("smart_hold_fast_bail", "breakout_failed_to_hold",
                    "lost_vwap_flatten", "bos_exit"):
        assert f'trigger="{trigger}", held_seconds=held' in src, trigger


def test_the_stop_the_max_loss_circuit_and_the_burst_exit_are_NOT_gated():
    """The whole safety argument rests on this: the floor delays an OPINION, never a stop.

    Asserted by what the gate is attached to, not by source position -- suppressing an
    earlier block does not skip a later one, so ordering proves nothing on its own. There
    are exactly four gated sites and they are the four opinion exits."""
    src = inspect.getsource(lr.tick_live_session)
    calls = src.count("_opinion_exit_suppressed(")
    assert calls == 4, calls
    gated = {t for t in ("smart_hold_fast_bail", "breakout_failed_to_hold",
                         "lost_vwap_flatten", "bos_exit")
             if f'trigger="{t}"' in src}
    assert len(gated) == 4
    # none of the three safety exits sits inside a gated condition
    for anchor in ('"reason": "max_loss_circuit"', '"live_burst_window_exit"',
                   'reason="momentum_break_stop"'):
        i = src.find(anchor)
        assert i > 0, anchor
        assert "_opinion_exit_suppressed(" not in src[max(0, i - 1200):i], anchor


def test_an_unknown_fill_time_is_not_treated_as_a_young_position():
    """The defect this closes, found by reviewing this change before it was benched.

    `opened_at_utc` is parsed inside a try/except whose fallback is `t0 = _utcnow()`. When
    it cannot be parsed, `held` is 0.0 on this tick -- and 0.0 again on the next, and the
    one after. A floor that trusted that number would suppress all three opinion exits for
    the LIFE of the session. That is not a delayed opinion, it is a deleted one, and it is
    the opposite of the fail-open promise this guard makes."""
    src = inspect.getsource(lr.tick_live_session)
    i = src.find('opened_raw = pos.get("opened_at_utc")')
    assert i > 0
    parse = src[i:i + 700]
    assert "held_is_measured = True" in parse and "held_is_measured = False" in parse
    # every gated site carries the flag through
    assert src.count("held_is_measured=held_is_measured") == 4
    gate = inspect.getsource(lr._opinion_exit_suppressed)
    j = gate.find("if not held_is_measured:")
    k = gate.find("opinion_exit_structure_floor(")
    assert 0 < j < k, "the unknown-hold check must precede the floor, not follow it"
    assert "return False" in gate[j:k]


def test_the_receipt_marker_is_cleared_on_recycle():
    """A per-trade marker that survives a recycle silences the receipt for the next leg --
    the shape of the burst-stamp incident, in observability rather than in behaviour."""
    assert "opinion_exit_floor_last_trigger" in lr._RECYCLE_ENTRY_STATE_KEYS


def test_the_receipt_is_written_on_change_of_trigger_not_once_per_pass():
    """A per-pass emit is how one decision became 6,765 events once already."""
    src = inspect.getsource(lr._opinion_exit_suppressed)
    assert 'le.get("opinion_exit_floor_last_trigger")' in src
    assert '"live_opinion_exit_below_structure_floor"' in src
    assert "derivation" in src


def test_the_derivation_travels_with_the_number():
    """§2.1 of the plan: a threshold carries its derivation, its sample size and its date,
    or it is a magic number."""
    text = lr._OPINION_EXIT_MIN_HOLD_DERIVATION
    assert "p75" in text and "189" in text and "2026-09-06" in text
