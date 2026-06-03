from __future__ import annotations

from types import SimpleNamespace

from app.config import (
    DATABASE_DEFAULT_MAX_OVERFLOW,
    DATABASE_DEFAULT_POOL_SIZE,
    DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
    DATABASE_PYTEST_DEFAULT_MAX_OVERFLOW,
    DATABASE_PYTEST_DEFAULT_POOL_SIZE,
    DATABASE_PYTEST_DEFAULT_POOL_TIMEOUT_SECONDS,
)
from app.db import _resolve_pool_config

_MP_CHILD_POOL_SIZE = 1
_MP_CHILD_MAX_OVERFLOW = 2


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "database_pool_size": DATABASE_DEFAULT_POOL_SIZE,
        "database_max_overflow": DATABASE_DEFAULT_MAX_OVERFLOW,
        "database_pool_timeout_seconds": DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
        "database_pytest_pool_size": DATABASE_PYTEST_DEFAULT_POOL_SIZE,
        "database_pytest_max_overflow": DATABASE_PYTEST_DEFAULT_MAX_OVERFLOW,
        "database_pytest_pool_timeout_seconds": DATABASE_PYTEST_DEFAULT_POOL_TIMEOUT_SECONDS,
        "brain_mp_child_database_pool_size": _MP_CHILD_POOL_SIZE,
        "brain_mp_child_database_max_overflow": _MP_CHILD_MAX_OVERFLOW,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_runtime_pool_uses_operational_budget() -> None:
    assert _resolve_pool_config(_settings(), mp_child=False, pytest_process=False) == (
        DATABASE_DEFAULT_POOL_SIZE,
        DATABASE_DEFAULT_MAX_OVERFLOW,
        DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def test_service_pool_cap_shrinks_resident_web_connections() -> None:
    assert _resolve_pool_config(
        _settings(),
        mp_child=False,
        pytest_process=False,
        app_name="chili-app",
        environ={},
    ) == (
        8,
        72,
        DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def test_service_pool_cap_keeps_bursty_scheduler_bounded() -> None:
    assert _resolve_pool_config(
        _settings(database_pool_size=25, database_max_overflow=50),
        mp_child=False,
        pytest_process=False,
        app_name="chili-scheduler-cron",
        environ={},
    ) == (
        8,
        67,
        DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def test_service_pool_cap_never_expands_lower_explicit_budget() -> None:
    assert _resolve_pool_config(
        _settings(database_pool_size=3, database_max_overflow=2),
        mp_child=False,
        pytest_process=False,
        app_name="chili-autotrader-worker",
        environ={},
    ) == (
        3,
        2,
        DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def test_service_pool_cap_preserves_peak_checkout_capacity() -> None:
    pool_size, max_overflow, _ = _resolve_pool_config(
        _settings(database_pool_size=10, database_max_overflow=15),
        mp_child=False,
        pytest_process=False,
        app_name="chili-autotrader-worker",
        environ={},
    )
    assert pool_size == 4
    assert pool_size + max_overflow == 25


def test_service_pool_cap_can_be_disabled_by_operator() -> None:
    assert _resolve_pool_config(
        _settings(database_pool_size=25, database_max_overflow=50),
        mp_child=False,
        pytest_process=False,
        app_name="chili-scheduler-cron",
        environ={"CHILI_DATABASE_SERVICE_POOL_CAPS_ENABLED": "0"},
    ) == (
        25,
        50,
        DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def test_service_pool_cap_does_not_shrink_brain_worker() -> None:
    assert _resolve_pool_config(
        _settings(database_pool_size=8, database_max_overflow=12),
        mp_child=False,
        pytest_process=False,
        app_name="chili-brain-worker",
        environ={},
    ) == (
        8,
        12,
        DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def test_pytest_pool_is_capped_to_debug_budget() -> None:
    assert _resolve_pool_config(_settings(), mp_child=False, pytest_process=True) == (
        DATABASE_PYTEST_DEFAULT_POOL_SIZE,
        DATABASE_PYTEST_DEFAULT_MAX_OVERFLOW,
        DATABASE_PYTEST_DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def test_pytest_pool_never_expands_an_explicitly_lower_runtime_pool() -> None:
    assert _resolve_pool_config(
        _settings(database_pool_size=1, database_max_overflow=0),
        mp_child=False,
        pytest_process=True,
    ) == (
        1,
        0,
        DATABASE_PYTEST_DEFAULT_POOL_TIMEOUT_SECONDS,
    )


def test_mp_child_pool_uses_backtest_child_budget_even_under_pytest() -> None:
    assert _resolve_pool_config(_settings(), mp_child=True, pytest_process=True) == (
        _MP_CHILD_POOL_SIZE,
        _MP_CHILD_MAX_OVERFLOW,
        DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
    )
