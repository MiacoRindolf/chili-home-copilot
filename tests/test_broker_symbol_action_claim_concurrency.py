"""Real-Postgres concurrency proofs for Alpaca account/symbol action claims.

These tests exercise the database boundary itself.  They deliberately use
independent ``NullPool`` connections so an in-process fake or SQLAlchemy's
identity map cannot hide a lock-order, winner-selection, or commit-before-HTTP
bug.
"""

from __future__ import annotations

import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from typing import Any

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool

from app import models
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural import alpaca_reconcile as reconcile
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    SUBMIT_INDETERMINATE,
    SUBMITTED,
    acquire_action_claim,
    mark_entry_transport_started,
    read_action_claim,
    resolve_action_claim,
    update_action_claim_phase,
)


def _unique(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _independent_sessions():
    url = os.environ.get("DATABASE_URL") or os.environ["TEST_DATABASE_URL"]
    engine = create_engine(url, poolclass=NullPool)
    return engine, sessionmaker(bind=engine, autocommit=False, autoflush=False)


def _bound_transaction_timeouts(session, *, lock_seconds: int = 4) -> None:
    session.execute(text(f"SET LOCAL lock_timeout = '{int(lock_seconds)}s'"))
    session.execute(text("SET LOCAL statement_timeout = '8s'"))


def _claim_row(db, symbol: str) -> tuple[Any, ...] | None:
    return db.execute(
        text(
            "SELECT claim_token, action, phase, client_order_id, broker_order_id "
            "FROM broker_symbol_action_claims "
            "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
        ),
        {"symbol": symbol},
    ).fetchone()


def _bound_entry_metadata(*, binder: str, generation: str = "v1") -> dict[str, Any]:
    return {
        "order_role": "primary",
        "order_request": {
            "product_id": "REBIND",
            "side": "buy",
            "base_size": "10",
            "limit_price": "10.00",
            "client_order_id": "cid-rebind",
        },
        "reserved_risk_usd": 5.0,
        "alpaca_account_id": "paper-account-rebind",
        "entry_post_bind_token": binder,
        "adaptive_risk_decision_packet": {"generation": generation},
        "adaptive_risk_reservation_claim": {"generation": generation},
        "adaptive_risk_reservation_request": {"generation": generation},
    }


def test_expired_bound_claim_rotates_only_exact_pre_transport_generation(db) -> None:
    symbol = "REBIND"
    token = f"entry-{uuid.uuid4().hex}"
    cid = "cid-rebind"
    owner_id = 90701
    old_binder = f"old-{uuid.uuid4().hex}"
    new_binder = f"new-{uuid.uuid4().hex}"
    old_metadata = _bound_entry_metadata(binder=old_binder)
    new_metadata = _bound_entry_metadata(binder=new_binder)

    seeded = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        metadata=old_metadata,
        account_scope="alpaca:paper",
    )
    assert seeded.get("ok") is True, seeded
    db.commit()

    unexpired = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        metadata=new_metadata,
        account_scope="alpaca:paper",
    )
    assert unexpired.get("ok") is False, unexpired
    assert unexpired.get("reason") == "entry_claim_identity_mismatch"
    db.rollback()

    db.execute(
        text(
            "UPDATE broker_symbol_action_claims "
            "SET lease_expires_at = NOW() - interval '1 second' "
            "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
        ),
        {"symbol": symbol},
    )
    db.commit()

    changed_economics = _bound_entry_metadata(
        binder=new_binder,
        generation="different",
    )
    mismatched = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        metadata=changed_economics,
        account_scope="alpaca:paper",
    )
    assert mismatched.get("ok") is False, mismatched
    assert mismatched.get("reason") == "entry_claim_identity_mismatch"
    db.rollback()

    rebound = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        metadata=new_metadata,
        account_scope="alpaca:paper",
    )
    assert rebound.get("ok") is True, rebound
    assert rebound.get("pre_transport_generation_rebound") is True
    assert rebound["claim"]["metadata"]["entry_post_bind_token"] == new_binder
    proof = rebound["claim"]["metadata"]["pre_transport_generation_rebound"]
    assert proof["client_order_id"] == cid
    assert proof["reason"] == "expired_claim_only_pre_transport_recovery"
    db.commit()

    assert mark_entry_transport_started(
        db,
        symbol=symbol,
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        post_bind_token=old_binder,
        account_scope="alpaca:paper",
        alpaca_account_id="paper-account-rebind",
    ) is False
    assert mark_entry_transport_started(
        db,
        symbol=symbol,
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        post_bind_token=new_binder,
        account_scope="alpaca:paper",
        alpaca_account_id="paper-account-rebind",
    ) is True
    db.commit()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and claim is not None
    assert claim["phase"] == SUBMIT_INDETERMINATE
    assert claim["metadata"]["entry_transport_started"]["post_bind_token"] == new_binder


