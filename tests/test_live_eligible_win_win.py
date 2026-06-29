"""LEVER 1 — LIVE-ELIGIBILITY WIN-WIN (chili_momentum_live_eligible_allow_extreme_explosive).

The day-monster (UPC +476%, Ross +$35k) scored CHILI's #1 but live_eligible=FALSE blocked
it -> $0. Two leaks blanket-blocked a GENUINE explosive Ross-class mover from LIVE while it
is exactly what the lane exists to trade:

  (a) the vol_regime==extreme blanket block, and
  (b) the A-setup quality floor failing CLOSED when the rvol DATUM is merely MISSING (None)
      rather than affirmatively low (UPC live: float=563K low-float OK, change=+10% OK, but
      rvol=None -> 1,520 rejects 2026-06-29).

The fix keeps a name that clears the lane's EXISTING explosiveness floor
(below_explosive_floor) AND is product_tradable AND has a spread within the live ceiling
LIVE-eligible on extreme-vol / missing-rvol ALONE, marking it for RISK-BOUNDED sizing. The
WIN-WIN INVARIANT: a name that is NOT a genuine mover (affirmatively below the floor,
untradeable, toxic spread, or unconfirmed float) is STILL gated.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from app.config import settings
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability

# atr_pct >= 0.045 -> VolatilityRegime.extreme (context.py).
_EXTREME_ATR = 0.09
# atr_pct in [0.02, 0.045) -> high (NOT extreme); used for the A-setup-floor missing-rvol case.
_NORMAL_ATR = 0.018


def _fam():
    fam = get_family("vwap_reclaim_continuation")
    assert fam is not None
    return fam


def _ctx(atr_pct: float, *, ross_score: float = 0.9):
    # ross_scores mirrors production: a genuine #1 mover carries a high Ross-quality
    # percentile (UPC scored 0.755 live), which boosts viability above the 0.42 floor.
    meta = {"spread_regime": "tight"}
    if ross_score is not None:
        meta["ross_scores"] = {"UPC": ross_score, "DULL": ross_score, "LOWV": ross_score,
                               "NOFLT": ross_score, "AREC": ross_score,
                               "JUNK": ross_score, "BIGF": ross_score}
    return build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc),
        atr_pct=atr_pct,
        meta=meta,
    )


def _feats(*, ross_signals: dict | None = None, spread_bps: float = 10.0, tradable: bool = True):
    """Tradeable spread (10bps, well under the 300bps live ceiling); ross_signals carries the
    per-symbol scanner pillars the floors read. (The wide-spread derate is covered separately;
    here we keep the score above the 0.42 quality floor so the ELIGIBILITY logic is isolated —
    a genuine #1 mover scored 0.755 live.)"""
    meta: dict = {
        "spread_bps": spread_bps,
        "slippage_estimate_bps": 6.0,
        "fee_to_target_ratio": 0.08,
        "product_tradable": tradable,
    }
    if ross_signals is not None:
        meta["ross_signals"] = ross_signals
    return ExecutionReadinessFeatures.from_meta(meta)


# A GENUINE explosive Ross-class mover (UPC-shape): low float, big change. The two cases
# differ only in whether the rvol datum is present.
def _upc_signal_missing_rvol():
    return {"float_shares": 563_338.0, "daily_change_pct": 18.0}  # rvol absent


def _upc_signal_with_rvol():
    return {"float_shares": 563_338.0, "daily_change_pct": 18.0, "vol_ratio": 8.0}


# ── (a) EXTREME-VOL relax ─────────────────────────────────────────────────────

def test_extreme_vol_genuine_mover_stays_live_and_is_risk_bounded(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    vr = score_viability(
        "UPC", _fam(), _ctx(_EXTREME_ATR), _feats(ross_signals={"UPC": _upc_signal_with_rvol()})
    )
    assert vr.live_eligible is True
    assert vr.extreme_vol_risk_bounded is True
    assert any("risk-bounded" in w.lower() for w in vr.warnings)


def test_extreme_vol_NON_mover_still_blocked(monkeypatch):
    """WIN-WIN INVARIANT: extreme-vol but AFFIRMATIVELY below the floor (low rvol) -> gated."""
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    sig = {"float_shares": 563_338.0, "daily_change_pct": 18.0, "vol_ratio": 2.0}  # 2x rvol
    vr = score_viability("DULL", _fam(), _ctx(_EXTREME_ATR), _feats(ross_signals={"DULL": sig}))
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_extreme_vol_untradeable_still_blocked(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    vr = score_viability(
        "UPC", _fam(), _ctx(_EXTREME_ATR),
        _feats(ross_signals={"UPC": _upc_signal_with_rvol()}, tradable=False),
    )
    assert vr.live_eligible is False


def test_extreme_vol_toxic_spread_still_blocked(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    # 400bps > the 300bps live ceiling -> toxic/broken quote -> never a win-win mover.
    vr = score_viability(
        "UPC", _fam(), _ctx(_EXTREME_ATR),
        _feats(ross_signals={"UPC": _upc_signal_with_rvol()}, spread_bps=400.0),
    )
    assert vr.live_eligible is False


def test_extreme_vol_junk_signal_blocked_with_a_setup_floor_OFF(monkeypatch):
    """WIN-WIN INVARIANT HOLE (regression): on the extreme-vol relax path, a non-empty
    but JUNK signal (no float, no rvol, no change) must NOT be admitted LIVE — even at
    DEFAULT config where the A-setup quality floor is OFF. below_explosive_floor() fails
    OPEN on absent rvol/change and never checks float, so without an AFFIRMATIVE
    explosiveness datum {'price': 1.0} previously cleared every gate -> live_eligible=True.
    The path-(a) affirmative-explosiveness confirmation closes this without depending on
    the default-OFF A-setup flag."""
    monkeypatch.setattr(settings, "chili_momentum_a_setup_quality_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    vr = score_viability(
        "JUNK", _fam(), _ctx(_EXTREME_ATR), _feats(ross_signals={"JUNK": {"price": 1.0}})
    )
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_extreme_vol_large_float_only_junk_blocked_with_a_setup_floor_OFF(monkeypatch):
    """WIN-WIN INVARIANT: a present-but-HIGH float with no rvol/change is not affirmatively
    explosive (large float fails the low-float leg; absent rvol/change fail open) -> still
    gated on the extreme-vol path at DEFAULT config (A-setup floor OFF)."""
    monkeypatch.setattr(settings, "chili_momentum_a_setup_quality_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    sig = {"float_shares": 107_000_000.0}  # high float only, no rvol/change
    vr = score_viability("BIGF", _fam(), _ctx(_EXTREME_ATR), _feats(ross_signals={"BIGF": sig}))
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_extreme_vol_blanket_block_when_flag_off(monkeypatch):
    """Flag OFF => the prior extreme-vol blanket block is byte-identical (genuine or not)."""
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", False)
    vr = score_viability(
        "UPC", _fam(), _ctx(_EXTREME_ATR), _feats(ross_signals={"UPC": _upc_signal_with_rvol()})
    )
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


# ── (b) A-SETUP FLOOR missing-rvol fail-OPEN for a genuine mover ───────────────

def test_a_setup_floor_missing_rvol_genuine_mover_admitted(monkeypatch):
    """UPC live case: A-setup floor ON, rvol datum MISSING but low-float + change OK ->
    admitted LIVE risk-bounded (instead of the fail-closed 'rvol none' reject)."""
    monkeypatch.setattr(settings, "chili_momentum_a_setup_quality_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    vr = score_viability(
        "UPC", _fam(), _ctx(_NORMAL_ATR),
        _feats(ross_signals={"UPC": _upc_signal_missing_rvol()}),
    )
    assert vr.live_eligible is True
    assert vr.extreme_vol_risk_bounded is True


def test_a_setup_floor_missing_rvol_fail_closed_when_flag_off(monkeypatch):
    """Flag OFF => the A-setup floor's prior fail-CLOSED-on-None-rvol is byte-identical."""
    monkeypatch.setattr(settings, "chili_momentum_a_setup_quality_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", False)
    vr = score_viability(
        "UPC", _fam(), _ctx(_NORMAL_ATR),
        _feats(ross_signals={"UPC": _upc_signal_missing_rvol()}),
    )
    assert vr.live_eligible is False
    assert any("a-setup quality floor" in w.lower() for w in vr.warnings)


def test_a_setup_floor_affirmatively_low_rvol_still_rejected(monkeypatch):
    """WIN-WIN INVARIANT: a PRESENT, affirmatively-low rvol is a real non-mover -> rejected
    even with the win-win flag ON."""
    monkeypatch.setattr(settings, "chili_momentum_a_setup_quality_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    sig = {"float_shares": 563_338.0, "daily_change_pct": 18.0, "vol_ratio": 1.0}  # 1x rvol
    vr = score_viability("LOWV", _fam(), _ctx(_NORMAL_ATR), _feats(ross_signals={"LOWV": sig}))
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_a_setup_floor_missing_float_still_rejected(monkeypatch):
    """WIN-WIN INVARIANT: float unconfirmed -> not a genuine low-float mover -> rejected."""
    monkeypatch.setattr(settings, "chili_momentum_a_setup_quality_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    sig = {"daily_change_pct": 18.0}  # no float, no rvol
    vr = score_viability("NOFLT", _fam(), _ctx(_NORMAL_ATR), _feats(ross_signals={"NOFLT": sig}))
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


def test_a_setup_floor_large_float_still_rejected(monkeypatch):
    """WIN-WIN INVARIANT: AREC-class large float (107M) with missing rvol -> still rejected
    (the low-float discriminator fires before the rvol leg)."""
    monkeypatch.setattr(settings, "chili_momentum_a_setup_quality_floor_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    sig = {"float_shares": 107_000_000.0, "daily_change_pct": 18.0}
    vr = score_viability("AREC", _fam(), _ctx(_NORMAL_ATR), _feats(ross_signals={"AREC": sig}))
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False


# ── public-dict / parity surface ──────────────────────────────────────────────

def test_public_dict_carries_marker(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    vr = score_viability(
        "UPC", _fam(), _ctx(_EXTREME_ATR), _feats(ross_signals={"UPC": _upc_signal_with_rvol()})
    )
    d = vr.to_public_dict()
    assert d["extreme_vol_risk_bounded"] is True
    assert d["live_eligible"] is True


def test_normal_vol_clean_mover_not_risk_bounded(monkeypatch):
    """A normal-vol, fully-confirmed mover is NOT flagged risk-bounded (no needless size-down)."""
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    vr = score_viability(
        "UPC", _fam(), _ctx(_NORMAL_ATR), _feats(ross_signals={"UPC": _upc_signal_with_rvol()})
    )
    assert vr.live_eligible is True
    assert vr.extreme_vol_risk_bounded is False
