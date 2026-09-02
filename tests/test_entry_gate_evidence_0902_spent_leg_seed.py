"""Spent-leg seed of the g4 re-entry escalation (2026-09-02 evidence).

Every loss of 09-01/09-02 on the Alpaca PAPER lane was a SPENT-LEG entry on a
name the Warrior Trading room already knew (session HOD minutes old, percent
away when the trigger fired):

    CANF#1  HOD 4.9297 @ 07:02 ET, entry 4.34 @ 07:10  8.3 min / 11.96%  -78.13 (MFE 0.13R)
    JLHL    HOD 7.92,            entry 7.396            7.6 min /  6.61%  -18.83
    AUUD    HOD 1.23,            entry 1.11             6.9 min /  9.76%  -44.01 (bars; tape 4.1 min)
    UPC     HOD 5.60,            entry 5.396           18.4 min /  3.64%  -48.97 (documented MISS)

No existing mechanism measures HOD age x depth on the FIRST entry (bench needs
a confirmed backside shape; g4 escalation starts after a stop-out; retrace veto
is blind to a 5-12% pullback on a +30% day). So this is NOT a new gate: the
predicate SEEDS the existing g4 level 1 (the #1252 slot) with the session top
as the reclaim reference. WAIT, not veto: clears when price prints at/above the
top or the top itself moves.

Honest evidence (C67 = 67 live fills 06-12..09-02, 11 W / 56 L): the HARD-veto
form blocks 33 L / 2 W but the July half is net -2.3R (JZXN +182 = 16.4R
blocked); only the WAIT form is defensible (JZXN re-took its HOD 4 min after
entry ⇒ cleared) and only the interleaved replay A/B can price re-admissions.
Current-era entry-quality dollars uniquely prevented: CANF#1, JLHL, LIDR#2
(~-100, 3 outcomes). Cost stated, not hidden: at level 1 non-leader STRUCTURAL
pullback triggers are also blocked below the HOD (reclaim = HOD + 0).

Runnable: pytest tests/test_entry_gate_evidence_0902_spent_leg_seed.py -v
"""
from __future__ import annotations

import ast
import inspect
from datetime import datetime, timedelta, timezone

import numpy as np
import pandas as pd
import pytest

from app.config import Settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.entry_gates import (
    evaluate_sticky_backside_bench,
    session_frame_hod_debug,
)
from app.services.trading.momentum_neural.risk_policy import (
    apply_spent_leg_tick,
    reentry_escalation_decision,
    reentry_escalation_level_update,
    spent_leg_seed_decision,
)

UTC = timezone.utc
_UNSET = object()
DAY = "2026-09-02"
# 07:10 ET on 2026-09-02 (the CANF#1 entry instant)
NOW = datetime(2026, 9, 2, 11, 10, 0, tzinfo=UTC)


def _decide(*, hod, age_min, px, now=NOW, hod_date=DAY, sess_date=DAY,
            frame_age_s=0.0, coverage_gap_s=None, interval_s=60.0,
            session_start=_UNSET, observed_since=_UNSET, observed_ticks=100, **kw):
    """Predicate fixture. COLD-START GUARD (A/B 2026-09-02): unless a case is
    ABOUT that guard, the session is stated as having opened 1 min before the
    HOD's bar END and to have been ticking for 10 min — i.e. this is a HOD the
    session watched print, which is what every named fixture below assumes."""
    _hod_ts = (now or NOW) - timedelta(minutes=age_min)
    return spent_leg_seed_decision(
        cur_hod=hod,
        hod_ts=_hod_ts,
        hod_date_et=hod_date,
        live_px=px,
        now_utc=now,
        session_date_et=sess_date,
        frame_age_s=frame_age_s,
        coverage_gap_s=coverage_gap_s,
        interval_s=interval_s,
        session_start_utc=(
            _hod_ts - timedelta(minutes=1) if session_start is _UNSET else session_start
        ),
        observed_since_utc=(
            (now or NOW) - timedelta(minutes=10) if observed_since is _UNSET else observed_since
        ),
        observed_ticks=observed_ticks,
        **kw,
    )


# ── predicate: the named fixtures ─────────────────────────────────────────────

def test_canf1_seeds():
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34)
    assert seed is True and dbg["reason"] == "spent_leg_seed"
    assert 11.9 < dbg["dd_pct"] < 12.1
    assert 8.0 < dbg["hod_age_min"] < 8.6


def test_jlhl_seeds_on_tick_surge_class():
    seed, dbg = _decide(hod=7.92, age_min=7.6, px=7.396376)
    assert seed is True and 6.5 < dbg["dd_pct"] < 6.7


def test_upc_is_a_documented_miss_too_shallow():
    seed, dbg = _decide(hod=5.60, age_min=18.4, px=5.395955)
    assert seed is False and dbg["reason"] == "spent_leg_too_shallow"
    assert 3.6 < dbg["dd_pct"] < 3.7


def test_auud_is_source_dependent():
    seed_bars, _ = _decide(hod=1.23, age_min=6.9, px=1.11)
    seed_tape, dbg_tape = _decide(hod=1.23, age_min=4.1, px=1.11)
    assert seed_bars is True
    assert seed_tape is False and dbg_tape["reason"] == "spent_leg_too_young"


def test_move_and_jzxn_seed_then_are_priced_by_the_clear():
    assert _decide(hod=17.5, age_min=11.2, px=16.24)[0] is True
    assert _decide(hod=1.92, age_min=11.0, px=1.61)[0] is True  # JZXN +182: the WAIT cost


def test_veee_first_pullback_is_not_seeded_on_bars():
    seed, dbg = _decide(hod=11.0, age_min=4.7, px=9.465069)
    assert seed is False and dbg["reason"] == "spent_leg_too_young"


def test_names_at_hod_are_never_seeded():
    for hod, px in ((17.31, 17.36), (9.8997, 9.91), (1.65, 1.65)):  # SDOT / COIW / LIDR#1
        seed, dbg = _decide(hod=hod, age_min=20.0, px=px)
        assert seed is False and dbg["reason"] == "spent_leg_at_or_above_hod"


def test_live_tick_above_frame_hod_is_at_or_above():
    seed, dbg = _decide(hod=4.93, age_min=9.0, px=4.95)
    assert (seed, dbg["reason"]) == (False, "spent_leg_at_or_above_hod")


def test_stale_frame_without_tick_coverage_fails_open():
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34, frame_age_s=636.0, coverage_gap_s=None)
    assert (seed, dbg["reason"]) == (False, "spent_leg_frame_stale")
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34, frame_age_s=636.0, coverage_gap_s=500.0)
    assert (seed, dbg["reason"]) == (False, "spent_leg_frame_stale")


def test_stale_frame_with_tick_coverage_is_evaluated():
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34, frame_age_s=636.0, coverage_gap_s=0.0)
    assert seed is True
    # the threshold is max(2*interval, max_frame_age_s): a 5m frame gets 600 s.
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34, frame_age_s=500.0,
                        coverage_gap_s=None, interval_s=300.0)
    assert seed is True and dbg["stale_threshold_s"] == 600.0


def test_yesterdays_hod_never_seeds_a_gapper():
    seed, dbg = _decide(hod=4.9297, age_min=400.0, px=4.34, hod_date="2026-09-01")
    assert (seed, dbg["reason"]) == (False, "spent_leg_hod_not_today")


@pytest.mark.parametrize("bad", [
    dict(hod=None), dict(px=float("nan")), dict(hod=0.0), dict(px=-1.0),
    dict(now=None), dict(hod_date=""), dict(sess_date=None),
])
def test_unreadable_inputs_fail_open(bad):
    base = dict(hod=4.9297, age_min=8.3, px=4.34)
    base.update(bad)
    seed, dbg = _decide(**base)
    assert (seed, dbg["reason"]) == (False, "spent_leg_unreadable")


def test_bad_hod_ts_fails_open():
    seed, dbg = spent_leg_seed_decision(
        cur_hod=4.93, hod_ts="not-a-time", hod_date_et=DAY, live_px=4.34, now_utc=NOW,
        session_date_et=DAY, frame_age_s=0.0, coverage_gap_s=0.0, interval_s=60.0,
    )
    assert (seed, dbg["reason"]) == (False, "spent_leg_unreadable")


def test_iso_string_and_naive_clocks_are_read_as_utc():
    hod_ts_iso = (NOW - timedelta(minutes=8)).replace(tzinfo=None).isoformat()
    seed, dbg = spent_leg_seed_decision(
        cur_hod=4.93, hod_ts=hod_ts_iso, hod_date_et=DAY, live_px=4.34,
        now_utc=NOW.replace(tzinfo=None), session_date_et=DAY,
        frame_age_s=0.0, coverage_gap_s=0.0, interval_s=60.0,
        session_start_utc=(NOW - timedelta(minutes=20)).replace(tzinfo=None).isoformat(),
        observed_since_utc=NOW - timedelta(minutes=10), observed_ticks=100,
    )
    assert seed is True and abs(dbg["hod_age_min"] - 8.0) < 1e-6


def test_thresholds_bind_verbatim():
    assert _decide(hod=100.0, age_min=5.0, px=95.0)[0] is True   # exactly at both floors
    assert _decide(hod=100.0, age_min=4.99, px=95.0)[1]["reason"] == "spent_leg_too_young"
    assert _decide(hod=100.0, age_min=5.0, px=95.01)[1]["reason"] == "spent_leg_too_shallow"
    assert _decide(hod=100.0, age_min=5.0, px=95.01, min_dd_pct=4.0)[0] is True
    assert _decide(hod=100.0, age_min=4.99, px=95.0, min_age_min=4.0)[0] is True


# ── tz-aware frame + aware clock ⇒ finite age (refuter 1 item 2) ─────────────

