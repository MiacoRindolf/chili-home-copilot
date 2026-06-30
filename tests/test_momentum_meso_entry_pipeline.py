"""MESO tier — the ENTRY-DECISION pipeline CONTRACT.

This file tests the entry-decision pipeline as a COMPOSED contract, not isolated
units:

    a fired trigger  ->  the ORDERED veto chain  ->  the sizing derate (reason-threaded)

It composes the REAL functions together:

  * ``entry_gates.pullback_break_confirmation`` — fires a trigger (raw_break /
    first_pullback) then runs the full ordered veto chain (backside, red-vol
    exhaustion, explosive-floor, low-volume, ...). The contract under test is the
    ORDER: when two vetoes would both trip, the EARLIER one in the chain owns the
    returned reason (a fired trigger that a veto kills yields the FIRST-tripped
    reason; a fired trigger that passes every veto yields a sized place-intent
    reason like ``pullback_break_ok``/``first_pullback_ok``).

  * ``spread_cost_veto.adaptive_spread_cost_veto_derate`` — the entry-sizing derate.
    The live runner (``live_runner.py`` ~L5363 sets ``le["entry_trigger_reason"]``;
    ~L6780 reads ``le.get("entry_trigger_reason")`` and threads it in) carries the
    FIRED trigger reason from the LIVE_ENTRY_CANDIDATE tick to the LATER sizing tick.
    The contract under test is that THREAD: a reclaim-family reason (dip/VWAP-reclaim)
    derates LESS than a non-reclaim at the SAME spread (the bug we fixed — the reason
    must reach the derate, not be dropped). A passing path reaches sizing at mult=1.0.

  * The SEAM linking the two: the trigger reasons ``pullback_break_confirmation``
    EMITS (``deep_reclaim_ok`` etc.) are exactly the ones the derate's reclaim
    classifier recognizes — so the trigger->derate thread is wired end-to-end.

Tier: MESO. Strategy: PURE-LOGIC + mocks (synthetic OHLCV frames, a fake DB, and
``patch.object(settings, ...)`` to neutralize ORTHOGONAL gates not under test). No
DB / no FSM — fast. Each assertion checks a SPECIFIC value/reason/transition so the
test fails if the code is subtly wrong (wrong veto order, dropped reason, wrong
derate magnitude).

Worktree HEAD 727b563.
"""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pandas as pd
import pytest

from app.config import settings
from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural.spread_cost_veto import (
    _is_reclaim_family,
    adaptive_spread_cost_veto_derate,
)


# ─────────────────────────────────────────────────────────────────────────────
# Synthetic-frame builders. A clean uptrend impulse -> a SHALLOW pullback (holds
# above the 9-EMA) -> a gentle GREEN break of the pullback high on the last bar,
# all ABOVE session VWAP (so the session-anchored backside gate fails OPEN).
# Tuned empirically against the live functions so the raw/first-pullback trigger
# fires and the chain is reached.
# ─────────────────────────────────────────────────────────────────────────────
def _make_entry_frame(
    *,
    slope: float = 0.05,
    base: float = 10.0,
    break_delta: float = 0.03,
    vol_last: float = 9000.0,
    red_exhaustion: bool = False,
) -> pd.DataFrame:
    n = 40
    close = np.array([base + i * slope for i in range(n)], dtype=float)
    # shallow pullback at n-3 / n-2, break at n-1 (the trigger candle)
    close[n - 3] -= slope * 1.2
    close[n - 2] -= slope * 0.4
    high = close + 0.02
    low = close - 0.02
    opn = close - 0.01
    vol = np.full(n, 1000.0)
    pb_high = max(high[n - 4], high[n - 3], high[n - 2])
    opn[n - 1] = pb_high + 0.005
    close[n - 1] = pb_high + break_delta
    high[n - 1] = pb_high + break_delta + 0.01
    low[n - 1] = pb_high - 0.01
    vol[n - 1] = vol_last
    if red_exhaustion:
        # last bar closes RED (close<open) while printing a NEW session high at the
        # session-MAX volume => the climactic high-vol-red exhaustion top.
        opn[n - 1] = high[n - 1] - 0.005
        close[n - 1] = low[n - 1] + 0.002  # below open -> red
    idx = pd.date_range("2026-06-26 14:30", periods=n, freq="1min", tz="UTC")
    return pd.DataFrame(
        {"Open": opn, "High": high, "Low": low, "Close": close, "Volume": vol},
        index=idx,
    )


