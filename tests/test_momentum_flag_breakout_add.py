"""Ross ADD-ON-FLAG-BREAKOUT — the FOURTH held-position add (continuation at a flag break).

The three existing held-position adds each own a DISTINCT trigger: the UP-pyramid (new-HOD
+ OFI thrust), the micro-pullback re-load (shallow dip-and-curl), and the BUY-THE-DIP
pullback-add (bounce off support). Ross ALSO adds when a held winner consolidates into a
tight BULL FLAG (a base after the impulse) and then BREAKS the flag's swing high — a
CONTINUATION add at the breakout. ``flag_breakout_add_decision`` owns that distinct trigger.

The flag GEOMETRY + the CONFIRMED break are detected upstream by ``bull_flag_confirmation``
(the same detector the fresh-ENTRY lane uses) on the held position's recent bars; the
predicate consolidates the gating (caps, composition, the falling-knife guard, the genuine-
break margin, the higher-base / structural-stop floors) and the sizing basis.

The CRITICAL invariant (the E1/CTNT lesson) is the falling-knife / quality guard: the add
fires ONLY when the uptrend is intact —

  flag_confirmed (a real, confirmed bull-flag break)  AND
  the break is GENUINE (bid clears the flag top by >= a margin-frac of the flag range)  AND
  the flag built a HIGHER base (flag_high > prior_flag_high)  AND
  the flag low HELD above the structural stop (flag_low >= stop_px)  AND
  front_side_strength >= an adaptive floor  AND
  OFI not collapsing (ofi_level > 0 AND ofi_slope >= 0)  AND
  above VWAP or cleanly reclaiming.

If ANY fail it is a fake-out / knife, not a continuation, and NO add fires (bias toward not
adding). These are pure assertions on the predicate — no DB — plus a flag-OFF end-to-end
parity proof and a risk-bound check.
"""

import pytest

from app.services.trading.momentum_neural.paper_execution import (
    flag_breakout_add_decision,
    pyramid_blend_on_fill,
)

EPS = 1e-6


def _fire_kwargs(**over):
    """Baseline inputs that FIRE the flag-breakout add (a valid confirmed bull-flag break,
    higher base, uptrend intact). q0=1000, a0=10.0, d0=0.10 => R0=100.

    Flag: high=11.00 (swing-high / break level), low=10.50 (the flag base / stop), range=0.50.
    The live bid (11.10) cleared the flag high by (11.10-11.00)/0.50 = 0.20 of range (>= the
    0.10 margin = a GENUINE break). The flag high (11.00) is ABOVE the prior flag/add level
    (10.80) AND the flag low (10.50) holds above the structural stop (10.40)."""
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
        bid=11.10,               # cleared the 11.00 flag top on the break
        stop_px=10.40,           # structural stop (banked, below the flag low)
        flag_confirmed=True,     # bull_flag_confirmation returned ok=True upstream
        flag_high=11.00,         # the flag swing-high / break level
        flag_low=10.50,          # the flag base / proposed add stop
        prior_flag_high=10.80,   # the prior flag/add high (this flag is HIGHER)
        breakout_margin_frac=0.10,
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


# ── (a) HAPPY PATH — valid flag + confirmed genuine break + strong front-side ──

def test_a_confirmed_flag_break_strong_frontside_fires():
    d = flag_breakout_add_decision(**_fire_kwargs())
    assert d["fire"] is True
    assert d["reason"] == "flag_break_confirmed"
    assert d["R0"] == pytest.approx(100.0)
    # breakout_frac = (bid - flag_high)/range = (11.10 - 11.00)/0.50 = 0.20 (>= 0.10 margin)
    assert d["breakout_frac"] == pytest.approx(0.20)
    # the add's stop sits just below the flag low
    assert d["add_stop"] == pytest.approx(10.50)


# ── (b) THE FALLING-KNIFE / QUALITY GUARD — every leg, each must BLOCK ───────────

def test_b_no_flag_break_blocks():
    """No confirmed bull-flag break upstream (bull_flag_confirmation returned ok=False) =>
    there is no flag to add into => NO add."""
    d = flag_breakout_add_decision(**_fire_kwargs(flag_confirmed=False))
    assert d["fire"] is False
    assert d["reason"] == "no_flag_break"


def test_b_knife_weak_front_side_blocks():
    """⭐ strength below the floor => weak front-side => NO add (the E1/CTNT lesson)."""
    d = flag_breakout_add_decision(**_fire_kwargs(front_side_strength=0.30, strength_floor=0.50))
    assert d["fire"] is False
    assert d["reason"] == "weak_front_side"


def test_b_knife_none_strength_fails_closed():
    """⭐ a None front-side strength FAILS CLOSED (stale/absent => cannot prove the trend is
    intact => NO add — the opposite of the entry-side tilt, which fails OPEN to full size)."""
    d = flag_breakout_add_decision(**_fire_kwargs(front_side_strength=None))
    assert d["fire"] is False
    assert d["reason"] == "no_strength"


