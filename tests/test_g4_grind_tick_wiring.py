"""[26] THE TAPE NOW DECIDES THE STRUCTURE TRAIL — and the window is not a clock.

`grind_mode_decision` has produced ZERO `g4_grind_mode` events across the ENTIRE live
book (240 `g4_grind_probe` rows since 2026-07-09; 0 activations, 0 topping-tail holds).
#1369 added a tape-native decision and #1370 armed the side-by-side comparison, but both
shipped LOG-ONLY: `_g4_cap` was never read from the tape verdict, the tape verdict was
computed once per MINUTE, and it was handed `prior_active` from the BAR version's state
(always False) so its MAINTENANCE branch was unreachable. This module pins the wiring
that makes the tape binding, and the two measurements that decided its shape.

MEASUREMENT 1 — THE WINDOW IS THE MECHANISM. The g4 probe site was the LAST caller of
`signed_tape_accel_features` still on the 15-SECOND clock (the [58] reversal exit and the
[59] re-entry ramp are print-indexed). Recomputed read-only at all 69 probe instants that
carried a tick verdict (2026-09-09..10, scratchpad/g4_tick_window_probe.py):

    SKYQ session 21591, 2026-09-10 13:53:01Z, peak 7.10R  (the highest-R refusal in the
    whole book)
        15-s clock window (1,353 prints):  signed_tape_accel  -9,898  -> buy_aggression_gone
        255-print window:                  signed_tape_accel +10,969  -> NOT refused

    population (61 instants on real-time feeds; TPET excluded, see MEASUREMENT 2):
        clock accel <= 0   37/61 (60.7%)      print accel <= 0   30/61 (49.2%)

MEASUREMENT 2 — AND THE REFUTATION THAT SHAPED IT. The activation condition was proposed
to move from the raw `signed_tape_accel` to `buy_share_delta` (scale-free share of volume,
the measure `_signed_tape_features` names in its own comment). Checked at the instant it
was built for, that proposal REFUSES it: at SKYQ 13:53:01Z `buy_share_delta` = **-0.1907**
(the clock window reads -0.1359 — negative either way) while the print-window accel is
+10,969. Binding on the share would have kept refusing the 7.10R runner. So the accel
stays binding and the share is REPORTED. The window definition is what flips that instant,
not the statistic.

MEASUREMENT 3 — A COUNT WINDOW HAS NO LOWER TIME BOUND. 7 of 7 live `swing_lows_unreadable`
refusals were TPET session 21589, whose `available_at - observed_at` is 900.5 s (min
900.14 / max 901.38, n=3,095) — a 15-minute-delayed feed ([38]). The clock window read
EMPTY there (unreadable, the correct answer); a count window would happily return 255
fifteen-minute-old prints. The age bound the re-entry ramp already uses —
max(chili_momentum_g4_reentry_max_print_age_seconds, the window's own gap p99) — is
therefore applied here too, and over it the decision falls back to the NAMED bar version.

MEASUREMENT 4 — THE ONE THAT WITHDREW A FIX (review, 2026-09-11). The first form of
this PR also lifted the print-window halt trim from the `window_s/2` constant to the
window's own RAW inter-print gap p99, on the argument that a constant measured in human
seconds trims a SLOW name's window to nothing. Both halves were checked and both failed:

  * the p99 is the statistic a halt MOVES. At <= 100 prints `ceil(0.99*n)-1` is the
    index of the LARGEST gap, so the halt set its own threshold and the guard could
    never fire (executed on that branch: 80 prints + one 180 s halt -> gap_split_s
    180.0, gap_restricted False, accel +4,000 measured ACROSS the halt; main: 7.50 /
    True / 0). At 255 prints the index is the 3rd-largest, so THREE halts lift it too.
    And because `gap_p99_s` is computed after the trim, the halt leaked into the shared
    staleness bound at two other decision sites (live `chili`: WYHG 14.69 s -> 52.41 s,
    MOBX 14.69 s -> 49.93 s).
  * the slow name does not exist on our tape. 4,392 disjoint 255-print windows, 69
    symbol-days, 2026-09-09..10, 08:00-20:00Z (scripts/g4_print_window_trim_probe.py,
    read-only, symbol+time bounded): windows whose MEDIAN inter-print gap is over 7.5 s = **0
    (0.000%)**, largest observed median 7.4845 s. The trim empties a window in
    158/4,392 = 3.60%, with p25 130 / p50 255 prints retained.

So the guard went back to the constant, and the 3.6% is answered at the CONSUMER
instead: the grind holds its last PROVEN structure floor through a tape flicker
(`maintained_carried_floor`) rather than dying, and the basis no longer flips to the
bar version tick by tick.

The FSM cases drive the REAL `tick_live_session` on a held TRAILING position (the
clamp-composition template) with `signed_tape_accel_features` stubbed, so the assertions
are on what production actually wrote: the position state, the emitted receipt and the
written stop.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app.config import settings
from app.models.trading import TradingAutomationEvent
from app.services.trading.momentum_neural.entry_gates import _signed_tape_features
from app.services.trading.momentum_neural.live_runner import (
    _G4_GRIND_ADD_LIFT_ACTIVE,
    _G4_GRIND_ADD_LIFT_BINDING,
)
from app.services.trading.momentum_neural.paper_execution import (
    grind_effective_max_adds,
    grind_mode_decision_tick,
)

# ── geometry shared with tests/test_g4_grind_clamp_composition.py ──────────────
_ENTRY = 10.0
_ATR_PCT = 0.01
_STOP_ATR_MULT = 0.60
#: risk_dist = entry * max(0.003, atr_pct * stop_atr_mult) = 10 * 0.006 = 0.06
_RISK_DIST = _ENTRY * max(0.003, _ATR_PCT * _STOP_ATR_MULT)
#: the helper's own wick buffer: entry * max(0.001, atr_pct * 0.25) = 0.025
_BUF = _ENTRY * max(0.001, _ATR_PCT * 0.25)
_BID = 10.55                      # peak_r = (10.55 - 10) / 0.06 = 9.17R
_SWING_PREV = 10.20
_SWING_NOW = 10.40                # higher low, above entry
_BUY_SUPPORT = 10.35
_TICK_FLOOR = max(_SWING_NOW, _BUY_SUPPORT) - _BUF   # = 10.375

#: The SKYQ 2026-09-10 13:53:01Z shape, as measured over the last 255 prints: aggressor-buy
#: volume ACCELERATING while the buy SHARE of the back half fell. The whole point of [26].
_SKYQ_ACCEL = 10969.0
_SKYQ_SHARE_DELTA = -0.19071911502699568


def _tape(*, accel=_SKYQ_ACCEL, share=_SKYQ_SHARE_DELTA, now=None, age_s=0.25,
          gap_p99=0.05, swing_now=_SWING_NOW, swing_prev=_SWING_PREV):
    """A `signed_tape_accel_features` return whose newest print is `age_s` old.

    Carries the [29] freshness stamp exactly as the real helper does: `print_age_s`
    measured at the DECISION instant and `print_age_bound_s` = the measured print-age
    floor — deliberately NOT raised by this window's own `gap_p99_s`."""
    ref = now or datetime.now(timezone.utc)
    last_ts = ref.timestamp() - float(age_s)
    bound = float(settings.chili_momentum_g4_reentry_max_print_age_seconds)
    return {
        "signed_tape_accel": accel,
        "buy_share_delta": share,
        "swing_low_now": swing_now,
        "swing_low_prev": swing_prev,
        "buy_support_px": _BUY_SUPPORT,
        "high_print_position": 0.12,
        "last_ts": last_ts,
        "gap_p99_s": gap_p99,
        "gap_max_s": gap_p99,
        "n_ticks": 255,
        "gap_restricted": False,
        "window_mode": "prints",
        "print_age_s": float(age_s),
        "print_age_bound_s": bound,
        "print_stale": bool(float(age_s) > bound),
    }


