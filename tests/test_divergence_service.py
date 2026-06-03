from __future__ import annotations

from types import SimpleNamespace

from app.services.trading import divergence_service as mod


class _Rows:
    def __init__(self, rows):
        self._rows = rows

    def fetchall(self):
        return self._rows


class _FakeDivergenceSession:
    def __init__(self, *, dialect: str = "postgresql") -> None:
        self.dialect = dialect
        self.calls: list[tuple[str, dict | None]] = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, stmt, params=None):
        sql = str(stmt)
        self.calls.append((sql, dict(params or {})))
        if "SELECT dp.scan_pattern_id" in sql:
            return _Rows([(585, "crypto_breakout"), (1250, None)])
        return _Rows([])


def test_discover_active_patterns_bounds_postgres_discovery_query(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mod.settings,
        "brain_divergence_scorer_discovery_timeout_ms",
        1234,
        raising=False,
    )
    db = _FakeDivergenceSession()

    rows = mod.discover_active_patterns(db, lookback_days=7, limit=10)

    assert rows == [(585, "crypto_breakout"), (1250, None)]
    assert db.calls[0][0] == "SET LOCAL statement_timeout = '30000ms'"
    assert db.calls[-1][0] == "SET LOCAL statement_timeout = DEFAULT"
    sql, params = db.calls[1]
    assert params == {"ld": 7, "lim": 10}
    assert "WITH source_patterns AS" in sql
    assert "distinct_patterns AS" in sql
    assert sql.count("UNION ALL") == 4
    assert "SELECT DISTINCT scan_pattern_id" in sql
    assert "SELECT dp.scan_pattern_id" in sql
    assert "WHERE EXISTS" not in sql


def test_discover_active_patterns_skips_timeout_on_non_postgres(
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        mod.settings,
        "brain_divergence_scorer_discovery_timeout_ms",
        1234,
        raising=False,
    )
    db = _FakeDivergenceSession(dialect="sqlite")

    rows = mod.discover_active_patterns(db, lookback_days=3)

    assert rows == [(585, "crypto_breakout"), (1250, None)]
    assert len(db.calls) == 1
    assert db.calls[0][1] == {"ld": 3}


def test_discovery_timeout_caps_lookback_pressure(monkeypatch) -> None:
    monkeypatch.setattr(
        mod.settings,
        "brain_divergence_scorer_discovery_timeout_ms",
        1234,
        raising=False,
    )

    assert mod._discovery_statement_timeout_ms(lookback_days=1) == 5000
    assert mod._discovery_statement_timeout_ms(lookback_days=7) == 30000
    assert mod._discovery_statement_timeout_ms(lookback_days=30) == 30000
