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
        # Global sampler heartbeat: the sampler skips whole minutes for ALL symbols
        # (cadence jitter / scheduler restarts). A per-symbol gap is a real HALT only
        # if OTHER symbols were sampled during it — i.e. the sampler was alive but
        # this symbol had no quote. (06-10 false-positive audit: 4,740 "halts" on 450
        # symbols collapsed to the real per-symbol LULD halts once discriminated.)
        global_minutes: set[datetime] = set()
        for rows2 in self.by_sym.values():
            for r in rows2:
                global_minutes.add(r[0].replace(second=0, microsecond=0))
        self.halts: dict[str, list[tuple[datetime, datetime]]] = defaultdict(list)
        for s, times in self._times.items():
            for a, b in zip(times, times[1:]):
                if (b - a).total_seconds() / 60.0 <= HALT_GAP_MIN or (a.hour * 60 + a.minute) < RTH_START_MIN:
                    continue
                m = a.replace(second=0, microsecond=0) + timedelta(minutes=1)
                alive = 0
                while m < b:
                    if m in global_minutes:
                        alive += 1
                        if alive >= 2:
                            break
                    m += timedelta(minutes=1)
                gap_min = (b - a).total_seconds() / 60.0
                # LULD-scale classification: real halts run ~5-15 min. A multi-hour
                # per-symbol gap is the SAMPLER rotating its universe (the symbol fell
                # out of the sampled set), not a halt — those polluted the analysis
                # (operator 2026-06-10). Long gaps still block entries via quote
                # staleness in .at(); they are just not labeled/masked as halts.
                if alive >= 2 and gap_min <= 20.0:
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


