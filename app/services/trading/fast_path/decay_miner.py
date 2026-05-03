"""Fast-path signal decay miner (F6).

Event-driven brain node that learns the empirical forward-return
distribution of every fast-path alert per ``(ticker, alert_type,
score_bucket, horizon_s)`` and Welford-updates ``fast_signal_decay``
incrementally on each new observation.

Design commitments (see docs/STRATEGY/NEXT_TASK.md):
  - Event-driven, not cycle-based. The miner reacts to Postgres
    NOTIFY events; book emits act as the natural event clock that
    finalizes deadline-elapsed observations. No ``while True: sleep``.
  - LISTEN/NOTIFY pattern lifted from ``scripts/brain_worker.py:1308``
    (psycopg2 autocommit + select + conn.poll). Thread-bridged into
    asyncio via ``run_in_executor`` so the supervisor's event loop
    doesn't block.
  - Welford running stats. ``mean_return`` and ``m2_return`` update
    via the standard online formula
        delta  = x - mean_old
        mean   = mean_old + delta / n_new
        delta2 = x - mean_new
        m2     = m2_old   + delta * delta2
    inlined into a single UPSERT so each finalization is one DB
    round-trip and atomic against the cold-start backfill.

What it doesn't do:
  - Cold-start backfill: separate subtask, separate module entry
    point — see ``backfill_signal_decay`` in this file. Called once
    at start() if the table is sparse.
  - Calibrated value queries: consumers (exit_manager, gates,
    stop_engine) read the table themselves via small helper
    functions in ``calibration.py`` (subtask 5). The miner is a
    pure writer.

Threading:
  - One asyncio task owns the miner.
  - Inside that task, ``select.select`` on the LISTEN connection runs
    via ``loop.run_in_executor`` so a stuck DB doesn't block other
    fast_path tasks (executor, exit_manager).
  - DB writes (UPSERT, exit-validation, cache lookups) also run via
    run_in_executor.
"""
from __future__ import annotations

import asyncio
import heapq
import json
import logging
import os
import select
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .settings import FastPathSettings

logger = logging.getLogger(__name__)


# ── Tunables ─────────────────────────────────────────────────────────

# Forward-return measurement horizons (seconds). Spans the imbalance
# signal's quant-lit predictive window (1-5s) up through the existing
# MAX_HOLD_S default (4h). Adding a new horizon does not require a
# schema migration -- ``fast_signal_decay.horizon_s`` accepts any
# positive integer.
HORIZONS_S: tuple[int, ...] = (1, 5, 30, 60, 300, 1800, 3600, 14400)

# Score bucket boundaries -- enforced by CHECK on the table. Cowork's
# suggested split; finer slicing dilutes sample counts before we have
# enough data.
_BUCKET_LOW_HI = 0.40   # < this -> 'low'
_BUCKET_MED_HI = 0.65   # < this -> 'med', else 'high'

# Cap on the in-memory pending-observation heap. At ~10 alerts/min ×
# 8 horizons × 4h longest lookback the typical heap holds ~19k. The
# cap exists to prevent runaway memory if the miner gets stuck and
# the alert stream spikes; it should basically never trip.
DEFAULT_MAX_PENDING_OBS = 50_000

# How long to block on the LISTEN socket per executor call. Short
# enough that stop() can interrupt promptly; long enough that we
# aren't burning CPU on idle ticks.
LISTEN_POLL_TIMEOUT_S = 1.0

# Channels the miner subscribes to. Mirrors migration 221 trigger
# functions exactly -- changing one without the other is a sync bug.
CHANNEL_ALERT_INSERTED = "fp_alert_inserted"
CHANNEL_EXIT_INSERTED = "fp_exit_inserted"
CHANNEL_BOOK_INSERTED = "fp_book_inserted"


def score_bucket(score: float) -> str:
    """Map a raw [0,1]-ish signal_score into the canonical bucket."""
    s = float(score or 0.0)
    if s < _BUCKET_LOW_HI:
        return "low"
    if s < _BUCKET_MED_HI:
        return "med"
    return "high"


