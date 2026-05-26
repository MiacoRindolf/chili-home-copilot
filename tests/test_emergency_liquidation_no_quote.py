"""broker-truth-self-heal (2026-05-04) -- regression test for Bug 2:
when fetch_quote returns None inside emergency_close_all, the prior
code wrote exit_price = entry_price (lying P/L). The fix sets
exit_price = None + exit_reason = '<reason>:no_quote' + leaves pnl
NULL.

Two scenarios (paper + live), both verifying the NULL path.
"""
from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import text


def test_emergency_close_all_writes_null_exit_price_when_no_quote(db):
    """Live trade: fetch_quote returns None -> exit_price IS NULL,
    exit_reason ends with ':no_quote', pnl IS NULL."""
    db.execute(text("""
        INSERT INTO trading_trades (
            id, ticker, status, broker_source, direction, quantity,
            entry_price, entry_date
        ) VALUES (
            6001, 'NOQT', 'open', 'robinhood', 'long', 10.0,
            5.0, NOW()
        ) ON CONFLICT (id) DO NOTHING
    """))
    db.commit()

    from app.services.trading import emergency_liquidation as elq

    with (
        # fetch_quote returns None -> trigger the no-quote branch.
        patch(
            "app.services.trading.market_data.fetch_quote",
            return_value=None,
        ),
        # Don't actually persist a kill-switch state side-effect to
        # disk during the test. activate_kill_switch is imported inside
        # emergency_close_all from the governance module; patch the
        # source module so the local-import binding resolves to the mock.
        patch(
            "app.services.trading.governance.activate_kill_switch",
            return_value=None,
        ),
    ):
        result = elq.emergency_close_all(db, user_id=None, reason="test_no_quote")

    assert result["ok"] is True
    assert result["closed_live"] >= 1

    row = db.execute(text(
        "SELECT status, exit_price, exit_reason, pnl "
        "FROM trading_trades WHERE id=6001"
    )).first()
    assert row[0] == "closed"
    assert row[1] is None, "exit_price must be NULL when fetch_quote returns None"
    assert row[2] is not None and row[2].endswith(":no_quote")
    assert row[3] is None, "pnl must be NULL when exit_price is NULL"


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *args, **kwargs):
        return self

    def all(self):
        return list(self._rows)


class _FakeEmergencyDb:
    def __init__(self, *, live_rows):
        self.live_rows = live_rows
        self.add = MagicMock()
        self.commit = MagicMock()
        self.refresh = MagicMock()

    def query(self, model):
        name = getattr(model, "__name__", "")
        if name == "PaperTrade":
            return _FakeQuery([])
        if name == "Trade":
            return _FakeQuery(self.live_rows)
        return _FakeQuery([])


def test_emergency_close_all_option_routes_sell_to_close_without_underlying_quote():
    from app.services.trading import emergency_liquidation as elq

    trade = SimpleNamespace(
        id=9901,
        user_id=None,
        ticker="SPY",
        direction="long",
        entry_price=1.25,
        quantity=1.0,
        entry_date=datetime.utcnow(),
        status="open",
        broker_source="robinhood",
        auto_trader_version="v1",
        tags="options",
        indicator_snapshot={
            "breakout_alert": {
                "asset_type": "options",
                "option_meta": {
                    "underlying": "SPY",
                    "expiration": "2026-06-19",
                    "strike": 729.0,
                    "option_type": "call",
                },
            }
        },
    )
    fake_db = _FakeEmergencyDb(live_rows=[trade])

    fake_options = MagicMock()
    fake_options.is_enabled.return_value = True
    fake_options.find_contract.return_value = {"id": "opt-contract-1"}
    fake_options.get_quote.return_value = {"bid_price": "1.40", "mark_price": "1.45"}
    fake_options.place_option_sell.return_value = {
        "ok": True,
        "order_id": "opt-emergency-close",
        "state": "queued",
        "raw": {"state": "queued"},
    }

    with (
        patch(
            "app.services.trading.market_data.fetch_quote",
            side_effect=AssertionError("option liquidation must not fetch underlying spot"),
        ),
        patch(
            "app.services.trading.governance.activate_kill_switch",
            return_value=None,
        ),
        patch(
            "app.services.trading.venue.robinhood_options.RobinhoodOptionsAdapter",
            return_value=fake_options,
        ),
    ):
        result = elq.emergency_close_all(
            fake_db,
            user_id=None,
            reason="test_option_liquidation",
        )

    assert result["ok"] is True
    assert result["closed_live"] == 0
    assert result["working_live"] == 1
    assert result["total_closed"] == 0

    assert trade.status == "open"
    assert trade.pending_exit_order_id == "opt-emergency-close"
    assert trade.pending_exit_status == "queued"
    assert trade.pending_exit_reason == "desk_close_now"
    assert trade.pending_exit_limit_price == pytest.approx(1.40)
    assert trade.tca_reference_exit_price == pytest.approx(1.45)
    fake_options.place_option_sell.assert_called_once_with(
        underlying="SPY",
        expiration="2026-06-19",
        strike=729.0,
        option_type="call",
        quantity=1,
        limit_price=1.40,
        position_effect="close",
    )
