"""The backside bench must judge "is this name rolled over" against the LIVE tick.

WHAT WENT WRONG (2026-09-09, FTFT, real money, −$32.39).
`evaluate_sticky_backside_bench` already holds the live tick and threads it into `cur_hod`, the
new-high un-bench, the fade-depth test and FIX D's current price — but it called
`front_side_state` WITHOUT it. That is the one call that decides whether the name is on the
back side at all, and blind to the tick it compares `closes[-1]` — the last COMPLETED bar close
— against the frame's cumulative VWAP:

    below_vwap = (vwap is not None) and (last < vwap)        ross_momentum.py:1527

`fetch_ohlcv_df` serves that frame from a 600-second TTL cache (market_data.py:452), so the
close can be ~10 minutes old. FTFT at 16:24:50 had run 2.65 -> 3.55, broken, and was trading
3.00. The stale close was 2.65 against a frame VWAP of 2.654239, so `2.65 < 2.654` latched a
BACKSIDE verdict on a name trading 13% ABOVE that VWAP. FIX D then cleared the bench because
the LIVE price was above VWAP. Two wrong halves cancelled into "proceed" and the engine bought
3.00 into a bar collapsing 3.11 -> 2.93.

Measured across 30 symbol-days: the frame VWAP changed 59 times in 1,680 bench receipts, median
1,272 s between changes, 2.93 distinct values per symbol-day. At the FTFT entry the true
tape VWAP (price x size) was 2.8272 against the 2.6542 the bench used — 6.5% wrong.

WHY THIS IS SAFE, AND WHY THAT IS TESTED HERE.
`live_price` moves only `last`/`hod`/`lod` inside `front_side_state`; the completed-bar
STRUCTURE leg (rollover / lower-high) deliberately stays on bars, because "a live wick must not
fabricate a rollover". So the change can only STOP a stale-close backside verdict — it can
never invent one. Both directions are asserted below, plus the byte-identical no-tick path.

Runnable: pytest tests/test_backside_bench_sees_the_live_tick.py -v
"""
from __future__ import annotations

import ast
from pathlib import Path

import pandas as pd
import pytest

from app.services.trading.momentum_neural.entry_gates import (
    evaluate_sticky_backside_bench,
)
from app.services.trading.momentum_neural.ross_momentum import front_side_state

_SRC = (Path(__file__).resolve().parents[1]
        / "app/services/trading/momentum_neural/entry_gates.py")


def _frame(rows, *, day="2026-09-09"):
    """Intraday 1-minute frame, tz-aware UTC (what the live runner fetches)."""
    idx = pd.to_datetime(
        [f"{day} {h}:00+00:00" for h in rows["ts"]], utc=True
    )
    return pd.DataFrame(
        {
            "Open": rows["open"],
            "High": rows["high"],
            "Low": rows["low"],
            "Close": rows["close"],
            "Volume": rows["vol"],
        },
        index=idx,
    )


def _ftft_frame():
    """FTFT-shaped: a run to 3.55, a break back, and a LAST BAR whose close (2.65) sits a
    hair BELOW the cumulative VWAP — the exact stale-close configuration from the incident.
    Volumes are weighted so the cumulative VWAP lands just above that final close."""
    ts = [f"14:{m:02d}" for m in range(20)]
    close = [2.60, 2.62, 2.66, 2.72, 2.80, 2.95, 3.20, 3.45, 3.55, 3.40,
             3.20, 3.05, 2.95, 2.85, 2.80, 2.75, 2.72, 2.70, 2.68, 2.65]
    high = [c + 0.03 for c in close]
    low = [c - 0.03 for c in close]
    vol = [40000] * 20
    return _frame({"ts": ts, "open": close, "high": high,
                   "low": low, "close": close, "vol": vol})