def _alert_direction(alert_type: str) -> str:
    """'long' / 'short' / 'neutral' from the alert_type convention.

    Naming is ``<signal>_long`` / ``<signal>_short`` for directional
    signals; everything else (``spread_squeeze``) is direction-
    neutral and treated as long for forward-return measurement (we
    only ever open longs in spot today; the short bucket data is
    kept for an eventual F8 "exit early on opposite-direction
    signal" feature).
    """
    a = (alert_type or "").lower()
    if a.endswith("_short"):
        return "short"
    if a.endswith("_long"):
        return "long"
    return "neutral"


# ── In-memory state ──────────────────────────────────────────────────


@dataclass(order=True)
class _PendingObs:
    """One pending forward-return observation."""

    deadline_unix: float
    # Tie-breaker so the heap doesn't try to compare dicts:
    seq: int = field(compare=True)
    alert_id: int = field(compare=False)
    ticker: str = field(compare=False)
    alert_type: str = field(compare=False)
    score_bucket_value: str = field(compare=False)
    horizon_s: int = field(compare=False)
    entry_at_alert: float = field(compare=False)
    direction: str = field(compare=False)  # 'long' / 'short' / 'neutral'
    fired_at: datetime = field(compare=False)


@dataclass
class _DecayMetrics:
    alerts_received: int = 0
    exits_received: int = 0
    book_ticks_received: int = 0
    obs_scheduled: int = 0
    obs_finalized_per_horizon: dict[int, int] = field(
        default_factory=lambda: {h: 0 for h in HORIZONS_S}
    )
    obs_dropped_overcap: int = 0
    obs_dropped_no_book: int = 0
    obs_dropped_malformed: int = 0
    obs_dropped_no_features: int = 0
    obs_dropped_alert_missing: int = 0
    obs_dropped_no_entry_price: int = 0
    validations_recorded: int = 0
    backfilled_rows_written: int = 0
    db_errors: int = 0
    last_finalize_at: datetime | None = None


# ── Miner ────────────────────────────────────────────────────────────


