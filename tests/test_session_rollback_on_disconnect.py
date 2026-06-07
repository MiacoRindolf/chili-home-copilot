"""Regression tests for the PendingRollbackError cascade fix.

A mid-transaction Postgres disconnect ("server closed the connection
unexpectedly") raises a DBAPI error and leaves the connection's transaction
invalid; any subsequent statement on that *same* session then raises
``PendingRollbackError`` until a rollback is issued, cascading through the rest
of a job's catch-and-continue loop (observed 2026-06-07 in the recovery
scheduler: ``brain_neural_mesh.publish_pattern_health`` and ``prescreen_internal``
predictions hit OperationalError, then ``pattern_position_monitor`` emitted 6x
PendingRollbackError).

Key SQLAlchemy 2.0 subtlety these tests pin down: after an *in-flight*
disconnect ``Session.is_active`` stays True (only a flush failure flips it), so
the fix keys off the exception TYPE, not ``is_active``. We reproduce a real
disconnect by closing the underlying driver socket out from under the session.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text
from sqlalchemy.exc import PendingRollbackError, SQLAlchemyError

from app.db import rollback_if_poisoned


def _kill_connection(session) -> None:
    """Close the driver socket beneath the session, mid-transaction.

    Mimics Postgres dropping the connection while it is checked out and in use:
    the next statement on the session raises a disconnect DBAPI error, exactly
    like the production incident.
    """
    session.execute(text("SELECT 1"))  # ensure a connection is checked out
    fairy = session.connection().connection
    raw = getattr(fairy, "dbapi_connection", None) or getattr(
        fairy, "driver_connection", None
    )
    raw.close()


def test_disconnect_state_keeps_is_active_true(db):
    """Pin the surprising behavior the fix is built around."""
    _kill_connection(db)
    with pytest.raises(SQLAlchemyError):
        db.execute(text("SELECT 1"))
    # The transaction is poisoned (next query would PendingRollbackError) yet
    # is_active is STILL True — which is why the fix cannot rely on it.
    assert db.is_active is True
    with pytest.raises(PendingRollbackError):
        db.execute(text("SELECT 1"))
    db.rollback()
    assert db.execute(text("SELECT 1")).scalar() == 1


def test_rollback_if_poisoned_recovers_after_disconnect(db):
    _kill_connection(db)
    performed = None
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        performed = rollback_if_poisoned(db)
    assert performed is True
    # Session is usable again — no PendingRollbackError cascade.
    assert db.execute(text("SELECT 42")).scalar() == 42


def test_rollback_if_poisoned_noop_on_non_db_error(db):
    """Non-DB errors leave the transaction healthy; pending writes must survive."""
    from app.models import User

    user = User(name="rollback-guard-keepme")
    db.add(user)
    db.flush()  # pending, uncommitted, transaction healthy

    performed = None
    try:
        raise ValueError("not a database error")
    except Exception:
        performed = rollback_if_poisoned(db)
    assert performed is False
    assert (
        db.query(User).filter(User.name == "rollback-guard-keepme").first()
        is not None
    )


def test_rollback_if_poisoned_noop_outside_except(db):
    """With no exception being handled it is a safe no-op."""
    db.execute(text("SELECT 1"))
    assert rollback_if_poisoned(db) is False


def test_publisher_rollback_helper_recovers_after_disconnect(db):
    from app.services.trading.brain_neural_mesh.publisher import (
        _rollback_publish_session,
    )

    _kill_connection(db)
    try:
        db.execute(text("SELECT 1"))
    except Exception:
        _rollback_publish_session(db, context="test")
    assert db.execute(text("SELECT 5")).scalar() == 5


def test_prescreen_bucket_recovers_from_disconnect(db):
    from app.services.trading.prescreen_internal_signals import (
        tickers_from_latest_predictions,
    )

    _kill_connection(db)
    # The bucket's first query hits the dead connection; the fix catches the
    # DBAPI error, rolls back, and returns an empty result instead of cascading.
    out = tickers_from_latest_predictions(db)
    assert out == {}
    assert db.execute(text("SELECT 1")).scalar() == 1


def test_prescreen_collect_recovers_across_buckets(db):
    """The named incident path: a disconnect in the first bucket must not cascade
    across the three sequential prescreen buckets in
    collect_internal_prescreen_tickers."""
    from app.services.trading.prescreen_internal_signals import (
        collect_internal_prescreen_tickers,
    )

    _kill_connection(db)
    merged = collect_internal_prescreen_tickers(db)
    assert isinstance(merged, dict)
    # Session is healthy after all three buckets ran on it.
    assert db.execute(text("SELECT 1")).scalar() == 1
