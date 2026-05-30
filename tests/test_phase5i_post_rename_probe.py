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

    class FakeConn:
        def close(self) -> None:
            pass

    monkeypatch.setattr(module.psycopg2, "connect", lambda _dsn: FakeConn())
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