def _canf_frame(day="2026-09-02", start_h=11, n=11, hod_at=2, hod=4.93, last_close=4.34):
    idx = pd.date_range(f"{day} {start_h:02d}:00", periods=n, freq="1min", tz="UTC")
    closes = np.linspace(4.60, last_close, n)
    highs = closes * 1.001
    highs[hod_at] = hod
    return pd.DataFrame({
        "Open": closes, "High": highs, "Low": closes * 0.999,
        "Volume": np.full(n, 5000.0), "Close": closes,
    }, index=idx)


def test_aware_frame_and_aware_now_give_finite_age():
    df = _canf_frame()
    now = NOW  # 11:10 UTC; last bar 11:10 -> ends 11:11; HOD bar 11:02 -> ends 11:03
    dbg = session_frame_hod_debug(df, now_utc=now)
    assert dbg["hod_age_min"] == pytest.approx(7.0)
    assert dbg["hod_bar_date_et"] == DAY
    seed, sdbg = spent_leg_seed_decision(
        cur_hod=dbg["frame_hod"], hod_ts=dbg["hod_bar_end_ts"], hod_date_et=dbg["hod_bar_date_et"],
        live_px=4.34, now_utc=now, session_date_et=DAY,
        frame_age_s=dbg["frame_age_s"], coverage_gap_s=None, interval_s=dbg["interval_s"],
        session_start_utc=df.index[0], observed_since_utc=df.index[0], observed_ticks=100,
    )
    assert seed is True and np.isfinite(sdbg["hod_age_min"])
    # the bench debug carries the same fields (the live runner's source)
    _b, _r, _a, bdbg = evaluate_sticky_backside_bench(df, benched_at_hod=None, live_price=4.34, now_utc=now)
    assert bdbg["hod_age_min"] == pytest.approx(7.0)
    assert bdbg["frame_hod"] == pytest.approx(4.93)


# ── table-driven replay of the real fills (fills_study.csv, hard-coded) ──────
# (sym, day, et_time, entry, pnl, hod_pre, hod_age_min_bars, dd_from_hod_pct)
REAL_FILLS = [
    # current era (08-21..09-02), Alpaca paper lane
    ("SDOT", "2026-08-21", "10:45", 17.36, -14.56, 17.31, 6.0, -0.29),
    ("COIW", "2026-08-21", "10:55", 9.91, None, 9.8997, 51.0, -0.10),
    ("XPON", "2026-08-24", "09:00", 8.17, None, 9.03, 7.4, 9.52),
    ("BDRX", "2026-08-25", "05:03", 1.51, None, 1.62, 7.6, 6.79),
    ("CDTG", "2026-08-26", "07:41", 1.369948, -82.00, 1.70, 18.4, 19.41),
    ("AEMD", "2026-08-27", "18:37", 3.34, None, 3.46, 91.9, 3.47),
    ("MOVE", "2026-08-31", "08:33", 16.24, None, 17.50, 11.2, 7.20),
    ("RDHL", "2026-08-31", "08:43", 1.44, -2.05, 2.16, 64.8, 33.33),
    ("GYGY", "2026-09-01", "04:42", 1.43, -29.20, 1.88, 42.1, 23.94),
    ("WETO", "2026-09-01", "06:21", 7.75, -17.70, 8.20, 112.2, 5.49),
    ("SSM", "2026-09-01", "06:44", 4.01, -25.98, 4.77, 164.6, 15.93),
    ("AUUD", "2026-09-01", "07:10", 1.11, -44.01, 1.23, 6.9, 9.76),
    ("LIDR", "2026-09-01", "08:26", 1.65, -4.91, 1.65, 2.8, 0.00),
    ("LIDR", "2026-09-01", "08:59", 1.67, -2.86, 1.81, 20.3, 7.73),
    ("UPC", "2026-09-02", "04:40", 5.395955, -48.97, 5.60, 18.4, 3.64),
    ("JLHL", "2026-09-02", "06:47", 7.396376, -18.83, 7.92, 7.6, 6.61),
    ("CANF", "2026-09-02", "07:10", 4.34, -78.13, 4.9297, 8.3, 11.96),
    # July (10x size, different exit stack) — the overfit guard
    ("SKYQ", "2026-07-08", "04:43", 3.35, 219.70, 3.4293, 2.0, 2.31),
    ("LUCY", "2026-07-08", "07:04", 1.59, 82.11, 1.69, 2.5, 5.92),
    ("LHAI", "2026-07-08", "09:45", 1.56, 48.07, 1.62, 83.4, 3.70),
    ("VTAK", "2026-07-08", "10:13", 1.55, -657.24, 1.74, 7.8, 10.92),
    ("VTAK", "2026-07-08", "13:37", 1.39, 3.99, 1.74, 211.6, 20.11),
    ("NVVE", "2026-07-08", "10:44", 7.952222, -24.81, 9.44, 10.6, 15.76),
    ("VRAX", "2026-07-09", "07:35", 5.76003, 371.01, 7.49, 3.6, 23.10),
    ("RKTO", "2026-07-09", "08:41", 1.02, 74.52, 1.06, 16.3, 3.77),
    ("JLHL", "2026-07-09", "11:31", 6.44, -27.03, 7.03, 9.4, 8.39),
    ("JZXN", "2026-07-10", "09:26", 1.61, 182.16, 1.92, 11.0, 16.15),
    ("TKLF", "2026-07-10", "10:18", 2.40, -2116.32, 2.84, 147.9, 15.49),
    ("NVVE", "2026-07-10", "10:53", 18.31, -1521.90, 20.74, 9.6, 11.72),
    ("SNAL", "2026-07-10", "13:41", 4.97, -356.92, 5.42, 49.8, 8.30),
    ("VEEE", "2026-07-13", "09:12", 9.465069, 444.60, 11.00, 4.7, 13.95),
    ("BRAI", "2026-07-13", "10:09", 7.45, -711.96, 8.29, 18.3, 10.13),
    ("SOBR", "2026-07-13", "13:57", 1.47, -679.56, 1.51, 2.7, 2.65),
    ("TRNR", "2026-07-13", "14:13", 2.99, -503.58, 3.1699, 60.0, 5.68),
]


def _replay(rows):
    """FILL-INSTANT predicate on each fill. The CSV ages are measured from the HOD
    bar START (idxmax); the shipped age is from the bar END (entry_gates.py
    session_frame_hod_debug, 1m bench interval) — one bar YOUNGER — so the replay
    feeds the live basis (age - 1 min), not the ablation's (review 2026-09-02 M11;
    nothing flips at M=5: AUUD 6.9->5.9, XPON 7.4->6.4, BDRX 7.6->6.6).
    The LIVE marker lifecycle equals this fill-instant read by construction: the
    marker clears when the pullback shallows under P% (see the RKTO lifecycle
    test below), so 'seeded here' == 'blocked at the fill' and vice versa."""
    seeded, not_seeded = [], []
    for sym, day, et_time, entry, pnl, hod, age, dd in rows:
        h, m = (int(x) for x in et_time.split(":"))
        now = datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:00-04:00").astimezone(UTC)
        age_bar_end = max(0.0, float(age) - 1.0)
        _hod_end = now - timedelta(minutes=age_bar_end)
        seed, dbg = spent_leg_seed_decision(
            cur_hod=hod, hod_ts=_hod_end, hod_date_et=day,
            live_px=entry, now_utc=now, session_date_et=day,
            frame_age_s=0.0, coverage_gap_s=0.0, interval_s=60.0,
            # the corpus is a FILL-INSTANT read: every one of these fills happened
            # on a name CHILI was already watching, so the cold-start guard's two
            # conjuncts (HOD bar END inside the window, session warm) hold by
            # construction. The guard is measured on its own fixtures below.
            session_start_utc=_hod_end - timedelta(minutes=1),
            observed_since_utc=now - timedelta(minutes=10), observed_ticks=100,
        )
        key = f"{sym}@{day} {et_time}"
        (seeded if seed else not_seeded).append((key, pnl, dbg["reason"]))
        if dd > 0 and entry < hod:
            assert dbg.get("dd_pct") is None or abs(dbg["dd_pct"] - dd) < 0.15, (key, dbg)
    return seeded, not_seeded


def test_table_driven_replay_of_the_real_fills():
    seeded, not_seeded = _replay(REAL_FILLS)
    seeded_keys = {k.split("@")[0] + "@" + k.split("@")[1] for k, _, _ in seeded}
    # The four named losers of 09-01/09-02: three seed, UPC is the documented miss.
    assert "CANF@2026-09-02 07:10" in seeded_keys
    assert "JLHL@2026-09-02 06:47" in seeded_keys
    assert "AUUD@2026-09-01 07:10" in seeded_keys
    assert "UPC@2026-09-02 04:40" not in seeded_keys
    # current-era: 12 of 17 fills seed; SDOT / COIW / LIDR#1 at HOD, AEMD 3.47%, UPC 3.64% do not.
    cur = [r for r in REAL_FILLS if r[1] >= "2026-08-21"]
    cur_seeded, cur_not = _replay(cur)
    assert len(cur_seeded) == 12 and len(cur_not) == 5, (cur_seeded, cur_not)
    assert {k.split("@")[0] for k, _, _ in cur_not} == {"SDOT", "COIW", "AEMD", "LIDR", "UPC"}
    # every current-era P&L that seeds is a loss or unbooked; no current-era winner is blocked.
    assert all((pnl is None) or (pnl < 0) for _, pnl, _ in cur_seeded)
    # July winners the 5-min / 5% floors protect AT THE FILL: VEEE, VRAX, LUCY, SKYQ, RKTO,
    # LHAI stay free. RKTO / LHAI both dipped ~7% >= 5 min after their HOD EARLIER (RKTO
    # 08:31 close 0.986 under 1.06; LHAI 08:28 close 1.50 under 1.62, 1m bars re-fetched
    # 2026-09-02) with no re-take before the fill: a marker that only cleared on a re-take
    # would have blocked both. The shipped marker clears when the pullback shallows
    # under P% (test_rkto_shape_shallowed_clear_admits_the_fill), so they stay free live.
    july_not = {k.split("@")[0] for k, _, _ in _replay([r for r in REAL_FILLS if r[1] < "2026-08-01"])[1]}
    assert {"VEEE", "VRAX", "LUCY", "SKYQ", "RKTO", "LHAI", "SOBR"} <= july_not
    # and the HONEST cost: JZXN (+182.16, 16.4R) IS seeded on bars — only its HOD re-take
    # 4 min after entry (the WAIT clear, tested below) can admit it.
    assert "JZXN@2026-07-10 09:26" in seeded_keys
    july_seeded = _replay([r for r in REAL_FILLS if r[1] < "2026-08-01"])[0]
    july_w = [p for _, p, _ in july_seeded if p is not None and p > 0]
    assert sorted(july_w) == sorted([182.16, 3.99])  # JZXN, VTAK#2 — the two July winners blocked


