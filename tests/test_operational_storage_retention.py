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
