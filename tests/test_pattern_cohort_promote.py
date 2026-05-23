"""f-promotion-pipeline-rebalance Phase 4 (2026-05-10).

Tests for the composite quality score + weekly cohort auto-promote.

Pure / unit (no DB):
  - composite formula with full evidence (all 5 weights)
  - NULL propagation: any of cpcv / dsr / pbo / wr / decay → None
  - clipping: negative cpcv → 0; pbo > 1 → full overfit penalty
  - operator-tuned weights actually shift score

Integration (DB; ``_test``-suffixed):
  - kill switch off → no advances
  - first cycle stages all adaptive-passed candidates for observation
  - eligibility allows thin/no directional evidence for shadow-observation bootstrap
  - eligibility trusts the adaptive CPCV verdict instead of a median-sharpe floor
  - strict eligibility excludes promotion_gate_passed False
  - bootstrap eligibility admits top pool-relative gate-failed near-misses
  - eligibility excludes already-shadow_promoted / promoted / live
  - observation staging ignores pilot/full roster target
  - tied scores → tiebreaker by id ASC
  - idempotent within week
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy import text

from app.models.trading import ScanPattern
from app.services.trading.pattern_quality_score import (
    compute_quality_composite_score,
    compute_and_persist_scores,
    _clip,
)
from app.services.trading.pattern_cohort_promote import (
    count_recent_cohort_promotions,
    run_cohort_promote_cycle,
    select_cohort_candidates,
)


DEFAULT_WEIGHTS = {
    "cpcv_sharpe": 0.30,
    "deflated_sharpe": 0.20,
    "pbo_inverse": 0.15,
    "directional_wr": 0.25,
    "decay_inverse": 0.10,
}


def _settings_stub(**overrides):
    base = dict(
        chili_cohort_promote_enabled=True,
        chili_cohort_score_weight_cpcv_sharpe=0.30,
        chili_cohort_score_weight_deflated_sharpe=0.20,
        chili_cohort_score_weight_pbo_inverse=0.15,
        chili_cohort_score_weight_directional_wr=0.25,
        chili_cohort_score_weight_decay_inverse=0.10,
        chili_cohort_promote_bootstrap_near_miss_enabled=True,
        chili_cohort_promote_bootstrap_min_cpcv_sharpe=0.0,
        chili_cohort_promote_bootstrap_min_deflated_sharpe=0.0,
        chili_cohort_promote_bootstrap_max_pbo=1.0,
        chili_cpcv_target_promotion_pool_pct=1.0,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


# ── Pure unit tests ──────────────────────────────────────────────────


def test_compute_with_full_evidence_uses_all_5_weights():
    pat = SimpleNamespace(
        cpcv_median_sharpe=2.0,
        deflated_sharpe=1.0,
        pbo=0.0,
    )
    # cpcv_n = clip(2.0/2.0)=1.0, dsr_n=1.0, pbo_inv=1.0, wr=0.7, dec_inv=0.9
    # composite = 0.30*1.0 + 0.20*1.0 + 0.15*1.0 + 0.25*0.7 + 0.10*0.9
    #           = 0.30 + 0.20 + 0.15 + 0.175 + 0.09 = 0.915
    score = compute_quality_composite_score(
        pat, directional_wr=0.7, decay=0.1, weights=DEFAULT_WEIGHTS,
    )
    assert score == pytest.approx(0.915, rel=1e-6)


def test_compute_pattern_585_calibration_check():
    # Cowork's calibration check from j.2 of the plan response:
    # cpcv=1.4 → 0.7, dsr=1.0 → 1.0, pbo=0.0 → 1.0, wr=0.733, decay=0 → 1.0
    pat = SimpleNamespace(
        cpcv_median_sharpe=1.4, deflated_sharpe=1.0, pbo=0.0,
    )
    score = compute_quality_composite_score(
        pat, directional_wr=0.733, decay=0.0, weights=DEFAULT_WEIGHTS,
    )
    # 0.30*0.7 + 0.20*1.0 + 0.15*1.0 + 0.25*0.733 + 0.10*1.0
    # = 0.21 + 0.20 + 0.15 + 0.18325 + 0.10 = 0.84325
    assert score == pytest.approx(0.84325, rel=1e-5)


def test_compute_with_null_directional_wr_returns_none():
    pat = SimpleNamespace(
        cpcv_median_sharpe=2.0, deflated_sharpe=1.0, pbo=0.0,
    )
    score = compute_quality_composite_score(
        pat, directional_wr=None, decay=0.1, weights=DEFAULT_WEIGHTS,
    )
    assert score is None


def test_compute_with_null_decay_returns_none():
    """j.1 binding: NULL decay → exclude entirely (NOT renormalize)."""
    pat = SimpleNamespace(
        cpcv_median_sharpe=2.0, deflated_sharpe=1.0, pbo=0.0,
    )
    score = compute_quality_composite_score(
        pat, directional_wr=0.7, decay=None, weights=DEFAULT_WEIGHTS,
    )
    assert score is None


def test_compute_with_null_cpcv_returns_none():
    pat = SimpleNamespace(
        cpcv_median_sharpe=None, deflated_sharpe=1.0, pbo=0.0,
    )
    score = compute_quality_composite_score(
        pat, directional_wr=0.7, decay=0.1, weights=DEFAULT_WEIGHTS,
    )
    assert score is None


def test_compute_with_null_dsr_returns_none():
    pat = SimpleNamespace(
        cpcv_median_sharpe=2.0, deflated_sharpe=None, pbo=0.0,
    )
    score = compute_quality_composite_score(
        pat, directional_wr=0.7, decay=0.1, weights=DEFAULT_WEIGHTS,
    )
    assert score is None


def test_compute_with_null_pbo_returns_none():
    pat = SimpleNamespace(
        cpcv_median_sharpe=2.0, deflated_sharpe=1.0, pbo=None,
    )
    score = compute_quality_composite_score(
        pat, directional_wr=0.7, decay=0.1, weights=DEFAULT_WEIGHTS,
    )
    assert score is None


def test_compute_negative_cpcv_clips_to_zero():
    pat = SimpleNamespace(
        cpcv_median_sharpe=-0.5, deflated_sharpe=1.0, pbo=0.0,
    )
    # cpcv_n = clip(-0.5/2.0, 0, 1) = 0.0 → no contribution from w1.
    # 0 + 0.20*1.0 + 0.15*1.0 + 0.25*0.5 + 0.10*1.0 = 0.575
    score = compute_quality_composite_score(
        pat, directional_wr=0.5, decay=0.0, weights=DEFAULT_WEIGHTS,
    )
    assert score == pytest.approx(0.575, rel=1e-6)


def test_compute_pbo_above_one_clips_to_full_penalty():
    pat = SimpleNamespace(
        cpcv_median_sharpe=2.0, deflated_sharpe=1.0, pbo=1.5,
    )
    # pbo_inv = 1 - clip(1.5, 0, 1) = 0.0 → w3 contributes 0.
    # 0.30*1.0 + 0.20*1.0 + 0 + 0.25*0.5 + 0.10*1.0 = 0.725
    score = compute_quality_composite_score(
        pat, directional_wr=0.5, decay=0.0, weights=DEFAULT_WEIGHTS,
    )
    assert score == pytest.approx(0.725, rel=1e-6)


def test_compute_settings_propagate_to_score():
    """Operator-tuned weights actually shift the score."""
    pat = SimpleNamespace(
        cpcv_median_sharpe=2.0, deflated_sharpe=1.0, pbo=0.0,
    )
    # Default weights → 0.915 per first test.
    default = compute_quality_composite_score(
        pat, directional_wr=0.7, decay=0.1, weights=DEFAULT_WEIGHTS,
    )
    # Tune w4 (directional_wr) up to 0.50, redistribute others down.
    tuned = {
        "cpcv_sharpe": 0.20,
        "deflated_sharpe": 0.15,
        "pbo_inverse": 0.10,
        "directional_wr": 0.50,
        "decay_inverse": 0.05,
    }
    # 0.20*1.0 + 0.15*1.0 + 0.10*1.0 + 0.50*0.7 + 0.05*0.9
    # = 0.20 + 0.15 + 0.10 + 0.35 + 0.045 = 0.845
    tuned_score = compute_quality_composite_score(
        pat, directional_wr=0.7, decay=0.1, weights=tuned,
    )
    assert default != tuned_score
    assert tuned_score == pytest.approx(0.845, rel=1e-6)


def test_clip_helper_bounds():
    assert _clip(-1.0) == 0.0
    assert _clip(0.5) == 0.5
    assert _clip(1.5) == 1.0
    assert _clip(2.5, lo=0.0, hi=2.0) == 2.0


# ── Integration tests (DB-bound) ─────────────────────────────────────


def _make_pattern(
    db,
    *,
    name,
    lifecycle="candidate",
    cpcv=1.5,
    dsr=1.0,
    pbo=0.0,
    promotion_gate=True,
    quality_score=None,
    lifecycle_changed_at=None,
):
    pat = ScanPattern(
        name=name,
        rules_json={"hold_hours": 24},
        origin="brain",
        asset_class="stock",
        timeframe="1d",
        confidence=0.7,
        evidence_count=10,
        active=True,
        promotion_status="legacy",
        lifecycle_stage=lifecycle,
        # CHECK constraint: promotion_gate_passed IS NULL OR cpcv_n_paths IS NOT NULL.
        cpcv_n_paths=10,
        cpcv_median_sharpe=cpcv,
        deflated_sharpe=dsr,
        pbo=pbo,
        n_effective_trials=10,
        promotion_gate_passed=promotion_gate,
        quality_composite_score=quality_score,
    )
    if lifecycle_changed_at is not None:
        pat.lifecycle_changed_at = lifecycle_changed_at
    db.add(pat)
    db.flush()
    return pat


def _seed_directional_outcomes(
    db, *, pattern_id, n_correct=20, n_incorrect=10, base_time=None,
):
    """Seed N rows in pattern_alert_directional_outcome with the
    requested correct/incorrect mix so the rolling view picks them up.

    Inserts directly via SQL (avoids needing a real trading_alerts row
    chain) — uses ON DELETE CASCADE-tolerant approach by inserting
    a placeholder trading_alerts row first.
    """
    base_time = base_time or datetime.utcnow().replace(microsecond=0)
    total = n_correct + n_incorrect

    rows = []
    for i in range(total):
        # Newer alerts (lower index) come first when ordered by alert_at DESC.
        alert_at = base_time - timedelta(hours=i)
        # Stripe correct vs incorrect to ensure both halves of the
        # rolling-30 split see a mix (otherwise older=all-correct,
        # newer=all-incorrect makes decay artificially extreme).
        correct = (i % 3 != 0) if i < n_correct else False
        if i < n_correct:
            correct = True
        else:
            correct = False
        rows.append((alert_at, correct))

    for i, (alert_at, correct) in enumerate(rows):
        # Insert a placeholder trading_alerts row so the FK is satisfied.
        alert_id = db.execute(text(
            """
            INSERT INTO trading_alerts (
                alert_type, ticker, message, sent_via, success,
                created_at
            ) VALUES (
                'pattern_breakout_imminent', 'TEST', 'seed', 'log_only',
                TRUE, :alert_at
            ) RETURNING id
            """
        ), {"alert_at": alert_at}).scalar()

        db.execute(text(
            """
            INSERT INTO pattern_alert_directional_outcome (
                alert_id, scan_pattern_id, ticker, alert_at,
                predicted_direction, hold_window_hours,
                window_close_at, directional_threshold_pct,
                directional_correct, evaluated_at
            ) VALUES (
                :alert_id, :pid, 'TEST', :alert_at, 'up', 24,
                :wclose, 1.5, :correct, :evaluated_at
            )
            """
        ), {
            "alert_id": alert_id,
            "pid": pattern_id,
            "alert_at": alert_at,
            "wclose": alert_at + timedelta(hours=24),
            "correct": correct,
            "evaluated_at": alert_at + timedelta(hours=25),
        })

    db.commit()


def _truncate_phase4_state(db):
    """Per-test: clear directional outcomes + alerts + scan patterns."""
    db.execute(text("DELETE FROM pattern_alert_directional_outcome"))
    db.execute(text(
        "DELETE FROM trading_alerts "
        "WHERE alert_type='pattern_breakout_imminent'"
    ))
    db.execute(text("DELETE FROM scan_patterns"))
    db.commit()


def test_kill_switch_off_state_skips_cycle(db):
    _truncate_phase4_state(db)
    pat = _make_pattern(
        db, name="kill_switch_test",
        lifecycle="candidate", quality_score=0.9,
    )
    _seed_directional_outcomes(
        db, pattern_id=pat.id, n_correct=25, n_incorrect=5,
    )
    cfg = _settings_stub(chili_cohort_promote_enabled=False)
    out = run_cohort_promote_cycle(db, settings_=cfg)
    assert out["skipped"] == "flag_disabled"
    db.refresh(pat)
    assert pat.lifecycle_stage == "candidate"


def test_first_cycle_stages_all_adaptive_passed_for_observation(db):
    _truncate_phase4_state(db)
    # Seed 12 eligible candidates with descending composite scores.
    pats = []
    for i in range(12):
        score = 0.95 - i * 0.01  # 0.95, 0.94, … 0.84
        p = _make_pattern(
            db, name=f"cand_{i:02d}",
            lifecycle="candidate", cpcv=1.5, quality_score=score,
        )
        _seed_directional_outcomes(
            db, pattern_id=p.id, n_correct=22, n_incorrect=8,
        )
        pats.append(p)
    cfg = _settings_stub(chili_cohort_promote_enabled=True)
    out = run_cohort_promote_cycle(db, settings_=cfg)
    assert out["observation_stage_uncapped"] is True
    assert out["promoted_count"] == 12
    expected_promoted_ids = {p.id for p in pats}
    assert set(out["promoted_ids"]) == expected_promoted_ids

    # Verify lifecycle transitioned in DB.
    for p in pats:
        db.refresh(p)
        assert p.lifecycle_stage == "shadow_promoted"
        assert p.lifecycle_changed_at is not None


def test_eligibility_allows_thin_directional_for_shadow_observation_bootstrap(db):
    """Shadow promotion is how a candidate collects directional outcomes."""
    _truncate_phase4_state(db)
    p_thin = _make_pattern(
        db, name="thin_29", quality_score=None, cpcv=2.0,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_thin.id, n_correct=20, n_incorrect=9,  # 29 total
    )
    p_none = _make_pattern(
        db, name="no_directional_yet", quality_score=None, cpcv=1.8,
    )
    cfg = _settings_stub()
    candidates = select_cohort_candidates(db, settings_=cfg)
    cand_ids = {p.id for p in candidates}
    assert p_thin.id in cand_ids
    assert p_none.id in cand_ids


def test_scored_candidates_rank_ahead_of_cpcv_only_candidates(db):
    _truncate_phase4_state(db)
    p_scored = _make_pattern(
        db, name="scored", quality_score=0.7, cpcv=1.2,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_scored.id, n_correct=20, n_incorrect=10,
    )
    p_cpcv_only = _make_pattern(
        db, name="cpcv_only_higher_sharpe", quality_score=None, cpcv=9.0,
    )
    cfg = _settings_stub()
    candidates = select_cohort_candidates(db, settings_=cfg)
    assert [p.id for p in candidates[:2]] == [p_scored.id, p_cpcv_only.id]


def test_eligibility_trusts_adaptive_cpcv_verdict_without_median_floor(db):
    _truncate_phase4_state(db)
    p_lower_cpcv = _make_pattern(
        db, name="adaptive_pass_lower_cpcv", cpcv=0.95, quality_score=None,
    )
    p_higher_cpcv = _make_pattern(db, name="higher_cpcv", cpcv=1.5, quality_score=None)
    cfg = _settings_stub()
    candidates = select_cohort_candidates(db, settings_=cfg)
    cand_ids = [p.id for p in candidates]
    assert p_lower_cpcv.id in cand_ids
    assert cand_ids.index(p_higher_cpcv.id) < cand_ids.index(p_lower_cpcv.id)


def test_eligibility_recovers_stale_challenged_when_adaptive_gate_passes(db):
    _truncate_phase4_state(db)
    p_challenged_now_passed = _make_pattern(
        db,
        name="old_gate_failed_now_passed",
        lifecycle="challenged",
        cpcv=1.7,
        quality_score=0.8,
        promotion_gate=True,
    )
    p_challenged_still_failed = _make_pattern(
        db,
        name="old_gate_failed_still_failed",
        lifecycle="challenged",
        cpcv=1.7,
        quality_score=0.8,
        promotion_gate=False,
    )
    cfg = _settings_stub(
        chili_cohort_promote_bootstrap_near_miss_enabled=False,
    )
    candidates = select_cohort_candidates(db, settings_=cfg)
    cand_ids = {p.id for p in candidates}
    assert p_challenged_now_passed.id in cand_ids
    assert p_challenged_still_failed.id not in cand_ids


def test_strict_eligibility_filter_excludes_promotion_gate_failed(db):
    _truncate_phase4_state(db)
    p_passed = _make_pattern(
        db, name="passed", promotion_gate=True, quality_score=0.7,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_passed.id, n_correct=20, n_incorrect=10,
    )
    p_failed = _make_pattern(
        db, name="failed", promotion_gate=False, quality_score=0.7,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_failed.id, n_correct=20, n_incorrect=10,
    )
    cfg = _settings_stub(
        chili_cohort_promote_bootstrap_near_miss_enabled=False,
    )
    candidates = select_cohort_candidates(db, settings_=cfg)
    cand_ids = {p.id for p in candidates}
    assert p_passed.id in cand_ids
    assert p_failed.id not in cand_ids


def test_bootstrap_near_miss_admits_top_pool_relative_gate_failures(db):
    _truncate_phase4_state(db)
    p_passed = _make_pattern(
        db,
        name="gate_passed_mid_pool",
        promotion_gate=True,
        quality_score=None,
        cpcv=1.0,
        dsr=0.6,
        pbo=0.5,
    )
    p_near_miss = _make_pattern(
        db,
        name="bootstrap_near_miss",
        promotion_gate=False,
        quality_score=None,
        cpcv=3.0,
        dsr=1.5,
        pbo=0.0,
    )
    p_weak = _make_pattern(
        db,
        name="weak_gate_failed",
        promotion_gate=False,
        quality_score=None,
        cpcv=-1.0,
        dsr=0.0,
        pbo=0.95,
    )
    p_wrong_sign = _make_pattern(
        db,
        name="wrong_sign_dsr",
        promotion_gate=False,
        quality_score=None,
        cpcv=4.0,
        dsr=0.0,
        pbo=0.0,
    )

    cfg = _settings_stub(chili_cpcv_target_promotion_pool_pct=0.5)
    candidates = select_cohort_candidates(db, settings_=cfg)
    cand_ids = [p.id for p in candidates]
    assert p_near_miss.id in cand_ids
    assert p_passed.id in cand_ids
    assert p_weak.id not in cand_ids
    assert p_wrong_sign.id not in cand_ids
    assert cand_ids.index(p_near_miss.id) < cand_ids.index(p_passed.id)


def test_eligibility_filter_excludes_promoted_and_shadow_promoted(db):
    _truncate_phase4_state(db)
    p_already_promoted = _make_pattern(
        db, name="already_promoted",
        lifecycle="promoted", quality_score=0.9,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_already_promoted.id,
        n_correct=20, n_incorrect=10,
    )
    p_already_shadow = _make_pattern(
        db, name="already_shadow",
        lifecycle="shadow_promoted", quality_score=0.9,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_already_shadow.id,
        n_correct=20, n_incorrect=10,
    )
    p_live = _make_pattern(
        db, name="already_live",
        lifecycle="live", quality_score=0.9,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_live.id, n_correct=20, n_incorrect=10,
    )
    p_eligible = _make_pattern(
        db, name="eligible",
        lifecycle="candidate", quality_score=0.7,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_eligible.id, n_correct=20, n_incorrect=10,
    )
    cfg = _settings_stub()
    candidates = select_cohort_candidates(db, settings_=cfg)
    cand_ids = {p.id for p in candidates}
    assert p_eligible.id in cand_ids
    assert p_already_promoted.id not in cand_ids
    assert p_already_shadow.id not in cand_ids
    assert p_live.id not in cand_ids


def test_observation_stage_ignores_risk_roster_target(db):
    _truncate_phase4_state(db)
    # First, place 8 patterns in staged/live roster states.
    recent = datetime.utcnow() - timedelta(days=2)
    existing_lifecycles = [
        "shadow_promoted",
        "pilot_promoted",
        "promoted",
        "live",
        "shadow_promoted",
        "pilot_promoted",
        "promoted",
        "live",
    ]
    for i, lifecycle in enumerate(existing_lifecycles):
        _make_pattern(
            db, name=f"recent_{i}",
            lifecycle=lifecycle,
            quality_score=0.95,
            lifecycle_changed_at=recent,
        )

    # Now seed 20 fresh eligible candidates.
    fresh = []
    for i in range(20):
        p = _make_pattern(
            db, name=f"fresh_{i:02d}",
            lifecycle="candidate", quality_score=0.95 - i * 0.01,
        )
        _seed_directional_outcomes(
            db, pattern_id=p.id, n_correct=22, n_incorrect=8,
        )
        fresh.append(p)

    cfg = _settings_stub(
        chili_cohort_promote_enabled=True,
        chili_cpcv_target_promotion_pool_pct=0.5,
    )

    # Legacy counter still counts shadow-only transitions.
    assert count_recent_cohort_promotions(db) == 2

    out = run_cohort_promote_cycle(db, settings_=cfg)
    # Shadow observation is not broker exposure; all adaptive-passed fresh
    # candidates are staged even though the pilot/full roster target is tighter.
    assert out["observation_stage_uncapped"] is True
    assert out["promoted_count"] == 20
    assert set(out["promoted_ids"]) == {p.id for p in fresh}

    # Re-running after all eligible rows are staged advances no additional rows.
    out2 = run_cohort_promote_cycle(db, settings_=cfg)
    assert out2["promoted_count"] == 0


def test_tied_scores_tiebreaker_by_id_asc(db):
    _truncate_phase4_state(db)
    # Two patterns with identical composite score.
    p_lower_id = _make_pattern(
        db, name="tied_lower_id", quality_score=0.80,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_lower_id.id, n_correct=20, n_incorrect=10,
    )
    p_higher_id = _make_pattern(
        db, name="tied_higher_id", quality_score=0.80,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_higher_id.id, n_correct=20, n_incorrect=10,
    )
    assert p_lower_id.id < p_higher_id.id

    cfg = _settings_stub(chili_cohort_promote_enabled=True)
    candidates = select_cohort_candidates(db, settings_=cfg)
    assert [p.id for p in candidates[:2]] == [p_lower_id.id, p_higher_id.id]


def test_idempotent_within_week(db):
    _truncate_phase4_state(db)
    pats = []
    for i in range(5):
        p = _make_pattern(
            db, name=f"idem_{i}", quality_score=0.9 - i * 0.01,
        )
        _seed_directional_outcomes(
            db, pattern_id=p.id, n_correct=22, n_incorrect=8,
        )
        pats.append(p)
    cfg = _settings_stub(chili_cohort_promote_enabled=True)
    out1 = run_cohort_promote_cycle(db, settings_=cfg)
    assert out1["promoted_count"] == 5

    # Second invocation: all 5 are now at shadow_promoted, none eligible.
    out2 = run_cohort_promote_cycle(db, settings_=cfg)
    assert out2["promoted_count"] == 0


def test_compute_and_persist_scores_populates_column(db):
    """End-to-end: the score-refresh job actually writes scores."""
    _truncate_phase4_state(db)
    p_full = _make_pattern(
        db, name="full_evidence",
        cpcv=1.5, dsr=1.0, pbo=0.0, quality_score=None,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_full.id, n_correct=20, n_incorrect=10,
    )
    p_thin = _make_pattern(
        db, name="thin_evidence",
        cpcv=1.5, dsr=1.0, pbo=0.0, quality_score=None,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_thin.id, n_correct=15, n_incorrect=10,
    )

    cfg = _settings_stub()
    result = compute_and_persist_scores(db, settings_=cfg)

    db.refresh(p_full)
    db.refresh(p_thin)
    assert p_full.quality_composite_score is not None
    assert p_thin.quality_composite_score is None  # < 30 outcomes
    assert result["scored"] >= 1
    assert result["skipped_thin_directional"] >= 1
