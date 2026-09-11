"""[58] The near-high give-back band of ``tape_accel_reversal_exit`` is DERIVED and REPORTED.

Before this change the band was an undocumented literal (``0.35``, "the ONE new knob"). It is
now ``ACCEL_REVERSAL_GIVEBACK_BAND_R`` — the p90 of the give-back ``(H − P) / risk_dist`` at the
first REAL accel rollover above entry per leg, print-indexed on the executed tape, ``risk_dist``
in the helper's OWN unit — and every armed return of the helper carries the value that decided:

    giveback_r        (hwm − bid) / risk_dist         the leg's give-back at this tick
    giveback_band_r   the band in R                    what it was compared against
    giveback_band_px  the EFFECTIVE band in price      after the one-tick floor
    binding           the named derivation             + "env override" / the tick floor
    arm_r/arm_frac/reward_risk                         gate 1 — 78 % of receipts stop there

REVIEW FIXES IN THIS FILE (see the PR body):
  * the edge test no longer pins a floating-point coincidence — it drives the boundary through
    the helper's own arithmetic (a micro-dollar inside / outside, over a sweep of bands, plus
    one exactly-representable binary case for inclusivity);
  * the assertions that pinned documentation PROSE ("DERIVED" in the field description, "p90" /
    "n=27" in the binding string) are gone: a legitimate re-derivation must not turn a wording
    change into a red test. The numeric pin and the AST pins stay — those are behaviour;
  * new pins for the things the review found missing: the gate-2 window is print-indexed (not a
    15-second clock), the derivation script named by the constant is actually IN the repo, the
    band is floored at the market's own tick, the gate-3 cliff is a ramp, and the receipt
    carries the arm that decides the majority of its rows.
"""

from __future__ import annotations

import ast
import pathlib
import re
import subprocess
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from app.services.trading.momentum_neural.entry_gates import _signed_tape_features
from app.services.trading.momentum_neural.paper_execution import (
    ACCEL_REVERSAL_GIVEBACK_BAND_R,
    ACCEL_REVERSAL_GIVEBACK_BINDING,
    ACCEL_REVERSAL_TICK_FLOOR_BINDING,
    tape_accel_reversal_exit,
)

_PE = "app.services.trading.momentum_neural.paper_execution"
_REPO = pathlib.Path(__file__).resolve().parents[1]
_LIVE_RUNNER = _REPO / "app" / "services" / "trading" / "momentum_neural" / "live_runner.py"
_PAPER_EXEC = _REPO / "app" / "services" / "trading" / "momentum_neural" / "paper_execution.py"
_VERIFY_SQL = _REPO / "project_ws" / "AgentOps" / "timeshare" / "58_accel_reversal_giveback_band_verify.sql"

# Same winner geometry as test_momentum_tape_accel_reversal_exit: risk_dist = 10 * 0.012 = 0.12,
# arm_r = 1.0R, hwm 10.30 -> peak_r 2.5R. The give-back in R is set by the bid.
_ENTRY = 10.0
_ATR_PCT = 0.02
_SM = 0.60
_RISK = 0.12
_HWM = 10.30
_RR = 2.0
_CUR_STOP = 9.90
_BE = 10.0
_BAND = ACCEL_REVERSAL_GIVEBACK_BAND_R
_BASE_LOCK_BPS = 120.0


def _bid_for_giveback_r(giveback_r: float) -> float:
    """The bid that gives back approximately ``giveback_r`` R from the high.

    NOTE: 'approximately'. ``_HWM - r*_RISK`` does not round-trip exactly through the helper's
    ``(hwm - bid) / risk_dist``, which is precisely why no test may assert a decision at the
    EDGE by reconstructing the bid this way — see ``test_band_edge_*`` below.
    """
    return _HWM - giveback_r * _RISK


# A micro-dollar: four orders of magnitude BELOW the finest price the market can quote
# (Reg NMS 612 sub-dollar increment is $0.0001) and six above the helper's penny-arithmetic
# tolerance, so nudging a bid by this crosses the band boundary and nothing else.
_MICRO = 1e-6


def _nudge(x: float, micros: int) -> float:
    """``x`` moved ``micros`` micro-dollars (positive = up)."""
    return x + micros * _MICRO


