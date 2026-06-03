import inspect
from pathlib import Path

from app import migrations
from app.models.trading import (
    BracketReconciliationLog,
    ExitParityLog,
    LedgerParityLog,
    PositionSizerLog,
    VenueTruthLog,
)
from app.services.trading import divergence_service


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


def _index_names(model) -> set[str]:
    return {idx.name for idx in model.__table__.indexes}


def test_discover_active_patterns_uses_source_driven_time_window_query():
    src = inspect.getsource(divergence_service.discover_active_patterns)

    assert "WITH source_patterns AS" in src
    assert "distinct_patterns AS" in src
    assert src.count("UNION ALL") == 4
    assert "SELECT DISTINCT scan_pattern_id" in src
    assert "SELECT dp.scan_pattern_id" in src
    assert "JOIN scan_patterns sp" in src
    assert "WHERE EXISTS" not in src
    assert "FROM trading_ledger_parity_log" in src
    assert "FROM trading_exit_parity_log" in src
    assert "JOIN trading_trades t ON t.id = v.trade_id" in src
    assert "FROM trading_bracket_reconciliation_log br" in src
    assert "FROM trading_position_sizer_log" in src
    assert "ORDER BY dp.scan_pattern_id" in src


def test_divergence_probe_indexes_registered_in_models():
    assert "ix_ledger_parity_created_pattern" in _index_names(LedgerParityLog)
    assert "ix_exit_parity_created_pattern" in _index_names(ExitParityLog)
    assert "ix_venue_truth_log_created_trade" in _index_names(VenueTruthLog)
    assert (
        "ix_bracket_reconciliation_observed_trade"
        in _index_names(BracketReconciliationLog)
    )
    assert "ix_position_sizer_log_observed_pattern" in _index_names(PositionSizerLog)


def test_divergence_probe_index_migration_registered_and_deferred():
    migrations._assert_migration_ids_unique()
    ids = [version_id for version_id, _ in migrations.MIGRATIONS]

    assert "296_divergence_discovery_probe_indexes" in ids
    assert ids.index("296_divergence_discovery_probe_indexes") > ids.index(
        "295_pattern_regime_perf_pattern_asof_cover_index"
    )

    src = inspect.getsource(migrations._migration_296_divergence_discovery_probe_indexes)

    for index_name in [
        "ix_ledger_parity_created_pattern",
        "ix_exit_parity_created_pattern",
        "ix_venue_truth_log_created_trade",
        "ix_bracket_reconciliation_observed_trade",
        "ix_position_sizer_log_observed_pattern",
    ]:
        assert index_name in src

    assert "CHILI_MIGRATION_BUILD_HEAVY_DIVERGENCE_INDEXES" in src
    assert "deferred heavy divergence index" in src
    assert "--target divergence-discovery --create-indexes" in src


def test_storage_maintenance_can_build_divergence_indexes_concurrently():
    src = _read("scripts/maintain_trading_storage.py")

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in src
    assert '"divergence-discovery"' in src
    assert '"all"' in src
    assert "ix_ledger_parity_created_pattern" in src
    assert "ix_exit_parity_created_pattern" in src
    assert "ix_venue_truth_log_created_trade" in src
    assert "ix_bracket_reconciliation_observed_trade" in src
    assert "ix_position_sizer_log_observed_pattern" in src
