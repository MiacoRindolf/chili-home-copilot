"""Fast-path exit manager (F5).

Closes the loop on F4 paper entries by monitoring open positions and
firing exit decisions when stops, targets, or time-stops trip.

Pipeline:
    every poll:
      - bootstrap any newly-opened positions found in fast_executions
        (decision='paper_fill' AND id NOT IN fast_exits)
      - compute (stop, target) for each new position via
        stop_engine.compute_initial_bracket using ATR(14) from
        fast_snapshots; cache in memory keyed by entry_execution_id
      - for each cached open position, read top-of-book from the in-
        memory L2 aggregator and check exit conditions:
            * stop_hit       — best_bid <= stop_price (long)
            * target_hit     — best_bid >= target_price (long)
            * time_stop      — held > MAX_HOLD_S
      - on exit: synthesise paper fill at best_bid, write fast_exits
        row with realized_pnl, drop from cache

Why exit_manager owns bracket computation (vs the executor writing it
at entry time): the exit_manager already needs DB read access for ATR
and an in-memory state cache; centralising the policy here means F5
ships without changing the F4 executor at all. Brackets are
deterministic from (entry_price, ATR-at-fill-time, regime, lifecycle),
so a container restart mid-position recomputes the same bracket from
fast_snapshots history.

Live exits are intentionally NOT implemented in F5 — the same three-
flag authorization belt that gates live entries (F4-followup) would
need to gate live exits, plus a Coinbase market-sell wrapper. F5 is
paper-only; live exit is its own follow-up.

Threading model: single asyncio task. DB calls run via
``loop.run_in_executor``.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .order_book import OrderBookAggregator
from .settings import FastPathSettings

logger = logging.getLogger(__name__)


POLL_INTERVAL_S = 1.0
"""Same cadence as the executor — 1s polling matches the L2 emit
throttle (~4/s/ticker) so we never act on a book that's more than ~1s
stale. F5b candidate: switch to LISTEN/NOTIFY-driven dispatch."""

ATR_LOOKBACK_BARS = 14
"""Wilder's classic. 14 bars of 1m crypto data is ~14 minutes —
enough that a single anomalous wick doesn't dominate the volatility
estimate, but short enough that the stop reflects current regime."""

MAX_HOLD_S_DEFAULT = 4 * 3600
"""Force-exit any open paper position held longer than this. 4h
matches the swing-path day-trade convention. Override via
``CHILI_FAST_PATH_EXIT_TIME_STOP_S``."""


# ── In-memory state ──────────────────────────────────────────────────


@dataclass
class _OpenPosition:
    """One row's worth of cached state per open paper entry."""

    entry_execution_id: int
    ticker: str
    side: str  # 'buy' (we only open longs in spot today)
    quantity: float
    entry_price: float
    entered_at: datetime
    stop_price: float
    target_price: float
    atr: float | None
    brain_payload: dict[str, Any] = field(default_factory=dict)
    # F6.5: per-position calibrated max-hold. None means "use global
    # MAX_HOLD_S_DEFAULT" (cold-start fallback path).
    max_hold_s: float | None = None


@dataclass
class _ExitMetrics:
    polls_total: int = 0
    bootstrap_runs: int = 0
    positions_bootstrapped: int = 0
    open_positions_now: int = 0
    decisions_stop_hit: int = 0
    decisions_target_hit: int = 0
    decisions_time_stop: int = 0
    decisions_skipped_no_book: int = 0
    db_errors: int = 0
    last_decision_at: datetime | None = None


# ── Manager ──────────────────────────────────────────────────────────


