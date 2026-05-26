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
