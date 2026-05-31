from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5o-brain-plasticity-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_brain_plasticity_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_live_probe_requires_test_db_or_explicit_opt_in(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)
    module._assert_probe_database_allowed("postgresql://chili:chili@localhost:5433/chili_test")

    try:
        module._assert_probe_database_allowed("postgresql://chili:chili@localhost:5433/chili")
    except RuntimeError as exc:
        assert module.LIVE_PROBE_OPT_IN in str(exc)
    else:
        raise AssertionError("live DB probe should require explicit opt-in")

    monkeypatch.setenv(module.LIVE_PROBE_OPT_IN, "true")
    module._assert_probe_database_allowed("postgresql://chili:chili@localhost:5433/chili")


def test_relation_sql_allowlist() -> None:
    module = _load_module()

    assert module._relation_sql(module.LEGACY_TRADES_COMPAT_RELATION) == "trading_trades"
    assert module._relation_sql(module.MANAGEMENT_ENVELOPES_RELATION) == "trading_management_envelopes"

    try:
        module._relation_sql("trading_decisions")
    except ValueError as exc:
        assert "unsupported relation" in str(exc)
    else:
        raise AssertionError("unexpected relation should fail closed")


def test_risked_capital_matches_plasticity_formula() -> None:
    module = _load_module()

    assert module._risked_capital({"entry_price": "10.00", "stop_loss": "9.25", "quantity": "4"}) == 3.0
    assert module._risked_capital({"entry_price": "10.00", "stop_loss": None, "quantity": "4"}) == 0.0
    assert module._risked_capital({"entry_price": "0", "stop_loss": "9.25", "quantity": "4"}) == 0.0
    assert module._risked_capital({"entry_price": "10.00", "stop_loss": "9.25", "quantity": "0"}) == 0.0


def test_scope_values_marks_eligible_and_path_trade_ids(monkeypatch) -> None:
    module = _load_module()

    def fake_closed_rows(_db, *, relation_name):
        assert relation_name == module.LEGACY_TRADES_COMPAT_RELATION
        return [
            {
                "id": 1,
                "mesh_entry_correlation_id": "corr-a",
                "entry_price": "10.00",
                "stop_loss": "9.00",
                "quantity": "2",
                "status": "closed",
            },
            {
                "id": 2,
                "mesh_entry_correlation_id": "corr-b",
                "entry_price": "10.00",
                "stop_loss": None,
                "quantity": "2",
                "status": "closed",
            },
        ]

    def fake_edge_counts(_db, correlation_ids):
        assert correlation_ids == ["corr-a"]
        return [{"correlation_id": "corr-a", "edge_count": 3}]

    monkeypatch.setattr(module, "_closed_correlation_rows", fake_closed_rows)
    monkeypatch.setattr(module, "_edge_counts_by_correlation", fake_edge_counts)

    values = module._scope_values(object(), relation_name=module.LEGACY_TRADES_COMPAT_RELATION)

    assert values["eligible_trade_ids"] == [1]
    assert values["eligible_with_path_trade_ids"] == [1]
    assert values["path_edge_counts"] == [{"correlation_id": "corr-a", "edge_count": 3}]
