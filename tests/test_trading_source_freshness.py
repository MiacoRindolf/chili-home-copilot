"""Tests for conservative board data_as_of from source timestamps."""
from __future__ import annotations

from app.services.trading.trading_source_freshness import compute_board_data_as_of


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
