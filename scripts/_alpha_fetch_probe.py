"""1m/daily OHLCV availability probe for sampled tape symbol-days (no DB access).

Reads scripts/_alpha_symday_sample.tsv (symbol, et_day, first_obs_et, tape_mins)
and checks, per symbol-day:
  - 1m bars exist for the selection day (fetched with explicit start/end)
  - >= 30 forward 1m bars at/after the first tape observation (labelable for +3R)
Also: SPY/QQQ 1m + 1d, and 1y daily for 3 names (lifecycle features).
"""
import sys
from collections import Counter
from datetime import datetime, timedelta

from app.services.trading.market_data import fetch_ohlcv_df

rows = []
with open("scripts/_alpha_symday_sample.tsv", encoding="utf-8") as f:
    for line in f:
        parts = line.rstrip("\n").split("\t")
        if len(parts) != 4:
            continue
        sym, d, first_obs, mins = parts
        rows.append((sym, datetime.strptime(d, "%Y-%m-%d").date(),
                     datetime.fromisoformat(first_obs), int(mins)))

hits = 0
checks = 0
day_hit = Counter()
day_n = Counter()
for sym, d, first_obs, mins in rows:
    start = str(d - timedelta(days=1))
    end = str(d + timedelta(days=2))
    checks += 1
    day_n[d] += 1
    try:
        df = fetch_ohlcv_df(sym, interval="1m", start=start, end=end)
    except Exception as e:  # noqa: BLE001
        print(f"  {d} {sym}: FETCH ERROR {type(e).__name__}: {e}")
        continue
    if df is None or df.empty:
        print(f"  {d} {sym}: no 1m bars (sel {first_obs:%H:%M} ET, tape_mins={mins})")
        continue
    idx = df.index.tz_convert("America/New_York") if df.index.tz is not None else df.index
    df = df.copy()
    df.index = idx.tz_localize(None) if getattr(idx, "tz", None) is not None else idx
    sel_day = df[[ts.date() == d for ts in df.index]]
    fwd = sel_day[sel_day.index >= first_obs] if len(sel_day) else sel_day
    ok = len(fwd) >= 30
    hits += int(ok)
    day_hit[d] += int(ok)
    print(f"  {d} {sym}: total={len(df)} sel_day={len(sel_day)} fwd={len(fwd)} "
          f"(sel {first_obs:%H:%M} ET) -> {'OK' if ok else 'THIN'}")

print(f"\n1m forward-label availability: {hits}/{checks} sampled symbol-days OK (>=30 fwd 1m bars)")
for d in sorted(day_n):
    print(f"  {d}: {day_hit[d]}/{day_n[d]}")

print("\nmarket regime fetch (SPY/QQQ):")
for t in ("SPY", "QQQ"):
    try:
        d1 = fetch_ohlcv_df(t, interval="1m", period="5d")
        dd = fetch_ohlcv_df(t, interval="1d", period="1y")
        r1 = "none" if d1 is None or d1.empty else f"{len(d1)} bars {d1.index.min()}..{d1.index.max()}"
        rd = "none" if dd is None or dd.empty else f"{len(dd)} bars"
        print(f"  {t}: 1m 5d: {r1}; 1d 1y: {rd}")
    except Exception as e:  # noqa: BLE001
        print(f"  {t}: ERROR {e}")

print("\ndaily lifecycle fetch (1y daily, 3 sampled names):")
seen = set()
for sym, d, _, _ in reversed(rows):
    if sym in seen or len(seen) >= 3:
        continue
    seen.add(sym)
    try:
        dd = fetch_ohlcv_df(sym, interval="1d", period="1y")
        n = 0 if dd is None else len(dd)
        print(f"  {sym}: 1d bars={n} (250d-high/200MA computable: {n >= 200})")
    except Exception as e:  # noqa: BLE001
        print(f"  {sym}: ERROR {e}")