def _settings(band: float = _BAND) -> SimpleNamespace:
    return SimpleNamespace(
        chili_momentum_exit_ofi_arm_frac=0.5,
        chili_momentum_exit_ofi_base_lock_bps=_BASE_LOCK_BPS,
        chili_momentum_exit_accel_reversal_giveback_frac=band,
    )


def _call(settings: SimpleNamespace | None = None, **over):
    base = dict(
        high_water_mark=_HWM,
        entry_price=_ENTRY,
        bid=_bid_for_giveback_r(0.10),
        atr_pct=_ATR_PCT,
        stop_atr_mult=_SM,
        reward_risk=_RR,
        current_stop=_CUR_STOP,
        breakeven_floor=_BE,
        signed_tape_accel=-5.0,       # turned net-negative
        prev_signed_tape_accel=20.0,  # was pushing up -> genuine TURN
        side_long=True,
    )
    base.update(over)
    with patch(f"{_PE}.settings", settings or _settings()):
        return tape_accel_reversal_exit(**base)


# ───────────────────────────── the band decides gate 3 ──────────────────────────────────

def test_constant_is_the_derived_band():
    """The constant is the measured p90 (0.393 R). This pins the NUMBER — the only thing a
    re-derivation is allowed to change, and then only by re-running the committed script."""
    assert _BAND == pytest.approx(0.393, abs=1e-9)
    assert isinstance(ACCEL_REVERSAL_GIVEBACK_BINDING, str)
    assert ACCEL_REVERSAL_GIVEBACK_BINDING.strip()


def test_rollover_within_band_fires():
    """A give-back inside the band (band − 0.05 R) at a genuine turn on a winner FIRES."""
    out = _call(bid=_bid_for_giveback_r(_BAND - 0.05))
    assert out["armed"] is True
    assert out["fired"] is True, out
    assert out["trigger"] == "tape_accel_reversal"
    assert out["reason"] == "fired"
    assert out["giveback_r"] == pytest.approx(_BAND - 0.05, abs=1e-3)
    assert out["giveback_band_r"] == pytest.approx(_BAND, abs=1e-9)


def test_rollover_beyond_band_is_the_trails_job():
    """A give-back beyond the band (band + 0.05 R) returns gave_back_too_much and the stop
    is untouched (Invariant A: never loosen, and no raise either)."""
    out = _call(bid=_bid_for_giveback_r(_BAND + 0.05))
    assert out["armed"] is True
    assert out["fired"] is False
    assert out["reason"] == "gave_back_too_much"
    assert out["new_stop_floor"] == _CUR_STOP
    assert out["giveback_r"] == pytest.approx(_BAND + 0.05, abs=1e-3)
    assert out["giveback_band_r"] == pytest.approx(_BAND, abs=1e-9)


@pytest.mark.parametrize(
    "band",
    [0.317, 0.35, 0.38, 0.391, 0.392, ACCEL_REVERSAL_GIVEBACK_BAND_R, 0.394, 0.422, 0.429, 0.5],
)
def test_band_edge_decision_is_the_helpers_own_comparison(band):
    """The boundary invariant, driven through the helper's OWN arithmetic.

    The previous version of this test reconstructed the edge bid as ``hwm − band·risk`` and
    asserted the helper does not refuse it. That round trip is not exact in binary floating
    point: at band 0.393 it happened to land 1.3e-16 on the passing side, while at 0.391 /
    0.392 / 0.394 / 0.317 / 0.38 the SAME assertion fails — 497 of the 999 bands ``i/1000``
    fail it. It pinned a coincidence, so the next re-derivation had a coin-flip chance of
    turning a wording-free, behaviour-free change into a red "the inclusive edge broke".

    What is actually invariant: a bid a MICRO-DOLLAR inside the edge is never refused, and a
    bid a micro-dollar outside it always is — for every band, with no reconstruction inside
    the assertion. (A micro-dollar rather than an ULP: the helper carries a nano-dollar
    tolerance so that ``3.04 - 3.03`` counts as one cent and the nudge must clear it. Both
    are still orders of magnitude below the finest price the market can quote.)
    """
    edge = _HWM - band * _RISK
    inside = _nudge(edge, 1)    # higher bid  => SMALLER give-back => inside the band
    outside = _nudge(edge, -1)  # lower  bid  => LARGER  give-back => outside the band
    got_inside = _call(settings=_settings(band=band), bid=inside)
    got_outside = _call(settings=_settings(band=band), bid=outside)
    assert got_inside["reason"] != "gave_back_too_much", (band, got_inside)
    assert got_outside["reason"] == "gave_back_too_much", (band, got_outside)


