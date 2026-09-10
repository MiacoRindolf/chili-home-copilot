"""ROSS EXIT GAP 2 (close-below-structure / BOS exit) -- RETIRED 2026-09-10 [57].

Until [57] the live held tick read a closed 5m bar against the last CONFIRMED swing low
(``entry_gates.bos_exit_triggered_long``: the pivot is confirmed only after 10 bars on each
side = 50 minutes each side, buffer 30 bps) and, since #1377, ARMED the tick exit on a close
below it. Measured on the print tape as what it is -- a pivot-low ratchet used as a
profit-taker (memory project_shelf_break_is_a_stop_not_a_profit_taker_0909, 2026-09-10 00:40Z,
the full extended July tape bought by hydration):

    13 legs with peak >= 1 R:   actual +47.03 R  ->  shelf k=3 -1.57 R
                                (11/13 cut at every k in 3..50)
    VRAX 07-09:                 actual +25.58 R  ->  -0.29 R   (5.76 -> 11.05 in two hours;
                                every breath broke the newest higher-low)
    body (54 legs, tape >= 08-26): 6 of the 10 best legs cut, +6.66 R -> +3.96 R
    86% of shelf breaks are trap / noise (median depth 2.90%, then reclaim)

Live, the bar twin fired ONCE in 28 days (BIAF 2026-09-04 18:10:56Z: last_close 17.30 vs bid
17.53, actual -$1.63 -> -$21.84 held) and was held back 162 ticks by the 30-s structure floor.
A shelf is a better STOP and a worse profit-taker: the level keeps its place on the RISK side
(the deadman / pullback-low stop) and has no reward-side exit. So the site is DELETED -- not
armed, not routed -- with its per-tick 5m fetch, its two settings (no dark flag), the paper
lane's direct ``reason="bos"`` exit and the then-callerless helper.

These end-to-end ``tick_live_session`` proofs drive the LIVE runner with the SAME injected
recorded-OHLCV frames the retired site used to fire on (the ``replay_ohlcv_provider`` seam):

  * a CONFIRMED last-closed-bar CLOSE below the swing low (minus the old buffer) -> NOTHING:
    no arming receipt, no ``live_bos_exit``, no bailout; the LONG stays HELD so the tick exit
    (``momentum_break_stop``) or the deadman is the exit -- on the first tick and the next;
  * an intrabar WICK below the swing low whose bar CLOSES back above -> the same HOLD (the
    wick case never fired; it must not start to);
  * the settings, the helper and the paper lane's direct exit are gone (source pins).
"""

from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pandas as pd
import pytest

from app.config import Settings, settings
from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_ENTERED
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
    """Build a 31-bar frame with a CONFIRMED swing low ~ 9.04 (a descent to a local trough at
    idx 14 then an ascent). The FIRING frame's last bar CLOSES at 8.6 (< swing-buffer); the
    WICK frame's last bar wicks to 8.5 (below swing) but CLOSES at 10.0 (above). These are the
    frames the retired site fired / did not fire on -- unchanged so the proof is on the same
    input."""
    lows = [11.0 - 0.14 * i for i in range(15)]
    for i in range(1, 17):
        lows.append(lows[14] + 0.18 * i)
    closes = [l + 0.12 for l in lows]
    lows_fire = list(lows)
    closes_fire = list(closes)
    lows_fire[-1] = 8.5
    closes_fire[-1] = 8.6
    lows_wick = list(lows)
    closes_wick = list(closes)
    lows_wick[-1] = 8.5
    closes_wick[-1] = 10.0
    return _mk_frame(lows_fire, closes_fire), _mk_frame(lows_wick, closes_wick)


_FIRE_DF, _WICK_DF = _bos_frames()
_PROD = "BOSX"  # equity symbol


@pytest.fixture(autouse=True)
def _frozen_account_identity(stable_non_alpaca_account_identity):
    """Since #1024 (2026-08-11) the tick runs ``_non_alpaca_account_identity_fence`` at
    ``tick_start`` BEFORE any FSM branch; without a frozen identity the mock adapter is
    quarantined (``skipped=non_alpaca_account_identity_quarantined``) and the held-tick chain
    is never reached -- a HOLD would then prove nothing. Pin the shared stable identity, as
    tests/test_max_loss_circuit_agentic_floor.py does."""
    return stable_non_alpaca_account_identity


def _provider(df: pd.DataFrame):
    return lambda t, *, interval, period: df


def _seed_entered_session(db, *, symbol: str):
    """A held LONG with a tiny unrealized loss (avg 8.8) and a stop FAR below (7.0) so neither
    the stop-breach nor the max-loss circuit acts -- exactly the seed the retired site fired
    on, so a HOLD here is the absence of that site and not another exit's silence."""
    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, f"bos_{symbol}")
    # 120 s old: past the 30-s opinion-exit structure floor (2026-09-06), so if a bar-shelf
    # opinion still existed it would be allowed to speak on the first tick.
    recent_open = (datetime.now(timezone.utc) - timedelta(seconds=120)).replace(microsecond=0).isoformat()
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


