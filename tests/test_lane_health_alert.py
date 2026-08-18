"""Lane-health FROZEN alert (the 2026-06-15 silent-frozen-lane incident).

A tripped safety breaker (global kill switch / per-broker daily-loss block) silently
empties the momentum lane. On 06-15 the global daily-loss kill switch tripped at 05:18
ET and the lane sat empty ~8h before the operator noticed. These tests pin the loud
signal:
  * frozen ON a held kill switch / per-broker block (past the adaptive grace);
  * NOT frozen within grace, and NOT frozen on a quiet-but-healthy lane (the pass keeps
    executing — anti-false-positive);
  * the reversible env kill-switch fully disables it;
  * change-only / cooldown so a long freeze keeps nagging without spamming;
  * a durable audit row in trading_alerts (the cockpit/notification log).
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import text

from app.config import settings
from app.models.trading import AlertHistory, BrainBatchJob
from app.services.trading import governance as gov
from app.services.trading.batch_job_constants import (
    IQFEED_EXACT_PRINT_HEARTBEAT_SCHEMA,
    IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
    JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
    JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
    JOB_SCHEDULER_WORKER_HEARTBEAT,
)
from app.services.trading.momentum_neural import lane_health as lh


_PAPER_ACCOUNT_ID = "3e0776af-76cd-4afd-8fe1-f2ee8dc6242f"
_PAPER_RUNTIME_GENERATION = "64fb7911-1a67-4e2c-a1ca-73cbe6efe5c6"


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    """Clean governance + lane_health module state; in-process kill switch authoritative
    (disable the DB poll so the test's state is not overwritten); small explicit grace."""
    # governance
    gov.deactivate_kill_switch()
    with gov._per_broker_lock:
        gov._per_broker_daily_loss.clear()
    # lane_health module state
    with lh._alert_lock:
        lh._last_alert_signature = None
        lh._last_alert_at_monotonic = None
    with lh._heartbeat_lock:
        lh._auto_arm_last_run_monotonic = None
        lh._auto_arm_last_run_wall = None
    # settings
    monkeypatch.setattr(settings, "chili_kill_switch_db_poll_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_lane_health_alert_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_lane_health_freeze_alert_seconds", 60.0, raising=False)
    monkeypatch.setattr(
        settings,
        "chili_lane_health_live_loop_stale_seconds",
        75.0,
        raising=False,
    )
    # Default the lane OFF so condition (c) only fires when a test opts in via
    # _enable_lane — independent of the operator's ambient .env (where it may be on).
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", False, raising=False)
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_equity_only", False, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_crypto_only", True, raising=False
    )
    yield
    gov.deactivate_kill_switch()
    with gov._per_broker_lock:
        gov._per_broker_daily_loss.clear()


def _age_kill_switch(seconds: float) -> None:
    """Pretend the (already-active) kill switch was set `seconds` ago."""
    with gov._kill_switch_lock:
        gov._kill_switch_set_at = datetime.utcnow() - timedelta(seconds=seconds)


# ── (a) Global kill switch ────────────────────────────────────────────────

def test_kill_switch_frozen_after_grace(db):
    gov.activate_kill_switch("global_daily_loss_breach_coinbase_spot_$60")
    _age_kill_switch(8 * 3600)  # the 06-15 ~8h freeze
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is True
    assert r["severity"] == "critical"
    assert "kill switch" in r["headline"].lower()
    assert any(c["kind"] == "kill_switch" and c["frozen"] for c in r["conditions"])


def test_kill_switch_within_grace_not_frozen(db):
    gov.activate_kill_switch("manual")
    _age_kill_switch(5)  # just tripped — a brief deliberate halt must not cry wolf
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is False
    # ...but the condition is still surfaced (armed), within grace.
    assert any(c["kind"] == "kill_switch" and not c["frozen"] for c in r["conditions"])


# ── (b) Per-broker daily-loss block ───────────────────────────────────────

def test_broker_block_frozen_after_grace(db):
    gov.set_broker_daily_loss_block(
        "coinbase_spot", reason="broker_daily_loss_breach_coinbase_spot_pct_$36",
        realized=-40.0, limit=36.0,
    )
    with gov._per_broker_lock:
        gov._per_broker_daily_loss["coinbase_spot"]["set_at"] = datetime.utcnow() - timedelta(hours=2)
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is True
    cond = next(c for c in r["conditions"] if c["kind"] == "broker_block")
    assert cond["family"] == "coinbase_spot"
    assert cond["frozen"] is True
    assert "coinbase_spot" in r["headline"]


def test_broker_block_within_grace_not_frozen(db):
    gov.set_broker_daily_loss_block(
        "robinhood_spot", reason="x", realized=-200.0, limit=189.0,
    )  # set_at = now
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is False


# ── reversible env kill-switch ────────────────────────────────────────────

def test_alert_disabled_flag_off(db, monkeypatch):
    monkeypatch.setattr(settings, "chili_lane_health_alert_enabled", False, raising=False)
    gov.activate_kill_switch("manual")
    _age_kill_switch(8 * 3600)
    r = lh.evaluate_lane_health(db)
    assert r["enabled"] is False
    assert r["frozen"] is False
    assert r["conditions"] == []


