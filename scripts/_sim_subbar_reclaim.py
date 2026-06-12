"""A/B the sub-bar reclaim tick-arm on TODAY's real 1m OHLCV (the live
trigger's own data source). Tick-cross approximation: an armed level counts
as ENTERED if the NEXT bar's high crosses it (a real tick crossed intra-bar);
entry price = the level. Score: forward close at +HORIZON bars, stop on bar
lows at pullback_low. Mid-fill ceiling, no fees — comparative, not absolute.
Usage: PYTHONPATH=. python scripts/_sim_subbar_reclaim.py SYM1,SYM2 ...
"""
import os
import sys

os.environ.setdefault("CHILI_PYTEST", "0")

from app.config import settings
from app.services.trading.market_data import fetch_ohlcv_df
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger

SYMS = (sys.argv[1].split(",") if len(sys.argv) > 1
        else ["BYAH", "DSY", "VSME", "CUPR", "UBXG", "ASBP", "EDHL", "GMM", "MASK", "AKAN"])
HORIZON = 10  # bars
WAIT_REASONS = ("waiting_for_reclaim_high", "waiting_for_break", "waiting_for_reclaim")


def run(sym, df, accel: bool):
    settings.chili_momentum_reclaim_tick_arm_after_first_hold = accel
    fires = []
    skip_until = -1
    for i in range(15, len(df) - 1):
        if i < skip_until:
            continue
        window = df.iloc[: i + 1]
        try:
            ok, reason, dbg = momentum_pullback_trigger(window, entry_interval="1m", symbol=sym)
        except Exception:
            continue
        level = dbg.get("pullback_high") if isinstance(dbg, dict) else None
        stop = dbg.get("pullback_low") if isinstance(dbg, dict) else None
        fired_at = None
        kind = None
        if ok and level:
            fired_at, kind = i, "bar"
        elif reason in WAIT_REASONS and level and float(df["High"].iloc[i + 1]) >= float(level):
            fired_at, kind = i + 1, "tick"
        if fired_at is None:
            continue
        lv, sp = float(level), float(stop or 0)
        fwd = df.iloc[fired_at + 1: fired_at + 1 + HORIZON]
        if fwd.empty:
            continue
        if sp > 0 and (fwd["Low"] <= sp).any():
            ret = (sp - lv) / lv
        else:
            ret = (float(fwd["Close"].iloc[-1]) - lv) / lv
        fires.append({"t": str(df.index[fired_at])[11:16], "kind": kind,
                      "ret_pct": round(ret * 100, 2)})
        skip_until = fired_at + HORIZON
    return fires


print(f"{'sym':6} {'mode':5} {'fires':5} {'wins':4} {'tot%':>8}  first fires")
for sym in SYMS:
    try:
        df = fetch_ohlcv_df(sym, interval="1m", period="1d")
    except Exception as e:
        print(f"{sym:6} fetch failed: {str(e)[:50]}")
        continue
    if df is None or len(df) < 30:
        print(f"{sym:6} (no/thin 1m data: {0 if df is None else len(df)} bars)")
        continue
    for accel in (False, True):
        res = run(sym, df, accel)
        tot = sum(r["ret_pct"] for r in res)
        wins = sum(1 for r in res if r["ret_pct"] > 0)
        label = "ON" if accel else "off"
        detail = " ".join(f"{r['t']}/{r['kind']}/{r['ret_pct']:+.1f}" for r in res[:6])
        print(f"{sym:6} {label:5} {len(res):5} {wins:4} {tot:+8.2f}  {detail}")
