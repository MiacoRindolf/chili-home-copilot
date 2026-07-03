"""FIX-16 (B3) — NOTIONAL-CEILING DOUBLE-HAIRCUT parity.

The variant-performance size multiplier used to be folded into the per-trade NOTIONAL
CEILING. Because the risk-first quantity (max_loss / stop_distance) sits well below that
ceiling on real live scores (0.58-0.73), a DOWN-only perf multiplier silently clipped the
realized dollar-risk far below the *designed* risk while never being able to add shares —
"arithmetic theater". Example (KTOS): designed risk $158.24, realized only $12.80.

The fix (`chili_momentum_notional_pure_liquidity_cap_enabled`, default True):
  * the notional ceiling reverts to a PURE liquidity/BP cap (no perf haircut), so the
    risk-first qty realizes the FULL designed dollar-risk (bounded only by the pure
    liquidity cap + the per-trade risk budget), and
  * perf scales the per-trade RISK BUDGET once instead (surfaced by the allocator as
    ``performance_size_mult`` and applied in the runner's ``_eff_max_loss`` composition).

Legacy (flag OFF) is byte-identical: perf folds into ``recommended_notional`` and the
runner receives ``performance_size_mult == 1.0`` so it never double-applies.

This parity module proves the KTOS profile end-to-end at the sizing-math level and asserts
the two safety invariants: realized dollar-risk NEVER exceeds the per-trade cap, and the
notional NEVER exceeds the pure liquidity cap.
"""
from __future__ import annotations

import pytest

from app.config import Settings
from app.services.trading.momentum_neural.risk_policy import compute_risk_first_quantity

# ---- KTOS live profile (reconstructed from the loss forensics) ---------------------------
# entry $40, ATR% 0.10 -> stop_pct = 0.10 * 0.60 = 0.06 -> stop_distance = $2.40/share.
KTOS_ENTRY = 40.0
KTOS_ATR_PCT = 0.10
KTOS_STOP_ATR_MULT = 0.60
KTOS_STOP_DIST = KTOS_ENTRY * (KTOS_ATR_PCT * KTOS_STOP_ATR_MULT)  # 2.40
DESIGNED_RISK_USD = 158.24
# Designed (uncapped) risk-first notional the pure liquidity cap must be able to admit.
DESIGNED_QTY = DESIGNED_RISK_USD / KTOS_STOP_DIST                  # ~65.93 sh
DESIGNED_NOTIONAL = DESIGNED_QTY * KTOS_ENTRY                      # ~$2637
# The historical combined conviction x perf haircut that collapsed the ceiling so the
# risk-first qty realized only $12.80.
LEGACY_HAIRCUT = 12.80 / DESIGNED_RISK_USD                        # ~0.0809
# The pure liquidity/BP cap (equity-relative): comfortably above the designed notional so
# the designed risk is realizable — the whole point of FIX-16.
PURE_LIQUIDITY_CAP = 4000.0
PER_TRADE_RISK_CAP = 200.0  # per-trade max-loss cap ($) — the hard risk bound


def _realized_risk(qty: float, stop_distance: float) -> float:
    """Realized worst-case dollar-risk = shares * stop distance."""
    return qty * stop_distance


def test_flag_default_is_on():
    assert (
        Settings.model_fields["chili_momentum_notional_pure_liquidity_cap_enabled"].default
        is True
    )


def test_ktos_legacy_ceiling_haircut_clips_realized_risk_far_below_designed():
    """LEGACY (flag OFF): perf/conviction folded into the ceiling clips the risk-first qty
    to a fraction of the designed shares, so realized risk collapses to ~$12.80."""
    # Legacy: recommended_notional = base_cap * conviction * perf, where base_cap tracks the
    # designed risk-first notional. The combined haircut collapses the ceiling to a fraction
    # of the designed notional, which then binds the risk-first qty (this is the live path,
    # where max_notional = min(pure_cap, recommended_notional)).
    legacy_ceiling = DESIGNED_NOTIONAL * LEGACY_HAIRCUT
    qty, meta = compute_risk_first_quantity(
        entry_price=KTOS_ENTRY,
        atr_pct=KTOS_ATR_PCT,
        max_loss_usd=DESIGNED_RISK_USD,
        max_notional_ceiling_usd=legacy_ceiling,
        stop_atr_mult=KTOS_STOP_ATR_MULT,
    )
    assert meta["capped_by"] == "notional_ceiling"  # the ceiling binds, not the risk budget
    realized = _realized_risk(qty, meta["stop_distance"])
    # The forensic KTOS realized risk (~$12.80) — an order of magnitude below designed.
    assert realized == pytest.approx(12.80, rel=0.02)
    assert realized < DESIGNED_RISK_USD * 0.10  # <10% of designed = the double-haircut bug


