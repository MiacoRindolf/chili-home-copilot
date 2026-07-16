from __future__ import annotations

import pytest

from scripts import autopilot_trading_db_incident_replay_benchmark as benchmark


def test_db_replay_retries_transient_setup_connection_once(monkeypatch):
    calls: list[tuple[tuple[str, ...], int]] = []

    def fake_run_pytest_slice(repo_root, tests, *, timeout_seconds):
        calls.append((tuple(tests), timeout_seconds))
        if len(calls) == 1:
            raise AssertionError(
                "ERROR at setup of test_candidate_selector "
                "engine.raw_connection sqlalchemy.exc.OperationalError connection refused"
            )
        return "pytest passed"

    monkeypatch.setattr(benchmark, "run_pytest_slice", fake_run_pytest_slice)

    evidence = benchmark._run_db_pytest_slice(("tests/test_auto_trader_safety.py::test_case",), timeout_seconds=7)

    assert evidence.startswith("pytest passed")
    assert "transient_db_setup_retry_attempts=2" in evidence
    assert "first_failure=ERROR at setup" in evidence
    assert calls == [
        (("tests/test_auto_trader_safety.py::test_case",), 7),
        (("tests/test_auto_trader_safety.py::test_case",), 7),
    ]


def test_db_replay_does_not_retry_behavior_assertion(monkeypatch):
    calls = 0

    def fake_run_pytest_slice(repo_root, tests, *, timeout_seconds):
        nonlocal calls
        calls += 1
        raise AssertionError("AssertionError: expected split candidate lanes but saw broad scope")

    monkeypatch.setattr(benchmark, "run_pytest_slice", fake_run_pytest_slice)

    with pytest.raises(AssertionError, match="expected split candidate lanes"):
        benchmark._run_db_pytest_slice(("tests/test_auto_trader_safety.py::test_case",))

    assert calls == 1


def test_db_replay_does_not_retry_query_time_operational_error(monkeypatch):
    calls = 0

    def fake_run_pytest_slice(repo_root, tests, *, timeout_seconds):
        nonlocal calls
        calls += 1
        raise AssertionError("sqlalchemy.exc.OperationalError while asserting candidate query shape")

    monkeypatch.setattr(benchmark, "run_pytest_slice", fake_run_pytest_slice)

    with pytest.raises(AssertionError, match="candidate query shape"):
        benchmark._run_db_pytest_slice(("tests/test_auto_trader_safety.py::test_case",))

    assert calls == 1
