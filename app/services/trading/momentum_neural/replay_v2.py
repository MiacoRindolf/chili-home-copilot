"""Replay v2 engine — high-fidelity momentum-lane replay, importable + UI-runnable.

The permanent improvement-loop benchmark (built 2026-06-10 after proving the proxy
replay optimistic: same day +$1,669 proxy vs −$575 at real fidelity). Four fidelity
sources, in order of impact:

  1. REAL SPREADS — joins ``momentum_nbbo_spread_tape`` (1-min consolidated NBBO the
     live system records all RTH). The spread gate, entry cost, and every exit price
     use the real bid/ask at that minute.
  2. LIMIT-TOUCH FILLS — a marketable limit at the tape ask fills only if a later bar
     trades through it (bar low <= limit), partial-filled at a participation cap of
     that bar's volume. No touch -> cancelled (the live ack-timeout outcome).
  3. HALT MASKING — a >3-min gap in a symbol's tape during RTH = a halt window (the
     same observable live #569 uses). No entries inside the window + resume cooldown;
     a stop breached inside a window exits at the first post-resume tape bid.
  4. AS-OF SELECTION — zero EOD lookahead: the candidate universe is the tape's own
     symbol set; qualification + the liquidity-biased score use only data observed up
     to each decision minute; rolling MAX_SLOTS armed set with no-trigger reaps.

``run_replay(date)`` returns a structured dict and persists it to
``REPLAY_RESULTS_DIR/<date>.json`` so the web UI (and the CLI) share one engine.
"""

from __future__ import annotations

import bisect
import json
import logging
import os
import warnings
from collections import defaultdict
from datetime import datetime, timedelta, timezone

warnings.filterwarnings("ignore")
import pandas as pd

from sqlalchemy import text

from ....db import SessionLocal
from ..indicator_core import compute_atr
from ..market_data import fetch_ohlcv_df
from .entry_gates import momentum_pullback_trigger
from .paper_execution import (
    effective_stop_atr_pct,
    runner_trail_stop,
    scale_out_fraction,
    stop_target_prices,
    structural_or_vol_floored_atr_pct,
)
from .ross_momentum import ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED, score_universe

logger = logging.getLogger(__name__)

# ── live-lane parameters (mirror the live config) ─────────────────────────────
STOP_ATR_MULT, REWARD_RISK = 0.60, 2.0
SCALE_FRAC = scale_out_fraction()
BASIS_USD = 22551.0
RISK_PER_TRADE_USD = BASIS_USD * 0.01
NOTIONAL_CAP_USD = BASIS_USD * 0.15
LIQ_FRACTION = 0.01
MAX_SLOTS = 10
DAILY_LOSS_CAP_USD = BASIS_USD * 0.05
GIVEBACK_FRAC = 0.5
GATE_MOVE_FRAC = 0.5
SPREAD_ABS_CAP_BPS = 300.0
REAP_MIN = 30
PARTICIPATION_CAP = 0.10
FILL_WINDOW_BARS = 2
HALT_GAP_MIN = 3.0
RESUME_COOLDOWN_MIN = 2.0
PX_MIN, PX_MAX = 1.0, 20.0
MIN_DVOL_USD = 1_000_000.0
MIN_ABS_CHG = 5.0
RTH_START_MIN = 13 * 60 + 30  # UTC minutes (09:30 ET)

REPLAY_RESULTS_DIR = os.environ.get("CHILI_REPLAY_RESULTS_DIR", "/app/data/replays")


def _aware(ts) -> datetime:
    t = ts.to_pydatetime() if hasattr(ts, "to_pydatetime") else ts
    return t.replace(tzinfo=timezone.utc) if t.tzinfo is None else t.astimezone(timezone.utc)