# ── reentry_escalation_decision with the seeded reference ────────────────────

def test_seeded_reference_blocks_canf1_non_structural():
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=False, live_price=4.34,
        prior_hwm=4.9297, prior_exit_price=None, prior_risk_dist=None, tape_accel=1.0,
    )
    assert ok is False and dbg["reason"] == "non_structural_trigger"


def test_seeded_reference_blocks_structural_below_hod_documented_cost():
    """At level 1 _reclaim_required() = HOD + 0: a NON-LEADER structural pullback
    trigger under the top is blocked too. This is the stated cost of A."""
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True, live_price=4.34,
        prior_hwm=4.9297, prior_exit_price=None, prior_risk_dist=None, tape_accel=1.0,
    )
    assert ok is False and dbg["reason"] == "reclaim_not_met"
    assert dbg["required_reclaim"] == pytest.approx(4.9297)


def test_leader_ignition_bypass_still_admits_the_day_leader():
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True, live_price=4.34,
        prior_hwm=4.9297, prior_exit_price=None, prior_risk_dist=None, tape_accel=1.0,
        is_day_leader=True,
    )
    assert ok is True and dbg["reason"] == "leader_ignition_bypass"


def test_canf2_level_2_with_maxed_reference_is_blocked():
    """CANF#2: level 2, prior ref 4.35 admitted 4.40 today; with the seed the reference
    is max(4.35, 4.9297) + 1 x 0.075 = 5.0047 -> blocked."""
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=2, structural_trigger=True, live_price=4.40,
        prior_hwm=max(4.35, 4.9297), prior_exit_price=4.30, prior_risk_dist=0.075, tape_accel=1.0,
    )
    assert ok is False and dbg["reason"] == "reclaim_not_met"
    assert dbg["required_reclaim"] == pytest.approx(5.0047)


def test_seeded_reference_admits_the_re_take():
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True, live_price=4.93,
        prior_hwm=4.9297, prior_exit_price=None, prior_risk_dist=None, tape_accel=1.0,
    )
    assert ok is True and dbg["reason"] == "reclaim_met"


def test_cleared_level_0_is_no_escalation():
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=0, structural_trigger=False, live_price=4.34,
        prior_hwm=4.9297, prior_exit_price=None, prior_risk_dist=None, tape_accel=None,
    )
    assert ok is True and dbg["reason"] == "no_escalation"


# ── apply_spent_leg_tick lifecycle ───────────────────────────────────────────

T0 = datetime(2026, 9, 2, 11, 0, 0, tzinfo=UTC)  # 07:00 ET


def _warm(le, *, now, session_date_et, warm_s=600.0):
    """State a session that has ALREADY been observing priced ticks for 10 min
    (cold-start guard, A/B 2026-09-02). Only installed when the ledger has no
    tick-hod run for TODAY, so an explicitly-built coverage run is never
    overwritten. px is 0.01 so it can never be the effective top. On later
    calls it only keeps the run CONTINUOUS across the fixture's deliberately
    sparse ticks (a real runner ticks every loop pass; a fixture that jumps 20
    minutes would otherwise book a coverage break and go cold again)."""
    th = le.get("spent_leg_tick_hod")
    if isinstance(th, dict) and str(th.get("session_date_et") or "") == session_date_et:
        th["last_tick_ts"] = (now - timedelta(seconds=1)).isoformat()
        return
    start = now - timedelta(seconds=warm_s)
    le["spent_leg_tick_hod"] = {
        "px": 0.01, "ts": start.isoformat(), "first_tick_ts": start.isoformat(),
        "session_first_tick_ts": start.isoformat(),
        "last_tick_ts": (now - timedelta(seconds=1)).isoformat(),
        "coverage_breaks": 0, "run_ticks": 100, "session_date_et": session_date_et,
    }


def _tick(le, *, px, now, frame_hod, hod_end, frame_last_end=None, frame_age_s=0.0,
          enabled=True, symbol="CANF", session_date_et=DAY, hod_date_et=DAY,
          warm=True, session_start=_UNSET, min_uptime_s=60.0, min_ticks=1,
          clear_dd=3.5, dwell_s=20.0):
    """SHIPPED defaults: hysteresis (arm 5%, clear 3.5%, 20 s dwell) and the
    cold-start floors (60 s uptime, 1 tick). ``warm=False`` opts a case out of
    the pre-observed session so it can measure the cold start itself."""
    if warm:
        _warm(le, now=now, session_date_et=session_date_et)
    return apply_spent_leg_tick(
        le, symbol=symbol, enabled=enabled, px=px, tick_ts=now, now_utc=now,
        session_date_et=session_date_et,
        frame_hod=frame_hod, frame_hod_ts=hod_end, frame_hod_date_et=hod_date_et,
        frame_last_bar_end_ts=(frame_last_end if frame_last_end is not None else now),
        frame_age_s=frame_age_s, interval_s=60.0,
        session_start_utc=(
            ((hod_end - timedelta(minutes=1)) if hod_end is not None
             else now - timedelta(hours=1))
            if session_start is _UNSET else session_start
        ),
        min_age_min=5.0, min_dd_pct=5.0, max_frame_age_s=120.0,
        min_session_uptime_s=min_uptime_s, min_observed_ticks=min_ticks,
        clear_dd_pct=clear_dd, clear_min_dwell_s=dwell_s,
    )


def _apply(le, updates):
    for k, v in updates.items():
        if v is None:
            le.pop(k, None)
        else:
            le[k] = v
    return le


def _events(actions):
    return [ev for ev, _ in actions]


def test_seed_once_and_never_re_seed_the_same_top():
    le: dict = {}
    up, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    _apply(le, up)
    assert _events(acts) == ["g4_spent_leg_seed"]
    m = le["g4_spent_leg"]
    assert m["active"] is True and m["hod"] == 4.9297 and m["hod_source"] == "frame"
    assert le["g4_reentry_escalation"] == 1
    assert m["key_absent_at_seed"] is True and m["seed_level_delta"] == 1
    assert le["spent_leg_tick_hod"]["px"] == 4.34
    up, acts = _tick(le, px=4.36, now=T0 + timedelta(minutes=11), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    _apply(le, up)
    assert acts == [] and le["g4_spent_leg"]["active"] is True


def test_re_take_clears_and_deletes_the_key_it_created():
    le: dict = {}
    _apply(le, _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))[0])
    up, acts = _tick(le, px=4.9297, now=T0 + timedelta(minutes=14), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    assert _events(acts) == ["g4_spent_leg_cleared"]
    payload = acts[0][1]
    assert payload["retopped"] is False and payload["level_after"] is None
    assert payload["minutes_waited"] == pytest.approx(4.0)
    assert up["g4_reentry_escalation"] is None  # delete -> cross-day seed re-evaluates
    _apply(le, up)
    assert "g4_reentry_escalation" not in le
    assert le["g4_spent_leg"]["active"] is False and le["g4_spent_leg"]["cleared_hod"] == 4.9297
    # the re-taken top then fades 10% for 30 min: NO re-seed on the same (retired) top.
    up, acts = _tick(le, px=4.45, now=T0 + timedelta(minutes=44), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    assert acts == []
    # ... but a strictly HIGHER top that then goes spent IS a new spent leg (re-seed).
    up, acts = _tick(le, px=4.45, now=T0 + timedelta(minutes=50), frame_hod=4.95,
                     hod_end=T0 + timedelta(minutes=40))
    assert _events(acts) == ["g4_spent_leg_seed"] and acts[0][1]["hod"] == 4.95


def test_retop_clears_and_reseeds_on_the_new_top_same_tick():
    """Refuter 0: marker at 4.93, frame cur_hod 5.10 (>= 5 min old), tick px 4.80 ->
    cleared (retopped) AND re-seeded at 5.10, never left at 4.93."""
    le: dict = {}
    _apply(le, _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.93,
                     hod_end=T0 + timedelta(minutes=3))[0])
    up, acts = _tick(le, px=4.80, now=T0 + timedelta(minutes=30), frame_hod=5.10,
                     hod_end=T0 + timedelta(minutes=24))
    assert _events(acts) == ["g4_spent_leg_cleared", "g4_spent_leg_seed"]
    assert acts[0][1]["retopped"] is True and acts[0][1]["new_hod"] == 5.10
    assert acts[1][1]["hod"] == 5.10 and acts[1][1]["retop_reseed"] is True
    _apply(le, up)
    assert le["g4_spent_leg"]["active"] is True and le["g4_spent_leg"]["hod"] == 5.10
    assert le["g4_reentry_escalation"] == 1


def test_retop_to_a_fresh_top_clears_without_reseed():
    le: dict = {}
    _apply(le, _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.93,
                     hod_end=T0 + timedelta(minutes=3))[0])
    up, acts = _tick(le, px=4.80, now=T0 + timedelta(minutes=13), frame_hod=5.10,
                     hod_end=T0 + timedelta(minutes=12))  # 5.10 bar 1 min old
    assert _events(acts) == ["g4_spent_leg_cleared"]
    _apply(le, up)
    assert le["g4_spent_leg"]["active"] is False
    assert "g4_reentry_escalation" not in le  # the next trigger is admitted


