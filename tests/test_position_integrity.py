"""Position-identity integrity guardrails."""
from __future__ import annotations

from pathlib import Path

from app.services.trading.position_integrity import PositionIntegrityReport


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


def test_position_integrity_report_counts():
    report = PositionIntegrityReport(
        open_positions_without_open_trade=[{"position_id": 1}],
        open_trades_without_open_position=[{"trade_id": 2}],
        open_positions_missing_current_envelope=[{"position_id": 3}],
        current_envelope_mismatches=[],
        repairable_current_envelope_links=[{"position_id": 3, "trade_id": 4}],
    )

    assert report.counts == {
        "open_positions_without_open_trade": 1,
        "open_trades_without_open_position": 1,
        "open_positions_missing_current_envelope": 1,
        "current_envelope_mismatches": 0,
        "repairable_current_envelope_links": 1,
    }


def test_position_integrity_repairs_only_exact_one_to_one_links():
    src = _read("app/services/trading/position_integrity.py")

    assert "open_trade_count = 1" in src
    assert "current_envelope_id IS NULL" in src
    assert "current_envelope_id = NULL" in src
    assert "UPDATE trading_positions" in src
    assert "does not create/close trades or positions" in src