def run_replay(date: str, *, persist: bool = True, armed_source: str = "asof") -> dict:
    """Run the high-fidelity replay for ``date`` (YYYY-MM-DD). Returns the structured
    result dict; persists it to REPLAY_RESULTS_DIR/<date>.json when ``persist``."""
    started = datetime.now(timezone.utc)
    tape = Tape(date)
    syms = tape.symbols()
    result: dict = {
        "date": date,
        "engine": "v2",
        "armed_source": armed_source,
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

    live_spans: dict[str, list] | None = None
    if armed_source == "live":
        live_spans = defaultdict(list)
        db2 = SessionLocal()
        try:
            rows2 = db2.execute(
                text(
                    "SELECT symbol, created_at, COALESCE(ended_at, updated_at) "
                    "FROM trading_automation_sessions "
                    "WHERE mode='live' AND symbol NOT LIKE '%-USD' "
                    "AND created_at >= :lo AND created_at < :hi"
                ),
                {"lo": f"{date} 04:00:00", "hi": f"{date} 23:59:59"},
            ).fetchall()
        finally:
            db2.rollback(); db2.close()
        for sym2, a2, b2 in rows2:
            live_spans[str(sym2).upper()].append((_aware(a2), _aware(b2)))
        result["live_sessions"] = sum(len(v) for v in live_spans.values())

    armed: dict[str, dict] = {}
    trigger_cache: dict[tuple, tuple] = {}
    trace: list[dict] = []
    _last_stage: dict[str, str] = {}

    def _tr(sym: str, stage: str, t) -> None:
        if _last_stage.get(sym) == stage or len(trace) >= 2000:
            return
        _last_stage[sym] = stage
        trace.append({"t": str(t)[11:16], "sym": sym, "stage": stage})
    armed_spans: dict[str, list[list[str]]] = defaultdict(list)   # sym -> [[from,to],...] HH:MM UTC
    trades: list[dict] = []
    open_pos: dict[str, dict] = {}
    state = {"cum": 0.0, "peak": 0.0, "halted": None}
    day_grid = pd.date_range(f"{date} 13:30:00", f"{date} 19:59:00", freq="1min", tz="UTC")

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

    def close_trade(s: str, p: dict, exit_px: float, why: str, when=None) -> None:
        pnl = (exit_px - p["entry"]) * p["qty"]
        state["cum"] += pnl
        state["peak"] = max(state["peak"], state["cum"])
        trades.append({**p["meta"], "exit": round(exit_px, 4), "why": why,
                       "exit_t": (str(when)[11:16] if when is not None else None),
                       "stop": round(p.get("stop0", p["stop"]), 4),
                       "target": round(p["target"], 4),
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
                        close_trade(s, p, nxt[1], "stop_through_halt_resume", when=now)
                continue
            if bl <= p["stop"]:
                px = min(p["stop"], tape_bid) if tape_bid else p["stop"]
                close_trade(s, p, px, "stop", when=now)
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
        if live_spans is not None:
            now_a = _aware(now)
            for s in list(armed):
                if not any(a <= now_a <= b for a, b in live_spans.get(s, [])):
                    if armed_spans[s] and armed_spans[s][-1][1] is None:
                        armed_spans[s][-1][1] = str(now)[11:16]
                    del armed[s]
            for s, sp2 in live_spans.items():
                if s in armed or s in open_pos:
                    continue
                if any(a <= now_a <= b for a, b in sp2):
                    armed[s] = {"since": now}
                    armed_spans[s].append([str(now)[11:16], None])
        else:
            for s in list(armed):
                if (now - armed[s]["since"]).total_seconds() / 60.0 > REAP_MIN:
                    if armed_spans[s] and armed_spans[s][-1][1] is None:
                        armed_spans[s][-1][1] = str(now)[11:16]
                    del armed[s]
            for s in asof_rank(now):
                if len(armed) >= MAX_SLOTS:
                    break
                if s not in armed and s not in open_pos:
                    armed[s] = {"since": now}
                    armed_spans[s].append([str(now)[11:16], None])
        for s in list(armed):
            if s in open_pos:
                continue
            if tape.in_halt_or_cooldown(s, now):
                _tr(s, "gate_fail:halt_window", now)
                continue
            # per-symbol attempt cooldown: a failed limit-touch attempt rests for the
            # fill window before retrying (mirrors the live ack-timeout + re-watch)
            la = armed[s].get("last_try")
            if la is not None and (now - la).total_seconds() / 60.0 < FILL_WINDOW_BARS * 5:
                continue
            df, c = day_frame(s)
            if df is None:
                continue
            # completed-bars-only, like live: a bar indexed by START closes at +5min
            upto = df[df.index <= now - pd.Timedelta(minutes=5)]
            if len(upto) < 12:
                continue
            # the trigger only changes when a NEW 5m bar completes — cache per
            # (symbol, last-bar) so the 1-min grid doesn't 5x the trigger cost
            _bar_key = (s, str(upto.index[-1]))
            if _bar_key in trigger_cache:
                ok, _treason, dbg = trigger_cache[_bar_key]
            else:
                ok, _treason, dbg = momentum_pullback_trigger(upto, entry_interval="5m")
                trigger_cache[_bar_key] = (ok, _treason, dbg)
            if not ok:
                _tr(s, "trigger_fail:" + str(_treason), now)
                continue
            _tr(s, "trigger_ok", now)
            armed[s]["since"] = now
            q = tape.at(s, now)
            if q is None:
                _tr(s, "gate_fail:stale_quote", now)
                continue
            bid, ask, sbps, dvol = q
            O, H, L, C, V = (c[k] for k in ("open", "high", "low", "close", "volume"))
            atr = compute_atr(upto[H].astype(float), upto[L].astype(float), upto[C].astype(float))
            mid = (bid + ask) / 2.0
            atrp = float(atr.iloc[-1]) / mid if (mid > 0 and pd.notna(atr.iloc[-1])) else 0.0
            move_bps = atrp * 10_000.0
            if move_bps <= 0 or sbps > min(GATE_MOVE_FRAC * move_bps, SPREAD_ABS_CAP_BPS):
                _tr(s, "gate_fail:wide_spread_%.0fbps" % sbps, now)
                continue
            armed[s]["last_try"] = now
            limit = ask  # marketable limit at the REAL ask
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
                _tr(s, "fill@%.4g" % fill_px, now)
                open_pos[s] = {
                    "entry": fill_px, "qty": qty, "stop": stop, "stop0": stop, "target": target,
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
        close_trade(s, p, last_bid, "eod", when=day_grid[-1])

    # close any still-open armed spans at EOD
    eod_label = str(day_grid[-1])[11:16]
    for s in armed_spans:
        for span in armed_spans[s]:
            if span[1] is None:
                span[1] = eod_label

    # chart payloads: 5m OHLCV series + halt spans for every symbol with activity
    traded_syms = {t["sym"] for t in trades}
    active_syms = list(dict.fromkeys(list(traded_syms) + list(armed_spans.keys())))
    series: dict[str, list] = {}
    halt_spans_out: dict[str, list] = {}
    for s in active_syms:
        df, c = day_frame(s)
        if df is None:
            continue
        series[s] = [
            [str(ix)[11:16],
             round(float(r[c["open"]]), 4), round(float(r[c["high"]]), 4),
             round(float(r[c["low"]]), 4), round(float(r[c["close"]]), 4),
             int(r[c["volume"]])]
            for ix, r in df.iterrows()
        ]
        halt_spans_out[s] = [[str(a)[11:16], str(b)[11:16]] for a, b in tape.halts.get(s, [])]
    result["series"] = series
    result["halt_spans"] = halt_spans_out
    result["armed_timeline"] = [
        {"sym": s, "spans": armed_spans[s], "traded": s in traded_syms}
        for s in active_syms
    ]

    result["decision_trace"] = trace
    if armed_source == "live":
        try:
            result["divergence"] = _build_divergence(date, trace, trades)
        except Exception:
            logger.warning("[replay_v2] divergence build failed", exc_info=True)
            result["divergence"] = []

    result["trades"] = sorted(trades, key=lambda z: z["t"])
    result["total_usd"] = round(state["cum"], 0)
    result["wins"] = sum(1 for t in trades if t["usd"] > 0)
    result["losses"] = sum(1 for t in trades if t["usd"] <= 0)
    result["day_halted"] = state["halted"]
    result["duration_s"] = round((datetime.now(timezone.utc) - started).total_seconds(), 1)
    if persist:
        _persist(result)
    return result


def _decisive_summary(lv: list[tuple]) -> list[tuple]:
    """Pick the DECISIVE events (submits/fills/trigger_ok first), deduping repeats.

    A symbol can log hundreds of blocked_by_risk repeats late in the day; showing the
    last 4 blindly buries the entry that actually happened. Works for both live event
    tuples (t, event_type, detail) and replay trace tuples (t, stage).
    """
    def _dedupe(seq: list[tuple]) -> list[tuple]:
        out: list[tuple] = []
        for x in seq:
            if out and out[-1][1:] == x[1:]:
                continue
            out.append(x)
        return out

    def _is_decisive(et: str) -> bool:
        return et in ("live_entry_submitted", "live_entry_filled") or et.startswith("fill@") or et == "trigger_ok"

    decisive = _dedupe([x for x in lv if _is_decisive(x[1])])
    others = _dedupe([x for x in lv if not _is_decisive(x[1])])
    shown = (decisive[:3] + others[-2:]) if decisive else others[-4:]
    return sorted(set(shown))


def _build_divergence(date: str, trace: list[dict], trades: list[dict]) -> list[dict]:
    """Per-symbol join of LIVE decisions vs REPLAY decisions, with a classified cause."""
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT s.symbol, to_char(e.ts,'HH24:MI') AS t, e.event_type, "
                "coalesce(e.payload_json->>'reason', e.payload_json->>'limit_price','') AS detail "
                "FROM trading_automation_events e "
                "JOIN trading_automation_sessions s ON s.id = e.session_id "
                "WHERE s.mode='live' AND s.symbol NOT LIKE '%-USD' "
                "AND e.ts >= :lo AND e.ts < :hi "
                "AND e.event_type IN ('live_entry_candidate_detected','live_entry_submitted',"
                "'live_entry_filled','live_blocked_by_risk','entry_ack_timeout') "
                "ORDER BY e.ts"
            ),
            {"lo": f"{date} 04:00:00", "hi": f"{date} 23:59:59"},
        ).fetchall()
    finally:
        db.rollback(); db.close()
    live_by_sym: dict[str, list] = defaultdict(list)
    for sym, t, et, detail in rows:
        live_by_sym[str(sym)].append((t, et, str(detail or "")[:40]))
    replay_by_sym: dict[str, list] = defaultdict(list)
    for r in trace:
        replay_by_sym[r["sym"]].append((r["t"], r["stage"]))
    traded_syms = {t["sym"] for t in trades}
    out: list[dict] = []
    for sym in sorted(set(live_by_sym) | set(replay_by_sym)):
        lv = live_by_sym.get(sym, [])
        rp = replay_by_sym.get(sym, [])
        live_submits = [x for x in lv if x[1] == "live_entry_submitted" and x[2]]
        live_blocks = [x for x in lv if x[1] == "live_blocked_by_risk"]
        rp_fills = [x for x in rp if x[1].startswith("fill@")]
        rp_trig_fail = [x for x in rp if x[1].startswith("trigger_fail")]
        rp_gate_fail = [x for x in rp if x[1].startswith("gate_fail")]
        if live_submits and sym in traded_syms:
            cause = "aligned"
        elif live_submits and not rp_fills:
            if rp_gate_fail:
                cause = "replay_gate:" + rp_gate_fail[-1][1].split(":", 1)[1]
            elif rp_trig_fail:
                cause = "replay_trigger:" + rp_trig_fail[-1][1].split(":", 1)[1]
            elif not rp:
                cause = "arming_timing"
            else:
                cause = "fill_model_no_touch"
        elif rp_fills and not live_submits:
            lb = live_blocks[-1][2] if live_blocks else "?"
            cause = "live_blocked:" + lb
        else:
            cause = "both_skipped"
        out.append({
            "sym": sym,
            "live": "; ".join(f"{t} {et.replace('live_','')}{(' '+d) if d else ''}" for t, et, d in _decisive_summary(lv)) or "-",
            "replay": "; ".join(f"{t} {st}" for t, st in _decisive_summary(rp)) or "-",
            "cause": cause,
        })
    return out


def _persist(result: dict) -> None:
    try:
        os.makedirs(REPLAY_RESULTS_DIR, exist_ok=True)
        suffix = "_live" if result.get("armed_source") == "live" else ""
        path = os.path.join(REPLAY_RESULTS_DIR, f"{result['date']}{suffix}.json")
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
                    "date", "ran_at_utc", "total_usd", "wins", "losses", "armed_source",
                    "tape_symbols", "candidates", "halt_windows", "day_halted", "error")}
                    | {"n_trades": len(r.get("trades") or [])})
            except Exception:
                continue
    except FileNotFoundError:
        pass
    return sorted(out, key=lambda r: r.get("date") or "", reverse=True)


def load_result(date: str, armed_source: str = "asof") -> dict | None:
    suffix = "_live" if armed_source == "live" else ""
    try:
        with open(os.path.join(REPLAY_RESULTS_DIR, f"{date}{suffix}.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