def test_hod_advance_on_a_non_trigger_tick_then_spent_under_the_new_top():
    """Refuter 1 item 3: the HOD advances on a benched / non-trigger tick (the clear
    runs on EVERY score-ok tick), then a trigger 5% under the NEW top >= 5 min later
    is still blocked — the reference is the new top, not the old one."""
    le: dict = {}
    _apply(le, _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.93,
                     hod_end=T0 + timedelta(minutes=3))[0])
    # non-trigger tick prints a live new high 5.10 (frame still shows 4.93)
    up, acts = _tick(le, px=5.10, now=T0 + timedelta(minutes=12), frame_hod=4.93,
                     hod_end=T0 + timedelta(minutes=3))
    assert _events(acts) == ["g4_spent_leg_cleared"]
    _apply(le, up)
    assert le["spent_leg_tick_hod"]["px"] == 5.10
    # 2 min later, 5% under 5.10 -> fresh top, NOT seeded (admitted)
    up, acts = _tick(le, px=4.84, now=T0 + timedelta(minutes=14), frame_hod=4.93,
                     hod_end=T0 + timedelta(minutes=3))
    _apply(le, up)
    assert acts == []
    # 6 min after the new top, still 5.1% under -> seeded against 5.10 (tick source)
    up, acts = _tick(le, px=4.84, now=T0 + timedelta(minutes=18), frame_hod=4.93,
                     hod_end=T0 + timedelta(minutes=3))
    assert _events(acts) == ["g4_spent_leg_seed"]
    assert acts[0][1]["hod"] == 5.10 and acts[0][1]["hod_source"] == "tick"
    _apply(le, up)
    assert le["g4_spent_leg"]["hod"] == 5.10 and le["g4_reentry_escalation"] == 1


def test_stale_session_date_marker_is_treated_as_absent():
    le = {
        "g4_spent_leg": {"active": True, "hod": 9.99, "session_date_et": "2026-09-01",
                         "seed_level_delta": 1, "key_absent_at_seed": True},
        "spent_leg_tick_hod": {"px": 9.99, "ts": T0.isoformat(), "first_tick_ts": T0.isoformat(),
                               "session_date_et": "2026-09-01"},
    }
    up, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    # yesterday's ACTIVE marker is UNWOUND (session_rollover), never cleared as a
    # re-take of 9.99 by today's price, then today's spent leg seeds on 4.9297.
    assert _events(acts) == ["g4_spent_leg_cleared", "g4_spent_leg_seed"]
    assert acts[0][1]["clear_reason"] == "session_rollover"
    assert acts[0][1]["marker_session_date_et"] == "2026-09-01"
    assert acts[0][1]["retopped"] is False and acts[0][1]["hod"] == 9.99
    _apply(le, up)
    assert le["g4_spent_leg"]["hod"] == 4.9297 and le["g4_spent_leg"]["session_date_et"] == DAY
    assert le["spent_leg_tick_hod"]["session_date_et"] == DAY
    assert le["g4_reentry_escalation"] == 1  # today's seed, not an orphan of yesterday's


def test_session_rollover_unwinds_the_orphaned_level():
    """Review M15: a day-1 seed (level 1, key created by the seed) that never
    re-took its top survives the ET rollover as an orphaned level-1 with no
    reference — the day-2 tick must unwind it (key deleted -> the #1252
    cross-day seed gets its first read again) and emit the clear."""
    le = {
        "g4_spent_leg": {"active": True, "hod": 4.9297, "session_date_et": "2026-09-01",
                         "seed_level_delta": 1, "key_absent_at_seed": True,
                         "seeded_at": (T0 - timedelta(days=1)).isoformat(),
                         "blocks_while_seeded": 7, "hod_source": "frame"},
        "g4_reentry_escalation": 1,
    }
    day2 = T0 + timedelta(days=1)
    # day 2, price at the (young, 2 min) new HOD: nothing to seed, only the unwind.
    up, acts = _tick(le, px=5.00, now=day2 + timedelta(minutes=5), frame_hod=5.00,
                     hod_end=day2 + timedelta(minutes=3), session_date_et="2026-09-03",
                     hod_date_et="2026-09-03")
    assert _events(acts) == ["g4_spent_leg_cleared"]
    p = acts[0][1]
    assert p["clear_reason"] == "session_rollover" and p["blocks_while_seeded"] == 7
    assert p["level_before"] == 1 and p["level_after"] is None
    assert up["g4_reentry_escalation"] is None
    _apply(le, up)
    assert "g4_reentry_escalation" not in le
    assert le["g4_spent_leg"]["active"] is False
    # a real-loss level (delta 0) is left alone by the rollover unwind
    le2 = {
        "g4_spent_leg": {"active": True, "hod": 4.9297, "session_date_et": "2026-09-01",
                         "seed_level_delta": 0, "key_absent_at_seed": False},
        "g4_reentry_escalation": 2,
    }
    up, acts = _tick(le2, px=5.00, now=day2 + timedelta(minutes=5), frame_hod=5.00,
                     hod_end=day2 + timedelta(minutes=3), session_date_et="2026-09-03",
                     hod_date_et="2026-09-03")
    assert acts[0][1]["clear_reason"] == "session_rollover" and up["g4_reentry_escalation"] == 2


def test_rkto_shape_hysteresis_now_holds_the_fill_the_corpus_calls_free():
    """MEASURED COST of CHANGE 2, stated not hidden. RKTO 07-09 — HOD 1.06 @
    08:25, 08:31 close 0.986 (6.98% under) then the +74.52 fill at 1.02 (3.77%
    under) with NO re-take between. At the PR's original single 5% line that
    fill cleared the WAIT (review M8); at the shipped 3.5% clear band 3.77% is
    INSIDE the dead band, so the marker holds and the fill is blocked. LHAI
    07-08 (+48.07, 3.70% under) is the same shape. Setting
    chili_momentum_g4_spent_leg_clear_drawdown_pct = 5.0 restores the exact
    fill-instant equality — the rest of this test pins that."""
    hod_end = datetime(2026, 7, 9, 12, 26, 0, tzinfo=UTC)  # 08:25 ET bar END
    le0: dict = {}
    _apply(le0, _tick(le0, px=0.986, now=hod_end + timedelta(minutes=6), frame_hod=1.06,
                      hod_end=hod_end, session_date_et="2026-07-09",
                      hod_date_et="2026-07-09")[0])
    _u, acts = _tick(le0, px=1.02, now=hod_end + timedelta(minutes=15), frame_hod=1.06,
                     hod_end=hod_end, session_date_et="2026-07-09", hod_date_et="2026-07-09")
    assert acts == []  # 3.77% > the 3.5% clear band: still seeded (the cost)
    assert le0["g4_spent_leg"]["active"] is True
    # LHAI's 3.70% is held too
    _u, acts = _tick(le0, px=1.0208, now=hod_end + timedelta(minutes=16), frame_hod=1.06,
                     hod_end=hod_end, session_date_et="2026-07-09", hod_date_et="2026-07-09")
    assert acts == []
    # ── with the hysteresis turned off (clear_dd = min_dd) the M8 behaviour is exact ──
    _rk = dict(clear_dd=5.0, session_date_et="2026-07-09", hod_date_et="2026-07-09")
    le: dict = {}
    up, acts = _tick(le, px=0.986, now=hod_end + timedelta(minutes=6), frame_hod=1.06,
                     hod_end=hod_end, **_rk)
    _apply(le, up)
    assert _events(acts) == ["g4_spent_leg_seed"] and le["g4_reentry_escalation"] == 1
    # 08:41 ET: 1.02 = 3.77% under -> shallowed clear, key deleted, top NOT retired
    up, acts = _tick(le, px=1.02, now=hod_end + timedelta(minutes=15), frame_hod=1.06,
                     hod_end=hod_end, **_rk)
    assert _events(acts) == ["g4_spent_leg_cleared"]
    p = acts[0][1]
    assert p["clear_reason"] == "shallowed" and p["retopped"] is False
    assert p["clear_px"] == 1.02 and p["level_after"] is None
    _apply(le, up)
    assert "g4_reentry_escalation" not in le
    assert le["g4_spent_leg"]["active"] is False and le["g4_spent_leg"]["cleared_hod"] is None
    ok, dbg = reentry_escalation_decision(
        enabled=True, escalation_level=int(le.get("g4_reentry_escalation") or 0),
        structural_trigger=False, live_price=1.02, prior_hwm=1.06, prior_exit_price=None,
        prior_risk_dist=None, tape_accel=1.0,
    )
    assert ok is True and dbg["reason"] == "no_escalation"
    # the SAME top goes spent again 10 min later (5.7% under) -> re-seeded (not retired)
    up, acts = _tick(le, px=1.00, now=hod_end + timedelta(minutes=25), frame_hod=1.06,
                     hod_end=hod_end, session_date_et="2026-07-09", hod_date_et="2026-07-09")
    assert _events(acts) == ["g4_spent_leg_seed"] and acts[0][1]["hod"] == 1.06
    # ... whereas a re-take (hit_top) RETIRES the top: a later 10% fade never re-seeds on it
    _apply(le, up)
    up, acts = _tick(le, px=1.06, now=hod_end + timedelta(minutes=30), frame_hod=1.06,
                     hod_end=hod_end, session_date_et="2026-07-09", hod_date_et="2026-07-09")
    assert acts[0][1]["clear_reason"] == "hit_top"
    _apply(le, up)
    assert le["g4_spent_leg"]["cleared_hod"] == 1.06
    _u, acts = _tick(le, px=0.95, now=hod_end + timedelta(minutes=60), frame_hod=1.06,
                     hod_end=hod_end, session_date_et="2026-07-09", hod_date_et="2026-07-09")
    assert acts == []


