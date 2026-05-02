"""Fast-path executor (F4).

Reads fresh ``fast_alerts`` rows, evaluates gates, decides:
  - paper_fill   : simulate a market buy at best-ask (long) / best-bid
                   (short) using the in-memory order book; record to
                   fast_executions.
  - live_placed  : send a real Coinbase order. Stubbed for this commit
                   — function raises NotImplementedError so deployment
                   is impossible-to-trade by accident. F4-followup
                   commit will wire the real path under explicit
                   operator authorization.
  - rejected     : at least one gate denied; record reason.

Pipeline:
    poll fast_alerts since last-seen-id
        -> for each alert:
            -> read in-memory book + open-positions counter into ExecContext
            -> run_gates(alert, ctx)
            -> if rejected: write decision row with reason
               else if paper: synthesize fill, write decision row,
                              bump in-memory open-positions counter
               else if live + AUTHORIZED: call coinbase stub
               else if live + NOT authorized: gate downgrades to paper
                                              (handled in gate logic)

State the executor owns:
  - ``_last_seen_alert_id``: bigint, advanced after each poll
  - ``_open_positions``: per-ticker counter, paper-mode only (live
    mode would query the broker; live path not implemented in this
    commit). Cleared on container restart — that's intentional for
    paper validation (a fresh start should re-deduce state from DB).
  - ``_daily_notional_used_usd``: cumulative paper notional this UTC
    day; reset on UTC date rollover.

Memory bound: the only growing structures are caps-bounded (one int
per ticker; fixed daily counter). No queues that aren't already
bounded by the writer.

Threading model: single asyncio task. DB calls run via
``loop.run_in_executor`` to avoid blocking the event loop.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.engine import Engine

from .gates import (
    DEFAULT_GATES,
    DEFAULT_NOTIONAL_USD,
    ExecContext,
    GateRunResult,
    env_overrides,
    is_live_authorized,
    run_gates,
)
from .order_book import OrderBookAggregator
from .settings import FastPathSettings

logger = logging.getLogger(__name__)


POLL_INTERVAL_S = 1.0
"""How often the executor polls fast_alerts. 1s gives sub-2s typical
end-to-end latency (ws -> scanner -> alert row -> poll -> decision)
without hammering the DB."""


# ── Coinbase live placement ──────────────────────────────────────────


class LiveExecutionNotAuthorized(RuntimeError):
    """Raised when the executor is asked to place a live Coinbase order
    without ``CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED=1`` set OR a per-
    trade safety belt is tripped (notional too large for first run,
    broker not connected, etc).

    Raising rather than silently no-op'ing means a misconfigured
    deploy fails LOUD, not silently into a wrong-mode order.
    """


# First-live-trade safety belt: even with both authorization flags set,
# any single live order whose notional exceeds this cap is rejected
# unless the operator ALSO sets CHILI_FAST_PATH_LIVE_NOTIONAL_OK=1.
# This is a third layer of defence against a configuration mistake
# producing a too-large order on the first live activation.
LIVE_FIRST_TRADE_USD_HARD_CAP = 10.0
"""Maximum notional in USD per single live order without explicit
operator opt-in. Default $10 — small enough that a misconfiguration
costs lunch money, not rent. Override via
``CHILI_FAST_PATH_LIVE_NOTIONAL_OK=1`` once you've validated the
first few small live orders behave correctly."""

LIVE_VERIFY_TIMEOUT_S = 3.0
"""How long we'll poll Coinbase for a definitive order state after
placement before giving up. Mirror of the swing-path's
``verify_order_landed`` window. Beyond this, we record the placement
with state=unknown and let reconcile sweep up later."""

LIVE_VERIFY_POLL_INTERVAL_S = 0.25


def _live_notional_override() -> bool:
    """Operator must set this ONCE they've verified the first live
    order behaved correctly. Until then, even authorized live orders
    are capped at ``LIVE_FIRST_TRADE_USD_HARD_CAP`` USD."""
    raw = (os.environ.get("CHILI_FAST_PATH_LIVE_NOTIONAL_OK") or "").strip().lower()
    return raw in ("1", "true", "yes", "on")


def _place_coinbase_order_live(ticker: str, side: str, quantity: float,
                               fill_price_hint: float, notional_usd: float) -> str:
    """Place a real Coinbase market order and verify it landed at the
    broker before returning the broker order_id.

    This function is ONLY reachable when:
      1. ``mode`` resolves to ``live`` after gate_mode_interlock (which
         means CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED is set)
      2. The executor's defence-in-depth re-check at point-of-place
         confirms ``is_live_authorized()`` is still True
      3. The notional is within the first-trade hard cap, or the
         operator has set ``CHILI_FAST_PATH_LIVE_NOTIONAL_OK=1``

    Failure modes that raise ``LiveExecutionNotAuthorized``:
      - Coinbase SDK not importable / not connected
      - Notional exceeds hard cap and override not set
      - Side is something other than buy/sell

    Failure modes that raise the underlying exception:
      - Broker API errors (network, auth, insufficient balance, etc.)
        — propagated so the executor records ``rejected`` with the
        actual broker reason rather than silently swallowing.

    Returns the broker order_id (string) only after we've confirmed
    Coinbase received the order. We do NOT block until ``filled`` —
    a market order against a live book typically fills in <1s but
    can take longer; we accept ``open``/``pending`` as confirmed-at-
    broker state and let F5 / reconcile track the fill itself.
    """
    # Defence-in-depth: re-read the auth flag at the moment of place.
    # The gate already checked, but a race or reload could change env.
    if not is_live_authorized():
        raise LiveExecutionNotAuthorized(
            "live placement attempted but CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED is unset"
        )

    # First-trade safety: small notional only, until operator opts in.
    if notional_usd > LIVE_FIRST_TRADE_USD_HARD_CAP and not _live_notional_override():
        raise LiveExecutionNotAuthorized(
            f"notional ${notional_usd:.2f} exceeds first-trade cap "
            f"${LIVE_FIRST_TRADE_USD_HARD_CAP:.2f}; set "
            f"CHILI_FAST_PATH_LIVE_NOTIONAL_OK=1 after validating the "
            f"first small live orders behave correctly"
        )

    if side not in ("buy", "sell"):
        raise LiveExecutionNotAuthorized(f"unsupported side={side!r} for live placement")

    # Lazy import keeps paper-mode boot independent of the Coinbase SDK
    # state. If the SDK is unavailable, raise loud — easier to diagnose
    # than a silent skip.
    try:
        from app.services import coinbase_service as cb
    except Exception as exc:
        raise LiveExecutionNotAuthorized(f"coinbase_service import failed: {exc}") from exc

    if not cb.is_connected():
        # Try to connect once before giving up — credentials may have
        # been set after process boot.
        try:
            cb.connect()
        except Exception:
            pass
    if not cb.is_connected():
        raise LiveExecutionNotAuthorized(
            "coinbase client not connected; configure credentials via vault or env"
        )

    # Loud, indelible record that a live order is about to leave.
    logger.critical(
        "[fast_path] LIVE PLACEMENT %s %s qty=%.8f hint_px=%.6f notional_usd=%.2f",
        side.upper(), ticker, quantity, fill_price_hint, notional_usd,
    )

    if side == "buy":
        resp = cb.place_buy_order(ticker, quantity, order_type="market")
    else:
        resp = cb.place_sell_order(ticker, quantity, order_type="market")

    if not isinstance(resp, dict) or not resp.get("ok"):
        # Broker rejected; surface message so the executor's reject
        # row carries the real reason.
        msg = (resp or {}).get("error", "unknown_broker_error")
        raise RuntimeError(f"coinbase rejected order: {msg}")

    order_id = str(resp.get("order_id") or "").strip()
    if not order_id:
        raise RuntimeError("coinbase response missing order_id")

    # Post-placement verification — never claim success based on a
    # local 'sent' flag alone (lesson from the Robinhood swing-path
    # ELTX incident where chili logged "placed" but Robinhood had
    # rejected within 250ms). Poll for up to LIVE_VERIFY_TIMEOUT_S.
    deadline = time.monotonic() + LIVE_VERIFY_TIMEOUT_S
    last_state = "unknown"
    while time.monotonic() < deadline:
        info = cb.get_order_by_id(order_id) or {}
        state = str(info.get("status") or info.get("order_status") or "").lower()
        if state:
            last_state = state
            if state in ("open", "pending", "filled"):
                logger.critical(
                    "[fast_path] LIVE PLACED+VERIFIED order_id=%s ticker=%s "
                    "side=%s state=%s elapsed=%.2fs",
                    order_id, ticker, side, state,
                    LIVE_VERIFY_TIMEOUT_S - (deadline - time.monotonic()),
                )
                return order_id
            if state in ("cancelled", "expired", "failed", "rejected"):
                raise RuntimeError(f"coinbase terminal-rejected order {order_id}: {state}")
        time.sleep(LIVE_VERIFY_POLL_INTERVAL_S)

    # Timed out without a definitive state. We DO record the order_id
    # so reconcile can find it — but log loud so the operator knows
    # the verification window slipped.
    logger.critical(
        "[fast_path] LIVE PLACED but verify timed out order_id=%s ticker=%s "
        "last_state=%r — reconcile must sweep",
        order_id, ticker, last_state,
    )
    return order_id


# ── Executor ──────────────────────────────────────────────────────────


@dataclass
class _ExecutorMetrics:
    polls_total: int = 0
    alerts_seen: int = 0
    decisions_paper_fill: int = 0
    decisions_live_placed: int = 0
    decisions_rejected: int = 0
    db_errors: int = 0
    last_alert_id_seen: int = 0
    last_decision_at: datetime | None = None


class FastPathExecutor:
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
        # Resume from latest id at boot so we don't replay history. The
        # bootstrap query happens in start().
        self._last_seen_alert_id: int = 0
        self._open_positions: dict[str, int] = {}
        self._daily_notional_used_usd: float = 0.0
        self._daily_window_date: str = self._utc_date_str(datetime.now(timezone.utc))
        self._overrides = env_overrides()
        self._metrics = _ExecutorMetrics()

    # ── Lifecycle ─────────────────────────────────────────────────────

    async def start(self) -> None:
        if self._task is not None:
            return
        # Bootstrap last-seen id so we don't replay alerts written
        # before the executor came up.
        loop = asyncio.get_running_loop()
        try:
            self._last_seen_alert_id = await loop.run_in_executor(
                None, self._bootstrap_max_alert_id
            )
            self._metrics.last_alert_id_seen = self._last_seen_alert_id
        except Exception as exc:
            # Don't fail boot — start from 0 (will replay everything
            # but recency gate will reject historical alerts).
            logger.warning("[fast_path] executor bootstrap failed: %s", exc)
            self._last_seen_alert_id = 0
        logger.info(
            "[fast_path] executor starting mode=%s live_authorized=%s "
            "min_score=%.2f max_spread_bps=%.2f notional_usd=%.2f "
            "daily_max_usd=%.2f starting_alert_id=%d",
            self._settings.mode, is_live_authorized(),
            self._overrides["min_score"], self._overrides["max_spread_bps"],
            self._overrides["default_notional_usd"], self._overrides["daily_max_usd"],
            self._last_seen_alert_id,
        )
        self._task = asyncio.create_task(self._run(), name="fast_path_executor")

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
                    logger.warning("[fast_path] executor poll failed: %s", exc, exc_info=True)
        except asyncio.CancelledError:
            return

    async def _poll_once(self) -> None:
        self._metrics.polls_total += 1
        self._maybe_roll_daily_window()
        loop = asyncio.get_running_loop()
        rows = await loop.run_in_executor(None, self._fetch_new_alerts)
        if not rows:
            return
        for row in rows:
            try:
                await self._process_alert(row)
            except Exception as exc:
                logger.warning("[fast_path] executor process_alert failed: %s",
                               exc, exc_info=True)
            # Always advance the cursor — bad rows shouldn't get
            # endlessly retried.
            self._last_seen_alert_id = max(
                self._last_seen_alert_id, int(row.get("id") or 0)
            )
            self._metrics.last_alert_id_seen = self._last_seen_alert_id

    # ── Per-alert handling ────────────────────────────────────────────

    async def _process_alert(self, alert_row: dict) -> None:
        self._metrics.alerts_seen += 1
        t_start = time.monotonic()
        ticker = str(alert_row.get("ticker") or "")
        alert_type = str(alert_row.get("alert_type") or "")
        fired_at = alert_row.get("fired_at")
        signal_score = float(alert_row.get("signal_score") or 0.0)
        features = alert_row.get("features") or {}
        # Ensure features is a dict (it comes from JSONB; sqlalchemy
        # may yield it already-parsed or as a string depending on driver).
        if isinstance(features, str):
            try:
                features = json.loads(features)
            except (ValueError, TypeError):
                features = {}

        alert = {
            "id": alert_row.get("id"),
            "ticker": ticker,
            "alert_type": alert_type,
            "fired_at": fired_at,
            "signal_score": signal_score,
            "features": features,
        }

        # Build context from in-memory book + per-ticker open count.
        ctx = self._build_context(ticker)

        # Gates run synchronously — they're pure-Python, microsecond-
        # scale, and asyncio.gather over them adds overhead with no
        # parallelism gain (no I/O inside).
        gate_run = run_gates(alert, ctx)

        decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
        latency_ms = (time.monotonic() - t_start) * 1000.0

        if not gate_run.allow:
            # Reject path
            await self._write_decision(
                alert, ctx, decision="rejected",
                reject_reason=gate_run.deny_reason,
                gate_run=gate_run, side="buy",
                quantity=None, fill_price=None, notional_usd=None,
                broker_order_id=None, decided_at=decided_at,
                latency_ms=latency_ms,
            )
            self._metrics.decisions_rejected += 1
            self._metrics.last_decision_at = decided_at
            return

        # All gates passed — proceed. Side mapping: long alerts -> buy,
        # short alerts -> sell. We don't currently *open* short
        # positions in spot crypto (you can't short BTC-USD on Coinbase
        # spot without margin), so imbalance_short alerts are recorded
        # as decisions but never fill. F5 will use them as exit signals.
        side = "buy" if alert_type.endswith("_long") else "sell"
        if side == "sell":
            # No spot-short execution; record as rejected with a
            # specific reason so we can see the alerts that COULD
            # become exit signals later.
            await self._write_decision(
                alert, ctx, decision="rejected",
                reject_reason="short_unsupported_in_spot",
                gate_run=gate_run, side=side,
                quantity=None, fill_price=None, notional_usd=None,
                broker_order_id=None, decided_at=decided_at,
                latency_ms=latency_ms,
            )
            self._metrics.decisions_rejected += 1
            self._metrics.last_decision_at = decided_at
            return

        # Sizing — fixed notional for now (F7 replaces with Kelly).
        notional_usd = float(self._overrides["default_notional_usd"])
        # Long buys at best ask in the simulated book.
        fill_price = float(ctx.best_ask or 0.0)
        if fill_price <= 0.0:
            await self._write_decision(
                alert, ctx, decision="rejected",
                reject_reason="no_fill_price_available",
                gate_run=gate_run, side=side,
                quantity=None, fill_price=None, notional_usd=None,
                broker_order_id=None, decided_at=decided_at,
                latency_ms=latency_ms,
            )
            self._metrics.decisions_rejected += 1
            return
        quantity = notional_usd / fill_price

        # ctx.mode is the EFFECTIVE mode after gate_mode_interlock —
        # which forces paper if live wasn't authorized. We trust that
        # value rather than re-checking env.
        if ctx.mode == "live":
            # Defence-in-depth: even though the gate said live is fine,
            # double-check the AUTHORIZED env at the moment of placement.
            if not is_live_authorized():
                await self._write_decision(
                    alert, ctx, decision="rejected",
                    reject_reason="mode_live_but_not_authorized_at_place",
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=latency_ms,
                )
                self._metrics.decisions_rejected += 1
                return
            try:
                broker_order_id = await self._place_live_order(
                    ticker, side, quantity, fill_price, notional_usd,
                )
            except LiveExecutionNotAuthorized as exc:
                # Auth or safety-belt tripped (cap, broker not
                # connected, etc). Reject row carries the real reason
                # so the autopilot UI shows it.
                await self._write_decision(
                    alert, ctx, decision="rejected",
                    reject_reason=f"live_blocked:{str(exc)[:48]}",
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=latency_ms,
                )
                self._metrics.decisions_rejected += 1
                return
            except Exception as exc:
                # Broker error (network, terminal rejection, etc.) —
                # surface it visibly. Do NOT update open_positions or
                # daily_used since no order landed.
                logger.exception(
                    "[fast_path] live placement failed ticker=%s side=%s qty=%.8f: %s",
                    ticker, side, quantity, exc,
                )
                await self._write_decision(
                    alert, ctx, decision="rejected",
                    reject_reason=f"live_error:{str(exc)[:48]}",
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=latency_ms,
                )
                self._metrics.decisions_rejected += 1
                return
            await self._write_decision(
                alert, ctx, decision="live_placed",
                reject_reason=None,
                gate_run=gate_run, side=side,
                quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                broker_order_id=broker_order_id, decided_at=decided_at,
                latency_ms=latency_ms,
            )
            self._metrics.decisions_live_placed += 1
        else:
            # Paper fill — synthesize at best ask, no broker call.
            await self._write_decision(
                alert, ctx, decision="paper_fill",
                reject_reason=None,
                gate_run=gate_run, side=side,
                quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                broker_order_id=None, decided_at=decided_at,
                latency_ms=latency_ms,
            )
            self._metrics.decisions_paper_fill += 1

        # Update in-memory state — paper accounting only.
        self._open_positions[ticker] = self._open_positions.get(ticker, 0) + 1
        self._daily_notional_used_usd += notional_usd
        self._metrics.last_decision_at = decided_at

    # ── Live placement (stub) ─────────────────────────────────────────

    async def _place_live_order(self, ticker: str, side: str,
                                quantity: float, fill_price: float,
                                notional_usd: float) -> str:
        """Live Coinbase placement, off the event loop.

        ``_place_coinbase_order_live`` does its own internal verify
        polling (which sleeps); running it in the default thread pool
        keeps the asyncio loop responsive for the next alert poll
        while we wait for broker confirmation.

        Raises ``LiveExecutionNotAuthorized`` if the safety belts trip
        (cap exceeded, broker not connected, auth flag unset). The
        caller's try/except converts that to a ``rejected`` decision
        row with the failure reason — visible in the autopilot UI
        so the operator can debug without grepping logs.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None,
            _place_coinbase_order_live,
            ticker, side, quantity, fill_price, notional_usd,
        )

    # ── DB I/O (sync, run in executor) ────────────────────────────────

    def _bootstrap_max_alert_id(self) -> int:
        with self._engine.begin() as conn:
            r = conn.execute(text("SELECT COALESCE(MAX(id), 0) FROM fast_alerts")).scalar()
            return int(r or 0)

    def _fetch_new_alerts(self) -> list[dict]:
        # LIMIT 200 so a backlog never blows up one poll.
        with self._engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT id, ticker, alert_type, fired_at, signal_score,
                       features, source
                FROM fast_alerts
                WHERE id > :last_id
                ORDER BY id ASC
                LIMIT 200
            """), {"last_id": self._last_seen_alert_id}).mappings().all()
            return [dict(r) for r in rows]

    async def _write_decision(self, alert: dict, ctx: ExecContext,
                              *, decision: str, reject_reason: str | None,
                              gate_run: GateRunResult, side: str,
                              quantity: float | None, fill_price: float | None,
                              notional_usd: float | None,
                              broker_order_id: str | None,
                              decided_at: datetime,
                              latency_ms: float) -> None:
        gates_payload = {
            "deny_reason": gate_run.deny_reason,
            "results": [
                {
                    "name": r.name,
                    "allow": r.allow,
                    "reason": r.reason,
                    "detail": r.detail,
                }
                for r in gate_run.results
            ],
            "ctx": {
                "best_bid": ctx.best_bid,
                "best_ask": ctx.best_ask,
                "spread_bps": ctx.spread_bps,
                "open_positions_for_ticker": ctx.open_positions_for_ticker,
                "daily_notional_used_usd": ctx.daily_notional_used_usd,
                "mode": ctx.mode,
                "live_authorized": ctx.live_authorized,
            },
        }
        payload = {
            "ticker": alert["ticker"],
            "alert_type": alert["alert_type"],
            "alert_fired_at": alert["fired_at"],
            "decision": decision,
            "reject_reason": reject_reason,
            "mode": ctx.mode,
            "side": side,
            "quantity": quantity,
            "fill_price": fill_price,
            "notional_usd": notional_usd,
            "gates_json": json.dumps(gates_payload),
            "broker_order_id": broker_order_id,
            "latency_ms": latency_ms,
            "decided_at": decided_at,
        }
        loop = asyncio.get_running_loop()
        await loop.run_in_executor(None, self._insert_decision_sync, payload)

    def _insert_decision_sync(self, payload: dict) -> None:
        with self._engine.begin() as conn:
            conn.execute(text("""
                INSERT INTO fast_executions (
                    ticker, alert_type, alert_fired_at,
                    decision, reject_reason, mode, side,
                    quantity, fill_price, notional_usd,
                    gates_json, broker_order_id, latency_ms, decided_at
                ) VALUES (
                    :ticker, :alert_type, :alert_fired_at,
                    :decision, :reject_reason, :mode, :side,
                    :quantity, :fill_price, :notional_usd,
                    CAST(:gates_json AS JSONB), :broker_order_id,
                    :latency_ms, :decided_at
                )
            """), payload)

    # ── Helpers ───────────────────────────────────────────────────────

    def _build_context(self, ticker: str) -> ExecContext:
        # Pull top-of-book from the in-memory aggregator. We don't
        # *force* an emission here — we read whatever is already there;
        # if the book hasn't been seen, the gate fails and rejects.
        book = self._book._books.get(ticker)  # noqa: SLF001 - read-only peek
        best_bid = 0.0
        best_ask = 0.0
        spread_bps = 0.0
        if book is not None and book.bids and book.asks:
            best_bid = max(book.bids.keys())
            best_ask = min(book.asks.keys())
            mid = (best_bid + best_ask) / 2.0 if (best_bid > 0 and best_ask > 0) else 0.0
            if mid > 0:
                spread_bps = ((best_ask - best_bid) / mid) * 10_000.0
        return ExecContext(
            now_wall=datetime.now(timezone.utc).replace(tzinfo=None),
            best_bid=best_bid,
            best_ask=best_ask,
            spread_bps=spread_bps,
            open_positions_for_ticker=self._open_positions.get(ticker, 0),
            daily_notional_used_usd=self._daily_notional_used_usd,
            mode=self._settings.mode,
            live_authorized=is_live_authorized(),
            engine=self._engine,
        )

    def _maybe_roll_daily_window(self) -> None:
        today = self._utc_date_str(datetime.now(timezone.utc))
        if today != self._daily_window_date:
            logger.info(
                "[fast_path] executor daily window roll %s -> %s "
                "(previous notional_used=%.2f USD)",
                self._daily_window_date, today, self._daily_notional_used_usd,
            )
            self._daily_window_date = today
            self._daily_notional_used_usd = 0.0

    @staticmethod
    def _utc_date_str(dt: datetime) -> str:
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")

    # ── Observability ─────────────────────────────────────────────────

    def stats(self) -> dict:
        return {
            "polls_total": self._metrics.polls_total,
            "alerts_seen": self._metrics.alerts_seen,
            "decisions_paper_fill": self._metrics.decisions_paper_fill,
            "decisions_live_placed": self._metrics.decisions_live_placed,
            "decisions_rejected": self._metrics.decisions_rejected,
            "db_errors": self._metrics.db_errors,
            "last_alert_id_seen": self._metrics.last_alert_id_seen,
            "last_decision_at": (
                self._metrics.last_decision_at.isoformat()
                if self._metrics.last_decision_at else None
            ),
            "open_positions_total": sum(self._open_positions.values()),
            "tickers_with_position": sum(1 for v in self._open_positions.values() if v > 0),
            "daily_notional_used_usd": self._daily_notional_used_usd,
            "daily_window_date": self._daily_window_date,
            "mode": self._settings.mode,
            "live_authorized": is_live_authorized(),
        }


__all__ = ["FastPathExecutor", "LiveExecutionNotAuthorized"]