class Tape:
    """Real 1-min NBBO per symbol for one date, + halt windows from tape gaps."""

    def __init__(self, date: str):
        self.by_sym: dict[str, list[tuple[datetime, float, float, float, float]]] = defaultdict(list)
        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    "SELECT symbol, observed_at, bid, ask, spread_bps, day_volume "
                    "FROM momentum_nbbo_spread_tape "
                    "WHERE observed_at >= :lo AND observed_at < :hi AND bid > 0 AND ask > 0 "
                    "ORDER BY symbol, observed_at"
                ),
                {"lo": f"{date} 13:00:00", "hi": f"{date} 20:10:00"},
            ).fetchall()
        finally:
            db.rollback()
            db.close()
        for sym, ts, bid, ask, sbps, dvol in rows:
            ts = _aware(ts)
            self.by_sym[str(sym)].append((ts, float(bid), float(ask), float(sbps or 0), float(dvol or 0)))
        self._times: dict[str, list[datetime]] = {s: [r[0] for r in v] for s, v in self.by_sym.items()}
        self.halts: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        for s, times in self._times.items():
            for a, b in zip(times, times[1:]):
                if (b - a).total_seconds() / 60.0 > HALT_GAP_MIN and (a.hour * 60 + a.minute) >= RTH_START_MIN:
                    self.halts[s].append((a, b))

    def symbols(self) -> list[str]:
        return list(self.by_sym.keys())

    def at(self, sym: str, ts) -> tuple[float, float, float, float] | None:
        times = self._times.get(sym)
        if not times:
            return None
        t = _aware(ts)
        i = bisect.bisect_right(times, t) - 1
        if i < 0:
            return None
        row = self.by_sym[sym][i]
        if t - row[0] > timedelta(minutes=5):
            return None
        return row[1], row[2], row[3], row[4]

    def in_halt_or_cooldown(self, sym: str, ts) -> bool:
        t = _aware(ts)
        for a, b in self.halts.get(sym, []):
            if a <= t < b + timedelta(minutes=RESUME_COOLDOWN_MIN):
                return True
        return False

    def first_after(self, sym: str, ts) -> tuple[datetime, float] | None:
        times = self._times.get(sym)
        if not times:
            return None
        i = bisect.bisect_right(times, _aware(ts))
        if i >= len(times):
            return None
        row = self.by_sym[sym][i]
        return row[0], row[1]


