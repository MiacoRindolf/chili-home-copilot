"""Crypto-aware exit monitor (Task HHH).

Closes open crypto Trade rows when the position's stop_loss / take_profit
levels are hit at the current market price. Built as a parallel of the
options exit monitor (Phase 5 PP) because the equity exit monitor
(``auto_trader_monitor.tick_auto_trader_monitor``) explicitly skips
``robinhood + ticker ends in -USD`` -- KK shipped the entry side but the
exit side was never wired.

Design choices, in line with the equity / options paths:

  - Submits the exit through the trade's broker venue. Robinhood crypto
    uses ``broker_service.place_crypto_sell_order``; Coinbase spot uses
    ``coinbase_service.place_sell_order``.
  - Reads thresholds directly from ``Trade.stop_loss`` /
    ``Trade.take_profit`` -- the per-trade levels, not a strategy
    parameter -- because those came from the alert / pattern that
    drove the entry. (The autotrader_options exit monitor uses
    StrategyParameter because its "premium-stop %" is a global
    knob, not per-trade.)
  - Idempotent: if the trade already has an open ``pending_exit_order_id``
    we don't re-submit. Subsequent passes pick up the order's terminal
    state via broker-sync.
  - Runs at the same cadence as the equity / options exit passes.

Flag-gated by ``chili_autotrader_crypto_exit_monitor_enabled`` (default
True). When False, no crypto Trade rows are touched.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import PatternMonitorDecision, Trade

logger = logging.getLogger(__name__)

# f-options-exit-monitor-pattern-exit-now-audit (2026-05-06):
# the freshness window + the monitor-decision helpers moved to the
# shared ``_exit_monitor_common`` module. Local re-exports preserved
# for backwards compatibility (any external caller that imported the
# private names keeps working). The previous
# ``_CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS`` is retired; the shared
# ``MONITOR_EXIT_NOW_MAX_AGE_HOURS`` is the single source of truth.
from .._exit_monitor_common import (
    MONITOR_EXIT_NOW_MAX_AGE_HOURS as _CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS,
    is_implausible_quote,
    latest_monitor_decisions_by_trade as _latest_monitor_decisions_by_trade,
    fresh_monitor_exit_meta as _fresh_monitor_exit_meta,
    should_consult_monitor_after_refusal,
)


def _is_crypto_ticker(ticker: str) -> bool:
    """RH crypto convention: 'BTC-USD', 'RAY-USD', etc."""
    return bool((ticker or "").upper().endswith("-USD"))


def _to_usd_product_id(ticker: str | None) -> str:
    """Normalize crypto symbols for cross-broker position matching."""
    t = (ticker or "").strip().upper()
    if t and not t.endswith("-USD"):
        t = f"{t}-USD"
    return t


def _current_crypto_price(ticker: str) -> Optional[float]:
    """Best-effort price fetch for a crypto ticker. Returns None on failure."""
    try:
        from ..market_data import fetch_quote
        q = fetch_quote(ticker)
        if q:
            p = q.get("price") or q.get("last_price")
            if p is not None:
                return float(p)
    except Exception as e:
        logger.debug("[crypto_exit] fetch_quote failed for %s: %s", ticker, e)
    # Fallback: try the broker's own quote endpoint.
    # Phase 3.2 (2026-05-01): broker SDK encapsulated in broker_service.
    try:
        from ... import broker_service
        q = broker_service.get_crypto_quote(ticker)
        if q and q.get("mark_price"):
            return float(q["mark_price"])
    except Exception as e:
        logger.debug("[crypto_exit] get_crypto_quote failed for %s: %s", ticker, e)
    return None


def _evaluate_exit_triggers(
    *,
    px: float,
    entry: float,
    stop: Optional[float],
    target: Optional[float],
    direction: str = "long",
) -> tuple[bool, str]:
    """Pure: should this trade exit at ``px``? Returns (should_exit, reason).

    Round-13 FIX (2026-04-30): added price-sanity guard. Trade 585
    (ARB-USD) was stopped at ``px=0.00075706 <= stop=0.110331`` -- the
    price reading was 145x lower than reality (data error from upstream
    quote provider), which triggered a "stop hit" on garbage data and
    sold the position at the real market price -$4.20.
    Refuse to trigger any exit when the observed price is implausibly
    far from entry (>10x or <1/10 of entry). Per the no-hardcoded-fallback
    rule: do NOT silently fall back to entry when px is bad -- return
    'no_trigger:bad_quote' so the next pass can retry with a fresh quote.
    """
    is_long = (direction or "long").lower() != "short"
    if px <= 0:
        return False, "no_quote"

    # Sanity: a real price should be within 10x of entry. Stops are
    # typically within 5-20% of entry; targets within 5-100%. A 10x
    # divergence (or 0.1x) is an upstream-data error, not a real move.
    # When entry is unset (0/None), skip this guard since we can't
    # judge plausibility.
    #
    # f-exit-monitor-quote-guard-unification (2026-05-06): the
    # implausibility threshold lives in ``_exit_monitor_common.py`` so
    # all three lanes share one definition. Reason string format
    # preserved byte-identical so the prefix-match contract in
    # ``run_crypto_exit_pass`` and the upstream-shape test
    # (``test_evaluate_exit_triggers_implausible_quote_prefix``) keep
    # working unmodified.
    if is_implausible_quote(px, entry):
        ratio = px / entry
        return False, (
            f"no_trigger:implausible_quote px={px} entry={entry} "
            f"ratio={ratio:.4f} (rejected; refusing to act on data error)"
        )

    if stop is not None and stop > 0:
        if is_long and px <= stop:
            return True, f"stop_loss_hit px={px} <= stop={stop}"
        if not is_long and px >= stop:
            return True, f"stop_loss_hit_short px={px} >= stop={stop}"
    if target is not None and target > 0:
        if is_long and px >= target:
            return True, f"take_profit_hit px={px} >= target={target}"
        if not is_long and px <= target:
            return True, f"take_profit_hit_short px={px} <= target={target}"
    return False, "no_trigger"


def _is_crypto_trade(trade: Trade) -> bool:
    """A Trade is a crypto trade if its ticker uses the RH ``-USD`` suffix.

    Also accepts ``broker_source='coinbase'`` so the same monitor works
    for Coinbase-routed crypto if/when that adapter is wired in.
    """
    if _is_crypto_ticker(trade.ticker or ""):
        return True
    src = (trade.broker_source or "").strip().lower()
    if src == "coinbase":
        return True
    return False


def _broker_source_for_trade(trade: Trade) -> str:
    src = (trade.broker_source or "").strip().lower()
    return "coinbase" if src == "coinbase" else "robinhood"


def _position_qty_for_trade(trade: Trade) -> tuple[float | None, str]:
    """Return broker-truth quantity for *trade* from the trade's own venue."""
    broker_source = _broker_source_for_trade(trade)
    try:
        if broker_source == "coinbase":
            from ... import coinbase_service

            positions = coinbase_service.get_positions() or []
        else:
            from ... import broker_service

            positions = broker_service.get_crypto_positions() or []
    except Exception:
        logger.debug(
            "[crypto_exit] broker-position fetch failed for %s via %s",
            trade.ticker, broker_source, exc_info=True,
        )
        return None, broker_source

    ticker_up = _to_usd_product_id(trade.ticker)
    for pos in positions:
        pos_ticker = (
            pos.get("ticker")
            or pos.get("symbol")
            or pos.get("product_id")
            or pos.get("currency")
        )
        if _to_usd_product_id(str(pos_ticker or "")) != ticker_up:
            continue
        try:
            return float(pos.get("quantity") or 0.0), broker_source
        except (TypeError, ValueError):
            return None, broker_source
    return None, broker_source


