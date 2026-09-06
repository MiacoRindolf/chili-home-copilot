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
    # reclaimed: last at least one noise band ABOVE the failed leg's level
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=1,
        last=6.30, reclaim_level=6.00, noise_abs=0.245,
    ) == (True, "lockout_watch_front_side_exempt")
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=True, exemptions_used=1, max_exemptions=1,
        last=6.30, reclaim_level=6.00, noise_abs=0.245,
    ) == (False, "lockout_watch_budget_spent")
    # a touch of the level (inside the band) is not a reclaim
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=1,
        last=6.20, reclaim_level=6.00, noise_abs=0.245,
    ) == (False, "lockout_watch_not_reclaimed")


# ── v4 RECLAIM LEVEL (2026-09-06, measured on the 12 lockout receipts of the gate-15
# baseline @ 9383324b2; scratchpad/lockout_reclaim_study.py). The level is the FAILED
# LEG's own max(entry, high-water mark); the session high (v3) was the wrong level.
_MEASURED_V4_LOCKOUTS = [
    # (case, leg entry, leg hwm, post-lock print that reclaims (or the post-lock HIGH when
    #  it never does), Ross outcome, expected)
    ("FCUV 07-31 RH", 7.50, 7.3604, 7.54, "winner: MFE +34% in 322 s", True),
    ("WETO 08-14 RH", 9.50, 9.49, 9.57, "winner: MFE +35% in 717 s", True),
    ("ILLR 06-25 ml1 RH", 0.924, 0.9159, 0.9387, "winner: MFE +320%", True),
    ("VEEE 07-13 alpaca", 10.83, 10.94, 11.07, "winner: MFE +29% (dd -1%)", True),
    ("EZRA 08-03 ml2 alpaca", 2.87, 2.92, 2.9397, "winner: MFE +28%", True),
    ("JWEL 08-10 RH", 5.57, 5.20, 5.45, "knife: post-lock HIGH 5.45 < level 5.57", False),
    ("DSY 08-07 alpaca (Ross -$52k)", 6.90, 7.30, 6.27, "knife: post-lock HIGH 6.27 < 7.30", False),
]


def test_v4_the_failed_leg_level_separates_the_winners_from_the_knives():
    for case, entry, hwm, last, why, expected in _MEASURED_V4_LOCKOUTS:
        level = max(entry, hwm)
        band = 0.003 * level  # the study's margin; the live band is the name's own 30-s range
        allowed, reason = symbol_day_lockout_watch_reentry(
            watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=1,
            last=last, reclaim_level=level, noise_abs=band,
        )
        assert allowed is expected, (case, why, reason)
        assert reason == ("lockout_watch_front_side_exempt" if expected else "lockout_watch_not_reclaimed"), case


def test_v4_the_session_high_would_have_bought_the_top():
    # WETO 08-14: v3's basis (session high 12.95, reached 09:56:59) = the day's top, -33% after.
    # The failed-leg level (9.50) had already reclaimed at 09:45:02 (+155 s), MFE +35%.
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=1,
        last=9.57, reclaim_level=9.50, noise_abs=0.03,
    ) == (True, "lockout_watch_front_side_exempt")
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=1,
        last=9.57, reclaim_level=12.95, noise_abs=0.03,
    ) == (False, "lockout_watch_not_reclaimed")


def test_v3_missing_reclaim_basis_fails_closed():
    for kw in ({"last": None, "reclaim_level": 6.25, "noise_abs": 0.2},
               {"last": 6.2, "reclaim_level": None, "noise_abs": 0.2},
               {"last": 6.2, "reclaim_level": 6.25, "noise_abs": None},
               {"last": float("nan"), "reclaim_level": 6.25, "noise_abs": 0.2},
               {"last": 6.2, "reclaim_level": 6.25, "noise_abs": -0.1},
               {"last": "x", "reclaim_level": 6.25, "noise_abs": 0.2}):
        assert symbol_day_lockout_watch_reentry(
            watch_active=True, tape_ok=True, exemptions_used=0, max_exemptions=1, **kw
        ) == (False, "lockout_watch_no_reclaim_basis"), kw
    # the order of refusals: budget, then tape, then reclaim
    assert symbol_day_lockout_watch_reentry(
        watch_active=True, tape_ok=False, exemptions_used=0, max_exemptions=1
    ) == (False, "lockout_watch_no_buyers_on_tape")


def test_v4_gate_reads_the_frozen_leg_level_and_the_noise_band_from_the_own_tape():
    src = _tick_source()
    i = src.find('_ldw = le.get("symbol_day_lockout_watch")')
    window = src[i:i + 5200]
    assert '_level = _float_or_none(_w.get("reclaim_level"))' in window
    assert "_own_tape_noise_floor_pct(db, sess.symbol, entry_price=_last)" in window
    assert "reclaim_level=_level" in window and "noise_abs=_band" in window
    assert "session_high=" not in window  # v3's basis is observability only now
    module_src = inspect.getsource(lr)
    # the level is frozen into the watch marker at lock time from the failed leg's record
    j = module_src.find('le["symbol_day_lockout_watch"] = {')
    assert j > 0
    marker = module_src[j - 1500:j + 900]
    assert '_l13_leg_entry = _float_or_none(_l13_prior.get("entry_price"))' in marker
    assert '_l13_leg_hwm = _float_or_none(_l13_prior.get("high_water_mark"))' in marker
    assert '"reclaim_level": _l13_level,' in marker
    # and the failed leg's record carries its entry (v4 addition next to the HWM)
    k = module_src.find('le["g4_prior_trade"] = {')
    assert '"entry_price": _float_or_none(entry_price),' in module_src[k:k + 600]


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
    window = src[i_l13:i_l13 + 6500]  # v4 froze the failed-leg level into the marker (longer block)
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


