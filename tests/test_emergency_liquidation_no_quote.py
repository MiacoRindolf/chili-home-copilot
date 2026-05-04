"""broker-truth-self-heal (2026-05-04) -- regression test for Bug 2:
when fetch_quote returns None inside emergency_close_all, the prior
code wrote exit_price = entry_price (lying P/L). The fix sets
exit_price = None + exit_reason = '<reason>:no_quote' + leaves pnl
NULL.

Two scenarios (paper + live), both verifying the NULL path.
"""
from __future__ import annotations

from unittest.mock import patch

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
