from __future__ import annotations

from types import SimpleNamespace

from app.models.trading import BreakoutAlert, ScanPattern
from app.services.trading import auto_trader_monitor


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeDb:
    def __init__(self, *, alert_rows, pattern_rows):
        self.alert_rows = alert_rows
        self.pattern_rows = pattern_rows
        self.query_calls = []
        self.get_calls = []
        self.added = []
        self.commits = 0

    def query(self, *args):
        self.query_calls.append(args)
        keys = tuple(getattr(arg, "key", None) for arg in args)
        if keys == ("id", "stop_loss", "target_price"):
            return _FakeQuery(self.alert_rows)
        if keys == ("id", "rules_json"):
            return _FakeQuery(self.pattern_rows)
        raise AssertionError(f"unexpected query shape: {keys!r}")

    def get(self, model, row_id):
        self.get_calls.append((model, row_id))
        return None

    def add(self, row):
        self.added.append(row)

    def commit(self):
        self.commits += 1


def _trade(
    row_id: int,
    *,
    related_alert_id=None,
    scan_pattern_id=None,
    direction="long",
    stop_loss=None,
    take_profit=None,
):
    return SimpleNamespace(
        id=row_id,
        ticker=f"T{row_id}",
        entry_price=100.0,
        direction=direction,
        stop_loss=stop_loss,
        take_profit=take_profit,
        related_alert_id=related_alert_id,
        scan_pattern_id=scan_pattern_id,
    )


def test_seed_missing_levels_uses_bulk_alert_and_pattern_lookups() -> None:
    trades = [
        _trade(1, related_alert_id=5, scan_pattern_id=10),
        _trade(2, scan_pattern_id=10),
        _trade(3, scan_pattern_id=11, direction="short"),
    ]
    db = _FakeDb(
        alert_rows=[SimpleNamespace(id=5, stop_loss=94.0, target_price=113.0)],
        pattern_rows=[
            (10, {"exits": {"stop_pct": 5, "target_pct": 10}}),
            (11, {"exits": {"stop_pct": 5, "target_pct": 10}}),
        ],
    )

    auto_trader_monitor._seed_missing_levels(db, trades)

    query_keys = [tuple(getattr(arg, "key", None) for arg in call) for call in db.query_calls]
    assert query_keys == [
        ("id", "stop_loss", "target_price"),
        ("id", "rules_json"),
    ]
    assert db.get_calls == []
    assert db.commits == 1
    assert len(db.added) == 3
    assert (trades[0].stop_loss, trades[0].take_profit) == (94.0, 113.0)
    assert (trades[1].stop_loss, trades[1].take_profit) == (95.0, 110.0)
    assert (trades[2].stop_loss, trades[2].take_profit) == (105.0, 90.0)


def test_seed_missing_levels_skips_bulk_queries_when_rows_have_levels() -> None:
    trade = _trade(1, related_alert_id=5, scan_pattern_id=10, stop_loss=95.0, take_profit=110.0)
    db = _FakeDb(alert_rows=[], pattern_rows=[])

    auto_trader_monitor._seed_missing_levels(db, [trade])

    assert db.query_calls == []
    assert db.get_calls == []
    assert db.commits == 0