def test_band_edge_is_inclusive_in_exact_binary_arithmetic():
    """``giveback > band·risk`` is the refusal, so EXACTLY at the band is still near-high.

    Every number here is exactly representable in binary (risk_dist = 10·0.0125 = 0.125,
    band 0.5 => band·risk = 0.0625, hwm 10.5, bid 10.4375 => give-back 0.0625 exactly), so
    the equality is real and not a rounding artifact.
    """
    out = _call(
        settings=_settings(band=0.5),
        entry_price=10.0,
        atr_pct=0.025,
        stop_atr_mult=0.5,
        high_water_mark=10.5,
        bid=10.4375,
        current_stop=9.0,
        breakeven_floor=10.0,
    )
    assert (10.5 - 10.4375) == 0.5 * (10.0 * 0.0125)  # the equality is exact
    assert out["reason"] != "gave_back_too_much", out


# ───────────────────── THE PENNY: a sub-tick band is not a band ─────────────────────────

def test_sub_tick_band_is_floored_at_one_tick_and_the_floor_is_named():
    """Both hwm and bid are exchange-quantized, so the give-back is a whole number of ticks.
    A band narrower than one tick silently degenerates to ``bid == hwm`` — a STRICTER gate
    than the one derived, arrived at by accident (42/369 live receipts over 14 d). The band
    distance is floored at the Reg NMS 612 minimum increment and the floor is REPORTED."""
    # $3 name, risk_dist = 3 * 0.003 = 0.009  =>  band*risk = 0.00354 < $0.01
    out = _call(
        entry_price=3.00,
        atr_pct=0.001,
        stop_atr_mult=1.0,
        high_water_mark=3.04,
        bid=3.03,
        current_stop=2.90,
        breakeven_floor=3.00,
    )
    # penny arithmetic in binary: 3.04 - 3.03 is 0.010000000000000231, which is why the
    # comparison carries a nano-dollar tolerance — without it a one-cent give-back would be
    # refused by a one-cent band on exactly the names this floor exists for.
    assert (3.04 - 3.03) > 0.01
    assert out["giveback_band_r"] == pytest.approx(_BAND, abs=1e-9)
    assert out["giveback_band_px"] == pytest.approx(0.01, abs=1e-9)  # one tick, not 0.0035
    assert ACCEL_REVERSAL_TICK_FLOOR_BINDING in out["binding"]
    assert ACCEL_REVERSAL_GIVEBACK_BINDING in out["binding"]
    # and it BINDS: one cent of give-back is admitted where the raw band would refuse it.
    assert out["reason"] != "gave_back_too_much", out


def test_tick_floor_is_absent_when_the_derived_band_is_wider_than_a_tick():
    """On a normal name the derived band is many ticks wide, so the floor must not appear in
    the binding (a receipt that always names the floor reports nothing)."""
    out = _call(bid=_bid_for_giveback_r(0.10))
    assert out["giveback_band_px"] == pytest.approx(_BAND * _RISK, abs=1e-9)
    assert out["binding"] == ACCEL_REVERSAL_GIVEBACK_BINDING
    assert ACCEL_REVERSAL_TICK_FLOOR_BINDING not in out["binding"]


def test_sub_dollar_name_uses_the_sub_penny_increment():
    """Reg NMS 612: below $1.00 the increment is $0.0001, so a 40-cent name does not get a
    penny-wide band handed to it."""
    out = _call(
        entry_price=0.40,
        atr_pct=0.001,
        stop_atr_mult=1.0,
        high_water_mark=0.44,
        bid=0.4399,
        current_stop=0.30,
        breakeven_floor=0.40,
    )
    # band*risk = 0.393 * (0.40*0.003) = 0.000472 > 0.0001 => no floor
    assert out["giveback_band_px"] == pytest.approx(0.393 * 0.40 * 0.003, abs=1e-9)
    assert ACCEL_REVERSAL_TICK_FLOOR_BINDING not in out["binding"]


# ──────────────── MECHANISM, NOT BINARY: the gate-3 cliff is a ramp ─────────────────────

