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
from .micro_bars import _resample_micro_bars  # re-exported: shared live+replay util
from .entry_gates import (
    TICK_ARMED_WAIT_REASONS,
    halt_resume_dip_trigger,
    momentum_pullback_trigger,
)
from .paper_execution import (
    class_aware_reward_risk,
    classify_stop_breach,
    cushion_adaptive_trail_stop,
    effective_stop_atr_pct,
    ofi_exhaustion_lock,
    pyramid_add_decision,
    pyramid_blend_on_fill,
    scale_out_fraction,
    sell_into_strength_ladder,
    stop_target_prices,
    structural_or_vol_floored_atr_pct,
)
from .pipeline import _live_ofi_microprice, read_ladder_distribution
from .risk_policy import adaptive_max_spread_bps
from .ross_momentum import (
    ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED,
    intraday_impulse_freshness,
    score_universe,
)
from .universe import EQUITY_ROSS_SMALLCAP, build_equity_universe
from .strategy_params import family_default_params

logger = logging.getLogger(__name__)

# ── live-lane parameters — read from the SAME sources the live runner uses ────
# Trigger timeframe: the SAME setting live_runner reads (live_runner.py:1904) —
# parity by construction; flipping live to 1m flips the replay with it.
ENTRY_INTERVAL = str(getattr(settings, "chili_momentum_pullback_entry_interval", "1m") or "1m").lower()
ENTRY_BAR_MIN = int(ENTRY_INTERVAL[:-1]) if ENTRY_INTERVAL.endswith("m") and ENTRY_INTERVAL[:-1].isdigit() else 5
_LIVE_PARAMS = family_default_params("default")
STOP_ATR_MULT = float(_LIVE_PARAMS["stop_atr_mult"])                      # 0.60 default family
TRAIL_ACTIVATE_BPS = float(_LIVE_PARAMS["trail_activate_return_bps"])     # live arms trailing pre-partial here
# R:R and scale-out are resolved PER-SYMBOL at the call sites now (A4: crypto
# takes a wider target + heavier first de-risk than equity) via
# class_aware_reward_risk(s) / scale_out_fraction(symbol=s) — equity replay is
# unchanged (crypto overrides only apply to -USD symbols).
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
# Selection->entry alignment (replay parity with live auto_arm). The live
# auto_arm reuses the entry gate's OWN shallow/deep boundary as its "fresh / faded"
# cutoff (auto_arm._freshness_retracement_threshold) so the freshness filter and the
# pullback gate share ONE self-consistent definition of "shallow" — mirror it here.
FRESHNESS_RETRACEMENT_THRESHOLD = float(
    getattr(settings, "chili_momentum_pullback_retracement_threshold", 0.50) or 0.50
)
REPLAY_FRESHNESS_FILTER = bool(
    getattr(settings, "chili_momentum_replay_freshness_filter_enabled", True)
)
# TICK-FAITHFUL ENTRY (2026-06-15): replay the densified sub-minute ticks inside the
# entry window so a micro-pullback break fires at the true instant it broke (where WS
# ticks exist). OFF ⇒ the prior single-sample tick-break (byte-identical / SUPERSET).
REPLAY_TICK_ENTRY = bool(
    getattr(settings, "chili_momentum_replay_tick_entry_enabled", False)
)
# FULL-PIPELINE armed_source: re-run the real as-of selection (build_equity_universe
# re-screen → re-score → re-arm) from raw tape. OFF ⇒ 'live'/'asof' byte-identical.
REPLAY_FULL_PIPELINE = bool(
    getattr(settings, "chili_momentum_replay_full_pipeline_enabled", False)
)
# FIDELITY-V2 (2026-06-28, diagnosis wf4wdtntt): two INDEPENDENT replay-only flags.
#   * FIDELITY_V2 — size off the full live ~18-dial de-risk stack (collapse the 2.71x
#     over-size) + a marketable-LIMIT FILL-OR-REJECT with a per-minute fill governor
#     (collapse the 2.24x over-fill) + a confidence band over the irreducible tail.
#   * ENGINE_ON — A/B the live ENGINE: aggregate-risk admission (vs the fixed slot cap)
#     + adaptive watch-fanout (vs the fixed MAX_SLOTS). HONEST: barely moves this data.
# Both DEFAULT-OFF ⇒ EVERY edit below is no-op'd ⇒ byte-identical to current HEAD (an
# md5-of-trades parity check proves it). READ ONLY in this replay module — no live path.
REPLAY_FIDELITY_V2 = bool(
    getattr(settings, "chili_momentum_replay_fidelity_v2", False)
)
REPLAY_ENGINE_ON = bool(
    getattr(settings, "chili_momentum_replay_engine_on", False)
)
# RECORDED-FILLS CONSUMER (2026-06-28, "RECORD don't derive"): for armed_source=='live'
# ONLY, consume the recorded broker truth in momentum_fill_outcomes instead of deriving
# fills from the tape — exact SETUP/FILL fidelity for the names live actually traded.
# INDEPENDENT of FIDELITY_V2. DEFAULT-OFF ⇒ byte-identical (no recorded-fill load).
REPLAY_RECORDED_FILLS = bool(
    getattr(settings, "chili_momentum_replay_recorded_fills_enabled", False)
)
# PRINTS-BASED FILL MODEL (2026-06-28, STEP 2; docs/DESIGN/VERSION_AGNOSTIC_BACKTEST.md): the
# quote-touch fill OVER-fills because quotes can't see executions — the TRADE PRINTS can. When
# ON, the entry-fill seam swaps the quote model (min(limit,max(bid,mid)) + the touch-through
# test) for prints_fill_decision over the immutable iqfeed_trade_ticks (version-agnostic: any
# version's (sym,limit,qty,t0) is scored against the same recorded prints). DEFAULT-OFF ⇒ the
# EXACT current quote fill (md5-of-trades byte-identical). REPLAY-ONLY (no live-path read).
REPLAY_PRINTS_FILL = bool(
    getattr(settings, "chili_momentum_replay_prints_fill_enabled", False)
)
# Adaptive review-latency multiplier on the DERIVED latency (NOT a constant latency). 1.0 =
# use the lane's own median (live_entry_submitted - live_entry_candidate_detected) as measured.
REPLAY_REVIEW_LATENCY_K = float(
    getattr(settings, "chili_momentum_replay_review_latency_k", 1.0) or 1.0
)

REPLAY_RESULTS_DIR = os.environ.get("CHILI_REPLAY_RESULTS_DIR", "/app/data/replays")


def _replay_derisk_stack_multiplier(
    *, cum_pnl_usd: float, peak_pnl_usd: float, base_loss_usd: float,
    trade_pnls: list[float], symbol_loss_strikes: int,
) -> tuple[float, dict]:
    """FIDELITY-V2 SIZE FIDELITY — the replay-side image of the live ~18-dial de-risk
    stack (live_runner.py:7307-7310 ``_eff_max_loss`` product, capped at base*3.0).

    The live dials each read live DB state that is meaningless inside a replay (the live
    system's realized day P&L, its streak window, its consecutive-green-days). So instead
    of calling them against the live DB, we feed the SAME bounded FORMULAS the live dials
    use their published callable bounds — applied to the REPLAY's OWN simulated running
    state, which is the faithful analog of what the dial would read live AT THAT POINT of
    the simulated day:

      * cushion   — cushion_risk_multiplier formula: clamp(0.5 + 0.5*cushion/base, 1.0, 2.0)
                    where cushion = max(0, replay's realized day P&L so far) [state["cum"]].
      * green-day — green_day_graduation_multiplier: OFF by default (returns 1.0) — gated on
                    the live flag exactly like the runner; a single replayed day cannot
                    establish a multi-day green streak, so it is 1.0 here (parity with the
                    runner's day-1 => 1.0 rule). The dial is wired (reads the live flag) so
                    if the operator turns graduation on it composes identically.
      * streak    — streak_risk_multiplier formula: clamp(0.5 + win_rate, 0.5, 1.5) over the
                    last 10 REAL entered trades; >=3 consecutive losses => 0.5; <5 => 1.0 —
                    computed from the replay's OWN closed-trade P&L list (the same lane).
      * per-name  — per-name de-risk: the replay already tracks per-symbol loss_strikes for
                    the live G2 2-strike guard; mirror live's per-symbol-fatigue down-size on a
                    name that has already chopped us today (>=1 prior loss strike) by the SAME
                    live knob chili_momentum_per_symbol_yellow_size_fraction — gated on the SAME
                    chili_momentum_per_symbol_fatigue_enabled flag (no new magic number).

    Each factor is bounded exactly as its live callable; the COMBINED product is clamped at
    base*3.0 (live_runner.py:7309) — the SAME hard ceiling. Returns (mult, meta). All knobs
    read the SAME settings the runner reads. REPLAY-ONLY (zero live-path effect)."""
    meta: dict = {}
    base = float(base_loss_usd or 0.0)
    if base <= 0:
        return 1.0, {"reason": "no_base_loss", "stack_mult": 1.0}

    # ── cushion dial (cushion_risk_multiplier formula, replay day-P&L as the cushion) ──
    cushion = max(0.0, float(cum_pnl_usd or 0.0))
    cushion_mult = max(1.0, min(2.0, 0.5 + 0.5 * (cushion / base)))
    meta["cushion_mult"] = round(cushion_mult, 4)

    # ── green-day graduation dial — gated on the live flag (runner parity: day-1 => 1.0) ──
    green_day_mult = 1.0
    if bool(getattr(settings, "chili_momentum_green_day_graduation_enabled", False)):
        # A single replayed session cannot establish a >1-day green streak, so the
        # graduation multiplier is 1.0 (clamp(1.0 + step*max(0, streak-1)) with streak<=1).
        green_day_mult = 1.0
    meta["green_day_mult"] = round(green_day_mult, 4)

    # ── streak dial (streak_risk_multiplier formula over the replay's closed trades) ──
    streak_mult = 1.0
    pnls = [float(p) for p in (trade_pnls or [])][-10:]
    pnls = list(reversed(pnls))  # newest first, mirroring the live query order
    if len(pnls) >= 5:
        win_rate = sum(1 for p in pnls if p > 0) / len(pnls)
        consec_losses = 0
        for p in pnls:
            if p <= 0:
                consec_losses += 1
            else:
                break
        streak_mult = max(0.5, min(1.5, 0.5 + win_rate))
        if consec_losses >= 3:
            streak_mult = 0.5
    meta["streak_mult"] = round(streak_mult, 4)

    # ── per-name de-risk dial — live per_symbol_fatigue yellow size-down on a name that
    # has already chopped us today (>=1 prior loss strike). Reuses the EXACT live knob +
    # flag the runner's per_symbol_fatigue_size_multiplier reads (no new magic number).
    per_name_mult = 1.0
    strikes = int(symbol_loss_strikes or 0)
    if strikes > 0 and bool(getattr(settings, "chili_momentum_per_symbol_fatigue_enabled", False)):
        _frac = float(getattr(settings, "chili_momentum_per_symbol_yellow_size_fraction", 0.5) or 0.5)
        if 0.0 < _frac < 1.0 and _frac == _frac:  # NaN guard, same as the live helper
            per_name_mult = _frac
    meta["per_name_mult"] = round(per_name_mult, 4)

    combined = cushion_mult * green_day_mult * streak_mult * per_name_mult
    # Sanitize + the SAME hard combined-multiplier ceiling the runner applies (3x base).
    if not (combined == combined) or combined < 0:  # NaN/negative fail-neutral
        combined = 1.0
    combined = min(combined, 3.0)
    meta["stack_mult"] = round(combined, 4)
    return combined, meta


class _ReplayFillGovernor:
    """FIDELITY-V2 per-minute fill-admission TOKEN BUCKET — the SIMULATION-TIME image of
    rail_governor.GovernorConfig (the live adaptive rate governor that bounds rail call
    rate so a burst of simultaneous admissions can never flood/429 the broker).

    The live governor is a WALL-CLOCK token bucket (time.monotonic); a replay runs on a
    simulated minute grid, so wall-clock is meaningless here. This mirrors the SAME config
    (burst capacity + refill_rps, read from the SAME chili_momentum_rail_governor_* settings)
    but refills against SIMULATION minutes: tokens replenish at refill_rps tokens/second of
    sim-time elapsed, capped at burst. When N triggers fire in the same sim-minute, only up
    to the available tokens admit; the rest are DEFERRED (the live 429/queue analog), exactly
    the burst-fill artifact that spiked the replay/live ratio to 6.7-8.0x on the high-fan-out
    days (06-23/24). Gated on the SAME chili_momentum_entry_placement_governor_enabled kill-
    switch the live governor uses — OFF ⇒ admit() always True (no rate limit). REPLAY-ONLY."""

    def __init__(self) -> None:
        self._burst = max(1.0, float(getattr(settings, "chili_momentum_rail_governor_burst", 4.0) or 4.0))
        self._rps = max(1e-3, float(getattr(settings, "chili_momentum_rail_governor_start_rps", 2.0) or 2.0))
        self._enabled = bool(getattr(settings, "chili_momentum_entry_placement_governor_enabled", True))
        self._tokens = self._burst
        self._last = None  # last sim-time we refilled at (tz-aware datetime)
        self.deferrals = 0

    def admit(self, now) -> bool:
        """Take one fill token at simulation-time ``now``. Refill from sim-elapsed
        seconds; defer (return False) when the bucket is empty. OFF ⇒ always True."""
        if not self._enabled:
            return True
        na = _aware(now)
        if self._last is not None:
            elapsed = (na - self._last).total_seconds()
            if elapsed > 0:
                self._tokens = min(self._burst, self._tokens + elapsed * self._rps)
        self._last = na
        if self._tokens >= 1.0:
            self._tokens -= 1.0
            return True
        self.deferrals += 1
        return False


