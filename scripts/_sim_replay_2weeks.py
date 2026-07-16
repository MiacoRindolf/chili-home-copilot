"""2-WEEK day-level replay of the momentum lane (current system), 2026-05-26..06-09.

Universe per day is BACK-FILLED from the grouped-daily aggregates (every US stock's daily OHLCV
for the date) -> the Ross small-cap movers ($1-20, $vol>=1M, |change|>=5%), ranked by move
strength. 5m intraday bars per name (cached). Per-name spread is the PROXY (percentile of the
day's dollar-volume -> floor 40bps .. cap 250bps); the live spread gate is applied. Per-day
concurrency sim (<=MAX_SLOTS, freshest-firing, daily caps). Then the 2-week aggregate.

⚠️ HONEST: only 06-08 + 06-09 have REAL recorded spreads (NBBO tape); every other day uses the
PROXY, which we proved is 6-17x too TIGHT for explosive low-float names (PAVS proxy 53bps vs
real 317). So the earlier days are an OPTIMISTIC CEILING, not a realistic result. Treat the
2-week proxy total as an upper bound; the real-spread reality (06-08/09) is far thinner.
"""
from __future__ import annotations

import os
import warnings
warnings.filterwarnings("ignore")
import pandas as pd

import app.services.massive_client as mc
from app.services.trading.indicator_core import compute_atr
from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.candles import is_topping_tail
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger, breakout_failed_to_hold
from app.services.trading.momentum_neural.paper_execution import (
    build_synthetic_quote, effective_stop_atr_pct, long_entry_fill_price, long_exit_fill_price,
    runner_trail_stop, scale_out_fraction, stop_target_prices, structural_or_vol_floored_atr_pct,
)

STOP_ATR_MULT, REWARD_RISK, SCALE_FRAC = 0.60, 2.0, scale_out_fraction()
SLIP_BPS = 15.0
INTERVAL = "5m"
BASIS_USD = 22551.0
RISK_PER_TRADE_USD = BASIS_USD * 0.01
NOTIONAL_CAP_USD = BASIS_USD * 0.15
MAX_SLOTS = 10
DAILY_LOSS_CAP_USD = BASIS_USD * 0.05
GIVEBACK_FRAC = 0.5
SECS = 300.0
SPREAD_FLOOR_BPS, SPREAD_CAP_BPS = 40.0, 250.0
GATE_MOVE_FRAC = 0.5
TOP_MOVERS_PER_DAY = 45                # the most-explosive names (Ross trades the top movers)

TRADING_DAYS = [
    "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29",
    "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05",
    "2026-06-08", "2026-06-09",
]


def _q(mid, sp): return build_synthetic_quote(mid, sp)
def _rth(ts) -> bool:
    m = ts.hour * 60 + ts.minute
    return 13 * 60 + 30 <= m <= 20 * 60


