from __future__ import annotations

import importlib.util
import sys
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = (
    REPO_ROOT
    / "scripts"
    / "d-phase5o-position-plan-generator-envelope-parity-probe.py"
)


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5o_position_plan_probe", SCRIPT_PATH)
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


def test_static_quote_inputs_split_options_and_market_quotes() -> None:
    module = _load_module()

    rows = [
        module.SimpleNamespace(
            id=1,
            ticker="AAPL",
            entry_price="100",
            asset_kind="stock",
            tags="",
            indicator_snapshot={},
        ),
        module.SimpleNamespace(
            id=2,
            ticker="SPY",
            entry_price="2.5",
            asset_kind="option",
            tags="",
            indicator_snapshot={},
        ),
    ]

    quotes, trade_quotes = module._static_quote_inputs(rows)

    assert quotes == {"AAPL": {"price": 100.0, "source": "phase5o_probe_static_entry"}}
    assert trade_quotes == {2: {"price": 2.5, "source": "phase5o_probe_static_entry"}}


def test_plan_values_capture_cache_quote_and_context_inputs(monkeypatch) -> None:
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
            "sector": "crypto",
            "trade_type": "swing",
            "notes": "watch",
            "asset_kind": "crypto",
            "tags": "",
            "indicator_snapshot": {},
        }
    ]

    def fake_build_context(_db, trades, quotes, trade_quotes):
        assert [t.id for t in trades] == [10]
        assert quotes == {"BTC-USD": {"price": 100.0, "source": "phase5o_probe_static_entry"}}
        assert trade_quotes == {}
        return [
            {
                "trade_id": 10,
                "ticker": "BTC-USD",
                "asset_type": "crypto",
                "direction": "long",
                "entry_price": 100.0,
                "current_price": 100.0,
                "quantity": 0.2,
                "entry_value_usd": 20.0,
                "current_value_usd": 20.0,
                "unrealized_pnl_usd": 0.0,
                "stop_loss": 90.0,
                "take_profit": 120.0,
                "sector": "crypto",
                "trade_type": "swing",
                "bars_held": 1,
            }
        ]

    monkeypatch.setattr(module, "_build_position_context", fake_build_context)

    values = module._plan_values_for_rows(object(), rows)

    assert values["plan_cache_trade_ids"] == [10]
    assert values["plan_quote_inputs"] == {
        "market_quote_tickers": ["BTC-USD"],
        "option_quote_trade_ids": [],
    }
    assert values["plan_context_rows"][0]["trade_id"] == 10
    assert values["plan_context_rows"][0]["has_bars_held"] is True
