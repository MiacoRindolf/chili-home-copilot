"""Adversarial tests for the ADAPTIVE pullback-depth ceiling
(``_adaptive_pullback_depth_ceiling`` in
app/services/trading/momentum_neural/entry_gates.py) and its bull-flag GUARD-4 seam.

THE TRAP (project_momentum_zero_fills_root_cause / MEMORY: the documented 0-fills,
fewer-fills regression): tightening the bull-flag retrace ceiling toward Ross's
~50%-of-prior-candle WITHOUT adapting cuts EXPLOSIVE low-float names, whose normal
pull is DEEPER (INHD-like volatility). The non-negotiable property these tests prove:

  * flag OFF (default) -> ceiling function returns 0.0 -> GUARD 4 uses the EXISTING
    ``flag_ceil`` unchanged => BYTE-IDENTICAL to the deployed image (no regression).
  * flag ON, CALM name (atr_pct ~ 0.01) -> ceiling ~0.515, TIGHTER than the current
    0.70 -> a calm name pulling back DEEPER than ~50% is REJECTED (faithful to Ross).
  * flag ON, EXPLOSIVE name (atr_pct ~ 0.05) with the SAME deep pull -> ceiling ~0.575
    and the seam ``min(flag_ceil, adaptive_ceiling)`` keeps the deeper tolerance ->
    that explosive name still PASSES (NO fewer-fills regression). This is the load-
    bearing test.
  * SHALLOW pull (either regime) -> well under both ceilings -> PASSES.
  * Hard cap: the ceiling never exceeds _VOL_SHALLOW_CEIL (0.75), no matter how high
    ATR% climbs.
  * The formula reuses the ONE documented base (_VOL_SHALLOW_BASE / _VOL_SHALLOW_ATR_MULT)
    -- no fixed per-name magic -- identical to the _vol_aware_pullback_tolerances shallow.

These are PURE-function + seam-logic tests (no DB, no live frame fixture), so they
isolate the ceiling behaviour from the rest of the bull-flag pipeline.

Run (operator):
  TEST_DATABASE_URL=postgresql://chili:chili@localhost:5433/chili_test \
    conda run -n chili-env pytest tests/test_momentum_adaptive_pullback_depth.py -v
"""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.entry_gates import (
    _BULL_FLAG_RETRACE_CEIL,
    _BULL_FLAG_RETRACE_CEIL_ATR_MULT,
    _VOL_SHALLOW_ATR_MULT,
    _VOL_SHALLOW_BASE,
    _VOL_SHALLOW_CEIL,
    _adaptive_pullback_depth_ceiling,
)

# Regime reference points (LOCATE): calm ~1% ATR, explosive ~5% ATR.
CALM_ATR = 0.01
EXPLOSIVE_ATR = 0.05

# A pullback that is "deeper than ~50% of the impulse" -- normal for an explosive
# low-float Ross name, but a reversal-warning for a calm large-cap.
DEEP_RETRACE = 0.56
# A shallow pull that comfortably clears the floor regardless of regime.
SHALLOW_RETRACE = 0.40


def _eff_ceil(atr_pct: float, *, enabled: bool) -> float:
    """Reproduce the bull-flag GUARD-4 seam EXACTLY:
    ``eff_ceil = min(flag_ceil, adaptive_ceiling) if adaptive_ceiling > 0 else flag_ceil``.
    ``flag_ceil`` is the existing vol-aware bull-flag ceiling."""
    a = max(0.0, atr_pct)
    flag_ceil = min(_BULL_FLAG_RETRACE_CEIL + a * _BULL_FLAG_RETRACE_CEIL_ATR_MULT, 0.90)
    adaptive_ceiling = _adaptive_pullback_depth_ceiling(atr_pct, enabled)
    return min(flag_ceil, adaptive_ceiling) if adaptive_ceiling > 0 else flag_ceil


def _flag_ceil_only(atr_pct: float) -> float:
    """The EXISTING (pre-feature) ceiling, for the byte-identical comparison."""
    a = max(0.0, atr_pct)
    return min(_BULL_FLAG_RETRACE_CEIL + a * _BULL_FLAG_RETRACE_CEIL_ATR_MULT, 0.90)