# ── v4c (2026-09-06, first v4 A/B): the re-entry's stop is STRUCTURAL, under the reclaimed level ─
from app.services.trading.momentum_neural.risk_policy import (  # noqa: E402
    lockout_reentry_structural_stop,
)


def test_v4c_the_reentry_stop_sits_one_noise_band_under_the_reclaimed_level():
    # EZRA 08-03 ml2 alpaca: level 2.90, band 0.15 -> 2.75; the retest low was 2.89 (the leg
    # died there on a 3.9% vol-floored stop before the +28% run)
    assert lockout_reentry_structural_stop(2.90, 0.15) == 2.75
    assert lockout_reentry_structural_stop(2.90, 0.15, bands=2.0) == 2.60
    for bad in ((None, 0.15), (2.9, None), (float("nan"), 0.15), (2.9, -0.1), (0.0, 0.15), ("x", 0.15)):
        assert lockout_reentry_structural_stop(*bad) is None, bad
    assert lockout_reentry_structural_stop(0.10, 0.15) is None  # a stop at/below zero is no stop


def test_v4d_one_permit_for_every_fire_path_grant_is_not_spend_and_the_fill_spends():
    src = _tick_source()
    i = src.find("def _ldw_permit(_fire_reason: str, _fire_px: Any) -> bool:")
    assert i > 0
    permit = src[i:i + 5200]
    # the gate: reclaim + band + buyers + budget, structural stop stashed on grant, marker KEPT
    assert "symbol_day_lockout_watch_reentry as _w_decide" in permit
    assert "_stop = _w_stop_fn(_level, _band)" in permit
    assert 'le["lockout_reentry_structural_stop"] = round(float(_stop), 6)' in permit
    assert '_w["granted_at_utc"] = _utcnow().isoformat()' in permit
    assert 'le["symbol_day_lockout_watch"] = _w  # stays until the fill spends the budget' in permit
    assert 'le.pop("symbol_day_lockout_watch", None)' not in permit
    assert '"lockout_front_side_exemptions"] = _used + 1' not in permit
    # a hold clears the stash so it can never reach an unrelated later fire
    assert 'if le.pop("lockout_reentry_structural_stop", None) is not None:' in permit
    assert "_own_tape_session_high(" not in permit  # the v3 whole-session scan is gone from the gate
    # the ladder call never runs on the score_only placeholder of a non-admissible session
    j = src.find('and _trigger_reason != "score_only"', i)
    assert j > 0 and "_score_ok" in src[j - 200:j] and "if not _ldw_permit(_trigger_reason, _ldw_px):" in src[j:j + 500]
    # every alternate fire path asks the same permit and applies the same stop helper
    assert 'if _th_struct_ok and _ldw_permit("tape_confirmed_hold", _th_px):' in src
    assert 'if _mc_tape_ok and _ldw_permit(' in src
    assert src.count("_apply_lockout_reentry_stop(le)") == 3
    module_src = inspect.getsource(lr)
    # the FILL spends the budget and pops the marker; the stash is cleared there too
    k = module_src.find('le["entry_filled_at_utc"] = _entry_filled_at_utc')
    fill = module_src[k:k + 1200]
    assert 'le.pop("lockout_reentry_structural_stop", None)' in fill
    assert '_ldw_spent = le.pop("symbol_day_lockout_watch", None)' in fill
    assert '"live_lockout_watch_exemption_spent"' in fill
    for key in ("structural_stop_source", "structural_stop_price", "lockout_reentry_structural_stop"):
        assert key in lr._RECYCLE_ENTRY_STATE_KEYS, key
    assert "symbol_day_lockout_watch" not in lr._RECYCLE_ENTRY_STATE_KEYS  # the lock survives a recycle


def test_v4d_the_stop_helper_only_widens_and_stamps_provenance():
    le = {"lockout_reentry_structural_stop": 2.75, "structural_stop_price": 2.89}
    lr._apply_lockout_reentry_stop(le)
    assert le["structural_stop_price"] == 2.75 and le["structural_stop_source"] == "lockout_reclaim_level_minus_noise"
    le = {"lockout_reentry_structural_stop": 2.95, "structural_stop_price": 2.89}
    lr._apply_lockout_reentry_stop(le)
    assert le["structural_stop_price"] == 2.89 and le["structural_stop_source"] == "trigger_pullback_low"
    le = {"lockout_reentry_structural_stop": 2.75}
    lr._apply_lockout_reentry_stop(le)
    assert le["structural_stop_price"] == 2.75
    le = {"structural_stop_price": 2.89, "structural_stop_source": "stale"}
    lr._apply_lockout_reentry_stop(le)
    assert le["structural_stop_price"] == 2.89 and "structural_stop_source" not in le
