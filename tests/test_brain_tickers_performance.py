from __future__ import annotations

from datetime import datetime

from app.routers.trading_sub import scanning


def test_cached_brain_tickers_payload_reuses_fetch_and_returns_copies(monkeypatch) -> None:
    calls = {"count": 0}
    payload = {
        "crypto": [{"ticker": "BTC-USD", "score": 92.4}],
        "stocks": [{"ticker": "NVDA", "score": 88.1}],
    }

    def fake_fetch(_db):
        calls["count"] += 1
        return payload

    monkeypatch.setattr(scanning, "_brain_tickers_cache", None)
    monkeypatch.setattr(scanning, "_fetch_brain_tickers_payload", fake_fetch)

    first = scanning._cached_brain_tickers_payload(db=object())
    first["crypto"][0]["score"] = 1.0
    second = scanning._cached_brain_tickers_payload(db=object())

    assert calls["count"] == 1
    assert second == payload


def test_cached_brain_tickers_payload_expires(monkeypatch) -> None:
    calls = {"count": 0}
    clock = {"now": 100.0}

    def fake_fetch(_db):
        calls["count"] += 1
        return {"crypto": [], "stocks": [{"ticker": f"T{calls['count']}", "score": 1.0}]}

    monkeypatch.setattr(scanning, "_brain_tickers_cache", None)
    monkeypatch.setattr(scanning, "_fetch_brain_tickers_payload", fake_fetch)
    monkeypatch.setattr(scanning.time, "monotonic", lambda: clock["now"])

    first = scanning._cached_brain_tickers_payload(db=object())
    clock["now"] += scanning._BRAIN_TICKERS_CACHE_TTL_SECONDS + 0.1
    second = scanning._cached_brain_tickers_payload(db=object())

    assert calls["count"] == 2
    assert first["stocks"][0]["ticker"] == "T1"
    assert second["stocks"][0]["ticker"] == "T2"


class _FakeQuery:
    def __init__(self, *, scalar_value=None, rows=()):
        self.scalar_value = scalar_value
        self.rows = list(rows)
        self.filters = []

    def scalar(self):
        return self.scalar_value

    def filter(self, expr):
        self.filters.append(expr)
        return self

    def group_by(self, *_args):
        return self

    def order_by(self, *_args):
        return self

    def limit(self, _n):
        return self

    def all(self):
        return self.rows


class _FakeBrainTickersDb:
    def __init__(self):
        self.queries = [
            _FakeQuery(scalar_value=datetime(2026, 5, 30, 12, 0, 0)),
            _FakeQuery(rows=[("BTC-USD", 92.44)]),
            _FakeQuery(rows=[("NVDA", 88.14)]),
        ]
        self.seen_queries = []

    def query(self, *_args):
        query = self.queries.pop(0)
        self.seen_queries.append(query)
        return query


def test_fetch_brain_tickers_payload_applies_recent_window_to_both_classes() -> None:
    db = _FakeBrainTickersDb()

    payload = scanning._fetch_brain_tickers_payload(db)

    assert payload == {
        "crypto": [{"ticker": "BTC-USD", "score": 92.4}],
        "stocks": [{"ticker": "NVDA", "score": 88.1}],
    }
    assert len(db.seen_queries) == 3
    assert len(db.seen_queries[1].filters) == 2
    assert len(db.seen_queries[2].filters) == 2


def test_fetch_brain_tickers_payload_empty_without_snapshots() -> None:
    class EmptyDb:
        def query(self, *_args):
            return _FakeQuery(scalar_value=None)

    assert scanning._fetch_brain_tickers_payload(EmptyDb()) == {"crypto": [], "stocks": []}