class FastPathDecayMiner:
    """Asyncio-managed signal-decay learner.

    Lifecycle:
      - ``start()`` opens the LISTEN connection, kicks the cold-start
        backfill if the table is sparse, and spawns the run loop.
      - ``stop()`` cancels the task, closes the LISTEN connection.
    """

    def __init__(
        self,
        settings: FastPathSettings,
        engine: Engine,
        *,
        max_pending_obs: int | None = None,
    ) -> None:
        self._settings = settings
        self._engine = engine
        self._max_pending_obs = int(
            max_pending_obs or os.environ.get(
                "CHILI_FAST_PATH_DECAY_MAX_PENDING", DEFAULT_MAX_PENDING_OBS
            )
        )
        self._stop = asyncio.Event()
        self._task: asyncio.Task | None = None
        self._listen_conn: Any = None  # psycopg2 connection
        self._pending: list[_PendingObs] = []
        self._seq_counter: int = 0
        self._metrics = _DecayMetrics()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        loop = asyncio.get_running_loop()
        # Open the LISTEN connection synchronously; failure here is
        # fatal (we don't want to silently start in poll-only mode --
        # the brief is explicit that this is event-driven).
        try:
            await loop.run_in_executor(None, self._open_listen_connection)
        except Exception as exc:
            logger.critical(
                "[fast_path] decay_miner failed to open LISTEN conn: %s "
                "-- miner will NOT start", exc, exc_info=True,
            )
            return

        # Cold-start backfill: separate function so subtask 4 can
        # iterate on it independently. Skipped if the table already
        # has any rows.
        try:
            written = await loop.run_in_executor(None, self._maybe_backfill)
            self._metrics.backfilled_rows_written = int(written or 0)
        except Exception as exc:
            self._metrics.db_errors += 1
            logger.warning(
                "[fast_path] decay_miner backfill failed: %s "
                "-- continuing with live-event-only mode",
                exc, exc_info=True,
            )

        logger.info(
            "[fast_path] decay_miner starting horizons=%s buckets=low<%.2f<med<%.2f<=high "
            "max_pending=%d backfilled=%d",
            HORIZONS_S, _BUCKET_LOW_HI, _BUCKET_MED_HI,
            self._max_pending_obs, self._metrics.backfilled_rows_written,
        )
        self._task = asyncio.create_task(self._run(), name="fast_path_decay_miner")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except asyncio.TimeoutError:
                self._task.cancel()
            self._task = None
        # Close listen connection on the calling thread; psycopg2
        # connections are not asyncio-aware but close() is fast.
        try:
            if self._listen_conn is not None:
                self._listen_conn.close()
        except Exception:
            pass
        self._listen_conn = None

    # ── Connection setup ──────────────────────────────────────────────

    def _open_listen_connection(self) -> None:
        """Open a dedicated psycopg2 connection in autocommit mode for
        LISTEN. Pattern lifted from scripts/brain_worker.py:1308.

        Uses the project's ``settings.database_url`` (same DSN the
        SQLAlchemy engine resolves). Returning a separate connection
        is necessary because psycopg2 can't do LISTEN over a pooled
        connection -- the LISTEN registration is per-connection."""
        import psycopg2
        from ....config import settings  # local: defers import cost
        dsn = settings.database_url
        if not dsn:
            raise RuntimeError("DATABASE_URL is empty -- cannot LISTEN")
        conn = psycopg2.connect(dsn)
        conn.set_isolation_level(0)  # autocommit; LISTEN requires it
        cur = conn.cursor()
        cur.execute(f"LISTEN {CHANNEL_ALERT_INSERTED}")
        cur.execute(f"LISTEN {CHANNEL_EXIT_INSERTED}")
        cur.execute(f"LISTEN {CHANNEL_BOOK_INSERTED}")
        cur.close()
        self._listen_conn = conn

    # ── Cold-start backfill (subtask 4) ───────────────────────────────

    def _maybe_backfill(self) -> int:
        """If ``fast_signal_decay`` is empty (cold start), batch-mine
        existing fast_alerts × fast_orderbook history.

        Returns the number of bucket rows written. The query is one
        DB-side aggregation pass per horizon -- no Python-side data
        loading. The SQL lives in this module rather than migrations
        because it's data-derivation (idempotent UPSERT against
        whatever's in fast_alerts), not schema.

        Skipped if the table already has rows -- the miner doesn't
        repeatedly re-mine; live NOTIFY-driven updates take over.
        """
        with self._engine.begin() as conn:
            existing = conn.execute(
                text("SELECT COUNT(*) FROM fast_signal_decay")
            ).scalar() or 0
        if existing > 0:
            logger.info(
                "[fast_path] decay_miner backfill SKIPPED -- table has %d rows",
                existing,
            )
            return 0

        logger.info(
            "[fast_path] decay_miner cold-start backfill BEGIN "
            "(7-day window across all 5 pairs × 8 horizons)",
        )
        t0 = time.monotonic()
        total_rows = 0
        for horizon in HORIZONS_S:
            try:
                with self._engine.begin() as conn:
                    n = conn.execute(text(_BACKFILL_UPSERT_SQL), {
                        "horizon": int(horizon),
                        "low_hi": _BUCKET_LOW_HI,
                        "med_hi": _BUCKET_MED_HI,
                    }).rowcount
                total_rows += int(n or 0)
            except Exception as exc:
                self._metrics.db_errors += 1
                logger.warning(
                    "[fast_path] decay_miner backfill horizon=%ds failed: %s",
                    horizon, exc, exc_info=True,
                )
        dt = time.monotonic() - t0
        logger.info(
            "[fast_path] decay_miner backfill DONE rows=%d elapsed=%.2fs",
            total_rows, dt,
        )
        return total_rows

    # ── Run loop ──────────────────────────────────────────────────────

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        try:
            while not self._stop.is_set():
                try:
                    notifs = await loop.run_in_executor(
                        None, self._poll_listen_blocking, LISTEN_POLL_TIMEOUT_S,
                    )
                except Exception as exc:
                    self._metrics.db_errors += 1
                    logger.warning(
                        "[fast_path] decay_miner LISTEN poll failed: %s "
                        "-- attempting to reopen",
                        exc, exc_info=True,
                    )
                    try:
                        await loop.run_in_executor(None, self._open_listen_connection)
                    except Exception as exc2:
                        logger.error(
                            "[fast_path] decay_miner reopen failed: %s "
                            "-- backing off 5s", exc2,
                        )
                        await asyncio.sleep(5.0)
                    continue

                for n in notifs:
                    try:
                        await self._dispatch_notification(n)
                    except Exception as exc:
                        self._metrics.db_errors += 1
                        logger.warning(
                            "[fast_path] decay_miner dispatch failed channel=%s: %s",
                            getattr(n, "channel", "?"), exc, exc_info=True,
                        )

                # Always finalize due observations on each pass --
                # even if no NOTIFY arrived, time may have advanced
                # past a deadline (mostly harmless: book NOTIFYs fire
                # ~14/sec under live load, so deadlines are checked
                # frequently in practice).
                try:
                    await loop.run_in_executor(None, self._finalize_due_observations)
                except Exception as exc:
                    self._metrics.db_errors += 1
                    logger.warning(
                        "[fast_path] decay_miner finalize pass failed: %s",
                        exc, exc_info=True,
                    )
        except asyncio.CancelledError:
            return

    def _poll_listen_blocking(self, timeout_s: float) -> list:
        """Block up to *timeout_s* on the LISTEN socket; return any
        notifications drained from conn.notifies. Empty list on
        timeout. Called from the executor thread, NOT the event loop.
        """
        if self._listen_conn is None:
            raise RuntimeError("LISTEN connection not open")
        ready = select.select([self._listen_conn], [], [], timeout_s)
        if ready == ([], [], []):
            return []
        self._listen_conn.poll()
        out = []
        while self._listen_conn.notifies:
            out.append(self._listen_conn.notifies.pop(0))
        return out

    # ── Notification handlers ─────────────────────────────────────────

    async def _dispatch_notification(self, n) -> None:
        loop = asyncio.get_running_loop()
        ch = n.channel
        if ch == CHANNEL_ALERT_INSERTED:
            self._metrics.alerts_received += 1
            try:
                payload = json.loads(n.payload)
            except (TypeError, ValueError):
                self._metrics.obs_dropped_malformed += 1
                return
            await loop.run_in_executor(None, self._handle_alert_inserted, payload)
        elif ch == CHANNEL_EXIT_INSERTED:
            self._metrics.exits_received += 1
            try:
                payload = json.loads(n.payload)
            except (TypeError, ValueError):
                self._metrics.obs_dropped_malformed += 1
                return
            await loop.run_in_executor(None, self._handle_exit_inserted, payload)
        elif ch == CHANNEL_BOOK_INSERTED:
            # Pure event-clock tick. Finalization happens on every
            # loop pass anyway; we just count for observability.
            self._metrics.book_ticks_received += 1

    def _handle_alert_inserted(self, payload: dict) -> None:
        """An alert was just inserted -- look up its features (best_ask
        at fire time) and schedule 8 forward-return observations on
        the heap."""
        alert_id = int(payload.get("id") or 0)
        if alert_id <= 0:
            return
        ticker = str(payload.get("ticker") or "")
        alert_type = str(payload.get("alert_type") or "")
        signal_score = float(payload.get("signal_score") or 0.0)
        if not ticker or not alert_type:
            self._metrics.obs_dropped_malformed += 1
            return

        # Look up entry_at_alert from the alert's features blob. The
        # scanner writes ``best_ask`` into features; that's what F4
        # would have used as fill price for a long.
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                SELECT features, fired_at
                FROM fast_alerts
                WHERE id = :id
            """), {"id": alert_id}).mappings().one_or_none()
        if row is None:
            self._metrics.obs_dropped_alert_missing += 1
            return

        features = row["features"] or {}
        if isinstance(features, str):
            try:
                features = json.loads(features)
            except (TypeError, ValueError):
                features = {}
        best_ask = float(features.get("best_ask") or 0.0)
        best_bid = float(features.get("best_bid") or 0.0)
        # ``close`` is carried by bar-derived alerts (volume_breakout_*)
        # which don't have best_bid/best_ask in features. For those,
        # fall back to ``close`` -- close-of-firing-bar is what F4
        # would have read for entry on a bar-close signal.
        bar_close = float(features.get("close") or 0.0)
        direction = _alert_direction(alert_type)
        if direction == "short":
            entry_at_alert = (
                best_bid if best_bid > 0
                else (
                    (best_bid + best_ask) / 2.0
                    if (best_bid > 0 and best_ask > 0)
                    else (bar_close if bar_close > 0 else 0.0)
                )
            )
        else:
            entry_at_alert = (
                best_ask if best_ask > 0
                else (
                    (best_bid + best_ask) / 2.0
                    if (best_bid > 0 and best_ask > 0)
                    else (bar_close if bar_close > 0 else 0.0)
                )
            )
        if entry_at_alert <= 0:
            self._metrics.obs_dropped_no_entry_price += 1
            if self._metrics.obs_dropped_no_entry_price <= 5:
                logger.warning(
                    "[fast_path] decay_miner alert %d ticker=%s type=%s "
                    "no entry price (best_ask=%r best_bid=%r features keys=%r)",
                    alert_id, ticker, alert_type,
                    features.get("best_ask"), features.get("best_bid"),
                    list(features.keys()) if isinstance(features, dict) else type(features).__name__,
                )
            return

        bucket = score_bucket(signal_score)
        fired_at: datetime = row["fired_at"]
        fired_unix = fired_at.replace(tzinfo=timezone.utc).timestamp() \
            if fired_at.tzinfo is None else fired_at.timestamp()

        # Schedule the 8 horizons.
        if len(self._pending) + len(HORIZONS_S) > self._max_pending_obs:
            self._metrics.obs_dropped_overcap += len(HORIZONS_S)
            logger.warning(
                "[fast_path] decay_miner heap at cap %d -- dropping alert id=%d",
                self._max_pending_obs, alert_id,
            )
            return
        for horizon in HORIZONS_S:
            self._seq_counter += 1
            obs = _PendingObs(
                deadline_unix=fired_unix + horizon,
                seq=self._seq_counter,
                alert_id=alert_id,
                ticker=ticker,
                alert_type=alert_type,
                score_bucket_value=bucket,
                horizon_s=horizon,
                entry_at_alert=entry_at_alert,
                direction=direction,
                fired_at=fired_at,
            )
            heapq.heappush(self._pending, obs)
            self._metrics.obs_scheduled += 1

    def _handle_exit_inserted(self, payload: dict) -> None:
        """An exit was just inserted -- find the alert that triggered
        the entry, look up the matching ``fast_signal_decay`` row at
        the holding-time horizon, and update the validation-residual
        running mean (mean absolute error vs. predicted return)."""
        entry_execution_id = int(payload.get("entry_execution_id") or 0)
        if entry_execution_id <= 0:
            return
        try:
            realized_return_frac = float(
                payload.get("realized_return_pct") or 0.0
            ) / 100.0
        except (TypeError, ValueError):
            return
        try:
            holding_period_s = float(payload.get("holding_period_s") or 0.0)
        except (TypeError, ValueError):
            return

        # Map exit -> alert. fast_executions denormalises alert_type
        # and alert_fired_at on each row (no FK), so we join on
        # (ticker, alert_type, fired_at == alert_fired_at). Inherited
        # bootstrap entries have no matching alert -> alert_row is
        # None and we skip validation.
        #
        # F-hygiene-2: ORDER BY a.id DESC LIMIT 1 + .first() prevents
        # MultipleResultsFound when several fast_alerts rows share an
        # exact (ticker, alert_type, fired_at) triple. That happens
        # naturally during snapshot-replay catchup, when the F8a
        # deferred-emit heap drains a burst of entries on one book
        # emit and they all fire with the same wall-clock fired_at to
        # microsecond precision. Most-recent-id wins is fine because
        # the duplicates are functionally identical (same source bar).
        with self._engine.begin() as conn:
            alert_row = conn.execute(text("""
                SELECT a.ticker, a.alert_type, a.signal_score
                FROM fast_executions e
                JOIN fast_alerts a
                  ON a.ticker = e.ticker
                 AND a.alert_type = e.alert_type
                 AND a.fired_at = e.alert_fired_at
                WHERE e.id = :eid
                ORDER BY a.id DESC
                LIMIT 1
            """), {"eid": entry_execution_id}).mappings().first()
        if alert_row is None:
            return  # entry wasn't from a tracked alert (e.g. inherited)

        ticker = alert_row["ticker"]
        alert_type = alert_row["alert_type"]
        bucket = score_bucket(float(alert_row.get("signal_score") or 0.0))

        # Find the closest horizon to the actual holding time.
        horizon_chosen = min(
            HORIZONS_S, key=lambda h: abs(h - holding_period_s),
        )

        # Update the validation columns Welford-style on the residual.
        # Residual is |realized - predicted|; we keep the running mean.
        with self._engine.begin() as conn:
            conn.execute(text("""
                UPDATE fast_signal_decay
                   SET realized_validation_count = realized_validation_count + 1,
                       realized_validation_residual =
                           realized_validation_residual
                           + (ABS(:realized - mean_return) - realized_validation_residual)
                             / (realized_validation_count + 1),
                       last_updated = NOW()
                 WHERE ticker = :t
                   AND alert_type = :at
                   AND score_bucket = :sb
                   AND horizon_s = :h
            """), {
                "realized": realized_return_frac,
                "t": ticker, "at": alert_type, "sb": bucket,
                "h": int(horizon_chosen),
            })
        self._metrics.validations_recorded += 1

    # ── Finalization (event clock) ────────────────────────────────────

    def _finalize_due_observations(self) -> None:
        """Pop and finalize every pending observation whose deadline
        has elapsed. Each finalization is one Welford UPSERT.

        Bound: if many observations are due simultaneously (e.g. just
        after a large book burst following a long quiet window), we
        finalize them one round-trip each. Could be batched if it
        ever becomes hot, but at current cadence this stays under
        single-digit %% of the loop's wall time.
        """
        if not self._pending:
            return
        now_unix = time.time()
        while self._pending and self._pending[0].deadline_unix <= now_unix:
            obs = heapq.heappop(self._pending)
            try:
                self._finalize_one_obs(obs)
            except Exception as exc:
                self._metrics.db_errors += 1
                logger.warning(
                    "[fast_path] decay_miner finalize_one failed alert=%d horizon=%d: %s",
                    obs.alert_id, obs.horizon_s, exc, exc_info=True,
                )

    def _finalize_one_obs(self, obs: _PendingObs) -> None:
        """Look up the book mid at obs.fired_at + obs.horizon_s and
        Welford-update the corresponding ``fast_signal_decay`` row.
        """
        target_at = obs.fired_at + _td_seconds(obs.horizon_s)
        with self._engine.begin() as conn:
            book_row = conn.execute(text("""
                SELECT bid_levels, ask_levels
                FROM fast_orderbook
                WHERE ticker = :t
                  AND snapshot_at >= :ts
                ORDER BY snapshot_at ASC
                LIMIT 1
            """), {"t": obs.ticker, "ts": target_at}).mappings().one_or_none()
        if book_row is None:
            self._metrics.obs_dropped_no_book += 1
            return
        bid_levels = book_row["bid_levels"] or []
        ask_levels = book_row["ask_levels"] or []
        best_bid = float(bid_levels[0][0]) if bid_levels else 0.0
        best_ask = float(ask_levels[0][0]) if ask_levels else 0.0
        if best_bid <= 0 or best_ask <= 0:
            self._metrics.obs_dropped_no_book += 1
            return
        mid = (best_bid + best_ask) / 2.0

        # Forward return (as fraction): direction-aware.
        if obs.direction == "short":
            forward_return = (obs.entry_at_alert - mid) / obs.entry_at_alert
        else:
            forward_return = (mid - obs.entry_at_alert) / obs.entry_at_alert

        self._welford_upsert(
            ticker=obs.ticker,
            alert_type=obs.alert_type,
            score_bucket_value=obs.score_bucket_value,
            horizon_s=obs.horizon_s,
            x=forward_return,
        )
        self._metrics.obs_finalized_per_horizon[obs.horizon_s] = (
            self._metrics.obs_finalized_per_horizon.get(obs.horizon_s, 0) + 1
        )
        self._metrics.last_finalize_at = datetime.now(
            timezone.utc).replace(tzinfo=None)

    def _welford_upsert(
        self, *,
        ticker: str,
        alert_type: str,
        score_bucket_value: str,
        horizon_s: int,
        x: float,
    ) -> None:
        """Atomic Welford update via INSERT ... ON CONFLICT.

        On first observation: row with sample_count=1, mean=x, m2=0.
        On subsequent: standard Welford expansion using the OLD
        ``fast_signal_decay.X`` values (which ON CONFLICT exposes as
        the table-qualified reference, while ``EXCLUDED.X`` would be
        the would-be-inserted values).
        """
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO fast_signal_decay (
                    ticker, alert_type, score_bucket, horizon_s,
                    sample_count, mean_return, m2_return, last_updated
                ) VALUES (
                    :t, :at, :sb, :h, 1, :x, 0, NOW()
                )
                ON CONFLICT (ticker, alert_type, score_bucket, horizon_s)
                DO UPDATE SET
                    sample_count = fast_signal_decay.sample_count + 1,
                    mean_return =
                        fast_signal_decay.mean_return
                        + (:x - fast_signal_decay.mean_return)
                          / (fast_signal_decay.sample_count + 1),
                    m2_return =
                        fast_signal_decay.m2_return
                        + (:x - fast_signal_decay.mean_return)
                        * (:x - (
                            fast_signal_decay.mean_return
                            + (:x - fast_signal_decay.mean_return)
                              / (fast_signal_decay.sample_count + 1)
                          )),
                    last_updated = NOW()
            """), {
                "t": ticker, "at": alert_type, "sb": score_bucket_value,
                "h": int(horizon_s), "x": float(x),
            })

    # ── Watchdog accessor ─────────────────────────────────────────────

    def get_task(self) -> asyncio.Task | None:
        """Return the running asyncio Task (or None if start() not called).

        F-hygiene-1: the supervisor's watchdog uses this to introspect
        ``done()`` / ``exception()`` for silent-failure detection. Read-
        only -- the watchdog doesn't restart the task; it just reports.
        """
        return self._task

    # ── Observability ─────────────────────────────────────────────────

    def stats(self) -> dict[str, Any]:
        return {
            "alerts_received": self._metrics.alerts_received,
            "exits_received": self._metrics.exits_received,
            "book_ticks_received": self._metrics.book_ticks_received,
            "obs_scheduled": self._metrics.obs_scheduled,
            "obs_finalized_per_horizon": dict(
                self._metrics.obs_finalized_per_horizon
            ),
            "obs_dropped_overcap": self._metrics.obs_dropped_overcap,
            "obs_dropped_no_book": self._metrics.obs_dropped_no_book,
            "obs_dropped_malformed": self._metrics.obs_dropped_malformed,
            "obs_dropped_no_features": self._metrics.obs_dropped_no_features,
            "obs_dropped_alert_missing": self._metrics.obs_dropped_alert_missing,
            "obs_dropped_no_entry_price": self._metrics.obs_dropped_no_entry_price,
            "validations_recorded": self._metrics.validations_recorded,
            "backfilled_rows_written": self._metrics.backfilled_rows_written,
            "db_errors": self._metrics.db_errors,
            "pending_heap_size": len(self._pending),
            "last_finalize_at": (
                self._metrics.last_finalize_at.isoformat()
                if self._metrics.last_finalize_at else None
            ),
        }