@pytest.fixture()
def tape_calls():
    """Patch the tape read and record every call's kwargs."""
    calls: list[dict] = []
    box = {"value": None}

    def _fn(symbol, **kw):
        calls.append({"symbol": symbol, **kw})
        return box["value"]

    with patch(
        "app.services.trading.momentum_neural.entry_gates.signed_tape_accel_features",
        side_effect=_fn,
    ):
        yield calls, box


def _seed(db, *, symbol: str, tick_active: bool = False, bar_active: bool = False,
          hwm: float = _BID, entry_filled_at: datetime | None = None,
          stop_price: float = 10.20):
    """A held LIVE_TRAILING session with the G4 bar anchors warm (crypto product id so
    nothing but the stub can reach the tape).

    [5] review fix: an EQUITY symbol plus ``entry_filled_at`` (the fill-lineage stamp the
    G/D verdict and the topping-tail leg both anchor on) seeds the reachable production
    path -- the topping tail then reads the REAL ``entry_gates.leg_print_candle`` off prints
    planted with ``_plant_leg``."""
    from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_TRAILING
    from app.services.trading.momentum_neural.persistence import (
        create_trading_automation_session,
    )
    from app.services.trading.momentum_neural.risk_policy import RISK_SNAPSHOT_KEY
    from tests.test_momentum_paper_runner import _seed_live_eligible_row, _uid
    from tests.test_momentum_pyramid import _RECENT_OPEN

    vid, _ = _seed_live_eligible_row(db, symbol=symbol)
    db.commit()
    uid = _uid(db, f"g4t{symbol[:3].lower()}")
    pos = {
        "product_id": symbol, "side": "long",
        "quantity": 1000.0, "original_quantity": 1000.0,
        "avg_entry_price": _ENTRY, "notional_usd": 10000.0,
        "opened_at_utc": _RECENT_OPEN,
        "high_water_mark": hwm, "stop_price": stop_price, "target_price": 12.00,
        "partial_taken": True,
    }
    if tick_active:
        pos["g4_tick_grind_active"] = True
        pos["g4_grind_active"] = True
    if bar_active:
        pos["g4_bar_grind_active"] = True
    le = {
        "position": dict(pos),
        "entry_sizing": {"model": "risk_first", "stop_distance": 0.10},
        "entry_stop_atr_pct": _ATR_PCT,
        "admission_viability_score": 0.9,
        "cadence_cls": "FAST",
        # the BAR anchors stay COLD on purpose: the bar version must refuse, so any
        # activation these tests see came from the tape.
    }
    if entry_filled_at is not None:
        le["entry_filled_at_utc"] = entry_filled_at.replace(tzinfo=timezone.utc).isoformat()
    sess = create_trading_automation_session(
        db, user_id=uid, symbol=symbol, variant_id=vid, mode="live",
        state=STATE_LIVE_TRAILING,
        risk_snapshot_json={
            RISK_SNAPSHOT_KEY: {"allowed": True},
            "momentum_risk_policy_summary": {"disable_live_if_governance_inhibit": True},
            "momentum_policy_caps": {
                "max_notional_per_trade_usd": 5000, "max_hold_seconds": 86400,
            },
            "momentum_live_execution": le,
        },
    )
    db.commit()
    return sess


#: [5] a LEG that IS a topping tail, as PRINTS (seconds before the tick's as-of, price):
#: o 10.50 -> h 11.50 -> l 10.40 -> c 10.51, n = 12, upper wick 0.99 / 1.10 = 0.90 of the
#: range. Planted in the REAL ``iqfeed_trade_ticks`` of the test DB and read by the REAL
#: ``entry_gates.leg_print_candle`` -- no stub (review fix: the first form stubbed the seam on
#: a ``-USD`` session, a combination production can never produce).
_TT_LEG_PRINTS: tuple[tuple[float, float], ...] = (
    (58.0, 10.50), (54.0, 10.70), (50.0, 11.00), (46.0, 11.30), (42.0, 11.50),
    (38.0, 11.20), (34.0, 10.90), (30.0, 10.60), (26.0, 10.45), (20.0, 10.40),
    (12.0, 10.48), (2.0, 10.51),
)
#: seconds before the as-of at which the leg's entry fill happened
_TT_ENTRY_S = 60.0


def _plant_leg(db, *, symbol: str, as_of: datetime,
               prints: tuple[tuple[float, float], ...] = _TT_LEG_PRINTS,
               publication_lag_s: float = 0.0) -> None:
    """Plant a leg's prints (``as_of`` naive UTC) with finite, ordered receipt and
    publication clocks -- what the live bridge writes. ``publication_lag_s`` models a
    delayed feed (TPET: ~900 s)."""
    from datetime import timedelta

    from sqlalchemy import text

    for ago_s, px in prints:
        at = as_of - timedelta(seconds=ago_s)
        pub = (at + timedelta(seconds=publication_lag_s)).replace(tzinfo=timezone.utc)
        db.execute(
            text(
                "INSERT INTO iqfeed_trade_ticks (symbol, observed_at, price, size, bid, ask, "
                "source, received_at, available_at) "
                "VALUES (:s, :t, :p, 100.0, :b, :a, 'test_topping_tail_leg', :r, :r)"
            ),
            {"s": symbol, "t": at, "p": px, "b": px - 0.01, "a": px + 0.01, "r": pub},
        )
    db.commit()


def _drop_leg(db, symbol: str) -> None:
    from sqlalchemy import text

    try:
        db.rollback()
        db.execute(text("DELETE FROM iqfeed_trade_ticks WHERE symbol = :s "
                        "AND source = 'test_topping_tail_leg'"), {"s": symbol})
        db.commit()
    except Exception:
        db.rollback()


def _equity_symbol(tag: str) -> str:
    """A unique equity symbol per test (the test DB's tape table is not truncated)."""
    import uuid

    return f"TT{tag}{uuid.uuid4().hex[:4].upper()}"


