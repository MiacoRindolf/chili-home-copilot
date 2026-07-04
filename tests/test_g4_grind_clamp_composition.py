"""G4 C1/C2 — grind structure-clamp COMPOSITION through the real live FSM.

Adversarial review C1/C2: the grind structure clamp must apply ONLY to the passive
heat-class trail candidate. FLOW-CONFIRMED reversal locks (OFI exhaustion, tape-accel
turn, sell-into-strength, measured-move/double-top) fire on confirmed real-time
exhaustion and are designed to lock near the high-water mark — clamping them down to
the (looser) structure floor reintroduces the giveback bug they exist to prevent
(worked example: an OFI lock at 11.964 pulled down to a 11.475 floor = ~1.6R extra
giveback).

These tests drive the REAL ``tick_live_session`` on a held TRAILING grind-mode
position (the pyramid full-tick template) with ``ofi_exhaustion_lock`` patched as the
only firing layer, and assert on the WRITTEN stop:

  1. grind ACTIVE + OFI lock fires ABOVE the structure floor  => written stop == the
     OFI candidate (NOT the floor);
  2. grind ACTIVE + only the passive trail runs               => written stop never
     exceeds the structure floor (the clamp still suppresses passive giveback creep);
  3. grind INACTIVE + OFI lock fires                          => written stop == the
     OFI candidate (parity: the no-grind path is byte-identical).
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from app.config import settings
from app.services.trading.momentum_neural.paper_execution import grind_mode_decision

# entry/anchor geometry shared by every case (mirrors the pyramid template numbers)
_ENTRY = 10.0
_ATR_PCT = 0.01
_EMA5 = 10.30
_HL5 = 10.35
# the helper's own floor formula: max(anchors) - entry * max(0.001, atr_pct * 0.25)
_FLOOR = max(_EMA5, _HL5) - _ENTRY * max(0.001, _ATR_PCT * 0.25)  # = 10.325


def _seed_grind_session(db, *, symbol: str, grind_active: bool, hwm: float,
                        cadence: str | None):
    """A held LIVE_TRAILING crypto session with warm G4 anchor caches."""
    from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_TRAILING
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
    )
    from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
    from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid
    from tests.test_momentum_pyramid import _RECENT_OPEN

    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, f"g4c{symbol[:3].lower()}")
    pos = {
        "product_id": symbol, "side": "long",
        "quantity": 1000.0, "original_quantity": 1000.0,
        "avg_entry_price": _ENTRY, "notional_usd": 10000.0,
        "opened_at_utc": _RECENT_OPEN,
        "high_water_mark": hwm, "stop_price": 10.20, "target_price": 12.00,
        "partial_taken": True,
    }
    if grind_active:
        pos["g4_grind_active"] = True
    le = {
        "position": dict(pos),
        "entry_sizing": {"model": "risk_first", "stop_distance": 0.10},
        "entry_stop_atr_pct": _ATR_PCT,
        "admission_viability_score": 0.9,
        # warm G4 anchor caches (the grind decision reads these at the tick top)
        "ema5m_val": _EMA5,
        "g4_hl5m_val": _HL5,
    }
    if cadence is not None:
        le["cadence_cls"] = cadence
    sess = create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=vid, mode="live",
        state=STATE_LIVE_TRAILING,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 5000, "max_hold_seconds": 86400},
            "momentum_live_execution": le,
        },
    )
    db.commit()
    return sess


def _run_tick(db, sess, *, symbol: str, bid: float, ask: float, ofi_lock_result: dict):
    """One real tick with the OFI lock stubbed as the only fire-capable flow layer."""
    import app.services.trading.momentum_neural.live_runner as lr
    from app.services.trading.momentum_neural.live_runner import tick_live_session
    from tests.test_momentum_pyramid import _mk_held_adapter

    ad = _mk_held_adapter(symbol, bid=bid, ask=ask)
    inert = {"fired": False, "new_stop_floor": None}
    with patch.object(lr, "ofi_exhaustion_lock", return_value=dict(ofi_lock_result)), \
         patch.object(lr, "tape_accel_reversal_exit", return_value=dict(inert)), \
         patch.object(lr, "measured_move_exit_enabled", return_value=False), \
         patch("app.services.trading.momentum_neural.paper_execution.sell_into_strength_ladder",
               return_value=dict(inert)), \
         patch("app.services.trading.momentum_neural.pipeline._live_ofi_microprice",
               return_value=(0.9, 1.0)), \
         patch.object(lr, "_venue_broker_connected", return_value=True), \
         patch.object(lr, "is_kill_switch_active", return_value=False):
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}
    return (le.get("position") or {})


def test_floor_constant_matches_helper() -> None:
    """The test's floor constant tracks the helper's own formula (drift guard)."""
    out = grind_mode_decision(
        enabled=True, prior_active=True, is_day_leader=None, cadence_cls="FAST",
        entry_price=_ENTRY, bid=10.55, atr_pct=_ATR_PCT, stop_atr_mult=0.60,
        high_water_mark=10.55, ema_5m=_EMA5, last_higher_low=_HL5,
    )
    assert out["active"] is True
    assert out["structure_floor"] == pytest.approx(_FLOOR)


def test_grind_flow_confirmed_ofi_lock_writes_unclamped(monkeypatch, db) -> None:
    """C1/C2 core: grind ACTIVE, the OFI exhaustion lock fires with a candidate ABOVE
    the structure floor => the WRITTEN stop equals the OFI candidate, not the floor."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    sym = "G4F-USD"
    sess = _seed_grind_session(db, symbol=sym, grind_active=True, hwm=10.55, cadence="FAST")
    pos = _run_tick(
        db, sess, symbol=sym, bid=10.55, ask=10.56,
        ofi_lock_result={"armed": True, "fired": True, "new_stop_floor": 10.50,
                         "trigger": "test_exhaustion", "peak_r": 2.0, "lock_bps": 50.0},
    )
    assert pos.get("g4_grind_active") is True  # grind maintained (bid 10.55 >= floor)
    # the flow-confirmed lock wrote UNCLAMPED: 10.50, not the 10.325 floor
    assert float(pos.get("stop_price") or 0.0) == pytest.approx(10.50)


