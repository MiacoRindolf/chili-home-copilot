from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from types import SimpleNamespace

import anyio

from app.routers.trading_sub import trades as trades_router


EXPECTED_TRADE_FIELDS = [
    "id",
    "ticker",
    "direction",
    "quantity",
    "entry_price",
    "exit_price",
    "entry_date",
    "exit_date",
    "pnl",
    "status",
    "broker_source",
    "tca_entry_slippage_bps",
    "tca_exit_slippage_bps",
    "scan_pattern_id",
    "pattern_tags",
]


class _RowsResult:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


class _AuditExportDb:
    def __init__(self):
        self.sql = ""
        self.params = None
        self.trade_rows = [
            {
                "id": 42,
                "ticker": "ABC",
                "direction": "long",
                "quantity": 3,
                "entry_price": 10.0,
                "exit_price": 12.0,
                "entry_date": datetime(2026, 5, 2, 9, 30),
                "exit_date": datetime(2026, 5, 3, 10, 0),
                "pnl": 6.0,
                "status": "closed",
                "broker_source": "robinhood",
                "tca_entry_slippage_bps": 11.5,
                "tca_exit_slippage_bps": 7.25,
                "scan_pattern_id": 585,
                "pattern_tags": "compression",
            }
        ]
        self.event_rows = [
            SimpleNamespace(
                id=77,
                trade_id=42,
                event_type="fill",
                status="filled",
                event_at=datetime(2026, 5, 2, 9, 31),
                reference_price=9.95,
                average_fill_price=10.0,
                realized_slippage_bps=5.0,
                spread_bps=2.0,
                submit_to_ack_ms=120,
                execution_family="entry",
            )
        ]
        self.pattern_rows = [
            SimpleNamespace(
                id=585,
                name="Compression",
                lifecycle_stage="promoted",
                lifecycle_changed_at=datetime(2026, 5, 2, 8, 0),
                promotion_status="promoted",
                win_rate=0.35,
                oos_win_rate=0.34,
                backtest_count=86,
                origin="scanner",
            )
        ]

    def execute(self, sql, params=None):
        self.sql = str(sql)
        self.params = params
        return _RowsResult(self.trade_rows)

    def query(self, model):
        if model.__name__ == "TradingExecutionEvent":
            return _Query(self.event_rows)
        if model.__name__ == "ScanPattern":
            return _Query(self.pattern_rows)
        raise AssertionError(f"unexpected audit-export query model: {model}")


def _install_identity(monkeypatch):
    monkeypatch.setattr(
        trades_router,
        "get_identity_ctx",
        lambda _request, _db: {"user_id": 7},
    )


async def _streaming_text(response) -> str:
    chunks = []
    async for chunk in response.body_iterator:
        chunks.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk)
    return "".join(chunks)


def test_audit_export_json_shape_stays_trades_compatible(monkeypatch):
    _install_identity(monkeypatch)
    db = _AuditExportDb()

    response = trades_router.api_audit_export(
        request=object(),
        db=db,
        start="2026-05-01",
        end="2026-06-01",
        fmt="json",
    )

    payload = json.loads(response.body)
    assert payload["ok"] is True
    assert list(payload["trades"][0].keys()) == EXPECTED_TRADE_FIELDS
    assert payload["trades"][0] == {
        "id": 42,
        "ticker": "ABC",
        "direction": "long",
        "quantity": 3,
        "entry_price": 10.0,
        "exit_price": 12.0,
        "entry_date": "2026-05-02T09:30:00",
        "exit_date": "2026-05-03T10:00:00",
        "pnl": 6.0,
        "status": "closed",
        "broker_source": "robinhood",
        "tca_entry_slippage_bps": 11.5,
        "tca_exit_slippage_bps": 7.25,
        "scan_pattern_id": 585,
        "pattern_tags": "compression",
    }
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
    assert db.params["uid"] == 7


def test_audit_export_csv_shape_keeps_trades_section(monkeypatch):
    _install_identity(monkeypatch)
    db = _AuditExportDb()

    response = trades_router.api_audit_export(
        request=object(),
        db=db,
        start="2026-05-01",
        end="2026-06-01",
        fmt="csv",
    )
    body = anyio.run(_streaming_text, response)

    assert body.startswith("# TRADES\n")
    trade_section = body.split("\n# EXECUTION EVENTS\n", 1)[0]
    lines = trade_section.splitlines()
    assert lines[0] == "# TRADES"
    assert lines[1].split(",") == EXPECTED_TRADE_FIELDS
    parsed = list(csv.DictReader(io.StringIO("\n".join(lines[1:]))))
    assert parsed[0]["id"] == "42"
    assert parsed[0]["ticker"] == "ABC"
    assert parsed[0]["entry_date"] == "2026-05-02T09:30:00"
    assert "\n# PATTERN GOVERNANCE\n" in body
    assert "FROM trading_management_envelopes" in db.sql
    assert "trading_trades" not in db.sql
