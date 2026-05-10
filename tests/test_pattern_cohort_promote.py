"""f-promotion-pipeline-rebalance Phase 4 (2026-05-10).

Tests for the composite quality score + weekly cohort auto-promote.

Pure / unit (no DB):
  - composite formula with full evidence (all 5 weights)
  - NULL propagation: any of cpcv / dsr / pbo / wr / decay → None
  - clipping: negative cpcv → 0; pbo > 1 → full overfit penalty
  - operator-tuned weights actually shift score

Integration (DB; ``_test``-suffixed):
  - kill switch off → no advances
  - first-week cycle promotes top-N capped by max_per_week
  - eligibility excludes thin directional evidence (< 30)
  - eligibility excludes below cpcv floor (< 1.0)
  - eligibility excludes promotion_gate_passed False
  - eligibility excludes already-shadow_promoted / promoted / live
  - cap enforcement within rolling 7-day window
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
        chili_cohort_promote_top_n=20,
        chili_cohort_promote_max_per_week=10,
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


def test_first_week_promotes_top_n_capped_by_max_per_week(db):
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
    cfg = _settings_stub(
        chili_cohort_promote_enabled=True,
        chili_cohort_promote_top_n=20,
        chili_cohort_promote_max_per_week=10,
    )
    out = run_cohort_promote_cycle(db, settings_=cfg)
    assert out["promoted_count"] == 10
    # Top 10 by score are the first 10 patterns (highest scores).
    expected_promoted_ids = {p.id for p in pats[:10]}
    assert set(out["promoted_ids"]) == expected_promoted_ids

    # Verify lifecycle transitioned in DB.
    for p in pats[:10]:
        db.refresh(p)
        assert p.lifecycle_stage == "shadow_promoted"
        assert p.lifecycle_changed_at is not None
    for p in pats[10:]:
        db.refresh(p)
        assert p.lifecycle_stage == "candidate"


def test_eligibility_filter_excludes_thin_directional_evidence(db):
    """j.1 binding: rolling_sample_n < 30 → not eligible."""
    _truncate_phase4_state(db)
    p_eligible = _make_pattern(
        db, name="eligible_30", quality_score=0.9,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_eligible.id, n_correct=20, n_incorrect=10,
    )
    p_thin = _make_pattern(
        db, name="thin_29", quality_score=0.95,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_thin.id, n_correct=20, n_incorrect=9,  # 29 total
    )
    cfg = _settings_stub()
    candidates = select_cohort_candidates(db, settings_=cfg)
    cand_ids = {p.id for p in candidates}
    assert p_eligible.id in cand_ids
    assert p_thin.id not in cand_ids


def test_eligibility_filter_excludes_below_cpcv_floor(db):
    _truncate_phase4_state(db)
    p_at_floor = _make_pattern(
        db, name="at_floor", cpcv=1.0, quality_score=0.7,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_at_floor.id, n_correct=20, n_incorrect=10,
    )
    p_below = _make_pattern(
        db, name="below_floor", cpcv=0.95, quality_score=0.7,
    )
    _seed_directional_outcomes(
        db, pattern_id=p_below.id, n_correct=20, n_incorrect=10,
    )
    cfg = _settings_stub()
    candidates = select_cohort_candidates(db, settings_=cfg)
    cand_ids = {p.id for p in candidates}
    assert p_at_floor.id in cand_ids
    assert p_below.id not in cand_ids


def test_eligibility_filter_excludes_promotion_gate_failed(db):
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
    cfg = _settings_stub()
    candidates = select_cohort_candidates(db, settings_=cfg)
    cand_ids = {p.id for p in candidates}
    assert p_passed.id in cand_ids
    assert p_failed.id not in cand_ids


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


def test_cap_enforcement_within_window(db):
    _truncate_phase4_state(db)
    # First, place 8 patterns at shadow_promoted within the last 7 days
    # (simulating prior cohort promotions earlier in the week).
    recent = datetime.utcnow() - timedelta(days=2)
    for i in range(8):
        _make_pattern(
            db, name=f"recent_{i}",
            lifecycle="shadow_promoted",
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
        chili_cohort_promote_max_per_week=10,
    )

    # Sanity check: counter sees the 8 prior promotions.
    assert count_recent_cohort_promotions(db) == 8

    out = run_cohort_promote_cycle(db, settings_=cfg)
    # Cap minus prior = 10 - 8 = 2 spots remaining → only 2 advance.
    assert out["promoted_count"] == 2
    assert out["spots_remaining_before"] == 2

    # Re-running within the same week → cap reached, 0 advance.
    out2 = run_cohort_promote_cycle(db, settings_=cfg)
    assert out2["skipped"] == "cap_reached"


def test_tied_scores_tiebreaker_by_id_asc(db):
    _truncate_phase4_state(db)
    # Two patterns with identical composite score; only one slot.
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

    cfg = _settings_stub(
        chili_cohort_promote_enabled=True,
        chili_cohort_promote_max_per_week=1,
    )
    out = run_cohort_promote_cycle(db, settings_=cfg)
    assert out["promoted_count"] == 1
    assert out["promoted_ids"] == [p_lower_id.id]


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
    cfg = _settings_stub(
        chili_cohort_promote_enabled=True,
        chili_cohort_promote_max_per_week=10,
    )
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