# Verticality is an ORTHOGONAL late gate (extension above the lagging 9-EMA). A
# clean monotonic synthetic impulse keeps price persistently above the EMA so the
# verticality gate would mask the vetoes we ARE testing. Neutralize it for the
# composition tests; it has its OWN coverage elsewhere.
_VERT_OFF = patch.object(settings, "chili_momentum_entry_verticality_atr_mult", 0.0)


class _FakeNoHistoryDB:
    """db.execute(...).fetchone() -> None so ``name_spread_percentiles`` returns None
    (insufficient history) and the derate falls to the cost-of-R path only."""

    def execute(self, *a, **k):  # noqa: D401, ANN001
        class _R:
            def fetchone(self_inner):  # noqa: ANN001
                return None

        return _R()


class _FakeDistDB:
    """db.execute(...).fetchone() -> a fixed (p50, p75, p90, count) percentile row so
    the name-relative anomaly + extreme-floor branch engages deterministically."""

    def __init__(self, p50: float, p75: float, p90: float, n: int) -> None:
        self._row = (p50, p75, p90, n)

    def execute(self, *a, **k):  # noqa: ANN001
        row = self._row

        class _R:
            def fetchone(self_inner):  # noqa: ANN001
                return row

        return _R()


# ═════════════════════════════════════════════════════════════════════════════
# PART 1 — TRIGGER -> VETO CHAIN: a fired trigger that passes every veto yields a
# sized place-intent reason; a fired trigger a veto kills yields the FIRST-tripped
# reason (the ordered chain).
# ═════════════════════════════════════════════════════════════════════════════
def test_fired_trigger_passes_all_vetoes_yields_place_intent_reason() -> None:
    """A clean shallow-pullback break that clears every veto returns ok=True with a
    place-intent reason AND the structural levels sizing/placement need."""
    with _VERT_OFF:
        df = _make_entry_frame()
        ok, reason, dbg = eg.pullback_break_confirmation(
            df, entry_interval="1m", volume_spike_multiple=1.5
        )
    assert ok is True
    # first_pullback is on by default and this geometry IS a first pullback -> it is
    # the EARLIEST/most-aggressive fire and owns the reason (a real place-intent path).
    assert reason == "first_pullback_ok"
    # The place-intent carries the structural stop (pullback_low) + breakout level
    # (pullback_high) the sizing/bailout seam depends on.
    assert dbg.get("pullback_low") is not None
    assert dbg.get("pullback_high") is not None
    assert float(dbg["pullback_high"]) > float(dbg["pullback_low"])
    # the break cleared a real volume spike (not a no-op pass)
    assert float(dbg.get("vol_ratio")) > 1.5


def test_red_vol_exhaustion_veto_kills_the_fired_break() -> None:
    """The trigger FIRES (the bar breaks the pullback high) but the break bar is a
    high-vol-red NEW-HIGH exhaustion top -> ``red_vol_exhaustion_veto`` (not a place
    intent). This is a VETO-after-fire, the core MESO contract."""
    with _VERT_OFF:
        df = _make_entry_frame(red_exhaustion=True)
        ok, reason, dbg = eg.pullback_break_confirmation(
            df, entry_interval="1m", volume_spike_multiple=1.5
        )
    assert ok is False
    assert reason == "red_vol_exhaustion_veto"
    assert dbg.get("red_vol_exhaustion") is not None


def test_break_low_volume_veto_kills_the_fired_break() -> None:
    """The trigger fires but the break bar lacks the required volume spike ->
    ``break_low_volume``. The volume floor is a post-fire veto."""
    with _VERT_OFF:
        df = _make_entry_frame(vol_last=1000.0)  # no spike
        ok, reason, dbg = eg.pullback_break_confirmation(
            df, entry_interval="1m", volume_spike_multiple=5.0
        )
    assert ok is False
    assert reason == "break_low_volume"
    assert float(dbg.get("vol_ratio")) < 5.0


