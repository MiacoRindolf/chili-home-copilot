from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5ag-position-overrides-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5ag_position_overrides_probe", SCRIPT_PATH)
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


def test_control_values_classify_close_adopt_and_unadopt_candidates() -> None:
    module = _load_module()

    rows = [
        {
            "id": 1,
            "user_id": 1,
            "ticker": "AAPL",
            "status": "open",
            "direction": "long",
            "broker_source": "robinhood",
            "auto_trader_version": "v1",
            "management_scope": "adopted_position",
            "scan_pattern_id": None,
            "related_alert_id": None,
            "stop_loss": None,
            "take_profit": None,
            "quantity": "2",
            "entry_price": "100",
            "asset_kind": "stock",
            "tags": "",
            "indicator_snapshot": {},
        },
        {
            "id": 2,
            "user_id": 1,
            "ticker": "MSFT",
            "status": "open",
            "direction": "long",
            "broker_source": "robinhood",
            "auto_trader_version": None,
            "management_scope": "pattern_linked",
            "scan_pattern_id": 585,
            "related_alert_id": None,
            "stop_loss": "95",
            "take_profit": None,
            "quantity": "3",
            "entry_price": "100",
            "asset_kind": "stock",
            "tags": "",
            "indicator_snapshot": {},
        },
        {
            "id": 3,
            "user_id": 1,
            "ticker": "SPY",
            "status": "open",
            "direction": "long",
            "broker_source": "robinhood",
            "auto_trader_version": None,
            "management_scope": "pattern_linked",
            "scan_pattern_id": None,
            "related_alert_id": 10,
            "stop_loss": None,
            "take_profit": None,
            "quantity": "1",
            "entry_price": "1.25",
            "asset_kind": "option",
            "tags": "option",
            "indicator_snapshot": {},
        },
    ]

    values = module._control_values_for_rows(rows)

    assert values["close_candidate_ids"] == [1, 2, 3]
    assert values["close_spot_candidate_ids"] == [1, 2]
    assert values["close_option_candidate_ids"] == [3]
    assert values["close_good_qty_candidate_ids"] == [1, 2, 3]
    assert values["adopt_candidate_ids"] == [2, 3]
    assert values["unadopt_candidate_ids"] == [1]


def test_trade_override_slice_parser_is_strict() -> None:
    module = _load_module()

    row = {
        "slice_name": "autotrader_v1_position:trade:42",
        "payload_json": {"monitor_paused": True, "synergy_excluded": False},
    }
    assert module._parse_trade_override_slice(row) == {
        "trade_id": 42,
        "monitor_paused": True,
        "synergy_excluded": False,
    }
    assert module._parse_trade_override_slice(
        {"slice_name": "autotrader_v1_position:paper:42", "payload_json": {}}
    ) is None
