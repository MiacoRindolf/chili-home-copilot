"""ROSS EXIT GAP 2 — live close-below-structure (BOS) exit.

Ross exits on a confirmed bar CLOSE below structure (the last confirmed swing low), NOT an
intrabar wick. The backtest/paper lane already had ``bos_exit_triggered_long`` (entry_gates);
the LIVE lane only had ATR/chandelier INTRABAR trailing. These end-to-end ``tick_live_session``
proofs drive the LIVE runner with an injected recorded-OHLCV frame (the ``replay_ohlcv_provider``
seam) so the closed-bar structure read is deterministic:

  * a CONFIRMED last-closed-bar CLOSE below the swing low (minus the buffer) → FLATTEN
    (transition to STATE_LIVE_BAILOUT, ``live_bos_exit`` emitted), and
  * an intrabar WICK below the swing low whose bar CLOSES back above → NO exit (the predicate
    keys off the last CLOSE, not the low), and
  * flag OFF → byte-identical (no BOS exit, no transition, no emit).
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


def _mk_frame(lows: list[float], closes: list[float]) -> pd.DataFrame:
    n = len(lows)
    highs = [max(l, c) + 0.10 for l, c in zip(lows, closes)]
    return pd.DataFrame(
        {
            "Open": list(closes),
            "High": highs,
            "Low": list(lows),
            "Close": list(closes),
            "Volume": [1000.0] * n,
        }
    )


def _bos_frames() -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build a 31-bar frame with a CONFIRMED swing low ≈ 9.04 (a descent to a local trough at
    idx 14 then an ascent). The FIRING frame's last bar CLOSES at 8.6 (< swing-buffer); the
    WICK frame's last bar wicks to 8.5 (below swing) but CLOSES at 10.0 (above)."""
    lows = [11.0 - 0.14 * i for i in range(15)]
    for i in range(1, 17):
        lows.append(lows[14] + 0.18 * i)
    closes = [l + 0.12 for l in lows]
    # FIRING: last bar closes below the swing low.
    lows_fire = list(lows)
    closes_fire = list(closes)
    lows_fire[-1] = 8.5
    closes_fire[-1] = 8.6
    # WICK: last bar wicks below the swing low but CLOSES back above it.
    lows_wick = list(lows)
    closes_wick = list(closes)
    lows_wick[-1] = 8.5
    closes_wick[-1] = 10.0
    return _mk_frame(lows_fire, closes_fire), _mk_frame(lows_wick, closes_wick)


_FIRE_DF, _WICK_DF = _bos_frames()
_PROD = "BOSX"  # equity symbol


def _provider(df: pd.DataFrame):
    return lambda t, *, interval, period: df


def _seed_entered_session(db, *, symbol: str):
    """A held LONG with a tiny unrealized loss (avg 8.8) and a stop FAR below (7.0) so neither
    the stop-breach nor the max-loss circuit pre-empts — the BOS exit is the only one in play."""
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, f"bos_{symbol}")
    recent_open = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    pos = {
        "product_id": symbol, "side": "long",
        "quantity": 100.0, "original_quantity": 100.0,
        "avg_entry_price": 8.8, "notional_usd": 880.0,
        "opened_at_utc": recent_open,
        "high_water_mark": 9.0, "stop_price": 7.0, "target_price": 12.0,
        "partial_taken": False,
    }
    sess = create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=vid, mode="live",
        state=STATE_LIVE_ENTERED,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {"max_notional_per_trade_usd": 5000, "max_hold_seconds": 86400},
            "momentum_live_execution": {
                "position": dict(pos),
                "entry_sizing": {"model": "risk_first", "stop_distance": 0.30},
                "entry_stop_atr_pct": 0.01,
                "admission_viability_score": 0.9,
            },
        },
    )
    db.commit()
    return sess


def _drive_tick(db, sess, *, bid: float, ask: float, df: pd.DataFrame):
    ad = _mk_held_adapter(sess.symbol, bid=bid, ask=ask)
    ad.get_order.return_value = (None, None)
    with patch.object(lr, "_venue_broker_connected", return_value=True), \
         patch.object(lr, "is_kill_switch_active", return_value=False), \
         lr.replay_ohlcv_provider(_provider(df)):
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


def _common_flags(monkeypatch, *, bos_on: bool):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_bos_exit_live_enabled", bos_on)
    # Isolate GAP 2 — keep the lost-VWAP flatten + the adds out of the way.
    monkeypatch.setattr(settings, "chili_momentum_lost_vwap_flatten_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pullback_add_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_micropullback_reentry_enabled", False)


# ── (a) CONFIRMED CLOSE BELOW SWING-LOW → EXIT ───────────────────────────────────
def test_confirmed_close_below_structure_exits(db, monkeypatch):
    """The last CLOSED bar closes below the confirmed swing low (minus the buffer) ⇒ the held
    LONG is flattened via the BAILOUT machinery (event ``live_bos_exit``)."""
    _common_flags(monkeypatch, bos_on=True)
    sess = _seed_entered_session(db, symbol=_PROD)
    # bid 8.7 > stop 7.0 (no stop-breach); the closed bar is 8.6 < swing≈9.04.
    out, _ad = _drive_tick(db, sess, bid=8.7, ask=8.72, df=_FIRE_DF)

    assert out.get("ok")
    assert sess.state == STATE_LIVE_BAILOUT
    evs = _events(db, sess, "live_bos_exit")
    assert len(evs) == 1
    assert (evs[0].payload_json or {}).get("reason") == "close_below_structure"
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    assert le.get("last_bailout_trigger") == "bos_exit_live"


# ── (b) INTRABAR WICK (close above) → NO EXIT ────────────────────────────────────
def test_intrabar_wick_close_above_does_not_exit(db, monkeypatch):
    """The last bar wicks BELOW the swing low intrabar but CLOSES back above it ⇒ the predicate
    (keyed off the CLOSE, not the low) does NOT fire ⇒ the position is held."""
    _common_flags(monkeypatch, bos_on=True)
    sess = _seed_entered_session(db, symbol=_PROD)
    # bid 8.7 > stop 7.0 (no stop-breach) and < avg 8.8 (no ENTERED→TRAILING flip); the closed
    # bar is 10.0 (> swing≈9.04) despite the 8.5 intrabar wick low ⇒ BOS must NOT fire.
    out, _ad = _drive_tick(db, sess, bid=8.7, ask=8.72, df=_WICK_DF)

    assert out.get("ok")
    assert sess.state == STATE_LIVE_ENTERED  # still held
    assert _events(db, sess, "live_bos_exit") == []


# ── (c) FLAG OFF → BYTE-IDENTICAL ────────────────────────────────────────────────
def test_flag_off_no_bos_exit(db, monkeypatch):
    """chili_momentum_bos_exit_live_enabled OFF on the SAME firing frame ⇒ NO BOS exit, NO
    transition, NO event (byte-identical to the pre-feature behavior)."""
    _common_flags(monkeypatch, bos_on=False)
    sess = _seed_entered_session(db, symbol=_PROD)
    out, _ad = _drive_tick(db, sess, bid=8.7, ask=8.72, df=_FIRE_DF)

    assert out.get("ok")
    assert sess.state == STATE_LIVE_ENTERED  # NOT flattened
    assert _events(db, sess, "live_bos_exit") == []
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    assert le.get("last_bailout_trigger") != "bos_exit_live"
