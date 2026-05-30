"""Option-aware exit monitor (Task PP / Phase 5).

Closes open option Trade rows when any of three exit triggers fire:

  1. **DTE threshold** — close before final-week gamma blowup.
  2. **Premium stop-loss** — cut losers before zero.
  3. **Premium take-profit** — lock winners before reversal.

The thresholds for each trigger are read from the StrategyParameter
ledger (family='autotrader_options'), bootstrapped from env values on
first run. The brain's learning loop adapts them from realized
outcomes the same way it adapts confidence_floor — no hardcoded
numbers in this module.

Triggers are checked in order; the first match wins. The exit fires
``sell-to-close`` via :class:`RobinhoodOptionsAdapter`, which uses
the same idempotency / audit plumbing the equity monitor uses.

Out of scope (deferred):
  - Multi-leg spread exits — currently exits each leg separately;
    closing-the-spread (single multi-leg sell-to-close) is a
    follow-up.
  - Trailing stops on premium.
  - Rolling (close + reopen at next expiration).

Flag-gated by ``chili_autotrader_options_exit_monitor_enabled`` (default
OFF). When OFF, no option Trade rows are touched by this pass.
"""
from __future__ import annotations

import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import Trade
from .contracts import parse_contract_quantity
from .quote_store import record_quote_snapshot

logger = logging.getLogger(__name__)

# Same StrategyParameter family as the synthesis path so the brain's
# learning loop sees options-related knobs as one unit.
STRATEGY_FAMILY = "autotrader_options"
_ACTIVE_PENDING_EXIT_STATES = {
    "queued",
    "pending",
    "confirmed",
    "unconfirmed",
    "partially_filled",
    "open",
    "working",
    "submitted",
}
_OPTION_EXIT_FILLED_STATES = {"filled", "done", "completed", "complete"}
_OPTION_EXIT_TERMINAL_STATES = {
    "cancelled",
    "canceled",
    "rejected",
    "failed",
    "expired",
}
_OPTION_EXIT_FILLED_QTY_KEYS = (
    "cumulative_quantity",
    "cumulative_filled_quantity",
    "filled_quantity",
    "processed_quantity",
    "quantity_filled",
    "filled_size",
)
_OPTION_EXIT_REQUESTED_QTY_KEYS = ("quantity", "requested_quantity")
EXIT_DTE_DEFAULT = 7.0
EXIT_DTE_MIN = 0.0
EXIT_DTE_MAX = 30.0
EXIT_STOP_PCT_DEFAULT = 50.0
EXIT_STOP_PCT_MIN = 10.0
EXIT_STOP_PCT_MAX = 80.0
EXIT_TP_PCT_DEFAULT = 100.0
EXIT_TP_PCT_MIN = 20.0
EXIT_TP_PCT_MAX = 500.0


def _register_exit_parameters(db: Session) -> None:
    """Idempotent registration of the exit-monitor knobs. Bootstraps
    with env values on first call (so the operator's CHILI_AUTOTRADER_*
    overrides act as initial values), then the DB is authoritative
    and the brain's learning loop is free to adapt within bounds.
    """
    try:
        from ..strategy_parameter import ParameterSpec, register_parameter

        register_parameter(db, ParameterSpec(
            strategy_family=STRATEGY_FAMILY,
            parameter_key="exit_dte",
            initial_value=_option_exit_setting_float(
                "chili_autotrader_options_exit_dte",
                EXIT_DTE_DEFAULT,
            ),
            min_value=EXIT_DTE_MIN, max_value=EXIT_DTE_MAX,
            description=(
                "Days-to-expiration threshold below which open option "
                "positions auto-close. Lower exposes the position to "
                "final-week gamma risk; higher gives up theta-bleed "
                "premium too early. Brain adapts within [0, 30] from "
                "realized PnL near-expiry vs early-close cohorts."
            ),
        ))
        register_parameter(db, ParameterSpec(
            strategy_family=STRATEGY_FAMILY,
            parameter_key="exit_stop_pct",
            initial_value=_option_exit_setting_float(
                "chili_autotrader_options_exit_stop_pct",
                EXIT_STOP_PCT_DEFAULT,
            ),
            min_value=EXIT_STOP_PCT_MIN, max_value=EXIT_STOP_PCT_MAX,
            description=(
                "Premium drop %% below entry that triggers stop-loss "
                "exit. Tighter = exit losers earlier, more frequent "
                "false stops. Brain adapts within [10, 80] from "
                "realized survival curves of option entries."
            ),
        ))
        register_parameter(db, ParameterSpec(
            strategy_family=STRATEGY_FAMILY,
            parameter_key="exit_tp_pct",
            initial_value=_option_exit_setting_float(
                "chili_autotrader_options_exit_tp_pct",
                EXIT_TP_PCT_DEFAULT,
            ),
            min_value=EXIT_TP_PCT_MIN, max_value=EXIT_TP_PCT_MAX,
            description=(
                "Premium gain %% above entry that triggers "
                "take-profit exit. Lower = lock smaller wins more "
                "often, miss home runs. Brain adapts within "
                "[20, 500] from option-trade outcome distributions."
            ),
        ))
    except Exception as e:
        logger.debug("[options_exit_monitor] _register_exit_parameters failed: %s", e)


