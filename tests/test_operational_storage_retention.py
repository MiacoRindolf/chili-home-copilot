"""Retention guardrails for trading operational logs."""
from __future__ import annotations

from pathlib import Path

from app.config import Settings


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


def test_exit_parity_retention_settings_defaults():
    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.brain_retention_exit_parity_backtest_days == 7
    assert s.brain_retention_exit_parity_live_days == 30
    assert s.brain_retention_exit_parity_delete_batch_size == 50_000


def test_retention_sweep_includes_exit_parity_log():
    src = _read("app/services/trading/data_retention.py")

    assert "_prune_exit_parity_log" in src
    assert "trading_exit_parity_log" in src
    assert "brain_retention_exit_parity_backtest_days" in src
    assert "brain_retention_exit_parity_live_days" in src
    assert "LIMIT :limit" in src
    assert "_retention_has_leading_time_index" in src


def test_storage_maintenance_script_uses_concurrent_indexes_and_dry_run_default():
    src = _read("scripts/maintain_trading_storage.py")

    assert "CREATE INDEX CONCURRENTLY IF NOT EXISTS" in src
    assert "ix_exit_parity_created_retention" in src
    assert "ix_fast_orderbook_default_snapshot_id_retention" in src
    assert "INDEX_TARGET_TABLES" in src
    assert "trading_exit_parity_log" in src
    assert "fast_orderbook_default" in src
    assert "action=\"store_true\", help=\"mutate the database\"" in src
