from __future__ import annotations

from collections import Counter
from datetime import datetime, timedelta, timezone

from app.config import settings
from app.models.trading import ScanPattern
from app.services.trading.backtest_queue import (
    EDGE_EVIDENCE_VARIANT_ORIGIN,
    EDGE_EVIDENCE_VARIANT_PROMOTION_STATUS,
    QUEUE_TIER_FULL,
    QUEUE_TIER_PRESCREEN,
    get_exploration_pattern_ids,
    get_pending_patterns,
    get_priority_bypass_retest_floor,
    get_queue_status,
    get_queue_lineage_cap_policy,
    get_queue_lineage_status_limit_basis,
    mark_pattern_tested,
    summarize_queue_batch,
)
from app.services.trading.backtest_queue_priority import run_priority_scoring

_FIXED_LINEAGE_CAP_DISABLED = 0
_ADAPTIVE_HALF_BATCH_LINEAGE_SHARE = 0.50
_ADAPTIVE_TWO_OF_FIVE_LINEAGE_SHARE = 0.40
_ADAPTIVE_LINEAGE_FLOOR = 0
_ENV_LINEAGE_SHARE = "0.25"
_ENV_LINEAGE_FLOOR = "2"
_FAST_BACKTEST_BATCH_BASIS = 30
_QUEUE_BATCH_BASIS = 80


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
    promotion_status: str = "test",
    origin: str = "test",
    parent_id: int | None = None,
) -> ScanPattern:
    pat = ScanPattern(
        name=name,
        rules_json={
            "conditions": [
                {"indicator": "rsi_14", "op": "<", "value": 40},
            ],
        },
        lifecycle_stage=lifecycle_stage,
        promotion_status=promotion_status,
        origin=origin,
        parent_id=parent_id,
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


def test_edge_evidence_variant_priority_before_generic_backlog(db):
    _deactivate_existing_patterns(db)
    parent = _queued_pattern(
        db,
        name="edge parent",
        lifecycle_stage="promoted",
        backtest_priority=0,
    )
    parent.active = False
    edge_child = _queued_pattern(
        db,
        name="edge evidence child",
        lifecycle_stage="challenged",
        backtest_priority=50,
        promotion_status=EDGE_EVIDENCE_VARIANT_PROMOTION_STATUS,
        origin=EDGE_EVIDENCE_VARIANT_ORIGIN,
        parent_id=parent.id,
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

    assert [p.id for p in pending] == [edge_child.id, generic.id]


def test_lane_planner_prioritizes_recert_before_prescreen(db):
    _deactivate_existing_patterns(db)
    prescreen = _queued_pattern(
        db,
        name="cheap prescreen should wait behind recert",
        lifecycle_stage="candidate",
        backtest_priority=999,
    )
    prescreen.queue_tier = QUEUE_TIER_PRESCREEN
    recert = _queued_pattern(
        db,
        name="recert safety debt",
        lifecycle_stage="promoted",
        backtest_priority=0,
        recert_required=True,
    )
    db.commit()

    pending = get_pending_patterns(db, limit=2)

    assert [p.id for p in pending] == [recert.id, prescreen.id]


def test_lane_planner_diversifies_generic_variant_families(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(
        settings,
        "brain_queue_max_per_lineage_per_batch",
        _FIXED_LINEAGE_CAP_DISABLED,
    )
    monkeypatch.setattr(
        settings,
        "brain_queue_lineage_max_batch_share",
        _ADAPTIVE_HALF_BATCH_LINEAGE_SHARE,
    )
    monkeypatch.setattr(
        settings,
        "brain_queue_lineage_min_per_batch",
        _ADAPTIVE_LINEAGE_FLOOR,
    )
    parent = _queued_pattern(
        db,
        name="crowded parent",
        lifecycle_stage="candidate",
        backtest_priority=0,
    )
    parent.active = False
    crowded_children = [
        _queued_pattern(
            db,
            name=f"crowded child {idx}",
            lifecycle_stage="challenged",
            backtest_priority=999 - idx,
            origin="exit_variant",
            parent_id=parent.id,
        )
        for idx in range(5)
    ]
    diverse_a = _queued_pattern(
        db,
        name="diverse generic a",
        lifecycle_stage="challenged",
        backtest_priority=10,
    )
    diverse_b = _queued_pattern(
        db,
        name="diverse generic b",
        lifecycle_stage="challenged",
        backtest_priority=9,
    )
    db.commit()

    pending = get_pending_patterns(db, limit=4)
    family_counts = Counter(int(p.parent_id or p.id) for p in pending)

    assert family_counts[int(parent.id)] == 2
    assert {diverse_a.id, diverse_b.id}.issubset({p.id for p in pending})
    assert len({p.id for p in pending}.intersection({p.id for p in crowded_children})) == 2


def test_lane_planner_final_refill_preserves_lineage_cap(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(
        settings,
        "brain_queue_max_per_lineage_per_batch",
        _FIXED_LINEAGE_CAP_DISABLED,
    )
    monkeypatch.setattr(
        settings,
        "brain_queue_lineage_max_batch_share",
        _ADAPTIVE_TWO_OF_FIVE_LINEAGE_SHARE,
    )
    monkeypatch.setattr(
        settings,
        "brain_queue_lineage_min_per_batch",
        _ADAPTIVE_LINEAGE_FLOOR,
    )
    monkeypatch.setattr(settings, "brain_queue_lane_fetch_multiplier", 5)
    parent = _queued_pattern(
        db,
        name="refill crowded parent",
        lifecycle_stage="candidate",
        backtest_priority=0,
    )
    parent.active = False
    crowded_children = [
        _queued_pattern(
            db,
            name=f"refill crowded child {idx}",
            lifecycle_stage="challenged",
            backtest_priority=999 - idx,
            origin="exit_variant",
            parent_id=parent.id,
        )
        for idx in range(5)
    ]
    db.commit()

    pending = get_pending_patterns(db, limit=5)
    family_counts = Counter(int(p.parent_id or p.id) for p in pending)

    assert family_counts[int(parent.id)] == 2
    assert len({p.id for p in pending}.intersection({p.id for p in crowded_children})) == 2


def test_queue_batch_summary_counts_lanes_tiers_and_lineage(db):
    _deactivate_existing_patterns(db)
    parent = _queued_pattern(
        db,
        name="edge parent",
        lifecycle_stage="promoted",
        backtest_priority=0,
    )
    parent.active = False
    recert = _queued_pattern(
        db,
        name="recert",
        lifecycle_stage="promoted",
        backtest_priority=0,
        recert_required=True,
    )
    edge_child = _queued_pattern(
        db,
        name="edge child",
        lifecycle_stage="challenged",
        backtest_priority=0,
        promotion_status=EDGE_EVIDENCE_VARIANT_PROMOTION_STATUS,
        origin=EDGE_EVIDENCE_VARIANT_ORIGIN,
        parent_id=parent.id,
    )
    prescreen = _queued_pattern(
        db,
        name="prescreen",
        lifecycle_stage="candidate",
        backtest_priority=0,
    )
    prescreen.queue_tier = QUEUE_TIER_PRESCREEN
    db.commit()

    summary = summarize_queue_batch([recert, edge_child, prescreen])

    assert summary["lanes"] == {
        "edge_evidence": 1,
        "prescreen": 1,
        "recert": 1,
    }
    assert summary["tiers"] == {"full": 2, "prescreen": 1}
    assert summary["max_lineage_count"] == 1


def test_lineage_cap_policy_adapts_to_batch_size(monkeypatch):
    monkeypatch.setattr(
        settings,
        "brain_queue_max_per_lineage_per_batch",
        _FIXED_LINEAGE_CAP_DISABLED,
    )
    monkeypatch.setattr(
        settings,
        "brain_queue_lineage_max_batch_share",
        _ADAPTIVE_TWO_OF_FIVE_LINEAGE_SHARE,
    )
    monkeypatch.setattr(
        settings,
        "brain_queue_lineage_min_per_batch",
        _ADAPTIVE_LINEAGE_FLOOR,
    )

    policy = get_queue_lineage_cap_policy(limit=5)

    assert policy["mode"] == "adaptive_share"
    assert policy["cap"] == 2
    assert policy["fixed_override"] == _FIXED_LINEAGE_CAP_DISABLED


def test_lineage_cap_settings_accept_env(monkeypatch):
    monkeypatch.setenv("BRAIN_QUEUE_LINEAGE_MAX_BATCH_SHARE", _ENV_LINEAGE_SHARE)
    monkeypatch.setenv("BRAIN_QUEUE_LINEAGE_MIN_PER_BATCH", _ENV_LINEAGE_FLOOR)

    from app.config import Settings

    cfg = Settings()

    assert cfg.brain_queue_lineage_max_batch_share == float(_ENV_LINEAGE_SHARE)
    assert cfg.brain_queue_lineage_min_per_batch == int(_ENV_LINEAGE_FLOOR)


def test_lineage_status_basis_uses_fast_backtest_batch_when_enabled(monkeypatch):
    monkeypatch.setattr(settings, "brain_fast_backtest_independent_loop", True)
    monkeypatch.setattr(
        settings,
        "brain_fast_backtest_batch_backtest",
        _FAST_BACKTEST_BATCH_BASIS,
    )
    monkeypatch.setattr(settings, "brain_queue_batch_size", _QUEUE_BATCH_BASIS)

    limit, source = get_queue_lineage_status_limit_basis()

    assert limit == _FAST_BACKTEST_BATCH_BASIS
    assert source == "fast_backtest_batch_backtest"


def test_exploration_refill_respects_existing_lineage_cap(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(
        settings,
        "brain_queue_max_per_lineage_per_batch",
        _FIXED_LINEAGE_CAP_DISABLED,
    )
    monkeypatch.setattr(
        settings,
        "brain_queue_lineage_max_batch_share",
        _ADAPTIVE_HALF_BATCH_LINEAGE_SHARE,
    )
    monkeypatch.setattr(
        settings,
        "brain_queue_lineage_min_per_batch",
        _ADAPTIVE_LINEAGE_FLOOR,
    )
    monkeypatch.setattr(settings, "brain_queue_lane_fetch_multiplier", 5)
    parent = _queued_pattern(
        db,
        name="crowded exploration parent",
        lifecycle_stage="candidate",
        backtest_priority=0,
    )
    parent.active = False
    already_selected = _queued_pattern(
        db,
        name="already selected child",
        lifecycle_stage="challenged",
        backtest_priority=0,
        parent_id=parent.id,
    )
    crowded_children = [
        _queued_pattern(
            db,
            name=f"exploration crowded child {idx}",
            lifecycle_stage="challenged",
            backtest_priority=0,
            parent_id=parent.id,
        )
        for idx in range(4)
    ]
    other = _queued_pattern(
        db,
        name="different lineage",
        lifecycle_stage="challenged",
        backtest_priority=0,
    )
    db.commit()

    refill = get_exploration_pattern_ids(db, {int(already_selected.id)}, 2)

    crowded_ids = {int(p.id) for p in crowded_children}
    assert len(crowded_ids.intersection(refill)) == 1
    assert int(other.id) in refill


def test_sparse_promotion_path_debt_cooldown_defers_recent_zero_trade_shadow(
    db,
    monkeypatch,
):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(
        settings,
        "brain_queue_sparse_promotion_debt_cooldown_enabled",
        True,
    )
    monkeypatch.setattr(settings, "brain_queue_sparse_promotion_debt_zero_runs", 5)
    monkeypatch.setattr(
        settings,
        "brain_queue_sparse_promotion_debt_cooldown_minutes",
        360,
    )
    fresh = datetime.now(timezone.utc).replace(tzinfo=None)
    cooled = _queued_pattern(
        db,
        name="sparse shadow path debt",
        lifecycle_stage="shadow_promoted",
        backtest_priority=0,
        promotion_gate_reasons=["cpcv_n_paths_below_provisional_min"],
        promotion_gate_passed=False,
        last_backtest_at=fresh,
    )
    cooled.consecutive_zero_trade_runs = 5
    generic = _queued_pattern(
        db,
        name="generic candidate",
        lifecycle_stage="challenged",
        backtest_priority=60,
    )
    db.commit()

    pending = get_pending_patterns(db, limit=5)
    status = get_queue_status(db, use_cache=False)

    assert [p.id for p in pending] == [generic.id]
    assert status["promotion_path_debt_pending"] == 0
    assert status["promotion_path_debt_cooled"] == 1

    cooled.backtest_priority = get_priority_bypass_retest_floor()
    db.commit()

    bypass_pending = get_pending_patterns(db, limit=5)

    assert cooled.id in [p.id for p in bypass_pending]


def test_sparse_promotion_path_debt_cooldown_allows_stale_retry(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(settings, "brain_queue_sparse_promotion_debt_zero_runs", 5)
    monkeypatch.setattr(
        settings,
        "brain_queue_sparse_promotion_debt_cooldown_minutes",
        360,
    )
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=7)
    path_debt = _queued_pattern(
        db,
        name="stale sparse shadow path debt",
        lifecycle_stage="shadow_promoted",
        backtest_priority=0,
        promotion_gate_reasons=["cpcv_n_paths_below_provisional_min"],
        promotion_gate_passed=False,
        last_backtest_at=stale,
    )
    path_debt.consecutive_zero_trade_runs = 9
    db.commit()

    pending = get_pending_patterns(db, limit=5)

    assert [p.id for p in pending] == [path_debt.id]


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


def test_fresh_promoted_recert_cools_down_without_priority_bypass(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(settings, "brain_queue_recert_cooldown_enabled", True)
    monkeypatch.setattr(settings, "brain_queue_recert_cooldown_minutes", 360)
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

    assert [p.id for p in pending] == []
    assert status["pending"] == 0
    assert status["recert_pending"] == 0
    assert status["recert_cooled"] == 1
    assert status["priority_bypass"] == 0

    recert.backtest_priority = bypass_floor
    db.commit()

    bypass_pending = get_pending_patterns(db, limit=5)

    assert [p.id for p in bypass_pending] == [recert.id]


def test_stale_promoted_recert_retries_after_cooldown(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(settings, "brain_queue_recert_cooldown_enabled", True)
    monkeypatch.setattr(settings, "brain_queue_recert_cooldown_minutes", 360)
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=7)
    recert = _queued_pattern(
        db,
        name="stale unresolved recert",
        lifecycle_stage="promoted",
        backtest_priority=0,
        recert_required=True,
        last_backtest_at=stale,
    )
    db.commit()

    pending = get_pending_patterns(db, limit=5)
    status = get_queue_status(db, use_cache=False)

    assert [p.id for p in pending] == [recert.id]
    assert status["recert_pending"] == 1
    assert status["recert_cooled"] == 0


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


def test_zero_trade_run_clears_stale_latest_metrics(db, monkeypatch):
    _deactivate_existing_patterns(db)
    monkeypatch.setattr(settings, "chili_backtest_zero_trade_demote_threshold", 3)
    pattern = _queued_pattern(
        db,
        name="candidate with stale positive metrics",
        lifecycle_stage="candidate",
        backtest_priority=10,
    )
    pattern.queue_tier = QUEUE_TIER_FULL
    pattern.win_rate = 0.9
    pattern.avg_return_pct = 2.5
    db.commit()

    mark_pattern_tested(
        db,
        pattern,
        backtests_run=24,
        trade_bearing_tickers=0,
    )
    db.refresh(pattern)

    assert pattern.win_rate is None
    assert pattern.avg_return_pct is None
    assert pattern.consecutive_zero_trade_runs == 1


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


def test_priority_scoring_deprioritizes_hard_negative_challenged_patterns(db):
    _deactivate_existing_patterns(db)
    stale = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=8)
    hard_negative = _queued_pattern(
        db,
        name="hard negative challenged",
        lifecycle_stage="challenged",
        promotion_status="challenged_cpcv_ev:realized_",
        backtest_priority=70,
        last_backtest_at=stale,
    )
    hard_negative.backtest_count = 100
    hard_negative.win_rate = 0.2
    hard_negative.avg_return_pct = -1.25
    demoted = _queued_pattern(
        db,
        name="demoted evidence gap",
        lifecycle_stage="challenged",
        promotion_status="demoted_evidence_gap",
        backtest_priority=70,
        last_backtest_at=stale,
    )
    demoted.backtest_count = 3
    uncertain = _queued_pattern(
        db,
        name="uncertain challenged",
        lifecycle_stage="challenged",
        promotion_status="challenged_cpcv_hypothesis_c",
        backtest_priority=0,
        last_backtest_at=stale,
    )
    uncertain.backtest_count = 10
    uncertain.win_rate = 0.55
    uncertain.avg_return_pct = 0.1
    db.commit()

    run_priority_scoring(db)
    db.refresh(hard_negative)
    db.refresh(demoted)
    db.refresh(uncertain)

    assert hard_negative.backtest_priority == 20
    assert demoted.backtest_priority == 0
    assert uncertain.backtest_priority == 70


def test_priority_scoring_routes_thin_candidates_to_prescreen(db):
    _deactivate_existing_patterns(db)
    thin = _queued_pattern(
        db,
        name="thin candidate",
        lifecycle_stage="candidate",
        backtest_priority=0,
    )
    thin.queue_tier = QUEUE_TIER_FULL
    thin.backtest_count = 0
    thin.avg_return_pct = None
    thin.win_rate = None
    proven = _queued_pattern(
        db,
        name="proven candidate",
        lifecycle_stage="candidate",
        backtest_priority=0,
    )
    proven.queue_tier = QUEUE_TIER_FULL
    proven.backtest_count = 20
    proven.avg_return_pct = 1.2
    proven.win_rate = 0.6
    db.commit()

    summary = run_priority_scoring(db)
    db.refresh(thin)
    db.refresh(proven)

    assert summary["prescreened"] == 1
    assert thin.queue_tier == QUEUE_TIER_PRESCREEN
    assert proven.queue_tier == QUEUE_TIER_FULL


def test_priority_scoring_routes_hard_negative_challenged_to_prescreen(db):
    _deactivate_existing_patterns(db)
    hard_negative = _queued_pattern(
        db,
        name="hard negative challenged",
        lifecycle_stage="challenged",
        promotion_status="challenged_cpcv_ev:negative",
        backtest_priority=0,
    )
    hard_negative.queue_tier = QUEUE_TIER_FULL
    hard_negative.backtest_count = 75
    hard_negative.avg_return_pct = -0.2
    hard_negative.win_rate = 0.31
    uncertain = _queued_pattern(
        db,
        name="uncertain challenged",
        lifecycle_stage="challenged",
        promotion_status="challenged_hypothesis",
        backtest_priority=0,
    )
    uncertain.queue_tier = QUEUE_TIER_FULL
    uncertain.backtest_count = 75
    uncertain.avg_return_pct = 0.1
    uncertain.win_rate = 0.45
    db.commit()

    summary = run_priority_scoring(db)
    db.refresh(hard_negative)
    db.refresh(uncertain)

    assert summary["prescreened"] == 1
    assert hard_negative.queue_tier == QUEUE_TIER_PRESCREEN
    assert uncertain.queue_tier == QUEUE_TIER_FULL


def test_priority_scoring_routes_demoted_evidence_gap_to_prescreen(db):
    _deactivate_existing_patterns(db)
    demoted = _queued_pattern(
        db,
        name="demoted evidence gap should stay cheap",
        lifecycle_stage="challenged",
        promotion_status="demoted_evidence_gap",
        backtest_priority=70,
    )
    demoted.backtest_count = 12
    demoted.win_rate = 0.45
    demoted.avg_return_pct = 0.1
    demoted.queue_tier = QUEUE_TIER_FULL
    db.commit()

    summary = run_priority_scoring(db)
    db.refresh(demoted)

    assert summary["prescreened"] == 1
    assert demoted.queue_tier == QUEUE_TIER_PRESCREEN


def test_walltime_timeout_demotes_non_operational_pattern_to_prescreen(db):
    from app.services.trading.backtest_queue_worker import mark_walltime_timeout_pattern

    _deactivate_existing_patterns(db)
    challenged = _queued_pattern(
        db,
        name="slow challenged",
        lifecycle_stage="challenged",
        backtest_priority=70,
    )
    challenged.queue_tier = QUEUE_TIER_FULL
    promoted = _queued_pattern(
        db,
        name="slow promoted",
        lifecycle_stage="promoted",
        backtest_priority=70,
    )
    promoted.queue_tier = QUEUE_TIER_FULL
    db.commit()

    mark_walltime_timeout_pattern(db, challenged, timeout_seconds=900.0)
    mark_walltime_timeout_pattern(db, promoted, timeout_seconds=900.0)
    db.refresh(challenged)
    db.refresh(promoted)

    assert challenged.queue_tier == QUEUE_TIER_PRESCREEN
    assert challenged.backtest_priority == 0
    assert challenged.consecutive_zero_trade_runs == 1
    assert promoted.queue_tier == QUEUE_TIER_FULL
    assert promoted.backtest_priority == 0
    assert promoted.consecutive_zero_trade_runs == 1
