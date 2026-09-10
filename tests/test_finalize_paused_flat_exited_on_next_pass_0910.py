"""[54] A PAUSED + broker-FLAT exited session finalizes on the NEXT pass, not after 20 idle minutes.

``live_exited`` is ledger-terminal for the loss guard, but its outcome is booked only at
``live_finished`` — which the sweep performs after ``chili_momentum_exited_finalize_idle_min``. A
session flattened under an automation_monitor pause has nobody to advance it, so the WHOLE account sat at
``loss_guard_history_unavailable`` (``loss_guard_terminal_outcome_unavailable``): DBGI 21649
18:12:37Z→18:23:04Z and SKYQ 21605 14:34:53Z→14:54:55Z on 2026-09-10; 11 flattened sessions in 14 days,
mean 17.8 min (max 21.9) in ``live_exited``.

Eligibility (all four, fail-closed): ``operator_pause.active``; no ``position`` in ``le``;
``le.last_exit_at_utc`` present; ``le.emergency_position_truth == 'broker_zero'``.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.services.trading.momentum_neural import auto_arm as AA
from app.services.trading.momentum_neural import live_runner as LR

from tests.test_finalize_sweep_decides_under_row_lock_0910 import _LockingDb, _PlainDb  # noqa: F401


def _flat_le(**over):
    le = {
        "last_exit_at_utc": "2026-09-10T18:12:37.162013",
        "last_exit_reason": "operator_flatten",
        "emergency_position_truth": "broker_zero",
    }
    le.update(over)
    return le


def _row(sid=21649, *, state="live_exited", idle_min=1, paused=True, le=None, symbol="DBGI"):
    snap = {"momentum_live_execution": _flat_le() if le is None else le}
    if paused:
        snap["operator_pause"] = {
            "active": True, "resume_state": "watching_live",
            "deferral_pending": "durable_alpaca_entry_claim_reconcile",
            "deferral_initiator": "automation_monitor",
        }
    return SimpleNamespace(
        id=sid, symbol=symbol, state=state, mode="live",
        updated_at=datetime.utcnow() - timedelta(minutes=idle_min), ended_at=None,
        risk_snapshot_json=snap,
    )


@pytest.fixture
def transitions(monkeypatch):
    calls: list[tuple[object, str]] = []
    monkeypatch.setattr(LR, "_safe_transition", lambda db, sess, new_state: calls.append((sess, new_state)))
    monkeypatch.setattr(AA.settings, "chili_momentum_exited_finalize_idle_min", 20.0, raising=False)
    return calls


# ── the predicate, all four legs ─────────────────────────────────────────────


def test_the_dbgi_shape_is_eligible():
    assert AA._exited_paused_and_flat(_row()) is True


@pytest.mark.parametrize("mutate", [
    lambda r: setattr(r, "state", "live_finished"),
    lambda r: r.risk_snapshot_json.pop("operator_pause"),
    lambda r: r.risk_snapshot_json["operator_pause"].__setitem__("active", False),
    lambda r: r.risk_snapshot_json["momentum_live_execution"].__setitem__("position", {"side": "long", "qty": 82}),
    lambda r: r.risk_snapshot_json["momentum_live_execution"].pop("last_exit_at_utc"),
    lambda r: r.risk_snapshot_json["momentum_live_execution"].__setitem__("emergency_position_truth", "unknown"),
    lambda r: r.risk_snapshot_json["momentum_live_execution"].pop("emergency_position_truth"),
    lambda r: setattr(r, "risk_snapshot_json", None),
])
def test_any_missing_leg_is_not_eligible(mutate):
    r = _row()
    mutate(r)
    assert AA._exited_paused_and_flat(r) is False


def test_predicate_never_raises():
    assert AA._exited_paused_and_flat(object()) is False
    assert AA._exited_paused_and_flat(None) is False


# ── the sweep: paused+flat finalizes NOW; a fresh unpaused row still waits ──


def test_paused_flat_fresh_row_finalizes_on_this_pass(transitions):
    row = _row(idle_min=1)
    db = _LockingDb([row], locked_row=row)
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 1
    assert transitions == [(row, "live_finished")]
    assert db.nested_commits == 1


def test_fresh_unpaused_exited_row_still_waits_for_the_idle_window(transitions):
    row = _row(idle_min=1, paused=False)
    db = _LockingDb([row], locked_row=row)
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 0
    assert transitions == []


def test_paused_but_still_holding_a_position_waits(transitions):
    row = _row(idle_min=1, le=_flat_le(position={"side": "long", "qty": 82}))
    db = _LockingDb([row], locked_row=row)
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 0
    assert transitions == []


def test_paused_flat_row_that_another_pass_already_finalized_is_skipped(transitions):
    snapshot = _row(idle_min=1)
    db = _LockingDb([snapshot], locked_row=_row(idle_min=1, state="live_finished"))
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 0
    assert transitions == []


def test_legacy_db_applies_the_same_rule(transitions):
    fresh_unpaused = _row(idle_min=1, paused=False)
    fresh_paused_flat = _row(sid=2, idle_min=1)
    db = _PlainDb([fresh_unpaused, fresh_paused_flat])
    done = AA._finalize_stale_exited_sessions(db, user_id=1, now=datetime.utcnow())
    assert done == 1
    assert [s.id for s, _ in transitions] == [2]


# ── Postgres: the SQL prefilter admits the paused row without the idle window ─


def test_postgres_paused_flat_row_is_finalized_immediately_and_books_one_clock(db, monkeypatch):
    from app.models.core import User
    from app.models.trading import MomentumAutomationOutcome, MomentumStrategyVariant, TradingAutomationSession

    monkeypatch.setattr(AA.settings, "chili_momentum_exited_finalize_idle_min", 20.0, raising=False)
    u = User(name="fin-paused-op"); db.add(u); db.flush()
    v = MomentumStrategyVariant(family="fin", variant_key="fin_paused_v", label="fin", params_json={})
    db.add(v); db.flush()
    paused_flat = TradingAutomationSession(
        user_id=int(u.id), symbol="DBGI", mode="live", state="live_exited",
        execution_family="robinhood_spot", variant_id=int(v.id),
        risk_snapshot_json={
            "momentum_live_execution": {**_flat_le(), "realized_pnl_usd": 0.0},
            "operator_pause": {"active": True, "resume_state": "watching_live",
                               "deferral_pending": "durable_alpaca_entry_claim_reconcile",
                               "deferral_initiator": "automation_monitor"},
        },
        updated_at=datetime.utcnow() - timedelta(seconds=30),
    )
    fresh_unpaused = TradingAutomationSession(
        user_id=int(u.id), symbol="QH", mode="live", state="live_exited",
        execution_family="robinhood_spot", variant_id=int(v.id),
        risk_snapshot_json={"momentum_live_execution": {"realized_pnl_usd": -7.0}},
        updated_at=datetime.utcnow() - timedelta(seconds=30),
    )
    db.add_all([paused_flat, fresh_unpaused]); db.commit()
    sid, other = int(paused_flat.id), int(fresh_unpaused.id)

    done = AA._finalize_stale_exited_sessions(db, user_id=int(u.id), now=datetime.utcnow())
    db.commit()
    assert done == 1
    db.expire_all()
    row = db.get(TradingAutomationSession, sid)
    assert row.state == "live_finished" and row.ended_at is not None
    outcome = db.query(MomentumAutomationOutcome).filter(MomentumAutomationOutcome.session_id == sid).one()
    assert outcome.terminal_at == row.ended_at
    assert db.get(TradingAutomationSession, other).state == "live_exited"
    # idempotent: the next pass touches nothing
    assert AA._finalize_stale_exited_sessions(db, user_id=int(u.id), now=datetime.utcnow()) == 0
    db.commit()
    assert db.execute(text("SELECT count(*) FROM momentum_automation_outcomes WHERE session_id=:s"), {"s": sid}).scalar() == 1
