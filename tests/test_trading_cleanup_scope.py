from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


def _request_for(filename: str) -> SimpleNamespace:
    return SimpleNamespace(node=SimpleNamespace(fspath=Path(filename)))


def test_triple_barrier_db_tests_use_trading_targeted_cleanup() -> None:
    conftest = sys.modules["tests.conftest"]

    for filename in (
        "test_triple_barrier_label_anchor.py",
        "test_triple_barrier_labeler.py",
        "test_triple_barrier_scheduler.py",
    ):
        tables = conftest._test_targeted_cleanup_tables(_request_for(filename))

        assert tables is conftest._TRADING_DOMAIN_TARGETED_TABLES
        assert "trading_snapshots" in tables
        assert "trading_triple_barrier_labels" in tables
        assert "scan_patterns" in tables
        assert "birthdays" not in tables


def test_autotrader_integration_db_tests_use_trading_targeted_cleanup() -> None:
    conftest = sys.modules["tests.conftest"]

    tables = conftest._test_targeted_cleanup_tables(
        _request_for("test_auto_trader_integration.py")
    )

    assert tables is conftest._TRADING_DOMAIN_TARGETED_TABLES
    assert "trading_autotrader_runs" in tables
    assert "trading_breakout_alerts" in tables
    assert "trading_paper_trades" in tables
    assert "birthdays" not in tables