def test_cushion_is_the_base_at_the_high():
    """At the high (give-back 0 — where ALL 15 live fires of the last 14 d actually sat) the
    conditioned cushion IS the base cushion: this change costs nothing on the live window."""
    out = _call(bid=_HWM)
    assert out["inside_band_frac"] == pytest.approx(0.0, abs=1e-9)
    assert out["lock_bps"] == pytest.approx(_BASE_LOCK_BPS, abs=1e-6)
    assert out["new_stop_floor"] == pytest.approx(
        _HWM * (1.0 - _BASE_LOCK_BPS / 10_000.0), abs=1e-9
    )


def test_cushion_ramps_with_distance_inside_the_band():
    """A high-ATR name whose band is WIDER than the base cushion: the lock loosens
    monotonically as the tick sits deeper inside the band, instead of stepping off a cliff at
    the edge. (risk_dist = 12 * 0.06 = 0.72, band*risk = 0.283 > base cushion 12*0.012 = 0.144.)"""
    common = dict(
        entry_price=12.0,
        atr_pct=0.06,
        stop_atr_mult=1.0,
        high_water_mark=13.0,
        current_stop=11.0,
        breakeven_floor=12.0,
    )
    band_px = _BAND * (12.0 * 0.06)
    at_high = _call(bid=13.0, **common)
    quarter = _call(bid=13.0 - 0.25 * band_px, **common)
    edge = _call(bid=_nudge(13.0 - band_px, 1), **common)
    assert at_high["lock_bps"] < quarter["lock_bps"] < edge["lock_bps"]
    assert at_high["inside_band_frac"] == pytest.approx(0.0, abs=1e-6)
    assert quarter["inside_band_frac"] == pytest.approx(0.25, abs=1e-3)
    assert edge["inside_band_frac"] == pytest.approx(1.0, abs=1e-6)
    # at the edge the cushion is the full band — the lock has handed over to the trail's width
    assert (13.0 - band_px) - edge["new_stop_floor"] == pytest.approx(band_px, abs=1e-3)


def test_the_edge_is_continuous_not_a_cliff():
    """One tick of give-back must not separate 'lock 120 bps under the bid' from 'write
    nothing'. Just inside the edge the written stop is at most the band's own width above
    what the just-outside tick leaves in place; before the ramp it was a 120 bps step."""
    common = dict(
        entry_price=12.0,
        atr_pct=0.06,
        stop_atr_mult=1.0,
        high_water_mark=13.0,
        current_stop=12.55,
        breakeven_floor=12.0,
    )
    band_px = _BAND * (12.0 * 0.06)
    inside = _call(bid=_nudge(13.0 - band_px, 1), **common)
    outside = _call(bid=_nudge(13.0 - band_px, -1), **common)
    assert outside["reason"] == "gave_back_too_much"
    assert outside["new_stop_floor"] == pytest.approx(12.55, abs=1e-9)
    # the ramp has widened the cushion past the current stop, so the inside tick no longer
    # writes a jump either — it fades out through the ratchet instead of stepping.
    assert inside["reason"] == "ratchet_no_raise"
    assert inside["new_stop_floor"] == pytest.approx(12.55, abs=1e-9)


# ───────────────────────── the receipt reports the binding ──────────────────────────────

@pytest.mark.parametrize(
    "over, expected_reason",
    [
        (dict(bid=_bid_for_giveback_r(0.10)), "fired"),
        (dict(bid=_bid_for_giveback_r(_BAND + 0.20)), "gave_back_too_much"),
        (dict(signed_tape_accel=30.0, prev_signed_tape_accel=10.0), "still_accelerating"),
        (dict(current_stop=10.25, breakeven_floor=10.25), "ratchet_no_raise"),
        (dict(high_water_mark=_ENTRY + 0.05, bid=_ENTRY + 0.04), "below_arm"),
    ],
)
def test_receipt_reports_binding_on_every_resolved_path(over, expected_reason):
    """Once the band is resolved (i.e. after risk_dist is known), EVERY return path carries
    giveback_r / giveback_band_r / giveback_band_px / binding — fired, refused, still
    accelerating, no-raise, and even below the arm (so the A/B receipt can be read on every
    tick)."""
    out = _call(**over)
    assert out["reason"] == expected_reason, out
    assert out["giveback_r"] is not None
    assert out["giveback_band_r"] == pytest.approx(_BAND, abs=1e-9)
    assert out["giveback_band_px"] is not None
    assert out["binding"] is not None and ACCEL_REVERSAL_GIVEBACK_BINDING in out["binding"]


