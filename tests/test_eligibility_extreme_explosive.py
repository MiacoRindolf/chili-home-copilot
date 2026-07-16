"""LEVER 1 tests — extreme-vol / explosive live-eligibility (risk-bounded).

Covers the contract in app/services/trading/momentum_neural/extreme_explosive_eligibility.py
and its wiring into score_viability():

  * extreme-vol + explosive-floor-clear + tradable + ok-spread => live_eligible True
    (NOT gated) + risk-bounded sizing flagged (risk_bounded True, risk_mult < 1).
  * extreme-vol + NOT-explosive => still gated (live_eligible False).
  * flag-OFF => blanket-block parity (extreme-vol always gated, byte-identical).
  * worst-case qty / loss bounded by risk_mult.

Pure-function level + integration level (score_viability) both exercised.
"""

from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.context import (
    ChopExpansionRegime,
    MomentumRegimeContext,
    VolatilityRegime,
)
from app.services.trading.momentum_neural.extreme_explosive_eligibility import (
    evaluate_extreme_explosive,
)
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import MOMENTUM_STRATEGY_FAMILIES
from app.services.trading.momentum_neural.viability import score_viability

FAMILY = MOMENTUM_STRATEGY_FAMILIES[0]  # impulse_breakout


def _ctx(vol: VolatilityRegime, ross_scores: dict | None = None) -> MomentumRegimeContext:
    meta = {}
    if ross_scores is not None:
        meta["ross_scores"] = ross_scores
    return MomentumRegimeContext(
        utc_iso="2026-06-29T14:00:00+00:00",
        utc_hour=14,
        session_label="us",
        vol_regime=vol,
        chop_expansion=ChopExpansionRegime.expansion,
        spread_regime="ok",
        fee_burden_regime="ok",
        liquidity_regime="ok",
        exhaustion_cooldown="none",
        rolling_range_state="compressed",
        breakout_continuity="holding",
        meta=meta,
    )


def _ok_feats() -> ExecutionReadinessFeatures:
    # ok spread (< 12bps live floor), tradable, low fees/slip => spread gate passes.
    return ExecutionReadinessFeatures(
        spread_bps=8.0,
        slippage_estimate_bps=4.0,
        fee_to_target_ratio=0.1,
        product_tradable=True,
    )


# --------------------------------------------------------------------------
# Pure-function contract
# --------------------------------------------------------------------------

def test_pure_extreme_explosive_admitted_risk_bounded():
    d = evaluate_extreme_explosive(
        is_extreme_vol=True,
        explosive_score=0.85,
        product_tradable=True,
        ok_spread=True,
        enabled=True,
        explosive_floor=0.7,
        risk_mult=0.5,
    )
    assert d.eligible is True
    assert d.risk_bounded is True
    assert 0.0 < d.risk_mult <= 1.0
    assert d.risk_mult == 0.5


def test_pure_extreme_not_explosive_gated():
    d = evaluate_extreme_explosive(
        is_extreme_vol=True,
        explosive_score=0.4,  # below floor
        product_tradable=True,
        ok_spread=True,
        enabled=True,
        explosive_floor=0.7,
        risk_mult=0.5,
    )
    assert d.eligible is False
    assert d.reason == "below_explosive_floor"


def test_pure_extreme_missing_score_gated():
    d = evaluate_extreme_explosive(
        is_extreme_vol=True,
        explosive_score=None,
        product_tradable=True,
        ok_spread=True,
        enabled=True,
        explosive_floor=0.7,
        risk_mult=0.5,
    )
    assert d.eligible is False


def test_pure_extreme_not_tradable_gated():
    d = evaluate_extreme_explosive(
        is_extreme_vol=True,
        explosive_score=0.9,
        product_tradable=False,
        ok_spread=True,
        enabled=True,
        explosive_floor=0.7,
        risk_mult=0.5,
    )
    assert d.eligible is False
    assert d.reason == "not_tradable"