def _place_market_sell_for_trade(trade: Trade, qty: float) -> dict[str, Any]:
    """Submit the exit order to the venue that owns the open position."""
    broker_source = _broker_source_for_trade(trade)
    if broker_source == "coinbase":
        from ... import coinbase_service

        return coinbase_service.place_sell_order(
            ticker=trade.ticker,
            quantity=qty,
            order_type="market",
        )

    from ... import broker_service

    return broker_service.place_crypto_sell_order(
        ticker=trade.ticker,
        quantity=qty,
        order_type="market",
    )


def _coinbase_insufficient_balance(error: str | None) -> bool:
    return "insufficient balance" in str(error or "").strip().lower()


def _cancel_coinbase_open_sell_orders(ticker: str) -> list[str]:
    """Release base-asset holds from stale Coinbase exits before market exit."""
    from ... import coinbase_service

    product_id = _to_usd_product_id(ticker)
    cancelled: list[str] = []
    for order in coinbase_service.get_open_orders(product_ids=[product_id]):
        side = str(order.get("side") or "").upper()
        if side != "SELL":
            continue
        order_id = str(order.get("order_id") or order.get("id") or "")
        if not order_id:
            continue
        result = coinbase_service.cancel_order_by_id(order_id)
        if isinstance(result, dict) and result.get("ok"):
            cancelled.append(order_id)
        else:
            logger.warning(
                "[crypto_exit] Coinbase cancel failed before market exit "
                "ticker=%s order_id=%s err=%s",
                product_id,
                order_id,
                (result or {}).get("error") if isinstance(result, dict) else result,
            )
    return cancelled