def _is_option_trade(t: Trade) -> bool:
    """True when the trade row carries option metadata. Used to filter
    the equity-monitor universe out of the options-monitor pass.

    Delegates to :func:`autopilot_scope.is_option_trade`, the canonical
    helper that handles BOTH locations option_meta can live in:
      1. indicator_snapshot.option_meta (top-level)
      2. indicator_snapshot.breakout_alert.option_meta (nested - 2026-04
         autotrader_v1 writer puts it here)
    Falls back to a tags-based check ("options" in trade.tags) when the
    canonical helper returns False, preserving the legacy behavior.

    The original local implementation only checked location (1), causing
    the options exit pass to silently skip option Trade rows whose
    option_meta lived in (2) — see trade 392 for the canonical example.
    """
    from ..autopilot_scope import is_option_trade as _canonical
    if _canonical(t):
        return True
    tags = (t.tags or "").lower()
    return "options" in tags


def _is_exit_candidate_trade(t: Trade) -> bool:
    return (
        str(getattr(t, "status", "") or "").strip().lower() == "open"
        and _is_option_trade(t)
    )


def _has_active_pending_exit(t: Trade) -> bool:
    order_id = str(getattr(t, "pending_exit_order_id", "") or "").strip()
    status = str(getattr(t, "pending_exit_status", "") or "").strip().lower()
    return bool(order_id) and status in _ACTIVE_PENDING_EXIT_STATES


def _option_exit_order_state(res: dict[str, Any]) -> str:
    raw = res.get("raw") if isinstance(res.get("raw"), dict) else {}
    return str(
        res.get("state")
        or res.get("status")
        or raw.get("state")
        or raw.get("status")
        or "submitted"
    ).strip().lower()


def _option_exit_raw_order(
    res: dict[str, Any],
    *,
    order_id: str,
    state: str,
) -> dict[str, Any]:
    raw = dict(res.get("raw") if isinstance(res.get("raw"), dict) else {})
    raw.setdefault("id", order_id)
    raw.setdefault("state", state)
    for key in (
        "average_price",
        "avg_price",
        "average_fill_price",
        "price",
        "quantity",
        "requested_quantity",
        "cumulative_quantity",
        "cumulative_filled_quantity",
        "filled_quantity",
        "processed_quantity",
        "quantity_filled",
        "filled_size",
    ):
        if key not in raw and res.get(key) is not None:
            raw[key] = res.get(key)
    return raw


