"""Durable brain work ledger (event-first; Postgres)."""

from __future__ import annotations

from datetime import datetime, timedelta

from app.models.trading import BrainWorkEvent
from app.services.trading.brain_work import ledger as ledger_mod
from app.services.trading.brain_work.emitters import emit_backtest_requested_for_pattern
from app.services.trading.brain_work.ledger import (
    claim_work_batch,
    coalesce_duplicate_open_work,
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


def test_emit_backtest_requested_carries_evidence_payload(db) -> None:
    eid = emit_backtest_requested_for_pattern(
        db,
        424243,
        source="autotrader_shadow_stock_fastlane",
        asset_class="stock",
        expected_evidence_value=1.23456789,
        payload={
            "alert_id": 77,
            "ticker": "FASTL",
            "expected_net_pct": 1.23456789,
            "cash_deployment_category": "positive_ev_shadow",
        },
    )
    db.commit()

    row = db.get(BrainWorkEvent, eid)

    assert row is not None
    assert row.event_type == "backtest_requested"
    assert row.lease_scope == "backtest"
    assert row.payload["scan_pattern_id"] == 424243
    assert row.payload["source"] == "autotrader_shadow_stock_fastlane"
    assert row.payload["asset_class"] == "stock"
    assert row.payload["alert_id"] == 77
    assert row.payload["ticker"] == "FASTL"
    assert row.payload["expected_evidence_value"] == 1.234568
    assert row.payload["expected_net_pct"] == 1.23456789


def test_emit_backtest_requested_refreshes_open_evidence_payload(db) -> None:
    first = emit_backtest_requested_for_pattern(
        db,
        424244,
        source="operator_boost",
    )
    db.commit()

    second = emit_backtest_requested_for_pattern(
        db,
        424244,
        source="autotrader_shadow_stock_fastlane",
        asset_class="stock",
        expected_evidence_value=4.5,
        payload={
            "alert_id": 88,
            "ticker": "BOOST",
            "expected_net_pct": 4.5,
            "cash_deployment_category": "positive_ev_shadow",
        },
    )
    db.commit()

    row = db.get(BrainWorkEvent, first)

    assert second == first
    assert row is not None
    assert row.payload["scan_pattern_id"] == 424244
    assert row.payload["source"] == "operator_boost"
    assert row.payload["latest_source"] == "autotrader_shadow_stock_fastlane"
    assert row.payload["sources"] == [
        "autotrader_shadow_stock_fastlane",
        "operator_boost",
    ]
    assert row.payload["asset_class"] == "stock"
    assert row.payload["alert_id"] == 88
    assert row.payload["ticker"] == "BOOST"
    assert row.payload["expected_evidence_value"] == 4.5


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


def test_retryable_dead_work_default_recovers_multiple_infra_failures(db) -> None:
    eid = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:retryable-dead-default-multi",
        payload={
            "scan_pattern_id": 537,
            "source": "operator_boost",
            "transient_dead_recovery_count": 1,
        },
        lease_scope="backtest",
        max_attempts=1,
    )
    db.commit()
    assert eid is not None

    row = db.get(BrainWorkEvent, eid)
    row.status = "dead"
    row.attempts = 1
    row.processed_at = row.updated_at
    row.last_error = "Can't reconnect until invalid transaction is rolled back."
    db.commit()

    result = recover_retryable_dead_work(
        db,
        event_types=("backtest_requested",),
        limit=4,
        delay_seconds=0,
    )
    db.commit()

    assert result["recovered"] == 1
    assert result["ids"] == [eid]
    assert result["recovered_by_marker"] == {
        "can't reconnect until invalid transaction is rolled back": 1,
    }
    recovered = db.get(BrainWorkEvent, eid)
    assert recovered.status == "retry_wait"
    assert recovered.payload["transient_dead_recovery_count"] == 2


