from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5o-brain-action-handlers-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_brain_action_probe", SCRIPT_PATH)
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


def test_trade_id_extraction_and_critical_subset() -> None:
    module = _load_module()

    rows = [
        {"trade_id": "12", "action": "exit_now", "urgency": ""},
        {"trade_id": "12", "action": "hold", "urgency": "info"},
        {"trade_id": "13", "action": "hold", "urgency": "critical"},
        {"trade_id": "bad", "action": "STOP_HIT", "urgency": ""},
    ]

    assert module._trade_ids_from_states(rows) == [12, 13]
    assert module._critical_trade_ids_from_states(rows) == [12, 13]


def test_scope_values_classify_missing_open_and_non_open_rows() -> None:
    module = _load_module()

    class FakeDb:
        def execute(self, *_args, **_kwargs):
            raise AssertionError("db should not be called by this unit path")

    state_rows = [{"trade_id": "1"}, {"trade_id": "2"}, {"trade_id": "3"}]

    def fake_rows(_db, *, relation_name, trade_ids):
        assert relation_name == module.LEGACY_TRADES_COMPAT_RELATION
        assert trade_ids == [1, 2, 3]
        return [
            {"id": 1, "status": "open", "ticker": "AAPL"},
            {"id": 2, "status": "closed", "ticker": "MSFT"},
        ]

    original = module._local_trade_validation_rows
    module._local_trade_validation_rows = fake_rows
    try:
        values = module._scope_values(
            FakeDb(),
            relation_name=module.LEGACY_TRADES_COMPAT_RELATION,
            state_rows=state_rows,
        )
    finally:
        module._local_trade_validation_rows = original

    assert values["all_child_trade_ids"] == [1, 2, 3]
    assert values["open_child_trade_ids"] == [1]
    assert values["non_open_child_trade_ids"] == [2]
    assert values["missing_child_trade_ids"] == [3]