def test_pure_extreme_bad_spread_gated():
    d = evaluate_extreme_explosive(
        is_extreme_vol=True,
        explosive_score=0.9,
        product_tradable=True,
        ok_spread=False,
        enabled=True,
        explosive_floor=0.7,
        risk_mult=0.5,
    )
    assert d.eligible is False
    assert d.reason == "spread_gated"


def test_pure_flag_off_blanket_block_parity():
    d = evaluate_extreme_explosive(
        is_extreme_vol=True,
        explosive_score=0.99,
        product_tradable=True,
        ok_spread=True,
        enabled=False,  # flag OFF
        explosive_floor=0.7,
        risk_mult=0.5,
    )
    assert d.eligible is False
    assert d.risk_bounded is False
    assert d.risk_mult == 1.0
    assert d.reason == "flag_off_blanket_block"


def test_pure_risk_mult_clamped_to_unit_interval():
    # A pathological config (>1) is clamped so worst-case size never EXCEEDS full.
    d = evaluate_extreme_explosive(
        is_extreme_vol=True,
        explosive_score=0.9,
        product_tradable=True,
        ok_spread=True,
        enabled=True,
        explosive_floor=0.7,
        risk_mult=4.0,
    )
    assert d.eligible is True
    assert d.risk_mult == 1.0  # clamped


def test_worst_case_loss_bounded_by_risk_mult():
    # The size-down multiple directly bounds worst-case qty and therefore loss:
    # bounded_qty = full_qty * risk_mult, so bounded_loss <= full_loss * risk_mult.
    full_qty = 1000.0
    d = evaluate_extreme_explosive(
        is_extreme_vol=True,
        explosive_score=0.9,
        product_tradable=True,
        ok_spread=True,
        enabled=True,
        explosive_floor=0.7,
        risk_mult=0.5,
    )
    bounded_qty = full_qty * d.risk_mult
    assert bounded_qty <= full_qty
    per_share_stop_loss = 0.20
    assert bounded_qty * per_share_stop_loss <= full_qty * per_share_stop_loss * d.risk_mult + 1e-9


# --------------------------------------------------------------------------
# Integration via score_viability()
# --------------------------------------------------------------------------

@pytest.fixture
def _flag_on(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "chili_momentum_extreme_explosive_eligible_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_extreme_explosive_floor", 0.7, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_extreme_explosive_risk_mult", 0.5, raising=False)


@pytest.fixture
def _flag_off(monkeypatch):
    from app.config import settings
    monkeypatch.setattr(settings, "chili_momentum_extreme_explosive_eligible_enabled", False, raising=False)


def test_integration_extreme_explosive_live_eligible(_flag_on):
    res = score_viability(
        "UPC",
        FAMILY,
        _ctx(VolatilityRegime.extreme, ross_scores={"UPC": 0.88}),
        _ok_feats(),
    )
    assert res.live_eligible is True
    assert res.risk_bounded is True
    assert res.risk_mult < 1.0


def test_integration_extreme_not_explosive_still_gated(_flag_on):
    res = score_viability(
        "MEH",
        FAMILY,
        _ctx(VolatilityRegime.extreme, ross_scores={"MEH": 0.3}),
        _ok_feats(),
    )
    assert res.live_eligible is False
    assert res.risk_bounded is False


def test_integration_flag_off_blanket_block(_flag_off):
    res = score_viability(
        "UPC",
        FAMILY,
        _ctx(VolatilityRegime.extreme, ross_scores={"UPC": 0.99}),
        _ok_feats(),
    )
    # Flag off => blanket block parity: extreme-vol is never live-eligible.
    assert res.live_eligible is False
    assert res.risk_bounded is False
    assert res.risk_mult == 1.0


def test_integration_normal_vol_unaffected_by_lever(_flag_on):
    # A normal-vol name never consults the extreme gate => no risk-bounding.
    res = score_viability(
        "AAPL",
        FAMILY,
        _ctx(VolatilityRegime.normal, ross_scores={"AAPL": 0.9}),
        _ok_feats(),
    )
    assert res.risk_bounded is False
    assert res.risk_mult == 1.0
