from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5o-auto-trader-synergy-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_auto_trader_synergy_probe", SCRIPT_PATH)
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


def test_selected_by_user_ticker_uses_highest_envelope_id() -> None:
    module = _load_module()

    rows = [
        {"id": 10, "user_id": 1, "ticker": "aapl"},
        {"id": 12, "user_id": 1, "ticker": "AAPL"},
        {"id": 11, "user_id": 2, "ticker": "AAPL"},
        {"id": 13, "user_id": None, "ticker": "AAPL"},
    ]

    selected = module._selected_by_user_ticker(rows)

    assert selected["1:AAPL"]["id"] == 12
    assert selected["2:AAPL"]["id"] == 11
    assert selected["NULL:AAPL"]["id"] == 13


def test_control_values_capture_synergy_scale_in_decision_inputs() -> None:
    module = _load_module()

    rows = [
        {
            "id": 21,
            "user_id": 1,
            "ticker": "AAPL",
            "status": "open",
            "broker_source": "robinhood",
            "auto_trader_version": "v1",
            "scan_pattern_id": 585,
            "scale_in_count": 1,
            "stop_loss": "95",
            "take_profit": "120",
            "entry_price": "100",
            "quantity": "2",
            "asset_kind": "stock",
            "tags": "",
            "indicator_snapshot": {
                module.SCALE_IN_PATTERN_IDS_SNAPSHOT_KEY: [537, "586", "bad"],
                module.SCALE_IN_ALERT_IDS_SNAPSHOT_KEY: [1001],
            },
        },
        {
            "id": 22,
            "user_id": 1,
            "ticker": "SPY",
            "status": "working",
            "broker_source": "robinhood",
            "auto_trader_version": "v1",
            "scan_pattern_id": 1011,
            "scale_in_count": 0,
            "stop_loss": "1",
            "take_profit": "3",
            "entry_price": "2",
            "quantity": "1",
            "asset_kind": "option",
            "tags": "option",
            "indicator_snapshot": {},
        },
    ]

    values = module._control_values_for_rows(
        rows,
        legacy_scan_pattern_ids_by_alert_id={1001: 1204},
        synergy_excluded_trade_ids=[21],
    )

    assert values["v1_pair_keys"] == ["1:AAPL", "1:SPY"]
    assert values["selected_trade_ids_by_pair"] == {"1:AAPL": 21, "1:SPY": 22}
    assert values["selected_spot_trade_ids"] == [21]
    assert values["selected_option_trade_ids"] == [22]
    assert values["scale_in_count_by_selected_id"] == {"21": 1, "22": 0}
    assert values["used_scale_in_pattern_ids_by_selected_id"]["21"] == [537, 586, 1204]
    assert values["synergy_excluded_selected_trade_ids"] == [21]


def test_all_scale_in_alert_ids_reads_normalized_snapshot() -> None:
    module = _load_module()

    rows = [
        {"indicator_snapshot": {module.SCALE_IN_ALERT_IDS_SNAPSHOT_KEY: [1, "2", "x"]}},
        {"indicator_snapshot": f'{{"{module.SCALE_IN_ALERT_IDS_SNAPSHOT_KEY}": [3]}}'},
        {"indicator_snapshot": None},
    ]

    assert module._all_scale_in_alert_ids(rows) == {1, 2, 3}
