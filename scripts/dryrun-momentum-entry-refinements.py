"""Dry-run validator for the three Ross RECENT entry-quality refinements.

Replays ``pullback_break_confirmation`` WALK-FORWARD over recent OHLCV (each bar
treated as "current", exactly as the live runner evaluates per tick) and compares,
without touching live behavior:

  * #1 break-retest vs raw first-break   — fire-rate + per-fire outcome quality
  * #3 sustaining-volume gate            — fire-rate + per-fire outcome quality
  * #2 breakout-or-bailout fast exit     — realized P/L with vs without the fast bail
                                           over the SAME baseline fires (avoided loss)

Per-fire outcome uses the lane's own risk model (structural pullback-low stop, 2:1
target, fixed forward horizon) so win-rate / avg-return are apples-to-apples.

Read-only: fetches OHLCV via the normal market-data path; no DB writes, no orders.

Run (operator):
    set DATABASE_URL=postgresql://chili:chili@localhost:5433/chili
    python scripts/dryrun-momentum-entry-refinements.py
    python scripts/dryrun-momentum-entry-refinements.py --symbols BTC-USD,SOL-USD --interval 5m
"""
from __future__ import annotations

import argparse
import logging
import sys
from dataclasses import dataclass, field
from pathlib import Path

# Allow running as `python scripts/dryrun-...py` from the project root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
log = logging.getLogger("dryrun-entry-refinements")

# Crypto-first basket (the lane is 24/7 crypto). Override with --symbols.
DEFAULT_SYMBOLS = [
    "BTC-USD", "ETH-USD", "SOL-USD", "DOGE-USD", "AVAX-USD",
    "LINK-USD", "XRP-USD", "ADA-USD", "LTC-USD", "BCH-USD",
]
# Evaluate at most this many of the most-recent bars per symbol (keeps it quick;
# "recent" is the point — these refinements are about the live entry tick).
MAX_EVAL_BARS = 500
# Forward horizon for the per-fire outcome sim (bars after entry).
HORIZON_BARS = 24


@dataclass
class VariantStat:
    name: str
    fires: int = 0
    wins: int = 0
    rets: list = field(default_factory=list)

    def add(self, ret_pct: float) -> None:
        self.fires += 1
        self.rets.append(ret_pct)
        if ret_pct > 0:
            self.wins += 1

    def summary(self) -> str:
        if self.fires == 0:
            return f"{self.name:<14} fires=0"
        import statistics
        wr = self.wins / self.fires
        avg = statistics.mean(self.rets)
        med = statistics.median(self.rets)
        return (f"{self.name:<14} fires={self.fires:<4} win_rate={wr:6.1%} "
                f"avg_ret={avg*100:+6.2f}%  med_ret={med*100:+6.2f}%")


def _forward_outcome(high, low, close, entry_idx, entry, stop, *, horizon, target_R=2.0):
    """Lane risk-model sim from entry_idx: structural stop (low<=stop), 2:1 target
    (high>=target), else last close in the horizon. Returns return fraction."""
    if stop is None or stop <= 0 or entry <= 0 or stop >= entry:
        end = min(entry_idx + horizon, len(close) - 1)
        return (float(close.iloc[end]) - entry) / entry
    target = entry + target_R * (entry - stop)
    end = min(entry_idx + horizon, len(close) - 1)
    for j in range(entry_idx + 1, end + 1):
        lo = float(low.iloc[j]); hi = float(high.iloc[j])
        if lo <= stop:
            return (stop - entry) / entry           # stopped (intrabar)
        if hi >= target:
            return (target - entry) / entry         # target (intrabar)
    return (float(close.iloc[end]) - entry) / entry  # horizon close


