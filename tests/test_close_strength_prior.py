"""Re-analysis survivor S1: cross-day close-strength prior (video 43). A stock that closed
NEAR its high-of-day (and green) into the power hour gap-continues the next day — warm the
lane on it early (CHILI's proven "right name early" bottleneck). Additive premarket
selection tilt; the only NEW selection signal the 32-video re-analysis surfaced.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.trading.momentum_neural import catalyst
from app.services.trading.momentum_neural.catalyst import (
    CLOSE_STRENGTH_PRIOR_WEIGHT,
    _close_strength_score,
    close_strength_priors,
    close_strength_viability_delta,
)
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability


# ── pure _close_strength_score ───────────────────────────────────────────────

def test_strong_green_close_near_hod():
    assert _close_strength_score(10.0, 12.0, 9.0, 11.8) > 0.9   # closed near HOD, green


def test_weak_red_close_near_lod():
    assert _close_strength_score(12.0, 12.0, 9.0, 9.2) < 0.15    # closed near LOD, red


def test_mid_close():
    s = _close_strength_score(10.0, 12.0, 10.0, 11.0)            # mid-range, green
    assert 0.45 < s < 0.85


def test_zero_range_is_neutral():
    assert _close_strength_score(10.0, 10.0, 10.0, 10.0) == 0.5


# ── close_strength_viability_delta ───────────────────────────────────────────

def test_delta_boosts_strong_discounts_weak():
    assert close_strength_viability_delta("ABCD", {"ABCD": 1.0}) == CLOSE_STRENGTH_PRIOR_WEIGHT * 0.5
    assert close_strength_viability_delta("ABCD", {"ABCD": 0.0}) == CLOSE_STRENGTH_PRIOR_WEIGHT * -0.5
    assert close_strength_viability_delta("ABCD", {"WXYZ": 1.0}) == 0.0   # not in map
    assert close_strength_viability_delta("ABCD", None) == 0.0
    assert close_strength_viability_delta("BTC-USD", {"BTC-USD": 1.0}) == 0.0


# ── close_strength_priors batch (bounded, drops neutral) ─────────────────────

def test_priors_bounded_and_drops_neutral(monkeypatch):
    vals = {"AAA": 0.9, "BBB": 0.5, "CCC": 0.2}   # BBB is neutral -> dropped
    monkeypatch.setattr(catalyst, "close_strength_prior", lambda s: vals.get(s, 0.5))
    out = close_strength_priors(["AAA", "BBB", "CCC", "DOGE-USD"])
    assert out == {"AAA": 0.9, "CCC": 0.2}   # neutral BBB + crypto dropped


def test_priors_lookup_cap(monkeypatch):
    monkeypatch.setattr(catalyst, "close_strength_prior", lambda s: 0.9)
    out = close_strength_priors([f"T{i}" for i in range(60)], max_lookups=10)
    assert len(out) == 10


# ── viability integration ────────────────────────────────────────────────────

def _ctx(csp=None):
    meta = {"spread_regime": "tight"}
    if csp is not None:
        meta["close_strength_priors"] = csp
    return build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc), atr_pct=0.018, meta=meta
    )


def _feats():
    return ExecutionReadinessFeatures(spread_bps=4.0, slippage_estimate_bps=4.0, fee_to_target_ratio=0.08)


def test_strong_close_lifts_viability():
    fam = get_family("vwap_reclaim_continuation")
    base = score_viability("ABCD", fam, _ctx(), _feats()).viability
    lifted = score_viability("ABCD", fam, _ctx(csp={"ABCD": 1.0}), _feats()).viability
    assert lifted > base
