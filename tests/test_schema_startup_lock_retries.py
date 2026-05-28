from __future__ import annotations

from sqlalchemy.exc import OperationalError

from app import migrations


class _Dialect:
    name = "postgresql"


class _FakeConnection:
    def __init__(self) -> None:
        self.execution_options_kwargs: dict[str, str] | None = None
        self.statements: list[str] = []

    def execution_options(self, **kwargs):
        self.execution_options_kwargs = kwargs
        return self

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def execute(self, statement, params=None):
        self.statements.append(str(statement))


class _FlakyPostgresEngine:
    dialect = _Dialect()

    def __init__(self, failures: int) -> None:
        self.failures = failures
        self.connect_calls = 0
        self.connection = _FakeConnection()

    def connect(self):
        self.connect_calls += 1
        if self.connect_calls <= self.failures:
            raise OperationalError("connect", {}, Exception("database is starting up"))
        return self.connection


def test_schema_startup_lock_retries_until_postgres_accepts_connections(monkeypatch):
    engine = _FlakyPostgresEngine(failures=2)
    sleeps: list[float] = []

    monkeypatch.setenv("CHILI_SCHEMA_STARTUP_CONNECT_ATTEMPTS", "3")
    monkeypatch.setenv("CHILI_SCHEMA_STARTUP_CONNECT_DELAY_SECONDS", "0.25")
    monkeypatch.setattr(migrations.time, "sleep", sleeps.append)

    with migrations.schema_startup_lock(engine):
        pass

    assert engine.connect_calls == 3
    assert sleeps == [0.25, 0.25]
    assert engine.connection.execution_options_kwargs == {"isolation_level": "AUTOCOMMIT"}
    assert any("pg_advisory_lock" in statement for statement in engine.connection.statements)
    assert any("pg_advisory_unlock" in statement for statement in engine.connection.statements)


def test_schema_startup_lock_leaves_non_postgres_engines_alone():
    class _NonPostgresEngine:
        class dialect:
            name = "sqlite"

        def connect(self):
            raise AssertionError("non-Postgres engines should not connect for schema lock")

    with migrations.schema_startup_lock(_NonPostgresEngine()):
        pass