# ── (c) starvation: distinguish a wedged lane from a quiet market ──────────

def _enable_lane(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_loop_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_live_scheduler_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_crypto_only", True, raising=False)  # 24/7


def _heartbeat(db, *, age_seconds: float) -> None:
    db.add(BrainBatchJob(
        id=str(uuid.uuid4()),
        job_type=JOB_SCHEDULER_WORKER_HEARTBEAT,
        status="ok",
        started_at=datetime.utcnow() - timedelta(seconds=age_seconds + 1),
        ended_at=datetime.utcnow() - timedelta(seconds=age_seconds),
    ))
    db.commit()


def _live_loop_heartbeat(
    db,
    *,
    age_seconds: float,
    status: str = "ok",
    completed: bool = True,
    owner_instance_id: str | None = None,
    generation: int = 1,
    generation_started_age_seconds: float | None = None,
    malformed_meta: bool = False,
    captured_paper: bool = False,
    expected_account_id: str = _PAPER_ACCOUNT_ID,
    runtime_generation: str = _PAPER_RUNTIME_GENERATION,
    tamper_content: bool = False,
) -> None:
    now = datetime.utcnow()
    heartbeat_at = now - timedelta(seconds=age_seconds)
    ended_at = (
        heartbeat_at
        if completed
        else None
    )
    owner_instance_id = owner_instance_id or str(uuid.uuid4())
    generation_started_age_seconds = (
        age_seconds + 120.0
        if generation_started_age_seconds is None
        else generation_started_age_seconds
    )
    generation_started_at = (
        datetime.now(timezone.utc)
        - timedelta(seconds=generation_started_age_seconds)
    )
    meta = {
        "schema": lh.LIVE_LOOP_HEARTBEAT_SCHEMA,
        "scope": lh.LIVE_LOOP_HEARTBEAT_SCOPE,
        "owner": "momentum_live_runner_loop",
        "owner_instance_id": owner_instance_id,
        "generation": generation,
        "generation_identity": f"{owner_instance_id}:{generation}",
        "generation_started_at_utc": (
            generation_started_at.isoformat().replace("+00:00", "Z")
        ),
    }
    if captured_paper:
        meta.update(
            {
                "schema": lh.LIVE_LOOP_HEARTBEAT_SCHEMA_V2,
                "account_scope": "alpaca:paper",
                "expected_account_id": expected_account_id,
                "runtime_generation": runtime_generation,
                "execution_family": "alpaca_spot",
                "live_cash_authorized": False,
                "row_started_at_utc": (
                    (heartbeat_at - timedelta(seconds=1))
                    .replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
                "heartbeat_at_utc": (
                    heartbeat_at.replace(tzinfo=timezone.utc)
                    .isoformat()
                    .replace("+00:00", "Z")
                ),
            }
        )
        meta["content_sha256"] = lh._heartbeat_content_sha256(meta)
        if tamper_content:
            meta["runtime_generation"] = str(uuid.uuid4())
    if malformed_meta:
        meta.pop("generation_identity")
    db.add(BrainBatchJob(
        id=str(uuid.uuid4()),
        job_type=JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
        status=status,
        started_at=heartbeat_at - timedelta(seconds=1),
        ended_at=ended_at,
        meta_json=meta,
    ))
    db.commit()


def test_scheduler_down_frozen(db, monkeypatch):
    _enable_lane(monkeypatch)  # lane on, no breaker, no heartbeat at all
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is True
    assert any(c["kind"] == "scheduler_down" and c["frozen"] for c in r["conditions"])


def test_quiet_market_not_frozen(db, monkeypatch):
    """The anti-false-positive case: a healthy lane that simply has no setup. The
    scheduler is alive AND the auto-arm pass keeps executing — so NOT frozen."""
    _enable_lane(monkeypatch)
    _heartbeat(db, age_seconds=2)        # scheduler alive
    lh.record_auto_arm_run()             # auto-arm pass just ran
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is False
    assert r["conditions"] == []


def test_auto_arm_stalled_frozen(db, monkeypatch):
    """Scheduler alive but the auto-arm job specifically wedged (heartbeat stale)."""
    _enable_lane(monkeypatch)
    _heartbeat(db, age_seconds=2)        # scheduler alive
    with lh._heartbeat_lock:             # auto-arm last ran 10 min ago
        import time as _t
        lh._auto_arm_last_run_monotonic = _t.monotonic() - 600.0
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is True
    assert any(c["kind"] == "auto_arm_stalled" and c["frozen"] for c in r["conditions"])


def _enable_event_lane(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_scheduler_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_live_runner_loop_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_autopilot_price_bus_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_ross_event_admission_enabled", True, raising=False)
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_loop_iqfeed_notify_enabled",
        True,
        raising=False,
    )
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_crypto_only", True, raising=False)


def _enable_equity_event_lane(monkeypatch):
    _enable_event_lane(monkeypatch)
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_equity_only", True, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_crypto_only", False, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_arm_tape_freshness_max_sec", 300.0
    )


def test_event_loop_heartbeat_keeps_quiet_lane_healthy(db, monkeypatch):
    _enable_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=2)
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is False
    assert r["conditions"] == []


