from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from decimal import Decimal
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "d-phase5z-stop-position-runtime-adapter-probe.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("phase5z_stop_position_probe", SCRIPT_PATH)
    assert spec is not None
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_envelope_runtime_exposes_trade_like_attributes() -> None:
    module = _load_module()

    runtime = module._envelope_runtime(
        {
            "id": 12,
            "ticker": "ABC",
            "entry_date": datetime(2026, 5, 30, 20, 45),
            "indicator_snapshot": {"asset_type": "stock"},
        }
    )

    assert runtime.id == 12
    assert runtime.ticker == "ABC"
    assert runtime.indicator_snapshot == {"asset_type": "stock"}


def test_load_envelope_objects_reads_management_envelopes_not_compat_view() -> None:
    module = _load_module()

    class _Rows:
        def mappings(self):
            return self

        def all(self):
            return [{"id": 1, "ticker": "ABC"}]

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
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert "status = 'open'" in db.sql
    assert db.params == {"uid": 7}


def test_normalize_stabilizes_probe_payload_values() -> None:
    module = _load_module()
    value = {
        "ts": datetime(2026, 5, 30, 20, 45),
        "px": Decimal("12.3400"),
        "nested": [{"b": 2, "a": 1}],
    }

    assert module._normalize(value) == {
        "nested": [{"a": 1, "b": 2}],
        "px": 12.34,
        "ts": "2026-05-30T20:45:00",
    }


def test_public_position_fields_include_stop_ui_contract() -> None:
    module = _load_module()

    assert module.PUBLIC_POSITION_FIELDS == (
        "id",
        "ticker",
        "asset_type",
        "direction",
        "entry_price",
        "current_price",
        "stop_loss",
        "take_profit",
        "trail_stop",
        "high_watermark",
        "stop_model",
        "quantity",
        "broker_source",
        "broker_truth_entry_price",
        "broker_truth_quantity",
        "broker_truth_position_id",
        "broker_truth_current_envelope_id",
        "broker_truth_metrics_source",
        "R",
        "current_r",
        "stop_distance_pct",
        "pnl_pct",
        "state",
        "entry_date",
        "brain",
    )
