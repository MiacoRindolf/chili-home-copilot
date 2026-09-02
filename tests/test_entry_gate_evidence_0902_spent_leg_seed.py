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
DAY = "2026-09-02"
# 07:10 ET on 2026-09-02 (the CANF#1 entry instant)
NOW = datetime(2026, 9, 2, 11, 10, 0, tzinfo=UTC)


def _decide(*, hod, age_min, px, now=NOW, hod_date=DAY, sess_date=DAY,
            frame_age_s=0.0, coverage_gap_s=None, interval_s=60.0, **kw):
    return spent_leg_seed_decision(
        cur_hod=hod,
        hod_ts=(now or NOW) - timedelta(minutes=age_min),
        hod_date_et=hod_date,
        live_px=px,
        now_utc=now,
        session_date_et=sess_date,
        frame_age_s=frame_age_s,
        coverage_gap_s=coverage_gap_s,
        interval_s=interval_s,
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
    seeded, not_seeded = [], []
    for sym, day, et_time, entry, pnl, hod, age, dd in rows:
        h, m = (int(x) for x in et_time.split(":"))
        now = datetime.fromisoformat(f"{day}T{h:02d}:{m:02d}:00-04:00").astimezone(UTC)
        seed, dbg = spent_leg_seed_decision(
            cur_hod=hod, hod_ts=now - timedelta(minutes=age), hod_date_et=day,
            live_px=entry, now_utc=now, session_date_et=day,
            frame_age_s=0.0, coverage_gap_s=0.0, interval_s=60.0,
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
    # July winners the 5-min / 5% floors protect: VEEE, VRAX, LUCY, SKYQ, RKTO, LHAI stay free.
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


def _tick(le, *, px, now, frame_hod, hod_end, frame_last_end=None, frame_age_s=0.0,
          enabled=True, symbol="CANF", session_date_et=DAY, hod_date_et=DAY):
    return apply_spent_leg_tick(
        le, symbol=symbol, enabled=enabled, px=px, tick_ts=now, now_utc=now,
        session_date_et=session_date_et,
        frame_hod=frame_hod, frame_hod_ts=hod_end, frame_hod_date_et=hod_date_et,
        frame_last_bar_end_ts=(frame_last_end if frame_last_end is not None else now),
        frame_age_s=frame_age_s, interval_s=60.0,
        min_age_min=5.0, min_dd_pct=5.0, max_frame_age_s=120.0,
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
    assert _events(acts) == ["g4_spent_leg_seed"]  # no phantom clear of yesterday's marker
    _apply(le, up)
    assert le["g4_spent_leg"]["hod"] == 4.9297 and le["g4_spent_leg"]["session_date_et"] == DAY
    assert le["spent_leg_tick_hod"]["session_date_et"] == DAY


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


def test_disabled_flag_still_clears_but_never_seeds():
    le: dict = {}
    _apply(le, _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))[0])
    up, acts = _tick(le, px=4.95, now=T0 + timedelta(minutes=14), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), enabled=False)
    assert _events(acts) == ["g4_spent_leg_cleared"]  # flipping OFF never strands a name
    le2: dict = {}
    up, acts = _tick(le2, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), enabled=False)
    assert acts == [] and "g4_spent_leg" not in up


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
                     frame_age_s=600.0)
    _apply(le, _u)
    assert acts == []
    # same stale frame, but the session's first tick was at 11:20 (10 min after the last bar):
    # gap 600 s > 120 s -> still stale
    _u, acts = _tick(le, px=4.34, now=T0 + timedelta(minutes=25), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), frame_last_end=T0 + timedelta(minutes=10),
                     frame_age_s=900.0)
    assert acts == []
    # a session that has ticked since BEFORE the frame's last bar bridges it
    le2 = {"spent_leg_tick_hod": {"px": 4.50, "ts": (T0 + timedelta(minutes=5)).isoformat(),
                                  "first_tick_ts": (T0 + timedelta(minutes=5)).isoformat(),
                                  "session_date_et": DAY}}
    _u, acts = _tick(le2, px=4.34, now=T0 + timedelta(minutes=25), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3), frame_last_end=T0 + timedelta(minutes=10),
                     frame_age_s=900.0)
    assert _events(acts) == ["g4_spent_leg_seed"]
    assert acts[0][1]["coverage_gap_s"] == 0.0 and acts[0][1]["frame_age_s"] == 900.0


def test_corrupt_marker_is_dropped_fail_open():
    le = {"g4_spent_leg": {"active": True, "hod": "garbage", "session_date_et": DAY}}
    up, acts = _tick(le, px=4.95, now=T0 + timedelta(minutes=14), frame_hod=4.9297,
                     hod_end=T0 + timedelta(minutes=3))
    assert acts == [] and up["g4_spent_leg"] is None


def test_le_is_not_mutated_by_the_pure_rule():
    le: dict = {"g4_reentry_escalation": 0}
    snapshot = dict(le)
    _tick(le, px=4.34, now=T0 + timedelta(minutes=10), frame_hod=4.9297,
          hod_end=T0 + timedelta(minutes=3))
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


def test_g4_block_hands_the_marker_top_to_the_decision_and_annotates_the_block():
    fn = _tick_fn()
    src = ast.unparse(fn)
    g4_call = _calls_named(fn, "reentry_escalation_decision")[0]
    hwm_kw = [kw for kw in g4_call.keywords if kw.arg == "prior_hwm"][0]
    assert ast.unparse(hwm_kw.value) == "_g4e_prior_hwm"
    assert "_g4e_prior_hwm = max(float(_g4e_prior_hwm or 0.0), float(_g4e_spent_hod))" in src
    blocked = _emit_sites(fn, "g4_reentry_escalation_blocked")
    assert len(blocked) == 1
    payload_src = ast.unparse(blocked[0].args[3])
    for key in ("spent_leg_seed", "spent_leg_hod", "hod_source"):
        assert f"'{key}'" in payload_src, key


def test_marker_keys_survive_recycle():
    assert "g4_spent_leg" not in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "spent_leg_tick_hod" not in lr._RECYCLE_ENTRY_STATE_KEYS
    assert "g4_reentry_escalation" not in lr._RECYCLE_ENTRY_STATE_KEYS


def test_ships_on():
    s = Settings()
    assert s.chili_momentum_g4_spent_leg_seed_enabled is True
    assert (
        s.chili_momentum_g4_spent_leg_min_hod_age_min,
        s.chili_momentum_g4_spent_leg_min_drawdown_pct,
        s.chili_momentum_g4_spent_leg_max_frame_age_s,
    ) == (5.0, 5.0, 120.0)
