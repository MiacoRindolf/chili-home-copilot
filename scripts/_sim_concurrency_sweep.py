"""Concurrency sweep: how much do MORE slots capture on a busy day (06-08)?

Gathers the universe's gate fires ONCE (ranked by mover-strength, like live viability),
then runs the day-level concurrency assignment at N = 5,8,10,12,16,20 slots. Shows the
marginal value + where diminishing returns kick in -> calibrates the adaptive cap.

Validation honesty: this is ONE busy day. On a QUIET day there are few fires, the cap is
rarely hit, so higher N is a NO-OP (identical result) -> the change can only help on busy
days and is neutral otherwise. The risk of higher N is more SIMULTANEOUS open positions;
that is what the equity-relative cap (N = equity*frac/per_trade_risk) bounds.
"""
from __future__ import annotations

import warnings
warnings.filterwarnings("ignore")
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
DAY, INTERVAL, RISK_USD, SECS = "2026-06-08", "5m", 50.0, 300.0
DAILY_LOSS_CAP_R, GIVEBACK_FRAC = 250.0 / RISK_USD, 0.5


def _q(mid):
    return build_synthetic_quote(mid, SPREAD_BPS)


def _rth(ts):
    m = ts.hour * 60 + ts.minute
    return 13 * 60 + 30 <= m <= 20 * 60


def _wide_stop(entry, atrp, pblow):
    eff = effective_stop_atr_pct(atrp, atrp * 10_000.0, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
    eff, _ = structural_or_vol_floored_atr_pct(vol_floored_atr_pct=eff, structural_stop_price=pblow, entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)


def _forward(d, O, H, L, C, ei, entry, stop, target, brk, atrp):
    n = len(d); risk = entry - stop
    if risk <= 0:
        return None, ei
    scaled = False; bal = stop; rh = entry; scx = None; j = ei; exit_px = None
    while j < n:
        bh, bl, bc = float(d[H].iloc[j]), float(d[L].iloc[j]), float(d[C].iloc[j])
        held = (j - ei) * SECS; qb = _q(bc)
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
        exit_px = long_exit_fill_price(_q(float(d[C].iloc[-1])).bid, float(d[C].iloc[-1]), SLIP_BPS); j = n - 1
    if scaled:
        return (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (exit_px - entry)) / risk, j
    return (exit_px - entry) / risk, j


snap = get_full_market_snapshot() or []
cand = []
for s in snap:
    try:
        dd = s.get("day") or {}; px = dd.get("c") or dd.get("vw"); v = dd.get("v") or 0
        ch = s.get("todaysChangePerc")
        if px and 1 <= float(px) <= 20 and float(px) * float(v) > 1_000_000 and ch is not None and abs(float(ch)) >= 5:
            cand.append((s["ticker"], abs(float(ch))))
    except Exception:
        continue
cand.sort(key=lambda z: z[1], reverse=True)
names = [t for t, _ in cand[:100]]

fires = []
for sym in names:
    try:
        df_all = fetch_ohlcv_df(sym, interval=INTERVAL, period="1mo")
        if df_all is None or len(df_all) == 0:
            continue
        c = {x.lower(): x for x in df_all.columns}
        O, H, L, C = c["open"], c["high"], c["low"], c["close"]
        df = df_all[[t.strftime("%Y-%m-%d") == DAY for t in df_all.index]]
        if len(df) < 14:
            continue
        idx = df.index; n = len(df); atr = compute_atr(df[H].astype(float), df[L].astype(float), df[C].astype(float)); i = 10
        while i < n - 1:
            if not _rth(idx[i + 1]):
                i += 1; continue
            ok, _, dbg = momentum_pullback_trigger(df.iloc[: i + 1], entry_interval=INTERVAL)
            if not ok:
                i += 1; continue
            ei = i + 1; mid0 = float(df[O].iloc[ei]); entry = long_entry_fill_price(_q(mid0).ask, mid0, SLIP_BPS)
            atrp = float(atr.iloc[i]) / mid0 if (mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
            pblow = dbg.get("pullback_low"); pblow = float(pblow) if pblow else None
            brk = dbg.get("pullback_high"); brk = float(brk) if brk else None
            stop, target = _wide_stop(entry, atrp, pblow)
            if not (0 < stop < entry):
                i += 1; continue
            r, xidx = _forward(df, O, H, L, C, ei, entry, stop, target, brk, atrp)
            if r is None:
                i += 1; continue
            dh = max(float(df[H].iloc[k]) for k in range(i + 1)); dl = min(float(df[L].iloc[k]) for k in range(i + 1))
            fresh = (mid0 - dl) / (dh - dl) if dh > dl else 0.5
            fires.append(dict(name=sym, ei=ei, xidx=xidx, r=r, fresh=fresh, t=idx[ei]))
            i = ei + 1
    except Exception:
        continue


def run_concurrency(N):
    fs = sorted(fires, key=lambda f: (f["t"], -f["fresh"]))
    active = []; sym_open = set(); cum_r = 0.0; peak_r = 0.0; halted = None; taken = 0; full_skips_r = 0.0; full_skips = 0

    def close_due(upto):
        nonlocal cum_r, peak_r, halted
        keep = []
        for tr in active:
            if tr["xidx"] <= upto:
                cum_r += tr["r"]; peak_r = max(peak_r, cum_r); sym_open.discard(tr["name"])
                if halted is None and cum_r <= -DAILY_LOSS_CAP_R:
                    halted = "daily_loss_cap"
                elif halted is None and peak_r >= DAILY_LOSS_CAP_R and cum_r <= peak_r * (1 - GIVEBACK_FRAC):
                    halted = "profit_giveback"
            else:
                keep.append(tr)
        active[:] = keep

    for f in fs:
        close_due(f["ei"])
        if halted or f["name"] in sym_open:
            continue
        if len(active) >= N:
            full_skips += 1; full_skips_r += max(0.0, f["r"]); continue
        active.append(f); sym_open.add(f["name"]); taken += 1
    for tr in active:
        cum_r += tr["r"]; peak_r = max(peak_r, cum_r)
    return taken, cum_r, full_skips, full_skips_r


print(f"=== CONCURRENCY SWEEP  {DAY}  universe={len(names)}  fires={len(fires)} ===")
print(f"{'N':>3} | {'trades':>6} | {'total R':>8} | {'total $':>8} | {'slot-full skips':>15} | {'missed-winner $ left':>20}")
prev = None
for N in (5, 8, 10, 12, 16, 20):
    taken, cum_r, fs_n, fs_r = run_concurrency(N)
    delta = "" if prev is None else f"  (+${(cum_r-prev)*RISK_USD:+.0f} vs prev N)"
    print(f"{N:>3} | {taken:>6} | {cum_r:>+7.2f}R | {cum_r*RISK_USD:>+7.0f} | {fs_n:>15} | {fs_r*RISK_USD:>19.0f}{delta}")
    prev = cum_r
