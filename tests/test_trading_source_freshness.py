"""Tests for conservative board data_as_of from source timestamps."""
from __future__ import annotations

from app.services.trading.trading_source_freshness import (
    compute_board_data_as_of,
    compute_board_freshness_status,
)


def test_compute_board_data_as_of_is_minimum_of_sources() -> None:
    sf = {
        "scan_results_latest_utc": "2026-04-01T00:00:00+00:00",
        "predictions_cache_last_updated_utc": "2026-04-05T00:00:00+00:00",
        "prescreen_snapshot_finished_latest_utc": None,
    }
    dao, keys = compute_board_data_as_of(sf)
    assert dao is not None
    assert dao.startswith("2026-04-01")
    assert "scan_results_latest_utc" in keys


def test_compute_board_data_as_of_empty() -> None:
    dao, keys = compute_board_data_as_of({})
    assert dao is None
    assert keys == []


def test_board_freshness_status_marks_partial_source_clocks_unknown() -> None:
    status = compute_board_freshness_status(
        {
            "scan_results_latest_utc": "2026-04-01T00:00:00+00:00",
            "predictions_cache_last_updated_utc": "2026-04-05T00:00:00+00:00",
        }
    )

    assert status["data_as_of"].startswith("2026-04-01")
    assert status["freshness_unknown"] is True
    assert "prescreen_snapshot_finished_latest_utc" in status["missing_source_keys"]
    assert status["source_status"]["scan_results_latest_utc"] == "complete"


def test_board_freshness_status_marks_malformed_source_clock_unknown() -> None:
    status = compute_board_freshness_status(
        {
            "scan_results_latest_utc": "2026-04-01T00:00:00+00:00",
            "prescreen_snapshot_finished_latest_utc": "not-a-date",
            "prescreen_candidate_last_seen_latest_utc": "2026-04-02T00:00:00+00:00",
            "imminent_job_ok_latest_utc": "2026-04-03T00:00:00+00:00",
            "predictions_cache_last_updated_utc": "2026-04-04T00:00:00+00:00",
        }
    )

    assert status["freshness_unknown"] is True
    assert status["invalid_source_keys"] == ["prescreen_snapshot_finished_latest_utc"]
    assert status["source_status"]["prescreen_snapshot_finished_latest_utc"] == "invalid"