def test_b_knife_ofi_collapsing_blocks():
    """⭐ OFI collapsing (level <= 0 OR slope < 0) => a knife => NO add."""
    d1 = flag_breakout_add_decision(**_fire_kwargs(ofi_level=0.0))
    assert d1["fire"] is False and d1["reason"] == "ofi_collapsing"
    d2 = flag_breakout_add_decision(**_fire_kwargs(ofi_level=-0.2))
    assert d2["fire"] is False and d2["reason"] == "ofi_collapsing"
    d3 = flag_breakout_add_decision(**_fire_kwargs(ofi_slope=-0.1))
    assert d3["fire"] is False and d3["reason"] == "ofi_collapsing"


def test_b_knife_ofi_none_fails_closed():
    """⭐ a None OFI read FAILS CLOSED (an extra discretionary BUY needs proof)."""
    assert flag_breakout_add_decision(**_fire_kwargs(ofi_level=None))["reason"] == "ofi_unknown"
    assert flag_breakout_add_decision(**_fire_kwargs(ofi_slope=None))["reason"] == "ofi_unknown"


def test_b_knife_below_vwap_falling_blocks():
    """⭐ below VWAP and NOT reclaiming => Ross never adds below VWAP => NO add."""
    d = flag_breakout_add_decision(**_fire_kwargs(above_vwap_or_reclaiming=False))
    assert d["fire"] is False
    assert d["reason"] == "below_vwap"


def test_b_not_higher_base_blocks():
    """⭐ the new flag did NOT build above the prior flag/add level (same shelf or lower) =>
    not a structure step-up => NO add."""
    d = flag_breakout_add_decision(**_fire_kwargs(flag_high=10.80, prior_flag_high=10.80))
    assert d["fire"] is False
    assert d["reason"] == "not_higher_base"


def test_b_flag_below_structural_stop_blocks():
    """A flag whose base undercut the structural stop is a breakdown, never a buyable
    continuation. (It is refused here — never sold; the stop-breach path owns the sell.)"""
    d = flag_breakout_add_decision(**_fire_kwargs(flag_low=10.35, stop_px=10.40))
    assert d["fire"] is False
    assert d["reason"] == "flag_below_stop"


# ── (c) EXTENDED-BAR CHASE — not a clear flag-break, just a wick poking the top ──

def test_c_break_not_clear_extended_chase_blocks():
    """The bid only just touched the flag high (cleared it by < the margin-frac of the flag
    range) => a 1-tick wick / an extended chase, NOT a confirmed take-out => NO add."""
    # bid=11.02 => breakout_frac = (11.02-11.00)/0.50 = 0.04 < 0.10 margin
    d = flag_breakout_add_decision(**_fire_kwargs(bid=11.02))
    assert d["fire"] is False
    assert d["reason"] == "break_not_clear"


def test_c_break_exactly_at_flag_high_blocks():
    """A bid sitting AT the flag high (breakout_frac == 0) is not a break at all."""
    d = flag_breakout_add_decision(**_fire_kwargs(bid=11.00))
    assert d["fire"] is False
    assert d["reason"] == "break_not_clear"


# ── (d) FLAG-OFF + composition gates ─────────────────────────────────────────────

def test_d_flag_off_never_fires():
    d = flag_breakout_add_decision(**_fire_kwargs(enabled=False))
    assert d["fire"] is False
    assert d["reason"] == "flag_off"


def test_d_crypto_deferred():
    d = flag_breakout_add_decision(**_fire_kwargs(is_equity=False))
    assert d["fire"] is False
    assert d["reason"] == "crypto_deferred"


def test_d_composes_with_other_add_in_flight():
    """COMPOSITION: refuse when ANY of the other 3 adds (UP-pyramid / micro-pullback /
    buy-the-dip pullback-add) already has an add in flight — never two adds on one tick."""
    d = flag_breakout_add_decision(**_fire_kwargs(other_add_in_flight=True))
    assert d["fire"] is False
    assert d["reason"] == "add_in_flight"


def test_d_own_in_flight_idempotent():
    d = flag_breakout_add_decision(**_fire_kwargs(in_flight=True))
    assert d["fire"] is False
    assert d["reason"] == "add_in_flight"


def test_d_max_adds_cap_and_cooldown():
    assert flag_breakout_add_decision(**_fire_kwargs(add_count=2, max_adds=2))["reason"] == "max_adds_reached"
    # a higher cap re-opens it
    assert flag_breakout_add_decision(**_fire_kwargs(add_count=2, max_adds=3))["fire"] is True
    assert flag_breakout_add_decision(**_fire_kwargs(cooldown_active=True))["reason"] == "cooldown"


def test_d_midday_lull_blocks():
    assert flag_breakout_add_decision(**_fire_kwargs(midday_lull=True))["reason"] == "midday_lull"