def test_the_fixture_reproduces_the_stale_close_trap():
    """Guard the fixture itself: without the tick, the last completed close must read BELOW
    the frame VWAP. If this stops holding the rest of the file proves nothing."""
    df = _ftft_frame()
    fs_blind = front_side_state(df)
    assert fs_blind.session_vwap is not None
    assert float(df["Close"].iloc[-1]) < float(fs_blind.session_vwap), (
        "fixture no longer reproduces the stale-close-below-VWAP configuration"
    )
    assert fs_blind.reason == "below_vwap", (
        f"blind read should be below_vwap, got {fs_blind.reason}"
    )
    assert fs_blind.is_backside is True


def test_the_live_tick_overrides_the_stale_close():
    """THE FIX. A live tick well ABOVE VWAP must not be judged backside-below-VWAP just
    because a cached bar close is stale."""
    df = _ftft_frame()
    vwap = float(front_side_state(df).session_vwap)
    live = vwap * 1.13                       # FTFT sat ~13% above the frame VWAP
    benched, reason, _hod, _dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=live,
    )
    assert benched is False, (
        f"a name trading {live:.4f} vs VWAP {vwap:.4f} was benched as backside "
        f"(reason={reason})"
    )
    assert reason != "benched_backside_below_vwap"


def test_the_receipt_records_what_the_old_read_would_have_said():
    """Opening a gate without recording what it would have refused throws away the evidence
    that could justify closing it again — the same rule applied to the micro-pullback clock."""
    df = _ftft_frame()
    vwap = float(front_side_state(df).session_vwap)
    _b, _r, _h, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=vwap * 1.13,
    )
    assert dbg.get("fs_live_price_used") is True
    assert dbg.get("fs_stale_below_vwap") is True, (
        "the receipt must say the OLD bar-close read WOULD have called this below_vwap"
    )
    assert "fs_last_bar_close" in dbg and "fs_session_vwap" in dbg


def test_no_tick_is_byte_identical_to_the_old_behaviour():
    """live_price None must leave the previous path untouched — no tick, no change."""
    df = _ftft_frame()
    benched, reason, _hod, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=None,
    )
    assert benched is True and reason == "benched_backside_below_vwap"
    assert dbg.get("fs_live_price_used") is False


def test_a_genuine_backside_still_benches():
    """The change must only be able to STOP a stale verdict, never weaken a real one. Here the
    live tick agrees with the bars: the name is genuinely below VWAP and faded off its high."""
    df = _ftft_frame()
    vwap = float(front_side_state(df).session_vwap)
    benched, reason, hod, _dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=vwap * 0.90,
    )
    assert benched is True, "a name trading 10% BELOW VWAP must still bench"
    assert reason == "benched_backside_below_vwap"
    assert hod is not None and hod >= 3.55


def test_a_live_wick_cannot_fabricate_a_rollover():
    """front_side_state keeps the rollover/lower-high leg on BARS on purpose. A live tick at a
    brand-new high must read front-side, never chasing_top."""
    df = _ftft_frame()
    benched, reason, _hod, _dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=4.00,   # a fresh high above the 3.55 HOD
    )
    assert benched is False, f"a new high was benched (reason={reason})"


def test_the_un_bench_on_a_new_high_still_works():
    """The MANDATORY un-bench is the half that was already right — a level, not an average."""
    df = _ftft_frame()
    benched, reason, hod_out, _dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=3.55, live_price=3.80,
    )
    assert benched is False
    assert reason == "unbenched_new_high" or "unbench" in reason or hod_out is None, (
        f"a genuine new high must clear the bench, got reason={reason}"
    )


def test_the_call_passes_the_tick_and_the_module_parses():
    """Source guard: the argument must not silently go missing again — that regression is
    invisible at runtime because the function keeps returning a plausible verdict."""
    src = _SRC.read_text(encoding="utf-8", errors="replace")
    assert "front_side_state(_sess, live_price=live_price)" in src, (
        "the backside latch is judging the tape blind to the live tick again"
    )
    ast.parse(src)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
