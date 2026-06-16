"""Ross gap #6: market-wide leading-gainer tilt (videos 01/11/17/23/32/34). The day's
top-N % gainers get the broker hot-lists / eyes that make a pattern resolve — a small
additive viability boost that orders WITHIN the eligible set (#3 gates membership).
Equity-only; absent set / crypto -> no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability


def _ctx(top=None):
    meta = {"spread_regime": "tight"}
    if top is not None:
        meta["top_market_gainers"] = top
    return build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc), atr_pct=0.018, meta=meta
    )


def _feats():
    return ExecutionReadinessFeatures(
        spread_bps=4.0, slippage_estimate_bps=4.0, fee_to_target_ratio=0.08
    )


def test_top_gainer_gets_small_boost():
    fam = get_family("vwap_reclaim_continuation")
    base = score_viability("ABCD", fam, _ctx(), _feats()).viability
    boosted = score_viability("ABCD", fam, _ctx(top=["ABCD"]), _feats()).viability
    assert boosted > base
    assert abs((boosted - base) - 0.03) < 1e-9


def test_non_top_gainer_unaffected():
    fam = get_family("vwap_reclaim_continuation")
    base = score_viability("ABCD", fam, _ctx(), _feats()).viability
    other = score_viability("ABCD", fam, _ctx(top=["WXYZ"]), _feats()).viability
    assert other == base


def test_crypto_gets_no_boost():
    fam = get_family("vwap_reclaim_continuation")
    base = score_viability("BTC-USD", fam, _ctx(), _feats()).viability
    same = score_viability("BTC-USD", fam, _ctx(top=["BTC-USD"]), _feats()).viability
    assert same == base
