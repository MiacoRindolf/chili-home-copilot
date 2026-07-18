"""Tick-based early-mover (ignition) detector — pure, event-time driven.

WHY (PIT-measured 2026-07-17, prod tape + IQFeed history):
  Every mover-discovery path funnels through the Massive full-market snapshot
  (~180s cadence, 300s max age) and the L1 bridge only watches symbols that
  already passed that snapshot.  Measured cost on the three Ross winners we
  missed: PLSM 2026-07-13 ignited 11:58-11:59 UTC but our tape's first print is
  12:02:34 (subscribe lag ~3.6-4.6 min); ERNA 2026-07-15 ignited 11:50 (first
  prints of its day), tape starts 11:57:53 (~7.9 min); VIVS 2026-07-15 ignited
  12:05, tape starts 12:07:47 (~2.8 min).  None of the three had ANY roster or
  viability presence before ignition, so on-stream detection needs BOTH a wide
  standing watch (bridge-side) and this detector to fire the moment the burst
  is on our tape.

This module is deliberately pure (no DB, no sockets, no wall clock): every
window is driven by the PRINT'S OWN event time, so live behaviour and replay /
backtest behaviour are identical and the unit tests are deterministic.

Detection rule (ALL must hold on a new print):
  1. pct_change_60s  = price / min(price over trailing 60s) - 1  >= pct threshold
  2. dollar_vol_60s  = sum(price*size over trailing 60s)         >= $ threshold
  3. prints_10s      = prints in the trailing 10s                >= prints threshold
  4. price inside [price_floor, price_ceiling] (the Ross small-cap universe)
  5. per-symbol dedup TTL elapsed and the global fires/minute cap not exhausted

ADAPTIVE thresholds (operator rule: adaptive by default, ONE documented base
per knob, floors not ceilings): each threshold is
    max(base_floor, surge_mult * the symbol's own trailing baseline)
where the baseline is measured over (t-300s, t-60s] — the surge window itself
is EXCLUDED so a burst can never raise its own bar.  A name that always trades
$2M/60s therefore needs a genuine 4x surge; a cold name falls back to the
documented floors.  Floors were derived from the PLSM/ERNA/VIVS tape: the
quietest true ignition minute (PLSM 11:57) ran ~$211k/min with thousands of
prints and +8%/min, while flat small-cap tape runs <$50k/min with <1% wiggle.

The detector only NOMINATES.  Admission stays behind the existing guarded
admit path (pg_advisory_xact_lock dedup + every admission gate).
"""
from __future__ import annotations

import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass(frozen=True)
class IgnitionConfig:
    """One documented base per knob; every threshold is a floor, never a ceiling."""

    # Base floors (the ONE documented setting per knob).
    pct_base: float = 0.05                # +5% over the trailing 60s window
    dollar_vol_base: float = 150_000.0    # $150k traded over the trailing 60s
    prints_base: int = 20                 # 20 prints over the trailing 10s
    # Adaptive multiplier applied to the symbol's own trailing baseline.
    surge_mult: float = 4.0
    # Window geometry (seconds).
    pct_window_s: float = 60.0
    vol_window_s: float = 60.0
    prints_window_s: float = 10.0
    baseline_window_s: float = 300.0
    # Baseline must cover at least this much history before it may raise a bar.
    baseline_min_span_s: float = 120.0
    # Universe price bounds (Ross small-cap lane).
    price_floor: float = 0.30
    price_ceiling: float = 100.0
    # Nomination governors (hard caps; the admit path keeps its own guards).
    dedup_ttl_s: float = 300.0            # one nomination per symbol per TTL
    max_fires_per_minute: int = 6         # global, across all symbols
    # Bounded memory: hard max tracked symbols (LRU-evicted) + idle TTL.
    max_symbols: int = 2048
    symbol_idle_ttl_s: float = 1800.0


@dataclass(frozen=True)
class IgnitionFire:
    symbol: str
    fired_at: datetime
    last_price: float
    pct_change_60s: float
    dollar_vol_60s: float
    prints_10s: int
    pct_threshold: float
    dollar_vol_threshold: float
    prints_threshold: float


@dataclass
class _SymbolState:
    last_t: float = 0.0
    # (t, price, dollar) prints inside the baseline window.
    prints: deque = field(default_factory=deque)
    # Sliding-window minimum over the pct window: (t, price), price ascending.
    min_q: deque = field(default_factory=deque)
    # Rolling sums, maintained by eviction against last_t.
    vol_sum_60: float = 0.0
    vol_idx: int = 0            # prints[:vol_idx] are older than the vol window
    prints_idx: int = 0         # prints[:prints_idx] are older than the prints window
    baseline_dollar: float = 0.0
    baseline_prints: int = 0


