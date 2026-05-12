"""Phase 1b of f-adaptive-promotion-architecture (2026-05-11).

Locks the behaviour of the flag ``chili_brain_outcome_claimable_enabled``:

* Flag OFF (default) → outcome rows are born terminal (status='done',
  processed_at=now) and ``claim_work_batch`` never returns them. Today's
  behaviour, byte-identical.
* Flag ON → outcome rows are born pending (status='pending',
  processed_at=NULL), and the unified ``claim_work_batch`` claims them
  through the same lifecycle as work rows. Historical done rows stay
  ineligible.

Brief: docs/STRATEGY/QUEUED/f-brain-event-kind-unify.md
"""

from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import text

from app.config import settings
from app.models.trading import BrainWorkEvent
from app.services.trading.brain_work.ledger import (
    claim_work_batch,
    enqueue_outcome_event,
    mark_work_done,
)


def _outcome_row(db, event_id: int) -> BrainWorkEvent:
    db.expire_all()
    row = db.query(BrainWorkEvent).filter(BrainWorkEvent.id == event_id).one()
    return row


def test_flag_off_outcome_born_terminal(db, monkeypatch) -> None:
    """Locks today's behaviour: outcome rows born status='done', never claimed."""
    monkeypatch.setattr(settings, "chili_brain_outcome_claimable_enabled", False)

    eid = enqueue_outcome_event(
        db,
        event_type="backtest_completed",
        dedupe_key="phase1b_off:bt_completed:1",
        payload={"scan_pattern_id": 7001},
    )
    db.commit()
    assert eid is not None

    row = _outcome_row(db, eid)
    assert row.event_kind == "outcome"
    assert row.status == "done"
    assert row.processed_at is not None
    assert row.max_attempts == 0

    rows = claim_work_batch(
        db,
        limit=4,
        lease_seconds=60,
        holder_id="pytest:phase1b_off",
        event_type="backtest_completed",
    )
    db.commit()
    assert rows == [], "flag-off path must never claim outcome rows"


def test_flag_on_outcome_pending_then_claimable(db, monkeypatch) -> None:
    """Flag-on: outcome rows born pending, claim_work_batch picks them up."""
    monkeypatch.setattr(settings, "chili_brain_outcome_claimable_enabled", True)

    eid = enqueue_outcome_event(
        db,
        event_type="backtest_completed",
        dedupe_key="phase1b_on:bt_completed:1",
        payload={"scan_pattern_id": 7002},
    )
    db.commit()
    assert eid is not None

    row = _outcome_row(db, eid)
    assert row.event_kind == "outcome"
    assert row.status == "pending"
    assert row.processed_at is None
    assert row.max_attempts == int(
        getattr(settings, "brain_work_max_attempts_default", 5)
    )

    rows = claim_work_batch(
        db,
        limit=4,
        lease_seconds=60,
        holder_id="pytest:phase1b_on",
        event_type="backtest_completed",
    )
    db.commit()
    assert len(rows) == 1
    assert rows[0].id == eid
    assert rows[0].event_kind == "outcome"
    assert rows[0].status == "processing"

    mark_work_done(db, int(rows[0].id))
    db.commit()
    row = _outcome_row(db, eid)
    assert row.status == "done"
    assert row.processed_at is not None

    rows2 = claim_work_batch(
        db,
        limit=4,
        lease_seconds=60,
        holder_id="pytest:phase1b_on:2",
        event_type="backtest_completed",
    )
    db.commit()
    assert rows2 == [], "completed outcome row must not be re-claimable"

    # Edge case (consult-flagged): same dedupe_key while row is in any
    # status is short-circuited by the dedupe lookup in
    # enqueue_outcome_event — returns the existing id, no UNIQUE violation.
    eid2 = enqueue_outcome_event(
        db,
        event_type="backtest_completed",
        dedupe_key="phase1b_on:bt_completed:1",
        payload={"scan_pattern_id": 7002},
    )
    db.commit()
    assert eid2 == eid


