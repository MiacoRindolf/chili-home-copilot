"""Why did CHILI take 0 trades on INHD? Tally the entry-gate reason per bar under
the live config vs progressively looser configs, and dump the intraday structure."""
from __future__ import annotations

from collections import Counter

import pandas as pd

from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation
from app.services.trading.indicator_core import compute_all_from_df

SYM, INTERVAL = "INHD", "5m"
df = fetch_ohlcv_df(SYM, interval=INTERVAL, period="1d")
c = {x.lower(): x for x in df.columns}
O, H, L, C, V = c["open"], c["high"], c["low"], c["close"], c["volume"]
n = len(df)
idx = df.index
RTH_START = pd.Timestamp("2026-06-08 13:30", tz="UTC")

CONFIGS = {
    "LIVE (retest=T, sustain=T)": dict(require_retest=True, require_sustained_volume=True),
    "no-retest (retest=F, sustain=T)": dict(require_retest=False, require_sustained_volume=True),
    "raw break (retest=F, sustain=F)": dict(require_retest=False, require_sustained_volume=False),
}
BASE = dict(entry_interval=INTERVAL, volume_spike_multiple=1.5, retest_tolerance=0.002,
            retest_lookback_bars=4, sustained_rvol_floor=1.0, sustain_lookback_bars=5)

for label, over in CONFIGS.items():
    reasons = Counter()
    fires = []
    for i in range(10, n):
        ok, reason, dbg = pullback_break_confirmation(df.iloc[: i + 1], **BASE, **over)
        reasons[reason] += 1
        if ok:
            fires.append((idx[i], dbg.get("pullback_low"), dbg.get("pullback_high")))
    print(f"\n=== {label} ===")
    for r, cnt in reasons.most_common():
        print(f"   {cnt:3d}  {r}")
    print(f"   FIRES: {len(fires)}", "->", [f"{t:%H:%M}" for t, _, _ in fires][:12])

# Intraday structure (RTH 5m): how deep are the pullbacks, does it hold EMA-9?
print("\n=== INHD 5m RTH structure (close, %chg/bar, EMA9, vol_ratio) ===")
arrs = compute_all_from_df(df, needed={"ema_9", "volume_ratio"})
ema9, vr = arrs.get("ema_9") or [], arrs.get("volume_ratio") or []
prev = None
for i in range(n):
    ts = idx[i]
    if ts < RTH_START:
        continue
    cl = float(df[C].iloc[i]); hi = float(df[H].iloc[i]); lo = float(df[L].iloc[i])
    chg = ((cl - prev) / prev * 100) if prev else 0.0
    e = ema9[i] if i < len(ema9) and ema9[i] is not None else None
    v = vr[i] if i < len(vr) and vr[i] is not None else None
    above = "" if e is None else ("aboveEMA9" if cl >= e else "BELOW-ema9")
    print(f" {ts:%H:%M}  C={cl:7.2f}  {chg:+6.1f}%/bar  H={hi:7.2f} L={lo:7.2f}  ema9={(f'{e:.2f}' if e else 'NA'):>7}  vr={(f'{v:.1f}' if v else 'NA'):>5}  {above}")
    prev = cl
