"""Shadow-vetting finalizer tests.

These cover the staged path after ``shadow_promoted``: strong CPCV patterns
can become confidence-sized ``pilot_promoted`` before full directional
evidence matures, then graduate to normal ``promoted`` only after a real
composite score clears the adaptive top-pool policy.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

from app.models.trading import PaperTrade
from app.services.trading.pattern_shadow_vetting import (
    pilot_promoted_risk_multiplier,
    run_shadow_vetting_cycle,
    select_shadow_vetting_candidates,
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


def test_shadow_vetting_counts_autotrader_paper_dynamic_outcomes(db):
    _truncate_phase4_state(db)
    now = datetime.utcnow().replace(microsecond=0)
    pat = _make_pattern(
        db,
        name="paper_dynamic_shadow",
        lifecycle="shadow_promoted",
        cpcv=1.5,
        dsr=0.8,
        pbo=0.1,
        quality_score=None,
    )
    db.add(
        PaperTrade(
            user_id=None,
            scan_pattern_id=pat.id,
            ticker="TEST",
            direction="long",
            entry_price=100.0,
            stop_price=95.0,
            target_price=110.0,
            quantity=1.0,
            status="closed",
            entry_date=now - timedelta(hours=1),
            exit_date=now,
            exit_price=102.0,
            exit_reason="pattern_exit_now",
            pnl=2.0,
            pnl_pct=2.0,
            signal_json={"auto_trader_v1": True, "paper_shadow": True},
            paper_shadow_of_alert_id=None,
        )
    )
    db.commit()

    rows = select_shadow_vetting_candidates(
        db,
        settings_=_settings(
            chili_shadow_vetting_include_paper_dynamic_outcomes=True,
        ),
        now=now,
    )
    row = next(r for r in rows if r["scan_pattern_id"] == pat.id)
    assert row["raw_sample_n"] == 1
    assert row["paper_dynamic_sample_n"] == 1
    assert row["paper_dynamic_exit_sample_n"] == 1
    assert row["weighted_directional_wr"] > 0.5


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


def test_shadow_vetting_uses_fresh_weighted_evidence_before_30_samples(db, monkeypatch):
    _truncate_phase4_state(db)
    now = datetime.utcnow().replace(microsecond=0)
    fresh = _make_pattern(
        db,
        name="fresh_less_than_30",
        lifecycle="shadow_promoted",
        cpcv=1.8,
        dsr=1.0,
        pbo=0.02,
        quality_score=None,
    )
    stale = _make_pattern(
        db,
        name="stale_less_than_30",
        lifecycle="shadow_promoted",
        cpcv=1.8,
        dsr=1.0,
        pbo=0.02,
        quality_score=None,
    )
    _seed_directional_outcomes(
        db,
        pattern_id=fresh.id,
        n_correct=7,
        n_incorrect=2,
        base_time=now,
    )
    _seed_directional_outcomes(
        db,
        pattern_id=stale.id,
        n_correct=7,
        n_incorrect=2,
        base_time=now - timedelta(days=45),
    )

    monkeypatch.setattr(
        "app.services.trading.pattern_quality_score.compute_and_persist_scores",
        lambda *_args, **_kwargs: {"ok": True, "scored": 0},
    )

    candidates = {
        row["scan_pattern_id"]: row
        for row in select_shadow_vetting_candidates(
            db,
            settings_=_settings(chili_cpcv_target_promotion_pool_pct=0.50),
            now=now,
        )
    }

    assert candidates[fresh.id]["raw_sample_n"] == 9
    assert candidates[fresh.id]["effective_sample_n"] > 0
    assert candidates[fresh.id]["pilot_score"] > candidates[stale.id]["pilot_score"]
    assert candidates[fresh.id]["freshness"] > candidates[stale.id]["freshness"]

    out = run_shadow_vetting_cycle(
        db,
        settings_=_settings(chili_cpcv_target_promotion_pool_pct=0.50),
        now=now,
    )

    db.refresh(fresh)
    db.refresh(stale)
    assert fresh.id in out["pilot_ids"]
    assert fresh.lifecycle_stage == "pilot_promoted"
    assert fresh.promotion_status == "pilot_via_shadow_vetting"
    assert stale.lifecycle_stage == "shadow_promoted"


def test_shadow_vetting_allows_pilot_when_alpha_gate_only_needs_more_samples(
    db, monkeypatch,
):
    _truncate_phase4_state(db)
    pat = _make_pattern(
        db,
        name="pilot_soft_alpha_gate",
        lifecycle="shadow_promoted",
        quality_score=None,
    )

    monkeypatch.setattr(
        "app.services.trading.pattern_quality_score.compute_and_persist_scores",
        lambda *_args, **_kwargs: {"ok": True, "scored": 0},
    )
    monkeypatch.setattr(
        "app.services.trading.alpha_portfolio_gate.broker_risk_allowed",
        lambda *_args, **_kwargs: (
            False,
            {
                "full_promotion_block_reasons": [
                    "recert_required",
                    "insufficient_execution_quality_samples",
                ]
            },
        ),
    )

    out = run_shadow_vetting_cycle(
        db,
        settings_=_settings(chili_alpha_portfolio_gate_enabled=True),
    )

    db.refresh(pat)
    assert out["promoted_count"] == 0
    assert out["pilot_ids"] == [pat.id]
    assert out["alpha_portfolio_gate"]["full_risk_allowed"] is False
    assert out["alpha_portfolio_gate"]["pilot_risk_allowed"] is True
    assert pat.lifecycle_stage == "pilot_promoted"


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
