"""EXIT VERDICT G -- the PURE module on synthetic tapes (2026-09-10, [21]/[44]/[47] + Amendments).

DB-free. Every rule in `exit_verdict.py` judged on hand-built prints:
  * the two floors (feature 3, binding 4) and what `binding` names when each holds;
  * the three conditions of D, each failing ALONE names itself; all three => `all_three`;
  * the leg high is the FIRST occurrence on a tied max and the high print is EXCLUDED;
  * G: the accel rollover fires only for prev > 0, now <= 0 AND the last print > entry;
    every single failing condition names itself;
  * [65] the tick deadman base = max(resting stop, entry - median continued-pullback depth of
    the ledger's COMPLETED cycles); every named fallback (no ledger / not caught up / no cycle
    / candidate outside (0, entry)) is the resting stop and says why; the level is monotone in
    the resting stop and always below the entry; the depths are read from the REAL scanner's
    `to_dict()`;
  * the old count-half base (now receipt context) picks the first of swing_low_prev /
    swing_low_now / buy_support_px strictly below the entry, else the resting stop; the
    MONOTONE ratchet primitive never lowers and refuses a level at or above the last print;
  * the held-print walk exits on the first print <= level, moves the leg high on a strictly
    higher print, counts prints since entry / since high, and reports the frontier as the
    LAST WALKED print (never anything it did not evaluate);
  * the phase table (armed / exit_pending / exited) accepts every allowed edge and raises on
    every other pair (full product); no partial phase exists;
  * exit_fraction = 1.0 is a reported constant with the 78-leg derivation.

Runnable: pytest tests/test_exit_verdict_f_pure.py -v
"""
from __future__ import annotations

import inspect
import itertools
from datetime import datetime, timedelta

import pytest

from app.services.trading.momentum_neural import exit_verdict as EV

T0 = datetime(2026, 9, 10, 14, 0, 0)
E0 = 1_800_000_000.0


def _row(i: int, px: float, size: float = 100.0, *, bid: float | None = None, ask: float | None = None,
         dt_s: float = 0.5):
    """(price, size, bid, ask, epoch, observed_at, id) -- the readers' shape."""
    b = bid if bid is not None else px - 0.01
    a = ask if ask is not None else px + 0.01
    return (px, size, b, a, E0 + i * dt_s, T0 + timedelta(seconds=i * dt_s), 1000 + i)


def _tape(prices, sizes=None, *, aggressor=None, dt_s: float = 0.5):
    """Build rows; `aggressor` per print: +1 lifts the ask (buy), -1 hits the bid (sell)."""
    out = []
    for i, px in enumerate(prices):
        sz = sizes[i] if sizes else 100.0
        ag = aggressor[i] if aggressor else 0
        if ag > 0:
            out.append(_row(i, px, sz, bid=px - 0.02, ask=px, dt_s=dt_s))      # px >= ask => buy
        elif ag < 0:
            out.append(_row(i, px, sz, bid=px, ask=px + 0.02, dt_s=dt_s))      # px <= bid => sell
        else:
            out.append(_row(i, px, sz, dt_s=dt_s))
    return out


