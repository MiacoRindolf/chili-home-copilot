"""Phase L.22 pure-model unit tests for intraday session classifier."""
from __future__ import annotations

import math
from datetime import date
from typing import List, Optional, Sequence

import pytest

from app.services.trading.intraday_session_model import (
    IntradayBar,
    IntradaySessionConfig,
    IntradaySessionInput,
    RTH_BARS_5MIN,
    RTH_CLOSE_MINUTE,
    RTH_OPEN_MINUTE,
    SESSION_COMPRESSED,
    SESSION_GAP_AND_GO,
    SESSION_GAP_FADE,
    SESSION_NEUTRAL,
    SESSION_RANGE_BOUND,
    SESSION_REVERSAL,
    SESSION_TRENDING_DOWN,
    SESSION_TRENDING_UP,
    VALID_SESSION_LABELS,
    compute_intraday_session,
    compute_snapshot_id,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _bars_linear(
    *,
    bars_count: int = RTH_BARS_5MIN,
    open_price: float = 100.0,
    slope_per_bar: float = 0.0,
    half_range: float = 0.05,
    volume: float = 1_000_000.0,
    start_minute: int = RTH_OPEN_MINUTE,
    bar_minutes: int = 5,
) -> List[IntradayBar]:
    """Synthetic 5-min bars with a linear close-to-close drift and a
    symmetric high/low range. Deterministic."""
    bars: List[IntradayBar] = []
    px = open_price
    for i in range(bars_count):
        c_open = px
        c_close = px + slope_per_bar
        hi = max(c_open, c_close) + half_range
        lo = min(c_open, c_close) - half_range
        bars.append(
            IntradayBar(
                ts_minute=start_minute + i * bar_minutes,
                open=c_open,
                high=hi,
                low=lo,
                close=c_close,
                volume=volume,
            )
        )
        px = c_close
    return bars


def _override_bars(
    bars: Sequence[IntradayBar],
    ts_start: int,
    ts_end: int,
    *,
    half_range: Optional[float] = None,
    close_offset: Optional[float] = None,
    volume: Optional[float] = None,
) -> List[IntradayBar]:
    """Return a new list where bars in [ts_start, ts_end) have modified
    fields. Used to plant an opening range, reversal etc."""
    out: List[IntradayBar] = []
    for b in bars:
        if ts_start <= b.ts_minute < ts_end:
            new_hi = b.high
            new_lo = b.low
            new_close = b.close
            new_vol = b.volume
            if half_range is not None:
                mid = (b.high + b.low) / 2.0
                new_hi = mid + half_range
                new_lo = mid - half_range
            if close_offset is not None:
                new_close = b.close + close_offset
                new_hi = max(new_hi, new_close)
                new_lo = min(new_lo, new_close)
            if volume is not None:
                new_vol = volume
            out.append(
                IntradayBar(
                    ts_minute=b.ts_minute,
                    open=b.open,
                    high=new_hi,
                    low=new_lo,
                    close=new_close,
                    volume=new_vol,
                )
            )
        else:
            out.append(b)
    return out


# ---------------------------------------------------------------------------
# snapshot_id
# ---------------------------------------------------------------------------


def test_compute_snapshot_id_is_deterministic() -> None:
    d = date(2026, 4, 16)
    a = compute_snapshot_id(d)
    b = compute_snapshot_id(d)
    assert a == b
    assert len(a) == 16


def test_compute_snapshot_id_varies_by_date() -> None:
    a = compute_snapshot_id(date(2026, 4, 16))
    b = compute_snapshot_id(date(2026, 4, 17))
    assert a != b


# ---------------------------------------------------------------------------
# Primitive behaviour (through the orchestrator)
# ---------------------------------------------------------------------------


def test_empty_bars_are_neutral() -> None:
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=[])
    )
    assert out.session_label == SESSION_NEUTRAL
    assert out.session_numeric == 0
    assert out.bars_observed == 0
    assert out.coverage_score == 0.0
    assert out.open_price is None
    assert out.close_price is None