def test_shallow_clear_uses_the_lower_hysteresis_band():
    """CHANGE 2 (A/B verdict 2026-09-02). The marker ARMS at dd >= 5% but the
    'shallowed' clear now fires only under the SEPARATE 3.5% band, so price
    oscillating across the arm line no longer re-arms/disarms every tick (36 of
    36 A/B clears were 'shallowed'; CANF produced 7 seed/clear pairs inside
    ~2 sim minutes). Setting clear_dd == min_dd restores the old equality."""
    le: dict = {}
    _apply(le, _tick(le, px=94.0, now=T0 + timedelta(minutes=10), frame_hod=100.0,
                     hod_end=T0 + timedelta(minutes=3))[0])
    for px in (95.0, 95.01, 96.4, 96.5):  # 5.00 / 4.99 / 3.60 / 3.50 % under
        _u, acts = _tick(le, px=px, now=T0 + timedelta(minutes=12), frame_hod=100.0,
                         hod_end=T0 + timedelta(minutes=3))
        assert acts == [], (px, acts)  # inside the dead band: the WAIT holds
    _u, acts = _tick(le, px=96.51, now=T0 + timedelta(minutes=13), frame_hod=100.0,
                     hod_end=T0 + timedelta(minutes=3))  # 3.49% under
    assert _events(acts) == ["g4_spent_leg_cleared"]
    assert acts[0][1]["clear_reason"] == "shallowed" and acts[0][1]["clear_dd_pct"] == 3.5
    # no hysteresis (clear_dd == min_dd) is the PR's original fill-instant rule
    le2: dict = {}
    _apply(le2, _tick(le2, px=94.0, now=T0 + timedelta(minutes=10), frame_hod=100.0,
                      hod_end=T0 + timedelta(minutes=3), clear_dd=5.0)[0])
    _u, acts = _tick(le2, px=95.01, now=T0 + timedelta(minutes=12), frame_hod=100.0,
                     hod_end=T0 + timedelta(minutes=3), clear_dd=5.0)
    assert _events(acts) == ["g4_spent_leg_cleared"]
    # a clear band ABOVE the arm band is clamped: it must never clear the tick it armed
    le3: dict = {}
    up, acts = _tick(le3, px=94.0, now=T0 + timedelta(minutes=10), frame_hod=100.0,
                     hod_end=T0 + timedelta(minutes=3), clear_dd=9.0)
    _apply(le3, up)
    assert _events(acts) == ["g4_spent_leg_seed"]
    _u, acts = _tick(le3, px=94.0, now=T0 + timedelta(minutes=11), frame_hod=100.0,
                     hod_end=T0 + timedelta(minutes=3), clear_dd=9.0)
    assert acts == []


def test_shallowed_clear_waits_out_the_dwell_but_retop_and_kill_never_do():
    """CHANGE 2, second half: 15 of the A/B's 36 clears fired with
    minutes_waited 0.017-0.06 (under 4 s). A 'shallowed' clear may not fire
    before ``clear_min_dwell_s``; hit_top / retop / disabled / session_rollover
    are NEVER delayed — a re-take and the kill switch must be instant."""
    le: dict = {}
    _apply(le, _tick(le, px=94.0, now=T0 + timedelta(minutes=10), frame_hod=100.0,
                     hod_end=T0 + timedelta(minutes=3))[0])
    _u, acts = _tick(le, px=97.0, now=T0 + timedelta(minutes=10, seconds=4),
                     frame_hod=100.0, hod_end=T0 + timedelta(minutes=3))
    assert acts == []  # 3.0% under (past the band) but only 4 s seeded
    _u, acts = _tick(le, px=97.0, now=T0 + timedelta(minutes=10, seconds=20),
                     frame_hod=100.0, hod_end=T0 + timedelta(minutes=3))
    assert _events(acts) == ["g4_spent_leg_cleared"]
    assert acts[0][1]["clear_reason"] == "shallowed" and acts[0][1]["clear_min_dwell_s"] == 20.0
    # a re-take 1 s after the seed clears IMMEDIATELY
    le2: dict = {}
    _apply(le2, _tick(le2, px=94.0, now=T0 + timedelta(minutes=10), frame_hod=100.0,
                      hod_end=T0 + timedelta(minutes=3))[0])
    _u, acts = _tick(le2, px=100.0, now=T0 + timedelta(minutes=10, seconds=1),
                     frame_hod=100.0, hod_end=T0 + timedelta(minutes=3))
    assert _events(acts) == ["g4_spent_leg_cleared"] and acts[0][1]["clear_reason"] == "hit_top"
    # ...and so does the KILL (flag OFF), still deep under the top
    le3: dict = {}
    _apply(le3, _tick(le3, px=94.0, now=T0 + timedelta(minutes=10), frame_hod=100.0,
                      hod_end=T0 + timedelta(minutes=3))[0])
    _u, acts = _tick(le3, px=94.0, now=T0 + timedelta(minutes=10, seconds=1),
                     frame_hod=100.0, hod_end=T0 + timedelta(minutes=3), enabled=False)
    assert _events(acts) == ["g4_spent_leg_cleared"] and acts[0][1]["clear_reason"] == "disabled"
    # a retop 1 s after the seed also clears immediately
    le4: dict = {}
    _apply(le4, _tick(le4, px=94.0, now=T0 + timedelta(minutes=10), frame_hod=100.0,
                      hod_end=T0 + timedelta(minutes=3))[0])
    _u, acts = _tick(le4, px=97.0, now=T0 + timedelta(minutes=10, seconds=1),
                     frame_hod=101.0, hod_end=T0 + timedelta(minutes=9))
    assert acts[0][1]["clear_reason"] == "retop"


def test_seed_level_delta_is_zero_when_a_real_loss_level_exists():
    le = {"g4_reentry_escalation": 1}
    up, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    _apply(le, up)
    assert le["g4_spent_leg"]["seed_level_delta"] == 0 and le["g4_reentry_escalation"] == 1
    up, acts = _tick(le, px=4.95, now=T0 + timedelta(minutes=14), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    _apply(le, up)
    assert _events(acts) == ["g4_spent_leg_cleared"]
    assert le["g4_reentry_escalation"] == 1  # the real loss's level is untouched


def test_seed_then_stop_loss_then_retake_leaves_the_loss_level():
    """seed(1) -> stop-class loss (2) -> HOD re-take -> 1 (only the seed's +1 removed)."""
    le: dict = {}
    _apply(le, _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))[0])
    lvl, why = reentry_escalation_level_update(
        current_level=le["g4_reentry_escalation"], was_loss=True, exit_reason="stop",
        green_banked=False,
    )
    assert (lvl, why) == (2, "stop_class_loss_increment")
    le["g4_reentry_escalation"] = lvl
    up, acts = _tick(le, px=4.95, now=T0 + timedelta(minutes=30), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    _apply(le, up)
    assert le["g4_reentry_escalation"] == 1 and acts[0][1]["level_after"] == 1


def test_disabled_flag_unwinds_immediately_and_never_seeds():
    """Review M1/M14 (the KILL): flag OFF must unwind an active marker on the
    very next score-ok tick — price still 12% under the top, no re-take — so
    the KILL itself can never leave a session-long WAIT behind."""
    le: dict = {}
    _apply(le, _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))[0])
    assert le["g4_reentry_escalation"] == 1
    up, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=11), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), enabled=False)
    assert _events(acts) == ["g4_spent_leg_cleared"]  # flipping OFF never strands a name
    assert acts[0][1]["clear_reason"] == "disabled" and up["g4_reentry_escalation"] is None
    _apply(le, up)
    assert le["g4_spent_leg"]["active"] is False and "g4_reentry_escalation" not in le
    # still OFF: nothing seeds, no further events
    _u, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=12), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), enabled=False)
    assert acts == []
    le2: dict = {}
    up, acts = _tick(le2, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), enabled=False)
    assert acts == [] and "g4_spent_leg" not in up
    # flag back ON: the same (un-retired) top re-seeds — the disable was not a retirement
    up, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=13), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), enabled=True)
    assert _events(acts) == ["g4_spent_leg_seed"]


def test_crypto_and_missing_frame_never_seed():
    le: dict = {}
    _u, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), symbol="BTC-USD")
    assert acts == []
    _u, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=None, hod_end=None)
    assert acts == []  # bench flag OFF -> no frame -> unreadable -> no seed


def test_stale_frame_is_bridged_only_by_tick_coverage():
    le: dict = {}
    # first tick arrives with a frame whose last bar ended 10 min ago: stale, no coverage yet
    _u, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=20), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), frame_last_end=T0 + timedelta(minutes=10),
                     frame_age_s=600.0, warm=False)
    _apply(le, _u)
    assert acts == []
    # same stale frame, but the session's first tick was at 11:20 (10 min after the last bar):
    # gap 600 s > 120 s -> still stale
    _u, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=25), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), frame_last_end=T0 + timedelta(minutes=10),
                     frame_age_s=900.0, warm=False)
    assert acts == []
    # a session that has ticked since BEFORE the frame's last bar bridges it
    le2 = {"spent_leg_tick_hod": {"px": 4.50, "ts": (T0 + timedelta(minutes=5)).isoformat(),
                                  "first_tick_ts": (T0 + timedelta(minutes=5)).isoformat(),
                                  "session_date_et": DAY}}
    _u, acts = _tick(le2, px=4.34, now=T0 + timedelta(minutes=25), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), frame_last_end=T0 + timedelta(minutes=10),
                     frame_age_s=900.0, warm=False)
    assert _events(acts) == ["g4_spent_leg_seed"]
    assert acts[0][1]["coverage_gap_s"] == 0.0 and acts[0][1]["frame_age_s"] == 900.0


