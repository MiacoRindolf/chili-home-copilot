"""Ross gap #3: absolute RVOL/change explosiveness FLOOR on top of the within-batch
percentile (videos 01/05/17/29/36). On a dull tape the best-of-a-dull-batch still
percentile-ranks #1; Ross's rule is that <~5x RVOL or <~10% up is simply not a setup, so
such a name is dropped from LIVE eligibility (pool membership + paper scoring untouched).

Two layers:
  * the pure ``below_explosive_floor`` helper — trips below the RVOL floor or the change
    floor, fails OPEN on missing fields (never benches on absent data),
  * the integration through ``score_viability`` — a symbol the pipeline marked below the
    floor (``ctx.meta['ross_below_floor']``) loses ``live_eligible``; a symbol not in the
    list is untouched.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT,
    ROSS_ELIGIBILITY_RVOL_FLOOR,
    below_explosive_floor,
)
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability


# ── pure below_explosive_floor ───────────────────────────────────────────────

def test_explosive_name_is_not_below_floor():
    sig = {"vol_ratio": 8.0, "daily_change_pct": 18.0}   # 8x / +18% -> a Ross setup
    assert below_explosive_floor(sig) is False


def test_low_rvol_is_below_floor():
    sig = {"vol_ratio": 2.0, "daily_change_pct": 18.0}   # only 2x RVOL
    assert below_explosive_floor(sig) is True


def test_low_change_is_below_floor():
    sig = {"vol_ratio": 8.0, "daily_change_pct": 3.0}    # only +3%
    assert below_explosive_floor(sig) is True


def test_exactly_at_floor_is_not_below():
    sig = {"vol_ratio": ROSS_ELIGIBILITY_RVOL_FLOOR, "daily_change_pct": ROSS_ELIGIBILITY_CHANGE_FLOOR_PCT}
    assert below_explosive_floor(sig) is False


def test_missing_fields_fail_open():
    assert below_explosive_floor({}) is False                       # nothing known -> not below
    assert below_explosive_floor({"vol_ratio": 8.0}) is False       # change unknown, rvol fine
    assert below_explosive_floor({"daily_change_pct": 18.0}) is False  # rvol unknown, change fine


def test_alt_schema_keys_read():
    # crypto-breakout schema uses rvol/change_24h; gap_pct is an alt momentum source.
    assert below_explosive_floor({"rvol": 2.0, "change_24h": 18.0}) is True
    assert below_explosive_floor({"volume_ratio": 8.0, "gap_pct": 18.0}) is False


# ── integration through score_viability ──────────────────────────────────────

def _calm_ctx(below=None):
    meta = {"spread_regime": "tight"}
    if below is not None:
        meta["ross_below_floor"] = below
    return build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc), atr_pct=0.018, meta=meta
    )


def _calm_feats():
    return ExecutionReadinessFeatures(
        spread_bps=4.0, slippage_estimate_bps=4.0, fee_to_target_ratio=0.08
    )


def test_symbol_below_floor_loses_live_eligibility():
    fam = get_family("vwap_reclaim_continuation")
    assert fam is not None
    base = score_viability("ABCD", fam, _calm_ctx(), _calm_feats())
    assert base.live_eligible is True  # tight + calm baseline -> live-eligible
    gated = score_viability("ABCD", fam, _calm_ctx(below=["ABCD"]), _calm_feats())
    assert gated.live_eligible is False
    assert any("explosiveness floor" in w.lower() for w in gated.warnings)


def test_symbol_not_in_floor_list_is_untouched():
    fam = get_family("vwap_reclaim_continuation")
    assert fam is not None
    vr = score_viability("ABCD", fam, _calm_ctx(below=["WXYZ"]), _calm_feats())
    assert vr.live_eligible is True   # ABCD not in the list -> unaffected
