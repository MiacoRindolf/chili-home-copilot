"""Guarded live automation runner (Phase 8) — spot adapter resolved by execution_family (Phase 11 seam).

Supported families: ``coinbase_spot``, ``robinhood_spot``; other families skip with ``execution_family_not_implemented``.

Snapshot contract:
- Never overwrite ``momentum_risk`` / admission keys.
- Mutable live execution state: ``risk_snapshot_json["momentum_live_execution"]`` only.
- Boundary checks each tick via ``evaluate_proposed_momentum_automation`` (mode=live).
"""

from __future__ import annotations

import logging
import math
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationSession
from ..execution_family_registry import (
    EXECUTION_FAMILY_COINBASE_SPOT,
    ExecutionFamilyNotImplementedError,
    normalize_execution_family,
    momentum_runner_supports_execution_family,
    resolve_live_spot_adapter_factory,
)
from ..autopilot_scope import (
    AUTOPILOT_MOMENTUM_NEURAL,
    check_autopilot_entry_gate,
)
from ..governance import is_kill_switch_active
from ..venue.protocol import NormalizedOrder, NormalizedProduct, is_fresh_enough
from .persistence import append_trading_automation_event
from ..decision_ledger import (
    finalize_packet_after_simulated_exit,
    mark_packet_executed,
    record_packet_execution_intent,
    run_momentum_entry_decision,
)
from ..deployment_ladder_service import record_trade_outcome_metrics
from .risk_evaluator import evaluate_proposed_momentum_automation
from .risk_policy import RISK_SNAPSHOT_KEY, compute_risk_first_quantity, policy_float_cap, policy_int_cap
from .paper_execution import (
    breakeven_stop_after_partial,
    effective_stop_atr_pct,
    regime_atr_pct,
    runner_trail_stop,
    scale_out_fraction,
    scale_out_quantity,
    stop_target_prices,
    structural_or_vol_floored_atr_pct,
    utc_iso,
)
from .persistence import variant_for_id
from .live_fsm import (
    LIVE_RUNNER_RUNNABLE_STATES,
    STATE_ARMED_PENDING_RUNNER,
    STATE_LIVE_BAILOUT,
    STATE_LIVE_COOLDOWN,
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_ERROR,
    STATE_LIVE_EXITED,
    STATE_LIVE_FINISHED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_LIVE_SCALING_OUT,
    STATE_LIVE_TRAILING,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    assert_transition_live,
)
from .session_lifecycle import is_operator_paused
from .strategy_params import normalize_strategy_params
from .entry_gates import breakout_failed_to_hold
from .tick_scalp import (
    TRIGGER_REASON as TICK_FIRST_PULLBACK_TRIGGER,
    evaluate_tick_first_pullback,
    expected_move_bps_from_ross_signal,
    ross_signal_for_symbol,
    ross_tick_scalp_evidence_ok,
)

_log = logging.getLogger(__name__)

KEY_LIVE_EXEC = "momentum_live_execution"

AdapterFactory = Callable[[], Any]


def _utcnow() -> datetime:
    return datetime.utcnow()


# ── Bounded momentum exit-submit retries (2026-06-07 audit) ───────────────
# A wedged exit (an unsellable-dust residual, a stale balance, a transient
# broker error) used to re-submit the flatten order on EVERY pulse — session
# 52 burned 1,500+ 'Insufficient balance' submits before being cancelled.
# Cap the broker submit RATE (exponential backoff between attempts) and the
# TOTAL attempts, then escalate to the broker-zero / dust reconcile so the
# wedged session clears itself (or surfaces a terminal error) instead of
# hammering the venue API. Settings-derived with documented defaults — one
# knob each, not scattered magic numbers.
_EXIT_SUBMIT_MAX_ATTEMPTS = int(
    getattr(settings, "chili_momentum_exit_submit_max_attempts", 8) or 8
)
_EXIT_SUBMIT_BACKOFF_BASE_SECONDS = float(
    getattr(settings, "chili_momentum_exit_submit_backoff_base_seconds", 5.0) or 5.0
)
_EXIT_SUBMIT_BACKOFF_MAX_SECONDS = float(
    getattr(settings, "chili_momentum_exit_submit_backoff_max_seconds", 300.0) or 300.0
)


def _exit_submit_backoff_seconds(attempts: int) -> float:
    """Exponential backoff (base * 2^(attempts-1)) capped at the max."""
    if attempts <= 0:
        return 0.0
    delay = _EXIT_SUBMIT_BACKOFF_BASE_SECONDS * (2.0 ** (attempts - 1))
    return min(delay, _EXIT_SUBMIT_BACKOFF_MAX_SECONDS)


def _policy_caps(snap: dict[str, Any]) -> dict[str, Any]:
    caps = snap.get("momentum_policy_caps")
    return caps if isinstance(caps, dict) else {}


def _live_exec(snap: dict[str, Any]) -> dict[str, Any]:
    le = snap.get(KEY_LIVE_EXEC)
    return dict(le) if isinstance(le, dict) else {}


def _commit_le(sess: TradingAutomationSession, le: dict[str, Any]) -> None:
    snap = dict(sess.risk_snapshot_json or {})
    snap[KEY_LIVE_EXEC] = le
    sess.risk_snapshot_json = snap
    # Force the JSON column dirty. When two commits happen in one tick around an
    # intervening flush (e.g. the scale-out: _apply_confirmed_live_partial_exit
    # flushes via its event emit, THEN the breakeven move mutates the same nested
    # position dict), the reassigned snapshot can compare EQUAL to the flush-pinned
    # baseline (shared nested refs) and SQLAlchemy skips the UPDATE — silently
    # losing the second mutation. flag_modified guarantees it persists.
    try:
        flag_modified(sess, "risk_snapshot_json")
    except Exception:
        # sess may be a lightweight test double (SimpleNamespace) with no ORM
        # instance state; the dirty-flag only matters for real mapped sessions.
        pass


def _emit(
    db: Session,
    sess: TradingAutomationSession,
    event_type: str,
    payload: dict[str, Any],
) -> None:
    append_trading_automation_event(
        db,
        sess.id,
        event_type,
        payload,
        correlation_id=sess.correlation_id,
        source_node_id="momentum_live_runner",
    )


def _finalize_live_decision_after_exit(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    realized_pnl_usd: float,
    slip_bps: float,
) -> None:
    pid = le.get("entry_decision_packet_id")
    if not pid:
        return
    try:
        finalize_packet_after_simulated_exit(
            db,
            packet_id=int(pid),
            realized_pnl_usd=realized_pnl_usd,
            slippage_bps=slip_bps,
        )
        record_trade_outcome_metrics(
            db,
            session_id=int(sess.id),
            variant_id=int(sess.variant_id),
            user_id=sess.user_id,
            mode="live",
            realized_pnl_usd=realized_pnl_usd,
            slippage_bps=slip_bps,
            missed_fill=False,
            partial_fill=False,
            cumulative_session_pnl_usd=float(le.get("realized_pnl_usd") or 0.0),
        )
    except Exception:
        _log.debug("live decision packet finalize skipped session=%s", sess.id, exc_info=True)


def _scan_pattern_id_for_session(db: Session, sess: TradingAutomationSession) -> int | None:
    try:
        variant = variant_for_id(db, int(sess.variant_id))
        sid = getattr(variant, "scan_pattern_id", None) if variant is not None else None
        return int(sid) if sid is not None else None
    except Exception:
        return None


def _record_live_entry_ledger_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    quantity: float,
    fill_price: float,
) -> None:
    try:
        from .. import economic_ledger as _ledger

        if not _ledger.mode_is_active():
            return
        _ledger.record_automation_session_entry_fill(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=_scan_pattern_id_for_session(db, sess),
            ticker=sess.symbol,
            quantity=quantity,
            fill_price=fill_price,
            fee=0.0,
            venue=sess.venue,
            mode="live",
            decision_packet_id=int(le["entry_decision_packet_id"]) if le.get("entry_decision_packet_id") else None,
            provenance={
                "runner": "momentum_live_runner",
                "entry_order_id": le.get("entry_order_id"),
                "entry_client_order_id": le.get("entry_client_order_id"),
            },
        )
    except Exception:
        _log.debug("live economic ledger entry hook skipped session=%s", sess.id, exc_info=True)


def _record_live_exit_ledger_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    quantity: float,
    entry_price: float,
    fill_price: float,
    realized_pnl_usd: float,
    reason: str,
) -> None:
    try:
        from .. import economic_ledger as _ledger

        if not _ledger.mode_is_active():
            return
        scan_pattern_id = _scan_pattern_id_for_session(db, sess)
        dpid = int(le["entry_decision_packet_id"]) if le.get("entry_decision_packet_id") else None
        _ledger.record_automation_session_exit_fill(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=scan_pattern_id,
            ticker=sess.symbol,
            quantity=quantity,
            fill_price=fill_price,
            entry_price=entry_price,
            realized_pnl_usd=realized_pnl_usd,
            fee=0.0,
            venue=sess.venue,
            mode="live",
            decision_packet_id=dpid,
            provenance={
                "runner": "momentum_live_runner",
                "reason": reason,
                "exit_order_id": le.get("exit_order_id"),
                "exit_client_order_id": le.get("exit_client_order_id"),
            },
        )
        _ledger.reconcile_automation_session(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=scan_pattern_id,
            ticker=sess.symbol,
            legacy_pnl=float(le.get("realized_pnl_usd") or realized_pnl_usd),
            mode="live",
            provenance={"runner": "momentum_live_runner", "reason": reason},
        )
    except Exception:
        _log.debug("live economic ledger exit hook skipped session=%s", sess.id, exc_info=True)


def _record_live_partial_exit_ledger_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    quantity: float,
    entry_price: float,
    fill_price: float,
    realized_pnl_usd: float,
    reason: str,
) -> None:
    try:
        from .. import economic_ledger as _ledger

        if not _ledger.mode_is_active():
            return
        scan_pattern_id = _scan_pattern_id_for_session(db, sess)
        dpid = int(le["entry_decision_packet_id"]) if le.get("entry_decision_packet_id") else None
        _ledger.record_automation_session_partial_exit_fill(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=scan_pattern_id,
            ticker=sess.symbol,
            quantity=quantity,
            fill_price=fill_price,
            entry_price=entry_price,
            realized_pnl_usd=realized_pnl_usd,
            fee=0.0,
            venue=sess.venue,
            mode="live",
            decision_packet_id=dpid,
            provenance={
                "runner": "momentum_live_runner",
                "reason": reason,
                "exit_order_id": le.get("exit_order_id"),
                "exit_client_order_id": le.get("exit_client_order_id"),
            },
        )
    except Exception:
        _log.debug("live economic ledger partial exit hook skipped session=%s", sess.id, exc_info=True)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


_PENDING_EXIT_TRACKING_KEYS = (
    "pending_exit_reason",
    "pending_exit_quantity",
    "pending_exit_submitted_at_utc",
    "pending_exit_queued_at_utc",
    "pending_exit_presubmit_deferred",
    "pending_exit_product_id",
    "pending_exit_client_order_id",
    "pending_exit_is_scale_out",
    "pending_exit_deferred_until_utc",
    "pending_exit_deferred_until",
    "pending_exit_market_session",
    "pending_exit_order_status",
    "pending_exit_filled_size",
)


def _clear_pending_exit_tracking(le: dict[str, Any], *, clear_order: bool = False) -> None:
    for key in _PENDING_EXIT_TRACKING_KEYS:
        le.pop(key, None)
    if clear_order:
        le.pop("exit_order_id", None)
        le.pop("exit_client_order_id", None)


def _record_live_exit_intent_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    reason: str,
    product_id: str,
    quantity: float,
    client_order_id: str | None,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    extra: dict[str, Any] | None = None,
) -> None:
    try:
        qty = _float_or_none(quantity)
        bid_f = _float_or_none(bid)
        ask_f = _float_or_none(ask)
        mid_f = _float_or_none(mid)
        spread_bps = None
        if bid_f is not None and ask_f is not None and mid_f and mid_f > 0:
            spread_bps = max(0.0, (ask_f - bid_f) / mid_f * 10_000.0)
        pos = le.get("position")
        pos = pos if isinstance(pos, dict) else {}
        ref_px = bid_f if bid_f is not None else mid_f
        intent: dict[str, Any] = {
            "surface": "momentum_live_runner_exit",
            "session_id": int(sess.id),
            "state": sess.state,
            "side": "sell",
            "order_type": "market",
            "reason": reason,
            "product_id": product_id,
            "quantity": qty,
            "base_size": _fmt_base_size(qty) if qty and qty > 0 else None,
            "client_order_id": client_order_id,
            "bid": bid_f,
            "ask": ask_f,
            "mid": mid_f,
            "spread_bps": spread_bps,
            "reference_notional_usd": (qty * ref_px) if qty is not None and ref_px is not None else None,
            "avg_entry_price": _float_or_none(pos.get("avg_entry_price")),
            "stop_price": _float_or_none(pos.get("stop_price")),
            "target_price": _float_or_none(pos.get("target_price")),
            "opened_at_utc": pos.get("opened_at_utc"),
            "recorded_at_utc": _utcnow().isoformat(),
        }
        if extra:
            intent.update(dict(extra))
        intents = list(le.get("exit_execution_intents") or [])
        intents.append(intent)
        le["exit_execution_intents"] = intents[-10:]
        le["last_exit_intent"] = intent
        packet_id = int(le["entry_decision_packet_id"]) if le.get("entry_decision_packet_id") else None
        record_packet_execution_intent(db, packet_id, intent)
    except Exception:
        _log.debug("live exit intent hook skipped session=%s reason=%s", sess.id, reason, exc_info=True)


