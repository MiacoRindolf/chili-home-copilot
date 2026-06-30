"""PRINCIPAL-LEVEL edge-case bug hunt — class [size-composition].

FOCUS: CROSS-COMPONENT SIZE-MULTIPLIER COMPOSITION in the momentum live runner.

The live entry-sizing path (live_runner._maybe_enter_live, ~line 6719) composes the
per-trade RISK BUDGET as the product of ~17 independent size multipliers, then HARD-
CLAMPS the product to ``base * 3.0``::

    _eff_max_loss = min(
        base * streak * graduation * cushion * l2 * sched * liq * meta * prior_day *
        overnight * fatigue * sym_fatigue * hot_cold * time_fatigue * halt_size *
        dip_velocity * catalyst_conviction * prime_window,
        base * 3.0,            # hard combined-multiplier ceiling (quant pass v2)
    )

and then re-clamps once more after the optional adaptive spread-cost derate::

    _eff_max_loss = min(_eff_max_loss * scv_mult, base * 3.0)

Finally the clamped ``_eff_max_loss`` feeds ``compute_risk_first_quantity`` which
ALSO caps qty at the hard ``max_notional_ceiling_usd`` (so even a maxed-out 3x risk
budget can never breach the notional / liquidity ceiling).

This module DOES NOT exercise the DB or the runner coroutine. It replicates the
EXACT production clamp expression as a pure helper (``_compose_eff_max_loss`` mirrors
the source line char-for-char in operator order) and pins the SPECIFIC IEEE-754
results for adversarial multiplier vectors, plus drives the REAL
``compute_risk_first_quantity`` / ``spread_liquidity_risk_multiplier`` /
``catalyst_conviction_size_multiplier`` helpers from risk_policy to prove the
downstream ceilings still bind AFTER the 3x clamp.

The GNARLY cases (each FAILS if the composition math is subtly wrong):
  (1) ALL upward levers at MAX simultaneously  -> clamped to EXACTLY base*3.0.
  (2) a mix of <1 derates and >1 boosts        -> net correct, still clamped/passthrough.
  (3) ALL multipliers == 1.0                    -> EXACTLY base (no drift).
  (4) a single 0.5 floor among 1.0s             -> EXACTLY base*0.5.
  (5) FLOAT ACCUMULATION vs the clamp boundary  -> a vector whose IEEE-754 product is
      3.0000000000000004 (math-true 3.0) is clamped to EXACTLY base*3.0 (NOT 3.0+eps);
      a vector just UNDER 3.0 passes through UN-clamped at its exact float value.
  (6) the downstream notional ceiling + liquidity cap STILL bind after the 3x clamp.
  (7) the spread-cost derate composes correctly on top of an already-clamped budget.
"""

from __future__ import annotations

import math

import pytest

from app.services.trading.momentum_neural.risk_policy import (
    catalyst_conviction_size_multiplier,
    compute_risk_first_quantity,
    spread_liquidity_risk_multiplier,
)


# ── exact production clamp, replicated char-for-char in operator order ───────────
# Mirrors live_runner.py line ~6720. Order matters for IEEE-754 reproducibility:
# a*b*c is left-associative ((a*b)*c) and float-mul is NOT associative, so we keep
# the SAME left-to-right factor order the source uses.
_CLAMP_FACTOR = 3.0  # base * 3.0 hard combined-multiplier ceiling


def _compose_eff_max_loss(
    base: float,
    *,
    streak: float = 1.0,
    graduation: float = 1.0,
    cushion: float = 1.0,
    l2: float = 1.0,
    sched: float = 1.0,
    liq: float = 1.0,
    meta: float = 1.0,
    prior_day: float = 1.0,
    overnight: float = 1.0,
    fatigue: float = 1.0,
    sym_fatigue: float = 1.0,
    hot_cold: float = 1.0,
    time_fatigue: float = 1.0,
    halt_size: float = 1.0,
    dip_velocity: float = 1.0,
    catalyst_conviction: float = 1.0,
    prime_window: float = 1.0,
) -> float:
    """EXACT replica of the runner's ``_eff_max_loss`` clamp (line ~6720).

    Keep this factor order byte-identical to the source so any drift between this
    test's expectation and production is itself a signal.
    """
    return min(
        float(base)
        * float(streak)
        * float(graduation)
        * float(cushion)
        * float(l2)
        * float(sched)
        * float(liq)
        * float(meta)
        * float(prior_day)
        * float(overnight)
        * float(fatigue)
        * float(sym_fatigue)
        * float(hot_cold)
        * float(time_fatigue)
        * float(halt_size)
        * float(dip_velocity)
        * float(catalyst_conviction)
        * float(prime_window),
        float(base) * _CLAMP_FACTOR,
    )