@pytest.mark.parametrize(
    "over, expected_reason",
    [
        (dict(bid=_bid_for_giveback_r(0.10)), "fired"),
        (dict(high_water_mark=_ENTRY + 0.05, bid=_ENTRY + 0.04), "below_arm"),
        (dict(signed_tape_accel=30.0, prev_signed_tape_accel=10.0), "still_accelerating"),
    ],
)
def test_receipt_reports_the_arm_that_decided(over, expected_reason):
    """78 % of this helper's live receipts (348/446 over 14 d) end at gate 1, and ``peak_r``
    alone cannot be read: arm_r = max(0.5, arm_frac · rr) depends on a settings knob AND on
    the plan's own reward:risk, neither of which was in the payload. Both are now reported."""
    out = _call(**over)
    assert out["reason"] == expected_reason, out
    assert out["arm_r"] == pytest.approx(max(0.5, 0.5 * _RR), abs=1e-9)
    assert out["arm_frac"] == pytest.approx(0.5, abs=1e-9)
    assert out["reward_risk"] == pytest.approx(_RR, abs=1e-9)


def test_arm_is_reported_from_the_plans_own_reward_risk():
    """A different rr moves arm_r, and the receipt says so rather than leaving the reader to
    guess whether a peak_r of 0.72 was a near miss or nowhere near."""
    out = _call(reward_risk=4.0, high_water_mark=_ENTRY + 0.05, bid=_ENTRY + 0.04)
    assert out["reason"] == "below_arm"
    assert out["reward_risk"] == pytest.approx(4.0, abs=1e-9)
    assert out["arm_r"] == pytest.approx(2.0, abs=1e-9)  # 0.5 * 4.0, above the 0.5 R floor


def test_hwm_sampling_gap_is_measured_when_the_tape_high_is_supplied():
    """Named approximation (a): ``high_water_mark`` is a running max of the peak BID sampled
    once per runner tick (p50 9.86 s apart live), while the derivation's H is the high of the
    continuous print tape — a max over a sparse sample is <= the max over the tape, in ONE
    direction. The helper now reports the size of that gap instead of arguing about it."""
    out = _call(bid=_bid_for_giveback_r(0.10), tape_window_high=_HWM + 0.06)
    assert out["tape_window_high"] == pytest.approx(_HWM + 0.06, abs=1e-9)
    assert out["hwm_sampling_gap_r"] == pytest.approx(0.06 / _RISK, abs=1e-3)
    # telemetry ONLY — the gate still divides hwm - bid, so the decision is unchanged
    assert out["reason"] == "fired"
    assert out["giveback_r"] == pytest.approx(0.10, abs=1e-3)


def test_hwm_sampling_gap_is_zero_not_negative_when_the_tape_is_below_the_hwm():
    """A tape high below the sampled peak bid (a wide spread) is a ZERO gap, never a negative
    one — the bias this measures is one-directional by construction."""
    out = _call(bid=_bid_for_giveback_r(0.10), tape_window_high=_HWM - 0.10)
    assert out["hwm_sampling_gap_r"] == pytest.approx(0.0, abs=1e-9)


def test_fail_safe_paths_report_nothing():
    """Before risk_dist exists (no tape / short / non-finite) the band is not resolved, so
    the keys are present but None — never a fabricated binding."""
    for over in (dict(signed_tape_accel=None), dict(side_long=False),
                 dict(high_water_mark=float("nan"))):
        out = _call(**over)
        assert out["fired"] is False
        for key in ("giveback_r", "giveback_band_r", "giveback_band_px", "binding",
                    "arm_r", "arm_frac", "reward_risk", "inside_band_frac", "lock_bps",
                    "tape_window_high", "hwm_sampling_gap_r"):
            assert out[key] is None, (key, out)


def test_env_override_is_named():
    """A settings value that differs from the constant is REPORTED as 'env override' — a
    differing band can never run as a dark literal."""
    out = _call(settings=_settings(band=0.25))
    assert out["giveback_band_r"] == pytest.approx(0.25, abs=1e-9)
    assert out["binding"] == "env override"
    # and it actually binds: 0.30 R is refused under a 0.25 band, accepted under the constant.
    refused = _call(settings=_settings(band=0.25), bid=_bid_for_giveback_r(0.30))
    assert refused["reason"] == "gave_back_too_much"
    accepted = _call(bid=_bid_for_giveback_r(0.30))
    assert accepted["reason"] == "fired"


