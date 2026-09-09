"""FIX D — BACKSIDE VWAP-RECLAIM EXCEPTION (chili_momentum_backside_vwap_reclaim_enabled).

The sticky below-VWAP backside bench (``evaluate_sticky_backside_bench``) latched the
SDOT/ILLR-class early pushes the w0av0u3qy replay showed the lane MISSED (44x/14x bench
hits): a name that dipped below VWAP for a tick but is RECLAIMING it from below with upward
momentum. FIX D adds an exception — a name actively reclaiming VWAP (price back at/above
VWAP within the existing vwap_hold_buffer AND rising vs the prior bar) is NOT benched on the
``below_vwap`` reason. A name still FALLING below VWAP stays benched.

Cases:
  (1) PARITY      — flag OFF -> a below-VWAP reclaim STILL latches (byte-identical to before).
  (2) RECLAIM     — flag ON  -> a below-VWAP reclaim is NOT benched (the SDOT/ILLR save).
  (3) FALLING     — flag ON  -> a name still FALLING below VWAP STAYS benched (no hole opened).
  (4) NON-VWAP    — flag ON  -> the exception is below_vwap-ONLY: a faded/chasing_top backside
                    is NEVER reprieved by the reclaim carve-out.
  (5) BUFFER      — flag ON  -> a price just UNDER VWAP but within vwap_hold_buffer + rising
                    counts as a reclaim (reuses the documented buffer base).
  (6) UNBENCH-INTACT — a name already benched (benched_at_hod set) is unaffected by FIX D
                    (the exception only declines the INITIAL latch); the mandatory fresh-HOD
                    un-bench still governs the latched case.
"""

import numpy as np
import pandas as pd

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import evaluate_sticky_backside_bench
from app.services.trading.momentum_neural.ross_momentum import front_side_state


def _session_index(date: str, n: int, freq: str = "1min", start: str = "13:30") -> pd.DatetimeIndex:
    return pd.date_range(f"{date} {start}", periods=n, freq=freq, tz="UTC")


def _ohlc_from_closes(closes, vols, idx) -> pd.DataFrame:
    closes = np.asarray(closes, dtype=float)
    vols = np.asarray(vols, dtype=float)
    n = len(closes)
    opens = np.empty(n)
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    highs = np.maximum(opens, closes) * 1.001
    lows = np.minimum(opens, closes) * 0.999
    return pd.DataFrame(
        {"Open": opens, "High": highs, "Low": lows, "Close": closes, "Volume": vols}, index=idx
    )


def _below_vwap_reclaim_day():
    """A name that ran up, FADED below VWAP, then is RECLAIMING: the last bar closes back
    ABOVE the cumulative session VWAP and is RISING vs the prior bar. front_side_state should
    NOT read below_vwap on the final reclaim bar (last >= vwap), but the bench is evaluated on
    the live tick + the prior dip — FIX D's job is to ensure the reclaim isn't benched."""
    up = np.linspace(10.0, 13.0, 12)            # morning run pulls VWAP up toward ~11.5
    dip = np.linspace(13.0, 10.6, 8)            # fade UNDER the (now ~11.5) VWAP
    reclaim = np.array([10.9, 11.6, 12.0])      # cross back above VWAP, rising into the last bar
    closes = np.concatenate([up, dip, reclaim])
    vols = np.full(len(closes), 1000.0)
    return closes, vols


def _below_vwap_falling_day():
    """A genuine BACKSIDE fade: ran up then is STILL falling below VWAP on the last bar
    (last < vwap AND last < prior close). This must STAY benched even with FIX D ON."""
    up = np.linspace(10.0, 14.0, 14)
    down = np.linspace(14.0, 9.5, 16)           # crash hard below VWAP, still dropping
    closes = np.concatenate([up, down])
    vols = np.full(len(closes), 1000.0)
    return closes, vols


