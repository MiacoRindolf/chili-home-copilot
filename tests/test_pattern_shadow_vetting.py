"""Shadow-vetting finalizer tests.

These cover the staged path after ``shadow_promoted``: strong CPCV patterns
can become confidence-sized ``pilot_promoted`` before full directional
evidence matures, then graduate to normal ``promoted`` only after a real
composite score clears the adaptive top-pool policy.
"""
from __future__ import annotations

from types import SimpleNamespace

from app.services.trading.pattern_shadow_vetting import (
    pilot_promoted_risk_multiplier,
    run_shadow_vetting_cycle,
)
from tests.test_pattern_cohort_promote import (
    _make_pattern,
    _seed_directional_outcomes,
    _truncate_phase4_state,
)


def _settings(**overrides):
    base = dict(
        chili_shadow_vetting_finalize_enabled=True,
        chili_pilot_promoted_enabled=True,
        chili_cpcv_target_promotion_pool_pct=0.05,
        chili_cpcv_ci_level=0.90,
        chili_cohort_score_weight_cpcv_sharpe=0.30,
        chili_cohort_score_weight_deflated_sharpe=0.20,
        chili_cohort_score_weight_pbo_inverse=0.15,
        chili_cohort_score_weight_directional_wr=0.25,
        chili_cohort_score_weight_decay_inverse=0.10,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def test_shadow_vetting_advances_strong_shadow_to_pilot_before_full_ev(db, monkeypatch):
    _truncate_phase4_state(db)
    pat = _make_pattern(
        db,
        name="thin_shadow",
        lifecycle="shadow_promoted",
        quality_score=None,
    )

    monkeypatch.setattr(
        "app.services.trading.pattern_quality_score.compute_and_persist_scores",
        lambda *_args, **_kwargs: {"ok": True, "scored": 0},
    )

    out = run_shadow_vetting_cycle(db, settings_=_settings())

    db.refresh(pat)
    assert out["promoted_count"] == 0
    assert out["pilot_ids"] == [pat.id]
    assert pat.lifecycle_stage == "pilot_promoted"
    assert pat.promotion_status == "pilot_via_shadow_vetting"

    mult = pilot_promoted_risk_multiplier(db, pat.id, settings_=_settings())
    assert mult is not None
    assert 0.0 < mult <= 1.0


def test_shadow_vetting_promotes_scored_top_pool_shadow(db, monkeypatch):
    _truncate_phase4_state(db)
    _make_pattern(
        db,
        name="live_reference",
        lifecycle="promoted",
        quality_score=0.50,
    )
    shadow = _make_pattern(
        db,
        name="strong_shadow",
        lifecycle="shadow_promoted",
        quality_score=0.90,
    )
    _seed_directional_outcomes(
        db, pattern_id=shadow.id, n_correct=24, n_incorrect=6,
    )

    monkeypatch.setattr(
        "app.services.trading.pattern_quality_score.compute_and_persist_scores",
        lambda *_args, **_kwargs: {"ok": True, "scored": 1},
    )

    out = run_shadow_vetting_cycle(db, settings_=_settings())

    db.refresh(shadow)
    assert out["promoted_ids"] == [shadow.id]
    assert shadow.lifecycle_stage == "promoted"
    assert shadow.promotion_status == "promoted_via_shadow_vetting"


def test_shadow_vetting_holds_scored_shadow_below_adaptive_pool(db, monkeypatch):
    _truncate_phase4_state(db)
    _make_pattern(
        db,
        name="live_strong_reference",
        lifecycle="promoted",
        cpcv=3.0,
        dsr=1.0,
        pbo=0.0,
        quality_score=0.90,
    )
    shadow = _make_pattern(
        db,
        name="weak_shadow",
        lifecycle="shadow_promoted",
        cpcv=0.2,
        dsr=0.2,
        pbo=0.8,
        quality_score=0.50,
    )
    _seed_directional_outcomes(
        db, pattern_id=shadow.id, n_correct=18, n_incorrect=12,
    )

    monkeypatch.setattr(
        "app.services.trading.pattern_quality_score.compute_and_persist_scores",
        lambda *_args, **_kwargs: {"ok": True, "scored": 1},
    )

    out = run_shadow_vetting_cycle(db, settings_=_settings())

    db.refresh(shadow)
    assert out["promoted_count"] == 0
    assert out["held"] == 1
    assert shadow.lifecycle_stage == "shadow_promoted"
    assert shadow.promotion_status == "shadow_vetted_hold"


def test_shadow_vetting_can_disable_pilot_ramp(db, monkeypatch):
    _truncate_phase4_state(db)
    pat = _make_pattern(
        db,
        name="pilot_disabled_shadow",
        lifecycle="shadow_promoted",
        quality_score=None,
    )

    monkeypatch.setattr(
        "app.services.trading.pattern_quality_score.compute_and_persist_scores",
        lambda *_args, **_kwargs: {"ok": True, "scored": 0},
    )

    out = run_shadow_vetting_cycle(
        db, settings_=_settings(chili_pilot_promoted_enabled=False)
    )

    db.refresh(pat)
    assert out["pilot_count"] == 0
    assert out["collecting_ev"] == 1
    assert pat.lifecycle_stage == "shadow_promoted"
    assert pat.promotion_status == "shadow_collecting_ev"
