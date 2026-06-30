"""EXPLOSIVE-PREQUAL SCORE FLOOR — the UPC blocker fix.

The real blocker (UPC live 2026-06-29): a +500% low-float day-monster scored viability
~0.55 — just BELOW the impulse_breakout entry bar (0.56, strategy_params.py:35) — so the
score arithmetic vetoed the exact name the lane exists to trade while a generic 0.56 bar
cleared. The fix is a bar-relative RAISE-ONLY floor (viability.py, just before the
``viability = max(...)`` clamp) that lifts the score of a GENUINE Ross A-setup just OVER
the default bar and couples it to RISK-BOUNDED sizing.

ANTI-JUNK INVARIANT (the hole closed): the floor is gated by a HARDENED signed A-setup
conjunction — low-float (float-confirmed, <= ceiling) AND SIGNED up-change >= the change
floor (NOT abs, so a low-float crasher with extreme rvol fails) AND rvol ok (present >=
floor, OR fail-open only when up-change already confirmed the mover). It also requires the
SAME ``_is_genuine_explosive`` conjunction the extreme-vol relax uses (tradable + spread-ok
+ affirm-explosive + not-below-floor) and that the name is STILL live_eligible. It NEVER
lowers a score. EQUITY-only; flag-off / crypto / missing-change => byte-identical no-op.
"""
from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.services.trading.momentum_neural.context import build_momentum_regime_context
from app.services.trading.momentum_neural.features import ExecutionReadinessFeatures
from app.services.trading.momentum_neural.variants import get_family
from app.services.trading.momentum_neural.viability import score_viability

# atr_pct in [0.02, 0.045) -> high (NOT extreme): isolates the prequal-floor lever from the
# extreme-vol relax path so the test exercises the score floor itself.
_NORMAL_ATR = 0.018
# ross_score 0.2 => the Ross-quality tilt drags the un-floored base to ~0.55 (BELOW the 0.58
# floor) so the lift is observable. Empirically: OFF=0.5500, ON=0.5800.
_LOW_QUALITY = 0.2


def _fam():
    fam = get_family("vwap_reclaim_continuation")
    assert fam is not None
    return fam


def _ctx(sym: str, *, ross_score: float = _LOW_QUALITY):
    meta = {"spread_regime": "tight", "ross_scores": {sym: ross_score}}
    return build_momentum_regime_context(
        now=datetime(2026, 4, 7, 17, 0, tzinfo=timezone.utc),
        atr_pct=_NORMAL_ATR,
        meta=meta,
    )


def _feats(sym: str, sig: dict, *, spread_bps: float = 10.0, tradable: bool = True):
    meta: dict = {
        "spread_bps": spread_bps,
        "slippage_estimate_bps": 6.0,
        "fee_to_target_ratio": 0.08,
        "product_tradable": tradable,
        "ross_signals": {sym: sig},
    }
    return ExecutionReadinessFeatures.from_meta(meta)


def _enable(monkeypatch, *, floor: bool = True):
    # The prequal floor reuses the LEVER-1 _is_genuine_explosive gate, which is itself gated
    # by chili_momentum_live_eligible_allow_extreme_explosive. The A-setup quality floor is
    # left at DEFAULT-OFF (the prequal floor must stand on its own at default config).
    monkeypatch.setattr(settings, "chili_momentum_live_eligible_allow_extreme_explosive", True)
    monkeypatch.setattr(settings, "chili_momentum_a_setup_quality_floor_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_explosive_prequal_floor_enabled", floor)


# ── #1 UPC-CLASS LIFT ──────────────────────────────────────────────────────────

def test_upc_class_mover_is_lifted_over_the_bar(monkeypatch):
    """A +500% low-float mover with NO rvol datum (the UPC shape) scores ~0.55 un-floored
    -> the floor lifts it to >= 0.58, keeps it LIVE, and couples it to risk-bounded sizing."""
    _enable(monkeypatch, floor=True)
    sig = {"float_shares": 563_000.0, "daily_change_pct": 500.0}  # rvol absent
    vr = score_viability("UPC", _fam(), _ctx("UPC"), _feats("UPC", sig))
    assert vr.viability >= 0.58
    assert vr.live_eligible is True
    assert vr.extreme_vol_risk_bounded is True


# ── #2 JUNK-REJECTION (the critical test — the hole closed) ──────────────────────

def test_junk_float_only_not_floored(monkeypatch):
    """(a) float only, NO rvol, NO change => the SIGNED up-change leg fails closed => the
    floor is a strict no-op: viability == the un-floored base (NOT raised)."""
    sig = {"float_shares": 5_000_000.0}
    _enable(monkeypatch, floor=False)
    off = score_viability("JUNKA", _fam(), _ctx("JUNKA"), _feats("JUNKA", sig))
    _enable(monkeypatch, floor=True)
    on = score_viability("JUNKA", _fam(), _ctx("JUNKA"), _feats("JUNKA", sig))
    assert on.viability == off.viability  # NOT raised
    assert on.extreme_vol_risk_bounded is False