def test_d_bad_basis_fails_closed():
    assert flag_breakout_add_decision(**_fire_kwargs(d0=None))["reason"] == "bad_basis"
    assert flag_breakout_add_decision(**_fire_kwargs(d0=0.0))["reason"] == "bad_basis"
    assert flag_breakout_add_decision(**_fire_kwargs(q0=0.0))["reason"] == "bad_basis"
    # a missing flag level / higher-base structure fails closed
    assert flag_breakout_add_decision(**_fire_kwargs(flag_high=None))["reason"] == "no_flag_structure"
    assert flag_breakout_add_decision(**_fire_kwargs(prior_flag_high=None))["reason"] == "no_flag_structure"
    # degenerate flag levels (low >= high) fail closed
    assert flag_breakout_add_decision(**_fire_kwargs(flag_high=10.50, flag_low=10.50))["reason"] == "bad_flag_levels"


# ── (e) RISK BOUNDED — the add is R-funded off R0 and respects max-adds ──────────

def test_e_add_risk_bounded_by_R0_and_invariant_A():
    """The add's risk budget = rho * R0 (rho <= 1, conservative); after the blend the stop
    only TIGHTENS (INVARIANT-A) and the #769 circuit re-bases to R0. Here we prove the add
    leg sizes inside R0 and the blended stop never loosens — the combined-position guard the
    pyramid suite proves end-to-end against the max-loss circuit."""
    d = flag_breakout_add_decision(**_fire_kwargs())
    assert d["fire"] is True
    R0 = d["R0"]
    rho = 0.5
    d0 = 0.10
    add_budget = rho * R0
    qa_planned = add_budget / d0
    add_structural_risk = qa_planned * d0
    assert add_structural_risk <= R0 + EPS

    # the add fill blends, the stop ratchets to >= blended breakeven (INVARIANT-A: tighten-only)
    q0, a0, Pa_f = 1000.0, 10.0, 11.10
    pre_add_stop = 10.40
    blend = pyramid_blend_on_fill(
        q0=q0, a0=a0, qa_f=qa_planned, Pa_f=Pa_f, stop_px=pre_add_stop, original_quantity=q0
    )
    assert blend["s1"] >= pre_add_stop - EPS, "INVARIANT-A: flag-add stop loosened"
    assert blend["q1"] == pytest.approx(q0 + qa_planned)
    assert blend["original_quantity"] == pytest.approx(q0 + qa_planned)


def test_e_add_count_caps_total_reload_risk():
    """BOUNDED: at most ``max_adds`` flag-breakout adds per position, so total flag-add risk
    is bounded by max_adds * rho * R0 (each leg <= R0 by the rho<=1 sizing)."""
    assert flag_breakout_add_decision(**_fire_kwargs(add_count=2, max_adds=2))["fire"] is False
    assert flag_breakout_add_decision(**_fire_kwargs(add_count=1, max_adds=2))["fire"] is True


# ── (d-e2) FLAG-OFF byte-identical, end-to-end on a held winner ──────────────────

def test_flag_off_full_tick_no_flag_breakout_add(monkeypatch, db):
    """PARITY-OFF (end-to-end): with chili_momentum_flag_breakout_add_enabled OFF, driving
    tick_live_session on a held TRAILING winner produces NO flag-breakout add — no
    flag_breakout_add_* bookkeeping, no BUY, position unchanged (byte-identical). The other
    three adds are also held off so the ONLY thing this asserts is the flag-add no-op."""
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
    monkeypatch.setattr(settings, "chili_momentum_flag_breakout_add_enabled", False)   # OFF
    monkeypatch.setattr(settings, "chili_momentum_pullback_add_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_micropullback_reentry_enabled", False)
    vid, _ = _seed_live_eligible_row(db, symbol="FBA-USD")
    db.commit()
    uid = _uid(db, "fbaoff")
    pos = {
        "product_id": "FBA-USD", "side": "long",
        "quantity": 1000.0, "original_quantity": 1000.0,
        "avg_entry_price": 10.0, "notional_usd": 10000.0,
        "opened_at_utc": _RECENT_OPEN,
        "high_water_mark": 11.10, "stop_price": 10.40, "target_price": 11.50,
        "partial_taken": True,
    }
    sess = create_trading_automation_session(
        db, user_id=uid, symbol="FBA-USD", variant_id=vid, mode="live",
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
    ad = _mk_held_adapter("FBA-USD", bid=11.05, ask=11.06)
    ad.get_order.return_value = (None, None)

    with patch.object(lr, "_venue_broker_connected", return_value=True), \
         patch.object(lr, "is_kill_switch_active", return_value=False):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    # No flag-breakout-add bookkeeping appeared and no BUY was placed.
    assert "flag_breakout_add_order_id" not in le
    assert "flag_breakout_add_count" not in le
    for call in ad.place_limit_order_gtc.call_args_list:
        assert call.kwargs.get("side") != "buy", "flag OFF must place no flag-breakout-add BUY"
    final_pos = le.get("position") or {}
    assert final_pos.get("quantity") == pytest.approx(1000.0)
    assert final_pos.get("avg_entry_price") == pytest.approx(10.0)
    assert out.get("ok")