def _td_seconds(s: int):
    """Local helper: ``timedelta(seconds=s)`` -- defensive against
    older Python where the ``+`` of a datetime and an int isn't
    supported. (3.11 supports neither, so this is required.)"""
    from datetime import timedelta
    return timedelta(seconds=int(s))


# ── Cold-start backfill SQL (subtask 4) ──────────────────────────────

# One-pass-per-horizon UPSERT. For each fast_alerts row:
#   - bucket the signal_score
#   - find entry_at_alert from features.best_ask (long) or
#     features.best_bid (short) -- mid as fallback
#   - find the first fast_orderbook row at or after fired_at + horizon
#     and compute its mid
#   - forward_return is direction-aware
# Aggregate into (ticker, alert_type, score_bucket, horizon) buckets,
# computing sample_count, mean_return, and m2_return via
# ``COUNT(*)``, ``AVG(forward_return)``, and a stddev_pop trick.
#
# Welford's M2 = sum((x - mean)^2) for n>=1. Postgres exposes that
# directly as VAR_POP(x) * COUNT(x) -- which is the population
# variance × n -- exactly M2.
#
# 7-day window per the brief; bound to keep backfill < 60s on the
# current dataset (hundreds of alerts × 100k books).
_BACKFILL_UPSERT_SQL = """
WITH alerts AS (
    SELECT a.id, a.ticker, a.alert_type,
           CASE
               WHEN COALESCE(a.signal_score, 0) < :low_hi THEN 'low'
               WHEN COALESCE(a.signal_score, 0) < :med_hi THEN 'med'
               ELSE 'high'
           END AS score_bucket,
           a.fired_at,
           CASE
               WHEN a.alert_type LIKE '%%_short' THEN 'short'
               WHEN a.alert_type LIKE '%%_long'  THEN 'long'
               ELSE 'neutral'
           END AS direction,
           -- Order-book signals carry best_bid/best_ask. Bar-derived
           -- signals (volume_breakout_*) carry close instead. Try the
           -- direction-appropriate book side first, then fall back to
           -- close, then 0 (which the WHERE filter excludes).
           CASE
               WHEN a.alert_type LIKE '%%_short' THEN
                   COALESCE(
                     NULLIF((a.features->>'best_bid')::float, 0),
                     NULLIF((a.features->>'close')::float, 0),
                     0
                   )
               ELSE
                   COALESCE(
                     NULLIF((a.features->>'best_ask')::float, 0),
                     NULLIF((a.features->>'close')::float, 0),
                     0
                   )
           END AS entry_at_alert
    FROM fast_alerts a
    WHERE a.fired_at > NOW() - INTERVAL '7 days'
),
horizon_books AS (
    SELECT al.id AS alert_id, al.ticker, al.alert_type,
           al.score_bucket, al.direction, al.entry_at_alert,
           (
               SELECT (
                   (b.bid_levels->0->>0)::float
                 + (b.ask_levels->0->>0)::float
               ) / 2.0
                 FROM fast_orderbook b
                WHERE b.ticker = al.ticker
                  AND b.snapshot_at >= al.fired_at + (:horizon || ' seconds')::interval
                  AND jsonb_array_length(b.bid_levels) > 0
                  AND jsonb_array_length(b.ask_levels) > 0
                ORDER BY b.snapshot_at ASC
                LIMIT 1
           ) AS mid_at_horizon
    FROM alerts al
    WHERE al.entry_at_alert > 0
),
forward_returns AS (
    SELECT ticker, alert_type, score_bucket,
           CASE
               WHEN direction = 'short' THEN
                   (entry_at_alert - mid_at_horizon) / entry_at_alert
               ELSE
                   (mid_at_horizon - entry_at_alert) / entry_at_alert
           END AS r
    FROM horizon_books
    WHERE mid_at_horizon IS NOT NULL
      AND mid_at_horizon > 0
)
INSERT INTO fast_signal_decay (
    ticker, alert_type, score_bucket, horizon_s,
    sample_count, mean_return, m2_return, last_updated
)
SELECT ticker, alert_type, score_bucket, :horizon AS horizon_s,
       COUNT(*) AS sample_count,
       AVG(r)   AS mean_return,
       -- M2 = sum of squared deviations = variance_population * n
       COALESCE(VAR_POP(r) * COUNT(*), 0) AS m2_return,
       NOW()
FROM forward_returns
GROUP BY ticker, alert_type, score_bucket
ON CONFLICT (ticker, alert_type, score_bucket, horizon_s) DO UPDATE SET
    sample_count = EXCLUDED.sample_count,
    mean_return = EXCLUDED.mean_return,
    m2_return   = EXCLUDED.m2_return,
    last_updated = EXCLUDED.last_updated;
"""


__all__ = ["FastPathDecayMiner", "score_bucket"]
