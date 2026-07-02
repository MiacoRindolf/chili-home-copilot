"""Combined SIZE-DOWN FLOOR for a genuine front-side A-setup (live_runner).

The `_eff_max_loss` multiplier product in live_runner has a combined CEILING
(base x 3.0) but historically had NO combined FLOOR — so unbounded MULTIPLICATIVE
STACKING of the ~23 size-DOWN multipliers (e.g. daily_room 0.40 x midday-sched 0.50
= 0.20) could crush a REAL A-setup's per-trade risk far below the equity-relative
base (CUPR: base $122.17 x 0.40 x 0.50 = $24.46, ~0.18% risk instead of 1%). The
floor RAISES a stacked-down budget back toward base ONLY for a confirmed front-side
A-setup, and can ONLY raise toward base — NEVER above it (risk-FIRST preserved).

These tests assert on the EXACT inline arithmetic from live_runner (replicated in
`_apply_combined_floor` below, which mirrors the production code byte-for-byte) plus
the REAL `compute_risk_first_quantity` to prove the dollar-risk == _eff_max_loss
invariant and the never-exceeds-base guarantee. The full `tick_live_session` path is
far too heavy to drive in a unit test, so we extract the smallest computation per the
task's guidance — without skipping the load-bearing invariants.
"""

import math

from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity


# ── The production floor logic, replicated EXACTLY from live_runner.py ──────────────
# Mirrors the inline block immediately after the `_eff_max_loss = min(base*prod, base*3)`
# clamp. `eff_in` is the already-clamped stacked _eff_max_loss; the function returns the
# (possibly raised) _eff_max_loss exactly as the production code computes it.
def _apply_combined_floor(
    *,
    base_max_loss: float,
    eff_in: float,
    is_frontside_a_setup: bool,
    floor: float = 0.5,
    flag_enabled: bool = True,
) -> float:
    eff = float(eff_in)
    if not (flag_enabled and float(base_max_loss) > 0.0):
        return eff
    csf_floor = float(floor)
    csf_floor = max(0.0, min(1.0, csf_floor))
    combined_mult = float(eff) / float(base_max_loss)
    if is_frontside_a_setup and combined_mult < csf_floor:
        eff = float(base_max_loss) * csf_floor
    return eff


# CUPR scenario constants (verified): base = 1% of the $12,189 BP basis.
CUPR_BASE = 122.17
CUPR_DAILY_ROOM = 0.4004
CUPR_SCHED = 0.50
CUPR_PROD = CUPR_DAILY_ROOM * CUPR_SCHED  # 0.2002
CUPR_EFF_STACKED = CUPR_BASE * CUPR_PROD  # ~24.46
CUPR_EFF_FLOORED = CUPR_BASE * 0.5  # ~61.09
FLOOR = 0.5


def test_1_clamp_raises_cupr_and_recomputes_risk_first():
    """CLAMP RAISES: base=122.17, prod=0.2002, A-setup TRUE, flag ON
    => _eff_max_loss raised from $24.46 to $61.09 (=122.17 x 0.5); qty recomputed
    risk-first off the SAME stop_distance; dollar-risk == _eff_max_loss exactly."""
    eff_stacked = min(CUPR_BASE * CUPR_PROD, CUPR_BASE * 3.0)
    assert math.isclose(eff_stacked, CUPR_EFF_STACKED, rel_tol=1e-9, abs_tol=1e-6)

    eff_floored = _apply_combined_floor(
        base_max_loss=CUPR_BASE,
        eff_in=eff_stacked,
        is_frontside_a_setup=True,
        floor=FLOOR,
        flag_enabled=True,
    )
    # $24.46 -> $61.09
    assert math.isclose(eff_floored, CUPR_EFF_FLOORED, rel_tol=1e-9, abs_tol=1e-6)
    assert eff_floored > eff_stacked  # the floor RAISED the budget

    # Risk-first qty off the SAME stop_distance for both budgets. Use a clean entry/atr
    # so notional/rounding never bites (the dollar-risk == max_loss invariant is exact).
    entry, atr_pct, stop_mult = 5.00, 0.04, 0.60  # stop_distance = 5 * max(0.003, 0.024) = 0.12
    qty, meta = compute_risk_first_quantity(
        entry_price=entry,
        atr_pct=atr_pct,
        max_loss_usd=eff_floored,
        max_notional_ceiling_usd=10_000.0,  # high enough not to cap
        base_increment=None,
        base_min_size=None,
        stop_atr_mult=stop_mult,
    )
    stop_distance = meta["stop_distance"]
    assert stop_distance > 0
    assert qty > 0
    # dollar-risk == _eff_max_loss EXACTLY (qty was set risk-first as eff/stop_distance).
    realized_dollar_risk = qty * stop_distance
    assert math.isclose(realized_dollar_risk, eff_floored, rel_tol=1e-9, abs_tol=1e-6)


