"""Durable venue idempotency store — DB-backed client_order_id guard.

The in-RAM guard alone resets on restart, leaving a window for duplicate
orders. The store adds a DB layer; these tests verify:

* Memory-only behavior (duplicate detection, reset_for_tests).
* DB persistence: after ``reset_for_tests`` wipes the memory cache, a prior
  ``remember`` still makes the key look duplicate (simulating restart).
* ``mark_broker_id`` + ``resolve_broker_id`` round-trip.
* TTL expiry: rows past ``ttl_expires_at`` are not considered duplicates.
* Empty/None ids are never flagged.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

from app.services.trading.venue import idempotency_store


def _clear_row(session, key: str) -> None:
    session.execute(
        text("DELETE FROM venue_order_idempotency WHERE client_order_id = :k"),
        {"k": key},
    )
    session.commit()


def test_empty_client_order_id_never_duplicate():
    idempotency_store.reset_for_tests()
    assert idempotency_store.is_duplicate(None, venue="coinbase") is False
    assert idempotency_store.is_duplicate("", venue="coinbase") is False
    # remember() with empty id is a safe no-op
    idempotency_store.remember(None, venue="coinbase", symbol="BTC-USD", side="buy", qty=0.1)
    idempotency_store.remember("", venue="coinbase", symbol="BTC-USD", side="buy", qty=0.1)


def test_memory_guard_detects_duplicate_same_process(db):
    idempotency_store.reset_for_tests()
    key = "test-mem-guard-1"
    _clear_row(db, key)

    assert idempotency_store.is_duplicate(key, venue="coinbase") is False
    idempotency_store.remember(
        key, venue="coinbase", symbol="BTC-USD", side="buy", qty=0.001
    )
    assert idempotency_store.is_duplicate(key, venue="coinbase") is True

    _clear_row(db, key)
    idempotency_store.reset_for_tests()


def test_db_layer_survives_memory_reset(db):
    """Simulate process restart: remember, wipe memory, then check again.

    This is the headline guarantee of P0.1 — the in-RAM guard resets on
    restart, the DB does not.
    """
    idempotency_store.reset_for_tests()
    key = "test-db-persist-1"
    _clear_row(db, key)

    idempotency_store.remember(
        key, venue="robinhood", symbol="AAPL", side="buy", qty=5.0
    )
    assert idempotency_store.is_duplicate(key, venue="robinhood") is True

    # "Restart": clear memory cache. DB row remains.
    idempotency_store.reset_for_tests()
    assert idempotency_store.is_duplicate(key, venue="robinhood") is True

    _clear_row(db, key)
    idempotency_store.reset_for_tests()


def test_mark_and_resolve_broker_id(db):
    idempotency_store.reset_for_tests()
    key = "test-broker-id-1"
    _clear_row(db, key)

    idempotency_store.remember(
        key, venue="coinbase", symbol="ETH-USD", side="sell", qty=0.25
    )
    assert idempotency_store.resolve_broker_id(key) is None

    idempotency_store.mark_broker_id(key, "broker-oid-xyz", status="acked")
    assert idempotency_store.resolve_broker_id(key) == "broker-oid-xyz"

    _clear_row(db, key)
    idempotency_store.reset_for_tests()


def test_ttl_expired_row_is_not_duplicate(db):
    """Manually pre-expire a row; it must not be treated as duplicate."""
    idempotency_store.reset_for_tests()
    key = "test-ttl-expired-1"
    _clear_row(db, key)

    past = datetime.utcnow() - timedelta(hours=1)
    db.execute(
        text(
            "INSERT INTO venue_order_idempotency "
            "(client_order_id, venue, symbol, side, qty, status, ttl_expires_at) "
            "VALUES (:k, :v, :sym, :side, :qty, 'submitted', :exp)"
        ),
        {"k": key, "v": "coinbase", "sym": "BTC-USD", "side": "buy", "qty": 0.01, "exp": past},
    )
    db.commit()

    # Memory cache is empty — DB check must honor TTL.
    idempotency_store.reset_for_tests()
    assert idempotency_store.is_duplicate(key, venue="coinbase") is False

    _clear_row(db, key)


def test_remember_upsert_does_not_error_on_duplicate_insert(db):
    """Calling remember twice with the same key must not raise."""
    idempotency_store.reset_for_tests()
    key = "test-remember-idempotent-1"
    _clear_row(db, key)

    idempotency_store.remember(
        key, venue="robinhood", symbol="TSLA", side="buy", qty=1.0
    )
    # Repeat — should be a DB-level no-op via ON CONFLICT DO NOTHING.
    idempotency_store.remember(
        key, venue="robinhood", symbol="TSLA", side="buy", qty=1.0
    )

    row = db.execute(
        text("SELECT COUNT(*) FROM venue_order_idempotency WHERE client_order_id = :k"),
        {"k": key},
    ).scalar()
    assert row == 1

    _clear_row(db, key)
    idempotency_store.reset_for_tests()
