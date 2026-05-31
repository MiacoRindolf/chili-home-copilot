from __future__ import annotations

from datetime import datetime
from decimal import Decimal
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5ab-c-pattern-monitor-runtime-object-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5ab_c_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Db:
    pass


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


def test_object_snapshot_normalizes_runtime_fields() -> None:
    module = _load_module()
    obj = SimpleNamespace(
        id=1,
        ticker="ABC",
        entry_price=Decimal("12.34"),
        entry_date=datetime(2026, 5, 31, 7, 0),
        indicator_snapshot={"z": Decimal("2.5"), "a": [Decimal("1.5")]},
    )

    snap = module._object_snapshot(obj)

    assert snap["id"] == 1
    assert snap["ticker"] == "ABC"
    assert snap["entry_price"] == 12.34
    assert snap["entry_date"] == "2026-05-31T07:00:00"
    assert snap["indicator_snapshot"] == {"a": [1.5], "z": 2.5}
    assert "broker_source" in snap
    assert snap["broker_source"] is None


def test_field_mismatches_detect_presence_and_value_drift() -> None:
    module = _load_module()
    old = [
        SimpleNamespace(id=1, ticker="ABC", status="open"),
        SimpleNamespace(id=2, ticker="XYZ", status="open"),
    ]
    new = [
        SimpleNamespace(id=1, ticker="ABC", status="closed"),
        SimpleNamespace(id=3, ticker="NEW", status="open"),
    ]

    mismatches = module._field_mismatches(old, new)

    assert {"id": 1, "field": "status", "old": "open", "new": "closed"} in mismatches
    assert {"id": 2, "field": "presence", "old": True, "new": False} in mismatches
    assert {"id": 3, "field": "presence", "old": False, "new": True} in mismatches


def test_run_probe_reports_complete_positive_when_runtime_objects_match(monkeypatch) -> None:
    module = _load_module()
    old = [SimpleNamespace(id=1, ticker="ABC", status="open")]
    new = [SimpleNamespace(id=1, ticker="ABC", status="open")]

    monkeypatch.setattr(
        module,
        "_scope_tickers",
        lambda db, *, relation_name: ["ABC"],
    )
    monkeypatch.setattr(module, "_load_old_trade_objects", lambda db, *, tickers: old)
    monkeypatch.setattr(module, "_load_new_envelope_objects", lambda db, *, tickers: new)
    monkeypatch.setattr(
        module,
        "_broker_truth_projection",
        lambda db, objects: {"live_ids": [1], "stale": []},
    )
    monkeypatch.setattr(
        module,
        "_relation_kind",
        lambda db, relation_name: "r"
        if relation_name == module.MANAGEMENT_ENVELOPES_RELATION
        else "v",
    )

    result = module.run_probe(_Db())

    assert result["status"] == "COMPLETE_POSITIVE"
    assert result["field_mismatches"] == []
    assert result["broker_truth_match"] is True


def test_run_probe_reports_alert_on_broker_truth_drift(monkeypatch) -> None:
    module = _load_module()
    old = [SimpleNamespace(id=1, ticker="ABC", status="open")]
    new = [SimpleNamespace(id=1, ticker="ABC", status="open")]

    monkeypatch.setattr(module, "_scope_tickers", lambda db, *, relation_name: ["ABC"])
    monkeypatch.setattr(module, "_load_old_trade_objects", lambda db, *, tickers: old)
    monkeypatch.setattr(module, "_load_new_envelope_objects", lambda db, *, tickers: new)

    def fake_projection(db, objects):
        if objects is old:
            return {"live_ids": [1], "stale": []}
        return {"live_ids": [], "stale": [{"id": 1, "reason": "drift"}]}

    monkeypatch.setattr(module, "_broker_truth_projection", fake_projection)
    monkeypatch.setattr(
        module,
        "_relation_kind",
        lambda db, relation_name: "r"
        if relation_name == module.MANAGEMENT_ENVELOPES_RELATION
        else "v",
    )

    result = module.run_probe(_Db())

    assert result["status"] == "ALERT"
    assert result["broker_truth_match"] is False