def _apply_spread_cost_reclamp(eff_max_loss: float, base: float, scv_mult: float) -> float:
    """EXACT replica of the runner's post-derate re-clamp (line ~6762)."""
    if 0.0 < scv_mult < 1.0:
        return min(float(eff_max_loss) * float(scv_mult), float(base) * _CLAMP_FACTOR)
    return float(eff_max_loss)


# ─────────────────────────────────────────────────────────────────────────────────
# CASE (3) — ALL multipliers == 1.0  ->  EXACTLY base, no float drift.
# 17 sequential multiplies by 1.0 must be an identity. If the source ever swapped a
# default to 0.999.../1.0001 or accumulated error, this catches it.
# ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("base", [1.0, 19.30, 135.27, 676.0, 2029.5, 0.0001, 1e6])
def test_all_unity_is_exactly_base_no_drift(base):
    eff = _compose_eff_max_loss(base)
    assert eff == base, f"17x *1.0 must be identity; got {eff!r} vs {base!r}"
    # And it is strictly below the 3x ceiling (so the clamp branch is NOT the one
    # returning here) for any positive base.
    if base > 0:
        assert eff < base * 3.0


# ─────────────────────────────────────────────────────────────────────────────────
# CASE (4) — a single 0.5 floor among 1.0s  ->  EXACTLY base*0.5.
# A lone derate must shrink exactly and never be swallowed by the unity neighbours.
# ─────────────────────────────────────────────────────────────────────────────────
def test_single_half_derate_among_unity():
    base = 200.0
    # place the 0.5 in the MIDDLE of the chain (time_fatigue) — position must not matter
    eff = _compose_eff_max_loss(base, time_fatigue=0.5)
    assert eff == 100.0
    # placing it FIRST (streak) or LAST (prime_window) yields the identical float
    assert _compose_eff_max_loss(base, streak=0.5) == eff
    assert _compose_eff_max_loss(base, prime_window=0.5) == eff


# ─────────────────────────────────────────────────────────────────────────────────
# CASE (1) — ALL upward levers at their documented MAX simultaneously.
# green_day graduation 2.0, cushion 2.0, catalyst 1.5, prime 1.5, hot_cold ~1.3,
# halt 1.2, dip 1.2, l2/sched/etc 1.x  ->  product is WAY over 3 -> clamp to EXACTLY
# base*3.0 (not 3.0+eps, not the raw 16x product).
# ─────────────────────────────────────────────────────────────────────────────────
@pytest.mark.parametrize("base", [19.30, 135.27, 676.0, 2029.5])
def test_all_upward_levers_maxed_clamps_to_exactly_3x(base):
    eff = _compose_eff_max_loss(
        base,
        graduation=2.0,
        cushion=2.0,
        catalyst_conviction=1.5,
        prime_window=1.5,
        hot_cold=1.3,
        halt_size=1.2,
        dip_velocity=1.2,
        l2=1.1,
        streak=1.5,
    )
    # The raw product (~16x) must be clamped down to EXACTLY base*3.0.
    assert eff == base * 3.0
    # And NOT a hair above it (the bug we hunt: 3.0+float-eps leaking through min()).
    assert eff <= base * 3.0
    # The clamp actually bound (raw product strictly exceeded the ceiling).
    raw = base * 2.0 * 2.0 * 1.5 * 1.5 * 1.3 * 1.2 * 1.2 * 1.1 * 1.5
    assert raw > base * 3.0


