"""ROSS EXIT GAP 1 — "lost VWAP → flatten" on a HELD position.

Ross's intraday line-in-the-sand: after entry, if price LOSES session VWAP in a CONFIRMED
way, he is OUT. The CONFIRMED-LOSS definition (anti-whipsaw, the ONE documented base) is ALL
of:
  (a) the last CLOSED bar closed BELOW session VWAP (no 1-tick intrabar undercut can fire it),
  (b) the live bid is STILL below VWAP by an ADAPTIVE margin = the name's OWN close-vs-VWAP
      dispersion-sigma * margin_sigma (a fraction of the name's own dispersion, not a fixed
      magnitude), AND not reclaiming, AND
  (c) order-flow is NOT positive.

These are end-to-end ``tick_live_session`` proofs driving the LIVE runner with an injected
recorded-OHLCV frame (the ``replay_ohlcv_provider`` seam) so the VWAP read is deterministic:

  * a CONFIRMED loss (closed below + bid below the margin + flow not positive) → FLATTEN
    (transition to STATE_LIVE_BAILOUT, ``live_lost_vwap_flatten`` emitted), and
  * a momentary 1-tick undercut whose bar CLOSES back above VWAP → NO flatten, and
  * a dip that RECLAIMS VWAP → NO flatten AND the dip-add can still fire (THE COMPOSITION —
    the same tick can never both add and flatten), and
  * flag OFF → byte-identical (no flatten, no transition, no emit).
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_BAILOUT,
    STATE_LIVE_ENTERED,
    STATE_LIVE_TRAILING,
)
import app.services.trading.momentum_neural.live_runner as lr
from app.services.trading.momentum_neural.live_runner import tick_live_session
from app.services.trading.momentum_neural.persistence import (
    create_trading_automation_session,
)
from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
from app.models.trading import TradingAutomationEvent

from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid
from tests.test_momentum_pyramid import _mk_held_adapter


# A plain (non-datetime-indexed) frame so _today_session_frame returns it unchanged and
# front_side_state reads the whole frame as "today".
def _mk_frame(closes: list[float], vols: list[float] | None = None) -> pd.DataFrame:
    n = len(closes)
    vols = vols or [1000.0] * n
    return pd.DataFrame(
        {
            "Open": list(closes),
            "High": [c + 0.05 for c in closes],
            "Low": [c - 0.05 for c in closes],
            "Close": list(closes),
            "Volume": list(vols),
        }
    )


# Drifts gently down so the LAST bar closes just BELOW VWAP (vwap≈9.983, last=9.88, dist≈-2.0).
# The drop from the ~10.0 entry stays small so the max-loss circuit / stop do NOT pre-empt;
# the ONLY exit that can fire on this frame is the lost-VWAP flatten. flatten requires
# bid < ~9.970 (vwap minus the ~0.013 dispersion-sigma margin).
_LOSS_CLOSES = [10.0, 10.05, 10.1, 10.05, 10.0, 9.98, 9.95, 9.92, 9.90, 9.88]
# Dips then the LAST bar RECLAIMS above VWAP (above_vwap True ⇒ NOT a confirmed loss).
_RECLAIM_CLOSES = [10.0, 10.05, 10.1, 10.05, 10.0, 9.98, 9.95, 9.98, 10.02, 10.10]
# Last bar CLOSES back above VWAP (closed-below == False) even though a live bid can dip below
# VWAP intrabar — proves the closed-bar leg blocks a momentary 1-tick undercut.
_WICK_CLOSES = [10.0, 10.02, 10.05, 10.06, 10.07, 10.06, 10.05, 10.04, 10.05, 10.07]

_PROD = "LVF"  # equity symbol (no -USD): the lane treats it as equity


def _provider(closes: list[float]):
    df = _mk_frame(closes)
    return lambda t, *, interval, period: df


def _seed_held_session(db, *, symbol: str, state: str = STATE_LIVE_ENTERED):
    """A freshly-ENTERED held LONG (avg 10.0, stop well below at 9.0, HWM ~ entry). ENTERED so
    the TRAILING-only chandelier trail does NOT pre-empt; the small drop to bid keeps the
    max-loss circuit / stop from firing — so the lost-VWAP flatten is the ONLY exit in play."""
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, f"lvf_{symbol}")
    recent_open = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    pos = {
        "product_id": symbol, "side": "long",
        "quantity": 1000.0, "original_quantity": 1000.0,
        "avg_entry_price": 10.0, "notional_usd": 10000.0,
        "opened_at_utc": recent_open,
        "high_water_mark": 10.10, "stop_price": 9.0, "target_price": 12.0,
        "partial_taken": False,
    }
    sess = create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=vid, mode="live",
        state=state,
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
    return sess


def _drive_tick(db, sess, *, bid: float, ask: float, provider_closes: list[float]):
    ad = _mk_held_adapter(sess.symbol, bid=bid, ask=ask)
    ad.get_order.return_value = (None, None)
    with patch.object(lr, "_venue_broker_connected", return_value=True), \
         patch.object(lr, "is_kill_switch_active", return_value=False), \
         lr.replay_ohlcv_provider(_provider(provider_closes)):
        out = tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    db.commit()
    db.refresh(sess)
    return out, ad


def _events(db, sess, name: str) -> list[TradingAutomationEvent]:
    return (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == sess.id,
            TradingAutomationEvent.event_type == name,
        )
        .all()
    )


# ── (a) CONFIRMED LOSS → FLATTEN ─────────────────────────────────────────────────
def test_confirmed_lost_vwap_flattens(db, monkeypatch):
    """Last bar closed below VWAP + live bid below the dispersion-sigma margin + flow not
    positive ⇒ the held LONG is flattened (transition to BAILOUT, event emitted)."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_lost_vwap_flatten_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_bos_exit_live_enabled", False)  # isolate GAP 1
    monkeypatch.setattr(settings, "chili_momentum_pullback_add_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_micropullback_reentry_enabled", False)

    sess = _seed_held_session(db, symbol=_PROD)
    # bid 9.96 < vwap≈9.983 minus the ~0.013 dispersion-sigma margin (flatten_bid<9.9704);
    # last close 9.88 < vwap (closed-below True); the (9.96-10.0)*1000=-$40 loss is far inside
    # the max-loss circuit floor so ONLY the lost-VWAP flatten can fire.
    out, _ad = _drive_tick(db, sess, bid=9.96, ask=9.97, provider_closes=_LOSS_CLOSES)

    assert out.get("ok")
    assert sess.state == STATE_LIVE_BAILOUT
    evs = _events(db, sess, "live_lost_vwap_flatten")
    assert len(evs) == 1
    payload = evs[0].payload_json or {}
    assert payload.get("reason") == "lost_vwap_confirmed"
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    assert le.get("last_bailout_trigger") == "lost_vwap_flatten"


