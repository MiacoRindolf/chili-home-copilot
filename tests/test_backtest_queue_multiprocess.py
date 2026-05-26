"""Unit tests for process-pool queue backtest wiring (env + worker caps)."""
from __future__ import annotations

import os
from types import SimpleNamespace


def test_configure_multiprocess_child_db_env_sets_flag_and_pool(monkeypatch):
    monkeypatch.delenv("CHILI_MP_BACKTEST_CHILD", raising=False)
    monkeypatch.delenv("DATABASE_POOL_SIZE", raising=False)
    monkeypatch.delenv("DATABASE_MAX_OVERFLOW", raising=False)

    from app.services.trading.backtest_queue_worker import (
        CHILD_ENV_FLAG,
        configure_multiprocess_child_db_env,
    )

    configure_multiprocess_child_db_env(1, 2)
    assert os.environ.get(CHILD_ENV_FLAG) == "1"
    assert os.environ.get("DATABASE_POOL_SIZE") == "1"
    assert os.environ.get("DATABASE_MAX_OVERFLOW") == "2"


def test_bt_workers_uses_cap_in_mp_child(monkeypatch):
    monkeypatch.setenv("CHILI_MP_BACKTEST_CHILD", "1")
    from app.config import settings
    from app.services.trading.backtest_engine import _bt_workers

    w = _bt_workers()
    assert w == max(2, int(settings.brain_smart_bt_max_workers_in_process))


def test_process_memory_guard_caps_process_workers():
    from app.config import (
        BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_MIN_WORKERS,
        BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_RESERVE_MB,
        BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_WORKER_MB,
    )
    from app.services.trading.learning import (
        BYTES_PER_MIB,
        _process_memory_guard_worker_cap,
    )

    cfg = SimpleNamespace(
        brain_queue_process_memory_guard_enabled=True,
        brain_queue_process_memory_guard_reserve_mb=(
            BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_RESERVE_MB
        ),
        brain_queue_process_memory_guard_worker_mb=(
            BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_WORKER_MB
        ),
        brain_queue_process_memory_guard_min_workers=(
            BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_MIN_WORKERS
        ),
    )

    cap = _process_memory_guard_worker_cap(
        cfg,
        memory_limit_bytes=5 * 1024 * BYTES_PER_MIB,
    )

    expected = (
        (5 * 1024 - BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_RESERVE_MB)
        // BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_WORKER_MB
    )
    assert cap == expected


def test_process_memory_guard_can_be_disabled():
    from app.config import (
        BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_MIN_WORKERS,
        BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_RESERVE_MB,
        BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_WORKER_MB,
    )
    from app.services.trading.learning import (
        BYTES_PER_MIB,
        _process_memory_guard_worker_cap,
    )

    cfg = SimpleNamespace(
        brain_queue_process_memory_guard_enabled=False,
        brain_queue_process_memory_guard_reserve_mb=(
            BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_RESERVE_MB
        ),
        brain_queue_process_memory_guard_worker_mb=(
            BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_WORKER_MB
        ),
        brain_queue_process_memory_guard_min_workers=(
            BRAIN_QUEUE_PROCESS_MEMORY_GUARD_DEFAULT_MIN_WORKERS
        ),
    )

    assert _process_memory_guard_worker_cap(
        cfg,
        memory_limit_bytes=5 * 1024 * BYTES_PER_MIB,
    ) is None


def test_settings_normalizes_queue_executor(monkeypatch):
    monkeypatch.setenv("BRAIN_QUEUE_BACKTEST_EXECUTOR", "mp")
    from app.config import Settings

    s = Settings()
    assert s.brain_queue_backtest_executor == "process"


def test_operational_queue_target_caps_promoted_lifecycle():
    from app.services.trading.backtest_queue_worker import (
        queue_target_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=60,
        brain_queue_operational_refresh_enabled=True,
        brain_queue_operational_refresh_lifecycles="promoted,live,shadow_promoted,pilot_promoted",
        brain_queue_operational_target_tickers=24,
    )
    pattern = SimpleNamespace(lifecycle_stage="promoted")

    assert queue_target_tickers_for_pattern(cfg, pattern) == 24


def test_operational_queue_target_keeps_generic_backlog_full_budget():
    from app.services.trading.backtest_queue_worker import (
        queue_target_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=60,
        brain_queue_operational_refresh_enabled=True,
        brain_queue_operational_refresh_lifecycles="promoted,live,shadow_promoted,pilot_promoted",
        brain_queue_operational_target_tickers=24,
    )
    pattern = SimpleNamespace(lifecycle_stage="challenged")

    assert queue_target_tickers_for_pattern(cfg, pattern) == 60


def test_operational_stored_refresh_cap_is_separate_from_full_refresh_cap():
    from app.services.trading.backtest_queue_worker import (
        queue_stored_refresh_max_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=60,
        brain_queue_stored_refresh_max_tickers=40,
        brain_queue_operational_refresh_enabled=True,
        brain_queue_operational_refresh_lifecycles="promoted,live,shadow_promoted,pilot_promoted",
        brain_queue_operational_target_tickers=24,
        brain_queue_operational_stored_refresh_max_tickers=18,
    )
    pattern = SimpleNamespace(lifecycle_stage="shadow_promoted")

    assert queue_stored_refresh_max_tickers_for_pattern(cfg, pattern) == 18


def test_operational_queue_budget_can_be_disabled():
    from app.services.trading.backtest_queue_worker import (
        queue_target_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=60,
        brain_queue_operational_refresh_enabled=False,
        brain_queue_operational_refresh_lifecycles="promoted",
        brain_queue_operational_target_tickers=24,
    )
    pattern = SimpleNamespace(lifecycle_stage="promoted")

    assert queue_target_tickers_for_pattern(cfg, pattern) == 60