# ─────────────────────────────────────────────────────────────────────────────────
# CASE (5) — FLOAT ACCUMULATION exactly at the clamp boundary.
# 1.5 * 1.6 * 1.25 is mathematically EXACTLY 3.0, but in IEEE-754 double it evaluates
# to 3.0000000000000004 (> 3.0). This is the canonical "does min() catch 3.0000001?"
# adversarial input. The clamp MUST pin _eff_max_loss to EXACTLY base*3.0 — never the
# float-inflated base*3.0000000000000004.
# ─────────────────────────────────────────────────────────────────────────────────
def test_float_overshoot_at_boundary_clamps_to_exact_3x():
    base = 200.0
    # sanity: the raw float product really does overshoot 3.0
    raw_factor = 1.5 * 1.6 * 1.25
    assert raw_factor > 3.0
    assert raw_factor == 3.0000000000000004  # pin the exact IEEE-754 value

    eff = _compose_eff_max_loss(
        base, cushion=1.5, graduation=1.6, catalyst_conviction=1.25
    )
    # Clamped to EXACTLY base*3.0 (600.0), NOT 600.0000000000001.
    assert eff == base * 3.0
    assert eff == 600.0
    # The product path, had min() been wrong (e.g. using > vs >=, or returning the
    # product when "close enough"), would have leaked the inflated value:
    assert eff != base * raw_factor
    assert base * raw_factor == 600.0000000000001  # the value that MUST be suppressed


def test_float_undershoot_passes_through_unclamped_exactly():
    """A product a hair UNDER 3.0 must pass through at its EXACT float value (the clamp
    must NOT round it up to base*3.0, and must NOT round it at all)."""
    base = 200.0
    # 1.5 * 1.6 * 1.24 = 2.976 in real math; pin the exact float.
    factor = 1.5 * 1.6 * 1.24
    assert factor < 3.0
    eff = _compose_eff_max_loss(
        base, cushion=1.5, graduation=1.6, catalyst_conviction=1.24
    )
    assert eff == base * factor  # un-clamped, exact
    assert eff < base * 3.0


def test_seventeen_root_product_undershoots_and_is_unclamped():
    """17 equal factors = 3**(1/17) each. IEEE-754 accumulation of the 17-fold product
    lands at 2.9999999999999996 (just UNDER 3.0) — so it must NOT be clamped, and the
    runner returns the (slightly-under-3x) product, never base*3.0."""
    base = 100.0
    r = 3.0 ** (1.0 / 17.0)
    eff = _compose_eff_max_loss(
        base,
        streak=r,
        graduation=r,
        cushion=r,
        l2=r,
        sched=r,
        liq=r,
        meta=r,
        prior_day=r,
        overnight=r,
        fatigue=r,
        sym_fatigue=r,
        hot_cold=r,
        time_fatigue=r,
        halt_size=r,
        dip_velocity=r,
        catalyst_conviction=r,
        prime_window=r,
    )
    # Recompute the SAME left-assoc product to pin the expected float exactly.
    expect = base
    for _ in range(17):
        expect *= r
    assert expect < base * 3.0  # the 17-root product undershoots in double precision
    assert eff == expect  # un-clamped, byte-identical to the accumulated product
    assert eff != base * 3.0


# ─────────────────────────────────────────────────────────────────────────────────
# CASE (2) — a MIX of <1 derates and >1 boosts.
# net = product; must be correct AND respect the clamp. The runner does NOT round,
# so the expectation must be the EXACT accumulated IEEE-754 product (no tidying):
# for THIS factor set (2.0*0.5*1.5*1.2 = math-true 1.8) the left-assoc product lands
# exactly on 360.0 — pin the accumulated float, whatever it is, byte-for-byte.
# ─────────────────────────────────────────────────────────────────────────────────
def test_mixed_derates_and_boosts_net_unclamped_exact():
    base = 200.0
    eff = _compose_eff_max_loss(
        base, graduation=2.0, sched=0.5, cushion=1.5, hot_cold=1.2
    )
    # The clamp must pass the product through at the EXACT accumulated float
    # (un-rounded, un-clamped). For this factor set the accumulation is exactly 360.0.
    expect = base * 2.0 * 0.5 * 1.5 * 1.2
    assert eff == expect
    assert expect == 360.0  # pin the exact accumulated float; bug if code "tidies"/rounds
    assert eff < base * 3.0


