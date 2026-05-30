from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.execution_audit import _event_should_require_open_position
from app.services.trading.position_resolver import resolve_position_id


class _Db:
    def __init__(self, row=None) -> None:
        self.sql = ""
        self.params = {}
        self.row = row

    def execute(self, stmt, params):
        self.sql = str(stmt)
        self.params = dict(params)
        return self

    def first(self):
        return self.row


def test_option_trade_position_resolution_requires_option_position() -> None:
    db = _Db()
    trade = SimpleNamespace(
        user_id=1,
        broker_source="robinhood",
        ticker="SPY",
        direction="long",
        asset_kind="option",
        tags=None,
        indicator_snapshot={"option_meta": {"strike": 729.0}},
    )

    assert resolve_position_id(db, trade=trade) is None

    assert db.sql == ""
    assert db.params == {}


def test_equity_position_resolution_uses_account_type_natural_key() -> None:
    db = _Db(row=(42,))
    trade = SimpleNamespace(
        user_id=1,
        broker_source="robinhood",
        ticker="SPY",
        direction="long",
        account_type="margin",
        asset_kind="stock",
        tags=None,
        indicator_snapshot={},
    )

    assert resolve_position_id(db, trade=trade) == 42

    assert "AND account_type = :account_type" in db.sql
    assert db.params["account_type"] == "margin"


def test_equity_position_resolution_defaults_coinbase_to_spot_account() -> None:
    db = _Db(row=(7,))

    assert (
        resolve_position_id(
            db,
            user_id=1,
            broker_source="coinbase",
            ticker="BTC-USD",
            direction="long",
        )
        == 7
    )

    assert db.params["account_type"] == "spot"


def test_open_only_position_resolution_filters_closed_identities() -> None:
    db = _Db(row=None)

    assert (
        resolve_position_id(
            db,
            user_id=1,
            broker_source="coinbase",
            ticker="ACX-USD",
            direction="long",
            open_only=True,
        )
        is None
    )

    assert "AND state = 'open'" in db.sql


def test_active_order_events_require_open_position_identity() -> None:
    assert _event_should_require_open_position(
        event_type="ack",
        status="open",
        cumulative_filled_quantity=0,
        payload_json={"side": "buy"},
    )
    assert _event_should_require_open_position(
        event_type="status",
        status="cancelled",
        cumulative_filled_quantity=0,
        payload_json={},
    )
    assert not _event_should_require_open_position(
        event_type="fill",
        status="filled",
        cumulative_filled_quantity=10,
        payload_json={"side": "sell"},
    )