def test_equity_event_loop_stale_exact_print_is_loud_even_with_fresh_heartbeat(
    db,
    monkeypatch,
):
    _enable_equity_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=2, captured_paper=True)
    monkeypatch.setattr(lh, "_iqfeed_data_session_age_seconds", lambda: 601.0)
    monkeypatch.setattr(
        lh,
        "_latest_exact_iqfeed_print_status",
        lambda _db: {
            "ok": True,
            "provider_event_at": datetime.utcnow() - timedelta(seconds=601),
            "received_at": datetime.utcnow() - timedelta(seconds=1),
            "available_at": datetime.utcnow() - timedelta(seconds=1),
        },
    )

    r = lh.evaluate_lane_health(db)

    cond = next(c for c in r["conditions"] if c["kind"] == "equity_tape_stalled")
    assert r["frozen"] is True
    assert cond["reason"] == "iqfeed_exact_print_provider_stale"
    assert cond["elapsed_seconds"] >= 600.0
    assert cond["stale_seconds"] == 300.0


def test_equity_event_loop_missing_exact_print_is_loud(db, monkeypatch):
    _enable_equity_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=2, captured_paper=True)
    monkeypatch.setattr(lh, "_iqfeed_data_session_age_seconds", lambda: 301.0)
    monkeypatch.setattr(
        lh,
        "_latest_exact_iqfeed_print_status",
        lambda _db: {"ok": False, "reason": "iqfeed_exact_print_missing"},
    )

    r = lh.evaluate_lane_health(db)

    cond = next(c for c in r["conditions"] if c["kind"] == "equity_tape_stalled")
    assert r["frozen"] is True
    assert cond["reason"] == "iqfeed_exact_print_missing"


def test_equity_event_loop_fresh_exact_print_keeps_lane_healthy(db, monkeypatch):
    _enable_equity_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=2, captured_paper=True)
    monkeypatch.setattr(lh, "_iqfeed_data_session_age_seconds", lambda: 301.0)
    monkeypatch.setattr(
        lh,
        "_latest_exact_iqfeed_print_status",
        lambda _db: {
            "ok": True,
            "provider_event_at": datetime.utcnow() - timedelta(seconds=2),
            "received_at": datetime.utcnow() - timedelta(seconds=1),
            "available_at": datetime.utcnow() - timedelta(seconds=1),
        },
    )

    r = lh.evaluate_lane_health(db)

    assert r["frozen"] is False
    assert r["conditions"] == []


def test_equity_exact_print_alert_is_quiet_outside_tradeable_window(
    db,
    monkeypatch,
):
    _enable_equity_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=2, captured_paper=True)
    monkeypatch.setattr(lh, "_iqfeed_data_session_age_seconds", lambda: None)
    monkeypatch.setattr(
        lh,
        "_latest_exact_iqfeed_print_status",
        lambda _db: (_ for _ in ()).throw(
            AssertionError("closed session must not read a tape receipt")
        ),
    )

    r = lh.evaluate_lane_health(db)

    assert r["frozen"] is False
    assert r["conditions"] == []


def test_exact_print_alert_waits_for_current_session_grace(db, monkeypatch):
    _enable_equity_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=2, captured_paper=True)
    monkeypatch.setattr(lh, "_iqfeed_data_session_age_seconds", lambda: 299.0)
    monkeypatch.setattr(
        lh,
        "_latest_exact_iqfeed_print_status",
        lambda _db: (_ for _ in ()).throw(
            AssertionError("session grace must avoid a receipt read")
        ),
    )

    r = lh.evaluate_lane_health(db)

    assert r["frozen"] is False
    assert r["conditions"] == []


