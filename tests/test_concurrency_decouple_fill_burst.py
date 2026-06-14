"""🔴 THE MERGE GATE for decouple_watching: the fill-boundary position cap must
hold EXACTLY under a concurrent entry-submit burst.

Positions are born at FILL — seconds after submit — and ``tick_live_session`` locks
only its own row, so two watchers tick fully in parallel and each read the same held
count → both submit → cap breached. The fix is a Postgres ``pg_advisory_xact_lock``
keyed on (user, lane) that serializes the count-and-submit across worker connections,
combined with a count that charges held positions PLUS in-flight-submitted entries.

These tests reproduce that exact critical section (same lane-key formula, the real
``count_open_positions`` + ``count_inflight_entry_orders`` helpers) across K REAL
threads on REAL Postgres connections:

* WITHOUT the lock (reads forced before any write via a barrier): the burst
  deterministically OVERSHOOTS to K — proving the hazard B1 describes is real.
* WITH the lock (the shipped pattern): reservations land at EXACTLY the cap —
  overshoot 0.

If the with-lock test ever fails, the decouple master flag must NOT be flipped on.
"""

from __future__ import annotations

import os
import threading
import uuid

from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.orm.attributes import flag_modified
from sqlalchemy.pool import NullPool

from app import models
from app.models.trading import MomentumStrategyVariant, TradingAutomationSession
from app.services.trading.momentum_neural.risk_evaluator import (
    count_inflight_entry_orders,
    count_open_positions,
)

_LANE_NS = 0x4D4C  # "ML" — must match live_runner's lane-lock namespace exactly.

# Dedicated engine for genuinely CONCURRENT connections (the shared app.db test
# engine is pool_size=1/overflow=1 — the barrier path would exhaust it). NullPool:
# every session opens a fresh connection and CLOSES it on .close(), so no idle
# pooled connection lingers to block the next test's TRUNCATE (the cleanup the `db`
# fixture runs). Same physical DB → sees the fixture's committed rows (READ COMMITTED).
_BURST_SESSION = None


def _new_session():
    global _BURST_SESSION
    if _BURST_SESSION is None:
        url = os.environ.get("DATABASE_URL") or os.environ["TEST_DATABASE_URL"]
        eng = create_engine(url, poolclass=NullPool)
        _BURST_SESSION = sessionmaker(bind=eng, autocommit=False, autoflush=False)
    return _BURST_SESSION()


def _uniq(prefix: str) -> str:
    """Globally-unique name so re-runs never collide on users.name (UNIQUE) even if
    a prior process's rows survived (counts are user-scoped, so leftovers are inert)."""
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _lane_key(user_id: int) -> int:
    return (_LANE_NS << 32) | (int(user_id) & 0xFFFFFFFF)


def _seed_watchers(db, *, n: int):
    """A user + variant + ``n`` pre-fill watchers in live_pending_entry, not yet
    submitted. Returns (user_id, variant_id, session_ids); all committed so worker
    threads on separate connections see them (variant_id is a NOT-NULL FK)."""
    u = models.User(name=_uniq("burst"))
    db.add(u)
    db.flush()
    v = MomentumStrategyVariant(
        family="dec", variant_key=_uniq("dec_burst"), label="dec", params_json={}
    )
    db.add(v)
    db.flush()
    sids = []
    for i in range(n):
        s = TradingAutomationSession(
            user_id=u.id, symbol=f"C{i}-USD", mode="live", state="live_pending_entry",
            variant_id=v.id, execution_family="coinbase_spot",
            risk_snapshot_json={"momentum_live_execution": {"entry_submitted": False}},
        )
        db.add(s)
        db.flush()
        sids.append(int(s.id))
    db.commit()  # worker threads run on separate connections — must see committed rows
    return int(u.id), int(v.id), sids


def _reserve(db_t, sid: int) -> None:
    """Simulate the entry submit: mark this session's order in-flight (entry_submitted)."""
    s = db_t.query(TradingAutomationSession).filter_by(id=sid).one()
    snap = dict(s.risk_snapshot_json or {})
    mle = dict(snap.get("momentum_live_execution") or {})
    mle["entry_submitted"] = True
    snap["momentum_live_execution"] = mle
    s.risk_snapshot_json = snap
    flag_modified(s, "risk_snapshot_json")
    db_t.flush()