def test_retryable_dead_work_cap_resets_after_cooldown(db, monkeypatch) -> None:
    monkeypatch.setattr(
        ledger_mod,
        "_dead_recovery_cap_reset_delay_seconds",
        lambda value=None: 60,
    )
    monkeypatch.setattr(
        ledger_mod,
        "_dead_recovery_max_cap_resets",
        lambda value=None: 1,
    )
    old_at = datetime.utcnow() - timedelta(minutes=5)
    row = BrainWorkEvent(
        domain="trading",
        event_type="backtest_requested",
        event_kind="work",
        dedupe_key="bt_req:retryable-dead-cap-reset",
        payload={
            "scan_pattern_id": 1016,
            "source": "recert_rescue_refresh",
            "transient_dead_recovery_count": 1,
        },
        lease_scope="backtest",
        status="dead",
        attempts=1,
        max_attempts=1,
        last_error="Can't reconnect until invalid transaction is rolled back.",
        processed_at=old_at,
        updated_at=old_at,
    )
    db.add(row)
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
    assert result["recovered_after_cap_reset"] == 1
    recovered = db.get(BrainWorkEvent, int(row.id))
    assert recovered.status == "retry_wait"
    assert recovered.attempts == 0
    assert recovered.payload["transient_dead_recovery_count"] == 1
    assert recovered.payload["transient_dead_recovery_cap_reset_count"] == 1
    assert recovered.payload["transient_dead_recovery_total_count"] == 1
    assert recovered.payload["transient_dead_recovery_prior_count"] == 1


def test_retryable_dead_work_recovery_skips_duplicate_dedupe(db) -> None:
    dedupe_key = "bt_req:retryable-dead-duplicate-dedupe"
    ids: list[int] = []
    for idx in range(2):
        row = BrainWorkEvent(
            domain="trading",
            event_type="backtest_requested",
            event_kind="work",
            dedupe_key=dedupe_key,
            payload={"scan_pattern_id": 537, "source": f"dead-{idx}"},
            lease_scope="backtest",
            status="dead",
            attempts=1,
            max_attempts=2,
            last_error="Can't reconnect until invalid transaction is rolled back.",
        )
        db.add(row)
        db.flush()
        ids.append(int(row.id))
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
    assert result["skipped_duplicate_dedupe"] == 1
    rows = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.id.in_(ids))
        .order_by(BrainWorkEvent.id.asc())
        .all()
    )
    assert [row.status for row in rows].count("retry_wait") == 1
    assert [row.status for row in rows].count("dead") == 1


def test_coalesce_duplicate_open_work_keeps_one_logical_row(db) -> None:
    keep = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:duplicate-open-537",
        payload={"scan_pattern_id": 537, "source": "operator_boost"},
        lease_scope="backtest",
    )
    db.commit()
    assert keep is not None

    duplicate = BrainWorkEvent(
        domain="trading",
        event_type="backtest_requested",
        event_kind="work",
        dedupe_key="bt_req:duplicate-open-537",
        payload={"scan_pattern_id": 537, "source": "operator_boost"},
        lease_scope="backtest",
        status="retry_wait",
        attempts=4,
        max_attempts=5,
    )
    db.add(duplicate)
    db.commit()

    result = coalesce_duplicate_open_work(
        db,
        event_types=("backtest_requested",),
    )
    db.commit()

    assert result["coalesced"] == 1
    rows = (
        db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.dedupe_key == "bt_req:duplicate-open-537")
        .order_by(BrainWorkEvent.id.asc())
        .all()
    )
    assert len(rows) == 2
    assert rows[0].status == "pending"
    assert rows[1].status == "done"
    assert rows[1].payload["duplicate_open_work_suppressed"] is True


