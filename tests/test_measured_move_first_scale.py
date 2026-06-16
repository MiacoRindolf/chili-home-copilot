"""Ross gap #2: round-number first-scale target (videos 37/03/12/14/20/24/25). Ross sells
half into the next round/half-dollar where sellers stack rather than waiting for a far
fixed R:R that may never print and trails back (the MEGA give-back). The first-scale
target is pulled in to the nearest round number above entry that clears a 1R floor and
sits BELOW the R:R target; otherwise the R:R target is unchanged (byte-identical). The
RUNNER (balance) still trails up from the partial.
"""
from __future__ import annotations

from app.services.trading.momentum_neural import paper_execution
from app.services.trading.momentum_neural.paper_execution import (
    round_number_first_scale_target,
    round_numbers_above,
    stop_target_prices,
)


# ── round_numbers_above (multi-scale psych grid) ─────────────────────────────

def test_round_numbers_above_dollar_name():
    lv = round_numbers_above(5.20)
    assert lv[0] == 5.25 and 5.5 in lv and 6.0 in lv   # half-dollar / dollar levels present
    assert all(x > 5.20 for x in lv) and lv == sorted(lv)


def test_round_numbers_above_teens_name_is_fine_grained():
    # the bug the multi-scale grid fixes: a $12 name must see $12.50 / $13, not only $20.
    lv = round_numbers_above(12.30)
    assert 12.5 in lv and 13.0 in lv and 15.0 in lv and 20.0 in lv


def test_round_numbers_above_subdollar_crypto():
    lv = round_numbers_above(0.12)
    assert lv and lv[0] > 0.12 and lv == sorted(lv)


def test_round_numbers_above_bad_input():
    assert round_numbers_above(0) == []
    assert round_numbers_above(-1) == []
    assert round_numbers_above(float("nan")) == []


# ── round_number_first_scale_target (explicit entry/stop/rr) ─────────────────

def test_pulls_in_to_round_number_when_in_band():
    # entry 5.20, stop 4.90 -> 1R = 0.30, floor 5.50; rr_target 6.10 (3:1). The $5.50 half-
    # dollar sits at the 1R floor and below the rr target -> first scale at $5.50.
    assert round_number_first_scale_target(5.20, 4.90, 6.10) == 5.5


def test_picks_whole_dollar_for_teens_name():
    # entry 12.30, stop 12.00 -> 1R 0.30, floor 12.60; rr_target 13.20. $12.50 is below the
    # floor, $13.00 is the nearest qualifying -> first scale at $13.00 (not the far $13.20).
    assert round_number_first_scale_target(12.30, 12.00, 13.20) == 13.0


def test_no_op_when_no_round_number_in_band():
    # tight 2:1: rr_target 12.90; $12.50 < floor(12.60), $13.00 >= rr_target -> nothing
    # qualifies -> the rr target stands (byte-identical).
    assert round_number_first_scale_target(12.30, 12.00, 12.90) == 12.90


def test_never_below_one_R():
    # a round number just above entry but < 1R must NOT be taken (no tiny-gain partial).
    # entry 5.01, stop 4.71 -> 1R 0.30, floor 5.31; $5.05/$5.10/$5.25 all < floor; rr 5.91.
    out = round_number_first_scale_target(5.01, 4.71, 5.91)
    assert out >= 5.31 or out == 5.91


def test_short_side_unchanged():
    assert round_number_first_scale_target(5.20, 5.50, 4.30, side_long=False) == 4.30


def test_nonpositive_risk_returns_rr_target():
    assert round_number_first_scale_target(5.20, 5.20, 6.10) == 6.10   # zero risk


# ── stop_target_prices integration + parity ──────────────────────────────────

def test_stop_target_first_scale_never_above_rr(monkeypatch):
    # whatever the round number, the first-scale target is <= the R:R target (it only ever
    # pulls IN), and the stop is unchanged.
    stop, target = stop_target_prices(5.20, atr_pct=0.05, side_long=True, reward_risk=3.0)
    rr_target = 5.20 + 3.0 * (5.20 - stop)
    assert target <= rr_target + 1e-9
    assert stop < 5.20


def test_stop_target_byte_identical_when_no_round_number(monkeypatch):
    # force no qualifying round number -> the target is EXACTLY the rr formula (parity).
    monkeypatch.setattr(paper_execution, "round_numbers_above", lambda price: [])
    stop, target = stop_target_prices(5.20, atr_pct=0.05, side_long=True, reward_risk=3.0)
    assert target == 5.20 + 3.0 * (5.20 - stop)
