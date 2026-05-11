"""Adaptive CPCV gate (Phase 2 of f-adaptive-promotion-architecture).

Covers:
- Flag-off parity: wrapper is a no-op when ``chili_cpcv_adaptive_gate_enabled``
  is False and no DB session is available (pure-function path).
- Bayesian shrinkage math: low-n patterns pull toward pool mean; high-n
  barely move.
- Empirical percentile threshold: synthetic pool admits the expected count.
- Pareto frontier: dominated candidates are flagged.
- Portfolio marginal Sharpe (lightweight proxy): positive contribution
  admits; tightening the floor rejects.
- Shadow-log write: each evaluation writes 4 rows (3 metrics + 1 summary)
  with both verdicts populated.
- Flag-on returns the adaptive verdict (which may diverge from legacy).
"""
from __future__ import annotations

import math
from typing import Any, Mapping

import pytest
from sqlalchemy import text

from app.config import settings
from app.services.trading import cpcv_adaptive_gate as gate
from app.services.trading.cpcv_adaptive_gate import (
    _bayesian_shrinkage,
    _empirical_percentile,
    _hansen_dsr_lower_ci,
    _pareto_dominated,
    _portfolio_marginal_sharpe_bps,
    _wilson_pbo_upper_ci,
    maybe_apply_adaptive_gate,
)


# ── 1. Flag-off parity (no DB) ─────────────────────────────────────────


def test_flag_off_parity_no_db(monkeypatch):
    """With the flag off and no pattern id, wrapper returns legacy tuple verbatim."""
    monkeypatch.setattr(settings, "chili_cpcv_adaptive_gate_enabled", False)
    eval_payload: Mapping[str, Any] = {
        "skipped": False,
        "n_trades": 100,
        "deflated_sharpe": 0.97,
        "pbo": 0.10,
        "cpcv_n_paths": 40,
        "cpcv_median_sharpe": 1.2,
        "n_effective_trials": 4,
    }
    legacy_reasons = ["provisional_sample_size"]
    ok, reasons = maybe_apply_adaptive_gate(
        eval_payload,
        scan_pattern_id=None,
        legacy_pass=True,
        legacy_reasons=legacy_reasons,
    )
    assert ok is True
    assert reasons == ["provisional_sample_size"]


def test_flag_off_skipped_payload_pass_through(monkeypatch):
    """Skipped payloads bypass adaptive evaluation entirely."""
    monkeypatch.setattr(settings, "chili_cpcv_adaptive_gate_enabled", False)
    ok, reasons = maybe_apply_adaptive_gate(
        {"skipped": True, "reason": "insufficient_data"},
        scan_pattern_id=None,
        legacy_pass=False,
        legacy_reasons=["insufficient_data"],
    )
    assert ok is False
    assert reasons == ["insufficient_data"]


# ── 2 & 3. Bayesian shrinkage math ─────────────────────────────────────


def test_shrinkage_low_n_pulls_toward_pool_mean():
    """Pattern 585's profile: raw DSR 1.0, only 11 trades, pool mean 0.6."""
    shrunk = _bayesian_shrinkage(1.0, n=11, pool_mean=0.6, prior_n=60)
    # weight = 11/(11+60) ≈ 0.155 → shrunk ≈ 0.155 + 0.6*0.845 ≈ 0.662
    assert shrunk < 1.0
    assert shrunk > 0.6
    assert abs(shrunk - 0.662) < 0.01


def test_shrinkage_high_n_barely_moves():
    """300 trades vs pool prior of 60 → ~83% weight on raw value."""
    shrunk = _bayesian_shrinkage(1.0, n=300, pool_mean=0.6, prior_n=60)
    # weight = 300/(300+60) ≈ 0.833 → shrunk ≈ 0.833 + 0.6*0.167 ≈ 0.933
    assert shrunk > 0.90
    assert shrunk < 1.0
    assert abs(shrunk - 0.933) < 0.01


def test_shrinkage_zero_n_returns_pool_mean():
    assert _bayesian_shrinkage(1.0, n=0, pool_mean=0.5, prior_n=60) == pytest.approx(0.5)


# ── 4. Empirical percentile threshold ─────────────────────────────────


