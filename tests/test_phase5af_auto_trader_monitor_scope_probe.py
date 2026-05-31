from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5af-auto-trader-monitor-scope-parity-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5af_monitor_probe", SCRIPT_PATH)
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


def test_partition_ids_match_monitor_delegation_logic() -> None:
    module = _load_module()

    rows = [
        {
            "id": 1,
            "ticker": "AAPL",
            "broker_source": "robinhood",
            "asset_kind": "stock",
            "tags": "",
            "indicator_snapshot": "{}",
            "related_alert_id": 10,
            "scan_pattern_id": None,
            "auto_trader_version": None,
            "stop_loss": None,
            "take_profit": None,
        },
        {
            "id": 2,
            "ticker": "BTC-USD",
            "broker_source": "coinbase",
            "asset_kind": "crypto",
            "tags": "",
            "indicator_snapshot": "{}",
            "related_alert_id": None,
            "scan_pattern_id": 20,
            "auto_trader_version": None,
            "stop_loss": None,
            "take_profit": None,
        },
        {
            "id": 3,
            "ticker": "SPY",
            "broker_source": "robinhood",
            "asset_kind": "option",
            "tags": "",
            "indicator_snapshot": json.dumps({"option_meta": {"contract": "x"}}),
            "related_alert_id": None,
            "scan_pattern_id": None,
            "auto_trader_version": "v1",
            "stop_loss": None,
            "take_profit": None,
        },
    ]

    assert module._partition_ids(rows) == {
        "selected_ids": [1, 2, 3],
        "option_ids": [3],
        "crypto_ids": [2],
        "equity_monitor_ids": [1],
        "scope_counts": [
            {"scope": "autotrader_v1", "n": 1},
            {"scope": "pattern_linked", "n": 2},
        ],
    }
