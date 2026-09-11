"""ROSS EXIT GAP 2 (close-below-structure / BOS exit) -- RETIRED 2026-09-10 [57].

WHAT WAS DELETED, ON WHICH CLOCK. Until [57] the live held tick read the LAST CLOSED BAR
against the last CONFIRMED swing low (``entry_gates.bos_exit_triggered_long`` ->
``_compute_confirmed_swing_low_last(lookback=10)``: the pivot is confirmed only after 10 bars
on EACH side, buffer 30 bps) and, since #1377, ARMED the tick exit on a close below it. The
frame came from ``chili_momentum_pullback_entry_interval``, whose default is ``"1m"``
(app/config.py, deliberately flipped 5m->1m in WAVE-4 ITEM-0) and which no ``.env`` on this
host pins -- so the site read 1-MINUTE bars and its "structure" was ~10 minutes old by
construction, not the 50 minutes an earlier draft of this receipt claimed. ``test_the_deleted
_site_read_one_minute_bars_not_five`` below pins the resolved interval so the receipt cannot
drift again. (The paper twin read 15m -- the same level, ~2.5 h old.)

⚠️ THE TAIL NUMBERS MEASURE A DIFFERENT RULE. The evidence that opened the question (memory
project_shelf_break_is_a_stop_not_a_profit_taker_0909, 2026-09-10 00:40Z, the full extended
July tape bought by hydration) is a PRINT-indexed pivot-low ratchet:

    13 legs with peak >= 1 R:   actual +47.03 R  ->  shelf k=3 -1.57 R
                                (11/13 cut at every k in 3..50 PRINTS)
    VRAX 07-09:                 actual +25.58 R  ->  -0.29 R   (5.76 -> 11.05 in two hours;
                                every breath broke the newest higher-low)
    body (54 legs, tape >= 08-26): 6 of the 10 best legs cut, +6.66 R -> +3.96 R
    86% of shelf breaks are trap / noise (median depth 2.90%, then reclaim)

That rule is NOT the deleted predicate, on four axes: (1) k counts PRINTS, not bars -- on a
1M-print name k=50 is seconds, while the deleted site was 10x1m bars (~10 min) per side;
(2) the proxy RATCHETS upward, while ``_compute_confirmed_swing_low_last`` returns the LATEST
confirmed pivot and can step DOWN; (3) the proxy has no buffer, the site had 30 bps; (4) the
proxy exits on the FIRST PRINT below the shelf, the site on a bar CLOSE. The measured harm
also SHRINKS as k grows (-1.57 @k=3, -1.45 @k=8, -1.81 @k=20, +1.04 @k=50), so it does not
extrapolate onto a slower, buffered, bar-close rule. The R gap is NOT what buys the deletion.

WHAT BUYS THE DELETION. (a) A bar close is not a print: on a HELD tick the tape answers, and
the tick exit (``momentum_break_stop``) already owns that verdict. (b) The site is inert --
it fired ONCE in 28 days live (BIAF 2026-09-04 18:10:56Z: last_close 17.30 vs bid 17.53) and
0 times in 14 days of paper. (c) The faster analog of the same level destroys the tail, so
the level has no forward path on the REWARD side; a shelf is a better STOP and keeps its
place on the RISK side (the deadman / pullback-low stop).

HONEST NOTE: the one direct measurement of the DELETED rule is the opposite sign -- BIAF was
-$1.63 actual vs -$21.84 held, so over 28 days the deletion is -$20.21 on the only decision
the site ever made (pinned in tests/test_opinion_exits_ask_the_tape.py). And the "162 ticks
held back by the 30-s structure floor" is not 162 near-misses: ``_opinion_exit_suppressed``
runs BEFORE the predicate, so it counts sub-30 s held ticks -- identically (162) for
``lost_vwap_flatten``.

These end-to-end ``tick_live_session`` proofs drive the LIVE runner with the SAME injected
recorded-OHLCV frames the retired site used to fire on (the ``replay_ohlcv_provider`` seam):

  * a CONFIRMED last-closed-bar CLOSE below the swing low (minus the old buffer) -> NOTHING:
    no arming receipt, no ``live_bos_exit``, no bailout; the LONG stays HELD so the tick exit
    (``momentum_break_stop``) or the deadman is the exit -- on the first tick and the next;
  * an intrabar WICK below the swing low whose bar CLOSES back above -> the same HOLD (the
    wick case never fired; it must not start to);
  * ⚠️ a POSITIVE ANCHOR on the same seed and the same frame: drop the bid THROUGH the resting
    stop and the stop exit fires. Without it every assertion here is an absence, and any
    future pre-gate that made ``tick_live_session`` return early (an identity quarantine, a
    held-tick floor, a flag default) would turn this file green while proving nothing;
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


def _seed_entered_session(db, *, symbol: str, stop_price: float = 7.0):
    """A held LONG with a tiny unrealized loss (avg 8.8) and a stop FAR below (7.0) so neither
    the stop-breach nor the max-loss circuit acts -- exactly the seed the retired site fired
    on, so a HOLD here is the absence of that site and not another exit's silence.

    ``stop_price`` is the ONLY dial: the positive anchor raises it just above the same bid so
    the stop check (which lives BELOW the retired site) has to speak, proving the tick reached
    that far. Everything else -- symbol, size, entry, age, frame, fixtures -- is identical."""
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
        "high_water_mark": 9.0, "stop_price": float(stop_price), "target_price": 12.0,
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


# ── (0) POSITIVE ANCHOR: the held-tick chain PAST the retired site really runs ───
def test_the_held_tick_chain_is_reached_on_this_seed(db, monkeypatch):
    """⚠️ THE ANCHOR. Every other assertion in this file is an ABSENCE, and an absence is
    also what a tick that never ran produces. ``tick_live_session`` has pre-gates that
    short-circuit with ``{"ok": True, "skipped": ...}`` and touch nothing -- the
    ``non_alpaca_account_identity_quarantined`` fence this module's ``_frozen_account_identity``
    fixture exists to defeat is one, and a new held-tick floor or a flag default flip would be
    another. Any of them would turn (a) and (b) green while proving nothing about [57].

    So: the SAME ``_FIRE_DF`` frame, the SAME bid 8.70, the same fixtures and the same seed
    with ONE dial moved -- the resting stop raised from 7.00 to 8.75, i.e. just above that
    bid. The stop check lives BELOW the retired site in the function, so its receipt
    (``stop_breach_pending_confirm`` -- the flicker guard's first read) can only be written by
    a tick that executed the whole held chain, past where the shelf block used to sit. If this
    test ever fails, (a) and (b) are vacuous and must not be trusted."""
    _isolate(monkeypatch)
    sess = _seed_entered_session(db, symbol=_PROD, stop_price=8.75)
    out, _ad = _drive_tick(db, sess, bid=8.7, ask=8.72, df=_FIRE_DF)
    assert out.get("ok"), out
    assert not out.get("skipped"), out
    pend = _events(db, sess, "stop_breach_pending_confirm")
    assert len(pend) == 1, [e.event_type for e in db.query(TradingAutomationEvent).filter(
        TradingAutomationEvent.session_id == sess.id).all()]
    assert float(pend[0].payload_json["bid"]) == pytest.approx(8.7)
    # the receipt carries the stop the tick ACTUALLY holds: >= the seeded 8.75 because the
    # trail ratchet (also below the retired site) may have raised it first -- never lowered
    # it (INVARIANT-A). Both of those running is the point of the anchor.
    assert float(pend[0].payload_json["stop_price"]) >= 8.75
    # ...and the same tick still writes NO shelf footprint: the deleted site is gone, not
    # merely out-ranked by the stop on this seed.
    _no_shelf_footprint(db, sess)


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
    # the module-level ``fetch_ohlcv_df`` the deleted 15m read was the only consumer of is
    # gone too -- it is not merely unused: two tests used to monkeypatch that name, and a
    # patch that binds nothing is a test that silently stopped controlling its input.
    assert not hasattr(paper_runner, "fetch_ohlcv_df")

    tick = ast.parse(inspect.getsource(lr.tick_live_session))
    assert "bos_exit_triggered_long" not in _call_names(tick)
    assert 'trigger="bos_exit"' not in inspect.getsource(lr.tick_live_session)


def test_the_backtest_lane_still_applies_the_shelf_break_and_that_is_named_not_hidden():
    """⚠️ WHAT [57] DID **NOT** CHANGE -- pinned so it is a receipt, not a surprise.

    The momentum LIVE and PAPER lanes have no shelf-break exit any more. A SECOND engine
    still has one and still defaults it **ON**: ``exit_evaluator.build_config_live``
    (``cfg.get("use_bos", True)``), ``build_config_backtest(use_bos=True)`` and
    ``backtest_service.run_pattern_backtest`` (``use_bos = True`` before the per-pattern
    ``exit_config`` is consulted). So a pattern whose ``exit_config`` omits ``use_bos`` is
    still backtested WITH a shelf-break profit-taker, and that expectancy is what
    ``ensemble_promotion_check`` reads.

    That is deliberate, and it is NOT a dark flag: [57]'s measurement is intraday momentum
    legs on the print tape, and the backtest engine runs a different predicate on a different
    bar clock (per-pattern ``interval``, its own ``bos_grace_bars``, seeded ``exit_config``
    rows in ``app/migrations.py``). Flipping this default would change the measured expectancy
    of every pattern mined without an explicit ``use_bos`` -- exactly the "extrapolate a
    measurement onto a predicate it did not measure" error that [57]'s own review caught in
    its first draft. It needs its own measurement and its own migration for the seeded rows.

    If that work lands, this test is the thing that fails and points at the receipt."""
    from app.services.trading import exit_evaluator as ev

    assert ev.build_config_live({}).use_bos is True
    assert ev.build_config_live({"use_bos": False}).use_bos is False
    assert inspect.signature(ev.build_config_backtest).parameters["use_bos"].default is True
    # the ExitConfig dataclass itself still defaults OFF: only the two builders opt in
    assert ev.ExitConfig().use_bos is False

    import app.services.backtest_service as bs

    src = inspect.getsource(bs.run_pattern_backtest)
    assert "use_bos = True" in src
    assert 'exit_config.get("use_bos", True)' in src


def test_the_deleted_site_read_one_minute_bars_not_five():
    """⚠️ THE RECEIPT PIN. The first draft of [57] wrote "closed 5m bar ... >= 50 minutes old
    by construction" into four permanent receipts. It was 5x wrong: the deleted block chose
    its frame with ``getattr(settings, "chili_momentum_pullback_entry_interval", "5m")``, and
    that setting's DEFAULT is ``"1m"`` (flipped 5m->1m on purpose in WAVE-4 ITEM-0, because a
    dropped env pin silently regressing to 5m was the -$137 bug on 2026-07-02). The ``"5m"``
    in the ``getattr`` fallback never applied -- the attribute always exists.

    So the retired site read 1m bars: with ``lookback=10`` on EACH side the pivot is confirmed
    ~10 minutes after it forms, not ~50. Pin the resolved value so no future receipt can
    inherit the wrong clock, and pin the arithmetic that turns it into an age."""
    resolved = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
    assert resolved == "1m", resolved
    assert Settings.model_fields["chili_momentum_pullback_entry_interval"].default == "1m"

    from app.services.trading.momentum_neural import entry_gates

    lookback = inspect.signature(
        entry_gates._compute_confirmed_swing_low_last
    ).parameters["lookback"].default
    assert lookback == 10
    minutes_per_bar = {"1m": 1, "2m": 2, "5m": 5, "15m": 15}[resolved]
    assert lookback * minutes_per_bar == 10  # ~10 min per side, NOT 50

    # ...and both permanent receipts state the CORRECTED clock positively (a grep for the old
    # "50 min" string is useless: the receipts now quote it to say it was wrong).
    import pathlib

    repo = pathlib.Path(__file__).resolve().parents[1]
    runner = (
        repo / "app/services/trading/momentum_neural/live_runner.py"
    ).read_text(encoding="utf-8-sig")
    assert 'chili_momentum_pullback_entry_interval`, at ang default niyan ay "1m"' in runner
    assert "~10 minuto ang tanda ng" in runner
    doc = (repo / "docs/DESIGN/MOMENTUM_LANE.md").read_text(encoding="utf-8-sig")
    assert "read **1-minute** bars" in doc and "~10 minutes** after it formed" in doc