def test_explosive_floor_rvol_veto_when_enabled() -> None:
    """With the explosive-floor flag ON, a fired break whose RVOL is below the floor
    is rejected with ``below_explosive_floor_rvol`` and records the observed RVOL."""
    with _VERT_OFF, patch.object(
        settings, "chili_momentum_explosive_floor_enabled", True
    ):
        df = _make_entry_frame(vol_last=1000.0)
        ok, reason, dbg = eg.pullback_break_confirmation(
            df, entry_interval="1m", volume_spike_multiple=1.5
        )
    assert ok is False
    assert reason == "below_explosive_floor_rvol"
    assert dbg.get("explosive_floor_rvol") is not None


# ── ORDERING: when TWO vetoes would both trip, the EARLIER chain position wins ──
def test_veto_order_explosive_floor_precedes_low_volume() -> None:
    """A low-RVOL break trips BOTH the explosive-floor (chain position ~6451) AND the
    low-volume floor (~6509). With the floor ON, the EARLIER ``below_explosive_floor_rvol``
    must own the reason; with it OFF, the LATER ``break_low_volume`` surfaces. Same
    frame, only the flag differs -> proves the ordering, not the inputs."""
    with _VERT_OFF:
        df = _make_entry_frame(vol_last=1000.0)
        with patch.object(settings, "chili_momentum_explosive_floor_enabled", True):
            ok_on, reason_on, _ = eg.pullback_break_confirmation(
                df, entry_interval="1m", volume_spike_multiple=5.0
            )
        with patch.object(settings, "chili_momentum_explosive_floor_enabled", False):
            ok_off, reason_off, _ = eg.pullback_break_confirmation(
                df, entry_interval="1m", volume_spike_multiple=5.0
            )
    assert ok_on is False and ok_off is False
    assert reason_on == "below_explosive_floor_rvol"  # earlier gate wins when ON
    assert reason_off == "break_low_volume"  # later gate surfaces when earlier OFF


def test_veto_order_backside_precedes_red_vol_exhaustion() -> None:
    """The backside gate (``back_side_disabled``, chain position ~6285) is EARLIER than
    the red-vol-exhaustion gate (~6415). A frame that would trip BOTH must return the
    EARLIER ``back_side_disabled``. We force the backside read True (a real helper the
    chain calls) on a red-exhaustion frame; the earlier reason must win."""
    with _VERT_OFF, patch.object(
        eg, "_detect_back_side", lambda *a, **k: (True, "ema9_below_ema20")
    ):
        df = _make_entry_frame(red_exhaustion=True)
        ok, reason, dbg = eg.pullback_break_confirmation(
            df, entry_interval="1m", volume_spike_multiple=1.5
        )
    assert ok is False
    assert reason == "back_side_disabled"  # earliest gate wins
    assert dbg.get("back_side") == "ema9_below_ema20"
    # the LATER red-vol gate never ran -> its debug key is absent
    assert dbg.get("red_vol_exhaustion") is None


def test_no_trigger_returns_wait_reason_not_a_veto() -> None:
    """A frame with NO break (current high does not exceed the pullback high) returns a
    WAIT reason, not a veto and not a fire. Distinguishes 'no trigger' from 'fired but
    vetoed' — the pipeline must not conflate them."""
    with _VERT_OFF:
        df = _make_entry_frame()
        # flatten the last bar so it does NOT break the pullback high
        df.iloc[-1, df.columns.get_loc("High")] = float(df.iloc[-5]["High"]) - 0.05
        df.iloc[-1, df.columns.get_loc("Close")] = float(df.iloc[-5]["Close"]) - 0.05
        ok, reason, _ = eg.pullback_break_confirmation(
            df, entry_interval="1m", volume_spike_multiple=1.5
        )
    assert ok is False
    # a non-fire wait/structure reason — NOT any of the post-fire veto reasons
    assert reason not in {
        "back_side_disabled",
        "red_vol_exhaustion_veto",
        "break_low_volume",
        "below_explosive_floor_rvol",
        "first_pullback_ok",
        "pullback_break_ok",
    }


