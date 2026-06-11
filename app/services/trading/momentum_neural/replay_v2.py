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
from ....config import settings
from ..indicator_core import compute_atr
from ..market_data import fetch_ohlcv_df
from .entry_gates import halt_resume_dip_trigger, momentum_pullback_trigger
from .paper_execution import (
    effective_stop_atr_pct,
    runner_trail_stop,
    scale_out_fraction,
    stop_target_prices,
    structural_or_vol_floored_atr_pct,
)
from .risk_policy import adaptive_max_spread_bps
from .ross_momentum import ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED, score_universe
from .strategy_params import family_default_params

logger = logging.getLogger(__name__)

# ── live-lane parameters — read from the SAME sources the live runner uses ────
# Trigger timeframe: the SAME setting live_runner reads (live_runner.py:1904) —
# parity by construction; flipping live to 1m flips the replay with it.
ENTRY_INTERVAL = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m").lower()
ENTRY_BAR_MIN = int(ENTRY_INTERVAL[:-1]) if ENTRY_INTERVAL.endswith("m") and ENTRY_INTERVAL[:-1].isdigit() else 5
_LIVE_PARAMS = family_default_params("default")
STOP_ATR_MULT = float(_LIVE_PARAMS["stop_atr_mult"])                      # 0.60 default family
TRAIL_ACTIVATE_BPS = float(_LIVE_PARAMS["trail_activate_return_bps"])     # live arms trailing pre-partial here
REWARD_RISK = float(settings.chili_momentum_risk_reward_risk_ratio)
SCALE_FRAC = scale_out_fraction()
GUARD_BPS = float(settings.chili_momentum_order_notional_guard_bps)       # live marketable-limit premium over ask
SPREAD_BASE_BPS = float(settings.chili_momentum_risk_max_spread_bps_live)
SPREAD_EM_RATIO = float(settings.chili_momentum_risk_spread_to_expected_move_ratio)
SPREAD_ABS_CAP_BPS = float(settings.chili_momentum_risk_max_spread_bps_abs_cap)
TARGET_FIRE_FRAC = 0.995                                                  # live partial fires at bid >= target*0.995
BASIS_USD = 22551.0
RISK_PER_TRADE_USD = BASIS_USD * 0.01
NOTIONAL_CAP_USD = BASIS_USD * 0.15
LIQ_FRACTION = 0.01
MAX_SLOTS = 10
DAILY_LOSS_CAP_USD = BASIS_USD * 0.05
GIVEBACK_FRAC = 0.5
REAP_MIN = 30
PARTICIPATION_CAP = 0.10
ENTRY_QUOTE_MAX_STALE_MIN = 2.5   # live blocks stale_bbo at 15s; the 1-min tape's best analog (cadence + jitter)
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


def _premarket_utc_hhmm(date: str) -> str:
    """The lane's premarket start (#562 setting, ET) as UTC HH:MM for ``date`` —
    DST-correct via the exchange tz. The replay day starts where the lane's
    tradeable session starts; Ross's money is made pre-market."""
    pre_et = str(getattr(settings, "chili_momentum_premarket_start_et", "07:00") or "07:00")
    try:
        t = pd.Timestamp(f"{date} {pre_et}", tz="America/New_York").tz_convert("UTC")
        return t.strftime("%H:%M")
    except Exception:
        return "11:00"  # 07:00 EDT


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
                {"lo": f"{date} {_premarket_utc_hhmm(date)}:00", "hi": f"{date} 20:10:00"},
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

    def at(self, sym: str, ts, max_stale_min: float = 5.0) -> tuple[float, float, float, float] | None:
        times = self._times.get(sym)
        if not times:
            return None
        t = _aware(ts)
        i = bisect.bisect_right(times, t) - 1
        if i < 0:
            return None
        row = self.by_sym[sym][i]
        if t - row[0] > timedelta(minutes=max_stale_min):
            return None
        return row[1], row[2], row[3], row[4]

    def in_halt(self, sym: str, ts) -> bool:
        """Inside the actual quote gap — no acting on the stale pre-halt quote."""
        t = _aware(ts)
        return any(a <= t < b for a, b in self.halts.get(sym, []))

    def last_halt_end_before(self, sym: str, ts) -> datetime | None:
        """End of the most recent halt that RESUMED at/before ts (None if none)."""
        t = _aware(ts)
        ends = [b for _a, b in self.halts.get(sym, []) if b <= t]
        return max(ends) if ends else None

    def in_halt_or_cooldown(self, sym: str, ts) -> bool:
        t = _aware(ts)
        for a, b in self.halts.get(sym, []):
            if a <= t < b + timedelta(minutes=RESUME_COOLDOWN_MIN):
                return True
        return False

    def first_after(self, sym: str, ts) -> tuple | None:
        """Full next tape row (ts, bid, ask, spread_bps, day_volume) after ts."""
        times = self._times.get(sym)
        if not times:
            return None
        i = bisect.bisect_right(times, _aware(ts))
        if i >= len(times):
            return None
        return self.by_sym[sym][i]