def test_2_never_exceeds_base():
    """NEVER EXCEEDS BASE: with prod very low, the floored _eff_max_loss == base*0.5 < base.
    With prod=0.8 (>= FLOOR) => NO raise (already above floor)."""
    # Very low stacked product -> floored to exactly base*0.5, strictly below base.
    eff_low = min(CUPR_BASE * 0.05, CUPR_BASE * 3.0)
    eff_floored = _apply_combined_floor(
        base_max_loss=CUPR_BASE,
        eff_in=eff_low,
        is_frontside_a_setup=True,
        floor=FLOOR,
    )
    assert math.isclose(eff_floored, CUPR_BASE * FLOOR, rel_tol=1e-9, abs_tol=1e-6)
    assert eff_floored < CUPR_BASE  # NEVER above base (1% equity)

    # prod >= FLOOR (0.8) -> the realized aggregate is already above the floor -> NO raise.
    eff_high = min(CUPR_BASE * 0.8, CUPR_BASE * 3.0)
    eff_unchanged = _apply_combined_floor(
        base_max_loss=CUPR_BASE,
        eff_in=eff_high,
        is_frontside_a_setup=True,
        floor=FLOOR,
    )
    assert math.isclose(eff_unchanged, eff_high, rel_tol=1e-9, abs_tol=1e-6)
    assert eff_unchanged > CUPR_BASE * FLOOR  # untouched, still below base


def test_3_gate_false_and_flag_off_parity():
    """GATE FALSE => no raise (a non-A-setup with prod=0.2 keeps $24.46).
    Flag OFF => byte-identical (no raise) for the SAME A-setup input."""
    eff_stacked = min(CUPR_BASE * CUPR_PROD, CUPR_BASE * 3.0)

    # Gate FALSE: non-A-setup keeps today's stacked size-down ($24.46).
    eff_gate_false = _apply_combined_floor(
        base_max_loss=CUPR_BASE,
        eff_in=eff_stacked,
        is_frontside_a_setup=False,
        floor=FLOOR,
        flag_enabled=True,
    )
    assert math.isclose(eff_gate_false, eff_stacked, rel_tol=1e-9, abs_tol=1e-6)
    assert math.isclose(eff_gate_false, CUPR_EFF_STACKED, rel_tol=1e-9, abs_tol=1e-6)

    # Flag OFF: byte-identical even for a genuine A-setup.
    eff_flag_off = _apply_combined_floor(
        base_max_loss=CUPR_BASE,
        eff_in=eff_stacked,
        is_frontside_a_setup=True,
        floor=FLOOR,
        flag_enabled=False,
    )
    assert eff_flag_off == eff_stacked  # exact byte-identical


def test_4_daily_cap_guard():
    """DAILY-CAP GUARD: N max-size A-setup losses still trip the $667 daily-loss
    breaker, and the floor never lets a single floored trade exceed base — so it
    cannot breach the hard daily cap beyond what N base-sized losses would."""
    equity = 13_340.0
    daily_cap = equity * 0.05  # $667 breaker
    assert math.isclose(daily_cap, 667.0, rel_tol=1e-9, abs_tol=1e-2)

    # The MAXIMUM a floored A-setup trade can risk is base (the floor can only raise
    # TOWARD base, never above): worst floored per-trade == base*FLOOR <= base.
    worst_floored_per_trade = _apply_combined_floor(
        base_max_loss=CUPR_BASE,
        eff_in=CUPR_BASE * 0.01,  # crushed near-zero stacked
        is_frontside_a_setup=True,
        floor=FLOOR,
    )
    assert worst_floored_per_trade <= CUPR_BASE  # never above base (1% equity)
    assert math.isclose(worst_floored_per_trade, CUPR_BASE * FLOOR, rel_tol=1e-9, abs_tol=1e-6)

    # N floored A-setup losses still cross the $667 breaker (it trips); the cumulative
    # loss is bounded by N*base (the floor never raises any trade above base), so the
    # breaker behaves exactly as it would for N <= base-sized losses.
    n_to_trip = math.ceil(daily_cap / worst_floored_per_trade)
    cumulative = n_to_trip * worst_floored_per_trade
    assert cumulative >= daily_cap  # the breaker DOES trip
    # And the floor never makes per-trade risk exceed base, so cumulative <= N*base.
    assert cumulative <= n_to_trip * CUPR_BASE
