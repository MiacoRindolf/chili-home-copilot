"""REPLAY v2 — the high-fidelity momentum-lane replay (the permanent improvement loop).

Built 2026-06-10 after proving the v1 proxy replay is optimistic (PAVS real spread
317bps vs 53bps proxy). v2 removes the four optimism sources, in order of impact:

  1. REAL SPREADS — joins `momentum_nbbo_spread_tape` (1-min consolidated NBBO the
     live system records all RTH, 694 symbols on 06-10). Entry cost, the spread gate,
     and every exit price use the REAL bid/ask at that minute. A symbol/minute with
     no tape coverage falls back to the v1 dollar-volume proxy and the trade is
     LABELED fidelity=proxy (excluded from the headline number).
  2. LIMIT-TOUCH FILLS — the marketable limit at the tape ask only FILLS if the next
     bar actually trades through it (bar low <= limit); quantity is capped at
     PARTICIPATION_CAP of that bar's volume (partial fills are real). No touch ->
     cancelled, no trade (the live ack-timeout outcome).
  3. HALT MASKING — a >HALT_GAP_MIN gap in a symbol's tape during RTH = a halt
     window (same observable the live #569 detection uses). No entries inside or
     within RESUME_COOLDOWN_MIN after; a stop breached inside a window exits at the
     FIRST post-resume tape bid (the gap-down reality), not the stop price.
  4. AS-OF SELECTION — no EOD lookahead. The candidate universe is the tape's own
     symbol set (what the live scanner was actually watching); qualification
     (price band / $vol / |change|) and the liquidity-biased score use only data
     observed UP TO each decision minute. A rolling 10-slot armed set with 30-min
     no-trigger reaps mirrors the live cadence.

Run:  PYTHONPATH=<worktree> DATABASE_URL=postgres://...chili  python scripts/_replay_v2.py 2026-06-10
"""
from __future__ import annotations

import bisect
import math
import os
import sys
import warnings
from collections import defaultdict
from datetime import datetime, timedelta

warnings.filterwarnings("ignore")
import pandas as pd  # noqa: E402

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text  # noqa: E402

from app.db import SessionLocal  # noqa: E402
from app.services.trading.indicator_core import compute_atr  # noqa: E402
from app.services.trading.market_data import fetch_ohlcv_df  # noqa: E402
from app.services.trading.momentum_neural.entry_gates import momentum_pullback_trigger  # noqa: E402
from app.services.trading.momentum_neural.paper_execution import (  # noqa: E402
    effective_stop_atr_pct, runner_trail_stop, scale_out_fraction, stop_target_prices,
    structural_or_vol_floored_atr_pct,
)
from app.services.trading.momentum_neural.ross_momentum import (  # noqa: E402
    ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED, score_universe,
)

# ── live-lane parameters (mirror the live config; same as v1 for comparability) ──
STOP_ATR_MULT, REWARD_RISK, SCALE_FRAC = 0.60, 2.0, scale_out_fraction()
BASIS_USD = 22551.0
RISK_PER_TRADE_USD = BASIS_USD * 0.01
NOTIONAL_CAP_USD = BASIS_USD * 0.15
LIQ_FRACTION = 0.01            # liquidity cap: ≤1% of day-so-far $-volume (risk_policy)
MAX_SLOTS = 10
DAILY_LOSS_CAP_USD = BASIS_USD * 0.05
GIVEBACK_FRAC = 0.5
GATE_MOVE_FRAC = 0.5           # spread gate: spread ≤ 0.5 × expected move (adaptive)
SPREAD_ABS_CAP_BPS = 300.0     # live absolute cap
REAP_MIN = 30                  # armed, no trigger fired in 30 min -> slot released
# fill realism
PARTICIPATION_CAP = 0.10       # ≤10% of the touch-bar volume
FILL_WINDOW_BARS = 2           # the limit rests ≤2 bars (~10 min) before cancel
# halt mask (same observable as live #569: sustained quote silence)
HALT_GAP_MIN = 3.0
RESUME_COOLDOWN_MIN = 2.0      # live cooldown is 120s
# as-of qualification (the live Ross band)
PX_MIN, PX_MAX = 1.0, 20.0
MIN_DVOL_USD = 1_000_000.0
MIN_ABS_CHG = 5.0
RTH_START_MIN, RTH_END_MIN = 13 * 60 + 30, 20 * 60  # UTC minutes (09:30–16:00 ET)


def _rth_min(ts) -> int:
    return ts.hour * 60 + ts.minute


def _aware(ts):
    """Coerce any pandas/python timestamp to an AWARE-UTC python datetime."""
    from datetime import timezone as _tz
    t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    return t.replace(tzinfo=_tz.utc) if t.tzinfo is None else t.astimezone(_tz.utc)


