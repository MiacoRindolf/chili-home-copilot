"""Crypto-aware exit monitor (Task HHH).

Closes open crypto Trade rows when the position's stop_loss / take_profit
levels are hit at the current market price. Built as a parallel of the
options exit monitor (Phase 5 PP) because the equity exit monitor
(``auto_trader_monitor.tick_auto_trader_monitor``) explicitly skips
``robinhood + ticker ends in -USD`` -- KK shipped the entry side but the
exit side was never wired.

Design choices, in line with the equity / options paths:

  - Uses ``broker_service.place_crypto_sell_order`` (mirrors crypto
    buys), not the spot adapter (which is share-based).
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
from ....models.trading import Trade

logger = logging.getLogger(__name__)


def _is_crypto_ticker(ticker: str) -> bool:
    """RH crypto convention: 'BTC-USD', 'RAY-USD', etc."""
    return bool((ticker or "").upper().endswith("-USD"))


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
    # Fallback: try the broker's own quote endpoint
    try:
        from ... import broker_service
        if broker_service.is_connected():
            try:
                import robin_stocks.robinhood as rh
                base = ticker.upper().split("-")[0]
                q = rh.crypto.get_crypto_quote(base)
                if q and q.get("mark_price"):
                    return float(q["mark_price"])
            except Exception as e:
                logger.debug("[crypto_exit] rh.crypto.get_crypto_quote failed for %s: %s", ticker, e)
    except Exception:
        pass
    return None


def _evaluate_exit_triggers(
    *,
    px: float,
    entry: float,
    stop: Optional[float],
    target: Optional[float],
    direction: str = "long",
) -> tuple[bool, str]:
    """Pure: should this trade exit at ``px``? Returns (should_exit, reason)."""
    is_long = (direction or "long").lower() != "short"
    if px <= 0:
        return False, "no_quote"
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
        if not should_exit:
            continue

        # Place the sell.
        try:
            from ... import broker_service
            qty = float(t.quantity or 0.0)
            if qty <= 0:
                out["errors"].append(f"bad_qty:{t.ticker}")
                continue
            res = broker_service.place_crypto_sell_order(
                ticker=t.ticker,
                quantity=qty,
                order_type="market",
            )
            if not (isinstance(res, dict) and res.get("ok")):
                err = (res or {}).get("error") if isinstance(res, dict) else "unknown"
                logger.warning(
                    "[crypto_exit] sell failed trade#%s ticker=%s reason=%s err=%s",
                    t.id, t.ticker, reason, err,
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
            logger.info(
                "[crypto_exit] CLOSED trade#%s ticker=%s qty=%s reason=%s order_id=%s",
                t.id, t.ticker, qty, reason, order_id,
            )
        except Exception as e:
            logger.exception("[crypto_exit] unexpected failure for trade#%s: %s", t.id, e)
            out["errors"].append(f"exception:{t.ticker}:{str(e)[:80]}")
    return out
