"""Per-pair status + circuit-breaker.

Tracks: last bar timestamp, last sequence number observed, error counts
within a 60s rolling window, reconnect counts. Decides when to flip a
pair to ``degraded`` / ``paused`` / ``halted`` based on the rules in
``docs/ARCHITECTURE-fast-path.md``.

Persists state into ``fast_path_status`` (one row per ticker, upsert).

Threading model: the tracker is owned by the supervisor and called
from a single asyncio task. No locks required.
"""
from __future__ import annotations

import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from typing import Deque

from sqlalchemy import text
from sqlalchemy.engine import Engine

logger = logging.getLogger(__name__)


# Canonical state values. Also enforced by the CHECK constraint on the
# fast_path_status table.
STATE_STREAMING = "streaming"
STATE_DEGRADED = "degraded"
STATE_PAUSED = "paused"
STATE_HALTED = "halted"


# F-hygiene-1: clear last_error after this many seconds of clean
# streaming. Operator UX, not strategy timing -- a transient hiccup
# at startup shouldn't show as an active error for hours after
# normal streaming resumed. 5 minutes is long enough that a flapping
# connection won't keep clearing-and-resetting (a fresh error within
# the window resets the streak), short enough that a healthy stream
# clears the dashboard within one operator refresh window.
ERROR_CLEAR_AFTER_HEALTHY_S = 5.0 * 60.0


@dataclass
class PairStatus:
    ticker: str
    state: str = STATE_PAUSED
    last_bar_at: datetime | None = None
    last_seq: int | None = None
    last_error: str | None = None
    last_reconnect_at: datetime | None = None
    reconnect_count: int = 0
    # Rolling 60s error timestamps (monotonic seconds)
    _error_times: Deque[float] = field(default_factory=lambda: deque(maxlen=128))
    # F-hygiene-1: monotonic timestamp of when the current healthy
    # streak began. None means "no streak in progress" (either we just
    # started, or an error reset it). After ERROR_CLEAR_AFTER_HEALTHY_S
    # of continuous healthy ticks, last_error self-clears so a
    # transient hiccup at boot doesn't haunt the operator dashboard
    # for hours. Reset on every record_error.
    _healthy_streak_started: float | None = None


