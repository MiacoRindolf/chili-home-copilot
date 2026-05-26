from __future__ import annotations

from app.config import settings
from app.models.trading import ScanPattern
from app.services.trading.backtest_queue import get_pending_patterns, get_queue_status


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
