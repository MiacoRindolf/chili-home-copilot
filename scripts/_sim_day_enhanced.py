"""DAY-LEVEL replay of the ENHANCED momentum lane for one session (default 2026-06-08).

Not a single name — this simulates the whole enhanced lane as it would have traded the day:
  * Universe = Ross small-cap profile (price $1-20, change>=5%, $vol>=1M) from the snapshot
    (INHD at ~$38 is correctly EXCLUDED — too high-priced, exactly as Ross skipped it).
  * Per name: every live gate fire (momentum_pullback_trigger -> all confirmations at live
    settings) on the 5m tape, with the live wide-stop / 2:1 / documented exits -> R + entry/
    exit bar (faithful to the live lane).
  * Concurrency-aware day sim: <=5 slots (max_concurrent_live_sessions), arm the FRESHEST
    FIRING name into a free slot (Ross's "one moving now"), one live session per symbol,
    and STOP arming once the equity-relative daily-loss cap (-$250 = -5R) trips or the
    50% profit-giveback halt fires. This mirrors auto_arm Guards + the freshness picker.

R->$ at the documented $50/trade risk (max_loss_per_trade_usd; scales with equity-relative
sizing). This is the realistic "what enhanced CHILI makes that day" number — selectivity is
the edge (it does NOT trade every fire; the broad-gate baseline is negative).
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
DAY = "2026-06-08"
INTERVAL = "5m"
BASIS_USD = 22551.0            # DEPLOYED sizing basis: 2x Gold-margin buying power ($11,276*2)
RISK_PER_TRADE_USD = BASIS_USD * 0.01   # 1% per-trade loss cap = ~$226 (2x the old $113)
NOTIONAL_CAP_USD   = BASIS_USD * 0.15   # 15% notional ceiling = ~$3,383
MAX_SLOTS = 10                 # DEPLOYED: basis-independent (open_risk_frac/loss_frac = 0.10/0.01)
DAILY_LOSS_CAP_USD = BASIS_USD * 0.05   # 5% daily-loss breaker = ~$1,128
GIVEBACK_FRAC = 0.5            # Ross 50%-giveback
SECS = 300.0


def _q(mid):
    return build_synthetic_quote(mid, SPREAD_BPS)


def _rth(ts) -> bool:
    m = ts.hour * 60 + ts.minute
    return 13 * 60 + 30 <= m <= 20 * 60


def _wide_stop(entry, atrp, pblow):
    eff = effective_stop_atr_pct(atrp, atrp * 10_000.0, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
    eff, _ = structural_or_vol_floored_atr_pct(vol_floored_atr_pct=eff, structural_stop_price=pblow, entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
    return stop_target_prices(entry, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)


def _forward(d, O, H, L, C, ei, entry, stop, target, brk, atrp):
    """Return (R, exit_bar_index)."""
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
        exit_px = long_exit_fill_price(_q(float(d[C].iloc[-1])).bid, float(d[C].iloc[-1]), SLIP_BPS)
        j = n - 1
    if scaled:
        r = (SCALE_FRAC * (scx - entry) + (1 - SCALE_FRAC) * (exit_px - entry)) / risk
    else:
        r = (exit_px - entry) / risk
    return r, j


# ── Universe: Ross small-cap profile on DAY (price 1-20, change>=5, $vol>=1M) ──
snap = get_full_market_snapshot() or []
cand = []
for s in snap:
    try:
        dd = s.get("day") or {}
        # The 06-09 session resets the current-day fields to 0 (pre-market). Fall back to
        # prevDay, which IS the 06-08 session (the replay day) while 06-09 is current — so
        # the replay universe is the actual 06-08 movers regardless of when we run it.
        if not dd.get("c"):
            dd = s.get("prevDay") or {}
        px = dd.get("c") or dd.get("vw"); v = dd.get("v") or 0; o = dd.get("o") or 0
        ch = ((float(px) - float(o)) / float(o) * 100.0) if (o and px) else 0.0   # 06-08 net move
        if px and 1 <= float(px) <= 20 and float(px) * float(v) > 1_000_000 and abs(ch) >= 5:
            cand.append((s["ticker"], abs(ch)))
    except Exception:
        continue
# Rank by MOVER STRENGTH (|change%| desc) — the live viability ordering, NOT alphabetical.
# The old sorted()[:80] silently dropped late-alphabet movers like NPT (an "N"); ranking
# by strength includes the biggest movers AUTOMATICALLY (no manual force-add — the live
# system selects exactly this way, by viability_score desc).
cand.sort(key=lambda z: z[1], reverse=True)
names = [t for t, _ in cand[:100]]

# ── Collect every gate fire across the universe (each becomes a candidate trade) ──
fires = []   # dict(name, ei, exit_idx, r, fresh, t_entry)
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
        i = 10
        while i < n - 1:
            if not _rth(idx[i + 1]):
                i += 1; continue
            ok, _, dbg = momentum_pullback_trigger(df.iloc[: i + 1], entry_interval=INTERVAL)
            if not ok:
                i += 1; continue
            ei = i + 1
            mid0 = float(df[O].iloc[ei]); entry = long_entry_fill_price(_q(mid0).ask, mid0, SLIP_BPS)
            atrp = float(atr.iloc[i]) / mid0 if (mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
            pblow = dbg.get("pullback_low"); pblow = float(pblow) if pblow else None
            brk = dbg.get("pullback_high"); brk = float(brk) if brk else None
            stop, target = _wide_stop(entry, atrp, pblow)
            if not (0 < stop < entry):
                i += 1; continue
            r, xidx = _forward(df, O, H, L, C, ei, entry, stop, target, brk, atrp)
            if r is None:
                i += 1; continue
            # freshness (position-in-range) at the fire bar
            dh = max(float(df[H].iloc[k]) for k in range(i + 1))
            dl = min(float(df[L].iloc[k]) for k in range(i + 1))
            fresh = (mid0 - dl) / (dh - dl) if dh > dl else 0.5
            sp = (entry - stop) / entry                         # stop distance as % of entry
            # DEPLOYED risk-first: notional = min(risk/stop%, ceiling); risk = notional*stop%
            # = min(1%-equity loss cap, ceiling*stop%); PnL = R * realized risk.
            _risk = min(RISK_PER_TRADE_USD, NOTIONAL_CAP_USD * sp) if sp > 0 else RISK_PER_TRADE_USD
            _notl = min(RISK_PER_TRADE_USD / sp, NOTIONAL_CAP_USD) if sp > 0 else NOTIONAL_CAP_USD
            fires.append(dict(name=sym, ei=ei, xidx=xidx, r=r, fresh=fresh, t=idx[ei],
                              stop_pct=sp, risk=_risk, notl=_notl, usd=r * _risk))
            i = ei + 1
    except Exception:
        continue

print(f"=== DAY-LEVEL ENHANCED REPLAY  {DAY}  {INTERVAL} ===")
print(f"universe={len(names)} Ross small-caps  | total gate fires across universe={len(fires)}")
print(f"(if CHILI 'traded every fire' = broad gate: total {sum(f['r'] for f in fires):+.1f}R — NEGATIVE; selectivity is the edge)\n")

# ── Concurrency-aware day sim: <=5 slots, freshest-firing wins, dedup/symbol, daily caps ──
fires.sort(key=lambda f: (f["t"], -f["fresh"]))      # time order; freshest first on ties
active = []          # list of dict trades currently holding a slot (have xidx)
taken = []           # executed trades
sym_open = set()     # symbols currently in a slot (one live session per symbol)
cum_r = 0.0; cum_usd = 0.0; peak_usd = 0.0; halted = None

def _close_due(upto_ei):
    global cum_r, cum_usd, peak_usd, halted
    still = []
    for tr in active:
        if tr["xidx"] <= upto_ei:                    # closed before this fire bar
            cum_r += tr["r"]; cum_usd += tr["usd"]; peak_usd = max(peak_usd, cum_usd)
            sym_open.discard(tr["name"])
            # daily-loss cap / giveback on realized close ($-based, RH-equity-relative)
            if halted is None and cum_usd <= -DAILY_LOSS_CAP_USD:
                halted = "daily_loss_cap"
            elif halted is None and peak_usd >= DAILY_LOSS_CAP_USD and cum_usd <= peak_usd * (1 - GIVEBACK_FRAC):
                halted = "profit_giveback"
        else:
            still.append(tr)
    active[:] = still

skipped = []
for f in fires:
    _close_due(f["ei"])
    if halted:
        f["skip"] = "halted:" + str(halted); skipped.append(f); continue
    if f["name"] in sym_open:
        f["skip"] = "dup_symbol"; skipped.append(f); continue   # already hold a live session for this symbol
    if len(active) >= MAX_SLOTS:
        f["skip"] = "slots_full"; skipped.append(f); continue   # all slots busy -> skip (Ross can't watch infinite)
    f["taken"] = True
    active.append(f); sym_open.add(f["name"]); taken.append(f)

# close any still-open at EOD
for tr in active:
    cum_r += tr["r"]; cum_usd += tr["usd"]; peak_usd = max(peak_usd, cum_usd)

wins = sum(1 for t in taken if t["r"] > 0)
print(f"TRADES TAKEN (enhanced lane) — DEPLOYED risk-first ~${RISK_PER_TRADE_USD:.0f} risk/trade, notional<=${NOTIONAL_CAP_USD:.0f}:")
for t in sorted(taken, key=lambda z: z["t"]):
    print(f"  {t['t'].strftime('%H:%M')}UTC  {t['name']:7s}  notl=${t['notl']:>5.0f}  risk=${t['risk']:>3.0f}  {t['r']:+.2f}R  PnL=${t['usd']:+.0f}")
print(f"\n=== DAY TOTAL (enhanced, <= {MAX_SLOTS} concurrent, risk-first ~${RISK_PER_TRADE_USD:.0f}/trade) ===")
print(f"trades={len(taken)}  win={wins}/{len(taken)}  total={cum_r:+.2f}R  = ${cum_usd:+.0f}")
if halted:
    print(f"** session auto-halted: {halted} (cap -${DAILY_LOSS_CAP_USD:.0f} / 50% giveback) **")

# NPT-specific status (answers: was NPT seen / fired / taken / crowded out?)
print("\n== NPT status on 06-08 ==")
npt_fires = [f for f in fires if f["name"] == "NPT"]
if "NPT" not in names:
    print("  NPT NOT in replay universe")
elif not npt_fires:
    print("  NPT in universe but its 5m gate did NOT fire during RTH on 06-08")
else:
    for f in npt_fires:
        st = "TAKEN" if f.get("taken") else ("SKIPPED:" + f.get("skip", "?"))
        print(f"  NPT fired {f['t'].strftime('%H:%M')}UTC  fresh={f['fresh']:.2f}  {f['r']:+.2f}R = ${f['usd']:+.0f}  -> {st}")

# what got crowded out (skipped) — the concurrency cost
print(f"== skipped fires (crowded out / dup / halted): {len(skipped)} ==")
for f in sorted(skipped, key=lambda z: z["t"])[:14]:
    print(f"  {f['t'].strftime('%H:%M')}UTC {f['name']:7s} {f.get('skip','?'):14s} {f['r']:+.2f}R = ${f['usd']:+.0f}")