def _run_tick(db, sess, *, symbol: str, bid: float = _BID, ask: float | None = None,
              reversal_calls: list | None = None, ofi_calls: list | None = None,
              verdict_calls: list | None = None, leg_calls: list | None = None,
              clock=None):
    """ONE real ``tick_live_session`` pass. For an EQUITY leg ([5] review fix) the topping
    tail reads the REAL leg candle off planted prints; the G/D verdict (#1385, its own
    tests) answers "hold" and records the as-of it was handed; no OHLCV fetch reaches the
    network. ``clock`` (optional) replaces ``live_runner._utcnow`` for the pass."""
    import app.services.trading.momentum_neural.entry_gates as eg
    import app.services.trading.momentum_neural.live_runner as lr
    from app.services.trading.momentum_neural.live_runner import tick_live_session
    from tests.test_momentum_pyramid import _mk_held_adapter

    ad = _mk_held_adapter(symbol, bid=bid, ask=(ask if ask is not None else bid + 0.01))
    inert = {"fired": False, "new_stop_floor": None}

    def _rev(**kw):
        if reversal_calls is not None:
            reversal_calls.append(dict(kw))
        return dict(inert)

    def _ofi(**kw):
        if ofi_calls is not None:
            ofi_calls.append(dict(kw))
        return dict(inert)

    def _verdict(_db, _sess, _le, **kw):
        if verdict_calls is not None:
            verdict_calls.append(dict(kw))
        return {"action": None, "phase": "armed"}

    _real_leg = eg.leg_print_candle

    def _leg_spy(symbol_, **kw):
        if leg_calls is not None:
            leg_calls.append({"symbol": symbol_, **kw})
        return _real_leg(symbol_, **kw)

    stack = [
        patch.object(lr, "ofi_exhaustion_lock", side_effect=_ofi),
        patch.object(lr, "tape_accel_reversal_exit", side_effect=_rev),
        patch.object(lr, "measured_move_exit_enabled", return_value=False),
        patch("app.services.trading.momentum_neural.paper_execution."
              "sell_into_strength_ladder", return_value=dict(inert)),
        patch("app.services.trading.momentum_neural.pipeline._live_ofi_microprice",
              return_value=(0.9, 1.0)),
        patch.object(lr, "_venue_broker_connected", return_value=True),
        patch.object(lr, "is_kill_switch_active", return_value=False),
        # [5]: the REAL leg read, spied (never stubbed) so a test can pin its kwargs.
        patch.object(eg, "leg_print_candle", side_effect=_leg_spy),
        # The test session is coinbase_spot with no FROZEN account identity, so the
        # tick-start fence quarantines it (`non_alpaca_account_identity_unfrozen`) and
        # returns before the TRAILING block ever runs. That fence is not what these
        # tests are about; stub it so the tick reaches the code under test.
        patch.object(
            lr, "verify_frozen_non_alpaca_account_identity",
            return_value={"ok": True, "applicable": False, "frozen_identity": None,
                          "current_identity": None, "reason": None},
        ),
    ]
    if not str(symbol).upper().endswith("-USD"):
        import requests
        from curl_cffi import requests as curl_requests

        def _no_http(*a, **kw):
            raise RuntimeError("external HTTP unavailable in the topping-tail tick test")

        stack += [
            patch.object(lr, "_exit_verdict_tick", side_effect=_verdict),
            patch.object(lr, "_replay_aware_fetch_ohlcv_df", return_value=None),
            # an equity symbol would otherwise reach provider HTTP (yfinance quote lookups)
            patch.object(requests.sessions.Session, "request", side_effect=_no_http),
            patch.object(curl_requests.Session, "request", side_effect=_no_http),
        ]
    if clock is not None:
        stack.append(patch.object(lr, "_utcnow", side_effect=clock))
    for cm in stack:
        cm.start()
    try:
        tick_live_session(db, sess.id, adapter_factory=lambda: ad)
    finally:
        for cm in reversed(stack):
            cm.stop()
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}
    return (le.get("position") or {})


def _events(db, sess, event_type: str) -> list[dict]:
    rows = (
        db.query(TradingAutomationEvent)
        .filter(
            TradingAutomationEvent.session_id == sess.id,
            TradingAutomationEvent.event_type == event_type,
        )
        .order_by(TradingAutomationEvent.id.asc())
        .all()
    )
    return [dict(r.payload_json or {}) for r in rows]


# ── 1. the pure decision: the SKYQ shape activates, and the share is not binding ──

def test_activates_on_the_skyq_shape_accel_positive_share_negative() -> None:
    """SKYQ 2026-09-10 13:53:01Z over 255 prints: accel +10,969 AND share -0.1907.

    This is the exact instant the whole item exists for (peak 7.10R, refused live as
    `buy_aggression_gone` on the 15-s clock window, which read accel -9,898). The tape
    version must ACTIVATE on it — which is only true while the raw accel is the binding
    statistic. It is the one-line proof that binding on `buy_share_delta` instead would
    have reproduced the refusal."""
    out = grind_mode_decision_tick(
        prior_active=False, entry_price=_ENTRY, bid=_BID, atr_pct=_ATR_PCT,
        stop_atr_mult=_STOP_ATR_MULT, high_water_mark=_BID,
        swing_low_now=_SWING_NOW, swing_low_prev=_SWING_PREV,
        buy_support_px=_BUY_SUPPORT, signed_tape_accel=_SKYQ_ACCEL,
        buy_share_delta=_SKYQ_SHARE_DELTA,
    )
    assert out["active"] is True
    assert out["reason"] == "activated"
    assert out["structure_floor"] == pytest.approx(_TICK_FLOOR)
    assert out["peak_r"] >= 1.0
    # REPORTED, not binding — it is negative and the decision activated anyway.
    assert out["buy_share_delta"] == pytest.approx(_SKYQ_SHARE_DELTA)
    assert out["buy_share_delta"] < 0


def test_refuses_when_signed_tape_accel_is_not_positive() -> None:
    """The binding statistic still refuses: accel <= 0 is `buy_aggression_gone` even
    with a POSITIVE share (the mirror of the case above — the conjunction did not
    silently grow a second condition)."""
    out = grind_mode_decision_tick(
        prior_active=False, entry_price=_ENTRY, bid=_BID, atr_pct=_ATR_PCT,
        stop_atr_mult=_STOP_ATR_MULT, high_water_mark=_BID,
        swing_low_now=_SWING_NOW, swing_low_prev=_SWING_PREV,
        buy_support_px=_BUY_SUPPORT, signed_tape_accel=-1.0,
        buy_share_delta=+0.44,
    )
    assert out["active"] is False
    assert out["reason"] == "buy_aggression_gone"
    assert out["buy_share_delta"] == pytest.approx(0.44)


# ── 2. clock independence: the same PRINTS, 400x apart in wall time ─────────────

def _rows(scale: float) -> list[tuple]:
    """A deterministic print sequence whose timestamps are stretched by `scale`."""
    t0 = 1_757_500_000.0
    rows = []
    for i in range(40):
        # a pullback then a continuation: the second half's low is higher
        px = 10.40 + (0.02 * math.sin(i / 3.0)) + (0.00 if i < 20 else 0.06)
        bid, ask = px - 0.01, px + 0.01
        # aggressor BUY accelerating into the back half (prints at/above the ask)
        trade_px = ask if i >= 12 else bid
        size = 100.0 + (40.0 if i >= 20 else 0.0)
        rows.append((trade_px, size, bid, ask, t0 + (i * 0.05 * scale)))
    return rows


