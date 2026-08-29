"""MIN FADE DEPTH sa sticky backside bench — ang SWVL/near-HOD na klase.

SINUKAT (2026-08-28, 14 live episodes): lahat ng TAMANG bench ay 15-40% ang
lalim mula sa HOD; ang 2 maling bench (SWVL — lumampas sa HOD sa loob ng 30
min) ay 1-1.3% lang ang "fade". Ang bench ay hindi dapat mag-latch sa
pangalang nasa loob ng min_fade_pct ng sariling high — front-side iyon sa
lalim. Replay sanity: XLAB/CELU/OBAI/BRNX identical, MIMI +0.78.

Runnable: pytest tests/test_backside_bench_min_fade.py -v
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from app.config import settings
from app.services.trading.momentum_neural.entry_gates import (
    evaluate_sticky_backside_bench,
)


def _idx(n):
    return pd.date_range("2026-08-28 13:30", periods=n, freq="1min", tz="UTC")


def _ohlc(closes):
    closes = np.asarray(closes, dtype=float)
    opens = np.empty(len(closes))
    opens[0] = closes[0]
    opens[1:] = closes[:-1]
    return pd.DataFrame({
        "Open": opens,
        "High": np.maximum(opens, closes) * 1.001,
        "Low": np.minimum(opens, closes) * 0.999,
        "Volume": np.full(len(closes), 1000.0),
        "Close": closes,
    }, index=_idx(len(closes)))


def _swvl_shallow_rollover():
    """SWVL shape: akyat sa ~3.04 tapos mababaw na 'rollover' sa ~3.00 —
    1.3% lang mula sa HOD, pero mukhang backside sa bar shape."""
    up = np.linspace(2.70, 3.04, 15)
    drift = np.linspace(3.03, 3.00, 10)
    return np.concatenate([up, drift])


def _deep_fade():
    """WHLR shape: 3.11 HOD tapos bagsak sa ~1.95 — 37% na fade."""
    up = np.linspace(2.00, 3.11, 10)
    down = np.linspace(3.05, 1.95, 20)
    return np.concatenate([up, down])


def test_shallow_fade_never_latches(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_backside_bench_min_fade_pct", 5.0, raising=False
    )
    df = _ohlc(_swvl_shallow_rollover())
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=3.00
    )
    assert benched is False
    # alinman: hindi backside ang shape (front_side) o hinuli ng shallow-fade
    # floor — ang mahalaga ay HINDI naka-latch at kapag backside ang basa,
    # ang dahilan ay ang bagong floor
    if reason not in ("front_side", "front_side_live_new_high"):
        assert reason == "front_side_shallow_fade", (reason, dbg)
        assert dbg.get("shallow_fade_pct") is not None


def test_deep_fade_still_latches(monkeypatch):
    monkeypatch.setattr(
        settings, "chili_momentum_backside_bench_min_fade_pct", 5.0, raising=False
    )
    df = _ohlc(_deep_fade())
    benched, reason, hod_out, dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=1.95
    )
    assert benched is True, (reason, dbg)
    assert hod_out is not None and hod_out > 3.0


def test_zero_knob_restores_old_behavior(monkeypatch):
    """0 = patay ang tseke — ang mababaw na rollover ay puwedeng ma-latch
    ulit kung backside ang basa ng shape (dating gawi)."""
    monkeypatch.setattr(
        settings, "chili_momentum_backside_bench_min_fade_pct", 0.0, raising=False
    )
    df = _ohlc(_swvl_shallow_rollover())
    _benched, reason, _hod, _dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=None, live_price=3.00
    )
    assert reason != "front_side_shallow_fade"


def test_existing_latch_is_untouched_by_the_floor(monkeypatch):
    """Ang floor ay LATCH-TIME lamang — ang naka-bench nang session ay
    nananatiling sticky (hindi nabubuksan ng floor)."""
    monkeypatch.setattr(
        settings, "chili_momentum_backside_bench_min_fade_pct", 5.0, raising=False
    )
    df = _ohlc(_deep_fade())
    benched, reason, hod_out, _dbg = evaluate_sticky_backside_bench(
        df, benched_at_hod=3.50, live_price=1.95
    )
    assert benched is True
    assert reason == "benched_backside_sticky"
