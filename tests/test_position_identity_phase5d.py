"""Position-identity Phase 5D semantic attribution repair."""
from __future__ import annotations

import inspect

from app import migrations


def test_phase5d_migration_registered_after_phase5c():
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]
    assert "274_position_identity_phase5c_attribution_columns" in ids
    assert "275_position_identity_phase5d_decision_pattern_backfill" in ids
    assert ids.index("275_position_identity_phase5d_decision_pattern_backfill") == (
        ids.index("274_position_identity_phase5c_attribution_columns") + 1
    )


def test_phase5d_backfill_only_repairs_null_decision_pattern_ids():
    src = inspect.getsource(
        migrations._migration_275_position_identity_phase5d_decision_pattern_backfill
    )
    assert "JOIN trading_trades t ON t.id = d.source_trade_id" in src
    assert "WHERE d.scan_pattern_id IS NULL" in src
    assert "AND t.scan_pattern_id IS NOT NULL" in src
    assert "SET scan_pattern_id = c.envelope_scan_pattern_id" in src
    assert "phase5d_pattern_backfill_from_envelope" in src
    assert "source_trade_id=" in src
    assert "UPDATE trading_trades" not in src
    assert "ALTER TABLE trading_trades RENAME" not in src
