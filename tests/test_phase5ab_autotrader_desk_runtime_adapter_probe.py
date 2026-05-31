from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5ab-autotrader-desk-runtime-adapter-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5ab_desk_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_desk_trade_fields_pin_public_live_row_contract() -> None:
    module = _load_module()

    assert module.DESK_TRADE_FIELDS == (
        "kind",
        "id",
        "ticker",
        "direction",
        "entry_price",
        "entry_date",
        "quantity",
        "stop_loss",
        "take_profit",
        "scan_pattern_id",
        "pattern_name",
        "monitor_scope",
        "related_alert_id",
        "broker_source",
        "asset_type",
        "auto_trader_v1",
        "scale_in_count",
        "tags",
        "overrides",
        "opened_today_et",
        "controls_supported",
        "close_supported",
        "current_price",
        "unrealized_pnl_usd",
        "unrealized_pnl_pct",
        "quote_source",
        "broker_truth_entry_price",
        "broker_truth_quantity",
        "broker_truth_position_id",
        "broker_truth_current_envelope_id",
        "broker_truth_metrics_source",
    )


def test_normalize_stabilizes_probe_payload_values() -> None:
    module = _load_module()

    assert module._normalize(
        {
            "ts": datetime(2026, 5, 30, 22, 40),
            "px": Decimal("12.3400"),
            "nested": [{"b": 2, "a": 1}],
        }
    ) == {
        "nested": [{"a": 1, "b": 2}],
        "px": 12.34,
        "ts": "2026-05-30T22:40:00",
    }


def test_load_envelope_objects_reads_management_envelopes_not_compat_view(monkeypatch) -> None:
    module = _load_module()

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return [{"id": 1, "ticker": "ABC", "entry_price": 10.0}]

    class _Db:
        sql = ""
        params = None

        def execute(self, sql, params=None):
            self.sql = str(sql)
            self.params = params
            return _Rows()

    monkeypatch.setattr(
        module,
        "filter_broker_stale_open_trades",
        lambda _db, rows: (rows, []),
    )
    monkeypatch.setattr(
        module,
        "broker_stale_open_trade_snapshot",
        lambda _db, _row, grace_seconds=0: None,
    )
    db = _Db()

    rows, suppressed = module.load_envelope_objects(db, user_id=7)

    assert suppressed == []
    assert rows[0].id == 1
    assert rows[0].ticker == "ABC"
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'open'" in db.sql
    assert "auto_trader_version = 'v1'" in db.sql
    assert "scan_pattern_id IS NOT NULL" in db.sql
    assert "related_alert_id IS NOT NULL" in db.sql
    assert "stop_loss IS NOT NULL" in db.sql
    assert "take_profit IS NOT NULL" in db.sql
    assert "ORDER BY id DESC" in db.sql
    assert db.params == {"uid": 7}


def test_probe_rejects_non_test_database_without_live_opt_in(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)

    try:
        module._assert_probe_database_allowed(
            "postgresql://chili:chili@localhost:5433/chili"
        )
    except RuntimeError as exc:
        assert module.LIVE_PROBE_OPT_IN in str(exc)
    else:
        raise AssertionError("expected non-test database to require live opt-in")


def test_probe_allows_test_database_without_live_opt_in(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.delenv(module.LIVE_PROBE_OPT_IN, raising=False)

    assert (
        module._assert_probe_database_allowed(
            "postgresql://chili:chili@localhost:5433/chili_test"
        )
        == "test"
    )


def test_probe_live_opt_in_allows_non_test_database(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.setenv(module.LIVE_PROBE_OPT_IN, "true")

    assert (
        module._assert_probe_database_allowed(
            "postgresql://chili:chili@localhost:5433/chili"
        )
        == "live_or_non_test"
    )