def _option_exit_float(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _option_exit_setting_float(name: str, default: float) -> float:
    parsed = _option_exit_float(getattr(settings, name, None))
    return parsed if parsed is not None else float(default)


def _option_exit_bounded_parameter_value(
    value: Any,
    default: float,
    *,
    min_value: float,
    max_value: float,
    integer: bool = False,
) -> float | int:
    parsed = _option_exit_float(_parameter_value_or_default(value, default))
    if parsed is None or parsed < min_value or parsed > max_value:
        parsed = float(default)
    return int(parsed) if integer else parsed


def _option_exit_quote_price(
    quote: dict[str, Any],
    *keys: str,
) -> tuple[float | None, bool]:
    for key in keys:
        raw = quote.get(key)
        if raw is None:
            continue
        if isinstance(raw, str) and not raw.strip():
            continue
        value = _option_exit_float(raw)
        if value is None or value < 0.0:
            return None, True
        return value, False
    return None, False


def _option_quote_has_malformed_price(quote: dict[str, Any]) -> bool:
    for keys in (
        ("bid_price", "bid"),
        ("ask_price", "ask"),
        ("mark_price", "mark"),
    ):
        _, malformed = _option_exit_quote_price(quote, *keys)
        if malformed:
            return True
    return False


def _option_quote_is_crossed(quote: dict[str, Any]) -> bool:
    bid, bid_malformed = _option_exit_quote_price(quote, "bid_price", "bid")
    ask, ask_malformed = _option_exit_quote_price(quote, "ask_price", "ask")
    if bid_malformed or ask_malformed:
        return False
    return bool(
        bid is not None
        and ask is not None
        and bid > 0.0
        and ask > 0.0
        and bid > ask
    )


def _option_exit_filled_quantity(raw_order: dict[str, Any]) -> float | None:
    for key in _OPTION_EXIT_FILLED_QTY_KEYS:
        qty = _option_exit_float(raw_order.get(key))
        if qty is not None:
            if qty < 0.0 or not float(qty).is_integer():
                return None
            return max(0.0, qty)
    return None


def _option_exit_has_filled_quantity(raw_order: dict[str, Any]) -> bool:
    return any(raw_order.get(key) is not None for key in _OPTION_EXIT_FILLED_QTY_KEYS)


def _option_exit_requested_quantity(trade: Trade, raw_order: dict[str, Any]) -> float | None:
    for key in _OPTION_EXIT_REQUESTED_QTY_KEYS:
        qty = parse_contract_quantity(raw_order.get(key))
        if qty is not None:
            return float(qty)
    qty = parse_contract_quantity(getattr(trade, "quantity", None))
    return float(qty) if qty is not None else None


def _option_exit_contract_quantity(trade: Trade) -> int | None:
    return parse_contract_quantity(getattr(trade, "quantity", None))


def _record_exit_quote_snapshot(
    db: Session,
    trade: Trade,
    meta: dict[str, Any],
    quote: dict[str, Any],
) -> bool:
    """Persist the quote the exit monitor actually used, best effort."""
    try:
        opt_meta = dict(meta or {})
        opt_meta.setdefault("underlying", getattr(trade, "ticker", None))
        return record_quote_snapshot(
            db,
            chain_id=None,
            option_meta=opt_meta,
            quote=quote,
        )
    except Exception:
        logger.debug(
            "[options_exit_monitor] quote snapshot write failed trade=%s",
            getattr(trade, "id", None),
            exc_info=True,
        )
        return False


def _option_exit_submit_fill_is_complete(
    trade: Trade,
    raw_order: dict[str, Any],
    state: str,
) -> bool:
    state = str(state or "").strip().lower()
    filled_qty = _option_exit_filled_quantity(raw_order)
    has_filled_qty = _option_exit_has_filled_quantity(raw_order)
    local_qty = _option_exit_contract_quantity(trade)
    requested_qty = _option_exit_requested_quantity(trade, raw_order)
    target_qty = float(local_qty) if local_qty is not None else requested_qty
    if state in _OPTION_EXIT_FILLED_STATES:
        if has_filled_qty:
            return (
                filled_qty is not None
                and target_qty is not None
                and filled_qty + 1e-9 >= target_qty
            )
        return target_qty is not None
    if state not in _OPTION_EXIT_TERMINAL_STATES:
        return False

    if filled_qty is None or target_qty is None or target_qty <= 0:
        return False
    return filled_qty + 1e-9 >= target_qty


def _parse_option_order_time(raw_order: dict[str, Any]) -> datetime | None:
    value = (
        raw_order.get("last_transaction_at")
        or raw_order.get("updated_at")
        or raw_order.get("created_at")
    )
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return None


def _configured_autotrader_user_id() -> int | None:
    uid = getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings,
        "brain_default_user_id",
        None,
    )
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


def _parameter_value_or_default(value: Any, default: Any) -> Any:
    return default if value is None else value


