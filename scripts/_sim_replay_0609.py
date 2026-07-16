"""DAY-LEVEL replay of the ENHANCED momentum lane for 2026-06-09 (TODAY) — the NEW system.

Why this run: today the LIVE lane took 0 fills. It armed only 2 equities (NMRK, CCTG) and both
were blocked by the spread gate (NMRK: 9x wide_bbo_spread, actual 26.6bps vs max-allowed 23.87bps,
then stale-data abort). The liquidity-bias (#552) deployed AFTER today's 13:30 open — so today ran
WITHOUT it. This replay asks: with the new system (price-band #548 + liquidity-bias #552), how many
FILLABLE movers were there today, and what would the lane have made?

Adds vs the 06-08 harness:
  * DAY = 2026-06-09; universe from the CURRENT-day snapshot field (today's movers).
  * Per-name DOLLAR-VOLUME (price*today-volume) = the selection-time liquidity proxy (#552).
  * Per-name REALISTIC SPREAD derived by PERCENTILE rank within the day's universe (adaptive,
    no magic constant): most-liquid decile -> tight floor, least-liquid -> wide cap. Calibrated to
    observed reality (deployed liquid spread 40bps; live events showed illiquid small-caps 127-500bps).
  * SPREAD GATE (faithful to the live lane): a fire only fills if its name's spread clears the bar
    (spread <= 0.5 * the name's intraday expected-move bps) — exactly why NMRK was blocked today.
  * A/B selection: (A) mover-strength rank (today's behaviour) vs (B) liquidity-bias rank (#552).
"""
from __future__ import annotations

import os
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
SLIP_BPS = 15.0
DAY = os.environ.get("REPLAY_DAY", "2026-06-09")
INTERVAL = "5m"
BASIS_USD = 22551.0
RISK_PER_TRADE_USD = BASIS_USD * 0.01
NOTIONAL_CAP_USD   = BASIS_USD * 0.15
MAX_SLOTS = 10
DAILY_LOSS_CAP_USD = BASIS_USD * 0.05
GIVEBACK_FRAC = 0.5
SECS = 300.0

# Per-name spread model (adaptive percentile within the day's universe).
SPREAD_FLOOR_BPS = 40.0     # deployed liquid round-trip spread (most-liquid decile)
SPREAD_CAP_BPS   = 250.0    # observed illiquid small-cap spread (least-liquid decile; live events 127-500)
# Spread GATE: the live lane blocks when spread > 0.5 * expected_move_bps. We proxy expected_move by
# the name's intraday ATR% at the fire bar (the same volatility the live expected_move is built from).
GATE_MOVE_FRAC = 0.5


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


# ── Universe: Ross small-cap profile on DAY (price 1-20, change>=5, $vol>=1M) ──
snap = get_full_market_snapshot() or []
cand = []
dvol_by_name: dict[str, float] = {}
for s in snap:
    try:
        dd = s.get("day") or {}
        if not dd.get("c"):
            dd = s.get("prevDay") or {}
        px = dd.get("c") or dd.get("vw"); v = dd.get("v") or 0; o = dd.get("o") or 0
        ch = ((float(px) - float(o)) / float(o) * 100.0) if (o and px) else 0.0
        if px and 1 <= float(px) <= 20 and float(px) * float(v) > 1_000_000 and abs(ch) >= 5:
            cand.append((s["ticker"], abs(ch)))
            dvol_by_name[s["ticker"]] = float(px) * float(v)
    except Exception:
        continue
cand.sort(key=lambda z: z[1], reverse=True)
names = [t for t, _ in cand[:100]]

# Percentile-rank dollar-volume -> per-name spread (adaptive: most-liquid->floor, least->cap).
_sorted_dv = sorted((dvol_by_name.get(n, 0.0) for n in names))
def _spread_for(name: str) -> float:
    dv = dvol_by_name.get(name, 0.0)
    if not _sorted_dv or len(_sorted_dv) < 2:
        return SPREAD_FLOOR_BPS
    # percentile of this name's $vol within the universe (0=least liquid, 1=most liquid)
    rank = sum(1 for x in _sorted_dv if x < dv) / (len(_sorted_dv) - 1)
    rank = max(0.0, min(1.0, rank))
    return SPREAD_CAP_BPS - rank * (SPREAD_CAP_BPS - SPREAD_FLOOR_BPS)