def test_exact_print_alert_is_independent_of_paper_and_notify(db, monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_equity_only", True, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_crypto_only", False, raising=False
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_loop_iqfeed_notify_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(lh, "_iqfeed_data_session_age_seconds", lambda: 301.0)
    monkeypatch.setattr(
        lh,
        "_latest_exact_iqfeed_print_status",
        lambda _db: {"ok": False, "reason": "iqfeed_exact_print_missing"},
    )

    r = lh.evaluate_lane_health(db)

    assert r["frozen"] is True
    assert any(c["kind"] == "equity_tape_stalled" for c in r["conditions"])


def test_exact_print_query_failure_becomes_typed_loud_condition(db, monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_equity_only", True, raising=False
    )
    monkeypatch.setattr(
        settings, "chili_momentum_auto_arm_crypto_only", False, raising=False
    )
    monkeypatch.setattr(lh, "_iqfeed_data_session_age_seconds", lambda: 301.0)
    monkeypatch.setattr(
        lh,
        "_latest_exact_iqfeed_print_status",
        lambda _db: (_ for _ in ()).throw(RuntimeError("db unavailable")),
    )

    r = lh.evaluate_lane_health(db)

    cond = next(c for c in r["conditions"] if c["kind"] == "equity_tape_stalled")
    assert cond["reason"] == "iqfeed_exact_print_unreadable"


def _exact_print_receipt_meta(
    *,
    at: datetime,
    bridge_run_id: str | None = None,
    bridge_run_started_at: datetime | None = None,
) -> dict:
    at_utc = at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    bridge_run_started_at = bridge_run_started_at or at - timedelta(minutes=5)
    meta = {
        "schema": IQFEED_EXACT_PRINT_HEARTBEAT_SCHEMA,
        "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
        "symbol": "AMIX",
        "observed_at_utc": at_utc,
        "provider_event_at_utc": at_utc,
        "received_at_utc": at_utc,
        "available_at_utc": at_utc,
        "bridge_version": "test-bridge-build",
        "bridge_run_id": bridge_run_id or str(uuid.uuid4()),
        "bridge_run_started_at_utc": (
            bridge_run_started_at.replace(tzinfo=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        ),
        "connection_generation": 1,
        "source_frame_sequence": 1,
        "source_frame_sha256": "a" * 64,
        "committed_print_count": 1,
    }
    meta["content_sha256"] = lh._heartbeat_content_sha256(meta)
    return meta


def _insert_exact_print_tape_row(db, *, meta: dict) -> None:
    db.execute(
        text(
            "INSERT INTO iqfeed_trade_ticks "
            "(symbol, observed_at, price, size, source, provider_event_at, "
            "received_at, timestamp_basis, bridge_version, message_type, "
            "bridge_run_id, connection_generation, source_frame_sequence, "
            "source_frame_sha256, available_at) VALUES "
            "(:symbol, :observed_at, 1.0, 100.0, 'iqfeed_l1', "
            ":provider_event_at, :received_at, "
            "'iqfeed_selected_trade_date_timems_exact', :bridge_version, 'Q', "
            ":bridge_run_id, :connection_generation, :source_frame_sequence, "
            ":source_frame_sha256, :available_at)"
        ),
        {
            "symbol": meta["symbol"],
            "observed_at": datetime.fromisoformat(
                meta["observed_at_utc"].replace("Z", "+00:00")
            ).replace(tzinfo=None),
            "provider_event_at": meta["provider_event_at_utc"],
            "received_at": meta["received_at_utc"],
            "available_at": meta["available_at_utc"],
            "bridge_version": meta["bridge_version"],
            "bridge_run_id": meta["bridge_run_id"],
            "connection_generation": meta["connection_generation"],
            "source_frame_sequence": meta["source_frame_sequence"],
            "source_frame_sha256": meta["source_frame_sha256"],
        },
    )


def test_exact_print_receipt_validator_accepts_content_bound_completed_row(db):
    at = datetime.utcnow()
    meta = _exact_print_receipt_meta(at=at)
    _insert_exact_print_tape_row(db, meta=meta)
    db.add(
        BrainBatchJob(
            id=str(uuid.uuid4()),
            job_type=JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
            status="ok",
            started_at=at,
            ended_at=at,
            meta_json=meta,
        )
    )
    db.commit()

    truth = lh._latest_exact_iqfeed_print_status(db)

    assert truth["ok"] is True
    assert truth["provider_event_at"] == at
    assert truth["scope"] == IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE


def test_exact_print_receipt_validator_accepts_parser_clock_tolerance(db):
    received_at = datetime.utcnow()
    provider_at = received_at + timedelta(milliseconds=500)
    available_at = received_at + timedelta(seconds=1)
    meta = _exact_print_receipt_meta(at=available_at)
    meta["observed_at_utc"] = (
        provider_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    )
    meta["provider_event_at_utc"] = meta["observed_at_utc"]
    meta["received_at_utc"] = (
        received_at.replace(tzinfo=timezone.utc).isoformat().replace("+00:00", "Z")
    )
    meta["content_sha256"] = lh._heartbeat_content_sha256(meta)
    _insert_exact_print_tape_row(db, meta=meta)
    db.add(
        BrainBatchJob(
            id=str(uuid.uuid4()),
            job_type=JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
            status="ok",
            started_at=available_at,
            ended_at=available_at,
            meta_json=meta,
        )
    )
    db.commit()

    truth = lh._latest_exact_iqfeed_print_status(db)

    assert truth["ok"] is True


def test_exact_print_receipt_rejects_missing_bound_tape_row(db):
    at = datetime.utcnow()
    db.add(
        BrainBatchJob(
            id=str(uuid.uuid4()),
            job_type=JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
            status="ok",
            started_at=at,
            ended_at=at,
            meta_json=_exact_print_receipt_meta(at=at),
        )
    )
    db.commit()

    truth = lh._latest_exact_iqfeed_print_status(db)

    assert truth["reason"] == "iqfeed_exact_print_tape_row_missing"


def test_exact_print_receipt_rejects_overlapping_bridge_owners(db):
    now = datetime.utcnow()
    old_at = now - timedelta(seconds=2)
    old_meta = _exact_print_receipt_meta(
        at=old_at,
        bridge_run_id=str(uuid.uuid4()),
        bridge_run_started_at=now - timedelta(minutes=10),
    )
    latest_meta = _exact_print_receipt_meta(
        at=now,
        bridge_run_id=str(uuid.uuid4()),
        bridge_run_started_at=now - timedelta(seconds=5),
    )
    _insert_exact_print_tape_row(db, meta=latest_meta)
    db.add_all(
        [
            BrainBatchJob(
                id=str(uuid.uuid4()),
                job_type=JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
                status="ok",
                started_at=old_at,
                ended_at=old_at,
                meta_json=old_meta,
            ),
            BrainBatchJob(
                id=str(uuid.uuid4()),
                job_type=JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
                status="ok",
                started_at=now,
                ended_at=now,
                meta_json=latest_meta,
            ),
        ]
    )
    db.commit()

    truth = lh._latest_exact_iqfeed_print_status(db)

    assert truth["reason"] == "iqfeed_exact_print_bridge_owner_overlap"
    assert truth["overlapping_owner_count"] == 2


def test_exact_print_receipt_accepts_clean_one_second_owner_handoff(db):
    now = datetime.utcnow()
    old_at = now - timedelta(seconds=1)
    old_meta = _exact_print_receipt_meta(
        at=old_at,
        bridge_run_id=str(uuid.uuid4()),
        bridge_run_started_at=now - timedelta(minutes=10),
    )
    latest_at = now + timedelta(seconds=1)
    latest_meta = _exact_print_receipt_meta(
        at=latest_at,
        bridge_run_id=str(uuid.uuid4()),
        bridge_run_started_at=now,
    )
    _insert_exact_print_tape_row(db, meta=latest_meta)
    db.add_all(
        [
            BrainBatchJob(
                id=str(uuid.uuid4()),
                job_type=JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
                status="ok",
                started_at=old_at,
                ended_at=old_at,
                meta_json=old_meta,
            ),
            BrainBatchJob(
                id=str(uuid.uuid4()),
                job_type=JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
                status="ok",
                started_at=latest_at,
                ended_at=latest_at,
                meta_json=latest_meta,
            ),
        ]
    )
    db.commit()

    truth = lh._latest_exact_iqfeed_print_status(db)

    assert truth["ok"] is True
    assert truth["bridge_run_id"] == latest_meta["bridge_run_id"]


def test_exact_print_latest_malformed_does_not_fall_back_to_older_success(db):
    older = datetime.utcnow() - timedelta(seconds=2)
    latest = datetime.utcnow()
    db.add_all(
        [
            BrainBatchJob(
                id=str(uuid.uuid4()),
                job_type=JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
                status="ok",
                started_at=older,
                ended_at=older,
                meta_json=_exact_print_receipt_meta(at=older),
            ),
            BrainBatchJob(
                id=str(uuid.uuid4()),
                job_type=JOB_IQFEED_EXACT_PRINT_HEARTBEAT,
                status="ok",
                started_at=latest,
                ended_at=latest,
                meta_json={"schema": IQFEED_EXACT_PRINT_HEARTBEAT_SCHEMA},
            ),
        ]
    )
    db.commit()

    truth = lh._latest_exact_iqfeed_print_status(db)

    assert truth == {
        "ok": False,
        "reason": "iqfeed_exact_print_latest_malformed",
        "scope": IQFEED_EXACT_PRINT_HEARTBEAT_SCOPE,
    }


def test_live_loop_owner_writes_completed_durable_heartbeat(db):
    owner_instance_id = str(uuid.uuid4())
    generation_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    job_id = lh.record_live_runner_loop_run(
        db,
        owner_instance_id=owner_instance_id,
        generation=7,
        generation_started_at=generation_started_at,
    )
    db.commit()

    row = db.query(BrainBatchJob).filter(BrainBatchJob.id == job_id).one()
    assert row.job_type == JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT
    assert row.status == "ok"
    assert row.ended_at is not None
    assert row.meta_json == {
        "schema": lh.LIVE_LOOP_HEARTBEAT_SCHEMA,
        "scope": lh.LIVE_LOOP_HEARTBEAT_SCOPE,
        "owner": "momentum_live_runner_loop",
        "owner_instance_id": owner_instance_id,
        "generation": 7,
        "generation_identity": f"{owner_instance_id}:7",
        "generation_started_at_utc": (
            generation_started_at.isoformat().replace("+00:00", "Z")
        ),
    }


def test_live_loop_owner_writes_hash_bound_captured_paper_heartbeat(db):
    owner_instance_id = str(uuid.uuid4())
    generation_started_at = datetime.now(timezone.utc) - timedelta(seconds=5)
    job_id = lh.record_live_runner_loop_run(
        db,
        owner_instance_id=owner_instance_id,
        generation=7,
        generation_started_at=generation_started_at,
        account_scope="alpaca:paper",
        expected_account_id=_PAPER_ACCOUNT_ID,
        runtime_generation=_PAPER_RUNTIME_GENERATION,
        execution_family="alpaca_spot",
        live_cash_authorized=False,
    )
    db.commit()

    row = db.query(BrainBatchJob).filter(BrainBatchJob.id == job_id).one()
    assert row.meta_json["schema"] == lh.LIVE_LOOP_HEARTBEAT_SCHEMA_V2
    assert row.meta_json["account_scope"] == "alpaca:paper"
    assert row.meta_json["expected_account_id"] == _PAPER_ACCOUNT_ID
    assert row.meta_json["runtime_generation"] == _PAPER_RUNTIME_GENERATION
    assert row.meta_json["execution_family"] == "alpaca_spot"
    assert row.meta_json["live_cash_authorized"] is False
    assert row.meta_json["content_sha256"] == lh._heartbeat_content_sha256(
        row.meta_json
    )
    truth = lh.captured_paper_live_runner_control_health(
        db,
        expected_account_id=_PAPER_ACCOUNT_ID,
    )
    assert truth["ok"] is True
    assert truth["captured_paper"] is True


def test_captured_paper_heartbeat_identity_must_be_atomic_and_exact(db):
    base = {
        "owner_instance_id": str(uuid.uuid4()),
        "generation": 1,
        "generation_started_at": datetime.now(timezone.utc),
    }
    with pytest.raises(ValueError, match="supplied atomically"):
        lh.record_live_runner_loop_run(
            db,
            **base,
            account_scope="alpaca:paper",
        )
    with pytest.raises(ValueError, match="not exact"):
        lh.record_live_runner_loop_run(
            db,
            **base,
            account_scope="alpaca:paper",
            expected_account_id=_PAPER_ACCOUNT_ID,
            runtime_generation=_PAPER_RUNTIME_GENERATION,
            execution_family="alpaca_spot",
            live_cash_authorized=True,
        )


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("generic", "captured_paper_heartbeat_generic"),
        ("stale", "live_runner_loop_heartbeat_stale"),
        ("tampered", "live_runner_loop_heartbeat_latest_malformed"),
        ("overlap", "live_runner_loop_owner_overlap"),
    ),
)
def test_non_exact_heartbeat_never_proves_captured_paper_active(
    db,
    case,
    expected_reason,
):
    if case == "generic":
        _live_loop_heartbeat(db, age_seconds=2)
    elif case == "stale":
        _live_loop_heartbeat(db, age_seconds=600, captured_paper=True)
    elif case == "tampered":
        _live_loop_heartbeat(
            db,
            age_seconds=2,
            captured_paper=True,
            tamper_content=True,
        )
    else:
        _live_loop_heartbeat(
            db,
            age_seconds=10,
            captured_paper=True,
            owner_instance_id=str(uuid.uuid4()),
            generation_started_age_seconds=120,
        )
        _live_loop_heartbeat(
            db,
            age_seconds=2,
            captured_paper=True,
            owner_instance_id=str(uuid.uuid4()),
            generation_started_age_seconds=30,
        )

    truth = lh.captured_paper_live_runner_control_health(
        db,
        expected_account_id=_PAPER_ACCOUNT_ID,
    )
    assert truth["ok"] is False
    assert truth["reason"] == expected_reason