def _opt_meta(t: Trade) -> dict[str, Any]:
    """Resolve option_meta from BOTH top-level AND nested breakout_alert.option_meta.

    Earlier writers (autotrader_v1) put option_meta NESTED under breakout_alert -
    the same nested-location bug that _is_option_trade had until commit 9ae90f8.
    Without this fix, run_options_exit_pass counts the trade as 'checked' but
    immediately 'continue's because expiration/strike/option_type come back empty,
    so the stop/TP/DTE triggers never evaluate even when conditions are met.
    Concrete example: 2026-04-28 trade 392 (SPY 729C). Bid 2.42 was below stop
    threshold 2.807, but the engine never fired because _opt_meta returned {}.
    """
    import json as _json
    snap = t.indicator_snapshot
    if isinstance(snap, str):
        try: snap = _json.loads(snap)
        except Exception: snap = {}
    if not isinstance(snap, dict):
        return {}
    if isinstance(snap.get("option_meta"), dict) and snap["option_meta"]:
        return snap["option_meta"]
    ba = snap.get("breakout_alert")
    if isinstance(ba, str):
        try: ba = _json.loads(ba)
        except Exception: ba = None
    if isinstance(ba, dict):
        bom = ba.get("option_meta")
        if isinstance(bom, dict) and bom:
            return bom
    return {}


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
) -> tuple[Optional[str], bool]:
    """Return ``(reason, abstained_implausible)``.

    ``reason`` is the exit-trigger string when a trigger fires, else
    ``None``. ``abstained_implausible`` is ``True`` only when the
    implausible-quote guard fired -- the lane has refused to trust
    its own price feed for this trade and the caller MUST NOT
    consult the LLM/monitor advisory in that case (see
    ``f-fix-implausible-quote-vs-exit_now-ordering`` 2026-05-06).
    For ordinary "no trigger fired" the second tuple element is
    ``False`` and monitor consultation may proceed.

    Reasons (also stored on the trade row as exit_reason):
      ``options_dte_threshold``     -- DTE <= dte_threshold
      ``options_premium_stop_loss``  -- drop > stop_pct
      ``options_premium_take_profit`` -- gain > tp_pct

    Round-15 (2026-04-30): added implausible-quote guard parallel to
    the stock + crypto exit decision paths. A corrupted upstream
    options quote (e.g. bid returning 0.001 vs entry 0.50) would falsely
    trigger ``options_premium_stop_loss``. Reject quotes where the
    ratio (current/entry) > 10 or < 0.1 -- that's a 10x divergence,
    far more than legitimate option moves between 5-min monitor passes.
    Returns ``(None, True)`` so the next pass retries with a fresh
    quote rather than acting on garbage data, AND so the caller can
    distinguish refusal from "no trigger" when deciding whether to
    fall through to the LLM advisory.

    Note: options CAN legitimately drop near zero at expiration, but
    the configured stop_pct (default 50%) fires LONG BEFORE the 0.1x
    bound. Real legitimate exits happen at change_pct = -50% which is
    ratio = 0.5 -- well within the (0.1, 10) plausibility envelope.
    """
    # f-exit-monitor-quote-guard-unification (2026-05-06): the
    # implausibility check is sourced from ``_exit_monitor_common.py``
    # so all three lanes share one trust-boundary definition.
    from .._exit_monitor_common import is_implausible_quote

    if dte is not None and dte <= dte_threshold:
        return "options_dte_threshold", False
    if entry_premium > 0 and current_premium is not None and current_premium > 0:
        if is_implausible_quote(current_premium, entry_premium):
            ratio = current_premium / entry_premium
            # Implausible move -- abstain. Per no-hardcoded-fallback rule,
            # don't synthesize a "current value" -- return None and let the
            # next pass retry with a fresh quote. Set abstained_implausible
            # so the caller does NOT consult the LLM advisory as an override.
            logger.warning(
                "[options_exit_monitor] implausible quote ratio=%.4f "
                "(current=%s, entry=%s); refusing to act on data error -- "
                "next pass retries with fresh quote.",
                ratio, current_premium, entry_premium,
            )
            return None, True
        ratio = current_premium / entry_premium
        change_pct = (ratio - 1.0) * 100.0
        if change_pct <= -abs(stop_pct):
            return "options_premium_stop_loss", False
        if change_pct >= abs(tp_pct):
            return "options_premium_take_profit", False
    return None, False


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
        "working": 0,
        "errors": 0,
        "skipped_no_quote": 0,
        "skipped_adapter_off": 0,
        "skipped_no_user_scope": 0,
        "quote_snapshots": 0,
    }

    if not bool(getattr(settings, "chili_autotrader_options_exit_monitor_enabled", False)):
        return summary

    uid = _configured_autotrader_user_id()
    if uid is None:
        logger.warning(
            "[options_exit_monitor] aborted: no chili_autotrader_user_id/"
            "brain_default_user_id configured; cannot scope live exits"
        )
        summary["skipped_no_user_scope"] = 1
        return summary

    # Bootstrap StrategyParameter rows on first call (idempotent),
    # then read brain-adapted values. Env values seed the rows; the
    # DB is authoritative thereafter.
    _register_exit_parameters(db)
    from ..strategy_parameter import get_parameter

    dte_threshold_raw = get_parameter(
        db, STRATEGY_FAMILY, "exit_dte",
        default=_option_exit_setting_float(
            "chili_autotrader_options_exit_dte",
            EXIT_DTE_DEFAULT,
        ),
    )
    dte_threshold = _option_exit_bounded_parameter_value(
        dte_threshold_raw,
        EXIT_DTE_DEFAULT,
        min_value=EXIT_DTE_MIN,
        max_value=EXIT_DTE_MAX,
        integer=True,
    )
    stop_pct_raw = get_parameter(
        db, STRATEGY_FAMILY, "exit_stop_pct",
        default=_option_exit_setting_float(
            "chili_autotrader_options_exit_stop_pct",
            EXIT_STOP_PCT_DEFAULT,
        ),
    )
    stop_pct = _option_exit_bounded_parameter_value(
        stop_pct_raw,
        EXIT_STOP_PCT_DEFAULT,
        min_value=EXIT_STOP_PCT_MIN,
        max_value=EXIT_STOP_PCT_MAX,
    )
    tp_pct_raw = get_parameter(
        db, STRATEGY_FAMILY, "exit_tp_pct",
        default=_option_exit_setting_float(
            "chili_autotrader_options_exit_tp_pct",
            EXIT_TP_PCT_DEFAULT,
        ),
    )
    tp_pct = _option_exit_bounded_parameter_value(
        tp_pct_raw,
        EXIT_TP_PCT_DEFAULT,
        min_value=EXIT_TP_PCT_MIN,
        max_value=EXIT_TP_PCT_MAX,
    )

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
    from ..autopilot_scope import live_autopilot_trade_filter

    open_trades = (
        db.query(Trade)
        .filter(
            Trade.user_id == int(uid),
            Trade.status == "open",
            live_autopilot_trade_filter(),
        )
        .all()
    )
    candidates = [t for t in open_trades if _is_exit_candidate_trade(t)]

    # f-options-exit-monitor-pattern-exit-now-audit (2026-05-06):
    # parity with equity + crypto. The pattern-monitor (LLM advisory)
    # may have flipped a position to "thesis dead" via
    # PatternMonitorDecision.action='exit_now'. Without this batch-
    # load the options lane silently sat on stale advisories the same
    # way crypto did until 2026-05-06 (TRUMP-USD trade 1829, ~20h
    # held). The shared module enforces ONE freshness window across
    # all three lanes (96h).
    from .._exit_monitor_common import (
        latest_monitor_decisions_by_trade,
        fresh_monitor_exit_meta,
        should_consult_monitor_after_refusal,
    )
    latest_monitor_decisions = latest_monitor_decisions_by_trade(
        db, [int(t.id) for t in candidates]
    )

    for t in candidates:
        summary["checked"] += 1
        if _has_active_pending_exit(t):
            summary["working"] += 1
            continue
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
        if _option_quote_has_malformed_price(quote):
            logger.warning(
                "[options_exit_monitor] trade=%s malformed option quote "
                "bid=%s ask=%s mark=%s; refusing automated exit on "
                "untrusted market data",
                t.id,
                quote.get("bid_price") or quote.get("bid"),
                quote.get("ask_price") or quote.get("ask"),
                (
                    quote.get("mark_price")
                    or quote.get("mark")
                ),
            )
            summary["skipped_no_quote"] += 1
            continue
        if _option_quote_is_crossed(quote):
            logger.warning(
                "[options_exit_monitor] trade=%s crossed option quote bid=%s ask=%s; "
                "refusing automated exit on untrusted market data",
                t.id,
                quote.get("bid_price") or quote.get("bid"),
                quote.get("ask_price") or quote.get("ask"),
            )
            summary["skipped_no_quote"] += 1
            continue
        if _record_exit_quote_snapshot(db, t, meta, quote):
            summary["quote_snapshots"] += 1
        bid = _option_exit_quote_price(quote, "bid_price", "bid")[0] or 0.0
        mark = (
            _option_exit_quote_price(
                quote,
                "mark_price",
                "mark",
            )[0]
            or 0.0
        )
        # Round-15 (2026-04-30): use MARK for change calculation rather
        # than bid. Bid-vs-entry-ask is apples-to-oranges and biases
        # toward false stops by the bid-ask spread amount immediately
        # after entry (a position that hasn't moved still appears -2%
        # to -10% if the spread is wide). Mark is the midpoint and is
        # the standard reference for option PnL accounting. Fall back
        # to bid only if mark is unavailable; if neither, current is
        # unknown and we must NOT compare to entry.
        if mark > 0:
            current_premium = mark
        elif bid > 0:
            current_premium = bid
        else:
            current_premium = None

        entry_premium = 0.0
        try:
            # Prefer avg_fill_price when available (actual fill), fall
            # back to entry_price (the limit) otherwise.
            entry_premium = float(t.avg_fill_price or t.entry_price or 0.0)
        except (TypeError, ValueError):
            entry_premium = 0.0

        reason, abstained_implausible = _evaluate_exit_triggers(
            dte=_dte(expiration),
            entry_premium=entry_premium,
            current_premium=current_premium,
            dte_threshold=dte_threshold,
            stop_pct=stop_pct,
            tp_pct=tp_pct,
        )
        # f-options-exit-monitor-pattern-exit-now-audit (2026-05-06):
        # if no premium / DTE / stop trigger fired, consult the
        # pattern-monitor's latest advisory. Stop-on-tie ordering
        # matters: native triggers (price/DTE/premium) WIN over
        # exit_now because they carry stronger semantics for the
        # postmortem ("stop hit at $X" vs "LLM said so"). exit_now
        # is the fallback when the position would otherwise drift.
        #
        # f-fix-implausible-quote-vs-exit_now-ordering (2026-05-06):
        # parity with the crypto fix -- when _evaluate_exit_triggers
        # abstains because the quote is implausible, do NOT consult
        # the LLM advisory. The lane has just declared it does not
        # trust its own price feed for this trade; the LLM may be
        # reading a different (clean) feed and acting on its
        # recommendation while the engine itself disowns the price
        # is a different kind of foot-gun. Per no-hardcoded-fallback:
        # when inputs disagree, abstain.
        # f-exit-monitor-quote-guard-unification (2026-05-06): gate
        # routed through the shared
        # ``should_consult_monitor_after_refusal`` helper so all three
        # lanes use one trust-boundary definition.
        monitor_exit_meta: Optional[dict[str, Any]] = None
        if not reason and should_consult_monitor_after_refusal(
            reason, abstained_implausible=abstained_implausible
        ):
            monitor_exit_meta = fresh_monitor_exit_meta(
                latest_monitor_decisions.get(int(t.id))
            )
            if monitor_exit_meta is not None:
                reason = "pattern_exit_now"
        if not reason:
            continue
        summary["triggered"] += 1

        # Round-15 (2026-04-30): refuse to submit sell-to-close when no
        # real market data is available. The previous code had:
        #     limit_price = current_premium or mark or entry_premium
        # which fell back to ENTRY price when neither bid nor mark was
        # known -- that submits an exit at the buy price, ignoring any
        # actual move. Per the no-hardcoded-fallback rule: defer the
        # exit to the next pass rather than send a sell at entry.
        if bid > 0:
            # Sell-to-close at the bid (cross spread for clean fill).
            limit_price = bid
        elif mark > 0:
            # Use mark when bid is missing; less likely to fill quickly
            # but at least it's market-aware.
            limit_price = mark
        else:
            logger.warning(
                "[options_exit_monitor] trade=%s reason=%s but no real "
                "market data (bid=%s mark=%s); deferring exit -- will "
                "retry next pass.",
                t.id, reason, bid, mark,
            )
            summary["skipped_no_quote"] += 1
            continue
        exit_qty = _option_exit_contract_quantity(t)
        if exit_qty is None:
            logger.warning(
                "[options_exit_monitor] trade=%s reason=%s invalid contract "
                "quantity=%r; refusing to synthesize quantity=1",
                t.id,
                reason,
                getattr(t, "quantity", None),
            )
            summary["errors"] += 1
            continue
        try:
            res = adapter.place_option_sell(
                underlying=str(meta.get("underlying") or t.ticker),
                expiration=expiration,
                strike=float(strike),
                option_type=option_type,
                quantity=exit_qty,
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
        # Filled submit responses finalize now; active submit responses leave
        # broker-sync enough option-order context to finalize later.
        order_id = str(res.get("order_id") or "").strip()
        state = _option_exit_order_state(res)
        if not order_id:
            summary["errors"] += 1
            logger.warning(
                "[options_exit_monitor] trade=%s sell-to-close returned ok with no order_id",
                t.id,
            )
            continue

        now = datetime.now(timezone.utc)
        raw_order = _option_exit_raw_order(res, order_id=order_id, state=state)
        reference_price = current_premium or limit_price
        try:
            if _option_exit_submit_fill_is_complete(t, raw_order, state):
                from ..robinhood_exit_execution import _finalize_filled_exit

                t.pending_exit_order_id = order_id
                t.pending_exit_status = state
                t.pending_exit_requested_at = now.replace(tzinfo=None)
                t.pending_exit_reason = reason
                t.pending_exit_limit_price = float(limit_price)
                t.tca_reference_exit_price = float(reference_price)
                _finalize_filled_exit(
                    db,
                    t,
                    raw_order=raw_order,
                    exit_reason=reason,
                    fallback_price=float(limit_price),
                    filled_at=_parse_option_order_time(raw_order) or now,
                )
                summary["closed"] += 1
            else:
                db.execute(text(
                    """
                    UPDATE trading_trades
                    SET pending_exit_order_id = :oid,
                        pending_exit_status = :state,
                        pending_exit_requested_at = :ts,
                        pending_exit_reason = :rsn,
                        pending_exit_limit_price = :limit,
                        tca_reference_exit_price = :ref
                    WHERE id = :tid
                    """
                ), {
                    "oid": order_id,
                    "state": state or "submitted",
                    "ts": now,
                    "rsn": reason,
                    "limit": float(limit_price),
                    "ref": float(reference_price),
                    "tid": t.id,
                })
                db.commit()
                summary["working"] += 1
            if monitor_exit_meta is not None:
                # f-options-exit-monitor-pattern-exit-now-audit
                # (2026-05-06): monitor-driven exits log the audit
                # metadata (decision_id / source / age / price) so the
                # postmortem trail is complete. The pending_exit_reason
                # column stays canonical "pattern_exit_now" -- audit
                # detail belongs in the log line, not the 50-char field.
                logger.info(
                    "[options_exit_monitor] trade=%s exit_state=%s reason=%s "
                    "premium_now=%s entry=%s order_id=%s "
                    "monitor_decision_id=%s monitor_src=%s "
                    "monitor_age_h=%s monitor_price=%s",
                    t.id, state, reason, current_premium, entry_premium,
                    order_id,
                    monitor_exit_meta.get("decision_id"),
                    monitor_exit_meta.get("decision_source"),
                    monitor_exit_meta.get("decision_age_hours"),
                    monitor_exit_meta.get("decision_price"),
                )
            else:
                logger.info(
                    "[options_exit_monitor] trade=%s exit_state=%s reason=%s premium_now=%s "
                    "entry=%s order_id=%s",
                    t.id, state, reason, current_premium, entry_premium, order_id,
                )
        except Exception:
            summary["errors"] += 1
            logger.exception(
                "[options_exit_monitor] trade=%s pending_exit write failed", t.id,
            )

    return summary


__all__ = ["run_options_exit_pass"]