# ═════════════════════════════════════════════════════════════════════════════
# PART 2 — THE entry_trigger_reason THREAD into the spread-cost derate (the bug we
# fixed). The reason fired by the trigger must reach the derate so a reclaim derates
# LESS than a non-reclaim at the SAME spread.
# ═════════════════════════════════════════════════════════════════════════════
def test_derate_flag_off_is_byte_identical_passthrough() -> None:
    """Flag OFF -> (True, 1.0, 'flag_off', {}) regardless of how toxic the spread is.
    The sizing path is untouched."""
    out = adaptive_spread_cost_veto_derate(
        symbol="PLSM",
        entry_price=10.0,
        current_spread_bps=9000.0,
        stop_distance=0.10,
        db=_FakeNoHistoryDB(),
        flag_enabled=False,
        entry_trigger_reason="vwap_reclaim",
    )
    assert out[0] is True
    assert out[1] == 1.0
    assert out[2] == "flag_off"


def test_cheap_spread_passes_at_mult_one() -> None:
    """A spread that is cheap relative to R hits NEITHER derate pressure -> mult 1.0
    (the no-0-fills guarantee: a normal Ross trade is never sized down)."""
    out = adaptive_spread_cost_veto_derate(
        symbol="PLSM",
        entry_price=10.0,
        current_spread_bps=100.0,  # cost_of_r = 0.10, below both engage points
        stop_distance=1.0,
        db=_FakeNoHistoryDB(),
        flag_enabled=True,
        entry_trigger_reason="pullback_break_ok",
    )
    assert out[0] is True
    assert out[1] == 1.0
    assert out[2] == "pass"


def test_reclaim_reason_derates_LESS_than_nonreclaim_same_spread() -> None:
    """THE bug-we-fixed contract: at the SAME spread the reclaim-family reason judges
    cost against the MORE-PERMISSIVE R base (0.35 vs 0.25) -> a STRICTLY LARGER (less
    aggressive) derate multiplier. Exact values (R=$1.00, entry $10, no history):

        sb=200 (cost_of_r 0.20): non-reclaim 0.7000 ; reclaim 0.9286
        sb=250 (cost_of_r 0.25): non-reclaim 0.5000 ; reclaim 0.7857
    """
    db = _FakeNoHistoryDB()

    def run(sb: float, reason: str):
        return adaptive_spread_cost_veto_derate(
            symbol="PLSM",
            entry_price=10.0,
            current_spread_bps=sb,
            stop_distance=1.0,
            db=db,
            flag_enabled=True,
            entry_trigger_reason=reason,
        )

    nr_200 = run(200.0, "pullback_break_ok")
    rc_200 = run(200.0, "vwap_reclaim")
    assert nr_200[1] == pytest.approx(0.7000, abs=1e-4)
    assert rc_200[1] == pytest.approx(0.9286, abs=1e-4)
    assert rc_200[1] > nr_200[1]  # reclaim derates LESS

    nr_250 = run(250.0, "pullback_break_ok")
    rc_250 = run(250.0, "deep_reclaim_dipbuy_ok")
    assert nr_250[1] == pytest.approx(0.5000, abs=1e-4)
    assert rc_250[1] == pytest.approx(0.7857, abs=1e-4)
    assert rc_250[1] > nr_250[1]

    # The threaded reason + the chosen R base are recorded in meta (the audit trail
    # that proves the reason actually reached the derate, was not dropped).
    assert rc_250[3].get("is_reclaim") is True
    assert rc_250[3].get("entry_trigger_reason") == "deep_reclaim_dipbuy_ok"
    assert rc_250[3].get("max_frac_of_r") == 0.35
    assert nr_250[3].get("is_reclaim") is False
    assert nr_250[3].get("max_frac_of_r") == 0.25


