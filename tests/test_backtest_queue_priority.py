from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.models.trading import ScanPattern
from app.services.trading.backtest_queue import (
    QUEUE_TIER_FULL,
    QUEUE_TIER_PRESCREEN,
    get_pending_patterns,
    get_priority_bypass_retest_floor,
    get_queue_status,
    mark_pattern_tested,
)


def _deactivate_existing_patterns(db) -> None:
    db.query(ScanPattern).update(
        {
            ScanPattern.active: False,
            ScanPattern.backtest_priority: 0,
        },
        synchronize_session=False,
    )
    db.commit()


def _queued_pattern(
    db,
    *,
    name: str,
    lifecycle_stage: str,
    backtest_priority: int,
    promotion_gate_reasons: list[str] | None = None,
    promotion_gate_passed: bool | None = None,
    recert_required: bool = False,
    last_backtest_at: datetime | None = None,
) -> ScanPattern:
    pat = ScanPattern(
        name=name,
        rules_json={
            "conditions": [
                {"indicator": "rsi_14", "op": "<", "value": 40},
            ],
        },
        lifecycle_stage=lifecycle_stage,
        promotion_status="test",
        active=True,
        backtest_priority=backtest_priority,
        promotion_gate_reasons=promotion_gate_reasons,
        promotion_gate_passed=promotion_gate_passed,
        cpcv_n_paths=20 if promotion_gate_passed is not None else None,
        recert_required=recert_required,
        recert_reason="missing_oos_recert" if recert_required else None,
        last_backtest_at=last_backtest_at,
    )
    db.add(pat)
    db.flush()
    return pat


def test_pending_patterns_prioritizes_promotion_path_debt_before_generic_backlog(db):
    _deactivate_existing_patterns(db)
    recert = _queued_pattern(
        db,
        name="recert first",
        lifecycle_stage="promoted",
        backtest_priority=10,
        recert_required=True,
    )
    path_debt = _queued_pattern(
        db,
        name="shadow needs more cpcv paths",
        lifecycle_stage="shadow_promoted",
        backtest_priority=1,
        promotion_gate_reasons=["cpcv_n_paths_below_provisional_min"],
        promotion_gate_passed=False,
    )
    generic = _queued_pattern(
        db,
        name="generic high priority challenged",
        lifecycle_stage="challenged",
        backtest_priority=999,
        promotion_gate_reasons=["adaptive_dsr_below_pool_threshold"],
        promotion_gate_passed=False,
    )
    db.commit()

    pending = get_pending_patterns(db, limit=3)

    assert [p.id for p in pending] == [recert.id, path_debt.id, generic.id]
    assert get_queue_status(db, use_cache=False)["promotion_path_debt_pending"] == 1


