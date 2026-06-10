"""A/B replay: BASELINE vs LIQUIDITY-BIASED Ross selection, on previous days.

Question (operator, 2026-06-10): the live lane fills its 10-slot cap with the most
EXPLOSIVE (small-float, high-RVOL) movers — many of which are so wide-spread they only
ever get WATCHED (spread-gated), never filled. Does adding a tradeable-liquidity pillar
(dollar turnover -> tighter spread -> fillable) get MORE fills without giving up PnL?

Method — identical universe + fill model for both arms; the ONLY difference is the
selection score:
  * universe per day  = grouped-daily Ross movers ($1-20, $vol>=$1M, |chg|>=5%), top N.
  * per-name signals  = rvol proxy (today vol / ~1mo avg daily vol), momentum (day
                        change %), dollar_volume (close*vol). float is unavailable
                        historically -> omitted from BOTH arms (fair).
  * BASELINE score    = score_universe(rvol+momentum)            -> top MAX_SLOTS armed.
  * BIASED score      = score_universe(+ tradeable_liquidity)    -> top MAX_SLOTS armed.
  * each armed name   = same pullback-break trigger + the live adaptive spread gate
                        (spread PROXY = percentile of the day's $-vol -> 40..250 bps) +
                        the same forward stop/target/trail sim. A name that fails the
                        spread gate is ARMED-BUT-UNFILLED (the exact "watch, never fill"
                        the operator is seeing).

HONEST: the spread is a $-vol PROXY (real low-float spreads run wider — proxy is an
optimistic ceiling), but it is IDENTICAL for both arms, so the RELATIVE comparison
(does the liquidity bias lift fills/PnL?) is valid even though the absolute $ is not.
Run from the worktree so it imports the NEW ross_momentum (the biased weights).
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
from app.services.trading.momentum_neural.entry_gates import breakout_failed_to_hold, momentum_pullback_trigger
from app.services.trading.momentum_neural.paper_execution import (
    build_synthetic_quote, effective_stop_atr_pct, long_entry_fill_price, long_exit_fill_price,
    runner_trail_stop, scale_out_fraction, stop_target_prices, structural_or_vol_floored_atr_pct,
)
from app.services.trading.momentum_neural.ross_momentum import (
    ROSS_PILLAR_WEIGHTS, ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED, score_universe,
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
UNIVERSE_TOP = 60   # broaden the pool so the two scores can actually pick different sets

TRADING_DAYS = [
    "2026-05-26", "2026-05-27", "2026-05-28", "2026-05-29",
    "2026-06-01", "2026-06-02", "2026-06-03", "2026-06-04", "2026-06-05",
    "2026-06-08", "2026-06-09",
]

# BASELINE here = rvol + momentum only (float omitted: unavailable historically). The
# biased arm adds tradeable_liquidity on the SAME two, so the delta isolates the pillar.
W_BASELINE = {"rvol": 0.56, "momentum": 0.44}
W_BIASED = {"rvol": 0.48, "momentum": 0.37, "tradeable_liquidity": 0.15}


def _q(mid, sp):
    return build_synthetic_quote(mid, sp)


def _rth(ts) -> bool:
    m = ts.hour * 60 + ts.minute
    return 13 * 60 + 30 <= m <= 20 * 60


def _wide_stop(entry, atrp, pblow):
    eff = effective_stop_atr_pct(atrp, atrp * 10_000.0, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
    eff, _ = structural_or_vol_floored_atr_pct(
        vol_floored_atr_pct=eff, structural_stop_price=pblow, entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)


def _forward(d, O, H, L, C, ei, entry, stop, target, brk, atrp, sp):
    n = len(d)
    risk = entry - stop
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


def _day_grouped(date):
    """All grouped-daily Ross movers for `date`: {sym: (chg, close, dollar_vol, day_vol)}."""
    r = mc._get(mc._base() + "/v2/aggs/grouped/locale/us/market/stocks/" + date, {"adjusted": "true"})
    res = (r or {}).get("results") or []
    out = {}
    for s in res:
        try:
            t = s.get("T"); c = s.get("c"); o = s.get("o"); v = s.get("v") or 0
            if not t or not c or not o or v <= 0:
                continue
            chg = (c - o) / o * 100.0
            if 1 <= c <= 20 and c * v > 1_000_000 and abs(chg) >= 5:
                out[t] = (chg, c, c * v, v)
        except Exception:
            continue
    return out


_df_cache: dict[str, object] = {}


def _bars(sym):
    if sym not in _df_cache:
        try:
            _df_cache[sym] = fetch_ohlcv_df(sym, interval=INTERVAL, period="1mo")
        except Exception:
            _df_cache[sym] = None
    return _df_cache[sym]


_rvol_cache: dict[str, float] = {}


def _rvol_proxy(sym, date, day_vol):
    """today's volume / ~1mo average DAILY volume (from the 5m bars). >1 = above-avg."""
    key = sym + "|" + date
    if key in _rvol_cache:
        return _rvol_cache[key]
    val = None
    try:
        df = _bars(sym)
        if df is not None and len(df) > 0:
            c = {x.lower(): x for x in df.columns}
            vol = df[c["volume"]].astype(float)
            by_day = vol.groupby([t.strftime("%Y-%m-%d") for t in df.index]).sum()
            avg = float(by_day[by_day > 0].mean()) if (by_day > 0).any() else None
            if avg and avg > 0:
                val = float(day_vol) / avg
    except Exception:
        val = None
    _rvol_cache[key] = val
    return val


