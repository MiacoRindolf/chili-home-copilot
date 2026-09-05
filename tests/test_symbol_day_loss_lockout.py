"""L13 — symbol-day loss lockout (2026-08-09, canon-v3 autopsy).

Ang ebidensya: VTAK −748 / CWD −309 / LHSW −252 = 84% ng gross red ng canon;
empirical sweep sa 16 fill-complete windows: +291.60 net sa K=1.5, tatlong
sakuna window lang ang natatamaan, zero epekto sa greens (13x margin).
"""
import inspect

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.risk_policy import (
    symbol_day_loss_lockout_decision,
)


# ── Pure helper ──────────────────────────────────────────────────────────────


def test_flag_off_ay_byte_identical_legacy():
    locked, reason, thr = symbol_day_loss_lockout_decision(
        enabled=False,
        day_net_realized_usd=-10_000.0,
        max_loss_per_trade_usd=130.0,
        r_multiple=1.5,
    )
    assert (locked, reason, thr) == (False, "flag_off", None)


def test_lockout_sa_empirical_na_vtak_point():
    # VTAK cycle 9: cum −508.20 sa R=130, K=1.5 → threshold 195 → LOCKED.
    locked, reason, thr = symbol_day_loss_lockout_decision(
        enabled=True,
        day_net_realized_usd=-508.20,
        max_loss_per_trade_usd=130.0,
        r_multiple=1.5,
    )
    assert locked is True
    assert reason == "symbol_day_loss_lockout"
    assert thr == 195.0


def test_hindi_naka_lock_ang_pinakamalalim_na_recovery_window():
    # TNMG bottom −131.20 (−1.01R) ang pinakamalalim na nakabawi sa 16-window
    # sweep — dapat HINDI ito tamaan ng K=1.5 (kaya 1.5 ang floor).
    locked, reason, thr = symbol_day_loss_lockout_decision(
        enabled=True,
        day_net_realized_usd=-131.20,
        max_loss_per_trade_usd=130.0,
        r_multiple=1.5,
    )
    assert locked is False
    assert reason == "above_lockout_threshold"
    assert thr == 195.0


def test_eksaktong_threshold_ay_naka_lock():
    locked, _, _ = symbol_day_loss_lockout_decision(
        enabled=True,
        day_net_realized_usd=-195.0,
        max_loss_per_trade_usd=130.0,
        r_multiple=1.5,
    )
    assert locked is True


def test_green_day_ay_hindi_kailanman_naka_lock():
    locked, reason, _ = symbol_day_loss_lockout_decision(
        enabled=True,
        day_net_realized_usd=99.56,
        max_loss_per_trade_usd=130.0,
        r_multiple=1.5,
    )
    assert locked is False


def test_walang_basis_ay_fail_open():
    for basis in (None, 0.0, -5.0):
        locked, reason, thr = symbol_day_loss_lockout_decision(
            enabled=True,
            day_net_realized_usd=-10_000.0,
            max_loss_per_trade_usd=basis,
            r_multiple=1.5,
        )
        assert locked is False, basis
        assert thr is None
    locked, reason, _ = symbol_day_loss_lockout_decision(
        enabled=True,
        day_net_realized_usd=-10_000.0,
        max_loss_per_trade_usd=130.0,
        r_multiple=0.0,
    )
    assert locked is False


def test_sirang_input_ay_fail_open():
    locked, reason, _ = symbol_day_loss_lockout_decision(
        enabled=True,
        day_net_realized_usd=float("nan"),
        max_loss_per_trade_usd=130.0,
        r_multiple=1.5,
    )
    assert locked is False
    assert reason == "bad_basis_fail_open"


def test_threshold_ay_umaangkop_sa_equity():
    # Adaptive by construction: mas malaking account (per-trade cap 1300) ⇒ mas
    # malalim ang lockout point — walang absolute dollar magic number.
    locked_maliit, _, thr_maliit = symbol_day_loss_lockout_decision(
        enabled=True,
        day_net_realized_usd=-300.0,
        max_loss_per_trade_usd=130.0,
        r_multiple=1.5,
    )
    locked_malaki, _, thr_malaki = symbol_day_loss_lockout_decision(
        enabled=True,
        day_net_realized_usd=-300.0,
        max_loss_per_trade_usd=1300.0,
        r_multiple=1.5,
    )
    assert locked_maliit is True and thr_maliit == 195.0
    assert locked_malaki is False and thr_malaki == 1950.0