# ── tape: real 1-min NBBO per symbol ──────────────────────────────────────────
class Tape:
    def __init__(self, date: str):
        self.by_sym: dict[str, list[tuple[datetime, float, float, float, float]]] = defaultdict(list)
        db = SessionLocal()
        try:
            rows = db.execute(text(
                "SELECT symbol, observed_at, bid, ask, spread_bps, day_volume "
                "FROM momentum_nbbo_spread_tape "
                "WHERE observed_at >= :lo AND observed_at < :hi AND bid > 0 AND ask > 0 "
                "ORDER BY symbol, observed_at"),
                {"lo": f"{date} 13:00:00", "hi": f"{date} 20:10:00"},
            ).fetchall()
        finally:
            db.rollback(); db.close()
        from datetime import timezone as _tz
        for sym, ts, bid, ask, sbps, dvol in rows:
            # normalize EVERYTHING to AWARE UTC (the 5m bars index is tz-aware UTC)
            ts = ts.replace(tzinfo=_tz.utc) if getattr(ts, "tzinfo", None) is None else ts.astimezone(_tz.utc)
            self.by_sym[str(sym)].append((ts, float(bid), float(ask), float(sbps or 0), float(dvol or 0)))
        self._times: dict[str, list[datetime]] = {s: [r[0] for r in v] for s, v in self.by_sym.items()}
        # halt windows per symbol: gaps > HALT_GAP_MIN inside RTH
        self.halts: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        for s, times in self._times.items():
            for a, b in zip(times, times[1:]):
                if (b - a).total_seconds() / 60.0 > HALT_GAP_MIN and _rth_min(a) >= RTH_START_MIN:
                    self.halts[s].append((a, b))

    def symbols(self) -> list[str]:
        return list(self.by_sym.keys())

    def at(self, sym: str, ts) -> tuple[float, float, float, float] | None:
        """(bid, ask, spread_bps, day_volume) at-or-before ts (≤5 min stale max)."""
        times = self._times.get(sym)
        if not times:
            return None
        t = _aware(ts)
        i = bisect.bisect_right(times, t) - 1
        if i < 0:
            return None
        row = self.by_sym[sym][i]
        if t - row[0] > timedelta(minutes=5):
            return None  # stale (likely inside a halt) — caller treats as no-quote
        return row[1], row[2], row[3], row[4]

    def in_halt_or_cooldown(self, sym: str, ts) -> bool:
        t = _aware(ts)
        for a, b in self.halts.get(sym, []):
            if a <= t < b + timedelta(minutes=RESUME_COOLDOWN_MIN):
                return True
        return False

    def first_after(self, sym: str, ts) -> tuple[datetime, float] | None:
        """(time, bid) of the first tape sample AFTER ts — the resume-exit price."""
        times = self._times.get(sym)
        if not times:
            return None
        t = _aware(ts)
        i = bisect.bisect_right(times, t)
        if i >= len(times):
            return None
        row = self.by_sym[sym][i]
        return row[0], row[1]


# ── bars (5m) + as-of helpers ─────────────────────────────────────────────────
_bars_cache: dict[str, object] = {}


def bars(sym: str):
    if sym not in _bars_cache:
        try:
            _bars_cache[sym] = fetch_ohlcv_df(sym, interval="5m", period="1mo")
        except Exception:
            _bars_cache[sym] = None
    return _bars_cache[sym]


def day_frame(sym: str, date: str):
    df = bars(sym)
    if df is None or len(df) == 0:
        return None, None
    c = {x.lower(): x for x in df.columns}
    sel = df[[t.strftime("%Y-%m-%d") == date for t in df.index]]
    return (sel, c) if len(sel) >= 14 else (None, None)


def avg_daily_vol_before(sym: str, date: str) -> float | None:
    df = bars(sym)
    if df is None or len(df) == 0:
        return None
    c = {x.lower(): x for x in df.columns}
    vol = df[c["volume"]].astype(float)
    days = pd.Series([t.strftime("%Y-%m-%d") for t in df.index], index=df.index)
    by_day = vol.groupby(days.values).sum()
    prior = by_day[by_day.index < date]
    prior = prior[prior > 0]
    return float(prior.mean()) if len(prior) else None


