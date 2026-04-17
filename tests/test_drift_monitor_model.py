"""Pure unit tests for Phase J drift monitor model."""
from __future__ import annotations

import pytest

from app.services.trading.drift_monitor_model import (
    DriftMonitorConfig,
    DriftMonitorInput,
    compute_drift,
    compute_drift_id,
)


def _input(
    *,
    pattern_id: int = 42,
    baseline: float | None = 0.55,
    outcomes: list[int] | None = None,
    as_of_key: str | None = "2026-04-17",
) -> DriftMonitorInput:
    return DriftMonitorInput(
        scan_pattern_id=pattern_id,
        pattern_name=f"pat_{pattern_id}",
        baseline_win_prob=baseline,
        outcomes=outcomes or [],
        as_of_key=as_of_key,
    )


class TestEmptyAndDegenerate:
    def test_no_baseline_returns_green_null(self):
        out = compute_drift(_input(baseline=None, outcomes=[1, 0, 1]))
        assert out.severity == "green"
        assert out.observed_win_prob is None
        assert out.brier_delta is None
        assert out.cusum_statistic is None
        assert out.payload["reason"] == "insufficient_inputs"

    def test_empty_outcomes_returns_green_null(self):
        out = compute_drift(_input(outcomes=[]))
        assert out.severity == "green"
        assert out.sample_size == 0
        assert out.observed_win_prob is None


class TestSmallSampleNeverRed:
    def test_small_sample_below_yellow_threshold_stays_green(self):
        out = compute_drift(_input(baseline=0.5, outcomes=[0, 0, 0]))
        assert out.severity == "green"
        assert out.sample_size == 3

    def test_mid_sample_can_yellow_but_not_red(self):
        cfg = DriftMonitorConfig(min_red_sample=20, min_yellow_sample=10)
        outcomes = [0] * 15
        out = compute_drift(_input(baseline=0.55, outcomes=outcomes), config=cfg)
        assert out.severity in ("yellow", "red")
        assert out.severity != "red" or out.sample_size >= 20


class TestRedSeverityFires:
    def test_huge_brier_delta_red_with_sufficient_sample(self):
        outcomes = [0] * 25
        out = compute_drift(_input(baseline=0.70, outcomes=outcomes))
        assert out.severity == "red"
        assert out.observed_win_prob == 0.0
        assert out.brier_delta == pytest.approx(-0.70)

    def test_cusum_breach_red_even_with_moderate_brier(self):
        cfg = DriftMonitorConfig(cusum_threshold_mult=0.1)
        outcomes = [0] * 50
        out = compute_drift(_input(baseline=0.5, outcomes=outcomes), config=cfg)
        assert out.severity == "red"
        assert out.cusum_statistic is not None
        assert out.cusum_statistic >= out.cusum_threshold


class TestGreenPath:
    def test_perfect_calibration_stays_green(self):
        baseline = 0.5
        outcomes = [1, 0] * 20
        out = compute_drift(_input(baseline=baseline, outcomes=outcomes))
        assert out.severity == "green"
        assert out.observed_win_prob == pytest.approx(0.5)
        assert out.brier_delta == pytest.approx(0.0)


class TestDeterminism:
    def test_drift_id_stable_for_same_pattern_and_key(self):
        a = compute_drift_id(scan_pattern_id=7, as_of_key="2026-04-17")
        b = compute_drift_id(scan_pattern_id=7, as_of_key="2026-04-17")
        assert a == b

    def test_drift_id_differs_by_pattern(self):
        a = compute_drift_id(scan_pattern_id=7, as_of_key="2026-04-17")
        b = compute_drift_id(scan_pattern_id=8, as_of_key="2026-04-17")
        assert a != b

    def test_drift_id_differs_by_as_of_key(self):
        a = compute_drift_id(scan_pattern_id=7, as_of_key="2026-04-17")
        b = compute_drift_id(scan_pattern_id=7, as_of_key="2026-04-18")
        assert a != b

    def test_compute_drift_repeatable(self):
        inp = _input(baseline=0.55, outcomes=[1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 0] * 3)
        a = compute_drift(inp)
        b = compute_drift(inp)
        assert a == b


class TestPayload:
    def test_payload_carries_baseline_and_config(self):
        out = compute_drift(_input(baseline=0.55, outcomes=[1, 0, 1, 0, 1] * 5))
        assert out.payload["baseline"] == pytest.approx(0.55)
        assert "yellow_brier_abs" in out.payload
        assert "red_brier_abs" in out.payload


class TestClamping:
    def test_out_of_range_baseline_is_clamped(self):
        # baseline > 1.0 is clamped to 1.0, so all-wins sample has 0 delta
        out = compute_drift(_input(baseline=1.5, outcomes=[1] * 30))
        assert out.baseline_win_prob == 1.0
        assert out.brier_delta == pytest.approx(0.0)

    def test_negative_baseline_is_clamped(self):
        out = compute_drift(_input(baseline=-0.3, outcomes=[0] * 30))
        assert out.baseline_win_prob == 0.0
        assert out.brier_delta == pytest.approx(0.0)


class TestNonBinaryInputs:
    def test_nonzero_values_count_as_wins(self):
        out = compute_drift(
            _input(baseline=0.5, outcomes=[1, 2, 3, 0, 0, 0, 1, 2]),
        )
        assert out.observed_win_prob == pytest.approx(5 / 8)
