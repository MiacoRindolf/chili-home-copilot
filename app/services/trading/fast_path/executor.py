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

from .alert_direction import SIDE_SELL, spot_entry_side_for_alert_type
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
from .universe_status import UNIVERSE_STATUS_ACTIVE, UNIVERSE_STATUS_SHADOW

logger = logging.getLogger(__name__)


POLL_INTERVAL_S = 1.0
"""How often the executor polls fast_alerts. 1s gives sub-2s typical
end-to-end latency (ws -> scanner -> alert row -> poll -> decision)
without hammering the DB."""

_SHADOW_MAKER_PROBE_BYPASS_GATES = frozenset({
    "negative_edge",
    "maker_attempt_adverse",
    "cost_aware_admission",
})
"""Learned denials that shadow maker probes are allowed to re-measure."""

_SHADOW_MAKER_PROBE_CAPACITY_GATES = frozenset({
    "capacity",
})
"""Paper-only shadow denials that may be re-measured as observe-only probes."""

_SHADOW_MAKER_PROBE_TERMINAL_VERDICTS = frozenset({
    "negative_edge",
    "below_cost",
    "adverse_selection",
})
"""Gate verdicts that are already conclusive enough to stop re-probing."""


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

    order_fn = getattr(cb, f"place_{side}_order")
    resp = order_fn(ticker, quantity, order_type="market")

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


# ── Maker-only execution helpers (f-fastpath-maker-only-executor) ────


MAKER_LIMIT_TICK_FRACTION_OF_MID = 1e-4
"""Default tick offset, expressed as a fraction of mid-price, when the
venue's quote_increment isn't available. 1bp = 0.01% — small enough
that the resting limit is consistently inside the spread for any
reasonable Coinbase pair (median spread is well above 1bp), but not so
small that it lands at the same price as a competing best bid/ask.

Settings-tunable via ``CHILI_FAST_PATH_MAKER_TICK_FRACTION_OF_MID`` if a
follow-up brief lifts this into ``settings.py``. For now the constant
encodes the default; an operator override should NOT be needed since
the gate already filters wide-spread pairs."""

MAKER_TIMEOUT_TASK_NAME_PREFIX = "fast_path_maker_timeout"
BPS_PER_UNIT = 10_000.0


def _maker_default_tick_size(mid_price: float) -> float:
    """Fallback tick offset in absolute price units when the venue
    quote_increment isn't available. Bounded below by $1e-8 so we
    don't generate sub-Coinbase-tick prices for high-priced assets like
    BTC where ``mid * 1e-4`` is still a valid sub-tick.
    """
    if mid_price <= 0:
        return 0.0
    return max(mid_price * MAKER_LIMIT_TICK_FRACTION_OF_MID, 1e-8)


def _compute_maker_limit_price(side: str, best_bid: float, best_ask: float,
                               tick_size: float) -> float:
    """Place a passive limit on our side of the book.

    Improve by one tick when that stays inside the spread. If the book
    is already one tick wide, join the current best bid/ask instead of
    rejecting; the order is still passive/post-only, and the market can
    produce maker-fill evidence.
    """
    if best_bid <= 0 or best_ask <= 0 or best_bid >= best_ask:
        return 0.0
    if side == "buy":
        candidate = best_bid + tick_size if tick_size > 0 else 0.0
        return candidate if best_bid < candidate < best_ask else best_bid
    elif side == "sell":
        candidate = best_ask - tick_size if tick_size > 0 else 0.0
        return candidate if best_bid < candidate < best_ask else best_ask
    return 0.0


def _place_coinbase_maker_order_live(ticker: str, side: str, quantity: float,
                                     limit_price: float, notional_usd: float) -> str:
    """Place a Coinbase POST_ONLY limit order and return the broker
    order_id. Mirror of ``_place_coinbase_order_live`` but for the
    maker path: same authorization belts, same first-trade hard cap,
    same SDK-level connection check.

    The Coinbase Advanced Trade SDK's POST_ONLY variant rejects any
    limit that would cross the book at place-time, so a misconfigured
    aggressive price can't accidentally take liquidity.
    """
    if not is_live_authorized():
        raise LiveExecutionNotAuthorized(
            "live placement attempted but CHILI_FAST_PATH_EXEC_LIVE_AUTHORIZED is unset"
        )

    if notional_usd > LIVE_FIRST_TRADE_USD_HARD_CAP and not _live_notional_override():
        raise LiveExecutionNotAuthorized(
            f"notional ${notional_usd:.2f} exceeds first-trade cap "
            f"${LIVE_FIRST_TRADE_USD_HARD_CAP:.2f}; set "
            f"CHILI_FAST_PATH_LIVE_NOTIONAL_OK=1 after validating the "
            f"first small live orders behave correctly"
        )

    if side not in ("buy", "sell"):
        raise LiveExecutionNotAuthorized(f"unsupported side={side!r} for live placement")
    if limit_price <= 0:
        raise LiveExecutionNotAuthorized(f"non-positive limit_price={limit_price!r}")

    try:
        from app.services import coinbase_service as cb
    except Exception as exc:
        raise LiveExecutionNotAuthorized(f"coinbase_service import failed: {exc}") from exc

    if not cb.is_connected():
        try:
            cb.connect()
        except Exception:
            pass
    if not cb.is_connected():
        raise LiveExecutionNotAuthorized(
            "coinbase client not connected; configure credentials via vault or env"
        )

    logger.critical(
        "[fast_path] LIVE MAKER PLACEMENT %s %s qty=%.8f limit_px=%.6f notional_usd=%.2f",
        side.upper(), ticker, quantity, limit_price, notional_usd,
    )

    order_fn = getattr(cb, f"place_{side}_order")
    resp = order_fn(
        ticker, quantity, order_type="limit",
        limit_price=limit_price, post_only=True,
    )

    if not isinstance(resp, dict) or not resp.get("ok"):
        msg = (resp or {}).get("error", "unknown_broker_error")
        raise RuntimeError(f"coinbase rejected maker order: {msg}")

    order_id = str(resp.get("order_id") or "").strip()
    if not order_id:
        raise RuntimeError("coinbase response missing order_id")
    return order_id


