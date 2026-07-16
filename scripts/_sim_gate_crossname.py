"""Is the 0-entry result INHD-specific or systemic across the explosive small-caps
the lane now selects? Tally pullback-break fires + reasons per name (live config)."""
from __future__ import annotations

from collections import Counter

from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation

NAMES = ["INHD", "SUNE", "NPT", "GLXU", "IXHL", "BYAH", "CBRG", "SDOT", "MOBX", "GLGG"]
LIVE = dict(entry_interval="5m", volume_spike_multiple=1.5,
            require_retest=True, retest_tolerance=0.002, retest_lookback_bars=4,
            require_sustained_volume=True, sustained_rvol_floor=1.0, sustain_lookback_bars=5)

print(f"{'name':6s} {'bars':>4s} {'fires':>5s}  top reasons")
tot_fires = 0
for sym in NAMES:
    try:
        df = fetch_ohlcv_df(sym, interval="5m", period="1d")
        if df is None or len(df) < 12:
            print(f"{sym:6s}  (insufficient bars)")
            continue
        n = len(df)
        reasons = Counter()
        fires = 0
        for i in range(10, n):
            ok, reason, _ = pullback_break_confirmation(df.iloc[: i + 1], **LIVE)
            reasons[reason] += 1
            fires += int(ok)
        tot_fires += fires
        top = "  ".join(f"{r}:{cnt}" for r, cnt in reasons.most_common(3))
        print(f"{sym:6s} {n:4d} {fires:5d}  {top}")
    except Exception as e:
        print(f"{sym:6s}  ERR {e}")
print(f"\nTOTAL fires across {len(NAMES)} explosive small-caps: {tot_fires}")
