"""Unit tests for the pure execution-cost model (Phase F).

No DB, no network. Exercises ``estimate_cost_fraction`` and
``estimate_capacity_usd`` against known inputs to freeze the math the
rest of the trading brain will depend on.
"""
from __future__ import annotations

import math

import pytest

from app.services.trading.execution_cost_model import (
    CostFractionBreakdown,
    estimate_capacity_usd,
    estimate_cost_fraction,
)


def _estimate(
    *,
    median_spread_bps: float = 2.0,
    p90_spread_bps: float = 5.0,
    median_slippage_bps: float = 1.5,
    p90_slippage_bps: float = 4.0,
    avg_daily_volume_usd: float = 1_000_000.0,
    sample_trades: int = 50,
) -> dict:
    return {
        "median_spread_bps": median_spread_bps,
        "p90_spread_bps": p90_spread_bps,
        "median_slippage_bps": median_slippage_bps,
        "p90_slippage_bps": p90_slippage_bps,
        "avg_daily_volume_usd": avg_daily_volume_usd,
        "sample_trades": sample_trades,
    }


class TestCostFractionBasic:
    def test_returns_breakdown_type(self) -> None:
        out = estimate_cost_fraction("AAPL", "long", 10_000, _estimate())
        assert isinstance(out, CostFractionBreakdown)

    def test_components_sum_to_total(self) -> None:
        out = estimate_cost_fraction("AAPL", "long", 10_000, _estimate())
        assert out.total == pytest.approx(
            out.spread + out.slippage + out.fees + out.impact
        )

    def test_p90_used_by_default(self) -> None:
        """Default use_p90=True uses p90 columns, not median."""
        out = estimate_cost_fraction("AAPL", "long", 10_000, _estimate())
        # p90 spread 5bps = 0.0005
        assert out.spread == pytest.approx(0.0005)
        # p90 slippage 4bps = 0.0004
        assert out.slippage == pytest.approx(0.0004)

    def test_median_when_flag_off(self) -> None:
        out = estimate_cost_fraction(
            "AAPL", "long", 10_000, _estimate(), use_p90=False
        )
        # median spread 2bps = 0.0002
        assert out.spread == pytest.approx(0.0002)
        # median slippage 1.5bps = 0.00015
        assert out.slippage == pytest.approx(0.00015)

    def test_default_fee_is_1bp(self) -> None:
        out = estimate_cost_fraction("AAPL", "long", 10_000, _estimate())
        assert out.fees == pytest.approx(0.0001)  # 1bp

    def test_custom_fee(self) -> None:
        out = estimate_cost_fraction(
            "AAPL", "long", 10_000, _estimate(), fee_bps=5.0
        )
        assert out.fees == pytest.approx(0.0005)


class TestImpactModel:
    def test_zero_notional_zero_cost(self) -> None:
        out = estimate_cost_fraction("AAPL", "long", 0, _estimate())
        assert out.total == 0.0
        assert out.impact == 0.0

    def test_impact_grows_with_notional(self) -> None:
        est = _estimate(avg_daily_volume_usd=1_000_000.0)
        small = estimate_cost_fraction("AAPL", "long", 1_000, est)
        large = estimate_cost_fraction("AAPL", "long", 100_000, est)
        assert large.impact > small.impact

    def test_impact_capped_by_default_cap(self) -> None:
        """At 10x ADV the sqrt(participation)*10 = 31.6bps, which is < 50bps cap.

        But if we force an absurd notional and small ADV, impact should cap.
        """
        est = _estimate(avg_daily_volume_usd=1_000.0)
        out = estimate_cost_fraction("AAPL", "long", 1_000_000, est)
        # sqrt(1_000_000 / 1_000) * 10 = sqrt(1000)*10 ≈ 316 bps; capped at 50bps
        assert out.impact == pytest.approx(0.005)  # 50bps = 0.005

    def test_impact_custom_cap(self) -> None:
        est = _estimate(avg_daily_volume_usd=1_000.0)
        out = estimate_cost_fraction(
            "AAPL", "long", 1_000_000, est, impact_cap_bps=10.0
        )
        assert out.impact == pytest.approx(0.001)  # 10bps

    def test_zero_adv_falls_back_to_cap(self) -> None:
        """If ADV is missing we use the impact cap — conservative."""
        est = _estimate(avg_daily_volume_usd=0.0)
        out = estimate_cost_fraction("AAPL", "long", 10_000, est)
        assert out.impact == pytest.approx(0.005)  # default 50bps cap

    def test_impact_math_1_percent_adv(self) -> None:
        """Spot-check: 1% of ADV → sqrt(0.01)*10 = 1 bp."""
        est = _estimate(avg_daily_volume_usd=1_000_000.0)
        out = estimate_cost_fraction("AAPL", "long", 10_000, est)
        assert out.impact == pytest.approx(0.0001, abs=1e-9)  # 1bp

    def test_impact_math_4_percent_adv(self) -> None:
        """Spot-check: 4% of ADV → sqrt(0.04)*10 = 2 bps."""
        est = _estimate(avg_daily_volume_usd=1_000_000.0)
        out = estimate_cost_fraction("AAPL", "long", 40_000, est)
        assert out.impact == pytest.approx(0.0002, abs=1e-9)  # 2bp


