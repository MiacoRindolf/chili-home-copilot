from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5o-autopilot-scope-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_autopilot_scope_probe", SCRIPT_PATH)
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


def test_scope_values_classify_live_option_and_plan_rows() -> None:
    module = _load_module()

    rows = [
        {
            "id": 1,
            "user_id": 1,
            "ticker": "AAPL",
            "status": "open",
            "broker_source": "robinhood",
            "auto_trader_version": "v1",
            "scan_pattern_id": None,
            "related_alert_id": None,
            "stop_loss": None,
            "take_profit": None,
            "asset_kind": "stock",
            "tags": "",
            "indicator_snapshot": {},
        },
        {
            "id": 2,
            "user_id": 1,
            "ticker": "SPY",
            "status": "open",
            "broker_source": "robinhood",
            "auto_trader_version": None,
            "scan_pattern_id": 585,
            "related_alert_id": None,
            "stop_loss": None,
            "take_profit": None,
            "asset_kind": "option",
            "tags": "",
            "indicator_snapshot": {},
        },
        {
            "id": 3,
            "user_id": 1,
            "ticker": "MSFT",
            "status": "open",
            "broker_source": "robinhood",
            "auto_trader_version": None,
            "scan_pattern_id": None,
            "related_alert_id": None,
            "stop_loss": "95",
            "take_profit": None,
            "asset_kind": "stock",
            "tags": "",
            "indicator_snapshot": {},
        },
    ]

    values = module._scope_values_for_rows(rows)

    assert values["live_autopilot_trade_ids"] == [1, 2, 3]
    assert values["option_trade_ids"] == [2]
    assert values["scope_by_trade_id"] == {
        "1": "autotrader_v1",
        "2": "pattern_linked",
        "3": "plan_levels",
    }


def test_count_values_capture_v1_owner_symbols() -> None:
    module = _load_module()

    rows = [
        {"user_id_key": 1, "ticker": "AAPL", "v1_open_trades": 2},
        {"user_id_key": -1, "ticker": "BTC-USD", "v1_open_trades": 1},
    ]

    values = module._count_values_for_rows(rows)

    assert values["v1_open_counts_by_user_symbol"] == {
        "1:AAPL": 2,
        "-1:BTC-USD": 1,
    }
    assert values["v1_owned_symbols"] == ["-1:BTC-USD", "1:AAPL"]


def test_normalize_json_handles_string_snapshot() -> None:
    module = _load_module()

    assert module._normalize_json('{"asset_type":"options"}') == {"asset_type": "options"}
    assert module._normalize_json("not json") == {}
    assert module._normalize_json(["not", "dict"]) == {}