# ── Istrukturang bantay sa call site ────────────────────────────────────────


def _tick_source() -> str:
    return inspect.getsource(lr.tick_live_session)


def test_ang_lockout_ay_pagkatapos_ng_mga_exemption_at_hindi_nalalampasan():
    """Ang L13 block ay dapat TUMAKBO PAGKATAPOS ng leader at ignition
    exemption blocks (para hindi siya ma-rescue ng mga iyon) at BAGO ang
    terminal na `if not _re_ok:`."""
    src = _tick_source()
    i_leader = src.find("live_reentry_cap_leader_exempt")
    i_ignition = src.find("live_reentry_cap_ignition_exempt")
    i_l13 = src.find("symbol_day_loss_lockout_decision(")
    i_l13_emit = src.find("live_symbol_day_loss_lockout")
    assert 0 < i_leader < i_l13, "L13 dapat pagkatapos ng leader exemption"
    assert 0 < i_ignition < i_l13, "L13 dapat pagkatapos ng ignition exemption"
    assert 0 < i_l13 < i_l13_emit, "may dedicated emit ang lockout"


def test_ang_lockout_flag_getattr_fallback_ay_true():
    src = _tick_source()
    assert (
        '"chili_momentum_symbol_day_loss_lockout_enabled", True' in src
    ), "ang getattr fallback ay dapat True (roster doctrine)"


def test_ang_lockout_ay_gumagamit_ng_day_net_hindi_session_lang():
    """Ang basis ay dapat session ledger + other-sessions banked sum (ang
    parehong read na ginagamit ng g4 green_banked) — hindi lang ang lokal na
    session PnL."""
    src = _tick_source()
    i_l13 = src.find("symbol_day_loss_lockout_decision(")
    window = src[i_l13 - 2500 : i_l13]
    assert "symbol_day_banked_pnl_other_sessions" in window


# ── Lockout WATCH (v2, 2026-09-05, Ross Parity Bench) ────────────────────────
from app.services.trading.momentum_neural.risk_policy import (  # noqa: E402
    symbol_day_lockout_watch_reentry,
)


def test_watch_inactive_ay_walang_epekto():
    assert symbol_day_lockout_watch_reentry(
        watch_active=False, tape_ok=False, exemptions_used=0, max_exemptions=1
    ) == (True, "no_lockout_watch")


def test_watch_na_may_buyers_budget_at_reclaim_ay_pumapasok_isang_beses():
    # reclaimed: last within one noise band of the session high
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=1,
        last=6.20, session_high=6.25, noise_abs=0.245,
    ) == (True, "lockout_watch_front_side_exempt")
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=True, exemptions_used=1, max_exemptions=1,
        last=6.20, session_high=6.25, noise_abs=0.245,
    ) == (False, "lockout_watch_budget_spent")


# ── v3 RECLAIM (2026-09-05, first A/B of the watch): the four measured exemptions ─────
# Every exemption v2 granted on tape alone fired ABOVE VWAP but 14-28% BELOW the day high,
# and every one lost. Ross: "hands off until it proves itself again" = back at the high.
_MEASURED_V2_EXEMPTIONS = [
    # (case, trigger, last, vwap, session_high, leg pnl)
    ("EDBL 07-27", "sub_vwap_trap_tick", 8.14, 7.692, 9.94, -34.48),
    ("JWEL 08-10", "abcd_break_tick_ok", 5.1158, 4.343, 6.25, -18.57),
    ("EZRA 08-03 RH", "abcd_break_tick_ok", 3.17, 2.763, 3.68, -21.70),
    ("EZRA 08-03 alpaca", "momentum_ok_tick_stream", 2.65, 2.661, 3.68, -25.74),
]


def test_v3_refuses_every_measured_mid_range_exemption():
    for case, trig, last, vwap, high, pnl in _MEASURED_V2_EXEMPTIONS:
        # a generous band (4.5% of price, the JWEL 30-s median) still does not reach the high
        noise = 0.045 * last
        assert last > vwap * 0.99, case  # above VWAP: v2's implicit state was NOT the discriminator
        assert symbol_day_lockout_watch_reentry(
            watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=1,
            last=last, session_high=high, noise_abs=noise,
        ) == (False, "lockout_watch_not_reclaimed"), case