def test_zero_band_is_an_honest_override_not_the_default():
    """An env value of 0.0 must NOT silently collapse to the derived default (the old
    ``or 0.35`` did exactly that): it is reported as 'env override' with band 0. Its
    EFFECTIVE distance is one tick — the finest the market can express — and the receipt
    says so in ``giveback_band_px`` and in the binding, so nothing is hidden."""
    out = _call(settings=_settings(band=0.0), bid=_bid_for_giveback_r(0.20))
    assert out["giveback_band_r"] == 0.0
    assert out["giveback_band_px"] == pytest.approx(0.01, abs=1e-9)
    assert out["binding"].startswith("env override")
    assert ACCEL_REVERSAL_TICK_FLOOR_BINDING in out["binding"]
    assert out["reason"] == "gave_back_too_much"  # 0.20 R = $0.024 > one tick
    # a settings object with NO attribute at all falls back to the derived constant
    bare = SimpleNamespace(chili_momentum_exit_ofi_arm_frac=0.5,
                           chili_momentum_exit_ofi_base_lock_bps=_BASE_LOCK_BPS)
    out2 = _call(settings=bare)
    assert out2["giveback_band_r"] == pytest.approx(_BAND, abs=1e-9)
    assert out2["binding"] == ACCEL_REVERSAL_GIVEBACK_BINDING


# ──────────────────────────────── source pins ────────────────────────────────────────────

def test_config_default_equals_the_constant():
    """app/config.py cannot import the service layer, so its literal default is pinned here.
    ONLY the number is pinned: the earlier version of this test also asserted that the field
    description contains "DERIVED" and does not contain "ONE new knob", which makes a
    re-wording a red test without any behaviour changing."""
    from app.config import Settings

    field = Settings.model_fields["chili_momentum_exit_accel_reversal_giveback_frac"]
    assert field.default == pytest.approx(_BAND, abs=1e-9)


def test_live_receipt_carries_every_binding_key():
    """The live emit of ``live_tape_accel_reversal_exit`` must report the values that decide:
    the band (gate 3), the arm (gate 1 — 78 % of rows), the print window (gate 2) and the
    measured HWM-sampling gap. AST-pinned so a refactor that drops one fails here."""
    tree = ast.parse(_LIVE_RUNNER.read_text(encoding="utf-8"))
    found = None
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_emit"
            and len(node.args) >= 4
            and isinstance(node.args[2], ast.Constant)
            and node.args[2].value == "live_tape_accel_reversal_exit"
            and isinstance(node.args[3], ast.Dict)
        ):
            found = node.args[3]
            break
    assert found is not None, "live_tape_accel_reversal_exit emit not found"
    keys = {k.value for k in found.keys if isinstance(k, ast.Constant)}
    required = {
        "giveback_r", "giveback_band_r", "giveback_band_px", "binding",
        "arm_r", "arm_frac", "reward_risk",
        "tape_window_prints", "hwm_sampling_gap_r",
        "inside_band_frac", "lock_bps",
    }
    assert required <= keys, sorted(required - keys)


def test_gate_two_reads_a_print_indexed_window_not_a_clock():
    """THE WINDOW MUST NOT BE A CLOCK. Gate 2 asks for a genuine tape rollover and the band
    conditioning it is the p90 of the give-back at a PRINT-INDEXED rollover; called with no
    ``window_prints`` the feature falls back to ``chili_momentum_l2_confirm_window_s`` = 15
    SECONDS, so ``prev`` and ``current`` could be computed over two entirely disjoint clock
    buckets (48/421 consecutive live evaluations were >= 15 s apart)."""
    src = _LIVE_RUNNER.read_text(encoding="utf-8")
    start = src.index("TAPE-ACCELERATION REVERSAL EXIT")
    end = src.index('"live_tape_accel_reversal_exit"', start)
    block = src[start:end]
    assert "signed_tape_accel_features(" in block
    assert "window_prints=" in block, "the accel-reversal exit still reads a seconds window"
    assert "chili_momentum_g4_reentry_tape_window_prints" in block
    # and it must not ALSO hand the feature a seconds window (the seconds knob still governs
    # the internal gap trim inside _signed_tape_features, which is why it is named in the
    # comment; what must not happen is this call site choosing a clock).
    assert "window_s=" not in block


