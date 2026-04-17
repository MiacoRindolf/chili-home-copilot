"""Unit tests for `app/services/trading/promotion_metric.py`."""
from __future__ import annotations

import pytest

from app.services.trading.promotion_metric import (
    compare_economic,
    compute_economic_score,
)


class TestComputeEconomicScore:
    def test_basic(self) -> None:
        # 1% expected PnL, Brier 0.2, penalty 1.0 → 0.01 - 0.2 = -0.19
        assert compute_economic_score(0.01, 0.2) == pytest.approx(-0.19)

    def test_penalty_scaling(self) -> None:
        assert compute_economic_score(0.05, 0.1, brier_penalty=0.0) == pytest.approx(0.05)
        assert compute_economic_score(0.05, 0.1, brier_penalty=2.0) == pytest.approx(-0.15)

    @pytest.mark.parametrize("expected,brier", [(None, 0.1), (0.01, None), (None, None)])
    def test_missing_returns_none(self, expected: float, brier: float) -> None:
        assert compute_economic_score(expected, brier) is None


class TestCompareEconomic:
    def _active(self, pnl: float = 0.010, brier: float = 0.210) -> dict:
        return {"expected_pnl_oos_pct": pnl, "oos_brier_score": brier}

    def _shadow(self, pnl: float = 0.015, brier: float = 0.200) -> dict:
        return {"expected_pnl_oos_pct": pnl, "oos_brier_score": brier}

    def test_shadow_better_on_both_axes(self) -> None:
        out = compare_economic(self._active(), self._shadow())
        assert out.better is True
        assert out.reason == "economic_improvement"
        assert out.economic_delta is not None and out.economic_delta > 0
        assert out.expected_pnl_delta == pytest.approx(0.005)
        assert out.brier_delta == pytest.approx(-0.010)
        assert out.brier_regression_ok is True

    def test_shadow_worse_pnl(self) -> None:
        out = compare_economic(
            self._active(pnl=0.020, brier=0.200),
            self._shadow(pnl=0.010, brier=0.200),
        )
        assert out.better is False
        assert out.reason == "insufficient_improvement"
        assert out.economic_delta == pytest.approx(-0.010)
        assert out.brier_regression_ok is True

    def test_brier_regression_rejects_even_with_better_pnl(self) -> None:
        # expected PnL up, but Brier worse by 0.05 > default 0.01 guardrail.
        out = compare_economic(
            self._active(pnl=0.010, brier=0.200),
            self._shadow(pnl=0.100, brier=0.260),
        )
        assert out.better is False
        assert out.reason == "brier_regression"
        assert out.brier_regression_ok is False
        assert out.brier_delta == pytest.approx(0.060)
        # economic_delta still reported for transparency
        assert out.economic_delta is not None

    def test_brier_regression_within_tolerance_allowed(self) -> None:
        # Brier up by 0.005 < default 0.01 → still allowed if PnL beats.
        out = compare_economic(
            self._active(pnl=0.010, brier=0.200),
            self._shadow(pnl=0.050, brier=0.205),
            min_improvement=0.0,
        )
        assert out.better is True
        assert out.reason == "economic_improvement"
        assert out.brier_regression_ok is True

    def test_missing_brier_returns_missing_metric(self) -> None:
        out = compare_economic(
            {"expected_pnl_oos_pct": 0.01},
            self._shadow(),
        )
        assert out.better is False
        assert out.reason == "missing_metric"

    def test_missing_pnl_returns_missing_metric(self) -> None:
        out = compare_economic(self._active(), {"oos_brier_score": 0.2})
        assert out.better is False
        assert out.reason == "missing_metric"

    def test_min_improvement_gate(self) -> None:
        # shadow is 0.001 better → with min_improvement 0.01 it should reject.
        out = compare_economic(
            self._active(pnl=0.010, brier=0.200),
            self._shadow(pnl=0.011, brier=0.200),
            min_improvement=0.01,
        )
        assert out.better is False
        assert out.reason == "insufficient_improvement"

    def test_alt_metric_keys_supported(self) -> None:
        """Accept ``expected_pnl_per_trade`` + ``oos_brier`` as fallbacks."""
        active = {"expected_pnl_per_trade": 0.01, "oos_brier": 0.210}
        shadow = {"expected_pnl_per_trade": 0.015, "oos_brier": 0.200}
        out = compare_economic(active, shadow)
        assert out.better is True
        assert out.reason == "economic_improvement"

    def test_none_inputs(self) -> None:
        out = compare_economic(None, None)
        assert out.better is False
        assert out.reason == "missing_metric"

    def test_brier_guardrail_beats_min_improvement(self) -> None:
        """When brier regresses, we reject even if economic_delta >= min_improvement."""
        out = compare_economic(
            {"expected_pnl_oos_pct": 0.01, "oos_brier_score": 0.20},
            {"expected_pnl_oos_pct": 0.10, "oos_brier_score": 0.30},  # brier +0.10
            max_brier_regression=0.01,
            min_improvement=0.0,
        )
        assert out.better is False
        assert out.reason == "brier_regression"
