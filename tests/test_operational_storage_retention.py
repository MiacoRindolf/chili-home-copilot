"""Retention guardrails for trading operational logs."""
from __future__ import annotations

from pathlib import Path

from app.config import (
    BRAIN_EXIT_ENGINE_BACKTEST_CLOSE_AGREEMENT_SAMPLE_PCT_DEFAULT,
    BRAIN_EXIT_ENGINE_BACKTEST_INTERESTING_DRIFT_BPS_DEFAULT,
    BRAIN_EXIT_ENGINE_BACKTEST_OPS_LOG_DEFAULT_ENABLED,
    BRAIN_EXIT_ENGINE_BACKTEST_PARITY_DEFAULT_SAMPLE_PCT,
    BRAIN_EXIT_ENGINE_PARITY_DEFAULT_SAMPLE_PCT,
    Settings,
)


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


def test_exit_parity_retention_settings_defaults():
    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.brain_exit_engine_parity_sample_pct == (
        BRAIN_EXIT_ENGINE_PARITY_DEFAULT_SAMPLE_PCT
    )
    assert s.brain_exit_engine_backtest_parity_sample_pct == (
        BRAIN_EXIT_ENGINE_BACKTEST_PARITY_DEFAULT_SAMPLE_PCT
    )
    assert s.brain_exit_engine_backtest_close_agreement_sample_pct == (
        BRAIN_EXIT_ENGINE_BACKTEST_CLOSE_AGREEMENT_SAMPLE_PCT_DEFAULT
    )
    assert s.brain_exit_engine_backtest_interesting_drift_bps == (
        BRAIN_EXIT_ENGINE_BACKTEST_INTERESTING_DRIFT_BPS_DEFAULT
    )
    assert s.brain_exit_engine_backtest_ops_log_enabled is (
        BRAIN_EXIT_ENGINE_BACKTEST_OPS_LOG_DEFAULT_ENABLED
    )
    assert s.brain_retention_exit_parity_backtest_days == 7
    assert s.brain_retention_exit_parity_live_days == 30
    assert s.brain_retention_exit_parity_delete_batch_size == 50_000
    assert s.brain_retention_exit_parity_max_rows_per_sweep == 5_000_000
    assert s.brain_retention_bracket_reconciliation_days == 30
    assert s.brain_retention_execution_event_days == 180


def test_backtest_exit_parity_settings_allow_zero_override(monkeypatch):
    monkeypatch.setenv("BRAIN_EXIT_ENGINE_BACKTEST_PARITY_SAMPLE_PCT", "0")
    monkeypatch.setenv("BRAIN_EXIT_ENGINE_BACKTEST_CLOSE_AGREEMENT_SAMPLE_PCT", "0")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.brain_exit_engine_backtest_parity_sample_pct == 0.0
    assert s.brain_exit_engine_backtest_close_agreement_sample_pct == 0.0


def test_retention_sweep_includes_operational_logs():
    src = _read("app/services/trading/data_retention.py")

    assert "_prune_exit_parity_log" in src
    assert "_prune_operational_time_log" in src
    assert "trading_exit_parity_log" in src
    assert "trading_bracket_reconciliation_log" in src
    assert "trading_execution_events" in src
    assert "trading_pattern_trades" in src
    assert "brain_retention_exit_parity_backtest_days" in src
    assert "brain_retention_exit_parity_live_days" in src
    assert "brain_retention_bracket_reconciliation_days" in src
    assert "brain_retention_execution_event_days" in src
    assert "brain_retention_pattern_trade_days" in src
    assert "LIMIT :limit" in src
    assert "_retention_has_leading_time_index" in src


def test_storage_maintenance_script_uses_concurrent_indexes_and_dry_run_default():
    src = _read("scripts/maintain_trading_storage.py")

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in src
    assert "ix_exit_parity_created_retention" in src
    assert "ix_exit_parity_pattern_created" in src
    assert "ix_bracket_reconciliation_observed_retention" in src
    assert "ix_pattern_trades_created_retention" in src
    assert "ix_execution_events_recorded_retention" in src
    assert "ix_fast_orderbook_default_snapshot_id_retention" in src
    assert "INDEX_TARGET_TABLES" in src
    assert "trading_exit_parity_log" in src
    assert "fast_orderbook_default" in src
    assert "action=\"store_true\", help=\"mutate the database\"" in src


