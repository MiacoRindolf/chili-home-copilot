"""The correlation instrument applies its actual read safeguards in each session."""
from datetime import datetime, timezone
from types import SimpleNamespace


def test_correlation_configures_both_read_sessions_before_their_queries(monkeypatch, capsys):
    from scripts import feature_outcome_correlation as instrument

    sessions = []
    instant = datetime(2026, 9, 10, 18, tzinfo=timezone.utc)

    class ReadSession:
        def __init__(self):
            self.read_only = False
            self.timeout_ms = None
            self.statements = []
            sessions.append(self)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def execute(self, statement, params=None):
            sql = str(statement)
            self.statements.append(sql)
            if sql.startswith("SET TRANSACTION READ ONLY"):
                self.read_only = True
            elif "statement_timeout" in sql:
                # Treat every later SET as an override, exactly as PostgreSQL
                # does. The former 60s/30s overrides therefore fail this test.
                self.timeout_ms = int(sql.split("'")[1].removesuffix("s")) * 1000
            else:
                assert self.read_only and self.timeout_ms == 20_000
            return SimpleNamespace(fetchall=lambda: [(instant, "ABC", instant.date(), 1.0)])

    monkeypatch.setattr(instrument, "create_engine", lambda *a, **k: object())
    monkeypatch.setattr(instrument, "sessionmaker", lambda **k: ReadSession)
    monkeypatch.setattr(instrument.sys, "argv", ["correlation", "--database-url", "test-only", "--prints", "255"])

    def read_tape(symbol, *, db, as_of, window_prints):
        assert db is sessions[1]
        assert db.read_only and db.timeout_ms == 20_000
        assert symbol == "ABC" and as_of == instant and window_prints == 255
        return {name: 1.0 for name in instrument.FEATURES}

    monkeypatch.setattr(instrument, "signed_tape_accel_features", read_tape)
    assert instrument.main() == 0
    assert len(sessions) == 2
    assert all(session.read_only and session.timeout_ms == 20_000 for session in sessions)
    assert "tape readable               : 1" in capsys.readouterr().out