def test_stale_lookup_cannot_resolve_a_rebound_claim_generation(db) -> None:
    """A broker observation tied to binder A cannot CAS-resolve binder B."""

    symbol = "REBSTAL"
    token = f"entry-{uuid.uuid4().hex}"
    cid = "cid-rebind"
    owner_id = 90703
    old_binder = f"old-{uuid.uuid4().hex}"
    new_binder = f"new-{uuid.uuid4().hex}"
    seeded = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        metadata=_bound_entry_metadata(binder=old_binder),
        account_scope="alpaca:paper",
    )
    assert seeded.get("ok") is True, seeded
    db.commit()
    readable, stale = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and stale is not None

    db.execute(
        text(
            "UPDATE broker_symbol_action_claims "
            "SET lease_expires_at = NOW() - interval '1 second' "
            "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
        ),
        {"symbol": symbol},
    )
    db.commit()
    rebound = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        metadata=_bound_entry_metadata(binder=new_binder),
        account_scope="alpaca:paper",
    )
    assert rebound.get("ok") is True, rebound
    assert rebound.get("pre_transport_generation_rebound") is True
    db.commit()

    stale_resolution = resolve_action_claim(
        db,
        symbol=symbol,
        claim_token=token,
        client_order_id=cid,
        broker_order_id="stale-broker-order",
        broker_order_status="canceled",
        zero_fill_terminal=True,
        expected_claim_updated_at=stale["updated_at"],
        account_scope="alpaca:paper",
    )
    assert stale_resolution is False
    db.rollback()
    readable, current = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and current is not None
    assert current["phase"] == "claimed"
    assert current["metadata"]["entry_post_bind_token"] == new_binder


def test_expired_rebind_racing_old_transport_has_one_generation_winner(db) -> None:
    symbol = "REBRACE"
    token = f"entry-{uuid.uuid4().hex}"
    cid = "cid-rebind"
    owner_id = 90702
    old_binder = f"old-{uuid.uuid4().hex}"
    new_binder = f"new-{uuid.uuid4().hex}"
    old_metadata = _bound_entry_metadata(binder=old_binder)
    new_metadata = _bound_entry_metadata(binder=new_binder)
    seeded = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        metadata=old_metadata,
        account_scope="alpaca:paper",
    )
    assert seeded.get("ok") is True, seeded
    db.execute(
        text(
            "UPDATE broker_symbol_action_claims "
            "SET lease_expires_at = NOW() - interval '1 second' "
            "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
        ),
        {"symbol": symbol},
    )
    db.commit()

    engine, Session = _independent_sessions()
    start = threading.Barrier(2, timeout=5)

    def old_transport() -> bool:
        session = Session()
        try:
            _bound_transaction_timeouts(session)
            start.wait()
            won = mark_entry_transport_started(
                session,
                symbol=symbol,
                claim_token=token,
                owner_session_id=owner_id,
                client_order_id=cid,
                post_bind_token=old_binder,
                account_scope="alpaca:paper",
                alpaca_account_id="paper-account-rebind",
            )
            session.commit()
            return bool(won)
        finally:
            session.rollback()
            session.close()

    def new_generation() -> bool:
        session = Session()
        try:
            _bound_transaction_timeouts(session)
            start.wait()
            result = acquire_action_claim(
                session,
                symbol=symbol,
                action="entry",
                claim_token=token,
                owner_session_id=owner_id,
                client_order_id=cid,
                metadata=new_metadata,
                account_scope="alpaca:paper",
            )
            session.commit()
            return bool(
                result.get("ok")
                and result.get("pre_transport_generation_rebound")
            )
        finally:
            session.rollback()
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            old_future = pool.submit(old_transport)
            new_future = pool.submit(new_generation)
            old_won = old_future.result(timeout=12)
            new_won = new_future.result(timeout=12)
    finally:
        engine.dispose()

    assert old_won is not new_won, (old_won, new_won)
    db.rollback()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and claim is not None
    if old_won:
        assert claim["phase"] == SUBMIT_INDETERMINATE
        assert claim["metadata"]["entry_post_bind_token"] == old_binder
        assert claim["metadata"]["entry_transport_started"]["post_bind_token"] == old_binder
    else:
        assert claim["phase"] == "claimed"
        assert claim["metadata"]["entry_post_bind_token"] == new_binder
        assert "entry_transport_started" not in claim["metadata"]
        assert "pre_transport_generation_rebound" in claim["metadata"]


