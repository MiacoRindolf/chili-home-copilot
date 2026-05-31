from __future__ import annotations

import importlib.util
import os
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5ad-alerts-envelope-parity-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5ad_alerts_probe", SCRIPT_PATH)
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


def test_sector_cap_counts_use_alerts_sector_mapping(monkeypatch) -> None:
    module = _load_module()

    class FakeDb:
        def execute(self, *_args, **_kwargs):
            class Result:
                def mappings(self):
                    return self

                def all(self):
                    return [
                        {"user_id": 1, "ticker": "AAPL"},
                        {"user_id": 1, "ticker": "MSFT"},
                        {"user_id": 1, "ticker": "UNKNOWN"},
                        {"user_id": 2, "ticker": "AAPL"},
                    ]

            return Result()

    monkeypatch.setitem(module.TICKER_TO_SECTOR, "AAPL", "technology")
    monkeypatch.setitem(module.TICKER_TO_SECTOR, "MSFT", "technology")

    assert module._sector_cap_counts(FakeDb(), relation_name=module.LEGACY_TRADES_COMPAT_RELATION) == [
        {"user_id": 1, "sector": "technology", "n": 2},
        {"user_id": 1, "sector": "unknown", "n": 1},
        {"user_id": 2, "sector": "technology", "n": 1},
    ]
