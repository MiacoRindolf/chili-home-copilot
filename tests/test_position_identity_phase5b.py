"""Position-identity Phase 5B -- read-only envelope semantics."""
from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from app import migrations
from app.services.trading.management_envelopes import (
    fetch_decision_envelopes,
    pattern_decision_performance,
    phase5b_parity_summary,
)


class _FakeMappings:
    def __init__(self, rows):
        self._rows = rows

    def all(self):
        return self._rows


class _FakeResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return _FakeMappings(self._rows)


def _db(rows):
    db = MagicMock()
    db.execute.return_value = _FakeResult(rows)
    return db


def test_phase5b_migration_registered_after_263():
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]
    assert "263_alpha_portfolio_gate" in ids
    assert "264_position_identity_phase5b_read_models" in ids
    assert "265_position_identity_phase5b_linkage_statuses" in ids
    assert ids.index("264_position_identity_phase5b_read_models") == (
        ids.index("263_alpha_portfolio_gate") + 1
    )
    assert ids.index("265_position_identity_phase5b_linkage_statuses") == (
        ids.index("264_position_identity_phase5b_read_models") + 1
    )


def test_phase5b_tca_quality_migration_registered_after_physical_rename():
    ids = [version_id for version_id, _fn in migrations.MIGRATIONS]
    assert "283_position_identity_phase5h_physical_rename" in ids
    assert "284_phase5b_tca_quality_filter" not in ids
    assert "287_phase5b_tca_quality_filter" in ids


def test_phase5b_migration_is_views_not_physical_rename():
    src = inspect.getsource(migrations._migration_264_position_identity_phase5b_read_models)
    assert "CREATE VIEW trading_management_envelopes AS" in src
    assert "SELECT * FROM trading_trades" in src
    assert "CREATE OR REPLACE VIEW trading_phase5b_decision_envelope_position" in src
    assert "CREATE OR REPLACE VIEW trading_phase5b_pattern_decision_performance" in src
    assert "ALTER TABLE trading_trades RENAME" not in src
    assert "RENAME TO trading_management_envelopes" not in src


def test_phase5b_tca_quality_migration_filters_unverified_extreme_costs():
    src = inspect.getsource(migrations._migration_287_phase5b_tca_quality_filter)
    assert "CREATE OR REPLACE VIEW trading_phase5b_decision_envelope_position" in src
    assert "envelope_broker_order_id" in src
    assert "envelope_broker_status" in src
    assert "envelope_avg_fill_price" in src
    assert "CREATE OR REPLACE VIEW trading_phase5b_pattern_decision_performance" in src
    assert "ABS(tca_entry_slippage_bps) <= 500.0" in src
    assert "ABS(tca_exit_slippage_bps) <= 500.0" in src
    assert "LOWER(COALESCE(envelope_broker_status, '')) IN ('filled', 'partially_filled')" in src


def test_phase5b_parity_summary_payload_and_ok():
    db = _db([{
        "valid_trades_missing_decision": 0,
        "open_broker_trades_missing_position": 0,
        "orphan_decisions": 0,
        "decisions_without_envelope": 0,
        "broker_envelopes_missing_position": 0,
        "open_position_envelope_mismatches": 0,
    }])
    summary = phase5b_parity_summary(db)
    assert summary.ok is True
    assert summary.to_payload()["ok"] is True
    sql = str(db.execute.call_args[0][0])
    assert "trading_phase5a_envelope_parity" in sql
    assert "trading_phase5b_decision_envelope_position" in sql


def test_fetch_decision_envelopes_filters_and_caps_limit():
    db = _db([])
    fetch_decision_envelopes(
        db,
        limit=5000,
        status="open",
        broker_source="Coinbase",
        ticker="thq-usd",
        only_linkage_issues=True,
    )
    sql = str(db.execute.call_args[0][0])
    params = db.execute.call_args[0][1]
    assert "FROM trading_phase5b_decision_envelope_position" in sql
    assert "envelope_status = :status" in sql
    assert "LOWER(COALESCE(broker_source, '')) = :broker_source" in sql
    assert "UPPER(COALESCE(envelope_ticker, decision_ticker)) = :ticker" in sql
    assert "linkage_status <> 'linked'" in sql
    assert params == {
        "limit": 1000,
        "status": "open",
        "broker_source": "coinbase",
        "ticker": "THQ-USD",
    }


def test_pattern_decision_performance_uses_phase5b_view():
    db = _db([])
    pattern_decision_performance(db, days=14, min_closed=2, limit=9)
    sql = str(db.execute.call_args[0][0])
    params = db.execute.call_args[0][1]
    assert "FROM trading_phase5b_decision_envelope_position" in sql
    assert "GROUP BY scan_pattern_id" in sql
    assert "HAVING COUNT(*) FILTER" in sql
    assert "historical_broker_envelope_missing_position" in sql
    assert "ABS(tca_entry_slippage_bps) <= :outlier_bps" in sql
    assert "ABS(tca_exit_slippage_bps) <= :outlier_bps" in sql
    assert "envelope_avg_fill_price" in sql
    assert "envelope_broker_order_id" in sql
    assert "envelope_broker_status" in sql
    assert "AVG(tca_entry_slippage_bps)" not in sql
    assert params == {"days": 14, "min_closed": 2, "limit": 9, "outlier_bps": 500.0}
