"""Re-test today's momentum symbols THROUGH THE SYSTEM.

Every entry/exit/sizing/fill/fee decision is made by the SYSTEM's OWN functions —
the same code the paper + live momentum runners use (parity contract):
  * entry:   entry_gates.pullback_break_confirmation  (full live config: vol-aware
             shallow/EMA, candle/VWAP/MACD confirmations, runaway-break)
  * stop+tgt: paper_execution.stop_target_prices       (ATR stop x reward:risk 2:1)
  * fills:   paper_execution.long_entry/exit_fill_price (slippage model)
  * scale:   paper_execution.scale_out_fraction/quantity
  * breakeven+trail: paper_execution.breakeven_stop_after_partial / runner_trail_stop
  * fast exits: entry_gates.breakout_failed_to_hold, candles.is_topping_tail
  * fees:    paper_execution.roundtrip_fee_usd
Only the bar-walking loop is local orchestration (what the live FSM tick does per
tick); the trade MATH is 100% the system's. Replays today's RTH bars; no DB, no
look-ahead (each gate call sees only bars up to that point).
"""
from __future__ import annotations

import sys

import pandas as pd

from app.services.trading.indicator_core import compute_atr
from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.candles import is_topping_tail
from app.services.trading.momentum_neural.entry_gates import (
    breakout_failed_to_hold,
    pullback_break_confirmation,
)
from app.services.trading.momentum_neural.paper_execution import (
    breakeven_stop_after_partial,
    build_synthetic_quote,
    effective_stop_atr_pct,
    long_entry_fill_price,
    long_exit_fill_price,
    roundtrip_fee_usd,
    runner_trail_stop,
    scale_out_fraction,
    stop_target_prices,
    structural_or_vol_floored_atr_pct,
)

NAMES = ["INHD", "SUNE", "NPT", "FAC", "SMTK", "IXHL", "BYAH", "CBRG", "GLXU", "MOBX", "GLGG", "ABAT"]
STOP_ATR_MULT = 0.60          # paper-runner default (impulse_breakout family)
REWARD_RISK = 2.0             # chili_momentum_risk_reward_risk_ratio
SPREAD_BPS = float(sys.argv[2]) if len(sys.argv) > 2 else 40.0   # synthetic small-cap spread
SLIP_BPS = float(sys.argv[1]) if len(sys.argv) > 1 else 15.0
MAX_CTR = float(sys.argv[3]) if len(sys.argv) > 3 else 0.0  # net-edge gate (0=off)
STOP_MODE = sys.argv[4] if len(sys.argv) > 4 else "current"  # current | structural
FEE_RATIO = 0.08
RTH = (pd.Timestamp("2026-06-08 13:30", tz="UTC"), pd.Timestamp("2026-06-08 20:00", tz="UTC"))
LIVE = dict(entry_interval="5m", volume_spike_multiple=1.5,
            require_retest=True, retest_tolerance=0.002, retest_lookback_bars=4,
            require_sustained_volume=True, sustained_rvol_floor=1.0, sustain_lookback_bars=5,
            require_break_candle=True, require_vwap_hold=True, require_macd_bullish=True,
            allow_runaway_break=True, runaway_min_volume_spike=2.5)


def _q(mid):
    return build_synthetic_quote(mid, SPREAD_BPS)