# ── floors ─────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("n", [0, 1, 2])
def test_below_the_feature_floor_is_no_verdict_and_names_the_floor(n):
    v = EV.since_high_verdict(_tape([1.0] * n), window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "feature_floor_n_lt_3"
    assert v["n_since_high"] == n
    assert v["floor_prints"] == 4 and v["feature_floor"] == 3


def test_three_prints_clear_the_feature_floor_but_not_the_count_halves_floor():
    v = EV.since_high_verdict(_tape([1.0, 0.99, 0.98]), window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "count_halves_floor_n_lt_4"
    assert v["n_ticks"] == 3
    assert v["swing_low_now"] is None and v["buy_share_delta"] is None


def test_feature_none_is_named():
    # zero sizes => the feature function returns None (zero total volume)
    v = EV.since_high_verdict(_tape([1.0, 1.0, 1.0, 1.0], sizes=[0, 0, 0, 0]),
                              window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False and v["binding"] == "feature_none"


# ── the three conditions of D ──────────────────────────────────────────────────

def _fires_tape():
    """4 prints: front half buyer-heavy and high, back half seller-heavy and lower."""
    return _tape([1.00, 1.00, 0.98, 0.97], sizes=[300, 300, 100, 100], aggressor=[1, 1, -1, -1])


def test_the_minimal_four_print_fire():
    v = EV.since_high_verdict(_fires_tape(), window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is True and v["binding"] == "all_three"
    assert v["signed_tape_accel"] < 0 and v["buy_share_delta"] < 0
    assert v["swing_low_now"] < v["swing_low_prev"]
    assert v["n_since_high"] == 4


def test_accel_failing_alone_names_itself():
    # back half buys MORE volume (accel > 0) but its buy SHARE falls and it prints a lower low
    rows = _tape([1.00, 1.00, 0.98, 0.97], sizes=[100, 100, 900, 100], aggressor=[1, 1, 1, -1])
    v = EV.since_high_verdict(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "accel_not_negative", v
    assert v["failed"] == ["accel_not_negative"]


def test_buy_share_delta_failing_alone_names_itself():
    # back half: less raw buy volume (accel < 0) but a HIGHER buy share; lower low still true
    rows = _tape([1.00, 1.00, 0.98, 0.97], sizes=[500, 500, 100, 100], aggressor=[1, -1, 1, 1])
    v = EV.since_high_verdict(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "buy_share_delta_not_negative", v


def test_no_lower_low_failing_alone_names_itself():
    # sellers took the back half but the back half's low is NOT below the front half's low
    rows = _tape([1.00, 0.97, 0.99, 0.98], sizes=[300, 300, 100, 100], aggressor=[1, 1, -1, -1])
    v = EV.since_high_verdict(rows, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["fired"] is False
    assert v["binding"] == "no_lower_low", v


# ── the since-high anchor ──────────────────────────────────────────────────────

def test_leg_high_is_the_first_occurrence_on_a_tied_max():
    rows = _tape([1.00, 1.05, 1.02, 1.05, 1.01])
    hi = EV.leg_high_print(rows)
    assert hi["price"] == 1.05 and hi["index"] == 1 and hi["id"] == 1001
    assert hi["tie_rule"] == "first_occurrence"
    # the feature dict's own `prints_since_high` uses the NEWEST occurrence -- not this rule
    since = rows[hi["index"] + 1:]
    assert len(since) == 3   # the high print itself is EXCLUDED; the later tie is COUNTED


def test_the_high_print_is_excluded_from_the_window():
    rows = _tape([1.00, 1.05, 1.00, 0.99, 0.98, 0.97], sizes=[100, 900, 300, 300, 100, 100],
                 aggressor=[1, 1, 1, 1, -1, -1])
    hi = EV.leg_high_print(rows)
    since = rows[hi["index"] + 1:]
    v = EV.since_high_verdict(since, window_s=15.0, tick_rate_floor_pctile=0.0)
    assert v["n_since_high"] == 4
    assert v["fired"] is True


def test_leg_high_ignores_unreadable_prices():
    rows = [(None, 1, None, None, E0, T0, 1), ("x", 1, None, None, E0, T0, 2)] + _tape([0.5])
    assert EV.leg_high_print(rows)["price"] == 0.5
    assert EV.leg_high_print([]) is None


# ── G: the accel rollover while the print is above entry ───────────────────────

def test_the_rollover_fires_only_from_positive_to_non_positive_above_entry():
    g = EV.accel_rollover(accel_prev=1200.0, accel_now=-30.0, last_print=10.35, entry_px=10.0)
    assert g["fired"] is True and g["binding"] == "rollover_above_entry"
    assert g["accel_prev"] == 1200.0 and g["accel_now"] == -30.0
    assert g["last_print"] == 10.35 and g["entry_px"] == 10.0
    # exactly zero counts as rolled over (`acc <= 0`, the script's comparison)
    assert EV.accel_rollover(accel_prev=5.0, accel_now=0.0, last_print=10.35, entry_px=10.0)["fired"] is True


@pytest.mark.parametrize("kw,binding", [
    (dict(accel_prev=1200.0, accel_now=None, last_print=10.35, entry_px=10.0), "accel_missing"),
    (dict(accel_prev=None, accel_now=-30.0, last_print=10.35, entry_px=10.0), "no_previous_evaluation"),
    (dict(accel_prev=0.0, accel_now=-30.0, last_print=10.35, entry_px=10.0), "prev_not_positive"),
    (dict(accel_prev=-5.0, accel_now=-30.0, last_print=10.35, entry_px=10.0), "prev_not_positive"),
    (dict(accel_prev=1200.0, accel_now=8.0, last_print=10.35, entry_px=10.0), "now_still_positive"),
    (dict(accel_prev=1200.0, accel_now=-30.0, last_print=None, entry_px=10.0), "no_print"),
    (dict(accel_prev=1200.0, accel_now=-30.0, last_print=10.35, entry_px=None), "no_print"),
    (dict(accel_prev=1200.0, accel_now=-30.0, last_print=10.0, entry_px=10.0), "print_not_above_entry"),
    (dict(accel_prev=1200.0, accel_now=-30.0, last_print=9.90, entry_px=10.0), "print_not_above_entry"),
])
def test_every_single_failing_condition_of_g_names_itself(kw, binding):
    g = EV.accel_rollover(**kw)
    assert g["fired"] is False and g["binding"] == binding, g


def test_g_reads_junk_as_missing_not_as_a_fire():
    g = EV.accel_rollover(accel_prev="x", accel_now="y", last_print="z", entry_px="w")
    assert g["fired"] is False and g["binding"] == "accel_missing"


# ── the tick deadman: base, candidate, MONOTONE ratchet ───────────────────────

def test_tick_deadman_base_picks_the_first_of_the_three_strictly_below_entry():
    feats = {"swing_low_prev": 9.80, "swing_low_now": 9.70, "buy_support_px": 9.60}
    assert EV.tick_deadman_base(feats, entry_px=10.0, resting_stop=9.0) == (9.80, "swing_low_prev")
    feats = {"swing_low_prev": 10.10, "swing_low_now": 9.70, "buy_support_px": 9.60}
    assert EV.tick_deadman_base(feats, entry_px=10.0, resting_stop=9.0) == (9.70, "swing_low_now")
    feats = {"swing_low_prev": None, "swing_low_now": 10.5, "buy_support_px": 9.95}
    assert EV.tick_deadman_base(feats, entry_px=10.0, resting_stop=9.0) == (9.95, "buy_support_px")


def test_tick_deadman_base_falls_back_to_the_resting_stop():
    assert EV.tick_deadman_base(None, entry_px=10.0, resting_stop=9.0) == (9.0, "resting_stop")
    assert EV.tick_deadman_base({"swing_low_prev": 10.2}, entry_px=10.0, resting_stop=9.0) == (9.0, "resting_stop")
    assert EV.tick_deadman_base({}, entry_px=10.0, resting_stop=None) == (None, "none")


# ── [65] the base from the tape's own CONTINUED pullbacks ─────────────────────

def _ledger(depth_rows, *, caught_up=True):
    return {"pullback_frac": 0.5, "n_prints": 900, "n_cycles": len(depth_rows),
            "cycles": [{"k": i, "hi": hi, "pb_low": lo} for i, (hi, lo) in enumerate(depth_rows)],
            "last_observed_at": "2026-09-10T13:59:58", "day": "2026-09-10",
            "feed": {"caught_up": caught_up, "reads": 1}}


def test_cont_base_is_entry_minus_the_median_completed_cycle_depth():
    st = _ledger([(10.50, 10.20), (10.80, 10.45), (11.00, 10.70)])        # 0.30 / 0.35 / 0.30
    b = EV.tick_deadman_cont_base(st, entry_px=10.0, resting_stop=9.0)
    assert b["level"] == pytest.approx(9.70) and b["level_source"] == "cont_depth_p50"
    assert b["binding"] == "cont_depth_p50" and b["fallback_reason"] is None
    assert b["cont_depth_p50"] == pytest.approx(0.30) and b["n_cycles"] == 3
    assert b["depths"] == pytest.approx([0.30, 0.35, 0.30])
    assert b["risk_R"] == pytest.approx(1.0) and b["distance_R"] == pytest.approx(0.30)
    assert b["ledger"] == {"day": "2026-09-10", "through": "2026-09-10T13:59:58", "n_prints": 900,
                           "n_cycles_total": 3, "pullback_frac": 0.5, "caught_up": True,
                           "expected_day": None}
    assert b["completed_pivot_claim"] is False and b["statistic"] == "median_of_completed_cycle_depths"
    # an even count: the median of the two middle depths (statistics.median)
    st = _ledger([(10.5, 10.4), (10.5, 10.3), (10.5, 10.2), (10.5, 10.0)])    # 0.1 / 0.2 / 0.3 / 0.5
    assert EV.tick_deadman_cont_base(st, entry_px=10.0, resting_stop=9.0)["level"] == pytest.approx(9.75)


def test_a_resting_stop_at_or_above_the_candidate_is_the_floor_and_says_so():
    st = _ledger([(10.50, 10.20), (10.80, 10.45), (11.00, 10.70)])
    b = EV.tick_deadman_cont_base(st, entry_px=10.0, resting_stop=9.80)
    assert b["level"] == 9.80 and b["level_source"] == "resting_stop"
    assert b["binding"] == "resting_stop_at_or_above_cont_depth_p50" and b["fallback_reason"] is None
    assert b["cont_candidate"] == pytest.approx(9.70) and b["distance_R"] == pytest.approx(1.0)
    b = EV.tick_deadman_cont_base(st, entry_px=10.0, resting_stop=None)     # no resting stop: the candidate
    assert b["level"] == pytest.approx(9.70) and b["risk_R"] is None and b["distance_R"] is None


@pytest.mark.parametrize("state,reason", [
    (None, "no_tape_cycle_state"),
    ("junk", "no_tape_cycle_state"),
    (_ledger([(10.5, 10.2)], caught_up=False), "tape_cycle_ledger_not_caught_up"),
    ({**_ledger([(10.5, 10.2)]), "feed": None}, "tape_cycle_ledger_not_caught_up"),
    (_ledger([]), "no_completed_cycles"),
    (_ledger([("x", 10.2), (10.2, 10.5), (10.5, 0.0), (10.5, None)]), "no_completed_cycles"),
    (_ledger([(30.0, 5.0)]), "cont_candidate_not_below_entry"),                 # depth 25 > entry 10
])
def test_every_named_fallback_is_the_resting_stop_and_names_itself(state, reason):
    b = EV.tick_deadman_cont_base(state, entry_px=10.0, resting_stop=9.0)
    assert b["level"] == 9.0 and b["level_source"] == "resting_stop"
    assert b["fallback_reason"] == reason and b["binding"] == "named_fallback_resting_stop"
    b = EV.tick_deadman_cont_base(state, entry_px=10.0, resting_stop=None)
    assert b["level"] is None and b["level_source"] == "none" and b["binding"] == "named_fallback_none"
    assert b["fallback_reason"] == reason


def test_a_ledger_from_another_session_day_is_a_named_fallback():
    st = _ledger([(10.50, 10.20), (10.80, 10.45), (11.00, 10.70)])            # day 2026-09-10
    same = EV.tick_deadman_cont_base(st, entry_px=10.0, resting_stop=9.0, expected_day="2026-09-10")
    assert same["level_source"] == "cont_depth_p50" and same["ledger"]["expected_day"] == "2026-09-10"
    other = EV.tick_deadman_cont_base(st, entry_px=10.0, resting_stop=9.0, expected_day="2026-09-11")
    assert other["fallback_reason"] == "tape_cycle_ledger_other_day" and other["level"] == 9.0


def test_an_unreadable_entry_or_stop_is_never_guessed():
    st = _ledger([(10.50, 10.20)])
    b = EV.tick_deadman_cont_base(st, entry_px=None, resting_stop=9.0)
    assert b["fallback_reason"] == "entry_unreadable" and b["level"] == 9.0
    b = EV.tick_deadman_cont_base(st, entry_px="junk", resting_stop="junk")
    assert b["fallback_reason"] == "entry_unreadable" and b["level"] is None
    b = EV.tick_deadman_cont_base(st, entry_px=10.0, resting_stop=-1.0)       # non-positive stop = none
    assert b["resting_stop"] is None and b["level"] == pytest.approx(9.70)


@pytest.mark.parametrize("rest", [None, 8.0, 9.0, 9.5, 9.69, 9.70, 9.71, 9.9, 9.99])
def test_the_level_is_monotone_in_the_resting_stop_and_always_below_the_entry(rest):
    st = _ledger([(10.50, 10.20), (10.80, 10.45), (11.00, 10.70)])
    b = EV.tick_deadman_cont_base(st, entry_px=10.0, resting_stop=rest)
    assert b["level"] < 10.0
    assert b["level"] == pytest.approx(max(9.70, rest if rest is not None else 0.0))
    if rest is not None:
        assert b["level"] >= rest


def test_the_depths_are_read_from_the_real_scanners_ledger():
    """Key compatibility with `tape_cycles.PullbackCycleScanner.to_dict()` (the live ledger)."""
    from app.services.trading.momentum_neural.tape_cycles import PullbackCycleScanner

    prices = [10.0, 10.2, 10.5, 10.35, 10.2, 10.6, 10.8, 10.6, 10.45, 10.9, 11.0, 10.8, 10.7, 11.1]
    sc = PullbackCycleScanner(0.5)
    sc.feed((T0 + timedelta(seconds=i), 5000 + i, px, 100.0, px - 0.01, px + 0.01)
            for i, px in enumerate(prices))
    assert sc.n_cycles == 3
    st = sc.to_dict()
    st["feed"] = {"caught_up": True}
    want = [c["hi"] - c["pb_low"] for c in sc.cycles]
    assert EV.continued_pullback_depths(st) == pytest.approx(want)
    assert want == pytest.approx([0.30, 0.35, 0.30])
    b = EV.tick_deadman_cont_base(st, entry_px=11.05, resting_stop=10.5)
    assert b["level"] == pytest.approx(11.05 - 0.30) and b["ledger"]["n_cycles_total"] == 3


def test_the_ratchet_candidate_is_the_first_non_null_of_the_three_keys():
    assert EV.swing_low_candidate({"swing_low_prev": 9.8, "swing_low_now": 9.9}) == (9.8, "swing_low_prev")
    assert EV.swing_low_candidate({"swing_low_prev": None, "swing_low_now": 9.9}) == (9.9, "swing_low_now")
    assert EV.swing_low_candidate({"swing_low_prev": 0.0, "swing_low_now": None, "buy_support_px": 9.5}) == (9.5, "buy_support_px")
    assert EV.swing_low_candidate({}) == (None, None)
    assert EV.swing_low_candidate(None) == (None, None)


def test_the_ratchet_never_lowers_and_rises_without_a_new_high():
    # a higher completed swing low arrives with NO new high (the last print is flat): it rises
    assert EV.tick_deadman_ratchet(9.5, 9.7, last_print=10.2) == (9.7, True)
    # never lowers, never re-stamps an equal level
    assert EV.tick_deadman_ratchet(9.7, 9.6, last_print=10.2) == (9.7, False)
    assert EV.tick_deadman_ratchet(9.7, 9.7, last_print=10.2) == (9.7, False)
    # junk / None / non-positive candidates are refused
    assert EV.tick_deadman_ratchet(9.7, None, last_print=10.2) == (9.7, False)
    assert EV.tick_deadman_ratchet(9.7, "junk", last_print=10.2) == (9.7, False)
    assert EV.tick_deadman_ratchet(9.7, 0.0, last_print=10.2) == (9.7, False)
    # no level yet => the candidate becomes the level
    assert EV.tick_deadman_ratchet(None, 9.1, last_print=10.2) == (9.1, True)


def test_the_ratchet_refuses_a_level_at_or_above_the_last_print():
    """The script's `v < px`: a level at or above the tape would fire on the very next print."""
    assert EV.tick_deadman_ratchet(9.5, 10.2, last_print=10.2) == (9.5, False)
    assert EV.tick_deadman_ratchet(9.5, 10.3, last_print=10.2) == (9.5, False)
    assert EV.tick_deadman_ratchet(9.5, 10.19, last_print=10.2) == (10.19, True)
    # With no print there is no evidence that the candidate is below the market.
    assert EV.tick_deadman_ratchet(9.5, 10.3) == (9.5, False)


@pytest.mark.parametrize("last_print", [None, "invalid", float("nan"), float("inf"), -float("inf")])
def test_ratchet_preserves_floor_without_a_finite_observed_print(last_print):
    assert EV.tick_deadman_ratchet(9.5, 10.3, last_print=last_print) == (9.5, False)
    assert EV.tick_deadman_ratchet(None, 10.3, last_print=last_print) == (None, False)


@pytest.mark.parametrize("candidate", [float("nan"), float("inf"), -float("inf")])
def test_ratchet_rejects_nonfinite_candidates(candidate):
    assert EV.tick_deadman_ratchet(9.5, candidate, last_print=10.4) == (9.5, False)


# ── the held-print walk ────────────────────────────────────────────────────────

def test_the_walk_exits_on_the_first_print_at_or_below_the_level_and_stops_there():
    batch = _tape([10.2, 10.1, 9.9, 9.8, 10.5])
    out = EV.walk_held_prints(batch, level=9.9, leg_high={"price": 10.0, "observed_at": T0, "id": 1})
    assert out["exit_print"]["price"] == 9.9 and out["exit_print"]["id"] == 1002
    assert out["prints_walked"] == 3                       # the walk STOPS at the crossing
    assert out["frontier"] == (batch[2][5], batch[2][6])   # ...and the frontier is THAT print
    assert out["leg_high"]["price"] == 10.2 and out["leg_high"]["id"] == 1000
    assert out["last_print"] == 9.9


def test_the_walk_moves_the_leg_high_on_a_strictly_higher_print_and_counts_since_high():
    batch = _tape([10.0, 10.4, 10.4, 10.1, 10.3])
    out = EV.walk_held_prints(batch, level=None, leg_high=None)
    assert out["exit_print"] is None
    assert out["leg_high"]["price"] == 10.4 and out["leg_high"]["id"] == 1001   # FIRST occurrence
    assert out["prints_walked"] == 5 and out["prints_since_high"] == 3        # the tie is counted
    assert out["frontier"] == (batch[-1][5], batch[-1][6])
    # the count carries across batches and restarts on a new high
    out2 = EV.walk_held_prints(_tape([10.2, 10.6, 10.5]), level=None, leg_high=out["leg_high"],
                               prints_since_high=out["prints_since_high"])
    assert out2["leg_high"]["price"] == 10.6 and out2["prints_since_high"] == 1


def test_the_walk_reports_no_frontier_for_an_empty_or_unreadable_batch():
    out = EV.walk_held_prints([], level=9.9, leg_high=None)
    assert out["frontier"] is None and out["prints_walked"] == 0 and out["last_print"] is None
    junk = [(None, 1, None, None, E0, T0, 1), ("x", 1, None, None, E0, T0, 2)]
    out = EV.walk_held_prints(junk, level=9.9, leg_high=None)
    assert out["frontier"] is None and out["prints_walked"] == 0 and out["leg_high"] is None


def test_the_walk_with_no_level_never_exits_but_still_tracks_the_high():
    out = EV.walk_held_prints(_tape([10.0, 10.4, 10.1]), level=None, leg_high=None)
    assert out["exit_print"] is None and out["leg_high"]["price"] == 10.4


# ── the phase table ────────────────────────────────────────────────────────────

ALLOWED = {
    (None, "armed"), ("armed", "armed"), ("armed", "exit_pending"), ("armed", "exited"),
    ("exit_pending", "exited"),
}


@pytest.mark.parametrize("frm,to", sorted(itertools.product([None, *EV.PHASES], EV.PHASES), key=str))
def test_the_phase_table_over_the_full_product(frm, to):
    if (frm, to) in ALLOWED:
        EV.assert_verdict_transition(frm, to)
    else:
        with pytest.raises(ValueError):
            EV.assert_verdict_transition(frm, to)


def test_unknown_and_partial_phases_raise():
    for bad in ("flying", "runner", "partial_shrink_pending", "partial_sell_pending", "runner_exit_pending"):
        with pytest.raises(ValueError):
            EV.assert_verdict_transition("armed", bad)
        with pytest.raises(ValueError):
            EV.assert_verdict_transition(bad, "armed")


def test_the_bypass_sets_are_what_the_amendment_says():
    assert EV.PHASES == ("armed", "exit_pending", "exited")
    assert EV.TRAIL_BYPASS_PHASES == {"armed", "exit_pending"}
    assert EV.FIRST_TARGET_BYPASS_PHASES == {"exit_pending"}
    assert "armed" not in EV.FIRST_TARGET_BYPASS_PHASES   # the target whole-exit stays reachable
    assert not hasattr(EV, "PENDING_PHASES") and not hasattr(EV, "partial_split")
    assert not hasattr(EV, "walk_runner_prints") and not hasattr(EV, "SELL_FRACTION_FALLBACK")


# ── the receipt shapes and the derivations ─────────────────────────────────────

def test_verdict_receipt_has_a_stable_key_set():
    keys = set(EV.verdict_receipt(None))
    assert keys == {"fired", "binding", "n_since_high", "signed_tape_accel", "buy_share_delta",
                    "swing_low_now", "swing_low_prev", "gap_restricted", "n_ticks", "window_s",
                    "floor_prints", "feature_floor", "feature_contract", "window_kind", "window_prints", "split",
                    "span_s", "half_print_counts", "gap_trim_s", "gap_trim_basis", "gap_trim_window_p90_s",
                    "gap_trim_mult", "print_age_s", "print_age_bound_s", "print_stale", "tick_rate_basis",
                    "count_parameter_sources", "calibration", "completed_pivot_claim", "withheld"}
    assert EV.verdict_receipt(None)["fired"] is False


def test_rollover_receipt_has_a_stable_key_set():
    assert set(EV.rollover_receipt(None)) == {"fired", "binding", "accel_prev", "accel_now",
                                              "last_print", "entry_px", "feature_contract_id", "previous_feature_contract_id",
                                              "withheld", "condition_fired_before_freshness"}
    assert EV.rollover_receipt(None)["fired"] is False


def test_the_module_is_pure_and_the_derivations_carry_the_measurement():
    src = inspect.getsource(EV)
    assert "datetime.now(" not in src and "datetime.utcnow(" not in src
    assert "getattr(settings" not in src and "config import" not in src   # no settings read
    assert "sqlalchemy" not in src and "iqfeed_trade_ticks" not in src
    # the two tables of record: the 35-leg G table and the 78-leg sell-all table
    for tok in ("2026-09-10", "-697.87", "-202.88", "-2.32", "11/35", "-1,216.28", "-59.25",
                "+157.52", "+321.62", "-164.11", "+271.45", "spike 28 / D 43 / deadman 7",
                "16/34", "-304.93", "-468.88"):
        assert tok in EV._EXIT_VERDICT_DERIVATION, tok
    # the superseded partial-era numbers are cited nowhere
    for tok in ("-502.18", "-495.7", "24/32", "8/32", "F(0.75"):
        assert tok not in src, tok
    assert EV.EXIT_FRACTION == 1.0
    for tok in ("1.0", "78 live Alpaca legs", "+157.52", "-59.25", "-1,216.28", "+217",
                "55/71", "unmeasured, not refuted", "Not a knob"):
        assert tok in EV._EXIT_FRACTION_DERIVATION, tok
    for tok in ("> 0", "<= 0", "last print > the entry fill", "11/35", "28/78", "N=458", "255",
                "EVERY held tick"):
        assert tok in EV._ACCEL_ROLLOVER_DERIVATION, tok
    # [65] the base, the named fallbacks, the ratchet decision and the measurement of record
    for tok in ("[65]", "max(resting_stop, entry - median(hi - pb_low))", "COMPLETED cycles",
                "CYCLE_PULLBACK_FRAC_BASE", "position.stop_price", "Named fallback",
                "not caught up", "NO pre-trigger ratchet", "#1408", "receipt context only",
                "swing_low_prev", "0.23 R", "0.31 R", "43/56", "81 legs / 34 symbol-days",
                "-429.64", "+79.57", "+509 [+217, +856]", "+338", "+304 [-41, +767]",
                "22 legs / 5 symbol-days", "+100 [-21, +320]", "+236 [+10, +470]",
                "+361 [+83, +762]", "1 of 103 legs", "15.3 s"):
        assert tok in EV._TICK_DEADMAN_DERIVATION, tok
    assert EV.TICK_DEADMAN_RATCHET_FALLBACK == "no_pre_trigger_ratchet_until_completed_swing_facts_wired"
    assert EV.FEATURE_FLOOR_PRINTS == 3 and EV.BINDING_FLOOR_PRINTS == 4
