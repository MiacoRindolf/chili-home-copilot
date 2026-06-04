"""Unit tests for process-pool queue backtest wiring (env + worker caps)."""
from __future__ import annotations

import os
import signal
import time
from types import SimpleNamespace

import pytest

from app.config import Settings

_TEST_PATTERN_ID = 123
_TEST_USER_ID = 7
_TEST_BACKTESTS_RUN = 5
_TEST_PATTERNS_PROCESSED = 1
_TEST_WALLTIME_SECONDS = 0.05
_TEST_SLEEP_SECONDS = 0.2


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


def test_queue_pattern_walltime_uses_env_override(monkeypatch):
    from app.services.trading.backtest_queue_worker import (
        QUEUE_PATTERN_WALLTIME_SECONDS_ENV,
        queue_pattern_walltime_seconds,
    )

    monkeypatch.setenv(QUEUE_PATTERN_WALLTIME_SECONDS_ENV, str(_TEST_WALLTIME_SECONDS))

    assert queue_pattern_walltime_seconds() == _TEST_WALLTIME_SECONDS


def test_queue_pattern_walltime_allows_explicit_zero_override(monkeypatch):
    from app.services.trading.backtest_queue_worker import (
        DISABLED_QUEUE_PATTERN_WALLTIME_SECONDS,
        QUEUE_PATTERN_WALLTIME_SECONDS_ENV,
        queue_pattern_walltime_seconds,
    )

    monkeypatch.setenv(
        QUEUE_PATTERN_WALLTIME_SECONDS_ENV,
        str(DISABLED_QUEUE_PATTERN_WALLTIME_SECONDS),
    )

    assert queue_pattern_walltime_seconds() == DISABLED_QUEUE_PATTERN_WALLTIME_SECONDS


def test_queue_pattern_soft_runtime_uses_fraction_env(monkeypatch):
    from app.services.trading.backtest_queue_worker import (
        QUEUE_PATTERN_SOFT_DEADLINE_FRACTION_ENV,
        QUEUE_PATTERN_WALLTIME_SECONDS_ENV,
        queue_pattern_soft_runtime_seconds,
    )

    monkeypatch.setenv(QUEUE_PATTERN_WALLTIME_SECONDS_ENV, "100")
    monkeypatch.setenv(QUEUE_PATTERN_SOFT_DEADLINE_FRACTION_ENV, "0.25")

    assert queue_pattern_soft_runtime_seconds() == 25.0


def test_queue_pattern_soft_runtime_disabled_when_walltime_zero(monkeypatch):
    from app.services.trading.backtest_queue_worker import (
        QUEUE_PATTERN_WALLTIME_SECONDS_ENV,
        queue_pattern_soft_runtime_seconds,
    )

    monkeypatch.setenv(QUEUE_PATTERN_WALLTIME_SECONDS_ENV, "0")

    assert queue_pattern_soft_runtime_seconds() is None


def test_queue_pattern_walltime_settings_drive_soft_budget(monkeypatch):
    from app.services.trading.backtest_queue_worker import (
        QUEUE_PATTERN_SOFT_DEADLINE_FRACTION_ENV,
        QUEUE_PATTERN_WALLTIME_SECONDS_ENV,
        queue_pattern_soft_runtime_seconds,
        queue_pattern_walltime_seconds,
    )

    monkeypatch.delenv(QUEUE_PATTERN_WALLTIME_SECONDS_ENV, raising=False)
    monkeypatch.delenv(QUEUE_PATTERN_SOFT_DEADLINE_FRACTION_ENV, raising=False)
    monkeypatch.setenv("CHILI_BACKTEST_QUEUE_PATTERN_WALLTIME_SECONDS", "120")
    monkeypatch.setenv("CHILI_BACKTEST_QUEUE_PATTERN_SOFT_DEADLINE_FRACTION", "0.5")
    settings = Settings(_env_file=None)  # type: ignore[call-arg]

    assert settings.brain_queue_pattern_walltime_seconds == pytest.approx(120.0)
    assert settings.brain_queue_pattern_soft_deadline_fraction == pytest.approx(0.5)
    assert queue_pattern_walltime_seconds(settings_obj=settings, environ={}) == (
        pytest.approx(120.0)
    )
    assert queue_pattern_soft_runtime_seconds(settings_obj=settings, environ={}) == (
        pytest.approx(60.0)
    )

    assert queue_pattern_walltime_seconds(
        settings_obj=settings,
        environ={QUEUE_PATTERN_WALLTIME_SECONDS_ENV: "20"},
    ) == pytest.approx(20.0)