def test_live_loop_heartbeat_rejects_missing_completed_row(db, monkeypatch):
    from app.services.trading import brain_batch_job_log

    missing_job_id = str(uuid.uuid4())
    monkeypatch.setattr(
        brain_batch_job_log,
        "brain_batch_job_record_completed",
        lambda *_args, **_kwargs: missing_job_id,
    )

    with pytest.raises(
        RuntimeError,
        match="did not persist the exact completed row",
    ):
        lh.record_live_runner_loop_run(
            db,
            owner_instance_id=str(uuid.uuid4()),
            generation=1,
            generation_started_at=datetime.now(timezone.utc),
        )

    assert (
        db.query(BrainBatchJob)
        .filter(BrainBatchJob.id == missing_job_id)
        .one_or_none()
        is None
    )


def test_missing_durable_heartbeat_is_loud_even_if_local_state_looks_fresh(
    db,
    monkeypatch,
):
    _enable_event_lane(monkeypatch)
    monkeypatch.setattr(
        lh,
        "_live_loop_heartbeat_age_seconds",
        lambda: 0.0,
        raising=False,
    )

    r = lh.evaluate_lane_health(db)

    assert r["frozen"] is True
    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_heartbeat_missing"


def test_event_loop_stale_durable_heartbeat_is_loud_without_auto_arm_scheduler(
    db,
    monkeypatch,
):

    _enable_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=600)
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is True
    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_heartbeat_stale"


