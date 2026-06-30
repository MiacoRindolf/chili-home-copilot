"""THIN/TOXIC-SPREAD SQUEEZE CARVE-OUT (spec #2, 2026-06-29).

Convert the BINARY 300bps live-eligibility decline into a bounded EM/squeeze-percentile
SIZE-DOWN admission for ONLY top within-batch squeeze-percentile high-RVOL movers. The
marketable-LIMIT entry + notional guard + risk-first sizing already bound the toxic-fill
downside the zero-fills fix solved; ordinary names keep the flat decline.

score_viability is pure (db=None) — these feed crafted ExecutionReadinessFeatures with a
wide spread + ross_signals[SYM] and assert ViabilityResult flags. NO DB.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability

# NON-extreme regime so we isolate the SPREAD carve-out (extreme-vol is a separate path,
# and its _spread_ok leg already rejects a >300bps spread, so it can't admit a thin name).
_NORMAL_ATR = 0.018


def _fam():
    fam = get_family("vwap_reclaim_continuation")
    assert fam is not None
    return fam


def _ctx(*, ross_score: float = 0.9):
    meta = {"spread_regime": "tight"}
    meta["ross_scores"] = {
        sym: ross_score for sym in ("SQZ", "ORD", "MIDSQ", "BROKEN", "CRYP-USD")
    }
    return build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc),
        atr_pct=_NORMAL_ATR,
        meta=meta,
    )


def _feats(*, ross_signals: dict, spread_bps: float, tradable: bool = True):
    meta: dict = {
        "spread_bps": spread_bps,
        "slippage_estimate_bps": 6.0,
        "fee_to_target_ratio": 0.08,
        "product_tradable": tradable,
    }
    meta["ross_signals"] = ross_signals
    return ExecutionReadinessFeatures.from_meta(meta)


# A genuine TOP-squeeze high-RVOL low-float mover: clears the explosiveness floor (low
# float + change), high rvol, top within-batch squeeze percentile.
def _top_squeeze_signal(sq_rank: float = 0.95, rvol: float = 8.0):
    return {
        "float_shares": 9_000_000.0,
        "daily_change_pct": 30.0,
        "vol_ratio": rvol,
        "squeeze_fuel_rank_pct": sq_rank,
    }


# An ORDINARY name: no squeeze rank, low rvol -> never carved out.
def _ordinary_signal():
    return {"float_shares": 9_000_000.0, "daily_change_pct": 12.0, "vol_ratio": 2.0}


@pytest.fixture(autouse=True)
def _enable_lane(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_thin_spread_squeeze_lane_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_thin_spread_squeeze_top_pctl", 0.80)
    monkeypatch.setattr(settings, "chili_momentum_thin_spread_ceiling_squeeze_slope", 1.0)
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_max_spread_bps", 300.0)
    yield


def test_ordinary_name_wide_spread_still_declined():
    """Case 1: an ordinary name (no squeeze rank, low rvol) at 460bps -> live_eligible False,
    extreme_vol_risk_bounded False. Zero-fills protection intact / byte-identical."""
    vr = score_viability(
        "ORD", _fam(), _ctx(), _feats(ross_signals={"ORD": _ordinary_signal()}, spread_bps=460.0)
    )
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_top_squeeze_high_rvol_carveout_fires():
    """Case 2: a top-squeeze high-RVOL mover (sq_rank 0.95, rvol 8, +30%, float<10M) at 460bps
    with base ceiling 300 + slope 1.0 -> live_eligible True AND risk-bounded (carve-out)."""
    vr = score_viability(
        "SQZ", _fam(), _ctx(), _feats(ross_signals={"SQZ": _top_squeeze_signal()}, spread_bps=460.0)
    )
    assert vr.live_eligible is True
    assert vr.extreme_vol_risk_bounded is True
    assert any("squeeze carve-out" in w.lower() for w in vr.warnings)


def test_top_squeeze_above_squeeze_ceiling_declined():
    """Case 3: the same top-squeeze name at 1400bps is above its squeeze-scaled ceiling
    (sq_rank 0.95 -> ceiling 300*(1+0.75)=525bps) -> declined (broken/halted quote)."""
    vr = score_viability(
        "SQZ", _fam(), _ctx(), _feats(ross_signals={"SQZ": _top_squeeze_signal()}, spread_bps=1400.0)
    )
    assert vr.live_eligible is False


def test_carveout_byte_identical_when_flag_off(monkeypatch):
    """Flag OFF -> binary decline, byte-identical (a top-squeeze name is still declined)."""
    monkeypatch.setattr(settings, "chili_momentum_thin_spread_squeeze_lane_enabled", False)
    vr = score_viability(
        "SQZ", _fam(), _ctx(), _feats(ross_signals={"SQZ": _top_squeeze_signal()}, spread_bps=460.0)
    )
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_squeeze_rank_below_top_pctl_declined():
    """A high-RVOL name BELOW the top squeeze percentile (sq_rank 0.5 < 0.80) is NOT carved
    out -> declined. The squeeze percentile gate is load-bearing."""
    sig = _top_squeeze_signal(sq_rank=0.50)
    vr = score_viability(
        "MIDSQ", _fam(), _ctx(), _feats(ross_signals={"MIDSQ": sig}, spread_bps=460.0)
    )
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_top_squeeze_low_rvol_declined():
    """Top squeeze rank but rvol BELOW the explosive floor -> the triple gate fails -> declined."""
    sig = _top_squeeze_signal(sq_rank=0.95, rvol=1.0)
    vr = score_viability(
        "MIDSQ", _fam(), _ctx(), _feats(ross_signals={"MIDSQ": sig}, spread_bps=460.0)
    )
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_crypto_name_never_carved_out():
    """Equity-only: a -USD name is never admitted via the carve-out (different spread semantics)."""
    sig = _top_squeeze_signal()
    vr = score_viability(
        "CRYP-USD", _fam(), _ctx(), _feats(ross_signals={"CRYP-USD": sig}, spread_bps=460.0)
    )
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_narrow_spread_top_squeeze_unaffected():
    """A top-squeeze name with a NARROW spread (under the ceiling) never enters the carve-out
    block at all -> live_eligible True, NOT marked risk-bounded (no needless size-down)."""
    vr = score_viability(
        "SQZ", _fam(), _ctx(), _feats(ross_signals={"SQZ": _top_squeeze_signal()}, spread_bps=40.0)
    )
    assert vr.live_eligible is True
    assert vr.extreme_vol_risk_bounded is False
