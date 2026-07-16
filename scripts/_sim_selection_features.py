"""Selection-edge research: which ENTRY FEATURES separate WINNERS from LOSERS?

The lane's edge is thin (+0.13R/trade, negative median — most trades small losers,
few big winners). If a causal entry feature (known AT entry, no lookahead) predicts
the outcome, filtering on it lifts the edge. Data-driven, overfit-resistant:
  * 15m bars x ~20 days x a BROAD low-priced name set (more entries than 5m's 4d,
    less selection bias than the top-50) -> a few hundred entries.
  * Per gate-fire entry: record features + the outcome R (validated WIDE stop +
    documented exits — same as the live lane).
  * UNIVARIATE analysis first (interpretable, hard to overfit): each feature binned
    into terciles -> win% + mean R per bin. A feature that monotonically separates
    is a candidate selection filter (then validate paired on 5m before shipping).
NOTE: 15m study (vs live 5m) — the SELECTION signals (RVOL/freshness/gap/time-of-
day/MACD) are timeframe-agnostic; findings transfer, then get 5m-confirmed.
"""
from __future__ import annotations

import statistics

import pandas as pd

from app.services.trading.indicator_core import compute_all_from_df, compute_atr
from app.services.trading.market_data import fetch_ohlcv_df
from app.services.massive_client import get_full_market_snapshot
from app.services.trading.momentum_neural.candles import is_topping_tail
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger, breakout_failed_to_hold
from app.services.trading.momentum_neural.paper_execution import (
    breakeven_stop_after_partial, build_synthetic_quote, effective_stop_atr_pct,
    long_entry_fill_price, long_exit_fill_price, runner_trail_stop, scale_out_fraction,
    stop_target_prices, structural_or_vol_floored_atr_pct,
)

STOP_ATR_MULT, REWARD_RISK, SCALE_FRAC = 0.60, 2.0, scale_out_fraction()
SLIP_BPS, SPREAD_BPS = 15.0, 40.0
INTERVAL = "15m"


def _q(mid):
    return build_synthetic_quote(mid, SPREAD_BPS)


def _rth(ts) -> bool:
    m = ts.hour * 60 + ts.minute
    return 13 * 60 + 30 <= m <= 20 * 60