def test_mixed_with_strong_boosts_still_clamps():
    """Derates present but the surviving boosts still overshoot 3x -> clamp binds."""
    base = 100.0
    eff = _compose_eff_max_loss(
        base,
        graduation=2.0,
        cushion=2.0,
        catalyst_conviction=1.5,
        prime_window=1.5,  # 2*2*1.5*1.5 = 9x
        sched=0.7,
        fatigue=0.8,  # derates: 9*0.7*0.8 = 5.04x, still > 3
    )
    assert eff == base * 3.0


def test_zero_sched_collapses_product_to_zero():
    """A 0.0 derate (sched 'late' window = 0.0) zeroes the whole product. In the live
    runner a <=0 sched short-circuits earlier, but the clamp expression itself must be
    proven to yield 0.0 (min(0.0, base*3) == 0.0) — never a negative or NaN."""
    base = 500.0
    eff = _compose_eff_max_loss(base, sched=0.0, graduation=2.0, cushion=2.0)
    assert eff == 0.0
    # downstream sizing must then refuse (max_loss_nonpositive), proven below.
    qty, meta = compute_risk_first_quantity(
        entry_price=5.0, atr_pct=0.02, max_loss_usd=eff,
        max_notional_ceiling_usd=10_000.0,
    )
    assert qty == 0.0
    assert meta["reason"] == "max_loss_nonpositive"


# ─────────────────────────────────────────────────────────────────────────────────
# CASE (6) — the downstream NOTIONAL CEILING + liquidity cap STILL bind after 3x.
# Even a maxed-out (clamped) risk budget cannot push qty past the hard notional
# ceiling: a tight stop on a maxed budget wants huge qty, but compute_risk_first_
# quantity caps qty at ceiling/entry. This is the real defense behind the comment
# "the mult can NEVER push notional past any cap".
# ─────────────────────────────────────────────────────────────────────────────────
def test_clamped_budget_still_bounded_by_notional_ceiling():
    base = 100.0
    entry = 5.0
    # Maxed + clamped budget = base*3 = 300 risk.
    eff = _compose_eff_max_loss(base, graduation=2.0, cushion=2.0, prime_window=1.5)
    assert eff == 300.0
    # TIGHT stop: atr_pct 0.004 * 0.60 -> but floored at 0.003 stop_pct -> stop_dist
    # = 5.0*0.003 = 0.015. risk-first qty = 300/0.015 = 20_000 shares = $100k notional,
    # which BLOWS PAST a $2_000 ceiling -> must be capped to ceiling/entry = 400 shares.
    ceiling = 2_000.0
    qty, meta = compute_risk_first_quantity(
        entry_price=entry, atr_pct=0.004, max_loss_usd=eff,
        max_notional_ceiling_usd=ceiling, stop_atr_mult=0.60,
    )
    assert meta["capped_by"] == "notional_ceiling"
    assert qty == ceiling / entry == 400.0
    # notional sits AT (never above) the ceiling despite the 3x-clamped risk budget.
    assert qty * entry <= ceiling
    assert meta["notional_usd"] == 2_000.0


def test_unclamped_budget_risk_first_binds_not_ceiling():
    """Contrast: a WIDE stop keeps risk-first qty below the ceiling, so the budget
    (not the ceiling) drives size — proving the ceiling only binds when it should."""
    base = 100.0
    entry = 10.0
    eff = _compose_eff_max_loss(base, cushion=1.5)  # 150 risk, un-clamped
    assert eff == 150.0
    # WIDE stop: atr_pct 0.10 * 0.60 = 0.06 -> stop_dist = 0.6. qty = 150/0.6 = 250
    # shares = $2500 notional, UNDER a $100k ceiling.
    qty, meta = compute_risk_first_quantity(
        entry_price=entry, atr_pct=0.10, max_loss_usd=eff,
        max_notional_ceiling_usd=100_000.0, stop_atr_mult=0.60,
    )
    assert meta["capped_by"] is None
    assert qty == pytest.approx(250.0)
    assert meta["risk_usd"] == 150.0