def test_missing_reason_defaults_to_nonreclaim_stricter_base() -> None:
    """A MISSING/None entry_trigger_reason (the key absent on the live-entry dict) must
    fail CLOSED to the non-reclaim (stricter) R base — the permissive carve-out only
    LOOSENS when we POSITIVELY recognize a reclaim. So None derates the SAME as an
    explicit non-reclaim, never the permissive reclaim amount."""
    db = _FakeNoHistoryDB()
    none_out = adaptive_spread_cost_veto_derate(
        symbol="PLSM",
        entry_price=10.0,
        current_spread_bps=250.0,
        stop_distance=1.0,
        db=db,
        flag_enabled=True,
        entry_trigger_reason=None,
    )
    nr_out = adaptive_spread_cost_veto_derate(
        symbol="PLSM",
        entry_price=10.0,
        current_spread_bps=250.0,
        stop_distance=1.0,
        db=db,
        flag_enabled=True,
        entry_trigger_reason="pullback_break_ok",
    )
    assert none_out[1] == pytest.approx(0.5000, abs=1e-4)
    assert none_out[1] == pytest.approx(nr_out[1], abs=1e-9)
    assert none_out[3].get("is_reclaim") is False
    assert none_out[3].get("max_frac_of_r") == 0.25


def test_wide_but_typical_spread_with_good_R_passes_no_0_fills() -> None:
    """A spread that is WIDE in absolute bps but TYPICAL for the name (anomaly ~1.0 vs
    its own p50) AND cheap relative to a healthy R must PASS at mult 1.0. This is the
    documented no-0-fills guarantee for Ross low-float movers (PAVS 300bps is the market,
    not a bug). Distribution: name normally trades ~300bps."""
    db = _FakeDistDB(300.0, 350.0, 400.0, 50)  # the name's OWN norm is ~300bps
    out = adaptive_spread_cost_veto_derate(
        symbol="PAVS",
        entry_price=10.0,
        current_spread_bps=300.0,  # anomaly_ratio = 1.0 (typical for IT)
        stop_distance=10.0,  # large R -> cost_of_r tiny
        db=db,
        flag_enabled=True,
        entry_trigger_reason="pullback_break_ok",
    )
    assert out[0] is True
    assert out[1] == 1.0
    assert out[2] == "pass"
    assert out[3].get("anomaly_ratio") == pytest.approx(1.0, abs=1e-6)


def test_anomaly_wide_for_name_derates_on_cheap_R() -> None:
    """A spread anomalously WIDE for the name (above its own p75) derates via the
    name-relative anomaly pressure EVEN when cost-of-R is benign (cheap R). Name norm
    p50=100/p75=120; sb=180 (anomaly 1.8, above p75) with a large R -> anomaly-only
    derate to a specific multiplier, reason ``anomaly_wide_for_name``."""
    db = _FakeDistDB(100.0, 120.0, 150.0, 50)
    out = adaptive_spread_cost_veto_derate(
        symbol="PLSM",
        entry_price=10.0,
        current_spread_bps=180.0,
        stop_distance=100.0,  # cost-of-R negligible -> isolate the anomaly pressure
        db=db,
        flag_enabled=True,
        entry_trigger_reason="pullback_break_ok",
    )
    assert out[0] is True
    assert out[1] == pytest.approx(0.6000, abs=1e-4)
    assert out[2] == "anomaly_wide_for_name"
    assert out[3].get("anomaly_ratio") == pytest.approx(1.8, abs=1e-6)


def test_extreme_toxic_spread_floors_never_blocks() -> None:
    """The EXTREME case (an EXTREME outlier vs the name's OWN p90 AND cost > the max
    fraction of R) DERATES TO THE FLOOR (mult=floor=0.5) but ALWAYS allow=True — the
    gate is derate-only globally, it NEVER returns allow=False. Name norm p50=100/p90=150;
    sb=300 is >= p90*1.5 (extreme) AND cost_of_r 0.30 > 0.25 -> floored."""
    db = _FakeDistDB(100.0, 120.0, 150.0, 50)
    out = adaptive_spread_cost_veto_derate(
        symbol="PLSM",
        entry_price=10.0,
        current_spread_bps=300.0,
        stop_distance=1.0,  # cost_of_r = 0.30 > 0.25 cap
        db=db,
        flag_enabled=True,
        entry_trigger_reason="pullback_break_ok",
    )
    assert out[0] is True  # NEVER blocks
    assert out[1] == pytest.approx(0.5000, abs=1e-4)  # floored
    assert out[3].get("extreme_floor") is True
    assert "extreme_spread_floored" in out[2]