def test_insufficient_bars_are_neutral() -> None:
    bars = _bars_linear(bars_count=10, slope_per_bar=0.001)
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=bars)
    )
    assert out.bars_observed == 10
    assert out.session_label == SESSION_NEUTRAL
    assert out.coverage_score == pytest.approx(10.0 / RTH_BARS_5MIN)


def test_bars_outside_rth_are_dropped() -> None:
    # Pre-market bar at 09:00 should be ignored.
    cfg = IntradaySessionConfig()
    bars = _bars_linear(bars_count=RTH_BARS_5MIN, slope_per_bar=0.001)
    pre = IntradayBar(
        ts_minute=9 * 60,
        open=99.0,
        high=99.5,
        low=98.5,
        close=99.2,
        volume=100.0,
    )
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16), bars=[pre, *bars], config=cfg
        )
    )
    assert out.bars_observed == RTH_BARS_5MIN  # pre-market not counted
    assert out.open_price == bars[0].open


# ---------------------------------------------------------------------------
# OR / midday / power hour extraction
# ---------------------------------------------------------------------------


def test_opening_range_covers_first_30_min() -> None:
    cfg = IntradaySessionConfig(
        min_bars=0, or_minutes=30, midday_compression_cut=1e9
    )
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN, slope_per_bar=0.0, half_range=0.02
    )
    # Plant a wide opening range on first 6 bars (09:30-10:00).
    bars = _override_bars(
        bars,
        RTH_OPEN_MINUTE,
        RTH_OPEN_MINUTE + 30,
        half_range=0.8,
    )
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=bars, config=cfg)
    )
    assert out.or_high is not None and out.or_low is not None
    assert out.or_high - out.or_low > 1.5  # we planted ~1.6
    assert out.or_range_pct is not None and out.or_range_pct > 0.01


def test_midday_window_respected() -> None:
    cfg = IntradaySessionConfig(min_bars=0)
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN, slope_per_bar=0.0, half_range=0.4
    )
    # Compress midday window (12:00-14:00) to nearly zero range.
    bars = _override_bars(
        bars, 12 * 60, 14 * 60, half_range=0.01, volume=500_000.0
    )
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=bars, config=cfg)
    )
    assert out.midday_range_pct is not None
    # Very small vs the un-compressed OR + session
    assert out.midday_range_pct < 0.002


def test_power_hour_covers_last_30_min() -> None:
    cfg = IntradaySessionConfig(min_bars=0, power_minutes=30)
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN, slope_per_bar=0.0, half_range=0.05
    )
    # Plant a wide power hour on last 6 bars (15:30-16:00).
    bars = _override_bars(
        bars, RTH_CLOSE_MINUTE - 30, RTH_CLOSE_MINUTE, half_range=1.5
    )
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=bars, config=cfg)
    )
    assert out.ph_range_pct is not None and out.ph_range_pct > 0.02


# ---------------------------------------------------------------------------
# Decision tree scenarios
# ---------------------------------------------------------------------------


def _make_trending_up_bars(open_price: float = 100.0) -> List[IntradayBar]:
    # 78 bars × +0.01 drift = +0.78 final move (~0.78%) above trending threshold.
    return _bars_linear(
        bars_count=RTH_BARS_5MIN,
        open_price=open_price,
        slope_per_bar=0.012,
        half_range=0.02,
    )


def test_trending_up_session() -> None:
    cfg = IntradaySessionConfig(
        min_bars=RTH_BARS_5MIN - 5,
        trending_close_threshold=0.005,
    )
    bars = _make_trending_up_bars()
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=bars[0].open,  # no gap
            config=cfg,
        )
    )
    assert out.session_label == SESSION_TRENDING_UP
    assert out.session_numeric == +1
    # close - open should be ~+0.9
    assert out.close_price is not None and out.open_price is not None
    assert out.close_price > out.open_price