def _cancel_coinbase_order_live(order_id: str) -> bool:
    """Cancel a Coinbase order. Returns True on accepted cancel, False
    otherwise. Errors are swallowed because the maker-timeout handler
    treats failed cancels as 'cancellation in flight; let reconcile
    sweep' rather than a fatal condition.
    """
    try:
        from app.services import coinbase_service as cb
    except Exception:
        return False
    try:
        resp = cb.cancel_order_by_id(order_id)
        return bool(resp.get("ok"))
    except Exception as exc:
        logger.warning("[fast_path] cancel_order_by_id %s failed: %s", order_id, exc)
        return False


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
    # f-fastpath-maker-only-executor (2026-05-08).
    maker_attempts_placed: int = 0
    maker_attempts_filled: int = 0
    maker_attempts_cancelled: int = 0
    maker_attempts_replaced: int = 0
    maker_attempts_rejected: int = 0
    maker_attempts_capped: int = 0  # blocked by 1-outstanding-per-(ticker,side) cap
    maker_attempts_adverse_cancelled: int = 0
    maker_observe_only_fills: int = 0


class FastPathExecutor:
    def __init__(
        self,
        settings: FastPathSettings,
        engine: Engine,
        order_book: OrderBookAggregator,
        decay_miner: Any | None = None,
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
        # f-fastpath-maker-only-executor (2026-05-08).
        # ``decay_miner`` is the FastPathDecayMiner instance (or None
        # in tests / when ingestion is disabled). The executor calls
        # its ``record_maker_outcome`` when a maker order's outcome is
        # known so the maker-filled decay table accumulates properly.
        self._decay_miner = decay_miner
        # Hard cap: 1 outstanding maker order per (ticker, side).
        # Keyed by (ticker, side); value is a dict carrying attempt_id,
        # broker_order_id, timeout_task, alert metadata. The
        # cancel-on-timeout handler removes its own entry when done.
        self._outstanding_maker: dict[tuple[str, str], dict] = {}

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
        try:
            await loop.run_in_executor(None, self._refresh_open_positions_sync)
        except Exception as exc:
            logger.warning(
                "[fast_path] executor open-position bootstrap failed: %s",
                exc, exc_info=True,
            )
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
        try:
            await loop.run_in_executor(None, self._refresh_open_positions_sync)
        except Exception as exc:
            self._metrics.db_errors += 1
            logger.warning(
                "[fast_path] executor open-position refresh failed: %s",
                exc, exc_info=True,
            )
        await self._sweep_paper_maker_fills()
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

        shadow_maker_probe = False
        if not gate_run.allow:
            shadow_maker_probe = await self._shadow_maker_probe_allowed_async(
                alert, ctx, gate_run,
            )
        shadow_maker_probe_observe_only = (
            shadow_maker_probe
            and self._shadow_maker_probe_observe_only(gate_run)
        )
        if not gate_run.allow and not shadow_maker_probe:
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
        # short alerts -> sell. Neutral signals are long-entry
        # candidates in today's scanner; do not turn them into shorts
        # merely because their names lack a "_long" suffix.
        side = spot_entry_side_for_alert_type(alert_type)
        if side == SIDE_SELL:
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

        # f-fastpath-maker-only-executor (2026-05-08): dispatch on
        # the effective execution mode. ``taker`` (default) is
        # bit-identical to the original taker path; the maker variants
        # delegate to the maker-only / maker-first methods.
        execution_mode = (self._settings.execution_mode or "taker").strip().lower()
        if execution_mode in ("maker_only", "maker_first_then_taker"):
            await self._process_alert_maker(
                alert=alert, ctx=ctx, gate_run=gate_run, side=side,
                quantity=quantity, fill_price=fill_price,
                notional_usd=notional_usd, decided_at=decided_at,
                latency_ms=latency_ms, execution_mode=execution_mode,
                observe_only=shadow_maker_probe_observe_only,
            )
            return

        # ── Taker path (unchanged) ────────────────────────────────────────
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
            paper_allowed, universe_status = (
                await self._paper_position_allowed_for_universe_async(ticker)
            )
            if not paper_allowed:
                await self._write_decision(
                    alert, ctx, decision="rejected",
                    reject_reason=self._universe_observe_only_reason(universe_status),
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=latency_ms,
                )
                self._metrics.decisions_rejected += 1
                self._metrics.last_decision_at = decided_at
                return
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

    # ── Maker-only path (f-fastpath-maker-only-executor, 2026-05-08) ──

    async def _shadow_maker_probe_allowed_async(
        self,
        alert: dict,
        ctx: ExecContext,
        gate_run: GateRunResult,
    ) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._shadow_maker_probe_allowed, alert, ctx, gate_run,
        )

    def _shadow_maker_probe_allowed(
        self,
        alert: dict,
        ctx: ExecContext,
        gate_run: GateRunResult,
    ) -> bool:
        """Allow paper shadow symbols to refresh maker-fill evidence.

        This is intentionally narrow: it only applies to latest-rotation
        shadow symbols in paper maker modes, and only when every denial is
        a learned maker/economic gate. Operational denials still reject.
        """
        if (ctx.mode or "paper").strip().lower() != "paper":
            return False
        execution_mode = (self._settings.execution_mode or "taker").strip().lower()
        if execution_mode not in ("maker_only", "maker_first_then_taker"):
            return False
        if not getattr(self._settings, "universe_rotation_enabled", False):
            return False
        denied = {r.name for r in gate_run.results if not r.allow}
        if not denied:
            return False
        capacity_probe_enabled = bool(getattr(
            self._settings,
            "universe_shadow_capacity_probe_enabled",
            False,
        ))
        bypass_gates = set(_SHADOW_MAKER_PROBE_BYPASS_GATES)
        if capacity_probe_enabled:
            bypass_gates.update(_SHADOW_MAKER_PROBE_CAPACITY_GATES)
        if not denied.issubset(bypass_gates):
            return False
        if any(
            r.name == "capacity"
            and not r.allow
            and str(r.reason or "") != "pair_already_held"
            for r in gate_run.results
        ):
            return False
        if (
            not getattr(
                self._settings,
                "universe_shadow_terminal_reprobe_enabled",
                False,
            )
            and not self._shadow_maker_probe_denials_refreshable(gate_run)
        ):
            return False
        ticker = str(alert.get("ticker") or "")
        if not ticker:
            return False
        try:
            status = self._latest_universe_status_sync(ticker)
        except Exception as exc:
            self._metrics.db_errors += 1
            logger.debug(
                "[fast_path] shadow maker probe status lookup failed "
                "ticker=%s: %s",
                ticker, exc, exc_info=True,
            )
            return False
        return status == UNIVERSE_STATUS_SHADOW

    @staticmethod
    def _shadow_maker_probe_denials_refreshable(gate_run: GateRunResult) -> bool:
        """Return False when learned gates already reached terminal evidence."""
        for result in gate_run.results:
            if result.allow:
                continue
            detail = result.detail if isinstance(result.detail, dict) else {}
            verdict = str(detail.get("verdict") or "").strip().lower()
            if verdict in _SHADOW_MAKER_PROBE_TERMINAL_VERDICTS:
                return False
        return True

    @staticmethod
    def _shadow_maker_probe_observe_only(gate_run: GateRunResult) -> bool:
        return any(
            result.name in _SHADOW_MAKER_PROBE_CAPACITY_GATES
            and not result.allow
            for result in gate_run.results
        )

    async def _process_alert_maker(
        self,
        *,
        alert: dict,
        ctx: ExecContext,
        gate_run: GateRunResult,
        side: str,
        quantity: float,
        fill_price: float,
        notional_usd: float,
        decided_at: datetime,
        latency_ms: float,
        execution_mode: str,
        observe_only: bool = False,
    ) -> None:
        """Maker-only / maker-first-then-taker placement entry point.

        Steps:
          1. 1-outstanding-per-(ticker,side) cap enforcement.
          2. Compute limit price one tick inside the spread.
          3. Live mode: place via ``_place_coinbase_maker_order_live``
             behind the same authorization belts as the taker path.
             Paper mode: synthesize a placement (no broker call).
          4. INSERT a ``fast_path_maker_attempts`` row.
          5. Schedule a background asyncio task that fires after
             ``settings.maker_cancel_on_timeout_s`` (or
             ``maker_first_taker_fallback_s`` in hybrid mode) and
             resolves the attempt: fill / cancel / replaced.

        ``execution_mode`` controls the timeout duration AND the
        outcome label when the timeout cancels: ``maker_only`` →
        ``cancelled``; ``maker_first_then_taker`` → ``replaced`` plus a
        sibling taker fallback placement.
        """
        ticker = str(alert.get("ticker") or "")
        # 1-outstanding cap: prevent stale-limit pile-up if signals
        # fire faster than the cancel-on-timeout window.
        cap_key = (ticker, side)
        if cap_key in self._outstanding_maker:
            self._metrics.maker_attempts_capped += 1
            await self._write_decision(
                alert, ctx, decision="rejected",
                reject_reason="maker_outstanding_cap_per_ticker_side",
                gate_run=gate_run, side=side,
                quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                broker_order_id=None, decided_at=decided_at,
                latency_ms=latency_ms,
            )
            self._metrics.decisions_rejected += 1
            return

        tick_size = _maker_default_tick_size(
            (ctx.best_bid + ctx.best_ask) / 2.0
            if (ctx.best_bid > 0 and ctx.best_ask > 0)
            else fill_price,
        )
        limit_price = _compute_maker_limit_price(
            side, ctx.best_bid, ctx.best_ask, tick_size,
        )
        if limit_price <= 0:
            await self._write_decision(
                alert, ctx, decision="rejected",
                reject_reason="maker_limit_price_unavailable",
                gate_run=gate_run, side=side,
                quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                broker_order_id=None, decided_at=decided_at,
                latency_ms=latency_ms,
            )
            self._metrics.decisions_rejected += 1
            return

        spread_at_placement_bps = ctx.spread_bps

        # Place — paper synthesises; live calls SDK.
        broker_order_id: str | None = None
        attempt_decision = "paper_fill"
        if ctx.mode == "live":
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
                loop = asyncio.get_running_loop()
                broker_order_id = await loop.run_in_executor(
                    None,
                    _place_coinbase_maker_order_live,
                    ticker, side, quantity, limit_price, notional_usd,
                )
                attempt_decision = "live_placed"
            except LiveExecutionNotAuthorized as exc:
                await self._write_decision(
                    alert, ctx, decision="rejected",
                    reject_reason=f"maker_live_blocked:{str(exc)[:40]}",
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=latency_ms,
                )
                self._metrics.decisions_rejected += 1
                self._metrics.maker_attempts_rejected += 1
                return
            except Exception as exc:
                logger.exception(
                    "[fast_path] maker live placement failed ticker=%s side=%s qty=%.8f: %s",
                    ticker, side, quantity, exc,
                )
                await self._write_decision(
                    alert, ctx, decision="rejected",
                    reject_reason=f"maker_live_error:{str(exc)[:40]}",
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=latency_ms,
                )
                self._metrics.decisions_rejected += 1
                self._metrics.maker_attempts_rejected += 1
                return

        # Audit row in fast_path_maker_attempts.
        loop = asyncio.get_running_loop()
        attempt_id = await loop.run_in_executor(
            None, self._insert_maker_attempt_sync,
            {
                "alert_id": alert.get("id"),
                "ticker": ticker,
                "side": side,
                "limit_price": float(limit_price),
                "spread_at_placement_bps": float(spread_at_placement_bps),
                "broker_order_id": broker_order_id,
                "execution_mode": execution_mode,
            },
        )
        self._metrics.maker_attempts_placed += 1

        if ctx.mode == "live":
            await self._write_decision(
                alert, ctx, decision=attempt_decision,
                reject_reason=None,
                gate_run=gate_run, side=side,
                quantity=quantity, fill_price=limit_price, notional_usd=notional_usd,
                broker_order_id=broker_order_id, decided_at=decided_at,
                latency_ms=latency_ms,
            )
            self._metrics.decisions_live_placed += 1

        # Schedule the timeout-driven outcome resolver. Hybrid mode
        # uses the shorter ``maker_first_taker_fallback_s`` and labels
        # the unfilled outcome ``replaced`` (since we'll then place a
        # taker). Pure maker_only uses ``maker_cancel_on_timeout_s`` and
        # labels ``cancelled``.
        if execution_mode == "maker_first_then_taker":
            timeout_s = max(int(self._settings.maker_first_taker_fallback_s), 1)
            unfilled_outcome = "replaced"
        else:
            timeout_s = max(int(self._settings.maker_cancel_on_timeout_s), 1)
            unfilled_outcome = "cancelled"

        attempt_record = {
            "attempt_id": attempt_id,
            "alert_id": int(alert.get("id") or 0),
            "ticker": ticker,
            "side": side,
            "limit_price": float(limit_price),
            "broker_order_id": broker_order_id,
            "execution_mode": execution_mode,
            "alert_type": str(alert.get("alert_type") or ""),
            "signal_score": float(alert.get("signal_score") or 0.0),
            "fired_at": alert.get("fired_at"),
            "placed_at": time.monotonic(),
            "quantity": float(quantity),
            "notional_usd": float(notional_usd),
            "observe_only": bool(observe_only),
            "_placement_ctx": ctx,
            "_alert": alert,
            "_gate_run": gate_run,
            "_unfilled_outcome": unfilled_outcome,
        }
        self._outstanding_maker[cap_key] = attempt_record

        timeout_task = asyncio.create_task(
            self._maker_timeout_handler(
                cap_key=cap_key,
                attempt=attempt_record,
                timeout_s=timeout_s,
                unfilled_outcome=unfilled_outcome,
                ctx=ctx,
                alert=alert,
                gate_run=gate_run,
            ),
            name=f"{MAKER_TIMEOUT_TASK_NAME_PREFIX}_{ticker}_{side}",
        )
        attempt_record["timeout_task"] = timeout_task

        self._metrics.last_decision_at = decided_at

    async def _maker_timeout_handler(
        self,
        *,
        cap_key: tuple[str, str],
        attempt: dict,
        timeout_s: int,
        unfilled_outcome: str,
        ctx: ExecContext,
        alert: dict,
        gate_run: GateRunResult,
        force_outcome: str | None = None,
    ) -> None:
        """Run after ``timeout_s`` seconds; resolve the maker attempt.

        Resolution logic:

          * Live mode: poll the broker for terminal state. If filled,
            mark filled. Else cancel via SDK and mark
            ``unfilled_outcome``.
          * Paper mode: peek at the in-memory book. If the BBO has
            traded through the passive limit on our side of the book,
            call it filled at the limit price. Else mark unfilled.
            The sweep path may force a cancellation before timeout
            when adverse mid drift reaches our passive quote first.

        On filled / partial: notify the decay_miner so the maker-filled
        forward-return obs accumulate.
        For ``unfilled_outcome='replaced'`` (hybrid mode), the caller's
        responsibility for the taker fallback would be a follow-up; for
        this brief we record the replaced outcome and let the next
        signal in.
        """
        try:
            await asyncio.sleep(int(timeout_s))
        except asyncio.CancelledError:
            return
        if attempt.get("resolved"):
            return
        if attempt.get("resolving") and force_outcome is None:
            return
        attempt["resolving"] = True

        ticker = attempt["ticker"]
        side = attempt["side"]
        limit_price = float(attempt["limit_price"])
        broker_order_id = attempt.get("broker_order_id")
        attempt_id = attempt.get("attempt_id")

        # Determine the realized outcome.
        outcome = unfilled_outcome
        final_price: float | None = None
        time_to_fill_ms: int | None = None

        # Re-read top-of-book from the same in-memory aggregator the
        # gate read at placement.
        peek_ctx = self._build_context(ticker)
        spread_at_fill_bps = peek_ctx.spread_bps
        mid_drift_bps: float | None = None
        if peek_ctx.best_bid > 0 and peek_ctx.best_ask > 0:
            mid_now = (peek_ctx.best_bid + peek_ctx.best_ask) / 2.0
            mid_at_place = (
                (ctx.best_bid + ctx.best_ask) / 2.0
                if (ctx.best_bid > 0 and ctx.best_ask > 0) else 0.0
            )
            if mid_at_place > 0:
                mid_drift_bps = (
                    (mid_now - mid_at_place) / mid_at_place
                ) * BPS_PER_UNIT

        if force_outcome in ("filled", "partial"):
            outcome = force_outcome
            final_price = limit_price
        elif force_outcome in ("cancelled", "replaced"):
            outcome = force_outcome
            final_price = None
            if ctx.mode == "live" and broker_order_id:
                _cancel_coinbase_order_live(broker_order_id)
        elif ctx.mode == "live" and broker_order_id:
            # Poll broker for terminal state.
            try:
                from app.services import coinbase_service as cb
                info = cb.get_order_by_id(broker_order_id) or {}
            except Exception:
                info = {}
            state = str(
                info.get("status") or info.get("order_status") or ""
            ).lower()
            if state == "filled":
                outcome = "filled"
                # Coinbase reports avg_filled_price / filled_value; use
                # whichever is present.
                try:
                    final_price = float(
                        info.get("average_filled_price")
                        or info.get("filled_avg_price")
                        or limit_price
                    )
                except (TypeError, ValueError):
                    final_price = limit_price
            elif state in ("partially_filled", "partial"):
                outcome = "partial"
                try:
                    final_price = float(
                        info.get("average_filled_price")
                        or info.get("filled_avg_price")
                        or limit_price
                    )
                except (TypeError, ValueError):
                    final_price = limit_price
            else:
                # Cancel the resting order; record the unfilled outcome.
                _cancel_coinbase_order_live(broker_order_id)
                outcome = unfilled_outcome
                final_price = None
        else:
            # Paper: book-cross simulation.
            if side == "buy":
                # A buy maker fills if the book traded down through our
                # limit, i.e. the prevailing best_bid is at or below our
                # limit price (someone matched us). Equivalent
                # observable: an aggressive seller hit the bid-side at
                # our limit.
                book_crossed = self._paper_maker_book_crossed(
                    side, limit_price, peek_ctx,
                )
            else:  # sell
                book_crossed = self._paper_maker_book_crossed(
                    side, limit_price, peek_ctx,
                )
            if book_crossed:
                outcome = "filled"
                final_price = limit_price
            # else: outcome stays as unfilled_outcome ('cancelled' or
            # 'replaced').

        if outcome in ("filled", "partial"):
            time_to_fill_ms = int(
                (time.monotonic() - float(attempt["placed_at"])) * 1000.0
            )

        # Update fast_path_maker_attempts row with the outcome.
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, self._update_maker_attempt_sync,
                {
                    "id": attempt_id,
                    "fill_outcome": outcome,
                    "final_price": final_price,
                    "time_to_fill_ms": time_to_fill_ms,
                    "spread_at_fill_bps": float(spread_at_fill_bps)
                        if spread_at_fill_bps else None,
                    "mid_drift_bps": (
                        float(mid_drift_bps) if mid_drift_bps is not None else None
                    ),
                },
            )
        except Exception as exc:
            self._metrics.db_errors += 1
            logger.warning(
                "[fast_path] maker attempt update failed id=%s: %s",
                attempt_id, exc, exc_info=True,
            )

        # Bump per-outcome counters.
        if outcome == "filled":
            self._metrics.maker_attempts_filled += 1
        elif outcome == "partial":
            self._metrics.maker_attempts_filled += 1
        elif outcome == "replaced":
            self._metrics.maker_attempts_replaced += 1
        elif outcome == "cancelled":
            self._metrics.maker_attempts_cancelled += 1
            if attempt.get("_adverse_cancel"):
                self._metrics.maker_attempts_adverse_cancelled += 1

        # Notify decay_miner so the maker-filled forward-return obs
        # accumulate. For unfilled outcomes the call is a no-op.
        if self._decay_miner is not None and outcome in ("filled", "partial"):
            try:
                fired_at = attempt.get("fired_at")
                if fired_at is None:
                    fired_at = datetime.now(timezone.utc).replace(tzinfo=None)
                self._decay_miner.record_maker_outcome(
                    alert_id=int(attempt.get("alert_id") or 0),
                    ticker=ticker,
                    alert_type=str(attempt.get("alert_type") or ""),
                    signal_score=float(attempt.get("signal_score") or 0.0),
                    fired_at=fired_at,
                    fill_outcome=outcome,
                    entry_at_alert=float(final_price or limit_price),
                )
            except Exception as exc:
                logger.warning(
                    "[fast_path] decay_miner.record_maker_outcome failed: %s",
                    exc, exc_info=True,
                )

        if ctx.mode != "live" and outcome in ("filled", "partial"):
            decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
            fill_price = float(final_price or limit_price)
            filled_notional = float(attempt.get("notional_usd") or 0.0)
            if bool(attempt.get("observe_only")):
                paper_allowed, universe_status = False, "shadow_capacity_probe"
            else:
                paper_allowed, universe_status = (
                    await self._paper_position_allowed_for_universe_async(ticker)
                )
            if not paper_allowed:
                self._metrics.maker_observe_only_fills += 1
                logger.info(
                    "[fast_path] maker fill kept observe-only "
                    "ticker=%s side=%s status=%s fill_price=%.8f notional=%.2f",
                    ticker, side, universe_status or "missing",
                    fill_price, filled_notional,
                )
            else:
                fill_ctx = self._build_context(ticker)
                fill_ctx.mode = "paper"
                fill_ctx.live_authorized = False
                await self._write_decision(
                    alert, fill_ctx, decision="paper_fill",
                    reject_reason=None,
                    gate_run=gate_run, side=side,
                    quantity=float(attempt.get("quantity") or 0.0),
                    fill_price=fill_price, notional_usd=filled_notional,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=0.0,
                )
                self._metrics.decisions_paper_fill += 1
                self._open_positions[ticker] = self._open_positions.get(ticker, 0) + 1
                self._daily_notional_used_usd += filled_notional
            self._metrics.last_decision_at = decided_at

        # f-fastpath-maker-only-executor (2026-05-08): hybrid taker
        # fallback. When the maker leg is being replaced (no fill
        # within `maker_first_taker_fallback_s`), place a sibling
        # taker. We do this BEFORE dropping the (ticker, side) entry
        # from the outstanding cap so the cap doesn't permit a fresh
        # maker to race the fallback.
        if (
            attempt.get("execution_mode") == "maker_first_then_taker"
            and outcome == "replaced"
        ):
            try:
                await self._taker_fallback_after_maker_replaced(
                    attempt=attempt, ctx=ctx, alert=alert, gate_run=gate_run,
                )
            except Exception as exc:
                logger.warning(
                    "[fast_path] taker fallback after maker_replaced failed "
                    "ticker=%s side=%s: %s",
                    ticker, side, exc, exc_info=True,
                )

        # Drop our entry from the outstanding cap.
        attempt["resolved"] = True
        self._outstanding_maker.pop(cap_key, None)

    async def _sweep_paper_maker_fills(self) -> None:
        """Resolve paper maker orders when the current book invalidates them.

        The timeout handler still owns cancel/replaced outcomes. This
        sweep accelerates paper fills and early adverse cancels so
        fill-time and mid-drift evidence are measured near the book
        event, not artificially at the cancel deadline.
        """
        if not self._outstanding_maker:
            return
        for cap_key, attempt in list(self._outstanding_maker.items()):
            if attempt.get("resolved") or attempt.get("resolving"):
                continue
            ctx = attempt.get("_placement_ctx")
            if ctx is None or getattr(ctx, "mode", "paper") == "live":
                continue
            ticker = str(attempt.get("ticker") or "")
            side = str(attempt.get("side") or "")
            limit_price = float(attempt.get("limit_price") or 0.0)
            peek_ctx = self._build_context(ticker)
            force_outcome: str | None = None
            if self._paper_maker_book_crossed(side, limit_price, peek_ctx):
                force_outcome = "filled"
            elif self._maker_adverse_drift_reached_quote(
                side,
                limit_price,
                ctx,
                peek_ctx,
            ):
                # If the mid has already moved all the way to our
                # passive quote and we still are not filled, the maker
                # premise has failed. Cancel rather than waiting to be
                # selected into the next adverse print.
                attempt["_adverse_cancel"] = True
                force_outcome = "cancelled"
            if force_outcome is not None:
                attempt["resolving"] = True
                task = attempt.get("timeout_task")
                if task is not None:
                    task.cancel()
                await self._maker_timeout_handler(
                    cap_key=cap_key,
                    attempt=attempt,
                    timeout_s=0,
                    unfilled_outcome=str(
                        attempt.get("_unfilled_outcome") or "cancelled"
                    ),
                    ctx=ctx,
                    alert=attempt.get("_alert") or {},
                    gate_run=attempt.get("_gate_run"),
                    force_outcome=force_outcome,
                )

    @staticmethod
    def _ctx_mid_price(ctx: ExecContext) -> float:
        if ctx.best_bid <= 0.0 or ctx.best_ask <= 0.0:
            return 0.0
        return (ctx.best_bid + ctx.best_ask) / 2.0

    @classmethod
    def _maker_quote_distance_bps(
        cls,
        side: str,
        limit_price: float,
        placement_ctx: ExecContext,
    ) -> float:
        mid_at_place = cls._ctx_mid_price(placement_ctx)
        if mid_at_place <= 0.0 or limit_price <= 0.0:
            return 0.0
        if side == "buy":
            distance = mid_at_place - limit_price
        elif side == "sell":
            distance = limit_price - mid_at_place
        else:
            return 0.0
        if distance <= 0.0:
            return 0.0
        return (distance / mid_at_place) * BPS_PER_UNIT

    @classmethod
    def _maker_side_adjusted_mid_drift_bps(
        cls,
        side: str,
        placement_ctx: ExecContext,
        peek_ctx: ExecContext,
    ) -> float | None:
        mid_at_place = cls._ctx_mid_price(placement_ctx)
        mid_now = cls._ctx_mid_price(peek_ctx)
        if mid_at_place <= 0.0 or mid_now <= 0.0:
            return None
        raw = ((mid_now - mid_at_place) / mid_at_place) * BPS_PER_UNIT
        if side == "sell":
            return -raw
        if side == "buy":
            return raw
        return None

    @classmethod
    def _maker_adverse_drift_reached_quote(
        cls,
        side: str,
        limit_price: float,
        placement_ctx: ExecContext,
        peek_ctx: ExecContext,
    ) -> bool:
        quote_distance_bps = cls._maker_quote_distance_bps(
            side,
            limit_price,
            placement_ctx,
        )
        if quote_distance_bps <= 0.0:
            return False
        side_drift_bps = cls._maker_side_adjusted_mid_drift_bps(
            side,
            placement_ctx,
            peek_ctx,
        )
        if side_drift_bps is None:
            return False
        return side_drift_bps <= -quote_distance_bps

    @staticmethod
    def _paper_maker_book_crossed(
        side: str,
        limit_price: float,
        ctx: ExecContext,
    ) -> bool:
        if limit_price <= 0:
            return False
        if side == "buy":
            return (
                ctx.best_bid > 0 and ctx.best_bid <= limit_price
                and ctx.best_ask > 0 and ctx.best_ask <= limit_price
            )
        if side == "sell":
            return (
                ctx.best_ask > 0 and ctx.best_ask >= limit_price
                and ctx.best_bid > 0 and ctx.best_bid >= limit_price
            )
        return False

    async def _taker_fallback_after_maker_replaced(
        self,
        *,
        attempt: dict,
        ctx: ExecContext,
        alert: dict,
        gate_run: GateRunResult,
    ) -> None:
        """Hybrid mode (`maker_first_then_taker`): the resting maker
        leg didn't fill within `maker_first_taker_fallback_s`, so we
        cross the spread with a taker. Mirrors the original taker
        path's fill-price + decision-row + state-bookkeeping logic
        but re-reads the in-memory book for a fresh fill price (the
        book may have moved during the maker wait).

        Live: same authorization belts and broker-error handling as
        ``_process_alert``'s taker branch.
        Paper: synthesises at the current best ask (long) / best bid
        (short).
        """
        ticker = attempt["ticker"]
        side = attempt["side"]
        quantity = float(attempt.get("quantity") or 0.0)
        notional_usd = float(attempt.get("notional_usd") or 0.0)
        if quantity <= 0 or notional_usd <= 0:
            return  # malformed; skip rather than emit a phantom row

        # Re-read top-of-book — book may have moved since placement.
        peek_ctx = self._build_context(ticker)
        if side == "buy":
            fill_price = peek_ctx.best_ask
        else:
            fill_price = peek_ctx.best_bid
        if fill_price <= 0:
            # Can't price; record a rejection row so the audit trail
            # shows the fallback was attempted and failed.
            decided_at = datetime.now(timezone.utc).replace(tzinfo=None)
            await self._write_decision(
                alert, peek_ctx, decision="rejected",
                reject_reason="maker_replaced_taker_no_fill_price",
                gate_run=gate_run, side=side,
                quantity=quantity, fill_price=None, notional_usd=notional_usd,
                broker_order_id=None, decided_at=decided_at,
                latency_ms=0.0,
            )
            self._metrics.decisions_rejected += 1
            return

        decided_at = datetime.now(timezone.utc).replace(tzinfo=None)

        if peek_ctx.mode == "live":
            if not is_live_authorized():
                await self._write_decision(
                    alert, peek_ctx, decision="rejected",
                    reject_reason="maker_replaced_taker_not_authorized",
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=0.0,
                )
                self._metrics.decisions_rejected += 1
                return
            try:
                broker_order_id = await self._place_live_order(
                    ticker, side, quantity, fill_price, notional_usd,
                )
            except LiveExecutionNotAuthorized as exc:
                await self._write_decision(
                    alert, peek_ctx, decision="rejected",
                    reject_reason=f"maker_replaced_taker_blocked:{str(exc)[:32]}",
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=0.0,
                )
                self._metrics.decisions_rejected += 1
                return
            except Exception as exc:
                logger.exception(
                    "[fast_path] taker fallback after maker_replaced live placement "
                    "failed ticker=%s side=%s qty=%.8f: %s",
                    ticker, side, quantity, exc,
                )
                await self._write_decision(
                    alert, peek_ctx, decision="rejected",
                    reject_reason=f"maker_replaced_taker_error:{str(exc)[:32]}",
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=0.0,
                )
                self._metrics.decisions_rejected += 1
                return
            await self._write_decision(
                alert, peek_ctx, decision="live_placed",
                reject_reason=None,
                gate_run=gate_run, side=side,
                quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                broker_order_id=broker_order_id, decided_at=decided_at,
                latency_ms=0.0,
            )
            self._metrics.decisions_live_placed += 1
        else:
            paper_allowed, universe_status = (
                await self._paper_position_allowed_for_universe_async(ticker)
            )
            if not paper_allowed:
                await self._write_decision(
                    alert, peek_ctx, decision="rejected",
                    reject_reason=self._universe_observe_only_reason(universe_status),
                    gate_run=gate_run, side=side,
                    quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                    broker_order_id=None, decided_at=decided_at,
                    latency_ms=0.0,
                )
                self._metrics.decisions_rejected += 1
                self._metrics.last_decision_at = decided_at
                return
            await self._write_decision(
                alert, peek_ctx, decision="paper_fill",
                reject_reason=None,
                gate_run=gate_run, side=side,
                quantity=quantity, fill_price=fill_price, notional_usd=notional_usd,
                broker_order_id=None, decided_at=decided_at,
                latency_ms=0.0,
            )
            self._metrics.decisions_paper_fill += 1

        # Update in-memory state — same accounting as the taker path
        # so daily caps and per-ticker open positions stay correct.
        self._open_positions[ticker] = self._open_positions.get(ticker, 0) + 1
        self._daily_notional_used_usd += notional_usd
        self._metrics.last_decision_at = decided_at

    def _insert_maker_attempt_sync(self, payload: dict) -> int:
        """INSERT a fast_path_maker_attempts row, return the new id."""
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                INSERT INTO fast_path_maker_attempts (
                    alert_id, ticker, side, limit_price,
                    spread_at_placement_bps, broker_order_id,
                    execution_mode
                ) VALUES (
                    :alert_id, :ticker, :side, :limit_price,
                    :spread_at_placement_bps, :broker_order_id,
                    :execution_mode
                )
                RETURNING id
            """), payload).mappings().first()
            return int(row["id"]) if row else 0

    def _update_maker_attempt_sync(self, payload: dict) -> None:
        """UPDATE a fast_path_maker_attempts row at outcome resolution.

        ``filled_at`` / ``cancelled_at`` are derived from
        ``fill_outcome`` so the schema's two timestamps stay distinct.
        """
        outcome = payload["fill_outcome"]
        with self._engine.begin() as conn:
            if outcome in ("filled", "partial"):
                conn.execute(text("""
                    UPDATE fast_path_maker_attempts SET
                        filled_at = NOW(),
                        final_price = :final_price,
                        fill_outcome = :fill_outcome,
                        time_to_fill_ms = :time_to_fill_ms,
                        spread_at_fill_bps = :spread_at_fill_bps,
                        mid_drift_bps = :mid_drift_bps
                    WHERE id = :id
                """), payload)
            else:
                conn.execute(text("""
                    UPDATE fast_path_maker_attempts SET
                        cancelled_at = NOW(),
                        final_price = :final_price,
                        fill_outcome = :fill_outcome,
                        time_to_fill_ms = :time_to_fill_ms,
                        spread_at_fill_bps = :spread_at_fill_bps,
                        mid_drift_bps = :mid_drift_bps
                    WHERE id = :id
                """), payload)

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

    def _refresh_open_positions_sync(self) -> None:
        """Refresh per-ticker capacity state from DB open entries."""
        with self._engine.begin() as conn:
            rows = conn.execute(text("""
                SELECT e.ticker, COUNT(*) AS open_n
                FROM fast_executions e
                LEFT JOIN fast_exits x
                  ON x.entry_execution_id = e.id
                WHERE e.decision IN ('paper_fill', 'live_placed')
                  AND x.id IS NULL
                GROUP BY e.ticker
            """)).mappings().all()
        self._open_positions = {
            str(r["ticker"]): int(r["open_n"] or 0)
            for r in rows
            if int(r["open_n"] or 0) > 0
        }

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

    async def _paper_position_allowed_for_universe_async(
        self,
        ticker: str,
    ) -> tuple[bool, str | None]:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(
            None, self._paper_position_allowed_for_universe, ticker,
        )

    def _paper_position_allowed_for_universe(
        self,
        ticker: str,
    ) -> tuple[bool, str | None]:
        """Return whether a paper fill may become a dashboard position.

        With universe rotation on, shadow symbols are observe-only by
        default: they can still emit alerts and maker-fill decay, but
        they should not inflate the paper-position book until promoted
        to active.
        """
        if not getattr(self._settings, "universe_rotation_enabled", False):
            return True, None
        if getattr(self._settings, "universe_shadow_paper_fills_enabled", False):
            return True, None
        try:
            status = self._latest_universe_status_sync(ticker)
        except Exception as exc:
            self._metrics.db_errors += 1
            logger.warning(
                "[fast_path] universe status lookup failed for paper fill "
                "ticker=%s: %s",
                ticker, exc, exc_info=True,
            )
            return False, None
        return status == UNIVERSE_STATUS_ACTIVE, status

    def _latest_universe_status_sync(self, ticker: str) -> str | None:
        with self._engine.begin() as conn:
            row = conn.execute(text("""
                WITH latest_rotation AS (
                    SELECT MAX(rotation_at) AS ts FROM fast_path_universe
                )
                SELECT status
                FROM fast_path_universe
                WHERE rotation_at = (SELECT ts FROM latest_rotation)
                  AND ticker = :ticker
                LIMIT 1
            """), {"ticker": ticker}).mappings().one_or_none()
        status = str((row or {}).get("status") or "")
        return status or None

    @staticmethod
    def _universe_observe_only_reason(status: str | None) -> str:
        return f"universe_observe_only:{status or 'missing'}"

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
                spread_bps = ((best_ask - best_bid) / mid) * BPS_PER_UNIT
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
            # f-fastpath-maker-only-executor (2026-05-08).
            "execution_mode": self._settings.execution_mode,
            "maker_attempts_placed": self._metrics.maker_attempts_placed,
            "maker_attempts_filled": self._metrics.maker_attempts_filled,
            "maker_attempts_cancelled": self._metrics.maker_attempts_cancelled,
            "maker_attempts_replaced": self._metrics.maker_attempts_replaced,
            "maker_attempts_rejected": self._metrics.maker_attempts_rejected,
            "maker_attempts_capped": self._metrics.maker_attempts_capped,
            "maker_attempts_adverse_cancelled":
                self._metrics.maker_attempts_adverse_cancelled,
            "maker_observe_only_fills": self._metrics.maker_observe_only_fills,
            "maker_outstanding_count": len(self._outstanding_maker),
        }


__all__ = ["FastPathExecutor", "LiveExecutionNotAuthorized"]