def test_derate_fails_open_on_unusable_inputs() -> None:
    """Unusable basis (no entry price / no spread / no stop distance) fails OPEN to
    (True, 1.0, ...) so the gate can NEVER newly block a fill it lacks data for. Each
    bad input carries its OWN diagnostic reason."""
    db = _FakeNoHistoryDB()
    common = dict(db=db, flag_enabled=True, entry_trigger_reason="x")
    no_px = adaptive_spread_cost_veto_derate(
        symbol="X", entry_price=0.0, current_spread_bps=400.0, stop_distance=1.0, **common
    )
    no_spread = adaptive_spread_cost_veto_derate(
        symbol="X", entry_price=10.0, current_spread_bps=0.0, stop_distance=1.0, **common
    )
    no_stop = adaptive_spread_cost_veto_derate(
        symbol="X", entry_price=10.0, current_spread_bps=400.0, stop_distance=0.0, **common
    )
    for out, expect in (
        (no_px, "no_entry_price"),
        (no_spread, "no_spread"),
        (no_stop, "no_stop_distance"),
    ):
        assert out[0] is True
        assert out[1] == 1.0
        assert out[2] == expect


# ═════════════════════════════════════════════════════════════════════════════
# PART 3 — THE SEAM: the trigger reasons the entry gate EMITS are exactly the ones
# the derate's reclaim classifier recognizes. This is the wire that makes the
# trigger->derate thread actually work end-to-end (a misclassified reason would
# silently apply the wrong R base — the class of bug we fixed).
# ═════════════════════════════════════════════════════════════════════════════
def test_emitted_reclaim_trigger_reasons_are_classified_reclaim() -> None:
    """Every deep-reclaim/dip place-intent reason ``pullback_break_confirmation``
    returns is recognized by ``_is_reclaim_family`` -> the permissive R base applies."""
    for reason in (
        "deep_reclaim_ok",
        "deep_reclaim_tick_ok",
        "deep_reclaim_dipbuy_ok",
        "deep_reclaim_dipbuy_tick_ok",
    ):
        assert _is_reclaim_family(reason) is True, reason


def test_emitted_nonreclaim_trigger_reasons_are_not_reclaim() -> None:
    """The continuation/first-pullback place-intent reasons are NOT reclaim-family ->
    the stricter standard R base applies. A false-positive here would over-permit a
    plain breakout's toxic spread."""
    for reason in (
        "first_pullback_ok",
        "first_pullback_tick_ok",
        "pullback_break_ok",
        "pullback_break_tick_ok",
        "tape_confirmed_hold",
        "raw_break",
        "momentum_continuation",
    ):
        assert _is_reclaim_family(reason) is False, reason


def test_trigger_to_derate_thread_end_to_end_nonreclaim() -> None:
    """Compose the two REAL functions: fire the trigger -> take its EMITTED reason ->
    feed it straight into the derate (exactly as the live runner threads
    ``le['entry_trigger_reason']`` from candidate-detection to sizing). A
    ``first_pullback_ok`` (non-reclaim) reason must drive the STRICTER R base."""
    with _VERT_OFF:
        df = _make_entry_frame()
        ok, reason, _ = eg.pullback_break_confirmation(
            df, entry_interval="1m", volume_spike_multiple=1.5
        )
    assert ok is True
    assert reason == "first_pullback_ok"  # the emitted place-intent reason

    # Thread that EXACT reason into the sizing derate (the live_runner seam).
    out = adaptive_spread_cost_veto_derate(
        symbol="PLSM",
        entry_price=10.0,
        current_spread_bps=250.0,
        stop_distance=1.0,
        db=_FakeNoHistoryDB(),
        flag_enabled=True,
        entry_trigger_reason=reason,
    )
    # non-reclaim -> standard 0.25 base -> the stricter 0.5000 derate (NOT 0.7857).
    assert out[3].get("is_reclaim") is False
    assert out[3].get("max_frac_of_r") == 0.25
    assert out[1] == pytest.approx(0.5000, abs=1e-4)
    assert out[3].get("entry_trigger_reason") == "first_pullback_ok"
