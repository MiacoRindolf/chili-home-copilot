"""Would CHILI's CURRENT live gate have caught Ross's actual NPT trade (2026-06-08)?

Ross: found NPT on his halt scanner (~119M shares), bought the first pullback at
$8.29, sold ~$10 in <1 min (+$416 / +20% on a $2k all-in). INHD (+5,465%) he SKIPPED.

This replays CHILI's SHARED live entry decision (momentum_pullback_trigger -> bundles
volume-spike + retest + sustained-vol + break-candle + VWAP-hold + MACD + runaway, all
at live settings) bar-by-bar on NPT's 5m tape, then simulates the live wide-stop / 2:1 /
documented exits. Faithful: same trigger + same stop chain + same forward exits as live.
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")
import statistics
import pandas as pd

from app.services.trading.indicator_core import compute_atr
from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.candles import is_topping_tail
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger, breakout_failed_to_hold
from app.services.trading.momentum_neural.paper_execution import (
    build_synthetic_quote, effective_stop_atr_pct, long_entry_fill_price, long_exit_fill_price,
    runner_trail_stop, scale_out_fraction, stop_target_prices, structural_or_vol_floored_atr_pct,
)

STOP_ATR_MULT, REWARD_RISK, SCALE_FRAC = 0.60, 2.0, scale_out_fraction()
SLIP_BPS, SPREAD_BPS = 15.0, 40.0
SYM, DAY = "NPT", "2026-06-08"
ROSS_ENTRY = 8.29


def _q(mid):
    return build_synthetic_quote(mid, SPREAD_BPS)


def _wide_stop(entry, atrp, pblow):
    eff = effective_stop_atr_pct(atrp, atrp * 10_000.0, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
    eff, _ = structural_or_vol_floored_atr_pct(vol_floored_atr_pct=eff, structural_stop_price=pblow, entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)


def _forward(df, O, H, L, C, ei, entry, stop, target, brk, atrp, secs):
    n = len(df); risk = entry - stop
    if risk <= 0:
        return None, "bad_risk", None
    scaled = False; bal = stop; rh = entry; scx = None; j = ei; exit_px = None; why = "eod"
    while j < n:
        bh, bl, bc = float(df[H].iloc[j]), float(df[L].iloc[j]), float(df[C].iloc[j])
        held = (j - ei) * secs; qb = _q(bc)
        if not scaled and brk and breakout_failed_to_hold(breakout_level=brk, bid=qb.bid, held_seconds=held, window_seconds=1800.0):
            exit_px = long_exit_fill_price(qb.bid, bc, SLIP_BPS); why = "break_fail"; break
        if bl <= bal:
            exit_px = long_exit_fill_price(_q(bal).bid, bal, SLIP_BPS); why = "stop" if not scaled else "trail_stop"; break
        if scaled and is_topping_tail(float(df[O].iloc[j]), bh, bl, bc):
            exit_px = long_exit_fill_price(qb.bid, bc, SLIP_BPS); why = "topping_tail"; break
        if not scaled and bh >= target:
            scaled = True; scx = long_exit_fill_price(_q(target).bid, target, SLIP_BPS); bal = entry; rh = max(rh, bh)
        if scaled:
            rh = max(rh, bh); bal = runner_trail_stop(high_water_mark=rh, atr_pct=atrp, stop_atr_mult=STOP_ATR_MULT, breakeven_floor=entry, current_stop=bal, side_long=True)
        j += 1
    if exit_px is None:
        exit_px = long_exit_fill_price(_q(float(df[C].iloc[-1])).bid, float(df[C].iloc[-1]), SLIP_BPS)
    if scaled:
        r = (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (exit_px - entry)) / risk
    else:
        r = (exit_px - entry) / risk
    return r, why, exit_px


def run(interval):
    secs = 300.0 if interval == "5m" else 60.0
    df_all = fetch_ohlcv_df(SYM, interval=interval, period="5d")
    if df_all is None or len(df_all) == 0:
        print(f"  [{interval}] no data"); return
    c = {x.lower(): x for x in df_all.columns}
    O, H, L, C, V = c["open"], c["high"], c["low"], c["close"], c["volume"]
    df = df_all[[t.strftime("%Y-%m-%d") == DAY for t in df_all.index]]
    if len(df) < 14:
        print(f"  [{interval}] only {len(df)} bars on {DAY}"); return
    idx = df.index; n = len(df)
    atr = compute_atr(df[H].astype(float), df[L].astype(float), df[C].astype(float))
    dlo = float(df[L].astype(float).min()); dhi = float(df[H].astype(float).max())
    print(f"  [{interval}] {DAY}: {n} bars  low=${dlo:.2f} high=${dhi:.2f}  (Ross entry ${ROSS_ENTRY})")
    fires = []
    i = 12
    while i < n - 1:
        ok, reason, dbg = momentum_pullback_trigger(df.iloc[: i + 1], entry_interval=interval)
        if not ok:
            i += 1; continue
        ei = i + 1
        mid0 = float(df[O].iloc[ei]); entry = long_entry_fill_price(_q(mid0).ask, mid0, SLIP_BPS)
        atrp = float(atr.iloc[i]) / mid0 if (mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
        pblow = dbg.get("pullback_low"); pblow = float(pblow) if pblow else None
        brk = dbg.get("pullback_high"); brk = float(brk) if brk else None
        stop, target = _wide_stop(entry, atrp, pblow)
        r, why, xpx = _forward(df, O, H, L, C, ei, entry, stop, target, brk, atrp, secs)
        if r is None:
            i += 1; continue
        near = "  <<< NEAR ROSS" if abs(entry - ROSS_ENTRY) / ROSS_ENTRY <= 0.12 else ""
        runaway = "runaway" if dbg.get("runaway") else "pullback"
        print(f"    FIRE @ {idx[ei].strftime('%H:%M')}UTC  entry=${entry:6.2f} stop=${stop:6.2f} tgt=${target:6.2f}  {runaway:8s}  -> {r:+.2f}R via {why}  (exit ${xpx:.2f}){near}")
        fires.append(r)
        i = ei + 1
    if not fires:
        print(f"    NO FIRES on {DAY}.")
    else:
        wins = sum(1 for r in fires if r > 0)
        print(f"    => {len(fires)} fires  mean={statistics.mean(fires):+.2f}R  total={sum(fires):+.2f}R  win={wins}/{len(fires)}")


print(f"=== NPT replay vs CHILI live gate  (scale={SCALE_FRAC}, stop_mult={STOP_ATR_MULT}, RR={REWARD_RISK}) ===")
for iv in ("5m", "1m"):
    run(iv)
