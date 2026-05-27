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
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....config import (
    CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_SECONDS,
    CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_START_STREAK,
    CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_SECONDS,
    CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_START_STREAK,
    CRYPTO_EXIT_MISSING_QTY_BACKOFF_MIN_SECONDS,
    settings,
)
from ....models.trading import PatternMonitorDecision, Trade

logger = logging.getLogger(__name__)

COINBASE_EXIT_SIDE = "sell"
CRYPTO_EXIT_MISSING_QTY_SNAPSHOT_KEY = "crypto_exit_missing_qty_backoff"
CRYPTO_EXIT_MISSING_QTY_PENDING_REASON = "missing_broker_qty"

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


def _current_crypto_price(
    ticker: str,
    *,
    broker_source: str | None = None,
    direction: str | None = None,
) -> Optional[float]:
    """Best-effort broker-first price fetch for a crypto ticker."""
    broker_key = (broker_source or "").strip().lower()
    if broker_key:
        try:
            from ..venue.factory import get_adapter

            adapter = get_adapter(broker_key)
            if adapter is not None:
                is_enabled = getattr(adapter, "is_enabled", None)
                if not callable(is_enabled) or is_enabled():
                    tick = None
                    fresh = None
                    get_ticker = getattr(adapter, "get_ticker", None)
                    if callable(get_ticker):
                        raw_tick = get_ticker(ticker)
                        if isinstance(raw_tick, tuple) and len(raw_tick) == 2:
                            tick, fresh = raw_tick
                    if tick is None:
                        get_bbo = getattr(adapter, "get_best_bid_ask", None)
                        if callable(get_bbo):
                            raw_bbo = get_bbo(ticker)
                            if isinstance(raw_bbo, tuple) and len(raw_bbo) == 2:
                                tick, fresh = raw_bbo
                    if tick is not None:
                        try:
                            if fresh is not None and float(fresh.age_seconds()) > float(fresh.max_age_seconds):
                                tick = None
                        except Exception:
                            tick = None
                    if tick is not None:
                        bid = getattr(tick, "bid", None)
                        ask = getattr(tick, "ask", None)
                        mid = getattr(tick, "mid", None)
                        last = getattr(tick, "last_price", None)
                        side = (direction or "long").strip().lower()
                        candidates = (
                            (bid, mid, last, ask)
                            if side != "short"
                            else (ask, mid, last, bid)
                        )
                        for px in candidates:
                            if px is not None and float(px) > 0:
                                return float(px)
        except Exception as e:
            logger.debug(
                "[crypto_exit] broker quote failed for %s via %s: %s",
                ticker,
                broker_key,
                e,
            )
        return None

    try:
        from ..market_data import fetch_quote
        q = fetch_quote(ticker)
        if q:
            p = q.get("price") or q.get("last_price")
            if p is not None:
                return float(p)
    except Exception as e:
        logger.debug("[crypto_exit] fetch_quote failed for %s: %s", ticker, e)
    # Legacy fallback for older Robinhood crypto rows missing broker_source.
    if broker_key in ("", "robinhood"):
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


def _settings_int_clamped(name: str, default: int, *, lower: int, upper: int) -> int:
    raw = getattr(settings, name, default)
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = int(default)
    return max(int(lower), min(int(upper), value))


def _missing_qty_backoff_seconds() -> int:
    return _settings_int_clamped(
        "chili_autotrader_crypto_exit_missing_qty_backoff_seconds",
        CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_SECONDS,
        lower=CRYPTO_EXIT_MISSING_QTY_BACKOFF_MIN_SECONDS,
        upper=CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_SECONDS,
    )


def _missing_qty_backoff_start_streak() -> int:
    return _settings_int_clamped(
        "chili_autotrader_crypto_exit_missing_qty_backoff_start_streak",
        CRYPTO_EXIT_MISSING_QTY_BACKOFF_DEFAULT_START_STREAK,
        lower=1,
        upper=CRYPTO_EXIT_MISSING_QTY_BACKOFF_MAX_START_STREAK,
    )


def _utcnow_naive() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _trade_snapshot_dict(trade: Trade) -> dict[str, Any]:
    snap = getattr(trade, "indicator_snapshot", None)
    return dict(snap) if isinstance(snap, dict) else {}


