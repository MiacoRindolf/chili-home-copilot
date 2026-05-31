from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5o-autotrader-desk-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_autotrader_desk_probe", SCRIPT_PATH)
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


def test_probe_user_id_prefers_explicit_override(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.setenv(module.PROBE_USER_ID_ENV, "42")

    assert module._probe_user_id(object()) == 42


def test_desk_values_capture_operator_visible_inputs() -> None:
    module = _load_module()

    rows = [
        {
            "id": 10,
            "user_id": 1,
            "ticker": "BTC-USD",
            "direction": "long",
            "entry_price": "100",
            "entry_date": "2026-05-31T12:00:00+00:00",
            "quantity": "0.2",
            "stop_loss": "90",
            "take_profit": "120",
            "scan_pattern_id": 585,
            "related_alert_id": None,
            "broker_source": "robinhood",
            "asset_kind": "crypto",
            "trade_type": "long",
            "auto_trader_version": "v1",
            "scale_in_count": 1,
            "tags": "",
            "position_id": 99,
            "indicator_snapshot": {},
        },
        {
            "id": 11,
            "user_id": 1,
            "ticker": "SPY",
            "direction": "long",
            "entry_price": "2.5",
            "entry_date": "2026-05-31T12:05:00+00:00",
            "quantity": "1",
            "stop_loss": None,
            "take_profit": None,
            "scan_pattern_id": None,
            "related_alert_id": 123,
            "broker_source": "robinhood",
            "asset_kind": "option",
            "trade_type": "option",
            "auto_trader_version": None,
            "scale_in_count": 0,
            "tags": "",
            "position_id": None,
            "indicator_snapshot": {},
        },
    ]

    values = module._desk_values_for_rows(rows)

    assert values["desk_override_keys"] == ["trade:10", "trade:11"]
    assert values["desk_scope_by_trade_id"] == {
        "10": "pattern_linked",
        "11": "pattern_linked",
    }
    assert values["desk_broker_truth_inputs"] == [
        {
            "id": 10,
            "position_id": 99,
            "ticker": "BTC-USD",
            "broker_source": "robinhood",
            "entry_price": "100",
            "quantity": "0.2",
        }
    ]
    assert values["desk_quote_inputs"] == [
        {
            "id": 10,
            "ticker": "BTC-USD",
            "broker_source": "robinhood",
            "asset_type": "crypto",
        },
        {
            "id": 11,
            "ticker": "SPY",
            "broker_source": "robinhood",
            "asset_type": "options",
        },
    ]


def test_normalize_json_handles_string_snapshot() -> None:
    module = _load_module()

    assert module._normalize_json('{"asset_type":"options"}') == {"asset_type": "options"}
    assert module._normalize_json("not json") == {}
    assert module._normalize_json(["not", "dict"]) == {}