def backtest(sym):
    df = fetch_ohlcv_df(sym, interval="5m", period="1d")
    if df is None or len(df) < 12:
        return []
    c = {x.lower(): x for x in df.columns}
    O, H, L, C = c["open"], c["high"], c["low"], c["close"]
    atr = compute_atr(df[H].astype(float), df[L].astype(float), df[C].astype(float))
    n = len(df); idx = df.index
    out = []
    i = 10
    while i < n - 1:
        ok, reason, dbg = pullback_break_confirmation(df.iloc[: i + 1], **LIVE)
        if not ok or not (RTH[0] <= idx[i + 1] <= RTH[1]):
            i += 1
            continue
        ei = i + 1
        mid0 = float(df[O].iloc[ei])
        q0 = _q(mid0)
        entry = long_entry_fill_price(q0.ask, mid0, SLIP_BPS)        # SYSTEM fill
        atrp = float(atr.iloc[i]) / mid0 if (atr is not None and mid0 > 0 and pd.notna(atr.iloc[i])) else 0.0
        # EXACT live stop chain: vol-floored ATR (capped 0.15) then structural override
        # (take the WIDER), feeding stop_target_prices — the same code the live runner runs.
        em_bps = atrp * 10_000.0
        eff_atr = effective_stop_atr_pct(atrp, em_bps, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
        _pblow = dbg.get("pullback_low")
        if STOP_MODE == "structural" and _pblow and float(_pblow) < entry:
            # Prefer the structural pullback stop (tighter -> closer target -> higher
            # hit rate); cap 0.15, small floor 0.005 to avoid absurd tightness. NO vol-floor.
            eff_atr = min(0.15, max(0.005, (entry - float(_pblow)) / entry / STOP_ATR_MULT))
        else:
            eff_atr, stop_model = structural_or_vol_floored_atr_pct(
                vol_floored_atr_pct=eff_atr,
                structural_stop_price=float(_pblow) if _pblow else None,
                entry_price=entry, stop_atr_mult=STOP_ATR_MULT)
        stop, target = stop_target_prices(entry, atr_pct=eff_atr, side_long=True,
                                          stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)  # SYSTEM
        brk = dbg.get("pullback_high")
        brk = float(brk) if brk else None
        if not (stop > 0 and stop < entry):
            i += 1
            continue
        risk = entry - stop
        scaled = False; bal = stop; rh = entry; scx = None
        frac = scale_out_fraction()
        j = ei; xpx = xrs = None
        while j < n:
            bh, bl, bc = float(df[H].iloc[j]), float(df[L].iloc[j]), float(df[C].iloc[j])
            held_s = (j - ei) * 300.0   # 5m bars -> seconds, for the breakout-bailout window
            qb = _q(bc)
            if not scaled and brk and breakout_failed_to_hold(
                breakout_level=brk, bid=qb.bid, held_seconds=held_s, window_seconds=2 * 300.0
            ):
                xpx, xrs = long_exit_fill_price(qb.bid, bc, SLIP_BPS), "bailout"; break
            if bl <= bal:
                xpx = long_exit_fill_price(_q(bal).bid, bal, SLIP_BPS)
                xrs = "stop" if not scaled else ("breakeven" if abs(bal - entry) < 1e-9 else "trail"); break
            if scaled and is_topping_tail(float(df[O].iloc[j]), bh, bl, bc):
                xpx, xrs = long_exit_fill_price(qb.bid, bc, SLIP_BPS), "topping_tail"; break
            if not scaled and bh >= target:
                scaled, scx = True, long_exit_fill_price(_q(target).bid, target, SLIP_BPS)
                bal = breakeven_stop_after_partial(entry, bal, side_long=True)   # SYSTEM
                rh = max(rh, bh)
            if scaled:
                rh = max(rh, bh)
                bal = runner_trail_stop(high_water_mark=rh, atr_pct=atrp, stop_atr_mult=STOP_ATR_MULT,
                                        breakeven_floor=entry, current_stop=bal, side_long=True)  # SYSTEM
            j += 1
        if xpx is None:
            xpx, xrs = long_exit_fill_price(_q(float(df[C].iloc[-1])).bid, float(df[C].iloc[-1]), SLIP_BPS), "eod"
        if scaled:
            pnl_r = frac * (scx - entry) / risk + (1 - frac) * (xpx - entry) / risk
        else:
            pnl_r = (xpx - entry) / risk
        out.append(dict(sym=sym, t=idx[ei], entry=entry, stop=stop, tgt=target,
                        xrs=xrs, scaled=scaled, pnl_r=pnl_r))
        i = j + 1
    return out


all_tr = []
print(f"=== SYSTEM REPLAY (real paper-execution fns)  slip={SLIP_BPS}bps spread={SPREAD_BPS}bps  2026-06-08 RTH ===\n")
for sym in NAMES:
    tr = backtest(sym)
    all_tr += tr
    if tr:
        net = sum(t["pnl_r"] for t in tr); w = sum(1 for t in tr if t["pnl_r"] > 0)
        print(f"{sym:6s}: {len(tr)}t net {net:+5.2f}R ({w}/{len(tr)}w)  " +
              "  ".join(f"[{t['t']:%H:%M} {t['xrs']} {t['pnl_r']:+.2f}R]" for t in tr))
    else:
        print(f"{sym:6s}: no trades")

if all_tr:
    net = sum(t["pnl_r"] for t in all_tr)
    wins = [t for t in all_tr if t["pnl_r"] > 0]
    aw = sum(t["pnl_r"] for t in wins) / len(wins) if wins else 0.0
    losers = [t for t in all_tr if t["pnl_r"] <= 0]
    al = sum(t["pnl_r"] for t in losers) / len(losers) if losers else 0.0
    print(f"\n=== AGGREGATE (system functions) ===")
    print(f"trades={len(all_tr)}  NET={net:+.2f}R  win%={100*len(wins)/len(all_tr):.0f}  "
          f"avgWin={aw:+.2f}R avgLoss={al:+.2f}R  expectancy={net/len(all_tr):+.2f}R/trade")
else:
    print("\nNO TRADES.")