def _bailout_outcome(high, low, close, entry_idx, entry, stop, level, *,
                     horizon, window_bars, buffer_pct):
    """Same as _forward_outcome but ALSO fast-bails (exit at that bar close) the first
    time price CLOSES back below the breakout level within the early window."""
    if level is None or level <= 0:
        return _forward_outcome(high, low, close, entry_idx, entry, stop, horizon=horizon), False
    bail_line = level * (1.0 - buffer_pct)
    target = entry + 2.0 * (entry - stop) if (stop and stop < entry) else None
    end = min(entry_idx + horizon, len(close) - 1)
    win_end = min(entry_idx + window_bars, end)
    for j in range(entry_idx + 1, end + 1):
        lo = float(low.iloc[j]); hi = float(high.iloc[j]); cl = float(close.iloc[j])
        if stop and stop < entry and lo <= stop:
            return (stop - entry) / entry, False
        if target is not None and hi >= target:
            return (target - entry) / entry, False
        if j <= win_end and cl < bail_line:
            return (cl - entry) / entry, True       # breakout failed to hold -> fast bail
    return (float(close.iloc[end]) - entry) / entry, False


def run_symbol(df, *, interval, retest_tol, retest_lookback, vol_spike,
               sustain_floor, sustain_lookback, bail_window_bars, bail_buffer):
    from app.services.trading.momentum_neural.entry_gates import pullback_break_confirmation

    high = df["High"].astype(float); low = df["Low"].astype(float); close = df["Close"].astype(float)
    n = len(df)
    start = max(12, n - MAX_EVAL_BARS)

    variants = {
        "baseline": VariantStat("baseline"),
        "+retest": VariantStat("+retest"),
        "+sustain": VariantStat("+sustain"),
        "+both": VariantStat("+both"),
    }
    prev_fire = {k: False for k in variants}
    # Breakout-or-bailout comparison over BASELINE fires.
    bail = {"n": 0, "triggered": 0, "pl_struct": 0.0, "pl_bail": 0.0, "avoided": 0.0}

    def _eval(sub, **kw):
        return pullback_break_confirmation(
            sub, entry_interval=interval, volume_spike_multiple=vol_spike,
            retest_tolerance=retest_tol, retest_lookback_bars=retest_lookback,
            sustained_rvol_floor=sustain_floor, sustain_lookback_bars=sustain_lookback,
            **kw,
        )

    for i in range(start, n):
        sub = df.iloc[: i + 1]
        configs = {
            "baseline": dict(require_retest=False, require_sustained_volume=False),
            "+retest": dict(require_retest=True, require_sustained_volume=False),
            "+sustain": dict(require_retest=False, require_sustained_volume=True),
            "+both": dict(require_retest=True, require_sustained_volume=True),
        }
        for name, cfg in configs.items():
            ok, reason, dbg = _eval(sub, **cfg)
            rising = ok and not prev_fire[name]
            prev_fire[name] = ok
            if not rising:
                continue
            entry = float(close.iloc[i])
            stop = dbg.get("pullback_low")
            ret = _forward_outcome(high, low, close, i, entry, stop, horizon=HORIZON_BARS)
            variants[name].add(ret)
            if name == "baseline":
                level = dbg.get("pullback_high")
                pl_s = _forward_outcome(high, low, close, i, entry, stop, horizon=HORIZON_BARS)
                pl_b, trig = _bailout_outcome(
                    high, low, close, i, entry, stop, level,
                    horizon=HORIZON_BARS, window_bars=bail_window_bars, buffer_pct=bail_buffer,
                )
                bail["n"] += 1
                bail["pl_struct"] += pl_s
                bail["pl_bail"] += pl_b
                if trig:
                    bail["triggered"] += 1
                    bail["avoided"] += (pl_b - pl_s)
    return variants, bail