def test_exit_parity_model_index_set_matches_post_migration_301_schema():
    """The ORM model must not redeclare the indexes mig 301 dropped.

    ``_migration_301_exit_parity_log_index_prune_and_autovacuum`` dropped 6
    zero-scan indexes from ``trading_exit_parity_log`` on prod. If the model
    still declared them, a fresh-DB ``create_all()`` would recreate exactly the
    indexes mig 301 only drops again -- create+drop churn, and the
    create_all-built test schema would drift from prod. This guards against
    re-introducing them.
    """
    from app.models.trading import ExitParityLog

    declared = {ix.name for ix in ExitParityLog.__table__.indexes}

    # Dropped by mig 301 -- must NOT be redeclared on the model.
    dropped_by_mig_301 = {
        "ix_exit_parity_source_created",
        "ix_exit_parity_ticker_created",
        "ix_exit_parity_agree_created",
        "ix_exit_parity_strict_agree_created",
        "ix_exit_parity_action_class_created",
        "ix_exit_parity_priority_winner_created",
    }
    assert declared.isdisjoint(dropped_by_mig_301), (
        "ExitParityLog still declares indexes mig 301 dropped: "
        f"{sorted(declared & dropped_by_mig_301)}"
    )

    # Kept by mig 301 (real read paths use these) -- must remain declared.
    kept_by_mig_301 = {
        "ix_exit_parity_created_retention",
        "ix_exit_parity_mode_created",
        "ix_exit_parity_pattern_created",
    }
    assert kept_by_mig_301.issubset(declared), (
        "ExitParityLog dropped an index mig 301 kept: "
        f"{sorted(kept_by_mig_301 - declared)}"
    )

    # SQLAlchemy still needs a mapped PK even though mig 301 dropped the physical
    # ``trading_exit_parity_log_pkey`` constraint (intentional model<->DB drift).
    assert [c.name for c in ExitParityLog.__table__.primary_key.columns] == ["id"]


def test_exit_parity_prune_drains_in_committed_loop():
    """Regression guard: the exit-parity prune must drain ALL eligible rows in
    a per-batch-committed loop, not one batch per daily sweep (the bug that let
    the table grow to dominate the DB). The per-batch commit also bounds the
    transaction so the sweep cannot become an idle-in-transaction holder.
    """
    src = _read("app/services/trading/data_retention.py")

    assert "while deleted_total < resolved_cap:" in src
    assert "db.commit()" in src
    assert "max_rows_per_sweep" in src
    assert "DEFAULT_EXIT_PARITY_MAX_ROWS_PER_SWEEP" in src
    assert "brain_retention_exit_parity_max_rows_per_sweep" in src


def test_exit_parity_max_rows_per_sweep_env_override(monkeypatch):
    monkeypatch.setenv("BRAIN_RETENTION_EXIT_PARITY_MAX_ROWS_PER_SWEEP", "250000")

    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.brain_retention_exit_parity_max_rows_per_sweep == 250_000


def test_migration_301_drops_zero_scan_indexes_and_pins_autovacuum():
    """Migration 301 reclaims ~10 GB of zero-scan indexes on the parity table
    and pins absolute-threshold autovacuum. Keeps the 3 indexes real code uses.
    """
    src = _read("app/migrations.py")

    assert "_migration_301_exit_parity_log_index_prune_and_autovacuum" in src
    assert "301_exit_parity_log_index_prune_and_autovacuum" in src  # registered

    # The 6 secondary zero-scan indexes are dropped via a loop over a name
    # tuple + an f-string template, so assert both pieces.
    assert "DROP INDEX IF EXISTS {idx}" in src
    for idx in (
        "ix_exit_parity_source_created",
        "ix_exit_parity_action_class_created",
        "ix_exit_parity_ticker_created",
        "ix_exit_parity_strict_agree_created",
        "ix_exit_parity_priority_winner_created",
        "ix_exit_parity_agree_created",
    ):
        assert f'"{idx}"' in src

    # The pkey is dropped via its constraint (also drops its backing index).
    assert "DROP CONSTRAINT IF EXISTS trading_exit_parity_log_pkey" in src

    # Per-table autovacuum pinned to an absolute dead-tuple threshold.
    assert "autovacuum_vacuum_scale_factor = 0" in src
    assert "autovacuum_vacuum_threshold = 50000" in src
    assert "autovacuum_vacuum_cost_delay = 0" in src


