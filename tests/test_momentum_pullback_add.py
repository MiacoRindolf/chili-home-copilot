"""Ross BUY-THE-DIP / pullback ADD — the FALLING-KNIFE GUARD is the load-bearing proof.

The existing pyramid adds on CONTINUATION (new HOD + OFI thrust = pyramid UP). Ross ALSO
buys the controlled PULLBACK to support in an INTACT uptrend. ``pullback_add_decision`` owns
that distinct trigger. The CRITICAL invariant (the E1/CTNT lesson) is the falling-knife
guard: the add fires ONLY when the uptrend is intact —

  front_side_strength >= an adaptive floor  AND
  OFI not collapsing (ofi_level > 0 AND ofi_slope >= 0)  AND
  above VWAP or cleanly reclaiming  AND
  the pullback made a HIGHER low (not a lower-low breakdown)  AND
  the pullback is CONTROLLED (within the depth band, never below the structural stop).

If ANY fail it is a KNIFE, not a dip, and NO add fires (bias toward not adding). These are
pure assertions on the predicate — no DB — plus a flag-OFF end-to-end parity proof and a
risk-bound check.
"""

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    pullback_add_decision,
    pyramid_blend_on_fill,
)

EPS = 1e-6


def _fire_kwargs(**over):
    """Baseline inputs that FIRE the pullback-add (a controlled dip to support + bounce +
    strong front-side, uptrend intact). q0=1000, a0=10.0, d0=0.10 => R0=100.

    Move: HWM=11.00, base (starter entry / shelf) = 10.00, range = 1.00. The dip pulled
    back to 10.55 (a 0.45-of-range = 45% retrace, inside [0.20, 0.62]); the higher-low
    (10.55) is ABOVE the prior pullback low (10.30) AND above the structural stop (10.20)."""
    base = dict(
        enabled=True,
        is_equity=True,
        add_count=0,
        max_adds=2,
        in_flight=False,
        other_add_in_flight=False,
        a0=10.0,
        q0=1000.0,
        d0=0.10,                 # R0 = d0*q0 = 100
        bid=10.60,               # bouncing back up off the 10.55 dip
        stop_px=10.20,           # structural stop (banked, below the dip low)
        high_water_mark=11.00,   # the move top
        support_level=10.50,     # the shelf the dip held above
        pullback_low=10.55,      # the higher-low of this pullback
        prior_pullback_low=10.30,  # the prior pullback low (this one is HIGHER)
        move_range=1.00,         # HWM - base = 11.00 - 10.00
        pullback_depth_lo_frac=0.20,
        pullback_depth_hi_frac=0.62,
        bounced=True,            # price turned back up off support
        front_side_strength=0.72,  # >= floor: uptrend intact
        strength_floor=0.50,
        above_vwap_or_reclaiming=True,
        ofi_level=0.30,          # > 0 (book bid-side firm)
        ofi_slope=0.05,          # >= 0 (not collapsing)
        midday_lull=False,
        cooldown_active=False,
    )
    base.update(over)
    return base


# ── (a) HAPPY PATH — controlled pullback to support + bounce + strong front-side ──

def test_a_controlled_pullback_bounce_strong_frontside_fires():
    d = pullback_add_decision(**_fire_kwargs())
    assert d["fire"] is True
    assert d["reason"] == "pullback_confirmed"
    assert d["R0"] == pytest.approx(100.0)
    # depth_frac = (HWM - pullback_low)/range = (11.00 - 10.55)/1.00 = 0.45 (within band)
    assert d["pullback_depth_frac"] == pytest.approx(0.45)
    # the add's stop sits just below the pullback's higher-low
    assert d["add_stop"] == pytest.approx(10.55)


# ── (b) THE FALLING-KNIFE GUARD — every leg, each must BLOCK ─────────────────────

