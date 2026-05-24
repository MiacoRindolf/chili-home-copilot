from pathlib import Path

from sqlalchemy import String

from app.models.trading import TradingExecutionEvent


REPO = Path(__file__).resolve().parent.parent


def test_trading_execution_event_type_accepts_reconciler_labels():
    col_type = TradingExecutionEvent.__table__.c.event_type.type

    assert isinstance(col_type, String)
    assert (col_type.length or 0) >= 64
    assert len("g2_place_missing_stop_existing_coinbase_coverage") <= col_type.length


def test_migration_widens_execution_event_type_column():
    src = (REPO / "app/migrations.py").read_text(encoding="utf-8")

    assert "266_execution_event_type_width" in src
    assert "DROP VIEW IF EXISTS trading_execution_events_quarantine" in src
    assert "ALTER COLUMN event_type TYPE VARCHAR(64)" in src
    assert "CREATE OR REPLACE VIEW trading_execution_events_quarantine" in src
