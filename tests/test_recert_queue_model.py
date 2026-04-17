"""Pure unit tests for Phase J re-cert queue model."""
from __future__ import annotations

from datetime import date

import pytest

from app.services.trading.drift_monitor_model import (
    DriftMonitorInput,
    compute_drift,
    DriftMonitorConfig,
)
from app.services.trading.recert_queue_model import (
    RecertQueueConfig,
    compute_recert_id,
    propose_from_drift,
    propose_manual,
)


def _red_drift(pattern_id: int = 42):
    return compute_drift(
        DriftMonitorInput(
            scan_pattern_id=pattern_id,
            pattern_name=f"pat_{pattern_id}",
            baseline_win_prob=0.7,
            outcomes=[0] * 30,
            as_of_key="2026-04-17",
        )
    )


def _green_drift(pattern_id: int = 42):
    return compute_drift(
        DriftMonitorInput(
            scan_pattern_id=pattern_id,
            pattern_name=f"pat_{pattern_id}",
            baseline_win_prob=0.5,
            outcomes=[1, 0] * 15,
            as_of_key="2026-04-17",
        )
    )


def _yellow_drift(pattern_id: int = 42):
    cfg = DriftMonitorConfig(
        red_brier_abs=0.30,
        yellow_brier_abs=0.10,
        min_yellow_sample=10,
        min_red_sample=100,
        cusum_threshold_mult=3.0,
    )
    return compute_drift(
        DriftMonitorInput(
            scan_pattern_id=pattern_id,
            pattern_name=f"pat_{pattern_id}",
            baseline_win_prob=0.6,
            outcomes=[1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0],
            as_of_key="2026-04-17",
        ),
        config=cfg,
    )


class TestDriftToProposal:
    def test_red_produces_proposal(self):
        prop = propose_from_drift(_red_drift(), as_of_date=date(2026, 4, 17))
        assert prop is not None
        assert prop.severity == "red"
        assert prop.source == "drift_monitor"
        assert prop.status == "proposed"
        assert prop.scan_pattern_id == 42
        assert prop.reason and "drift_severity=red" in prop.reason

    def test_green_produces_no_proposal(self):
        prop = propose_from_drift(_green_drift(), as_of_date=date(2026, 4, 17))
        assert prop is None

    def test_yellow_default_no_proposal(self):
        prop = propose_from_drift(_yellow_drift(), as_of_date=date(2026, 4, 17))
        assert prop is None

    def test_yellow_with_opt_in_proposes(self):
        prop = propose_from_drift(
            _yellow_drift(),
            as_of_date=date(2026, 4, 17),
            config=RecertQueueConfig(include_yellow=True),
        )
        assert prop is not None
        assert prop.severity == "yellow"

    def test_string_date_accepted(self):
        prop = propose_from_drift(_red_drift(), as_of_date="2026-04-17")
        assert prop is not None
        assert prop.as_of_date == date(2026, 4, 17)

    def test_invalid_source_rejected(self):
        with pytest.raises(ValueError):
            propose_from_drift(
                _red_drift(),
                as_of_date=date(2026, 4, 17),
                source="not_a_source",
            )

    def test_drift_log_id_carried(self):
        prop = propose_from_drift(
            _red_drift(), as_of_date=date(2026, 4, 17), drift_log_id=7777,
        )
        assert prop is not None
        assert prop.drift_log_id == 7777


class TestManualProposal:
    def test_manual_always_produces(self):
        prop = propose_manual(
            scan_pattern_id=11,
            pattern_name="manual_pat",
            as_of_date=date(2026, 4, 17),
            reason="operator requested",
        )
        assert prop.source == "manual"
        assert prop.status == "proposed"
        assert prop.severity is None
        assert prop.reason == "operator requested"


class TestDeterminism:
    def test_recert_id_stable(self):
        a = compute_recert_id(
            scan_pattern_id=1, as_of_date=date(2026, 4, 17), source="manual",
        )
        b = compute_recert_id(
            scan_pattern_id=1, as_of_date=date(2026, 4, 17), source="manual",
        )
        assert a == b

    def test_recert_id_varies_by_source(self):
        a = compute_recert_id(
            scan_pattern_id=1, as_of_date=date(2026, 4, 17), source="manual",
        )
        b = compute_recert_id(
            scan_pattern_id=1, as_of_date=date(2026, 4, 17), source="drift_monitor",
        )
        assert a != b

    def test_recert_id_varies_by_date(self):
        a = compute_recert_id(
            scan_pattern_id=1, as_of_date=date(2026, 4, 17), source="manual",
        )
        b = compute_recert_id(
            scan_pattern_id=1, as_of_date=date(2026, 4, 18), source="manual",
        )
        assert a != b


class TestPayload:
    def test_payload_carries_stats(self):
        prop = propose_from_drift(_red_drift(), as_of_date=date(2026, 4, 17))
        assert prop is not None
        assert "baseline_win_prob" in prop.payload
        assert "observed_win_prob" in prop.payload
        assert "cusum_statistic" in prop.payload
        assert prop.payload["sample_size"] == 30