def _parse_backoff_until(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", ""))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


def _missing_qty_backoff_meta(trade: Trade, *, now: datetime) -> dict[str, Any] | None:
    snap = _trade_snapshot_dict(trade)
    meta = snap.get(CRYPTO_EXIT_MISSING_QTY_SNAPSHOT_KEY)
    if not isinstance(meta, dict):
        return None
    backoff_until = _parse_backoff_until(meta.get("backoff_until"))
    if backoff_until is None or now >= backoff_until:
        return None
    return {**meta, "backoff_until_dt": backoff_until}


def _clear_missing_qty_backoff(trade: Trade) -> bool:
    changed = False
    if int(getattr(trade, "crypto_broker_zero_qty_streak", 0) or 0) != 0:
        trade.crypto_broker_zero_qty_streak = 0
        changed = True
    snap = _trade_snapshot_dict(trade)
    if CRYPTO_EXIT_MISSING_QTY_SNAPSHOT_KEY in snap:
        snap.pop(CRYPTO_EXIT_MISSING_QTY_SNAPSHOT_KEY, None)
        trade.indicator_snapshot = snap
        changed = True
    if (
        not getattr(trade, "pending_exit_order_id", None)
        and (getattr(trade, "pending_exit_reason", None) or "")
        == CRYPTO_EXIT_MISSING_QTY_PENDING_REASON
    ):
        trade.pending_exit_status = None
        trade.pending_exit_requested_at = None
        trade.pending_exit_reason = None
        changed = True
    return changed


def _mark_missing_qty_deferred(
    db: Session,
    trade: Trade,
    *,
    broker_source: str,
    local_qty: float,
    now: datetime,
) -> dict[str, Any]:
    streak = int(getattr(trade, "crypto_broker_zero_qty_streak", 0) or 0) + 1
    start_streak = _missing_qty_backoff_start_streak()
    backoff_seconds = _missing_qty_backoff_seconds()
    backoff_until = (
        now + timedelta(seconds=backoff_seconds)
        if streak >= start_streak and backoff_seconds > 0
        else None
    )
    meta: dict[str, Any] = {
        "reason": CRYPTO_EXIT_MISSING_QTY_PENDING_REASON,
        "broker_source": broker_source,
        "streak": streak,
        "start_streak": start_streak,
        "local_qty": float(local_qty or 0.0),
        "observed_at": now.isoformat(),
        "backoff_seconds": backoff_seconds,
        "backoff_until": backoff_until.isoformat() if backoff_until else None,
    }
    snap = _trade_snapshot_dict(trade)
    snap[CRYPTO_EXIT_MISSING_QTY_SNAPSHOT_KEY] = meta
    trade.indicator_snapshot = snap
    trade.crypto_broker_zero_qty_streak = streak
    trade.pending_exit_status = "deferred"
    trade.pending_exit_requested_at = now
    trade.pending_exit_reason = CRYPTO_EXIT_MISSING_QTY_PENDING_REASON
    db.add(trade)
    db.commit()
    return meta


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


def _coinbase_exit_limit_buffer_pct() -> float:
    try:
        value = float(
            getattr(
                settings,
                "chili_coinbase_exit_limit_fallback_buffer_pct",
                getattr(settings, "chili_coinbase_stop_limit_buffer_pct", 0.005),
            )
            or 0.0
        )
    except (TypeError, ValueError):
        value = 0.005
    return min(max(value, 0.0001), 0.10)


def _coinbase_limit_only_mode(error: str | None) -> bool:
    msg = str(error or "").strip().lower()
    return "limit only mode" in msg or "please use limit order type" in msg


def _coinbase_spot_adapter():
    from ..venue.coinbase_spot import CoinbaseSpotAdapter

    return CoinbaseSpotAdapter()


def _place_coinbase_sell_for_trade(
    trade: Trade,
    qty: float,
    *,
    px: float | None = None,
) -> dict[str, Any]:
    """Submit a Coinbase crypto exit using product-specific precision.

    Coinbase enforces per-product ``base_increment``. The legacy
    ``coinbase_service.place_sell_order`` only normalized crypto quantity
    to eight decimals, which is still too fine for some products. The spot
    venue adapter pulls product metadata first and quantizes size/price
    before the broker sees the order.
    """
    product_id = _to_usd_product_id(trade.ticker)
    adapter = _coinbase_spot_adapter()
    market_res = adapter.place_market_order(
        product_id=product_id,
        side=COINBASE_EXIT_SIDE,
        base_size=str(qty),
    )
    if isinstance(market_res, dict) and market_res.get("ok"):
        market_res.setdefault("order_type", "market")
        return market_res

    err = (market_res or {}).get("error") if isinstance(market_res, dict) else None
    if not (_coinbase_limit_only_mode(err) and px is not None and px > 0):
        return market_res

    buffer_pct = _coinbase_exit_limit_buffer_pct()
    limit_px = px * (1.0 - buffer_pct)
    if limit_px <= 0:
        return market_res
    limit_res = adapter.place_limit_order_gtc(
        product_id=product_id,
        side=COINBASE_EXIT_SIDE,
        base_size=str(qty),
        limit_price=str(limit_px),
        post_only=False,
    )
    if isinstance(limit_res, dict):
        limit_res.setdefault("fallback_from", "market")
        limit_res.setdefault("fallback_reason", str(err or "limit_only_mode"))
        limit_res.setdefault("order_type", "limit")
        limit_res.setdefault("market_error", str(err or ""))
    if isinstance(limit_res, dict) and limit_res.get("ok"):
        logger.warning(
            "[crypto_exit] Coinbase market sell refused limit-only mode; "
            "submitted marketable limit exit trade#%s ticker=%s qty=%s px=%s "
            "limit_px=%s buffer_pct=%s",
            getattr(trade, "id", None),
            product_id,
            qty,
            px,
            limit_px,
            buffer_pct,
        )
        return limit_res
    if isinstance(market_res, dict) and isinstance(limit_res, dict):
        fallback_error = str(limit_res.get("error") or "")
        market_error = str(err or "")
        logger.warning(
            "[crypto_exit] Coinbase limit-only fallback failed trade#%s "
            "ticker=%s market_error=%s fallback_error=%s",
            getattr(trade, "id", None),
            product_id,
            market_error,
            fallback_error,
        )
        merged = dict(market_res)
        merged["market_error"] = market_error
        merged["fallback_error"] = fallback_error
        merged["fallback_order_type"] = "limit"
        if fallback_error:
            merged["error"] = fallback_error
        return merged
    return market_res


def _place_market_sell_for_trade(
    trade: Trade,
    qty: float,
    *,
    px: float | None = None,
) -> dict[str, Any]:
    """Submit the exit order to the venue that owns the open position."""
    broker_source = _broker_source_for_trade(trade)
    if broker_source == "coinbase":
        return _place_coinbase_sell_for_trade(trade, qty, px=px)

    from ... import broker_service

    return broker_service.place_crypto_sell_order(
        ticker=trade.ticker,
        quantity=qty,
        order_type="market",
    )


def _coinbase_insufficient_balance(error: str | None) -> bool:
    return "insufficient balance" in str(error or "").strip().lower()


def _coinbase_dust_notional_threshold_usd() -> float:
    try:
        from ... import coinbase_service

        return float(getattr(coinbase_service, "_MIN_AUTO_CREATE_NOTIONAL_USD", 1.0))
    except Exception:
        return 1.0


def _is_coinbase_unmarketable_dust(qty: float, px: float | None) -> bool:
    try:
        q = float(qty or 0.0)
        p = float(px or 0.0)
    except (TypeError, ValueError):
        return False
    if q <= 0 or p <= 0:
        return False
    threshold = _coinbase_dust_notional_threshold_usd()
    return threshold > 0 and (q * p) < threshold


def _close_coinbase_dust_trade(
    db: Session,
    trade: Trade,
    *,
    qty: float,
    px: float,
    trigger_reason: str,
) -> None:
    """Stop retrying unmarketable Coinbase dust as an actionable position."""
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    q = float(qty or 0.0)
    p = float(px or 0.0)
    entry = float(trade.entry_price or 0.0)
    pnl = (p - entry) * q
    if str(trade.direction or "long").lower() == "short":
        pnl = -pnl
    notional = q * p
    threshold = _coinbase_dust_notional_threshold_usd()

    trade.status = "closed"
    if q > 0:
        trade.quantity = q
    trade.exit_price = p
    trade.exit_date = now
    trade.pnl = round(pnl, 4)
    trade.exit_reason = "coinbase_dust_unmarketable"
    trade.broker_status = "dust_unmarketable"
    trade.last_broker_sync = now
    trade.pending_exit_order_id = None
    trade.pending_exit_status = None
    trade.pending_exit_requested_at = None
    trade.pending_exit_reason = None
    trade.notes = (
        (trade.notes or "")
        + "\nAuto-closed dust residual: Coinbase position notional "
        + f"${notional:.6f} below ${threshold:.2f} minimum; "
        + "broker may retain untradeable dust."
    )
    db.add(trade)
    db.commit()

    try:
        from ..execution_audit import record_execution_event

        record_execution_event(
            db,
            user_id=trade.user_id,
            ticker=trade.ticker,
            trade=trade,
            scan_pattern_id=getattr(trade, "scan_pattern_id", None),
            broker_source="coinbase",
            order_id=None,
            event_type="coinbase_dust_close",
            status="closed",
            requested_quantity=q,
            cumulative_filled_quantity=0.0,
            average_fill_price=p,
            payload_json={
                "side": "sell",
                "synthetic": True,
                "source": "crypto_exit_monitor",
                "reason": "coinbase_dust_unmarketable",
                "trigger_reason": trigger_reason[:120],
                "notional_usd": notional,
                "dust_threshold_usd": threshold,
            },
        )
    except Exception:
        logger.debug(
            "[crypto_exit] dust close execution_event failed for trade#%s",
            trade.id,
            exc_info=True,
        )
    try:
        from ..brain_work.execution_hooks import on_live_trade_closed

        on_live_trade_closed(db, trade, source="coinbase_dust_residual_close")
    except Exception:
        logger.debug(
            "[crypto_exit] dust close learning hook failed for trade#%s",
            trade.id,
            exc_info=True,
        )


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
        px = _current_crypto_price(
            t.ticker,
            broker_source=t.broker_source,
            direction=t.direction,
        )
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
        now = _utcnow_naive()
        backoff_meta = _missing_qty_backoff_meta(t, now=now)
        if backoff_meta is not None:
            out["deferred"] += 1
            out["missing_qty_backoff_skipped"] = int(
                out.get("missing_qty_backoff_skipped") or 0
            ) + 1
            logger.debug(
                "[crypto_exit] broker qty backoff active for trade#%s %s "
                "until=%s streak=%s",
                t.id,
                t.ticker,
                backoff_meta.get("backoff_until"),
                backoff_meta.get("streak"),
            )
            continue

        # Place the sell.
        try:
            trade_id = int(getattr(t, "id", 0) or 0)
            trade_ticker = str(getattr(t, "ticker", "") or "")
            trade_user_id = getattr(t, "user_id", None)
            trade_scan_pattern_id = getattr(t, "scan_pattern_id", None)
            trade_broker_order_id = getattr(t, "broker_order_id", None)
            trade_status = getattr(t, "status", None)
            trade_broker_status = getattr(t, "broker_status", None)
            qty = float(t.quantity or 0.0)
            if qty <= 0:
                out["errors"].append(f"bad_qty:{trade_ticker}")
                continue

            # Round-13 FIX (2026-04-30): clamp the sell qty to broker
            # truth. f-coinbase-stop-hit-exit-routing (2026-05-12):
            # broker truth must come from the trade's venue. Coinbase
            # trades used to hit Robinhood's crypto position API here,
            # which left valid Coinbase stop hits deferred forever.
            _broker_qty, _broker_source = _position_qty_for_trade(t)

            if _broker_qty is None:
                meta = _mark_missing_qty_deferred(
                    db,
                    t,
                    broker_source=_broker_source,
                    local_qty=qty,
                    now=now,
                )
                logger.warning(
                    "[crypto_exit] cannot resolve %s broker qty for "
                    "trade#%s %s (local_qty=%s); deferring sell "
                    "streak=%s backoff_until=%s.",
                    _broker_source,
                    t.id,
                    t.ticker,
                    qty,
                    meta.get("streak"),
                    meta.get("backoff_until") or "next_pass",
                )
                out["deferred"] += 1
                out["missing_qty_deferred"] = int(
                    out.get("missing_qty_deferred") or 0
                ) + 1
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
            if _clear_missing_qty_backoff(t):
                db.add(t)
            if _broker_qty < qty:
                logger.warning(
                    "[crypto_exit] clamping sell qty for trade#%s %s via %s "
                    "(local=%s -> broker=%s)",
                    t.id, t.ticker, _broker_source, qty, _broker_qty,
                )
                qty = _broker_qty

            if _broker_source == "coinbase" and _is_coinbase_unmarketable_dust(qty, px):
                _close_coinbase_dust_trade(
                    db,
                    t,
                    qty=qty,
                    px=px,
                    trigger_reason=reason,
                )
                out["closed"] += 1
                out["dust_closed"] = int(out.get("dust_closed") or 0) + 1
                logger.warning(
                    "[crypto_exit] closed Coinbase dust residual trade#%s "
                    "ticker=%s qty=%s px=%s reason=%s",
                    t.id,
                    t.ticker,
                    qty,
                    px,
                    reason,
                )
                continue

            res = _place_market_sell_for_trade(t, qty, px=px)
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
                        res = _place_market_sell_for_trade(t, qty, px=px)
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

            # f-coinbase-exit-side-recording (2026-05-19): write a
            # sell-side execution_events row so the Phase 4 helper
            # ``position_has_recorded_sell`` sees this exit. Covers
            # both Coinbase AND Robinhood crypto exits because this
            # function dispatches to both via
            # ``_place_market_sell_for_trade``. Wrapped in try/except
            # so a record-event failure NEVER blocks the exit
            # submission that already succeeded.
            try:
                from ..execution_audit import record_execution_event
                _raw = res.get("raw") if isinstance(res, dict) else None
                _payload: dict[str, Any] = {
                    "side": "sell",
                    "source": "crypto_exit_monitor",
                    "trade_id": trade_id,
                    "reason": reason[:50],
                }
                if _raw is not None:
                    _payload["raw"] = _raw
                audit_trade = SimpleNamespace(
                    id=trade_id,
                    broker_source=_broker_source,
                    scan_pattern_id=trade_scan_pattern_id,
                    broker_order_id=trade_broker_order_id,
                    status=trade_status,
                    broker_status=trade_broker_status,
                )
                record_execution_event(
                    db,
                    user_id=trade_user_id,
                    ticker=trade_ticker,
                    trade=audit_trade,
                    scan_pattern_id=trade_scan_pattern_id,
                    broker_source=_broker_source,
                    order_id=str(order_id) if order_id else None,
                    event_type="crypto_exit_submitted",
                    status="submitted",
                    requested_quantity=float(qty) if qty is not None else None,
                    payload_json=_payload,
                )
            except Exception:
                logger.debug(
                    "[crypto_exit] record_execution_event failed for trade#%s "
                    "(non-fatal — exit was already submitted to the venue)",
                    trade_id, exc_info=True,
                )

            out["closed"] += 1
            if monitor_exit_meta is not None:
                logger.info(
                    "[crypto_exit] CLOSED trade#%s ticker=%s qty=%s reason=%s "
                    "order_id=%s monitor_decision_id=%s monitor_src=%s "
                    "monitor_age_h=%s monitor_price=%s",
                    trade_id, trade_ticker, qty, reason, order_id,
                    monitor_exit_meta.get("decision_id"),
                    monitor_exit_meta.get("decision_source"),
                    monitor_exit_meta.get("decision_age_hours"),
                    monitor_exit_meta.get("decision_price"),
                )
            else:
                logger.info(
                    "[crypto_exit] CLOSED trade#%s ticker=%s qty=%s reason=%s order_id=%s",
                    trade_id, trade_ticker, qty, reason, order_id,
                )
        except Exception as e:
            fallback_trade_id = locals().get("trade_id", "unknown")
            fallback_ticker = locals().get("trade_ticker", "unknown")
            logger.exception(
                "[crypto_exit] unexpected failure for trade#%s: %s",
                fallback_trade_id,
                e,
            )
            out["errors"].append(f"exception:{fallback_ticker}:{str(e)[:80]}")
    return out