# ── 1. flag OFF => byte-identical to the existing ceiling (no-op) ──────────────────
@pytest.mark.parametrize("atr_pct", [0.0, CALM_ATR, 0.02, EXPLOSIVE_ATR, 0.10, 0.50])
def test_disabled_is_byte_identical(atr_pct: float) -> None:
    # Function returns the explicit disabled sentinel.
    assert _adaptive_pullback_depth_ceiling(atr_pct, enabled=False) == 0.0
    # And the GUARD-4 seam reduces to the pre-existing flag_ceil EXACTLY.
    assert _eff_ceil(atr_pct, enabled=False) == _flag_ceil_only(atr_pct)


# ── 2. CALM name + deeper-than-~50% pull => REJECTED (ceiling tighter than retrace) ─
def test_calm_deep_pullback_rejected() -> None:
    ceiling = _adaptive_pullback_depth_ceiling(CALM_ATR, enabled=True)
    # ~0.515 per LOCATE: tighter than the current 0.70 ceiling.
    assert ceiling == pytest.approx(0.515, abs=1e-9)
    assert ceiling < _BULL_FLAG_RETRACE_CEIL  # genuinely tightens for calm names
    eff = _eff_ceil(CALM_ATR, enabled=True)
    # A deep retrace exceeds the calm ceiling -> GUARD 4 would REJECT (bull_flag_too_deep).
    assert DEEP_RETRACE > eff
    # Sanity: under the OLD ceiling the same calm pull would have PASSED (this is the fix).
    assert DEEP_RETRACE <= _flag_ceil_only(CALM_ATR)


# ── 3. EXPLOSIVE name + SAME deep pull => PASSES (no fewer-fills regression) ────────
def test_explosive_same_deep_pullback_passes() -> None:
    ceiling = _adaptive_pullback_depth_ceiling(EXPLOSIVE_ATR, enabled=True)
    # ~0.575 per LOCATE: wider than the calm ceiling -> deeper tolerance preserved.
    assert ceiling == pytest.approx(0.575, abs=1e-9)
    eff = _eff_ceil(EXPLOSIVE_ATR, enabled=True)
    # The SAME deep retrace that a calm name failed now clears the explosive ceiling.
    assert DEEP_RETRACE <= eff
    # The widening is monotone: explosive ceiling strictly > calm ceiling.
    assert ceiling > _adaptive_pullback_depth_ceiling(CALM_ATR, enabled=True)
    # THE load-bearing property: enabling the feature does NOT reject this explosive
    # name's normal deeper pull (the documented 0-fills regression is avoided).
    assert _eff_ceil(EXPLOSIVE_ATR, enabled=True) >= DEEP_RETRACE


# ── 4. SHALLOW pull (either regime) => PASSES under both the ON and OFF ceilings ────
@pytest.mark.parametrize("atr_pct", [CALM_ATR, EXPLOSIVE_ATR])
def test_shallow_pullback_passes_both_regimes(atr_pct: float) -> None:
    eff_on = _eff_ceil(atr_pct, enabled=True)
    eff_off = _eff_ceil(atr_pct, enabled=False)
    assert SHALLOW_RETRACE <= eff_on
    assert SHALLOW_RETRACE <= eff_off


# ── 5. Hard cap: never deeper than _VOL_SHALLOW_CEIL (0.75) ─────────────────────────
@pytest.mark.parametrize("atr_pct", [0.20, 0.50, 1.0, 5.0])
def test_hard_cap_never_exceeded(atr_pct: float) -> None:
    ceiling = _adaptive_pullback_depth_ceiling(atr_pct, enabled=True)
    assert ceiling <= _VOL_SHALLOW_CEIL
    assert _VOL_SHALLOW_CEIL == 0.75
    # At a very high ATR% the cap binds exactly.
    assert _adaptive_pullback_depth_ceiling(0.50, enabled=True) == pytest.approx(
        _VOL_SHALLOW_CEIL, abs=1e-9
    )


# ── 6. Formula reuses the ONE documented base (no fixed per-name magic) ─────────────
@pytest.mark.parametrize("atr_pct", [CALM_ATR, 0.03, EXPLOSIVE_ATR, 0.12])
def test_formula_reuses_documented_base(atr_pct: float) -> None:
    expected = min(_VOL_SHALLOW_CEIL, _VOL_SHALLOW_BASE + atr_pct * _VOL_SHALLOW_ATR_MULT)
    assert _adaptive_pullback_depth_ceiling(atr_pct, enabled=True) == pytest.approx(
        expected, abs=1e-12
    )
    # The base IS the calm-name floor of the ceiling (Ross ~50%).
    assert _VOL_SHALLOW_BASE == 0.50


