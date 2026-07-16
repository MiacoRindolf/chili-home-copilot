"""Full trade backtest of CHILI's momentum lane (REAL vol-aware gate + documented
stop/target/scale/trail/bailout exits) across the explosive small-caps the universe
now selects. Reports per-trade + aggregate P&L in R-multiples, gross and slippage-
adjusted, so the recalibration is judged on NET edge — not trade count."""
from __future__ import annotations

import sys

import pandas as pd

from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation
from app.services.trading.momentum_neural.candles import is_topping_tail

NAMES = ["INHD", "SUNE", "NPT", "GLXU", "IXHL", "BYAH", "CBRG", "SDOT", "MOBX", "GLGG"]
RR, SCALE_FRAC, BAILOUT_BARS, BAILOUT_BUF = 2.0, 0.5, 2, 0.001
RTH_START = pd.Timestamp("2026-06-08 13:30", tz="UTC")
RTH_END = pd.Timestamp("2026-06-08 20:00", tz="UTC")
SLIP = float(sys.argv[1]) if len(sys.argv) > 1 else 0.0  # per-side slippage (e.g. 0.01 = 1%)

LIVE = dict(entry_interval="5m", volume_spike_multiple=1.5,
            require_retest=True, retest_tolerance=0.002, retest_lookback_bars=4,
            require_sustained_volume=True, sustained_rvol_floor=1.0, sustain_lookback_bars=5,
            require_break_candle=True, require_vwap_hold=True, require_macd_bullish=True,
            allow_runaway_break=True, runaway_min_volume_spike=4.0)


def _rth(ts):
    return RTH_START <= ts <= RTH_END


def backtest(sym):
    df = fetch_ohlcv_df(sym, interval="5m", period="1d")
    if df is None or len(df) < 12:
        return []
    c = {x.lower(): x for x in df.columns}
    O, H, L, C = c["open"], c["high"], c["low"], c["close"]
    n = len(df); idx = df.index
    out = []
    i = 10
    while i < n - 1:
        ok, reason, dbg = pullback_break_confirmation(df.iloc[: i + 1], **LIVE)
        if not ok or not _rth(idx[i + 1]):
            i += 1
            continue
        ei = i + 1
        entry = float(df[O].iloc[ei]) * (1 + SLIP)        # buy with slippage
        stop = float(dbg.get("pullback_low") or 0.0)
        brk = dbg.get("pullback_high")
        brk = float(brk) if brk else None
        if not (stop > 0 and stop < entry):
            i += 1
            continue
        risk = entry - stop
        target = entry + RR * risk
        scaled = False; bal = stop; rh = entry; scx = None
        j = ei; xpx = xrs = None
        while j < n:
            bh, bl, bc = float(df[H].iloc[j]), float(df[L].iloc[j]), float(df[C].iloc[j])
            held = j - ei
            if not scaled and brk and held <= BAILOUT_BARS and bl < brk * (1 - BAILOUT_BUF):
                xpx, xrs = min(bc, brk * (1 - BAILOUT_BUF)) * (1 - SLIP), "bailout"; break
            if bl <= bal:
                xpx = bal * (1 - SLIP); xrs = "stop" if not scaled else ("breakeven" if abs(bal - entry) < 1e-9 else "trail"); break
            bo = float(df[O].iloc[j])
            if scaled and is_topping_tail(bo, bh, bl, bc):   # runner-only: lock the tail on exhaustion
                xpx, xrs = bc * (1 - SLIP), "topping_tail"; break
            if not scaled and bh >= target:
                scaled, scx, bal = True, target * (1 - SLIP), entry; rh = max(rh, bh)
            if scaled:
                rh = max(rh, bh); bal = max(entry, rh - risk)
            j += 1
        if xpx is None:
            xpx, xrs = float(df[C].iloc[-1]) * (1 - SLIP), "eod"
        if scaled:
            pnl_r = SCALE_FRAC * (scx - entry) / risk + (1 - SCALE_FRAC) * (xpx - entry) / risk
        else:
            pnl_r = (xpx - entry) / risk
        out.append(dict(sym=sym, t=idx[ei], entry=entry, stop=stop, tgt=target,
                        xrs=xrs, scaled=scaled, pnl_r=pnl_r))
        i = j + 1
    return out


all_tr = []
print(f"=== momentum-lane backtest (vol-aware gate)  slippage/side={SLIP*100:.1f}%  2026-06-08 RTH ===\n")
for sym in NAMES:
    tr = backtest(sym)
    all_tr += tr
    if tr:
        net = sum(t["pnl_r"] for t in tr)
        w = sum(1 for t in tr if t["pnl_r"] > 0)
        print(f"{sym:6s}: {len(tr)} trades  net {net:+5.2f}R  ({w}/{len(tr)} win)  " +
              "  ".join(f"[{t['t']:%H:%M} {t['xrs']} {t['pnl_r']:+.2f}R]" for t in tr))
    else:
        print(f"{sym:6s}: no trades")

if all_tr:
    net = sum(t["pnl_r"] for t in all_tr)
    wins = [t for t in all_tr if t["pnl_r"] > 0]
    loss = [t for t in all_tr if t["pnl_r"] <= 0]
    aw = sum(t["pnl_r"] for t in wins) / len(wins) if wins else 0
    al = sum(t["pnl_r"] for t in loss) / len(loss) if loss else 0
    exp = net / len(all_tr)
    print(f"\n=== AGGREGATE ===")
    print(f"trades={len(all_tr)}  NET={net:+.2f}R  win%={100*len(wins)/len(all_tr):.0f}  "
          f"avgWin={aw:+.2f}R avgLoss={al:+.2f}R  expectancy={exp:+.2f}R/trade")
    print(f"at 1% equity risk/trade -> {net:+.1f}% of equity for the day across the basket")
else:
    print("\nNO TRADES.")
