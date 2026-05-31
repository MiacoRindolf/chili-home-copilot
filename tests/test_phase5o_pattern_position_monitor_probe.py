from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5o-pattern-position-monitor-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_pattern_position_monitor_probe", SCRIPT_PATH)
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


def test_control_values_capture_pattern_and_plan_lanes() -> None:
    module = _load_module()

    rows = [
        {
            "id": 10,
            "user_id": 1,
            "ticker": "AAPL",
            "status": "open",
            "direction": "long",
            "broker_source": "robinhood",
            "auto_trader_version": "v1",
            "related_alert_id": 100,
            "scan_pattern_id": 585,
            "stop_loss": "95",
            "take_profit": "120",
            "entry_price": "100",
            "quantity": "2",
            "asset_kind": "stock",
            "tags": "",
            "indicator_snapshot": {},
            "monitor_lane": "pattern_linked",
        },
        {
            "id": 11,
            "user_id": 1,
            "ticker": "SPY",
            "status": "open",
            "direction": "long",
            "broker_source": "robinhood",
            "auto_trader_version": None,
            "related_alert_id": None,
            "scan_pattern_id": None,
            "stop_loss": "1.20",
            "take_profit": None,
            "entry_price": "1.50",
            "quantity": "1",
            "asset_kind": "option",
            "tags": "",
            "indicator_snapshot": {},
            "monitor_lane": "plan_levels",
        },
    ]

    values = module._control_values_for_rows(rows)

    assert values["selected_trade_ids"] == [10, 11]
    assert values["pattern_linked_trade_ids"] == [10]
    assert values["plan_level_trade_ids"] == [11]
    assert values["option_trade_ids"] == [11]
    assert values["lane_by_trade_id"] == {"10": "pattern_linked", "11": "plan_levels"}


def test_normalize_json_handles_mixed_storage() -> None:
    module = _load_module()

    assert module._normalize_json('{"breakout_alert":{"asset_type":"options"}}') == {
        "breakout_alert": {"asset_type": "options"}
    }
    assert module._normalize_json("not-json") == {}
    assert module._normalize_json(None) == {}
