"""2026-09-10 SKYQ 21605 — the finalize sweep must DECIDE UNDER THE ROW LOCK.

Two auto-arm passes (the scheduler job and the ignition→arm bridge) ran
``_finalize_stale_exited_sessions`` on the same ``live_exited`` row within one second.
Pass A booked the outcome with ``terminal_at = t1`` and committed; pass B's in-memory
row still carried ``ended_at=None`` so its transition re-stamped ``ended_at`` at
``t2`` (+194 ms). The loss-guard loader demands ``outcome.terminal_at ==
session.ended_at`` to the microsecond → ``loss_guard_outcome_session_terminal_clock_mismatch``
→ the WHOLE account sat at ``loss_guard_history_unavailable`` from 14:34:53Z to
17:00:48Z. Same signature on 09-04 (BIAF 20245, +12 s) and 09-08 (SUNE 20716, +8 s).

The contract pinned here: the scan is a snapshot; the decision is re-made on the
LOCKED, REFRESHED row. Lock refused → skip. State advanced → skip. Touched since the
scan → skip. Only a row still exited and still idle transitions, and the object that
transitions is the locked one (whose ``ended_at`` is what the booked outcome anchors to).
"""
from __future__ import annotations

import ast
import pathlib
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.exc import OperationalError

from app.services.trading.momentum_neural import auto_arm as AA
from app.services.trading.momentum_neural import live_runner as LR

_SRC = pathlib.Path(AA.__file__)


def _fn_src() -> str:
    tree = ast.parse(_SRC.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_finalize_stale_exited_sessions"
    )
    return ast.unparse(fn)


# ── fakes (same shape as tests/test_naked_position_0902_reaper.py) ──────────


class _Nested:
    def __init__(self, db):
        self._db = db
        self.is_active = True

    def commit(self):
        self._db.nested_commits += 1
        self.is_active = False

    def rollback(self):
        self._db.nested_rollbacks += 1
        self.is_active = False


class _LockedQuery:
    def __init__(self, db):
        self._db = db

    def filter(self, *_a, **_k):
        return self

    def with_for_update(self, *, nowait=False, **_k):
        self._db.nowait_seen = bool(nowait)
        return self

    def populate_existing(self):
        self._db.populate_seen = True
        return self

    def one_or_none(self):
        if self._db.lock_error is not None:
            raise self._db.lock_error
        return self._db.locked_row

    def all(self):
        return list(self._db.rows)


class _LockingDb:
    def __init__(self, rows, *, locked_row=None, lock_error=None):
        self.rows = rows
        self.locked_row = locked_row if locked_row is not None else (rows[0] if rows else None)
        self.lock_error = lock_error
        self.nested_commits = 0
        self.nested_rollbacks = 0
        self.nowait_seen = None
        self.populate_seen = False

    def query(self, *_a, **_k):
        return _LockedQuery(self)

    def begin_nested(self):
        return _Nested(self)

    def flush(self):
        pass


class _PlainDb(_LockingDb):
    """No begin_nested => legacy doubles keep the unlocked path."""
    begin_nested = None


def _row(sid=21605, *, state="live_exited", idle_min=45, symbol="SKYQ", ended_at=None):
    return SimpleNamespace(
        id=sid, symbol=symbol, state=state, mode="live",
        updated_at=datetime.utcnow() - timedelta(minutes=idle_min),
        ended_at=ended_at,
        risk_snapshot_json={"momentum_live_execution": {}},
    )


@pytest.fixture
def transitions(monkeypatch):
    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(LR, "_safe_transition", lambda db, sess, new_state: calls.append((sess, new_state)))
    monkeypatch.setattr(AA.settings, "chili_momentum_exited_finalize_idle_min", 20.0, raising=False)
    return calls


def _lock_refused() -> OperationalError:
    return OperationalError("SELECT ... FOR UPDATE NOWAIT", {}, Exception("could not obtain lock on row"))


# ── the four decisions under the lock ───────────────────────────────────────


def test_row_already_finalized_by_another_pass_is_skipped(transitions):
    """The exact 21605 race: the scan saw live_exited, the lock sees live_finished."""
    snapshot = _row()
    t1 = datetime.utcnow() - timedelta(seconds=1)
    db = _LockingDb([snapshot], locked_row=_row(state="live_finished", ended_at=t1))
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 0
    assert transitions == []
    assert db.nowait_seen is True and db.populate_seen is True
    assert db.nested_rollbacks == 1 and db.nested_commits == 0
    assert snapshot.ended_at is None  # nothing re-stamped the clock