def test_unreadable_durable_heartbeat_fails_closed_even_if_local_state_is_fresh(
    db,
    monkeypatch,
):
    _enable_event_lane(monkeypatch)
    monkeypatch.setattr(
        lh,
        "_live_loop_heartbeat_age_seconds",
        lambda: 0.0,
        raising=False,
    )
    monkeypatch.setattr(
        lh,
        "_latest_live_loop_heartbeat_status",
        lambda _db, *, stale_seconds: (_ for _ in ()).throw(
            RuntimeError("db unreadable")
        ),
    )

    r = lh.evaluate_lane_health(db)

    assert r["frozen"] is True
    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_heartbeat_unreadable"


def test_unfinished_live_loop_row_cannot_spoof_completed_heartbeat(db, monkeypatch):
    _enable_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=2, status="running", completed=False)

    r = lh.evaluate_lane_health(db)

    assert r["frozen"] is True
    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_heartbeat_latest_unfinished"


def test_event_loop_exit_owner_is_monitored_when_entry_admission_is_paused(
    db,
    monkeypatch,
):
    _enable_event_lane(monkeypatch)
    monkeypatch.setattr(
        settings,
        "chili_momentum_ross_event_admission_enabled",
        False,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_loop_iqfeed_notify_enabled",
        False,
        raising=False,
    )
    _live_loop_heartbeat(db, age_seconds=600)

    r = lh.evaluate_lane_health(db)

    assert r["frozen"] is True
    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_heartbeat_stale"


