from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from app.models.trading import PaperTrade, ScanPattern
from app.services.trading import auto_trader_position_overrides, autotrader_desk, paper_trading


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def order_by(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, *, paper_rows, pattern_rows):
        self.paper_rows = paper_rows
        self.pattern_rows = pattern_rows
        self.query_calls = []
        self.get_calls = []

    def query(self, *args):
        self.query_calls.append(args)
        if len(args) == 1 and args[0] is PaperTrade:
            return _FakeQuery(self.paper_rows)
        keys = tuple(getattr(arg, "key", None) for arg in args)
        if keys == ("id", "name"):
            return _FakeQuery(self.pattern_rows)
        raise AssertionError(f"unexpected query shape: {keys!r}")

    def get(self, model, row_id):
        self.get_calls.append((model, row_id))
        return None


def _trade(row_id: int, pattern_id: int, ticker: str):
    return SimpleNamespace(
        id=row_id,
        ticker=ticker,
        direction="long",
        entry_price=100.0,
        entry_date=datetime(2026, 6, 1, tzinfo=UTC),
        quantity=1.0,
        stop_loss=None,
        take_profit=None,
        scan_pattern_id=pattern_id,
        related_alert_id=None,
        broker_source=None,
        auto_trader_version="v1",
        scale_in_count=0,
        tags="",
        position_id=None,
        indicator_snapshot={},
        asset_kind="stock",
    )


def _paper(row_id: int, pattern_id: int, ticker: str):
    return SimpleNamespace(
        id=row_id,
        ticker=ticker,
        direction="long",
        entry_price=100.0,
        entry_date=datetime(2026, 6, 1, tzinfo=UTC),
        quantity=1.0,
        stop_price=None,
        target_price=None,
        scan_pattern_id=pattern_id,
        signal_json={"auto_trader_v1": True},
    )


def test_autotrader_desk_bulk_loads_pattern_names(monkeypatch) -> None:
    trades = [_trade(1, 10, "AAA"), _trade(2, 10, "BBB")]
    papers = [_paper(3, 11, "CCC")]
    db = _FakeDb(
        paper_rows=papers,
        pattern_rows=[(10, "Breakout"), SimpleNamespace(id=11, name="Mean Reversion")],
    )

    monkeypatch.setattr(autotrader_desk, "load_autotrader_desk_live_envelope_objects", lambda db, user_id: trades)
    monkeypatch.setattr(autotrader_desk, "filter_broker_stale_open_trades", lambda db, rows: (rows, []))
    monkeypatch.setattr(autotrader_desk, "broker_stale_open_trade_snapshot", lambda db, trade, grace_seconds=0: None)
    monkeypatch.setattr(autotrader_desk, "broker_position_display_metrics", lambda db, trade: {})
    monkeypatch.setattr(autotrader_desk, "classify_live_autopilot_trade_scope", lambda trade: "autotrader_v1")
    monkeypatch.setattr(autotrader_desk, "_broker_quote_price_for_trade", lambda trade: (101.0, "test_quote"))
    monkeypatch.setattr(autotrader_desk, "_fallback_quote", lambda ticker: 101.0)
    monkeypatch.setattr(auto_trader_position_overrides, "list_position_overrides", lambda db, pairs: {})
    monkeypatch.setattr(auto_trader_position_overrides, "_opened_today_et", lambda dt: False)
    monkeypatch.setattr(paper_trading, "_is_option_paper_trade", lambda pt: False)

    out = autotrader_desk.list_pattern_linked_open_positions(db, user_id=7)

    query_keys = [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls]
    assert query_keys == [(None,), ("id", "name")]
    assert db.query_calls[0] == (PaperTrade,)
    assert db.get_calls == []
    assert [row["pattern_name"] for row in out["trades"]] == ["Breakout", "Breakout"]
    assert [row["pattern_name"] for row in out["paper_trades"]] == ["Mean Reversion"]