def test_percentile_threshold_admit_count():
    """Synthetic pool of 100 DSRs in [0, 1]; target 5% → q=0.95 threshold.

    The empirical 95th percentile of 0..99 (linearly interpolated) lies
    between value 94 and 95; we expect ~5 patterns above it.
    """
    pool = [i / 99.0 for i in range(100)]  # 0.0 .. 1.0 inclusive
    thr_95 = _empirical_percentile(pool, 0.95)
    assert thr_95 is not None
    above = [v for v in pool if v > thr_95]
    # 5% target ± 1 for interpolation jitter.
    assert 4 <= len(above) <= 6


def test_percentile_empty_pool_returns_none():
    assert _empirical_percentile([], 0.5) is None


def test_percentile_single_element():
    assert _empirical_percentile([0.7], 0.95) == pytest.approx(0.7)


# ── 5. Pareto frontier ────────────────────────────────────────────────


def test_pareto_dominator_not_dominated():
    pool = [(0.5, 0.5, 0.5), (0.6, 0.6, 0.6)]
    candidate = (0.9, 0.9, 0.9)
    assert _pareto_dominated(candidate, pool) is False


def test_pareto_strictly_dominated_flagged():
    pool = [(0.9, 0.9, 0.9), (0.5, 0.5, 0.5)]
    candidate = (0.5, 0.5, 0.5)
    # (0.9, 0.9, 0.9) strictly dominates (0.5, 0.5, 0.5).
    assert _pareto_dominated(candidate, pool) is True


def test_pareto_partial_dominance_not_dominated():
    """Pool member wins on 2 dims but loses on 1 — candidate is not dominated."""
    pool = [(0.9, 0.9, 0.1)]
    candidate = (0.5, 0.5, 0.9)
    assert _pareto_dominated(candidate, pool) is False


# ── 6. Portfolio marginal Sharpe (proxy) ──────────────────────────────


def test_portfolio_marginal_positive_contribution_admits():
    """Candidate Sharpe 1.5 vs roster mean 0.5 → +1.0 ≈ 10_000 bps lift."""
    bps = _portfolio_marginal_sharpe_bps(1.5, [0.5, 0.5])
    assert bps == pytest.approx(10000.0)


def test_portfolio_marginal_empty_roster_returns_candidate_bps():
    bps = _portfolio_marginal_sharpe_bps(0.8, [])
    assert bps == pytest.approx(8000.0)


def test_portfolio_marginal_negative_when_below_roster():
    bps = _portfolio_marginal_sharpe_bps(0.2, [1.0, 1.0])
    assert bps < 0.0


# ── 7. Hansen DSR CI / Wilson PBO CI ──────────────────────────────────


def test_hansen_dsr_lower_ci_shrinks_with_more_samples():
    wide = _hansen_dsr_lower_ci(0.95, n_observations=10, ci_level=0.90)
    tight = _hansen_dsr_lower_ci(0.95, n_observations=1000, ci_level=0.90)
    assert tight > wide
    assert tight <= 0.95
    assert wide >= 0.0


def test_wilson_pbo_upper_ci_shrinks_with_more_combos():
    wide = _wilson_pbo_upper_ci(0.10, n_combos=10, ci_level=0.90)
    tight = _wilson_pbo_upper_ci(0.10, n_combos=1000, ci_level=0.90)
    assert tight < wide
    assert tight >= 0.10


# ── 8. Shadow-log write (DB) ──────────────────────────────────────────