# ── Collect every gate fire; price at the name's REALISTIC spread; apply the spread GATE ──
fires = []
gated_out = 0
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
        idx = df.index; n = len(df)
        atr = compute_atr(df[H].astype(float), df[L].astype(float), df[C].astype(float))
        spread_bps = _spread_for(sym)
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
            # SPREAD GATE (faithful to live wide_bbo_spread): need spread <= 0.5 * expected_move(bps).
            move_bps = atrp * 10_000.0
            if move_bps <= 0 or spread_bps > GATE_MOVE_FRAC * move_bps:
                gated_out += 1; i = ei + 1; continue
            entry = long_entry_fill_price(_q(mid0, spread_bps).ask, mid0, SLIP_BPS)
            pblow = dbg.get("pullback_low"); pblow = float(pblow) if pblow else None
            brk = dbg.get("pullback_high"); brk = float(brk) if brk else None
            stop, target = _wide_stop(entry, atrp, pblow)
            if not (0 < stop < entry):
                i += 1; continue
            r, xidx = _forward(df, O, H, L, C, ei, entry, stop, target, brk, atrp, spread_bps)
            if r is None:
                i += 1; continue
            dh = max(float(df[H].iloc[k]) for k in range(i + 1))
            dl = min(float(df[L].iloc[k]) for k in range(i + 1))
            fresh = (mid0 - dl) / (dh - dl) if dh > dl else 0.5
            sp = (entry - stop) / entry
            _risk = min(RISK_PER_TRADE_USD, NOTIONAL_CAP_USD * sp) if sp > 0 else RISK_PER_TRADE_USD
            _notl = min(RISK_PER_TRADE_USD / sp, NOTIONAL_CAP_USD) if sp > 0 else NOTIONAL_CAP_USD
            fires.append(dict(name=sym, ei=ei, xidx=xidx, r=r, fresh=fresh, t=idx[ei],
                              stop_pct=sp, risk=_risk, notl=_notl, usd=r * _risk,
                              dvol=dvol_by_name.get(sym, 0.0), spread=spread_bps))
            i = ei + 1
    except Exception:
        continue

print(f"=== 06-09 REPLAY (NEW system: price-band #548 + liquidity-bias #552 + spread gate) {INTERVAL} ===")
print(f"universe={len(names)} Ross small-caps | fillable gate fires={len(fires)} | spread-GATED out={gated_out}")
print(f"(REAL lane today: 0 fills — armed only NMRK+CCTG, both wide_bbo_spread blocked)\n")


def _run_sim(order_key, label):
    # Causal time-order is primary (can't arm a fire before it happens); the preference key
    # only breaks ties when several fires compete for a free slot at the same bar.
    ordered = sorted(fires, key=order_key)
    active = []; taken = []; sym_open = set()
    cum_r = 0.0; cum_usd = 0.0; peak_usd = 0.0; halted = [None]

    def _close_due(upto_ei):
        nonlocal cum_r, cum_usd, peak_usd
        still = []
        for tr in active:
            if tr["xidx"] <= upto_ei:
                cum_r += tr["r"]; cum_usd += tr["usd"]; peak_usd = max(peak_usd, cum_usd)
                sym_open.discard(tr["name"])
                if halted[0] is None and cum_usd <= -DAILY_LOSS_CAP_USD:
                    halted[0] = "daily_loss_cap"
                elif halted[0] is None and peak_usd >= DAILY_LOSS_CAP_USD and cum_usd <= peak_usd * (1 - GIVEBACK_FRAC):
                    halted[0] = "profit_giveback"
            else:
                still.append(tr)
        active[:] = still

    for f in ordered:
        _close_due(f["ei"])
        if halted[0]:
            continue
        if f["name"] in sym_open or len(active) >= MAX_SLOTS:
            continue
        active.append(f); sym_open.add(f["name"]); taken.append(f)
    for tr in active:
        cum_r += tr["r"]; cum_usd += tr["usd"]; peak_usd = max(peak_usd, cum_usd)
    wins = sum(1 for t in taken if t["r"] > 0)
    print(f"--- {label} ---")
    for t in sorted(taken, key=lambda z: z["t"]):
        print(f"  {t['t'].strftime('%H:%M')}UTC {t['name']:7s} $vol=${t['dvol']/1e6:>5.0f}M sprd={t['spread']:>3.0f}bps {t['r']:+.2f}R PnL=${t['usd']:+.0f}")
    print(f"  TOTAL trades={len(taken)} win={wins}/{len(taken)} total={cum_r:+.2f}R = ${cum_usd:+.0f}"
          + (f"  [halted:{halted[0]}]" if halted[0] else "") + "\n")
    return cum_usd


# A: today's behaviour (freshest-firing, liquidity-blind). B: #552 liquidity-bias (prefer high $vol).
usd_a = _run_sim(lambda f: (f["t"], -f["fresh"]), "A) mover-rank (today's liquidity-BLIND selection)")
# liquidity-bias: blend freshness rank + dollar-volume rank (the #552 50/50 rerank)
_vr = {id(f): i for i, f in enumerate(sorted(fires, key=lambda z: -z["fresh"]))}
_dr = {id(f): i for i, f in enumerate(sorted(fires, key=lambda z: -z["dvol"]))}
usd_b = _run_sim(lambda f: (f["t"], _vr[id(f)] + _dr[id(f)]), "B) liquidity-bias #552 (prefer FILLABLE high-$vol)")

print(f"=== STUDY ===")
print(f"REAL today               = $0 (0 fills, spread-gated)")
print(f"A liquidity-blind replay = ${usd_a:+.0f}")
print(f"B liquidity-bias  replay = ${usd_b:+.0f}   (delta from #552: ${usd_b-usd_a:+.0f})")