def _isolate(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    # Keep the lost-VWAP opinion and the adds out of the way (the same isolation the retired
    # site's proofs used) so a HOLD here is the absence of the bar-shelf site.
    monkeypatch.setattr(settings, "chili_momentum_lost_vwap_flatten_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pullback_add_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_micropullback_reentry_enabled", False)


def _no_shelf_footprint(db, sess):
    """Nothing the retired site ever wrote: no arming receipt, no dedicated event, no
    bailout, no marker on the leg."""
    assert _events(db, sess, "live_bos_exit") == []
    assert _events(db, sess, "live_opinion_exit_armed") == []
    assert _events(db, sess, "live_bailout") == []
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution", {})
    assert le.get("opinion_exit_armed") is None
    assert le.get("last_bailout_trigger") is None


# ── (a) CONFIRMED CLOSE BELOW THE SWING LOW -> NOTHING, HELD ─────────────────────
def test_confirmed_close_below_structure_does_not_arm_and_does_not_exit(db, monkeypatch):
    """The frame the retired site fired on. bid 8.7 > stop 7.0 (no stop-breach) and < avg 8.8
    (no ENTERED->TRAILING flip); the closed bar is 8.6 < swing~9.04 * (1 - 0.003). The LONG
    stays HELD and the leg carries no shelf footprint -- on this tick and on the next (the
    site is gone, not debounced)."""
    _isolate(monkeypatch)
    sess = _seed_entered_session(db, symbol=_PROD)
    out, _ad = _drive_tick(db, sess, bid=8.7, ask=8.72, df=_FIRE_DF)
    assert out.get("ok")
    assert "opinion_exit_armed" not in out
    assert sess.state == STATE_LIVE_ENTERED  # held: the tape or the deadman decides
    _no_shelf_footprint(db, sess)

    out2, _ad = _drive_tick(db, sess, bid=8.7, ask=8.72, df=_FIRE_DF)
    assert out2.get("ok")
    assert "opinion_exit_armed" not in out2
    assert sess.state == STATE_LIVE_ENTERED
    _no_shelf_footprint(db, sess)


# ── (b) INTRABAR WICK (close above) -> the same HOLD ─────────────────────────────
def test_intrabar_wick_close_above_still_holds(db, monkeypatch):
    """The last bar wicks BELOW the swing low intrabar but CLOSES back above it. The retired
    predicate never fired on this frame; nothing must start to."""
    _isolate(monkeypatch)
    sess = _seed_entered_session(db, symbol=_PROD)
    out, _ad = _drive_tick(db, sess, bid=8.7, ask=8.72, df=_WICK_DF)
    assert out.get("ok")
    assert sess.state == STATE_LIVE_ENTERED
    _no_shelf_footprint(db, sess)


# ── (c) SOURCE PINS: settings, helper and the paper lane's direct exit are gone ──
def _call_names(tree: ast.AST) -> set[str]:
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            f = n.func
            if isinstance(f, ast.Name):
                out.add(f.id)
            elif isinstance(f, ast.Attribute):
                out.add(f.attr)
    return out


def _fill_reasons(tree: ast.AST) -> set[str]:
    """Every literal ``reason=...`` keyword and ``"reason": ...`` dict value in a module."""
    out: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            for k in n.keywords:
                if k.arg == "reason" and isinstance(k.value, ast.Constant):
                    out.add(str(k.value.value))
        elif isinstance(n, ast.Dict):
            for k, v in zip(n.keys, n.values):
                if (isinstance(k, ast.Constant) and k.value == "reason"
                        and isinstance(v, ast.Constant)):
                    out.add(str(v.value))
    return out


def test_the_settings_the_helper_and_the_paper_exit_are_gone():
    """No dark flag left behind (the two settings are gone from ``Settings``, not merely
    defaulted off); the helper has no caller and is gone from ``entry_gates`` while the
    swing-low READER stays for the risk side (G4 grind clamp, micro-pullback ratchet); the
    paper lane has no direct ``bos`` exit fill, so paper mirrors live: no shelf-break exit."""
    from app.services.trading.momentum_neural import entry_gates, paper_runner

    for key in ("chili_momentum_bos_exit_live_enabled", "chili_momentum_bos_exit_buffer_pct"):
        assert key not in Settings.model_fields, key
        assert not hasattr(settings, key), key
    assert not hasattr(entry_gates, "bos_exit_triggered_long")
    assert callable(entry_gates._compute_confirmed_swing_low_last)

    paper = ast.parse(inspect.getsource(paper_runner))
    assert "bos" not in _fill_reasons(paper), sorted(_fill_reasons(paper))
    assert "bos_exit_triggered_long" not in _call_names(paper)

    tick = ast.parse(inspect.getsource(lr.tick_live_session))
    assert "bos_exit_triggered_long" not in _call_names(tick)
    assert 'trigger="bos_exit"' not in inspect.getsource(lr.tick_live_session)
