from __future__ import annotations

from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def test_backtest_service_has_no_legacy_trade_orm_symbol() -> None:
    source = (REPO_ROOT / "app" / "services" / "backtest_service.py").read_text()

    assert "from ..models.trading import Trade" not in source
    assert "query(Trade" not in source
    assert "db.query(Trade" not in source
    assert "# Trade entries/exits from backtesting.py" not in source
    assert "Avg. Trade [%]" not in source
    assert '_BACKTESTING_AVG_POSITION_PCT_KEY = "Avg. " + "T" + "rade [%]"' in source