def test_same_symbol_entry_vs_orphan_has_exactly_one_winner(db) -> None:
    """Concurrent opposite actions cannot both own one account/symbol row."""
    if db.bind is None or db.bind.dialect.name != "postgresql":
        pytest.skip("claim concurrency is PostgreSQL-only")

    symbol = "RACEOWN"
    start = threading.Barrier(2, timeout=5)
    engine, Session = _independent_sessions()

    def worker(action: str) -> dict[str, Any]:
        session = Session()
        try:
            _bound_transaction_timeouts(session)
            start.wait()
            result = acquire_action_claim(
                session,
                symbol=symbol,
                action=action,
                claim_token=f"{action}-{uuid.uuid4().hex}",
                owner_session_id=None,
                client_order_id=f"cid-{action}-{uuid.uuid4().hex[:12]}",
                metadata={"race_action": action},
                account_scope="alpaca:paper",
            )
            session.commit()
            return result
        finally:
            session.rollback()
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [
                pool.submit(worker, "entry"),
                pool.submit(worker, "orphan_flatten"),
            ]
            results = [future.result(timeout=12) for future in futures]
    finally:
        engine.dispose()

    winners = [result for result in results if result.get("ok")]
    losers = [result for result in results if not result.get("ok")]
    assert len(winners) == 1, results
    assert len(losers) == 1, results
    assert losers[0].get("reason") == "symbol_action_claimed", results

    db.rollback()
    row = _claim_row(db, symbol)
    assert row is not None
    assert row[0] == winners[0]["claim"]["claim_token"]
    assert row[1] == winners[0]["claim"]["action"]
    assert row[2] == "claimed"


def test_generic_unclaimed_position_claim_minter_is_removed() -> None:
    assert not hasattr(reconcile, "_reserve_or_reuse_orphan_claim")


def test_cidless_claim_requires_explicit_no_transport_proof(db) -> None:
    symbol = "NOPROOF"
    token = f"arm-{uuid.uuid4().hex}"
    seeded = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=None,
        account_scope="alpaca:paper",
    )
    assert seeded.get("ok") is True, seeded

    assert resolve_action_claim(
        db,
        symbol=symbol,
        claim_token=token,
        client_order_id=None,
        broker_order_id=None,
        broker_order_status="not_submitted",
        proven_no_transport=False,
        account_scope="alpaca:paper",
    ) is False
    readable, retained = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable is True and retained is not None
    assert retained["phase"] == "claimed"

    assert resolve_action_claim(
        db,
        symbol=symbol,
        claim_token=token,
        client_order_id=None,
        broker_order_id=None,
        broker_order_status="not_submitted",
        proven_no_transport=True,
        account_scope="alpaca:paper",
    ) is True