def test_partial_soft_deadline_result_cannot_certify_recert_or_promotion():
    from app.services.trading.backtest_queue_worker import (
        queue_backtest_can_certify_result,
    )

    assert queue_backtest_can_certify_result(
        {
            "soft_deadline_hit": True,
            "backtests_run": 9,
            "tickers_selected": 24,
        }
    ) is False


def test_complete_soft_deadline_result_can_certify_when_all_selected_attempted():
    from app.services.trading.backtest_queue_worker import (
        queue_backtest_can_certify_result,
    )

    assert queue_backtest_can_certify_result(
        {
            "soft_deadline_hit": True,
            "backtests_run": 24,
            "tickers_selected": 24,
        }
    ) is True


def test_non_deadline_result_can_certify():
    from app.services.trading.backtest_queue_worker import (
        queue_backtest_can_certify_result,
    )

    assert queue_backtest_can_certify_result(
        {
            "soft_deadline_hit": False,
            "backtests_run": 12,
            "tickers_selected": 24,
        }
    ) is True


def test_incomplete_ticker_attempts_cannot_certify_without_deadline_flag():
    from app.services.trading.backtest_queue_worker import (
        queue_backtest_can_certify_result,
    )

    assert queue_backtest_can_certify_result(
        {
            "soft_deadline_hit": False,
            "backtests_run": 12,
            "tickers_selected": 24,
            "complete_ticker_attempts": False,
        }
    ) is False


def test_run_one_pattern_job_keeps_db_child_env_and_delegates(monkeypatch):
    from app.services.trading import backtest_queue_worker as worker

    monkeypatch.delenv("DATABASE_POOL_SIZE", raising=False)
    monkeypatch.delenv("DATABASE_MAX_OVERFLOW", raising=False)
    monkeypatch.setenv(
        worker.QUEUE_PATTERN_WALLTIME_SECONDS_ENV,
        str(worker.DISABLED_QUEUE_PATTERN_WALLTIME_SECONDS),
    )
    monkeypatch.setattr(
        worker,
        "execute_queue_backtest_for_pattern",
        lambda pattern_id, user_id: (_TEST_BACKTESTS_RUN, _TEST_PATTERNS_PROCESSED),
    )

    assert worker.run_one_pattern_job(_TEST_PATTERN_ID, _TEST_USER_ID) == (
        _TEST_BACKTESTS_RUN,
        _TEST_PATTERNS_PROCESSED,
    )
    assert os.environ.get("DATABASE_POOL_SIZE") == "1"
    assert os.environ.get("DATABASE_MAX_OVERFLOW") == "2"


@pytest.mark.skipif(
    not hasattr(signal, "SIGALRM") or not hasattr(signal, "setitimer"),
    reason="SIGALRM walltime guard is Unix-only",
)
def test_queue_pattern_walltime_guard_interrupts_runaway_child():
    from app.services.trading.backtest_queue_worker import (
        QueuePatternWalltimeExceeded,
        _queue_pattern_walltime_guard,
    )

    with pytest.raises(QueuePatternWalltimeExceeded):
        with _queue_pattern_walltime_guard(_TEST_PATTERN_ID, _TEST_WALLTIME_SECONDS):
            time.sleep(_TEST_SLEEP_SECONDS)


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
        brain_queue_intraday_timeframes="1m,5m,15m",
        brain_queue_intraday_target_tickers=24,
        brain_queue_operational_refresh_enabled=True,
        brain_queue_operational_refresh_lifecycles="promoted,live,shadow_promoted,pilot_promoted",
        brain_queue_operational_target_tickers=24,
    )
    pattern = SimpleNamespace(lifecycle_stage="promoted", timeframe="1d")

    assert queue_target_tickers_for_pattern(cfg, pattern) == 24