def test_coverage_run_restarts_after_a_hole():
    """Review M2/M12/M18: the coverage proof is a CONTINUOUS run of priced ticks
    no more than T apart (T = max(2*interval, max_frame_age_s) = 120 s here).
    A session that ticked at 11:05 and then went dark until 11:25 (benched
    out, score flicker, host stall) has NOT covered the 11:10 -> 11:25 gap:
    a frame whose last bar ended 11:10 stays STALE for it (no seed), and the
    hole is counted; once the run is continuous again the frame is bridged."""
    le: dict = {}
    stale = dict(frame_hod=4.9297, hod_end=T0 + timedelta(minutes=3),
                 frame_last_end=T0 + timedelta(minutes=10), frame_age_s=900.0,
                 warm=False)
    # first priced tick at 11:05 (before the frame's last bar) — the run starts
    _u, acts = _tick(le, px=4.50, now=T0 + timedelta(minutes=5), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), frame_last_end=T0 + timedelta(minutes=6),
                     frame_age_s=0.0, warm=False)
    _apply(le, _u)
    assert acts == [] and le["spent_leg_tick_hod"]["coverage_breaks"] == 0
    # 20 min hole, then a tick at 11:25 against the stale frame: the run RESTARTS at 11:25
    _u, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=25), **stale)
    _apply(le, _u)
    assert acts == []  # coverage_gap 15 min > 120 s -> frame_stale -> no seed
    th = le["spent_leg_tick_hod"]
    assert th["coverage_breaks"] == 1
    assert th["first_tick_ts"] == (T0 + timedelta(minutes=25)).isoformat()
    assert th["px"] == 4.50  # the running high itself survives the hole
    # continuous ticks from 11:25 on cannot bridge a bar that ended 11:10 either
    _u, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=26), **stale)
    _apply(le, _u)
    assert acts == [] and le["spent_leg_tick_hod"]["coverage_breaks"] == 1
    # the same shape with NO hole (ticks every minute since 11:05) is bridged
    le2: dict = {}
    for k in range(5, 26):
        _u, acts = _tick(le2, px=4.50 if k < 20 else 4.34, now=T0 + timedelta(minutes=k),
                         frame_hod=4.9297, hod_end=T0 + timedelta(minutes=3),
                         frame_last_end=T0 + timedelta(minutes=10),
                         frame_age_s=max(0.0, (k - 10) * 60.0), warm=False)
        _apply(le2, _u)
        if acts:
            break
    assert _events(acts) == ["g4_spent_leg_seed"] and acts[0][1]["coverage_breaks"] == 0
    assert le2["spent_leg_tick_hod"]["first_tick_ts"] == (T0 + timedelta(minutes=5)).isoformat()


def test_corrupt_marker_is_dropped_fail_open():
    le = {"g4_spent_leg": {"active": True, "hod": "garbage", "session_date_et": DAY}}
    up, acts = _tick(le, px=4.95, now=T0 + timedelta(minutes=14), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    assert acts == [] and up["g4_spent_leg"] is None


def test_le_is_not_mutated_by_the_pure_rule():
    le: dict = {"g4_reentry_escalation": 0}
    snapshot = dict(le)
    _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
          hod_end=T0 + timedelta(minutes=3), warm=False)
    assert le == snapshot


# ── live_runner wiring: AST guards (source, not regex) ──────────────────────

def _tick_fn() -> ast.FunctionDef:
    tree = ast.parse(inspect.getsource(lr))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "tick_live_session":
            return node
    raise AssertionError("tick_live_session not found")


def _with_parents(fn: ast.AST):
    parents: dict[int, ast.AST] = {}
    for parent in ast.walk(fn):
        for child in ast.iter_child_nodes(parent):
            parents[id(child)] = parent
    return parents


def _ancestor_if_tests(node: ast.AST, parents) -> list[str]:
    out = []
    cur = node
    while id(cur) in parents:
        cur = parents[id(cur)]
        if isinstance(cur, ast.If):
            out.append(ast.unparse(cur.test))
    return out


def _emit_sites(fn, event: str) -> list[ast.Call]:
    sites = []
    for node in ast.walk(fn):
        if (
            isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_emit"
            and len(node.args) >= 3 and isinstance(node.args[2], ast.Constant) and node.args[2].value == event
        ):
            sites.append(node)
    return sites


def _calls_named(fn, name: str) -> list[ast.Call]:
    return [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == name
    ]


def test_seed_and_clear_emit_sites_are_outside_the_g4_block_after_the_bench():
    fn = _tick_fn()
    parents = _with_parents(fn)
    bench_calls = _calls_named(fn, "evaluate_sticky_backside_bench")
    assert len(bench_calls) == 1
    bench_line = bench_calls[0].lineno
    g4_calls = _calls_named(fn, "reentry_escalation_decision")
    assert len(g4_calls) == 1
    for event in ("g4_spent_leg_seed", "g4_spent_leg_cleared"):
        sites = _emit_sites(fn, event)
        assert len(sites) == 1, (event, len(sites))
        site = sites[0]
        assert site.lineno > bench_line, event
        assert site.lineno < g4_calls[0].lineno, event
        tests = _ancestor_if_tests(site, parents)
        assert any(t == "_score_ok" for t in tests), (event, tests)
        assert not any("_trigger_ok" in t for t in tests), (event, tests)
        assert not any("chili_momentum_g4_reentry_escalation_enabled" in t for t in tests), (event, tests)


def test_call_sites_pass_the_aware_clock():
    fn = _tick_fn()

    def _kw(call: ast.Call, name: str):
        for kw in call.keywords:
            if kw.arg == name:
                return kw.value
        return None

    apply_calls = _calls_named(fn, "apply_spent_leg_tick")
    assert len(apply_calls) == 1
    now_v = _kw(apply_calls[0], "now_utc")
    assert isinstance(now_v, ast.Call) and isinstance(now_v.func, ast.Name)
    assert now_v.func.id == "_utcnow_aware", ast.unparse(now_v)
    bench_v = _kw(_calls_named(fn, "evaluate_sticky_backside_bench")[0], "now_utc")
    assert isinstance(bench_v, ast.Call) and bench_v.func.id == "_utcnow_aware", ast.unparse(bench_v)
    # the naive chokepoint is never the seed's clock
    for call in apply_calls:
        assert "_utcnow()" not in ast.unparse(call)


def _assigns_to(fn, name: str) -> list[ast.Assign]:
    return [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == name for t in n.targets)
    ]


def _names_in(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def test_g4_block_hands_the_marker_top_to_the_decision_and_annotates_the_block():
    """Structural (review M21), not a string pin: the prior_hwm handed to the
    decision is the name whose LAST assignment before the call is a max() over
    the prior HWM and the marker top, and that assignment is reachable (guarded
    only by the marker-active test, never by a constant-false branch)."""
    fn = _tick_fn()
    parents = _with_parents(fn)
    g4_call = _calls_named(fn, "reentry_escalation_decision")[0]
    hwm_kw = [kw for kw in g4_call.keywords if kw.arg == "prior_hwm"][0]
    assert isinstance(hwm_kw.value, ast.Name)
    hwm_name = hwm_kw.value.id
    assigns = [a for a in _assigns_to(fn, hwm_name) if a.lineno < g4_call.lineno]
    assert assigns, hwm_name
    last = assigns[-1]
    assert isinstance(last.value, ast.Call) and isinstance(last.value.func, ast.Name)
    assert last.value.func.id == "max", ast.unparse(last)
    assert {"_g4e_spent_hod", hwm_name} <= _names_in(last.value), ast.unparse(last)
    guards = _ancestor_if_tests(last, parents)
    assert not any(g in ("False", "0", "None") for g in guards), guards
    assert any("_g4e_spent_active" in g for g in guards), guards
    # the marker-active read is flag-gated (review M14): OFF ⇒ no hand-off
    active_assigns = [a for a in _assigns_to(fn, "_g4e_spent_active") if a.lineno < g4_call.lineno]
    active_guards = [t for a in active_assigns for t in _ancestor_if_tests(a, parents)]
    assert any("chili_momentum_g4_spent_leg_seed_enabled" in g for g in active_guards), active_guards
    blocked = _emit_sites(fn, "g4_reentry_escalation_blocked")
    assert len(blocked) == 1
    payload_src = ast.unparse(blocked[0].args[3])
    for key in ("spent_leg_seed", "spent_leg_hod", "hod_source", "suppressed_repeats"):
        assert f"'{key}'" in payload_src, key
    # once-per-reason (review M16): the emit is guarded by the dedupe decision
    blk_guards = _ancestor_if_tests(blocked[0], parents)
    assert any(g == "_g4e_blk_emit" for g in blk_guards), blk_guards


def test_runner_wiring_keys_and_kill_switch():
    """Review M18 / M1: (a) every ``_bench_dbg.get('<key>')`` the runner hands to
    apply_spent_leg_tick is a key session_frame_hod_debug actually emits — a
    rename would otherwise pass None on every tick and make the feature
    silently inert; (b) ``enabled`` is the FLAG (never the constant True) and
    the block also runs when a marker is present, so flag OFF still unwinds."""
    fn = _tick_fn()
    parents = _with_parents(fn)
    call = _calls_named(fn, "apply_spent_leg_tick")[0]
    emitted = set(session_frame_hod_debug(pd.DataFrame()).keys())
    handed = set()
    for kw in call.keywords:
        for n in ast.walk(kw.value):
            if (
                isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "get" and isinstance(n.func.value, ast.Name)
                and n.func.value.id == "_bench_dbg" and n.args
                and isinstance(n.args[0], ast.Constant)
            ):
                handed.add(str(n.args[0].value))
    assert handed, "runner reads no _bench_dbg keys"
    assert handed <= emitted, handed - emitted
    enabled_kw = [kw for kw in call.keywords if kw.arg == "enabled"][0]
    assert not isinstance(enabled_kw.value, ast.Constant), ast.unparse(enabled_kw.value)
    assert isinstance(enabled_kw.value, ast.Name)
    flag_assigns = _assigns_to(fn, enabled_kw.value.id)
    assert any("chili_momentum_g4_spent_leg_seed_enabled" in ast.unparse(a.value) for a in flag_assigns)
    guards = _ancestor_if_tests(call, parents)
    assert any("_spent_marker_active" in g and enabled_kw.value.id in g for g in guards), guards
    # emit BEFORE apply (review M7): the first _emit of the seed/clear precedes the le writes
    seed_site = _emit_sites(fn, "g4_spent_leg_seed")[0]
    commit_after = [
        c for c in _calls_named(fn, "_commit_le")
        if c.lineno > seed_site.lineno and c.lineno < _calls_named(fn, "reentry_escalation_decision")[0].lineno
    ]
    assert commit_after and min(c.lineno for c in commit_after) > seed_site.lineno


def test_marker_keys_survive_recycle():
    assert "g4_spent_leg" not in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "spent_leg_tick_hod" not in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "g4_reentry_escalation" not in lr._RECYCLE_ENTRY_STATE_KEYS


def test_shelf_candidate_record_is_et_day_scoped_and_emits_before_the_flag():
    """Review M17 / M7: the per-candidate shelf record is once per session per
    ET DAY (the flag stores the date), and the flag is written AFTER the emit."""
    fn = _tick_fn()
    sites = _emit_sites(fn, "shelf_registration_state")
    assert len(sites) == 1
    site = sites[0]
    flag_writes = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.Assign)
        and any(
            isinstance(t, ast.Subscript) and isinstance(t.slice, ast.Constant)
            and t.slice.value == "shelf_state_emitted" for t in n.targets
        )
    ]
    assert len(flag_writes) == 1
    assert flag_writes[0].lineno > site.lineno
    assert isinstance(flag_writes[0].value, ast.Name)  # the ET date, not True
    assert "'session_date_et'" in ast.unparse(site.args[3])