def main() -> int:
    ap = argparse.ArgumentParser(description="Dry-run the 3 momentum entry-quality refinements over recent OHLCV.")
    ap.add_argument("--symbols", default=",".join(DEFAULT_SYMBOLS))
    ap.add_argument("--interval", default="5m")
    ap.add_argument("--period", default="5d")
    args = ap.parse_args()

    from app.config import settings
    from app.services.trading.market_data import fetch_ohlcv_df

    retest_tol = float(getattr(settings, "chili_momentum_pullback_retest_tolerance", 0.002))
    retest_lb = int(getattr(settings, "chili_momentum_pullback_retest_lookback_bars", 4))
    vol_spike = float(getattr(settings, "chili_momentum_pullback_volume_spike_multiple", 1.5))
    sustain_floor = float(getattr(settings, "chili_momentum_entry_sustained_rvol_floor", 1.0))
    sustain_lb = int(getattr(settings, "chili_momentum_entry_sustain_lookback_bars", 5))
    bail_bars = float(getattr(settings, "chili_momentum_breakout_bailout_max_bars", 2.0))
    bail_buffer = float(getattr(settings, "chili_momentum_breakout_bailout_buffer_pct", 0.001))

    symbols = [s.strip().upper() for s in args.symbols.split(",") if s.strip()]
    agg = {k: VariantStat(k) for k in ("baseline", "+retest", "+sustain", "+both")}
    bail_agg = {"n": 0, "triggered": 0, "pl_struct": 0.0, "pl_bail": 0.0, "avoided": 0.0}

    log.info("knobs: retest_tol=%s retest_lb=%s vol_spike=%s sustain_floor=%s sustain_lb=%s bail_bars=%s bail_buf=%s",
             retest_tol, retest_lb, vol_spike, sustain_floor, sustain_lb, bail_bars, bail_buffer)
    for sym in symbols:
        try:
            df = fetch_ohlcv_df(sym, interval=args.interval, period=args.period)
        except Exception as e:
            log.warning("%s fetch failed: %s", sym, e)
            continue
        if df is None or df.empty or len(df) < 40:
            log.warning("%s insufficient bars (%s)", sym, 0 if df is None else len(df))
            continue
        variants, bail = run_symbol(
            df, interval=args.interval, retest_tol=retest_tol, retest_lookback=retest_lb,
            vol_spike=vol_spike, sustain_floor=sustain_floor, sustain_lookback=sustain_lb,
            bail_window_bars=int(round(bail_bars)), bail_buffer=bail_buffer,
        )
        log.info("%-9s bars=%-5d base=%-3d retest=%-3d sustain=%-3d both=%-3d",
                 sym, len(df), variants["baseline"].fires, variants["+retest"].fires,
                 variants["+sustain"].fires, variants["+both"].fires)
        for k in agg:
            agg[k].fires += variants[k].fires
            agg[k].wins += variants[k].wins
            agg[k].rets.extend(variants[k].rets)
        for k in bail_agg:
            bail_agg[k] += bail[k]

    print("\n=== Fire-rate + per-fire outcome (lane risk model: structural stop, 2:1 target, %d-bar horizon) ===" % HORIZON_BARS)
    for k in ("baseline", "+retest", "+sustain", "+both"):
        print("  " + agg[k].summary())

    base = agg["baseline"]
    print("\n=== #1 break-retest vs raw + #3 sustaining-volume (fire-rate impact) ===")
    if base.fires:
        for k in ("+retest", "+sustain", "+both"):
            v = agg[k]
            pct = (v.fires / base.fires - 1.0) * 100 if base.fires else 0.0
            print(f"  {k:<10} fire-rate {pct:+5.0f}% vs baseline   ({base.fires} -> {v.fires})")

    print("\n=== #2 breakout-or-bailout (realized P/L over the SAME baseline fires) ===")
    n = bail_agg["n"]
    if n:
        print(f"  baseline fires simulated : {n}")
        print(f"  fast-bail triggered on   : {bail_agg['triggered']} ({bail_agg['triggered']/n:.0%})")
        print(f"  total return  no-bail     : {bail_agg['pl_struct']*100:+7.2f}%  (sum over fires)")
        print(f"  total return  with-bail   : {bail_agg['pl_bail']*100:+7.2f}%  (sum over fires)")
        print(f"  net would-have-avoided    : {bail_agg['avoided']*100:+7.2f}%  (with-bail minus no-bail)")
        print(f"  avg per triggered bail    : {(bail_agg['avoided']/bail_agg['triggered']*100) if bail_agg['triggered'] else 0:+7.2f}%")
    else:
        print("  no baseline fires in window")
    return 0


if __name__ == "__main__":
    sys.exit(main())