def test_intraday_queue_target_caps_expensive_full_patterns():
    from app.services.trading.backtest_queue_worker import (
        queue_target_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=60,
        brain_queue_intraday_timeframes="1m,5m,15m",
        brain_queue_intraday_target_tickers=24,
        brain_queue_operational_refresh_enabled=True,
        brain_queue_operational_refresh_lifecycles="promoted,live,shadow_promoted,pilot_promoted",
        brain_queue_operational_target_tickers=18,
    )
    pattern = SimpleNamespace(lifecycle_stage="candidate", timeframe="5m")

    assert queue_target_tickers_for_pattern(cfg, pattern) == 24


def test_intraday_queue_target_does_not_raise_lower_full_budget():
    from app.services.trading.backtest_queue_worker import (
        queue_target_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=12,
        brain_queue_intraday_timeframes="1m,5m,15m",
        brain_queue_intraday_target_tickers=24,
        brain_queue_operational_refresh_enabled=True,
        brain_queue_operational_refresh_lifecycles="promoted,live,shadow_promoted,pilot_promoted",
        brain_queue_operational_target_tickers=18,
    )
    pattern = SimpleNamespace(lifecycle_stage="candidate", timeframe="1m")

    assert queue_target_tickers_for_pattern(cfg, pattern) == 12


def test_daily_queue_target_keeps_full_budget():
    from app.services.trading.backtest_queue_worker import (
        queue_target_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=60,
        brain_queue_intraday_timeframes="1m,5m,15m",
        brain_queue_intraday_target_tickers=24,
        brain_queue_operational_refresh_enabled=True,
        brain_queue_operational_refresh_lifecycles="promoted,live,shadow_promoted,pilot_promoted",
        brain_queue_operational_target_tickers=18,
    )
    pattern = SimpleNamespace(lifecycle_stage="candidate", timeframe="1d")

    assert queue_target_tickers_for_pattern(cfg, pattern) == 60


def test_operational_queue_target_keeps_generic_backlog_full_budget():
    from app.services.trading.backtest_queue_worker import (
        queue_target_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=60,
        brain_queue_intraday_timeframes="1m,5m,15m",
        brain_queue_intraday_target_tickers=24,
        brain_queue_operational_refresh_enabled=True,
        brain_queue_operational_refresh_lifecycles="promoted,live,shadow_promoted,pilot_promoted",
        brain_queue_operational_target_tickers=24,
    )
    pattern = SimpleNamespace(lifecycle_stage="challenged", timeframe="1d")

    assert queue_target_tickers_for_pattern(cfg, pattern) == 60


def test_operational_stored_refresh_cap_is_separate_from_full_refresh_cap():
    from app.services.trading.backtest_queue_worker import (
        queue_stored_refresh_max_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=60,
        brain_queue_stored_refresh_max_tickers=40,
        brain_queue_intraday_timeframes="1m,5m,15m",
        brain_queue_intraday_target_tickers=24,
        brain_queue_operational_refresh_enabled=True,
        brain_queue_operational_refresh_lifecycles="promoted,live,shadow_promoted,pilot_promoted",
        brain_queue_operational_target_tickers=24,
        brain_queue_operational_stored_refresh_max_tickers=18,
    )
    pattern = SimpleNamespace(lifecycle_stage="shadow_promoted", timeframe="1d")

    assert queue_stored_refresh_max_tickers_for_pattern(cfg, pattern) == 18


def test_operational_queue_budget_can_be_disabled():
    from app.services.trading.backtest_queue_worker import (
        queue_target_tickers_for_pattern,
    )

    cfg = SimpleNamespace(
        brain_queue_target_tickers=60,
        brain_queue_intraday_timeframes="1m,5m,15m",
        brain_queue_intraday_target_tickers=24,
        brain_queue_operational_refresh_enabled=False,
        brain_queue_operational_refresh_lifecycles="promoted",
        brain_queue_operational_target_tickers=24,
    )
    pattern = SimpleNamespace(lifecycle_stage="promoted", timeframe="1d")

    assert queue_target_tickers_for_pattern(cfg, pattern) == 60
