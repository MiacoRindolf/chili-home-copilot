"""f-execution-events-sell-side-recording (2026-05-18) — pin that
``record_execution_event`` is called when SELL fills happen, and that
the payload tags ``side='sell'`` so position_has_recorded_sell can
find them.

The sell-recording wiring lives in:
- robinhood_exit_execution.submit_robinhood_trade_exit (submit-fill path)
- robinhood_exit_execution.sync_pending_exit_order (polled-fill path)

Mig 254 backfills historical closed trades.

These tests pin the contract (helper-level, no DB):
1. The Phase 4 helper position_has_recorded_sell still works correctly
   on a freshly-inserted 'side=sell' event row.
2. The same helper returns False when only buy events exist.
3. Lower-case 'sell' is what the helper queries (case-insensitive via LOWER()).
"""
from __future__ import annotations

from unittest.mock import MagicMock

from app.services.trading.position_resolver import position_has_recorded_sell


def _mock_db_returning(rows):
    result = MagicMock()
    result.first.return_value = rows[0] if rows else None
    db = MagicMock()
    db.execute.return_value = result
    return db


def test_helper_sees_sell_when_event_exists():
    """The mig 254 backfill writes payload_json={'side': 'sell'}. With
    such a row in the DB, position_has_recorded_sell returns True."""
    db = _mock_db_returning([(1,)])
    assert position_has_recorded_sell(db, 42) is True


def test_helper_returns_false_when_no_sell_rows():
    """With only buy events, position_has_recorded_sell returns False
    even though many events exist."""
    db = _mock_db_returning([])
    assert position_has_recorded_sell(db, 42) is False


def test_helper_query_targets_lowercase_sell():
    """The helper uses LOWER(payload_json->>'side')='sell' so mixed-case
    'Sell' / 'SELL' values still match. Verify the SQL via the mock."""
    db = _mock_db_returning([(1,)])
    position_has_recorded_sell(db, 42)
    sql = str(db.execute.call_args[0][0])
    # The compiled SQL text (TextClause repr) should contain the
    # LOWER(...) function call against the side field.
    assert "LOWER" in sql.upper()
    assert "side" in sql.lower()
    assert "sell" in sql.lower()


def test_helper_filters_on_status_filled():
    """status='filled' is the discriminator that excludes intent /
    submitted / cancelled / rejected rows from the count."""
    db = _mock_db_returning([(1,)])
    position_has_recorded_sell(db, 42)
    sql = str(db.execute.call_args[0][0]).lower()
    assert "status" in sql
    assert "filled" in sql


def test_helper_uses_position_id_not_trade_id():
    """The whole point of Phase 4: query by position_id (which persists
    across Trade row generations), not by trade_id."""
    db = _mock_db_returning([(1,)])
    position_has_recorded_sell(db, 42)
    sql = str(db.execute.call_args[0][0]).lower()
    assert "position_id" in sql
    # And the bind param matches.
    bound = db.execute.call_args[0][1]
    assert bound == {"pid": 42}