def test_low_float_extreme_rvol_crasher_not_floored(monkeypatch):
    """(b) low-float + extreme rvol but a NEGATIVE day-change (a CRASHER) => the SIGNED
    _up_ok fails (NOT abs) => NOT floored. Closes the hole a magnitude-only floor would
    leave (a high-rvol dump must never be lifted)."""
    sig = {"float_shares": 1_000_000.0, "vol_ratio": 70.0, "daily_change_pct": -30.0}
    _enable(monkeypatch, floor=False)
    off = score_viability("CRASH", _fam(), _ctx("CRASH"), _feats("CRASH", sig))
    _enable(monkeypatch, floor=True)
    on = score_viability("CRASH", _fam(), _ctx("CRASH"), _feats("CRASH", sig))
    assert on.viability == off.viability  # NOT raised
    assert on.extreme_vol_risk_bounded is False


# ── #3 AFFIRMATIVELY-LOW RVOL ────────────────────────────────────────────────────

def test_affirmatively_low_rvol_not_floored(monkeypatch):
    """A PRESENT, affirmatively-low rvol (1.2 < the 3.0 explosive floor) is a real
    non-mover => the _rvol_ok leg fails => NOT floored."""
    sig = {"float_shares": 5_000_000.0, "vol_ratio": 1.2, "daily_change_pct": 12.0}
    _enable(monkeypatch, floor=False)
    off = score_viability("LOWR", _fam(), _ctx("LOWR"), _feats("LOWR", sig))
    _enable(monkeypatch, floor=True)
    on = score_viability("LOWR", _fam(), _ctx("LOWR"), _feats("LOWR", sig))
    assert on.viability == off.viability
    assert on.extreme_vol_risk_bounded is False


# ── #4 OVERSIZE FLOAT (AREC) ─────────────────────────────────────────────────────

def test_oversize_float_not_floored(monkeypatch):
    """AREC-class 107M float fails the low-float leg (> the 20M ceiling) => NOT floored,
    even with a real rvol + change."""
    sig = {"float_shares": 107_000_000.0, "vol_ratio": 6.0, "daily_change_pct": 15.0}
    _enable(monkeypatch, floor=False)
    off = score_viability("AREC", _fam(), _ctx("AREC"), _feats("AREC", sig))
    _enable(monkeypatch, floor=True)
    on = score_viability("AREC", _fam(), _ctx("AREC"), _feats("AREC", sig))
    assert on.viability == off.viability
    assert on.extreme_vol_risk_bounded is False


# ── #5 ALREADY-ABOVE (raise-only no-op) ──────────────────────────────────────────

def test_already_above_bar_unchanged(monkeypatch):
    """A genuine A-setup whose base is ALREADY >= the floor (high Ross quality) is
    UNCHANGED — the floor is raise-only (max() no-op), it never lifts a name twice and
    never marks an already-strong score risk-bounded."""
    sig = {"float_shares": 563_000.0, "daily_change_pct": 500.0}
    _enable(monkeypatch, floor=False)
    off = score_viability("UPC", _fam(), _ctx("UPC", ross_score=0.9), _feats("UPC", sig))
    assert off.viability >= 0.58  # already above the floor un-floored
    _enable(monkeypatch, floor=True)
    on = score_viability("UPC", _fam(), _ctx("UPC", ross_score=0.9), _feats("UPC", sig))
    assert on.viability == off.viability  # max() no-op
    assert on.extreme_vol_risk_bounded is False


# ── #6 FLAG-OFF BYTE-IDENTICAL ───────────────────────────────────────────────────

def test_flag_off_byte_identical_for_upc(monkeypatch):
    """enabled=False => viability == the un-floored base for the UPC input (byte-identical;
    no dark behavior when the kill-switch is thrown)."""
    sig = {"float_shares": 563_000.0, "daily_change_pct": 500.0}
    _enable(monkeypatch, floor=False)
    off = score_viability("UPC", _fam(), _ctx("UPC"), _feats("UPC", sig))
    assert off.viability < 0.58  # confirms the floor would otherwise have lifted it
    assert off.extreme_vol_risk_bounded is False


# ── #7 LIVE-INELIGIBLE NOT LIFTED ────────────────────────────────────────────────

def test_live_ineligible_not_lifted(monkeypatch):
    """A name an upstream HARD gate already rejected (product not tradable => live_eligible
    False AND _is_genuine_explosive False) is NEVER lifted by the floor."""
    sig = {"float_shares": 563_000.0, "daily_change_pct": 500.0}
    _enable(monkeypatch, floor=True)
    vr = score_viability("UPC", _fam(), _ctx("UPC"), _feats("UPC", sig, tradable=False))
    assert vr.live_eligible is False
    assert vr.extreme_vol_risk_bounded is False