def _defer_live_exit_submit_until_tradable(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    product_id: str,
    quantity: float,
    client_order_id: str,
    reason: str,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    market: dict[str, Any],
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utcnow()
    qty = float(quantity)
    existing_order_id = le.get("exit_order_id")
    broker_status = "existing_order_unpolled" if existing_order_id else "not_submitted"
    lifecycle = "queued_deferred" if existing_order_id else "presubmit_deferred"
    why = "exit_existing_order_deferred_non_tradable" if existing_order_id else "exit_submit_deferred_non_tradable"
    intent_extra = dict(extra or {})
    intent_extra.update(
        {
            "why": why,
            "exit_lifecycle_state": lifecycle,
            "market_session": market.get("market_session"),
            "deferred_until_utc": market.get("deferred_until_utc"),
            "broker_submit_deferred": True,
        }
    )
    _record_live_exit_intent_safe(
        db,
        sess,
        le=le,
        reason=reason,
        product_id=product_id,
        quantity=qty,
        client_order_id=client_order_id,
        bid=bid,
        ask=ask,
        mid=mid,
        extra=intent_extra,
    )
    payload: dict[str, Any] = {
        "reason": reason,
        "order_id": existing_order_id,
        "client_order_id": le.get("exit_client_order_id") or client_order_id,
        "why": why,
        "exit_lifecycle_state": lifecycle,
        "market_session": market.get("market_session"),
        "deferred_until_utc": market.get("deferred_until_utc"),
        "deferred_until": market.get("deferred_until_utc"),
        "expected_quantity": qty,
        "filled_size": 0.0,
        "recorded_at_utc": now.isoformat(),
        "symbol": getattr(sess, "symbol", None),
        "product_id": product_id,
        "order_status": broker_status,
        "broker_order_status": broker_status,
    }
    le["pending_exit_reason"] = reason
    le["pending_exit_quantity"] = qty
    le["pending_exit_market_session"] = payload["market_session"]
    le["pending_exit_order_status"] = broker_status
    le["pending_exit_filled_size"] = 0.0
    le["pending_exit_deferred_until_utc"] = payload["deferred_until_utc"]
    le["pending_exit_deferred_until"] = payload["deferred_until"]
    if existing_order_id:
        le["pending_exit_submitted_at_utc"] = le.get("pending_exit_submitted_at_utc") or now.isoformat()
        le.pop("pending_exit_presubmit_deferred", None)
        le.pop("pending_exit_product_id", None)
        le.pop("pending_exit_client_order_id", None)
    else:
        le.pop("exit_order_id", None)
        le["exit_client_order_id"] = client_order_id
        le["pending_exit_presubmit_deferred"] = True
        le["pending_exit_product_id"] = product_id
        le["pending_exit_client_order_id"] = client_order_id
        le["pending_exit_queued_at_utc"] = le.get("pending_exit_queued_at_utc") or now.isoformat()
    le.pop("last_exit_submit_failed", None)
    le["exit_place_result"] = {"ok": False, "error": why, "deferred": True}
    should_emit = _should_emit_exit_deferred(le, payload)
    le["last_exit_deferred"] = payload
    le["last_exit_pending_confirmation"] = payload
    _commit_le(sess, le)
    if should_emit:
        _emit(db, sess, "live_exit_queued_deferred", payload)
    return {"ok": False, "error": why, "deferred": True, **payload}


def _submit_live_market_exit(
    db: Session,
    sess: TradingAutomationSession,
    adapter: Any,
    *,
    le: dict[str, Any],
    product_id: str,
    quantity: float,
    client_order_id: str,
    reason: str,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    now = _utcnow()
    attempts = int(le.get("exit_submit_attempts", 0) or 0)

    # Backoff gate — do NOT place another broker order until the scheduled
    # retry time. Returns a synthetic deferred result (no broker call, no
    # attempt increment) so the caller stays in its exit state and retries
    # on a later pulse without hammering the venue API.
    next_retry_raw = le.get("exit_next_retry_at_utc")
    if next_retry_raw:
        try:
            next_retry = datetime.fromisoformat(
                str(next_retry_raw).replace("Z", "+00:00")
            ).replace(tzinfo=None)
            if now < next_retry:
                return {
                    "ok": False,
                    "error": "exit_retry_backoff",
                    "deferred": True,
                    "retry_at_utc": next_retry.isoformat(),
                    "attempts": attempts,
                }
        except Exception:
            pass

    # Max-attempts cap — stop submitting and signal escalation to the
    # broker-zero / dust reconcile (handled in _live_exit_submit_succeeded).
    if attempts >= _EXIT_SUBMIT_MAX_ATTEMPTS:
        return {
            "ok": False,
            "error": "exit_retry_cap_exceeded",
            "cap_exceeded": True,
            "attempts": attempts,
        }

    market = _exit_market_window(getattr(sess, "symbol", None))
    if not bool(market.get("is_tradable")):
        return _defer_live_exit_submit_until_tradable(
            db,
            sess,
            le=le,
            product_id=product_id,
            quantity=quantity,
            client_order_id=client_order_id,
            reason=reason,
            bid=bid,
            ask=ask,
            mid=mid,
            market=market,
            extra=extra,
        )

    _record_live_exit_intent_safe(
        db,
        sess,
        le=le,
        reason=reason,
        product_id=product_id,
        quantity=quantity,
        client_order_id=client_order_id,
        bid=bid,
        ask=ask,
        mid=mid,
        extra=extra,
    )
    attempts += 1
    le["exit_submit_attempts"] = attempts
    le["exit_next_retry_at_utc"] = (
        now + timedelta(seconds=_exit_submit_backoff_seconds(attempts))
    ).isoformat()
    result = adapter.place_market_order(
        product_id=product_id,
        side="sell",
        base_size=_fmt_base_size(quantity),
        client_order_id=client_order_id,
    ) or {}
    le["exit_order_id"] = result.get("order_id")
    le["exit_client_order_id"] = result.get("client_order_id") or client_order_id
    le["exit_place_result"] = {"ok": result.get("ok"), "error": result.get("error")}
    if result.get("ok"):
        _clear_pending_exit_tracking(le)
        le["pending_exit_reason"] = reason
        le["pending_exit_quantity"] = float(quantity)
        le["pending_exit_submitted_at_utc"] = now.isoformat()
        # Accepted by the broker — reset the retry state so a later,
        # independent exit (e.g. re-exit of a remainder) starts fresh.
        le["exit_submit_attempts"] = 0
        le.pop("exit_next_retry_at_utc", None)
    # Persist the counter/backoff state so it survives across pulses (the
    # caller's flush/commit writes sess.risk_snapshot_json to the DB).
    _commit_le(sess, le)
    return result


def _is_unsellable_dust(symbol: str, qty: float) -> bool:
    """True when `qty` of `symbol`'s base asset is below what Coinbase will accept as
    a SELL — below the product base_min_size, OR whose notional (qty x price) is below
    quote_min_size (typically $1). Such a residual can never be flattened, so for
    reconcile purposes it is effectively ZERO: leaving it 'live' makes the exit loop
    re-submit doomed sells forever ('Insufficient balance'). Conservative — any failure
    to determine the venue minimums returns False so a real, sellable position is never
    false-reconciled. (This is the dust that wedged session 52 / CTSI: 3.65 units ~=
    $0.09, below the $1 quote_min but above the strict ~0 check.)"""
    try:
        if not qty or float(qty) <= 0.0:
            return True
        from ...coinbase_service import get_coinbase_rest_client

        client = get_coinbase_rest_client()
        if client is None:
            return False
        sym = str(symbol or "").upper()
        product_id = sym if "-" in sym else f"{sym}-USD"
        prod = client.get_product(product_id)
        pd = prod if isinstance(prod, dict) else (getattr(prod, "__dict__", {}) or {})
        base_min = float(pd.get("base_min_size") or 0.0)
        quote_min = float(pd.get("quote_min_size") or 0.0)
        price = float(pd.get("price") or 0.0)
        if base_min > 0.0 and float(qty) < base_min:
            return True  # below the venue's minimum sellable size
        if quote_min > 0.0 and price > 0.0 and (float(qty) * price) < quote_min:
            return True  # notional below the venue's minimum order value
        return False
    except Exception:
        return False


def _broker_balance_confirms_zero(symbol: str) -> bool:
    """True when a SUCCESSFUL Coinbase fetch shows the symbol's base asset is ~0 OR an
    UNSELLABLE-DUST residual (below the venue's min sell size / notional). A
    failed/disconnected fetch returns False so it never triggers a false reconcile
    (mirrors the M5a safe-fetch rule). (crypto/coinbase only)

    The strict ~0 (1e-9) check alone was DEFEATED by dust: a position sold down to a
    fractional remainder the venue rejects as an order (CTSI 3.65 units = $0.09 < $1
    quote_min) left the exit loop re-submitting doomed sells forever. Dust IS
    effectively zero for reconcile purposes."""
    try:
        from ...coinbase_service import get_accounts_raw

        accts = get_accounts_raw()
        if not accts:
            return False  # disconnected / fetch failed -> unknown, do NOT reconcile
        base = str(symbol or "").upper().split("-", 1)[0]
        for a in accts:
            if not isinstance(a, dict):
                continue
            if str(a.get("currency") or "").upper() != base:
                continue
            bal = a.get("available_balance", {})
            hold = a.get("hold", {})
            v = (
                float((bal.get("value") if isinstance(bal, dict) else 0) or 0)
                + float((hold.get("value") if isinstance(hold, dict) else 0) or 0)
            )
            if v <= 1e-9:
                return True
            return _is_unsellable_dust(symbol, v)  # non-zero but unsellable dust == zero
        return True  # base wallet absent in a successful fetch -> confirmed zero
    except Exception:
        return False


def _live_exit_submit_succeeded(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    result: dict[str, Any],
    reason: str,
) -> bool:
    # Backoff deferral — no broker order was placed this pulse (rate-limited
    # by _submit_live_market_exit). Not a real failure: stay in the exit
    # state and retry after the backoff window WITHOUT recording a failure
    # or emitting an event (avoids the per-pulse event spam that itself was
    # part of the wedged-session problem). (2026-06-07 audit.)
    if result.get("deferred"):
        return False

    # Max-attempts cap reached — stop re-submitting and escalate. If the
    # broker is flat (or only unsellable dust remains) the position already
    # left, so reconcile to EXITED; otherwise a real sellable position keeps
    # failing to flatten for a non-balance reason → surface a terminal error
    # for operator attention instead of looping forever.
    if result.get("cap_exceeded"):
        _emit(
            db, sess, "live_exit_retry_cap_exceeded",
            {
                "reason": reason,
                "attempts": result.get("attempts"),
                "max_attempts": _EXIT_SUBMIT_MAX_ATTEMPTS,
            },
        )
        if (
            normalize_execution_family(sess.execution_family) == EXECUTION_FAMILY_COINBASE_SPOT
            and _broker_balance_confirms_zero(sess.symbol)
        ):
            le["position"] = None
            le["last_exit_reason"] = (reason or "exit") + "_retry_cap_broker_zero_reconcile"
            _clear_pending_exit_tracking(le)
            le["exit_submit_attempts"] = 0
            le.pop("exit_next_retry_at_utc", None)
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_EXITED)
            _emit(
                db, sess, "live_exit_reconciled_broker_zero",
                {
                    "reason": reason,
                    "note": "exit retry cap reached and broker holds 0/dust — reconciled to exited",
                },
            )
            return True
        le["last_exit_submit_failed"] = {
            "reason": reason,
            "error": "exit_retry_cap_exceeded",
            "attempts": result.get("attempts"),
            "recorded_at_utc": _utcnow().isoformat(),
        }
        _clear_pending_exit_tracking(le)
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        return False

    if result.get("ok") and le.get("exit_order_id"):
        return True
    missing_order_id = bool(result.get("ok")) and not le.get("exit_order_id")
    # BUGFIX: an exit/bailout sell that fails with "insufficient balance" while the
    # broker CONFIRMS zero means the position already left (sold externally / a
    # prior fill we missed) — retrying loops forever on insufficient balance and
    # pins the slot. Reconcile to EXITED instead of spinning. (coinbase only;
    # confirmed-zero only — never on a failed balance fetch.)
    _err = str(result.get("error") or "").lower()
    if (
        ("insufficient balance" in _err or "insufficient_balance" in _err)
        and normalize_execution_family(sess.execution_family) == EXECUTION_FAMILY_COINBASE_SPOT
        and _broker_balance_confirms_zero(sess.symbol)
    ):
        le["position"] = None
        le["last_exit_reason"] = (reason or "exit") + "_broker_zero_reconcile"
        _clear_pending_exit_tracking(le)
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_EXITED)
        _emit(
            db, sess, "live_exit_reconciled_broker_zero",
            {"reason": reason, "note": "broker holds 0 — position already exited externally; not retrying"},
        )
        return True
    if _exit_submit_requires_operator_action(result):
        failed = _exit_operator_action_payload(sess, le=le, result=result, reason=reason)
        le["last_exit_operator_action_required"] = failed
        le["last_exit_submit_failed"] = failed
        _clear_pending_exit_tracking(le)
        _commit_le(sess, le)
        _emit(db, sess, "live_exit_operator_action_required", failed)
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        return False
    failed = {
        "reason": reason,
        "result": {
            "ok": result.get("ok"),
            "error": result.get("error") or ("missing_exit_order_id" if missing_order_id else None),
        },
        "exit_client_order_id": le.get("exit_client_order_id"),
        "recorded_at_utc": _utcnow().isoformat(),
    }
    le["last_exit_submit_failed"] = failed
    _clear_pending_exit_tracking(le)
    _commit_le(sess, le)
    _emit(db, sess, "live_exit_submit_failed", failed)
    return False


def _exit_submit_requires_operator_action(result: dict[str, Any]) -> bool:
    err = str((result or {}).get("error") or "").lower()
    if not err:
        return False
    return any(
        token in err
        for token in (
            "unauthorized",
            "not authenticated",
            "authentication",
            "missing/expired",
            "expired token",
            "token missing",
            "token expired",
            "no_token",
            "no token",
            "oauth",
            "re-auth",
            "reauth",
        )
    )


def _exit_operator_action_payload(
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    result: dict[str, Any],
    reason: str,
) -> dict[str, Any]:
    pos = le.get("position")
    return {
        "reason": reason,
        "why": "broker_auth_required",
        "action_required": "broker_reauth_or_manual_flatten",
        "error": (result or {}).get("error"),
        "result": {"ok": (result or {}).get("ok"), "error": (result or {}).get("error")},
        "exit_client_order_id": le.get("exit_client_order_id"),
        "exit_order_id": le.get("exit_order_id"),
        "position_open": isinstance(pos, dict) and float(pos.get("quantity") or 0.0) > 0.0,
        "quantity": (pos or {}).get("quantity") if isinstance(pos, dict) else None,
        "symbol": getattr(sess, "symbol", None),
        "recorded_at_utc": _utcnow().isoformat(),
    }


_EXIT_ORDER_FILLED_STATUSES = {"filled", "done", "closed", "complete", "completed"}
_EXIT_ORDER_TERMINAL_STATUSES = {
    "filled",
    "done",
    "closed",
    "complete",
    "completed",
    "cancelled",
    "canceled",
    "expired",
    "failed",
    "rejected",
}
_EXIT_ORDER_TERMINAL_NO_FILL_STATUSES = {
    "cancelled",
    "canceled",
    "expired",
    "failed",
    "rejected",
}
_EXIT_ORDER_QUEUED_STATUSES = {
    "queued",
    "unconfirmed",
    "confirmed",
    "held",
    "non_tradable",
    "not_tradable",
    "market_closed",
    "scheduled",
}
_EXIT_ORDER_ACTIVE_STATUSES = {
    "ack",
    "acknowledged",
    "accepted",
    "active",
    "new",
    "open",
    "pending",
    "submitted",
    "working",
    "partially_filled",
    "partial",
    "partial_filled",
}


def _exit_order_status(no: NormalizedOrder) -> str:
    raw = no.raw if isinstance(getattr(no, "raw", None), dict) else {}
    for key in ("state", "status", "order_state", "order_status"):
        value = raw.get(key)
        if value not in (None, ""):
            return str(value).strip().lower()
    return str(no.status or "").strip().lower()


def _exit_order_status_payload(no: NormalizedOrder) -> dict[str, Any]:
    raw = no.raw if isinstance(getattr(no, "raw", None), dict) else {}
    return {
        "order_status": no.status,
        "broker_order_status": _exit_order_status(no),
        "raw_order_state": raw.get("state"),
        "raw_order_status": raw.get("status"),
    }


def _exit_market_window(symbol: str | None) -> dict[str, Any]:
    if not str(symbol or "").strip():
        return {
            "asset_class": "unknown",
            "market_session": "unknown",
            "is_tradable": True,
            "deferred_until_utc": None,
        }
    try:
        from .market_profile import market_session_for_symbol

        return dict(
            market_session_for_symbol(
                symbol,
                now=_utcnow(),
                allow_extended_hours=bool(getattr(settings, "chili_autotrader_allow_extended_hours", False)),
            )
        )
    except Exception:
        return {
            "asset_class": "unknown",
            "market_session": "unknown",
            "is_tradable": True,
            "deferred_until_utc": None,
        }


def _exit_order_deferred_by_market(no: NormalizedOrder, market: dict[str, Any], *, filled_size: float) -> bool:
    if bool(market.get("is_tradable")) or filled_size > 1e-12:
        return False
    st = _exit_order_status(no)
    normalized = str(no.status or "").strip().lower()
    return st in _EXIT_ORDER_QUEUED_STATUSES or normalized in _EXIT_ORDER_ACTIVE_STATUSES


def _deferred_exit_payload(
    *,
    sess: TradingAutomationSession,
    no: NormalizedOrder,
    reason: str,
    quantity: float,
    filled_size: float,
    market: dict[str, Any],
) -> dict[str, Any]:
    deferred_until = market.get("deferred_until_utc")
    payload: dict[str, Any] = {
        "reason": reason,
        "order_id": no.order_id,
        "why": "exit_queued_non_tradable",
        "exit_lifecycle_state": "queued_deferred",
        "market_session": market.get("market_session"),
        "deferred_until_utc": deferred_until,
        "deferred_until": deferred_until,
        "expected_quantity": float(quantity),
        "filled_size": filled_size,
        "recorded_at_utc": _utcnow().isoformat(),
        "symbol": getattr(sess, "symbol", None),
    }
    payload.update(_exit_order_status_payload(no))
    return payload


def _should_emit_exit_deferred(le: dict[str, Any], payload: dict[str, Any]) -> bool:
    prior = le.get("last_exit_deferred")
    if not isinstance(prior, dict):
        return True
    keys = (
        "order_id",
        "broker_order_status",
        "market_session",
        "deferred_until_utc",
        "expected_quantity",
        "filled_size",
    )
    return any(prior.get(key) != payload.get(key) for key in keys)


def _order_done_for_exit(no: NormalizedOrder) -> bool:
    st = _exit_order_status(no)
    if st in _EXIT_ORDER_FILLED_STATUSES:
        return float(no.filled_size or 0.0) > 1e-12 and no.average_filled_price is not None
    if no.filled_size > 1e-12:
        return st in ("cancelled", "canceled", "expired", "failed", "rejected")
    return False


def _order_terminal_without_exit_fill(no: NormalizedOrder) -> bool:
    st = _exit_order_status(no)
    if st in _EXIT_ORDER_TERMINAL_NO_FILL_STATUSES:
        return float(no.filled_size or 0.0) <= 1e-12
    if st in _EXIT_ORDER_FILLED_STATUSES:
        return float(no.filled_size or 0.0) <= 1e-12 or no.average_filled_price is None
    return False