def test_coalesce_duplicate_open_work_thins_recert_rescue_pattern_asset(db) -> None:
    older = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:recert_rescue:p537:stock:older-fp",
        payload={
            "scan_pattern_id": 537,
            "source": "recert_rescue_refresh",
            "asset_class": "stock",
            "evidence_fingerprint": "older-fp",
        },
        lease_scope="backtest",
    )
    newer = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:recert_rescue:p537:stock:newer-fp",
        payload={
            "scan_pattern_id": 537,
            "source": "recert_rescue_refresh",
            "asset_class": "stock",
            "evidence_fingerprint": "newer-fp",
        },
        lease_scope="backtest",
    )
    other_asset = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:recert_rescue:p537:crypto:newer-fp",
        payload={
            "scan_pattern_id": 537,
            "source": "recert_rescue_refresh",
            "asset_class": "crypto",
            "evidence_fingerprint": "newer-fp",
        },
        lease_scope="backtest",
    )
    db.commit()
    assert older is not None
    assert newer is not None
    assert other_asset is not None

    result = coalesce_duplicate_open_work(
        db,
        event_types=("backtest_requested",),
    )
    db.commit()

    rows = {
        int(row.id): row
        for row in db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.id.in_([older, newer, other_asset]))
        .all()
    }
    assert result["coalesced"] == 1
    assert result["reasons"] == {"recert_rescue_pattern_asset_superseded": 1}
    assert rows[older].status == "done"
    assert rows[older].payload["duplicate_open_work_suppressed"] is True
    assert (
        rows[older].payload["duplicate_open_work_suppressed_reason"]
        == "recert_rescue_pattern_asset_superseded"
    )
    assert rows[older].payload["duplicate_open_work_kept_event_id"] == newer
    assert rows[newer].status == "pending"
    assert rows[other_asset].status == "pending"


def test_coalesce_duplicate_open_work_prefers_recert_rescue_over_operator_boost(
    db,
) -> None:
    recert = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:recert_rescue:p537:stock:recert-fp",
        payload={
            "scan_pattern_id": 537,
            "source": "recert_rescue_refresh",
            "asset_class": "stock",
            "evidence_fingerprint": "recert-fp",
        },
        lease_scope="backtest",
    )
    generic = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:operator_boost:p537:generic",
        payload={"scan_pattern_id": 537, "source": "operator_boost"},
        lease_scope="backtest",
    )
    running_generic = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:operator_boost:p537:running",
        payload={"scan_pattern_id": 537, "source": "operator_boost"},
        lease_scope="backtest",
    )
    other_pattern = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key="bt_req:operator_boost:p538:generic",
        payload={"scan_pattern_id": 538, "source": "operator_boost"},
        lease_scope="backtest",
    )
    db.commit()
    assert recert is not None
    assert generic is not None
    assert running_generic is not None
    assert other_pattern is not None

    running = db.get(BrainWorkEvent, running_generic)
    running.status = "processing"
    running.lease_holder = "pytest:operator-boost-running"
    running.lease_expires_at = datetime.utcnow() + timedelta(minutes=5)
    db.commit()

    result = coalesce_duplicate_open_work(
        db,
        event_types=("backtest_requested",),
    )
    db.commit()

    rows = {
        int(row.id): row
        for row in db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.id.in_([recert, generic, running_generic, other_pattern]))
        .all()
    }
    assert result["coalesced"] == 1
    assert result["reasons"] == {
        "operator_boost_backtest_superseded_by_recert_rescue": 1,
    }
    assert rows[recert].status == "pending"
    assert rows[generic].status == "done"
    assert (
        rows[generic].payload["duplicate_open_work_suppressed_reason"]
        == "operator_boost_backtest_superseded_by_recert_rescue"
    )
    assert rows[generic].payload["duplicate_open_work_kept_event_id"] == recert
    assert rows[running_generic].status == "processing"
    assert rows[other_pattern].status == "pending"


