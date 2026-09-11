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
    """A `signed_tape_accel_features` return whose newest print is `age_s` old."""
    ref = now or datetime.now(timezone.utc)
    last_ts = ref.timestamp() - float(age_s)
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
          hwm: float = _BID):
    """A held LIVE_TRAILING session with the G4 bar anchors warm (crypto product id so
    nothing but the stub can reach the tape)."""
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
        "high_water_mark": hwm, "stop_price": 10.20, "target_price": 12.00,
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


def _run_tick(db, sess, *, symbol: str, bid: float = _BID, ask: float | None = None,
              topping_tail: bool = False):
    import app.services.trading.momentum_neural.live_runner as lr
    from app.services.trading.momentum_neural.live_runner import tick_live_session
    from tests.test_momentum_pyramid import _mk_held_adapter

    ad = _mk_held_adapter(symbol, bid=bid, ask=(ask if ask is not None else bid + 0.01))
    inert = {"fired": False, "new_stop_floor": None}
    stack = [
        patch.object(lr, "ofi_exhaustion_lock", return_value=dict(inert)),
        patch.object(lr, "tape_accel_reversal_exit", return_value=dict(inert)),
        patch.object(lr, "measured_move_exit_enabled", return_value=False),
        patch("app.services.trading.momentum_neural.paper_execution."
              "sell_into_strength_ladder", return_value=dict(inert)),
        patch("app.services.trading.momentum_neural.pipeline._live_ofi_microprice",
              return_value=(0.9, 1.0)),
        patch.object(lr, "_venue_broker_connected", return_value=True),
        patch.object(lr, "is_kill_switch_active", return_value=False),
        patch("app.services.trading.momentum_neural.candles.topping_tail_from_df",
              return_value=bool(topping_tail)),
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


@pytest.mark.parametrize("scale", [1.0, 400.0])
def test_the_verdict_does_not_move_when_wall_time_is_stretched(scale: float) -> None:
    """CLOCK INDEPENDENCE. A window counted in PRINTS must read the same tape the same
    way whether those prints took 2 seconds or 13 minutes. The same 40 prints, 400x
    apart in wall time, must produce the same grind verdict."""
    feats = _signed_tape_features(
        _rows(scale), window_s=15.0, tick_rate_floor_pctile=0.0, window_mode="prints",
    )
    assert feats is not None
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

    g4_calls = [c for c in calls if "as_of" in c]
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


def test_a_slow_name_carries_its_own_age_scale(db, monkeypatch, tape_calls) -> None:
    """The bound is not a human clock: a name whose OWN inter-print gap p99 is 120 s is
    not refused on a 60-second-old print (max(14.69, 120) = 120)."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    calls, box = tape_calls
    box["value"] = _tape(age_s=60.0, gap_p99=120.0)
    sym = "G4L-USD"
    sess = _seed(db, symbol=sym)
    pos = _run_tick(db, sess, symbol=sym)

    assert pos.get("g4_grind_active") is True
    r = _events(db, sess, "g4_grind_mode")[-1]
    assert r["basis"] == "tick"
    assert r["tape_max_print_age_s"] == pytest.approx(120.0)
    assert r["tape_gap_p99_s"] == pytest.approx(120.0)


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

def test_topping_tail_arms_the_tick_exit_even_while_grinding(
    db, monkeypatch, tape_calls
) -> None:
    """The 2026-07 grind branch said a topping tail must not full-flatten the day leader
    mid-grind. That branch has never run, and by the time [26] makes it reachable it is
    OBSOLETE: [21] already replaced the full-flatten with ARMING the tick exit, so the
    grind branch would be strictly LOOSER than the non-grind one (neither exiting nor
    arming — nothing answering the candle). Both paths now arm; grind's only effect in
    this PR is the passive-trail clamp."""
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_momentum_pyramid_enabled", False)
    monkeypatch.setattr(settings, "chili_momentum_exit_topping_tail_enabled", True)
    calls, box = tape_calls
    box["value"] = _tape()
    sym = "G4T-USD"
    sess = _seed(db, symbol=sym, tick_active=True)
    _run_tick(db, sess, symbol=sym, topping_tail=True)

    held = _events(db, sess, "g4_grind_hold_topping_tail")
    assert held, "the grind receipt must still record the candle"
    assert held[-1]["arms_tick_exit"] is True
    db.refresh(sess)
    le = (sess.risk_snapshot_json or {}).get("momentum_live_execution") or {}
    armed = le.get("opinion_exit_armed") or {}
    assert armed, "grind must not swallow the candle: the tick exit has to be armed"
    assert "topping_tail_runner_exit" in (armed.get("reasons") or [])