def _spread_for(dvol, sorted_dv):
    if not sorted_dv or len(sorted_dv) < 2:
        return SPREAD_FLOOR_BPS
    rank = sum(1 for x in sorted_dv if x < dvol) / (len(sorted_dv) - 1)
    rank = max(0.0, min(1.0, rank))
    return SPREAD_CAP_BPS - rank * (SPREAD_CAP_BPS - SPREAD_FLOOR_BPS)


def _sim_symbol(sym, date, sp):
    """Run the pullback-trigger + spread-gate + forward sim for one armed name on `date`.
    Returns (filled, list_of_fires). filled=False means armed-but-spread-gated (watched)."""
    df_all = _bars(sym)
    if df_all is None or len(df_all) == 0:
        return False, []
    try:
        c = {x.lower(): x for x in df_all.columns}
        O, H, L, C = c["open"], c["high"], c["low"], c["close"]
        df = df_all[[t.strftime("%Y-%m-%d") == date for t in df_all.index]]
        if len(df) < 14:
            return False, []
        idx = df.index; n = len(df)
        atr = compute_atr(df[H].astype(float), df[L].astype(float), df[C].astype(float))
        fires = []; any_trigger = False; passed_gate = False
        i = 10
        while i < n - 1:
            if not _rth(idx[i + 1]):
                i += 1; continue
            ok, _, dbg = momentum_pullback_trigger(df.iloc[: i + 1], entry_interval=INTERVAL)
            if not ok:
                i += 1; continue
            any_trigger = True
            ei = i + 1; mid0 = float(df[O].iloc[ei])
            atrp = float(atr.iloc[i]) / mid0 if (mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
            move_bps = atrp * 10_000.0
            if move_bps <= 0 or sp > GATE_MOVE_FRAC * move_bps:   # the live spread gate
                i = ei + 1; continue
            passed_gate = True
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
        return passed_gate, fires
    except Exception:
        return False, []


def _concurrency(fires):
    """Same per-day concurrency + daily-cap sim as the live lane."""
    fires.sort(key=lambda f: (f["t"], -f["fresh"]))
    active = []; taken = []; sym_open = set(); cum_usd = 0.0; peak = 0.0; halted = None

    def _close(upto):
        nonlocal cum_usd, peak, halted
        still = []
        for tr in active:
            if tr["xidx"] <= upto:
                cum_usd += tr["usd"]; peak = max(peak, cum_usd); sym_open.discard(tr["name"])
                if halted is None and cum_usd <= -DAILY_LOSS_CAP_USD:
                    halted = "daily_loss"
                elif halted is None and peak >= DAILY_LOSS_CAP_USD and cum_usd <= peak * (1 - GIVEBACK_FRAC):
                    halted = "giveback"
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
    return taken, cum_usd, halted


def _replay_day(date):
    g = _day_grouped(date)
    if not g:
        return None
    # rank the day's movers by |chg|, keep the top UNIVERSE_TOP as the armable pool
    pool = sorted(g.items(), key=lambda kv: abs(kv[1][0]), reverse=True)[:UNIVERSE_TOP]
    sorted_dv = sorted(v[2] for _, v in pool)
    # build the per-name Ross signals (rvol proxy, momentum, $-vol)
    signals = {}
    for sym, (chg, close, dvol, dayv) in pool:
        rv = _rvol_proxy(sym, date, dayv)
        sig = {"daily_change_pct": chg, "dollar_volume": dvol}
        if rv is not None:
            sig["rvol"] = rv
        signals[sym] = sig

    def _armed_set(weights):
        scored = score_universe(signals, weights=weights)
        ranked = sorted(scored.values(), key=lambda s: s.rank)
        return [s.symbol for s in ranked[:MAX_SLOTS]]

    out = {}
    for arm, weights in (("base", W_BASELINE), ("biased", W_BIASED)):
        armed = _armed_set(weights)
        all_fires = []; n_filled = 0
        for sym in armed:
            sp = _spread_for(g[sym][2], sorted_dv)
            filled, fires = _sim_symbol(sym, date, sp)
            if filled:
                n_filled += 1
            all_fires.extend(fires)
        taken, usd, halted = _concurrency(all_fires)
        wins = sum(1 for t in taken if t["r"] > 0)
        out[arm] = dict(armed=len(armed), filled=n_filled, trades=len(taken), wins=wins, usd=usd, halted=halted)
    return dict(date=date, **{f"{a}_{k}": out[a][k] for a in out for k in out[a]})


print("=== LIQUIDITY-BIASED vs BASELINE selection — previous-days A/B (proxy spread; RELATIVE valid) ===")
print("%-12s | %s | %s" % ("DAY", "BASELINE  fill/arm trades win  PnL", "BIASED  fill/arm trades win  PnL"))
agg = {"base_usd": 0.0, "biased_usd": 0.0, "base_filled": 0, "biased_filled": 0,
       "base_trades": 0, "biased_trades": 0, "base_wins": 0, "biased_wins": 0}
ndays = 0
_only = os.environ.get("REPLAY_ONLY")
for d in TRADING_DAYS:
    if _only and d != _only:
        continue
    row = _replay_day(d)
    if row is None:
        print("%-12s   (no data / holiday)" % d); continue
    ndays += 1
    for k in agg:
        agg[k] += row[k]
    print("%-12s |   %2d/%2d   %3d   %2d  $%+8.0f |   %2d/%2d   %3d   %2d  $%+8.0f" % (
        d, row["base_filled"], row["base_armed"], row["base_trades"], row["base_wins"], row["base_usd"],
        row["biased_filled"], row["biased_armed"], row["biased_trades"], row["biased_wins"], row["biased_usd"]))
print("-" * 96)
print("TOTAL (%d days)   BASELINE: %d fills, %d trades, %d wins, $%+.0f   |   BIASED: %d fills, %d trades, %d wins, $%+.0f" % (
    ndays, agg["base_filled"], agg["base_trades"], agg["base_wins"], agg["base_usd"],
    agg["biased_filled"], agg["biased_trades"], agg["biased_wins"], agg["biased_usd"]))
_df = agg["biased_filled"] - agg["base_filled"]
_du = agg["biased_usd"] - agg["base_usd"]
print("DELTA (biased - baseline): %+d fills, $%+.0f PnL  ->  %s" % (
    _df, _du,
    "LIQUIDITY BIAS WINS (more fills + better/comparable PnL)" if (_df >= 0 and _du >= -abs(agg["base_usd"]) * 0.1)
    else "inconclusive / baseline better — DO NOT ship"))