def run_crypto_exit_pass(db: Session) -> dict[str, Any]:
    """Single pass over open crypto Trade rows. Returns a summary dict."""
    out: dict[str, Any] = {
        "checked": 0,
        "closed": 0,
        "deferred": 0,
        "skipped": 0,
        "errors": [],
    }

    if not bool(getattr(settings, "chili_autotrader_crypto_exit_monitor_enabled", True)):
        out["skipped_reason"] = "flag_off"
        return out

    # Kill switch + drawdown breaker apply uniformly to all lanes -- the entry
    # path already gates on these; on the exit path we still want to honor
    # the kill switch so a panicked operator can pause every action.
    try:
        from ..governance import is_kill_switch_active
        if is_kill_switch_active():
            out["skipped_reason"] = "kill_switch"
            return out
    except Exception:
        pass

    uid = getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )

    open_rows = (
        db.query(Trade)
        .filter(Trade.status == "open")
        .all()
    )
    crypto_rows = [t for t in open_rows if _is_crypto_trade(t)]
    out["candidate_pool"] = len(crypto_rows)

    # Parity with the equity exit lane (auto_trader_monitor.py:413-453): the
    # pattern monitor's latest `exit_now` recommendation is itself an exit
    # trigger, even when price has not hit stop or target. Without this lookup
    # the crypto lane held positions for ~20h after the LLM/pattern monitor
    # had recommended exit -- TRUMP-USD trade 1829 was the surfaced case
    # (recommendations from 2026-05-05 20:40 onward, never executed). The
    # equity lane reads the same table and acts on it; crypto must mirror.
    latest_monitor_decisions = _latest_monitor_decisions_by_trade(
        db,
        [int(t.id) for t in crypto_rows],
    )

    for t in crypto_rows:
        out["checked"] += 1
        # Already submitted exit -- defer
        if t.pending_exit_order_id:
            out["deferred"] += 1
            continue
        entry = float(t.entry_price or 0.0)
        stop = float(t.stop_loss) if t.stop_loss is not None else None
        target = float(t.take_profit) if t.take_profit is not None else None
        if (stop is None or stop <= 0) and (target is None or target <= 0):
            out["skipped"] += 1
            continue
        px = _current_crypto_price(t.ticker)
        if px is None:
            out["errors"].append(f"no_quote:{t.ticker}")
            continue
        should_exit, reason = _evaluate_exit_triggers(
            px=px, entry=entry, stop=stop, target=target,
            direction=(t.direction or "long"),
        )
        # Pattern-monitor exit_now branch -- only consulted when price triggers
        # have not fired, so stop/target wins on tie (cheaper to evaluate, and
        # those reasons carry stronger semantics for postmortems). The canonical
        # `pending_exit_reason` value is "pattern_exit_now" to match the equity
        # lane (auto_trader_monitor.py:453); the decision-id audit detail goes
        # in the structured log line rather than being truncated into the 50-char
        # reason column.
        #
        # f-fix-implausible-quote-vs-exit_now-ordering (2026-05-06): when
        # _evaluate_exit_triggers refuses on an implausible quote, do NOT
        # consult the pattern-monitor advisory -- the lane has just
        # declared it does not trust its own price feed for this trade.
        # f-exit-monitor-quote-guard-unification (2026-05-06): gate routed
        # through the shared ``should_consult_monitor_after_refusal`` helper
        # so all three lanes use one trust-boundary definition.
        monitor_exit_meta: Optional[dict[str, Any]] = None
        if not should_exit and should_consult_monitor_after_refusal(reason):
            monitor_exit_meta = _fresh_monitor_exit_meta(
                latest_monitor_decisions.get(int(t.id))
            )
            if monitor_exit_meta is not None:
                should_exit = True
                reason = "pattern_exit_now"
        if not should_exit:
            continue

        # Place the sell.
        try:
            qty = float(t.quantity or 0.0)
            if qty <= 0:
                out["errors"].append(f"bad_qty:{t.ticker}")
                continue

            # Round-13 FIX (2026-04-30): clamp the sell qty to broker
            # truth. f-coinbase-stop-hit-exit-routing (2026-05-12):
            # broker truth must come from the trade's venue. Coinbase
            # trades used to hit Robinhood's crypto position API here,
            # which left valid Coinbase stop hits deferred forever.
            _broker_qty, _broker_source = _position_qty_for_trade(t)

            if _broker_qty is None:
                logger.warning(
                    "[crypto_exit] cannot resolve %s broker qty for "
                    "trade#%s %s (local_qty=%s); deferring sell to next pass.",
                    _broker_source, t.id, t.ticker, qty,
                )
                out["deferred"] += 1
                continue
            if _broker_qty <= 0:
                logger.warning(
                    "[crypto_exit] %s broker holds 0 of %s "
                    "(trade#%s local_qty=%s); position already closed externally. "
                    "Marking trade with no_position; broker_sync close path "
                    "will reconcile.",
                    _broker_source, t.ticker, t.id, qty,
                )
                out["skipped"] += 1
                out["errors"].append(f"broker_holds_zero:{t.ticker}")
                continue
            if _broker_qty < qty:
                logger.warning(
                    "[crypto_exit] clamping sell qty for trade#%s %s via %s "
                    "(local=%s -> broker=%s)",
                    t.id, t.ticker, _broker_source, qty, _broker_qty,
                )
                qty = _broker_qty

            res = _place_market_sell_for_trade(t, qty)
            if not (isinstance(res, dict) and res.get("ok")):
                err = (res or {}).get("error") if isinstance(res, dict) else "unknown"
                if _broker_source == "coinbase" and _coinbase_insufficient_balance(err):
                    cancelled_orders = _cancel_coinbase_open_sell_orders(t.ticker)
                    if cancelled_orders:
                        logger.warning(
                            "[crypto_exit] cancelled %s open Coinbase sell order(s) "
                            "for trade#%s %s before retrying market exit",
                            len(cancelled_orders), t.id, t.ticker,
                        )
                        res = _place_market_sell_for_trade(t, qty)
                        if isinstance(res, dict) and res.get("ok"):
                            err = None
                        else:
                            err = (
                                (res or {}).get("error")
                                if isinstance(res, dict)
                                else "unknown"
                            )
                    else:
                        logger.warning(
                            "[crypto_exit] Coinbase insufficient balance for trade#%s "
                            "%s but no open sell order could be cancelled",
                            t.id, t.ticker,
                        )
                if isinstance(res, dict) and res.get("ok"):
                    pass
                else:
                    logger.warning(
                        "[crypto_exit] sell failed trade#%s ticker=%s broker=%s "
                        "reason=%s err=%s",
                        t.id, t.ticker, _broker_source, reason, err,
                    )
                    out["errors"].append(f"sell_failed:{t.ticker}:{err}")
                    continue
            order_id = (res.get("raw") or {}).get("id") or res.get("order_id") or ""
            t.pending_exit_order_id = str(order_id)
            t.pending_exit_reason = reason[:50]
            t.pending_exit_status = "submitted"
            t.pending_exit_requested_at = datetime.now(timezone.utc).replace(tzinfo=None)
            db.add(t)
            db.commit()
            out["closed"] += 1
            if monitor_exit_meta is not None:
                logger.info(
                    "[crypto_exit] CLOSED trade#%s ticker=%s qty=%s reason=%s "
                    "order_id=%s monitor_decision_id=%s monitor_src=%s "
                    "monitor_age_h=%s monitor_price=%s",
                    t.id, t.ticker, qty, reason, order_id,
                    monitor_exit_meta.get("decision_id"),
                    monitor_exit_meta.get("decision_source"),
                    monitor_exit_meta.get("decision_age_hours"),
                    monitor_exit_meta.get("decision_price"),
                )
            else:
                logger.info(
                    "[crypto_exit] CLOSED trade#%s ticker=%s qty=%s reason=%s order_id=%s",
                    t.id, t.ticker, qty, reason, order_id,
                )
        except Exception as e:
            logger.exception("[crypto_exit] unexpected failure for trade#%s: %s", t.id, e)
            out["errors"].append(f"exception:{t.ticker}:{str(e)[:80]}")
    return out
