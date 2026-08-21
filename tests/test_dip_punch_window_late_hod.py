"""2026-08-21 flush_dip_buy audit fixes — tatlong pure dip-family helper (walang I/O).

Audit: 39 fires → 33 pending_place → 2 trades LANG. Mga pumatay post-fire:
(a) one-shot wide_bbo_spread demote (kasama ang phantom 3131bps book ng WYHG
session 14675, 08-20 18:41Z), (b) live_entry_wait_late_window ×80 (18:41Z =
14:41 ET late band), (c) FSM regression sa trigger_wait kung saan nabubulok ang
flush setup. Ang WYHG tape ang measured fixture: ramp 18:05-18:25Z → 5.88 (HOD
6.04), flush → 5.21, tapos VERTICAL 19:00Z → 6.92 — ang na-miss na leg.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural.entry_gates import (
    _dip_bid_stack_tilt_mult,
    dip_punch_window_seconds,
    late_window_dip_fresh_hod_mult,
)


# ── punch-window retry hold length (ATR-scaled, [60, 120]) ─────────────────────────


def _punch(**kw):
    base = dict(
        atr_pct=0.01,
        enabled=True,
        min_seconds=60.0,
        max_seconds=120.0,
        ref_atr_pct=0.01,
    )
    base.update(kw)
    return dip_punch_window_seconds(**base)


def test_punch_calm_name_holds_longest():
    # atr ≤ 1× ref → buong max window (mabagal mabulok ang structure).
    assert _punch(atr_pct=0.005) == 120.0
    assert _punch(atr_pct=0.01) == 120.0


def test_punch_violent_tape_holds_shortest():
    # atr ≥ 3× ref → min window (mabilis mapawalang-bisa ang setup).
    assert _punch(atr_pct=0.03) == pytest.approx(60.0)
    assert _punch(atr_pct=0.10) == pytest.approx(60.0)


def test_punch_midband_interpolates():
    # 2× ref = kalagitnaan ng 1×..3× band → kalagitnaan ng [60, 120].
    assert _punch(atr_pct=0.02) == pytest.approx(90.0)


def test_punch_missing_atr_uses_ref_base():
    # walang ATR → ang 1% ref base ng dip-family vocabulary → max window.
    assert _punch(atr_pct=None) == 120.0
    assert _punch(atr_pct=0.0) == 120.0


def test_punch_disabled_or_bad_input_means_no_hold():
    assert _punch(enabled=False) == 0.0
    assert _punch(ref_atr_pct=0.0) == 0.0
    assert _punch(atr_pct="garbage") == 0.0


def test_punch_min_above_max_normalizes():
    # degenerate knobs: min ≥ max → collapse sa min (walang negatibong window).
    assert _punch(min_seconds=120.0, max_seconds=60.0, atr_pct=0.05) == 120.0


# ── late-window dip fresh-HOD placement mult ───────────────────────────────────────


def _hod(**kw):
    # WYHG 08-20 measured geometry sa 18:41Z fire: recent 4-bar high 5.74 (ang
    # 18:20-18:25Z pre-flush leg), session high 6.04, frame ATR% ~4.5% → slack =
    # min(0.25, max(0.10, 0.045)) = 0.10 → threshold 0.90; proximity 0.9503 → bukas.
    base = dict(
        window="late",
        trigger_reason="flush_dip_buy",
        recent_high=5.74,
        session_high=6.04,
        atr_pct=0.045,
        enabled=True,
        hod_slack_base_frac=0.10,
        hod_slack_atr_k=1.0,
        hod_slack_max_frac=0.25,
        dip_mult=0.5,
    )
    base.update(kw)
    return late_window_dip_fresh_hod_mult(**base)


def test_wyhg_flush_dip_fresh_hod_opens_at_half_size():
    m, dbg = _hod()
    assert m == 0.5 and dbg["opened"] is True
    assert dbg["threshold"] == pytest.approx(0.90)


def test_afterhours_band_also_in_scope():
    m, dbg = _hod(window="afterhours")
    assert m == 0.5 and dbg["opened"] is True


def test_atr_widens_slack_only_for_violent_tape():
    # atr 18% → slack = min(0.25, max(0.10, 0.18)) = 0.18 → threshold 0.82:
    # ang 0.85 proximity na sarado sa base slack ay bukas sa hyper-volatile tape.
    m, dbg = _hod(atr_pct=0.18, recent_high=5.134, session_high=6.04)
    assert m == 0.5 and dbg["threshold"] == pytest.approx(0.82)


def test_max_frac_caps_the_atr_slack():
    # atr 60% → k×atr 0.60 pero ang cap 0.25 ang namamahala: threshold 0.75 —
    # ang 0.70 proximity (malayong backside) ay sarado pa rin kahit ATR-monster.
    m, dbg = _hod(atr_pct=0.60, recent_high=4.228, session_high=6.04)
    assert m == 0.0 and dbg["reject"] == "hod_not_fresh"
    assert dbg["threshold"] == pytest.approx(0.75)


def test_stale_afternoon_name_stays_blocked():
    # backside na hapon: recent high 5.10 vs HOD 6.04 (84.4% < 90%) → sarado — ito
    # ang 1W/11L AH-loss class na hindi dapat mabuksan ng dip exemption.
    m, dbg = _hod(recent_high=5.10)
    assert m == 0.0 and dbg["reject"] == "hod_not_fresh"


def test_non_dip_trigger_stays_blocked():
    for trig in ("abcd_break", "momentum_ok_tick_stream", "hod_break", None):
        m, dbg = _hod(trigger_reason=trig)
        assert m == 0.0 and dbg["reject"] == "non_dip_trigger", trig


def test_all_dip_family_triggers_open():
    # kasama ang double_bottom pair: mula #1093, ang flush-low retest ay pumuputok
    # bilang double_bottom_break(_tick_ok) — dip-buy ang semantiko (stop = flush low).
    for trig in (
        "flush_dip_buy", "vwap_reclaim", "wick_reclaim",
        "ask_thins_dip", "ask_thins_dip_tick",
        "sub_vwap_trap", "sub_vwap_trap_tick",
        "double_bottom_break", "double_bottom_break_tick_ok",
    ):
        m, _ = _hod(trigger_reason=trig)
        assert m == 0.5, trig


def test_only_late_ah_windows_are_in_scope():
    for win in ("hot", "midday", "unknown", "", None):
        m, dbg = _hod(window=win)
        assert m == 0.0 and dbg["reject"] == "not_late_ah_window", win


def test_flag_off_and_bad_inputs_fail_toward_legacy():
    assert _hod(enabled=False)[0] == 0.0
    for kw in (
        {"recent_high": None},
        {"session_high": None},
        {"session_high": 0.0},
        {"dip_mult": 0.0},
        {"recent_high": "garbage"},
    ):
        m, _ = _hod(**kw)
        assert m == 0.0, kw


def test_missing_atr_falls_to_base_slack():
    # walang ATR → 1% family base → slack = max(0.10, 0.01) = 0.10 pa rin (ang base
    # slack ang FLOOR); ang WYHG geometry ay bukas pa rin.
    m, dbg = _hod(atr_pct=None)
    assert m == 0.5 and dbg["threshold"] == pytest.approx(0.90)


def test_mult_knob_binds_verbatim():
    m, _ = _hod(dip_mult=0.25)
    assert m == 0.25


# ── L2 bid-stack confirm tilt (B2 kabilang kalahati) ───────────────────────────────


def _tilt(imbalance5, **settings_kw):
    cfg = dict(
        chili_momentum_dip_bid_stack_tilt_enabled=True,
        chili_momentum_dip_bid_stack_tilt_max_boost=0.25,
    )
    cfg.update(settings_kw)
    return _dip_bid_stack_tilt_mult(
        imbalance5=imbalance5, settings_obj=SimpleNamespace(**cfg)
    )


def test_tilt_neutral_below_measured_threshold():
    # ang +0.4 threshold ay ang sinukat na B2 |imbalance5| — sa o ilalim nito, 1.0.
    for imb in (None, -0.9, 0.0, 0.39, 0.4):
        assert _tilt(imb) == 1.0, imb


def test_tilt_interpolates_to_metric_bound():
    # +0.7 = kalagitnaan ng (0.4, 1.0] band → kalahati ng max_boost.
    assert _tilt(0.7) == pytest.approx(1.125)
    # +1.0 (fully bid-stacked — ang sariling bound ng metric) → buong boost.
    assert _tilt(1.0) == pytest.approx(1.25)


def test_tilt_never_below_one_and_boost_clamped():
    assert _tilt(2.0) == pytest.approx(1.25)  # frac clamped sa 1.0
    # max_boost clamp sa 0.5 (kapareho ng dip-velocity convention).
    assert _tilt(1.0, chili_momentum_dip_bid_stack_tilt_max_boost=5.0) == pytest.approx(1.5)


def test_tilt_flag_off_and_bad_input_fail_neutral():
    assert _tilt(0.9, chili_momentum_dip_bid_stack_tilt_enabled=False) == 1.0
    assert _tilt("garbage") == 1.0
    assert _tilt(float("nan")) == 1.0