# ── 7. atr_pct None / <= 0 collapses to the calm-floor base (defensive) ────────────
@pytest.mark.parametrize("atr_pct", [None, 0.0, -0.01])
def test_none_or_nonpositive_atr_uses_base_floor(atr_pct) -> None:
    assert _adaptive_pullback_depth_ceiling(atr_pct, enabled=True) == pytest.approx(
        _VOL_SHALLOW_BASE, abs=1e-12
    )
    # Still byte-identical (0.0) when disabled, irrespective of atr_pct shape.
    assert _adaptive_pullback_depth_ceiling(atr_pct, enabled=False) == 0.0


# ════════════════════════════════════════════════════════════════════════════════════
# HARDENING (THIN, adversarial): boundary ATR, min-seam crossover, monotonicity,
# huge/zero atr cap-binding, and flag-parity edges. Each asserts the SPECIFIC value or
# branch reason so a regression in that exact seam fails loudly (not just truthiness).
# ════════════════════════════════════════════════════════════════════════════════════

# The ATR% at which the ceiling formula hits the hard cap: 0.50 + a*1.5 == 0.75 -> a = 1/6.
_CAP_BIND_ATR = (_VOL_SHALLOW_CEIL - _VOL_SHALLOW_BASE) / _VOL_SHALLOW_ATR_MULT  # 1/6


# ── H1. Cap-binding BOUNDARY: eps-below caps below 0.75, exactly-at and eps-above pin ─
def test_cap_bind_boundary_exact_and_around() -> None:
    # Sanity: the derived crossover is 1/6 with this documented base/mult.
    assert _CAP_BIND_ATR == pytest.approx(1.0 / 6.0, abs=1e-12)
    eps = 1e-6
    below = _adaptive_pullback_depth_ceiling(_CAP_BIND_ATR - eps, enabled=True)
    at = _adaptive_pullback_depth_ceiling(_CAP_BIND_ATR, enabled=True)
    above = _adaptive_pullback_depth_ceiling(_CAP_BIND_ATR + eps, enabled=True)
    # eps-below the crossover the cap does NOT yet bind -> strictly under 0.75.
    assert below < _VOL_SHALLOW_CEIL
    assert below == pytest.approx(
        _VOL_SHALLOW_BASE + (_CAP_BIND_ATR - eps) * _VOL_SHALLOW_ATR_MULT, abs=1e-12
    )
    # exactly-at and eps-above the cap binds at 0.75 EXACTLY (min clamps, never overshoots).
    assert at == pytest.approx(_VOL_SHALLOW_CEIL, abs=1e-12)
    assert above == pytest.approx(_VOL_SHALLOW_CEIL, abs=1e-12)


# ── H2. The min SEAM only TIGHTENS: eff_ceil(enabled) is never looser than flag_ceil ─
@pytest.mark.parametrize(
    "atr_pct", [0.0, CALM_ATR, 0.02, 0.03, EXPLOSIVE_ATR, 0.10, _CAP_BIND_ATR, 0.20, 0.50]
)
def test_seam_never_relaxes_existing_ceiling(atr_pct: float) -> None:
    eff_on = _eff_ceil(atr_pct, enabled=True)
    flag_only = _flag_ceil_only(atr_pct)
    # Enabling the feature can only narrow (or equal) the existing ceiling, never widen.
    assert eff_on <= flag_only + 1e-12
    # And the seam equals the literal min of the two ceilings (no other arithmetic).
    adaptive = _adaptive_pullback_depth_ceiling(atr_pct, enabled=True)
    assert eff_on == pytest.approx(min(flag_only, adaptive), abs=1e-12)


# ── H3. flag_ceil vs adaptive CROSSOVER: which limb the min picks flips with ATR% ────
def test_min_seam_picks_tighter_limb_either_side_of_crossover() -> None:
    # flag_ceil = 0.70 + 1.5*a ; adaptive = 0.50 + 1.5*a (until adaptive caps at 0.75).
    # Below the adaptive cap the two move in PARALLEL (adaptive always 0.20 lower), so the
    # min picks ADAPTIVE for every calm/explosive ATR -> the seam genuinely tightens.
    for atr in (CALM_ATR, EXPLOSIVE_ATR, 0.10):
        adaptive = _adaptive_pullback_depth_ceiling(atr, enabled=True)
        flag_only = _flag_ceil_only(atr)
        assert adaptive < flag_only  # adaptive is the binding (tighter) limb
        assert _eff_ceil(atr, enabled=True) == pytest.approx(adaptive, abs=1e-12)
    # Once adaptive is capped (0.75) but flag_ceil is still climbing toward 0.90, adaptive
    # remains the tighter limb -> the seam stays pinned at the 0.75 cap.
    big = 0.20  # > 1/6 crossover; flag_ceil = 0.70 + 0.30 = 1.00 -> clamped 0.90
    assert _flag_ceil_only(big) == pytest.approx(0.90, abs=1e-12)
    assert _adaptive_pullback_depth_ceiling(big, enabled=True) == pytest.approx(0.75, 1e-12)
    assert _eff_ceil(big, enabled=True) == pytest.approx(0.75, abs=1e-12)