def _poll_live_exit_fill(
    db: Session,
    sess: TradingAutomationSession,
    adapter: Any,
    *,
    le: dict[str, Any],
    reason: str,
    quantity: float,
) -> dict[str, Any]:
    oid = le.get("exit_order_id")
    if not oid:
        _emit(db, sess, "live_exit_pending_unconfirmed", {"reason": reason, "why": "missing_exit_order_id"})
        return {"filled": False, "pending": True, "why": "missing_exit_order_id"}
    market = _exit_market_window(getattr(sess, "symbol", None))
    if not bool(market.get("is_tradable")) and le.get("pending_exit_deferred_until_utc"):
        filled_size = _float_or_none(le.get("pending_exit_filled_size")) or 0.0
        if filled_size <= 1e-12:
            deferred = {
                "reason": reason,
                "order_id": oid,
                "why": (
                    (le.get("last_exit_deferred") or {}).get("why")
                    if isinstance(le.get("last_exit_deferred"), dict)
                    else None
                ) or "exit_existing_order_deferred_non_tradable",
                "exit_lifecycle_state": "queued_deferred",
                "market_session": market.get("market_session"),
                "deferred_until_utc": market.get("deferred_until_utc"),
                "deferred_until": market.get("deferred_until_utc"),
                "expected_quantity": float(quantity),
                "filled_size": 0.0,
                "recorded_at_utc": _utcnow().isoformat(),
                "symbol": getattr(sess, "symbol", None),
                "order_status": le.get("pending_exit_order_status") or "deferred_not_polled",
                "broker_order_status": le.get("pending_exit_order_status") or "deferred_not_polled",
            }
            le["pending_exit_deferred_until_utc"] = deferred.get("deferred_until_utc")
            le["pending_exit_deferred_until"] = deferred.get("deferred_until")
            le["pending_exit_market_session"] = deferred.get("market_session")
            le["pending_exit_filled_size"] = 0.0
            should_emit = _should_emit_exit_deferred(le, deferred)
            le["last_exit_deferred"] = deferred
            le["last_exit_pending_confirmation"] = deferred
            _commit_le(sess, le)
            if should_emit:
                _emit(db, sess, "live_exit_queued_deferred", deferred)
            return {"filled": False, "pending": True, "deferred": True, **deferred}
    try:
        no, _ = adapter.get_order(str(oid))
    except Exception:
        _log.debug("live exit order poll failed session=%s order_id=%s", sess.id, oid, exc_info=True)
        no = None
    if no is None:
        _emit(db, sess, "live_exit_pending_unconfirmed", {"reason": reason, "order_id": oid, "why": "order_missing"})
        return {"filled": False, "pending": True, "why": "order_missing"}

    filled_size = float(no.filled_size or 0.0)
    avg_px = _float_or_none(no.average_filled_price)
    full_fill = _order_done_for_exit(no) and avg_px is not None and filled_size + 1e-12 >= float(quantity) * 0.999
    if full_fill:
        out = {"filled": True, "fill_price": avg_px, "filled_size": filled_size, "order_status": no.status}
        out.update(_exit_order_status_payload(no))
        return out

    terminal_status = _exit_order_status(no) in _EXIT_ORDER_TERMINAL_STATUSES
    if terminal_status and filled_size > 1e-12 and avg_px is not None:
        out = {
            "filled": False,
            "partial": True,
            "fill_price": avg_px,
            "filled_size": filled_size,
            "order_status": no.status,
        }
        out.update(_exit_order_status_payload(no))
        return out

    if _order_terminal_without_exit_fill(no):
        failed = {
            "reason": reason,
            "order_id": oid,
            "why": "terminal_no_fill",
            "exit_lifecycle_state": "terminal_no_fill_retry_ready",
            "filled_size": filled_size,
            "expected_quantity": float(quantity),
            "market_session": market.get("market_session"),
            "recorded_at_utc": _utcnow().isoformat(),
        }
        failed.update(_exit_order_status_payload(no))
        le["last_exit_terminal_no_fill"] = failed
        _clear_pending_exit_tracking(le, clear_order=True)
        _commit_le(sess, le)
        _emit(db, sess, "live_exit_terminal_no_fill", failed)
        return {"filled": False, "failed": True, **failed}

    if _exit_order_deferred_by_market(no, market, filled_size=filled_size):
        deferred = _deferred_exit_payload(
            sess=sess,
            no=no,
            reason=reason,
            quantity=quantity,
            filled_size=filled_size,
            market=market,
        )
        le["pending_exit_deferred_until_utc"] = deferred.get("deferred_until_utc")
        le["pending_exit_deferred_until"] = deferred.get("deferred_until")
        le["pending_exit_market_session"] = deferred.get("market_session")
        le["pending_exit_order_status"] = deferred.get("broker_order_status")
        le["pending_exit_filled_size"] = filled_size
        should_emit = _should_emit_exit_deferred(le, deferred)
        le["last_exit_deferred"] = deferred
        le["last_exit_pending_confirmation"] = deferred
        _commit_le(sess, le)
        if should_emit:
            _emit(db, sess, "live_exit_queued_deferred", deferred)
        return {"filled": False, "pending": True, "deferred": True, **deferred}

    was_deferred = any(
        key in le
        for key in (
            "pending_exit_deferred_until_utc",
            "pending_exit_deferred_until",
            "pending_exit_market_session",
            "pending_exit_order_status",
        )
    )
    if was_deferred:
        adopted = {
            "reason": reason,
            "order_id": oid,
            "why": "exit_deferred_order_active",
            "exit_lifecycle_state": "active_pending_after_defer",
            "market_session": market.get("market_session"),
            "expected_quantity": float(quantity),
            "filled_size": filled_size,
            "recorded_at_utc": _utcnow().isoformat(),
        }
        adopted.update(_exit_order_status_payload(no))
        le["last_exit_deferred_adopted"] = adopted
        for key in (
            "pending_exit_deferred_until_utc",
            "pending_exit_deferred_until",
            "pending_exit_market_session",
            "pending_exit_order_status",
            "pending_exit_filled_size",
        ):
            le.pop(key, None)
        _commit_le(sess, le)
        _emit(db, sess, "live_exit_deferred_adopted", adopted)

    pending = {
        "reason": reason,
        "order_id": oid,
        "filled_size": filled_size,
        "expected_quantity": float(quantity),
        "market_session": market.get("market_session"),
        "deferred_until_utc": market.get("deferred_until_utc"),
        "deferred_until": market.get("deferred_until_utc"),
        "recorded_at_utc": _utcnow().isoformat(),
        "exit_lifecycle_state": "active_pending",
    }
    pending.update(_exit_order_status_payload(no))
    if filled_size > 1e-12:
        pending["why"] = "partial_exit_fill_pending"
        pending["exit_lifecycle_state"] = "active_partial_pending"
        if avg_px is not None:
            pending["average_filled_price"] = avg_px
    else:
        pending["why"] = "exit_fill_pending"
    le["last_exit_pending_confirmation"] = pending
    _commit_le(sess, le)
    _emit(db, sess, "live_exit_pending_confirmation", pending)
    return {"filled": False, "pending": True, **pending}


def _complete_confirmed_live_exit(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    quantity: float,
    entry_price: float,
    fill_price: float,
    reason: str,
    slip_bps: float,
    sell_result: dict[str, Any] | None = None,
) -> float:
    pnl = (float(fill_price) - float(entry_price)) * float(quantity)
    notional_basis = abs(float(entry_price) * float(quantity))
    le["realized_pnl_usd"] = float(le.get("realized_pnl_usd") or 0.0) + pnl
    le["last_exit_price"] = float(fill_price)
    le["last_exit_entry_price"] = float(entry_price)
    le["last_exit_quantity"] = float(quantity)
    le["last_exit_notional_basis_usd"] = notional_basis
    le["last_exit_return_bps"] = (pnl / notional_basis) * 10_000.0 if notional_basis > 1e-12 else None
    _record_live_exit_ledger_safe(
        db,
        sess,
        le=le,
        quantity=float(quantity),
        entry_price=float(entry_price),
        fill_price=float(fill_price),
        realized_pnl_usd=pnl,
        reason=reason,
    )
    _finalize_live_decision_after_exit(db, sess, le=le, realized_pnl_usd=pnl, slip_bps=slip_bps)
    le["last_exit_reason"] = reason
    # Shake-out learning: stash the inputs (incl. the REAL momentum stop/target,
    # still on the position here) so a deferred job can judge whether the thesis
    # worked AFTER we exited — was the stop too tight? — instead of the learner
    # seeing a shallow loss. (post_exit_excursion.py; docs/DESIGN/MOMENTUM_LANE.md)
    _exit_pos = le.get("position") if isinstance(le.get("position"), dict) else {}
    le["post_exit_excursion_pending"] = {
        "symbol": sess.symbol,
        "entry_price": float(entry_price),
        "exit_price": float(fill_price),
        "original_stop": _exit_pos.get("stop_price"),
        "original_target": _exit_pos.get("target_price"),
        "side_long": True,
        "exit_reason": reason,
        "realized_pnl": pnl,
        "exit_time_utc": _utcnow().isoformat(),
        "horizon_seconds": int(getattr(settings, "chili_momentum_post_exit_horizon_seconds", 1800) or 1800),
        "state": "pending",
    }
    le["position"] = None
    _clear_pending_exit_tracking(le)
    _commit_le(sess, le)
    _safe_transition(db, sess, STATE_LIVE_EXITED)
    payload = {"reason": reason, "pnl_usd": pnl, "fill_price": float(fill_price)}
    if sell_result is not None:
        payload["sell_result"] = sell_result
    _emit(db, sess, "live_exit_filled", payload)
    return pnl


def _apply_confirmed_live_partial_exit(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    filled_quantity: float,
    entry_price: float,
    fill_price: float,
    reason: str,
) -> float:
    pos = le.get("position")
    pos = dict(pos) if isinstance(pos, dict) else {}
    current_qty = _float_or_none(pos.get("quantity")) or 0.0
    qty = min(max(float(filled_quantity), 0.0), current_qty)
    pnl = (float(fill_price) - float(entry_price)) * qty
    notional_basis = abs(float(entry_price) * qty)
    remaining = max(0.0, current_qty - qty)
    le["realized_pnl_usd"] = float(le.get("realized_pnl_usd") or 0.0) + pnl
    le["last_partial_exit_price"] = float(fill_price)
    le["last_partial_exit_reason"] = reason
    le["last_partial_exit_quantity"] = qty
    le["last_partial_exit_notional_basis_usd"] = notional_basis
    le["last_partial_exit_return_bps"] = (pnl / notional_basis) * 10_000.0 if notional_basis > 1e-12 else None
    pos["quantity"] = remaining
    pos["partial_taken"] = True
    le["position"] = pos
    _clear_pending_exit_tracking(le)
    _record_live_partial_exit_ledger_safe(
        db,
        sess,
        le=le,
        quantity=qty,
        entry_price=float(entry_price),
        fill_price=float(fill_price),
        realized_pnl_usd=pnl,
        reason=reason,
    )
    _commit_le(sess, le)
    _emit(
        db,
        sess,
        "live_partial_exit_filled",
        {"reason": reason, "qty": qty, "remain": remaining, "pnl_usd": pnl, "fill_price": float(fill_price)},
    )
    return pnl


def _scale_out_to_runner(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    filled_quantity: float,
    entry_price: float,
    fill_price: float,
    reason: str,
) -> float:
    """Ross first-target scale-out: bank the partial, move the BALANCE stop to
    breakeven, and HOLD the remainder as the runner (transition to TRAILING).

    Reuses ``_apply_confirmed_live_partial_exit`` for the partial bookkeeping/ledger,
    then ratchets the runner's stop to entry ("adjust my stop to my entry price on
    the balance"), clears the scale-out pending markers, and arms TRAILING so the
    chandelier trail (above) carries the runner up for the tail. The breakeven move
    is derived (= entry); the trail is derived (frozen entry ATR). One knob total:
    the scale-out fraction. (docs/DESIGN/MOMENTUM_LANE.md)"""
    pnl = _apply_confirmed_live_partial_exit(
        db,
        sess,
        le=le,
        filled_quantity=filled_quantity,
        entry_price=entry_price,
        fill_price=fill_price,
        reason=reason,
    )
    le.pop("pending_exit_is_scale_out", None)
    pos = le.get("position")
    if isinstance(pos, dict):
        old_stop = _float_or_none(pos.get("stop_price"))
        be_stop = breakeven_stop_after_partial(
            float(entry_price),
            float(old_stop if old_stop is not None else entry_price),
            side_long=True,
        )
        pos["stop_price"] = be_stop
        pos["scaled_out_at_utc"] = _utcnow().isoformat()
        pos["scale_out_fraction"] = scale_out_fraction()
        le["position"] = pos
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_TRAILING)
        _emit(
            db,
            sess,
            "live_scaled_out_to_runner",
            {
                "reason": reason,
                "partial_qty": float(filled_quantity),
                "runner_qty": _float_or_none(pos.get("quantity")),
                "breakeven_stop": be_stop,
                "partial_pnl_usd": pnl,
            },
        )
    return pnl


def _safe_transition(db: Session, sess: TradingAutomationSession, new_state: str) -> None:
    old = sess.state
    if old == new_state:
        return
    assert_transition_live(old, new_state)
    sess.state = new_state
    sess.updated_at = _utcnow()
    from .feedback_emit import emit_feedback_after_terminal_transition
    from .outcome_extract import session_terminal_for_feedback

    if session_terminal_for_feedback(sess.mode or "live", new_state):
        emit_feedback_after_terminal_transition(db, sess)


def runner_boundary_risk_ok(
    db: Session,
    sess: TradingAutomationSession,
    *,
    expected_move_bps: float | None = None,
) -> tuple[bool, dict[str, Any]]:
    if sess.user_id is None:
        return False, {"reason": "no_user"}
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(sess.user_id),
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        mode="live",
        execution_family=normalize_execution_family(sess.execution_family),
        exclude_session_id=int(sess.id),
        expected_move_bps=expected_move_bps,
    )
    return bool(ev.get("allowed", False)), ev


def _round_base_size(qty: float, increment: Optional[float], min_sz: Optional[float]) -> float:
    if qty <= 0:
        return 0.0
    if increment and increment > 0:
        q = math.floor(qty / increment) * increment
    else:
        q = round(qty, 8)
    if min_sz and q + 1e-12 < min_sz:
        return 0.0
    return float(q)


def _fmt_base_size(q: float) -> str:
    s = f"{q:.12f}".rstrip("0").rstrip(".")
    return s if s else "0"


def _notional_guard_multiplier() -> float:
    try:
        raw_bps = getattr(settings, "chili_momentum_order_notional_guard_bps", 25.0)
        bps = 25.0 if raw_bps is None else float(raw_bps)
    except (TypeError, ValueError):
        bps = 25.0
    return 1.0 + max(0.0, bps) / 10_000.0


def _expected_move_bps_from_ohlcv(df: Any) -> float | None:
    """Typical recent 15m bar range in bps (ATR / last close) as an expected-move
    proxy. The BBO spread is a round-trip cost, so the adaptive spread gate
    tolerates proportionally more of it on instruments that actually move this
    much. Returns None when candle data is missing or too thin to be meaningful.
    Pure + side-effect-free for unit testing. (docs/DESIGN/MOMENTUM_LANE.md)"""
    try:
        if df is None or getattr(df, "empty", True) or len(df) < 5:
            return None
        import pandas as pd

        high = df["High"].astype(float)
        low = df["Low"].astype(float)
        close = df["Close"].astype(float)
        prev_close = close.shift(1)
        true_range = pd.concat(
            [(high - low).abs(), (high - prev_close).abs(), (low - prev_close).abs()],
            axis=1,
        ).max(axis=1).dropna()
        if len(true_range) < 1:
            return None
        n = min(14, len(true_range))
        atr = float(true_range.tail(n).mean())
        last_close = float(close.iloc[-1])
        if not math.isfinite(atr) or atr <= 0 or last_close <= 0:
            return None
        return (atr / last_close) * 10_000.0
    except Exception:
        return None


def _adaptive_live_max_spread_bps(
    expected_move_bps: float | None, *, is_low_float: bool = False
) -> float:
    """Live spread cap, volatility-relative: ``max(base_floor, ratio x expected
    move)``. Reuses the shared, tested policy helper so the runner BBO gate and
    the pre-entry risk evaluator agree on the same adaptive tolerance. Reads the
    documented base floor + ratio knobs from settings (no inline magic).

    CONVERSION FIX A: when ``is_low_float`` and the expected-move proxy is cold/None,
    pass the documented low-float fallback move so the cap scales off it instead of
    collapsing to the 12bps floor (which blocks the explosive low-float names the
    lane selects). Flag-off / non-low-float => fallback is None => byte-identical."""
    from .risk_policy import adaptive_max_spread_bps

    try:
        raw_base = getattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)
        base = 12.0 if raw_base is None else float(raw_base)
    except (TypeError, ValueError):
        base = 12.0
    try:
        raw_ratio = getattr(settings, "chili_momentum_risk_spread_to_expected_move_ratio", 0.5)
        ratio = 0.5 if raw_ratio is None else float(raw_ratio)
    except (TypeError, ValueError):
        ratio = 0.5
    try:
        raw_cap = getattr(settings, "chili_momentum_risk_max_spread_bps_abs_cap", 300.0)
        abs_cap = 300.0 if raw_cap is None else float(raw_cap)
    except (TypeError, ValueError):
        abs_cap = 300.0
    fallback: float | None = None
    if is_low_float and bool(
        getattr(settings, "chili_momentum_spread_lowfloat_fallback_enabled", True)
    ):
        try:
            raw_fb = getattr(settings, "chili_momentum_spread_lowfloat_fallback_move_bps", 120.0)
            fb = float(raw_fb) if raw_fb is not None else None
            if fb is not None and fb > 0:
                fallback = fb
        except (TypeError, ValueError):
            fallback = None
    return adaptive_max_spread_bps(
        base, expected_move_bps, ratio, abs_cap_bps=abs_cap, low_float_fallback_move_bps=fallback
    )


def _signal_dicts(*roots: Any) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[int] = set()
    keys = (
        "anticipation",
        "entry_confirmation",
        "micro_frame",
        "micro_trace",
        "last_micro_frame",
        "last_micro_trace",
        "tape",
        "last_tape",
        "ofi",
        "order_flow",
        "last_order_flow",
        "trace",
    )
    stack = [r for r in roots if isinstance(r, dict)]
    while stack:
        item = stack.pop()
        ident = id(item)
        if ident in seen:
            continue
        seen.add(ident)
        out.append(item)
        for key in keys:
            child = item.get(key)
            if isinstance(child, dict):
                stack.append(child)
    return out


def _first_signal_bool(dicts: list[dict[str, Any]], keys: tuple[str, ...]) -> bool | None:
    for src in dicts:
        for key in keys:
            if key not in src:
                continue
            val = src.get(key)
            if isinstance(val, bool):
                return val
            f = _float_or_none(val)
            if f is not None:
                return f > 0.0
    return None


def _first_signal_float(dicts: list[dict[str, Any]], keys: tuple[str, ...]) -> float | None:
    for src in dicts:
        for key in keys:
            f = _float_or_none(src.get(key))
            if f is not None:
                return f
    return None


def _optional_micro_hold(dicts: list[dict[str, Any]]) -> bool | None:
    direct = _first_signal_bool(
        dicts,
        (
            "micro_hold",
            "micro_holds_vwap",
            "micro_vwap_hold",
            "vwap_hold",
            "pivot_hold",
            "reclaim_ok",
            "micro_confirmed",
        ),
    )
    if direct is not None:
        return direct
    ref = _first_signal_float(dicts, ("micro_price", "micro_mid", "price", "mid"))
    level = _first_signal_float(dicts, ("vwap", "session_vwap", "pivot", "reclaim_level"))
    if ref is None or level is None:
        return None
    return ref >= level


def _optional_tape_confirm(dicts: list[dict[str, Any]]) -> bool | None:
    direct = _first_signal_bool(
        dicts,
        (
            "tape_confirmed",
            "tape_ok",
            "tape_thrust",
            "buy_pressure",
            "ofi_improving",
            "order_flow_positive",
        ),
    )
    if direct is not None:
        return direct
    impulse = _first_signal_float(
        dicts,
        (
            "ofi_slope",
            "ofi_level",
            "ofi",
            "order_flow_imbalance",
            "tape_score",
            "tape_delta",
            "buy_sell_imbalance",
        ),
    )
    if impulse is None:
        return None
    return impulse > 0.0


def _anticipation_confirmation_legs(
    *,
    le: dict[str, Any],
    pos: dict[str, Any] | None,
    bid: float | None,
    ask: float | None,
    mid: float | None,
    snap: dict[str, Any] | None = None,
) -> dict[str, bool | None]:
    pos = pos if isinstance(pos, dict) else {}
    dicts = _signal_dicts(le, pos, snap or {})
    bid_f = _float_or_none(bid)
    ask_f = _float_or_none(ask)
    mid_f = _float_or_none(mid)
    avg = _float_or_none(pos.get("avg_entry_price"))
    hwm = _float_or_none(pos.get("high_water_mark"))
    structural_level = (
        _float_or_none(le.get("breakout_level_price"))
        or _first_signal_float(dicts, ("breakout_level", "pullback_high", "shelf", "pivot"))
    )
    price_ref = mid_f if mid_f is not None else bid_f
    spread_ref = bid_f is not None and ask_f is not None and ask_f >= bid_f
    return {
        "structural_hold": (
            None if structural_level is None or price_ref is None else price_ref >= structural_level
        ),
        "mid_reclaims_avg": None if avg is None or mid_f is None else mid_f >= avg,
        "high_water_progress": None if avg is None or hwm is None else hwm > avg,
        "micro_hold": _optional_micro_hold(dicts),
        "tape_or_ofi_confirm": _optional_tape_confirm(dicts),
        "quote_cross_valid": spread_ref,
    }


