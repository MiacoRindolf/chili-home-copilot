"""Unit tests for neural-mesh momentum intelligence (no learning-cycle)."""

from __future__ import annotations

from datetime import datetime, timezone

from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import MOMENTUM_STRATEGY_FAMILIES, get_family
from app.services.trading.momentum_neural.viability import score_viability


def test_regime_context_session_bucket():
    # 10 UTC falls in europe window (7–14) before US (13–21) overlap at 13+.
    ctx = build_momentum_regime_context(now=datetime(2026, 4, 7, 10, 0, tzinfo=timezone.utc))
    assert ctx.utc_hour == 10
    assert ctx.session_label == "europe"


def test_momentum_families_count():
    assert len(MOMENTUM_STRATEGY_FAMILIES) == 10
    assert get_family("impulse_breakout") is not None
    assert get_family("nope") is None


def test_viability_live_blocked_on_wide_spread():
    fam = get_family("impulse_breakout")
    assert fam is not None
    ctx = build_momentum_regime_context(
        now=datetime(2026, 4, 7, 16, 0, tzinfo=timezone.utc),
        atr_pct=0.015,
        meta={"spread_regime": "tight", "fee_burden_regime": "low"},
    )
    feats = ExecutionReadinessFeatures(spread_bps=40.0, fee_to_target_ratio=0.1)
    vr = score_viability("BTC-USD", fam, ctx, feats)
    assert vr.paper_eligible is True
    assert vr.live_eligible is False
    assert any("spread" in w.lower() for w in vr.warnings)


def test_viability_allows_live_when_tight_and_calm():
    fam = get_family("vwap_reclaim_continuation")
    assert fam is not None
    ctx = build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc),
        atr_pct=0.018,
        meta={"spread_regime": "tight"},
    )
    feats = ExecutionReadinessFeatures(spread_bps=4.0, slippage_estimate_bps=4.0, fee_to_target_ratio=0.08)
    vr = score_viability("ETH-USD", fam, ctx, feats)
    assert vr.viability >= 0.42
    assert vr.live_eligible is True