def test_replay_reads_the_same_print_window_as_live():
    """DUAL CODE PATHS MUST ASK THE TAPE THE SAME QUESTION. ``replay_v2`` runs the same
    helper; if live moves to a print-indexed window and replay keeps the 15-second one, the
    exit stack diverges on equities again (the parity gap #1037 already recorded once)."""
    replay = (_REPO / "app" / "services" / "trading" / "momentum_neural" / "replay_v2.py").read_text(
        encoding="utf-8"
    )
    start = replay.index("tape_accel_reversal_exit")
    block = replay[max(0, start - 4000):replay.index("side_long=True,", start)]
    assert "signed_tape_accel_features(" in block
    assert "window_prints=" in block, "replay_v2 still reads a seconds window for gate 2"
    assert "chili_momentum_g4_reentry_tape_window_prints" in block
    assert "tape_window_high=" in replay[start:start + 4000]


def test_the_named_derivation_script_is_committed():
    """The constant ships its provenance into every live receipt. A provenance that points at
    a per-session scratchpad path is not provenance — the first cut of this constant did
    exactly that and the number could not be re-run by anyone. The script the comment and the
    binding name must be IN the repo and tracked by git."""
    src = _PAPER_EXEC.read_text(encoding="utf-8")
    assert "scratchpad/derive_giveback_band_58" not in src
    named = re.findall(r"[\w/]*derive_giveback_band_58_helper_units\.py", src)
    assert named, "the constant no longer names its derivation script"
    script = _REPO / "project_ws" / "AgentOps" / "timeshare" / "derive_giveback_band_58_helper_units.py"
    assert script.exists(), f"{script} is named as the derivation but is not in the repo"
    assert "derive_giveback_band_58_helper_units.py" in ACCEL_REVERSAL_GIVEBACK_BINDING
    tracked = subprocess.run(
        ["git", "ls-files", "--error-unmatch", str(script.relative_to(_REPO)).replace("\\", "/")],
        cwd=_REPO, capture_output=True, text=True,
    )
    assert tracked.returncode == 0, f"derivation script is not tracked by git: {tracked.stderr}"


def test_release_gate_counts_bound_decisions_not_merely_present_keys():
    """The deployment check in the committed SQL is the release gate for this change. It must
    NOT be ``payload_json ? 'giveback_band_r'``: the helper emits the key as JSON *null* on
    every path that returns before the band is resolved (no_tape was 37/446 in the measured
    window) and ``'{"a": null}'::jsonb ? 'a'`` is TRUE — so a night of tapeless names would
    read as a verified deployment while the band had decided nothing."""
    sql = _VERIFY_SQL.read_text(encoding="utf-8")
    gate = sql[sql.index("1. DEPLOYMENT CHECK"):sql.index("1b.")]
    assert "band_bound" in gate
    assert "IS NOT NULL" in gate
    assert "'fired','gave_back_too_much','ratchet_no_raise'" in gate.replace(" ", "")


def test_no_stray_035_literal_remains_in_the_helper():
    """The old undocumented literal must not survive as a fallback anywhere in the helper."""
    src = _PAPER_EXEC.read_text(encoding="utf-8")
    start = src.index("def tape_accel_reversal_exit(")
    end = src.index("\ndef ", start + 1)
    body = src[start:end]
    assert not re.search(r"\b0\.35\b", body), "0.35 literal still present in tape_accel_reversal_exit"


# ─────────────── the tape feature that feeds the measured sampling gap ──────────────────

def test_signed_tape_features_reports_the_window_high_print():
    """``window_high_px`` is the window's own high PRINT — the term the runner's tick-sampled
    peak bid can miss. Pure, no I/O: rows are ``(price, size, bid, ask, ts_seconds)``."""
    rows = [
        (3.00, 100, 2.99, 3.01, 1000.0),
        (3.09, 100, 3.08, 3.10, 1001.0),   # the spike print, between two runner ticks
        (3.04, 100, 3.03, 3.05, 1002.0),
        (3.03, 100, 3.02, 3.04, 1003.0),
    ]
    feat = _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert feat is not None
    assert feat["window_high_px"] == pytest.approx(3.09, abs=1e-9)
    assert feat["prints_since_high"] == 2
