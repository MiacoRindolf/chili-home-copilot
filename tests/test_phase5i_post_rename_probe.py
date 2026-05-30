from __future__ import annotations

import importlib.util
from datetime import datetime
from pathlib import Path
from types import ModuleType


def _load_probe_module() -> ModuleType:
    path = Path(__file__).resolve().parents[1] / "scripts" / "d-phase5i-post-rename-soak-probe.py"
    spec = importlib.util.spec_from_file_location("phase5i_probe", path)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_phase5i_probe_cancelled_pattern_drift_is_diagnostic(
    monkeypatch,
    capsys,
):
    module = _load_probe_module()

    class FakeCursor:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql: str, *_args, **_kwargs) -> None:
            self.executed.append(sql)

        def fetchone(self):
            return ("on",)

    class FakeConn:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()
            self.session_args = None

        def set_session(self, **kwargs) -> None:
            self.session_args = kwargs

        def cursor(self):
            return self.cursor_obj

        def close(self) -> None:
            pass

    conn = FakeConn()
    monkeypatch.setattr(module.psycopg2, "connect", lambda _dsn: conn)
    monkeypatch.setattr(
        module,
        "_fetch_all",
        lambda _conn, _sql, _params=(): [
            {"relname": "trading_management_envelopes", "relkind": "r"},
            {"relname": "trading_phase5b_decision_envelope_position", "relkind": "v"},
            {"relname": "trading_trades", "relkind": "v"},
        ],
    )

    calls = {"n": 0}

    def fake_fetch_one(_conn, sql, _params=()):
        if "schema_version" in sql:
            return {"applied_at": datetime(2026, 5, 28, 17, 6, 51)}
        if "fresh_closes" in sql:
            assert "AND envelope_status = 'closed'" in sql
            assert "AS fresh_cancelled_mismatches" in sql
            calls["n"] += 1
            return {
                "fresh_decisions": 1,
                "fresh_envelopes": 1,
                "fresh_closes": 1,
                "fresh_close_mismatches": 0,
                "fresh_terminal_non_closed": 1,
                "fresh_cancelled_mismatches": 1,
                "hard_linkage_issues": 0,
            }
        if "closed_rows" in sql:
            return {
                "closed_rows": 1,
                "mismatched_rows": 0,
                "mismatched_pnl": 0,
            }
        raise AssertionError(sql)

    monkeypatch.setattr(module, "_fetch_one", fake_fetch_one)

    assert module._main() == 0
    out = capsys.readouterr().out
    assert "VERDICT_STATUS=COMPLETE_POSITIVE" in out
    assert "FRESH_CLOSES=1" in out
    assert "FRESH_CLOSE_MISMATCHES=0" in out
    assert "FRESH_CANCELLED_MISMATCHES=1" in out
    assert calls["n"] == 1
    assert conn.session_args == {"readonly": True, "autocommit": False}
    assert conn.cursor_obj.executed == [
        "SET TRANSACTION READ ONLY",
        "SHOW transaction_read_only",
    ]


def test_phase5i_probe_enforces_read_only_transaction():
    module = _load_probe_module()

    class FakeCursor:
        def __init__(self) -> None:
            self.executed: list[str] = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, sql: str, *_args, **_kwargs) -> None:
            self.executed.append(sql)

        def fetchone(self):
            return ("on",)

    class FakeConn:
        def __init__(self) -> None:
            self.cursor_obj = FakeCursor()
            self.session_args = None

        def set_session(self, **kwargs) -> None:
            self.session_args = kwargs

        def cursor(self):
            return self.cursor_obj

    conn = FakeConn()

    module._enforce_read_only(conn)

    assert conn.session_args == {"readonly": True, "autocommit": False}
    assert conn.cursor_obj.executed == [
        "SET TRANSACTION READ ONLY",
        "SHOW transaction_read_only",
    ]


def test_phase5i_probe_rejects_non_read_only_transaction():
    module = _load_probe_module()

    class FakeCursor:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def execute(self, *_args, **_kwargs) -> None:
            pass

        def fetchone(self):
            return ("off",)

    class FakeConn:
        def set_session(self, **_kwargs) -> None:
            pass

        def cursor(self):
            return FakeCursor()

    try:
        module._enforce_read_only(FakeConn())
    except RuntimeError as exc:
        assert "read-only transaction mode" in str(exc)
    else:
        raise AssertionError("expected read-only enforcement to fail")