def _faded_above_vwap_day():
    """A FADED (already_faded, NOT below_vwap) backside: a long flat base drags the cumulative
    VWAP low, a quick spike makes the HOD, then a retrace ends >66% off the HOD but still ABOVE
    the (low) session VWAP. front_side_state reads reason='already_faded' (above_vwap=True), so
    FIX D's below_vwap-ONLY exception must NOT apply. (Shape verified empirically.)"""
    base = np.full(24, 10.0)
    spike = np.array([11.0, 13.0, 16.0])        # quick HOD at 16 (VWAP stays ~10.75)
    retr = np.linspace(15.0, 12.0, 4)           # >66% off HOD, last close 12.0 > VWAP
    closes = np.concatenate([base, spike, retr])
    vols = np.full(len(closes), 1000.0)
    return closes, vols


def _df(day):
    closes, vols = day
    return _ohlc_from_closes(closes, vols, _session_index("2026-06-15", len(closes)))


# --------------------------------------------------------------------------- #
# (1) PARITY — flag OFF: a below-VWAP reclaim STILL latches (byte-identical).
# --------------------------------------------------------------------------- #
def test_parity_flag_off_below_vwap_reclaim_still_benched(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_backside_vwap_reclaim_enabled", False, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.0, raising=False)
    df = _df(_below_vwap_falling_day())   # below_vwap on the last bar
    # ensure the shape reads below_vwap
    assert front_side_state(df).reason == "below_vwap"
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(df, benched_at_hod=None)
    assert benched is True
    assert reason == "benched_backside_below_vwap"
    assert "vwap_reclaim_exception" not in dbg     # the exception block never ran (flag OFF)


# --------------------------------------------------------------------------- #
# (2) RECLAIM — flag ON: a below-VWAP reclaim is NOT benched (the SDOT/ILLR save).
# --------------------------------------------------------------------------- #
def test_below_vwap_reclaim_not_benched_flag_on(monkeypatch):
    """THE GUARANTEE (unchanged): a below-VWAP name whose LIVE tick has reclaimed VWAP is not
    benched. The SDOT/ILLR save still holds.

    WHAT CHANGED, AND WHY THIS TEST WAS REWRITTEN (2026-09-09, FTFT). FIX D existed to rescue a
    name from a `below_vwap` verdict that the blind read should never have produced: the verdict
    came from `closes[-1]`, a completed bar close served from a 600 s TTL cache, while the live
    tick was far above VWAP. That upstream blindness is now fixed — `evaluate_sticky_backside_bench`
    passes `live_price` into `front_side_state` (entry_gates.py) — so with the tick above VWAP the
    name simply reads `front_side` and FIX D never needs to run.

    So the OUTCOME assertions are the real contract and they are asserted first and hardest;
    the reason string names WHICH mechanism delivered it, and the earlier, more truthful one now
    wins. At the binding config (`chili_momentum_entry_vwap_hold_buffer` default 0.0) FIX D is
    unreachable BY CONSTRUCTION — its precondition needs `last < vwap` AND `last >= vwap`. It is
    config-inert, not dead: the second half of this test proves it still fires when a non-zero
    buffer opens the band it was written for, so raising the buffer restores it."""
    monkeypatch.setattr(settings, "chili_momentum_backside_vwap_reclaim_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.0, raising=False)
    df = _df(_below_vwap_falling_day())
    fs = front_side_state(df)
    assert fs.reason == "below_vwap"          # the BAR-ONLY read still says below_vwap
    vwap = float(fs.session_vwap)

    # live tick reclaiming: a price back ABOVE vwap, with the frame's prior close below it.
    benched, reason, hod_out, _dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=vwap * 1.01
    )
    assert benched is False                   # THE CONTRACT
    assert hod_out is None
    assert reason == "front_side", (
        "with the live tick above VWAP the name must read front-side UPSTREAM; "
        f"got {reason} — FIX D should no longer be the mechanism here"
    )

    # ...and FIX D itself is still live where it can be: a tick just BELOW VWAP but inside a
    # non-zero hold buffer, rising off the prior bar, is the band FIX D was written for.
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.02, raising=False)
    benched_b, reason_b, hod_b, dbg_b = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=vwap * 0.995
    )
    assert benched_b is False
    assert hod_b is None
    assert reason_b == "front_side_vwap_reclaim"
    assert "vwap_reclaim_exception" in dbg_b


