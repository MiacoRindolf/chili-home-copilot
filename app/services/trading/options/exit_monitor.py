"""Option-aware exit monitor (Task PP / Phase 5).

Closes open option Trade rows when any of three exit triggers fire:

  1. **DTE threshold** — when days-to-expiration drops below
     ``chili_autotrader_options_exit_dte`` (default 7), close the
     position. This avoids the gamma blowup that hits long options
     in the final week.
  2. **Premium stop-loss** — when current bid drops more than
     ``chili_autotrader_options_exit_stop_pct`` below entry premium
     (default 50% loss). Cuts losers before zero.
  3. **Premium take-profit** — when current bid rises more than
     ``chili_autotrader_options_exit_tp_pct`` above entry premium
     (default 100% gain). Locks in winners before reversal.

Triggers are checked in order; the first match wins. The exit fires
``sell-to-close`` via :class:`RobinhoodOptionsAdapter`, which uses
the same idempotency / audit plumbing the equity monitor uses.

Out of scope (deferred):
  - Multi-leg spread exits. The current version exits each leg
    separately. A real spread-close would use the same multi-leg
    spread endpoint with action=sell on each leg; that's a follow-up.
  - Trailing stops on premium. Phase 5 keeps the rules linear.
  - Rolling (close + reopen at next expiration). Operator-driven only.

Flag-gated by ``chili_autotrader_options_exit_monitor_enabled`` (default
OFF). When OFF, no option Trade rows are touched by this pass — the
existing equity exit monitor doesn't recognize option metadata, so
closing them stays the operator's job.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import Trade

logger = logging.getLogger(__name__)


def _is_option_trade(t: Trade) -> bool:
    """True when the trade row carries option metadata. Used to filter
    the equity-monitor universe out of the options-monitor pass.

    Heuristic priority:
      1. indicator_snapshot.option_meta is a non-empty dict
      2. tags contain 'options' (case-insensitive)
    """
    snap = t.indicator_snapshot if isinstance(t.indicator_snapshot, dict) else {}
    if isinstance(snap.get("option_meta"), dict) and snap["option_meta"]:
        return True
    tags = (t.tags or "").lower()
    return "options" in tags


def _opt_meta(t: Trade) -> dict[str, Any]:
    snap = t.indicator_snapshot if isinstance(t.indicator_snapshot, dict) else {}
    return snap.get("option_meta") or {}


def _dte(expiration: str) -> Optional[int]:
    """Calendar days from today to expiration. Negative when expired."""
    try:
        exp_d = datetime.strptime(str(expiration), "%Y-%m-%d").date()
        return (exp_d - datetime.now(timezone.utc).date()).days
    except Exception:
        return None


def _evaluate_exit_triggers(
    *, dte: Optional[int], entry_premium: float, current_premium: Optional[float],
    dte_threshold: int, stop_pct: float, tp_pct: float,
) -> Optional[str]:
    """Return the exit reason string when a trigger fires, else None.

    Reasons (also stored on the trade row as exit_reason):
      ``options_dte_threshold``     — DTE <= dte_threshold
      ``options_premium_stop_loss``  — drop > stop_pct
      ``options_premium_take_profit`` — gain > tp_pct
    """
    if dte is not None and dte <= dte_threshold:
        return "options_dte_threshold"
    if entry_premium > 0 and current_premium is not None and current_premium > 0:
        change_pct = (current_premium - entry_premium) / entry_premium * 100.0
        if change_pct <= -abs(stop_pct):
            return "options_premium_stop_loss"
        if change_pct >= abs(tp_pct):
            return "options_premium_take_profit"
    return None


def run_options_exit_pass(db: Session) -> dict[str, int]:
    """One pass over open option Trade rows. Idempotent — safe to call
    every monitor tick. Skips silently when the flag is OFF.

    Returns a counter dict for the audit log:
      checked / triggered / closed / errors / skipped_no_quote
    """
    summary = {
        "checked": 0,
        "triggered": 0,
        "closed": 0,
        "errors": 0,
        "skipped_no_quote": 0,
        "skipped_adapter_off": 0,
    }

    if not bool(getattr(settings, "chili_autotrader_options_exit_monitor_enabled", False)):
        return summary

    dte_threshold = int(getattr(settings, "chili_autotrader_options_exit_dte", 7))
    stop_pct = float(getattr(settings, "chili_autotrader_options_exit_stop_pct", 50.0))
    tp_pct = float(getattr(settings, "chili_autotrader_options_exit_tp_pct", 100.0))

    # Lazy import to avoid a hard module-load dependency on the
    # adapter (broker_service ultimately imports robin_stocks).
    from ..venue.robinhood_options import RobinhoodOptionsAdapter
    adapter = RobinhoodOptionsAdapter()
    if not adapter.is_enabled():
        summary["skipped_adapter_off"] = 1
        return summary

    # Pull open trades that look like options. Filter in Python so we
    # don't have to extend the model's query helpers — the open-trade
    # universe is small enough (typically < 100) that this is cheap.
    open_trades = (
        db.query(Trade)
        .filter(Trade.status.in_(("open", "working")))
        .all()
    )
    candidates = [t for t in open_trades if _is_option_trade(t)]

    for t in candidates:
        summary["checked"] += 1
        meta = _opt_meta(t)
        expiration = str(meta.get("expiration") or "")
        strike = meta.get("strike")
        option_type = str(meta.get("option_type") or "").lower()
        if not (expiration and strike and option_type in ("call", "put")):
            continue

        contract = adapter.find_contract(meta.get("underlying") or t.ticker, expiration, float(strike), option_type)
        if not contract:
            summary["skipped_no_quote"] += 1
            continue
        quote = adapter.get_quote(str(contract.get("id", "")))
        if not quote:
            summary["skipped_no_quote"] += 1
            continue
        try:
            bid = float(quote.get("bid_price") or 0)
        except (TypeError, ValueError):
            bid = 0.0
        current_premium = bid if bid > 0 else None

        entry_premium = 0.0
        try:
            # Prefer avg_fill_price when available (actual fill), fall
            # back to entry_price (the limit) otherwise.
            entry_premium = float(t.avg_fill_price or t.entry_price or 0.0)
        except (TypeError, ValueError):
            entry_premium = 0.0

        reason = _evaluate_exit_triggers(
            dte=_dte(expiration),
            entry_premium=entry_premium,
            current_premium=current_premium,
            dte_threshold=dte_threshold,
            stop_pct=stop_pct,
            tp_pct=tp_pct,
        )
        if not reason:
            continue
        summary["triggered"] += 1

        # Submit sell-to-close. Limit price = bid (cross spread for
        # clean fill) when available, else previous mark.
        limit_price = current_premium or float(quote.get("mark_price") or 0) or entry_premium
        try:
            res = adapter.place_option_sell(
                underlying=str(meta.get("underlying") or t.ticker),
                expiration=expiration,
                strike=float(strike),
                option_type=option_type,
                quantity=int(t.quantity or 0) or 1,
                limit_price=float(limit_price),
                position_effect="close",
            )
        except Exception as e:
            summary["errors"] += 1
            logger.warning(
                "[options_exit_monitor] trade=%s sell-to-close raised: %s",
                t.id, e, exc_info=True,
            )
            continue

        if not res.get("ok"):
            summary["errors"] += 1
            logger.warning(
                "[options_exit_monitor] trade=%s sell-to-close failed reason=%s broker_error=%s",
                t.id, reason, res.get("error"),
            )
            continue

        # Mark the trade row as 'closing' — pending broker fill. The
        # broker-sync reconciler picks up the fill and finalizes
        # status='closed' + exit_price + pnl. We just record the intent
        # + reason + the new pending order id.
        try:
            db.execute(text(
                """
                UPDATE trading_trades
                SET pending_exit_order_id = :oid,
                    pending_exit_status = 'submitted',
                    pending_exit_requested_at = :ts,
                    pending_exit_reason = :rsn
                WHERE id = :tid
                """
            ), {
                "oid": res.get("order_id"),
                "ts": datetime.now(timezone.utc),
                "rsn": reason,
                "tid": t.id,
            })
            db.commit()
            summary["closed"] += 1
            logger.info(
                "[options_exit_monitor] trade=%s closed reason=%s premium_now=%s "
                "entry=%s order_id=%s",
                t.id, reason, current_premium, entry_premium, res.get("order_id"),
            )
        except Exception:
            summary["errors"] += 1
            logger.exception(
                "[options_exit_monitor] trade=%s pending_exit write failed", t.id,
            )

    return summary


__all__ = ["run_options_exit_pass"]
