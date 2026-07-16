"""Replay CHILI's REAL momentum-lane entry/exit logic against INHD intraday bars.

Uses the actual entry_gates.pullback_break_confirmation + breakout_failed_to_hold
(the live functions, live settings) for ENTRY + the documented stop/target/scale/
trail rules (stop_target_prices RR=2.0, scale-out half + breakeven + trail, RTH-only
equity entries). Prints every entry/exit so the logic can be judged. Not wired to
brokers — a pure offline replay.
"""
from __future__ import annotations

import pandas as pd

from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.entry_gates import (
    pullback_break_confirmation,
)

SYM = "INHD"
INTERVAL = "5m"          # chili_momentum_pullback_entry_interval (live default)
RR = 2.0                 # chili_momentum_risk_reward_risk_ratio (Ross floor)
SCALE_FRAC = 0.5         # scale_out_fraction (sell half at target)
BAILOUT_BARS = 2         # chili_momentum_breakout_bailout_max_bars
BAILOUT_BUF = 0.001
# RTH in UTC for 2026-06-08 (EDT = UTC-4): 13:30–20:00 UTC. Equities ENTER only in RTH.
RTH_START, RTH_END = pd.Timestamp("2026-06-08 13:30", tz="UTC"), pd.Timestamp("2026-06-08 20:00", tz="UTC")

PARAMS = dict(
    entry_interval=INTERVAL, volume_spike_multiple=1.5,
    require_retest=True, retest_tolerance=0.002, retest_lookback_bars=4,
    require_sustained_volume=True, sustained_rvol_floor=1.0, sustain_lookback_bars=5,
)

df = fetch_ohlcv_df(SYM, interval=INTERVAL, period="1d")
c = {x.lower(): x for x in df.columns}
O, H, L, C = c["open"], c["high"], c["low"], c["close"]
n = len(df)
idx = df.index

def _rth(ts) -> bool:
    try:
        return RTH_START <= ts <= RTH_END
    except Exception:
        return True

print(f"=== {SYM} {INTERVAL}: {n} bars  {idx[0]} -> {idx[-1]} ===")
print(f"day range: low ${df[L].min():.2f}  high ${df[H].max():.2f}  last ${df[C].iloc[-1]:.2f}")
print(f"RTH bars (entries allowed): {sum(_rth(t) for t in idx)} of {n}\n")

trades = []
signals_total = 0
signals_blocked_premkt = 0
i = 10
while i < n - 1:
    ok, reason, dbg = pullback_break_confirmation(df.iloc[: i + 1], **PARAMS)
    if not ok:
        i += 1
        continue
    signals_total += 1
    sig_ts = idx[i]
    # E3: equities enter only during RTH; a premarket break is observed but not entered.
    if not _rth(idx[i + 1]):
        signals_blocked_premkt += 1
        i += 1
        continue
    entry_idx = i + 1
    entry = float(df[O].iloc[entry_idx])          # realistic fill: next-bar open
    stop = float(dbg.get("pullback_low") or 0.0)
    brk = dbg.get("pullback_high")
    brk = float(brk) if brk else None
    if not (stop > 0 and stop < entry):
        i += 1
        continue
    risk = entry - stop
    target = entry + RR * risk

    scaled = False
    bal_stop = stop
    runner_high = entry
    scale_px = None
    j = entry_idx
    exit_px = exit_reason = exit_ts = None
    while j < n:
        bh, bl, bc = float(df[H].iloc[j]), float(df[L].iloc[j]), float(df[C].iloc[j])
        held = j - entry_idx
        # #2 breakout-or-bailout (early window, pre-scale)
        if not scaled and brk and held <= BAILOUT_BARS and bl < brk * (1 - BAILOUT_BUF):
            exit_px, exit_reason, exit_ts = min(bc, brk * (1 - BAILOUT_BUF)), "breakout_bailout", idx[j]
            break
        # stop (structural pre-scale; breakeven/trail post-scale)
        if bl <= bal_stop:
            exit_px = bal_stop
            exit_reason = "stop" if not scaled else ("breakeven" if abs(bal_stop - entry) < 1e-9 else "trail")
            exit_ts = idx[j]
            break
        # 2:1 target -> scale half, move balance to breakeven, then trail
        if not scaled and bh >= target:
            scaled, scale_px, bal_stop = True, target, entry
            runner_high = max(runner_high, bh)
        if scaled:
            runner_high = max(runner_high, bh)
            bal_stop = max(entry, runner_high - risk)   # ~1R chandelier trail on the runner
        j += 1
    if exit_px is None:
        exit_px, exit_reason, exit_ts = float(df[C].iloc[-1]), "eod", idx[-1]

    if scaled:
        pnl_r = SCALE_FRAC * (scale_px - entry) / risk + (1 - SCALE_FRAC) * (exit_px - entry) / risk
        pnl_pct = SCALE_FRAC * (scale_px - entry) / entry + (1 - SCALE_FRAC) * (exit_px - entry) / entry
    else:
        pnl_r = (exit_px - entry) / risk
        pnl_pct = (exit_px - entry) / entry
    trades.append(dict(
        sig=sig_ts, entry_ts=idx[entry_idx], entry=entry, stop=stop, target=target, brk=brk,
        exit_ts=exit_ts, exit=exit_px, reason=exit_reason, scaled=scaled, scale_px=scale_px,
        pnl_r=pnl_r, pnl_pct=pnl_pct,
    ))
    i = j + 1  # resume after the exit bar

print(f"pullback-break signals: {signals_total}  (blocked premarket/RTH: {signals_blocked_premkt})")
print(f"trades taken: {len(trades)}\n")
for k, t in enumerate(trades, 1):
    print(f"[{k}] ENTRY {t['entry_ts']:%H:%M} @ ${t['entry']:.3f}  stop ${t['stop']:.3f} (risk ${t['entry']-t['stop']:.3f})  tgt ${t['target']:.3f}  brk ${t['brk']}")
    sc = f" scaled@${t['scale_px']:.3f}" if t['scaled'] else ""
    print(f"     EXIT  {t['exit_ts']:%H:%M} @ ${t['exit']:.3f}  [{t['reason']}]{sc}  ->  {t['pnl_r']:+.2f}R  ({t['pnl_pct']*100:+.1f}%)")

if trades:
    tot_r = sum(t["pnl_r"] for t in trades)
    wins = [t for t in trades if t["pnl_r"] > 0]
    print(f"\n=== SUMMARY ===")
    print(f"net: {tot_r:+.2f}R   win rate: {len(wins)}/{len(trades)} = {100*len(wins)/len(trades):.0f}%")
    print(f"avg win: {(sum(t['pnl_r'] for t in wins)/len(wins)) if wins else 0:+.2f}R   "
          f"avg loss: {(sum(t['pnl_r'] for t in trades if t['pnl_r']<=0)/max(1,len(trades)-len(wins))):+.2f}R")
    print(f"(at 1% equity risk/trade, net {tot_r:+.2f}R ~= {tot_r:+.1f}% of equity for the day)")
else:
    print("\nNO TRADES TAKEN.")