def test_coalesce_duplicate_open_work_thins_exit_variant_by_parent_pattern(db) -> None:
    low_value = enqueue_work_event(
        db,
        event_type="exit_variant_refresh",
        dedupe_key="exit_variant_refresh:p537:astock:low-fp",
        payload={
            "scan_pattern_id": 537,
            "asset_class": "stock",
            "expected_evidence_value": 1.25,
            "calibrated_ev_after_cost_pct": 0.2,
        },
        lease_scope="evolution",
    )
    high_value = enqueue_work_event(
        db,
        event_type="exit_variant_refresh",
        dedupe_key="exit_variant_refresh:p537:acrypto:high-fp",
        payload={
            "scan_pattern_id": 537,
            "asset_class": "crypto",
            "expected_evidence_value": 5.0,
            "calibrated_ev_after_cost_pct": 0.6,
        },
        lease_scope="evolution",
    )
    other_pattern = enqueue_work_event(
        db,
        event_type="exit_variant_refresh",
        dedupe_key="exit_variant_refresh:p538:astock:other-fp",
        payload={
            "scan_pattern_id": 538,
            "asset_class": "stock",
            "expected_evidence_value": 0.5,
        },
        lease_scope="evolution",
    )
    db.commit()
    assert low_value is not None
    assert high_value is not None
    assert other_pattern is not None

    result = coalesce_duplicate_open_work(
        db,
        event_types=("exit_variant_refresh",),
    )
    db.commit()

    rows = {
        int(row.id): row
        for row in db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.id.in_([low_value, high_value, other_pattern]))
        .all()
    }
    assert result["coalesced"] == 1
    assert result["reasons"] == {"exit_variant_pattern_superseded": 1}
    assert rows[low_value].status == "done"
    assert (
        rows[low_value].payload["duplicate_open_work_suppressed_reason"]
        == "exit_variant_pattern_superseded"
    )
    assert rows[low_value].payload["duplicate_open_work_kept_event_id"] == high_value
    assert rows[high_value].status == "pending"
    assert rows[other_pattern].status == "pending"


def test_coalesce_duplicate_open_work_keeps_processing_exit_variant(db) -> None:
    now = datetime.utcnow()
    processing = BrainWorkEvent(
        domain="trading",
        event_type="exit_variant_refresh",
        event_kind="work",
        dedupe_key="exit_variant_refresh:p537:astock:processing-fp",
        payload={
            "scan_pattern_id": 537,
            "asset_class": "stock",
            "expected_evidence_value": 1.0,
        },
        lease_scope="evolution",
        status="processing",
        attempts=1,
        max_attempts=5,
        next_run_at=now - timedelta(minutes=2),
        lease_holder="pytest:evolution",
        lease_expires_at=now + timedelta(minutes=10),
        created_at=now - timedelta(minutes=3),
        updated_at=now - timedelta(minutes=1),
    )
    queued = BrainWorkEvent(
        domain="trading",
        event_type="exit_variant_refresh",
        event_kind="work",
        dedupe_key="exit_variant_refresh:p537:acrypto:queued-fp",
        payload={
            "scan_pattern_id": 537,
            "asset_class": "crypto",
            "expected_evidence_value": 50.0,
        },
        lease_scope="evolution",
        status="pending",
        attempts=0,
        max_attempts=5,
        next_run_at=now,
        created_at=now,
        updated_at=now,
    )
    db.add_all([processing, queued])
    db.commit()
    ids = [int(processing.id), int(queued.id)]

    result = coalesce_duplicate_open_work(
        db,
        event_types=("exit_variant_refresh",),
    )
    db.commit()

    rows = {
        int(row.id): row
        for row in db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.id.in_(ids))
        .all()
    }
    assert result["coalesced"] == 1
    assert result["reasons"] == {"exit_variant_pattern_superseded_by_processing": 1}
    assert rows[int(processing.id)].status == "processing"
    assert rows[int(queued.id)].status == "done"
    assert rows[int(queued.id)].payload["duplicate_open_work_kept_event_id"] == int(
        processing.id
    )