class StatusTracker:
    """Maintains a ``PairStatus`` per ticker and persists state changes
    to ``fast_path_status``.

    The flush is bounded — at most one UPDATE per ticker per
    ``flush_min_interval_s``, so bursts of state changes coalesce.
    """

    def __init__(
        self,
        engine: Engine,
        cb_threshold: int = 5,
        flush_min_interval_s: float = 1.0,
    ) -> None:
        self._engine = engine
        self._cb_threshold = int(cb_threshold)
        self._flush_min_interval = float(flush_min_interval_s)
        self._pairs: dict[str, PairStatus] = {}
        self._dirty: set[str] = set()
        self._last_flush_at: dict[str, float] = {}

    # ── Lifecycle ─────────────────────────────────────────────────────

    def register(self, ticker: str) -> None:
        """Make sure we track *ticker*. Idempotent.

        F-hygiene-1: pulls existing ``last_error`` from
        ``fast_path_status`` into the new in-memory PairStatus so the
        self-clear logic in ``record_healthy_tick`` has visibility into
        errors persisted across restarts. Without this, a stale error
        from a prior process would never clear because the in-memory
        ``last_error`` would be None and the early-return in
        record_healthy_tick would skip the clear.
        """
        if ticker not in self._pairs:
            ps = PairStatus(ticker=ticker)
            try:
                with self._engine.connect() as conn:
                    row = conn.execute(text(
                        "SELECT last_error FROM fast_path_status WHERE ticker = :t"
                    ), {"t": ticker}).mappings().one_or_none()
                if row is not None and row.get("last_error"):
                    ps.last_error = row["last_error"]
            except Exception:
                # Don't let a startup-time DB hiccup block registration
                # of the in-memory tracker. Worst case: we just don't
                # see the prior persisted error and the existing
                # behavior (no clear) applies.
                pass
            self._pairs[ticker] = ps
            self._dirty.add(ticker)

    def get(self, ticker: str) -> PairStatus:
        if ticker not in self._pairs:
            self.register(ticker)
        return self._pairs[ticker]

    # ── Event recorders ───────────────────────────────────────────────

    def mark_streaming(self, ticker: str) -> None:
        ps = self.get(ticker)
        if ps.state != STATE_STREAMING:
            ps.state = STATE_STREAMING
            self._dirty.add(ticker)

    def mark_degraded(self, ticker: str, reason: str) -> None:
        ps = self.get(ticker)
        ps.state = STATE_DEGRADED
        ps.last_error = (reason or "")[:500]
        self._dirty.add(ticker)

    def mark_paused(self, ticker: str, reason: str) -> None:
        ps = self.get(ticker)
        ps.state = STATE_PAUSED
        ps.last_error = (reason or "")[:500]
        self._dirty.add(ticker)
        logger.warning(
            "[fast_path] PAUSED ticker=%s reason=%s", ticker, reason[:200],
        )

    def mark_halted(self, ticker: str, reason: str) -> None:
        ps = self.get(ticker)
        ps.state = STATE_HALTED
        ps.last_error = (reason or "")[:500]
        self._dirty.add(ticker)
        logger.critical(
            "[fast_path] HALTED ticker=%s reason=%s", ticker, reason[:200],
        )

    def record_bar(self, ticker: str, bar_close_at: datetime, seq: int | None) -> None:
        ps = self.get(ticker)
        ps.last_bar_at = bar_close_at
        if seq is not None:
            ps.last_seq = int(seq)
        # A successful bar implicitly clears stale degraded state.
        if ps.state in (STATE_DEGRADED, STATE_PAUSED):
            self.mark_streaming(ticker)
        self._dirty.add(ticker)
        # F-hygiene-1: every successful bar is a healthy-tick. After
        # ERROR_CLEAR_AFTER_HEALTHY_S of continuous healthy ticks the
        # ticker's last_error self-clears so a transient startup hiccup
        # doesn't haunt the operator dashboard for hours.
        self.record_healthy_tick(ticker)

    def record_healthy_tick(self, ticker: str) -> None:
        """Mark *ticker* as having had a successful data delivery.

        Called from record_bar (successful 1m candle). Each successful
        delivery either starts a streak (if none in progress) or
        extends one. After ERROR_CLEAR_AFTER_HEALTHY_S of unbroken
        streak the ticker's last_error self-clears.

        Idempotent and cheap; safe to call from any successful-data
        path.
        """
        ps = self.get(ticker)
        now = time.monotonic()
        if ps.last_error is None:
            # Nothing to clear; just keep the streak fresh so the
            # next error->recovery cycle has a clean baseline.
            ps._healthy_streak_started = now
            return
        if ps._healthy_streak_started is None:
            ps._healthy_streak_started = now
            return
        if (now - ps._healthy_streak_started) >= ERROR_CLEAR_AFTER_HEALTHY_S:
            logger.info(
                "[fast_path] status_tracker: clearing stale last_error on "
                "ticker=%s after %.0f min healthy streak (was: %s)",
                ticker, ERROR_CLEAR_AFTER_HEALTHY_S / 60.0,
                (ps.last_error or "")[:120],
            )
            ps.last_error = None
            ps._healthy_streak_started = None
            self._dirty.add(ticker)

    def record_reconnect(self, ticker: str) -> None:
        ps = self.get(ticker)
        ps.reconnect_count += 1
        ps.last_reconnect_at = datetime.utcnow()
        self._dirty.add(ticker)

    def record_error(self, ticker: str, error: str) -> bool:
        """Return True if the circuit breaker tripped this call.

        Side effect: when the per-60s threshold is reached, the pair
        is moved to ``state='paused'``.
        """
        ps = self.get(ticker)
        now = time.monotonic()
        ps._error_times.append(now)
        # Trim entries older than 60s
        while ps._error_times and (now - ps._error_times[0]) > 60.0:
            ps._error_times.popleft()
        ps.last_error = (error or "")[:500]
        # F-hygiene-1: any new error resets the healthy-streak clock.
        # The recovery threshold is "uninterrupted streak", not
        # "cumulative time"; a flap-and-recover pattern shouldn't
        # silently clear the most-recent error.
        ps._healthy_streak_started = None
        self._dirty.add(ticker)
        if len(ps._error_times) >= self._cb_threshold and ps.state != STATE_PAUSED:
            self.mark_paused(
                ticker,
                f"circuit_breaker:{len(ps._error_times)}_errors_in_60s:{error[:100]}",
            )
            return True
        return False

    # ── Persistence ───────────────────────────────────────────────────

    def flush(self, force: bool = False) -> int:
        """Upsert dirty pairs into fast_path_status. Returns count flushed."""
        if not self._dirty:
            return 0
        now = time.monotonic()
        flushed = 0
        with self._engine.begin() as conn:
            for ticker in list(self._dirty):
                last = self._last_flush_at.get(ticker, 0.0)
                if not force and (now - last) < self._flush_min_interval:
                    continue
                ps = self._pairs[ticker]
                conn.execute(text("""
                    INSERT INTO fast_path_status (
                        ticker, state, last_bar_at, last_seq,
                        error_count_60s, last_error,
                        last_reconnect_at, reconnect_count, updated_at
                    ) VALUES (
                        :ticker, :state, :last_bar_at, :last_seq,
                        :error_count, :last_error,
                        :last_reconnect_at, :reconnect_count, NOW()
                    )
                    ON CONFLICT (ticker) DO UPDATE SET
                        state = EXCLUDED.state,
                        last_bar_at = COALESCE(EXCLUDED.last_bar_at, fast_path_status.last_bar_at),
                        last_seq = COALESCE(EXCLUDED.last_seq, fast_path_status.last_seq),
                        error_count_60s = EXCLUDED.error_count_60s,
                        -- F-hygiene-1: last_error overwrites directly
                        -- (not COALESCE) so when the self-clear path
                        -- sets it to NULL the clear actually persists.
                        -- In-memory PairStatus.last_error is loaded
                        -- from DB on register(), so this is safe --
                        -- it always reflects intended truth.
                        last_error = EXCLUDED.last_error,
                        last_reconnect_at = COALESCE(EXCLUDED.last_reconnect_at, fast_path_status.last_reconnect_at),
                        reconnect_count = EXCLUDED.reconnect_count,
                        updated_at = NOW()
                """), {
                    "ticker": ticker,
                    "state": ps.state,
                    "last_bar_at": ps.last_bar_at,
                    "last_seq": ps.last_seq,
                    "error_count": len(ps._error_times),
                    "last_error": ps.last_error,
                    "last_reconnect_at": ps.last_reconnect_at,
                    "reconnect_count": ps.reconnect_count,
                })
                self._last_flush_at[ticker] = now
                self._dirty.discard(ticker)
                flushed += 1
        return flushed

    # ── Snapshot for healthz ──────────────────────────────────────────

    def snapshot(self) -> dict:
        """Read-only summary for the healthz endpoint."""
        out: dict = {"pairs": {}}
        for ticker, ps in self._pairs.items():
            out["pairs"][ticker] = {
                "state": ps.state,
                "last_bar_at": ps.last_bar_at.isoformat() if ps.last_bar_at else None,
                "last_seq": ps.last_seq,
                "error_count_60s": len(ps._error_times),
                "reconnect_count": ps.reconnect_count,
                "last_error": ps.last_error,
            }
        return out


__all__ = [
    "PairStatus",
    "StatusTracker",
    "STATE_STREAMING",
    "STATE_DEGRADED",
    "STATE_PAUSED",
    "STATE_HALTED",
]
