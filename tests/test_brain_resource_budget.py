"""Brain resource budget invariants."""

from __future__ import annotations

import logging

from app.services.trading.brain_resource_budget import BrainResourceBudget


def test_miner_rows_cap_logs_once_and_tracks_rejections(caplog) -> None:
    budget = BrainResourceBudget(
        ohlcv_cap=0,
        miner_rows_cap=5,
        pattern_inject_cap=0,
    )

    with caplog.at_level(logging.WARNING):
        assert budget.add_miner_rows(3) == 3
        assert budget.add_miner_rows(4) == 2
        assert budget.add_miner_rows(7) == 0

    messages = [
        rec.getMessage()
        for rec in caplog.records
        if "[brain.budget] miner_rows cap" in rec.getMessage()
    ]
    assert messages == ["[brain.budget] miner_rows cap: accepted 2 of 4 rows"]

    report = budget.to_report_dict()
    assert report["miner_rows_used"] == 5
    assert report["miner_rows_rejected"] == 9
    assert report["miner_rows_remaining"] == 0
    assert report["exhausted"] == {"miner_rows": "cap=5"}


def test_remaining_miner_rows_is_none_when_unlimited() -> None:
    budget = BrainResourceBudget(
        ohlcv_cap=0,
        miner_rows_cap=0,
        pattern_inject_cap=0,
    )

    assert budget.remaining_miner_rows() is None
    assert budget.add_miner_rows(123) == 123
    report = budget.to_report_dict()
    assert report["miner_rows_used"] == 0
    assert report["miner_rows_rejected"] == 0
    assert report["miner_rows_remaining"] is None
