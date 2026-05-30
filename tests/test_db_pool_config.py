from __future__ import annotations

from types import SimpleNamespace

from app.config import (
    DATABASE_DEFAULT_MAX_OVERFLOW,
    DATABASE_DEFAULT_IDLE_SESSION_TIMEOUT_MS,
    DATABASE_DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT_MS,
    DATABASE_DEFAULT_POOL_SIZE,
    DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
    DATABASE_PYTEST_DEFAULT_MAX_OVERFLOW,
    DATABASE_PYTEST_DEFAULT_POOL_SIZE,
    DATABASE_PYTEST_DEFAULT_POOL_TIMEOUT_SECONDS,
)
from app.db import _build_pg_connect_options, _resolve_pool_config

_MP_CHILD_POOL_SIZE = 1
_MP_CHILD_MAX_OVERFLOW = 2


def _settings(**overrides: object) -> SimpleNamespace:
    values = {
        "database_pool_size": DATABASE_DEFAULT_POOL_SIZE,
        "database_max_overflow": DATABASE_DEFAULT_MAX_OVERFLOW,
        "database_pool_timeout_seconds": DATABASE_DEFAULT_POOL_TIMEOUT_SECONDS,
        "database_idle_in_transaction_timeout_ms": DATABASE_DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT_MS,
        "database_idle_session_timeout_ms": DATABASE_DEFAULT_IDLE_SESSION_TIMEOUT_MS,
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


def test_runtime_default_pool_budget_is_incident_capped() -> None:
    assert DATABASE_DEFAULT_POOL_SIZE == 8
    assert DATABASE_DEFAULT_MAX_OVERFLOW == 8


def test_pg_connect_options_set_idle_transaction_and_idle_session_timeouts() -> None:
    options = _build_pg_connect_options(
        idle_xact_timeout_ms=DATABASE_DEFAULT_IDLE_IN_TRANSACTION_TIMEOUT_MS,
        idle_session_timeout_ms=DATABASE_DEFAULT_IDLE_SESSION_TIMEOUT_MS,
    )

    assert options == [
        "-c",
        "idle_in_transaction_session_timeout=120000",
        "-c",
        "idle_session_timeout=600000",
    ]


def test_pg_connect_options_can_disable_idle_timeouts() -> None:
    assert _build_pg_connect_options(
        idle_xact_timeout_ms=0,
        idle_session_timeout_ms=0,
    ) == []