def test_trending_down_session() -> None:
    cfg = IntradaySessionConfig(
        min_bars=RTH_BARS_5MIN - 5,
        trending_close_threshold=0.005,
    )
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN,
        open_price=100.0,
        slope_per_bar=-0.012,
        half_range=0.02,
    )
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=100.0,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_TRENDING_DOWN
    assert out.session_numeric == -1


def test_gap_and_go_long_session() -> None:
    cfg = IntradaySessionConfig(
        min_bars=RTH_BARS_5MIN - 5,
        or_range_high=0.002,  # ensure wide session_range_pct qualifies
        trending_close_threshold=0.002,
        gap_magnitude_go=0.005,
    )
    # gap +1% then trend up; prev_close = 99.0, open = 100.0
    bars = _make_trending_up_bars(open_price=100.0)
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=99.0,
            config=cfg,
        )
    )
    assert out.gap_open_pct is not None and out.gap_open_pct > 0.005
    assert out.session_label == SESSION_GAP_AND_GO
    assert out.session_numeric == +3


def test_gap_fade_short_session() -> None:
    cfg = IntradaySessionConfig(
        min_bars=RTH_BARS_5MIN - 5,
        gap_magnitude_fade=0.005,
        reversal_close_threshold=0.0005,
        trending_close_threshold=1.0,  # prevent plain trending label
    )
    # Gap up 1% from 99.0 to 100.0, then fade down to ~99.
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN,
        open_price=100.0,
        slope_per_bar=-0.012,
        half_range=0.02,
    )
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=99.0,
            config=cfg,
        )
    )
    assert out.gap_open_pct is not None and out.gap_open_pct > 0.005
    # Close below open -> against gap direction
    assert out.close_price is not None and out.open_price is not None
    assert out.close_price < out.open_price
    assert out.session_label == SESSION_GAP_FADE
    assert out.session_numeric == -3


def test_reversal_session() -> None:
    cfg = IntradaySessionConfig(
        min_bars=RTH_BARS_5MIN - 5,
        midday_compression_cut=0.4,
        reversal_close_threshold=0.001,
        trending_close_threshold=1.0,  # suppress trending branch
        or_range_low=0.0,
    )
    # Flat session (no trend), but plant a wide OR and compressed midday.
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN,
        open_price=100.0,
        slope_per_bar=0.0,
        half_range=0.05,
    )
    # Wide OR (first 30 min).
    bars = _override_bars(
        bars, RTH_OPEN_MINUTE, RTH_OPEN_MINUTE + 30, half_range=0.8
    )
    # Compressed midday.
    bars = _override_bars(bars, 12 * 60, 14 * 60, half_range=0.01)
    # Push the last bar close above OR midpoint.
    bars[-1] = IntradayBar(
        ts_minute=bars[-1].ts_minute,
        open=bars[-1].open,
        high=bars[-1].open + 1.2,
        low=bars[-1].open - 0.01,
        close=bars[-1].open + 1.0,
        volume=bars[-1].volume,
    )
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=bars[0].open,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_REVERSAL
    assert out.session_numeric == +2


def test_compressed_session() -> None:
    cfg = IntradaySessionConfig(
        min_bars=RTH_BARS_5MIN - 5,
        or_range_low=0.01,
        or_range_high=0.02,
        trending_close_threshold=1.0,
        reversal_close_threshold=1.0,
    )
    # Very tight session (OR tiny, total range tiny).
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN,
        open_price=100.0,
        slope_per_bar=0.0,
        half_range=0.005,
    )
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=100.0,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_COMPRESSED
    assert out.session_numeric == 0


def test_range_bound_fallback_session() -> None:
    cfg = IntradaySessionConfig(
        min_bars=RTH_BARS_5MIN - 5,
        or_range_low=0.0005,
        or_range_high=0.05,
        trending_close_threshold=1.0,
        reversal_close_threshold=1.0,
    )
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN,
        open_price=100.0,
        slope_per_bar=0.0,
        half_range=0.1,
    )
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=100.0,
            config=cfg,
        )
    )
    assert out.session_label == SESSION_RANGE_BOUND
    assert out.session_numeric == 0