# ── CHANGE 1: COLD-START GUARD (A/B verdict 2026-09-02, JLHL w6) ─────────────

def test_hod_whose_bar_ended_before_the_window_is_unobserved():
    """The predicate half of CHANGE 1: a HOD bar that ENDED before the session /
    replay window opened is a price this session never watched."""
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34,
                        session_start=NOW - timedelta(minutes=8.0))  # window opens AFTER the bar
    assert (seed, dbg["reason"]) == (False, "spent_leg_hod_unobserved")
    assert dbg["session_start_utc"] is not None
    # the same HOD one second INSIDE the window seeds normally
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34,
                        session_start=NOW - timedelta(minutes=8.3, seconds=1))
    assert (seed, dbg["reason"]) == (True, "spent_leg_seed")
    # an unreadable / absent window start can never prove observation -> no seed
    for bad in (None, "", "not-a-time"):
        seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34, session_start=bad)
        assert (seed, dbg["reason"]) == (False, "spent_leg_hod_unobserved"), bad


def test_a_cold_session_cannot_arm_until_it_is_warm():
    """The uptime / tick half of CHANGE 1."""
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34, observed_since=NOW)
    assert (seed, dbg["reason"]) == (False, "spent_leg_session_cold_start")
    assert dbg["session_uptime_s"] == 0.0 and dbg["min_session_uptime_s"] == 60.0
    seed, _ = _decide(hod=4.9297, age_min=8.3, px=4.34,
                      observed_since=NOW - timedelta(seconds=59.9))
    assert seed is False
    seed, _ = _decide(hod=4.9297, age_min=8.3, px=4.34,
                      observed_since=NOW - timedelta(seconds=60))
    assert seed is True  # the floor binds verbatim
    # never having processed a priced tick is also cold
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34, observed_ticks=0)
    assert (seed, dbg["reason"]) == (False, "spent_leg_session_cold_start")
    seed, dbg = _decide(hod=4.9297, age_min=8.3, px=4.34, observed_since=None)
    assert (seed, dbg["reason"]) == (False, "spent_leg_session_cold_start")
    # floors of 0 disable the guard (the escape hatch the config documents)
    seed, _ = _decide(hod=4.9297, age_min=8.3, px=4.34, observed_since=NOW,
                      observed_ticks=0, min_session_uptime_s=0.0, min_observed_ticks=0)
    assert seed is True


def test_jlhl_cold_start_regression_no_seed_at_the_first_grid_step():
    """THE regression the A/B measured. JLHL 2026-09-02 window 10:02-11:48Z:
    arm B armed the WAIT at the FIRST grid step off a frame HOD 7.70 whose bar
    ENDED 09:25:00Z — 37.0 min before the window opened, inherited from the
    7200-minute warm-up, before the runner had seen a single live tick. It held
    25.35 min with blocks_while_seeded=122 and blocked a STRUCTURAL
    double_bottom_break_tick_ok: arm A's 10:20:04Z @7.09 entry, +28.10 USD /
    +1.515R, the best trade in the whole A/B set. Shape reproduced exactly:
    frame HOD 37 min old at session start, uptime 0."""
    win_start = datetime(2026, 9, 2, 10, 2, 0, tzinfo=UTC)
    hod_bar_end = datetime(2026, 9, 2, 9, 25, 0, tzinfo=UTC)  # 37.0 min before the window
    le: dict = {}
    up, acts = _tick(le, px=7.01, now=win_start + timedelta(seconds=1), frame_hod=7.70,
                     hod_end=hod_bar_end, frame_last_end=win_start,
                     session_start=win_start, warm=False)
    assert acts == [], acts               # NO seed at grid step 1
    _apply(le, up)
    assert "g4_spent_leg" not in le and "g4_reentry_escalation" not in le
    assert le["spent_leg_tick_hod"]["run_ticks"] == 1
    # and it stays refused for as long as that stale top is the effective HOD:
    # at +30 min the session is warm, but the 09:25 bar is still outside the window
    for secs in (60, 120, 1800):
        up, acts = _tick(le, px=7.01, now=win_start + timedelta(seconds=secs),
                         frame_hod=7.70, hod_end=hod_bar_end, frame_last_end=win_start,
                         session_start=win_start, warm=False)
        _apply(le, up)
        assert acts == [], (secs, acts)
    seed, dbg = spent_leg_seed_decision(
        cur_hod=7.70, hod_ts=hod_bar_end, hod_date_et=DAY, live_px=7.01,
        now_utc=win_start + timedelta(seconds=1), session_date_et=DAY,
        frame_age_s=0.0, coverage_gap_s=0.0, interval_s=60.0,
        session_start_utc=win_start, observed_since_utc=win_start + timedelta(seconds=1),
        observed_ticks=1,
    )
    assert (seed, dbg["reason"]) == (False, "spent_leg_hod_unobserved")
    assert "hod_age_min" not in dbg  # refused BEFORE the age/depth arithmetic
    # a top the session DID watch print (10:41 bar, seen at 10:50) still seeds
    le2: dict = {}
    up, acts = _tick(le2, px=7.39, now=win_start + timedelta(minutes=48), frame_hod=7.92,
                     hod_end=win_start + timedelta(minutes=39),
                     frame_last_end=win_start + timedelta(minutes=47),
                     session_start=win_start)
    assert _events(acts) == ["g4_spent_leg_seed"]
    assert acts[0][1]["hod"] == 7.92 and acts[0][1]["observed_ticks"] >= 1
    assert acts[0][1]["session_uptime_s"] >= 60.0