def _expected_move_bps_15m(upto: pd.DataFrame, H: str, L: str, C: str) -> float | None:
    """Live's expected-move basis (live_runner._expected_move_bps_from_ohlcv):
    mean true range of the last <=14 FIFTEEN-minute bars over the last close, in
    bps. The live spread gate AND the live stop vol-floor both use this number —
    deriving it the same way is what makes those two gates replay at parity."""
    try:
        h = upto[H].astype(float).resample("15min").max()
        low = upto[L].astype(float).resample("15min").min()
        c = upto[C].astype(float).resample("15min").last()
        ok = c.notna()
        h, low, c = h[ok], low[ok], c[ok]
        if len(c) < 2:
            return None
        pc = c.shift(1)
        tr = pd.concat([h - low, (h - pc).abs(), (low - pc).abs()], axis=1).max(axis=1).iloc[1:].tail(14)
        last_close = float(c.iloc[-1])
        if not len(tr) or last_close <= 0:
            return None
        em = float(tr.mean()) / last_close * 10_000.0
        return em if em > 0 else None
    except Exception:
        return None


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
        "entry_interval": ENTRY_INTERVAL,
        "bar_interval_min": ENTRY_BAR_MIN,
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

    entry_bars_cache: dict[str, object] = {}

    def entry_bars(sym: str):
        """Bars at the LIVE trigger timeframe (5m today; follows the setting)."""
        if ENTRY_INTERVAL == "5m":
            return bars(sym)
        if sym not in entry_bars_cache:
            try:
                entry_bars_cache[sym] = fetch_ohlcv_df(sym, interval=ENTRY_INTERVAL, period="5d")
            except Exception:
                entry_bars_cache[sym] = None
        return entry_bars_cache[sym]

    def day_frame(sym: str):
        df = entry_bars(sym)
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
    day_grid = pd.date_range(f"{date} {_premarket_utc_hhmm(date)}:00", f"{date} 19:59:00", freq="1min", tz="UTC")

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
        # Live manages positions every ~30s tick on NBBO bid (live_runner.py:2550+,
        # 2837-2924); the tape's 1-min cadence is the replay analog. All live exits
        # are MARKET sells realized near the bid — exits here price AT the tape bid.
        for s in list(open_pos):
            p = open_pos[s]
            if tape.in_halt(s, now):
                continue  # no quotes inside the gap; the resume sample handles any breach
            q = tape.at(s, now)
            if q is None:
                continue
            bid = q[0]
            p["hwm"] = max(p["hwm"], bid)
            if bid <= p["stop"]:
                why = "trail_stop" if (p["scaled"] or p["trail_armed"]) and p["stop"] > p["stop0"] else "stop"
                close_trade(s, p, bid, why, when=now)
                continue
            # partial at the first target — live fires at bid >= target*0.995 and
            # sells scale_out_fraction of the ORIGINAL qty (live_runner.py:2916-2964)
            if not p["scaled"] and bid >= p["target"] * TARGET_FIRE_FRAC:
                p["scaled"] = True
                part = min(p["qty"], p["qty0"] * SCALE_FRAC)
                p["scale_usd"] = (bid - p["entry"]) * part
                state["cum"] += p["scale_usd"]
                p["qty"] -= part
                p["stop"] = max(p["stop"], p["entry"])  # breakeven_stop_after_partial: ratchet only
            # live also arms trailing pre-partial once bid clears entry by
            # trail_activate_return_bps (live_runner.py:3033-3037)
            if not p["trail_armed"] and bid >= p["entry"] * (1.0 + TRAIL_ACTIVATE_BPS / 10_000.0):
                p["trail_armed"] = True
            if p["scaled"] or p["trail_armed"]:
                p["stop"] = runner_trail_stop(
                    high_water_mark=p["hwm"], atr_pct=p["atrp"], stop_atr_mult=STOP_ATR_MULT,
                    breakeven_floor=p["entry"] if p["scaled"] else p["stop0"],
                    current_stop=p["stop"], side_long=True)

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
            ranked = asof_rank(now)
            pos = {s: i for i, s in enumerate(ranked)}
            for s in ranked:
                if s in armed or s in open_pos:
                    continue
                if len(armed) < MAX_SLOTS:
                    armed[s] = {"since": now}
                    armed_spans[s].append([str(now)[11:16], None])
                    continue
                if pos[s] >= MAX_SLOTS:
                    break  # ranked is ordered — nothing further down can displace either
                # Displacement arming: live re-scans continuously, so a newly-hot name
                # (e.g. a halt-resume pop) gets armed within minutes; first-come-slots +
                # 30-min reaps made the replay arm it ~20 min late. A top-MAX_SLOTS
                # newcomer takes the slot of the worst-ranked armed symbol — but only
                # one that itself FELL OUT of the top set (hysteresis: an armed symbol
                # still holding a top rank is never displaced, so pullback dips that
                # stay top-ranked keep their watcher while the entry forms).
                evict = max((a for a in armed if a not in open_pos), key=lambda a: pos.get(a, 1 << 30), default=None)
                if evict is None or pos.get(evict, 1 << 30) < MAX_SLOTS:
                    break  # every armed symbol still holds a top rank
                if armed_spans[evict] and armed_spans[evict][-1][1] is None:
                    armed_spans[evict][-1][1] = str(now)[11:16]
                del armed[evict]
                armed[s] = {"since": now}
                armed_spans[s].append([str(now)[11:16], None])
        for s in list(armed):
            if s in open_pos:
                continue
            if tape.in_halt(s, now):
                _tr(s, "gate_fail:halt_window", now)
                continue
            # Live parity: inside the resume-dip window the halt_resume_dip trigger
            # owns the tape and may enter DURING the whipsaw cooldown (it demands
            # dip+hold+reclaim structure); the generic trigger still waits it out.
            _resume_end = tape.last_halt_end_before(s, now)
            _dip_window = (
                _resume_end is not None
                and (_aware(now) - _resume_end).total_seconds()
                <= float(getattr(settings, "chili_momentum_halt_resume_dip_window_seconds", 600.0) or 600.0)
            )
            if tape.in_halt_or_cooldown(s, now) and not _dip_window:
                _tr(s, "gate_fail:halt_window", now)
                continue
            df, c = day_frame(s)
            if df is None:
                continue
            # completed-bars-only, like live: a bar indexed by START closes at +5min
            upto = df[df.index <= now - pd.Timedelta(minutes=ENTRY_BAR_MIN)]
            if len(upto) < 12:
                continue
            ok = False
            if _dip_window:
                ok, _treason, dbg = halt_resume_dip_trigger(
                    upto, entry_interval=ENTRY_INTERVAL,
                    halt_resumed_at_utc=_resume_end, now=now)
            if not ok and tape.in_halt_or_cooldown(s, now):
                _tr(s, "gate_fail:halt_window", now)   # cooldown holds unless the dip fired
                continue
            if not ok:
                # the generic trigger only changes when a NEW bar completes — cache
                # per (symbol, last-bar) so the 1-min grid doesn't multiply its cost
                _bar_key = (s, str(upto.index[-1]))
                if _bar_key in trigger_cache:
                    ok, _treason, dbg = trigger_cache[_bar_key]
                else:
                    ok, _treason, dbg = momentum_pullback_trigger(upto, entry_interval=ENTRY_INTERVAL)
                    trigger_cache[_bar_key] = (ok, _treason, dbg)
            if not ok:
                _tr(s, "trigger_fail:" + str(_treason), now)
                continue
            _tr(s, "trigger_ok", now)
            armed[s]["since"] = now
            # live blocks stale_bbo past 15s — the 1-min tape's analog is a tight window
            q = tape.at(s, now, max_stale_min=ENTRY_QUOTE_MAX_STALE_MIN)
            if q is None:
                _tr(s, "gate_fail:stale_quote", now)
                continue
            bid, ask, sbps, dvol = q
            O, H, L, C, V = (c[k] for k in ("open", "high", "low", "close", "volume"))
            atr = compute_atr(upto[H].astype(float), upto[L].astype(float), upto[C].astype(float))
            mid = (bid + ask) / 2.0
            atrp = float(atr.iloc[-1]) / mid if (mid > 0 and pd.notna(atr.iloc[-1])) else 0.0
            # LIVE spread gate: clamp(ratio*EM15m, base floor, abs cap) — same function,
            # same settings, same 15m expected-move basis (live_runner.py:1159-1195)
            em_bps = _expected_move_bps_15m(upto, H, L, C)
            max_spread = adaptive_max_spread_bps(
                SPREAD_BASE_BPS, em_bps, SPREAD_EM_RATIO, abs_cap_bps=SPREAD_ABS_CAP_BPS)
            if sbps > max_spread:
                _tr(s, "gate_fail:wide_spread_%.0fbps" % sbps, now)
                continue
            armed[s]["last_try"] = now
            # LIVE fill semantics: marketable LIMIT at ask*(1+guard_bps), penny-rounded
            # UP; rests only ~10-40s (ack timeout) then cancels (live_runner.py:2084-2131).
            # 1-min tape analog: the order takes the offer NOW unless the next sample
            # shows the market gapped ABOVE the limit (bid > limit -> unfilled).
            limit = ask * (1.0 + GUARD_BPS / 10_000.0)
            if ask >= 1.0:
                limit = float(int(limit * 100.0 + 0.999999)) / 100.0  # ceil to the penny
            nxt = tape.first_after(s, now)
            if nxt is None or (nxt[0] - _aware(now)) > timedelta(minutes=2):
                _tr(s, "gate_fail:no_confirming_quote", now)
                continue
            if nxt[1] > limit:  # next bid above our limit: it ran away inside the ack window
                _tr(s, "gate_fail:ack_timeout", now)
                continue
            fill_px = ask  # marketable limit realizes ~the ask (live: 1.66 limit -> 1.63-1.64 fills)
            pblow = dbg.get("pullback_low")
            # LIVE stop width: vol-floored by the 15m expected move (the floor can BIND
            # here, exactly like live_runner.py:2358-2382), then structural pullback-low
            eff = effective_stop_atr_pct(
                atrp, em_bps if em_bps else atrp * 10_000.0,
                stop_atr_mult=STOP_ATR_MULT, vol_floor_mult=0.5)
            eff, _ = structural_or_vol_floored_atr_pct(
                vol_floored_atr_pct=eff, structural_stop_price=float(pblow) if pblow else None,
                entry_price=fill_px, stop_atr_mult=STOP_ATR_MULT)
            stop, target = stop_target_prices(
                fill_px, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT, reward_risk=REWARD_RISK)
            if not (0 < stop < fill_px):
                continue
            max_notional = min(NOTIONAL_CAP_USD, LIQ_FRACTION * mid * dvol)
            want_qty = min(RISK_PER_TRADE_USD / max(fill_px - stop, 1e-9), max_notional / fill_px)
            minute_vol = max(0.0, float(nxt[4]) - dvol)  # day-volume diff = shares printed next minute
            qty = min(want_qty, PARTICIPATION_CAP * minute_vol)
            if qty <= 0:
                _tr(s, "gate_fail:no_liquidity_printed", now)
                continue
            _tr(s, "fill@%.4g" % fill_px, now)
            open_pos[s] = {
                "entry": fill_px, "qty": qty, "qty0": qty, "stop": stop, "stop0": stop,
                "target": target, "hwm": fill_px, "atrp": eff, "scaled": False,
                "trail_armed": False, "scale_usd": 0.0,
                "meta": {"sym": s, "t": str(now)[11:16], "entry": round(fill_px, 4),
                         "qty": round(qty, 0), "spread_bps": round(sbps, 0),
                         "partial": round(qty / want_qty if want_qty > 0 else 1.0, 2),
                         "fidelity": "tape"},
            }

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

    # chart payloads: OHLCV series at the ENTRY interval (what the trigger actually
    # saw) + halt spans for every symbol with activity. NOTE: minutes with zero
    # prints / inside halts have NO aggregate bar — gaps in the chart are the real
    # tape, not missing data.
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