def test_ktos_pure_liquidity_cap_realizes_designed_risk():
    """FIXED (flag ON): the ceiling is a PURE liquidity cap (no perf haircut), so the
    risk-first qty realizes the FULL designed dollar-risk — bounded only by the liquidity
    cap + the per-trade risk budget."""
    qty, meta = compute_risk_first_quantity(
        entry_price=KTOS_ENTRY,
        atr_pct=KTOS_ATR_PCT,
        max_loss_usd=DESIGNED_RISK_USD,
        max_notional_ceiling_usd=PURE_LIQUIDITY_CAP,
        stop_atr_mult=KTOS_STOP_ATR_MULT,
    )
    # The pure cap is above the designed notional, so the risk budget binds — not the ceiling.
    assert meta["capped_by"] is None
    realized = _realized_risk(qty, meta["stop_distance"])
    assert realized == pytest.approx(DESIGNED_RISK_USD, rel=1e-3)
    # ~12x the crippled legacy realized risk — the boost levers now actually add shares.
    assert realized > 12.80 * 10


def test_invariant_realized_risk_never_exceeds_per_trade_cap():
    """Safety: the per-trade RISK budget (max_loss_usd) is the hard bound. Even under the
    pure liquidity cap, realized worst-case risk == the per-trade risk budget, never above."""
    qty, meta = compute_risk_first_quantity(
        entry_price=KTOS_ENTRY,
        atr_pct=KTOS_ATR_PCT,
        max_loss_usd=PER_TRADE_RISK_CAP,       # the per-trade cap IS the risk budget
        max_notional_ceiling_usd=PURE_LIQUIDITY_CAP,
        stop_atr_mult=KTOS_STOP_ATR_MULT,
    )
    realized = _realized_risk(qty, meta["stop_distance"])
    assert realized <= PER_TRADE_RISK_CAP + 1e-6


def test_invariant_notional_never_exceeds_pure_liquidity_cap():
    """Safety: with a fat risk budget the ceiling binds — and it can NEVER exceed the pure
    liquidity cap (the #769 circuit + structural stop stay untouched by this fix)."""
    qty, meta = compute_risk_first_quantity(
        entry_price=KTOS_ENTRY,
        atr_pct=0.002,  # tiny ATR -> 0.3% stop floor -> huge risk-first qty -> ceiling binds
        max_loss_usd=10_000.0,  # deliberately fat risk budget
        max_notional_ceiling_usd=PURE_LIQUIDITY_CAP,
        stop_atr_mult=KTOS_STOP_ATR_MULT,
    )
    assert meta["capped_by"] == "notional_ceiling"
    assert qty * KTOS_ENTRY <= PURE_LIQUIDITY_CAP + 1e-6


def test_perf_multiplier_scales_risk_budget_once_not_the_ceiling():
    """The perf multiplier, applied to the RISK BUDGET (as the runner now does), sizes DOWN
    the realized dollar-risk PROPORTIONALLY — and it composes cleanly (a hot variant at 1.0
    is a no-op; a cold variant at the 0.3 floor cuts realized risk to 30%, NOT to a random
    fraction dictated by where the ceiling happened to sit)."""
    perf_cold = 0.30  # the floor of _momentum_variant_performance_size_mult
    qty_full, meta_full = compute_risk_first_quantity(
        entry_price=KTOS_ENTRY,
        atr_pct=KTOS_ATR_PCT,
        max_loss_usd=DESIGNED_RISK_USD,
        max_notional_ceiling_usd=PURE_LIQUIDITY_CAP,
        stop_atr_mult=KTOS_STOP_ATR_MULT,
    )
    qty_cold, meta_cold = compute_risk_first_quantity(
        entry_price=KTOS_ENTRY,
        atr_pct=KTOS_ATR_PCT,
        max_loss_usd=DESIGNED_RISK_USD * perf_cold,  # perf scales the RISK BUDGET once
        max_notional_ceiling_usd=PURE_LIQUIDITY_CAP,
        stop_atr_mult=KTOS_STOP_ATR_MULT,
    )
    r_full = _realized_risk(qty_full, meta_full["stop_distance"])
    r_cold = _realized_risk(qty_cold, meta_cold["stop_distance"])
    # Cold variant realizes exactly perf x the full risk — proportional, predictable.
    assert r_cold == pytest.approx(r_full * perf_cold, rel=1e-3)
    # And it is a pure size-DOWN (never above the full designed risk).
    assert r_cold < r_full
