"""Durable brain work ledger (event-first; Postgres)."""

from __future__ import annotations

from app.services.trading.brain_work.ledger import (
    claim_work_batch,
    enqueue_or_refresh_debounced_work,
    enqueue_outcome_event,
    enqueue_work_event,
    get_work_ledger_summary,
    mark_work_done,
    mark_work_retry_or_dead,
    recover_retryable_dead_work,
    release_stale_leases,
)


def test_enqueue_work_open_dedupe_second_returns_none(db) -> None:
    a = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:pattern:424242",
        payload={"scan_pattern_id": 424242, "source": "test"},
    )
    db.commit()
    assert a is not None
    b = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:pattern:424242",
        payload={"scan_pattern_id": 424242, "source": "test"},
    )
    db.commit()
    assert b is None


def test_enqueue_outcome_idempotent(db) -> None:
    k = "bt_done:req:unique-test-1"
    o1 = enqueue_outcome_event(
        db,
        event_type="backtest_completed",
        dedupe_key=k,
        payload={"scan_pattern_id": 1},
    )
    db.commit()
    assert o1 is not None
    o2 = enqueue_outcome_event(
        db,
        event_type="backtest_completed",
        dedupe_key=k,
        payload={"scan_pattern_id": 1},
    )
    db.commit()
    assert o2 == o1


def test_claim_and_complete_work_row(db) -> None:
    eid = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:pattern:777001",
        payload={"scan_pattern_id": 777001, "source": "test"},
    )
    db.commit()
    assert eid is not None
    rows = claim_work_batch(db, limit=4, lease_seconds=60, holder_id="pytest:1", event_type="backtest_requested")
    db.commit()
    assert len(rows) == 1
    assert rows[0].status == "processing"
    mark_work_done(db, int(rows[0].id))
    db.commit()
    rows2 = claim_work_batch(db, limit=4, lease_seconds=60, holder_id="pytest:2", event_type="backtest_requested")
    db.commit()
    assert rows2 == []


def test_claim_prioritizes_expected_evidence_value_within_type(db) -> None:
    low = enqueue_work_event(
        db,
        event_type="exit_variant_refresh",
        dedupe_key="xv:low-evidence",
        payload={"scan_pattern_id": 1001, "expected_evidence_value": 1.0},
        lease_scope="edge",
    )
    high = enqueue_work_event(
        db,
        event_type="exit_variant_refresh",
        dedupe_key="xv:high-evidence",
        payload={"scan_pattern_id": 1002, "expected_evidence_value": 9.0},
        lease_scope="edge",
    )
    db.commit()

    rows = claim_work_batch(db, limit=2, lease_seconds=60, holder_id="pytest:evidence", event_type="exit_variant_refresh")
    db.commit()

    assert [int(row.id) for row in rows] == [high, low]


def test_release_stale_lease_marks_retry(db) -> None:
    from sqlalchemy import text

    eid = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:pattern:777002",
        payload={"scan_pattern_id": 777002, "source": "test"},
    )
    db.commit()
    rows = claim_work_batch(db, limit=4, lease_seconds=3600, holder_id="pytest:stale", event_type="backtest_requested")
    db.commit()
    assert rows
    db.execute(
        text("UPDATE brain_work_events SET lease_expires_at = CURRENT_TIMESTAMP - interval '1 minute' WHERE id = :id"),
        {"id": int(rows[0].id)},
    )
    db.commit()
    n = release_stale_leases(db)
    db.commit()
    assert n >= 1


def test_summary_includes_dead_letter_diagnostics(db) -> None:
    eid = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:dead-letter-diagnostics",
        payload={"scan_pattern_id": 537, "source": "operator_boost"},
        lease_scope="backtest",
        max_attempts=1,
    )
    db.commit()
    assert eid is not None

    rows = claim_work_batch(
        db,
        limit=1,
        lease_seconds=60,
        holder_id="pytest:dead-letter",
        event_type="backtest_requested",
    )
    db.commit()
    assert len(rows) == 1

    mark_work_retry_or_dead(
        db,
        int(rows[0].id),
        "Can't reconnect until invalid transaction is rolled back.",
    )
    db.commit()

    summary = get_work_ledger_summary(db, recent_limit=5)

    assert summary["dead_last_24h"] >= 1
    assert summary["dead_by_type_24h"]["backtest_requested"] >= 1
    recent_dead = [row for row in summary["recent_dead_letters"] if row["id"] == eid]
    assert recent_dead
    row = recent_dead[0]
    assert row["event_type"] == "backtest_requested"
    assert row["lease_scope"] == "backtest"
    assert row["scan_pattern_id"] == 537
    assert row["source"] == "operator_boost"
    assert row["attempts"] == 1
    assert row["max_attempts"] == 1
    assert "Can't reconnect" in row["last_error"]
    assert row["processed_at"] is not None


def test_retryable_dead_work_recovery_requeues_once(db) -> None:
    eid = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:retryable-dead-recovery",
        payload={"scan_pattern_id": 537, "source": "operator_boost"},
        lease_scope="backtest",
        max_attempts=1,
    )
    db.commit()
    assert eid is not None

    rows = claim_work_batch(
        db,
        limit=1,
        lease_seconds=60,
        holder_id="pytest:dead-recovery",
        event_type="backtest_requested",
    )
    db.commit()
    assert len(rows) == 1
    mark_work_retry_or_dead(
        db,
        int(rows[0].id),
        "Can't reconnect until invalid transaction is rolled back.",
    )
    db.commit()

    result = recover_retryable_dead_work(
        db,
        event_types=("backtest_requested",),
        limit=4,
        max_recoveries_per_event=1,
        delay_seconds=0,
    )
    db.commit()

    assert result["recovered"] == 1
    assert result["ids"] == [eid]
    recovered = claim_work_batch(
        db,
        limit=1,
        lease_seconds=60,
        holder_id="pytest:dead-recovered",
        event_type="backtest_requested",
    )
    db.commit()
    assert [int(row.id) for row in recovered] == [eid]
    assert recovered[0].attempts == 1
    payload = recovered[0].payload
    assert payload["transient_dead_recovery_count"] == 1
    assert (
        payload["transient_dead_recovery_marker"]
        == "can't reconnect until invalid transaction is rolled back"
    )

    mark_work_retry_or_dead(
        db,
        int(recovered[0].id),
        "Can't reconnect until invalid transaction is rolled back.",
    )
    db.commit()
    second = recover_retryable_dead_work(
        db,
        event_types=("backtest_requested",),
        limit=4,
        max_recoveries_per_event=1,
        delay_seconds=0,
    )
    db.commit()

    assert second["recovered"] == 0
    assert second["skipped_max_recoveries"] >= 1


def test_enqueue_or_refresh_debounced_merges_payload(db) -> None:
    dk = "exec_fb_digest:user:99001"
    a = enqueue_or_refresh_debounced_work(
        db,
        event_type="execution_feedback_digest",
        dedupe_key=dk,
        payload={"user_id": 99001, "trigger": "a"},
        debounce_seconds=60,
        lease_scope="execution_feedback",
    )
    db.commit()
    assert a is not None
    b = enqueue_or_refresh_debounced_work(
        db,
        event_type="execution_feedback_digest",
        dedupe_key=dk,
        payload={"user_id": 99001, "trigger": "b"},
        debounce_seconds=60,
        lease_scope="execution_feedback",
    )
    db.commit()
    assert b == a
    dup = enqueue_work_event(
        db,
        event_type="execution_feedback_digest",
        dedupe_key=dk,
        payload={"user_id": 99001},
        lease_scope="execution_feedback",
    )
    db.commit()
    assert dup is None