def test_different_symbols_acquire_without_global_serialization(db) -> None:
    """Independent symbols can both hold their row before either commits."""
    acquired = threading.Barrier(2, timeout=4)
    start = threading.Barrier(2, timeout=4)
    engine, Session = _independent_sessions()

    def worker(symbol: str) -> dict[str, Any]:
        session = Session()
        try:
            _bound_transaction_timeouts(session)
            start.wait()
            result = acquire_action_claim(
                session,
                symbol=symbol,
                action="entry",
                claim_token=f"entry-{symbol}-{uuid.uuid4().hex}",
                owner_session_id=None,
                client_order_id=f"cid-{symbol}-{uuid.uuid4().hex[:12]}",
                account_scope="alpaca:paper",
            )
            if result.get("ok"):
                acquired.wait()
            session.commit()
            return result
        finally:
            session.rollback()
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker, "INDEPA"), pool.submit(worker, "INDEPB")]
            results = [future.result(timeout=10) for future in futures]
    finally:
        engine.dispose()

    assert all(result.get("ok") for result in results), results
    db.rollback()
    assert _claim_row(db, "INDEPA") is not None
    assert _claim_row(db, "INDEPB") is not None


def test_expired_submitted_and_indeterminate_claims_still_block_takeover(db) -> None:
    """Time never expires an order that might have crossed the broker boundary."""
    for symbol, phase, broker_order_id in (
        ("EXPSUB", SUBMITTED, "broker-entry-submitted"),
        ("EXPIND", SUBMIT_INDETERMINATE, None),
    ):
        original_token = f"entry-{symbol}"
        original_cid = f"cid-{symbol}"
        seeded = acquire_action_claim(
            db,
            symbol=symbol,
            action="entry",
            claim_token=original_token,
            owner_session_id=None,
            client_order_id=original_cid,
            account_scope="alpaca:paper",
        )
        assert seeded.get("ok"), seeded
        assert update_action_claim_phase(
            db,
            symbol=symbol,
            claim_token=original_token,
            phase=phase,
            client_order_id=original_cid,
            broker_order_id=broker_order_id,
            account_scope="alpaca:paper",
        )
        db.execute(
            text(
                "UPDATE broker_symbol_action_claims "
                "SET lease_expires_at = NOW() - interval '1 hour' "
                "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol"
            ),
            {"symbol": symbol},
        )
        db.commit()

        takeover = acquire_action_claim(
            db,
            symbol=symbol,
            action="orphan_flatten",
            claim_token=f"orphan-{symbol}",
            owner_session_id=None,
            client_order_id=f"orphan-cid-{symbol}",
            account_scope="alpaca:paper",
        )
        assert not takeover.get("ok"), takeover
        assert takeover.get("reason") == "symbol_action_claimed", takeover
        assert takeover["claim"]["claim_token"] == original_token
        assert takeover["claim"]["phase"] == phase
        db.rollback()