def _wide_stop(entry, atrp, pblow):
    eff = effective_stop_atr_pct(atrp, atrp * 10_000.0, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
    eff, _ = structural_or_vol_floored_atr_pct(vol_floored_atr_pct=eff, structural_stop_price=pblow, entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)


def _forward(df, O, H, L, C, ei, entry, stop, target, brk, atrp):
    n = len(df); risk = entry - stop
    scaled = False; bal = stop; rh = entry; scx = None; j = ei
    while j < n:
        bh, bl, bc = float(df[H].iloc[j]), float(df[L].iloc[j]), float(df[C].iloc[j])
        held = (j - ei) * 900.0; qb = _q(bc)
        if not scaled and brk and breakout_failed_to_hold(breakout_level=brk, bid=qb.bid, held_seconds=held, window_seconds=1800.0):
            return (long_exit_fill_price(qb.bid, bc, SLIP_BPS) - entry) / risk
        if bl <= bal:
            xpx = long_exit_fill_price(_q(bal).bid, bal, SLIP_BPS)
            return (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (xpx - entry)) / risk if scaled else (xpx - entry) / risk
        if scaled and is_topping_tail(float(df[O].iloc[j]), bh, bl, bc):
            xpx = long_exit_fill_price(qb.bid, bc, SLIP_BPS)
            return (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (xpx - entry)) / risk
        if not scaled and bh >= target:
            scaled = True; scx = long_exit_fill_price(_q(target).bid, target, SLIP_BPS); bal = entry; rh = max(rh, bh)
        if scaled:
            rh = max(rh, bh); bal = runner_trail_stop(high_water_mark=rh, atr_pct=atrp, stop_atr_mult=STOP_ATR_MULT, breakeven_floor=entry, current_stop=bal, side_long=True)
        j += 1
    xpx = long_exit_fill_price(_q(float(df[C].iloc[-1])).bid, float(df[C].iloc[-1]), SLIP_BPS)
    return (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (xpx - entry)) / risk if scaled else (xpx - entry) / risk


# Broad low-priced name set from the snapshot (more names + less selection bias).
snap = get_full_market_snapshot() or []
names = []
for s in snap:
    try:
        d = s.get("day") or {}; px = d.get("c") or d.get("vw"); v = d.get("v") or 0
        ch = s.get("todaysChangePerc")
        if px and 1 <= float(px) <= 20 and float(px) * float(v) > 1_000_000 and ch is not None and abs(float(ch)) >= 3:
            names.append(s["ticker"])
    except Exception:
        continue
names = names[:120]

rows = []   # each: dict(feature..., r)
for sym in names:
    try:
        df = fetch_ohlcv_df(sym, interval=INTERVAL, period="60d")
        if df is None or len(df) < 40:
            continue
        c = {x.lower(): x for x in df.columns}
        O, H, L, C, V = c["open"], c["high"], c["low"], c["close"], c["volume"]
        arr = compute_all_from_df(df, needed={"vwap", "macd_hist", "volume_ratio"})
        vwap = arr.get("vwap") or []; mh = arr.get("macd_hist") or []; vr = arr.get("volume_ratio") or []
        atr = compute_atr(df[H].astype(float), df[L].astype(float), df[C].astype(float))
        idx = df.index; n = len(df); i = 12
        while i < n - 1:
            ok, reason, dbg = momentum_pullback_trigger(df.iloc[: i + 1], entry_interval=INTERVAL)
            if not ok or not _rth(idx[i + 1]):
                i += 1
                continue
            ei = i + 1
            mid0 = float(df[O].iloc[ei]); entry = long_entry_fill_price(_q(mid0).ask, mid0, SLIP_BPS)
            atrp = float(atr.iloc[i]) / mid0 if (mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
            pblow = dbg.get("pullback_low"); pblow = float(pblow) if pblow else None
            brk = dbg.get("pullback_high"); brk = float(brk) if brk else None
            stop, target = _wide_stop(entry, atrp, pblow)
            if not (stop > 0 and stop < entry):
                i += 1
                continue
            r = _forward(df, O, H, L, C, ei, entry, stop, target, brk, atrp)
            # day-level position-in-range (freshness) from this bar's session so far
            day_mask = [t.date() == idx[i].date() for t in idx[: i + 1]]
            day_h = max(float(df[H].iloc[k]) for k in range(i + 1) if day_mask[k])
            day_l = min(float(df[L].iloc[k]) for k in range(i + 1) if day_mask[k])
            pir = (mid0 - day_l) / (day_h - day_l) if day_h > day_l else 0.5
            rows.append(dict(
                r=r,
                rvol=float(vr[i]) if i < len(vr) and vr[i] is not None else None,
                atr_pct=atrp,
                freshness=pir,
                hour_et=(idx[ei].hour - 4),                       # UTC->ET
                macd_hist=float(mh[i]) if i < len(mh) and mh[i] is not None else None,
                vwap_dist=((mid0 - float(vwap[i])) / float(vwap[i]) if (i < len(vwap) and vwap[i]) else None),
                runaway=1 if dbg.get("runaway") else 0,
            ))
            i = ei + 1
    except Exception:
        continue

print(f"=== SELECTION-FEATURE STUDY  names={len(names)}  {INTERVAL}/60d  entries={len(rows)} ===")
if not rows:
    print("no entries"); raise SystemExit
allr = [x["r"] for x in rows]
print(f"baseline: mean={statistics.mean(allr):+.3f}R median={statistics.median(allr):+.2f}R win%={100*sum(1 for x in allr if x>0)/len(allr):.0f}\n")


def tercile_report(feat):
    vals = [(x[feat], x["r"]) for x in rows if x.get(feat) is not None]
    if len(vals) < 30:
        print(f"{feat:11s}: too few ({len(vals)})"); return
    vals.sort(key=lambda z: z[0])
    k = len(vals) // 3
    bins = [("low", vals[:k]), ("mid", vals[k:2 * k]), ("high", vals[2 * k:])]
    out = []
    for nm, b in bins:
        rr = [r for _, r in b]; fv = [f for f, _ in b]
        out.append(f"{nm}[{min(fv):.2f}..{max(fv):.2f}] n={len(rr)} mean={statistics.mean(rr):+.2f}R win%={100*sum(1 for r in rr if r>0)/len(rr):.0f}")
    print(f"{feat:11s}: " + "  |  ".join(out))


for f in ("rvol", "freshness", "atr_pct", "macd_hist", "vwap_dist", "hour_et"):
    tercile_report(f)
# runaway is binary
rw = [x["r"] for x in rows if x["runaway"] == 1]; nrw = [x["r"] for x in rows if x["runaway"] == 0]
if rw:
    print(f"{'runaway':11s}: yes n={len(rw)} mean={statistics.mean(rw):+.2f}R win%={100*sum(1 for r in rw if r>0)/len(rw):.0f}  |  no n={len(nrw)} mean={statistics.mean(nrw):+.2f}R")


# ── combined filters (quantify the lift + entries retained = overfit check) ──
def _filt(name, pred):
    sub = [x["r"] for x in rows if pred(x)]
    if not sub:
        print(f"{name:34s}: 0 entries"); return
    import statistics as _st
    print(f"{name:34s}: n={len(sub):3d} ({100*len(sub)/len(rows):2.0f}% kept)  mean={_st.mean(sub):+.2f}R  median={_st.median(sub):+.2f}R  win%={100*sum(1 for r in sub if r>0)/len(sub):.0f}")
print("=== COMBINED FILTERS vs baseline (mean -0.13R, win 31%) ===")
_filt("runaway only", lambda x: x["runaway"]==1)
_filt("rvol>=3 & atr>=0.015 & hour<13", lambda x: (x.get("rvol") or 0)>=3 and x["atr_pct"]>=0.015 and x["hour_et"]<13)
_filt("rvol>=3 & not-afternoon(h<13)", lambda x: (x.get("rvol") or 0)>=3 and x["hour_et"]<13)
_filt("runaway OR (rvol>=3 & atr>=0.015)", lambda x: x["runaway"]==1 or ((x.get("rvol") or 0)>=3 and x["atr_pct"]>=0.015))
_filt("rvol>=3 & atr>=0.015 & vwapd>=0.02 & h<13", lambda x: (x.get("rvol") or 0)>=3 and x["atr_pct"]>=0.015 and (x.get("vwap_dist") or -1)>=0.02 and x["hour_et"]<13)
_filt("NOT-afternoon only (h<13)", lambda x: x["hour_et"]<13)