# ─────────────────────────────────────────────────────────────────────────────────
# CASE (7) — the SPREAD-COST derate composes on top of an ALREADY-clamped budget,
# and re-clamps. Two sub-cases:
#   (a) budget already AT base*3 (clamped), derate 0.5 -> eff = base*1.5 (the re-clamp
#       min() is a no-op here because 3*0.5 < 3).
#   (b) prove the post-derate re-clamp is itself bounded (derate >=1 is a no-op path;
#       only 0<mult<1 derates apply — a mult>=1 must NOT increase the budget).
# ─────────────────────────────────────────────────────────────────────────────────
def test_spread_cost_derate_on_clamped_budget():
    base = 100.0
    eff_clamped = _compose_eff_max_loss(
        base, graduation=2.0, cushion=2.0, prime_window=1.5
    )
    assert eff_clamped == base * 3.0  # 300
    eff2 = _apply_spread_cost_reclamp(eff_clamped, base, scv_mult=0.5)
    assert eff2 == 150.0  # 300 * 0.5, re-clamp min(150, 300) is a no-op
    assert eff2 <= base * 3.0


def test_spread_cost_mult_ge_one_is_noop_never_increases():
    """The runner only applies the derate when 0 < mult < 1. A mult of exactly 1.0 or
    >= 1.0 must leave the budget UNCHANGED (a spread 'derate' can never UP-size)."""
    base = 100.0
    eff = _compose_eff_max_loss(base, cushion=1.5)  # 150
    assert _apply_spread_cost_reclamp(eff, base, scv_mult=1.0) == 150.0
    assert _apply_spread_cost_reclamp(eff, base, scv_mult=1.7) == 150.0  # NOT 255
    assert _apply_spread_cost_reclamp(eff, base, scv_mult=0.0) == 150.0  # 0 => skip


def test_spread_derate_reclamp_binds_when_post_product_exceeds_3x():
    """Defensive: if (hypothetically) an un-clamped huge budget reached the derate with
    a mult that still leaves it > 3x, the re-clamp min() must bind. Construct
    eff=base*5 (as if pre-clamp) * 0.8 = base*4 -> re-clamp to base*3."""
    base = 100.0
    eff_inflated = base * 5.0
    eff2 = _apply_spread_cost_reclamp(eff_inflated, base, scv_mult=0.8)
    assert eff2 == base * 3.0  # min(400, 300)


# ─────────────────────────────────────────────────────────────────────────────────
# REAL-HELPER composition: drive the actual risk_policy multipliers and feed their
# output into the clamp, proving the helpers' bounded outputs compose correctly.
# ─────────────────────────────────────────────────────────────────────────────────
def test_real_liquidity_mult_composes_and_floors_at_half():
    """spread_liquidity_risk_multiplier floors at 0.5; compose it as the liq factor.
    A name eating its FULL spread tolerance -> mult == floor 0.5 -> halves the budget."""
    # spread_bps == tolerance -> 1 - 1 = 0 -> clamped up to floor 0.5
    mult, meta = spread_liquidity_risk_multiplier(
        60.0, None, floor=0.5, ratio=0.5, abs_cap_bps=800.0
    )
    assert mult == 0.5  # tol == base 60 (no expected move) -> 1-(60/60)=0 -> floor
    base = 200.0
    eff = _compose_eff_max_loss(base, liq=mult)
    assert eff == 100.0


def test_real_liquidity_mult_tight_name_is_neutral():
    mult, _ = spread_liquidity_risk_multiplier(0.0, None)
    assert mult == 1.0  # no_spread -> fail-neutral
    base = 200.0
    assert _compose_eff_max_loss(base, liq=mult) == base


