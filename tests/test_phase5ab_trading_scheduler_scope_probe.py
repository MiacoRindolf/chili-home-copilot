from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5ab-trading-scheduler-scope-parity-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5ab_scheduler_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Result:
    def __init__(self, rows=None, scalar_value=None):
        self._rows = rows or []
        self._scalar = scalar_value

    def fetchall(self):
        return self._rows

    def mappings(self):
        return self

    def all(self):
        return self._rows

    def scalar(self):
        return self._scalar


class _Db:
    def __init__(self):
        self.calls: list[tuple[str, dict | None]] = []

    def execute(self, sql, params=None):
        rendered = str(sql)
        self.calls.append((rendered, params))
        if "pg_class" in rendered:
            name = params["name"]
            return _Result(
                scalar_value="r"
                if name == "trading_management_envelopes"
                else "v"
            )
        if "COUNT(*)::bigint" in rendered:
            return _Result(rows=[{"user_id": 1, "n": 2}])
        if "SELECT id" in rendered:
            return _Result(rows=[(10,), (11,)])
        if "SELECT DISTINCT UPPER(ticker)" in rendered:
            return _Result(rows=[("ABC",), ("XYZ-USD",)])
        if "SELECT DISTINCT user_id" in rendered:
            return _Result(rows=[(1,), (2,)])
        return _Result(rows=[])


def test_probe_rejects_non_test_database_without_live_opt_in(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)

    try:
        module._assert_probe_database_allowed(
            "postgresql://chili:chili@localhost:5433/chili"
        )
    except RuntimeError as exc:
        assert module.LIVE_PROBE_OPT_IN in str(exc)
    else:
        raise AssertionError("expected live database to require opt-in")


def test_probe_allows_test_database_without_live_opt_in(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)

    module._assert_probe_database_allowed(
        "postgresql://chili:chili@localhost:5433/chili_test"
    )


def test_bind_list_builds_named_params() -> None:
    module = _load_module()

    binds, params = module._bind_list("x", ["a", "b"])

    assert binds == ":x_0, :x_1"
    assert params == {"x_0": "a", "x_1": "b"}


def test_scope_values_read_management_envelopes_not_compat_view() -> None:
    module = _load_module()
    db = _Db()

    module._scope_values(db, relation_name=module.MANAGEMENT_ENVELOPES_RELATION)

    assert any("FROM trading_management_envelopes" in sql for sql, _ in db.calls)
    assert not any("FROM trading_trades" in sql for sql, _ in db.calls)


def test_run_probe_reports_complete_positive_when_scopes_match() -> None:
    module = _load_module()

    result = module.run_probe(_Db())

    assert result["status"] == "COMPLETE_POSITIVE"
    assert result["mismatches"] == 0
    assert result["checks"] == 9


def test_run_probe_reports_alert_on_scope_drift(monkeypatch) -> None:
    module = _load_module()
    original = module._scope_values

    def fake_scope_values(db, *, relation_name):
        values = original(db, relation_name=relation_name)
        if relation_name == module.MANAGEMENT_ENVELOPES_RELATION:
            values["price_monitor_user_ids"] = [99]
        return values

    monkeypatch.setattr(module, "_scope_values", fake_scope_values)

    result = module.run_probe(_Db())

    assert result["status"] == "ALERT"
    assert result["mismatches"] == 1