def test_promotion_path_debt_priority_can_be_disabled(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(settings, "chili_backtest_prioritize_promotion_path_debt", False)
    path_debt = _queued_pattern(
        db,
        name="shadow needs more cpcv paths",
        lifecycle_stage="shadow_promoted",
        backtest_priority=1,
        promotion_gate_reasons=["cpcv_n_paths_below_provisional_min"],
        promotion_gate_passed=False,
    )
    generic = _queued_pattern(
        db,
        name="generic high priority challenged",
        lifecycle_stage="challenged",
        backtest_priority=999,
        promotion_gate_reasons=["adaptive_dsr_below_pool_threshold"],
        promotion_gate_passed=False,
    )
    db.commit()

    pending = get_pending_patterns(db, limit=2)

    assert [p.id for p in pending] == [generic.id, path_debt.id]


def test_scored_priority_does_not_requeue_fresh_pattern_below_bypass_floor(db):
    _deactivate_existing_patterns(db)
    bypass_floor = get_priority_bypass_retest_floor()
    fresh = datetime.now(timezone.utc).replace(tzinfo=None)
    _queued_pattern(
        db,
        name="fresh scored candidate",
        lifecycle_stage="candidate",
        backtest_priority=bypass_floor - 1,
        last_backtest_at=fresh,
    )
    manual = _queued_pattern(
        db,
        name="fresh explicit boost",
        lifecycle_stage="candidate",
        backtest_priority=bypass_floor,
        last_backtest_at=fresh,
    )
    db.commit()

    pending = get_pending_patterns(db, limit=5)
    status = get_queue_status(db, use_cache=False)

    assert [p.id for p in pending] == [manual.id]
    assert status["pending"] == 1
    assert status["boosted"] == 2
    assert status["priority_bypass"] == 1
    assert status["priority_bypass_floor"] == bypass_floor


def test_fresh_promoted_recert_stays_pending_without_priority_bypass(db):
    _deactivate_existing_patterns(db)
    bypass_floor = get_priority_bypass_retest_floor()
    fresh = datetime.now(timezone.utc).replace(tzinfo=None)
    recert = _queued_pattern(
        db,
        name="fresh recert",
        lifecycle_stage="promoted",
        backtest_priority=bypass_floor - 1,
        recert_required=True,
        last_backtest_at=fresh,
    )
    db.commit()

    pending = get_pending_patterns(db, limit=5)
    status = get_queue_status(db, use_cache=False)

    assert [p.id for p in pending] == [recert.id]
    assert status["pending"] == 1
    assert status["recert_pending"] == 1
    assert status["priority_bypass"] == 0


def test_zero_trade_demote_uses_trade_bearing_tickers_not_jobs_run(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(settings, "chili_backtest_zero_trade_demote_threshold", 3)
    pattern = _queued_pattern(
        db,
        name="candidate burns ticker jobs without trades",
        lifecycle_stage="candidate",
        backtest_priority=10,
    )
    pattern.queue_tier = QUEUE_TIER_FULL
    pattern.consecutive_zero_trade_runs = 2
    db.commit()

    mark_pattern_tested(
        db,
        pattern,
        backtests_run=24,
        trade_bearing_tickers=0,
    )
    db.refresh(pattern)

    assert pattern.consecutive_zero_trade_runs == 3
    assert pattern.queue_tier == QUEUE_TIER_PRESCREEN


def test_zero_trade_counter_resets_on_trade_bearing_evidence(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(settings, "chili_backtest_zero_trade_demote_threshold", 3)
    pattern = _queued_pattern(
        db,
        name="candidate starts producing trades",
        lifecycle_stage="candidate",
        backtest_priority=10,
    )
    pattern.queue_tier = QUEUE_TIER_FULL
    pattern.consecutive_zero_trade_runs = 2
    db.commit()

    mark_pattern_tested(
        db,
        pattern,
        backtests_run=24,
        trade_bearing_tickers=1,
    )
    db.refresh(pattern)

    assert pattern.consecutive_zero_trade_runs == 0
    assert pattern.queue_tier == QUEUE_TIER_FULL


def test_zero_trade_demote_protects_promoted_recert_lane(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(settings, "chili_backtest_zero_trade_demote_threshold", 3)
    pattern = _queued_pattern(
        db,
        name="promoted recert can be sparse",
        lifecycle_stage="promoted",
        backtest_priority=10,
        recert_required=True,
    )
    pattern.queue_tier = QUEUE_TIER_FULL
    pattern.consecutive_zero_trade_runs = 2
    db.commit()

    mark_pattern_tested(
        db,
        pattern,
        backtests_run=24,
        trade_bearing_tickers=0,
    )
    db.refresh(pattern)

    assert pattern.consecutive_zero_trade_runs == 3
    assert pattern.queue_tier == QUEUE_TIER_FULL


def test_zero_trade_counter_keeps_legacy_backtests_run_fallback(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(settings, "chili_backtest_zero_trade_demote_threshold", 3)
    pattern = _queued_pattern(
        db,
        name="legacy zero job caller",
        lifecycle_stage="candidate",
        backtest_priority=10,
    )
    pattern.queue_tier = QUEUE_TIER_FULL
    pattern.consecutive_zero_trade_runs = 2
    db.commit()

    mark_pattern_tested(db, pattern, backtests_run=0)
    db.refresh(pattern)

    assert pattern.consecutive_zero_trade_runs == 3
    assert pattern.queue_tier == QUEUE_TIER_PRESCREEN
