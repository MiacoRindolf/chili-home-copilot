"""Guardrails for the JSONB/schema repair migrations."""
from __future__ import annotations

import inspect

from app import migrations


def test_jsonb_contract_repair_migrations_are_registered_after_phase5a():
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]

    assert "258_position_identity_phase5a_residual_backfill" in ids
    assert "259_jsonb_contract_repair" in ids
    assert "260_operational_retention_time_indexes" in ids
    assert "261_jsonb_string_payload_unwrap" in ids
    assert "262_fast_orderbook_default_snapshot_id_retention" in ids
    assert ids.index("259_jsonb_contract_repair") == (
        ids.index("258_position_identity_phase5a_residual_backfill") + 1
    )
    assert ids.index("260_operational_retention_time_indexes") == (
        ids.index("259_jsonb_contract_repair") + 1
    )
    assert ids.index("261_jsonb_string_payload_unwrap") == (
        ids.index("260_operational_retention_time_indexes") + 1
    )
    assert ids.index("262_fast_orderbook_default_snapshot_id_retention") == (
        ids.index("261_jsonb_string_payload_unwrap") + 1
    )


def test_jsonb_contract_repair_preserves_legacy_text_and_wraps_bad_json():
    src = inspect.getsource(migrations._migration_259_jsonb_contract_repair)

    for table, column in [
        ("trading_trades", "indicator_snapshot"),
        ("trading_backtests", "params"),
        ("trading_proposals", "signals_json"),
        ("trading_proposals", "indicator_json"),
        ("trading_scans", "indicator_data"),
        ("trading_hypotheses", "last_result_json"),
    ]:
        assert table in src
        assert column in src

    assert "trading_jsonb_contract_repair_audit" in src
    assert "trading_jsonb_contract_legacy_text" in src
    assert "_legacy_text" in src
    assert "_jsonb_repair_259" in src
    assert "RENAME COLUMN" in src
    assert "sanitized_constant_rows" in src
    assert "NaN|-Infinity|Infinity" in src
    assert "chili_jsonb_or_legacy_object" in src
    assert "'_chili_legacy_text', raw" in src
    assert "ALTER COLUMN {column_ident} TYPE JSONB" in src


def test_operational_retention_index_migration_defers_heavy_tables():
    src = inspect.getsource(migrations._migration_260_operational_retention_time_indexes)

    assert "ix_exit_parity_created_retention" in src
    assert "ix_bracket_reconciliation_observed_retention" in src
    assert "ix_pattern_trades_created_retention" in src
    assert "CHILI_MIGRATION_BUILD_HEAVY_OPERATIONAL_INDEXES" in src
    assert "deferred heavy startup index" in src


def test_jsonb_string_payload_unwrap_handles_double_encoded_json():
    src = inspect.getsource(migrations._migration_261_jsonb_string_payload_unwrap)

    assert "trading_jsonb_string_unwrap_audit" in src
    assert "chili_jsonb_unwrap_string_payload" in src
    assert "jsonb_typeof(raw) <> 'string'" in src
    assert "LOCK TABLE trading_trades IN ACCESS EXCLUSIVE MODE" in src
    assert "chk_trades_entry_price_positive" in src


def test_fast_orderbook_default_exact_retention_index_defers_heavy_table():
    src = inspect.getsource(
        migrations._migration_262_fast_orderbook_default_snapshot_id_retention
    )

    assert "ix_fast_orderbook_default_snapshot_id_retention" in src
    assert "snapshot_at, id" in src
    assert "CHILI_MIGRATION_BUILD_HEAVY_FAST_DEFAULT_INDEXES" in src
    assert "deferred heavy startup index" in src