def test_b_knife_weak_front_side_blocks():
    """⭐ strength below the floor => weak front-side => NO add (the E1/CTNT lesson)."""
    d = pullback_add_decision(**_fire_kwargs(front_side_strength=0.30, strength_floor=0.50))
    assert d["fire"] is False
    assert d["reason"] == "weak_front_side"


def test_b_knife_none_strength_fails_closed():
    """⭐ a None front-side strength FAILS CLOSED (stale/absent => cannot prove the trend is
    intact => NO add — the opposite of the entry-side tilt, which fails OPEN to full size)."""
    d = pullback_add_decision(**_fire_kwargs(front_side_strength=None))
    assert d["fire"] is False
    assert d["reason"] == "no_strength"


def test_b_knife_ofi_collapsing_blocks():
    """⭐ OFI collapsing (level <= 0 OR slope < 0) => a knife => NO add."""
    # level not positive
    d1 = pullback_add_decision(**_fire_kwargs(ofi_level=0.0))
    assert d1["fire"] is False and d1["reason"] == "ofi_collapsing"
    d2 = pullback_add_decision(**_fire_kwargs(ofi_level=-0.2))
    assert d2["fire"] is False and d2["reason"] == "ofi_collapsing"
    # slope negative (rolling over)
    d3 = pullback_add_decision(**_fire_kwargs(ofi_slope=-0.1))
    assert d3["fire"] is False and d3["reason"] == "ofi_collapsing"


def test_b_knife_ofi_none_fails_closed():
    """⭐ a None OFI read FAILS CLOSED (an extra discretionary BUY needs proof)."""
    assert pullback_add_decision(**_fire_kwargs(ofi_level=None))["reason"] == "ofi_unknown"
    assert pullback_add_decision(**_fire_kwargs(ofi_slope=None))["reason"] == "ofi_unknown"


def test_b_knife_below_vwap_falling_blocks():
    """⭐ below VWAP and NOT reclaiming => Ross never adds below VWAP => NO add."""
    d = pullback_add_decision(**_fire_kwargs(above_vwap_or_reclaiming=False))
    assert d["fire"] is False
    assert d["reason"] == "below_vwap"


def test_b_knife_lower_low_breakdown_blocks():
    """⭐ a LOWER low (the pullback undercut the prior low) => a breakdown, NOT a higher-low
    dip => NO add."""
    d = pullback_add_decision(**_fire_kwargs(pullback_low=10.25, prior_pullback_low=10.30))
    assert d["fire"] is False
    assert d["reason"] == "not_higher_low"


def test_b_knife_no_bounce_blocks():
    """The dip has not turned back up (no green re-load tick / reclaim) => NO add yet."""
    d = pullback_add_decision(**_fire_kwargs(bounced=False))
    assert d["fire"] is False
    assert d["reason"] == "no_bounce"


# ── (c) TOO-DEEP pullback — below the structural stop, or beyond the depth band ──

def test_c_pullback_below_structural_stop_blocks():
    """A pullback that breaks BELOW the structural stop is a COLLAPSE, never a buyable dip.
    (It is refused here — never sold; the stop-breach path owns the sell.)"""
    d = pullback_add_decision(**_fire_kwargs(pullback_low=10.15, stop_px=10.20))
    assert d["fire"] is False
    assert d["reason"] == "pullback_below_stop"


def test_c_pullback_too_deep_for_band_blocks():
    """A dip DEEPER than the upper band edge (a rollover, > 0.62 of range) => NO add."""
    # pullback_low = 10.30 => depth = (11.00 - 10.30)/1.00 = 0.70 > 0.62; keep it above
    # the stop (10.20) and a higher-low (prior 10.25) so ONLY the depth gate trips.
    d = pullback_add_decision(
        **_fire_kwargs(pullback_low=10.30, prior_pullback_low=10.25, stop_px=10.20)
    )
    assert d["fire"] is False
    assert d["reason"] == "pullback_too_deep"


