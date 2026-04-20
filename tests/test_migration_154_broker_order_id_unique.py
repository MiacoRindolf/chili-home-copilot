"""Migration 154 — partial UNIQUE index on trading_trades.broker_order_id.

Verifies:
  * The index exists on the test database.
  * A second INSERT with an already-used broker_order_id raises IntegrityError.
  * Empty-string and NULL broker_order_id rows are NOT constrained (the
    partial predicate excludes them).

The dedup path (renaming existing duplicates) is exercised implicitly —
if migrations ran successfully, either there were no dupes or the migration
rewrote them in place before creating the index.
"""
from __future__ import annotations

from datetime import datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app import models
from app.models.trading import Trade


def test_unique_index_exists(db):
    row = db.execute(text(
        "SELECT 1 FROM pg_indexes "
        "WHERE schemaname = 'public' "
        "  AND tablename = 'trading_trades' "
        "  AND indexname = 'ix_trading_trades_broker_order_id_unique'"
    )).fetchone()
    assert row is not None, "migration 154 did not create the unique index"


def test_duplicate_broker_order_id_is_rejected(db):
    u = models.User(name="mig154_u1")
    db.add(u)
    db.flush()

    t1 = Trade(
        user_id=u.id,
        ticker="MG154A",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        entry_date=datetime.utcnow(),
        status="open",
        broker_source="robinhood",
        broker_order_id="mig154-shared-oid",
    )
    db.add(t1)
    db.commit()

    t2 = Trade(
        user_id=u.id,
        ticker="MG154A",
        direction="long",
        entry_price=10.0,
        quantity=1.0,
        entry_date=datetime.utcnow(),
        status="open",
        broker_source="robinhood",
        broker_order_id="mig154-shared-oid",
    )
    db.add(t2)
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_empty_and_null_broker_order_ids_are_unconstrained(db):
    """The partial predicate ``broker_order_id IS NOT NULL AND <> ''`` means
    legacy rows with no broker id can still co-exist freely."""
    u = models.User(name="mig154_u2")
    db.add(u)
    db.flush()

    for i in range(3):
        db.add(Trade(
            user_id=u.id,
            ticker=f"MG154B{i}",
            direction="long",
            entry_price=10.0,
            quantity=1.0,
            entry_date=datetime.utcnow(),
            status="open",
            broker_source="manual",
            broker_order_id=None,
        ))
        db.add(Trade(
            user_id=u.id,
            ticker=f"MG154C{i}",
            direction="long",
            entry_price=10.0,
            quantity=1.0,
            entry_date=datetime.utcnow(),
            status="open",
            broker_source="manual",
            broker_order_id="",
        ))
    db.commit()