# ── H4. Strict MONOTONICITY then plateau: ceiling rises with ATR% up to the cap, flat after ─
def test_ceiling_monotone_nondecreasing_and_plateaus() -> None:
    sweep = [0.0, 0.005, CALM_ATR, 0.02, 0.03, EXPLOSIVE_ATR, 0.10, _CAP_BIND_ATR]
    vals = [_adaptive_pullback_depth_ceiling(a, enabled=True) for a in sweep]
    # Strictly increasing while under the cap (calm < explosive, the load-bearing widening).
    for lo, hi in zip(vals, vals[1:]):
        assert hi >= lo
    assert vals[0] == pytest.approx(_VOL_SHALLOW_BASE, abs=1e-12)  # ATR 0 -> base floor
    assert vals[2] > vals[0]  # calm already above the bare floor
    assert vals[5] > vals[2]  # explosive strictly wider than calm
    # After the crossover it plateaus at the cap (no further widening).
    plateau = [_adaptive_pullback_depth_ceiling(a, enabled=True) for a in (0.17, 0.30, 2.0)]
    assert all(v == pytest.approx(_VOL_SHALLOW_CEIL, abs=1e-12) for v in plateau)


# ── H5. HUGE / pathological ATR% never overshoots the cap; NaN-free, bounded ─────────
@pytest.mark.parametrize("atr_pct", [1.0, 5.0, 1e6, float("inf")])
def test_huge_atr_pins_cap_no_overshoot(atr_pct: float) -> None:
    ceiling = _adaptive_pullback_depth_ceiling(atr_pct, enabled=True)
    assert ceiling == pytest.approx(_VOL_SHALLOW_CEIL, abs=1e-12)
    assert ceiling == ceiling  # not NaN
    # Disabled still byte-identical 0.0 even at pathological ATR%.
    assert _adaptive_pullback_depth_ceiling(atr_pct, enabled=False) == 0.0


# ── H6. FLAG PARITY at the same atr: ON tightens by exactly the base gap, OFF is no-op ─
@pytest.mark.parametrize("atr_pct", [0.0, CALM_ATR, EXPLOSIVE_ATR, 0.10])
def test_flag_parity_off_is_noop_on_tightens_by_base_gap(atr_pct: float) -> None:
    # OFF: the seam is EXACTLY the legacy ceiling (the regression-proof invariant).
    assert _eff_ceil(atr_pct, enabled=False) == _flag_ceil_only(atr_pct)
    # ON (below the cap): the binding ceiling drops by precisely the documented base gap
    # between the flag ceiling (0.70) and the adaptive base (0.50) = 0.20, since both
    # limbs share the SAME ATR multiplier and the lower limb binds.
    if atr_pct < _CAP_BIND_ATR:
        gap = _BULL_FLAG_RETRACE_CEIL - _VOL_SHALLOW_BASE  # 0.20
        assert _eff_ceil(atr_pct, enabled=True) == pytest.approx(
            _flag_ceil_only(atr_pct) - gap, abs=1e-12
        )


# ── H7. The disabled SHORT-CIRCUIT: enabled=False returns before reading atr at all ──
def test_disabled_short_circuits_before_reading_atr() -> None:
    # A non-numeric atr would raise in float(...) IF the function read it while disabled.
    # The early `if not enabled: return 0.0` must fire first -> no exception, exact 0.0.
    class _Boom:
        def __float__(self):  # pragma: no cover - must never be called when disabled
            raise AssertionError("atr_pct read while disabled -- short-circuit regressed")

    assert _adaptive_pullback_depth_ceiling(_Boom(), enabled=False) == 0.0
    # Whereas ENABLED it WOULD consult atr (here the guarded None-path returns the base).
    assert _adaptive_pullback_depth_ceiling(None, enabled=True) == pytest.approx(
        _VOL_SHALLOW_BASE, abs=1e-12
    )
