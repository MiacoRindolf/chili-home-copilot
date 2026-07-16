"""ACCURATE replay using the NBBO spread TAPE (real recorded spreads), not a proxy.

The 06-08/06-09 day-replays priced spread from a dollar-volume PROXY that was 6-17x
too tight for explosive low-float names (PAVS proxy 53bps vs the real 317bps). This
re-runs those days reading each name's REAL spread from momentum_nbbo_spread_tape
(back-filled from the live lane's recorded NBBO), so the numbers are honest.

For each armed name on the day it reports:
  * FILLABLE-at-real-spread: would the live gate (spread <= 0.5 x ATR-move) have let
    it enter at its REAL spread? (the honest "could it fill" count)
  * FORCE-FILL PnL: if you traded every fire at the REAL spread anyway (ignoring the
    gate), the realized PnL — i.e. the true cost of these wide spreads.

REPLAY_DAY=2026-06-09 (default) or 2026-06-08.
"""
from __future__ import annotations

import os
import warnings
warnings.filterwarnings("ignore")
import pandas as pd

from app.db import SessionLocal
from app.services.trading.indicator_core import compute_atr
from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.candles import is_topping_tail
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger, breakout_failed_to_hold
from app.services.trading.momentum_neural.paper_execution import (
    build_synthetic_quote, effective_stop_atr_pct, long_entry_fill_price, long_exit_fill_price,
    runner_trail_stop, scale_out_fraction, stop_target_prices, structural_or_vol_floored_atr_pct,
)
from sqlalchemy import text

STOP_ATR_MULT, REWARD_RISK, SCALE_FRAC = 0.60, 2.0, scale_out_fraction()
SLIP_BPS = 15.0
DAY = os.environ.get("REPLAY_DAY", "2026-06-09")
INTERVAL = "5m"
BASIS_USD = 22551.0
RISK_PER_TRADE_USD = BASIS_USD * 0.01
NOTIONAL_CAP_USD = BASIS_USD * 0.15
MAX_SLOTS = 10
SECS = 300.0
GATE_RATIO = 0.5          # live gate: spread must be <= ratio x expected per-bar move
GATE_FLOOR_BPS = 12.0     # live base floor (max_spread_bps_live)


def _q(mid, spread_bps):
    return build_synthetic_quote(mid, spread_bps)


def _rth(ts) -> bool:
    m = ts.hour * 60 + ts.minute
    return 13 * 60 + 30 <= m <= 20 * 60


