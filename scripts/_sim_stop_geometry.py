"""Stop-geometry study: does a TIGHT structural(+buffer) stop beat the live WIDE
vol-floored stop? Rigorous, monster-resistant:
  * 5-day intraday bars (mostly-stationary) x the FULL Ross universe (~40 names,
    a range of movers — not just today's monsters) -> reduces survivorship/outlier bias.
  * PAIRED comparison: every gate-fire entry is simulated forward under EACH stop
    mode on the SAME bars (only the stop differs) — isolates the stop's effect.
  * Robust stats: mean AND median AND winsorized-mean (R capped to +/-10R so 1-2
    monster runners can't dictate the conclusion) + win%.
  * Shake-out analysis: count entries where the TIGHT stop loses but the WIDE stop
    wins (shake-outs the tight stop causes) vs where tight wins and wide loses/smaller
    (the hit-rate gain) — the core trade-off.
All entries + exits use the SYSTEM's real functions. RTH-gated across all 5 days.
"""
from __future__ import annotations

import statistics

import pandas as pd

from app.services.trading.indicator_core import compute_atr
from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.candles import is_topping_tail
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger, breakout_failed_to_hold
from app.services.trading.momentum_neural.paper_execution import (
    breakeven_stop_after_partial, build_synthetic_quote, effective_stop_atr_pct,
    long_entry_fill_price, long_exit_fill_price, runner_trail_stop, scale_out_fraction,
    stop_target_prices, structural_or_vol_floored_atr_pct,
)
from app.services.trading.momentum_neural.universe import EQUITY_ROSS_SMALLCAP, build_equity_universe

STOP_ATR_MULT, REWARD_RISK, SCALE_FRAC = 0.60, 2.0, scale_out_fraction()
SLIP_BPS, SPREAD_BPS = 15.0, 40.0   # same for every mode -> doesn't bias the paired comparison
WINSOR = 10.0                        # cap |R| for the robust mean


def _q(mid):
    return build_synthetic_quote(mid, SPREAD_BPS)


def _rth(ts) -> bool:
    try:
        mins = ts.hour * 60 + ts.minute  # bars are UTC; RTH = 13:30-20:00 UTC
        return 13 * 60 + 30 <= mins <= 20 * 60
    except Exception:
        return True


def _forward(df, O, H, L, C, ei, entry, stop, brk, atrp):
    """Documented exits (bailout/stop/2:1-scale/breakeven/trail/topping-tail/EOD).
    Returns (pnl_r, exit_reason)."""
    n = len(df)
    if not (stop > 0 and stop < entry):
        return None, "bad_stop"
    risk = entry - stop
    target = entry + REWARD_RISK * risk
    scaled = False; bal = stop; rh = entry; scx = None
    j = ei
    while j < n:
        bh, bl, bc = float(df[H].iloc[j]), float(df[L].iloc[j]), float(df[C].iloc[j])
        held_s = (j - ei) * 300.0
        qb = _q(bc)
        if not scaled and brk and breakout_failed_to_hold(breakout_level=brk, bid=qb.bid, held_seconds=held_s, window_seconds=600.0):
            xpx = long_exit_fill_price(qb.bid, bc, SLIP_BPS)
            return SCALE_FRAC * 0 + (xpx - entry) / risk, "bailout"
        if bl <= bal:
            xpx = long_exit_fill_price(_q(bal).bid, bal, SLIP_BPS)
            r = (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (xpx - entry)) / risk if scaled else (xpx - entry) / risk
            return r, ("trail" if scaled else "stop")
        if scaled and is_topping_tail(float(df[O].iloc[j]), bh, bl, bc):
            xpx = long_exit_fill_price(qb.bid, bc, SLIP_BPS)
            return (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (xpx - entry)) / risk, "topping_tail"
        if not scaled and bh >= target:
            scaled = True; scx = long_exit_fill_price(_q(target).bid, target, SLIP_BPS); bal = entry; rh = max(rh, bh)
        if scaled:
            rh = max(rh, bh)
            bal = runner_trail_stop(high_water_mark=rh, atr_pct=atrp, stop_atr_mult=STOP_ATR_MULT, breakeven_floor=entry, current_stop=bal, side_long=True)
        j += 1
    xpx = long_exit_fill_price(_q(float(df[C].iloc[-1])).bid, float(df[C].iloc[-1]), SLIP_BPS)
    r = (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (xpx - entry)) / risk if scaled else (xpx - entry) / risk
    return r, "eod"


