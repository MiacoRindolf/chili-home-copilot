"""PAIRED 5m-vs-1m study: should CHILI's live entry-interval move from 5m to 1m?

Rationale: Ross enters on the 1-min chart; on NPT (Jun-8) 1m caught a bigger move
(+6.38R) than 5m (+1.88R). But that is ONE monster day — the exact single-day trap we
hit before. This runs the SAME live decision (momentum_pullback_trigger -> all
confirmations at live settings) on BOTH intervals over a BASKET of Ross-style small-caps
across EVERY available day, PAIRED by (name, day) so the comparison is apples-to-apples.

Faithful: identical trigger + wide-stop chain + documented exits per interval. Entries are
gated to RTH (13:30-20:00 UTC) and forward-simulated intraday (flat by close, like Ross).
Per-day slicing makes session VWAP correct AND bounds the O(n^2) trigger cost.

Constraint: yfinance 1m history is ~7 sessions, so the window is short (noted in output).
This is the best available for a true 1m test without a paid tick feed.
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")
import statistics
import sys
import pandas as pd

from app.services.trading.indicator_core import compute_atr
from app.services.trading.market_data import fetch_ohlcv_df
from app.services.massive_client import get_full_market_snapshot
from app.services.trading.momentum_neural.candles import is_topping_tail
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger, breakout_failed_to_hold
from app.services.trading.momentum_neural.paper_execution import (
    build_synthetic_quote, effective_stop_atr_pct, long_entry_fill_price, long_exit_fill_price,
    runner_trail_stop, scale_out_fraction, stop_target_prices, structural_or_vol_floored_atr_pct,
)

STOP_ATR_MULT, REWARD_RISK, SCALE_FRAC = 0.60, 2.0, scale_out_fraction()
SLIP_BPS, SPREAD_BPS = 15.0, 40.0
MAX_NAMES = int(sys.argv[1]) if len(sys.argv) > 1 else 45


def _q(mid):
    return build_synthetic_quote(mid, SPREAD_BPS)


def _rth(ts) -> bool:
    m = ts.hour * 60 + ts.minute
    return 13 * 60 + 30 <= m <= 20 * 60


def _wide_stop(entry, atrp, pblow):
    eff = effective_stop_atr_pct(atrp, atrp * 10_000.0, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
    eff, _ = structural_or_vol_floored_atr_pct(vol_floored_atr_pct=eff, structural_stop_price=pblow, entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)


def _forward(d, O, H, L, C, ei, entry, stop, target, brk, atrp, secs):
    n = len(d); risk = entry - stop
    if risk <= 0:
        return None
    scaled = False; bal = stop; rh = entry; scx = None; j = ei; exit_px = None
    while j < n:
        bh, bl, bc = float(d[H].iloc[j]), float(d[L].iloc[j]), float(d[C].iloc[j])
        held = (j - ei) * secs; qb = _q(bc)
        if not scaled and brk and breakout_failed_to_hold(breakout_level=brk, bid=qb.bid, held_seconds=held, window_seconds=1800.0):
            exit_px = long_exit_fill_price(qb.bid, bc, SLIP_BPS); break
        if bl <= bal:
            exit_px = long_exit_fill_price(_q(bal).bid, bal, SLIP_BPS); break
        if scaled and is_topping_tail(float(d[O].iloc[j]), bh, bl, bc):
            exit_px = long_exit_fill_price(qb.bid, bc, SLIP_BPS); break
        if not scaled and bh >= target:
            scaled = True; scx = long_exit_fill_price(_q(target).bid, target, SLIP_BPS); bal = entry; rh = max(rh, bh)
        if scaled:
            rh = max(rh, bh); bal = runner_trail_stop(high_water_mark=rh, atr_pct=atrp, stop_atr_mult=STOP_ATR_MULT, breakeven_floor=entry, current_stop=bal, side_long=True)
        j += 1
    if exit_px is None:
        exit_px = long_exit_fill_price(_q(float(d[C].iloc[-1])).bid, float(d[C].iloc[-1]), SLIP_BPS)
    if scaled:
        return (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (exit_px - entry)) / risk
    return (exit_px - entry) / risk


def day_entries(daydf, interval, secs):
    """Return list of R for all gate fires within one session (RTH entries only)."""
    c = {x.lower(): x for x in daydf.columns}
    O, H, L, C = c["open"], c["high"], c["low"], c["close"]
    n = len(daydf)
    atr = compute_atr(daydf[H].astype(float), daydf[L].astype(float), daydf[C].astype(float))
    idx = daydf.index
    out = []
    i = 10
    while i < n - 1:
        if not _rth(idx[i + 1]):
            i += 1; continue
        ok, _, dbg = momentum_pullback_trigger(daydf.iloc[: i + 1], entry_interval=interval)
        if not ok:
            i += 1; continue
        ei = i + 1
        mid0 = float(daydf[O].iloc[ei]); entry = long_entry_fill_price(_q(mid0).ask, mid0, SLIP_BPS)
        atrp = float(atr.iloc[i]) / mid0 if (mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
        pblow = dbg.get("pullback_low"); pblow = float(pblow) if pblow else None
        brk = dbg.get("pullback_high"); brk = float(brk) if brk else None
        stop, target = _wide_stop(entry, atrp, pblow)
        if not (0 < stop < entry):
            i += 1; continue
        r = _forward(daydf, O, H, L, C, ei, entry, stop, target, brk, atrp, secs)
        if r is not None:
            out.append(r)
        i = ei + 1
    return out


# ── Universe: Ross-style small-cap movers from today's snapshot ──
snap = get_full_market_snapshot() or []
names = []
for s in snap:
    try:
        dd = s.get("day") or {}; px = dd.get("c") or dd.get("vw"); v = dd.get("v") or 0
        ch = s.get("todaysChangePerc")
        if px and 1 <= float(px) <= 20 and float(px) * float(v) > 1_000_000 and ch is not None and abs(float(ch)) >= 3:
            names.append(s["ticker"])
    except Exception:
        continue
names = names[:MAX_NAMES]
print(f"=== PAIRED 5m-vs-1m  names={len(names)}  RTH-only entries, intraday exits ===")

paired = {}  # (sym, day) -> {"5m":[r...], "1m":[r...]}
for k, sym in enumerate(names):
    for interval, period, secs in (("5m", "1mo", 300.0), ("1m", "7d", 60.0)):
        try:
            df = fetch_ohlcv_df(sym, interval=interval, period=period)
        except Exception:
            continue
        if df is None or len(df) < 20:
            continue
        for day, daydf in df.groupby(df.index.date):
            if len(daydf) < 14:
                continue
            rs = day_entries(daydf, interval, secs)
            if rs:
                paired.setdefault((sym, str(day)), {}).setdefault(interval, []).extend(rs)
    print(f"  .. {k+1}/{len(names)} {sym}", end="\r")
print(" " * 40)

# ── Aggregate (only days where BOTH intervals are present overlap; report all too) ──
def agg(tf):
    rs = [r for v in paired.values() for r in v.get(tf, [])]
    if not rs:
        return (0, 0.0, 0.0, 0)
    return (len(rs), statistics.mean(rs), sum(rs), 100 * sum(1 for r in rs if r > 0) / len(rs))

for tf in ("5m", "1m"):
    n, mean, tot, win = agg(tf)
    print(f"{tf}: entries={n:4d}  mean={mean:+.3f}R  total={tot:+.1f}R  win%={win:.0f}")

# Paired by (name, day): days where BOTH traded — the apples-to-apples diff.
both = [(k, sum(v["5m"]), sum(v["1m"])) for k, v in paired.items() if v.get("5m") and v.get("1m")]
if both:
    diffs = [r1 - r5 for _, r5, r1 in both]
    wins1 = sum(1 for d in diffs if d > 0)
    print(f"\nPAIRED (both traded) name-days={len(both)}:")
    print(f"  sum 5m={sum(r5 for _,r5,_ in both):+.1f}R   sum 1m={sum(r1 for _,_,r1 in both):+.1f}R")
    print(f"  mean(1m - 5m) per name-day = {statistics.mean(diffs):+.3f}R   1m>=5m on {wins1}/{len(both)} ({100*wins1/len(both):.0f}%)")
    top = sorted(both, key=lambda z: (z[2] - z[1]), reverse=True)
    print("  biggest 1m-edge name-days:")
    for k, r5, r1 in top[:5]:
        print(f"    {k[0]:6s} {k[1]}  5m={r5:+.2f}R  1m={r1:+.2f}R  (1m-5m={r1-r5:+.2f})")
    print("  biggest 5m-edge name-days:")
    for k, r5, r1 in top[-5:]:
        print(f"    {k[0]:6s} {k[1]}  5m={r5:+.2f}R  1m={r1:+.2f}R  (1m-5m={r1-r5:+.2f})")
else:
    print("\nno overlapping name-days")