class IgnitionDetector:
    """Feed every genuinely-new print; returns an IgnitionFire when a symbol ignites.

    Thread-safe; all clocks are event-time (the print timestamps themselves).
    """

    def __init__(self, config: IgnitionConfig | None = None) -> None:
        self.config = config or IgnitionConfig()
        self._lock = threading.Lock()
        self._symbols: dict[str, _SymbolState] = {}
        self._last_fire_t: dict[str, float] = {}
        self._global_fire_t: deque = deque()
        self._global_last_t = 0.0
        self.suppressed_dedup = 0
        self.suppressed_cap = 0

    # ── internals ─────────────────────────────────────────────────────────────

    def _evict_symbol_windows(self, st: _SymbolState) -> None:
        cfg = self.config
        cutoff_base = st.last_t - cfg.baseline_window_s
        while st.prints and st.prints[0][0] <= cutoff_base:
            t, _price, dollar = st.prints.popleft()
            st.baseline_dollar -= dollar
            st.baseline_prints -= 1
            if st.vol_idx > 0:
                st.vol_idx -= 1
            else:
                st.vol_sum_60 -= dollar
            if st.prints_idx > 0:
                st.prints_idx -= 1
        cutoff_vol = st.last_t - cfg.vol_window_s
        while st.vol_idx < len(st.prints) and st.prints[st.vol_idx][0] <= cutoff_vol:
            st.vol_sum_60 -= st.prints[st.vol_idx][2]
            st.vol_idx += 1
        cutoff_prints = st.last_t - cfg.prints_window_s
        while (
            st.prints_idx < len(st.prints)
            and st.prints[st.prints_idx][0] <= cutoff_prints
        ):
            st.prints_idx += 1
        cutoff_pct = st.last_t - cfg.pct_window_s
        while st.min_q and st.min_q[0][0] <= cutoff_pct:
            st.min_q.popleft()

    def _prune_symbol_table(self, now_t: float) -> None:
        """Make room for one incoming symbol; called before every insert."""
        cfg = self.config
        if len(self._symbols) < cfg.max_symbols:
            stale = [
                sym
                for sym, st in self._symbols.items()
                if now_t - st.last_t > cfg.symbol_idle_ttl_s
            ]
            for sym in stale:
                del self._symbols[sym]
                self._last_fire_t.pop(sym, None)
            return
        # Hard cap: drop the least-recently printed symbols first, leaving one
        # free slot for the symbol about to be inserted.
        ranked = sorted(self._symbols.items(), key=lambda kv: kv[1].last_t)
        for sym, _st in ranked[: len(self._symbols) - cfg.max_symbols + 1]:
            del self._symbols[sym]
            self._last_fire_t.pop(sym, None)

    # ── public API ────────────────────────────────────────────────────────────

    def on_print(
        self,
        symbol: str,
        at: datetime,
        price: float,
        size: float,
    ) -> IgnitionFire | None:
        cfg = self.config
        sym = str(symbol or "").strip().upper()
        if not sym or not isinstance(at, datetime):
            return None
        if price <= 0 or size <= 0:
            return None
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        t = at.timestamp()
        dollar = float(price) * float(size)
        with self._lock:
            self._global_last_t = max(self._global_last_t, t)
            st = self._symbols.get(sym)
            if st is None:
                self._prune_symbol_table(self._global_last_t)
                st = _SymbolState()
                self._symbols[sym] = st
            st.last_t = max(st.last_t, t)
            st.prints.append((t, float(price), dollar))
            st.baseline_dollar += dollar
            st.baseline_prints += 1
            st.vol_sum_60 += dollar
            while st.min_q and st.min_q[-1][1] >= price:
                st.min_q.pop()
            st.min_q.append((t, float(price)))
            self._evict_symbol_windows(st)

            if not (cfg.price_floor <= price <= cfg.price_ceiling):
                return None
            if not st.min_q:
                return None
            min_60 = st.min_q[0][1]
            pct = float(price) / min_60 - 1.0
            dollar_60 = st.vol_sum_60
            prints_10 = len(st.prints) - st.prints_idx

            # Adaptive bars: the symbol's own baseline over (t-300s, t-60s],
            # normalised to the surge-window length; inactive until the tape
            # history actually spans baseline_min_span_s.
            span = st.last_t - st.prints[0][0]
            pct_thr = cfg.pct_base
            vol_thr = cfg.dollar_vol_base
            prints_thr = float(cfg.prints_base)
            if span >= cfg.baseline_min_span_s:
                pre_dollar = st.baseline_dollar - st.vol_sum_60
                pre_prints = st.baseline_prints - (len(st.prints) - st.vol_idx)
                pre_span = max(span - cfg.vol_window_s, 1.0)
                vol_thr = max(
                    vol_thr,
                    cfg.surge_mult * pre_dollar / pre_span * cfg.vol_window_s,
                )
                prints_thr = max(
                    prints_thr,
                    cfg.surge_mult * pre_prints / pre_span * cfg.prints_window_s,
                )
            if pct < pct_thr or dollar_60 < vol_thr or prints_10 < prints_thr:
                return None

            last_fire = self._last_fire_t.get(sym)
            if last_fire is not None and t - last_fire < cfg.dedup_ttl_s:
                self.suppressed_dedup += 1
                return None
            cap_cutoff = self._global_last_t - 60.0
            while self._global_fire_t and self._global_fire_t[0] <= cap_cutoff:
                self._global_fire_t.popleft()
            if len(self._global_fire_t) >= cfg.max_fires_per_minute:
                self.suppressed_cap += 1
                return None
            self._last_fire_t[sym] = t
            self._global_fire_t.append(t)
            return IgnitionFire(
                symbol=sym,
                fired_at=at.astimezone(timezone.utc),
                last_price=float(price),
                pct_change_60s=pct,
                dollar_vol_60s=dollar_60,
                prints_10s=prints_10,
                pct_threshold=pct_thr,
                dollar_vol_threshold=vol_thr,
                prints_threshold=prints_thr,
            )