def _wide_stop(entry, atrp, pblow):
    eff = effective_stop_atr_pct(atrp, atrp * 10_000.0, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
    eff, _ = structural_or_vol_floored_atr_pct(vol_floored_atr_pct=eff, structural_stop_price=pblow, entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)


def _forward(d, O, H, L, C, ei, entry, stop, target, brk, atrp, spread_bps):
    n = len(d); risk = entry - stop
    if risk <= 0:
        return None, ei
    scaled = False; bal = stop; rh = entry; scx = None; j = ei; exit_px = None
    while j < n:
        bh, bl, bc = float(d[H].iloc[j]), float(d[L].iloc[j]), float(d[C].iloc[j])
        held = (j - ei) * SECS; qb = _q(bc, spread_bps)
        if not scaled and brk and breakout_failed_to_hold(breakout_level=brk, bid=qb.bid, held_seconds=held, window_seconds=1800.0):
            exit_px = long_exit_fill_price(qb.bid, bc, SLIP_BPS); break
        if bl <= bal:
            exit_px = long_exit_fill_price(_q(bal, spread_bps).bid, bal, SLIP_BPS); break
        if scaled and is_topping_tail(float(d[O].iloc[j]), bh, bl, bc):
            exit_px = long_exit_fill_price(qb.bid, bc, SLIP_BPS); break
        if not scaled and bh >= target:
            scaled = True; scx = long_exit_fill_price(_q(target, spread_bps).bid, target, SLIP_BPS); bal = entry; rh = max(rh, bh)
        if scaled:
            rh = max(rh, bh); bal = runner_trail_stop(high_water_mark=rh, atr_pct=atrp, stop_atr_mult=STOP_ATR_MULT, breakeven_floor=entry, current_stop=bal, side_long=True)
        j += 1
    if exit_px is None:
        exit_px = long_exit_fill_price(_q(float(d[C].iloc[-1]), spread_bps).bid, float(d[C].iloc[-1]), SLIP_BPS)
        j = n - 1
    if scaled:
        r = (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (exit_px - entry)) / risk
    else:
        r = (exit_px - entry) / risk
    return r, j


# ── real spread per name from the TAPE (median for the day) ──────────────────
db = SessionLocal()
spread_by_name = {}
try:
    rows = db.execute(text(
        "SELECT symbol, percentile_cont(0.5) WITHIN GROUP (ORDER BY spread_bps) AS med "
        "FROM momentum_nbbo_spread_tape WHERE observed_at::date = :d AND spread_bps IS NOT NULL "
        "GROUP BY symbol"
    ), {"d": DAY}).all()
    spread_by_name = {str(s).strip().upper(): float(m) for s, m in rows if m is not None}
finally:
    db.close()

names = sorted(spread_by_name.keys())
print(f"=== REAL-SPREAD replay {DAY} {INTERVAL} (spreads from the NBBO tape, not a proxy) ===")
print(f"universe = {len(names)} names the lane ARMED that day | median real spread = "
      f"{sorted(spread_by_name.values())[len(spread_by_name)//2]:.0f}bps\n")

fires = []
for sym in names:
    real_spread = spread_by_name[sym]
    try:
        df_all = fetch_ohlcv_df(sym, interval=INTERVAL, period="1mo")
        if df_all is None or len(df_all) == 0:
            continue
        c = {x.lower(): x for x in df_all.columns}
        O, H, L, C = c["open"], c["high"], c["low"], c["close"]
        df = df_all[[t.strftime("%Y-%m-%d") == DAY for t in df_all.index]]
        if len(df) < 14:
            continue
        idx = df.index; n = len(df)
        atr = compute_atr(df[H].astype(float), df[L].astype(float), df[C].astype(float))
        i = 10
        while i < n - 1:
            if not _rth(idx[i + 1]):
                i += 1; continue
            ok, _, dbg = momentum_pullback_trigger(df.iloc[: i + 1], entry_interval=INTERVAL)
            if not ok:
                i += 1; continue
            ei = i + 1
            mid0 = float(df[O].iloc[ei])
            atrp = float(atr.iloc[i]) / mid0 if (mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
            move_bps = atrp * 10_000.0
            gate = max(GATE_FLOOR_BPS, GATE_RATIO * move_bps)
            fillable = real_spread <= gate                 # would the LIVE gate let it in at the real spread?
            entry = long_entry_fill_price(_q(mid0, real_spread).ask, mid0, SLIP_BPS)
            pblow = dbg.get("pullback_low"); pblow = float(pblow) if pblow else None
            brk = dbg.get("pullback_high"); brk = float(brk) if brk else None
            stop, target = _wide_stop(entry, atrp, pblow)
            if not (0 < stop < entry):
                i += 1; continue
            r, xidx = _forward(df, O, H, L, C, ei, entry, stop, target, brk, atrp, real_spread)
            if r is None:
                i += 1; continue
            dh = max(float(df[H].iloc[k]) for k in range(i + 1)); dl = min(float(df[L].iloc[k]) for k in range(i + 1))
            fresh = (mid0 - dl) / (dh - dl) if dh > dl else 0.5
            sp = (entry - stop) / entry
            _risk = min(RISK_PER_TRADE_USD, NOTIONAL_CAP_USD * sp) if sp > 0 else RISK_PER_TRADE_USD
            fires.append(dict(name=sym, ei=ei, xidx=xidx, r=r, fresh=fresh, t=idx[ei], usd=r * _risk,
                              spread=real_spread, move=move_bps, gate=gate, fillable=fillable))
            i = ei + 1
    except Exception:
        continue

n_fill = sum(1 for f in fires if f["fillable"])
print(f"total gate-fires across the universe = {len(fires)}  |  FILLABLE at REAL spread (spread<=0.5x move) = {n_fill}")
print(f"(this is the honest count: at real spreads, how many setups could actually enter)\n")

# Concurrency sim over the FILLABLE fires (the realistic set) at REAL spreads.
def _run(fireset, label):
    fireset = sorted(fireset, key=lambda f: (f["t"], -f["fresh"]))
    active = []; taken = []; sym_open = set(); cum_usd = 0.0
    def _close(upto):
        nonlocal cum_usd
        still = []
        for tr in active:
            if tr["xidx"] <= upto:
                cum_usd += tr["usd"]; sym_open.discard(tr["name"])
            else:
                still.append(tr)
        active[:] = still
    for f in fireset:
        _close(f["ei"])
        if f["name"] in sym_open or len(active) >= MAX_SLOTS:
            continue
        active.append(f); sym_open.add(f["name"]); taken.append(f)
    for tr in active:
        cum_usd += tr["usd"]
    wins = sum(1 for t in taken if t["r"] > 0)
    print(f"--- {label}: trades={len(taken)} win={wins}/{len(taken)} PnL=${cum_usd:+.0f} ---")
    for t in sorted(taken, key=lambda z: z["t"])[:14]:
        print(f"  {t['t'].strftime('%H:%M')} {t['name']:6s} sprd={t['spread']:>4.0f}bps move={t['move']:>4.0f} gate={t['gate']:>4.0f} {t['r']:+.2f}R ${t['usd']:+.0f}")
    return cum_usd

print(">>> HONEST result — only the fires that PASS the gate at REAL spreads:")
usd_fillable = _run([f for f in fires if f["fillable"]], "fillable-at-real-spread")
print("\n>>> HYPOTHETICAL — if you FORCE-traded every fire at its REAL spread (ignoring the gate):")
usd_force = _run(fires, "force-fill-all-at-real-spread")
print(f"\n=== {DAY} SUMMARY ===")
print(f"fillable at real spread : ${usd_fillable:+.0f}  ({n_fill}/{len(fires)} fires could enter)")
print(f"force-fill all at real  : ${usd_force:+.0f}  (the true cost of trading these wide spreads)")