def _wide_stop(entry, atrp, pblow):
    em = atrp * 10_000.0
    eff = effective_stop_atr_pct(atrp, em, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
    eff, _ = structural_or_vol_floored_atr_pct(vol_floored_atr_pct=eff, structural_stop_price=pblow, entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)[0]


def _tight_stop(entry, atrp, pblow, buffer_atr):
    if not (pblow and pblow < entry):
        return None
    sp = pblow - buffer_atr * atrp * entry          # structural low minus a small ATR buffer
    eff = min(0.15, max(0.005, (entry - sp) / entry / STOP_ATR_MULT))
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)[0]


LIVE = dict(entry_interval="5m")
MODES = {"WIDE(live)": None, "tight+0.0atr": 0.0, "tight+0.25atr": 0.25, "tight+0.5atr": 0.5}
results = {m: [] for m in MODES}        # list of (pnl_r, reason)
shake = {m: 0 for m in MODES if m != "WIDE(live)"}   # tight loses & wide wins
gain = {m: 0 for m in MODES if m != "WIDE(live)"}     # tight wins & wide loses
entries = 0

names = build_equity_universe(EQUITY_ROSS_SMALLCAP)[:40]
print(f"=== STOP-GEOMETRY STUDY  names={len(names)}  5d/5m bars  RTH-gated  (paired) ===")
for sym in names:
    try:
        df = fetch_ohlcv_df(sym, interval="5m", period="5d")
        if df is None or len(df) < 30:
            continue
        c = {x.lower(): x for x in df.columns}
        O, H, L, C = c["open"], c["high"], c["low"], c["close"]
        atr = compute_atr(df[H].astype(float), df[L].astype(float), df[C].astype(float))
        idx = df.index; n = len(df)
        i = 10
        while i < n - 1:
            ok, reason, dbg = momentum_pullback_trigger(df.iloc[: i + 1], entry_interval="5m")
            if not ok or not _rth(idx[i + 1]):
                i += 1
                continue
            ei = i + 1
            mid0 = float(df[O].iloc[ei]); entry = long_entry_fill_price(_q(mid0).ask, mid0, SLIP_BPS)
            atrp = float(atr.iloc[i]) / mid0 if (mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
            pblow = dbg.get("pullback_low"); pblow = float(pblow) if pblow else None
            brk = dbg.get("pullback_high"); brk = float(brk) if brk else None
            entries += 1
            per_mode = {}
            for m, buf in MODES.items():
                sp = _wide_stop(entry, atrp, pblow) if buf is None else _tight_stop(entry, atrp, pblow, buf)
                if sp is None:
                    continue
                r, rs = _forward(df, O, H, L, C, ei, entry, sp, brk, atrp)
                if r is not None:
                    results[m].append((r, rs)); per_mode[m] = r
            wide_r = per_mode.get("WIDE(live)")
            for m in shake:
                tr = per_mode.get(m)
                if tr is not None and wide_r is not None:
                    if tr <= 0 < wide_r:
                        shake[m] += 1
                    elif wide_r <= 0 < tr:
                        gain[m] += 1
            i = ei + 1
    except Exception:
        continue


def _stats(rs):
    v = [r for r, _ in rs]
    if not v:
        return "no trades"
    w = sum(1 for x in v if x > 0)
    wins_mean = statistics.mean([x for x in v if x > 0]) if w else 0.0
    loss_mean = statistics.mean([x for x in v if x <= 0]) if (len(v) - w) else 0.0
    wz = [max(-WINSOR, min(WINSOR, x)) for x in v]
    return (f"n={len(v):3d}  net={sum(v):+7.1f}R  mean={statistics.mean(v):+5.2f}R  "
            f"median={statistics.median(v):+5.2f}R  winsor_mean={statistics.mean(wz):+5.2f}R  "
            f"win%={100*w/len(v):2.0f}  avgW={wins_mean:+.2f} avgL={loss_mean:+.2f}")


print(f"\ntotal entries (gate fires, RTH): {entries}\n")
for m in MODES:
    print(f"{m:14s} {_stats(results[m])}")
print(f"\n=== SHAKE-OUT trade-off vs WIDE (paired, same entries) ===")
for m in shake:
    print(f"{m:14s} shake-outs(tight loses, wide wins)={shake[m]:3d}   hit-gains(tight wins, wide loses)={gain[m]:3d}")
