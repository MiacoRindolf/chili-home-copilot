"""LOG-ONLY 10s-candle pattern layer — pure-fn units + the zero-decision-path guard.

The whole point is MEASUREMENT with no live risk: these tests prove the 10s aggregation
+ ABCD/flat-top detectors are correct, the thin-data guard keeps a 60s-sparse source
DARK, and NOTHING in the decision path imports this module.
"""
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

from app.services.trading.fast_path.tenbeat_candle_log import (
    _bucket_floor,
    _candle_shape,
    aggregate_10s_candles,
    detect_abcd,
    detect_flat_top,
)


def _bar(o, h, l, c, ts=None):
    return {"ts": ts or datetime(2026, 6, 14), "open": o, "high": h, "low": l,
            "close": c, "volume": 0.0, "tick_count": 5}


# ── 10s aggregation ──────────────────────────────────────────────────────────

def test_bucket_floor_aligned():
    ts = datetime(2026, 6, 14, 13, 30, 17, 500000)
    assert _bucket_floor(ts, 10) == datetime(2026, 6, 14, 13, 30, 10)


def test_aggregate_completed_bars_and_gap_skip():
    base = datetime.utcnow() - timedelta(minutes=10)  # well in the past => completed
    base = _bucket_floor(base, 10)
    ticks = []
    # bucket 0: 3 ticks 100->102 ; bucket 1: 1 tick (GAP, min_ticks=2) ; bucket 2: 2 ticks
    for i, m in enumerate((100.0, 101.0, 102.0)):
        ticks.append((base + timedelta(seconds=i * 2), m, 1.0))
    ticks.append((base + timedelta(seconds=12), 103.0, 1.0))           # lone tick -> gap
    ticks.append((base + timedelta(seconds=20), 104.0, 1.0))
    ticks.append((base + timedelta(seconds=22), 105.0, 1.0))
    bars = aggregate_10s_candles(ticks, bucket_s=10, min_ticks=2, max_bars=12)
    assert len(bars) == 2                                              # the gap bucket is skipped
    assert bars[0]["open"] == 100.0 and bars[0]["high"] == 102.0 and bars[0]["close"] == 102.0


def test_thin_60s_source_emits_no_bars():
    """A 60s-sparse source (1 tick per 10s bucket) => every bucket fails min_ticks =>
    NO bars. This is the equity-stays-dark / no-fiction guarantee."""
    base = _bucket_floor(datetime.utcnow() - timedelta(minutes=10), 10)
    ticks = [(base + timedelta(seconds=i * 60), 100.0 + i, 1.0) for i in range(6)]
    assert aggregate_10s_candles(ticks, bucket_s=10, min_ticks=2, max_bars=12) == []


def test_in_progress_bucket_dropped():
    now = datetime.utcnow()
    ticks = [(now, 100.0, 1.0), (now, 101.0, 1.0)]  # current bucket only
    assert aggregate_10s_candles(ticks, bucket_s=10, min_ticks=2) == []


# ── ABCD detector ────────────────────────────────────────────────────────────

def _abcd_bars(c_low):
    # A(100) -> impulse -> B(110) -> C(c_low) -> D(close 111 breaks B)
    return [
        _bar(100, 101, 100, 101),   # A
        _bar(101, 106, 101, 106),
        _bar(106, 110, 105, 110),   # B (high 110)
        _bar(110, 108, c_low, c_low + 0.5),  # C (pullback low = c_low)
        _bar(108, 112, 108, 111),   # D (close 111 > B 110)
    ]


def test_abcd_fires_on_clean_retrace():
    ctx = detect_abcd(_abcd_bars(105.0), retrace_base=0.50, atr_pct=0.02)  # retrace 0.5
    assert ctx["abcd_pattern"] is True
    assert ctx["entry_level"] == 110.0 and ctx["stop_level"] == 105.0
    assert ctx["abcd_score"] is not None and 0.0 <= ctx["abcd_score"] <= 1.0


def test_abcd_rejects_too_deep_retrace():
    # C at 100.5 => retrace (110-100.5)/10 = 0.95 >> shallow cap => not a clean ABCD
    ctx = detect_abcd(_abcd_bars(100.5), retrace_base=0.50, atr_pct=0.02)
    assert ctx["abcd_pattern"] is False


def test_abcd_too_few_bars():
    assert detect_abcd([_bar(1, 1, 1, 1)] * 3)["abcd_pattern"] is False


# ── flat-top detector ────────────────────────────────────────────────────────

def test_flat_top_fires_on_touches_and_break():
    # 3 highs clustered ~10.00 (flat resistance), then a bar closing well above it
    bars = [
        _bar(9.8, 10.00, 9.7, 9.9),
        _bar(9.9, 10.01, 9.8, 9.95),
        _bar(9.95, 9.99, 9.85, 9.9),
        _bar(9.9, 10.40, 9.9, 10.35),   # break + thrust above ~10.00
    ]
    ctx = detect_flat_top(bars, touches_min=3, lookback_bars=6, atr_pct=0.02)
    assert ctx["flatop_pattern"] is True
    assert ctx["entry_level"] is not None


def test_flat_top_rejects_one_tick_poke():
    # same resistance but the last bar only pokes 1 cent above => not an ATR thrust
    bars = [
        _bar(9.8, 10.00, 9.7, 9.9),
        _bar(9.9, 10.01, 9.8, 9.95),
        _bar(9.95, 9.99, 9.85, 9.9),
        _bar(9.9, 10.02, 9.9, 10.005),  # barely over => below the ATR-scaled thrust
    ]
    ctx = detect_flat_top(bars, touches_min=3, lookback_bars=6, atr_pct=0.02)
    assert ctx["flatop_pattern"] is False


# ── candle shape ─────────────────────────────────────────────────────────────

def test_candle_shape_tags():
    assert _candle_shape(_bar(100, 110, 100, 109)) == "strong_bull"     # big body, no upper wick
    assert _candle_shape(_bar(100, 110, 99, 101)) == "topping_tail"     # long upper wick, close low
    assert _candle_shape(_bar(100, 101, 99, 100.5)) == "neutral"        # small body, small wicks


# ── the zero-decision-path guarantee ─────────────────────────────────────────

def test_no_decision_path_imports_tenbeat():
    """LOG-ONLY: nothing in the live decision path may import tenbeat_candle_log.
    Only the scheduler (which schedules the log jobs) and tests may reference it."""
    root = Path(__file__).resolve().parents[1] / "app" / "services" / "trading"
    targets = ["momentum_neural", "auto_trader.py", "pipeline.py", "replay_v2.py"]
    out = subprocess.run(
        ["grep", "-rl", "tenbeat_candle_log",
         *[str(root / t) for t in targets]],
        capture_output=True, text=True,
    )
    assert out.stdout.strip() == "", f"decision-path import of tenbeat_candle_log: {out.stdout}"