def main(date: str) -> None:
    print(f"=== REPLAY v2 (real spreads + limit-touch fills + halt mask + as-of selection) — {date} ===")
    tape = Tape(date)
    syms = tape.symbols()
    print(f"tape: {len(syms)} symbols | halt windows: {sum(len(v) for v in tape.halts.values())} on {sum(1 for v in tape.halts.values() if v)} symbols")
    if not syms:
        print("NO TAPE for this date — v2 needs the recorded NBBO; use v1 (labeled optimistic) instead.")
        return

    # ── as-of qualification timeline from the tape itself (no EOD lookahead) ──
    # open price ≈ first tape mid; change/dvol as-of each minute from tape rows.
    qualify_at: dict[str, datetime] = {}
    open_px: dict[str, float] = {}
    for s in syms:
        rows = tape.by_sym[s]
        o = (rows[0][1] + rows[0][2]) / 2.0
        if o <= 0:
            continue
        open_px[s] = o
        for ts, bid, ask, sbps, dvol in rows:
            mid = (bid + ask) / 2.0
            if not (PX_MIN <= mid <= PX_MAX):
                continue
            if mid * dvol < MIN_DVOL_USD:
                continue
            if abs((mid - o) / o * 100.0) < MIN_ABS_CHG:
                continue
            qualify_at[s] = ts
            break
    cand = sorted(qualify_at, key=lambda s: qualify_at[s])
    print(f"as-of qualified candidates: {len(cand)} (first: {', '.join(cand[:8])}...)")

    # RVOL history only for qualified names (bounded fetch set)
    adv: dict[str, float | None] = {s: avg_daily_vol_before(s, date) for s in cand}

    # ── rolling simulation over RTH minutes ──────────────────────────────────
    armed: dict[str, dict] = {}      # sym -> {since, last_trigger_check}
    trades: list[dict] = []
    open_pos: dict[str, dict] = {}
    cum_usd, peak = 0.0, 0.0
    halted_day: str | None = None
    day_grid = pd.date_range(f"{date} 13:30:00", f"{date} 19:55:00", freq="5min", tz="UTC")

    def asof_score_rank(now) -> list[str]:
        sigs = {}
        for s in cand:
            if qualify_at[s] > _aware(now):
                continue
            q = tape.at(s, now)
            if q is None:
                continue
            bid, ask, sbps, dvol = q
            mid = (bid + ask) / 2.0
            sig = {"daily_change_pct": (mid - open_px[s]) / open_px[s] * 100.0,
                   "dollar_volume": mid * dvol}
            if adv.get(s):
                sig["rvol"] = dvol / adv[s]
            sigs[s] = sig
        if not sigs:
            return []
        scored = score_universe(sigs, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
        return [r.symbol for r in sorted(scored.values(), key=lambda r: r.rank)]

    def manage_open(now_idx, now):
        nonlocal cum_usd, peak, halted_day
        for s in list(open_pos):
            p = open_pos[s]
            df, c = p["df"], p["c"]
            if now_idx not in df.index:
                continue
            row = df.loc[now_idx]
            bh, bl = float(row[c["high"]]), float(row[c["low"]])
            q = tape.at(s, now)
            tape_bid = q[0] if q else None
            exit_px, why = None, None
            if tape.in_halt_or_cooldown(s, now):
                if bl <= p["stop"]:
                    nxt = tape.first_after(s, now)          # resume gap reality
                    if nxt:
                        exit_px, why = nxt[1], "stop_through_halt_resume"
            else:
                if bl <= p["stop"]:
                    exit_px = min(p["stop"], tape_bid) if tape_bid else p["stop"]
                    why = "stop"
                elif not p["scaled"] and bh >= p["target"]:
                    p["scaled"] = True
                    px = max(p["target"], tape_bid) if tape_bid else p["target"]
                    part = p["qty"] * SCALE_FRAC
                    cum_usd += (px - p["entry"]) * part
                    p["qty"] -= part
                    p["stop"] = p["entry"]                   # breakeven floor
            if p["scaled"]:
                p["hwm"] = max(p["hwm"], bh)
                p["stop"] = runner_trail_stop(high_water_mark=p["hwm"], atr_pct=p["atrp"],
                                              stop_atr_mult=STOP_ATR_MULT, breakeven_floor=p["entry"],
                                              current_stop=p["stop"], side_long=True)
            if exit_px is not None:
                pnl = (exit_px - p["entry"]) * p["qty"]
                cum_usd += pnl
                peak = max(peak, cum_usd)
                trades.append({**p["meta"], "exit": exit_px, "why": why,
                               "usd": round(pnl + p.get("scale_usd", 0.0), 0)})
                del open_pos[s]
                if halted_day is None and cum_usd <= -DAILY_LOSS_CAP_USD:
                    halted_day = "daily_loss"
                elif halted_day is None and peak >= DAILY_LOSS_CAP_USD and cum_usd <= peak * (1 - GIVEBACK_FRAC):
                    halted_day = "giveback"

    fidelity_proxy_trades = 0
    for now in day_grid:
        manage_open(now, now)
        if halted_day:
            continue
        # reap stale arms + refill slots from the as-of ranking
        for s in list(armed):
            if (now - armed[s]["since"]).total_seconds() / 60.0 > REAP_MIN:
                del armed[s]
        ranked = asof_score_rank(now)
        for s in ranked:
            if len(armed) >= MAX_SLOTS:
                break
            if s in armed or s in open_pos:
                continue
            armed[s] = {"since": now}
        # entry checks for armed names
        for s in list(armed):
            if s in open_pos or tape.in_halt_or_cooldown(s, now):
                continue
            df, c = day_frame(s, date)
            if df is None:
                continue
            upto = df[df.index <= now]
            if len(upto) < 12 or now not in df.index:
                continue
            ok, _, dbg = momentum_pullback_trigger(upto, entry_interval="5m")
            if not ok:
                continue
            armed[s]["since"] = now                       # trigger activity resets the reap
            q = tape.at(s, now)
            if q is None:
                continue                                  # no live quote = no entry (stale gate)
            bid, ask, sbps, dvol = q
            O, H, L, C, V = (c[k] for k in ("open", "high", "low", "close", "volume"))
            atr = compute_atr(upto[H].astype(float), upto[L].astype(float), upto[C].astype(float))
            mid = (bid + ask) / 2.0
            atrp = float(atr.iloc[-1]) / mid if (mid > 0 and pd.notna(atr.iloc[-1])) else 0.0
            move_bps = atrp * 10_000.0
            # the REAL spread gate (adaptive + absolute cap), on the REAL spread
            if move_bps <= 0 or sbps > min(GATE_MOVE_FRAC * move_bps, SPREAD_ABS_CAP_BPS):
                continue
            # limit-touch fill: marketable limit at the REAL ask
            limit = ask
            later = df[df.index > now]
            fill_px, fill_bar, fill_frac = None, None, 1.0
            for k in range(min(FILL_WINDOW_BARS, len(later))):
                b = later.iloc[k]
                if float(b[L]) <= limit:
                    fill_px = limit
                    fill_bar = later.index[k]
                    pblow = dbg.get("pullback_low")
                    eff = effective_stop_atr_pct(atrp, move_bps, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
                    eff, _ = structural_or_vol_floored_atr_pct(
                        vol_floored_atr_pct=eff, structural_stop_price=float(pblow) if pblow else None,
                        entry_price=fill_px, stop_atr_mult=STOP_ATR_MULT)
                    stop, target = stop_target_prices(fill_px, atr_pct=eff, side_long=True,
                                                      stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)
                    if not (0 < stop < fill_px):
                        fill_px = None
                        break
                    spct = (fill_px - stop) / fill_px
                    max_notional = min(NOTIONAL_CAP_USD, LIQ_FRACTION * mid * dvol)
                    want_qty = min(RISK_PER_TRADE_USD / max(fill_px - stop, 1e-9), max_notional / fill_px)
                    cap_qty = PARTICIPATION_CAP * float(b[V])
                    qty = min(want_qty, cap_qty)
                    if qty <= 0:
                        fill_px = None
                        break
                    fill_frac = qty / want_qty if want_qty > 0 else 1.0
                    open_pos[s] = {
                        "entry": fill_px, "qty": qty, "stop": stop, "target": target,
                        "hwm": fill_px, "atrp": eff, "scaled": False, "df": df, "c": c,
                        "meta": {"sym": s, "t": str(fill_bar)[11:16], "entry": round(fill_px, 4),
                                 "qty": round(qty, 0), "spread_bps": round(sbps, 0),
                                 "partial": round(fill_frac, 2), "fidelity": "tape"},
                    }
                    break
            # no touch in the window -> cancelled, walang trade (ack-timeout reality)

    # close anything still open at EOD at the last tape bid
    for s in list(open_pos):
        p = open_pos[s]
        q = tape.by_sym[s][-1]
        pnl = (q[1] - p["entry"]) * p["qty"]
        cum_usd += pnl
        trades.append({**p["meta"], "exit": q[1], "why": "eod", "usd": round(pnl, 0)})
        del open_pos[s]

    wins = sum(1 for t in trades if t["usd"] > 0)
    print(f"\nTRADES ({len(trades)}; {wins}W/{len(trades)-wins}L; halted={halted_day}):")
    for t in sorted(trades, key=lambda z: z["t"]):
        print("  %s %-6s entry=%.3f exit=%.3f qty=%-6.0f spread=%3.0fbps partial=%.2f %-26s $%+8.0f" % (
            t["t"], t["sym"], t["entry"], t["exit"], t["qty"], t["spread_bps"], t["partial"], t["why"], t["usd"]))
    print(f"\nDAY TOTAL (v2, real-spread fidelity): ${cum_usd:+,.0f}")
    print("v1 (proxy) benchmark same day: +$1,669 / 3 fills — the v2-vs-v1 gap IS the optimism that was removed.")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else "2026-06-10")