def _wide_stop(entry, atrp, pblow):
    eff = effective_stop_atr_pct(atrp, atrp * 10_000.0, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
    eff, _ = structural_or_vol_floored_atr_pct(vol_floored_atr_pct=eff, structural_stop_price=pblow, entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)


def _forward(d, O, H, L, C, ei, entry, stop, target, brk, atrp, sp):
    n = len(d); risk = entry - stop
    if risk <= 0:
        return None, ei
    scaled = False; bal = stop; rh = entry; scx = None; j = ei; exit_px = None
    while j < n:
        bh, bl, bc = float(d[H].iloc[j]), float(d[L].iloc[j]), float(d[C].iloc[j])
        held = (j - ei) * SECS; qb = _q(bc, sp)
        if not scaled and brk and breakout_failed_to_hold(breakout_level=brk, bid=qb.bid, held_seconds=held, window_seconds=1800.0):
            exit_px = long_exit_fill_price(qb.bid, bc, SLIP_BPS); break
        if bl <= bal:
            exit_px = long_exit_fill_price(_q(bal, sp).bid, bal, SLIP_BPS); break
        if scaled and is_topping_tail(float(d[O].iloc[j]), bh, bl, bc):
            exit_px = long_exit_fill_price(qb.bid, bc, SLIP_BPS); break
        if not scaled and bh >= target:
            scaled = True; scx = long_exit_fill_price(_q(target, sp).bid, target, SLIP_BPS); bal = entry; rh = max(rh, bh)
        if scaled:
            rh = max(rh, bh); bal = runner_trail_stop(high_water_mark=rh, atr_pct=atrp, stop_atr_mult=STOP_ATR_MULT, breakeven_floor=entry, current_stop=bal, side_long=True)
        j += 1
    if exit_px is None:
        exit_px = long_exit_fill_price(_q(float(d[C].iloc[-1]), sp).bid, float(d[C].iloc[-1]), SLIP_BPS); j = n - 1
    if scaled:
        r = (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (exit_px - entry)) / risk
    else:
        r = (exit_px - entry) / risk
    return r, j


def _day_universe(date):
    """Top Ross movers for `date` from grouped-daily; returns [(sym, |chg|, dollar_vol)]."""
    r = mc._get(mc._base() + "/v2/aggs/grouped/locale/us/market/stocks/" + date, {"adjusted": "true"})
    res = (r or {}).get("results") or []
    cand = []
    for s in res:
        try:
            t = s.get("T"); c = s.get("c"); o = s.get("o"); v = s.get("v") or 0
            if not t or not c or not o:
                continue
            chg = abs((c - o) / o * 100.0)
            if 1 <= c <= 20 and c * v > 1_000_000 and chg >= 5:
                cand.append((t, chg, c * v))
        except Exception:
            continue
    cand.sort(key=lambda z: z[1], reverse=True)
    return cand[:TOP_MOVERS_PER_DAY]


_df_cache: dict[str, object] = {}
def _bars(sym):
    if sym not in _df_cache:
        try:
            _df_cache[sym] = fetch_ohlcv_df(sym, interval=INTERVAL, period="1mo")
        except Exception:
            _df_cache[sym] = None
    return _df_cache[sym]


def _spread_for(dvol, sorted_dv):
    if not sorted_dv or len(sorted_dv) < 2:
        return SPREAD_FLOOR_BPS
    rank = sum(1 for x in sorted_dv if x < dvol) / (len(sorted_dv) - 1)
    rank = max(0.0, min(1.0, rank))
    return SPREAD_CAP_BPS - rank * (SPREAD_CAP_BPS - SPREAD_FLOOR_BPS)


def _replay_day(date):
    uni = _day_universe(date)
    if not uni:
        return None
    sorted_dv = sorted(dv for _, _, dv in uni)
    dv_by = {t: dv for t, _, dv in uni}
    fires = []
    for sym, _, _ in uni:
        df_all = _bars(sym)
        if df_all is None or len(df_all) == 0:
            continue
        try:
            c = {x.lower(): x for x in df_all.columns}
            O, H, L, C = c["open"], c["high"], c["low"], c["close"]
            df = df_all[[t.strftime("%Y-%m-%d") == date for t in df_all.index]]
            if len(df) < 14:
                continue
            idx = df.index; n = len(df)
            atr = compute_atr(df[H].astype(float), df[L].astype(float), df[C].astype(float))
            sp = _spread_for(dv_by[sym], sorted_dv)
            i = 10
            while i < n - 1:
                if not _rth(idx[i + 1]):
                    i += 1; continue
                ok, _, dbg = momentum_pullback_trigger(df.iloc[: i + 1], entry_interval=INTERVAL)
                if not ok:
                    i += 1; continue
                ei = i + 1; mid0 = float(df[O].iloc[ei])
                atrp = float(atr.iloc[i]) / mid0 if (mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
                move_bps = atrp * 10_000.0
                if move_bps <= 0 or sp > GATE_MOVE_FRAC * move_bps:   # the live spread gate
                    i = ei + 1; continue
                entry = long_entry_fill_price(_q(mid0, sp).ask, mid0, SLIP_BPS)
                pblow = dbg.get("pullback_low"); pblow = float(pblow) if pblow else None
                brk = dbg.get("pullback_high"); brk = float(brk) if brk else None
                stop, target = _wide_stop(entry, atrp, pblow)
                if not (0 < stop < entry):
                    i += 1; continue
                r, xidx = _forward(df, O, H, L, C, ei, entry, stop, target, brk, atrp, sp)
                if r is None:
                    i += 1; continue
                dh = max(float(df[H].iloc[k]) for k in range(i + 1)); dl = min(float(df[L].iloc[k]) for k in range(i + 1))
                fresh = (mid0 - dl) / (dh - dl) if dh > dl else 0.5
                spct = (entry - stop) / entry
                _risk = min(RISK_PER_TRADE_USD, NOTIONAL_CAP_USD * spct) if spct > 0 else RISK_PER_TRADE_USD
                fires.append(dict(name=sym, ei=ei, xidx=xidx, r=r, fresh=fresh, t=idx[ei], usd=r * _risk))
                i = ei + 1
        except Exception:
            continue
    # concurrency sim
    fires.sort(key=lambda f: (f["t"], -f["fresh"]))
    active = []; taken = []; sym_open = set(); cum_usd = 0.0; peak = 0.0; halted = None
    def _close(upto):
        nonlocal cum_usd, peak, halted
        still = []
        for tr in active:
            if tr["xidx"] <= upto:
                cum_usd += tr["usd"]; peak = max(peak, cum_usd); sym_open.discard(tr["name"])
                if halted is None and cum_usd <= -DAILY_LOSS_CAP_USD: halted = "daily_loss"
                elif halted is None and peak >= DAILY_LOSS_CAP_USD and cum_usd <= peak * (1 - GIVEBACK_FRAC): halted = "giveback"
            else:
                still.append(tr)
        active[:] = still
    for f in fires:
        _close(f["ei"])
        if halted or f["name"] in sym_open or len(active) >= MAX_SLOTS:
            continue
        active.append(f); sym_open.add(f["name"]); taken.append(f)
    for tr in active:
        cum_usd += tr["usd"]
    wins = sum(1 for t in taken if t["r"] > 0)
    if os.environ.get("REPLAY_DEBUG"):
        fac = [c for c in uni if c[0] == "FAC"]
        print("  [%s] FAC in top-%d universe: %s%s" % (
            date, TOP_MOVERS_PER_DAY, bool(fac),
            (" (|chg|=%.0f%% $vol=$%.0fM)" % (fac[0][1], fac[0][2] / 1e6)) if fac else ""))
        run = 0.0
        for tr in sorted(taken, key=lambda z: z["t"]):
            run += tr["usd"]
            print("    %s %-6s %+.2fR $%+8.0f  (cum $%+.0f)" % (
                tr["t"].strftime("%H:%M"), tr["name"], tr["r"], tr["usd"], run))
    return dict(date=date, universe=len(uni), fires=len(fires), trades=len(taken), wins=wins, usd=cum_usd, halted=halted)


print("=== 2-WEEK REPLAY (proxy spread = OPTIMISTIC; real spread far thinner) %s..%s ===" % (TRADING_DAYS[0], TRADING_DAYS[-1]))
print("%-12s %4s %5s %6s %4s %10s" % ("DAY", "univ", "fires", "trades", "win", "PnL(proxy)"))
total = 0.0; n_days = 0
_only = os.environ.get("REPLAY_ONLY")
for d in TRADING_DAYS:
    if _only and d != _only:
        continue
    row = _replay_day(d)
    if row is None:
        print("%-12s   (no data / holiday)" % d); continue
    total += row["usd"]; n_days += 1
    halt = (" [halt:%s]" % row["halted"]) if row["halted"] else ""
    print("%-12s %4d %5d %6d %3d/%-2d $%+8.0f%s" % (d, row["universe"], row["fires"], row["trades"], row["wins"], row["trades"], row["usd"], halt))
print("-" * 52)
print("2-WEEK TOTAL (proxy, %d days): $%+.0f" % (n_days, total))
print()
print("⚠️ HONEST: proxy spread is 6-17x too tight for low-float names (we proved PAVS 53 vs 317).")
print("   Real spreads (06-08/09 from the tape) showed ~5/48 fires fillable, gate-protective.")
print("   So this 2-week total is an OPTIMISTIC CEILING, not a realistic/achievable result.")