def test_grind_passive_trail_still_clamped_to_floor(monkeypatch, db) -> None:
    """The clamp still suppresses PASSIVE giveback creep: grind ACTIVE, no flow layer
    fires, hwm high enough that the heat trail candidate sits above the floor => the
    written stop never exceeds the structure floor."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    sym = "G4P-USD"
    # hwm 11.0: the flat-500bps cushion trail candidate = 11.0*0.95 = 10.45 > floor
    sess = _seed_grind_session(db, symbol=sym, grind_active=True, hwm=11.0, cadence="FAST")
    pos = _run_tick(
        db, sess, symbol=sym, bid=10.55, ask=10.56,
        ofi_lock_result={"armed": False, "fired": False, "new_stop_floor": None},
    )
    assert pos.get("g4_grind_active") is True
    written = float(pos.get("stop_price") or 0.0)
    # ratcheted UP from 10.20 but clamped at the structure floor, never inside it
    assert written > 10.20
    assert written <= _FLOOR + 1e-9


def test_no_grind_ofi_lock_parity_unclamped(monkeypatch, db) -> None:
    """Parity: grind INACTIVE => the OFI lock write path is byte-identical (candidate
    written verbatim) — the C1/C2 fix cannot have changed the non-grind behavior."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    sym = "G4N-USD"
    sess = _seed_grind_session(db, symbol=sym, grind_active=False, hwm=10.55, cadence=None)
    pos = _run_tick(
        db, sess, symbol=sym, bid=10.55, ask=10.56,
        ofi_lock_result={"armed": True, "fired": True, "new_stop_floor": 10.50,
                         "trigger": "test_exhaustion", "peak_r": 2.0, "lock_bps": 50.0},
    )
    assert not pos.get("g4_grind_active")
    assert float(pos.get("stop_price") or 0.0) == pytest.approx(10.50)
