from __future__ import annotations

from app.services.trading.options.quote_store import (
    create_chain_snapshot,
    record_quote_snapshot,
)


class _Result:
    def __init__(self, row=None):
        self._row = row

    def first(self):
        return self._row


class _FakeDb:
    def __init__(self):
        self.calls = []

    def execute(self, stmt, params):
        sql = str(stmt)
        self.calls.append((sql, dict(params)))
        if "RETURNING id" in sql:
            return _Result((123,))
        return _Result()


class _Scope:
    def __init__(self, db):
        self.db = db

    def __enter__(self):
        self.db.entered += 1
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type is not None:
            self.db.rolled_back += 1
        else:
            self.db.released += 1
        return False


class _SavepointDb(_FakeDb):
    def __init__(self, *, fail: bool = False):
        super().__init__()
        self.fail = fail
        self.entered = 0
        self.released = 0
        self.rolled_back = 0

    def begin_nested(self):
        return _Scope(self)

    def execute(self, stmt, params):
        if self.fail:
            raise RuntimeError("boom")
        return super().execute(stmt, params)


def test_create_chain_snapshot_writes_lightweight_chain_row() -> None:
    db = _FakeDb()

    chain_id = create_chain_snapshot(
        db,
        underlying="spy",
        expiration="20260619",
        venue="Robinhood",
        spot_price=715.37,
        n_contracts=7,
    )

    assert chain_id == 123
    sql, params = db.calls[0]
    assert "INSERT INTO options_chains" in sql
    assert params["underlying"] == "SPY"
    assert params["venue"] == "robinhood"
    assert params["expirations_json"] == '["2026-06-19"]'
    assert params["spot_price"] == 715.37


def test_create_chain_snapshot_sanitizes_bad_numeric_telemetry() -> None:
    db = _FakeDb()

    chain_id = create_chain_snapshot(
        db,
        underlying="spy",
        expiration="20260619",
        venue="Robinhood",
        spot_price=float("inf"),
        n_contracts=True,
    )

    assert chain_id == 123
    _sql, params = db.calls[0]
    assert params["spot_price"] is None
    assert params["n_contracts"] is None


def test_record_quote_snapshot_writes_tradeable_quote_row() -> None:
    db = _FakeDb()

    ok = record_quote_snapshot(
        db,
        chain_id=123,
        option_meta={
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
        },
        quote={
            "bid_price": "3.95",
            "ask_price": "4.05",
            "mark_price": "4.00",
            "implied_volatility": "0.23",
            "open_interest": "1200",
            "volume": "340",
            "delta": "0.42",
            "gamma": "0.03",
            "theta": "-0.08",
            "vega": "0.11",
            "rho": "0.02",
        },
    )

    assert ok is True
    sql, params = db.calls[0]
    assert "INSERT INTO options_quotes" in sql
    assert params["chain_id"] == 123
    assert params["occ_symbol"] == "SPY260619C00729000"
    assert params["bid"] == 3.95
    assert params["ask"] == 4.05
    assert params["last"] == 4.0
    assert params["implied_vol"] == 0.23
    assert params["open_interest"] == 1200


def test_record_quote_snapshot_persists_nested_broker_greeks() -> None:
    db = _FakeDb()

    ok = record_quote_snapshot(
        db,
        chain_id=123,
        option_meta={
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
        },
        quote={
            "bid_price": "3.95",
            "ask_price": "4.05",
            "mark_price": "4.00",
            "greeks": {
                "delta": "0.42",
                "gamma": "0.03",
                "theta": "-0.08",
                "vega": "0.11",
                "rho": "0.02",
            },
        },
    )

    assert ok is True
    _sql, params = db.calls[0]
    assert params["delta"] == 0.42
    assert params["gamma"] == 0.03
    assert params["theta"] == -0.08
    assert params["vega"] == 0.11
    assert params["rho"] == 0.02


def test_record_quote_snapshot_rejects_crossed_premium_quote() -> None:
    db = _FakeDb()

    ok = record_quote_snapshot(
        db,
        chain_id=123,
        option_meta={
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
        },
        quote={"bid_price": "4.10", "ask_price": "4.00", "mark_price": "4.05"},
    )

    assert ok is False
    assert db.calls == []


def test_record_quote_snapshot_rejects_quote_without_positive_premium() -> None:
    db = _FakeDb()

    ok = record_quote_snapshot(
        db,
        chain_id=123,
        option_meta={
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
        },
        quote={"bid_price": "0", "ask_price": "0", "mark_price": "0"},
    )

    assert ok is False
    assert db.calls == []


def test_record_quote_snapshot_rejects_boolean_premium_quote() -> None:
    db = _FakeDb()

    ok = record_quote_snapshot(
        db,
        chain_id=123,
        option_meta={
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
        },
        quote={"bid_price": True, "ask_price": False, "mark_price": False},
    )

    assert ok is False
    assert db.calls == []


def test_record_quote_snapshot_ignores_bad_quote_metrics_but_keeps_valid_premium() -> None:
    db = _FakeDb()

    ok = record_quote_snapshot(
        db,
        chain_id=123,
        option_meta={
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
        },
        quote={
            "bid_price": True,
            "ask_price": "4.05",
            "mark_price": "4.00",
            "implied_volatility": float("inf"),
            "open_interest": True,
            "volume": "12.5",
            "greeks": {
                "delta": True,
                "gamma": float("nan"),
                "theta": "-0.08",
                "vega": "0.11",
                "rho": True,
            },
        },
    )

    assert ok is True
    _sql, params = db.calls[0]
    assert params["bid"] is None
    assert params["ask"] == 4.05
    assert params["last"] == 4.0
    assert params["implied_vol"] is None
    assert params["open_interest"] is None
    assert params["volume"] is None
    assert params["delta"] is None
    assert params["gamma"] is None
    assert params["theta"] == -0.08
    assert params["vega"] == 0.11
    assert params["rho"] is None


def test_record_quote_snapshot_is_best_effort_for_incomplete_meta() -> None:
    db = _FakeDb()

    ok = record_quote_snapshot(
        db,
        chain_id=123,
        option_meta={"underlying": "SPY"},
        quote={"bid_price": "3.95", "ask_price": "4.05"},
    )

    assert ok is False
    assert db.calls == []


def test_quote_snapshot_uses_savepoint_and_returns_false_on_write_error() -> None:
    db = _SavepointDb(fail=True)

    ok = record_quote_snapshot(
        db,
        chain_id=123,
        option_meta={
            "underlying": "SPY",
            "expiration": "2026-06-19",
            "strike": 729.0,
            "option_type": "call",
        },
        quote={"bid_price": "3.95", "ask_price": "4.05"},
    )

    assert ok is False
    assert db.entered == 1
    assert db.rolled_back == 1
