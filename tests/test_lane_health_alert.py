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

from app.config import settings
from app.models.trading import AlertHistory, BrainBatchJob
from app.services.trading import governance as gov
from app.services.trading.batch_job_constants import (
    JOB_MOMENTUM_LIVE_LOOP_HEARTBEAT,
    JOB_SCHEDULER_WORKER_HEARTBEAT,
)
from app.services.trading.momentum_neural import lane_health as lh


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


def test_event_loop_heartbeat_keeps_quiet_lane_healthy(db, monkeypatch):
    _enable_event_lane(monkeypatch)
    _live_loop_heartbeat(db, age_seconds=2)
    r = lh.evaluate_lane_health(db)
    assert r["frozen"] is False
    assert r["conditions"] == []


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