def test_coalesce_duplicate_open_work_keeps_latest_queued_market_snapshot_batch(db) -> None:
    now = datetime.utcnow()

    older = BrainWorkEvent(
        domain="trading",
        event_type="market_snapshots_batch",
        event_kind="outcome",
        dedupe_key="mine:older",
        payload={"job_id": "older", "snapshots_taken_daily": 120},
        lease_scope="mine",
        status="pending",
        attempts=0,
        max_attempts=5,
        next_run_at=now - timedelta(minutes=4),
        created_at=now - timedelta(minutes=4),
        updated_at=now - timedelta(minutes=4),
    )
    middle = BrainWorkEvent(
        domain="trading",
        event_type="market_snapshots_batch",
        event_kind="outcome",
        dedupe_key="mine:middle",
        payload={"job_id": "middle", "snapshots_taken_daily": 120},
        lease_scope="mine",
        status="pending",
        attempts=0,
        max_attempts=5,
        next_run_at=now - timedelta(minutes=2),
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=2),
    )
    latest = BrainWorkEvent(
        domain="trading",
        event_type="market_snapshots_batch",
        event_kind="outcome",
        dedupe_key="mine:latest",
        payload={"job_id": "latest", "snapshots_taken_daily": 120},
        lease_scope="mine",
        status="retry_wait",
        attempts=1,
        max_attempts=5,
        next_run_at=now - timedelta(minutes=1),
        created_at=now - timedelta(minutes=1),
        updated_at=now - timedelta(minutes=1),
    )
    db.add_all([older, middle, latest])
    db.commit()
    ids = [int(older.id), int(middle.id), int(latest.id)]

    result = coalesce_duplicate_open_work(
        db,
        event_types=("market_snapshots_batch",),
    )
    db.commit()

    rows = {
        int(row.id): row
        for row in db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.id.in_(ids))
        .all()
    }
    assert result["coalesced"] == 2
    assert result["reasons"] == {"market_snapshot_batch_superseded": 2}
    assert rows[int(latest.id)].status == "retry_wait"
    assert rows[int(older.id)].status == "done"
    assert rows[int(middle.id)].status == "done"
    assert rows[int(older.id)].payload["duplicate_open_work_kept_event_id"] == int(latest.id)
    assert rows[int(middle.id)].payload["duplicate_open_work_kept_event_id"] == int(latest.id)


def test_coalesce_duplicate_open_work_retires_queued_market_snapshot_covered_by_processing(
    db,
) -> None:
    now = datetime.utcnow()

    older_retry = BrainWorkEvent(
        domain="trading",
        event_type="market_snapshots_batch",
        event_kind="outcome",
        dedupe_key="mine:older-retry",
        payload={"job_id": "older-retry", "snapshots_taken_daily": 120},
        lease_scope="mine",
        status="retry_wait",
        attempts=1,
        max_attempts=5,
        next_run_at=now - timedelta(minutes=10),
        created_at=now - timedelta(minutes=10),
        updated_at=now - timedelta(minutes=5),
    )
    processing = BrainWorkEvent(
        domain="trading",
        event_type="market_snapshots_batch",
        event_kind="outcome",
        dedupe_key="mine:newer-processing",
        payload={"job_id": "newer-processing", "snapshots_taken_daily": 120},
        lease_scope="mine",
        status="processing",
        attempts=1,
        max_attempts=5,
        next_run_at=now - timedelta(minutes=2),
        lease_holder="pytest:mine",
        lease_expires_at=now + timedelta(minutes=10),
        created_at=now - timedelta(minutes=2),
        updated_at=now - timedelta(minutes=2),
    )
    covered_pending = BrainWorkEvent(
        domain="trading",
        event_type="market_snapshots_batch",
        event_kind="outcome",
        dedupe_key="mine:covered-pending",
        payload={"job_id": "covered-pending", "snapshots_taken_daily": 120},
        lease_scope="mine",
        status="pending",
        attempts=0,
        max_attempts=5,
        next_run_at=now,
        created_at=now + timedelta(seconds=1),
        updated_at=now + timedelta(seconds=1),
    )
    outside_grace_pending = BrainWorkEvent(
        domain="trading",
        event_type="market_snapshots_batch",
        event_kind="outcome",
        dedupe_key="mine:outside-grace-pending",
        payload={"job_id": "outside-grace-pending", "snapshots_taken_daily": 120},
        lease_scope="mine",
        status="pending",
        attempts=0,
        max_attempts=5,
        next_run_at=now,
        created_at=now + timedelta(minutes=20),
        updated_at=now + timedelta(minutes=20),
    )
    db.add_all([older_retry, processing, covered_pending, outside_grace_pending])
    db.commit()
    ids = [
        int(older_retry.id),
        int(processing.id),
        int(covered_pending.id),
        int(outside_grace_pending.id),
    ]

    result = coalesce_duplicate_open_work(
        db,
        event_types=("market_snapshots_batch",),
    )
    db.commit()

    rows = {
        int(row.id): row
        for row in db.query(BrainWorkEvent)
        .filter(BrainWorkEvent.id.in_(ids))
        .all()
    }
    assert result["coalesced"] == 2
    assert result["reasons"] == {"market_snapshot_batch_superseded_by_processing": 2}
    assert rows[int(older_retry.id)].status == "done"
    assert (
        rows[int(older_retry.id)].payload["duplicate_open_work_suppressed_reason"]
        == "market_snapshot_batch_superseded_by_processing"
    )
    assert rows[int(older_retry.id)].payload["duplicate_open_work_kept_event_id"] == int(
        processing.id
    )
    assert rows[int(processing.id)].status == "processing"
    assert rows[int(covered_pending.id)].status == "done"
    assert rows[int(covered_pending.id)].payload["duplicate_open_work_kept_event_id"] == int(
        processing.id
    )
    assert rows[int(outside_grace_pending.id)].status == "pending"


