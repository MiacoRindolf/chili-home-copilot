"""Halt-aware tape accel + majority-buy confirm — XPON 2026-08-26 forensics.

Dalawang sinukat na poisoning mode ng ``signed_tape_accel`` sa paligid ng LULD
halt (XPON 13:50:29→13:55:29, tapos +17% sa 69s):
  A) window na tumatawid sa gap: pinaghahambing ang pre-halt vs post-resume na
     tape na parang tuloy-tuloy;
  B) burst decay: ang reopening cross + unang burst seconds ay nasa FRONT half
     ng bawat susunod na window, kaya ang back−front sa hilaw na volume ay
     garantisadong malaking negatibo (−142k..−175k) habang ang presyo ay
     rumirip sa at-ask na pagbili — 21 sunod-sunod na tape_not_confirming.

Mga fix na tinitiyak dito:
  1) gap restriction: internal gap > window/2 ⇒ ang tuloy-tuloy na segment
     pagkatapos ng huling gap lamang ang sinusukat;
  2) back_buy_share/front_buy_share sa features output;
  3) reentry_escalation_decision: accel<=0 ay humaharang lamang kapag HINDI
     buyer-dominado ang back half (share > 0.5 = confirming).

Runnable: pytest tests/test_tape_accel_halt_aware.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.entry_gates import (
    _signed_tape_features,
)
from app.services.trading.momentum_neural.risk_policy import (
    reentry_escalation_decision,
)


T0 = 1_700_000_000.0  # arbitraryong epoch base; relative offsets ang mahalaga


def _tick(off_s, px, sz, bid, ask):
    return (px, sz, bid, ask, T0 + off_s)


def test_gap_straddling_window_measures_only_post_gap_segment():
    # Pre-halt: 3 malalaking SELL (sa bid). 8s gap (> 15/2). Post-resume:
    # 4 na BUY (sa ask). Kung walang restriction ang front half ay ang mga
    # pre-halt sell; may restriction, ang post-gap segment lamang.
    rows = [
        _tick(0.0, 8.66, 5000, 8.66, 8.67),
        _tick(0.5, 8.66, 4000, 8.66, 8.67),
        _tick(1.0, 8.66, 3000, 8.66, 8.67),
        # --- 8s na katahimikan (halt) ---
        _tick(9.0, 8.67, 1000, 8.66, 8.67),
        _tick(10.0, 8.70, 900, 8.69, 8.70),
        _tick(11.0, 8.75, 800, 8.74, 8.75),
        _tick(12.0, 8.80, 700, 8.79, 8.80),
    ]
    out = _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert out is not None
    assert out["gap_restricted"] is True
    assert out["n_ticks"] == 4  # ang post-gap segment lamang
    # Lahat ng post-gap ay buy: parehong halves buyer-dominado.
    assert out["back_buy_share"] == 1.0


def test_no_gap_window_is_not_restricted_and_matches_old_accel():
    rows = [
        _tick(0.0, 5.00, 100, 4.99, 5.00),
        _tick(3.0, 5.01, 100, 5.00, 5.01),
        _tick(6.0, 5.02, 100, 5.01, 5.02),
        _tick(9.0, 5.03, 300, 5.02, 5.03),
        _tick(12.0, 5.04, 300, 5.03, 5.04),
    ]
    out = _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert out is not None
    assert out["gap_restricted"] is False
    # front (0,3) buy=200; back (6,9,12) buy=700 ⇒ accel +500 (di nagbago).
    assert out["signed_tape_accel"] == 500.0
    assert out["back_buy_share"] == 1.0


def test_post_gap_segment_too_thin_fails_open_none():
    rows = [
        _tick(0.0, 8.66, 5000, 8.66, 8.67),
        _tick(0.5, 8.66, 4000, 8.66, 8.67),
        _tick(1.0, 8.66, 3000, 8.66, 8.67),
        _tick(9.0, 8.67, 1000, 8.66, 8.67),
        _tick(10.0, 8.70, 900, 8.69, 8.70),
    ]
    # 2 ticks lamang pagkatapos ng gap ⇒ None (existing fail-open contract).
    assert _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0) is None


def test_burst_decay_reads_negative_accel_but_full_buy_share():
    # Ang XPON mode-B shape: napakalaking buying sa front (resume cross +
    # burst), bumabagal pero buyer-dominado pa rin sa back.
    rows = [
        _tick(0.0, 8.67, 44000, 8.66, 8.67),   # reopening cross (at ask)
        _tick(1.0, 9.00, 20000, 8.99, 9.00),
        _tick(2.0, 9.20, 15000, 9.19, 9.20),
        _tick(9.0, 9.60, 4000, 9.59, 9.60),
        _tick(11.0, 9.80, 3000, 9.79, 9.80),
        _tick(13.0, 10.00, 2000, 9.99, 10.00),
    ]
    out = _signed_tape_features(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert out is not None
    assert out["signed_tape_accel"] < 0  # ito ang lumang poison
    assert out["back_buy_share"] == 1.0  # pero buyer ang MAY HAWAK ng tape


def _decision(**over):
    kw = dict(
        enabled=True,
        escalation_level=2,
        structural_trigger=True,
        live_price=8.67,
        prior_hwm=8.31,
        prior_exit_price=8.21,
        prior_risk_dist=0.124,
        tape_accel=-142_008.0,
        is_day_leader=None,
    )
    kw.update(over)
    return reentry_escalation_decision(**kw)


def test_escalation_negative_accel_majority_buy_confirms():
    # Ang eksaktong XPON post-halt case: reclaim pasado (8.67 > 8.434),
    # accel −142k, pero back half buyer-dominado ⇒ payagan.
    allowed, dbg = _decision(tape_back_buy_share=0.92)
    assert allowed is True
    assert dbg["reason"] == "tape_majority_buy_confirms"


def test_escalation_negative_accel_seller_tape_still_blocks():
    allowed, dbg = _decision(tape_back_buy_share=0.30)
    assert allowed is False
    assert dbg["reason"] == "tape_not_confirming"


def test_escalation_negative_accel_no_share_blocks_as_before():
    allowed, dbg = _decision(tape_back_buy_share=None)
    assert allowed is False
    assert dbg["reason"] == "tape_not_confirming"


def test_escalation_positive_accel_unchanged():
    allowed, dbg = _decision(tape_accel=75_000.0, tape_back_buy_share=None)
    assert allowed is True
    assert dbg["reason"] == "reclaim_met"


def test_leader_substitute_accepts_majority_buy_tape():
    # Day-leader na may non-structural trigger: ang substitute ay humihingi ng
    # positibong tape + tunay na reclaim — ang majority-buy ay dapat kuwalipikado.
    allowed, dbg = _decision(
        structural_trigger=False,
        is_day_leader=True,
        tape_accel=-50_000.0,
        tape_back_buy_share=0.85,
    )
    assert allowed is True
