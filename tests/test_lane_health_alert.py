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
from datetime import datetime, timedelta

import pytest

from app.config import settings
from app.models.trading import AlertHistory, BrainBatchJob
from app.services.trading import governance as gov
from app.services.trading.batch_job_constants import JOB_SCHEDULER_WORKER_HEARTBEAT
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