def test_v3_missing_reclaim_basis_fails_closed():
    for kw in ({"last": None, "session_high": 6.25, "noise_abs": 0.2},
               {"last": 6.2, "session_high": None, "noise_abs": 0.2},
               {"last": 6.2, "session_high": 6.25, "noise_abs": None},
               {"last": float("nan"), "session_high": 6.25, "noise_abs": 0.2},
               {"last": 6.2, "session_high": 6.25, "noise_abs": -0.1},
               {"last": "x", "session_high": 6.25, "noise_abs": 0.2}):
        assert symbol_day_lockout_watch_reentry(
            watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=1, **kw
        ) == (False, "lockout_watch_no_reclaim_basis"), kw
    # the order of refusals: budget, then tape, then reclaim
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=False, exemptions_used=0, max_exemptions=1
    ) == (False, "lockout_watch_no_buyers_on_tape")


def test_v3_gate_reads_the_session_high_and_noise_band_from_the_own_tape():
    src = _tick_source()
    i = src.find('_ldw = le.get("symbol_day_lockout_watch")')
    window = src[i:i + 3500]
    assert "_own_tape_session_high(db, sess.symbol)" in window
    assert "_own_tape_noise_floor_pct(db, sess.symbol, entry_price=_ldw_last)" in window
    assert "session_high=_ldw_high" in window and "noise_abs=_ldw_noise_abs" in window
    module_src = inspect.getsource(lr)
    assert "def _own_tape_session_high(db, symbol: str) -> float | None:" in module_src


def test_watch_na_walang_buyers_sa_tape_ay_naghihintay():
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=False, exemptions_used=0, max_exemptions=1
    ) == (False, "lockout_watch_no_buyers_on_tape")


def test_zero_budget_ay_hindi_kailanman_pumapasok():
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=0
    ) == (False, "lockout_watch_budget_spent")


def test_pure_lockout_decision_ay_byte_identical_pa_rin():
    locked, reason, thr = symbol_day_loss_lockout_decision(
        enabled=True, day_net_realized_usd=-508.20, max_loss_per_trade_usd=130.0, r_multiple=1.5,
    )
    assert (locked, reason, thr) == (True, "symbol_day_loss_lockout", 195.0)


def test_ang_l13_edge_ay_nagwa_watch_kapag_may_budget_at_terminal_kapag_ubos():
    src = _tick_source()
    i_l13 = src.find("symbol_day_loss_lockout_decision(")
    window = src[i_l13:i_l13 + 4000]
    assert "if _l13_locked and _l13_fs_used < _l13_fs_max:" in window
    assert 'le["symbol_day_lockout_watch"]' in window
    assert "live_symbol_day_loss_lockout_watch" in window
    assert "elif _l13_locked:" in window and "live_symbol_day_loss_lockout" in window


def test_ang_watch_gate_ay_nasa_candidate_edge_bago_ang_hvm101_at_pagkatapos_ng_bottom_of_range():
    src = _tick_source()
    i_bor = src.find("live_entry_bottom_of_range_veto")
    i_gate = src.find('_ldw = le.get("symbol_day_lockout_watch")')
    i_hvm = src.find("# HVM101 (B): BID-PROP / SPREAD-TIGHTENING CONFIRMER")
    i_cand = src.find("_safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)")
    assert 0 < i_bor < i_gate < i_hvm < i_cand
    gate = src[i_gate:i_hvm]  # the whole gate block, however long the reclaim basis makes it
    assert "tape_confirms_hold" in gate and "symbol_day_lockout_watch_reentry" in gate
    assert '_trigger_reason = "symbol_day_lockout_watch"' in gate
    assert "live_lockout_watch_front_side_exempt" in gate
    assert '"chili_momentum_max_ignition_exemptions", 1' in gate   # one documented budget


def test_ang_watch_marker_ay_hindi_binubura_ng_recycle_reset():
    assert "symbol_day_lockout_watch" not in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "lockout_front_side_exemptions" not in lr._RECYCLE_ENTRY_STATE_KEYS
