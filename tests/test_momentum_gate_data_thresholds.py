"""Comprehensive GATE + DATA + PROVIDER fail-safe test suite for the momentum lane.

Threshold-boundary coverage (just-below blocks / just-above passes) and fail-safe
behavior across three surfaces:

1. GATE THRESHOLDS
   - spread gate: adaptive_max_spread_bps + the live BBO _quote_quality_block, tested
     just-under vs just-over the adaptive max_spread_bps (incl. the absolute cap).
   - extension / verticality: regime_entry_allowed ATR ceiling (below cap passes /
     above blocks for non-momentum families) AND the SD-1 cold-frame fail-open
     (atr_pct=None => no veto, even for the families that DO have a ceiling).
   - stale-age: is_fresh_enough / _quote_quality_block stale_bbo (age below the floor
     is fresh, above is stale->blocked).
   - RVOL / explosive floor: build_equity_universe min_change_pct + min_dollar_volume
     floors (>= floor passes / < floor blocks), plus the sustaining-volume gate.
   - weak_break_candle: is_strong_bull_break_candle adaptive close-pos (conviction
     close passes / weak ordinary close blocks); the lane-wide reuse lives in
     tests/test_candles.py, this asserts the threshold boundary directly.

2. DATA EDGE CASES
   - NaN / zero / negative bar: one bad bar drops but the frame survives; a mostly-bad
     frame still whole-rejects (clean_ohlcv + validate_ohlcv_integrity).
   - short / cold frame: the trigger / momentum gates increment-and-WAIT (observe,
     never a false fire), and the SD-1 atr_pct=None fail-open path.
   - split / hyper-mover (#678 dollar-volume veto): a real +100% intraday move passes
     (not flagged as a split), a real daily common-ratio split is caught; daily-only.
   - halt: stale market data -> blocked.
   - gap: a large non-split gap stays clean.
   - duplicate tick: identical consecutive bars never synthesize a false break.

3. PROVIDER FAILURE
   - sizing returns qty 0 (never a crash / oversize) on None / zero atr / equity / entry.
   - a stale quote blocks (does not fill).
   - a per-symbol provider error skips, not crashes (fail-open empty frame / [] universe).

Each test asserts the EXPECTED fail-safe behavior; the new-flag tests assert flag-off
parity (turning a confirmation off lets the otherwise-blocked frame through).
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

from app.services.trading.data_quality import (
    clean_ohlcv,
    detect_stock_split,
    validate_ohlcv_integrity,
)
from app.services.trading.momentum_neural.candles import is_strong_bull_break_candle
from app.services.trading.momentum_neural.entry_gates import (
    _sustained_rvol,
    momentum_volume_confirmation,
    pullback_break_confirmation,
    regime_entry_allowed,
)
from app.services.trading.momentum_neural.volume_pace import compute_volume_pace
from app.services.trading.momentum_neural.live_runner import (
    _adaptive_live_max_spread_bps,
    _quote_quality_block,
)
from app.services.trading.momentum_neural.risk_policy import (
    adaptive_max_spread_bps,
    compute_risk_first_quantity,
)
from app.services.trading.momentum_neural.universe import (
    EQUITY_ROSS_SMALLCAP,
    _snapshot_volume_pace,
    build_equity_universe,
)
from app.services.trading.venue.protocol import FreshnessMeta, is_fresh_enough


# ──────────────────────────────────────────────────────────────────────────────
# helpers
# ──────────────────────────────────────────────────────────────────────────────
def _ohlcv(closes: list[float], *, vol: float = 100_000.0) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "Open": closes,
            "High": [c * 1.01 for c in closes],
            "Low": [c * 0.99 for c in closes],
            "Close": closes,
            "Volume": [vol for _ in closes],
        },
        index=pd.date_range("2026-01-01", periods=len(closes), freq="D"),
    )


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    """rows = (close, high, low, volume) — Open == Close (matches test_pullback_break)."""
    return pd.DataFrame(
        [{"Open": c, "High": h, "Low": lo, "Close": c, "Volume": v} for (c, h, lo, v) in rows]
    )


def _base(close: float, vol: float = 1000.0) -> tuple[float, float, float, float]:
    return (close, close + 0.3, close - 0.3, vol)


def _firing_pullback_rows() -> list[tuple[float, float, float, float]]:
    """A canonical raw first-break fire (mirrors test_pullback_break)."""
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]  # impulse
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]  # shallow pullback
    rows.append((110.6, 111.2, 109.6, 3200.0))  # break + volume spike
    return rows


def _tick(*, bid: float, ask: float, mid: float | None = None, spread_bps: float | None = None,
          age: float = 0.0, max_age: float = 30.0) -> SimpleNamespace:
    m = mid if mid is not None else (bid + ask) / 2.0
    fr = FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc) - timedelta(seconds=age),
        max_age_seconds=max_age,
    )
    if spread_bps is None and m > 0:
        spread_bps = ((ask - bid) / m) * 10_000.0
    return SimpleNamespace(bid=bid, ask=ask, mid=m, spread_bps=spread_bps, freshness=fr)


def _snap_row(ticker: str, *, price: float, chg_pct: float, day_vol: float) -> dict:
    return {
        "ticker": ticker,
        "lastTrade": {"p": price},
        "todaysChangePerc": chg_pct,
        "day": {"c": price, "h": price * 1.02, "l": price * 0.90, "v": day_vol},
    }


# ══════════════════════════════════════════════════════════════════════════════
# 1. GATE THRESHOLDS
# ══════════════════════════════════════════════════════════════════════════════

# ── spread gate ───────────────────────────────────────────────────────────────
def test_adaptive_spread_loosens_above_floor_for_explosive_move() -> None:
    # ratio=0.5: a name expected to move 200 bps/bar tolerates up to 100 bps spread,
    # above the 12 bps base floor. A quiet/None-move name keeps the floor.
    assert adaptive_max_spread_bps(12.0, 200.0, 0.5) == 100.0
    assert adaptive_max_spread_bps(12.0, None, 0.5) == 12.0
    assert adaptive_max_spread_bps(12.0, 0.0, 0.5) == 12.0  # zero move -> floor


def test_adaptive_spread_never_below_floor_and_capped() -> None:
    # Tiny move can't drag tolerance below the floor; huge move can't exceed the abs cap.
    assert adaptive_max_spread_bps(12.0, 4.0, 0.5) == 12.0           # 0.5*4=2 < 12 floor
    assert adaptive_max_spread_bps(12.0, 100_000.0, 0.5, abs_cap_bps=300.0) == 300.0


def test_quote_block_spread_just_under_passes_just_over_blocks() -> None:
    # max_spread tolerance 20 bps. 19.x bps just-under passes; 21.x bps just-over blocks.
    mid = 100.0
    under = _tick(bid=mid - 0.095, ask=mid + 0.095)   # ~19 bps
    over = _tick(bid=mid - 0.105, ask=mid + 0.105)    # ~21 bps
    assert _quote_quality_block(under, under.freshness, max_spread_bps=20.0) is None
    blk = _quote_quality_block(over, over.freshness, max_spread_bps=20.0)
    assert blk is not None and blk["reason"] == "wide_bbo_spread"
    assert blk["max_spread_bps"] == 20.0


def test_quote_block_zero_cap_blocks_all() -> None:
    # A 0.0 tolerance is a deliberate "block all" and is preserved (not coerced to default).
    t = _tick(bid=99.99, ask=100.01)
    blk = _quote_quality_block(t, t.freshness, max_spread_bps=0.0)
    assert blk is not None and blk["reason"] == "wide_bbo_spread"


def test_adaptive_live_max_spread_uses_policy_defaults() -> None:
    # The runner helper agrees with the shared policy helper (same floor/ratio/cap).
    assert _adaptive_live_max_spread_bps(None) == _adaptive_live_max_spread_bps(0.0)
    assert _adaptive_live_max_spread_bps(1_000_000.0) >= _adaptive_live_max_spread_bps(10.0)


# ── extension / verticality + SD-1 cold-frame fail-open ───────────────────────
def test_extension_below_cap_passes_above_blocks_for_nonmomentum() -> None:
    # Non-momentum family: ATR just under 4.5% passes, just over blocks (extreme veto).
    ok_under, _ = regime_entry_allowed("mean_reversion", atr_pct=0.044, chop_expansion="", vol_regime="")
    assert ok_under is True
    ok_over, reason = regime_entry_allowed("mean_reversion", atr_pct=0.046, chop_expansion="", vol_regime="")
    assert ok_over is False and reason == "extreme_atr_block_all"


def test_extension_extreme_atr_is_the_setup_for_momentum_families() -> None:
    # The breakout/momentum families ARE the explosive lane — extreme ATR is the SETUP,
    # not a disqualifier (risk bounded by wide stop + sizing, not by refusing the trade).
    for fid in ("breakout_continuation", "impulse_surge", "ross_momentum"):
        ok, reason = regime_entry_allowed(fid, atr_pct=0.20, chop_expansion="", vol_regime="")
        assert ok is True, (fid, reason)


def test_sd1_cold_frame_fail_open_atr_none_no_veto() -> None:
    # SD-1: when ATR% is unknown (None) the volatility veto fails OPEN — a cold/thin
    # frame must not be vetoed on missing data, for momentum AND non-momentum families.
    for fid in ("breakout_continuation", "mean_reversion", "vwap_reclaim"):
        ok, reason = regime_entry_allowed(fid, atr_pct=None, chop_expansion="", vol_regime="")
        assert ok is True and reason == "regime_ok", (fid, reason)


def test_extension_low_atr_blocks_breakout_family() -> None:
    # The other extension boundary: a dead-flat <0.8% ATR is no breakout setup.
    ok, reason = regime_entry_allowed("breakout_continuation", atr_pct=0.007,
                                      chop_expansion="", vol_regime="")
    assert ok is False and reason == "low_atr_block_breakout_family"
    # ...but just above the floor it passes.
    ok2, _ = regime_entry_allowed("breakout_continuation", atr_pct=0.009,
                                  chop_expansion="", vol_regime="")
    assert ok2 is True


# ── stale-age ─────────────────────────────────────────────────────────────────
def test_stale_age_below_floor_fresh_above_stale() -> None:
    # max_age 30s: a 29s-old read is fresh, a 31s-old read is stale.
    fresh = FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc) - timedelta(seconds=29), max_age_seconds=30.0
    )
    stale = FreshnessMeta(
        retrieved_at_utc=datetime.now(timezone.utc) - timedelta(seconds=31), max_age_seconds=30.0
    )
    assert is_fresh_enough(fresh) is True
    assert is_fresh_enough(stale) is False


def test_quote_block_stale_bbo_blocks() -> None:
    # A quote past its freshness floor is blocked as stale_bbo BEFORE any spread check.
    t = _tick(bid=99.99, ask=100.01, age=120.0, max_age=30.0)
    blk = _quote_quality_block(t, t.freshness)
    assert blk is not None and blk["reason"] == "stale_bbo"
    assert blk["age_seconds"] >= 30.0


# ── RVOL / explosive floor ────────────────────────────────────────────────────
def test_universe_change_floor_just_below_blocks_just_above_passes() -> None:
    # min_change_pct = 5.0: +4.9% is dropped (dead tape), +5.1% survives the pool.
    snap = [
        _snap_row("BELOW", price=5.0, chg_pct=4.9, day_vol=1_000_000.0),
        _snap_row("ABOVE", price=5.0, chg_pct=5.1, day_vol=1_000_000.0),
    ]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "ABOVE" in out
    assert "BELOW" not in out


def test_universe_dollar_volume_floor_gate() -> None:
    # min_dollar_volume = $1M: a name with too-thin turnover can't be entered+exited.
    snap = [
        _snap_row("THIN", price=2.0, chg_pct=50.0, day_vol=100_000.0),     # $200k < $1M
        _snap_row("LIQUID", price=2.0, chg_pct=50.0, day_vol=1_000_000.0),  # $2M >= $1M
    ]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "LIQUID" in out
    assert "THIN" not in out


def test_sustained_rvol_floor_blocks_below_passes_above() -> None:
    # Mean per-bar rel-vol over the lookback. >= floor sustains, < floor is a faded mover.
    above = _sustained_rvol([1.4, 1.5, 1.6], cur=2, lookback=3)
    below = _sustained_rvol([0.4, 0.5, 0.6], cur=2, lookback=3)
    assert above is not None and above >= 1.0
    assert below is not None and below < 1.0


def test_sustained_rvol_thin_data_fails_open_none() -> None:
    # < 2 valid samples -> None so the caller fails OPEN (never blocks on thin data).
    assert _sustained_rvol([1.5], cur=0, lookback=5) is None
    assert _sustained_rvol([None, None], cur=1, lookback=5) is None


def test_volume_pace_regular_session_market_curve_fallback() -> None:
    # 10:09 ET is 10% through RTH; the generic market curve expects ~20% of ADV.
    now = datetime(2026, 7, 1, 10, 9, tzinfo=ZoneInfo("America/New_York"))
    pace = compute_volume_pace(
        actual_cum_vol=1_000_000,
        full_session_baseline_vol=5_000_000,
        now=now,
    )
    assert pace.session_bucket == "regular"
    assert pace.rvol_source == "market_session_curve"
    assert pace.fallback_reason == "per_symbol_curve_unavailable"
    assert pace.expected_cum_vol == pytest.approx(1_000_000)
    assert pace.rvol_pace == pytest.approx(1.0)


def test_volume_pace_premarket_without_curve_is_incomplete_not_low_rvol() -> None:
    # Premarket must not borrow the regular-session full-day curve blindly.
    now = datetime(2026, 7, 1, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    pace = compute_volume_pace(
        actual_cum_vol=100_000,
        full_session_baseline_vol=5_000_000,
        now=now,
    )
    assert pace.session_bucket == "premarket"
    assert pace.rvol_source == "rvol_incomplete"
    assert pace.rvol_pace is None
    assert pace.expected_cum_vol is None
    assert pace.fallback_reason == "premarket_curve_unavailable"


def test_volume_pace_uses_explicit_expected_cum_vol_in_extended_hours() -> None:
    # A supplied expected cumulative value is already session-aware, so it can be used.
    now = datetime(2026, 7, 1, 8, 0, tzinfo=ZoneInfo("America/New_York"))
    pace = compute_volume_pace(
        actual_cum_vol=200_000,
        expected_cum_vol=50_000,
        now=now,
        curve_source="per_symbol_extended_curve",
    )
    assert pace.session_bucket == "premarket"
    assert pace.rvol_source == "per_symbol_extended_curve"
    assert pace.rvol_pace == pytest.approx(4.0)
    assert pace.expected_cum_vol == pytest.approx(50_000)


def test_snapshot_volume_pace_uses_min_av_and_prev_day_baseline() -> None:
    now = datetime(2026, 7, 1, 10, 9, tzinfo=ZoneInfo("America/New_York"))
    snap = {
        "ticker": "PACE",
        "day": {"v": 0},
        "min": {"av": 200_000},
        "prevDay": {"v": 1_000_000},
    }
    pace = _snapshot_volume_pace(snap, now=now)
    assert pace["actual_cum_vol"] == pytest.approx(200_000)
    assert pace["expected_cum_vol"] == pytest.approx(200_000)
    assert pace["rvol_pace"] == pytest.approx(1.0)


def test_rvol_relative_floor_rejects_cumulative_day_adv_basis() -> None:
    rows = _firing_pullback_rows()
    ok, reason, debug = pullback_break_confirmation(
        _df(rows),
        entry_interval="5m",
        volume_spike_multiple=4.0,
        rvol_relative_floor=True,
        session_rvol=5.0,
        rvol_source="snapshot_participation",
        rvol_basis="cumulative_day_over_prev_day",
    )
    assert ok is False
    assert reason == "break_low_volume"
    assert debug["rvol_source"] == "rvol_incomplete"
    assert debug["rvol_basis"] == "cumulative_day_over_prev_day"
    assert debug["fallback_reason"] == "rvol_basis_not_time_normalized"
    assert "rvol_relaxed_vol_floor" not in debug


def test_rvol_relative_floor_uses_time_normalized_pace_telemetry() -> None:
    rows = _firing_pullback_rows()
    telemetry = {
        "rvol_source": "market_session_curve",
        "rvol_basis": "actual_cum_over_expected_cum",
        "rvol_pace": 5.0,
        "expected_cum_vol": 100_000.0,
        "actual_cum_vol": 500_000.0,
        "session_elapsed_fraction": 0.10,
        "session_bucket": "regular",
        "fallback_reason": "per_symbol_curve_unavailable",
    }
    ok, reason, debug = pullback_break_confirmation(
        _df(rows),
        entry_interval="5m",
        volume_spike_multiple=4.0,
        rvol_relative_floor=True,
        rvol_telemetry=telemetry,
        rvol_floor_reference=5.0,
        rvol_floor_min_multiple=1.0,
    )
    assert ok is True
    assert reason == "pullback_break_ok"
    assert debug["rvol_source"] == "market_session_curve"
    assert debug["rvol_pace"] == pytest.approx(5.0)
    assert debug["expected_cum_vol"] == pytest.approx(100_000.0)
    assert debug["actual_cum_vol"] == pytest.approx(500_000.0)
    assert debug["session_elapsed_fraction"] == pytest.approx(0.10)
    assert debug["session_bucket"] == "regular"
    assert debug["rvol_relaxed_vol_floor"] == pytest.approx(1.0)


# ── weak_break_candle adaptive close-pos ──────────────────────────────────────
def test_weak_break_candle_conviction_passes_ordinary_blocks() -> None:
    # min_close_pos boundary: a conviction close in the upper range passes; a weak close
    # in the lower range (an ordinary / doji break) blocks.
    # range 10.0..11.0; close 10.85 -> 85% of range (pass at default 0.50).
    assert is_strong_bull_break_candle(o=10.0, h=11.0, l=10.0, c=10.85) is True
    # close 10.2 -> 20% of range (weak -> block).
    assert is_strong_bull_break_candle(o=10.0, h=11.0, l=10.0, c=10.2) is False


def test_weak_break_candle_relaxed_close_pos_lets_marginal_through() -> None:
    # The adaptive close-pos: a high-RVOL name gets a RELAXED conviction bar. A green bar
    # that closes 45% up its range blocks at the strict 0.50 floor but passes when the
    # close-pos floor is relaxed to 0.40 (the upper-wick allowance is widened in lockstep,
    # since a 45%-close bar inherently carries a >50% upper portion).
    # range 10.0..10.2 (rng 0.2), close 10.09 -> 45% of range.
    assert is_strong_bull_break_candle(o=10.0, h=10.2, l=10.0, c=10.09, min_close_pos=0.50) is False
    assert is_strong_bull_break_candle(
        o=10.0, h=10.2, l=10.0, c=10.09, min_close_pos=0.40, max_upper_wick_frac=0.60
    ) is True


def test_break_candle_gate_flag_off_parity() -> None:
    # require_break_candle off: a weak (low-close) break bar that would be vetoed as a
    # weak_break_candle is instead allowed through (flag-off parity for the new gate).
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]
    # current bar: breaks the pullback high but closes weak in the lower range (doji-ish).
    rows.append((109.7, 111.2, 109.6, 3200.0))  # high clears, close 109.7 low in range
    ok_off, reason_off, _ = pullback_break_confirmation(
        _df(rows), entry_interval="5m", require_break_candle=False
    )
    ok_on, reason_on, _ = pullback_break_confirmation(
        _df(rows), entry_interval="5m", require_break_candle=True, break_candle_min_close_pos=0.50
    )
    assert ok_on is False and reason_on == "weak_break_candle"
    assert ok_off is True and reason_off == "pullback_break_ok"


# ══════════════════════════════════════════════════════════════════════════════
# 2. DATA EDGE CASES
# ══════════════════════════════════════════════════════════════════════════════

def test_one_nan_bar_drops_frame_survives() -> None:
    # A single bad-print bar is a z-score outlier; clean_ohlcv drops it, the frame survives.
    closes = [10.0] * 20 + [10_000.0] + [10.0] * 20  # one absurd spike bar
    df = _ohlcv(closes)
    cleaned = clean_ohlcv(df, symbol="ABCD")
    assert len(cleaned) < len(df)         # the bad bar was dropped
    assert len(cleaned) >= len(df) - 2    # but the rest of the frame is intact
    assert cleaned["Close"].max() < 10_000.0


def test_zero_and_negative_volume_handling() -> None:
    # Zero-volume bars dropped for stocks; a negative-volume bar is an integrity issue.
    df = _ohlcv([10.0, 10.1, 10.2])
    df.loc[df.index[1], "Volume"] = 0.0
    cleaned = clean_ohlcv(df, symbol="ABCD")
    assert len(cleaned) == 2  # the zero-vol bar dropped

    neg = _ohlcv([10.0, 10.1])
    neg.loc[neg.index[0], "Volume"] = -5.0
    report = validate_ohlcv_integrity(neg, symbol="ABCD", interval="1d")
    assert report["clean"] is False
    assert any(i.startswith("negative_volume") for i in report["issues"])


def test_mostly_bad_frame_whole_rejects() -> None:
    # A frame riddled with structural errors (High<Low, Close outside range, nulls)
    # is whole-rejected, not silently half-cleaned into a misleading signal.
    df = pd.DataFrame(
        {
            "Open": [10.0, 10.0, 10.0],
            "High": [9.0, 9.0, 9.0],     # High < Low everywhere
            "Low": [11.0, 11.0, 11.0],
            "Close": [50.0, 50.0, 50.0],  # Close outside [Low, High]
            "Volume": [100.0, 100.0, 100.0],
        },
        index=pd.date_range("2026-01-01", periods=3, freq="D"),
    )
    report = validate_ohlcv_integrity(df, symbol="ABCD", interval="1d")
    assert report["clean"] is False
    assert any(i.startswith("high_below_low") for i in report["issues"])
    assert any(i.startswith("close_outside_range") for i in report["issues"])


def test_short_cold_frame_waits_never_false_fires() -> None:
    # Too few bars -> the trigger / momentum gates WAIT (observe), never a false fire.
    short = _df([_base(100.0) for _ in range(5)])
    ok_t, reason_t, _ = pullback_break_confirmation(short, entry_interval="5m")
    assert ok_t is False and reason_t == "insufficient_bars"
    ok_m, reason_m = momentum_volume_confirmation(short)
    assert ok_m is False and reason_m == "insufficient_bars"


def test_split_hyper_mover_real_double_passes_split_caught_daily_only() -> None:
    # #678 split veto guards: a real intraday +100% runner (INHD-style) is let through
    # because split detection is DAILY-only — the marquee #678 fix that stopped real
    # explosive movers being rejected as splits.
    intraday_hyper = validate_ohlcv_integrity(_ohlcv([3.0, 6.0]), symbol="INHD", interval="5m")
    assert intraday_hyper["clean"] is True and intraday_hyper["issues"] == []
    # A real daily +40% gap (1.4x — not near any common split ratio) stays clean even daily.
    real_gapper = validate_ohlcv_integrity(_ohlcv([5.0, 7.0]), symbol="GAPR", interval="1d")
    assert real_gapper["clean"] is True and real_gapper["issues"] == []
    # A real daily 2:1 split (exactly the common ratio) IS caught — daily only.
    split = validate_ohlcv_integrity(_ohlcv([100.0, 50.0]), symbol="AAPL", interval="1d")
    assert split["clean"] is False and split["issues"] == ["probable_splits_1"]
    # The SAME split-looking ratio INTRADAY is NOT treated as a split.
    intraday_split = validate_ohlcv_integrity(_ohlcv([100.0, 50.0]), symbol="AAPL", interval="5m")
    assert intraday_split["clean"] is True


def test_split_detection_only_flags_common_ratios() -> None:
    # The dollar-volume / common-ratio guard: a +100% real move (2.0x) is the marginal
    # case — exactly a common 2:1 ratio, so on a DAILY bar it IS flagged (correct: a
    # real unadjusted 2:1 split looks identical and must be caught). A 2.6x gap is NOT.
    assert detect_stock_split(_ohlcv([10.0, 20.0])) != []   # 2.0x common ratio -> flagged
    assert detect_stock_split(_ohlcv([10.0, 26.0])) == []   # 2.6x not a common ratio


def test_halt_stale_data_blocks() -> None:
    # A halt manifests as stale market data (no fresh prints) -> the quote gate blocks.
    t = _tick(bid=9.99, ask=10.01, age=300.0, max_age=30.0)  # 5min stale ~ halted
    blk = _quote_quality_block(t, t.freshness)
    assert blk is not None and blk["reason"] == "stale_bbo"


def test_large_gap_not_near_split_stays_clean() -> None:
    # A genuine large gap (not a common split ratio) is left clean — not mistaken for a split.
    report = validate_ohlcv_integrity(_ohlcv([214.34, 318.10]), symbol="MDB", interval="1d")
    assert report["clean"] is True and report["issues"] == []


def test_duplicate_tick_no_false_break() -> None:
    # Identical consecutive bars (a stuck / duplicated feed) never synthesize a break:
    # there is no range / no new high, so the trigger waits rather than false-firing.
    flat = _df([_base(100.0) for _ in range(30)])
    ok, reason, _ = pullback_break_confirmation(flat, entry_interval="5m")
    assert ok is False
    assert reason in ("no_range", "waiting_for_break", "pullback_too_deep")


# ══════════════════════════════════════════════════════════════════════════════
# 3. PROVIDER FAILURE
# ══════════════════════════════════════════════════════════════════════════════

def test_sizing_zero_qty_on_bad_inputs_never_oversize_or_crash() -> None:
    # Each unusable provider input yields qty 0 with a reason — never a crash, never
    # an oversize (NaN / inf would be catastrophic with real broker routing).
    for kwargs, expect in [
        (dict(entry_price=0.0, atr_pct=0.05, max_loss_usd=50.0, max_notional_ceiling_usd=500.0), "invalid_entry"),
        (dict(entry_price=10.0, atr_pct=0.05, max_loss_usd=0.0, max_notional_ceiling_usd=500.0), "max_loss_nonpositive"),
        (dict(entry_price=float("nan"), atr_pct=0.05, max_loss_usd=50.0, max_notional_ceiling_usd=500.0), "invalid_entry"),
        (dict(entry_price=10.0, atr_pct=0.05, max_loss_usd=float("nan"), max_notional_ceiling_usd=500.0), "max_loss_nonpositive"),
    ]:
        qty, meta = compute_risk_first_quantity(**kwargs)
        assert qty == 0.0
        assert meta["reason"] == expect


def test_sizing_zero_atr_uses_floor_not_div_by_zero() -> None:
    # atr_pct = 0 must NOT divide by zero — the stop floor (0.3%) bounds the distance,
    # producing a finite, positive, ceiling-bounded qty.
    qty, meta = compute_risk_first_quantity(
        entry_price=10.0, atr_pct=0.0, max_loss_usd=50.0, max_notional_ceiling_usd=500.0
    )
    assert qty > 0.0 and math.isfinite(qty)
    assert qty * 10.0 <= 500.0 + 1e-6           # never exceeds the notional ceiling
    assert meta["model"] == "risk_first"


def test_sizing_capped_by_notional_ceiling_not_oversize() -> None:
    # A tiny stop (huge risk-first qty) is clamped to the notional ceiling, never oversize.
    qty, meta = compute_risk_first_quantity(
        entry_price=10.0, atr_pct=0.0001, max_loss_usd=10_000.0, max_notional_ceiling_usd=500.0
    )
    assert qty * 10.0 <= 500.0 + 1e-6
    assert meta["capped_by"] == "notional_ceiling"


def test_stale_quote_blocks_not_fills() -> None:
    # The provider returns a stale quote (provider-side latency / outage) -> the entry
    # gate blocks; it must NOT fall through and fill on a stale price.
    t = _tick(bid=99.99, ask=100.01, age=60.0, max_age=30.0)
    assert _quote_quality_block(t, t.freshness) is not None


def test_invalid_bbo_blocks() -> None:
    # Provider returns a crossed / zero book (corrupt quote) -> blocked as invalid_bbo.
    crossed = _tick(bid=100.0, ask=99.0, mid=99.5)  # ask < bid
    blk = _quote_quality_block(crossed, crossed.freshness)
    assert blk is not None and blk["reason"] == "invalid_bbo"
    zero = _tick(bid=0.0, ask=0.0, mid=0.0)
    blk2 = _quote_quality_block(zero, zero.freshness)
    assert blk2 is not None and blk2["reason"] == "invalid_bbo"


def test_per_symbol_provider_error_skips_not_crashes() -> None:
    # A snapshot full of malformed per-symbol rows must SKIP the bad rows (fail-open),
    # not raise — the universe build returns only the parseable survivors.
    snap = [
        None,                                   # not a dict
        {"ticker": ""},                         # empty ticker
        {"ticker": "NOPRICE"},                  # missing price -> skipped
        {"ticker": "BADCHG", "lastTrade": {"p": 5.0}, "day": {"v": 1_000_000.0}},  # no chg -> skip
        _snap_row("GOOD", price=5.0, chg_pct=40.0, day_vol=1_000_000.0),
    ]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert out == ["GOOD"]


def test_universe_empty_snapshot_fails_open_to_empty_list() -> None:
    # An upstream provider outage (empty / None snapshot) returns [] so the caller falls
    # back to its default universe — never a crash, never a half-built pool.
    assert build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=[]) == []


def test_quote_block_invalid_numeric_bbo_fails_safe() -> None:
    # Non-numeric provider fields (a string price) fail safe to invalid_bbo, not a crash.
    bad = SimpleNamespace(bid="x", ask="y", mid="z", spread_bps=None, freshness=None)
    blk = _quote_quality_block(bad, None)
    assert blk is not None and blk["reason"] == "invalid_bbo"