def test_enqueue_work_reuses_retryable_dead_dedupe(db, monkeypatch) -> None:
    monkeypatch.setattr(
        ledger_mod,
        "_dead_recovery_max_per_event",
        lambda value=None: 1,
    )
    dedupe_key = "bt_req:retryable-dead-dedupe-reuse"
    eid = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key=dedupe_key,
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
        holder_id="pytest:dead-dedupe",
        event_type="backtest_requested",
    )
    db.commit()
    mark_work_retry_or_dead(
        db,
        int(rows[0].id),
        "Can't reconnect until invalid transaction is rolled back.",
    )
    db.commit()

    reused = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key=dedupe_key,
        payload={"scan_pattern_id": 537, "source": "operator_boost_again"},
        lease_scope="backtest",
        max_attempts=2,
    )
    db.commit()

    assert reused == eid
    all_rows = db.query(BrainWorkEvent).filter(BrainWorkEvent.dedupe_key == dedupe_key).all()
    assert [int(row.id) for row in all_rows] == [eid]
    row = all_rows[0]
    assert row.status == "retry_wait"
    assert row.attempts == 1
    assert row.max_attempts == 2
    assert row.payload["source"] == "operator_boost_again"
    assert row.payload["transient_dead_recovery_count"] == 1
    assert (
        row.payload["transient_dead_recovery_marker"]
        == "can't reconnect until invalid transaction is rolled back"
    )


def test_enqueue_work_suppresses_retryable_dead_dedupe_after_recovery_cap(
    db,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        ledger_mod,
        "_dead_recovery_max_per_event",
        lambda value=None: 1,
    )
    dedupe_key = "bt_req:retryable-dead-dedupe-cap"
    eid = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key=dedupe_key,
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
        holder_id="pytest:dead-dedupe-cap",
        event_type="backtest_requested",
    )
    db.commit()
    mark_work_retry_or_dead(
        db,
        int(rows[0].id),
        "Can't reconnect until invalid transaction is rolled back.",
    )
    row = db.get(BrainWorkEvent, eid)
    row.payload = {
        **(row.payload or {}),
        "transient_dead_recovery_count": 1,
    }
    db.commit()

    duplicate = enqueue_work_event(
        db,
        event_type="backtest_requested",
        dedupe_key=dedupe_key,
        payload={"scan_pattern_id": 537, "source": "operator_boost_again"},
        lease_scope="backtest",
        max_attempts=2,
    )
    db.commit()

    assert duplicate is None
    all_rows = db.query(BrainWorkEvent).filter(BrainWorkEvent.dedupe_key == dedupe_key).all()
    assert [int(row.id) for row in all_rows] == [eid]
    assert all_rows[0].status == "dead"


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
