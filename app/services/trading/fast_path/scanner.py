"""Event-driven scalp scanner (F3).

Listens for two event types from the WS client:
  - on_bar_close(bar): fired once a 1m bar closes
  - on_book_emit(ticker, book_dict): fired up to ~4/sec/ticker (throttled)

Maintains a small rolling window of recent bars per ticker to compute
volume baselines without a DB roundtrip. Order-book events read the
already-computed imbalance + spread fields from F2's emission.

Cooldown: each (ticker, alert_type) pair has its own cooldown so the
same setup doesn't fire continuously while it persists. Volume signals
are inherently 1-per-bar so they don't need cooldown beyond the close
boundary; book signals do (book updates ~4/sec).

Signals (initial set — more can layer in):

  ``volume_breakout_long``
    Bar's ``volume`` >= ``vol_mult * mean(volumes[-N:])`` AND
    ``close > open`` (positive bar). Score = clamped (vol / mean - 1) / 4.
    Fires on bar close; one max per minute per pair by definition.

  ``imbalance_long`` / ``imbalance_short``
    Order-book imbalance crosses a threshold (default 0.65 / 0.35).
    Score = abs(imbalance - 0.5) * 2 (clamped). Per-ticker 30s cooldown.

  ``spread_squeeze``
    spread_bps < ``squeeze_bps`` AND volume on last bar > 1.2x mean.
    Tight spread + volume often precedes a breakout. Per-ticker 60s
    cooldown.

This module is intentionally pure-Python with no DB or broker imports —
it must stay safe to unit test without spinning up infra.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Iterable

logger = logging.getLogger(__name__)


# Default thresholds — tuned conservatively. Operators can override
# via env (CHILI_FAST_PATH_SCANNER_*) but for now these are constants
# so the module ships with sane behavior out of the box.
VOL_BREAKOUT_LOOKBACK = 20            # bars
VOL_BREAKOUT_MULT = 2.0               # 2x mean-volume
IMBALANCE_LONG_THRESHOLD = 0.65
IMBALANCE_SHORT_THRESHOLD = 0.35
IMBALANCE_COOLDOWN_S = 30.0
SPREAD_SQUEEZE_BPS = 1.5
SPREAD_SQUEEZE_VOL_MULT = 1.2
SPREAD_SQUEEZE_COOLDOWN_S = 60.0


@dataclass
class _PerTickerState:
    # Most-recent N closed-bar volumes (oldest first).
    recent_vols: deque[float] = field(default_factory=lambda: deque(maxlen=VOL_BREAKOUT_LOOKBACK))
    # Most-recent close prices in the same window — used for sanity gates.
    recent_closes: deque[float] = field(default_factory=lambda: deque(maxlen=VOL_BREAKOUT_LOOKBACK))
    # Per-(alert_type) last fire time for cooldown.
    last_fire: dict[str, float] = field(default_factory=dict)
    # Most-recent observed spread_bps (for spread_squeeze; refreshed on
    # every book emit).
    last_spread_bps: float = 0.0


class MomentumScanner:
    """Per-ticker rolling state + alert-emission entry points.

    Single-threaded usage assumed (asyncio task on the WS client).
    Diagnostic counters are surfaced via :meth:`stats`.
    """

    def __init__(self) -> None:
        self._state: dict[str, _PerTickerState] = {}
        # Diagnostic counters — surfaced via stats().
        self.bars_seen = 0
        self.books_seen = 0
        self.fired_volume_breakout_long = 0
        self.fired_imbalance_long = 0
        self.fired_imbalance_short = 0
        self.fired_spread_squeeze = 0
        self.suppressed_cooldown = 0
        self.suppressed_warmup = 0

    # ── Bar-close handler ──────────────────────────────────────────────

    def on_bar_close(self, bar: dict) -> list[dict]:
        """Update rolling state with the just-closed bar; return any
        alerts triggered. Each alert is a dict shaped like an
        ``AlertItem`` payload but un-typed so this module remains
        pure-Python.

        The caller is responsible for converting the dict to an
        ``AlertItem`` and enqueueing it.

        ``bar`` should be a dict with at least: ``ticker`` (str),
        ``volume`` (float), ``open`` (float), ``close`` (float),
        ``bar_close_at`` (datetime).
        """
        self.bars_seen += 1
        ticker = bar.get("ticker")
        if not ticker:
            return []
        vol = float(bar.get("volume") or 0.0)
        open_p = float(bar.get("open") or 0.0)
        close_p = float(bar.get("close") or 0.0)
        ts: datetime = bar.get("bar_close_at") or datetime.now(timezone.utc).replace(tzinfo=None)

        st = self._state.get(ticker)
        if st is None:
            st = _PerTickerState()
            self._state[ticker] = st

        alerts: list[dict] = []
        # Need full lookback before we can emit; until then just learn.
        if len(st.recent_vols) >= VOL_BREAKOUT_LOOKBACK:
            mean_vol = sum(st.recent_vols) / len(st.recent_vols)
            if mean_vol > 0 and vol >= VOL_BREAKOUT_MULT * mean_vol and close_p > open_p:
                # Score saturates at 4x mean (uncommon mega-spike).
                ratio = vol / mean_vol
                score = max(0.0, min(1.0, (ratio - 1.0) / 4.0))
                alerts.append({
                    "ticker": ticker,
                    "alert_type": "volume_breakout_long",
                    "fired_at": ts,
                    "signal_score": float(score),
                    "features": {
                        "volume": float(vol),
                        "mean_vol_lookback": int(len(st.recent_vols)),
                        "mean_vol": float(mean_vol),
                        "vol_ratio": float(ratio),
                        "open": float(open_p),
                        "close": float(close_p),
                        "ret_pct": float((close_p - open_p) / open_p if open_p else 0.0),
                    },
                })
                self.fired_volume_breakout_long += 1
            # Layered signal: tight spread + above-average volume on a
            # green bar. Spread comes from the most recent book emit; if
            # we haven't seen one yet, last_spread_bps is 0 and the
            # check below short-circuits safely.
            if (st.last_spread_bps > 0
                and st.last_spread_bps <= SPREAD_SQUEEZE_BPS
                and mean_vol > 0
                and vol >= SPREAD_SQUEEZE_VOL_MULT * mean_vol
                and close_p > open_p):
                if self._cooldown_ok(st, "spread_squeeze", SPREAD_SQUEEZE_COOLDOWN_S):
                    score = max(0.0, min(1.0, vol / max(mean_vol, 1e-9) / 4.0))
                    alerts.append({
                        "ticker": ticker,
                        "alert_type": "spread_squeeze",
                        "fired_at": ts,
                        "signal_score": float(score),
                        "features": {
                            "spread_bps": float(st.last_spread_bps),
                            "volume": float(vol),
                            "mean_vol": float(mean_vol),
                            "vol_ratio": float(vol / mean_vol),
                            "close": float(close_p),
                        },
                    })
                    self.fired_spread_squeeze += 1
                else:
                    self.suppressed_cooldown += 1
        else:
            self.suppressed_warmup += 1

        # Always update rolling state at the end so this bar's volume
        # contributes to the NEXT bar's mean.
        st.recent_vols.append(vol)
        st.recent_closes.append(close_p)
        return alerts

    # ── Book-emit handler ──────────────────────────────────────────────

    def on_book_emit(self, ticker: str, book: dict,
                     *, now_monotonic: float | None = None) -> list[dict]:
        """Inspect a freshly-emitted top-N book; emit imbalance alerts
        when thresholds are crossed. Cooldown gates the rate."""
        self.books_seen += 1
        if not ticker or not isinstance(book, dict):
            return []
        st = self._state.get(ticker)
        if st is None:
            st = _PerTickerState()
            self._state[ticker] = st
        st.last_spread_bps = float(book.get("spread_bps") or 0.0)
        imb = float(book.get("imbalance") or 0.5)
        ts: datetime = book.get("snapshot_at") or datetime.now(timezone.utc).replace(tzinfo=None)
        alerts: list[dict] = []
        if imb >= IMBALANCE_LONG_THRESHOLD:
            if self._cooldown_ok(st, "imbalance_long", IMBALANCE_COOLDOWN_S, now_monotonic):
                alerts.append(self._book_alert(ticker, "imbalance_long", ts, imb, book))
                self.fired_imbalance_long += 1
            else:
                self.suppressed_cooldown += 1
        elif imb <= IMBALANCE_SHORT_THRESHOLD:
            if self._cooldown_ok(st, "imbalance_short", IMBALANCE_COOLDOWN_S, now_monotonic):
                alerts.append(self._book_alert(ticker, "imbalance_short", ts, imb, book))
                self.fired_imbalance_short += 1
            else:
                self.suppressed_cooldown += 1
        return alerts

    # ── Helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _cooldown_ok(state: _PerTickerState, alert_type: str,
                     cooldown_s: float, now_monotonic: float | None = None) -> bool:
        now = now_monotonic if now_monotonic is not None else time.monotonic()
        last = state.last_fire.get(alert_type, 0.0)
        if (now - last) < cooldown_s:
            return False
        state.last_fire[alert_type] = now
        return True

    @staticmethod
    def _book_alert(ticker: str, alert_type: str, ts: datetime,
                    imb: float, book: dict) -> dict:
        # Score: how far the imbalance is from balanced (0.5), capped.
        score = max(0.0, min(1.0, abs(imb - 0.5) * 2.0))
        return {
            "ticker": ticker,
            "alert_type": alert_type,
            "fired_at": ts,
            "signal_score": float(score),
            "features": {
                "imbalance": float(imb),
                "spread_bps": float(book.get("spread_bps") or 0.0),
                "bid_total_size": float(book.get("bid_total_size") or 0.0),
                "ask_total_size": float(book.get("ask_total_size") or 0.0),
                "best_bid": float(book.get("bid_levels", [(0, 0)])[0][0]) if book.get("bid_levels") else 0.0,
                "best_ask": float(book.get("ask_levels", [(0, 0)])[0][0]) if book.get("ask_levels") else 0.0,
            },
        }

    # ── Observability ──────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "bars_seen": self.bars_seen,
            "books_seen": self.books_seen,
            "fired_volume_breakout_long": self.fired_volume_breakout_long,
            "fired_imbalance_long": self.fired_imbalance_long,
            "fired_imbalance_short": self.fired_imbalance_short,
            "fired_spread_squeeze": self.fired_spread_squeeze,
            "suppressed_cooldown": self.suppressed_cooldown,
            "suppressed_warmup": self.suppressed_warmup,
            "tickers_tracked": len(self._state),
        }


__all__ = ["MomentumScanner"]