def _confirmation_strength(legs: dict[str, bool | None]) -> float:
    usable = [bool(v) for k, v in legs.items() if v is not None and k != "quote_cross_valid"]
    if not usable:
        return 0.0
    return sum(1 for v in usable if v) / float(len(usable))


def _anticipation_starter_plan(
    *,
    full_qty: float,
    base_increment: float | None,
    base_min_size: float | None,
    le: dict[str, Any],
    bid: float | None,
    ask: float | None,
    mid: float | None,
    snap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Adaptive starter sizing for anticipation entries.

    The intended full size remains the existing risk-first size. If live structure
    is only partly confirmed, submit a starter derived from the documented Ross
    scale-out fraction and leave the balance for the event-driven add. Stronger
    confirmation moves the starter toward full size; no fixed probe fraction is
    embedded here.
    """
    fq = _float_or_none(full_qty) or 0.0
    if fq <= 0.0:
        return {"enabled": False, "reason": "full_qty_zero", "full_qty": fq, "probe_qty": 0.0, "remainder_qty": 0.0}
    legs = _anticipation_confirmation_legs(le=le, pos=None, bid=bid, ask=ask, mid=mid, snap=snap)
    strength = _confirmation_strength(legs)
    starter_floor = max(0.0, min(1.0, 1.0 - scale_out_fraction()))
    fraction = min(1.0, starter_floor + (1.0 - starter_floor) * strength)
    probe_qty = _round_base_size(fq * fraction, base_increment, base_min_size)
    remainder_qty = _round_base_size(max(0.0, fq - probe_qty), base_increment, base_min_size)
    if probe_qty <= 0.0 or remainder_qty <= 0.0 or probe_qty >= fq:
        return {
            "enabled": False,
            "reason": "full_size_entry",
            "full_qty": fq,
            "probe_qty": fq,
            "remainder_qty": 0.0,
            "starter_fraction": 1.0,
            "confirmation_strength": strength,
            "confirmation_legs": legs,
        }
    return {
        "enabled": True,
        "reason": "adaptive_starter",
        "full_qty": fq,
        "probe_qty": probe_qty,
        "remainder_qty": remainder_qty,
        "starter_fraction": probe_qty / fq,
        "confirmation_strength": strength,
        "confirmation_legs": legs,
    }


def _anticipation_remainder_confirmation(
    *,
    le: dict[str, Any],
    pos: dict[str, Any],
    tick: Any,
    freshness: Any,
    bid: float,
    ask: float,
    mid: float,
    remainder_qty: float,
    max_spread_bps: float | None,
    boundary_ok: bool,
    market_open: bool,
    max_notional: float | None,
    guarded_ask: float | None,
    held_seconds: float | None,
    snap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    avg = _float_or_none(pos.get("avg_entry_price"))
    stop_px = _float_or_none(pos.get("stop_price"))
    quote_block = _quote_quality_block(tick, freshness, max_spread_bps=max_spread_bps)
    legs = _anticipation_confirmation_legs(le=le, pos=pos, bid=bid, ask=ask, mid=mid, snap=snap)
    payload: dict[str, Any] = {
        "bid": _float_or_none(bid),
        "ask": _float_or_none(ask),
        "mid": _float_or_none(mid),
        "avg_entry": avg,
        "high_water_mark": _float_or_none(pos.get("high_water_mark")),
        "remainder_qty": _float_or_none(remainder_qty),
        "confirmation_legs": legs,
        "stale_or_missing_data_reason": None,
    }
    if quote_block is not None:
        payload["reason"] = quote_block.get("reason") or "quote_quality_block"
        payload["quote_block"] = quote_block
        payload["stale_or_missing_data_reason"] = quote_block.get("reason")
        return {"confirmed": False, **payload}
    if not boundary_ok:
        payload["reason"] = "boundary_risk_block"
        return {"confirmed": False, **payload}
    if not market_open:
        payload["reason"] = "market_closed_or_unknown"
        return {"confirmed": False, **payload}
    rq = _float_or_none(remainder_qty) or 0.0
    if rq <= 0.0:
        payload["reason"] = "no_remainder_qty"
        return {"confirmed": False, **payload}
    if stop_px is not None and _float_or_none(bid) is not None and float(bid) <= stop_px:
        payload["reason"] = "stop_hazard"
        payload["stop_price"] = stop_px
        return {"confirmed": False, **payload}
    if breakout_failed_to_hold(
        breakout_level=le.get("breakout_level_price"),
        bid=bid,
        held_seconds=float(held_seconds or 0.0),
        window_seconds=_breakout_bailout_window_seconds(),
        buffer_pct=float(getattr(settings, "chili_momentum_breakout_bailout_buffer_pct", 0.001) or 0.0),
    ):
        payload["reason"] = "breakout_failed_hazard"
        payload["breakout_level"] = le.get("breakout_level_price")
        return {"confirmed": False, **payload}
    max_notional_f = _float_or_none(max_notional)
    guarded_ask_f = _float_or_none(guarded_ask)
    current_qty = _float_or_none(pos.get("quantity")) or 0.0
    if max_notional_f is not None and guarded_ask_f is not None:
        projected = (current_qty + rq) * guarded_ask_f
        payload["projected_guarded_notional_usd"] = projected
        payload["max_notional_usd"] = max_notional_f
        if projected > max_notional_f:
            payload["reason"] = "notional_cap"
            return {"confirmed": False, **payload}
    if not any(v is True for k, v in legs.items() if k != "quote_cross_valid"):
        if not any(v is not None for k, v in legs.items() if k != "quote_cross_valid"):
            payload["stale_or_missing_data_reason"] = "no_structural_micro_tape_or_avg_data"
        payload["reason"] = "no_confirmation"
        return {"confirmed": False, **payload}
    payload["reason"] = "confirmed"
    return {"confirmed": True, **payload}


def _apply_anticipation_remainder_fill(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    filled_quantity: float,
    fill_price: float,
    params: dict[str, Any],
) -> dict[str, Any]:
    pos = dict(le.get("position") or {})
    current_qty = _float_or_none(pos.get("quantity")) or 0.0
    current_avg = _float_or_none(pos.get("avg_entry_price")) or float(fill_price)
    fill_qty = max(0.0, float(filled_quantity))
    new_qty = current_qty + fill_qty
    if new_qty <= 0.0:
        return pos
    new_avg = ((current_qty * current_avg) + (fill_qty * float(fill_price))) / new_qty
    pos["quantity"] = new_qty
    pos["original_quantity"] = max(new_qty, _float_or_none(pos.get("original_quantity")) or 0.0)
    pos["avg_entry_price"] = new_avg
    pos["notional_usd"] = new_qty * new_avg
    pos["high_water_mark"] = max(_float_or_none(pos.get("high_water_mark")) or new_avg, new_avg)
    atrp = _float_or_none(le.get("entry_stop_atr_pct"))
    if atrp is not None and atrp > 0.0:
        stop_px, target_px = stop_target_prices(
            new_avg,
            atr_pct=atrp,
            side_long=True,
            stop_atr_mult=float(params["stop_atr_mult"]),
            target_atr_mult=float(params["target_atr_mult"]),
        )
        pos["stop_price"] = stop_px
        pos["target_price"] = target_px
    le["position"] = pos
    full_qty = _float_or_none(le.get("anticipation_full_qty"))
    remaining = max(0.0, (full_qty or new_qty) - new_qty)
    le["anticipation_remainder_qty"] = remaining
    le["anticipation_remainder_filled"] = remaining <= 0.0
    le["anticipation_remainder_state"] = "filled" if remaining <= 0.0 else "partial_filled"
    le["anticipation_remainder_filled_qty"] = (
        (_float_or_none(le.get("anticipation_remainder_filled_qty")) or 0.0) + fill_qty
    )
    le["anticipation_remainder_fill_price"] = float(fill_price)
    le.pop("anticipation_remainder_submitted", None)
    le.pop("anticipation_remainder_order_id", None)
    le.pop("anticipation_remainder_client_order_id", None)
    _record_live_entry_ledger_safe(db, sess, le=le, quantity=fill_qty, fill_price=float(fill_price))
    _commit_le(sess, le)
    _emit(
        db,
        sess,
        "live_anticipation_remainder_filled",
        {
            "filled_qty": fill_qty,
            "fill_price": float(fill_price),
            "new_quantity": new_qty,
            "new_avg_entry": new_avg,
            "remaining_remainder_qty": remaining,
        },
    )
    return pos


def _clear_anticipation_remainder_inflight(le: dict[str, Any]) -> None:
    le.pop("anticipation_remainder_submitted", None)
    le.pop("anticipation_remainder_order_id", None)
    le.pop("anticipation_remainder_client_order_id", None)
    le["anticipation_remainder_state"] = "waiting"


def _clear_anticipation_entry_state(le: dict[str, Any]) -> None:
    for key in (
        "anticipation_full_qty",
        "anticipation_probe_qty",
        "anticipation_probe_filled_qty",
        "anticipation_remainder_qty",
        "anticipation_remainder_state",
        "anticipation_remainder_submitted",
        "anticipation_remainder_order_id",
        "anticipation_remainder_client_order_id",
        "anticipation_remainder_submit_utc",
        "anticipation_remainder_submit_result",
        "anticipation_remainder_last_confirmation",
        "anticipation_remainder_filled",
        "anticipation_remainder_filled_qty",
        "anticipation_remainder_fill_price",
        "anticipation_starter_plan",
        "last_anticipation_remainder_wait",
        "last_anticipation_remainder_terminal_no_fill",
    ):
        le.pop(key, None)


def _handle_anticipation_remainder(
    db: Session,
    sess: TradingAutomationSession,
    adapter: Any,
    *,
    le: dict[str, Any],
    product_id: str,
    tick: Any,
    freshness: Any,
    bid: float,
    ask: float,
    mid: float,
    max_spread_bps: float | None,
    boundary_ok: bool,
    market_open: bool,
    max_notional: float | None,
    guarded_ask: float | None,
    params: dict[str, Any],
    held_seconds: float | None,
    snap: dict[str, Any] | None = None,
) -> dict[str, Any]:
    pos = le.get("position")
    if not isinstance(pos, dict):
        return {"handled": False, "reason": "position_missing"}
    if le.get("anticipation_remainder_filled"):
        return {"handled": False, "reason": "already_filled"}
    remainder_qty = _float_or_none(le.get("anticipation_remainder_qty")) or 0.0
    if remainder_qty <= 0.0:
        return {"handled": False, "reason": "no_remainder_qty"}
    if pos.get("partial_taken"):
        wait = {
            "reason": "position_already_de_risked",
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "avg_entry": _float_or_none(pos.get("avg_entry_price")),
            "remainder_qty": remainder_qty,
            "confirmation_legs": {},
            "stale_or_missing_data_reason": None,
        }
        le["last_anticipation_remainder_wait"] = wait
        _commit_le(sess, le)
        _emit(db, sess, "live_anticipation_remainder_wait", wait)
        return {"handled": False, "wait": True, "reason": wait["reason"]}

    if le.get("anticipation_remainder_submitted"):
        oid = le.get("anticipation_remainder_order_id")
        if not oid:
            wait = {
                "reason": "in_flight_missing_order_id",
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "avg_entry": _float_or_none(pos.get("avg_entry_price")),
                "remainder_qty": remainder_qty,
                "confirmation_legs": {},
                "stale_or_missing_data_reason": "missing_order_id",
            }
            le["last_anticipation_remainder_wait"] = wait
            _commit_le(sess, le)
            _emit(db, sess, "live_anticipation_remainder_wait", wait)
            return {"handled": True, "pending": True, "reason": wait["reason"]}
        try:
            no, _ = adapter.get_order(str(oid))
        except Exception:
            no = None
        if no is not None and _order_done_for_entry(no):
            filled_size = float(no.filled_size or 0.0)
            avg_px = _float_or_none(no.average_filled_price)
            if filled_size > 0.0 and avg_px is not None:
                _apply_anticipation_remainder_fill(
                    db,
                    sess,
                    le=le,
                    filled_quantity=min(filled_size, remainder_qty),
                    fill_price=avg_px,
                    params=params,
                )
                return {"handled": True, "filled": True}
            terminal = {
                "reason": "terminal_no_fill",
                "order_id": oid,
                "order_status": getattr(no, "status", None),
                "filled_size": filled_size,
                "remainder_qty": remainder_qty,
            }
            le["last_anticipation_remainder_terminal_no_fill"] = terminal
            _clear_anticipation_remainder_inflight(le)
            _commit_le(sess, le)
            _emit(db, sess, "live_anticipation_remainder_terminal_no_fill", terminal)
            return {"handled": False, "retryable": True, "reason": "terminal_no_fill"}
        if no is not None and _order_open(no):
            wait = {
                "reason": "in_flight",
                "order_id": oid,
                "order_status": no.status,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "avg_entry": _float_or_none(pos.get("avg_entry_price")),
                "remainder_qty": remainder_qty,
                "confirmation_legs": {},
                "stale_or_missing_data_reason": None,
            }
            le["last_anticipation_remainder_wait"] = wait
            _commit_le(sess, le)
            _emit(db, sess, "live_anticipation_remainder_wait", wait)
            return {"handled": True, "pending": True, "reason": "in_flight"}
        terminal_status = str(getattr(no, "status", "") or "").lower() if no is not None else "missing"
        terminal = {
            "reason": "terminal_no_fill" if terminal_status in ("cancelled", "canceled", "expired", "failed", "rejected", "missing") else "order_state_unconfirmed",
            "order_id": oid,
            "order_status": terminal_status,
            "remainder_qty": remainder_qty,
        }
        _clear_anticipation_remainder_inflight(le)
        le["last_anticipation_remainder_terminal_no_fill"] = terminal
        _commit_le(sess, le)
        _emit(db, sess, "live_anticipation_remainder_terminal_no_fill", terminal)
        return {"handled": False, "retryable": True, "reason": terminal["reason"]}

    conf = _anticipation_remainder_confirmation(
        le=le,
        pos=pos,
        tick=tick,
        freshness=freshness,
        bid=bid,
        ask=ask,
        mid=mid,
        remainder_qty=remainder_qty,
        max_spread_bps=max_spread_bps,
        boundary_ok=boundary_ok,
        market_open=market_open,
        max_notional=max_notional,
        guarded_ask=guarded_ask,
        held_seconds=held_seconds,
        snap=snap,
    )
    if not conf.get("confirmed"):
        wait = dict(conf)
        wait.pop("confirmed", None)
        le["last_anticipation_remainder_wait"] = wait
        _commit_le(sess, le)
        _emit(db, sess, "live_anticipation_remainder_wait", wait)
        return {"handled": False, "wait": True, "reason": wait.get("reason")}

    cid = f"chili_ml_ar_{sess.id}_{uuid.uuid4().hex[:12]}"[:120]
    result = adapter.place_market_order(
        product_id=product_id,
        side="buy",
        base_size=_fmt_base_size(remainder_qty),
        client_order_id=cid,
    ) or {}
    le["anticipation_remainder_last_confirmation"] = conf
    le["anticipation_remainder_submit_result"] = {"ok": result.get("ok"), "error": result.get("error")}
    le["anticipation_remainder_client_order_id"] = result.get("client_order_id") or cid
    le["anticipation_remainder_order_id"] = result.get("order_id")
    if result.get("ok") and result.get("order_id"):
        le["anticipation_remainder_submitted"] = True
        le["anticipation_remainder_state"] = "submitted"
        le["anticipation_remainder_submit_utc"] = _utcnow().isoformat()
        _commit_le(sess, le)
        _emit(
            db,
            sess,
            "live_anticipation_remainder_submitted",
            {
                "order_id": result.get("order_id"),
                "client_order_id": le["anticipation_remainder_client_order_id"],
                "remainder_qty": remainder_qty,
                "confirmation_legs": conf.get("confirmation_legs"),
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "avg_entry": conf.get("avg_entry"),
            },
        )
        return {"handled": True, "submitted": True}

    if result.get("ok"):
        le["anticipation_remainder_submitted"] = True
        le["anticipation_remainder_state"] = "submit_unconfirmed"
    else:
        le["anticipation_remainder_state"] = "waiting"
    _commit_le(sess, le)
    _emit(
        db,
        sess,
        "live_anticipation_remainder_wait",
        {
            "reason": "submit_failed" if not result.get("ok") else "submit_missing_order_id",
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "avg_entry": conf.get("avg_entry"),
            "remainder_qty": remainder_qty,
            "confirmation_legs": conf.get("confirmation_legs"),
            "stale_or_missing_data_reason": None if result.get("ok") else result.get("error"),
            "submit_result": result,
        },
    )
    return {"handled": bool(result.get("ok")), "submitted": bool(result.get("ok")), "reason": le["anticipation_remainder_state"]}


def repeg_decision(
    *,
    armed: bool,
    filled: bool,
    arm_price: float | None,
    resting_price: float | None,
    current_ref_price: float | None,
    enabled: bool | None = None,
    chase_ceiling_pct: float | None = None,
    min_advance_pct: float | None = None,
) -> dict[str, Any]:
    """CONVERSION FIX B — should an armed/unfilled resting entry RE-PEG up?

    When a resting marketable-limit entry hasn't filled and the reference price has
    ADVANCED away from it, the order is stranded below the market — the move leaves
    without us. This decides whether to re-peg UP toward the new marketable price
    (which means: CANCEL the prior order, RE-SUBMIT at the advanced price), bounded
    by a CHASE CEILING measured off the ORIGINAL arm price so we never chase a
    runaway into extension.

    Returns a dict: ``{escalate, cancel_prior, new_target_price, reason}``.
      * escalate=True only when armed AND not filled AND the ref advanced past the
        min-advance threshold AND the new price is within the chase ceiling.
      * cancel_prior mirrors escalate (re-peg = cancel the stale resting order first).
      * Past the ceiling => escalate False, reason ``chase_ceiling_exceeded`` (abandon,
        do not chase). Pure + side-effect-free. Flag-off => never escalates.
    """
    en = bool(getattr(settings, "chili_momentum_repeg_on_advance_enabled", True)) if enabled is None else bool(enabled)
    try:
        ceil_pct = (
            float(getattr(settings, "chili_momentum_repeg_chase_ceiling_pct", 0.02))
            if chase_ceiling_pct is None else float(chase_ceiling_pct)
        )
    except (TypeError, ValueError):
        ceil_pct = 0.02
    try:
        min_adv = (
            float(getattr(settings, "chili_momentum_repeg_min_advance_pct", 0.001))
            if min_advance_pct is None else float(min_advance_pct)
        )
    except (TypeError, ValueError):
        min_adv = 0.001

    def _no(reason: str) -> dict[str, Any]:
        return {"escalate": False, "cancel_prior": False, "new_target_price": None, "reason": reason}

    if not en:
        return _no("disabled")
    if not armed:
        return _no("not_armed")
    if filled:
        return _no("already_filled")
    try:
        ap = float(arm_price) if arm_price is not None else None
        rest = float(resting_price) if resting_price is not None else None
        ref = float(current_ref_price) if current_ref_price is not None else None
    except (TypeError, ValueError):
        return _no("bad_inputs")
    if ap is None or ref is None or ap <= 0 or ref <= 0:
        return _no("bad_inputs")
    # The order is stranded only when the reference has advanced ABOVE the resting
    # price (or above the arm price when no resting price is tracked yet).
    anchor = rest if (rest is not None and rest > 0) else ap
    advance = (ref - anchor) / anchor
    if advance < max(0.0, min_adv):
        return _no("no_advance")
    # Chase ceiling measured off the ORIGINAL arm price.
    ceiling_price = ap * (1.0 + max(0.0, ceil_pct))
    if ref > ceiling_price:
        return {
            "escalate": False,
            "cancel_prior": False,
            "new_target_price": None,
            "reason": "chase_ceiling_exceeded",
        }
    return {
        "escalate": True,
        "cancel_prior": True,
        "new_target_price": round(ref, 6),
        "reason": "repeg_advance",
    }


def _live_entry_quote_gate_applies(sess: TradingAutomationSession, le: dict[str, Any]) -> bool:
    state = sess.state
    if state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE, STATE_WATCHING_LIVE, STATE_LIVE_ENTRY_CANDIDATE):
        return True
    return state == STATE_LIVE_PENDING_ENTRY and not le.get("entry_submitted")


_HELD_LIVE_STATES = frozenset(
    {STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT}
)


def _stop_vol_floor_mult() -> float:
    """Fraction of the live expected-move the stop must clear to sit outside the
    noise (default 0.5). One documented knob; everything else derived."""
    try:
        v = float(getattr(settings, "chili_momentum_risk_stop_vol_floor_mult", 0.5))
    except (TypeError, ValueError):
        return 0.5
    return v if v >= 0 else 0.0


_INTERVAL_SECONDS: dict[str, float] = {
    "1m": 60.0, "2m": 120.0, "5m": 300.0, "15m": 900.0, "30m": 1800.0,
    "60m": 3600.0, "90m": 5400.0, "1h": 3600.0, "1d": 86400.0,
}


def _entry_interval_seconds() -> float:
    iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m").lower()
    return float(_INTERVAL_SECONDS.get(iv, 300.0))


def _breakout_bailout_window_seconds() -> float:
    """Early window (seconds) for the #2 breakout-or-bailout fast exit = N
    entry-interval bars. One documented knob (bars), derived from the configured
    timeframe so it stays adaptive to 1m vs 5m. (docs/DESIGN/MOMENTUM_LANE.md §8)"""
    try:
        bars = float(getattr(settings, "chili_momentum_breakout_bailout_max_bars", 2.0) or 0.0)
    except (TypeError, ValueError):
        bars = 2.0
    return max(0.0, bars) * _entry_interval_seconds()


def _held_position_keeps_exit_on_boundary_fail(state: str, has_position: Any) -> bool:
    """A held momentum position must keep its EXIT/stop management even when the
    entry-oriented boundary risk eval (viability freshness / caps / concurrency)
    refuses. The stop/target is a SAFETY mechanism that must always run — only the
    kill-switch force-exits. Returns True when the tick must fall through to the
    exit handler instead of blocking. (docs/DESIGN/MOMENTUM_LANE.md)"""
    return bool(has_position) and state in _HELD_LIVE_STATES


def _quote_quality_block(
    tick: Any, freshness: Any, max_spread_bps: float | None = None
) -> dict[str, Any] | None:
    meta = getattr(tick, "freshness", None) or freshness
    if meta is not None and not is_fresh_enough(meta):
        return {
            "reason": "stale_bbo",
            "age_seconds": round(float(meta.age_seconds()), 4),
            "max_age_seconds": float(getattr(meta, "max_age_seconds", 0.0) or 0.0),
        }
    try:
        mid = float(getattr(tick, "mid", 0.0) or 0.0)
        bid = float(getattr(tick, "bid", 0.0) or 0.0)
        ask = float(getattr(tick, "ask", 0.0) or 0.0)
    except (TypeError, ValueError):
        return {"reason": "invalid_bbo"}
    if mid <= 0 or bid <= 0 or ask <= 0 or ask < bid:
        return {"reason": "invalid_bbo", "bid": bid, "ask": ask, "mid": mid}
    try:
        spread_bps = float(getattr(tick, "spread_bps", None))
    except (TypeError, ValueError):
        spread_bps = ((ask - bid) / mid) * 10_000.0
    if not math.isfinite(spread_bps):
        spread_bps = ((ask - bid) / mid) * 10_000.0
    # max_spread_bps is the caller-supplied ADAPTIVE tolerance (volatility-relative);
    # fall back to the documented base floor when absent or invalid.
    raw_max_spread = max_spread_bps
    if raw_max_spread is None:
        raw_max_spread = getattr(settings, "chili_momentum_risk_max_spread_bps_live", 12.0)
    try:
        # A 0.0 cap is a deliberate "block all" and is preserved; only None / NaN /
        # inf / unparseable values fall back to the documented default.
        max_spread = 12.0 if raw_max_spread is None else float(raw_max_spread)
        if not math.isfinite(max_spread):
            max_spread = 12.0
    except (TypeError, ValueError):
        max_spread = 12.0
    if spread_bps > max_spread:
        return {
            "reason": "wide_bbo_spread",
            "spread_bps": round(spread_bps, 4),
            "max_spread_bps": max_spread,
            "bid": bid,
            "ask": ask,
            "mid": mid,
        }
    return None


def _order_done_for_entry(no: NormalizedOrder) -> bool:
    st = (no.status or "").lower()
    if st in ("filled", "done", "closed"):
        return True
    if no.filled_size > 1e-12:
        return st in ("cancelled", "canceled", "expired", "failed")
    return False


def _order_open(no: NormalizedOrder) -> bool:
    st = (no.status or "").lower()
    return st in ("open", "pending", "active", "unknown", "")


def summarize_live_execution(snap: Any) -> dict[str, Any]:
    if not isinstance(snap, dict):
        return {}
    le = snap.get(KEY_LIVE_EXEC)
    if not isinstance(le, dict):
        return {}
    pos = le.get("position")
    out: dict[str, Any] = {
        "tick_count": le.get("tick_count"),
        "last_tick_utc": le.get("last_tick_utc"),
        "entry_order_id": le.get("entry_order_id"),
        "entry_client_order_id": le.get("entry_client_order_id"),
        "exit_order_id": le.get("exit_order_id"),
        "exit_client_order_id": le.get("exit_client_order_id"),
        "realized_pnl_usd": le.get("realized_pnl_usd"),
        "fees_usd": le.get("fees_usd"),
        "last_mid": le.get("last_mid"),
        "last_exit_reason": le.get("last_exit_reason"),
        "last_exit_intent": le.get("last_exit_intent") if isinstance(le.get("last_exit_intent"), dict) else None,
        "exit_execution_intent_count": len(le.get("exit_execution_intents") or []),
        "pending_exit_reason": le.get("pending_exit_reason"),
        "pending_exit_quantity": le.get("pending_exit_quantity"),
        "pending_exit_submitted_at_utc": le.get("pending_exit_submitted_at_utc"),
        "pending_exit_queued_at_utc": le.get("pending_exit_queued_at_utc"),
        "pending_exit_presubmit_deferred": le.get("pending_exit_presubmit_deferred"),
        "pending_exit_product_id": le.get("pending_exit_product_id"),
        "pending_exit_client_order_id": le.get("pending_exit_client_order_id"),
        "pending_exit_deferred_until_utc": le.get("pending_exit_deferred_until_utc"),
        "pending_exit_deferred_until": le.get("pending_exit_deferred_until"),
        "pending_exit_market_session": le.get("pending_exit_market_session"),
        "pending_exit_order_status": le.get("pending_exit_order_status"),
        "pending_exit_filled_size": le.get("pending_exit_filled_size"),
        "last_exit_pending_confirmation": (
            le.get("last_exit_pending_confirmation")
            if isinstance(le.get("last_exit_pending_confirmation"), dict)
            else None
        ),
        "last_exit_deferred": (
            le.get("last_exit_deferred") if isinstance(le.get("last_exit_deferred"), dict) else None
        ),
        "last_exit_deferred_adopted": (
            le.get("last_exit_deferred_adopted")
            if isinstance(le.get("last_exit_deferred_adopted"), dict)
            else None
        ),
        "last_exit_terminal_no_fill": (
            le.get("last_exit_terminal_no_fill")
            if isinstance(le.get("last_exit_terminal_no_fill"), dict)
            else None
        ),
        "last_exit_operator_action_required": (
            le.get("last_exit_operator_action_required")
            if isinstance(le.get("last_exit_operator_action_required"), dict)
            else None
        ),
        "last_partial_exit_reason": le.get("last_partial_exit_reason"),
        "last_partial_exit_price": le.get("last_partial_exit_price"),
        "last_quote_quality_gate": (
            le.get("last_quote_quality_gate") if isinstance(le.get("last_quote_quality_gate"), dict) else None
        ),
        "last_exit_notional_basis_usd": le.get("last_exit_notional_basis_usd"),
        "last_exit_return_bps": le.get("last_exit_return_bps"),
        "last_partial_exit_notional_basis_usd": le.get("last_partial_exit_notional_basis_usd"),
        "last_partial_exit_return_bps": le.get("last_partial_exit_return_bps"),
        "cooldown_until_utc": le.get("cooldown_until_utc"),
    }
    if isinstance(pos, dict):
        out["in_position"] = True
        out["avg_entry_price"] = pos.get("avg_entry_price")
        out["quantity"] = pos.get("quantity")
        out["original_quantity"] = pos.get("original_quantity")
        out["notional_usd"] = pos.get("notional_usd")
        out["stop_price"] = pos.get("stop_price")
        out["target_price"] = pos.get("target_price")
        out["high_water_mark"] = pos.get("high_water_mark")
        # Ross asymmetric exit state: did we take the first-target partial yet, and
        # what's the runner riding on?
        out["partial_taken"] = bool(pos.get("partial_taken"))
        out["scaled_out_at_utc"] = pos.get("scaled_out_at_utc")
        out["scale_out_fraction"] = pos.get("scale_out_fraction")
    else:
        out["in_position"] = False
    return out


def list_runnable_live_sessions(db: Session, *, limit: int = 25) -> list[TradingAutomationSession]:
    lim = max(1, min(int(limit), 200))
    rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.state.in_(LIVE_RUNNER_RUNNABLE_STATES),
        )
        .order_by(TradingAutomationSession.updated_at.asc())
        .limit(lim)
        .all()
    )
    return [row for row in rows if not is_operator_paused(row.risk_snapshot_json)]


def run_live_runner_batch(
    db: Session,
    *,
    limit: int = 25,
    adapter_factory: Optional[AdapterFactory] = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for sess in list_runnable_live_sessions(db, limit=limit):
        try:
            out.append(tick_live_session(db, int(sess.id), adapter_factory=adapter_factory))
        except Exception:
            _log.warning("[live_runner] tick failed session=%s", sess.id, exc_info=True)
            out.append({"ok": False, "session_id": sess.id, "error": "tick_exception"})
    return out


_RECONCILE_TICK_INTERVAL = 5  # only reconcile every Nth tick
_reconcile_counters: dict[int, int] = {}


def _reconcile_venue_position(adapter: Any, db: Session, sess: Any, product_id: str) -> None:
    """Rate-limited venue reconciliation: detect orphaned orders or stale positions."""
    sid = int(sess.id)
    _reconcile_counters[sid] = _reconcile_counters.get(sid, 0) + 1
    if _reconcile_counters[sid] % _RECONCILE_TICK_INTERVAL != 0:
        return
    try:
        le = _live_exec(dict(sess.risk_snapshot_json or {}))
        st = sess.state
        entry_oid = le.get("entry_order_id")
        has_pos = isinstance(le.get("position"), dict)

        if not entry_oid:
            return

        # Check if venue has filled order but session hasn't caught up
        if st in (STATE_LIVE_PENDING_ENTRY,) and not has_pos:
            no, _ = adapter.get_order(str(entry_oid))
            if no and no.status == "filled" and float(no.filled_size or 0) > 0:
                _log.warning(
                    "[live_runner] Reconcile: venue shows filled entry for session=%s but state=%s — next tick will process",
                    sid, st,
                )
                _emit(db, sess, "reconcile_stale_entry_detected", {"order_id": entry_oid, "venue_status": no.status})

        # Check if session thinks it has position but venue shows nothing
        if st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING) and has_pos:
            exit_oid = le.get("exit_order_id")
            if exit_oid:
                no, _ = adapter.get_order(str(exit_oid))
                if no and no.status == "filled":
                    _log.warning(
                        "[live_runner] Reconcile: venue shows filled exit for session=%s — marking for review",
                        sid,
                    )
                    _emit(db, sess, "reconcile_orphaned_exit_detected", {"order_id": exit_oid})
    except Exception as e:
        _log.debug("[live_runner] reconcile failed for session=%s: %s", sid, e)


def tick_live_session(
    db: Session,
    session_id: int,
    *,
    adapter_factory: Optional[AdapterFactory] = None,
) -> dict[str, Any]:
    if not settings.chili_momentum_live_runner_enabled:
        return {"ok": True, "skipped": "live_runner_disabled"}

    try:
        sess = (
            db.query(TradingAutomationSession)
            .filter(
                TradingAutomationSession.id == int(session_id),
                TradingAutomationSession.mode == "live",
            )
            .with_for_update(nowait=True)
            .one_or_none()
        )
    except Exception:
        return {"ok": True, "skipped": "concurrent_tick"}
    if sess is None:
        return {"ok": False, "error": "not_found"}
    if is_operator_paused(sess.risk_snapshot_json):
        return {"ok": True, "skipped": "operator_paused", "state": sess.state}
    ef = normalize_execution_family(sess.execution_family)
    if not momentum_runner_supports_execution_family(ef):
        return {"ok": True, "skipped": "execution_family_not_implemented", "execution_family": ef}
    try:
        factory = adapter_factory or resolve_live_spot_adapter_factory(ef)
    except ExecutionFamilyNotImplementedError:
        return {"ok": True, "skipped": "execution_family_not_implemented", "execution_family": ef}
    adapter = factory()
    if not adapter.is_enabled():
        return {"ok": True, "skipped": "coinbase_adapter_unavailable"}

    if sess.state not in LIVE_RUNNER_RUNNABLE_STATES:
        return {"ok": True, "skipped": "not_runnable", "state": sess.state}

    product_id = sess.symbol.upper().strip()
    if ef == EXECUTION_FAMILY_COINBASE_SPOT:
        # Coinbase crypto convention: ensure the BASE-USD pair suffix.
        if not product_id.endswith("-USD"):
            product_id = f"{product_id}-USD"
    # robinhood_spot: pass the symbol AS-IS — a bare equity ticker (AAPL, ARKK) or
    # an -USD RH-crypto pair. NEVER append -USD to an equity (that broke the entry:
    # AAPL -> AAPL-USD is not a Robinhood product).

    # C2: Orphaned order recovery — reconcile with venue (rate-limited)
    _reconcile_venue_position(adapter, db, sess, product_id)

    snap = dict(sess.risk_snapshot_json or {})
    if RISK_SNAPSHOT_KEY not in snap:
        _emit(db, sess, "live_error", {"reason": "missing_frozen_risk_snapshot"})
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        db.flush()
        return {"ok": False, "error": "missing_risk_snapshot"}

    le = _live_exec(snap)
    mid: float | None = None
    bid: float | None = None
    ask: float | None = None

    def _kill_switch_blocks_live() -> bool:
        pol = snap.get("momentum_risk_policy_summary") or {}
        if not pol.get("disable_live_if_governance_inhibit", True):
            return False
        return is_kill_switch_active()

    def _handle_kill_switch_mid_run() -> bool:
        """Safest effort: cancel open entry order; flatten if position recorded."""
        nonlocal le, snap
        if le.get("entry_order_id") and not le.get("position"):
            oid = str(le["entry_order_id"])
            cr = adapter.cancel_order(oid)
            _emit(db, sess, "live_order_cancelled", {"order_id": oid, "raw": cr})
        pos = le.get("position")
        if isinstance(pos, dict) and float(pos.get("quantity") or 0) > 0:
            pid = pos.get("product_id") or sess.symbol
            cid = f"chili_ml_x_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=str(pid),
                quantity=float(pos["quantity"]),
                client_order_id=cid,
                reason="kill_switch_flatten",
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"trigger": "kill_switch"},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="kill_switch_flatten"):
                return False
            _emit(db, sess, "live_exit_submitted", {"reason": "kill_switch", "result": sr})
            poll = _poll_live_exit_fill(
                db,
                sess,
                adapter,
                le=le,
                reason="kill_switch_flatten",
                quantity=float(pos["quantity"]),
            )
            if not poll.get("filled"):
                if poll.get("partial"):
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=float(pos.get("avg_entry_price") or bid or mid or 0.0),
                        fill_price=float(poll["fill_price"]),
                        reason="kill_switch_flatten",
                    )
                return False
            _complete_confirmed_live_exit(
                db,
                sess,
                le=le,
                quantity=float(pos["quantity"]),
                entry_price=float(pos.get("avg_entry_price") or bid or mid or 0.0),
                fill_price=float(poll["fill_price"]),
                reason="kill_switch_flatten",
                slip_bps=float(le.get("entry_slip_bps_ref") or 6.0),
                sell_result=sr,
            )
            return True
        _commit_le(sess, le)
        return True

    # ── Early kill switch (before venue reads) ───────────────────────────
    if _kill_switch_blocks_live() and sess.state in (
        STATE_ARMED_PENDING_RUNNER,
        STATE_QUEUED_LIVE,
        STATE_WATCHING_LIVE,
        STATE_LIVE_ENTRY_CANDIDATE,
    ):
        _emit(db, sess, "live_blocked_by_risk", {"reason": "kill_switch"})
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        db.flush()
        return {"ok": True, "blocked": True, "reason": "kill_switch"}

    via = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == int(sess.variant_id),
        )
        .one_or_none()
    )
    if not via:
        _emit(db, sess, "live_error", {"reason": "viability_missing"})
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        db.flush()
        return {"ok": False, "error": "no_viability"}
    variant = variant_for_id(db, int(sess.variant_id))
    params = normalize_strategy_params(
        variant.params_json if variant is not None else {},
        family_id=variant.family if variant is not None else None,
    )
    _ross_signal = ross_signal_for_symbol(via.execution_readiness_json, sess.symbol)
    _tick_scalp_evidence_ok = False
    _tick_scalp_evidence_reason = "tick_scalp_disabled"
    _tick_scalp_evidence_debug: dict[str, Any] = {}
    if bool(getattr(settings, "chili_momentum_tick_first_pullback_enabled", True)):
        (
            _tick_scalp_evidence_ok,
            _tick_scalp_evidence_reason,
            _tick_scalp_evidence_debug,
        ) = ross_tick_scalp_evidence_ok(_ross_signal)

    tick, _fr = adapter.get_best_bid_ask(product_id)
    if tick is None or tick.mid is None or tick.mid <= 0:
        _emit(db, sess, "live_blocked_by_risk", {"reason": "no_bbo"})
        if sess.state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE):
            _safe_transition(db, sess, STATE_LIVE_ERROR)
        db.flush()
        return {"ok": True, "blocked": True, "reason": "no_quote"}

    # Adaptive spread tolerance (no magic 12 bps): the BBO spread is a round-trip
    # cost, so gate it relative to how far THIS instrument actually moves (its
    # realized volatility). Explosive momentum names (Ross's universe) carry
    # wider absolute spreads that are still tiny vs. their move; we only ever
    # loosen above the documented floor. Ross/5-Pillars tick-scalp names derive
    # this from scanner evidence so they do not wait on a candle fetch.
    # (docs/DESIGN/MOMENTUM_LANE.md)
    _entry_df = None
    _expected_move_bps: float | None = None
    _adaptive_max_spread: float | None = None
    _needs_adaptive_quote_gate = _live_entry_quote_gate_applies(sess, le) or (
        sess.state == STATE_LIVE_ENTERED
        and (_float_or_none(le.get("anticipation_remainder_qty")) or 0.0) > 0.0
        and not le.get("anticipation_remainder_filled")
    )
    if _needs_adaptive_quote_gate:
        if _tick_scalp_evidence_ok:
            # Ross/5-Pillars evidence is already a volatility signal. Do not wait
            # on a 15m candle fetch before the quote gate for the micro-scalp path.
            _expected_move_bps = expected_move_bps_from_ross_signal(_ross_signal)
        else:
            try:
                from ..market_data import fetch_ohlcv_df

                _entry_df = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
            except Exception:
                _entry_df = None
            _expected_move_bps = _expected_move_bps_from_ohlcv(_entry_df)
        # The momentum lane SELECTS low-float explosives (universe low_float_bias),
        # so every candidate reaching the live entry gate is low-float by construction
        # — eligible for the CONVERSION FIX A fallback when the realized-move proxy is
        # cold. The fallback itself is config-gated (default on, abs-cap-bounded).
        _adaptive_max_spread = _adaptive_live_max_spread_bps(
            _expected_move_bps, is_low_float=True
        )
        _log.info(
            "[momentum_live] adaptive_spread symbol=%s state=%s expected_move_bps=%s max_spread_bps=%.2f",
            sess.symbol,
            sess.state,
            None if _expected_move_bps is None else round(_expected_move_bps, 2),
            _adaptive_max_spread,
        )

    quote_block = _quote_quality_block(tick, _fr, max_spread_bps=_adaptive_max_spread)
    if quote_block is not None:
        quote_block["expected_move_bps"] = (
            None if _expected_move_bps is None else round(_expected_move_bps, 4)
        )
        if _tick_scalp_evidence_ok:
            quote_block["tick_scalp_evidence"] = _tick_scalp_evidence_debug
        _emit(db, sess, "live_blocked_by_risk", quote_block)
        le["last_quote_quality_gate"] = quote_block
        _commit_le(sess, le)
        if sess.state in (STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_PENDING_ENTRY) and not le.get("entry_submitted"):
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
        if _live_entry_quote_gate_applies(sess, le):
            db.flush()
            return {"ok": True, "blocked": True, "reason": quote_block.get("reason")}

    mid = float(tick.mid)
    bid = float(tick.bid or mid)
    ask = float(tick.ask or mid)

    ok_b, ev = runner_boundary_risk_ok(db, sess, expected_move_bps=_expected_move_bps)
    if not ok_b:
        _emit(
            db,
            sess,
            "live_blocked_by_risk",
            {"severity": ev.get("severity"), "errors": ev.get("errors")},
        )
        if sess.state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE):
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": True, "blocked": True, "risk_evaluation": ev}
        if sess.state == STATE_LIVE_PENDING_ENTRY and le.get("entry_order_id") and not le.get("position"):
            adapter.cancel_order(str(le["entry_order_id"]))
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {"ok": True, "blocked": True, "risk_evaluation": ev}
        if _held_position_keeps_exit_on_boundary_fail(sess.state, le.get("position")):
            # BUGFIX: do NOT block a held position's stop/target on an entry-risk
            # refusal. Kill-switch still force-exits; otherwise fall through to the
            # exit handler below (it places no new entry/scale-in), so the stop is
            # always enforced even if viability went stale / a cap tripped.
            if _handle_kill_switch_mid_run():
                _safe_transition(db, sess, STATE_LIVE_EXITED)
                db.flush()
                return {"ok": True, "blocked": True, "risk_evaluation": ev}
            # fall through to exit management (no early return)
        else:
            db.flush()
            return {"ok": True, "blocked": True, "risk_evaluation": ev}

    if _kill_switch_blocks_live() and sess.state in (
        STATE_LIVE_PENDING_ENTRY,
        STATE_LIVE_ENTERED,
        STATE_LIVE_SCALING_OUT,
        STATE_LIVE_TRAILING,
        STATE_LIVE_BAILOUT,
    ):
        _emit(db, sess, "live_blocked_by_risk", {"reason": "kill_switch_mid_run"})
        if _handle_kill_switch_mid_run():
            _safe_transition(db, sess, STATE_LIVE_EXITED)
        le = _live_exec(dict(sess.risk_snapshot_json or {}))
        db.flush()
        return {"ok": True, "blocked": True, "reason": "kill_switch"}

    prod: Optional[NormalizedProduct] = None
    try:
        prod, _ = adapter.get_product(product_id)
    except Exception as ex:
        _log.debug("get_product: %s", ex)
    if prod and not prod.tradable_for_spot_momentum():
        _emit(db, sess, "live_error", {"reason": "product_not_tradable"})
        _safe_transition(db, sess, STATE_LIVE_ERROR)
        db.flush()
        return {"ok": False, "error": "product_not_tradable"}

    caps = _policy_caps(snap)
    max_notional = policy_float_cap(
        caps,
        "max_notional_per_trade_usd",
        settings.chili_momentum_risk_max_notional_per_trade_usd,
    )
    try:
        cap_max_hold = int(caps.get("max_hold_seconds") or settings.chili_momentum_risk_max_hold_seconds)
    except (TypeError, ValueError):
        cap_max_hold = int(settings.chili_momentum_risk_max_hold_seconds)
    max_hold = min(int(params.get("max_hold_seconds") or cap_max_hold), cap_max_hold)
    if le.get("entry_trigger_reason") == TICK_FIRST_PULLBACK_TRIGGER:
        try:
            tick_max_hold = float(
                le.get("tick_scalp_max_hold_seconds")
                or getattr(settings, "chili_momentum_tick_scalp_max_hold_seconds", 12.0)
                or 12.0
            )
            if math.isfinite(tick_max_hold) and tick_max_hold > 0:
                max_hold = min(float(max_hold), tick_max_hold)
        except (TypeError, ValueError):
            pass

    snap = dict(sess.risk_snapshot_json or {})
    le = _live_exec(snap)
    le["tick_count"] = int(le.get("tick_count") or 0) + 1
    le["last_mid"] = mid
    le["last_tick_utc"] = utc_iso()
    _commit_le(sess, le)
    snap = dict(sess.risk_snapshot_json or {})
    le = _live_exec(snap)

    st = sess.state

    if st == STATE_ARMED_PENDING_RUNNER:
        _safe_transition(db, sess, STATE_QUEUED_LIVE)
        _emit(db, sess, "live_runner_queued", {"symbol": sess.symbol})
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_QUEUED_LIVE:
        _safe_transition(db, sess, STATE_WATCHING_LIVE)
        _emit(db, sess, "live_runner_started", {"mid": mid})
        _emit(db, sess, "live_watch_started", {"product_id": product_id})
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_WATCHING_LIVE:
        _score_ok = (
            float(via.viability_score or 0) >= float(params["entry_viability_min"])
            and via.live_eligible
        )
        # M4.1: require an active momentum-continuation trigger (price > EMA-9 +
        # volume surge) on top of the viability score — Ross enters on confirmed
        # strength, never on a stale score. No confirmation -> WAIT this tick.
        # (docs/DESIGN/MOMENTUM_LANE.md)
        # M4.2: trigger mode (config, default "hybrid") — Ross-style pullback-break
        # on 1m/5m (price breaks the pullback high after a shallow, EMA-9-holding
        # pullback, with a volume spike) PREFERRED, with momentum_volume (15m
        # price>EMA-9 + volume) as the fallback. live + on, fallback-safe.
        _trigger_ok, _trigger_reason = True, "score_only"
        _pb_debug = {}
        if _score_ok:
            _trigger_ok, _trigger_reason = False, "trigger_wait"
            if _tick_scalp_evidence_ok:
                try:
                    _tick_decision = evaluate_tick_first_pullback(
                        symbol=sess.symbol,
                        signal=_ross_signal,
                        state=le.get("tick_scalp_state") if isinstance(le.get("tick_scalp_state"), dict) else None,
                        bid=bid,
                        ask=ask,
                        mid=mid,
                        min_pullback_bps=float(
                            getattr(settings, "chili_momentum_tick_first_pullback_min_pullback_bps", 35.0)
                            or 35.0
                        ),
                        max_pullback_bps=float(
                            getattr(settings, "chili_momentum_tick_first_pullback_max_pullback_bps", 1800.0)
                            or 1800.0
                        ),
                        min_reclaim_bps=float(
                            getattr(settings, "chili_momentum_tick_first_pullback_min_reclaim_bps", 8.0)
                            or 8.0
                        ),
                        stop_buffer_bps=float(
                            getattr(settings, "chili_momentum_tick_first_pullback_stop_buffer_bps", 12.0)
                            or 12.0
                        ),
                        max_hold_seconds=float(
                            getattr(settings, "chili_momentum_tick_scalp_max_hold_seconds", 12.0)
                            or 12.0
                        ),
                    )
                    le["tick_scalp_state"] = _tick_decision.state
                    le["last_tick_scalp_debug"] = _tick_decision.debug
                    _commit_le(sess, le)
                    if _tick_decision.fire:
                        _trigger_ok = True
                        _trigger_reason = _tick_decision.reason
                        _pb_debug = dict(_tick_decision.debug)
                    else:
                        _trigger_ok = False
                        _trigger_reason = _tick_decision.reason
                except Exception:
                    _trigger_ok, _trigger_reason = False, "tick_scalp_error_wait"
            try:
                from .entry_gates import momentum_pullback_trigger, momentum_volume_confirmation
                from ..market_data import fetch_ohlcv_df

                _mode = str(getattr(settings, "chili_momentum_entry_trigger_mode", "hybrid") or "hybrid").lower()
                _interval = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                if not _trigger_ok:
                    _trigger_ok, _trigger_reason = False, _trigger_reason or "trigger_wait"
                if not _trigger_ok and _mode in ("hybrid", "pullback_break"):
                    try:
                        _df_pb = fetch_ohlcv_df(sess.symbol, interval=_interval, period="5d")
                        if _df_pb is not None and not getattr(_df_pb, "empty", True):
                            # Shared trigger (parity): paper calls the SAME helper, so
                            # both paths take the identical Ross pullback-break entry
                            # (vol-aware, candle/VWAP/MACD, runaway). docs/DESIGN/MOMENTUM_LANE.md §8
                            _trigger_ok, _trigger_reason, _pb_debug = momentum_pullback_trigger(
                                _df_pb, entry_interval=_interval
                            )
                    except Exception:
                        _trigger_ok = False
                if not _trigger_ok and _mode != "pullback_break":
                    _df = _entry_df  # reuse the adaptive-spread 15m candles if present
                    if _df is None:
                        _df = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
                    if _df is None or getattr(_df, "empty", True):
                        _trigger_ok, _trigger_reason = False, "no_data_wait"
                    else:
                        _trigger_ok, _trigger_reason = momentum_volume_confirmation(_df)
            except Exception:
                _trigger_ok, _trigger_reason = False, "trigger_error_wait"
        # Equities can enter pre/post only when the explicit extended-hours switch is on.
        _mkt_open = True
        try:
            from .market_profile import market_open_now

            _mkt_open = bool(
                market_open_now(
                    sess.symbol,
                    allow_extended_hours=bool(getattr(settings, "chili_autotrader_allow_extended_hours", False)),
                )
            )
        except Exception:
            _mkt_open = True
        if _score_ok and _trigger_ok and _mkt_open:
            # Ross structural stop: when the pullback-break trigger fired, stash the
            # pullback low so sizing + placement can stop just UNDER the structure
            # (not at a noise-tight ATR). The momentum_volume fallback has no
            # structure -> clear it so the vol-floored ATR stop is used instead.
            if _trigger_reason == TICK_FIRST_PULLBACK_TRIGGER:
                if _pb_debug.get("structural_stop_price"):
                    le["structural_stop_price"] = float(_pb_debug["structural_stop_price"])
                elif _pb_debug.get("pullback_low"):
                    le["structural_stop_price"] = float(_pb_debug["pullback_low"])
                if _pb_debug.get("breakout_level_price"):
                    le["breakout_level_price"] = float(_pb_debug["breakout_level_price"])
                elif _pb_debug.get("reclaim_level"):
                    le["breakout_level_price"] = float(_pb_debug["reclaim_level"])
                else:
                    le.pop("breakout_level_price", None)
                if _pb_debug.get("max_hold_seconds"):
                    le["tick_scalp_max_hold_seconds"] = float(_pb_debug["max_hold_seconds"])
            elif _trigger_reason == "pullback_break_ok" and _pb_debug.get("pullback_low"):
                le["structural_stop_price"] = float(_pb_debug["pullback_low"])
                # #2 Breakout-or-bailout: stash the broken pullback HIGH (the breakout
                # level) so the held-position handler can fast-bail if it fails to hold
                # shortly after entry. Cleared on the momentum_volume fallback (which
                # has no structural level). (docs/DESIGN/MOMENTUM_LANE.md §8)
                if _pb_debug.get("pullback_high"):
                    le["breakout_level_price"] = float(_pb_debug["pullback_high"])
                else:
                    le.pop("breakout_level_price", None)
            else:
                le.pop("structural_stop_price", None)
                le.pop("breakout_level_price", None)
            le["entry_trigger_reason"] = _trigger_reason
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
            _emit(
                db, sess, "live_entry_candidate_detected",
                {"viability_score": via.viability_score, "trigger": _trigger_reason,
                 "structural_stop": le.get("structural_stop_price")},
            )
            if _trigger_reason == TICK_FIRST_PULLBACK_TRIGGER:
                _safe_transition(db, sess, STATE_LIVE_PENDING_ENTRY)
                st = STATE_LIVE_PENDING_ENTRY
                _emit(db, sess, "live_entry_submitted", {"note": "tick_scalp_same_tick_pending"})
        elif _score_ok and not _mkt_open:
            _emit(db, sess, "live_entry_wait_market_closed", {"symbol": sess.symbol})
        elif _score_ok:
            _emit(db, sess, "live_entry_trigger_wait", {"reason": _trigger_reason})
        if st != STATE_LIVE_PENDING_ENTRY:
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_ENTRY_CANDIDATE:
        if float(via.viability_score or 0) < float(params["entry_revalidate_floor"]) or not via.live_eligible:
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
        else:
            _safe_transition(db, sess, STATE_LIVE_PENDING_ENTRY)
            _emit(db, sess, "live_entry_submitted", {"note": "pending_place"})
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_PENDING_ENTRY:
        if not le.get("entry_submitted") and (
            float(via.viability_score or 0) < float(params["entry_revalidate_floor"]) or not via.live_eligible
        ):
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}
        if le.get("entry_submitted") and le.get("entry_order_id"):
            no, _ = adapter.get_order(str(le["entry_order_id"]))
            if no and _order_done_for_entry(no):
                avg = float(no.average_filled_price or ask)
                filled = float(no.filled_size or 0.0)
                if filled <= 0:
                    _emit(db, sess, "live_error", {"reason": "zero_fill"})
                    _safe_transition(db, sess, STATE_LIVE_ERROR)
                    db.flush()
                    return {"ok": False, "error": "zero_fill"}
                if le.get("anticipation_remainder_state") == "waiting":
                    full_qty = _float_or_none(le.get("anticipation_full_qty"))
                    if full_qty is not None and full_qty > 0.0:
                        inc_fill = prod.base_increment if prod else None
                        mn_fill = prod.base_min_size if prod else None
                        rem = _round_base_size(max(0.0, full_qty - filled), inc_fill, mn_fill)
                        le["anticipation_probe_filled_qty"] = filled
                        le["anticipation_remainder_qty"] = rem
                        if rem <= 0.0:
                            le["anticipation_remainder_state"] = "none"
                            le["anticipation_remainder_filled"] = True
                le["position"] = {
                    "product_id": product_id,
                    "side": "long",
                    "quantity": filled,
                    "original_quantity": filled,
                    "intended_full_quantity": _float_or_none(le.get("anticipation_full_qty")) or filled,
                    "avg_entry_price": avg,
                    "notional_usd": filled * avg,
                    "opened_at_utc": _utcnow().isoformat(),
                    "high_water_mark": avg,
                    "stop_price": None,
                    "target_price": None,
                }
                regime = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
                _stop_atr_mult = float(params["stop_atr_mult"])
                # Reuse the vol-floored ATR frozen at sizing time so the ACTUAL stop
                # matches the stop the qty was risk-sized against (else a wider stop
                # would over-risk, a narrower one would re-introduce the shake-out).
                atrp = le.get("entry_stop_atr_pct")
                if not atrp or float(atrp) <= 0:
                    atrp = effective_stop_atr_pct(
                        regime_atr_pct(regime), _expected_move_bps,
                        stop_atr_mult=_stop_atr_mult, vol_floor_mult=_stop_vol_floor_mult(),
                    )
                stop_px, target_px = stop_target_prices(
                    avg,
                    atr_pct=float(atrp),
                    side_long=True,
                    stop_atr_mult=_stop_atr_mult,
                    target_atr_mult=float(params["target_atr_mult"]),
                )
                le["position"]["stop_price"] = stop_px
                le["position"]["target_price"] = target_px
                le["admission_viability_score"] = float(via.viability_score or 0)
                _commit_le(sess, le)
                if le.get("entry_decision_packet_id"):
                    try:
                        mark_packet_executed(db, int(le["entry_decision_packet_id"]))
                    except Exception:
                        _log.debug("mark_packet_executed live skipped session=%s", sess.id, exc_info=True)
                _record_live_entry_ledger_safe(db, sess, le=le, quantity=filled, fill_price=avg)
                _safe_transition(db, sess, STATE_LIVE_ENTERED)
                _emit(
                    db,
                    sess,
                    "live_entry_filled",
                    {
                        "order_id": no.order_id,
                        "avg": avg,
                        "filled_size": filled,
                    },
                )
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            if no and _order_open(no):
                # C3: Ack timeout — cancel if pending too long
                submit_raw = le.get("entry_submit_utc")
                if submit_raw:
                    try:
                        t_sub = datetime.fromisoformat(str(submit_raw).replace("Z", "+00:00")).replace(tzinfo=None)
                        if (_utcnow() - t_sub).total_seconds() > 10:
                            adapter.cancel_order(str(le["entry_order_id"]))
                            _emit(db, sess, "entry_ack_timeout", {"elapsed_sec": (_utcnow() - t_sub).total_seconds()})
                            _safe_transition(db, sess, STATE_WATCHING_LIVE)
                            le["entry_submitted"] = False
                            le["entry_order_id"] = None
                            _commit_le(sess, le)
                            db.flush()
                            return {"ok": True, "session_id": sess.id, "state": sess.state, "timeout": True}
                    except Exception:
                        pass
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "pending": "entry_open"}
            _emit(db, sess, "live_error", {"reason": "entry_order_state", "status": no.status if no else None})
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": "bad_entry_order"}

        # Submit entry once (duplicate guard)
        if le.get("entry_submitted"):
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # P1.2 — venue health circuit breaker. Gate new entries when the
        # venue's rolling-window latency / error rate crosses the threshold.
        # Fires BEFORE autopilot_mutex because venue-sick is the more
        # fundamental "stop" signal. Fails open (flag off or exception →
        # healthy) so unwired environments behave unchanged. Falling back to
        # STATE_WATCHING_LIVE keeps the session alive for retry on the next
        # pulse once the venue recovers.
        try:
            from ..venue.venue_health import (
                is_venue_degraded,
                should_auto_switch_to_paper,
                venue_degraded_reason,
                canonicalize_venue,
            )
            _venue_key = canonicalize_venue(ef)
            if is_venue_degraded(db, venue=_venue_key):
                _reason = venue_degraded_reason(db, venue=_venue_key) or "unknown"
                _auto_paper = should_auto_switch_to_paper(db, venue=_venue_key)
                _emit(
                    db,
                    sess,
                    "live_entry_blocked_by_venue_degraded",
                    {
                        "venue": _venue_key,
                        "reason": _reason,
                        "auto_switch_to_paper": _auto_paper,
                    },
                )
                if _auto_paper:
                    # Flip to paper so the session stays productive instead
                    # of stalling. Paper mode writes no events so has no
                    # effect on the venue health signal — recovery detected
                    # via live events from other sessions / manual traffic.
                    try:
                        sess.mode = "paper"
                    except Exception:
                        pass
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
                db.flush()
                return {
                    "ok": True,
                    "session_id": sess.id,
                    "state": sess.state,
                    "blocked": True,
                    "reason": "venue_degraded",
                }
        except Exception:
            # Defensive: never let a venue-health failure stall the live
            # runner. Log and continue — the rate limiter + idempotency
            # store handle the worst-case retry scenarios.
            pass

        # P0.4 — autopilot mutual exclusion. Our own active session counts as
        # the lease holder (owner_self → allowed), so this only blocks when
        # an AutoTrader v1 live Trade is already open on the same symbol/user.
        gate = check_autopilot_entry_gate(
            db,
            candidate=AUTOPILOT_MOMENTUM_NEURAL,
            symbol=sess.symbol,
            user_id=sess.user_id,
        )
        if not gate.get("allowed"):
            _emit(
                db,
                sess,
                "live_entry_blocked_by_autopilot_mutex",
                {
                    "reason": gate.get("reason"),
                    "owner": gate.get("owner"),
                    "primary": gate.get("primary"),
                    "strict": gate.get("strict"),
                },
            )
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True,
                "session_id": sess.id,
                "state": sess.state,
                "blocked": True,
                "reason": "autopilot_mutex",
            }

        # P1.4 — runtime feature-parity assertion at entry. Fetches fresh
        # OHLCV and verifies the live indicator snapshot matches the
        # canonical compute_all_from_df output. Fails open on any error /
        # flag-off so unwired environments behave identically. Soft mode
        # records + alerts without blocking; hard mode blocks critical drift.
        #
        # CRITICAL: short-circuit on the feature flag BEFORE any imports or
        # OHLCV fetch. The flag defaults OFF, so every live session fires
        # through this block — without the pre-flag guard, unwired
        # environments pay a network fetch per entry attempt. On Windows the
        # test suite observed this exhausting the ephemeral socket pool
        # (WinError 10055) under the autopilot mutex regression runs.
        _parity_blocked_feature_parity = False
        if bool(getattr(settings, "chili_feature_parity_enabled", False)):
            try:
                from ..feature_parity import (
                    DEFAULT_FEATURES as _PARITY_FEATURES,
                    check_entry_feature_parity as _check_parity,
                )
                from ..indicator_core import compute_all_from_df as _pc_compute
                from ..market_data import fetch_ohlcv_df as _pc_fetch
                _pc_df = _pc_fetch(sess.symbol, "1h", "30d")
                if _pc_df is not None and not _pc_df.empty:
                    _pc_arrays = _pc_compute(_pc_df, needed=set(_PARITY_FEATURES))
                    _pc_live: dict[str, Any] = {}
                    for _k, _v in _pc_arrays.items():
                        if isinstance(_v, list) and _v and _v[-1] is not None:
                            _pc_live[_k] = _v[-1]
                    _pc_venue = "coinbase" if str(ef).lower() in ("crypto", "coinbase_spot", "coinbase") else "robinhood"
                    _pc_result = _check_parity(
                        db,
                        ticker=sess.symbol,
                        live_snap=_pc_live,
                        reference_df=_pc_df,
                        features=_PARITY_FEATURES,
                        source="momentum_neural",
                        scan_pattern_id=getattr(variant, "scan_pattern_id", None),
                        venue=_pc_venue,
                    )
                    if not _pc_result.ok:
                        _emit(
                            db,
                            sess,
                            "live_entry_blocked_by_feature_parity",
                            {
                                "severity": _pc_result.severity,
                                "mode": _pc_result.mode,
                                "n_mismatches": _pc_result.n_mismatches,
                                "reason": _pc_result.reason,
                                "record_id": _pc_result.record_id,
                            },
                        )
                        _safe_transition(db, sess, STATE_WATCHING_LIVE)
                        db.flush()
                        _parity_blocked_feature_parity = True
            except Exception:
                # Defensive: never let a parity-check failure stall the live
                # runner. The check is an observability net, not a safety gate.
                pass
        if _parity_blocked_feature_parity:
            return {
                "ok": True,
                "session_id": sess.id,
                "state": sess.state,
                "blocked": True,
                "reason": "feature_parity",
            }

        regime_live = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
        ex_live = via.execution_readiness_json if isinstance(via.execution_readiness_json, dict) else {}
        try:
            spread_bps_live = float(ex_live.get("spread_bps") or 8.0)
        except (TypeError, ValueError):
            spread_bps_live = 8.0
        try:
            slip_ref = float(ex_live.get("slippage_estimate_bps") or 6.0)
        except (TypeError, ValueError):
            slip_ref = 6.0

        decision_packet_id = None
        if bool(getattr(settings, "brain_enable_decision_ledger", True)):
            dec = run_momentum_entry_decision(
                db,
                session=sess,
                viability=via,
                variant=variant,
                user_id=sess.user_id,
                max_notional_policy=float(max_notional),
                quote_mid=mid,
                spread_bps=spread_bps_live,
                execution_mode="live",
                regime_snapshot=regime_live,
            )
            if not dec.get("proceed"):
                alloc = dec.get("allocation") or {}
                _emit(
                    db,
                    sess,
                    "live_entry_abstain",
                    {
                        "packet_id": dec.get("packet_id"),
                        "reason": alloc.get("abstain_reason_code"),
                        "detail": alloc.get("abstain_reason_text"),
                    },
                )
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "abstained": True}
            decision_packet_id = dec.get("packet_id")
            max_notional = min(float(max_notional), float(dec["allocation"]["recommended_notional"]))
        if bool(getattr(settings, "brain_decision_packet_required_for_runners", True)) and decision_packet_id is None:
            _emit(db, sess, "live_error", {"reason": "decision_packet_required_missing"})
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": "decision_packet_missing"}
        le["entry_decision_packet_id"] = decision_packet_id
        le["entry_slip_bps_ref"] = slip_ref
        _commit_le(sess, le)
        snap = dict(sess.risk_snapshot_json or {})
        le = _live_exec(snap)

        inc = prod.base_increment if prod else None
        mn = prod.base_min_size if prod else None
        guarded_ask = ask * _notional_guard_multiplier()
        # Risk-first sizing (Ross-style): qty = per-trade max-loss / stop distance,
        # capped at the (conviction-scaled, equity-relative) notional ceiling — a
        # tighter stop buys MORE size at constant risk. Falls back to notional-first
        # when ATR/inputs are unusable. (docs/DESIGN/MOMENTUM_LANE.md)
        _regime = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
        _stop_atr_mult = float(params.get("stop_atr_mult") or 0.60)
        # Vol-floored stop ATR-pct: never tighter than vol_floor_mult x the live
        # expected-move, so the stop sits OUTSIDE the intraday noise (the KAIO
        # shake-out fix). Frozen in le and reused at the post-fill stop so sizing
        # and the actual stop agree. (docs/DESIGN/MOMENTUM_LANE.md)
        _eff_atr_pct = effective_stop_atr_pct(
            regime_atr_pct(_regime), _expected_move_bps,
            stop_atr_mult=_stop_atr_mult, vol_floor_mult=_stop_vol_floor_mult(),
        )
        # Ross structural stop: if the pullback-break captured a pullback low, stop
        # just UNDER that structure instead of a noise-tight ATR — but never TIGHTER
        # than the vol floor (shake-out guard). Risk-first sizing then trims qty
        # against the wider, structure-aware distance (constant $risk); the 2:1
        # target auto-scales off the actual stop distance. Fix for the lane's
        # all-stop-out streak (every exit flagged stop_too_tight). MOMENTUM_LANE.md
        _eff_atr_pct, _stop_model = structural_or_vol_floored_atr_pct(
            vol_floored_atr_pct=_eff_atr_pct,
            structural_stop_price=le.get("structural_stop_price"),
            entry_price=guarded_ask,
            stop_atr_mult=_stop_atr_mult,
        )
        le["entry_stop_atr_pct"] = _eff_atr_pct
        le["entry_stop_model"] = _stop_model
        if _stop_model == "structural_pullback":
            le["structural_stop_atr_pct"] = round(_eff_atr_pct, 6)
        _rf_qty, _rf_meta = compute_risk_first_quantity(
            entry_price=guarded_ask,
            atr_pct=_eff_atr_pct,
            max_loss_usd=policy_float_cap(
                caps, "max_loss_per_trade_usd", settings.chili_momentum_risk_max_loss_per_trade_usd
            ),
            max_notional_ceiling_usd=max_notional,
            base_increment=inc,
            base_min_size=mn,
            stop_atr_mult=_stop_atr_mult,
        )
        if _rf_qty and _rf_qty > 0:
            qty = _rf_qty
            le["entry_sizing"] = _rf_meta
        else:
            qty = _round_base_size(max_notional / guarded_ask, inc, mn)
            le["entry_sizing"] = {"model": "notional_first_fallback", "reason": _rf_meta.get("reason")}
        if qty <= 0:
            _emit(db, sess, "live_error", {"reason": "size_zero_after_rounding"})
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": "size_zero"}
        estimated_guarded_notional = qty * guarded_ask
        if estimated_guarded_notional > max_notional + 1e-9:
            _emit(
                db,
                sess,
                "live_entry_blocked_by_notional_cap",
                {
                    "max_notional_usd": max_notional,
                    "estimated_guarded_notional_usd": estimated_guarded_notional,
                    "ask": ask,
                    "guarded_ask": guarded_ask,
                    "quantity": qty,
                    "decision_packet_id": decision_packet_id,
                },
            )
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True,
                "session_id": sess.id,
                "state": sess.state,
                "blocked": True,
                "reason": "notional_cap",
            }
        starter_plan = _anticipation_starter_plan(
            full_qty=qty,
            base_increment=inc,
            base_min_size=mn,
            le=le,
            bid=bid,
            ask=ask,
            mid=mid,
            snap=snap,
        )
        submit_qty = _float_or_none(starter_plan.get("probe_qty")) or qty
        if starter_plan.get("enabled"):
            _clear_anticipation_entry_state(le)
            le["anticipation_full_qty"] = float(starter_plan["full_qty"])
            le["anticipation_probe_qty"] = float(starter_plan["probe_qty"])
            le["anticipation_remainder_qty"] = float(starter_plan["remainder_qty"])
            le["anticipation_remainder_state"] = "waiting"
            le["anticipation_starter_plan"] = starter_plan
            _emit(
                db,
                sess,
                "live_anticipation_probe_sized",
                {
                    "full_qty": starter_plan["full_qty"],
                    "probe_qty": starter_plan["probe_qty"],
                    "remainder_qty": starter_plan["remainder_qty"],
                    "starter_fraction": starter_plan.get("starter_fraction"),
                    "confirmation_strength": starter_plan.get("confirmation_strength"),
                    "confirmation_legs": starter_plan.get("confirmation_legs"),
                    "trigger": le.get("entry_trigger_reason"),
                },
            )
        else:
            _clear_anticipation_entry_state(le)
        estimated_submitted_notional = submit_qty * guarded_ask
        le["entry_notional_guard"] = {
            "max_notional_usd": max_notional,
            "ask": ask,
            "bid": bid,
            "mid": mid,
            "guarded_ask": guarded_ask,
            "estimated_guarded_notional_usd": estimated_guarded_notional,
            "estimated_submitted_notional_usd": estimated_submitted_notional,
            "intended_full_quantity": qty,
            "quantity": submit_qty,
            "order_type": "market",
            "spread_bps": spread_bps_live,
            "slippage_bps_ref": slip_ref,
            "anticipation": starter_plan if starter_plan.get("enabled") else None,
        }
        record_packet_execution_intent(
            db,
            decision_packet_id,
            {
                "surface": "momentum_live_runner_entry",
                "order_type": "market",
                "side": "buy",
                "product_id": product_id,
                "bid": bid,
                "ask": ask,
                "mid": mid,
                "spread_bps": spread_bps_live,
                "slippage_bps_ref": slip_ref,
                "max_notional_usd": max_notional,
                "guarded_ask": guarded_ask,
                "estimated_guarded_notional_usd": estimated_submitted_notional,
                "intended_full_estimated_guarded_notional_usd": estimated_guarded_notional,
                "intended_full_quantity": qty,
                "quantity": submit_qty,
                "base_increment": inc,
                "base_min_size": mn,
                "notional_guard_multiplier": _notional_guard_multiplier(),
                "anticipation": starter_plan if starter_plan.get("enabled") else None,
            },
        )
        _commit_le(sess, le)

        cid = f"chili_ml_e_{sess.id}_{(sess.correlation_id or 'x')[:8]}_{uuid.uuid4().hex[:10]}"[:120]
        res = adapter.place_market_order(
            product_id=product_id,
            side="buy",
            base_size=_fmt_base_size(submit_qty),
            client_order_id=cid,
        )
        le["entry_submitted"] = True
        le["entry_submit_utc"] = _utcnow().isoformat()
        le["entry_client_order_id"] = res.get("client_order_id") or cid
        le["entry_order_id"] = res.get("order_id")
        le["entry_place_result"] = {"ok": res.get("ok"), "error": res.get("error")}
        _commit_le(sess, le)
        _emit(
            db,
            sess,
            "live_entry_submitted",
            {
                "client_order_id": le["entry_client_order_id"],
                "result": res,
                "quantity": submit_qty,
                "intended_full_quantity": qty,
                "anticipation_remainder_qty": le.get("anticipation_remainder_qty"),
            },
        )
        if not res.get("ok"):
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": res.get("error") or "place_failed"}
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING, STATE_LIVE_BAILOUT):
        pos = le.get("position")
        if not isinstance(pos, dict):
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": "position_missing"}

        qty = float(pos["quantity"])
        avg = float(pos["avg_entry_price"])
        stop_px = float(pos["stop_price"])
        target_px = float(pos["target_price"])
        # Ross runner: track the high-water mark (peak bid) each tick so the
        # trailing chandelier stop can ratchet up off it. Frozen in the position.
        _hwm_prev = _float_or_none(pos.get("high_water_mark"))
        _hwm = max(_hwm_prev if _hwm_prev is not None else avg, float(bid))
        if _hwm_prev is None or _hwm > _hwm_prev:
            pos["high_water_mark"] = _hwm
            le["position"] = pos
            _commit_le(sess, le)
        pending_exit_reason = le.get("pending_exit_reason")
        if pending_exit_reason:
            try:
                pending_qty = float(le.get("pending_exit_quantity") or qty)
            except (TypeError, ValueError):
                pending_qty = qty
            pending_qty = min(max(pending_qty, 0.0), qty)
            is_scale_out = bool(le.get("pending_exit_is_scale_out"))
            if bool(le.get("pending_exit_presubmit_deferred")) and not le.get("exit_order_id"):
                pending_product_id = str(le.get("pending_exit_product_id") or product_id)
                pending_client_order_id = str(
                    le.get("pending_exit_client_order_id")
                    or f"chili_ml_x_{sess.id}_{uuid.uuid4().hex[:12]}"
                )
                market = _exit_market_window(getattr(sess, "symbol", None))
                if not bool(market.get("is_tradable")):
                    _defer_live_exit_submit_until_tradable(
                        db,
                        sess,
                        le=le,
                        product_id=pending_product_id,
                        quantity=pending_qty,
                        client_order_id=pending_client_order_id,
                        reason=str(pending_exit_reason),
                        bid=bid,
                        ask=ask,
                        mid=mid,
                        market=market,
                        extra={"deferred_recheck": True},
                    )
                    db.flush()
                    return {
                        "ok": True,
                        "session_id": sess.id,
                        "state": sess.state,
                        "pending_exit": True,
                        "exit_deferred": True,
                    }
                sr = _submit_live_market_exit(
                    db,
                    sess,
                    adapter,
                    le=le,
                    product_id=pending_product_id,
                    quantity=pending_qty,
                    client_order_id=pending_client_order_id,
                    reason=str(pending_exit_reason),
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    extra={"deferred_release": True},
                )
                if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason=str(pending_exit_reason)):
                    db.flush()
                    return {
                        "ok": bool(sr.get("deferred")),
                        "session_id": sess.id,
                        "state": sess.state,
                        "pending_exit": bool(sr.get("deferred")),
                        "exit_deferred": bool(sr.get("deferred")),
                        "exit_submit_failed": not bool(sr.get("deferred")),
                    }
                if is_scale_out:
                    le["pending_exit_is_scale_out"] = True
                    _commit_le(sess, le)
            poll = _poll_live_exit_fill(
                db,
                sess,
                adapter,
                le=le,
                reason=str(pending_exit_reason),
                quantity=pending_qty,
            )
            if poll.get("filled"):
                slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
                if is_scale_out:
                    # Deliberate first-target scale-out confirmed on a later tick:
                    # bank the partial, move the balance to breakeven, hold the runner.
                    _scale_out_to_runner(
                        db,
                        sess,
                        le=le,
                        filled_quantity=pending_qty,
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=str(pending_exit_reason),
                    )
                else:
                    _complete_confirmed_live_exit(
                        db,
                        sess,
                        le=le,
                        quantity=pending_qty,
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=str(pending_exit_reason),
                        slip_bps=slip_live,
                    )
            elif poll.get("partial"):
                if is_scale_out:
                    _scale_out_to_runner(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=str(pending_exit_reason),
                    )
                else:
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=str(pending_exit_reason),
                    )
            db.flush()
            return {
                "ok": bool(poll.get("filled") or poll.get("partial") or poll.get("pending")),
                "session_id": sess.id,
                "state": sess.state,
                "pending_exit": bool(poll.get("pending")),
                "partial_exit": bool(poll.get("partial")),
                "exit_failed": bool(poll.get("failed")),
            }
        opened_raw = pos.get("opened_at_utc")
        try:
            t0 = datetime.fromisoformat(str(opened_raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            t0 = _utcnow()
        held = (_utcnow() - t0).total_seconds()

        if st == STATE_LIVE_ENTERED and (not pos.get("partial_taken")):
            _remainder_market_open = True
            if ef != EXECUTION_FAMILY_COINBASE_SPOT:
                try:
                    from .market_profile import market_open_now

                    _remainder_market_open = bool(
                        market_open_now(
                            sess.symbol,
                            allow_extended_hours=bool(
                                getattr(settings, "chili_autotrader_allow_extended_hours", False)
                            ),
                        )
                    )
                except Exception:
                    _remainder_market_open = False
            _rem_result = _handle_anticipation_remainder(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                tick=tick,
                freshness=_fr,
                bid=bid,
                ask=ask,
                mid=mid,
                max_spread_bps=_adaptive_max_spread,
                boundary_ok=ok_b,
                market_open=_remainder_market_open,
                max_notional=max_notional,
                guarded_ask=ask * _notional_guard_multiplier(),
                params=params,
                held_seconds=held,
                snap=snap,
            )
            if _rem_result.get("submitted") or _rem_result.get("pending") or _rem_result.get("filled"):
                db.flush()
                return {
                    "ok": True,
                    "session_id": sess.id,
                    "state": sess.state,
                    "anticipation_remainder": _rem_result,
                }
            pos = le.get("position") if isinstance(le.get("position"), dict) else pos
            qty = float(pos["quantity"])
            avg = float(pos["avg_entry_price"])
            stop_px = float(pos["stop_price"])
            target_px = float(pos["target_price"])
        trail_activate_return = 1.0 + float(params["trail_activate_return_bps"]) / 10_000.0

        # C1: Per-trade loss enforcement
        max_loss_usd = float(caps.get("max_loss_per_trade_usd") or 0)
        if max_loss_usd > 0 and st != STATE_LIVE_BAILOUT:
            unrealized_pnl = (bid - avg) * qty
            if unrealized_pnl <= -max_loss_usd:
                _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                _emit(db, sess, "live_bailout", {"reason": "max_loss_per_trade", "unrealized_pnl": unrealized_pnl})
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}

        # #2 Breakout-or-bailout fast exit (Ross flat-top): within the early window
        # after a pullback_break entry, if the broken breakout level fails to HOLD on
        # the bid, cut NOW — well inside the structural stop — reusing the BAILOUT
        # machinery (the next tick flattens). Guarded so it never fights the normal
        # stop/target: only with a recorded breakout level (pullback_break entry, not
        # the momentum_volume fallback), only while plainly ENTERED (scaling/trailing
        # are already past target/in profit), and only inside the time window.
        if (
            st == STATE_LIVE_ENTERED
            and bool(getattr(settings, "chili_momentum_breakout_bailout_enabled", True))
            and breakout_failed_to_hold(
                breakout_level=le.get("breakout_level_price"),
                bid=bid,
                held_seconds=held,
                window_seconds=_breakout_bailout_window_seconds(),
                buffer_pct=float(getattr(settings, "chili_momentum_breakout_bailout_buffer_pct", 0.001) or 0.0),
            )
        ):
            le["last_bailout_trigger"] = "breakout_failed_to_hold"
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_BAILOUT)
            _emit(db, sess, "live_bailout", {
                "reason": "breakout_failed_fast_bail",
                "breakout_level": le.get("breakout_level_price"),
                "bid": bid,
                "held_seconds": held,
                "window_seconds": _breakout_bailout_window_seconds(),
            })
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_BAILOUT:
            cid = f"chili_ml_b_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=qty,
                client_order_id=cid,
                reason="bailout",
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"unrealized_pnl_usd": (bid - avg) * qty},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="bailout"):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason="bailout", quantity=qty)
            if not poll.get("filled"):
                if poll.get("partial"):
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason="bailout",
                    )
                db.flush()
                return {
                    "ok": bool(poll.get("pending") or poll.get("partial")),
                    "session_id": sess.id,
                    "state": sess.state,
                    "pending_exit": bool(poll.get("pending")),
                    "partial_exit": bool(poll.get("partial")),
                    "exit_failed": bool(poll.get("failed")),
                }
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            _complete_confirmed_live_exit(
                db,
                sess,
                le=le,
                quantity=qty,
                entry_price=avg,
                fill_price=float(poll["fill_price"]),
                reason="bailout",
                slip_bps=slip_live,
                sell_result=sr,
            )
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if float(via.viability_score or 0) < float(params["bailout_viability_floor"]):
            _safe_transition(db, sess, STATE_LIVE_BAILOUT)
            _emit(db, sess, "live_bailout", {"viability_score": via.viability_score})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # C4: Viability degradation — tighten stop if score drops >15% from admission
        admission_via = float(le.get("admission_viability_score") or 0)
        current_via = float(via.viability_score or 0)
        if admission_via > 0 and current_via < admission_via * 0.85:
            tighter_stop = max(stop_px, avg * 0.995)
            if tighter_stop > stop_px:
                pos["stop_price"] = tighter_stop
                _commit_le(sess, le)
                _emit(db, sess, "viability_degraded_tighten", {
                    "admission_viability": admission_via,
                    "current_viability": current_via,
                    "old_stop": stop_px,
                    "new_stop": tighter_stop,
                })

        if held >= max_hold:
            cid = f"chili_ml_t_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=qty,
                client_order_id=cid,
                reason="max_hold",
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"held_seconds": held, "max_hold_seconds": max_hold},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="max_hold"):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason="max_hold", quantity=qty)
            if not poll.get("filled"):
                if poll.get("partial"):
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason="max_hold",
                    )
                db.flush()
                return {
                    "ok": bool(poll.get("pending") or poll.get("partial")),
                    "session_id": sess.id,
                    "state": sess.state,
                    "pending_exit": bool(poll.get("pending")),
                    "partial_exit": bool(poll.get("partial")),
                    "exit_failed": bool(poll.get("failed")),
                }
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            _complete_confirmed_live_exit(
                db,
                sess,
                le=le,
                quantity=qty,
                entry_price=avg,
                fill_price=float(poll["fill_price"]),
                reason="max_hold",
                slip_bps=slip_live,
            )
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # Ross runner trail: in TRAILING, ratchet the stop UP to a chandelier off
        # the high-water mark (the same ATR distance the initial stop used), floored
        # at breakeven once the first-target partial de-risked the runner. The stop
        # check below then enforces it SAME tick. Derived from the frozen entry ATR —
        # not a static floor. (docs/DESIGN/MOMENTUM_LANE.md)
        if st == STATE_LIVE_TRAILING:
            # Ross sell-into-strength: a topping-tail / shooting-star on the runner's
            # candles is momentum exhaustion — lock the tail NOW rather than waiting for
            # the chandelier trail to be hit on the way back down. Runner-only (post
            # first-target scale-out); reuses the bars already fetched for the adaptive-
            # spread check; fail-safe (no candle data -> no exit). docs/DESIGN/MOMENTUM_LANE.md
            if bool(getattr(settings, "chili_momentum_exit_topping_tail_enabled", True)):
                try:
                    from .candles import topping_tail_from_df

                    if topping_tail_from_df(_entry_df):
                        le["last_bailout_trigger"] = "topping_tail_runner"
                        _commit_le(sess, le)
                        _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                        _emit(db, sess, "live_bailout", {
                            "reason": "topping_tail_runner_exit", "bid": bid,
                            "high_water_mark": _float_or_none(pos.get("high_water_mark")),
                        })
                        db.flush()
                        return {"ok": True, "session_id": sess.id, "state": sess.state}
                except Exception:
                    pass
            _atr_pct_trail = _float_or_none(le.get("entry_stop_atr_pct")) or 0.0
            _hwm_trail = _float_or_none(pos.get("high_water_mark")) or avg
            _be_floor = avg if pos.get("partial_taken") else stop_px
            _trailed = runner_trail_stop(
                high_water_mark=_hwm_trail,
                atr_pct=_atr_pct_trail,
                stop_atr_mult=float(params.get("stop_atr_mult") or 0.60),
                breakeven_floor=_be_floor,
                current_stop=stop_px,
                side_long=True,
            )
            if _trailed > stop_px:
                pos["stop_price"] = _trailed
                stop_px = _trailed
                le["position"] = pos
                _commit_le(sess, le)
                _emit(db, sess, "live_trail_ratchet", {
                    "new_stop": _trailed,
                    "high_water_mark": _hwm_trail,
                    "partial_taken": bool(pos.get("partial_taken")),
                })

        if bid <= stop_px:
            # A stop hit while TRAILING (or after the first-target partial) IS the
            # runner's trailing stop; before that it's the initial protective stop.
            _stop_reason = "trail_stop" if (st == STATE_LIVE_TRAILING or pos.get("partial_taken")) else "stop"
            cid = f"chili_ml_s_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=qty,
                client_order_id=cid,
                reason=_stop_reason,
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"stop_price": stop_px, "high_water_mark": _float_or_none(pos.get("high_water_mark"))},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason=_stop_reason):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason=_stop_reason, quantity=qty)
            if not poll.get("filled"):
                if poll.get("partial"):
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=_stop_reason,
                    )
                db.flush()
                return {
                    "ok": bool(poll.get("pending") or poll.get("partial")),
                    "session_id": sess.id,
                    "state": sess.state,
                    "pending_exit": bool(poll.get("pending")),
                    "partial_exit": bool(poll.get("partial")),
                    "exit_failed": bool(poll.get("failed")),
                }
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            _complete_confirmed_live_exit(
                db,
                sess,
                le=le,
                quantity=qty,
                entry_price=avg,
                fill_price=float(poll["fill_price"]),
                reason=_stop_reason,
                slip_bps=slip_live,
            )
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # First-target (2:1) reached and not yet scaled — take the Ross partial.
        # Fires from ENTERED or from TRAILING (price drifted up past trail-activate
        # before reaching the target); the partial_taken guard ensures it fires once.
        if (
            st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)
            and not pos.get("partial_taken")
            and bid >= target_px * 0.995
        ):
            _safe_transition(db, sess, STATE_LIVE_SCALING_OUT)
            _emit(db, sess, "live_partial_exit", {"bid": bid, "target_price": target_px})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_SCALING_OUT:
            # Ross asymmetric exit: sell `scale_out_fraction` of the ORIGINAL size
            # into the first (2:1) target, then move the balance stop to breakeven
            # and HOLD the runner (-> TRAILING). A position too small to leave a
            # sellable runner is flattened whole at target (the old flat exit) so we
            # never strand un-sellable dust. (docs/DESIGN/MOMENTUM_LANE.md)
            inc = prod.base_increment if prod else None
            mn = prod.base_min_size if prod else None
            orig_qty = _float_or_none(pos.get("original_quantity")) or qty
            frac = scale_out_fraction()
            scale_qty, runner_qty, can_split = scale_out_quantity(
                current_qty=qty,
                original_qty=orig_qty,
                fraction=frac,
                base_increment=inc,
                base_min_size=mn,
            )
            scaling = can_split and not pos.get("partial_taken")
            exit_qty = scale_qty if scaling else qty
            exit_reason = "scale_out_target" if scaling else "target"
            cid = f"chili_ml_{'so' if scaling else 'p'}_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=exit_qty,
                client_order_id=cid,
                reason=exit_reason,
                bid=bid,
                ask=ask,
                mid=mid,
                extra={
                    "target_price": target_px,
                    "scale_out_fraction": frac if scaling else None,
                    "runner_qty": runner_qty if scaling else 0.0,
                },
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason=exit_reason):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            if scaling:
                # Mark the pending exit as a deliberate scale-out so a later-tick
                # confirmation banks the partial + holds the runner (NOT a flatten).
                le["pending_exit_is_scale_out"] = True
                _commit_le(sess, le)
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason=exit_reason, quantity=exit_qty)
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            if poll.get("filled"):
                if scaling:
                    _scale_out_to_runner(
                        db,
                        sess,
                        le=le,
                        filled_quantity=exit_qty,
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=exit_reason,
                    )
                else:
                    _complete_confirmed_live_exit(
                        db,
                        sess,
                        le=le,
                        quantity=qty,
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason="target",
                        slip_bps=slip_live,
                    )
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            if poll.get("partial"):
                if scaling:
                    # Any portion of the scale order filling establishes the runner
                    # + breakeven; never over-sell. Remaining intent is abandoned.
                    _scale_out_to_runner(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=exit_reason,
                    )
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state}
                _apply_confirmed_live_partial_exit(
                    db,
                    sess,
                    le=le,
                    filled_quantity=float(poll["filled_size"]),
                    entry_price=avg,
                    fill_price=float(poll["fill_price"]),
                    reason="target",
                )
            db.flush()
            return {
                "ok": bool(poll.get("pending") or poll.get("partial")),
                "session_id": sess.id,
                "state": sess.state,
                "pending_exit": bool(poll.get("pending")),
                "partial_exit": bool(poll.get("partial")),
                "exit_failed": bool(poll.get("failed")),
            }

        if st == STATE_LIVE_ENTERED and bid >= avg * trail_activate_return:
            _safe_transition(db, sess, STATE_LIVE_TRAILING)
            _emit(db, sess, "live_trailing_armed", {"bid": bid})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # TRAILING runs the chandelier ratchet above; the shared stop check enforces
        # the trailed stop. No dedicated static-floor trail exit remains.
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_EXITED:
        cd_sec = policy_int_cap(
            caps,
            "cooldown_after_stopout_seconds",
            settings.chili_momentum_risk_cooldown_after_stopout_seconds,
        )
        until = _utcnow() + timedelta(seconds=max(0, cd_sec))
        le["cooldown_until_utc"] = until.isoformat()
        _safe_transition(db, sess, STATE_LIVE_COOLDOWN)
        _commit_le(sess, le)
        _emit(db, sess, "live_cooldown_started", {"until_utc": le["cooldown_until_utc"]})
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_COOLDOWN:
        until_raw = le.get("cooldown_until_utc")
        try:
            until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            until = _utcnow()
        if _utcnow() >= until:
            le.pop("cooldown_until_utc", None)
            le["trade_cycles"] = int(le.get("trade_cycles") or 0) + 1
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            _emit(db, sess, "live_recycled", {
                "realized_pnl_usd": le.get("realized_pnl_usd"),
                "trade_cycles": le["trade_cycles"],
            })
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    db.flush()
    return {"ok": True, "session_id": sess.id, "state": sess.state}