def test_migration_357_dedupes_viability_indexes_and_pins_autovacuum():
    """Migration 357 drops the 3 duplicate momentum_symbol_viability indexes
    (pkey copy + the ix_msvi_* twins of the model-named indexes) and pins
    absolute-threshold autovacuum including the TOAST relation (the 5.8 GB
    was TOAST bloat from JSONB update churn).
    """
    src = _read("app/migrations.py")

    assert "_migration_357_momentum_viability_index_dedupe_and_autovacuum" in src
    assert "357_momentum_viability_index_dedupe_and_autovacuum" in src  # registered

    # The pkey duplicate is dropped unconditionally.
    assert "DROP INDEX IF EXISTS ix_momentum_symbol_viability_id" in src

    # The migration-named twins are dropped only when the model-named copy
    # exists (prod-shaped fresh installs only ever get the ix_msvi_* copy,
    # which must survive as the sole index on its column).
    assert '"ix_msvi_corr", "ix_momentum_symbol_viability_correlation_id"' in src
    assert '"ix_msvi_freshness", "ix_momentum_symbol_viability_freshness_ts"' in src

    # Absolute-threshold autovacuum incl. TOAST.
    assert "autovacuum_vacuum_threshold = 10000" in src
    assert "toast.autovacuum_vacuum_threshold = 10000" in src

    # The ORM no longer declares the pkey-duplicating index on id.
    model_src = _read("app/models/trading.py")
    marker = 'tablename__ = "momentum_symbol_viability"'
    section = model_src[model_src.index(marker):model_src.index(marker) + 800]
    assert "id: int = Column(Integer, primary_key=True)\n" in section


def test_retention_sweep_includes_viability_snapshot_slim():
    src = _read("app/services/trading/data_retention.py")

    assert "_slim_stale_viability_snapshots" in src
    assert "brain_retention_viability_snapshot_days" in src
    # Slim, not delete: rows are kept; the four JSONB columns reset to '{}'.
    assert "regime_snapshot_json = '{{}}'::jsonb" in src
    assert "evidence_window_json = '{{}}'::jsonb" in src
    assert "DEFAULT_VIABILITY_SNAPSHOT_SLIM_BATCH_SIZE" in src
    assert "DEFAULT_VIABILITY_SNAPSHOT_MAX_ROWS_PER_SWEEP" in src

    s = Settings(_env_file=None)  # type: ignore[call-arg]
    assert s.brain_retention_viability_snapshot_days == 30


def test_viability_snapshot_slim_only_touches_stale_rows(db):
    """DB-backed: stale rows get their JSONB slimmed to {}, fresh rows and all
    scalar columns are untouched, and a second sweep finds nothing (convergent).
    """
    from datetime import datetime, timedelta

    from app.models.trading import MomentumStrategyVariant, MomentumSymbolViability
    from app.services.trading.data_retention import _slim_stale_viability_snapshots

    variant = MomentumStrategyVariant(
        family="test_family", variant_key="slim_test", version=1, label="slim test",
    )
    db.add(variant)
    db.flush()

    now = datetime.utcnow()
    payload = {"k": "v", "n": 1}
    stale = MomentumSymbolViability(
        symbol="STALE", variant_id=variant.id, viability_score=1.0,
        freshness_ts=now - timedelta(days=45),
        regime_snapshot_json=dict(payload),
        execution_readiness_json=dict(payload),
        explain_json=dict(payload),
        evidence_window_json=dict(payload),
    )
    fresh = MomentumSymbolViability(
        symbol="FRESH", variant_id=variant.id, viability_score=2.0,
        freshness_ts=now - timedelta(days=1),
        regime_snapshot_json=dict(payload),
        execution_readiness_json=dict(payload),
        explain_json=dict(payload),
        evidence_window_json=dict(payload),
    )
    db.add_all([stale, fresh])
    db.commit()

    dry = _slim_stale_viability_snapshots(db, retain_days=30, dry_run=True)
    assert dry["eligible_batch"] == 1
    assert dry["slimmed"] == 0

    result = _slim_stale_viability_snapshots(db, retain_days=30, dry_run=False)
    assert result["slimmed"] == 1
    assert result.get("error") is None

    db.expire_all()
    stale_row = db.query(MomentumSymbolViability).filter_by(symbol="STALE").one()
    fresh_row = db.query(MomentumSymbolViability).filter_by(symbol="FRESH").one()
    assert stale_row.regime_snapshot_json == {}
    assert stale_row.execution_readiness_json == {}
    assert stale_row.explain_json == {}
    assert stale_row.evidence_window_json == {}
    # Scalars survive: the row still answers existence/score/eligibility reads.
    assert stale_row.viability_score == 1.0
    assert stale_row.freshness_ts == now - timedelta(days=45)
    # Fresh row untouched.
    assert fresh_row.regime_snapshot_json == payload

    again = _slim_stale_viability_snapshots(db, retain_days=30, dry_run=False)
    assert again["slimmed"] == 0
