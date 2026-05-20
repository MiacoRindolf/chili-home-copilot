"""Position-identity Phase 5A -- additive decision/envelope bridge.

Phase 5A deliberately avoids the destructive ``trading_trades`` rename.
It creates an immutable ``trading_decisions`` layer, links today's Trade
rows to decision_id + position_id, and exposes a parity view for soak.
"""
from __future__ import annotations

import inspect

from app import migrations
from app.models.trading import Trade, TradingDecision


def test_phase5a_migration_registered_after_255():
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]
    assert "255_fast_path_retention_time_indexes" in ids
    assert "256_position_identity_phase5a_decision_bridge" in ids
    assert "257_position_identity_phase5a_trade_insert_trigger" in ids
    assert "258_position_identity_phase5a_residual_backfill" in ids
    assert ids.index("256_position_identity_phase5a_decision_bridge") == (
        ids.index("255_fast_path_retention_time_indexes") + 1
    )
    assert ids.index("257_position_identity_phase5a_trade_insert_trigger") == (
        ids.index("256_position_identity_phase5a_decision_bridge") + 1
    )
    assert ids.index("258_position_identity_phase5a_residual_backfill") == (
        ids.index("257_position_identity_phase5a_trade_insert_trigger") + 1
    )


def test_phase5a_is_additive_not_table_rename():
    src = inspect.getsource(migrations._migration_256_position_identity_phase5a_decision_bridge)
    assert "CREATE TABLE IF NOT EXISTS trading_decisions" in src
    assert "ADD COLUMN IF NOT EXISTS decision_id" in src
    assert "ADD COLUMN IF NOT EXISTS position_id" in src
    assert "CREATE OR REPLACE VIEW trading_phase5a_envelope_parity" in src
    assert "RENAME TO trading_management_envelopes" not in src
    assert "ALTER TABLE trading_trades RENAME" not in src


def test_phase5a_backfill_is_idempotent_and_lineage_keyed():
    src = inspect.getsource(migrations._migration_256_position_identity_phase5a_decision_bridge)
    assert "source_trade_id" in src
    assert "NOT EXISTS" in src
    assert "phase5a_backfill_from_trade_id=" in src
    assert "t.decision_id IS NULL" in src
    assert "t.position_id IS NULL" in src
    assert "t.entry_price > 0" in src
    assert "t.quantity > 0" in src


def test_phase5a_insert_trigger_keeps_future_trades_linked():
    src = inspect.getsource(
        migrations._migration_257_position_identity_phase5a_trade_insert_trigger
    )
    assert "CREATE TRIGGER trg_trading_trades_phase5a_after_insert" in src
    assert "INSERT INTO trading_decisions" in src
    assert "phase5a_insert_trigger_from_trade_id=" in src
    assert "UPDATE trading_trades" in src
    assert "decision_id = COALESCE" in src
    assert "position_id = COALESCE" in src


def test_phase5a_residual_backfill_covers_deploy_race_window():
    src = inspect.getsource(migrations._migration_258_position_identity_phase5a_residual_backfill)
    assert "phase5a_residual_backfill_from_trade_id=" in src
    assert "t.decision_id IS NULL" in src
    assert "t2.position_id IS NULL" in src
    assert "p.current_envelope_id = t2.id" in src


def test_phase5a_orm_metadata_declared():
    assert TradingDecision.__tablename__ == "trading_decisions"
    assert "source_trade_id" in TradingDecision.__table__.columns
    assert "indicator_snapshot" in TradingDecision.__table__.columns
    assert "decision_id" in Trade.__table__.columns
    assert "position_id" in Trade.__table__.columns
    decision_fk = next(iter(Trade.__table__.columns["decision_id"].foreign_keys))
    position_fk = next(iter(Trade.__table__.columns["position_id"].foreign_keys))
    assert str(decision_fk.column) == (
        "trading_decisions.id"
    )
    assert str(position_fk.column) == "trading_positions.id"