def _run_r_value(entry: float, stop0: float, mfe_px: float) -> float:
    """Favorable excursion in R: (peak_mark − entry) / (entry − original_stop).

    The MESO follow-through separator surfaced by the 2026-06-22 loss decomposition
    (wf w6c11y2s9): winners thrust >=~1.3R, losers run <=~0.18R before fading to the
    stop — so run_r is the metric A/Bs read instead of the (skewed) replay dollars.
    Normalized by the ORIGINAL structural risk (entry − stop0) so a replay run_r is
    directly comparable to the live lane. Returns 0.0 for a degenerate/non-positive
    risk and never goes negative (the MFE is floored at entry). [momentum_neural]
    """
    try:
        risk0 = float(entry) - float(stop0)
        if risk0 <= 0:
            return 0.0
        return max(0.0, (float(mfe_px) - float(entry)) / risk0)
    except Exception:
        return 0.0


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


def _load_recorded_fills(date: str) -> dict[str, list[dict]]:
    """RECORDED-FILLS CONSUMER ("RECORD don't derive") — load the day's REAL broker fills
    from ``momentum_fill_outcomes`` (equity lane = symbol NOT LIKE '%-USD') and collapse the
    per-leg rows into per-``(session_id, leg_seq)`` round-trips. Returns ``{SYMBOL_UPPER:
    [round_trip, ...]}`` — a symbol present in the map = it RECORDED a live entry fill that day
    (the FILL-SET truth the replay must reproduce exactly for live-armed names).

    Leg model (verified in-container 2026-06-28): one row per fill leg, ``side`` in
    {entry, exit, partial_exit, scale_out}. A round-trip is keyed by ``(session_id, leg_seq)``:
      * entry leg     — ``broker_fill_price`` = the realized entry fill, ``qty``,
                        ``spread_bps_at_decision``, ``intended_price`` (the marketable-limit).
      * exit-side legs — exit/partial_exit/scale_out: ``broker_fill_price`` = the exit fill,
                        ``realized_pnl_usd`` = that leg's realized $; ``exit_reason`` the live
                        exit reason. The round-trip's exit price = the LAST exit-side leg's fill,
                        its $ = the SUM of all exit-side legs' realized_pnl_usd, its exit reason =
                        the last terminal (non-partial) exit leg's reason (else the last leg's).
    An entry with NO exit-side leg (open at EOD / a re-arm that did not close) is still a faithful
    FILL — emitted with exit=entry and $=Σ(any partials) so the SETUP counts (the operator's
    priority is setup/fill accuracy, not the $). Faithful by construction: NO derivation, NO tape.
    REPLAY-ONLY (read-only single SELECT; rolled back). [momentum_neural]"""
    out: dict[str, list[dict]] = defaultdict(list)
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT symbol, session_id, leg_seq, side, qty, broker_fill_price, "
                "entry_price, intended_price, spread_bps_at_decision, realized_pnl_usd, "
                "exit_reason, fill_ts "
                "FROM momentum_fill_outcomes "
                "WHERE symbol NOT LIKE '%-USD' "
                "AND created_at >= :lo AND created_at < :hi "
                "ORDER BY symbol, session_id, leg_seq, fill_ts, id"
            ),
            {"lo": f"{date} 00:00:00", "hi": f"{date} 23:59:59"},
        ).fetchall()
    finally:
        db.rollback(); db.close()

    # group raw legs by (symbol, session_id, leg_seq)
    grp: dict[tuple, list] = defaultdict(list)
    for r in rows:
        grp[(str(r[0]).upper(), r[1], r[2])].append(r)

    def _f(x, d=0.0):
        try:
            return float(x) if x is not None else d
        except (TypeError, ValueError):
            return d

    for (sym, sid, leg), legs in grp.items():
        entry_leg = next((l for l in legs if str(l[3]) == "entry"), None)
        if entry_leg is None:
            continue  # an exit with no recorded entry leg cannot anchor a round-trip
        exit_legs = [l for l in legs if str(l[3]) in ("exit", "partial_exit", "scale_out")]
        entry_px = _f(entry_leg[5])               # broker_fill_price on the entry leg
        qty = _f(entry_leg[4])
        spread = _f(entry_leg[8])
        intended = _f(entry_leg[7], entry_px)
        entry_ts = entry_leg[11]
        if entry_px <= 0 or qty <= 0:
            continue
        # exit price = the LAST exit-side leg's fill; $ = Σ realized_pnl over exit-side legs;
        # reason = the last TERMINAL (exit) leg's reason, else the last exit-side leg's reason.
        pnl = sum(_f(l[9]) for l in exit_legs)
        if exit_legs:
            exit_px = _f(exit_legs[-1][5], entry_px)
            _terminal = [l for l in exit_legs if str(l[3]) == "exit"]
            _src_leg = _terminal[-1] if _terminal else exit_legs[-1]
            exit_reason = str(_src_leg[10] or "exit")
            exit_ts = exit_legs[-1][11]
            closed = bool(_terminal)
        else:
            exit_px = entry_px            # open at EOD / unclosed re-arm: flat-mark the fill
            exit_reason = "open_eod"
            exit_ts = entry_ts
            closed = False
        out[sym].append({
            "sym": sym, "session_id": sid, "leg_seq": leg,
            "entry": entry_px, "exit": exit_px, "qty": qty,
            "spread_bps": spread, "intended": intended,
            "usd": pnl, "exit_reason": exit_reason, "closed": closed,
            "entry_ts": _aware(entry_ts) if entry_ts is not None else None,
            "exit_ts": _aware(exit_ts) if exit_ts is not None else None,
        })
    # stable order per symbol: by entry time then leg_seq
    for sym in out:
        out[sym].sort(key=lambda d: (d["entry_ts"] or _aware(datetime(1970, 1, 1)), d["leg_seq"]))
    return dict(out)


class Tape:
    """Real 1-min NBBO per symbol for one date, + halt windows from tape gaps.

    Each row is ``(ts, bid, ask, spread_bps, day_volume, source)``; ``source``
    distinguishes the 1-min sampler ('massive_snapshot') from the densified
    sub-minute WS ticks ('massive_ws' / 'coinbase_ws' armed names, plus the
    whole-universe densifiers 'massive_ws_universe' for equity and
    'coinbase_ws_universe' for the crypto L2-drain twin) so the tick-faithful
    entry can resolve INSIDE a minute where ticks exist. Consumers read by
    index ≤4 (the appended ``source`` never shifts them)."""

    def __init__(self, date: str):
        self.by_sym: dict[str, list[tuple[datetime, float, float, float, float, str]]] = defaultdict(list)
        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    "SELECT symbol, observed_at, bid, ask, spread_bps, day_volume, source "
                    "FROM momentum_nbbo_spread_tape "
                    "WHERE observed_at >= :lo AND observed_at < :hi AND bid > 0 AND ask > 0 "
                    "ORDER BY symbol, observed_at"
                ),
                {"lo": f"{date} {_premarket_utc_hhmm(date)}:00", "hi": f"{date} 20:10:00"},
            ).fetchall()
        finally:
            db.rollback()
            db.close()
        for sym, ts, bid, ask, sbps, dvol, src in rows:
            ts = _aware(ts)
            self.by_sym[str(sym)].append(
                (ts, float(bid), float(ask), float(sbps or 0), float(dvol or 0), str(src or ""))
            )
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
        """Full next tape row (ts, bid, ask, spread_bps, day_volume, source) after ts."""
        times = self._times.get(sym)
        if not times:
            return None
        i = bisect.bisect_right(times, _aware(ts))
        if i >= len(times):
            return None
        return self.by_sym[sym][i]

    def prices_between(self, sym: str, t0, t1) -> list[tuple[datetime, float, float, str]]:
        """TICK-FAITHFUL ENTRY (2026-06-15): every (ts, bid, ask, source) row in the
        HALF-OPEN window [t0, t1), in time order — the densified sub-minute ticks the
        replay walks to fire the entry at the true instant it broke, not a 1-min
        sample later. SUPERSET: where only the 1-min sampler exists, this returns the
        same single (or zero) row .at() would have seen ⇒ byte-identical behavior.
        Reuses the per-symbol bisected ``_times`` index (no scan)."""
        times = self._times.get(sym)
        if not times:
            return []
        lo = bisect.bisect_left(times, _aware(t0))
        hi = bisect.bisect_left(times, _aware(t1))
        out: list[tuple[datetime, float, float, str]] = []
        for r in self.by_sym[sym][lo:hi]:
            # r = (ts, bid, ask, spread_bps, day_volume, source)
            out.append((r[0], r[1], r[2], r[5] if len(r) > 5 else ""))
        return out