# ---------------------------------------------------------------------------
# Scalars & meta
# ---------------------------------------------------------------------------


def test_gap_pct_is_none_without_prev_close() -> None:
    bars = _bars_linear(bars_count=RTH_BARS_5MIN, slope_per_bar=0.0)
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16),
            bars=bars,
            prev_close=None,
        )
    )
    assert out.gap_open is None
    assert out.gap_open_pct is None


def test_intraday_rv_is_finite_and_positive() -> None:
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN,
        slope_per_bar=0.0,
        half_range=0.05,
    )
    # Alternate close offsets to create variance.
    perturbed: List[IntradayBar] = []
    for i, b in enumerate(bars):
        delta = 0.02 if (i % 2 == 0) else -0.02
        perturbed.append(
            IntradayBar(
                ts_minute=b.ts_minute,
                open=b.open,
                high=b.high,
                low=b.low,
                close=b.close + delta,
                volume=b.volume,
            )
        )
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=perturbed)
    )
    assert out.intraday_rv is not None
    assert out.intraday_rv > 0.0
    assert math.isfinite(out.intraday_rv)


def test_intraday_rv_none_on_zero_variance() -> None:
    # All closes identical -> sd=0 -> None
    bars = _bars_linear(
        bars_count=RTH_BARS_5MIN,
        slope_per_bar=0.0,
        half_range=0.05,
    )
    flat: List[IntradayBar] = [
        IntradayBar(
            ts_minute=b.ts_minute,
            open=100.0,
            high=100.01,
            low=99.99,
            close=100.0,
            volume=b.volume,
        )
        for b in bars
    ]
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=flat)
    )
    assert out.intraday_rv is None


def test_coverage_score_one_when_full_session() -> None:
    bars = _bars_linear(bars_count=RTH_BARS_5MIN, slope_per_bar=0.0)
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=bars)
    )
    assert out.bars_observed == RTH_BARS_5MIN
    assert out.coverage_score == pytest.approx(1.0)


def test_label_is_always_in_valid_set() -> None:
    bars = _bars_linear(bars_count=RTH_BARS_5MIN, slope_per_bar=0.01)
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=bars)
    )
    assert out.session_label in VALID_SESSION_LABELS


def test_payload_config_echo() -> None:
    bars = _bars_linear(bars_count=RTH_BARS_5MIN, slope_per_bar=0.0)
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=bars)
    )
    assert "config" in out.payload
    cfg_echo = out.payload["config"]
    assert "or_minutes" in cfg_echo
    assert "midday_compression_cut" in cfg_echo
    assert cfg_echo["bar_minutes"] == 5


def test_close_vs_or_mid_sign() -> None:
    cfg = IntradaySessionConfig(min_bars=RTH_BARS_5MIN - 5)
    bars = _make_trending_up_bars()
    out = compute_intraday_session(
        IntradaySessionInput(
            as_of_date=date(2026, 4, 16), bars=bars, config=cfg
        )
    )
    # In a trending-up session the close is above the OR midpoint.
    assert out.close_vs_or_mid_pct is not None
    assert out.close_vs_or_mid_pct > 0


def test_or_volume_ratio_reflects_openers() -> None:
    cfg = IntradaySessionConfig(min_bars=RTH_BARS_5MIN - 5)
    bars = _bars_linear(bars_count=RTH_BARS_5MIN, slope_per_bar=0.0)
    # Boost volume on the first 6 bars 5×.
    bars = _override_bars(
        bars, RTH_OPEN_MINUTE, RTH_OPEN_MINUTE + 30, volume=5_000_000.0
    )
    out = compute_intraday_session(
        IntradaySessionInput(as_of_date=date(2026, 4, 16), bars=bars, config=cfg)
    )
    assert out.or_volume_ratio is not None
    assert out.or_volume_ratio > 1.5