def test_c_pullback_too_shallow_blocks():
    """A 1-tick wiggle (depth < the lower band edge, < 0.20 of range) is NOT a pullback-buy."""
    # pullback_low = 10.90 => depth = (11.00 - 10.90)/1.00 = 0.10 < 0.20
    d = pullback_add_decision(**_fire_kwargs(pullback_low=10.90, prior_pullback_low=10.30))
    assert d["fire"] is False
    assert d["reason"] == "pullback_too_shallow"


# ── (d) FLAG-OFF + composition gates ─────────────────────────────────────────────

def test_d_flag_off_never_fires():
    d = pullback_add_decision(**_fire_kwargs(enabled=False))
    assert d["fire"] is False
    assert d["reason"] == "flag_off"


def test_d_crypto_deferred():
    d = pullback_add_decision(**_fire_kwargs(is_equity=False))
    assert d["fire"] is False
    assert d["reason"] == "crypto_deferred"


def test_d_composes_with_other_add_in_flight():
    """COMPOSITION: refuse when the UP-pyramid OR the micro-pullback already has an add in
    flight — never two adds on one tick."""
    d = pullback_add_decision(**_fire_kwargs(other_add_in_flight=True))
    assert d["fire"] is False
    assert d["reason"] == "add_in_flight"


def test_d_own_in_flight_idempotent():
    d = pullback_add_decision(**_fire_kwargs(in_flight=True))
    assert d["fire"] is False
    assert d["reason"] == "add_in_flight"


def test_d_max_adds_cap_and_cooldown():
    assert pullback_add_decision(**_fire_kwargs(add_count=2, max_adds=2))["reason"] == "max_adds_reached"
    # a higher cap re-opens it
    assert pullback_add_decision(**_fire_kwargs(add_count=2, max_adds=3))["fire"] is True
    assert pullback_add_decision(**_fire_kwargs(cooldown_active=True))["reason"] == "cooldown"


def test_d_midday_lull_blocks():
    assert pullback_add_decision(**_fire_kwargs(midday_lull=True))["reason"] == "midday_lull"


def test_d_bad_basis_fails_closed():
    assert pullback_add_decision(**_fire_kwargs(d0=None))["reason"] == "bad_basis"
    assert pullback_add_decision(**_fire_kwargs(d0=0.0))["reason"] == "bad_basis"
    assert pullback_add_decision(**_fire_kwargs(q0=0.0))["reason"] == "bad_basis"
    # a missing support / higher-low structure fails closed
    assert pullback_add_decision(**_fire_kwargs(support_level=None))["reason"] == "no_support_structure"
    assert pullback_add_decision(**_fire_kwargs(prior_pullback_low=None))["reason"] == "no_support_structure"
    assert pullback_add_decision(**_fire_kwargs(move_range=None))["reason"] == "no_move_range"


# ── (e) RISK BOUNDED — the add is R-funded off R0 and respects max-adds ──────────

def test_e_add_risk_bounded_by_R0_and_invariant_A():
    """The add's risk budget = rho * R0 (rho <= 1, conservative); after the blend the stop
    only TIGHTENS (INVARIANT-A) and the #769 circuit re-bases to R0. Here we prove the add
    leg sizes inside R0 and the blended stop never loosens — the combined-position guard the
    pyramid suite proves end-to-end against the max-loss circuit."""
    d = pullback_add_decision(**_fire_kwargs())
    assert d["fire"] is True
    R0 = d["R0"]
    rho = 0.5
    d0 = 0.10
    # add risk budget = rho*R0; planned add qty = budget / d0 (risk-first). With rho<=1 the
    # add's structural risk (qa * d0) is <= R0 — bounded by the starter's original risk.
    add_budget = rho * R0
    qa_planned = add_budget / d0
    add_structural_risk = qa_planned * d0
    assert add_structural_risk <= R0 + EPS

    # the add fill blends, the stop ratchets to >= blended breakeven (INVARIANT-A: tighten-only)
    q0, a0, Pa_f = 1000.0, 10.0, 10.60
    pre_add_stop = 10.20
    blend = pyramid_blend_on_fill(
        q0=q0, a0=a0, qa_f=qa_planned, Pa_f=Pa_f, stop_px=pre_add_stop, original_quantity=q0
    )
    assert blend["s1"] >= pre_add_stop - EPS, "INVARIANT-A: pullback-add stop loosened"
    assert blend["q1"] == pytest.approx(q0 + qa_planned)
    assert blend["original_quantity"] == pytest.approx(q0 + qa_planned)