@pytest.mark.parametrize("scale", [1.0, 100.0])
def test_the_verdict_does_not_move_when_wall_time_is_stretched(scale: float) -> None:
    """CLOCK INDEPENDENCE, WITHIN THE CONTINUITY GUARD. A window counted in PRINTS must
    read the same tape the same way whether those prints took 2 seconds or 3.25 minutes.
    The same 40 prints, 100x apart in wall time (0.05 s -> 5 s per gap), must produce the
    same grind verdict.

    100x, not 400x: at 400x every gap is 20 s, i.e. over the window_s/2 = 7.5 s
    continuity guard, and the window is correctly refused as DISCONTINUOUS — see
    `test_a_window_whose_every_gap_exceeds_the_guard_is_refused_not_believed`. [26]'s
    first form lifted that guard to make 400x pass and thereby disabled halt detection
    (the measurement that withdrew it is in `_signed_tape_features`' own comment)."""
    feats = _signed_tape_features(
        _rows(scale), window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="prints",
    )
    assert feats is not None
    assert feats["gap_restricted"] is False
    assert feats["gap_split_s"] == pytest.approx(7.5)
    out = grind_mode_decision_tick(
        prior_active=False, entry_price=_ENTRY, bid=_BID, atr_pct=_ATR_PCT,
        stop_atr_mult=_STOP_ATR_MULT, high_water_mark=_BID,
        swing_low_now=feats["swing_low_now"], swing_low_prev=feats["swing_low_prev"],
        buy_support_px=feats["buy_support_px"],
        signed_tape_accel=feats["signed_tape_accel"],
        buy_share_delta=feats["buy_share_delta"],
    )
    # pinned expectations, identical at both scales
    assert out["active"] is True
    assert out["reason"] == "activated"
    assert feats["signed_tape_accel"] > 0
    assert feats["swing_low_now"] > feats["swing_low_prev"]


# ── 2b. the HALT GUARD, and the bound that rides on it ([26] review fixes) ─────
#
# [26] shipped `half_window = max(window_s/2, p99(RAW pre-trim gaps))` for print
# windows. Executed against that branch it did two things it claimed not to do:
#   * at <= 100 prints `ceil(0.99*n)-1` IS the index of the largest gap, so the halt
#     set its own threshold and the guard could never fire;
#   * `gap_p99_s` is computed AFTER the trim, so the halt leaked into it and through
#     `tape_print_age_bound_s` into the micro-pullback re-load and the front-side
#     spent-move gate (WYHG 14.69 s -> 52.41 s; MOBX 14.69 s -> 49.93 s, live).
# And the slow name it was written for does not exist on our tape: 0 of 4,392 disjoint
# 255-print windows over 69 symbol-days (2026-09-09..10, 08:00-20:00Z) has a median
# inter-print gap above 7.5 s (largest observed median 7.4845 s), while the trim empties
# a window in only 158/4,392 = 3.60%. These cases run the REAL function on the REAL
# statistic — the coupling the stubbed `_tape()` dict cannot see.


def _halted_rows(n: int, *, halt_at: int, halt_s: float, gap: float = 0.05) -> list[tuple]:
    """`n` prints at `gap` seconds apart with ONE halt of `halt_s` before `halt_at`."""
    out, t = [], 1_757_500_000.0
    for i in range(n):
        if i == halt_at:
            t += halt_s
        elif i:
            t += gap
        px = 10.0 + (0.001 * i)
        out.append((px, 100.0, px - 0.01, px + 0.01, t))
    return out