def _final_submitted_count(db, user_id: int) -> int:
    """How many of the user's pending_entry sessions ended up in-flight (submitted)."""
    return count_inflight_entry_orders(db, user_id=user_id)


def test_fill_burst_without_lock_overshoots(db) -> None:
    """Proof B1 is a real hazard: with no serialization and reads forced before
    writes, every concurrent submitter sees held==0 and all reserve."""
    cap = 3
    k = 8
    user_id, _variant_id, sids = _seed_watchers(db, n=k)
    barrier = threading.Barrier(k)

    def worker(sid: int) -> None:
        db_t = _new_session()
        try:
            # READ first (no lock) ...
            pos = count_open_positions(db_t, user_id=user_id, mode="live") + \
                count_inflight_entry_orders(db_t, user_id=user_id, exclude_session_id=sid)
            barrier.wait(timeout=20)  # ... force all reads before any write
            if pos < cap:
                _reserve(db_t, sid)
            db_t.commit()
        finally:
            db_t.rollback()
            db_t.close()

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    db.expire_all()
    # No lock + reads-before-writes => the cap is blown wide open (all K reserve).
    assert _final_submitted_count(db, user_id) == k
    assert _final_submitted_count(db, user_id) > cap


def test_fill_burst_with_advisory_lock_holds_cap_exactly(db) -> None:
    """The shipped pattern: the advisory lock serializes count-and-reserve so the
    in-flight count is always fresh — reservations stop at EXACTLY the cap."""
    cap = 3
    k = 8
    user_id, _variant_id, sids = _seed_watchers(db, n=k)
    key = _lane_key(user_id)

    def worker(sid: int) -> None:
        db_t = _new_session()
        try:
            # xact-scoped advisory lock: auto-releases at commit (no orphan-lock).
            db_t.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
            pos = count_open_positions(db_t, user_id=user_id, mode="live") + \
                count_inflight_entry_orders(db_t, user_id=user_id, exclude_session_id=sid)
            if pos < cap:
                _reserve(db_t, sid)
            db_t.commit()  # releases the lock; the next thread reads a fresh count
        finally:
            db_t.rollback()
            db_t.close()

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    db.expire_all()
    # Serialized + in-flight-aware count => overshoot is exactly 0.
    assert _final_submitted_count(db, user_id) == cap


def test_fill_burst_with_lock_at_cap_minus_one_admits_one(db) -> None:
    """held == cap-1 already; a burst of K submitters fills the single open slot
    exactly once (the rest fall back to watching)."""
    cap = 4
    k = 6
    user_id, variant_id, sids = _seed_watchers(db, n=k)
    key = _lane_key(user_id)
    # Pre-seed (cap-1) HELD positions on the same user (own connection, committed).
    pre = _new_session()
    try:
        for i in range(cap - 1):
            pre.add(TradingAutomationSession(
                user_id=user_id, symbol=f"H{i}-USD", mode="live", state="live_entered",
                variant_id=variant_id, execution_family="coinbase_spot",
                risk_snapshot_json={"momentum_live_execution": {"position": {"quantity": 1}}},
            ))
        pre.commit()
    finally:
        pre.close()

    def worker(sid: int) -> None:
        db_t = _new_session()
        try:
            db_t.execute(text("SELECT pg_advisory_xact_lock(:k)"), {"k": key})
            pos = count_open_positions(db_t, user_id=user_id, mode="live") + \
                count_inflight_entry_orders(db_t, user_id=user_id, exclude_session_id=sid)
            if pos < cap:
                _reserve(db_t, sid)
            db_t.commit()
        finally:
            db_t.rollback()
            db_t.close()

    threads = [threading.Thread(target=worker, args=(sid,)) for sid in sids]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    db.expire_all()
    # cap-1 held + exactly one new in-flight == cap; never more.
    assert _final_submitted_count(db, user_id) == 1
    held = count_open_positions(db, user_id=user_id, mode="live")
    assert held + _final_submitted_count(db, user_id) == cap
