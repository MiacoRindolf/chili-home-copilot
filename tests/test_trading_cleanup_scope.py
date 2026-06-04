from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace


def _request_for(filename: str) -> SimpleNamespace:
    return SimpleNamespace(node=SimpleNamespace(fspath=Path(filename)))


def test_triple_barrier_db_tests_use_trading_targeted_cleanup() -> None:
    conftest = sys.modules["tests.conftest"]

    for filename in (
        "test_triple_barrier_label_anchor.py",
        "test_triple_barrier_labeler.py",
        "test_triple_barrier_scheduler.py",
    ):
        tables = conftest._test_targeted_cleanup_tables(_request_for(filename))

        assert tables is conftest._TRADING_DOMAIN_TARGETED_TABLES
        assert "trading_snapshots" in tables
        assert "trading_triple_barrier_labels" in tables
        assert "scan_patterns" in tables
        assert "birthdays" not in tables


def test_autotrader_integration_db_tests_use_trading_targeted_cleanup() -> None:
    conftest = sys.modules["tests.conftest"]

    tables = conftest._test_targeted_cleanup_tables(
        _request_for("test_auto_trader_integration.py")
    )

    assert tables is conftest._TRADING_DOMAIN_TARGETED_TABLES
    assert "trading_autotrader_runs" in tables
    assert "trading_breakout_alerts" in tables
    assert "trading_paper_trades" in tables
    assert "birthdays" not in tables


def test_prescreen_artifact_db_tests_use_trading_targeted_cleanup() -> None:
    conftest = sys.modules["tests.conftest"]

    tables = conftest._test_targeted_cleanup_tables(
        _request_for("test_prescreen_artifacts.py")
    )

    assert tables is conftest._TRADING_DOMAIN_TARGETED_TABLES
    assert "trading_prescreen_candidates" in tables
    assert "trading_prescreen_snapshots" in tables
    assert "brain_batch_jobs" in tables
    assert "birthdays" not in tables


def test_neural_mesh_db_tests_use_mesh_targeted_cleanup() -> None:
    conftest = sys.modules["tests.conftest"]

    tables = conftest._test_targeted_cleanup_tables(
        _request_for("test_brain_neural_mesh.py")
    )

    assert tables is conftest._TRADING_NEURAL_MESH_TARGETED_TABLES
    assert "brain_activation_events" in tables
    assert "brain_node_states" in tables
    assert "brain_graph_nodes" not in tables
    assert "birthdays" not in tables


def test_full_cleanup_bounds_truncate_statement_timeout(monkeypatch) -> None:
    conftest = sys.modules["tests.conftest"]
    calls: list[str] = []

    class _Conn:
        def execute(self, statement):
            calls.append(str(statement))

    class _Begin:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *_exc):
            return False

    class _Engine:
        def begin(self):
            return _Begin()

    monkeypatch.setenv("CHILI_PYTEST_TRUNCATE_STATEMENT_TIMEOUT_S", "37")
    monkeypatch.setattr(conftest, "engine", _Engine())
    monkeypatch.setattr(
        conftest,
        "Base",
        SimpleNamespace(
            metadata=SimpleNamespace(
                sorted_tables=[
                    SimpleNamespace(name="schema_version"),
                    SimpleNamespace(name="users"),
                ]
            )
        ),
    )
    monkeypatch.setattr(conftest, "_evict_idle_in_transaction_peers", lambda: None)
    monkeypatch.setattr(conftest, "_terminate_stale_truncate_peers", lambda: None)
    monkeypatch.setattr(conftest, "_truncate_relation_names", lambda _conn, names: names)

    conftest._truncate_app_tables()

    assert "SET LOCAL lock_timeout = '120s'" in calls
    assert "SET LOCAL statement_timeout = '37s'" in calls
    assert calls[-1].startswith("TRUNCATE ")
    assert "users" in calls[-1]


def test_full_cleanup_caps_oversized_statement_timeout(monkeypatch) -> None:
    conftest = sys.modules["tests.conftest"]
    calls: list[str] = []

    class _Conn:
        def execute(self, statement):
            calls.append(str(statement))

    class _Begin:
        def __enter__(self):
            return _Conn()

        def __exit__(self, *_exc):
            return False

    class _Engine:
        def begin(self):
            return _Begin()

    monkeypatch.setenv("CHILI_PYTEST_TRUNCATE_STATEMENT_TIMEOUT_S", "900")
    monkeypatch.setattr(conftest, "engine", _Engine())
    monkeypatch.setattr(
        conftest,
        "Base",
        SimpleNamespace(
            metadata=SimpleNamespace(
                sorted_tables=[
                    SimpleNamespace(name="schema_version"),
                    SimpleNamespace(name="users"),
                ]
            )
        ),
    )
    monkeypatch.setattr(conftest, "_evict_idle_in_transaction_peers", lambda: None)
    monkeypatch.setattr(conftest, "_terminate_stale_truncate_peers", lambda: None)
    monkeypatch.setattr(conftest, "_truncate_relation_names", lambda _conn, names: names)

    conftest._truncate_app_tables()

    assert "SET LOCAL statement_timeout = '90s'" in calls