@pytest.mark.parametrize(("n_prints", "halt_s"), [(60, 900.0), (80, 180.0), (100, 180.0)])
def test_the_halt_guard_fires_on_short_print_windows(n_prints: int, halt_s: float) -> None:
    """THE HALT MUST NOT SET ITS OWN THRESHOLD. A <= 255-row read is ordinary on a name
    whose tape has just started (median first tick 13:57Z; 60% after the RTH open) or
    across a subscription gap, so this band is not exotic. On the withdrawn form these
    windows came back `gap_restricted=False` with the accel measured ACROSS the halt
    (80 prints + a 180 s halt: gap_split_s 180.0, n_ticks 80, accel +4,000)."""
    feats = _signed_tape_features(
        _halted_rows(n_prints, halt_at=n_prints // 2, halt_s=halt_s),
        window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="prints",
    )
    assert feats is not None
    assert feats["gap_restricted"] is True, "the halt must be detected, not absorbed"
    assert feats["gap_split_s"] == pytest.approx(7.5)      # window_s / 2, both modes
    assert feats["n_ticks"] == n_prints // 2               # post-halt segment only
    # and the halt is NOT in the cadence statistic the age bound reads
    assert feats["gap_p99_s"] == pytest.approx(0.05)


def test_three_halts_cannot_lift_the_threshold_even_at_255_prints() -> None:
    """The withdrawn form's own defence was "a p99 over 254 gaps cannot be lifted by
    three outliers". At n=254 the p99 index is the 3rd-LARGEST, so three halts lift it
    exactly: 255 prints with halts of 300/450/600 s gave gap_split_s 300.0 on that
    branch. The constant cannot be lifted by any number of outliers."""
    rows = [list(r) for r in _halted_rows(255, halt_at=10**9, halt_s=0.0)]
    for idx, extra in ((60, 300.0), (120, 450.0), (180, 600.0)):
        for j in range(idx, len(rows)):
            rows[j][4] += extra
    feats = _signed_tape_features(
        [tuple(r) for r in rows],
        window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="prints",
    )
    assert feats is not None
    assert feats["gap_split_s"] == pytest.approx(7.5)
    assert feats["gap_restricted"] is True
    assert feats["n_ticks"] == 75          # only the segment after the LAST halt


def test_a_window_whose_every_gap_exceeds_the_guard_is_refused_not_believed() -> None:
    """The policy, pinned: a print window whose EVERY gap is over window_s/2 is a
    discontinuous read and the answer is `None` (the caller's fail-open contract) —
    NOT a lifted threshold. On the withdrawn form this window returned a usable number
    and, at the [58] reversal exit (which had no age check at all), that number was
    computed from prints up to 23 hours old."""
    feats = _signed_tape_features(
        _rows(400.0), window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="prints",
    )
    assert feats is None


def test_a_halt_cannot_inflate_the_shared_staleness_bound() -> None:
    """FINDING 3 — the coupling no stubbed-dict test could see. Every age-bound test in
    this module hand-writes `gap_p99_s`; this one takes the REAL statistic out of the
    REAL function and feeds it to the REAL bound, which is what the micro-pullback
    re-load (live_runner :49023) and the front-side spent-move gate (:49717) do.

    A 900-second halt inside the window must not make a 50-second-old print look
    decision-fresh."""
    from app.services.trading.momentum_neural.entry_gates import tape_print_age_bound_s

    feats = _signed_tape_features(
        _halted_rows(255, halt_at=200, halt_s=900.0),
        window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="prints",
    )
    assert feats is not None
    floor = float(settings.chili_momentum_g4_reentry_max_print_age_seconds)
    bound = tape_print_age_bound_s(age_floor_s=floor, gap_p99_s=feats["gap_p99_s"])
    assert bound == pytest.approx(floor)           # the floor still binds
    assert bound < 60.0, "a halted window must not buy a minute of staleness tolerance"


# ── 3. the call site: the window is a COUNT, and the count is the derived one ───

def test_g4_call_site_passes_window_prints(db, monkeypatch, tape_calls) -> None:
    """The g4 tape read was the last caller still on `chili_momentum_l2_confirm_window_s`
    (15 SECONDS). It must pass the tape's own clock instead — the SAME derived setting
    the [58] exit and the [59] re-entry ramp already read. No new number."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4W-USD"
    sess = _seed(db, symbol=sym)
    _run_tick(db, sess, symbol=sym)

    # The g4 read is the one that asks for a PRINT window on the default (count_v1)
    # contract — the [58] exit pins `legacy_time_split` and other readers pass seconds.
    g4_calls = [
        c for c in calls
        if "as_of" in c and "feature_contract" not in c and "window_prints" in c
    ]
    assert g4_calls, "the g4 grind block did not read the tape at all"
    expected = int(settings.chili_momentum_g4_reentry_tape_window_prints)
    assert expected == 255  # the derived binding value, pinned
    for c in g4_calls:
        assert c.get("window_prints") == expected
        assert "window_s" not in c


# ── 4. the wiring: the tape decides, the bar is a NAMED fallback ────────────────

def test_tape_activation_drives_the_cap_and_the_receipt_names_the_basis(
    db, monkeypatch, tape_calls
) -> None:
    """`_g4_cap` now comes from the TICK decision. The bar anchors are deliberately
    COLD here, so the bar version cannot activate — any grind at all proves the tape
    is what decided, and the receipt must say so in `basis` and carry both statistics
    with the derivation of the binding one."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4A-USD"
    sess = _seed(db, symbol=sym)
    pos = _run_tick(db, sess, symbol=sym)

    assert pos.get("g4_grind_active") is True
    assert pos.get("g4_tick_grind_active") is True
    assert pos.get("g4_bar_grind_active") is False   # the bar version refused

    rec = _events(db, sess, "g4_grind_mode")
    assert rec, "activating grind must emit its receipt"
    r = rec[-1]
    assert r["active"] is True
    assert r["basis"] == "tick"
    assert r["reason"] == "activated"
    assert r["tick_active"] is True
    assert r["bar_active"] is False
    # both statistics on the receipt, and the binding one carries its derivation
    assert r["signed_tape_accel"] == pytest.approx(_SKYQ_ACCEL)
    assert r["buy_share_delta"] == pytest.approx(_SKYQ_SHARE_DELTA)
    assert r["tape_window_prints"] == 255
    assert "chili_momentum_g4_reentry_tape_window_prints" in r["tape_window_prints_binding"]
    assert r["tape_max_print_age_s"] is not None
    assert r["tape_last_print_age_s"] is not None
    # the only risk-ADDING effect is named as off, never a hidden switch
    assert r["grind_add_lift"] == _G4_GRIND_ADD_LIFT_BINDING
    assert "off(unmeasured)" in r["grind_add_lift"]


def test_maintenance_runs_off_the_tape_versions_own_shadow_state(
    db, monkeypatch, tape_calls
) -> None:
    """#1370's probe fed the tape version `prior_active` from the BAR version's flag
    (always False), so only ACTIVATION was ever evaluated and "would it have HELD the
    runner" was unanswerable. With its own shadow state the MAINTENANCE branch is
    reachable: a tape that could NOT activate (no higher low in prints, buying gone)
    still MAINTAINS a grind that is already on while price holds the floor."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    # would REFUSE activation: swing_low_now <= swing_low_prev AND accel <= 0
    box["value"] = _tape(accel=-5000.0, swing_now=10.30, swing_prev=10.45)
    sym = "G4M-USD"
    sess = _seed(db, symbol=sym, tick_active=True)
    pos = _run_tick(db, sess, symbol=sym)

    assert pos.get("g4_tick_grind_active") is True
    assert pos.get("g4_grind_active") is True
    # no state change => no new receipt; the probe carries the maintained verdict
    probes = _events(db, sess, "g4_grind_probe")
    assert not probes, "grind is ACTIVE, so the inactive-only probe must not fire"


def test_maintenance_drops_the_grind_when_the_floor_breaks(
    db, monkeypatch, tape_calls
) -> None:
    """Fail toward SCALP: a structure break drops the tape grind on the same tick."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4B-USD"
    sess = _seed(db, symbol=sym, tick_active=True)
    # bid BELOW the structure floor (10.375) but above the placed stop (10.20)
    pos = _run_tick(db, sess, symbol=sym, bid=10.30)

    assert not pos.get("g4_tick_grind_active")
    assert not pos.get("g4_grind_active")
    r = _events(db, sess, "g4_grind_mode")[-1]
    assert r["active"] is False
    assert r["basis"] == "tick"
    assert r["reason"] == "structure_broken"


def test_an_unreadable_tape_does_not_kill_a_working_grind(
    db, monkeypatch, tape_calls
) -> None:
    """FINDING 6 — the flicker was a kill switch. `_g4_tick_active_now` was False on any
    unreadable/stale read regardless of the prior, so ONE bad read cleared the shadow
    state, routed the decision to the bar version (0 activations in 240 probes), left
    `_g4_cap` None and let the unclamped chandelier candidate through `if _trailed >
    stop_px` — a ONE-WAY ratchet that survives the tape coming back, after which
    MAINTENANCE is gone and the full ACTIVATION conjunction must be re-earned.

    The contract the module already documented ("a `None` flicker never drops a working
    grind") is now true: the LAST PROVEN structure floor is carried, and the one break
    test that needs no tape — `bid` against that floor — still runs."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    # tick 1: a readable tape activates the grind and PROVES a floor
    box["value"] = _tape()
    sym = "G4F-USD"
    sess = _seed(db, symbol=sym)
    pos = _run_tick(db, sess, symbol=sym)
    assert pos.get("g4_grind_active") is True
    assert pos.get("g4_tick_structure_floor") == pytest.approx(_TICK_FLOOR)

    # tick 2: the tape is UNREADABLE (halt / DB hiccup) and price still holds the floor
    box["value"] = None
    pos = _run_tick(db, sess, symbol=sym)
    assert pos.get("g4_tick_grind_active") is True, "a flicker must not drop the grind"
    assert pos.get("g4_grind_active") is True
    assert pos.get("g4_basis") == "tick", "and must not hand the leg to the bar version"
    probes = _events(db, sess, "g4_grind_probe")
    assert not probes, "grind is still ACTIVE, so the inactive-only probe must not fire"


def test_a_flicker_still_drops_the_grind_when_price_breaks_the_carried_floor(
    db, monkeypatch, tape_calls
) -> None:
    """Conditioning, not a blanket hold: while the tape is unreadable the grind is
    measured against the floor it last PROVED, and a break of that floor drops it on
    that same tick."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4K-USD"
    sess = _seed(db, symbol=sym)
    assert _run_tick(db, sess, symbol=sym).get("g4_grind_active") is True

    box["value"] = None
    pos = _run_tick(db, sess, symbol=sym, bid=10.30)   # below the 10.375 carried floor
    assert not pos.get("g4_tick_grind_active")
    assert not pos.get("g4_grind_active")
    r = _events(db, sess, "g4_grind_mode")[-1]
    assert r["active"] is False
    assert r["basis"] == "tick"
    assert r["reason"] == "structure_broken"
    assert r["tick_flicker"] == "tape_unreadable"


def test_a_stale_read_maintains_on_the_carried_floor_too(
    db, monkeypatch, tape_calls
) -> None:
    """Same rule for the other flicker shape: a print older than the age bound is not a
    structure break either. The receipt names which flicker it was."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4Q-USD"
    sess = _seed(db, symbol=sym)
    assert _run_tick(db, sess, symbol=sym).get("g4_grind_active") is True

    box["value"] = _tape(age_s=900.5)      # the measured TPET lag
    pos = _run_tick(db, sess, symbol=sym)
    assert pos.get("g4_tick_grind_active") is True
    assert pos.get("g4_basis") == "tick"


def test_the_basis_never_flaps_back_to_bar_once_the_tape_has_answered(
    db, monkeypatch, tape_calls
) -> None:
    """FINDING 4 — the effective state mixed two bases with independent hysteresis and
    none of its own. A name on an intermittently readable feed alternated tick-refuses /
    bar-active every tick, and every flip wrote `pos`, committed, emitted a full
    `g4_grind_mode` receipt and toggled the clamp. The basis is a property of the
    SYMBOL's tape, not of this tick: once the tape has answered for this leg, the bar
    version is no longer an alternative opinion."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    # a READABLE tape that REFUSES activation (no higher low, buying gone) — the tick
    # basis is established but grind never turns on, so nothing is carried.
    box["value"] = _tape(accel=-5000.0, swing_now=10.30, swing_prev=10.45)
    sym = "G4X-USD"
    sess = _seed(db, symbol=sym)
    pos = _run_tick(db, sess, symbol=sym)
    assert pos.get("g4_basis") == "tick"
    assert not pos.get("g4_grind_active")

    box["value"] = None                      # the feed drops out
    pos = _run_tick(db, sess, symbol=sym)
    assert pos.get("g4_basis") == "tick", "an unreadable tick must not re-open the bar"
    assert not pos.get("g4_grind_active")
    # and no `g4_grind_mode` receipt at all: on the old code this pair of ticks was a
    # basis FLIP, which wrote `pos`, committed and emitted the full state-change payload.
    assert not _events(db, sess, "g4_grind_mode")
    # the probe is throttled to one per minute, so tick 1's row is the one on the book;
    # it must already carry the tick verdict under the headline names.
    probes = _events(db, sess, "g4_grind_probe")
    assert len(probes) == 1
    p = probes[-1]
    assert p["probe_schema"] == 2
    assert p["basis"] == "tick"
    assert p["reason"] == p["tick_reason"] == "no_higher_low_in_prints"
    assert p["bar_reason"] and p["bar_reason"] != p["reason"]


def test_the_probe_headline_follows_the_binding_basis(
    db, monkeypatch, tape_calls
) -> None:
    """FINDING 7 — `basis: tick` with a BAR `reason`/`peak_r`/`structure_floor`. The
    field's meaning changed silently, so a census grouping the 240 existing rows by
    `reason` would attribute bar reasons to tick rows. `probe_schema: 2` separates the
    populations and the bar verdict is carried under `bar_*`."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape(accel=-5000.0)       # readable, refuses: buy_aggression_gone
    sym = "G4P-USD"
    sess = _seed(db, symbol=sym)
    _run_tick(db, sess, symbol=sym)

    p = _events(db, sess, "g4_grind_probe")[-1]
    assert p["probe_schema"] == 2
    assert p["basis"] == "tick"
    assert p["reason"] == "buy_aggression_gone" == p["tick_reason"]
    assert p["peak_r"] == p["tick_peak_r"]
    assert p["structure_floor"] == p["tick_structure_floor"]
    # the bar counterfactual is still there, under its own name
    assert p["bar_reason"] and p["bar_reason"] != p["reason"]
    assert "bar_peak_r" in p and "bar_structure_floor" in p


def test_basis_falls_back_to_bar_only_when_the_tape_is_unreadable(
    db, monkeypatch, tape_calls
) -> None:
    """No tape (crypto / empty window / read error) => the 5m-bar decision runs, and the
    receipt NAMES it (`basis: bar`, `tick_reason: tape_unreadable`). Not a silent pass."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = None
    sym = "G4U-USD"
    sess = _seed(db, symbol=sym)
    pos = _run_tick(db, sess, symbol=sym)

    assert not pos.get("g4_grind_active")          # cold bar anchors => inactive
    probes = _events(db, sess, "g4_grind_probe")
    assert probes, "an inactive trailing tick must still emit the probe"
    p = probes[-1]
    assert p["basis"] == "bar"
    assert p["tick_reason"] == "tape_unreadable"


def test_a_fifteen_minute_old_print_is_stale_and_falls_back_to_bar(
    db, monkeypatch, tape_calls
) -> None:
    """FIX-DON'T-DEFER ([38] on this path). A COUNT window has no lower time bound, so
    on a delayed feed it returns 255 prints that are 15 MINUTES old and every structure
    field reads fine. 7 of 7 live `swing_lows_unreadable` refusals at this site were
    TPET session 21589, whose ingest lag is 900.5 s. Over
    max(chili_momentum_g4_reentry_max_print_age_seconds, the window's own gap p99) the
    tape is `tape_source_stale` and the NAMED bar fallback decides."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape(age_s=900.5)     # the measured TPET lag
    sym = "G4S-USD"
    sess = _seed(db, symbol=sym)
    pos = _run_tick(db, sess, symbol=sym)

    assert not pos.get("g4_grind_active"), "a 15-minute-old tape must not arm the grind"
    p = _events(db, sess, "g4_grind_probe")[-1]
    assert p["basis"] == "bar"
    assert p["tick_reason"] == "tape_source_stale"
    assert p["tape_last_print_age_s"] > p["tape_max_print_age_s"]


def test_the_window_cannot_raise_its_own_freshness_ceiling(
    db, monkeypatch, tape_calls
) -> None:
    """FINDING 2 — the bound must not be derived from the sample it is judging.

    The first form of this block re-implemented `max(floor, gap_p99)` inline, and
    `gap_p99_s` is computed AFTER the halt trim, so any change to the trim moved this
    bound with it (measured live when [26] lifted the trim: WYHG 14.69 -> 52.41 s,
    MOBX 14.69 -> 49.93 s) — at this site AND at the two other sites that call
    `tape_print_age_bound_s`. The binding value is now the helper's own [29] stamp,
    whose bound is the measured print-age floor and is never raised by the window
    being tested: a tape whose own gap p99 is 120 s does NOT buy a 60-second-old
    print the right to decide."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape(age_s=60.0, gap_p99=120.0)
    sym = "G4L-USD"
    sess = _seed(db, symbol=sym)
    pos = _run_tick(db, sess, symbol=sym)

    floor = float(settings.chili_momentum_g4_reentry_max_print_age_seconds)
    assert not pos.get("g4_grind_active"), "a 60 s old print must not arm the grind"
    p = _events(db, sess, "g4_grind_probe")[-1]
    assert p["basis"] == "bar"                     # never activated => bar still open
    assert p["tick_reason"] == "tape_source_stale"
    assert p["tape_max_print_age_s"] == pytest.approx(floor)
    assert p["tape_age_basis"] == "helper_stamp"


def test_below_1r_decides_without_consuming_any_tape(db, monkeypatch, tape_calls) -> None:
    """Activation is impossible below 1R — the pure helper refuses before it touches a
    single tape field — and 47 of the 69 recorded probe instants are exactly that. So
    the block skips the 255-print read there, which is what keeps the new per-tick cost
    off the majority of trailing ticks. It is NOT a new gate: the same frozen risk unit
    that the helper uses decides, and the receipt proves the verdict was reached with no
    tape input at all (every tape field null, basis still `tick`)."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4R-USD"
    # hwm only 0.03 above entry => peak_r well under 1R
    sess = _seed(db, symbol=sym, hwm=_ENTRY + 0.03)
    _run_tick(db, sess, symbol=sym, bid=_ENTRY + 0.03)

    p = _events(db, sess, "g4_grind_probe")[-1]
    assert p["basis"] == "tick"
    assert p["tick_reason"] == "below_1r"
    assert p["signed_tape_accel"] is None
    assert p["buy_share_delta"] is None
    assert p["tape_n_ticks"] is None
    assert p["tape_last_print_age_s"] is None


# ── 5. the risk-ADDING effect stays off, and is named ───────────────────────────

def test_the_pyramid_add_count_lift_stays_off_and_is_named() -> None:
    """`grind_effective_max_adds` is the ONLY effect of grind mode that ADDS risk (every
    other one refuses to TIGHTEN a passive trail). It has never once been exercised — 0
    `g4_grind_mode` events all-time — and [26] is what makes grind reachable, so it stays
    closed behind a NAMED constant with a receipt rather than a hidden switch or a new
    off-by-default knob."""
    assert _G4_GRIND_ADD_LIFT_ACTIVE is False
    assert "off(unmeasured)" in _G4_GRIND_ADD_LIFT_BINDING
    # and with the lift off the helper returns the base cap untouched
    assert grind_effective_max_adds(
        base_max_adds=2, grind_active=False, cushion_r=9.0, min_cushion_r=1.0,
    ) == 2


# ── 6. a topping tail ARMS the tick exit, in grind or out of it ─────────────────

def _advancing_clock(t0: datetime, step_ms: float = 1.0):
    """``live_runner._utcnow`` for one pass: starts at ``t0`` (naive UTC) and moves 1 ms per
    call, so a FRESH clock read later in the pass can never equal the tick's as-of."""
    from datetime import timedelta

    state = {"n": 0}

    def _now():
        state["n"] += 1
        return t0 + timedelta(milliseconds=step_ms * (state["n"] - 1))

    return _now


@pytest.fixture()
def planted_legs(db):
    """Symbols whose planted prints are removed after the test (the test DB's tape table
    is not truncated between tests)."""
    syms: list[str] = []
    yield syms
    for s in syms:
        _drop_leg(db, s)


def _equity_tt_session(db, planted_legs, *, tag: str, tick_active: bool = False,
                       stop_price: float = 10.20):
    """An EQUITY TRAILING leg whose entry fill was ``_TT_ENTRY_S`` before the tick, with a
    topping-tail leg planted in the real tape table. Returns ``(sess, symbol, t0)``."""
    from datetime import timedelta

    sym = _equity_symbol(tag)
    planted_legs.append(sym)
    t0 = datetime.utcnow().replace(microsecond=0)
    _plant_leg(db, symbol=sym, as_of=t0)
    sess = _seed(db, symbol=sym, tick_active=tick_active,
                 entry_filled_at=t0 - timedelta(seconds=_TT_ENTRY_S), stop_price=stop_price)
    return sess, sym, t0


def test_topping_tail_arms_the_tick_exit_even_while_grinding(
    db, monkeypatch, tape_calls, planted_legs
) -> None:
    """The 2026-07 grind branch said a topping tail must not full-flatten the day leader
    mid-grind. That branch has never run, and by the time [26] makes it reachable it is
    OBSOLETE: [21] already replaced the full-flatten with ARMING the tick exit, so the
    grind branch would be strictly LOOSER than the non-grind one (neither exiting nor
    arming — nothing answering the candle). Both paths now arm; grind's only effect in
    this PR is the passive-trail clamp.

    [5] review fix: an EQUITY leg with the REAL leg read over REAL planted prints. The
    first form ran this on ``G4T-USD`` with ``leg_print_candle`` stubbed -- but the real
    seam returns None for every ``-USD`` symbol, so it pinned a path production cannot
    take."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_exit_topping_tail_enabled", True)
    calls, box = tape_calls
    box["value"] = _tape()
    sess, sym, t0 = _equity_tt_session(db, planted_legs, tag="G", tick_active=True)
    leg_calls: list[dict] = []
    _run_tick(db, sess, symbol=sym, leg_calls=leg_calls, clock=_advancing_clock(t0))

    assert leg_calls, "the TRAILING block must read the real leg candle"
    held = _events(db, sess, "g4_grind_hold_topping_tail")
    assert held, "the grind receipt must still record the candle"
    assert held[-1]["arms_tick_exit"] is True
    assert held[-1]["arm_outcome"] == "newly_armed"
    # [5]: both receipts name the LEG candle that decided, and its binding condition
    for rcpt in (held[-1], _events(db, sess, "live_opinion_exit_armed")[-1]):
        assert rcpt["window_kind"] == "leg_prints_since_entry_fill"
        assert (rcpt["leg_o"], rcpt["leg_h"], rcpt["leg_l"], rcpt["leg_c"]) == (
            10.50, 11.50, 10.40, 10.51)
        assert rcpt["leg_n"] == len(_TT_LEG_PRINTS)
        assert rcpt["binding"] == "upper_wick_frac"
        assert rcpt["upper_wick_frac"] == pytest.approx(0.9)
        assert rcpt["leg_anchor_source"] == "entry_filled_at_utc"
        assert rcpt["leg_print_stale"] is False
        assert rcpt["leg_print_age_s"] == pytest.approx(2.0, abs=1.0)
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}
    armed = le.get("opinion_exit_armed") or {}
    assert armed, "grind must not swallow the candle: the tick exit has to be armed"
    assert "topping_tail_runner_exit" in (armed.get("reasons") or [])
    assert not _events(db, sess, "live_topping_tail_unavailable")


def test_the_topping_tail_receipt_reports_the_arm_that_actually_happened(
    db, monkeypatch, tape_calls, planted_legs
) -> None:
    """FINDING 5 — the receipt asserted `arms_tick_exit: true` BEFORE the arm was
    attempted, inside `except Exception: pass`. `_arm_opinion_exit` does DB work
    (`_commit_le` + `_emit`), so on a failure the book carried a grind receipt claiming
    the tick exit was armed on a topping tail while nothing was armed and no bailout
    fired — the candle answered by nothing at all, and nothing in the book saying so.
    ([5] review fix: on an equity leg reading real prints, see the test above.)"""
    import app.services.trading.momentum_neural.live_runner as lr

    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_exit_topping_tail_enabled", True)
    calls, box = tape_calls
    box["value"] = _tape()
    sess, sym, t0 = _equity_tt_session(db, planted_legs, tag="Z", tick_active=True)

    def _boom(*a, **kw):
        raise RuntimeError("arm path exploded")

    with patch.object(lr, "_arm_opinion_exit", side_effect=_boom):
        _run_tick(db, sess, symbol=sym, clock=_advancing_clock(t0))

    held = _events(db, sess, "g4_grind_hold_topping_tail")
    assert held, "the failure itself must be on the book"
    assert held[-1]["arms_tick_exit"] is False
    assert held[-1]["arm_outcome"] == "arm_failed"
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}
    assert not (le.get("opinion_exit_armed") or {})


def test_a_crypto_leg_says_once_that_the_topping_tail_cannot_judge_it(
    db, monkeypatch, tape_calls
) -> None:
    """[5] review fix — THE FLAG WAS SILENTLY INERT FOR CRYPTO. `leg_print_candle` returns
    None for every ``-USD`` symbol (there is no print tape), and the TRAILING block had no
    else branch: a default-True flag that cannot fire and records nothing, which is the
    dark flag e91c18092 removed. It now says so ONCE per leg with the G/D verdict's own
    binding (`no_equity_tape`) and never reads the tape."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_exit_topping_tail_enabled", True)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4Y-USD"
    sess = _seed(db, symbol=sym)
    leg_calls: list[dict] = []
    _run_tick(db, sess, symbol=sym, leg_calls=leg_calls)
    _run_tick(db, sess, symbol=sym, leg_calls=leg_calls)       # a second pass, same leg

    assert not leg_calls, "a crypto leg has no print tape to read"
    rec = _events(db, sess, "live_topping_tail_unavailable")
    assert len(rec) == 1, "once per leg per binding, not once per pass"
    assert rec[0]["binding"] == "no_equity_tape"
    assert rec[0]["reported_once_per_leg_per_binding"] is True
    assert "no_arm" in rec[0]["fallback"]
    assert not _events(db, sess, "live_opinion_exit_armed")