def test_real_catalyst_mult_disabled_is_neutral():
    """With the flag default-OFF, catalyst_conviction returns EXACTLY 1.0 -> the
    composition is byte-identical (the lever cannot silently up-size when disabled)."""
    mult, meta = catalyst_conviction_size_multiplier("FAKE")
    assert mult == 1.0
    assert meta.get("conviction_mult") == 1.0
    base = 676.0
    assert _compose_eff_max_loss(base, catalyst_conviction=mult) == base


def test_real_catalyst_mult_strong_grade_boosts_then_clamps(monkeypatch):
    """When enabled with a STRONG catalyst (grade rank 3), the helper yields a BOUNDED
    boost (<= max_multiplier) which composes under the 3x clamp. We stub the news grade
    so the test is hermetic (no feed/DB) and pin the exact composed budget."""
    import app.services.trading.momentum_neural.risk_policy as rp

    # Force the flag ON and force a STRONG grade via the catalyst.catalyst_grade_rank
    # import inside the function.
    monkeypatch.setattr(
        rp.settings, "chili_momentum_catalyst_conviction_enabled", True, raising=False
    )
    monkeypatch.setattr(
        rp.settings, "chili_momentum_catalyst_conviction_step", 0.15, raising=False
    )
    monkeypatch.setattr(
        rp.settings,
        "chili_momentum_catalyst_conviction_max_multiplier",
        1.5,
        raising=False,
    )
    import app.services.trading.momentum_neural.catalyst as cat

    monkeypatch.setattr(cat, "catalyst_grade_rank", lambda *a, **k: 3, raising=False)

    mult, meta = catalyst_conviction_size_multiplier("STRONGCO")
    # 1.0 + 0.15*3 = 1.45, clamped to max 1.5 -> 1.45
    assert mult == pytest.approx(1.45)
    assert meta["grade_rank"] == 3
    # Compose: a moderate base, this boost alone stays UNDER 3x -> un-clamped exact.
    base = 100.0
    eff = _compose_eff_max_loss(base, catalyst_conviction=mult)
    assert eff == pytest.approx(base * mult)
    assert eff < base * 3.0
    # But stacked with other maxed boosts it CLAMPS:
    eff2 = _compose_eff_max_loss(
        base, catalyst_conviction=mult, graduation=2.0, cushion=2.0
    )  # 1.45*2*2 = 5.8x
    assert eff2 == base * 3.0


# ─────────────────────────────────────────────────────────────────────────────────
# NEGATIVE / DEGENERATE inputs into the clamp expression — the runner floats() each
# factor; a negative or NaN factor would corrupt the budget. Document current
# behavior (these are NOT guarded inside the min() itself — the guard lives upstream
# in each helper). If a helper ever returns a negative, the product flips sign and the
# min() picks it (more-negative) => a NEGATIVE budget that compute_risk_first_quantity
# then REJECTS as nonpositive. We assert that downstream safety net.
# ─────────────────────────────────────────────────────────────────────────────────
def test_negative_factor_yields_negative_budget_rejected_downstream():
    base = 100.0
    # A hypothetical buggy helper returning -1.0 flips the sign; min(-100, 300) = -100.
    eff = _compose_eff_max_loss(base, hot_cold=-1.0)
    assert eff == -100.0  # min picks the negative product
    # The downstream sizing is the SAFETY NET: negative max_loss -> qty 0, reason set.
    qty, meta = compute_risk_first_quantity(
        entry_price=5.0, atr_pct=0.02, max_loss_usd=eff,
        max_notional_ceiling_usd=10_000.0,
    )
    assert qty == 0.0
    assert meta["reason"] == "max_loss_nonpositive"


def test_nan_factor_propagates_and_is_rejected_downstream():
    base = 100.0
    eff = _compose_eff_max_loss(base, meta=float("nan"))
    # min(nan, 300): Python returns the FIRST arg when a NaN is involved in min() with
    # this ordering, so eff is NaN. Either way it is non-finite.
    assert math.isnan(eff)
    qty, m = compute_risk_first_quantity(
        entry_price=5.0, atr_pct=0.02, max_loss_usd=eff,
        max_notional_ceiling_usd=10_000.0,
    )
    assert qty == 0.0
    assert m["reason"] == "max_loss_nonpositive"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