class TradeTape:
    """STEP 2 — the immutable per-symbol TRADE PRINTS for one date (the prints-based fill
    model; docs/DESIGN/VERSION_AGNOSTIC_BACKTEST.md). A twin of ``Tape`` but over
    ``iqfeed_trade_ticks`` (real per-trade executions: price + size + bid/ask at the print)
    instead of the 1-min NBBO. Bulk-loads the whole day ONCE, indexes by symbol, and exposes
    a lookahead-free ``prints_through`` (executions AT/THROUGH a limit in a half-open window
    using only ``observed_at`` in [t_lo, t_hi)).

    Why this exists: the quote-touch fill in the entry seam treats a quote touch as a fill and
    OVER-fills (BEEM 29/34 predicted vs 1/34 live) because quotes cannot SEE executions. A
    through-print IS an execution — direct evidence shares traded at/through the limit. The
    fill model below cumulates real print size against an inferred queue to decide FILL /
    PARTIAL / CANCEL and a MEASURED ``fill_vwap``. Version-agnostic: nothing here reads
    ``momentum_fill_outcomes``; any version's order at any ``(sym, limit, qty, t0)`` is scored
    against these same recorded prints. Reuses the bid/ask carried on each print so
    ``queue_ahead`` (the L1 size-at-touch proxy) can be inferred without a separate L2 load."""

    def __init__(self, date: str):
        # per-symbol list of (ts, price, size, bid, ask) in time order
        self.by_sym: dict[str, list[tuple[datetime, float, float, float, float]]] = defaultdict(list)
        db = SessionLocal()
        try:
            rows = db.execute(
                text(
                    "SELECT symbol, observed_at, price, size, bid, ask "
                    "FROM iqfeed_trade_ticks "
                    "WHERE observed_at >= :lo AND observed_at < :hi "
                    "AND price > 0 AND size > 0 "
                    "ORDER BY symbol, observed_at"
                ),
                {"lo": f"{date} {_premarket_utc_hhmm(date)}:00", "hi": f"{date} 20:10:00"},
            ).fetchall()
        except Exception:
            # iqfeed_trade_ticks may not exist on this DB / date → no prints; the fill model
            # degrades every order to quote_fallback (handled by the caller). Fail-open.
            rows = []
        finally:
            db.rollback()
            db.close()
        for sym, ts, px, sz, bid, ask in rows:
            ts = _aware(ts)
            self.by_sym[str(sym)].append(
                (ts, float(px), float(sz or 0.0),
                 float(bid) if bid is not None else 0.0,
                 float(ask) if ask is not None else 0.0)
            )
        self._times: dict[str, list[datetime]] = {s: [r[0] for r in v] for s, v in self.by_sym.items()}
        # adaptive per-name inter-trade cadence (median gap between prints) — the fallback
        # review-latency basis when no automation-event latencies exist for the day.
        self._cadence_s: dict[str, float] = {}
        for s, tms in self._times.items():
            if len(tms) < 2:
                continue
            gaps = sorted((b - a).total_seconds() for a, b in zip(tms, tms[1:]) if b > a)
            if gaps:
                self._cadence_s[s] = gaps[len(gaps) // 2]
        # DERIVED review latency (seconds): the lane's OWN median submit-minus-detected
        # latency from the recorded automation events for this date (NOT a constant). Computed
        # once here so the fill window opens at t0 - review_latency without a per-call query.
        self.review_latency_s: float | None = _derive_review_latency_s(date)

    def symbols(self) -> list[str]:
        return list(self.by_sym.keys())

    def cadence_s(self, sym: str) -> float | None:
        """Adaptive inter-trade cadence (median print gap, seconds) for the name, or None."""
        return self._cadence_s.get(sym)

    def queue_ahead_at(self, sym: str, ts, side: str = "long") -> float | None:
        """L1 size-at-touch proxy for the resting queue ahead of our order: the bid/ask SIZE is
        not carried on the print, so we use the most recent print's size at/just-before ``ts``
        as the observable depth proxy (the live model degrades to 0 + a low-confidence flag when
        absent). Returns the print size of the last print at/before ts, or None if no print."""
        times = self._times.get(sym)
        if not times:
            return None
        i = bisect.bisect_right(times, _aware(ts)) - 1
        if i < 0:
            return None
        return float(self.by_sym[sym][i][2])

    def prints_through(self, sym: str, limit: float, side: str, t_lo, t_hi) -> list[tuple[float, float, datetime]]:
        """LOOKAHEAD-FREE executions AT/THROUGH ``limit`` whose ``observed_at`` ∈ [t_lo, t_hi),
        in time order, as ``(price, size, ts)``. For a long marketable buy LIMIT, an execution
        "through" the limit is a print at ``price <= limit`` (the offer traded down to/through
        our resting bid-side limit). Reuses the bisected ``_times`` index (no scan)."""
        times = self._times.get(sym)
        if not times:
            return []
        lo = bisect.bisect_left(times, _aware(t_lo))
        hi = bisect.bisect_left(times, _aware(t_hi))
        out: list[tuple[float, float, datetime]] = []
        _long = (side or "long").lower() != "short"
        for r in self.by_sym[sym][lo:hi]:
            _ts, _px, _sz = r[0], r[1], r[2]
            if _sz <= 0:
                continue
            if (_long and _px <= limit) or ((not _long) and _px >= limit):
                out.append((_px, _sz, _ts))
        return out

    def has_prints_near(self, sym: str, t_lo, t_hi) -> bool:
        """LOOKAHEAD-FREE local coverage probe: does the name have ANY print (at ANY price, not
        just through our limit) whose ``observed_at`` ∈ [t_lo, t_hi]? Used to source-tag a fill
        honestly: 'prints_fill' means there WAS local print coverage for THIS order's window so a
        no-through-print is a genuine no-fill; 'quote_fallback' means the name had no prints in
        the neighborhood of this order at all (so the absence of a through-print is uninformative).
        Whole-day cadence cannot answer this — a name can have prints elsewhere in the day yet none
        in this order's window. Inclusive on t_hi so a print exactly at the ack horizon still counts
        as coverage. Reuses the bisected ``_times`` index (no scan)."""
        times = self._times.get(sym)
        if not times:
            return False
        lo = bisect.bisect_left(times, _aware(t_lo))
        hi = bisect.bisect_right(times, _aware(t_hi))
        return hi > lo


def _derive_review_latency_s(date: str) -> float | None:
    """ADAPTIVE review latency for the prints-fill window (NO magic constant): the MEDIAN of the
    momentum lane's OWN recorded (live_entry_submitted.ts − live_entry_candidate_detected.ts)
    latencies for ``date``, paired per session in time order. Returns seconds, or None when no
    pairable events exist (the caller then falls back to the name's inter-trade print cadence).
    REPLAY-ONLY read of recorded events (the same table _build_divergence joins)."""
    db = SessionLocal()
    try:
        rows = db.execute(
            text(
                "SELECT e.session_id, e.event_type, e.ts FROM trading_automation_events e "
                "JOIN trading_automation_sessions s ON s.id = e.session_id "
                "WHERE s.mode='live' AND s.symbol NOT LIKE '%-USD' "
                "AND e.event_type IN ('live_entry_candidate_detected','live_entry_submitted') "
                "AND e.ts >= :lo AND e.ts < :hi ORDER BY e.session_id, e.ts"
            ),
            {"lo": f"{date} 04:00:00", "hi": f"{date} 23:59:59"},
        ).fetchall()
    except Exception:
        return None
    finally:
        db.rollback(); db.close()
    # pair each detected with the NEXT submit in the same session (FIFO) → latency seconds
    pending: dict = {}
    lats: list[float] = []
    for sid, et, ts in rows:
        if et == "live_entry_candidate_detected":
            pending.setdefault(sid, []).append(ts)
        elif et == "live_entry_submitted":
            q = pending.get(sid)
            if q:
                det = q.pop(0)
                dt = (ts - det).total_seconds()
                if dt >= 0:
                    lats.append(dt)
    if not lats:
        return None
    lats.sort()
    return lats[len(lats) // 2]


def prints_fill_decision(
    trade_tape: "TradeTape",
    sym: str,
    limit: float,
    qty: float,
    t0,
    *,
    side: str = "long",
    queue_ahead: float | None,
    review_latency_s: float | None,
    ack_window_s: float,
    bid: float,
    participation: float,
    min_size: float = 1.0,
) -> dict:
    """STEP 2 prints-based fill model. Scores ONE order ``(sym, limit, qty, t0)`` against the
    immutable trade prints in [t0 − review_latency, t0 + ack_window):

      * cumulate ``participation * size`` of the through-prints (executions at/through the limit),
      * ``filled = max(0, min(qty, cum_size − queue_ahead))`` (queue_ahead = inferred size ahead),
      * ``fill_vwap`` = the size-weighted price over the FILLING slice, bounded [bid, limit]
        (never better than the best bid, never worse than our limit),
      * ``filled >= qty`` → FILL; ``0 < filled < qty`` → PARTIAL (caller emits + cancels the
        remainder; below ``min_size`` → CANCEL); ``filled <= 0`` → CANCEL.

    Returns {filled_qty, fill_vwap, status ∈ FILL|PARTIAL|CANCEL, source, meta}. ``source`` =
    'prints_fill' when the name has ANY print in a neighborhood of THIS order ([t_lo, t_hi]),
    'quote_fallback' when it has NO local print coverage for this order's window (so the caller
    widens the confidence band) — tagged off LOCAL coverage, not whole-day cadence, so it honestly
    reflects whether THIS order could be resolved against prints. When the queue depth ahead of us
    is unobservable (no print at/before the window open), ``queue_ahead`` falls back to a
    CONSERVATIVE non-zero proxy = the median in-window through-print size (so a single tiny print
    cannot clear an unknown queue and over-fill); ``meta.low_confidence`` flags this, and
    ``meta.queue_proxy`` records which basis was used ('observed' | 'median_through_print' |
    'none'). Lookahead-free (only ``observed_at`` ∈ the window). NO magic numbers — every input is
    derived/passed."""
    review_latency_s = max(0.0, float(review_latency_s or 0.0))
    t_lo = _aware(t0) - timedelta(seconds=review_latency_s)
    t_hi = _aware(t0) + timedelta(seconds=max(0.0, float(ack_window_s)))
    prints = trade_tape.prints_through(sym, limit, side, t_lo, t_hi)
    # SEMANTICS: the queue ahead of us must be the queue that EXISTED when the fill window OPENS
    # (t_lo = t0 − review_latency), NOT at t0. The through-prints we credit span [t_lo, t_hi); if
    # we snapshotted the queue at t0 we'd consume prints in [t_lo, t0) against a queue that did
    # not yet exist at the time those prints happened — systematically over-filling on fast movers
    # (most through-prints sit in that backward sub-window). Re-snapshot at t_lo so the queue and
    # the through-prints share the same time origin. The caller-supplied ``queue_ahead`` (taken at
    # t0) is used only to preserve the low-confidence flag (None ⇒ no observable depth proxy).
    low_conf = queue_ahead is None
    qa_at_open = trade_tape.queue_ahead_at(sym, t_lo, side=side)
    qa_obs = qa_at_open if qa_at_open is not None else queue_ahead
    if qa_obs is not None:
        # We have an OBSERVED depth proxy (a print's size at/just-before the window open).
        qa = max(0.0, float(qa_obs))
    else:
        # FIX 1 — CONSERVATIVE UNKNOWN-QUEUE PROXY (anti-over-fill). When the queue depth is
        # unobservable (no print at/before t_lo to read a size off) the previous code coerced the
        # queue to 0.0, which FULLY BYPASSES the queue gate: a single tiny through-print then clears
        # the (unknown) queue and the order fills as if first-in-line — exactly the BEEM over-fill
        # the prints model exists to prevent. Instead, stand a NON-ZERO proxy queue ahead of us,
        # derived from the order's OWN in-window through-prints: the MEDIAN through-print size. This
        # is adaptive (no constant) and makes the model demand at least a typical-print's worth of
        # flow before we are credited — so one undersized print can no longer clear an unknown
        # queue. low_confidence stays set so the caller still widens the band. If there are no
        # through-prints the proxy is 0.0 (moot — the no-print branch below returns CANCEL anyway).
        _sizes = sorted(_sz for (_px, _sz, _ts) in prints if _sz > 0)
        qa = float(_sizes[len(_sizes) // 2]) if _sizes else 0.0
    # queue_proxy semantics for the meta/audit: 'observed' = a real print size at/before t_lo;
    # 'median_through_print' = FIX-1 conservative non-zero proxy (queue depth unobservable);
    # 'none' = no through-prints so the proxy was moot (the no-print branch returns below).
    qa_proxy = "observed" if qa_obs is not None else ("median_through_print" if prints else "none")
    # FIX 2 — HONEST SOURCE TAG (local coverage, not whole-day cadence). The previous code tagged
    # 'prints_fill' vs 'quote_fallback' off whole-day cadence_s (>=2 prints ANYWHERE in the day),
    # which mis-tagged an order whose OWN [t_lo, t_hi] window had no prints as 'prints_fill'. Tag
    # off whether the name has ANY print in a NEIGHBORHOOD of THIS order instead, so quote_fallback
    # honestly means "no local print coverage for THIS order's window." Lookahead-free (the probe
    # only inspects observed_at ≤ t_hi). When there ARE through-prints we resolved against real
    # prints ⇒ always 'prints_fill'; only the no-through-print path consults local coverage.
    src = "prints_fill" if trade_tape.has_prints_near(sym, t_lo, t_hi) else "quote_fallback"
    if not prints:
        # No execution at/through the limit in the window — version-agnostically this is a
        # genuine NO-FILL (the offer never traded down to us). But there may simply be no print
        # COVERAGE for this name/day (87% of candidates have no prints, per the design audit);
        # the caller distinguishes by checking whether the name has ANY prints at all.
        return {
            "filled_qty": 0.0, "fill_vwap": None, "status": "CANCEL",
            "source": src,
            "meta": {"reason": "no_through_print", "queue_ahead": qa, "queue_proxy": qa_proxy,
                     "review_latency_s": round(review_latency_s, 3),
                     "low_confidence": low_conf, "n_prints": 0},
        }
    # cumulate participation-weighted through-print size; consume queue_ahead first, then fill us
    cum = 0.0
    fill_sz = 0.0
    fill_notional = 0.0
    remaining_q = max(0.0, float(qty))
    for _px, _sz, _ts in prints:
        avail = max(0.0, float(participation)) * float(_sz)
        if avail <= 0:
            continue
        cum += avail
        # shares available to US after the inferred queue ahead is served
        usable = cum - qa
        if usable <= fill_sz:
            continue  # still serving the queue / already counted
        take = min(remaining_q - fill_sz, usable - fill_sz)
        if take <= 0:
            continue
        # the filling price is bounded [bid, limit] — never better than the best bid, never
        # worse than our resting limit (the empirical resting-limit fill shape, no hardcoded bps)
        px_eff = min(limit, max(float(bid), float(_px)))
        fill_sz += take
        fill_notional += take * px_eff
        if fill_sz >= remaining_q:
            break
    filled = max(0.0, min(float(qty), fill_sz))
    if filled <= 0:
        return {
            "filled_qty": 0.0, "fill_vwap": None, "status": "CANCEL",
            "source": src,
            "meta": {"reason": "queue_not_cleared", "queue_ahead": qa, "queue_proxy": qa_proxy,
                     "cum_size": round(cum, 1),
                     "review_latency_s": round(review_latency_s, 3),
                     "low_confidence": low_conf, "n_prints": len(prints)},
        }
    fill_vwap = fill_notional / filled if filled > 0 else None
    if fill_vwap is not None:
        fill_vwap = min(limit, max(float(bid), float(fill_vwap)))
    if filled >= float(qty) - 1e-9:
        status = "FILL"
    elif filled >= float(min_size):
        status = "PARTIAL"
    else:
        status = "CANCEL"
    return {
        "filled_qty": filled, "fill_vwap": fill_vwap, "status": status,
        "source": src,
        "meta": {"reason": status.lower(), "queue_ahead": qa, "queue_proxy": qa_proxy,
                 "cum_size": round(cum, 1),
                 "review_latency_s": round(review_latency_s, 3),
                 "low_confidence": low_conf, "n_prints": len(prints)},
    }


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


def freshness_arm_decision(upto, *, firing: bool) -> bool:
    """The live auto_arm fresh-impulse selection rule, as a pure function over a
    completed-bars frame — shared by the replay arming gate so it is unit-testable
    and provably equals the live discipline:

      (1) a FIRING break is always a valid arm (live: 'a name whose break is FIRING
          now is always a valid entry');
      (2) otherwise arm only a name POSITIVELY known to be in a fresh up-impulse
          (``intraday_impulse_freshness().is_fresh``) — drop FADED 24h leaders;
      (3) UNKNOWN freshness (bad/insufficient bars -> is_fresh False) is NOT armed on
          freshness alone (live ``_known_fresh`` treats unknown as not-fresh).

    Uses the SAME helper + threshold the live auto_arm uses (parity by construction).
    """
    if firing:
        return True
    fr = intraday_impulse_freshness(
        upto, retracement_threshold=FRESHNESS_RETRACEMENT_THRESHOLD)
    return bool(getattr(fr, "is_fresh", False))


def _capture_entry_features(s, dbg, upto, fill_px, stop, target, qty, want_qty, sbps, atrp,
                            eff, mid, dvol, minute_vol, liq_mult, fire_ts, entry_fidelity,
                            l2db, Hc, Lc, Cc, Vc):
    """Replay adapter for the SHARED entry_features.capture_entry_features — maps the replay's
    fill locals to the shared signature so replay + live produce parity-identical vectors.
    dollar_vol=mid*dvol; l2_as_of=the historical fire ts (live passes None for the in-process
    WS ring). Byte-identical to the prior inline version (pure refactor)."""
    from .entry_features import capture_entry_features

    return capture_entry_features(
        s, fill_px=fill_px, stop=stop, target=target, qty=qty, want_qty=want_qty,
        spread_bps=sbps, atr_pct=atrp, stop_atr_pct_eff=eff, mid=mid,
        dollar_vol=(mid * dvol), liq_mult=liq_mult, fire_ts=fire_ts,
        entry_fidelity=entry_fidelity, trigger_debug=dbg, session_df=upto,
        df_cols=(Hc, Lc, Cc, Vc), minute_vol=minute_vol, l2_db=l2db,
        l2_as_of=_aware(fire_ts).replace(tzinfo=None))


def run_replay(date: str, *, persist: bool = True, armed_source: str = "live") -> dict:
    """Run the high-fidelity replay for ``date`` (YYYY-MM-DD). Returns the structured
    result dict; persists it to REPLAY_RESULTS_DIR/<date>.json when ``persist``."""
    started = datetime.now(timezone.utc)
    tape = Tape(date)
    syms = tape.symbols()
    # STEP 2 prints-based fill model: bulk-load the day's TRADE PRINTS once (twin of Tape over
    # iqfeed_trade_ticks) only when the flag is on. OFF ⇒ None ⇒ the entry seam keeps the EXACT
    # quote fill (byte-identical). REPLAY-ONLY.
    trade_tape = TradeTape(date) if REPLAY_PRINTS_FILL else None
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

    # REPLAY->LIVE SIZING PARITY (operator "walang mintis", 2026-06-23): size off the SAME
    # real account equity the live lane uses, NOT a stale fixed basis, so the replay's
    # per-trade risk / notional + the daily-loss cap track LIVE (a fixed $22551 basis sized
    # ~1.6x too big and capped at $1127 vs live's equity-based ~$686). Override pins it
    # (deterministic A/B); else read the live agentic equity; else fall back to BASIS_USD
    # (e.g. a local run without the broker token). SAME equity-fractions live uses.
    basis_usd = float(getattr(settings, "chili_replay_equity_basis_usd", 0.0) or 0.0)
    if basis_usd <= 0:
        try:
            from .risk_policy import _account_equity_usd
            from ..execution_family_registry import EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP

            basis_usd = float(_account_equity_usd(
                EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP, apply_margin_multiple=False, prefer_equity=True) or 0.0)
        except Exception:
            basis_usd = 0.0
    if basis_usd <= 0:
        basis_usd = BASIS_USD  # fixed-basis fallback (broker equity unavailable)
    risk_per_trade_usd = basis_usd * float(getattr(settings, "chili_momentum_risk_loss_fraction_of_equity", 0.01) or 0.01)
    notional_cap_usd = basis_usd * float(getattr(settings, "chili_momentum_risk_notional_fraction_of_equity", 0.15) or 0.15)
    daily_loss_cap_usd = basis_usd * float(getattr(settings, "chili_global_max_daily_loss_pct_of_equity", 0.05) or 0.05)

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
        for ts, bid, ask, _sbps, dvol, *_rest in rows:
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

    # RECORDED-FILLS CONSUMER ("RECORD don't derive"): for armed_source=='live' + flag ON,
    # load the day's REAL broker fills and partition the live-armed universe into
    #   * recorded_filled_syms — live ARMED *and* recorded a broker entry fill: the replay
    #     emits these from the RECORDED round-trips (exact entry/exit/spread/$/qty) and the
    #     derived tape model is BYPASSED for them.
    #   * live_armed_syms \ recorded_filled_syms — live armed but the recorded truth shows NO
    #     fill (live cancelled pre-entry): the replay DROPS them (trace gate_fail:live_cancelled)
    #     so the over-firing derived auto-fill can't invent a trade live never took.
    # A name the replay arms that live NEVER armed (pure counterfactual) is untouched here and
    # keeps the DERIVED model (tagged ':counterfactual' at emission). OFF / non-live ⇒ all empty.
    recorded_fills: dict[str, list[dict]] = {}
    live_armed_syms: set[str] = set()
    recorded_filled_syms: set[str] = set()
    use_recorded = bool(REPLAY_RECORDED_FILLS and armed_source == "live")
    if use_recorded:
        live_armed_syms = set(live_spans or {})
        try:
            recorded_fills = _load_recorded_fills(date)
        except Exception:
            logger.warning("[replay_v2] recorded-fill load failed; falling back to derived", exc_info=True)
            recorded_fills = {}
            use_recorded = False
        recorded_filled_syms = {s for s in recorded_fills if s in live_armed_syms}
        result["recorded_fills_consumer"] = {
            "enabled": True,
            "live_armed": len(live_armed_syms),
            "recorded_filled": sorted(recorded_filled_syms),
            "live_cancelled_dropped": sorted(live_armed_syms - recorded_filled_syms),
        }

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
    # CONVERGENCE GATES (2026-06-14): the replay must reproduce LIVE entry DISCIPLINE,
    # not over-trade. Live keys a SESSION (arm→cooldown) for concurrency + re-entry, so
    # a name can't stack while alive and the lane holds only ~N at once. Each gate below
    # is a faithful mirror of a real live gate + its config knob (no magic numbers):
    #   G1 concurrency cap = adaptive_max_concurrent_live_sessions base (auto_arm.py:985)
    #   G2 post-loss cooldown + 2-strike = _symbol_loss_guards (auto_arm.py:776-819)
    # Without these the replay re-entered GMM 6x/VSME 5x (36 trades vs live's 8 on 06/12).
    MAX_OPEN_CONCURRENT = int(getattr(settings, "chili_momentum_risk_max_concurrent_live_sessions", 5) or 5)
    MAX_DAILY_STOPOUTS = int(getattr(settings, "chili_momentum_symbol_max_daily_stopouts", 2) or 2)
    LOSS_COOLDOWN_MIN = float(getattr(settings, "chili_momentum_symbol_loss_cooldown_min", 5.0) or 5.0)
    loss_cooldown_until: dict = {}   # symbol -> re-arm-allowed time (tz-aware), set on a LOSS only
    loss_strikes: dict = {}          # symbol -> count of losing trades today
    state = {"cum": 0.0, "peak": 0.0, "halted": None}
    day_grid = pd.date_range(f"{date} {_premarket_utc_hhmm(date)}:00", f"{date} 19:59:00", freq="1min", tz="UTC")
    # ── FIDELITY-V2 / ENGINE-ON running ledgers (no-op when both flags OFF) ──────────
    # ENGINE-ON aggregate dollars-at-risk across OPEN positions (Σ(entry-stop)*qty),
    # accumulated on fill + released on close (mirrors aggregate_open_risk_usd); the
    # admit_by_aggregate_risk gate reads it. FIDELITY-V2 per-minute fill governor + the
    # irreducible-tail names the confidence band brackets.
    engine_open_risk_usd = {"v": 0.0}
    fill_governor = _ReplayFillGovernor() if REPLAY_FIDELITY_V2 else None
    band_tail = {"rail_4xx": [], "tape_ceiling_miss": []}  # symbols feeding the band

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

    def _full_pipeline_rank(now) -> list[str]:
        """FULL-PIPELINE armed_source (2026-06-15): re-run the REAL selection
        pipeline AS-OF ``now`` from raw tape, so the replay can test whether a NEW
        selection/scoring change would arm names the recorded day missed.

        HONEST as-of scope (what IS / ISN'T re-run, no faking):
          * Stage 1 (re-SCREEN) — IS as-of: a Massive-shaped snapshot is synthesized
            from the tape state at ``now`` ONLY (each candidate's last bid/ask/dvol
            ≤ now, change vs its tape open, the minute-bar accumulated volume = dvol),
            and ``build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=...)`` applies
            the REAL price/$-vol/change screen + freshness×move rank. No EOD lookahead.
          * Stage 2 (re-SCORE) — IS as-of: ``score_universe`` (the SAME liquidity-biased
            Ross percentile ranker the live lane + asof_rank use) re-ranks the Stage-1
            survivors from the same as-of signals.
          * Stage 3/4 (re-ARM / re-ENTER) — IS as-of: the caller arms from THIS ranked
            list through the SAME freshness filter, slot cap, and entry trigger the
            'asof' path uses (shared code below).
        LIMITATION (documented, not faked): the snapshot is built from the NBBO tape
        the sampler recorded (Ross-universe names with a clean quote), NOT a frozen
        copy of the full Massive market snapshot as it existed at ``now`` — a name the
        live sampler never quoted that minute has no tape row and cannot re-enter the
        pool here (the tape IS the candidate ceiling, same as 'asof'). Fields the
        snapshot can't reconstruct (lastQuote sub-fields, prevDay) are omitted; the
        screen reads the ones present (day.c/day.v/min.av/todaysChangePerc), which is
        exactly what ``build_equity_universe`` needs.
        """
        snapshot: list[dict] = []
        sigs: dict[str, dict] = {}
        for s in cand:
            if qualify_at[s] > _aware(now):
                continue
            q = tape.at(s, now)
            if q is None:
                continue
            bid, ask, _sbps, dvol = q
            mid = (bid + ask) / 2.0
            if mid <= 0:
                continue
            o = open_px.get(s) or mid
            chg = (mid - o) / o * 100.0 if o > 0 else 0.0
            # Massive-snapshot shape build_equity_universe screens on (as-of fields only).
            snapshot.append({
                "ticker": s,
                "day": {"c": mid, "v": dvol, "o": o},
                "min": {"c": mid, "av": dvol},
                "lastQuote": {"p": bid, "P": ask},
                "todaysChangePerc": chg,
            })
            sig = {"daily_change_pct": chg, "dollar_volume": mid * dvol}
            if adv.get(s):
                sig["rvol"] = dvol / adv[s]
            sigs[s] = sig
        if not snapshot:
            return []
        # Stage 1: the REAL universe screen + freshness×move rank (as-of snapshot).
        try:
            screened = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snapshot)
        except Exception:
            screened = []
        screened_set = {str(x).upper() for x in screened}
        if not screened_set:
            return []
        # Stage 2: re-score ONLY the Stage-1 survivors with the live percentile ranker.
        sub = {s: sigs[s] for s in sigs if s.upper() in screened_set}
        if not sub:
            return []
        scored = score_universe(sub, weights=ROSS_PILLAR_WEIGHTS_LIQUIDITY_BIASED)
        ranked = [r.symbol for r in sorted(scored.values(), key=lambda r: r.rank)]
        # Stage 2b: reflect the LIVE eligibility FLOOR (gap #3) — live marks a name below
        # Ross's RVOL/change floor as NOT live-eligible, so the replay must drop the same
        # names or it would arm a set live never would. As-of faithful: reads the same
        # as-of signals (daily_change_pct + rvol) the floor uses live. (The viability
        # RANKING tilts — sympathy/gainer/catalyst-grade/dilution/close-strength — reorder
        # WITHIN this set; the structural ones are as-of, the fetch-based ones would read
        # CURRENT external data in replay, so they are intentionally NOT re-applied here —
        # the floor is the gate that decides WHO arms, which is what changes the trade set.)
        try:
            from .ross_momentum import below_explosive_floor

            ranked = [s for s in ranked if not below_explosive_floor(sub.get(s, sub.get(s.upper(), {})))]
        except Exception:
            pass
        return ranked

    def _rank(now) -> list[str]:
        """Dispatch the as-of ranker: full-pipeline re-screen+re-score when enabled,
        else the standard as-of rank. Byte-identical to before when the flag is OFF."""
        if armed_source == "full_pipeline":
            return _full_pipeline_rank(now)
        return asof_rank(now)

    def _completed_upto(s: str, now):
        """The completed-bars frame the entry trigger sees at ``now`` (no lookahead:
        a bar indexed by START closes at +ENTRY_BAR_MIN) — the SAME slice the entry
        loop builds, so freshness and the trigger read one frame."""
        df, _c = day_frame(s)
        if df is None:
            return None
        upto = df[df.index <= now - pd.Timedelta(minutes=ENTRY_BAR_MIN)]
        return upto if len(upto) >= 12 else None

    def _trigger_firing_now(s: str, upto) -> bool:
        """Is the generic pullback break firing on the completed bars at ``now``?
        Reuses the SAME per-(symbol, last-bar) cache the entry loop uses so the gate
        and the entry decision agree and the 1-min grid doesn't multiply trigger cost.
        Bar-level only (no live_price / no tick-break) — a firing bar is always a
        valid arm, mirroring live auto_arm's 'a name whose break is FIRING is always
        a valid entry' rule."""
        try:
            _bar_key = (s, str(upto.index[-1]))
            if _bar_key in trigger_cache:
                ok = trigger_cache[_bar_key][0]
            else:
                # Same as_of L2 plumbing as the entry-decision call so the shared cache
                # is consistent under Gate 3 (default-OFF ⇒ db unused ⇒ byte-identical).
                try:
                    _l2_asof = _aware(upto.index[-1]).replace(tzinfo=None)
                except Exception:
                    _l2_asof = None
                res = momentum_pullback_trigger(
                    upto, entry_interval=ENTRY_INTERVAL, db=_l2db, l2_as_of=_l2_asof,
                )
                trigger_cache[_bar_key] = res
                ok = res[0]
            return bool(ok)
        except Exception:
            return False

    def _arm_freshness_ok(s: str, now) -> bool:
        """SELECTION->ENTRY ALIGNMENT — replay parity with the live auto_arm's
        fresh-impulse discipline (auto_arm.run_auto_arm_pass: _require_fresh_impulse /
        _candidate_freshness / _known_fresh). The live lane does NOT pin a watch slot
        on a FADED 24h leader; it watches the freshest in-impulse name and lets a
        FIRING break arm anything. Mirror that here so the replay arms the same set
        live would:
          (1) trigger FIRING now -> always armable (a firing break is always valid);
          (2) otherwise arm only a name we POSITIVELY know is in a fresh up-impulse
              (intraday_impulse_freshness.is_fresh) — drop faded names;
          (3) UNKNOWN freshness (no/insufficient bars) -> NOT armed proactively, same
              as live (_known_fresh treats None as not-fresh); only a firing break
              arms an unknown.
        Computed completed-bars-only from the data the replay HAS at ``now`` (no
        lookahead). Reuses the SAME ``intraday_impulse_freshness`` helper the live
        auto_arm calls (parity by construction). Disable via the kill-switch knob to
        restore viability-rank-only arming."""
        if not REPLAY_FRESHNESS_FILTER:
            return True
        upto = _completed_upto(s, now)
        if upto is None:
            # No completed-bars frame yet — the entry loop will skip it anyway
            # (len(upto) < 12), so let it arm (a watch with no bars is inert and
            # the firing/fresh decision is made the moment bars exist). This keeps
            # the gate from depending on the OHLCV feed's per-name lookback depth.
            return True
        return freshness_arm_decision(upto, firing=_trigger_firing_now(s, upto))

    def close_trade(s: str, p: dict, exit_px: float, why: str, when=None) -> None:
        pnl = (exit_px - p["entry"]) * p["qty"]
        state["cum"] += pnl
        state["peak"] = max(state["peak"], state["cum"])
        # run-R instrumentation (additive — does NOT touch pnl/cum/fills/exits, so the
        # day totals stay byte-identical). MFE = the maintained bid high-water over the
        # hold (what we could have realized); run_R normalizes it by the ORIGINAL
        # structural risk. The MESO separator: winners thrust, losers fade (~0R).
        _mfe_px = max(float(p.get("hwm", p["entry"])), float(p["entry"]))
        _run_r = _run_r_value(p["entry"], p.get("stop0", p["stop"]), _mfe_px)
        # RECORDED-FILLS CONSUMER: when active, any DERIVED trade that reaches here is a
        # pure COUNTERFACTUAL (live-armed-and-filled names are emitted from the recorded
        # round-trips, not here; live-armed-not-filled names are dropped) — so tag its why
        # ':counterfactual' + source='derived' to distinguish it from the recorded_live set.
        # OFF ⇒ no new keys / no suffix (byte-identical why + dict).
        _extra: dict = {}
        if use_recorded:
            why = why + ":counterfactual"
            _extra["source"] = "derived"
        trades.append({**p["meta"], "exit": round(exit_px, 4), "why": why,
                       "exit_t": (str(when)[11:16] if when is not None else None),
                       "stop": round(p.get("stop0", p["stop"]), 4),
                       "target": round(p["target"], 4),
                       "mfe_px": round(_mfe_px, 4), "run_r": round(_run_r, 2),
                       **_extra,
                       "usd": round(pnl + p.get("scale_usd", 0.0), 0)})
        # G2: per-symbol post-loss discipline — mirror live _symbol_loss_guards (only a
        # net LOSS cools down + strikes; a WIN stays re-armable, preserving the legit
        # winner re-entry live takes — the ASTN-twice case — so this is not overfit).
        if pnl + p.get("scale_usd", 0.0) < 0:
            _w = when if when is not None else now
            loss_strikes[s] = loss_strikes.get(s, 0) + 1
            loss_cooldown_until[s] = _aware(_w) + timedelta(minutes=LOSS_COOLDOWN_MIN)
        # ENGINE-ON: release this position's dollars-at-risk from the running aggregate
        # (mirrors the live atomic boundary releasing on exit). No-op when the flag is OFF
        # (engine_open_risk_usd stays 0.0 because nothing accumulated into it).
        if REPLAY_ENGINE_ON:
            engine_open_risk_usd["v"] = max(0.0, engine_open_risk_usd["v"] - float(p.get("open_risk_usd", 0.0)))
        del open_pos[s]
        if state["halted"] is None and state["cum"] <= -daily_loss_cap_usd:
            state["halted"] = "daily_loss"
        elif state["halted"] is None and state["peak"] >= daily_loss_cap_usd and state["cum"] <= state["peak"] * (1 - GIVEBACK_FRAC):
            state["halted"] = "giveback"

    def manage_open(now, _l2db=None) -> None:
        # Live manages positions every ~30s tick on NBBO bid (live_runner.py:2550+,
        # 2837-2924); the tape's 1-min cadence is the replay analog. All live exits
        # are MARKET sells realized near the bid — exits here price AT the tape bid.
        # ORDER CONTRACT (mirrors live STATE_LIVE_TRAILING): hwm → partial/arm →
        # cushion trail → v1 ofi_exhaustion_lock → v2 sell_into_strength_ladder
        # (gain-side, ratchet-only) → LOSS-SIDE BREACH **LAST**, tested against the
        # freshly-ratcheted stop, with the L2 anti-shake-out hold. Do NOT move the
        # breach above the trail block — a winner would close against a stale stop.
        for s in list(open_pos):
            p = open_pos[s]
            if tape.in_halt(s, now):
                continue  # no quotes inside the gap; the resume sample handles any breach
            q = tape.at(s, now)
            if q is None:
                continue
            bid = q[0]
            p["hwm"] = max(p["hwm"], bid)
            _as_of = _aware(now).replace(tzinfo=None)  # UTC-naive instant for the as-of L2 reads
            # partial at the first target — live fires at bid >= target*0.995 and
            # sells scale_out_fraction of the ORIGINAL qty (live_runner.py:2916-2964)
            if not p["scaled"] and bid >= p["target"] * TARGET_FIRE_FRAC:
                p["scaled"] = True
                part = min(p["qty"], p["qty0"] * scale_out_fraction(symbol=s))
                p["scale_usd"] = (bid - p["entry"]) * part
                state["cum"] += p["scale_usd"]
                p["qty"] -= part
                p["stop"] = max(p["stop"], p["entry"])  # breakeven_stop_after_partial: ratchet only
            # live also arms trailing pre-partial once bid clears entry by
            # trail_activate_return_bps (live_runner.py:3033-3037)
            if not p["trail_armed"] and bid >= p["entry"] * (1.0 + TRAIL_ACTIVATE_BPS / 10_000.0):
                p["trail_armed"] = True
            if p["scaled"] or p["trail_armed"]:
                # cushion-adaptive trail (Ross day-4): width scales with this
                # position's unrealized R + the day's banked R — same primitive
                # live uses (parity by construction). 2026-06-12: live now
                # anchors >=1R runners to the 5m EMA9 — the replay passes the
                # SAME anchor from its cached 5m bars (completed bars only,
                # the lookahead rule) so the parity contract holds.
                _e5 = None
                try:
                    _df5 = bars(s)
                    if _df5 is not None and len(_df5) >= 9:
                        import pandas as _pd

                        _now_a = _aware(now)
                        _idx = _df5.index
                        if getattr(_idx, "tz", None) is None:
                            _cut = _pd.Timestamp(now) - _pd.Timedelta(minutes=5)
                        else:
                            _cut = _pd.Timestamp(_now_a) - _pd.Timedelta(minutes=5)
                        _win = _df5[_idx <= _cut]
                        if len(_win) >= 9:
                            _e5 = float(_win["Close"].ewm(span=9, adjust=False).mean().iloc[-1])
                except Exception:
                    _e5 = None
                p["stop"] = cushion_adaptive_trail_stop(
                    high_water_mark=p["hwm"], entry_price=p["entry"],
                    atr_pct=p["atrp"], stop_atr_mult=STOP_ATR_MULT,
                    day_realized_usd=float(state["cum"]),
                    position_risk_usd=(p["entry"] * max(0.003, p["atrp"] * STOP_ATR_MULT)) * p["qty0"],
                    breakeven_floor=p["entry"] if p["scaled"] else p["stop0"],
                    current_stop=p["stop"], side_long=True,
                    ema_5m=_e5)
                # v1 ofi_exhaustion_lock — flow-confirmed gain-side tighten (live_runner
                # 3842-3922). Reads L2 AS-OF the sim minute; INVARIANT A: ratchet-only.
                if (s.endswith("-USD") or bool(getattr(settings, "chili_momentum_exit_adaptive_equity_enabled", True))) \
                        and bool(getattr(settings, "chili_momentum_exit_ofi_lock_enabled", True)):
                    try:
                        _ofi_x, _mpe_x = _live_ofi_microprice(s, db=_l2db, as_of=_as_of)
                        _band_bps = ((p["hwm"] - p["stop"]) / p["hwm"] * 10_000.0) if p["hwm"] > 0 else 0.0
                        _lock = ofi_exhaustion_lock(
                            high_water_mark=p["hwm"], entry_price=p["entry"], bid=bid,
                            atr_pct=p["atrp"], stop_atr_mult=STOP_ATR_MULT,
                            ofi=_ofi_x, micro_edge=_mpe_x, hidden_seller=None,
                            reward_risk=class_aware_reward_risk(s),
                            current_stop=p["stop"],
                            breakeven_floor=(p["entry"] if p["scaled"] else p["stop0"]),
                            current_band_bps=_band_bps, side_long=True)
                        _ls = _lock.get("new_stop_floor")
                        if _lock.get("fired") and _ls is not None and _ls > p["stop"]:
                            p["stop"] = _ls  # INVARIANT A: ratchet-only
                    except Exception:
                        pass
                # v2 sell_into_strength_ladder — distribution-aware ratchet (live_runner
                # 3932-4045). Action A (stop ratchet) only; the size-MOVING resting limit
                # is gated live by exit_ladder_live (default False), so the replay
                # faithfully omits it (no adapter to rest a limit against).
                if (s.endswith("-USD") or bool(getattr(settings, "chili_momentum_exit_adaptive_equity_enabled", True))) \
                        and bool(getattr(settings, "chili_momentum_exit_ladder_enabled", True)):
                    try:
                        _ladder = read_ladder_distribution(s, db=_l2db, as_of=_as_of)
                        _sis = sell_into_strength_ladder(
                            high_water_mark=p["hwm"], entry_price=p["entry"], bid=bid,
                            atr_pct=p["atrp"], stop_atr_mult=STOP_ATR_MULT,
                            reward_risk=class_aware_reward_risk(s),
                            current_stop=p["stop"],
                            breakeven_floor=(p["entry"] if p["scaled"] else p["stop0"]),
                            remaining_qty=p["qty"], ladder=_ladder,
                            prior_partial_taken=bool(p["scaled"]),
                            cooldown_active=False, side_long=True)
                        _ss = _sis.get("new_stop_floor")
                        if _sis.get("fired") and _ss is not None and _ss > p["stop"]:
                            p["stop"] = _ss  # INVARIANT A: ratchet-only
                    except Exception:
                        pass
                # RISK-NEUTRAL CONFIRMATION PYRAMID (replay mirror of live_runner's
                # add-decision block). Gated on the SAME flag => OFF is byte-identical
                # in replay too. Same cushion+confirm predicate as live: cushion banked
                # in original-R0 units, new-HOD (bid >= p["hwm"], already recomputed
                # this minute), OFI thrust (as-of the sim minute), trail ratcheted since
                # entry, equity-only, no midday lull. The add fills AT THE TAPE BID (the
                # marketable-buy analog — replay exits already price at the bid). On add:
                # blend entry/qty, GROW qty0 (so the scale-out de-risks the enlarged
                # size), ratchet p["stop"] up (INVARIANT-A), and freeze p["pyr_R0"] (the
                # GUARD-#1 loss-side clamp anchor applied at the breach below).
                if bool(getattr(settings, "chili_momentum_pyramid_enabled", False)):
                    try:
                        # Freeze the STARTER basis ONCE (entry0/qty0_starter/d0) so a
                        # prior add never re-bases R0. d0 = the original stop distance.
                        p.setdefault("entry0", p["entry"])
                        p.setdefault("qty0_starter", p["qty0"])
                        p.setdefault("pyr_d0", p["entry0"] - p["stop0"])
                        p.setdefault("pyr_entry_stop_ref", p["stop"])
                        _a0s = p["entry0"]
                        _q0s = p["qty0_starter"]
                        _d0 = p["pyr_d0"]
                        _o, _ = _live_ofi_microprice(s, db=_l2db, as_of=_as_of)
                        # SHARED pure predicate — IDENTICAL to live_runner's gate.
                        _decn = pyramid_add_decision(
                            enabled=True,
                            is_equity=not s.endswith("-USD"),
                            add_count=int(p.get("pyr_adds") or 0),
                            max_adds=int(getattr(settings, "chili_momentum_pyramid_max_adds", 1) or 1),
                            in_flight=False,  # replay fills instantly; no in-flight order
                            a0=_a0s, q0=_q0s, d0=_d0,
                            bid=bid, stop_px=p["stop"],
                            entry_stop_ref=p["pyr_entry_stop_ref"],
                            high_water_mark=p["hwm"],
                            ofi=_o,
                            ofi_threshold=float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25),
                            min_cushion_r=float(getattr(settings, "chili_momentum_pyramid_min_cushion_r", 1.0) or 1.0),
                            midday_lull=False,  # tape day is RTH momentum; lull handled at arm
                        )
                        if _decn.get("fire") and _decn.get("R0"):
                            _R0 = float(_decn["R0"])
                            _rho = float(getattr(settings, "chili_momentum_pyramid_add_risk_fraction", 0.5) or 0.5)
                            _qa = (_rho * _R0 / _d0) if _d0 > 0 else 0.0
                            if _qa > 0:
                                # The add fills AT THE TAPE BID (marketable-buy analog).
                                _blend = pyramid_blend_on_fill(
                                    q0=p["qty"], a0=p["entry"], qa_f=_qa, Pa_f=bid,
                                    stop_px=p["stop"], original_quantity=p["qty0"],
                                )
                                p["qty"] = _blend["q1"]
                                p["qty0"] = _blend["original_quantity"]  # grow original
                                p["entry"] = _blend["a1"]
                                p["stop"] = _blend["s1"]                 # INVARIANT-A
                                p["pyr_R0"] = _R0                        # GUARD-#1 anchor
                                p["pyr_adds"] = int(p.get("pyr_adds") or 0) + 1
                    except Exception:
                        pass
            # LOSS-SIDE BREACH **LAST** — vs the freshly-ratcheted stop (mirrors live
            # order). L2 anti-shake-out: a CHOP-classified breach rides one bounded beat
            # (the OPG-USD shake-out); a BREAKDOWN / stale-or-missing L2 sells now. The
            # hold NEVER touches the stop (INVARIANT A intact); it only delays the sell.
            if bid <= p["stop"]:
                _do_hold = False
                if bool(getattr(settings, "chili_momentum_stop_l2_confirm_enabled", False)):
                    try:
                        _thr = float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25)
                        _mage = float(getattr(settings, "chili_momentum_stop_l2_confirm_max_age_s", 2.5) or 2.5)
                        _msnap = int(getattr(settings, "chili_momentum_stop_l2_confirm_min_snaps", 3) or 3)
                        _mtick = int(getattr(settings, "chili_momentum_stop_l2_confirm_max_ticks", 2) or 2)
                        _bl = read_ladder_distribution(s, db=_l2db, as_of=_as_of)
                        _bc = classify_stop_breach(ladder=_bl, ofi_threshold=_thr,
                                                   max_age_s=_mage, min_snaps=_msnap)
                        _holds = int(p.get("stop_breach_chop_holds") or 0)
                        if _bc.get("cls") == "CHOP" and _holds < _mtick:
                            p["stop_breach_chop_holds"] = _holds + 1
                            _do_hold = True
                    except Exception:
                        _do_hold = False  # any L2 miss => sell (protective)
                if _do_hold:
                    continue  # ride one bounded beat
                p.pop("stop_breach_chop_holds", None)
                why = "trail_stop" if (p["scaled"] or p["trail_armed"]) and p["stop"] > p["stop0"] else "stop"
                # GUARD-#1 (replay mirror of the #769 max-loss circuit — T1.1 fidelity fix
                # 2026-06-27): the LIVE runner caps EVERY entry's realized loss via
                # risk_policy.max_loss_circuit_decision (floor = avg - k*stop_distance, basis =
                # structural stop_distance*qty) when chili_momentum_max_loss_circuit_enabled is on
                # — NOT only pyramids. The old replay mirror floored ONLY pyramided positions
                # (pyr_R0 set), so single-entry gap-throughs (99% of fills) exited at the raw deep
                # bid and over-counted the loss tail vs live (the -$1,988-vs-$345 5.8x driver).
                # Wire the SAME live leaf with the SAME args (pyr_R0 threads as the risk anchor,
                # exactly as live le["pyramid_risk_anchor_usd"]): a breach exits AT the floor
                # (<= ~k*R); a clean stop (bid > floor / no breach) is byte-identical to the raw
                # bid. Flag OFF => exit at the raw bid (pre-circuit byte-identical).
                _exit_px = bid
                if bool(getattr(settings, "chili_momentum_max_loss_circuit_enabled", False)):
                    from .risk_policy import max_loss_circuit_decision
                    _sd_circ = float(p["entry"]) - float(p.get("stop0", p["stop"]))
                    _k_circ = float(getattr(settings, "chili_momentum_max_loss_risk_multiple", 2.0) or 2.0)
                    _circ = max_loss_circuit_decision(
                        avg=p["entry"], qty=p["qty"], stop_distance=_sd_circ, bid=bid, k=_k_circ,
                        risk_anchor_usd=p.get("pyr_R0"),
                    )
                    if _circ.get("breach") and _circ.get("floor_price") is not None:
                        _exit_px = max(bid, float(_circ["floor_price"]))
                close_trade(s, p, _exit_px, why, when=now)
                continue
            # flicker recovery: bid back above stop clears the hold counter (live 4047-4054)
            if p.get("stop_breach_chop_holds"):
                p.pop("stop_breach_chop_holds", None)

    _l2db = SessionLocal()  # read-only L2 session spanning the day-grid (SELECT-only)
    try:
      for now in day_grid:
        manage_open(now, _l2db)
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
            ranked = _rank(now)   # full_pipeline re-screen+re-score when on; asof otherwise
            pos = {s: i for i, s in enumerate(ranked)}
            # ENGINE-ON (B): the WATCH-fanout cap floats with the live-eligible FIELD SIZE
            # (adaptive_watch_fanout) instead of the fixed MAX_SLOTS — wider watch breadth
            # when many names are igniting. field_size = the eligible-name count at `now`
            # (``ranked`` IS that as-of field). Reads the SAME watch_fanout settings the
            # live auto_arm reads. OFF ⇒ _slots == MAX_SLOTS (byte-identical). HONEST: on
            # this data the slot cap rarely binds, so this barely moves the trade-set.
            _slots = MAX_SLOTS
            if REPLAY_ENGINE_ON:
                from .risk_policy import adaptive_watch_fanout as _awf
                _slots = int(_awf(len(ranked)))
            for s in ranked:
                if s in armed or s in open_pos:
                    continue
                # SELECTION->ENTRY ALIGNMENT: drop FADED non-firing names from the
                # watch slot, exactly like the live auto_arm (parity). A faded 24h
                # leader pinned a slot in the old replay (e.g. SMSI armed 13:56 at
                # position 0.005 in its range) and inflated the arm count vs live.
                if not _arm_freshness_ok(s, now):
                    _tr(s, "arm_skip:faded_impulse", now)
                    continue
                if len(armed) < _slots:
                    armed[s] = {"since": now}
                    armed_spans[s].append([str(now)[11:16], None])
                    continue
                if pos[s] >= _slots:
                    break  # ranked is ordered — nothing further down can displace either
                # Displacement arming: live re-scans continuously, so a newly-hot name
                # (e.g. a halt-resume pop) gets armed within minutes; first-come-slots +
                # 30-min reaps made the replay arm it ~20 min late. A top-MAX_SLOTS
                # newcomer takes the slot of the worst-ranked armed symbol — but only
                # one that itself FELL OUT of the top set (hysteresis: an armed symbol
                # still holding a top rank is never displaced, so pullback dips that
                # stay top-ranked keep their watcher while the entry forms).
                evict = max((a for a in armed if a not in open_pos), key=lambda a: pos.get(a, 1 << 30), default=None)
                if evict is None or pos.get(evict, 1 << 30) < _slots:
                    break  # every armed symbol still holds a top rank
                if armed_spans[evict] and armed_spans[evict][-1][1] is None:
                    armed_spans[evict][-1][1] = str(now)[11:16]
                del armed[evict]
                armed[s] = {"since": now}
                armed_spans[s].append([str(now)[11:16], None])
        for s in list(armed):
            if s in open_pos:
                continue
            # RECORDED-FILLS CONSUMER ("RECORD don't derive"): bypass the DERIVED fill model
            # for any live-armed name. A name the recorded broker truth shows FILLED is emitted
            # from its RECORDED round-trips (handled once, just below the loop), so skip it here;
            # a live-armed name with NO recorded fill = live CANCELLED it pre-entry, so DROP it
            # (the over-firing derived auto-fill must not invent a trade live never took). OFF /
            # non-live ⇒ this whole block is skipped (byte-identical). Counterfactual names (not
            # live-armed) never enter this branch and keep the derived model.
            if use_recorded and s in live_armed_syms:
                if s not in recorded_filled_syms:
                    _tr(s, "gate_fail:live_cancelled", now)
                # FILLED names are emitted from the recorded round-trips (post-loop); either
                # way the derived path does not run for a live-armed name.
                continue
            # G1: concurrency cap — live holds only ~MAX_OPEN_CONCURRENT positions at once
            # (adaptive_max_concurrent_live_sessions base); open_pos is the held analog.
            # ENGINE-ON (A): the FIXED count cap is replaced by the engine's shape-aware
            # admit_by_aggregate_risk — but the candidate's dollars-at-risk ((entry-stop)*qty)
            # are not known until the fill block computes the stop + qty, so the aggregate-risk
            # admission is evaluated THERE (search 'admit_by_aggregate_risk'); here we only skip
            # the fixed count gate when ENGINE_ON. OFF ⇒ the fixed count gate (byte-identical).
            if not REPLAY_ENGINE_ON and len(open_pos) >= MAX_OPEN_CONCURRENT:
                _tr(s, "gate_fail:concurrency_cap", now)
                continue
            # G2: per-symbol re-entry discipline — 2-strike (today's losses) then a
            # post-loss cooldown; mirrors live so a just-lost name can't immediately
            # re-stack (the GMM-6x / VSME-5x churn the replay used to invent).
            if loss_strikes.get(s, 0) >= MAX_DAILY_STOPOUTS:
                _tr(s, "gate_fail:loss_2strike", now)
                continue
            _cd_until = loss_cooldown_until.get(s)
            if _cd_until is not None and _aware(now) < _cd_until:
                _tr(s, "gate_fail:loss_cooldown", now)
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
            # Per-entry fidelity (tick-faithful): a bar/dip fire is a 1-min snapshot
            # entry; only the tick-faithful sub-minute walk below upgrades it to
            # 'ws_tick'. Defaulted here so it's always defined for the trade meta.
            _entry_fidelity = "snapshot_1min"
            _fire_ts = now
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
                    # Gate 3 (L2 entry veto) is default-OFF ⇒ db unused ⇒ byte-identical;
                    # when the operator flips it ON, the as-of L2 read exercises the veto
                    # against the day's historical book (the same as_of the exit reads use).
                    ok, _treason, dbg = momentum_pullback_trigger(
                        upto, entry_interval=ENTRY_INTERVAL,
                        db=_l2db, l2_as_of=_aware(now).replace(tzinfo=None),
                    )
                    trigger_cache[_bar_key] = (ok, _treason, dbg)
                # TICK-BREAK parity (live_runner does the same with the WS ask): the
                # completed-bar structure is valid but waiting, and THIS minute's tape
                # ask is already through the level -> re-evaluate uncached with the
                # live price so the tick-break path can fire mid-bar like live.
                if (
                    not ok
                    and _treason in TICK_ARMED_WAIT_REASONS
                    and isinstance(dbg, dict)
                    and dbg.get("pullback_high")
                ):
                    _lvl = float(dbg["pullback_high"])
                    if REPLAY_TICK_ENTRY:
                        # TICK-FAITHFUL: walk EVERY densified tick in the entry window in
                        # time order; the FIRST whose ask > pullback_high fires at THAT
                        # ts (true sub-minute resolution where WS ticks exist). SUPERSET:
                        # where only the 1-min sampler exists, prices_between returns the
                        # same single sample .at() saw ⇒ identical to the snapshot path.
                        _win_lo = _aware(now) - timedelta(minutes=ENTRY_BAR_MIN)
                        _hit = None
                        for _t, _b, _a, _src in tape.prices_between(s, _win_lo, _aware(now) + timedelta(seconds=1)):
                            if _a > _lvl:
                                _hit = (_t, _a, _src)
                                break
                        if _hit is not None:
                            _fire_ts = _hit[0]
                            ok2, _tr2, dbg2 = momentum_pullback_trigger(
                                upto, entry_interval=ENTRY_INTERVAL, live_price=float(_hit[1]),
                                now=_aware(_fire_ts),
                                db=_l2db, l2_as_of=_aware(_fire_ts).replace(tzinfo=None))
                            if ok2:
                                ok, _treason, dbg = ok2, _tr2, dbg2
                                # 'massive_snapshot' is the 1-min sampler; anything else is
                                # a real sub-minute WS tick (densified) → ws_tick fidelity.
                                _entry_fidelity = (
                                    "ws_tick" if _hit[2] and _hit[2] != "massive_snapshot"
                                    else "snapshot_1min"
                                )
                    else:
                        _q_tb = tape.at(s, now, max_stale_min=ENTRY_QUOTE_MAX_STALE_MIN)
                        if _q_tb is not None and _q_tb[1] > _lvl:
                            # pass the SIM time so the premarket tick-break confirmation
                            # (CUPR guard) evaluates the right session, not wall-clock now.
                            ok, _treason, dbg = momentum_pullback_trigger(
                                upto, entry_interval=ENTRY_INTERVAL, live_price=float(_q_tb[1]),
                                now=_aware(now),
                                db=_l2db, l2_as_of=_aware(now).replace(tzinfo=None))
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
            # SKIP-FOR-LIMITS PARITY (2026-06-23): mirror live_runner.py:2828-2839. When the
            # live default chili_momentum_skip_spread_gate_for_limit_entry is on, the marketable
            # LIMIT price bounds the cost, so the adaptive spread gate is skipped and only the
            # abs-cap broken-quote ceiling applies. Without this the replay UNDER-fills the wide
            # low-float movers live now accepts (the NXTS-class divergence on the under-fill side).
            _skip_spread = bool(getattr(settings, "chili_momentum_skip_spread_gate_for_limit_entry", True))
            _spread_ceiling = SPREAD_ABS_CAP_BPS if _skip_spread else max_spread
            # FIDELITY-V2 (STEP 2a) SPREAD-CEILING REJECT: the deterministic auto-fill below
            # passed 59 names the live rail killed (diagnosis). When fidelity_v2 is on, judge
            # the spread at the live RAIL's adaptive threshold (the SAME adaptive_max_spread_bps
            # gate, NOT the lenient skip-for-limits abs-cap) so wide low-float quotes the rail
            # would not have crossed are rejected (trace gate_fail:wide_spread). OFF ⇒ unchanged.
            if REPLAY_FIDELITY_V2:
                _spread_ceiling = max_spread
            if sbps > _spread_ceiling:
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
            # FIDELITY-V2 (STEP 2b) MARKETABLE-LIMIT FILL-OR-REJECT — the dominant over-fill
            # lever (302 of 451 reducible live cancels were cancelled_pre_entry: the resting
            # marketable LIMIT never crossed within the ack window, then cancelled). The
            # deterministic auto-fill above converts those to fills live cancels. When
            # fidelity_v2 is on, model the resting LIMIT as FILLED only if WITHIN THE ACK
            # WINDOW the tape shows the offer actually TRADED THROUGH the limit (an ask<=limit
            # print), else CANCELLED. The ack window = the SAME live backstop the runner uses:
            # chili_momentum_entry_max_rest_bars x the entry-interval seconds (live_runner.py
            # :6128-6131). Plus the per-minute fill-admission TOKEN BUCKET (STEP 2c) suppresses
            # the burst-fill artifact on high-fan-out minutes. OFF ⇒ none of this runs.
            if REPLAY_FIDELITY_V2:
                _rest_bars = float(getattr(settings, "chili_momentum_entry_max_rest_bars", 2.0) or 2.0)
                _ack_window_s = _rest_bars * float(ENTRY_BAR_MIN) * 60.0
                _ack_end = _aware(now) + timedelta(seconds=_ack_window_s)
                _through = False
                for _ts, _b, _a, _src in tape.prices_between(s, now, _ack_end):
                    if _a <= limit:  # the offer traded down through our resting limit
                        _through = True
                        break
                if not _through:
                    _tr(s, "gate_fail:limit_not_filled", now)
                    continue
                # STEP 2c per-minute fill-admission token bucket (rail-governor analog): a
                # burst of simultaneous triggers cannot all "fill" — admit up to the bucket,
                # defer the rest (live would 429/queue). OFF ⇒ admit() always True (no-op).
                if fill_governor is not None and not fill_governor.admit(now):
                    _tr(s, "gate_fail:fill_governor_deferred", now)
                    continue
            # T1.2 FILL-PRICE FIDELITY (2026-06-27): the marketable LIMIT rests at `limit`
            # (just above the ask) but FILLS as price pulls back toward the bid/mid, NOT by
            # sweeping the offer — 74 real broker entries averaged ~247bps BELOW the intended
            # limit (0 ever filled above it). Modeling fill@ask paid the full spread on every
            # entry that live does NOT, the dominant driver of the -$1,988-vs-$345 (5.8x) loss
            # overstatement. Realize at the MID — the tape-anchored central fill of a resting
            # marketable limit (the improvement scales with the live spread; NO hardcoded bps) —
            # bounded above by the limit (never worse) and below by the bid (never better than
            # the best bid). For wide low-float spreads this ~= the empirical -247bps; for tight
            # names it collapses to ~the ask (small improvement), exactly the live shape.
            fill_px = min(limit, max(bid, mid))
            # ── STEP 2 fill-source bookkeeping (default quote model unless prints override) ──
            _fill_source = "quote_fallback" if REPLAY_PRINTS_FILL else "quote"
            _prints_avail: float | None = None      # max fillable shares per the prints (None ⇒ unbounded)
            _prints_meta: dict | None = None
            _entry_partial_pf: float | None = None   # prints PARTIAL fraction (None ⇒ not applicable)
            # STEP 2 PRINTS-BASED FILL MODEL (docs/DESIGN/VERSION_AGNOSTIC_BACKTEST.md): the
            # quote-touch above can't SEE executions and OVER-fills. When the prints flag is on,
            # resolve the fill against the immutable TRADE PRINTS: an execution AT/THROUGH the
            # limit is direct evidence shares traded. The window opens at t0 − DERIVED review
            # latency (median submit-minus-detected of the lane's OWN recorded events × K; NO
            # magic constant) and closes at t0 + the SAME ack window the fidelity_v2 test uses
            # (chili_momentum_entry_max_rest_bars × entry-interval). queue_ahead = the L1 size-
            # at-touch proxy (degrade to 0 + low-confidence when absent). On status==CANCEL the
            # trade is DROPPED (gate_fail:prints_no_fill); else fill_vwap REPLACES the quote
            # fill and the available print size caps qty below (PARTIAL). The qty-dependent
            # PARTIAL/CANCEL split is finalized AFTER sizing; here we capture the max fillable +
            # the measured vwap. OFF ⇒ this whole block is skipped ⇒ byte-identical.
            if REPLAY_PRINTS_FILL and trade_tape is not None:
                _rest_bars_p = float(getattr(settings, "chili_momentum_entry_max_rest_bars", 2.0) or 2.0)
                _ack_window_p = _rest_bars_p * float(ENTRY_BAR_MIN) * 60.0
                # DERIVED review latency: the day's median lane latency (× K), fallback = the
                # name's inter-trade print cadence (× K) when no events exist. NO constant.
                _rev_lat = trade_tape.review_latency_s
                if _rev_lat is None:
                    _rev_lat = trade_tape.cadence_s(s)
                _rev_lat = (float(_rev_lat) if _rev_lat is not None else 0.0) * REPLAY_REVIEW_LATENCY_K
                _qa = trade_tape.queue_ahead_at(s, now, side="long")
                # ask an unbounded qty so the decision returns the MAX fillable (cum_size −
                # queue_ahead) + the vwap; the qty-bounded PARTIAL/CANCEL is finalized after the
                # real sizing below (sizing needs fill_px, which the vwap here provides).
                _probe_qty = 1e12
                _pf = prints_fill_decision(
                    trade_tape, s, limit, _probe_qty, now, side="long",
                    queue_ahead=_qa, review_latency_s=_rev_lat,
                    ack_window_s=_ack_window_p, bid=bid,
                    participation=PARTICIPATION_CAP, min_size=1.0,
                )
                _prints_meta = _pf.get("meta")
                _fill_source = _pf.get("source", "quote_fallback")
                if _pf["status"] == "CANCEL" or float(_pf.get("filled_qty") or 0.0) <= 0.0:
                    # no execution at/through the limit in the window → live never filled here
                    _tr(s, "gate_fail:prints_no_fill", now)
                    continue
                # measured fill price REPLACES the quote estimate (already bounded [bid, limit])
                if _pf.get("fill_vwap") is not None:
                    fill_px = float(_pf["fill_vwap"])
                _prints_avail = float(_pf["filled_qty"])
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
                fill_px, atr_pct=eff, side_long=True, stop_atr_mult=STOP_ATR_MULT,
                reward_risk=class_aware_reward_risk(s))
            if not (0 < stop < fill_px):
                continue
            max_notional = min(notional_cap_usd, LIQ_FRACTION * mid * dvol)
            # L2.2 liquidity-scaled risk cap — PARITY with live: the SAME helper with the SAME
            # sbps + em_bps the spread gate above already computed (so the replay's size-shrink
            # == live's). OFF / mult==1.0 => byte-identical want_qty.
            _liq_mult = 1.0
            if bool(getattr(settings, "chili_momentum_liquidity_risk_cap_enabled", True)):
                from .risk_policy import spread_liquidity_risk_multiplier
                _liq_mult, _ = spread_liquidity_risk_multiplier(
                    sbps, em_bps,
                    floor=float(getattr(settings, "chili_momentum_liquidity_risk_floor", 0.5) or 0.5))
            # T1.3 SIZING PARITY (2026-06-27): route through the SAME live leaf
            # compute_risk_first_quantity the runner calls (live_runner.py:6810) instead of a bare
            # division. The bare `(risk)/(fill-stop)` OVER-sized — no 0.003 stop-distance floor, no
            # whole-share rounding, no below-min-size rejection, no consistent notional ceiling —
            # inflating per-trade $ (and thus loss magnitude) on chaotic tight-stop low-float names.
            # atr_pct=eff is the structural-or-vol-floored stop the exit also uses, so qty is sized
            # off the same stop (matching the old denominator) but with the live floors/rounding.
            # FIDELITY-V2 (STEP 1) SIZE FIDELITY — the biggest + most consistent lever: replay
            # sized $1,994 median vs live $463 (~4.3x) because it modeled only the BASE risk
            # budget. Multiply the per-trade max-loss by the LIVE ~18-dial de-risk stack
            # (cushion / green-day / streak / per-name de-risk) — each via the SAME live
            # callable's bounded formula, computed against the REPLAY's OWN running simulated
            # state (state["cum"] day P&L for cushion; the closed-trade P&L list for the streak;
            # per-symbol loss_strikes for the per-name de-risk), capped at the SAME base*3.0
            # combined ceiling the runner uses. NO hardcoded multipliers. OFF ⇒ _stack_mult=1.0
            # ⇒ byte-identical max_loss_usd.
            _stack_mult = 1.0
            if REPLAY_FIDELITY_V2:
                _stack_mult, _stack_meta = _replay_derisk_stack_multiplier(
                    cum_pnl_usd=state["cum"], peak_pnl_usd=state["peak"],
                    base_loss_usd=risk_per_trade_usd,
                    trade_pnls=[float(t.get("usd", 0.0)) for t in trades],
                    symbol_loss_strikes=loss_strikes.get(s, 0),
                )
            # (The remaining T1.3 piece = the full ~18-dial risk-budget stack; budget here stays the
            # liquidity-adjusted base, the dominant term on these days where cushion/green-day~=1.0.)
            from .risk_policy import compute_risk_first_quantity
            want_qty, _rf_meta = compute_risk_first_quantity(
                entry_price=fill_px, atr_pct=eff,
                max_loss_usd=risk_per_trade_usd * _liq_mult * _stack_mult,
                max_notional_ceiling_usd=max_notional,
                base_increment=1.0, base_min_size=1.0, stop_atr_mult=STOP_ATR_MULT,
            )
            if not want_qty or want_qty < 1.0:
                _tr(s, "gate_fail:%s" % (_rf_meta.get("reason", "below_min_size")), now)
                continue
            # shares printed over the next ~minute: diff day-volume against the row
            # ~55s ahead — on the dense WS tape (rows every ~1s) the immediate next
            # row would show a near-zero diff and falsely reject for no liquidity
            _vol_row = tape.first_after(s, _aware(now) + timedelta(seconds=55))
            _ref_dvol = float(_vol_row[4]) if _vol_row is not None else float(nxt[4])
            minute_vol = max(0.0, _ref_dvol - dvol)
            qty = min(want_qty, PARTICIPATION_CAP * minute_vol)
            if qty <= 0:
                _tr(s, "gate_fail:no_liquidity_printed", now)
                continue
            # STEP 2 PRINTS PARTIAL/CANCEL: cap the desired qty by the shares the PRINTS actually
            # delivered at/through the limit in the window (_prints_avail). filled>=desired ⇒ FILL;
            # 0<filled<desired ⇒ PARTIAL (fill what printed, cancel the remainder); below the live
            # min size ⇒ CANCEL (drop). OFF / no prints flag ⇒ _prints_avail is None ⇒ no-op.
            if _prints_avail is not None:
                # min size = the SAME base_min_size compute_risk_first_quantity used above (1.0
                # whole share); a sub-min partial is a CANCEL, not a fill. No new magic number.
                _min_size_pf = 1.0
                _filled_pf = max(0.0, min(qty, _prints_avail))
                if _filled_pf < _min_size_pf:
                    _tr(s, "gate_fail:prints_no_fill", now)
                    continue
                _entry_partial_pf = (_filled_pf / qty) if qty > 0 else 1.0
                qty = _filled_pf
            # ENGINE-ON (A) AGGREGATE-RISK ADMISSION — the candidate's SHAPE-AWARE dollars-at-
            # risk are now known: (entry-stop)*qty. Admit iff the running open aggregate plus
            # this candidate's risk stays within budget_fraction x equity, via the SAME live
            # admit_by_aggregate_risk helper + the SAME chili_momentum_max_aggregate_risk_pct_
            # of_equity budget (wiring the live risk_block denials the replay used to let
            # through). HONEST: on this data the budget rarely binds (peak ~9 tight-stop names
            # ~$1.2k vs ~$1.35k budget), so this is engine A/B fidelity, not a reliability lever.
            # OFF ⇒ this whole block is skipped (no aggregate maintained) ⇒ byte-identical.
            _cand_risk_usd = max(0.0, (fill_px - stop)) * qty
            if REPLAY_ENGINE_ON:
                from .risk_policy import admit_by_aggregate_risk as _admit
                _ok_adm, _adm_meta = _admit(
                    open_risk_usd=engine_open_risk_usd["v"],
                    candidate_risk_usd=_cand_risk_usd,
                    equity_usd=basis_usd,
                    budget_fraction=float(getattr(settings, "chili_momentum_max_aggregate_risk_pct_of_equity", 0.03) or 0.0),
                )
                if not _ok_adm:
                    _tr(s, "gate_fail:risk_block", now)
                    continue
                engine_open_risk_usd["v"] += _cand_risk_usd
            # FIDELITY-V2 (STEP 3 input) IRREDUCIBLE-TAIL TAG: a fill on a WIDE-spread low-float
            # name is exactly the rail-4xx-reject class (LHSW/RGNX/SKYQ/WKSP — error_exit-
            # dominated; 23.3% of live over-fill attempts) whose live fill/no-fill the replay
            # genuinely CANNOT resolve offline. Tag a fill as band-tail when its entry spread is
            # in the wide irreducible tail (>= the live rail abs-cap broken-quote ceiling, the
            # documented widest the rail tolerates) so the confidence band brackets these names'
            # PnL between fill and no-fill extremes. NOT a hardcoded bps — reads SPREAD_ABS_CAP_BPS,
            # the SAME live setting. OFF ⇒ never tagged (band stays empty / point==band).
            _band_tail = bool(REPLAY_FIDELITY_V2 and sbps >= SPREAD_ABS_CAP_BPS)
            if _band_tail:
                band_tail["rail_4xx"].append(s)
            _tr(s, "fill@%.4g" % fill_px, now)
            _feat = None
            if bool(getattr(settings, "chili_momentum_replay_capture_features", False)):
                _feat = _capture_entry_features(
                    s, dbg, upto, fill_px, stop, target, qty, want_qty, sbps, atrp, eff,
                    mid, dvol, minute_vol, _liq_mult, _fire_ts, _entry_fidelity, _l2db, H, L, C, V)
            open_pos[s] = {
                "entry": fill_px, "qty": qty, "qty0": qty, "stop": stop, "stop0": stop,
                "target": target, "hwm": fill_px, "atrp": eff, "scaled": False,
                "trail_armed": False, "scale_usd": 0.0,
                # ENGINE-ON: this position's dollars-at-risk to RELEASE from the running
                # aggregate on close (0.0 when ENGINE_ON is OFF ⇒ no aggregate maintained).
                "open_risk_usd": (_cand_risk_usd if REPLAY_ENGINE_ON else 0.0),
                "meta": {"sym": s, "t": str(_fire_ts)[11:16], "entry": round(fill_px, 4),
                         "qty": round(qty, 0), "spread_bps": round(sbps, 0),
                         "partial": round(qty / want_qty if want_qty > 0 else 1.0, 2),
                         "fidelity": "tape", "entry_fidelity": _entry_fidelity,
                         # STEP 2: how this fill was RESOLVED — 'prints_fill' (measured against
                         # real executions) vs 'quote_fallback' (no prints / degraded queue → the
                         # quote model) so the confidence band can widen on the latter. Only
                         # emitted when the prints flag is ON (flag-off ⇒ key absent ⇒ md5-of-
                         # trades byte-identical to HEAD). Carries the prints meta (queue_ahead /
                         # review_latency_s / n_prints / low_confidence) when the model ran.
                         **({"source": _fill_source} if REPLAY_PRINTS_FILL else {}),
                         **({"prints_meta": _prints_meta} if _prints_meta else {}),
                         **({"prints_partial": round(_entry_partial_pf, 2)}
                            if _entry_partial_pf is not None else {}),
                         **({"band_tail": True} if _band_tail else {}),
                         **({"features": _feat} if _feat else {})},
            }
    finally:
        _l2db.rollback()
        _l2db.close()

    for s in list(open_pos):
        p = open_pos[s]
        last_bid = tape.by_sym[s][-1][1]
        close_trade(s, p, last_bid, "eod", when=day_grid[-1])

    # RECORDED-FILLS CONSUMER ("RECORD don't derive"): emit the live-armed FILLED names from
    # the RECORDED broker round-trips (NOT the derived tape model — those were skipped in the
    # entry loop above). Each round-trip = one trade with the EXACT recorded entry/exit/spread/
    # $/qty; why='recorded_live' (or carrying the live exit reason). The point-estimate $ now
    # sums RECORDED $ for these + DERIVED $ for any counterfactual names. This is what makes the
    # live-armed fill-SET match what live actually traded. OFF / non-live ⇒ none of this runs.
    if use_recorded and recorded_filled_syms:
        _rec_emitted = 0
        for s in sorted(recorded_filled_syms):
            for rt in recorded_fills.get(s, []):
                _ent = float(rt["entry"]); _exi = float(rt["exit"]); _q = float(rt["qty"])
                _usd = float(rt["usd"])
                state["cum"] += _usd
                state["peak"] = max(state["peak"], state["cum"])
                _et = rt.get("entry_ts"); _xt = rt.get("exit_ts")
                _why = "recorded_live:" + str(rt.get("exit_reason") or "open_eod")
                trades.append({
                    "sym": s,
                    "t": (str(_et)[11:16] if _et is not None else None),
                    "entry": round(_ent, 4), "qty": round(_q, 0),
                    "spread_bps": round(float(rt.get("spread_bps") or 0.0), 0),
                    "partial": 1.0,
                    "fidelity": "recorded", "entry_fidelity": "recorded_broker",
                    "source": "recorded_live",
                    "session_id": rt.get("session_id"), "leg_seq": rt.get("leg_seq"),
                    "exit": round(_exi, 4), "why": _why,
                    "exit_t": (str(_xt)[11:16] if _xt is not None else None),
                    # recorded broker truth carries no modeled bracket; stop/target/run_r are
                    # not derivable from a fill leg → null (the $ + setup are the truth here).
                    "stop": None, "target": None, "mfe_px": None, "run_r": None,
                    "closed": bool(rt.get("closed")),
                    "usd": round(_usd, 0),
                })
                _rec_emitted += 1
        if isinstance(result.get("recorded_fills_consumer"), dict):
            result["recorded_fills_consumer"]["recorded_trades_emitted"] = _rec_emitted

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
    # Divergence vs LIVE actuals is meaningful for BOTH armed sources: armed_source
    # ="live" replays the real arm spans (the faithful reference); armed_source="asof"
    # exercises the engine's OWN selection, so its divergence is the metric that proves
    # the as-of selection tracks live (e.g. fewer arming_timing / replay-only rows once
    # the freshness filter drops the faded arms live never made).
    try:
        result["divergence"] = _build_divergence(date, trace, trades)
    except Exception:
        logger.warning("[replay_v2] divergence build failed", exc_info=True)
        result["divergence"] = []

    result["trades"] = sorted(trades, key=lambda z: z["t"])
    result["total_usd"] = round(state["cum"], 0)
    result["wins"] = sum(1 for t in trades if t["usd"] > 0)
    result["losses"] = sum(1 for t in trades if t["usd"] <= 0)
    # DOLLAR-SKEW LABELS (replay $ are NOT the live lane's expectancy — the shared leaf
    # math makes setup-shape / run_r live-faithful, but the dollars are not). Surface the
    # known skews so any A/B reads run_r, not usd. (wf w6c11y2s9.)
    result["sizing_basis_usd"] = round(basis_usd, 2)
    result["daily_loss_cap_usd"] = round(daily_loss_cap_usd, 2)
    # FIDELITY-V2 (STEP 3) CONFIDENCE BAND — the day-$ is an INTERVAL, never a false-precise
    # single number. The point estimate stays result["total_usd"]; the band brackets the
    # IRREDUCIBLE tail the replay genuinely cannot resolve offline (diagnosis honest_ceiling):
    #   (i)  RAIL-4xx names (wide-spread low-float, error_exit-dominated) — whether they
    #        filled live is a coin flip. Bracket each such replay trade between FILLED (its
    #        realized PnL, already in total) and NOT-FILLED ($0). So removing the tail's
    #        PROFITS gives the conservative low; removing its LOSSES gives the optimistic high.
    #   (ii) TAPE-CEILING MISSES — live-filled equity names the replay never quoted (no tape
    #        row ⇒ no replay entry). A one-sided FLOOR caveat (their live PnL is unknown), so
    #        it is annotated with the realized fill-set overlap, not numerically folded in.
    # OFF ⇒ band == [point, point] and the note is unchanged (byte-identical result shape adds
    # only the additive day_pnl_band key; trades/total are untouched).
    if REPLAY_FIDELITY_V2:
        _tail_names = set(band_tail["rail_4xx"])
        _tail_trades = [t for t in trades if t.get("band_tail") or t["sym"] in _tail_names]
        _tail_profit = sum(float(t["usd"]) for t in _tail_trades if float(t["usd"]) > 0)
        _tail_loss = sum(float(t["usd"]) for t in _tail_trades if float(t["usd"]) <= 0)
        _point = float(state["cum"])
        _band_low = _point - _tail_profit   # the rail-4xx winners may NOT have filled live
        _band_high = _point - _tail_loss    # the rail-4xx losers may NOT have filled live
        # tape-ceiling misses: live-filled equity names this replay never entered (the
        # one-sided floor). Best-effort live read (bounded, rollback'd, SELECT-only); a DB
        # error degrades to an empty miss-set (the band still emits from the rail tail).
        _miss = []
        _live_n = None
        try:
            _dbm = SessionLocal()
            try:
                _rowsm = _dbm.execute(
                    text(
                        "SELECT DISTINCT symbol FROM momentum_fill_outcomes "
                        "WHERE symbol NOT LIKE '%-USD' AND created_at >= :lo AND created_at < :hi"
                    ),
                    {"lo": f"{date} 04:00:00", "hi": f"{date} 23:59:59"},
                ).fetchall()
            finally:
                _dbm.rollback(); _dbm.close()
            _live_set = {str(r[0]).upper() for r in _rowsm}
            _live_n = len(_live_set)
            _miss = sorted(_live_set - {t["sym"].upper() for t in trades})
        except Exception:
            logger.warning("[replay_v2] band tape-ceiling-miss read failed", exc_info=True)
        _overlap = (_live_n - len(_miss)) if _live_n is not None else None
        result["day_pnl_band"] = [round(_band_low, 0), round(_band_high, 0)]
        result["day_pnl_band_meta"] = {
            "point_usd": round(_point, 0),
            "rail_4xx_tail_names": sorted(_tail_names),
            "rail_4xx_tail_profit_usd": round(_tail_profit, 0),
            "rail_4xx_tail_loss_usd": round(_tail_loss, 0),
            "tape_ceiling_miss_names": _miss,
            "tape_ceiling_miss_count": len(_miss),
            "live_filled_names": _live_n,
            "fill_set_overlap": _overlap,
        }
    result["dollar_skew_note"] = (
        "Sizing basis is now LIVE-faithful (real account equity, same equity-fractions + "
        "daily-loss cap as live). RESIDUAL replay->live gaps (decision-side, being closed "
        "next): no #789 entry re-peg (can under-count a runaway fill), no #769 early-flatten "
        "(a gap-through loss runs deeper than live), frictionless ask/bid fills. Lead A/Bs "
        "with run_r; absolute $ are now ~live-scale, treat the residual gaps as noise."
    )
    if REPLAY_FIDELITY_V2 and result.get("day_pnl_band"):
        _bm = result["day_pnl_band_meta"]
        _ov = _bm.get("fill_set_overlap")
        _ln = _bm.get("live_filled_names")
        result["dollar_skew_note"] += (
            " FIDELITY-V2: day-$ is %s over band [%s, %s]"
            % (result["total_usd"], result["day_pnl_band"][0], result["day_pnl_band"][1])
            + (", fill-set overlap %s/%s" % (_ov, _ln) if _ov is not None and _ln else "")
            + " (band brackets the irreducible rail-4xx tail; tape-ceiling misses are a one-sided floor)."
        )
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


def _armed_source_suffix(armed_source: str | None) -> str:
    """Filename suffix per armed_source so the three results don't clobber each other
    ('asof' = no suffix → byte-identical to the historical path)."""
    if armed_source == "live":
        return "_live"
    if armed_source == "full_pipeline":
        return "_fullpipe"
    return ""


def _persist(result: dict) -> None:
    try:
        os.makedirs(REPLAY_RESULTS_DIR, exist_ok=True)
        suffix = _armed_source_suffix(result.get("armed_source"))
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
    suffix = _armed_source_suffix(armed_source)
    try:
        with open(os.path.join(REPLAY_RESULTS_DIR, f"{date}{suffix}.json"), encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None