class FastPathExitManager:
    """Streaming exit manager for fast-path paper positions."""

    def __init__(
        self,
        settings: FastPathSettings,
        engine: Engine,
        order_book: OrderBookAggregator,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._book = order_book
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._open: dict[int, _OpenPosition] = {}
        self._metrics = _ExitMetrics()
        self._max_hold_s = self._read_max_hold_s()

    @staticmethod
    def _read_max_hold_s() -> float:
        raw = (os.environ.get("CHILI_FAST_PATH_EXIT_TIME_STOP_S") or "").strip()
        if not raw:
            return float(MAX_HOLD_S_DEFAULT)
        try:
            return max(60.0, float(raw))
        except ValueError:
            return float(MAX_HOLD_S_DEFAULT)

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        # Bootstrap once before the loop so the first poll has cached
        # state. If this fails, log loud but don't block boot — the
        # poll loop will retry bootstrap on the next tick.
        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, self._bootstrap_open_positions)
        except Exception as exc:
            logger.warning("[fast_path] exit_manager bootstrap failed: %s",
                           exc, exc_info=True)
        logger.info(
            "[fast_path] exit_manager starting open=%d max_hold_s=%.0f atr_lookback=%d",
            len(self._open), self._max_hold_s, ATR_LOOKBACK_BARS,
        )
        self._task = asyncio.create_task(self._run(), name="fast_path_exit_manager")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None

    # ── Poll loop ─────────────────────────────────────────────────────

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=POLL_INTERVAL_S)
                    return  # _stop set
                except asyncio.TimeoutError:
                    pass
                try:
                    await self._poll_once()
                except Exception as exc:
                    self._metrics.db_errors += 1
                    logger.warning("[fast_path] exit_manager poll failed: %s",
                                   exc, exc_info=True)
        except asyncio.CancelledError:
            return

    async def _poll_once(self) -> None:
        self._metrics.polls_total += 1
        loop = asyncio.get_running_loop()

        # Re-bootstrap every N polls so freshly-written paper_fill rows
        # get picked up. We do this every poll — the query is fast and
        # short-circuits when no new rows exist.
        try:
            await loop.run_in_executor(None, self._bootstrap_open_positions)
        except Exception as exc:
            self._metrics.db_errors += 1
            logger.warning("[fast_path] exit_manager bootstrap retry failed: %s", exc)

        if not self._open:
            self._metrics.open_positions_now = 0
            return

        # Snapshot the cache so concurrent dict-mutation in the exit
        # path is safe (we evict keys as we exit).
        for entry_id in list(self._open.keys()):
            pos = self._open.get(entry_id)
            if pos is None:
                continue
            try:
                await self._evaluate_position(pos)
            except Exception as exc:
                logger.warning(
                    "[fast_path] exit_manager evaluate failed entry_id=%s: %s",
                    entry_id, exc, exc_info=True,
                )

        self._metrics.open_positions_now = len(self._open)

    # ── Bootstrap from DB ─────────────────────────────────────────────

    def _bootstrap_open_positions(self) -> None:
        """Find paper_fill rows that don't yet have a fast_exits row,
        compute brackets for any not already cached, add to in-memory.

        Idempotent — re-runs every poll. Computation cost is dominated
        by the LEFT JOIN; at steady state the IN-cache filter cuts the
        per-row cost to a single dict lookup.
        """
        self._metrics.bootstrap_runs += 1
        with self._engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT e.id, e.ticker, e.side, e.quantity, e.fill_price,
                       e.decided_at
                FROM fast_executions e
                LEFT JOIN fast_exits x
                  ON x.entry_execution_id = e.id
                WHERE e.decision = 'paper_fill'
                  AND e.mode = 'paper'
                  AND x.id IS NULL
                ORDER BY e.decided_at ASC
            """)).mappings().all()

        for row in rows:
            entry_id = int(row["id"])
            if entry_id in self._open:
                continue  # already cached
            ticker = str(row["ticker"])
            side = str(row["side"])
            qty = float(row["quantity"] or 0.0)
            entry_price = float(row["fill_price"] or 0.0)
            entered_at = row["decided_at"]
            if qty <= 0 or entry_price <= 0:
                # Malformed row — skip rather than enter a perpetually-
                # stuck position. A reconcile pass can mark it.
                logger.warning(
                    "[fast_path] exit_manager skipping malformed entry id=%s "
                    "qty=%r price=%r", entry_id, qty, entry_price,
                )
                continue
            # F6.5: try calibrated bracket and max_hold_s first; each
            # falls back independently to the cold-start default. We
            # need alert_type and signal_score for the lookup -- both
            # live on fast_alerts joined by (ticker, alert_type,
            # alert_fired_at) on the fast_executions row.
            alert_meta = self._fetch_source_alert_meta(entry_id)
            calibrated_bracket = None
            calibrated_max_hold = None
            bracket_source = "atr_fallback"
            if alert_meta is not None:
                from .calibration import (
                    compute_calibrated_bracket,
                    get_calibrated_max_hold_s,
                )
                try:
                    calibrated_bracket = compute_calibrated_bracket(
                        self._engine,
                        ticker=ticker,
                        alert_type=alert_meta["alert_type"],
                        signal_score=float(alert_meta["signal_score"]),
                        entry=entry_price,
                        direction="long" if side == "buy" else "short",
                    )
                    calibrated_max_hold = get_calibrated_max_hold_s(
                        self._engine,
                        ticker=ticker,
                        alert_type=alert_meta["alert_type"],
                        signal_score=float(alert_meta["signal_score"]),
                    )
                except Exception as exc:
                    logger.warning(
                        "[fast_path] exit_manager calibration lookup failed "
                        "entry_id=%d: %s -- falling back to ATR",
                        entry_id, exc,
                    )

            # Brackets need ATR — pull the most recent N closed bars.
            atr = self._compute_atr(ticker)
            if calibrated_bracket is not None:
                stop, target = calibrated_bracket
                bracket_source = "calibrated"
            else:
                try:
                    from ..stop_engine import compute_initial_bracket
                    stop, target = compute_initial_bracket(
                        entry=entry_price,
                        direction="long" if side == "buy" else "short",
                        atr=atr,
                        asset_class="crypto",
                        stop_model="atr_crypto_breakout",
                        regime=self._read_regime_or_default(),
                        lifecycle_stage="validated",
                    )
                except Exception as exc:
                    logger.warning(
                        "[fast_path] exit_manager compute_initial_bracket failed "
                        "ticker=%s entry=%s atr=%r: %s — skipping",
                        ticker, entry_price, atr, exc,
                    )
                    continue
            # F6.5: enrich brain_json with calibration provenance so
            # postmortem analysis can tell whether a position used
            # empirical or ATR-fallback brackets without re-querying.
            # IMPORTANT: ``computed_at`` is set ONCE here, at the moment
            # the bracket is decided. For F5-native trades that's within
            # ~1s of ``entered_at``; for F4-era inherited entries adopted
            # at first F5 boot it can be hours after entry. The gap
            # between this timestamp and ``entered_at`` is the load-
            # bearing classifier behind migration 219's
            # ``fast_exits_native`` view (see _migration_219_fast_exits_
            # native_view in app/migrations.py). Do NOT refresh this
            # timestamp on later updates or on restart-driven re-bootstrap
            # — that silently breaks the native-vs-inherited filter and
            # contaminates F6's training set with backfilled brackets.
            brain_payload = {
                "atr": atr,
                "stop_model": "atr_crypto_breakout",
                "regime": self._read_regime_or_default(),
                "lifecycle_stage": "validated",
                "atr_lookback_bars": ATR_LOOKBACK_BARS,
                "computed_at": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
                "bracket_source": bracket_source,
                "calibrated_max_hold_s": calibrated_max_hold,
                "effective_max_hold_s": (
                    float(calibrated_max_hold)
                    if calibrated_max_hold is not None
                    else float(self._max_hold_s)
                ),
            }
            self._open[entry_id] = _OpenPosition(
                entry_execution_id=entry_id,
                ticker=ticker,
                side=side,
                quantity=qty,
                entry_price=entry_price,
                entered_at=entered_at,
                stop_price=float(stop),
                target_price=float(target),
                atr=atr,
                brain_payload=brain_payload,
                max_hold_s=(
                    float(calibrated_max_hold)
                    if calibrated_max_hold is not None else None
                ),
            )
            self._metrics.positions_bootstrapped += 1
            logger.info(
                "[fast_path] exit_manager tracking entry_id=%d %s qty=%.8f "
                "entry=%.6f stop=%.6f target=%.6f atr=%r",
                entry_id, ticker, qty, entry_price, stop, target, atr,
            )

    def _fetch_source_alert_meta(self, entry_execution_id: int) -> dict | None:
        """JOIN fast_executions <-> fast_alerts on the denormalised
        (ticker, alert_type, alert_fired_at) tuple to recover the
        signal_score for calibration lookup.

        Inherited bootstrap entries have no matching alert row -- the
        return is None and the caller falls back to ATR. F4-native
        entries always have a match (the executor wrote the row from
        the alert it consumed).
        """
        try:
            with self._engine.begin() as conn:
                row = conn.execute(text("""
                    SELECT a.alert_type, a.signal_score
                    FROM fast_executions e
                    JOIN fast_alerts a
                      ON a.ticker = e.ticker
                     AND a.alert_type = e.alert_type
                     AND a.fired_at = e.alert_fired_at
                    WHERE e.id = :eid
                    LIMIT 1
                """), {"eid": int(entry_execution_id)}).mappings().one_or_none()
        except Exception:
            return None
        if row is None:
            return None
        return {
            "alert_type": row["alert_type"],
            "signal_score": float(row["signal_score"] or 0.0),
        }

    @staticmethod
    def _read_regime_or_default() -> str:
        """Best-effort regime read; default 'cautious' if unavailable.

        The regime module pulls from DB-cached regime indicators that
        the chili web container refreshes; the fast-data-worker may not
        have a fresh value at boot. Cautious is the safe default — it
        tightens stops slightly vs neutral.
        """
        try:
            from ..regime import get_regime_indicators
            r = get_regime_indicators()
            return str(r.get("regime_composite", "cautious"))
        except Exception:
            return "cautious"

    def _compute_atr(self, ticker: str) -> float | None:
        """Wilder's ATR(14) from the last 14 closed 1m bars in
        fast_snapshots. Returns None if fewer than 14 bars available
        (caller's stop_engine fallback policy fires)."""
        with self._engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT high_price, low_price, close_price
                FROM fast_snapshots
                WHERE ticker = :ticker
                  AND interval = '1m'
                ORDER BY bar_close_at DESC
                LIMIT :n
            """), {"ticker": ticker, "n": ATR_LOOKBACK_BARS + 1}).all()
        if len(rows) < ATR_LOOKBACK_BARS + 1:
            return None
        # rows[0] is most recent — flip to chronological
        rows = list(reversed(rows))
        trs: list[float] = []
        prev_close = float(rows[0][2])
        for h, l, c in rows[1:]:
            h_f, l_f, c_f = float(h), float(l), float(c)
            tr = max(
                h_f - l_f,
                abs(h_f - prev_close),
                abs(l_f - prev_close),
            )
            trs.append(tr)
            prev_close = c_f
        if not trs:
            return None
        return sum(trs) / len(trs)

    # ── Per-position evaluation ───────────────────────────────────────

    async def _evaluate_position(self, pos: _OpenPosition) -> None:
        t_start = time.monotonic()
        # Read top-of-book from the in-memory aggregator. Long-close
        # exits at best_bid (the price we'd be filled at on a paper
        # market sell).
        book = self._book._books.get(pos.ticker)  # noqa: SLF001 - read-only peek
        if book is None or not book.bids or not book.asks:
            # No book yet for this ticker — can't decide. Don't time
            # stop here either; the next poll will probably have a book.
            self._metrics.decisions_skipped_no_book += 1
            return

        best_bid = max(book.bids.keys()) if book.bids else 0.0
        best_ask = min(book.asks.keys()) if book.asks else 0.0
        if best_bid <= 0.0 or best_ask <= 0.0:
            self._metrics.decisions_skipped_no_book += 1
            return

        now = datetime.now(timezone.utc).replace(tzinfo=None)
        held_s = (now - pos.entered_at).total_seconds() if pos.entered_at else 0.0

        exit_reason: str | None = None
        # Order matters: stop (capital preservation) > target (profit
        # taking) > time stop (forced flatten). For a long, the exit
        # price is best_bid in all three cases (paper market sell).
        # F6.5: per-position max_hold_s if calibrated; else global.
        effective_max_hold = (
            pos.max_hold_s if pos.max_hold_s is not None else self._max_hold_s
        )
        if pos.side == "buy":
            if best_bid <= pos.stop_price:
                exit_reason = "stop_hit"
            elif best_bid >= pos.target_price:
                exit_reason = "target_hit"
            elif held_s >= effective_max_hold:
                exit_reason = "time_stop"
        else:
            # No spot-short positions in F4; defensive only.
            if best_ask >= pos.stop_price:
                exit_reason = "stop_hit"
            elif best_ask <= pos.target_price:
                exit_reason = "target_hit"
            elif held_s >= effective_max_hold:
                exit_reason = "time_stop"

        if exit_reason is None:
            return

        exit_price = best_bid if pos.side == "buy" else best_ask
        # Realised P/L: long = (exit - entry) * qty; short flipped.
        if pos.side == "buy":
            realized_pnl = (exit_price - pos.entry_price) * pos.quantity
            realized_return_pct = ((exit_price / pos.entry_price) - 1.0) * 100.0
        else:
            realized_pnl = (pos.entry_price - exit_price) * pos.quantity
            realized_return_pct = ((pos.entry_price / exit_price) - 1.0) * 100.0

        latency_ms = (time.monotonic() - t_start) * 1000.0
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, self._insert_exit_sync,
                pos, exit_reason, exit_price, realized_pnl,
                realized_return_pct, held_s, now, latency_ms,
            )
        except Exception as exc:
            self._metrics.db_errors += 1
            logger.warning(
                "[fast_path] exit_manager insert_exit failed entry_id=%d: %s",
                pos.entry_execution_id, exc, exc_info=True,
            )
            return

        # Bookkeeping: drop from cache, bump counters, log loud.
        self._open.pop(pos.entry_execution_id, None)
        if exit_reason == "stop_hit":
            self._metrics.decisions_stop_hit += 1
        elif exit_reason == "target_hit":
            self._metrics.decisions_target_hit += 1
        elif exit_reason == "time_stop":
            self._metrics.decisions_time_stop += 1
        self._metrics.last_decision_at = now
        logger.info(
            "[fast_path] exit_manager EXIT entry_id=%d %s reason=%s "
            "entry=%.6f exit=%.6f qty=%.8f pnl=%+.4f USD ret=%+.3f%% "
            "held=%.1fs",
            pos.entry_execution_id, pos.ticker, exit_reason,
            pos.entry_price, exit_price, pos.quantity,
            realized_pnl, realized_return_pct, held_s,
        )

    def _insert_exit_sync(
        self,
        pos: _OpenPosition,
        exit_reason: str,
        exit_price: float,
        realized_pnl: float,
        realized_return_pct: float,
        holding_period_s: float,
        exited_at: datetime,
        latency_ms: float,
    ) -> None:
        # Closing side: opposite of the entry side. For F4 we only
        # open longs (side='buy'); the close is therefore a 'sell'.
        close_side = "sell" if pos.side == "buy" else "buy"
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO fast_exits (
                    entry_execution_id, ticker, side, quantity,
                    entry_price, exit_price, stop_at_entry,
                    target_at_entry, exit_reason, realized_pnl_usd,
                    realized_return_pct, holding_period_s, mode,
                    broker_order_id, brain_json, latency_ms,
                    entered_at, exited_at
                ) VALUES (
                    :entry_execution_id, :ticker, :side, :quantity,
                    :entry_price, :exit_price, :stop_at_entry,
                    :target_at_entry, :exit_reason, :realized_pnl_usd,
                    :realized_return_pct, :holding_period_s, :mode,
                    :broker_order_id, CAST(:brain_json AS JSONB), :latency_ms,
                    :entered_at, :exited_at
                )
                ON CONFLICT (entry_execution_id, exited_at) DO NOTHING
            """), {
                "entry_execution_id": pos.entry_execution_id,
                "ticker": pos.ticker,
                "side": close_side,
                "quantity": pos.quantity,
                "entry_price": pos.entry_price,
                "exit_price": exit_price,
                "stop_at_entry": pos.stop_price,
                "target_at_entry": pos.target_price,
                "exit_reason": exit_reason,
                "realized_pnl_usd": realized_pnl,
                "realized_return_pct": realized_return_pct,
                "holding_period_s": holding_period_s,
                "mode": "paper",
                "broker_order_id": None,
                "brain_json": json.dumps(pos.brain_payload),
                "latency_ms": latency_ms,
                "entered_at": pos.entered_at,
                "exited_at": exited_at,
            })

    # ── Observability ─────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "polls_total": self._metrics.polls_total,
            "bootstrap_runs": self._metrics.bootstrap_runs,
            "positions_bootstrapped": self._metrics.positions_bootstrapped,
            "open_positions_now": self._metrics.open_positions_now,
            "decisions_stop_hit": self._metrics.decisions_stop_hit,
            "decisions_target_hit": self._metrics.decisions_target_hit,
            "decisions_time_stop": self._metrics.decisions_time_stop,
            "decisions_skipped_no_book": self._metrics.decisions_skipped_no_book,
            "db_errors": self._metrics.db_errors,
            "last_decision_at": (
                self._metrics.last_decision_at.isoformat()
                if self._metrics.last_decision_at else None
            ),
            "max_hold_s": self._max_hold_s,
            "tickers_tracked": sorted({p.ticker for p in self._open.values()}),
        }


__all__ = ["FastPathExitManager"]
