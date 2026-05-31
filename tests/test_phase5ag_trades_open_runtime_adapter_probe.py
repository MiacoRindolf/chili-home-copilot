from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5ag-trades-open-runtime-adapter-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5ag_trades_open_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_public_open_trade_fields_pin_trades_contract() -> None:
    module = _load_module()

    assert module.PUBLIC_OPEN_TRADE_FIELDS == (
        "id",
        "ticker",
        "direction",
        "entry_price",
        "exit_price",
        "quantity",
        "local_entry_price",
        "local_quantity",
        "entry_date",
        "exit_date",
        "status",
        "pnl",
        "tags",
        "notes",
        "broker_source",
        "broker_status",
        "broker_order_id",
        "filled_at",
        "avg_fill_price",
        "tca_reference_entry_price",
        "tca_entry_slippage_bps",
        "tca_reference_exit_price",
        "tca_exit_slippage_bps",
        "strategy_proposal_id",
        "scan_pattern_id",
        "position_id",
        "broker_truth_entry_price",
        "broker_truth_quantity",
        "broker_truth_position_id",
        "broker_truth_current_envelope_id",
        "broker_truth_metrics_source",
    )


def test_normalize_stabilizes_dates_decimals_and_nested_values() -> None:
    module = _load_module()

    assert module._normalize(
        {
            "ts": datetime(2026, 5, 31, 1, 15),
            "px": Decimal("4.2500"),
            "nested": [{"b": 2, "a": 1}],
        }
    ) == {
        "nested": [{"a": 1, "b": 2}],
        "px": 4.25,
        "ts": "2026-05-31T01:15:00",
    }


def test_load_envelope_objects_reads_management_envelopes_not_compat_view() -> None:
    module = _load_module()

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return [{"id": 1, "ticker": "ABC", "status": "open"}]

    class _Db:
        sql = ""
        params = None

        def execute(self, sql, params=None):
            self.sql = str(sql)
            self.params = params
            return _Rows()

    db = _Db()
    rows = module.load_envelope_objects(db, user_id=7)

    assert rows[0].id == 1
    assert rows[0].ticker == "ABC"
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'open'" in db.sql
    assert db.params == {"uid": 7}


def test_serialize_open_rows_preserves_broker_truth_overlay(monkeypatch) -> None:
    module = _load_module()
    trade = SimpleNamespace(
        id=42,
        ticker="ABC",
        direction="long",
        entry_price=10.0,
        exit_price=None,
        quantity=4.0,
        entry_date=datetime(2026, 5, 31, 1, 30),
        exit_date=None,
        status="open",
        pnl=None,
        tags="alpha",
        notes=None,
        broker_source="robinhood",
        broker_status="filled",
        broker_order_id="ord-1",
        filled_at=datetime(2026, 5, 31, 1, 31),
        avg_fill_price=10.0,
        tca_reference_entry_price=9.9,
        tca_entry_slippage_bps=10.0,
        tca_reference_exit_price=None,
        tca_exit_slippage_bps=None,
        strategy_proposal_id=5,
        scan_pattern_id=585,
        position_id=99,
    )

    monkeypatch.setattr(
        "app.services.trading.broker_position_truth.filter_broker_stale_open_trades",
        lambda _db, rows: (rows, []),
    )
    monkeypatch.setattr(
        "app.services.trading.broker_position_truth.broker_position_display_metrics",
        lambda _db, _trade: {
            "entry_price": 10.25,
            "quantity": 4.5,
            "position_id": 99,
            "current_envelope_id": 42,
            "source": "broker_position_identity",
        },
    )

    payload = module.serialize_open_trades_api_rows(SimpleNamespace(), [trade])

    assert payload["suppressed_stale_count"] == 0
    assert payload["trades"][0]["entry_price"] == 10.25
    assert payload["trades"][0]["quantity"] == 4.5
    assert payload["trades"][0]["local_entry_price"] == 10.0
    assert payload["trades"][0]["local_quantity"] == 4.0
    assert payload["trades"][0]["broker_truth_metrics_source"] == (
        "broker_position_identity"
    )


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

    module._assert_probe_database_allowed(
        "postgresql://chili:chili@localhost:5433/chili_test"
    )


def test_probe_live_opt_in_allows_non_test_database(monkeypatch) -> None:
    module = _load_module()

    monkeypatch.setenv(module.LIVE_PROBE_OPT_IN, "true")

    module._assert_probe_database_allowed(
        "postgresql://chili:chili@localhost:5433/chili"
    )