def test_lock_refused_skips_without_a_transition(transitions):
    db = _LockingDb([_row()], lock_error=_lock_refused())
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 0
    assert transitions == []
    assert db.nested_rollbacks == 1


def test_still_exited_and_idle_row_transitions_the_locked_object(transitions):
    snapshot = _row()
    locked = _row()  # a distinct object: the refreshed DB row
    db = _LockingDb([snapshot], locked_row=locked)
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 1
    assert len(transitions) == 1
    assert transitions[0][0] is locked and transitions[0][0] is not snapshot
    assert transitions[0][1] == "live_finished"
    assert db.nested_commits == 1 and db.nested_rollbacks == 0


def test_row_touched_by_a_tick_since_the_scan_is_skipped(transitions):
    db = _LockingDb([_row()], locked_row=_row(idle_min=1))
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 0
    assert transitions == []
    assert db.nested_rollbacks == 1


def test_legacy_db_without_savepoints_keeps_the_unlocked_path(transitions):
    db = _PlainDb([_row()])
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 1
    assert len(transitions) == 1
    assert db.nowait_seen is None


# ── structural pins ─────────────────────────────────────────────────────────


def test_lock_probe_precedes_the_transition_in_source():
    src = _fn_src()
    i_transition = src.index("_live_safe_transition(db, sess, 'live_finished')")
    assert src.index("with_for_update(nowait=True)") < i_transition
    assert src.index("populate_existing()") < i_transition
    assert src.index("begin_nested") < i_transition


def test_scan_and_lock_recheck_share_one_state_tuple():
    src = _fn_src()
    assert src.count("_FINALIZE_SOURCE_STATES") == 2, "scan filter + re-check must use the same tuple"
    assert AA._FINALIZE_SOURCE_STATES == ("live_exited", "live_cooldown")


# ── Postgres: a real lock holder, then one clock in the books ───────────────


def test_postgres_lock_holder_blocks_the_sweep_then_one_clock_survives(db, monkeypatch):
    from app.models.core import User
    from app.models.trading import (
        MomentumAutomationOutcome,
        MomentumStrategyVariant,
        TradingAutomationSession,
    )

    monkeypatch.setattr(AA.settings, "chili_momentum_exited_finalize_idle_min", 20.0, raising=False)
    u = User(name="fin-lock-op"); db.add(u); db.flush()
    v = MomentumStrategyVariant(family="fin", variant_key="fin_lock_v", label="fin", params_json={})
    db.add(v); db.flush()
    sess = TradingAutomationSession(
        user_id=int(u.id), symbol="SKYQ", mode="live", state="live_exited",
        execution_family="robinhood_spot", variant_id=int(v.id),
        risk_snapshot_json={"momentum_live_execution": {"realized_pnl_usd": -61.2}},
        updated_at=datetime.utcnow() - timedelta(minutes=45),
    )
    db.add(sess); db.commit()
    sid = int(sess.id)

    # Pass A holds the row FOR UPDATE (another pass / a live tick mid-write).
    holder = db.get_bind().connect()
    holder_tx = holder.begin()
    holder.execute(text("SELECT id FROM trading_automation_sessions WHERE id = :i FOR UPDATE"), {"i": sid})
    try:
        done = AA._finalize_stale_exited_sessions(db, user_id=int(u.id), now=datetime.utcnow())
        assert done == 0
        db.expire_all()
        assert db.get(TradingAutomationSession, sid).state == "live_exited"
        assert db.query(MomentumAutomationOutcome).filter(
            MomentumAutomationOutcome.session_id == sid
        ).count() == 0
    finally:
        holder_tx.rollback()
        holder.close()

    # Lock released: exactly one pass finalizes, and the books carry ONE clock.
    done = AA._finalize_stale_exited_sessions(db, user_id=int(u.id), now=datetime.utcnow())
    db.commit()
    assert done == 1
    db.expire_all()
    row = db.get(TradingAutomationSession, sid)
    outcome = db.query(MomentumAutomationOutcome).filter(
        MomentumAutomationOutcome.session_id == sid
    ).one()
    assert row.state == "live_finished"
    assert row.ended_at is not None
    assert outcome.terminal_at == row.ended_at
    first_ended_at = row.ended_at

    # A late second pass (the 21605 shape) finds live_finished under the lock: no re-stamp.
    done = AA._finalize_stale_exited_sessions(db, user_id=int(u.id), now=datetime.utcnow())
    db.commit()
    assert done == 0
    db.expire_all()
    row = db.get(TradingAutomationSession, sid)
    assert row.ended_at == first_ended_at
    assert db.query(MomentumAutomationOutcome).filter(
        MomentumAutomationOutcome.session_id == sid
    ).count() == 1