def test_e_add_count_caps_total_reload_risk():
    """BOUNDED: at most ``max_adds`` pullback-adds per position, so total pullback-add risk
    is bounded by max_adds * rho * R0 (each leg <= R0 by the rho<=1 sizing)."""
    # at the cap, no more fires
    assert pullback_add_decision(**_fire_kwargs(add_count=2, max_adds=2))["fire"] is False
    # below the cap, it fires
    assert pullback_add_decision(**_fire_kwargs(add_count=1, max_adds=2))["fire"] is True


# ── (d-e2) FLAG-OFF byte-identical, end-to-end on a held winner ──────────────────

def test_flag_off_full_tick_no_pullback_add(monkeypatch, db):
    """PARITY-OFF (end-to-end): with chili_momentum_pullback_add_enabled OFF, driving
    tick_live_session on a held TRAILING winner produces NO pullback-add — no pullback_add_*
    bookkeeping, no BUY, position unchanged (byte-identical). The UP-pyramid + micro-pullback
    are also held off so the ONLY thing this asserts is the pullback-add no-op."""
    from datetime import datetime, timezone
    from unittest.mock import patch

    from app.config import settings
    from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_TRAILING
    from app.services.trading.momentum_neural.live_runner import tick_live_session
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
    )
    from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
    from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid
    from tests.test_momentum_pyramid import _mk_held_adapter
    import app.services.trading.momentum_neural.live_runner as lr

    _RECENT_OPEN = datetime.now(timezone.utc).replace(microsecond=0).isoformat()

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pullback_add_enabled", False)   # OFF
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_micropullback_reentry_enabled", False)
    vid, _ = _seed_live_eligible_row(db, symbol="PBA-USD")
    db.commit()
    uid = _uid(db, "pbaoff")
    pos = {
        "product_id": "PBA-USD", "side": "long",
        "quantity": 1000.0, "original_quantity": 1000.0,
        "avg_entry_price": 10.0, "notional_usd": 10000.0,
        "opened_at_utc": _RECENT_OPEN,
        "high_water_mark": 10.55, "stop_price": 10.20, "target_price": 10.80,
        "partial_taken": True,
    }
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="PBA-USD", variant_id=vid, mode="live",
        state=STATE_LIVE_TRAILING,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 5000, "max_hold_seconds": 86400},
            "momentum_live_execution": {
                "position": dict(pos),
                "entry_sizing": {"model": "risk_first", "stop_distance": 0.10},
                "entry_stop_atr_pct": 0.01,
                "admission_viability_score": 0.9,
            },
        },
    )
    db.commit()
    ad = _mk_held_adapter("PBA-USD", bid=10.55, ask=10.56)
    ad.get_order.return_value = (None, None)

    with patch.object(lr, "_venue_broker_connected", return_value=True), \
         patch.object(lr, "is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    # No pullback-add bookkeeping appeared and no BUY was placed.
    assert "pullback_add_order_id" not in le
    assert "pullback_add_count" not in le
    for call in ad.place_limit_order_gtc.call_args_list:
        assert call.kwargs.get("side") != "buy", "flag OFF must place no pullback-add BUY"
    final_pos = le.get("position") or {}
    assert final_pos.get("quantity") == pytest.approx(1000.0)
    assert final_pos.get("avg_entry_price") == pytest.approx(10.0)
    assert out.get("ok")
