"""Retention guardrails for fast-path high-volume tables."""
from __future__ import annotations

from pathlib import Path

from app.config import Settings


REPO = Path(__file__).parent.parent


def _read(rel: str) -> str:
    return (REPO / rel).read_text(encoding="utf-8", errors="ignore")


def test_fast_path_retention_settings_defaults():
    s = Settings(_env_file=None)  # type: ignore[call-arg]

    assert s.brain_retention_fast_orderbook_days == 3
    assert s.brain_retention_fast_snapshot_days == 30
    assert s.brain_retention_fast_alert_days == 14
    assert s.brain_retention_fast_execution_days == 30
    assert s.brain_retention_fast_exit_days == 90
    assert s.brain_retention_fast_delete_batch_size == 50_000


def test_retention_sweep_includes_fast_path_tables():
    src = _read("app/services/trading/data_retention.py")

    for table in [
        "fast_orderbook",
        "fast_snapshots",
        "fast_alerts",
        "fast_executions",
        "fast_exits",
    ]:
        assert table in src

    assert "_prune_fast_path_tables" in src
    assert "_fast_retention_has_leading_time_index" in src
    assert "brain_retention_fast_delete_batch_size" in src
    assert "LIMIT :limit" in src


def test_fast_path_retention_migration_adds_timestamp_leading_indexes():
    src = _read("app/migrations.py")

    assert "255_fast_path_retention_time_indexes" in src
    for index_name, column in [
        ("ix_fast_snapshots_bar_close_retention", "bar_close_at"),
        ("ix_fast_orderbook_snapshot_retention", "snapshot_at"),
        ("ix_fast_alerts_fired_retention", "fired_at"),
        ("ix_fast_executions_decided_retention", "decided_at"),
        ("ix_fast_exits_exited_retention", "exited_at"),
    ]:
        assert index_name in src
        assert f"ON {{table}} ({{ts_col}}, id)" in src
        assert column in src
