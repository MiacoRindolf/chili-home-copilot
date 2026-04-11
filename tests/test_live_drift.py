"""Phase 3 live drift: deterministic scoring, skip contracts, idempotent confidence."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.trading.live_drift import (
    apply_live_drift_to_pattern,
    baseline_degenerate,
    binomial_two_sided_p_value,
    build_skip_contract,
    compute_live_drift_contract,
    compute_live_drift_v2_contract,
    live_drift_summary,
    live_drift_v2_summary,
    select_primary_runtime,
)


def _settings(**kwargs):
    base = dict(
        brain_live_drift_window_days=120,
        brain_live_drift_live_min_primary=8,
        brain_live_drift_min_trades=8,
        brain_live_drift_baseline_p0_low=0.05,
        brain_live_drift_baseline_p0_high=0.95,
        brain_live_drift_warning_delta_pp=8.0,
        brain_live_drift_critical_delta_pp=18.0,
        brain_live_drift_strong_p_like=0.02,
        brain_live_drift_confidence_nudge_enabled=True,
        brain_live_drift_confidence_mult_healthy=1.0,
        brain_live_drift_confidence_mult_warning=0.94,
        brain_live_drift_confidence_mult_critical=0.88,
        brain_live_drift_confidence_floor=0.1,
        brain_live_drift_confidence_cap=0.95,
        brain_live_drift_auto_challenged_enabled=False,
        brain_live_drift_auto_challenged_max_p_like=0.02,
    )
    base.update(kwargs)
    return SimpleNamespace(**base)


def test_binomial_two_sided_symmetric():
    p = binomial_two_sided_p_value(20, 10, 0.5)
    assert p is not None and p <= 1.0 and p > 0.2


def test_baseline_degenerate_edges():
    assert baseline_degenerate(2.0, low=0.05, high=0.95)[0] is True
    assert baseline_degenerate(99.0, low=0.05, high=0.95)[0] is True
    assert baseline_degenerate(55.0, low=0.05, high=0.95)[0] is False


def test_select_primary_live_vs_paper():
    src, n, w, mix, sk = select_primary_runtime(
        n_live=10,
        n_paper=3,
        wins_live=6,
        wins_paper=2,
        live_min_primary=8,
        min_trades=8,
    )
    assert src == "live" and n == 10 and w == 6 and mix is False and sk is None


def test_select_primary_paper_with_sparse_live_suppresses_context():
    src, n, w, mix, sk = select_primary_runtime(
        n_live=3,
        n_paper=10,
        wins_live=1,
        wins_paper=4,
        live_min_primary=8,
        min_trades=8,
    )
    assert src == "paper" and mix is True and sk is None


def test_insufficient_sample_skip_contract_fresh():
    prev = {
        "drift_tier": "critical",
        "drift_delta": -40.0,
        "drift_p_like": 0.001,
        "confidence_reference": 0.75,
    }
    sk = build_skip_contract(
        skip_reason="insufficient_runtime_sample",
        prev_live_drift=prev,
        evaluation_window_days=120,
    )
    assert sk["drift_tier"] == "n/a"
    assert sk["drift_delta"] is None
    assert sk["drift_p_like"] is None
    assert sk["skip_reason"] == "insufficient_runtime_sample"
    assert sk["confidence_reference"] == 0.75


def test_mixed_runtime_suppresses_p_like():
    pattern = SimpleNamespace(oos_win_rate=0.55, confidence=0.72)
    rt = {"n_live": 3, "wins_live": 1, "n_paper": 12, "wins_paper": 5}
    c = compute_live_drift_contract(
        pattern=pattern,
        oos_val={},
        runtime=rt,
        prev_live_drift=None,
        settings=_settings(),
    )
    assert c.get("skip_reason") is None
    assert c["primary_runtime_source"] == "paper"
    assert c["runtime_mixed_context"] is True
    assert c["p_like_suppressed"] is True
    assert c["drift_p_like"] is None


def test_degenerate_baseline_skips_p_like():
    pattern = SimpleNamespace(oos_win_rate=0.98, confidence=0.7)
    rt = {"n_live": 20, "wins_live": 12, "n_paper": 0, "wins_paper": 0}
    c = compute_live_drift_contract(
        pattern=pattern,
        oos_val={},
        runtime=rt,
        prev_live_drift=None,
        settings=_settings(),
    )
    assert c.get("degenerate_baseline") is True
    assert c["drift_p_like"] is None


def test_confidence_nudge_idempotent():
    pattern = SimpleNamespace(
        confidence=0.5,
        oos_validation_json={},
        lifecycle_stage="promoted",
        id=1,
        name="t",
        active=True,
        promotion_status="promoted",
    )
    contract = {
        "drift_version": 1,
        "drift_tier": "critical",
        "skip_reason": None,
        "confidence_reference": 0.8,
        "sample_count": 10,
        "primary_runtime_source": "live",
        "p_like_suppressed": False,
        "degenerate_baseline": False,
    }
    db = MagicMock()
    st = _settings()
    apply_live_drift_to_pattern(db, pattern, contract, st)
    once = float(pattern.confidence)
    apply_live_drift_to_pattern(db, pattern, contract, st)
    twice = float(pattern.confidence)
    assert once == pytest.approx(twice, rel=0, abs=1e-6)
    assert once == pytest.approx(0.8 * 0.88, rel=0, abs=1e-6)


def test_live_drift_summary_shape():
    s = live_drift_summary({"drift_tier": "warning", "drift_delta": -9.0, "sample_count": 12})
    assert s["drift_tier"] == "warning"
    assert s["drift_delta"] == -9.0
    assert s["sample_count"] == 12


def test_v2_flags_expectancy_collapse_even_with_stable_win_rate():
    pattern = SimpleNamespace(oos_win_rate=0.55, oos_avg_return_pct=2.0)
    scorecards = {
        "live": {
            "source": "live",
            "sample_count": 12,
            "win_rate_pct": 55.0,
            "expectancy_per_trade_pct": 0.2,
            "avg_winner_pct": 1.1,
            "avg_loser_pct": -1.4,
            "profit_factor": 0.72,
            "p25_trade_outcome_pct": -1.4,
            "slippage_burden_bps": 52.0,
            "freshness_at": "2026-04-10T12:00:00",
        },
        "paper": None,
        "n_live": 12,
        "n_paper": 0,
    }
    c = compute_live_drift_v2_contract(pattern=pattern, oos_val={}, scorecards=scorecards, settings=_settings())
    assert c["skip_reason"] is None
    assert c["composite_tier"] == "critical"
    assert "expectancy_critical" in c["composite_flags"]
    assert "profit_factor_critical" in c["composite_flags"]
    assert "slippage_burden_critical" in c["composite_flags"]
    assert live_drift_v2_summary(c)["primary_runtime_source"] == "live"


def test_v2_falls_back_to_paper_when_live_is_sparse():
    pattern = SimpleNamespace(oos_win_rate=0.6, oos_avg_return_pct=1.5)
    scorecards = {
        "live": {
            "source": "live",
            "sample_count": 3,
            "win_rate_pct": 66.0,
            "expectancy_per_trade_pct": 1.0,
            "avg_winner_pct": 2.0,
            "avg_loser_pct": -1.0,
            "profit_factor": 1.8,
            "p25_trade_outcome_pct": -0.2,
            "slippage_burden_bps": 10.0,
            "freshness_at": "2026-04-10T12:00:00",
        },
        "paper": {
            "source": "paper",
            "sample_count": 10,
            "win_rate_pct": 50.0,
            "expectancy_per_trade_pct": 0.8,
            "avg_winner_pct": 1.5,
            "avg_loser_pct": -0.9,
            "profit_factor": 1.2,
            "p25_trade_outcome_pct": -0.4,
            "slippage_burden_bps": None,
            "freshness_at": "2026-04-10T12:00:00",
        },
        "n_live": 3,
        "n_paper": 10,
    }
    c = compute_live_drift_v2_contract(pattern=pattern, oos_val={}, scorecards=scorecards, settings=_settings())
    assert c["primary_runtime_source"] == "paper"
    assert c["fallback_used"] is True