@pytest.mark.parametrize("breaker", ("kill_switch", "broker_block"))
def test_event_loop_stall_remains_loud_while_safety_breaker_is_active(
    db,
    monkeypatch,
    breaker,
):
    _enable_event_lane(monkeypatch)
    if breaker == "kill_switch":
        gov.activate_kill_switch("manual_loss_halt")
    else:
        gov.set_broker_daily_loss_block(
            "alpaca_spot",
            reason="daily_loss_halt",
            realized=-251.0,
            limit=250.0,
        )
    _live_loop_heartbeat(db, age_seconds=90)

    r = lh.evaluate_lane_health(db)

    kinds = {c["kind"] for c in r["conditions"]}
    expected_breaker_kind = (
        "kill_switch" if breaker == "kill_switch" else "broker_block"
    )
    assert expected_breaker_kind in kinds
    assert "live_loop_stalled" in kinds
    assert next(
        c for c in r["conditions"] if c["kind"] == "live_loop_stalled"
    )["reason"] == "live_runner_loop_heartbeat_stale"


def test_event_loop_uses_tight_timeout_and_is_monitored_outside_arm_window(
    db,
    monkeypatch,
):
    _enable_event_lane(monkeypatch)
    monkeypatch.setattr(
        settings,
        "chili_lane_health_freeze_alert_seconds",
        900.0,
        raising=False,
    )
    monkeypatch.setattr(lh, "_expected_trading_window_open", lambda: False)
    _live_loop_heartbeat(db, age_seconds=80)

    r = lh.evaluate_lane_health(db)

    assert r["live_loop_stale_seconds"] == 75.0
    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_heartbeat_stale"


def test_latest_malformed_live_loop_row_fails_closed(db, monkeypatch):
    _enable_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=2, malformed_meta=True)

    r = lh.evaluate_lane_health(db)

    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_heartbeat_latest_malformed"


def test_latest_error_row_cannot_fall_back_to_prior_ok_heartbeat(db, monkeypatch):
    _enable_event_lane(monkeypatch)
    owner = str(uuid.uuid4())
    _live_loop_heartbeat(
        db,
        age_seconds=3,
        owner_instance_id=owner,
        generation=1,
    )
    _live_loop_heartbeat(
        db,
        age_seconds=1,
        owner_instance_id=owner,
        generation=1,
        status="error",
    )

    r = lh.evaluate_lane_health(db)

    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_heartbeat_latest_error"


def test_overlapping_distinct_live_loop_owners_fail_closed(db, monkeypatch):
    _enable_event_lane(monkeypatch)
    _live_loop_heartbeat(
        db,
        age_seconds=10,
        owner_instance_id=str(uuid.uuid4()),
        generation=1,
        generation_started_age_seconds=120,
    )
    _live_loop_heartbeat(
        db,
        age_seconds=2,
        owner_instance_id=str(uuid.uuid4()),
        generation=1,
        generation_started_age_seconds=30,
    )

    r = lh.evaluate_lane_health(db)

    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_owner_overlap"
    assert cond["overlapping_owner_count"] == 2


def test_clean_live_loop_owner_handoff_does_not_false_positive(db, monkeypatch):
    _enable_event_lane(monkeypatch)
    _live_loop_heartbeat(
        db,
        age_seconds=40,
        owner_instance_id=str(uuid.uuid4()),
        generation=1,
        generation_started_age_seconds=120,
    )
    _live_loop_heartbeat(
        db,
        age_seconds=2,
        owner_instance_id=str(uuid.uuid4()),
        generation=1,
        generation_started_age_seconds=30,
    )

    r = lh.evaluate_lane_health(db)

    assert r["frozen"] is False
    assert r["conditions"] == []


def test_future_live_loop_heartbeat_fails_closed(db, monkeypatch):
    _enable_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=-10)

    r = lh.evaluate_lane_health(db)

    cond = next(c for c in r["conditions"] if c["kind"] == "live_loop_stalled")
    assert cond["reason"] == "live_runner_loop_heartbeat_future"