def test_window_start_is_the_earlier_of_arm_time_and_first_observed_tick():
    """The session row's ``started_at`` is stamped from ``datetime.utcnow`` — the
    WALL clock — while a replay runner ticks on the SIM clock, so an
    un-normalised window start sits hours ahead of every sim bar and refuses
    EVERY seed. apply_spent_leg_tick takes the earlier of the arm instant and
    the first tick of the observation run: a no-op in production (started_at is
    always <= the session's first tick) and the difference between a measurable
    rule and an inert one under replay."""
    from app.services.trading.momentum_neural.risk_policy import _spent_leg_window_start

    early, late = T0, T0 + timedelta(hours=9)
    assert _spent_leg_window_start(early, late) == early          # prod shape: no-op
    assert _spent_leg_window_start(late, early) == early          # replay shape: normalised
    assert _spent_leg_window_start(early, None) == early
    assert _spent_leg_window_start(None, early) is None           # fail-open, still refuses
    # MEASURED (JLHL arm B, 2026-09-02): the run restarted at 10:44:30 after 3
    # coverage breaks, four minutes AFTER the session's own 7.92 tick high at
    # 10:40:23. The window is the SESSION's first tick, which survives holes —
    # a top this session's own tape recorded must never become "unobserved".
    sess_first = datetime(2026, 9, 2, 10, 2, 0, tzinfo=UTC)
    tick_high = datetime(2026, 9, 2, 10, 40, 23, tzinfo=UTC)
    le0: dict = {}
    for k in range(0, 3):  # a continuous run from 10:02
        _apply(le0, _tick(le0, px=7.01, now=sess_first + timedelta(seconds=k),
                          frame_hod=7.10, hod_end=sess_first - timedelta(minutes=30),
                          session_start=sess_first, warm=False)[0])
    # ...a 20-minute hole restarts the RUN but not the session stamp
    _apply(le0, _tick(le0, px=7.39, now=tick_high + timedelta(minutes=4),
                      frame_hod=7.10, hod_end=sess_first - timedelta(minutes=30),
                      session_start=sess_first, warm=False)[0])
    th = le0["spent_leg_tick_hod"]
    assert th["coverage_breaks"] == 1
    assert th["first_tick_ts"] == (tick_high + timedelta(minutes=4)).isoformat()
    assert th["session_first_tick_ts"] == sess_first.isoformat()
    # the session's own earlier top is still inside the window and seeds once the
    # NEW run is warm again (60 s / 1 tick — ticks 60 s apart keep it continuous)
    up, acts = _tick(le0, px=7.39, now=tick_high + timedelta(minutes=5), frame_hod=7.92,
                     hod_end=tick_high, session_start=sess_first, warm=False)
    assert _events(acts) == ["g4_spent_leg_seed"], acts
    assert acts[0][1]["hod"] == 7.92 and acts[0][1]["session_uptime_s"] == 60.0
    assert acts[0][1]["coverage_breaks"] == 1  # the hole is recorded, not fatal
    # end to end: a wall-clock started_at 9 h in the sim future must NOT make the
    # seed inert — the run's own first tick is the window the session observed.
    le: dict = {}
    up, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3),
                     session_start=T0 + timedelta(hours=9))
    assert _events(acts) == ["g4_spent_leg_seed"]
    _apply(le, up)
    assert le["g4_spent_leg"]["hod"] == 4.9297
    # ...and the JLHL cold start is STILL refused under the same normalisation,
    # because the run's first tick is also inside the window (10:02Z), after the
    # 09:25Z bar END.
    win_start = datetime(2026, 9, 2, 10, 2, 0, tzinfo=UTC)
    le2: dict = {}
    _u, acts = _tick(le2, px=7.01, now=win_start + timedelta(seconds=1), frame_hod=7.70,
                     hod_end=datetime(2026, 9, 2, 9, 25, 0, tzinfo=UTC),
                     frame_last_end=win_start,
                     session_start=win_start + timedelta(hours=10), warm=False)
    assert acts == []


def test_run_ticks_reset_with_the_coverage_run_so_a_hole_re_arms_the_cold_start():
    """The uptime the guard measures is the CURRENT continuous run — the same
    run that bridges a stale frame — so a host stall / bench-out makes the
    session cold again instead of inheriting a stale warm clock."""
    le: dict = {}
    for k in range(0, 4):
        _apply(le, _tick(le, px=4.50, now=T0 + timedelta(seconds=30 * k), frame_hod=4.9297,
                         hod_end=T0 - timedelta(minutes=1), warm=False)[0])
    assert le["spent_leg_tick_hod"]["run_ticks"] == 4
    # 20 min hole -> run (and its tick count) restarts here
    _apply(le, _tick(le, px=4.34, now=T0 + timedelta(minutes=20), frame_hod=4.9297,
                     hod_end=T0 - timedelta(minutes=1), warm=False)[0])
    th = le["spent_leg_tick_hod"]
    assert th["coverage_breaks"] == 1 and th["run_ticks"] == 1
    assert th["first_tick_ts"] == (T0 + timedelta(minutes=20)).isoformat()


# ── CHANGE 3: STRUCTURAL OVERRIDE (A/B verdict 2026-09-02) ───────────────────

def test_structural_override_emit_site_lets_the_trigger_through():
    """CHANGE 3, AST (source, not regex): the override branch must (a) test the
    structural class and the marker, (b) NOT set _trigger_ok False, (c) leave
    the blocked path as the else, and (d) be gated on the seed having CREATED
    the level (seed_level_delta), so a real stop-out's ladder is untouched."""
    fn = _tick_fn()
    parents = _with_parents(fn)
    sites = _emit_sites(fn, "g4_spent_leg_seed_structural_override")
    assert len(sites) == 1
    site = sites[0]
    payload = ast.unparse(site.args[3])
    for key in ("blocked_trigger", "spent_leg_hod", "hod_source", "hod_age_min", "dd_pct"):
        assert f"'{key}'" in payload, key
    guards = _ancestor_if_tests(site, parents)
    assert any(g == "_g4e_override" for g in guards), guards
    # the branch that DOES block is the sibling else-if of the override branch
    blocked = _emit_sites(fn, "g4_reentry_escalation_blocked")[0]
    blk_guards = _ancestor_if_tests(blocked, parents)
    assert any("not _g4e_ok" in g for g in blk_guards), blk_guards
    # the override decision reads the structural class, the marker and the delta
    def _assigns_override(node: ast.AST) -> bool:
        return any(
            isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_g4e_override" for t in n.targets)
            for n in ast.walk(node)
        )

    src = " ".join(
        [
            ast.unparse(n.test) for n in ast.walk(fn)
            if isinstance(n, ast.If) and _assigns_override(n)
            and ast.unparse(n.test) != "_g4e_override"
        ] + [
            ast.unparse(n) for n in ast.walk(fn)
            if isinstance(n, ast.Assign)
            and any(isinstance(t, ast.Name) and t.id == "_g4e_override" for t in n.targets)
        ]
    )
    assert "structural_trigger_reasons()" in src
    assert "_g4e_spent_active" in src
    assert "seed_level_delta" in src
    assert "chili_momentum_g4_spent_leg_structural_override_enabled" in src
    # nothing in the override branch flips the trigger off
    branch = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If) and ast.unparse(n.test) == "_g4e_override"
    ]
    assert len(branch) == 1
    body_src = "\n".join(ast.unparse(s) for s in branch[0].body)
    assert "_trigger_ok" not in body_src and "g4_reentry_escalation_wait" not in body_src


def test_structural_override_is_exactly_the_block_the_seed_created():
    """The override's precondition, priced on the decision itself: at level 1
    (which ONLY the seed can create — seed_level_delta 1) a structural trigger
    under the seeded top is refused by reentry_escalation_decision, and WITHOUT
    the seed the same trigger reads level 0 = no_escalation = admitted. So
    letting it through restores arm A exactly and erodes nothing."""
    blocked, dbg_b = reentry_escalation_decision(
        enabled=True, escalation_level=1, structural_trigger=True, live_price=7.09,
        prior_hwm=7.70, prior_exit_price=None, prior_risk_dist=None, tape_accel=1.0,
    )
    assert blocked is False and dbg_b["reason"] != "no_escalation"
    arm_a, dbg_a = reentry_escalation_decision(
        enabled=True, escalation_level=0, structural_trigger=True, live_price=7.09,
        prior_hwm=None, prior_exit_price=None, prior_risk_dist=None, tape_accel=1.0,
    )
    assert arm_a is True and dbg_a["reason"] == "no_escalation"
    # a REAL stop-out level (the seed added nothing: delta 0) is NOT overridden —
    # the runner's guard is `seed_level_delta >= 1`, which is false here.
    le: dict = {"g4_reentry_escalation": 1}
    up, _acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                      hod_end=T0 + timedelta(minutes=3))
    _apply(le, up)
    assert le["g4_spent_leg"]["seed_level_delta"] == 0
    assert le["g4_spent_leg"]["structural_overrides"] == 0
    # ...whereas a seed on a clean ledger owns its level
    le2: dict = {}
    up, _acts = _tick(le2, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                      hod_end=T0 + timedelta(minutes=3))
    _apply(le2, up)
    assert le2["g4_spent_leg"]["seed_level_delta"] == 1


def test_structural_override_count_is_carried_out_on_the_clear():
    le: dict = {}
    _apply(le, _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))[0])
    le["g4_spent_leg"]["structural_overrides"] = 3  # what the runner increments
    _u, acts = _tick(le, px=4.95, now=T0 + timedelta(minutes=14), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    assert acts[0][1]["structural_overrides"] == 3


def test_runner_passes_the_cold_start_and_hysteresis_knobs():
    """Wiring guard: a knob the runner never hands down is a knob that ships
    inert. Every new config key must appear in the apply_spent_leg_tick call."""
    fn = _tick_fn()
    call = _calls_named(fn, "apply_spent_leg_tick")[0]
    src = ast.unparse(call)
    for key in (
        "chili_momentum_g4_spent_leg_min_session_uptime_s",
        "chili_momentum_g4_spent_leg_min_observed_ticks",
        "chili_momentum_g4_spent_leg_clear_drawdown_pct",
        "chili_momentum_g4_spent_leg_clear_min_dwell_s",
    ):
        assert key in src, key
    kw = {k.arg for k in call.keywords}
    assert {"session_start_utc", "min_session_uptime_s", "min_observed_ticks",
            "clear_dd_pct", "clear_min_dwell_s"} <= kw, kw
    start_kw = [k for k in call.keywords if k.arg == "session_start_utc"][0]
    assert "started_at" in ast.unparse(start_kw.value)


def test_ships_on():
    s = Settings()
    assert s.chili_momentum_g4_spent_leg_seed_enabled is True
    assert (
        s.chili_momentum_g4_spent_leg_min_hod_age_min,
        s.chili_momentum_g4_spent_leg_min_drawdown_pct,
        s.chili_momentum_g4_spent_leg_max_frame_age_s,
    ) == (5.0, 5.0, 120.0)
    # CHANGE 1 / 2 / 3 — LIVE + ON, no dark flags
    assert (
        s.chili_momentum_g4_spent_leg_min_session_uptime_s,
        s.chili_momentum_g4_spent_leg_min_observed_ticks,
    ) == (60.0, 1)
    assert (
        s.chili_momentum_g4_spent_leg_clear_drawdown_pct,
        s.chili_momentum_g4_spent_leg_clear_min_dwell_s,
    ) == (3.5, 20.0)
    assert s.chili_momentum_g4_spent_leg_clear_drawdown_pct < s.chili_momentum_g4_spent_leg_min_drawdown_pct
    assert s.chili_momentum_g4_spent_leg_structural_override_enabled is True