# ── 7. the clamp's BINDING VALUE is on the write that it decided ────────────────

def test_the_ratchet_receipt_carries_the_floor_that_decided(
    db, monkeypatch, tape_calls
) -> None:
    """FINDING 8 — `live_trail_ratchet` reported `grind_clamped: bool`, a flag, while the
    value that decided `min(candidate, max(floor, stop))` lived only on `g4_grind_mode`,
    which emits ONLY on a state transition, and `g4_grind_probe` is suppressed while
    grind is active. So for the whole life of a grind every clamped trail write was
    recorded without the number that produced it — and the floor is recomputed from the
    last 255 prints on every pass."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4C-USD"
    # hwm 11.0 => the passive cushion candidate sits ABOVE the 10.375 structure floor,
    # so the clamp is what decides the written stop (same geometry as the C1/C2 module).
    sess = _seed(db, symbol=sym, tick_active=True, hwm=11.0)
    pos = _run_tick(db, sess, symbol=sym)

    assert pos.get("g4_grind_active") is True
    rat = _events(db, sess, "live_trail_ratchet")
    assert rat, "the clamped candidate must still ratchet the stop up from 10.20"
    r = rat[-1]
    assert r["grind_clamped"] is True
    assert r["grind_structure_floor"] == pytest.approx(_TICK_FLOOR)
    assert r["grind_basis"] == "tick"
    assert r["grind_reason"] in {"activated", "maintained", "maintained_carried_floor"}
    # the pre-clamp candidate is carried too, so the clamp's effect is reconstructable
    assert r["trail_candidate_preclamp"] >= r["new_stop"]
    assert float(pos.get("stop_price") or 0.0) == pytest.approx(_TICK_FLOOR)


# ── 7b. the [58] reversal exit no longer reads a day-old tape ──────────────────

def test_the_reversal_exit_refuses_a_stale_print_window(
    db, monkeypatch, tape_calls
) -> None:
    """FINDING 1 — the [58] tape-accel reversal exit is print-indexed and had NO age
    check, by explicit design: its own comment said the window_s/2 trim meant "a stalled
    tape still fails to no_tape rather than reading ancient prints". That premise was
    incomplete — the trim forbids a hole INSIDE the window, not an OLD window — and
    [26]'s first form deleted what was left of it (measured on live `chili`: MOBX as_of
    2026-09-10 18:00Z returned a usable number over rows spanning 2026-09-09 08:01 →
    2026-09-10 17:41). It matters because `prev_signed_tape_accel` is a recycle-cleared
    key, so on the FIRST trailing tick of every leg gate 2 degenerates from a rollover
    to `accel <= 0` — one stale negative number ratchets the runner's stop.

    Two halves close it, and this pins both: [26] withdrew the trim lift, and [29]
    stamps `print_stale` on every print-form read and refuses the accel on it. An old
    print is NO TAPE (the original no-op), not an exit input, and the stale value never
    becomes the next tick's `prev`."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(
        settings, "chili_momentum_exit_tape_accel_reversal_enabled", True
    )
    calls, box = tape_calls
    sym = "G4V-USD"

    # 1) FRESH tape: the accel is handed to the exit exactly as before (parity).
    box["value"] = _tape(accel=-9898.0, age_s=0.25)
    sess = _seed(db, symbol=sym, tick_active=True)
    seen: list[dict] = []
    _run_tick(db, sess, symbol=sym, reversal_calls=seen)
    assert seen, "the reversal exit must still run"
    assert seen[-1]["signed_tape_accel"] == pytest.approx(-9898.0)
    rec = _events(db, sess, "live_tape_accel_reversal_exit")[-1]
    assert rec["tape_print_stale"] is False

    # 2) STALE tape (the measured TPET ingest lag): no accel reaches the exit at all.
    box["value"] = _tape(accel=-5555.0, age_s=900.5)
    seen2: list[dict] = []
    _run_tick(db, sess, symbol=sym, reversal_calls=seen2)
    assert seen2
    assert seen2[-1]["signed_tape_accel"] is None, "a day-old tape is not an exit input"
    rec2 = _events(db, sess, "live_tape_accel_reversal_exit")[-1]
    assert rec2["tape_print_stale"] is True
    # and the stale value must not become the NEXT tick's `prev` (the rollover memory)
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}
    assert le.get("prev_signed_tape_accel") == pytest.approx(-9898.0)  # from tick 1 only


# ── 8. the risk-ADDING effect is suppressed WHERE THE ADD IS DECIDED ────────────

def test_the_add_lift_suppression_is_receipted_at_the_add_site(
    db, monkeypatch, tape_calls
) -> None:
    """FINDING 9 — `_G4_GRIND_ADD_LIFT_ACTIVE` ships off (defensible: it is the only
    risk-ADDING effect and has never been exercised), but the `grind_add_lift` binding
    was attached only to the `g4_grind_mode` state-change payload. At the add site the
    cap was silently the base cap with nothing in the book saying grind was active and
    the lift was suppressed — so the day it is opened there is no before/after to
    compare. The suppression is now recorded where the add is decided."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", True)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4D-USD"
    sess = _seed(db, symbol=sym, tick_active=True)
    pos = _run_tick(db, sess, symbol=sym)

    assert pos.get("g4_grind_active") is True
    sup = _events(db, sess, "g4_grind_add_lift_suppressed")
    assert sup, "grind active + lift off must leave a row at the add site"
    s = sup[-1]
    assert s["grind_active"] is True
    assert s["base_max_adds"] == s["effective_max_adds"]   # byte-identical cap
    assert s["grind_add_lift"] == _G4_GRIND_ADD_LIFT_BINDING