@pytest.mark.parametrize(
    ("batch_on", "loop_on", "bus_on", "reason"),
    (
        (True, True, True, "live_runner_batch_and_event_loop_both_enabled"),
        (False, False, True, "live_runner_no_driver_enabled"),
        (False, True, False, "live_runner_event_loop_price_bus_disabled"),
    ),
)
def test_master_enabled_invalid_driver_configuration_is_loud(
    db,
    monkeypatch,
    batch_on,
    loop_on,
    bus_on,
    reason,
):
    _enable_event_lane(monkeypatch)
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_scheduler_enabled",
        batch_on,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_momentum_live_runner_loop_enabled",
        loop_on,
        raising=False,
    )
    monkeypatch.setattr(
        settings,
        "chili_autopilot_price_bus_enabled",
        bus_on,
        raising=False,
    )

    r = lh.evaluate_lane_health(db)

    cond = next(c for c in r["conditions"] if c["kind"] == "driver_misconfigured")
    assert cond["reason"] == reason


def test_lane_disabled_no_starvation_alert(db, monkeypatch):
    """When the lane is intentionally OFF, an empty lane is not a freeze."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", False, raising=False)
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is False


# ── run_lane_health_check: loud side effects, change-only + audit row ──────

def test_run_emits_critical_and_writes_audit_row(db, caplog):
    import logging

    gov.activate_kill_switch("global_daily_loss_breach_coinbase_spot_$60")
    _age_kill_switch(8 * 3600)
    with caplog.at_level(logging.CRITICAL):
        r = lh.run_lane_health_check(db)
    assert r["frozen"] is True
    assert r["emitted"] is True
    assert any("[lane_health] FROZEN" in rec.message for rec in caplog.records)
    rows = db.query(AlertHistory).filter(AlertHistory.alert_type == "lane_health_frozen").all()
    assert len(rows) == 1
    assert "FROZEN" in rows[0].message


def test_run_change_only_no_spam(db):
    gov.activate_kill_switch("manual")
    _age_kill_switch(8 * 3600)
    first = lh.run_lane_health_check(db)
    second = lh.run_lane_health_check(db)   # same state, within cooldown
    assert first["emitted"] is True
    assert second["emitted"] is False
    rows = db.query(AlertHistory).filter(AlertHistory.alert_type == "lane_health_frozen").all()
    assert len(rows) == 1  # exactly one row, not one per tick


def test_run_recovery_resets(db, caplog):
    import logging

    gov.activate_kill_switch("manual")
    _age_kill_switch(8 * 3600)
    lh.run_lane_health_check(db)
    assert lh._last_alert_signature is not None
    gov.deactivate_kill_switch()
    with caplog.at_level(logging.WARNING):
        r = lh.run_lane_health_check(db)
    assert r["frozen"] is False
    assert lh._last_alert_signature is None
    assert any("RECOVERED" in rec.message for rec in caplog.records)


def test_exact_print_fresh_receipt_ahead_of_stale_sweep_clock_stays_healthy(
    db,
    monkeypatch,
):
    """2026-08-18 false-freeze: evaluate_lane_health snapshots `now` at the top
    of a >10s multi-probe sweep, so on a HEALTHY tape (commits every ~1s) the
    just-fetched receipt is stamped AFTER the snapshot and the future guard
    froze entries all morning. The age math must anchor to a clock read taken
    after the fetch."""
    _enable_equity_event_lane(monkeypatch)
    monkeypatch.setattr(lh, "_iqfeed_data_session_age_seconds", lambda: 601.0)
    monkeypatch.setattr(
        lh,
        "_latest_exact_iqfeed_print_status",
        lambda _db: {
            "ok": True,
            "provider_event_at": datetime.utcnow() - timedelta(seconds=1),
            "received_at": datetime.utcnow() - timedelta(seconds=1),
            "available_at": datetime.utcnow() - timedelta(seconds=1),
        },
    )

    stale_sweep_now = datetime.utcnow() - timedelta(seconds=10)
    cond = lh._equity_exact_print_tape_condition(db, now=stale_sweep_now)

    assert cond is None


def test_exact_print_genuinely_future_receipt_still_trips_guard(
    db,
    monkeypatch,
):
    """A receipt stamped ahead of BOTH clocks (host clock step backwards /
    forged availability) must still freeze loudly — the anchor is a floor,
    not a bypass of the future guard."""
    _enable_equity_event_lane(monkeypatch)
    monkeypatch.setattr(lh, "_iqfeed_data_session_age_seconds", lambda: 601.0)
    monkeypatch.setattr(
        lh,
        "_latest_exact_iqfeed_print_status",
        lambda _db: {
            "ok": True,
            "provider_event_at": datetime.utcnow() - timedelta(seconds=1),
            "received_at": datetime.utcnow() - timedelta(seconds=1),
            "available_at": datetime.utcnow() + timedelta(seconds=10),
        },
    )

    cond = lh._equity_exact_print_tape_condition(db, now=datetime.utcnow())

    assert cond is not None
    assert cond["reason"] == "iqfeed_exact_print_available_future"
    assert cond["frozen"] is True