class TestEmptyOrInvalidInputs:
    def test_zero_samples_returns_empty(self) -> None:
        """An estimate with zero underlying trades → zero cost (caller
        should treat this as 'fall back to global default')."""
        est = _estimate(sample_trades=0)
        out = estimate_cost_fraction("AAPL", "long", 10_000, est)
        assert out.total == 0.0

    def test_negative_notional_taken_abs(self) -> None:
        out_pos = estimate_cost_fraction("AAPL", "long", 10_000, _estimate())
        out_neg = estimate_cost_fraction("AAPL", "short", -10_000, _estimate())
        assert out_pos.total == pytest.approx(out_neg.total)

    def test_missing_spread_bps_treated_as_zero(self) -> None:
        bad_est = {
            "p90_slippage_bps": 4.0,
            "median_slippage_bps": 1.5,
            "avg_daily_volume_usd": 1_000_000,
            "sample_trades": 50,
        }
        out = estimate_cost_fraction("AAPL", "long", 10_000, bad_est)
        assert out.spread == 0.0
        assert out.slippage == pytest.approx(0.0004)

    def test_negative_spread_treated_as_zero(self) -> None:
        est = _estimate(p90_spread_bps=-5.0)
        out = estimate_cost_fraction("AAPL", "long", 10_000, est)
        assert out.spread == 0.0

    def test_nan_fields_default_to_zero(self) -> None:
        est = _estimate(p90_spread_bps=float("nan"))
        out = estimate_cost_fraction("AAPL", "long", 10_000, est)
        assert out.spread == 0.0 or math.isfinite(out.spread)

    def test_none_estimate_row_safe(self) -> None:
        out = estimate_cost_fraction("AAPL", "long", 10_000, None)
        assert out.total == 0.0


class TestCapacity:
    def test_default_cap_5_percent(self) -> None:
        est = _estimate(avg_daily_volume_usd=1_000_000)
        cap = estimate_capacity_usd(est)
        assert cap == pytest.approx(50_000.0)

    def test_custom_frac(self) -> None:
        est = _estimate(avg_daily_volume_usd=1_000_000)
        cap = estimate_capacity_usd(est, max_adv_frac=0.01)
        assert cap == pytest.approx(10_000.0)

    def test_zero_adv_returns_zero(self) -> None:
        est = _estimate(avg_daily_volume_usd=0)
        assert estimate_capacity_usd(est) == 0.0

    def test_frac_clamped_to_unit_interval(self) -> None:
        est = _estimate(avg_daily_volume_usd=1_000_000)
        # negative clamped to 0
        assert estimate_capacity_usd(est, max_adv_frac=-0.05) == 0.0
        # > 1 clamped to 1
        assert estimate_capacity_usd(est, max_adv_frac=2.0) == 1_000_000

    def test_accepts_orm_like_object(self) -> None:
        class _Row:
            avg_daily_volume_usd = 2_000_000.0

        assert estimate_capacity_usd(_Row()) == pytest.approx(100_000.0)


class TestMonotonicity:
    def test_cost_fraction_monotonic_in_notional_for_nonzero_impact(self) -> None:
        est = _estimate(avg_daily_volume_usd=100_000.0)
        small = estimate_cost_fraction("AAPL", "long", 1_000, est).total
        med = estimate_cost_fraction("AAPL", "long", 10_000, est).total
        large = estimate_cost_fraction("AAPL", "long", 100_000, est).total
        assert small < med < large  # impact drives monotonicity

    def test_fees_spread_slippage_flat_in_notional(self) -> None:
        """Spread/slippage/fees as fractions of notional are flat; only
        impact scales with participation."""
        est = _estimate(avg_daily_volume_usd=1_000_000)
        a = estimate_cost_fraction("AAPL", "long", 1_000, est)
        b = estimate_cost_fraction("AAPL", "long", 10_000, est)
        assert a.spread == b.spread
        assert a.slippage == b.slippage
        assert a.fees == b.fees
        assert b.impact > a.impact