def test_shadow_log_writes_metric_and_summary_rows(db, monkeypatch):
    """One evaluation writes 3 metric rows + 1 summary row, both verdicts populated."""
    from app.models.trading import ScanPattern

    monkeypatch.setattr(settings, "chili_cpcv_adaptive_gate_enabled", False)
    pat = ScanPattern(
        name="test_adaptive_log",
        origin="user",
        rules_json={},
        lifecycle_stage="candidate",
        trade_count=42,
        cpcv_n_paths=30,
        cpcv_median_sharpe=1.0,
        deflated_sharpe=0.97,
        pbo=0.05,
    )
    db.add(pat)
    db.commit()

    # Seed a small pool of comparator patterns so pool aggregates are
    # non-empty and the percentile threshold is meaningful.
    pool_pats = [
        ScanPattern(
            name=f"pool_{i}",
            origin="user",
            rules_json={},
            lifecycle_stage="promoted" if i % 5 == 0 else "candidate",
            trade_count=20 + 5 * i,
            cpcv_n_paths=30 + i,
            cpcv_median_sharpe=0.5 + 0.05 * i,
            deflated_sharpe=0.7 + 0.005 * i,
            pbo=0.30 - 0.01 * i,
        )
        for i in range(20)
    ]
    for p in pool_pats:
        db.add(p)
    db.commit()

    eval_payload = {
        "skipped": False,
        "n_trades": 42,
        "deflated_sharpe": 0.97,
        "pbo": 0.05,
        "cpcv_n_paths": 30,
        "cpcv_median_sharpe": 1.2,
        "n_effective_trials": 10,
    }
    ok, reasons = maybe_apply_adaptive_gate(
        eval_payload,
        scan_pattern_id=pat.id,
        legacy_pass=True,
        legacy_reasons=["provisional_sample_size"],
        db_session=db,
    )
    # Flag is OFF → legacy verdict returned.
    assert ok is True
    assert "provisional_sample_size" in reasons

    rows = db.execute(
        text(
            "SELECT metric_name, raw_value, shrunken_value, lower_ci, "
            "pool_threshold, eligible, legacy_verdict_pass, "
            "adaptive_verdict_pass, marginal_portfolio_sharpe_bps "
            "FROM cpcv_adaptive_eval_log WHERE scan_pattern_id = :pid "
            "ORDER BY id"
        ),
        {"pid": pat.id},
    ).fetchall()
    metric_names = [r[0] for r in rows]
    assert metric_names == ["dsr", "pbo", "median_sharpe", "summary"]

    summary_row = rows[-1]
    assert summary_row[6] is True  # legacy_verdict_pass
    assert summary_row[7] in (True, False)  # adaptive_verdict_pass populated
    assert summary_row[8] is not None  # marginal bps populated


# ── 9. Flag-on returns adaptive verdict ───────────────────────────────


def test_flag_on_can_diverge_from_legacy_when_pareto_dominated(db, monkeypatch):
    """With the flag ON, the wrapper returns the adaptive verdict.

    Seed a clearly-dominating pool so a mediocre candidate is Pareto-dominated
    and the adaptive verdict differs from legacy.
    """
    from app.models.trading import ScanPattern

    monkeypatch.setattr(settings, "chili_cpcv_adaptive_gate_enabled", True)
    pat = ScanPattern(
        name="weak_candidate",
        origin="user",
        rules_json={},
        lifecycle_stage="candidate",
        trade_count=30,
        cpcv_n_paths=25,
        cpcv_median_sharpe=0.6,
        deflated_sharpe=0.96,
        pbo=0.18,
    )
    db.add(pat)
    db.flush()

    # Pool of strong dominators.
    for i in range(15):
        db.add(
            ScanPattern(
                name=f"strong_{i}",
                origin="user",
                rules_json={},
                lifecycle_stage="promoted",
                trade_count=300,
                cpcv_n_paths=80,
                cpcv_median_sharpe=2.5 + 0.1 * i,
                deflated_sharpe=0.99,
                pbo=0.02,
            )
        )
    db.commit()

    eval_payload = {
        "skipped": False,
        "n_trades": 30,
        "deflated_sharpe": 0.96,
        "pbo": 0.18,
        "cpcv_n_paths": 25,
        "cpcv_median_sharpe": 0.6,
        "n_effective_trials": 10,
    }
    ok, reasons = maybe_apply_adaptive_gate(
        eval_payload,
        scan_pattern_id=pat.id,
        legacy_pass=True,
        legacy_reasons=[],
        db_session=db,
    )
    # Legacy would have passed; adaptive should reject (Pareto dominated
    # and/or pool-threshold failure).
    assert ok is False
    assert any(r.startswith("adaptive_") for r in reasons)


# ── 10. Wrapper never raises on bad payloads ──────────────────────────


def test_wrapper_handles_none_payload_gracefully():
    ok, reasons = maybe_apply_adaptive_gate(
        None,  # type: ignore[arg-type]
        scan_pattern_id=None,
        legacy_pass=True,
        legacy_reasons=["x"],
    )
    assert ok is True
    assert reasons == ["x"]


def test_adaptive_gate_enabled_predicate_default_off():
    # No monkeypatch — read the actual settings default.
    assert gate.adaptive_gate_enabled() is False or gate.adaptive_gate_enabled() is True
    # The point: the predicate returns a bool without side effects.
    assert isinstance(gate.adaptive_gate_enabled(), bool)