def test_claim_orders_due_rows_by_next_run_at_before_created_at(db, monkeypatch) -> None:
    """A backfill can pace claim order via next_run_at without rewriting history."""
    monkeypatch.setattr(settings, "chili_brain_outcome_claimable_enabled", True)

    older = enqueue_outcome_event(
        db,
        event_type="backtest_completed",
        dedupe_key="phase1b_order:older_created_later_due",
        payload={"scan_pattern_id": 7101},
    )
    newer = enqueue_outcome_event(
        db,
        event_type="backtest_completed",
        dedupe_key="phase1b_order:newer_created_earlier_due",
        payload={"scan_pattern_id": 7102},
    )
    db.commit()
    assert older is not None
    assert newer is not None

    now = datetime.utcnow()
    db.execute(
        text(
            """
            UPDATE brain_work_events
               SET created_at = :older_created,
                   updated_at = :older_created,
                   next_run_at = :older_due
             WHERE id = :older_id
            """
        ),
        {
            "older_id": int(older),
            "older_created": now - timedelta(minutes=10),
            "older_due": now + timedelta(minutes=5),
        },
    )
    db.execute(
        text(
            """
            UPDATE brain_work_events
               SET created_at = :newer_created,
                   updated_at = :newer_created,
                   next_run_at = :newer_due
             WHERE id = :newer_id
            """
        ),
        {
            "newer_id": int(newer),
            "newer_created": now,
            "newer_due": now - timedelta(seconds=1),
        },
    )
    db.commit()

    rows = claim_work_batch(
        db,
        limit=1,
        lease_seconds=60,
        holder_id="pytest:phase1b_order",
        event_type="backtest_completed",
    )
    db.commit()
    assert [int(r.id) for r in rows] == [int(newer)]


def test_flag_on_legacy_done_row_not_reclaimed(db, monkeypatch) -> None:
    """Flag-on: historical status='done' outcome rows stay ineligible.

    Backward-compatibility lock: ~4,000 production rows that pre-date this
    flag have status='done' / processed_at=<historic>. Phase 1c is the
    controlled mechanism to bring them forward; until then they must not
    be claimed by the broadened SQL.
    """
    monkeypatch.setattr(settings, "chili_brain_outcome_claimable_enabled", True)

    now = datetime.utcnow()
    db.execute(
        text(
            """
            INSERT INTO brain_work_events
              (domain, event_type, event_kind, payload, dedupe_key,
               lease_scope, status, attempts, max_attempts,
               next_run_at, correlation_id, created_at, updated_at,
               processed_at)
            VALUES
              ('trading', 'broker_fill_closed', 'outcome',
               '{"scan_pattern_id": 9001}'::jsonb,
               'phase1b_legacy:broker_fill_closed:1',
               'general', 'done', 0, 0,
               :now, 'legacy-corr-1', :now, :now,
               :now)
            """
        ),
        {"now": now},
    )
    db.commit()

    rows = claim_work_batch(
        db,
        limit=8,
        lease_seconds=60,
        holder_id="pytest:phase1b_legacy",
        event_type="broker_fill_closed",
    )
    db.commit()
    assert rows == [], (
        "historical status='done' rows must remain ineligible under flag-on"
    )


def test_flag_on_explicit_audit_outcome_stays_terminal(db, monkeypatch) -> None:
    """Audit-only outcomes can opt out of unified claiming under flag-on."""
    monkeypatch.setattr(settings, "chili_brain_outcome_claimable_enabled", True)

    eid = enqueue_outcome_event(
        db,
        event_type="promotion_changed",
        dedupe_key="phase1b_audit:promotion_changed:1",
        payload={"scan_pattern_id": 7010},
        claimable=False,
    )
    db.commit()
    assert eid is not None

    row = _outcome_row(db, eid)
    assert row.event_kind == "outcome"
    assert row.status == "done"
    assert row.processed_at is not None
    assert row.max_attempts == 0

    rows = claim_work_batch(
        db,
        limit=4,
        lease_seconds=60,
        holder_id="pytest:phase1b_audit",
        event_type="promotion_changed",
    )
    db.commit()
    assert rows == []
