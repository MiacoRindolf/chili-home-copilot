"""The tape activity floor must rank against a real distribution, in PRINTS.

TWO DEFECTS ARE UNDER TEST HERE.

1. THE FLOOR WAS A TAUTOLOGY. The sample used to be the two per-half rates, so
   ``sorted([front_rate, back_rate])`` had length 2 and the nearest-rank index
   ``int(fp * (len - 1))`` collapsed to 0 for every ``fp < 1.0``. The floor was
   therefore ``min(front_rate, back_rate)`` while ``tick_rate`` IS ``back_rate``,
   making ``tick_rate >= tick_rate_floor`` unconditionally true at every
   configured percentile — decision-inert, not merely permissive. Three consumers
   read that pair as a gate (``_l2_entry_confirm``, ``first_dip_tape_decision``
   at :2238/:2275, ``auto_arm`` at :701), so all three lost their activity leg
   silently. The single live refusal receipt in the entire book carries the two
   values byte-identical: ``tick_rate 4.7707 / tick_rate_floor 4.7707``
   (TNMG 2026-06-29).

2. THE WINDOW WAS COUPLED TO THE WALL CLOCK. Fifteen seconds is a different
   amount of tape on every name: on a fast name it is hundreds of prints, on a
   slow one it is two. Measured on our own book: CHILI has never entered on a new
   60-second high (0 of 73 doctrine winner legs), and 24 of those 73 (32.9%)
   printed their whole in-hold high within THREE seconds of the fill while
   carrying 48% of the capture gap. A window in seconds cannot see an event that
   resolves in three; a window in prints stretches and contracts with the tape.

The load-bearing test is :func:`test_the_same_prints_read_the_same_at_any_clock_speed`
— the same print sequence played fast and slow must read identically.

DB-free: ``_signed_tape_features`` is pure over the row tuples the SQL returns,
``(price, size, bid, ask, epoch_seconds)``.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural.entry_gates import _signed_tape_features


def _row(px: float, ts: float, size: float = 100.0, spread: float = 0.02):
    """One tape print, at the ask (so the aggressor sign is a buy)."""
    return (px, size, px - spread, px, ts)


def _tape(prices, *, start=1_000_000.0, step=0.10):
    """A print sequence on an even cadence of ``step`` seconds."""
    return [_row(px, start + i * step) for i, px in enumerate(prices)]


def _f(rows, pctile=0.5, window_s=15.0):
    return _signed_tape_features(
        rows, window_s=window_s, tick_rate_floor_pctile=pctile
    )


# ── 1. the tautology ────────────────────────────────────────────────────────────

def test_the_floor_is_ranked_against_more_than_two_samples():
    """The whole bug in one assertion: a 2-sample 'percentile' cannot rank."""
    out = _f(_tape([1.0 + i * 0.01 for i in range(40)]))
    assert out is not None
    assert out["tick_rate_floor_n"] > 2, (
        "with only two samples the nearest-rank index collapses to 0 and the "
        "floor degenerates to min(front, back), which tick_rate can never fall "
        "below"
    )


def test_a_decelerating_tape_can_now_fall_below_its_own_floor():
    """The refusal must be REACHABLE. Under the old code it never was.

    Front half prints fast, back half crawls. The current activity (the back
    half) is genuinely the quietest stretch of the window, so at a high
    percentile it must rank below its own recent distribution.
    """
    fast = [_row(1.0 + i * 0.001, 1_000_000.0 + i * 0.01) for i in range(60)]
    t0 = fast[-1][4]
    slow = [_row(1.06 + i * 0.001, t0 + (i + 1) * 2.0) for i in range(12)]
    out = _f(fast + slow, pctile=0.9)
    assert out is not None
    assert out["tick_rate"] < out["tick_rate_floor"], (
        "a tape that has decelerated to a crawl must be able to fail its own "
        "self-relative activity floor"
    )


def test_a_steady_tape_still_clears_its_own_floor():
    """The mirror: the fix must not flip the tautology from always-true to
    always-false. An evenly-printing tape is not 'inactive'."""
    out = _f(_tape([1.0 + i * 0.01 for i in range(40)]), pctile=0.5)
    assert out is not None
    assert out["tick_rate"] >= out["tick_rate_floor"]


def test_the_percentile_knob_actually_selects():
    """Turning the knob must move the floor. Under the old code it could not.

    Bursts and lulls, strictly monotonic and every gap well under the window's
    own half (the halt-gap restriction would otherwise truncate the sample).
    """
    ts = 1_000_000.0
    rows = []
    for i in range(60):
        ts += 0.02 if (i % 4) else 0.30  # three quick prints, then a pause
        rows.append(_row(1.0 + i * 0.005, ts))
    low = _f(rows, pctile=0.0)
    high = _f(rows, pctile=1.0)
    assert low is not None and high is not None
    assert high["tick_rate_floor"] > low["tick_rate_floor"], (
        "the configured percentile must change the floor it selects"
    )


# ── 2. position in our own move, counted in prints ──────────────────────────────

def test_prints_since_high_is_zero_when_the_newest_print_is_the_high():
    out = _f(_tape([1.0, 1.1, 1.2, 1.3, 1.4, 1.5]))
    assert out is not None
    assert out["prints_since_high"] == 0
    assert out["high_print_position"] == pytest.approx(0.0)


def test_the_high_being_old_in_prints_reads_as_a_spent_burst():
    """Rip, then fade: the high is the oldest print, the burst is spent."""
    out = _f(_tape([2.0, 1.9, 1.8, 1.7, 1.6, 1.5]))
    assert out is not None
    assert out["prints_since_high"] == 5
    assert out["high_print_position"] == pytest.approx(1.0)


def test_a_repeated_high_counts_from_the_newest_touch():
    """Re-tagging the high is not a spent burst — count from the LAST touch."""
    out = _f(_tape([1.5, 1.2, 1.5, 1.3, 1.4]))
    assert out is not None
    assert out["prints_since_high"] == 2


# ── 3. the property the whole change exists for ─────────────────────────────────

def test_the_same_prints_read_the_same_at_any_clock_speed():
    """THE LOAD-BEARING TEST.

    One print sequence, played at 100 prints/second and at one print every four
    seconds — a 400x difference in wall time for identical tape. Every
    print-indexed field must be identical. This is what a seconds-based window
    cannot do, and it is the reason the late-arrival separator did not reproduce
    out of sample: it was comparing different amounts of tape between names and
    calling them the same measurement.
    """
    prices = [1.0, 1.4, 1.2, 1.3, 1.25, 1.28, 1.26]
    fast = _f(_tape(prices, step=0.01), window_s=1.0)
    slow = _f(_tape(prices, step=4.0), window_s=600.0)
    assert fast is not None and slow is not None
    for key in ("prints_since_high", "high_print_position", "n_ticks",
                "tick_rate_floor_n"):
        assert fast[key] == slow[key], f"{key} moved with the clock, not the tape"


def test_the_floor_ranking_is_scale_free_across_clock_speeds():
    """The floor's VALUE scales with the clock (it is a rate), but the DECISION
    it produces must not: the same tape shape passes or fails identically."""
    prices = [1.0 + i * 0.005 for i in range(50)]
    fast = _f(_tape(prices, step=0.01), window_s=1.0, pctile=0.75)
    slow = _f(_tape(prices, step=1.0), window_s=600.0, pctile=0.75)
    assert fast is not None and slow is not None
    assert (fast["tick_rate"] >= fast["tick_rate_floor"]) is (
        slow["tick_rate"] >= slow["tick_rate_floor"]
    )


# ── 4. fail-open contract is preserved ──────────────────────────────────────────

def test_no_timestamps_leaves_the_floor_permissive():
    """Every caller documents fail-open on bad data; this leg must never
    manufacture a refusal from missing timestamps."""
    rows = [(1.0 + i * 0.01, 100.0, 0.99, 1.0, None) for i in range(8)]
    out = _f(rows)
    assert out is not None
    assert out["tick_rate_floor"] == 0.0
    assert out["tick_rate_floor_n"] == 0
    assert out["tick_rate"] >= out["tick_rate_floor"]


def test_too_few_prints_still_returns_none():
    """Unchanged contract: under three prints there is no tape to read."""
    assert _f(_tape([1.0, 1.1])) is None


# ── 5. swing lows split by COUNT, and where the buying actually happened ───────

def test_swing_lows_split_the_window_by_print_count_not_by_time():
    """Each half must hold the same NUMBER of prints, so a higher low is a
    comparison between equal populations however fast the tape was running."""
    out = _f(_tape([2.0, 1.8, 1.9, 2.1, 2.3, 2.2, 2.5, 2.4]))
    assert out is not None
    assert out["swing_low_prev"] == pytest.approx(1.8)   # min of the older four
    assert out["swing_low_now"] == pytest.approx(2.2)    # min of the newer four
    assert out["swing_low_now"] > out["swing_low_prev"]  # a higher low, in prints


def test_a_stepping_down_tape_reports_a_lower_low():
    out = _f(_tape([2.5, 2.4, 2.3, 2.2, 2.0, 1.9, 1.8, 1.7]))
    assert out is not None
    assert out["swing_low_now"] < out["swing_low_prev"]


def test_swing_lows_are_none_when_there_is_not_enough_tape_to_halve():
    out = _f(_tape([1.0, 1.1, 1.2]))
    assert out is not None
    assert out["swing_low_prev"] is None and out["swing_low_now"] is None


def test_buy_support_is_the_volume_weighted_price_of_the_lifts():
    """The level where the buying actually happened — the thing the 5m EMA-9 only
    stands in for. Sells must not drag it."""
    rows = [
        _row(10.00, 1_000_000.0, size=100.0),              # at the ask -> a buy
        _row(12.00, 1_000_000.1, size=300.0),              # at the ask -> a buy
        (8.00, 900.0, 8.00, 8.05, 1_000_000.2),            # at the bid -> a sell
    ]
    out = _f(rows)
    assert out is not None
    # (10*100 + 12*300) / 400 = 11.5, and the 8.00 sell is excluded entirely
    assert out["buy_support_px"] == pytest.approx(11.5)


def test_buy_support_is_none_when_nobody_lifted():
    rows = [(9.0 - i * 0.1, 100.0, 9.0 - i * 0.1, 9.05 - i * 0.1, 1_000_000.0 + i * 0.1)
            for i in range(6)]
    out = _f(rows)
    assert out is not None
    assert out["buy_support_px"] is None


def test_the_structure_features_also_ignore_the_clock():
    """Same property as the rest: identical prints, 400x apart in wall time."""
    prices = [2.0, 1.8, 1.9, 2.1, 2.3, 2.2, 2.5, 2.4]
    fast = _f(_tape(prices, step=0.01), window_s=1.0)
    slow = _f(_tape(prices, step=4.0), window_s=600.0)
    for key in ("swing_low_prev", "swing_low_now", "buy_support_px"):
        assert fast[key] == pytest.approx(slow[key]), key


# ── 6. buy share between equal-count halves: the window must not decide it ─────

def test_buy_share_delta_is_positive_when_the_buying_is_carrying():
    """Front half mixed, back half all lifts."""
    ts = 1_000_000.0
    rows = []
    for i in range(4):                                   # front: half of it sells
        px = 1.0 + i * 0.01
        rows.append((px, 100.0, px, px + 0.01, ts + i * 0.1) if i % 2
                    else (px, 100.0, px - 0.01, px, ts + i * 0.1))
    for i in range(4):                                   # back: every print at the ask
        px = 1.05 + i * 0.01
        rows.append((px, 100.0, px - 0.01, px, ts + (4 + i) * 0.1))
    out = _f(rows)
    assert out is not None
    assert out["buy_share_delta"] > 0


def test_buy_share_delta_is_negative_when_the_buying_fades():
    ts = 1_000_000.0
    rows = [(1.0 + i * 0.01, 100.0, 0.99 + i * 0.01, 1.0 + i * 0.01, ts + i * 0.1)
            for i in range(4)]                            # front: all lifts
    rows += [(1.04 - i * 0.01, 100.0, 1.04 - i * 0.01, 1.05 - i * 0.01,
              ts + (4 + i) * 0.1) for i in range(4)]      # back: all hits the bid
    out = _f(rows)
    assert out is not None
    assert out["buy_share_delta"] < 0


def test_buy_share_delta_does_not_move_with_the_clock():
    """THE POINT. `signed_tape_accel` compares RAW volume between two halves of the
    WINDOW split at the timestamp midpoint, so its sign can flip on the window
    length alone — measured on WYHG 2026-09-08 08:41:02, the same instant read
    -2,138 over 20s and +8,212 over 15s. This measure is a SHARE between halves
    split by print COUNT, so neither the clock nor the volume level can decide it.
    """
    def build(step):
        ts = 1_000_000.0
        rows = [(1.0 + i * 0.01, 100.0, 0.99 + i * 0.01, 1.0 + i * 0.01,
                 ts + i * step) for i in range(5)]
        rows += [(1.05 - i * 0.01, 100.0, 1.05 - i * 0.01, 1.06 - i * 0.01,
                  ts + (5 + i) * step) for i in range(5)]
        return rows

    fast = _f(build(0.01), window_s=1.0)
    slow = _f(build(3.0), window_s=600.0)
    assert fast["buy_share_delta"] == pytest.approx(slow["buy_share_delta"])


def test_buy_share_delta_is_scale_free_in_volume():
    """Ten times the size on every print must not change the reading."""
    def build(mult):
        ts = 1_000_000.0
        rows = [(1.0 + i * 0.01, 100.0 * mult, 0.99 + i * 0.01, 1.0 + i * 0.01,
                 ts + i * 0.1) for i in range(4)]
        rows += [(1.04 - i * 0.01, 100.0 * mult, 1.04 - i * 0.01, 1.05 - i * 0.01,
                  ts + (4 + i) * 0.1) for i in range(4)]
        return rows

    assert _f(build(1))["buy_share_delta"] == pytest.approx(
        _f(build(10))["buy_share_delta"])


def test_buy_share_delta_is_none_when_there_is_not_enough_tape():
    out = _f(_tape([1.0, 1.1, 1.2]))
    assert out is not None
    assert out["buy_share_delta"] is None


def test_the_existing_keys_are_all_still_present():
    """Three modules read this dict; none of their keys may disappear."""
    out = _f(_tape([1.0 + i * 0.01 for i in range(20)]))
    assert out is not None
    for key in ("signed_tape_accel", "tick_rate", "tick_rate_floor", "n_ticks",
                "front_buy_share", "back_buy_share", "gap_restricted"):
        assert key in out
