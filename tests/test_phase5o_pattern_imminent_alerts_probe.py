from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5o-pattern-imminent-alerts-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_pattern_imminent_probe", SCRIPT_PATH)
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


def test_control_values_capture_deflection_keys() -> None:
    module = _load_module()

    rows = [
        {
            "id": 10,
            "user_id": 1,
            "scan_pattern_id": 585,
            "ticker": "aapl",
            "status": "open",
            "auto_trader_version": "v1",
            "broker_source": "robinhood",
        },
        {
            "id": 11,
            "user_id": None,
            "scan_pattern_id": 586,
            "ticker": "btc-usd",
            "status": "working",
            "auto_trader_version": "v1",
            "broker_source": "coinbase",
        },
    ]

    values = module._control_values_for_rows(rows)

    assert values["deflection_trade_ids"] == [10, 11]
    assert values["pattern_ticker_keys"] == ["585:AAPL", "586:BTC-USD"]
    assert values["user_pattern_ticker_keys"] == ["-1:586:BTC-USD", "1:585:AAPL"]
    assert values["keys_by_user"] == {
        "-1": ["586:BTC-USD"],
        "1": ["585:AAPL"],
    }
    assert values["deflection_row_fingerprints"][0] == {
        "id": 10,
        "user_id": 1,
        "scan_pattern_id": 585,
        "ticker": "aapl",
        "status": "open",
        "auto_trader_version": "v1",
        "broker_source": "robinhood",
    }


def test_deflection_key_skips_invalid_rows() -> None:
    module = _load_module()

    assert module._pattern_ticker_key({"scan_pattern_id": None, "ticker": "AAPL"}) is None
    assert module._pattern_ticker_key({"scan_pattern_id": 585, "ticker": ""}) is None
    assert module._pattern_ticker_key({"scan_pattern_id": 585, "ticker": "aapl"}) == "585:AAPL"
