"""The structure trail must be able to hold a runner. Today it never has.

`grind_mode_decision` has produced ZERO `g4_grind_mode` events across the entire
live book. The `g4_grind_probe` telemetry shipped 2026-07-09 to name the binding
gate has answered, and the answer indicts the gate rather than the market:

    reason                       probes  sessions  MEAN PEAK R
    not_day_leader                   72        11         9.31
    no_higher_low_above_entry        54         5        13.53
    below_1r                         22        10         0.54
    cadence_unreadable               14        11         0.35
    cadence_not_fast                  7         2         0.46
    ema_not_held                      2         1        29.04

Trades that reached NINE and THIRTEEN times their risk were refused the structure
trail — precisely the runners it exists to hold. Two category errors cause it:

  * `is_day_leader` is a CROSS-SECTIONAL rank answering a WITHIN-TRADE question,
    and the board's #1 name changes in 41% of minutes.
  * `last_higher_low` needs a pullback-and-continue cycle on 5-MINUTE BARS. A move
    that peaks at 13R can finish inside one or two of them.

This is the third time this gate family has excluded the class it was built for:
the cadence disqualifier was removed in 2026-07-09 because VRAX carried the
SLOW_CHOPPER label all the way to +172%.

`grind_mode_decision_tick` asks the same question of the tape. These tests hold it
to two standards: it must ACTIVATE on the situations the probe recorded as
refusals, and it must still REFUSE everything the bar version was right to refuse.

DB-free; both functions are pure.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    grind_mode_decision,
    grind_mode_decision_tick,
)

# A trade whose numbers put it well past 1R, so peak_r is never the binding gate.
BASE = dict(
    prior_active=False,
    entry_price=10.00,
    bid=11.80,
    atr_pct=0.04,
    stop_atr_mult=0.60,
    high_water_mark=12.00,      # ~8.3R on a 0.024 risk unit
)
TAPE_OK = dict(
    swing_low_now=11.40,        # higher low, above entry
    swing_low_prev=10.60,
    buy_support_px=11.20,       # where the buying actually happened
    signed_tape_accel=4200.0,   # back half still buying harder than the front
    vwap=11.00,
)


def tick(**over):
    return grind_mode_decision_tick(**{**BASE, **TAPE_OK, **over})


# ── the two refusals the probe recorded, replayed ──────────────────────────────

def test_a_nine_r_runner_that_is_not_the_day_leader_now_trails():
    """`not_day_leader`: 72 probes, mean peak 9.31R. The bar version refuses on a
    cross-sectional rank; the tape version never asks the question."""
    bar = grind_mode_decision(
        enabled=True, prior_active=False,
        is_day_leader=False,                 # the only thing wrong
        cadence_cls="FAST",
        entry_price=10.00, bid=11.80, atr_pct=0.04, stop_atr_mult=0.60,
        high_water_mark=12.00, ema_5m=11.20, last_higher_low=11.40, vwap=11.00,
    )
    assert bar["active"] is False and bar["reason"] == "not_day_leader"

    out = tick()
    assert out["active"] is True, out["reason"]
    assert out["peak_r"] > 8.0


def test_a_thirteen_r_runner_whose_higher_low_never_printed_on_5m_bars_now_trails():
    """`no_higher_low_above_entry`: 54 probes, mean peak 13.53R. The move finished
    inside the bar that was supposed to describe it. In prints it is visible."""
    bar = grind_mode_decision(
        enabled=True, prior_active=False, is_day_leader=True, cadence_cls="FAST",
        entry_price=10.00, bid=11.80, atr_pct=0.04, stop_atr_mult=0.60,
        high_water_mark=12.00, ema_5m=11.20,
        last_higher_low=None,                # no 5m swing has completed yet
        vwap=11.00,
    )
    assert bar["active"] is False and bar["reason"] == "no_higher_low_above_entry"

    out = tick()   # the same instant, read in prints
    assert out["active"] is True, out["reason"]


def test_the_leader_flag_is_not_an_input_at_all():
    """Not merely defaulted — the question is gone. It cannot be reintroduced by a
    caller passing it."""
    with pytest.raises(TypeError):
        grind_mode_decision_tick(**{**BASE, **TAPE_OK}, is_day_leader=True)


def test_cadence_is_not_an_input_either():
    """Removed for the same reason in 2026-07-09: VRAX was SLOW_CHOPPER to +172%."""
    with pytest.raises(TypeError):
        grind_mode_decision_tick(**{**BASE, **TAPE_OK}, cadence_cls="SLOW_CHOPPER")


# ── it must still refuse what the bar version was right to refuse ──────────────

def test_below_one_r_still_refuses():
    """22 probes at mean 0.54R — the bar version was RIGHT here, and this is the
    self-relative condition worth keeping."""
    out = tick(high_water_mark=10.01, bid=10.00)
    assert out["active"] is False
    assert out["reason"] == "below_1r"


def test_a_lower_low_refuses():
    """No pullback-and-continue cycle: the tape is stepping down, not grinding up."""
    out = tick(swing_low_now=10.40, swing_low_prev=11.00)
    assert out["active"] is False
    assert out["reason"] == "no_higher_low_in_prints"


def test_a_higher_low_that_is_still_under_water_refuses():
    """Higher than the last low, but not above our entry — not yet a grind."""
    out = tick(swing_low_now=9.80, swing_low_prev=9.20, buy_support_px=9.50, vwap=None)
    assert out["active"] is False
    assert out["reason"] == "higher_low_not_above_entry"


def test_selling_pressure_refuses():
    """The structure can look intact while the aggressor flow has already turned."""
    out = tick(signed_tape_accel=-3100.0)
    assert out["active"] is False
    assert out["reason"] == "buy_aggression_gone"


def test_a_broken_structure_floor_refuses():
    """Never report a floor the price has already lost (the bar version's review M2)."""
    out = tick(bid=10.90)
    assert out["active"] is False
    assert out["reason"] == "structure_floor_not_held"


def test_losing_vwap_refuses_when_it_is_readable():
    out = tick(bid=11.45, vwap=11.60, swing_low_now=11.40, buy_support_px=11.20)
    assert out["active"] is False
    assert out["reason"] == "vwap_not_held"


def test_an_unreadable_vwap_is_skipped_not_failed():
    out = tick(vwap=None)
    assert out["active"] is True, out["reason"]


# ── fail-closed on missing inputs, exactly as before ───────────────────────────

@pytest.mark.parametrize("missing", ["swing_low_now", "swing_low_prev"])
def test_unreadable_swing_lows_fail_closed(missing):
    out = tick(**{missing: None})
    assert out["active"] is False
    assert out["reason"] == "swing_lows_unreadable"


@pytest.mark.parametrize("bad", [None, float("nan")])
def test_an_unreadable_tape_fails_closed(bad):
    out = tick(signed_tape_accel=bad)
    assert out["active"] is False
    assert out["reason"] == "tape_unreadable"


def test_nonsense_prices_fail_closed():
    out = tick(entry_price=0.0)
    assert out["active"] is False
    assert out["reason"] == "bad_inputs"


# ── maintenance is deliberately identical to the bar version ───────────────────
# Activation is the thing being changed; keeping maintenance the same is what makes
# a side-by-side comparison of the two attributable.

def test_maintenance_holds_while_structure_holds():
    out = tick(prior_active=True)
    assert out["active"] is True
    assert out["reason"] == "maintained"
    assert out["structure_floor"] == pytest.approx(11.40 - 10.00 * 0.01)


def test_maintenance_drops_on_a_structure_break():
    out = tick(prior_active=True, bid=10.90)
    assert out["active"] is False
    assert out["reason"] == "structure_broken"


def test_maintenance_drops_when_the_swing_low_falls_back_to_entry():
    out = tick(prior_active=True, swing_low_now=9.90, buy_support_px=9.80, bid=9.95,
               vwap=None)
    assert out["active"] is False
    assert out["reason"] == "lower_low_below_entry"


def test_maintenance_drops_on_vwap_loss():
    out = tick(prior_active=True, bid=11.45, vwap=11.60)
    assert out["active"] is False
    assert out["reason"] == "vwap_lost"


def test_a_flicker_to_none_never_drops_a_working_grind():
    """The bar version's own flicker rule, preserved: an unreadable input must not
    kill a grind that is otherwise holding."""
    out = tick(prior_active=True, vwap=None, swing_low_prev=None)
    assert out["active"] is True
    assert out["reason"] == "maintained"


# ── the new signal is reported, not gated on ──────────────────────────────────

def test_high_print_position_is_reported_but_never_decides():
    """Adding an unmeasured condition to a conjunction is how the bar version became
    inert. It rides along as telemetry until it has evidence of its own."""
    spent = tick(high_print_position=1.0)
    fresh = tick(high_print_position=0.0)
    assert spent["active"] is fresh["active"] is True
    assert spent["high_print_position"] == 1.0
    assert fresh["high_print_position"] == 0.0


def test_the_structure_floor_uses_the_same_wick_buffer_as_the_bar_version():
    """Shared basis, no new number: entry * max(0.001, atr_pct * 0.25)."""
    out = tick()
    assert out["structure_floor"] == pytest.approx(11.40 - 10.00 * (0.04 * 0.25))
