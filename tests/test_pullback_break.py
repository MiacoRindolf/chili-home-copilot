"""Ross-style pullback-break entry trigger (1m/5m)."""
from __future__ import annotations

import pandas as pd

from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation


def _df(rows: list[tuple[float, float, float, float]]) -> pd.DataFrame:
    return pd.DataFrame(
        [{"Open": c, "High": h, "Low": lo, "Close": c, "Volume": v} for (c, h, lo, v) in rows]
    )


def _base(close: float, vol: float = 1000.0) -> tuple[float, float, float, float]:
    return (close, close + 0.3, close - 0.3, vol)


def test_pullback_break_fires_on_shallow_pullback_then_break() -> None:
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]  # impulse
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]  # shallow pullback (holds high)
    rows.append((110.6, 111.2, 109.6, 3200.0))  # current: breaks pullback high + volume spike
    ok, reason, dbg = pullback_break_confirmation(_df(rows), entry_interval="5m")
    assert ok is True, (reason, dbg)
    assert reason == "pullback_break_ok"
    assert "pullback_low" in dbg  # structural stop available


def test_deep_pullback_rejected() -> None:
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]
    rows += [_base(103.0, 800.0), _base(102.0, 800.0)]  # deep pullback (>50% retrace)
    rows.append((104.0, 110.5, 103.0, 3200.0))
    ok, reason, _ = pullback_break_confirmation(_df(rows), entry_interval="5m")
    assert ok is False
    assert reason == "pullback_too_deep"


def test_no_break_waits() -> None:
    rows = [_base(100.0) for _ in range(14)]
    rows += [_base(c) for c in (102.0, 104.0, 106.0, 108.0, 110.0)]
    rows += [_base(109.0, 800.0), _base(108.5, 800.0)]
    rows.append((109.0, 109.5, 108.0, 3200.0))  # current high 109.5 < pullback high ~110.3
    ok, reason, _ = pullback_break_confirmation(_df(rows), entry_interval="5m")
    assert ok is False
    assert reason == "waiting_for_break"


def test_insufficient_bars() -> None:
    ok, reason, _ = pullback_break_confirmation(_df([_base(100.0) for _ in range(5)]))
    assert ok is False
    assert reason == "insufficient_bars"