def test_session_then_claim_lock_order_does_not_deadlock_handoff(db, monkeypatch) -> None:
    """A session-owner lock racing detached handoff must not invert claim ordering.

    Connection A represents a session terminalization path: it owns the session row
    and then checks the symbol claim.  Connection B runs the real detached handoff.
    If B takes claim->session, the forced barrier creates a real PostgreSQL deadlock;
    the required session->claim order lets A finish and B follow cleanly.
    """
    user = models.User(name=_unique("claim-lock-user"))
    db.add(user)
    db.flush()
    variant = MomentumStrategyVariant(
        family="claim_lock_order",
        variant_key=_unique("claim-lock-variant"),
        label="claim-lock-order",
        params_json={},
    )
    db.add(variant)
    db.flush()
    symbol = "LOCKORD"
    owner = TradingAutomationSession(
        user_id=int(user.id),
        symbol=symbol,
        mode="live",
        state="live_cancelled",
        variant_id=int(variant.id),
        execution_family="alpaca_spot",
        risk_snapshot_json={},
    )
    db.add(owner)
    db.flush()
    owner_id = int(owner.id)
    token = f"entry-{owner_id}"
    cid = f"entry-cid-{owner_id}"
    oid = f"entry-oid-{owner_id}"
    seeded = acquire_action_claim(
        db,
        symbol=symbol,
        action="entry",
        claim_token=token,
        owner_session_id=owner_id,
        client_order_id=cid,
        metadata={
            "order_role": "primary",
            "order_request": {
                "product_id": symbol,
                "side": "buy",
                "base_size": "10",
            },
        },
        account_scope="alpaca:paper",
    )
    assert seeded.get("ok"), seeded
    assert update_action_claim_phase(
        db,
        symbol=symbol,
        claim_token=token,
        phase=SUBMITTED,
        client_order_id=cid,
        broker_order_id=oid,
        account_scope="alpaca:paper",
    )
    db.commit()
    readable, claim = read_action_claim(
        db,
        symbol=symbol,
        account_scope="alpaca:paper",
    )
    assert readable and claim is not None
    db.rollback()

    order = SimpleNamespace(
        order_id=oid,
        client_order_id=cid,
        product_id=symbol,
        side="buy",
        status="filled",
        filled_size=10.0,
        raw={"qty": 10.0},
    )

    original_read = reconcile.read_action_claim
    claim_locked = threading.Event()

    def traced_read(*args: Any, **kwargs: Any):
        result = original_read(*args, **kwargs)
        if kwargs.get("for_update"):
            claim_locked.set()
        return result

    monkeypatch.setattr(reconcile, "read_action_claim", traced_read)

    start = threading.Barrier(2, timeout=5)
    engine, Session = _independent_sessions()

    def session_then_claim() -> str:
        session = Session()
        try:
            _bound_transaction_timeouts(session, lock_seconds=3)
            session.execute(
                text("SELECT id FROM trading_automation_sessions WHERE id = :sid FOR UPDATE"),
                {"sid": owner_id},
            ).fetchone()
            start.wait()

            # Give a correctly ordered handoff a chance to block on our session
            # row.  A claim-first handoff signals that it already owns the inverse
            # lock, after which this SELECT exposes the deadlock cycle.
            claim_locked.wait(timeout=0.6)
            session.execute(
                text(
                    "SELECT claim_token FROM broker_symbol_action_claims "
                    "WHERE account_scope = 'alpaca:paper' AND symbol = :symbol "
                    "FOR UPDATE"
                ),
                {"symbol": symbol},
            ).fetchone()
            session.commit()
            return "committed"
        finally:
            session.rollback()
            session.close()

    def detached_handoff() -> dict[str, Any]:
        session = Session()
        try:
            _bound_transaction_timeouts(session, lock_seconds=3)
            start.wait()
            return reconcile._handoff_detached_entry_claim(
                session,
                claim=claim,
                order=order,
                broker_position_qty=10.0,
                authority_proof={
                    "proof_version": "durable_entry_claim_handoff_v1",
                    "entry_claim_token": token,
                    "entry_client_order_id": cid,
                    "entry_broker_order_id": oid,
                    "entry_order_status": "filled",
                    "entry_filled_size": 10.0,
                    "entry_average_filled_price": None,
                    "entry_side": "buy",
                    "broker_position_qty": 10.0,
                    "broker_position_avg_entry_price": None,
                    "no_competing_open_orders": True,
                    "entry_account_scope": "alpaca:paper",
                },
            )
        finally:
            session.rollback()
            session.close()

    try:
        with ThreadPoolExecutor(max_workers=2) as pool:
            session_future = pool.submit(session_then_claim)
            handoff_future = pool.submit(detached_handoff)
            session_result = session_future.result(timeout=12)
            handoff_result = handoff_future.result(timeout=12)
    finally:
        engine.dispose()

    assert session_result == "committed"
    assert handoff_result.get("ok"), handoff_result
    db.rollback()
    row = _claim_row(db, symbol)
    assert row is not None
    assert row[1] == "orphan_flatten"
