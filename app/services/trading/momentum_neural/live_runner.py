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

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationSession
from ..execution_family_registry import (
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
from .paper_execution import effective_stop_atr_pct, regime_atr_pct, stop_target_prices, utc_iso
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

_log = logging.getLogger(__name__)

KEY_LIVE_EXEC = "momentum_live_execution"

AdapterFactory = Callable[[], Any]


def _utcnow() -> datetime:
    return datetime.utcnow()


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
        le["pending_exit_reason"] = reason
        le["pending_exit_quantity"] = float(quantity)
        le["pending_exit_submitted_at_utc"] = _utcnow().isoformat()
    return result


def _live_exit_submit_succeeded(
    db: Session,
    sess: TradingAutomationSession,
    *,
    le: dict[str, Any],
    result: dict[str, Any],
    reason: str,
) -> bool:
    if result.get("ok") and le.get("exit_order_id"):
        return True
    missing_order_id = bool(result.get("ok")) and not le.get("exit_order_id")
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
    le.pop("pending_exit_reason", None)
    le.pop("pending_exit_quantity", None)
    le.pop("pending_exit_submitted_at_utc", None)
    _commit_le(sess, le)
    _emit(db, sess, "live_exit_submit_failed", failed)
    return False


def _order_done_for_exit(no: NormalizedOrder) -> bool:
    st = (no.status or "").lower()
    if st in ("filled", "done", "closed"):
        return float(no.filled_size or 0.0) > 1e-12 and no.average_filled_price is not None
    if no.filled_size > 1e-12:
        return st in ("cancelled", "canceled", "expired", "failed")
    return False


def _order_terminal_without_exit_fill(no: NormalizedOrder) -> bool:
    st = (no.status or "").lower()
    if st in ("cancelled", "canceled", "expired", "failed", "rejected"):
        return float(no.filled_size or 0.0) <= 1e-12
    if st in ("filled", "done", "closed"):
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
        return {"filled": True, "fill_price": avg_px, "filled_size": filled_size, "order_status": no.status}

    terminal_status = (no.status or "").lower() in ("filled", "done", "closed", "cancelled", "canceled", "expired", "failed")
    if terminal_status and filled_size > 1e-12 and avg_px is not None:
        return {
            "filled": False,
            "partial": True,
            "fill_price": avg_px,
            "filled_size": filled_size,
            "order_status": no.status,
        }

    if _order_terminal_without_exit_fill(no):
        failed = {
            "reason": reason,
            "order_id": oid,
            "order_status": no.status,
            "filled_size": filled_size,
            "recorded_at_utc": _utcnow().isoformat(),
        }
        le["last_exit_terminal_no_fill"] = failed
        le.pop("pending_exit_reason", None)
        le.pop("pending_exit_quantity", None)
        le.pop("pending_exit_submitted_at_utc", None)
        _commit_le(sess, le)
        _emit(db, sess, "live_exit_terminal_no_fill", failed)
        return {"filled": False, "failed": True, "why": "terminal_no_fill", "order_status": no.status}

    pending = {
        "reason": reason,
        "order_id": oid,
        "order_status": no.status,
        "filled_size": filled_size,
        "expected_quantity": float(quantity),
        "recorded_at_utc": _utcnow().isoformat(),
    }
    if filled_size > 1e-12:
        pending["why"] = "partial_exit_fill_pending"
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
    le["position"] = None
    le.pop("pending_exit_reason", None)
    le.pop("pending_exit_quantity", None)
    le.pop("pending_exit_submitted_at_utc", None)
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
    le.pop("pending_exit_reason", None)
    le.pop("pending_exit_quantity", None)
    le.pop("pending_exit_submitted_at_utc", None)
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


def _adaptive_live_max_spread_bps(expected_move_bps: float | None) -> float:
    """Live spread cap, volatility-relative: ``max(base_floor, ratio x expected
    move)``. Reuses the shared, tested policy helper so the runner BBO gate and
    the pre-entry risk evaluator agree on the same adaptive tolerance. Reads the
    documented base floor + ratio knobs from settings (no inline magic)."""
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
    return adaptive_max_spread_bps(base, expected_move_bps, ratio)


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
        "last_exit_pending_confirmation": (
            le.get("last_exit_pending_confirmation")
            if isinstance(le.get("last_exit_pending_confirmation"), dict)
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
        out["notional_usd"] = pos.get("notional_usd")
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
    if not product_id.endswith("-USD"):
        product_id = f"{product_id}-USD"

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

    tick, _fr = adapter.get_best_bid_ask(product_id)
    if tick is None or tick.mid is None or tick.mid <= 0:
        _emit(db, sess, "live_blocked_by_risk", {"reason": "no_bbo"})
        if sess.state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE):
            _safe_transition(db, sess, STATE_LIVE_ERROR)
        db.flush()
        return {"ok": True, "blocked": True, "reason": "no_quote"}

    # Adaptive spread tolerance (no magic 12 bps): the BBO spread is a round-trip
    # cost, so gate it relative to how far THIS instrument actually moves (its
    # realized 15m volatility). Explosive momentum names (Ross's universe) carry
    # wider absolute spreads that are still tiny vs. their move; we only ever
    # loosen above the documented floor. The 15m candles are reused below by the
    # M4.1 momentum-continuation trigger, so fetch them once per pre-entry tick.
    # (docs/DESIGN/MOMENTUM_LANE.md)
    _entry_df = None
    _expected_move_bps: float | None = None
    _adaptive_max_spread: float | None = None
    if _live_entry_quote_gate_applies(sess, le):
        try:
            from ..market_data import fetch_ohlcv_df

            _entry_df = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
        except Exception:
            _entry_df = None
        _expected_move_bps = _expected_move_bps_from_ohlcv(_entry_df)
        _adaptive_max_spread = _adaptive_live_max_spread_bps(_expected_move_bps)
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
        if _score_ok:
            try:
                from .entry_gates import momentum_volume_confirmation, pullback_break_confirmation
                from ..market_data import fetch_ohlcv_df

                _mode = str(getattr(settings, "chili_momentum_entry_trigger_mode", "hybrid") or "hybrid").lower()
                _interval = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                _trigger_ok, _trigger_reason = False, "trigger_wait"
                if _mode in ("hybrid", "pullback_break"):
                    try:
                        _df_pb = fetch_ohlcv_df(sess.symbol, interval=_interval, period="5d")
                        if _df_pb is not None and not getattr(_df_pb, "empty", True):
                            _trigger_ok, _trigger_reason, _ = pullback_break_confirmation(
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
        if _score_ok and _trigger_ok:
            _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
            _emit(
                db, sess, "live_entry_candidate_detected",
                {"viability_score": via.viability_score, "trigger": _trigger_reason},
            )
        elif _score_ok:
            _emit(db, sess, "live_entry_trigger_wait", {"reason": _trigger_reason})
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
                le["position"] = {
                    "product_id": product_id,
                    "side": "long",
                    "quantity": filled,
                    "avg_entry_price": avg,
                    "notional_usd": filled * avg,
                    "opened_at_utc": _utcnow().isoformat(),
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
        le["entry_stop_atr_pct"] = _eff_atr_pct
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
        le["entry_notional_guard"] = {
            "max_notional_usd": max_notional,
            "ask": ask,
            "bid": bid,
            "mid": mid,
            "guarded_ask": guarded_ask,
            "estimated_guarded_notional_usd": estimated_guarded_notional,
            "quantity": qty,
            "order_type": "market",
            "spread_bps": spread_bps_live,
            "slippage_bps_ref": slip_ref,
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
                "estimated_guarded_notional_usd": estimated_guarded_notional,
                "quantity": qty,
                "base_increment": inc,
                "base_min_size": mn,
                "notional_guard_multiplier": _notional_guard_multiplier(),
            },
        )
        _commit_le(sess, le)

        cid = f"chili_ml_e_{sess.id}_{(sess.correlation_id or 'x')[:8]}_{uuid.uuid4().hex[:10]}"[:120]
        res = adapter.place_market_order(
            product_id=product_id,
            side="buy",
            base_size=_fmt_base_size(qty),
            client_order_id=cid,
        )
        le["entry_submitted"] = True
        le["entry_submit_utc"] = _utcnow().isoformat()
        le["entry_client_order_id"] = res.get("client_order_id") or cid
        le["entry_order_id"] = res.get("order_id")
        le["entry_place_result"] = {"ok": res.get("ok"), "error": res.get("error")}
        _commit_le(sess, le)
        _emit(db, sess, "live_entry_submitted", {"client_order_id": le["entry_client_order_id"], "result": res})
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
        pending_exit_reason = le.get("pending_exit_reason")
        if pending_exit_reason:
            try:
                pending_qty = float(le.get("pending_exit_quantity") or qty)
            except (TypeError, ValueError):
                pending_qty = qty
            poll = _poll_live_exit_fill(
                db,
                sess,
                adapter,
                le=le,
                reason=str(pending_exit_reason),
                quantity=min(max(pending_qty, 0.0), qty),
            )
            if poll.get("filled"):
                slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
                _complete_confirmed_live_exit(
                    db,
                    sess,
                    le=le,
                    quantity=min(max(pending_qty, 0.0), qty),
                    entry_price=avg,
                    fill_price=float(poll["fill_price"]),
                    reason=str(pending_exit_reason),
                    slip_bps=slip_live,
                )
            elif poll.get("partial"):
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
        trail_activate_return = 1.0 + float(params["trail_activate_return_bps"]) / 10_000.0
        trail_floor_return = 1.0 + float(params["trail_floor_return_bps"]) / 10_000.0

        # C1: Per-trade loss enforcement
        max_loss_usd = float(caps.get("max_loss_per_trade_usd") or 0)
        if max_loss_usd > 0 and st != STATE_LIVE_BAILOUT:
            unrealized_pnl = (bid - avg) * qty
            if unrealized_pnl <= -max_loss_usd:
                _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                _emit(db, sess, "live_bailout", {"reason": "max_loss_per_trade", "unrealized_pnl": unrealized_pnl})
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

        if bid <= stop_px:
            cid = f"chili_ml_s_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=qty,
                client_order_id=cid,
                reason="stop",
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"stop_price": stop_px},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="stop"):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason="stop", quantity=qty)
            if not poll.get("filled"):
                if poll.get("partial"):
                    _apply_confirmed_live_partial_exit(
                        db,
                        sess,
                        le=le,
                        filled_quantity=float(poll["filled_size"]),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason="stop",
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
                reason="stop",
                slip_bps=slip_live,
            )
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_ENTERED and bid >= target_px * 0.995:
            _safe_transition(db, sess, STATE_LIVE_SCALING_OUT)
            _emit(db, sess, "live_partial_exit", {"bid": bid})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_SCALING_OUT:
            cid = f"chili_ml_p_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = _submit_live_market_exit(
                db,
                sess,
                adapter,
                le=le,
                product_id=product_id,
                quantity=qty,
                client_order_id=cid,
                reason="target",
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"target_price": target_px},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="target"):
                db.flush()
                return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
            poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason="target", quantity=qty)
            if not poll.get("filled"):
                if poll.get("partial"):
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
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
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

        if st == STATE_LIVE_ENTERED and bid >= avg * trail_activate_return:
            _safe_transition(db, sess, STATE_LIVE_TRAILING)
            _emit(db, sess, "live_trailing_armed", {"bid": bid})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_TRAILING:
            trail_stop = max(stop_px, avg * trail_floor_return)
            if bid <= trail_stop:
                cid = f"chili_ml_tr_{sess.id}_{uuid.uuid4().hex[:12]}"
                sr = _submit_live_market_exit(
                    db,
                    sess,
                    adapter,
                    le=le,
                    product_id=product_id,
                    quantity=qty,
                    client_order_id=cid,
                    reason="trail_stop",
                    bid=bid,
                    ask=ask,
                    mid=mid,
                    extra={"trail_stop_price": trail_stop, "trail_floor_return": trail_floor_return},
                )
                if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="trail_stop"):
                    db.flush()
                    return {"ok": False, "session_id": sess.id, "state": sess.state, "exit_submit_failed": True}
                poll = _poll_live_exit_fill(db, sess, adapter, le=le, reason="trail_stop", quantity=qty)
                if not poll.get("filled"):
                    if poll.get("partial"):
                        _apply_confirmed_live_partial_exit(
                            db,
                            sess,
                            le=le,
                            filled_quantity=float(poll["filled_size"]),
                            entry_price=avg,
                            fill_price=float(poll["fill_price"]),
                            reason="trail_stop",
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
                    reason="trail_stop",
                    slip_bps=slip_live,
                )
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

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