def run_replay(date: str, *, persist: bool = True) -> dict:
    """Run the high-fidelity replay for ``date`` (YYYY-MM-DD). Returns the structured
    result dict; persists it to REPLAY_RESULTS_DIR/<date>.json when ``persist``."""
    started = datetime.now(timezone.utc)
    tape = Tape(date)
    syms = tape.symbols()
    result: dict = {
        "date": date,
        "engine": "v2",
        "ran_at_utc": started.isoformat(),
        "tape_symbols": len(syms),
        "halt_windows": sum(len(v) for v in tape.halts.values()),
        "halted_symbols": sum(1 for v in tape.halts.values() if v),
        "trades": [],
        "total_usd": 0.0,
        "wins": 0,
        "losses": 0,
        "day_halted": None,
        "candidates": 0,
        "error": None,
    }
    if not syms:
        result["error"] = "no_tape_for_date"
        if persist:
            _persist(result)
        return result

    bars_cache: dict[str, object] = {}

    def bars(sym: str):
        if sym not in bars_cache:
            try:
                bars_cache[sym] = fetch_ohlcv_df(sym, interval="5m", period="1mo")
            except Exception:
                bars_cache[sym] = None
        return bars_cache[sym]

    def day_frame(sym: str):
        df = bars(sym)
        if df is None or len(df) == 0:
            return None, None
        c = {x.lower(): x for x in df.columns}
        sel = df[[t.strftime("%Y-%m-%d") == date for t in df.index]]
        return (sel, c) if len(sel) >= 14 else (None, None)

    def avg_daily_vol_before(sym: str) -> float | None:
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

    # as-of qualification timeline from the tape itself (no EOD lookahead)
    qualify_at: dict[str, datetime] = {}
    open_px: dict[str, float] = {}
    for s in syms:
        rows = tape.by_sym[s]
        o = (rows[0][1] + rows[0][2]) / 2.0
        if o <= 0:
            continue
        open_px[s] = o
        for ts, bid, ask, _sbps, dvol in rows:
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
    result["candidates"] = len(cand)
    adv: dict[str, float | None] = {s: avg_daily_vol_before(s) for s in cand}

    armed: dict[str, dict] = {}
    trades: list[dict] = []
    open_pos: dict[str, dict] = {}
    state = {"cum": 0.0, "peak": 0.0, "halted": None}
    day_grid = pd.date_range(f"{date} 13:30:00", f"{date} 19:55:00", freq="5min", tz="UTC")

    def asof_rank(now) -> list[str]:
        sigs = {}
        for s in cand:
            if qualify_at[s] > _aware(now):
                continue
            q = tape.at(s, now)
            if q is None:
                continue
            bid, ask, _sbps, dvol = q
            mid = (bid + ask) / 2.0
            sig = {"daily_change_pct": (mid - open_px[s]) / open_px[s] * 100.0, "dollar_volume": mid * dvol}
            if adv.get(s):
                sig["rvol"] = dvol / adv[s]
            sigs[s] = sig
        if not sigs:
            return []
        scored = score_universe(sigs, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
        return [r.symbol for r in sorted(scored.values(), key=lambda r: r.rank)]

    def close_trade(s: str, p: dict, exit_px: float, why: str) -> None:
        pnl = (exit_px - p["entry"]) * p["qty"]
        state["cum"] += pnl
        state["peak"] = max(state["peak"], state["cum"])
        trades.append({**p["meta"], "exit": round(exit_px, 4), "why": why,
                       "usd": round(pnl + p.get("scale_usd", 0.0), 0)})
        del open_pos[s]
        if state["halted"] is None and state["cum"] <= -DAILY_LOSS_CAP_USD:
            state["halted"] = "daily_loss"
        elif state["halted"] is None and state["peak"] >= DAILY_LOSS_CAP_USD and state["cum"] <= state["peak"] * (1 - GIVEBACK_FRAC):
            state["halted"] = "giveback"

    def manage_open(now) -> None:
        for s in list(open_pos):
            p = open_pos[s]
            df, c = p["df"], p["c"]
            if now not in df.index:
                continue
            row = df.loc[now]
            bh, bl = float(row[c["high"]]), float(row[c["low"]])
            q = tape.at(s, now)
            tape_bid = q[0] if q else None
            if tape.in_halt_or_cooldown(s, now):
                if bl <= p["stop"]:
                    nxt = tape.first_after(s, now)
                    if nxt:
                        close_trade(s, p, nxt[1], "stop_through_halt_resume")
                continue
            if bl <= p["stop"]:
                px = min(p["stop"], tape_bid) if tape_bid else p["stop"]
                close_trade(s, p, px, "stop")
                continue
            if not p["scaled"] and bh >= p["target"]:
                p["scaled"] = True
                px = max(p["target"], tape_bid) if tape_bid else p["target"]
                part = p["qty"] * SCALE_FRAC
                p["scale_usd"] = (px - p["entry"]) * part
                state["cum"] += p["scale_usd"]
                p["qty"] -= part
                p["stop"] = p["entry"]
            if p["scaled"]:
                p["hwm"] = max(p["hwm"], bh)
                p["stop"] = runner_trail_stop(
                    high_water_mark=p["hwm"], atr_pct=p["atrp"], stop_atr_mult=STOP_ATR_MULT,
                    breakeven_floor=p["entry"], current_stop=p["stop"], side_long=True)

    for now in day_grid:
        manage_open(now)
        if state["halted"]:
            continue
        for s in list(armed):
            if (now - armed[s]["since"]).total_seconds() / 60.0 > REAP_MIN:
                del armed[s]
        for s in asof_rank(now):
            if len(armed) >= MAX_SLOTS:
                break
            if s not in armed and s not in open_pos:
                armed[s] = {"since": now}
        for s in list(armed):
            if s in open_pos or tape.in_halt_or_cooldown(s, now):
                continue
            df, c = day_frame(s)
            if df is None:
                continue
            upto = df[df.index <= now]
            if len(upto) < 12 or now not in df.index:
                continue
            ok, _, dbg = momentum_pullback_trigger(upto, entry_interval="5m")
            if not ok:
                continue
            armed[s]["since"] = now
            q = tape.at(s, now)
            if q is None:
                continue
            bid, ask, sbps, dvol = q
            O, H, L, C, V = (c[k] for k in ("open", "high", "low", "close", "volume"))
            atr = compute_atr(upto[H].astype(float), upto[L].astype(float), upto[C].astype(float))
            mid = (bid + ask) / 2.0
            atrp = float(atr.iloc[-1]) / mid if (mid > 0 and pd.notna(atr.iloc[-1])) else 0.0
            move_bps = atrp * 10_000.0
            if move_bps <= 0 or sbps > min(GATE_MOVE_FRAC * move_bps, SPREAD_ABS_CAP_BPS):
                continue
            limit = ask
            later = df[df.index > now]
            for k in range(min(FILL_WINDOW_BARS, len(later))):
                b = later.iloc[k]
                if float(b[L]) > limit:
                    continue
                fill_px = limit
                pblow = dbg.get("pullback_low")
                eff = effective_stop_atr_pct(atrp, move_bps, stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
                eff, _ = structural_or_vol_floored_atr_pct(
                    vol_floored_atr_pct=eff, structural_stop_price=float(pblow) if pblow else None,
                    entry_price=fill_px, stop_atr_mult=STOP_ATR_MULT)
                stop, target = stop_target_prices(
                    fill_px, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)
                if not (0 < stop < fill_px):
                    break
                max_notional = min(NOTIONAL_CAP_USD, LIQ_FRACTION * mid * dvol)
                want_qty = min(RISK_PER_TRADE_USD / max(fill_px - stop, 1e-9), max_notional / fill_px)
                qty = min(want_qty, PARTICIPATION_CAP * float(b[V]))
                if qty <= 0:
                    break
                open_pos[s] = {
                    "entry": fill_px, "qty": qty, "stop": stop, "target": target,
                    "hwm": fill_px, "atrp": eff, "scaled": False, "df": df, "c": c, "scale_usd": 0.0,
                    "meta": {"sym": s, "t": str(later.index[k])[11:16], "entry": round(fill_px, 4),
                             "qty": round(qty, 0), "spread_bps": round(sbps, 0),
                             "partial": round(qty / want_qty if want_qty > 0 else 1.0, 2),
                             "fidelity": "tape"},
                }
                break

    for s in list(open_pos):
        p = open_pos[s]
        last_bid = tape.by_sym[s][-1][1]
        close_trade(s, p, last_bid, "eod")

    result["trades"] = sorted(trades, key=lambda z: z["t"])
    result["total_usd"] = round(state["cum"], 0)
    result["wins"] = sum(1 for t in trades if t["usd"] > 0)
    result["losses"] = sum(1 for t in trades if t["usd"] <= 0)
    result["day_halted"] = state["halted"]
    result["duration_s"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    if persist:
        _persist(result)
    return result


def _persist(result: dict) -> None:
    try:
        os.makedirs(REPLAY_RESULTS_DIR, exist_ok=True)
        path = os.path.join(REPLAY_RESULTS_DIR, f"{result['date']}.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=1)
    except Exception:
        logger.warning("[replay_v2] persist failed for %s", result.get("date"), exc_info=True)


def list_results() -> list[dict]:
    """Summaries of persisted replay results, newest first."""
    out: list[dict] = []
    try:
        for name in os.listdir(REPLAY_RESULTS_DIR):
            if not name.endswith(".json"):
                continue
            try:
                with open(os.path.join(REPLAY_RESULTS_DIR, name), encoding="utf-8") as f:
                    r = json.load(f)
                out.append({k: r.get(k) for k in (
                    "date", "ran_at_utc", "total_usd", "wins", "losses",
                    "tape_symbols", "candidates", "halt_windows", "day_halted", "error")}
                    | {"n_trades": len(r.get("trades") or [])})
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return sorted(out, key=lambda r: r.get("date") or "", reverse=True)


def load_result(date: str) -> dict | None:
    try:
        with open(os.path.join(REPLAY_RESULTS_DIR, f"{date}.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