# --------------------------------------------------------------------------- #
# (3) FALLING — flag ON: a name still FALLING below VWAP STAYS benched.
# --------------------------------------------------------------------------- #
def test_below_vwap_still_falling_stays_benched_flag_on(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_backside_vwap_reclaim_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.0, raising=False)
    df = _df(_below_vwap_falling_day())
    fs = front_side_state(df)
    assert fs.reason == "below_vwap"
    vwap = float(fs.session_vwap)
    # live tick still BELOW vwap (no reclaim) -> the exception declines -> stays benched.
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=vwap * 0.95
    )
    assert benched is True
    assert reason == "benched_backside_below_vwap"
    assert dbg.get("vwap_reclaim_declined", {}).get("reclaimed") is False


# --------------------------------------------------------------------------- #
# (4) NON-VWAP — the exception is below_vwap-ONLY (faded backside not reprieved).
# --------------------------------------------------------------------------- #
def test_faded_backside_not_reprieved_by_reclaim(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_backside_vwap_reclaim_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.0, raising=False)
    df = _df(_faded_above_vwap_day())
    fs = front_side_state(df)
    assert fs.reason == "already_faded"     # NOT below_vwap -> FIX D must not touch it
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=float(fs.session_vwap) * 1.05
    )
    assert benched is True
    assert reason == "benched_backside_already_faded"
    assert "vwap_reclaim_exception" not in dbg


# --------------------------------------------------------------------------- #
# (5) BUFFER — a price just under VWAP but within vwap_hold_buffer + rising reclaims.
# --------------------------------------------------------------------------- #
def test_within_hold_buffer_counts_as_reclaim(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_backside_vwap_reclaim_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_momentum_entry_vwap_hold_buffer", 0.01, raising=False)  # 1%
    df = _df(_below_vwap_falling_day())
    fs = front_side_state(df)
    assert fs.reason == "below_vwap"
    vwap = float(fs.session_vwap)
    # price 0.5% BELOW vwap (inside the 1% buffer) AND rising vs the prior bar -> reclaim.
    # build a live price that is below vwap but within buffer, and above the prior close.
    prior_close = float(df["Close"].iloc[-2])
    live = max(vwap * 0.995, prior_close * 1.001)
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=live
    )
    # within-buffer + rising -> not benched
    assert benched is False
    assert reason == "front_side_vwap_reclaim"


# --------------------------------------------------------------------------- #
# (6) UNBENCH-INTACT — FIX D only declines the INITIAL latch; a latched bench still
# follows the mandatory fresh-HOD un-bench.
# --------------------------------------------------------------------------- #
def test_already_latched_unaffected_by_fix_d(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_backside_vwap_reclaim_enabled", True, raising=False)
    df = _df(_below_vwap_falling_day())
    # already benched at a HOD ABOVE the current session HOD -> no new high -> stays latched
    cur_hod = float(df["High"].astype(float).max())
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=cur_hod + 5.0
    )
    assert benched is True
    assert reason == "benched_backside_sticky"   # the sticky-latch path, FIX D not consulted
    # a genuine NEW HIGH above the benched-at hod still clears it (mandatory un-bench intact)
    benched2, reason2, hod_out2, dbg2 = evaluate_sticky_backside_bench(
        df, benched_at_hod=cur_hod - 1.0, live_price=cur_hod + 1.0
    )
    assert benched2 is False
    assert reason2 == "unbenched_fresh_hod"
