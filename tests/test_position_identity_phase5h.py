"""Position-identity Phase 5H physical rename migration."""
from __future__ import annotations

import inspect

from app import migrations


def test_phase5h_migration_registered_after_282():
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]
    assert "282_autotrader_imminent_selector_indexes" in ids
    assert "283_position_identity_phase5h_physical_rename" in ids
    assert ids.index("283_position_identity_phase5h_physical_rename") == (
        ids.index("282_autotrader_imminent_selector_indexes") + 1
    )


def test_phase5h_migration_is_compatibility_rename_only():
    src = inspect.getsource(migrations._migration_283_position_identity_phase5h_physical_rename)
    assert "DROP VIEW IF EXISTS trading_management_envelopes" in src
    assert "ALTER TABLE trading_trades RENAME TO trading_management_envelopes" in src
    assert "CREATE VIEW trading_trades AS" in src
    assert "SELECT * FROM trading_management_envelopes" in src
    assert "DROP COLUMN" not in src
    assert "DROP TABLE" not in src
    assert "DROP COLUMN trade_id" not in src
    assert "DROP COLUMN source_trade_id" not in src


def test_phase5h_migration_is_idempotent_for_renamed_shape():
    src = inspect.getsource(migrations._migration_283_position_identity_phase5h_physical_rename)
    assert 'trades_kind == "v" and envelopes_kind == "r"' in src
    assert "physical envelope rename already installed" in src
