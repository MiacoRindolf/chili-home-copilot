"""OUT-OF-SAMPLE validation of the selection-edge filter before shipping.

The in-sample study found a combined filter (rvol>=3 & atr>=0.015 & vwap_dist>=0.02 &
hour<13) = +0.49R vs -0.13R baseline. 4 tuned thresholds + ~49 entries = OVERFIT RISK.
This validates RIGOROUSLY before any ship:

  1. Gather gate-fire entries across a broad low-priced universe (15m/60d), each with
     causal entry features (rvol/atr/vwap_dist/hour/freshness/runaway) + outcome R
     (faithful live gate + wide stop + documented exits) + the entry DATE.
  2. CHRONOLOGICAL split: earlier half = TRAIN, later half = TEST (mimics going-forward;
     no peeking — the realistic OOS).
  3. Derive the filter cutoffs from TRAIN ONLY:
       * ADAPTIVE (no magic number): rvol >= train-median, atr >= train-median,
         vwap_dist >= 0, hour < 13  (percentile-within-batch — the live-shippable form).
       * also test the ORIGINAL FIXED thresholds for comparison (overfit check).
  4. Apply the FROZEN cutoffs to the held-out TEST set; compare filtered vs baseline.
     Edge that SURVIVES OOS on TEST -> real, ship the adaptive form. Edge that collapses
     -> overfit, do NOT ship.
  5. Univariate persistence: TEST terciles per feature (more robust than the combo).
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")
import statistics
import pandas as pd

from app.services.trading.indicator_core import compute_all_from_df, compute_atr
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
    if risk <= 0:
        return None
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


# ── Universe ──
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
names = sorted(set(names))[:140]

rows = []
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
            if r is None:
                i += 1
                continue
            day_mask = [t.date() == idx[i].date() for t in idx[: i + 1]]
            day_h = max(float(df[H].iloc[k]) for k in range(i + 1) if day_mask[k])
            day_l = min(float(df[L].iloc[k]) for k in range(i + 1) if day_mask[k])
            pir = (mid0 - day_l) / (day_h - day_l) if day_h > day_l else 0.5
            rows.append(dict(
                r=r, date=idx[ei].date(),
                rvol=float(vr[i]) if i < len(vr) and vr[i] is not None else None,
                atr_pct=atrp,
                vwap_dist=((mid0 - float(vwap[i])) / float(vwap[i]) if (i < len(vwap) and vwap[i]) else None),
                hour_et=(idx[ei].hour - 4),
                freshness=pir,
                runaway=1 if dbg.get("runaway") else 0,
            ))
            i = ei + 1
    except Exception:
        continue

print(f"=== SELECTION-EDGE OOS VALIDATION  names={len(names)} {INTERVAL}/60d  entries={len(rows)} ===")
if len(rows) < 60:
    print(f"too few entries ({len(rows)}) for a credible split"); raise SystemExit

# drop rows missing any feature used by the filter
rows = [x for x in rows if x["rvol"] is not None and x["vwap_dist"] is not None]
rows.sort(key=lambda x: x["date"])
mid = len(rows) // 2
train, test = rows[:mid], rows[mid:]
print(f"chronological split: TRAIN={len(train)} (<= {train[-1]['date']})  |  TEST={len(test)} (>= {test[0]['date']})\n")


def _stats(sub):
    if not sub:
        return "n=0"
    rr = [x["r"] for x in sub]
    return f"n={len(rr):3d}  mean={statistics.mean(rr):+.3f}R  median={statistics.median(rr):+.2f}R  win%={100*sum(1 for r in rr if r>0)/len(rr):.0f}"


# cutoffs derived from TRAIN ONLY
tr_rvol = statistics.median([x["rvol"] for x in train])
tr_atr = statistics.median([x["atr_pct"] for x in train])
print(f"TRAIN-derived ADAPTIVE cutoffs: rvol>={tr_rvol:.2f} (median), atr>={tr_atr:.4f} (median), vwap_dist>=0, hour<13\n")

adaptive = lambda x: (x["rvol"] >= tr_rvol and x["atr_pct"] >= tr_atr and x["vwap_dist"] >= 0 and x["hour_et"] < 13)
fixed = lambda x: ((x["rvol"] or 0) >= 3 and x["atr_pct"] >= 0.015 and (x["vwap_dist"] or -1) >= 0.02 and x["hour_et"] < 13)

print("== TRAIN (in-sample, sanity) ==")
print(f"  baseline      : {_stats(train)}")
print(f"  ADAPTIVE filt : {_stats([x for x in train if adaptive(x)])}")
print(f"  FIXED filt    : {_stats([x for x in train if fixed(x)])}\n")

print("== TEST (HELD-OUT - the verdict) ==")
print(f"  baseline      : {_stats(test)}")
tf_a = [x for x in test if adaptive(x)]
tf_f = [x for x in test if fixed(x)]
print(f"  ADAPTIVE filt : {_stats(tf_a)}   ({100*len(tf_a)/len(test):.0f}% kept)")
print(f"  FIXED filt    : {_stats(tf_f)}   ({100*len(tf_f)/len(test):.0f}% kept)\n")

# univariate persistence on TEST (terciles)
def tercile(feat):
    vals = [(x[feat], x["r"]) for x in test if x.get(feat) is not None]
    if len(vals) < 30:
        print(f"  {feat:10s}: too few"); return
    vals.sort(key=lambda z: z[0]); k = len(vals) // 3
    out = []
    for nm, b in (("low", vals[:k]), ("mid", vals[k:2*k]), ("high", vals[2*k:])):
        rr = [r for _, r in b]
        out.append(f"{nm} mean={statistics.mean(rr):+.2f}R win%={100*sum(1 for r in rr if r>0)/len(rr):.0f}")
    print(f"  {feat:10s}: " + "  |  ".join(out))

print("== TEST univariate persistence (terciles) ==")
for f in ("rvol", "atr_pct", "vwap_dist", "hour_et"):
    tercile(f)

# verdict
ba = statistics.mean([x["r"] for x in test])
fa = statistics.mean([x["r"] for x in tf_a]) if tf_a else None
print("\n=== VERDICT ===")
if fa is None or len(tf_a) < 10:
    print(f"INCONCLUSIVE: adaptive filter kept only {len(tf_a)} TEST entries (too few to trust).")
elif fa > ba and fa > 0:
    print(f"HOLDS OOS: adaptive filter TEST mean {fa:+.3f}R > baseline {ba:+.3f}R and positive -> shippable (adaptive form).")
else:
    print(f"DOES NOT HOLD: adaptive filter TEST mean {fa:+.3f}R vs baseline {ba:+.3f}R -> likely overfit; do NOT ship.")