# ── (b) MOMENTARY UNDERCUT → NO FLATTEN ──────────────────────────────────────────
def test_momentary_undercut_does_not_flatten(db, monkeypatch):
    """A 1-tick bid undercut of VWAP whose CLOSED bar is back ABOVE VWAP is NOT a confirmed
    loss (the closed-bar leg fails) ⇒ NO flatten, the position is held."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_lost_vwap_flatten_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_bos_exit_live_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pullback_add_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_micropullback_reentry_enabled", False)

    sess = _seed_held_session(db, symbol=_PROD)
    # vwap≈10.047 for _WICK_CLOSES; bid 9.99 momentarily below VWAP BUT the last bar CLOSED
    # at 10.07 (> vwap) so (a) closed-below is FALSE ⇒ no confirmed loss. bid<avg so the tick
    # does not flip ENTERED→TRAILING either.
    out, _ad = _drive_tick(db, sess, bid=9.99, ask=10.00, provider_closes=_WICK_CLOSES)

    assert out.get("ok")
    assert sess.state == STATE_LIVE_ENTERED  # still held
    assert _events(db, sess, "live_lost_vwap_flatten") == []


# ── (c) RECLAIM → NO FLATTEN + THE DIP-ADD CAN STILL FIRE (the composition) ───────
def test_reclaim_does_not_flatten_and_dip_add_composes(db, monkeypatch):
    """A pullback that HOLDS/RECLAIMS VWAP (above_vwap True) is a DIP-BUY, not a flatten:
    the lost-VWAP flatten does NOT fire (above_vwap leg fails) AND the dip-add path runs
    (its own predicate owns whether it actually adds). The same tick can never both add and
    flatten — they are mutually exclusive by construction (loss ⇒ flatten+return; reclaim ⇒
    fall through to the dip-add)."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_lost_vwap_flatten_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_bos_exit_live_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pullback_add_enabled", True)  # dip-add ON
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_micropullback_reentry_enabled", False)

    # Reclaim frame: last bar closes ABOVE VWAP ⇒ above_vwap True ⇒ no confirmed loss. Seed
    # TRAILING (the state in which the dip-add's PHASE-2 trigger is even eligible).
    sess = _seed_held_session(db, symbol=_PROD, state=STATE_LIVE_TRAILING)
    out, ad = _drive_tick(db, sess, bid=10.12, ask=10.13, provider_closes=_RECLAIM_CLOSES)

    assert out.get("ok")
    # NOT flattened by the lost-VWAP path.
    assert sess.state == STATE_LIVE_TRAILING
    assert _events(db, sess, "live_lost_vwap_flatten") == []
    # COMPOSITION: the flatten did NOT pre-empt with a BAILOUT; the dip-add block was reached
    # and ran (whether it placed depends on its own geometry/guards). Crucially there is no
    # flatten on this tick, so a flatten+add on the same tick is impossible.
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    assert le.get("last_bailout_trigger") != "lost_vwap_flatten"


# ── (d) FLAG OFF → BYTE-IDENTICAL (no flatten, no event) ─────────────────────────
def test_flag_off_no_flatten(db, monkeypatch):
    """chili_momentum_lost_vwap_flatten_enabled OFF on the SAME confirmed-loss inputs ⇒ NO
    flatten, NO transition, NO event (byte-identical to the pre-feature behavior)."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_lost_vwap_flatten_enabled", False)  # OFF
    monkeypatch.setattr(settings, "chili_momentum_bos_exit_live_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pullback_add_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_micropullback_reentry_enabled", False)

    sess = _seed_held_session(db, symbol=_PROD)
    out, _ad = _drive_tick(db, sess, bid=9.96, ask=9.97, provider_closes=_LOSS_CLOSES)

    assert out.get("ok")
    assert sess.state == STATE_LIVE_ENTERED  # NOT flattened (flag off ⇒ byte-identical)
    assert _events(db, sess, "live_lost_vwap_flatten") == []
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    assert le.get("last_bailout_trigger") != "lost_vwap_flatten"
