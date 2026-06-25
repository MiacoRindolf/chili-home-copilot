"""Guarded live automation runner (Phase 8) — spot adapter resolved by execution_family (Phase 11 seam).

Supported families: ``coinbase_spot``, ``robinhood_spot``; other families skip with ``execution_family_not_implemented``.

Snapshot contract:
- Never overwrite ``momentum_risk`` / admission keys.
- Mutable live execution state: ``risk_snapshot_json["momentum_live_execution"]`` only.
- Boundary checks each tick via ``evaluate_proposed_momentum_automation`` (mode=live).
"""

from __future__ import annotations

import hashlib
import json
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
    EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
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
from .risk_policy import (
    RISK_SNAPSHOT_KEY,
    compute_risk_first_quantity,
    equity_relative_notional_cap,
    liquidity_capped_notional,
    max_loss_circuit_decision,
    policy_float_cap,
    policy_int_cap,
)
from .paper_execution import (
    cushion_adaptive_trail_stop,
    breakeven_stop_after_partial,
    class_aware_reward_risk,
    effective_stop_atr_pct,
    iceberg_seller_score,
    ofi_exhaustion_lock,
    pyramid_add_decision,
    pyramid_blend_on_fill,
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
from .entry_gates import _entry_extension_veto, _entry_flow_veto, breakout_failed_to_hold

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
    fee: float = 0.0,
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
            fee=float(fee or 0.0),
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
    fee: float = 0.0,
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
            fee=float(fee or 0.0),
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
    fee: float = 0.0,
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
            fee=float(fee or 0.0),
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


def _fill_log_asset_class(sess: TradingAutomationSession) -> str:
    """crypto for the Coinbase-style spot lane, else equity (RH/Alpaca)."""
    if str(sess.symbol or "").upper().endswith("-USD"):
        return "crypto"
    fam = str(getattr(sess, "execution_family", "") or "").lower()
    return "crypto" if "coinbase" in fam else "equity"


def _fill_log_decision_packet_id(sess: TradingAutomationSession) -> int | None:
    try:
        snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
        le = snap.get(KEY_LIVE_EXEC) if isinstance(snap.get(KEY_LIVE_EXEC), dict) else {}
        dpid = le.get("entry_decision_packet_id")
        return int(dpid) if dpid else None
    except Exception:
        return None


def _record_fill_outcome_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    side: str,
    fill_source: str,
    broker_order_id: str | None,
    fill_price: float | None,
    qty: float | None,
    fees_usd: float | None,
    order_status: str | None,
    intended_price: float | None,
    spread_bps_at_decision: float | None,
    entry_price: float | None = None,
    exit_reason: str | None = None,
    realized_pnl_usd: float | None = None,
    pnl_gross_usd: float | None = None,
    fill_ts: datetime | None = None,
    entry_l2_snapshot: dict | None = None,
    raw: dict | None = None,
) -> None:
    """FILL_OUTCOME_LOG (mig308) — record ONE row per real broker fill leg.

    KILL-SWITCH + LIVE-ONLY: returns IMMEDIATELY (before ANY DB work or broker read)
    when the flag is off or the session is not live — byte-identical, no new SQL.
    FAIL-OPEN: the whole body is guarded; the INSERT runs inside a SAVEPOINT
    (``begin_nested``) so a write error rolls back ONLY the insert and never poisons
    the shared trade transaction. IDEMPOTENT: ``leg_seq`` is the next int per
    (session_id, side) and the INSERT is ``ON CONFLICT (session_id, side, leg_seq)
    DO NOTHING`` — a retried/repegged poll cannot double-insert. Stage-1 write-only.
    """
    # Kill-switch + live-mode gate FIRST — no DB work, no broker read when off/paper.
    if not getattr(settings, "chili_momentum_fill_log_enabled", False):
        return
    if str(getattr(sess, "mode", "") or "").lower() != "live":
        return
    try:
        from sqlalchemy import text as _text

        _raw_json = None
        if raw is not None:
            try:
                _raw_json = json.loads(json.dumps(raw, default=str)[:8000])
            except Exception:
                _raw_json = None
        params = {
            "session_id": int(sess.id),
            "user_id": sess.user_id,
            "symbol": sess.symbol,
            "side": str(side),
            "mode": "live",
            "asset_class": _fill_log_asset_class(sess),
            "execution_family": str(getattr(sess, "execution_family", "") or "") or None,
            "fill_source": str(fill_source),
            "broker_order_id": str(broker_order_id) if broker_order_id else None,
            "broker_fill_price": _float_or_none(fill_price),
            "qty": _float_or_none(qty),
            "fees_usd": _float_or_none(fees_usd),
            "order_status": str(order_status) if order_status else None,
            "fill_ts": fill_ts or _utcnow(),
            "realized_pnl_usd": _float_or_none(realized_pnl_usd),
            "pnl_gross_usd": _float_or_none(pnl_gross_usd),
            "intended_price": _float_or_none(intended_price),
            "entry_price": _float_or_none(entry_price),
            "spread_bps_at_decision": _float_or_none(spread_bps_at_decision),
            "exit_reason": (str(exit_reason)[:40] if exit_reason else None),
            "decision_packet_id": (
                int(le_dpid) if (le_dpid := _fill_log_decision_packet_id(sess)) is not None else None
            ),
            "entry_l2_snapshot_json": (
                json.loads(json.dumps(entry_l2_snapshot, default=str))
                if isinstance(entry_l2_snapshot, dict) else None
            ),
            "raw_json": _raw_json,
        }
        # SAVEPOINT: the insert (incl. its guarded leg_seq read) is fully isolated —
        # any error rolls back ONLY this nested block, leaving the trade txn clean.
        with db.begin_nested():
            row = db.execute(
                _text(
                    "SELECT COALESCE(MAX(leg_seq), -1) + 1 FROM momentum_fill_outcomes "
                    "WHERE session_id = :sid AND side = :side"
                ),
                {"sid": params["session_id"], "side": params["side"]},
            ).scalar()
            params["leg_seq"] = int(row or 0)
            db.execute(
                _text(
                    "INSERT INTO momentum_fill_outcomes ("
                    " session_id, leg_seq, user_id, symbol, side, mode, asset_class,"
                    " execution_family, fill_source, broker_order_id, broker_fill_price,"
                    " qty, fees_usd, order_status, fill_ts, realized_pnl_usd, pnl_gross_usd,"
                    " intended_price, entry_price, spread_bps_at_decision, exit_reason,"
                    " decision_packet_id, entry_l2_snapshot_json, raw_json"
                    ") VALUES ("
                    " :session_id, :leg_seq, :user_id, :symbol, :side, :mode, :asset_class,"
                    " :execution_family, :fill_source, :broker_order_id, :broker_fill_price,"
                    " :qty, :fees_usd, :order_status, :fill_ts, :realized_pnl_usd, :pnl_gross_usd,"
                    " :intended_price, :entry_price, :spread_bps_at_decision, :exit_reason,"
                    " :decision_packet_id,"
                    " CAST(:entry_l2_snapshot_json AS JSONB), CAST(:raw_json AS JSONB)"
                    ") ON CONFLICT (session_id, side, leg_seq) DO NOTHING"
                ),
                {
                    **params,
                    "entry_l2_snapshot_json": (
                        json.dumps(params["entry_l2_snapshot_json"])
                        if params["entry_l2_snapshot_json"] is not None else None
                    ),
                    "raw_json": (
                        json.dumps(params["raw_json"]) if params["raw_json"] is not None else None
                    ),
                },
            )
    except Exception:
        _log.debug("[momentum_fill_log] write skipped session=%s side=%s", sess.id, side, exc_info=True)


def _float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _order_total_fees_usd(no: Any) -> float | None:
    """Broker-reported commission on an order, from the raw payload.

    Coinbase Advanced Trade returns ``total_fees`` on every order; Robinhood
    equities have no such field (commission ~0) so this returns None and the
    caller books 0 — same as the old behavior. This is the live half of the
    2026-06-13 fee-truth fix: fees the broker actually charged must reach the
    economic ledger and the session PnL, not be silently dropped.
    """
    try:
        raw = getattr(no, "raw", None) or {}
        val = raw.get("total_fees")
        if val is None:
            val = raw.get("totalFees")
        if val is None:
            return None
        fee = float(val)
    except (TypeError, ValueError):
        return None
    return fee if math.isfinite(fee) and fee >= 0.0 else None


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
    hard_floor_price: float | None = None,
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

    # Sell-into-strength invariant: a resting scale-out limit may be working this
    # position. Cancel it FIRST and adopt any fill it caught, then clamp the sell
    # quantity to the true remainder — the one chokepoint every exit path crosses.
    quantity = _cancel_scale_limit_and_clamp(
        db, sess, adapter, le=le, requested_qty=quantity, reason=reason
    )
    if quantity <= 0:
        _emit(db, sess, "live_exit_noop_scale_limit_consumed", {"reason": reason})
        return {"ok": False, "error": "no_remaining_quantity", "noop": True}
    # AGENTIC COVERING-SELL RELEASE (2026-06-23 strand fix): the TRACKED scale-out is
    # cancelled above, but an UNTRACKED resting sell — or a cancel not yet propagated at
    # the broker — still locks shares, so the full exit is rejected 'Not enough shares to
    # sell' (-> 8 retries -> live_error -> stranded naked). Cancel ANY working agentic sell
    # for this symbol so the whole position is sellable; re-runs each attempt to clear the
    # propagation race. Agentic-only; spot/crypto byte-identical. Kill-switch default-ON.
    if (
        bool(getattr(settings, "chili_momentum_exit_cancel_covering_sells", True))
        and normalize_execution_family(sess.execution_family) == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
    ):
        _ncov = _cancel_agentic_covering_sells(adapter, sess.symbol)
        if _ncov:
            _emit(db, sess, "live_exit_cancelled_covering_sells", {"count": _ncov, "reason": reason})
    # BROKER-QTY CLAMP (2026-06-12 quant pass v2 A6): sell what the BROKER
    # says we hold, not what the session remembers — selling phantom shares
    # produced the "Not enough shares to sell"/"cannot be sold short" reject
    # storms (37/40 RH rejects, 8 stuck Alpaca sessions). A SUCCESSFUL fetch
    # showing less than requested clamps; a failed fetch changes nothing.
    try:
        _bq = adapter.get_position_quantity(product_id) if hasattr(adapter, "get_position_quantity") else None
        if _bq is None:
            from ...broker_service import get_open_position_quantity as _rh_qty

            if normalize_execution_family(sess.execution_family) == EXECUTION_FAMILY_ROBINHOOD_SPOT:
                _bq = _rh_qty(sess.symbol)
        if _bq is not None and float(_bq) >= 0 and float(_bq) < float(quantity) - 1e-9:
            _emit(db, sess, "live_exit_qty_clamped_to_broker", {
                "requested": float(quantity), "broker_qty": float(_bq), "reason": reason,
            })
            quantity = float(_bq)
            if quantity <= 0:
                return {"ok": False, "error": "no_remaining_quantity", "noop": True,
                        "broker_zero": True}
    except Exception:
        pass

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
    # EXIT LADDER (2026-06-12 exit study: 30/70 exits filled WORSE than the
    # planned stop, −15.7R ≈ $428/wk — naked market sells crossing wide books).
    # Attempt 1: marketable LIMIT at bid − guard (mirror of the entry's
    # ask-guard; sits AT/UNDER the bid = immediately matchable, slip capped).
    # Attempt 2: 4× guard. Attempt 3+: market (the old behavior as the floor).
    # Kill-switch/operator/EOD flatten intent = OUT NOW = market immediately.
    # An unfilled limit is re-pegged by the poll loop (repeg knob) — each
    # repeg re-enters here with attempts+1, walking the ladder.
    # HOURS-AWARE EQUITY EXIT (2026-06-16 — BEEM/AHMA stranded-long bug). In
    # premarket/after-hours Robinhood REJECTS a regular-hours order ("no order_id"),
    # so a premarket equity entry whose stop breached could NOT be flattened — the
    # sell never placed → 8 rejects → live_error → naked long with no working stop
    # (exactly the AHMA position the operator had to exit by hand). The ENTRY and
    # scale-out already pass the ext-hours overrides (which DO work premarket); only
    # this reactive exit was hours-blind. Mirror the entry idiom: when an RH equity
    # session is non-regular, pass the RH-only overrides AND force a marketable LIMIT
    # (a bare market order is rejected in extended hours even WITH the override).
    # Overrides are RH-only kwargs — pass them ONLY for robinhood_spot (the coinbase
    # adapter does not accept them; crypto + regular-hours stay byte-identical).
    _exit_extended = False
    if normalize_execution_family(sess.execution_family) in (
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    ):
        try:
            from .market_profile import market_session_now

            _exit_extended = market_session_now(sess.symbol) != "regular"
        except Exception:
            _exit_extended = False
    # ⚠️ 2026-06-23 STRANDED-EXIT FIX: use extended_hours, NOT all_day_hours. all_day_hours
    # is RH's 24-HOUR market — only valid for the few 24h-eligible names; for ~every Ross
    # low-float mover RH rejects it ("untradable for 24 hour trading"), and the AGENTIC MCP
    # adapter (this lane) has NO all_day_hours->fallback (robinhood_spot.py:909 does; the MCP
    # rail does not), so a premarket/after-hours STOP-OUT could not flatten -> naked stranded
    # long (the exact AHMA/SMCX class). extended_hours covers pre+regular+post (the lane's
    # 04:00-20:00 ET window) and is accepted for ALL equities. Mirrors the entry-side fix.
    _ext_kwargs: dict[str, Any] = (
        {"market_hours_override": "extended_hours", "extended_hours_override": True}
        if _exit_extended
        else {}
    )

    # HARD MAX-LOSS-CIRCUIT FLOOR (2026-06-17): when the caller supplies an absolute
    # loss-anchored floor (avg - K*stop_distance), OVERRIDE the entire bid-relative
    # ladder — skip the attempt<=2 bid-guard branch, the extended-hours 8×-bid branch,
    # AND the attempt-3+ naked-MARKET fallback. Place a SINGLE marketable-but-CAPPED
    # sell at exactly the floor (no repeg, partial fills final, unfilled remainder
    # bounded by the existing structural stop). Anchored to entry+structural-risk, NOT
    # a falling bid, so a deep gap-through fill is mechanically impossible. Pass the RH
    # ext-hours overrides through (_ext_kwargs) so a premarket equity floor still places.
    # hard_floor_price=None => byte-identical legacy ladder for every existing caller.
    _floor_override = None
    try:
        if hard_floor_price is not None and float(hard_floor_price) > 0:
            _floor_override = float(hard_floor_price)
    except (TypeError, ValueError):
        _floor_override = None
    _urgent = str(reason or "") in ("kill_switch_flatten", "operator_flatten")
    _lim_px = None
    if _floor_override is not None:
        _lim_px = _floor_override
    elif not _urgent and attempts <= 2:
        _g = (_notional_guard_multiplier() - 1.0) * (1.0 if attempts <= 1 else 4.0)
        _ref = None
        for _cand in (bid, mid):
            try:
                if _cand and float(_cand) > 0:
                    _ref = float(_cand)
                    break
            except (TypeError, ValueError):
                continue
        if _ref is not None:
            _lim_px = _ref * (1.0 - _g)
    # Extended-hours equity: a market order is rejected outright, so ALWAYS price a
    # marketable limit — even on an urgent flatten or the attempt-3+ market fallback.
    # Cross the bid HARD (8× guard) so it fills immediately, like the market order it
    # replaces. Regular hours / crypto: _exit_extended is False → branch unchanged.
    if _floor_override is None and _exit_extended and _lim_px is None:
        _ref = None
        for _cand in (bid, mid):
            try:
                if _cand and float(_cand) > 0:
                    _ref = float(_cand)
                    break
            except (TypeError, ValueError):
                continue
        if _ref is not None:
            _lim_px = _ref * (1.0 - (_notional_guard_multiplier() - 1.0) * 8.0)
    if _lim_px is not None and hasattr(adapter, "place_limit_order_gtc"):
        # TICK-VALID SELL PRICE (SMCX premarket stranded-position fix, 2026-06-22): an
        # RH-agentic equity limit finer than a penny on a $1+ stock is rejected by
        # place_equity_order (SEC/NMS Rule 612) -> isError -> exit retry cap exhausted
        # -> STRANDED POSITION. This reactive trail-stop priced bid*0.9975 =
        # 11.98*0.9975 = 11.95005 (sub-penny via the attempts<=2 rung) and was rejected,
        # while the ENTRY (_fmt_limit_price_buy) and the resting SCALE-OUT
        # (_fmt_limit_price_sell) both penny-round and DID fill premarket. Use the SAME
        # penny-FLOOR helper for RH equity sells (a lower sell limit is strictly MORE
        # marketable -> never starves the fill). Crypto (coinbase) keeps its fine
        # 6-decimal precision byte-identical.
        _is_rh_equity_exit = normalize_execution_family(sess.execution_family) in (
            EXECUTION_FAMILY_ROBINHOOD_SPOT,
            EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        )
        _exit_limit_str = (
            _fmt_limit_price_sell(_lim_px)
            if _is_rh_equity_exit
            else f"{_lim_px:.6f}".rstrip("0").rstrip(".")
        )
        _lim_kwargs: dict[str, Any] = dict(
            product_id=product_id,
            side="sell",
            base_size=_fmt_base_size(quantity),
            limit_price=_exit_limit_str,
            client_order_id=client_order_id,
        )
        if _ext_kwargs:
            _lim_kwargs["extended_hours"] = True
            _lim_kwargs.update(_ext_kwargs)
        result = adapter.place_limit_order_gtc(**_lim_kwargs) or {}
        le["exit_order_type"] = "limit"
        le["exit_limit_price"] = _lim_px
        if _floor_override is not None:
            le["exit_floor_order"] = True
        if _exit_extended:
            le["exit_session_extended"] = True
    else:
        _mkt_kwargs: dict[str, Any] = dict(
            product_id=product_id,
            side="sell",
            base_size=_fmt_base_size(quantity),
            client_order_id=client_order_id,
        )
        if _ext_kwargs:
            _mkt_kwargs.update(_ext_kwargs)
        result = adapter.place_market_order(**_mkt_kwargs) or {}
        le["exit_order_type"] = "market"
        le.pop("exit_limit_price", None)
    le["exit_order_id"] = result.get("order_id")
    le["exit_client_order_id"] = result.get("client_order_id") or client_order_id
    le["exit_place_result"] = {"ok": result.get("ok"), "error": result.get("error")}
    if result.get("ok"):
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


def _broker_position_confirms_zero(sess: TradingAutomationSession) -> bool:
    """Family-agnostic broker-truth flat check for the exit-retry-cap reconcile.
    Coinbase: balance/dust check (existing). Robinhood: open-position quantity
    (2026-06-11 INDP: the reconcile was Coinbase-only, so an RH phantom position
    looped 8 flatten retries into LIVE_ERROR while the broker was already flat).
    Unknown family / failed fetch -> False (fail safe, surface the error)."""
    fam = normalize_execution_family(sess.execution_family)
    if fam == EXECUTION_FAMILY_COINBASE_SPOT:
        return _broker_balance_confirms_zero(sess.symbol)
    if fam == EXECUTION_FAMILY_ROBINHOOD_SPOT:
        try:
            from ...broker_service import get_open_position_quantity

            q = get_open_position_quantity(sess.symbol)
        except Exception:
            return False
        return q is not None and float(q) <= 1e-6
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
        if _broker_position_confirms_zero(sess):
            le["position"] = None
            le["last_exit_reason"] = (reason or "exit") + "_retry_cap_broker_zero_reconcile"
            le.pop("pending_exit_reason", None)
            le.pop("pending_exit_quantity", None)
            le.pop("pending_exit_submitted_at_utc", None)
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
        # GENUINELY STRANDED POSITION (2026-06-16): the exit hit the retry cap AND
        # the broker still HOLDS the position (not zero/dust) — a real naked long with
        # no working exit (this is what stranded BEEM/AHMA premarket before the
        # hours-aware-exit fix). Emit a LOUD, distinct alert so it is never lost in the
        # cosmetic arm-twin live_errors (those are blocked AT ARM — no position, no
        # money at risk). The operator's monitoring keys on this event to take over.
        _held_qty = None
        try:
            from ...broker_service import get_open_position_quantity as _gq

            if normalize_execution_family(sess.execution_family) == EXECUTION_FAMILY_ROBINHOOD_SPOT:
                _held_qty = _gq(sess.symbol)
        except Exception:
            _held_qty = None
        _emit(
            db, sess, "live_exit_stranded_position",
            {
                "severity": "critical",
                "reason": reason,
                "symbol": sess.symbol,
                "execution_family": sess.execution_family,
                "broker_held_qty": (float(_held_qty) if _held_qty is not None else None),
                "attempts": result.get("attempts"),
                "last_error": (le.get("exit_place_result") or {}).get("error"),
                "note": (
                    "exit retry cap reached and broker STILL HOLDS the position — "
                    "naked long, no working exit; operator action required"
                ),
            },
        )
        _log.error(
            "[momentum_live] STRANDED POSITION sess=%s %s qty=%s — exit retry cap "
            "exceeded and broker still holds; needs operator flatten",
            sess.id, sess.symbol, _held_qty,
        )
        le["last_exit_submit_failed"] = {
            "reason": reason,
            "error": "exit_retry_cap_exceeded",
            "attempts": result.get("attempts"),
            "broker_held_qty": (float(_held_qty) if _held_qty is not None else None),
            "stranded": True,
            "recorded_at_utc": _utcnow().isoformat(),
        }
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
        le.pop("pending_exit_reason", None)
        le.pop("pending_exit_quantity", None)
        le.pop("pending_exit_submitted_at_utc", None)
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_EXITED)
        _emit(
            db, sess, "live_exit_reconciled_broker_zero",
            {"reason": reason, "note": "broker holds 0 — position already exited externally; not retrying"},
        )
        return True
    # BUGFIX (2026-06-16, the SLNH/LION phantom spin): the broker-qty clamp in
    # _submit_live_market_exit returns broker_zero=True ONLY when a SUCCESSFUL broker
    # read found the held qty at 0 (None / a failed fetch never sets it). That means
    # the position is a PHANTOM — the session still thinks it holds shares, but the
    # broker holds none (sold externally, a prior fill we missed, or the entry never
    # actually filled). The generic failed path below returns False, so the trail /
    # max-hold loop re-submits the SAME exit every tick forever (SLNH sess 5033 spun
    # no_remaining_quantity for HOURS; LION sess 4996 the same) — pinning the slot AND
    # showing a phantom position + phantom unrealized P&L in the cockpit (operator saw
    # "profit" on a position that did not exist). Confirm with a second INDEPENDENT
    # broker read (same belt-and-suspenders as the retry-cap reconcile @766) so a
    # one-off spurious 0 can't close a real position, then reconcile to EXITED instead
    # of spinning. Family-agnostic (Robinhood + Coinbase); never fires on a None/failed
    # read (broker_zero is unset) so an API hiccup degrades to the safe retry path.
    if result.get("broker_zero") is True and _broker_position_confirms_zero(sess):
        le["position"] = None
        le["last_exit_reason"] = (reason or "exit") + "_broker_zero_reconcile"
        le.pop("pending_exit_reason", None)
        le.pop("pending_exit_quantity", None)
        le.pop("pending_exit_submitted_at_utc", None)
        _commit_le(sess, le)
        _safe_transition(db, sess, STATE_LIVE_EXITED)
        _emit(
            db, sess, "live_exit_reconciled_broker_zero",
            {
                "reason": reason,
                "error": result.get("error"),
                "note": (
                    "broker-qty clamp + confirming read both 0 — phantom position; "
                    "reconciled to EXITED, not retrying (was spinning no_remaining_quantity)"
                ),
            },
        )
        return True
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
    # FILL-BY-SIZE (2026-06-12 SMU/RZLV phantoms): RH kept reporting the stop
    # sell as status "open" while filled_size was already FULL — the status-
    # string gate spun live_exit_pending_confirmation forever and the session
    # held a phantom position. An order that has filled its full size with a
    # known average price IS done, whatever the status string says.
    if not full_fill and avg_px is not None and filled_size + 1e-12 >= float(quantity) * 0.999:
        full_fill = True
    if full_fill:
        le.pop("exit_pending_first_seen_utc", None)
        # Fee truth (2026-06-13): stash the broker-reported commission so the
        # completion fn (which never sees the order object) books it into the
        # ledger and nets it out of session PnL. le is the existing poll →
        # complete side channel — no caller signatures change.
        le["last_exit_fee_usd"] = _order_total_fees_usd(no)
        # FILL_OUTCOME_LOG (mig308): stash the REAL polled broker exit truth so the
        # completer flags fill_source='broker_confirmed' (vs the reconstructed
        # broker-zero path). Side channel only — does not alter any behavior.
        le["last_exit_broker_truth"] = {
            "broker_order_id": str(oid) if oid else None,
            "order_status": no.status,
            "avg_px": avg_px,
            "filled_size": filled_size,
        }
        _commit_le(sess, le)
        return {"filled": True, "fill_price": avg_px, "filled_size": filled_size, "order_status": no.status}

    terminal_status = (no.status or "").lower() in ("filled", "done", "closed", "cancelled", "canceled", "expired", "failed")
    if terminal_status and filled_size > 1e-12 and avg_px is not None:
        le["last_exit_fee_usd"] = _order_total_fees_usd(no)
        le["last_exit_broker_truth"] = {
            "broker_order_id": str(oid) if oid else None,
            "order_status": no.status,
            "avg_px": avg_px,
            "filled_size": filled_size,
        }
        _commit_le(sess, le)
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
    # LIMIT REPEG (2026-06-12 exit ladder): an exit LIMIT that hasn't filled
    # within the repeg window is resting above a falling market — cancel it
    # and clear pending state so the next pulse re-submits one rung down the
    # ladder (wider guard, then market). Market orders never repeg.
    # MAX-LOSS-CIRCUIT FLOOR (2026-06-17): a floor-anchored remainder must NOT chase
    # down — the floor IS the loss cap. Skip the repeg; the unfilled remainder is left
    # resting at the absolute floor (bounded by the existing structural stop). Keyed on
    # exit_floor_anchored, set ONLY by the circuit (RH path) — legacy limits unchanged.
    if le.get("exit_order_type") == "limit" and not le.get("exit_floor_anchored"):
        _sub_at = le.get("pending_exit_submitted_at_utc")
        try:
            _sub_age = (
                (_utcnow() - datetime.fromisoformat(str(_sub_at))).total_seconds()
                if _sub_at else 0.0
            )
        except (TypeError, ValueError):
            _sub_age = 0.0
        _repeg_s = float(getattr(settings, "chili_momentum_exit_limit_repeg_seconds", 20.0) or 20.0)
        if _repeg_s > 0 and _sub_age > _repeg_s and filled_size <= 1e-12:
            try:
                adapter.cancel_order(str(oid))
            except Exception:
                pass
            le.pop("exit_order_id", None)
            le.pop("exit_order_type", None)
            le.pop("pending_exit_reason", None)
            le.pop("pending_exit_quantity", None)
            le.pop("pending_exit_submitted_at_utc", None)
            le.pop("exit_pending_first_seen_utc", None)
            _commit_le(sess, le)
            _emit(db, sess, "live_exit_limit_repegged", {
                "reason": reason, "order_id": oid, "age_s": round(_sub_age, 1),
            })
            return {"filled": False, "repegged": True, "why": "limit_repeg"}

    # STUCK-PENDING ESCAPE (2026-06-12): if the order status never goes
    # terminal but the BROKER confirms the position is gone, the exit
    # happened — finalize instead of spinning forever. Deadline-gated and
    # broker-truth-gated (a failed positions fetch never reconciles).
    first_seen = le.get("exit_pending_first_seen_utc")
    if not first_seen:
        le["exit_pending_first_seen_utc"] = _utcnow().isoformat()
        _commit_le(sess, le)
    else:
        try:
            _age = (_utcnow() - datetime.fromisoformat(str(first_seen))).total_seconds()
        except (TypeError, ValueError):
            _age = 0.0
        if _age > 90.0 and _broker_position_confirms_zero(sess):
            fill_px = avg_px or _float_or_none(getattr(no, "price", None))
            le.pop("exit_pending_first_seen_utc", None)
            _commit_le(sess, le)
            _emit(db, sess, "live_exit_reconciled_broker_zero", {
                "reason": reason, "order_id": oid, "order_status": no.status,
                "fill_price": fill_px,
                "note": "order status never went terminal; broker confirms flat",
            })
            if fill_px is not None:
                return {"filled": True, "fill_price": fill_px,
                        "filled_size": filled_size or float(quantity),
                        "order_status": no.status, "reconciled": "broker_zero"}
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
    pnl_gross = (float(fill_price) - float(entry_price)) * float(quantity)
    notional_basis = abs(float(entry_price) * float(quantity))
    # Fee truth (2026-06-13): net the broker-reported commissions out of the
    # session's realized PnL — the exit order's own fee (stashed by the poll)
    # plus any entry-side fee not yet booked (charged once, at the FULL exit,
    # so partial-exit accounting needs no fractional allocation). Sessions
    # whose exits reconcile without an order poll (broker-zero escape) book 0,
    # exactly the old behavior.
    _exit_fee = _float_or_none(le.pop("last_exit_fee_usd", None)) or 0.0
    _entry_fee = _float_or_none(le.pop("entry_fee_usd_unbooked", None)) or 0.0
    fees_usd = max(0.0, _exit_fee) + max(0.0, _entry_fee)
    pnl = pnl_gross - fees_usd
    le["fees_usd_total"] = float(le.get("fees_usd_total") or 0.0) + fees_usd
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
        fee=fees_usd,
    )
    # FILL_OUTCOME_LOG (mig308) — Hook B: full exit. fill_source is broker_confirmed
    # ONLY when the poll captured a real broker average fill price; the broker-zero
    # reconcile-finalize path (no order poll → price RECONSTRUCTED, the CAST −$253
    # case) is flagged 'reconstructed' so day-net can quarantine it.
    _bt = le.pop("last_exit_broker_truth", None)
    _bt = _bt if isinstance(_bt, dict) else None
    _record_fill_outcome_safe(
        db,
        sess,
        side="exit",
        fill_source="broker_confirmed" if _bt is not None else "reconstructed",
        broker_order_id=(_bt or {}).get("broker_order_id"),
        fill_price=float(fill_price),
        qty=float(quantity),
        fees_usd=fees_usd,
        order_status=(_bt or {}).get("order_status"),
        intended_price=_float_or_none(le.get("last_exit_intended_price")),
        spread_bps_at_decision=_float_or_none(le.get("entry_spread_bps_at_decision")),
        entry_price=float(entry_price),
        exit_reason=reason,
        realized_pnl_usd=pnl,
        pnl_gross_usd=pnl_gross,
        raw={"slip_bps": slip_bps, "fees_usd": fees_usd, "reconciled": (_bt is None)},
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
    le.pop("pending_exit_reason", None)
    le.pop("pending_exit_quantity", None)
    le.pop("pending_exit_submitted_at_utc", None)
    _commit_le(sess, le)
    _safe_transition(db, sess, STATE_LIVE_EXITED)
    payload = {"reason": reason, "pnl_usd": pnl, "fill_price": float(fill_price)}
    if fees_usd > 0.0:
        payload["pnl_gross_usd"] = pnl_gross
        payload["fees_usd"] = fees_usd
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
    # Fee truth (2026-06-13): a partial exit nets only ITS OWN order's
    # commission; the entry-side fee is booked once at the final full exit.
    _exit_fee = max(0.0, _float_or_none(le.pop("last_exit_fee_usd", None)) or 0.0)
    pnl = (float(fill_price) - float(entry_price)) * qty - _exit_fee
    notional_basis = abs(float(entry_price) * qty)
    remaining = max(0.0, current_qty - qty)
    le["fees_usd_total"] = float(le.get("fees_usd_total") or 0.0) + _exit_fee
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
        fee=_exit_fee,
    )
    # FILL_OUTCOME_LOG (mig308) — Hook C: partial / scale-out leg. The scale-out path
    # (_scale_out_to_runner) calls THIS completer before popping the flag, so a truthy
    # pending_exit_is_scale_out here means side='scale_out'; else a plain partial. Same
    # broker-truth side channel as the full exit (broker_confirmed vs reconstructed).
    _bt = le.pop("last_exit_broker_truth", None)
    _bt = _bt if isinstance(_bt, dict) else None
    _record_fill_outcome_safe(
        db,
        sess,
        side="scale_out" if le.get("pending_exit_is_scale_out") else "partial_exit",
        fill_source="broker_confirmed" if _bt is not None else "reconstructed",
        broker_order_id=(_bt or {}).get("broker_order_id"),
        fill_price=float(fill_price),
        qty=qty,
        fees_usd=_exit_fee,
        order_status=(_bt or {}).get("order_status"),
        intended_price=_float_or_none(le.get("last_exit_intended_price")),
        spread_bps_at_decision=_float_or_none(le.get("entry_spread_bps_at_decision")),
        entry_price=float(entry_price),
        exit_reason=reason,
        realized_pnl_usd=pnl,
        pnl_gross_usd=(float(fill_price) - float(entry_price)) * qty,
        raw={"fees_usd": _exit_fee, "remaining": remaining, "reconciled": (_bt is None)},
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
        pos["scale_out_fraction"] = scale_out_fraction(symbol=sess.symbol)
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


def _fmt_limit_price_sell(p: float) -> str:
    """Penny-FLOOR for sell limits >= $1 (never ask above the intended level)."""
    if p >= 1.0:
        return f"{math.floor(p * 100.0) / 100.0:.2f}"
    return f"{p:.4f}"


def _place_scale_out_limit(
    db: Session,
    sess: TradingAutomationSession,
    adapter: Any,
    *,
    le: dict[str, Any],
    product_id: str,
    target_px: float,
    filled: float,
    prod: Any,
) -> None:
    """Sell INTO strength (Ross): rest a GTC LIMIT for the scale-out fraction AT
    the first target the moment the entry fills — the partial executes while the
    pop is still paying the level, instead of a reactive market sell after the
    trigger (which pays the give-back). Fail-open: any failure here leaves the
    reactive market scale-out path fully in charge."""
    try:
        _eq_shares = not str(sess.symbol or "").upper().endswith("-USD")
        inc = prod.base_increment if prod else (1.0 if _eq_shares else None)
        mn = prod.base_min_size if prod else (1.0 if _eq_shares else None)
        scale_qty, _runner_qty, can_split = scale_out_quantity(
            current_qty=float(filled),
            original_qty=float(filled),
            fraction=scale_out_fraction(symbol=sess.symbol),
            base_increment=inc,
            base_min_size=mn,
        )
        if not can_split or scale_qty <= 0:
            return
        _ext = False
        try:
            from .market_profile import market_session_now

            _ext = market_session_now(sess.symbol) != "regular"
        except Exception:
            _ext = False
        cid = f"chili_ml_sol_{sess.id}_{uuid.uuid4().hex[:12]}"
        res = adapter.place_limit_order_gtc(
            product_id=product_id,
            side="sell",
            base_size=_fmt_base_size(scale_qty),
            limit_price=_fmt_limit_price_sell(float(target_px)),
            client_order_id=cid,
            extended_hours=_ext,
        ) or {}
        if res.get("ok") and res.get("order_id"):
            le["scale_limit_order_id"] = str(res["order_id"])
            le["scale_limit_px"] = float(target_px)
            le["scale_limit_qty"] = float(scale_qty)
            le["scale_limit_adopted_qty"] = 0.0
            _commit_le(sess, le)
            _emit(db, sess, "scale_out_limit_placed", {
                "order_id": le["scale_limit_order_id"],
                "qty": float(scale_qty), "limit_price": float(target_px),
                "extended_hours": _ext,
            })
        else:
            _emit(db, sess, "scale_out_limit_place_failed", {
                "error": str(res.get("error"))[:120], "fallback": "reactive_market_scale_out",
            })
    except Exception:
        logger.warning(
            "[live_runner] scale-out limit placement failed sess=%s (reactive path covers)",
            sess.id, exc_info=True,
        )


def _cancel_scale_limit_and_clamp(
    db: Session,
    sess: TradingAutomationSession,
    adapter: Any,
    *,
    le: dict[str, Any],
    requested_qty: float,
    reason: str,
) -> float:
    """OVERSELL INVARIANT for sell-into-strength: before ANY market exit, cancel
    the resting scale-out limit and adopt whatever it already filled (cancel-race
    safe), then clamp the requested sell quantity to the TRUE remaining position.
    Without this, the resting limit and the market exit could both execute and
    flip the account short. Called from the single exit chokepoint so every path
    (stop / trail / bailout / kill-switch / EOD / max-hold) is covered."""
    oid = le.get("scale_limit_order_id")
    if not oid:
        return float(requested_qty)
    try:
        try:
            adapter.cancel_order(str(oid))
        except Exception:
            pass
        no, _ = adapter.get_order(str(oid))
        filled = float(getattr(no, "filled_size", 0) or 0) if no is not None else 0.0
        adopted = float(le.get("scale_limit_adopted_qty") or 0.0)
        new_fill = max(0.0, filled - adopted)
        if new_fill > 0:
            pos = le.get("position") if isinstance(le.get("position"), dict) else {}
            px = float(getattr(no, "average_filled_price", 0) or 0) or float(le.get("scale_limit_px") or 0)
            # Fee truth: this adopt path never goes through the exit poll, so
            # stash the order's commission for the partial bookkeeping here.
            le["last_exit_fee_usd"] = _order_total_fees_usd(no)
            _apply_confirmed_live_partial_exit(
                db, sess, le=le, filled_quantity=new_fill,
                entry_price=float(pos.get("avg_entry_price") or 0),
                fill_price=px, reason="scale_out_limit_fill",
            )
            le["scale_limit_adopted_qty"] = adopted + new_fill
        _emit(db, sess, "scale_out_limit_cancelled", {
            "order_id": str(oid), "filled_qty": filled, "for_exit": reason,
        })
    except Exception:
        logger.warning(
            "[live_runner] scale-limit cancel-adopt failed sess=%s", sess.id, exc_info=True
        )
    finally:
        le.pop("scale_limit_order_id", None)
        _commit_le(sess, le)
    pos2 = le.get("position") if isinstance(le.get("position"), dict) else {}
    remaining = float(_float_or_none(pos2.get("quantity")) or 0.0)
    return max(0.0, min(float(requested_qty), remaining))


_OPEN_ORDER_STATES_FOR_CANCEL = frozenset(
    {"open", "confirmed", "queued", "unconfirmed", "partially_filled", "pending", "accepted", "new"}
)


def _cancel_agentic_covering_sells(adapter: Any, symbol: str) -> int:
    """Cancel ANY working SELL on the pinned agentic account for ``symbol`` so a
    full-position stop/trail/bailout isn't rejected 'Not enough shares to sell' by a
    resting partial-target that locks shares (the 2026-06-23 strand bug:
    PALI/LILA/RDGT/AIIO -> 8 rejects -> live_error). Generalizes the tracked-only
    _cancel_scale_limit_and_clamp (catches an UNTRACKED sell) and, by re-running each
    exit attempt, clears a cancel-propagation race. Mirrors crypto
    _cancel_coinbase_open_sell_orders. Best-effort; returns count cancelled."""
    n = 0
    try:
        if not (hasattr(adapter, "get_agentic_open_orders") and hasattr(adapter, "cancel_order")):
            return 0
        for o in (adapter.get_agentic_open_orders(symbol=symbol) or []):
            try:
                get = o.get if isinstance(o, dict) else (lambda k, d=None: getattr(o, k, d))
                side = str(get("side") or "").lower()
                state = str(get("state") or get("status") or "").lower()
                oid = get("id") or get("order_id")
                if side == "sell" and oid and state in _OPEN_ORDER_STATES_FOR_CANCEL:
                    adapter.cancel_order(str(oid))
                    n += 1
            except Exception:
                continue
    except Exception:
        pass
    return n


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


def _only_transient_freshness_block(ev: dict[str, Any]) -> bool:
    """True iff the boundary-risk evaluation failed EXCLUSIVELY on the transient
    ``viability_freshness`` check — a stale snapshot the equity refresh will renew —
    i.e. there is at least one failing check and EVERY failing check is the freshness
    one. Used to re-watch (retry) a freshly-armed session instead of terminally
    ERRORing it on a staleness blip. FAIL-SAFE: any unexpected shape / parse error
    returns False so the caller keeps its conservative hard-error.

    Keys on the structured ``checks`` list (``_check`` dicts: ``id`` + ``ok``), not
    free-text, so it never matches a kill-switch / drawdown / cap failure."""
    try:
        checks = ev.get("checks")
        if not isinstance(checks, list) or not checks:
            return False
        failed = [c for c in checks if isinstance(c, dict) and not c.get("ok", True)]
        if not failed:
            return False
        return all(str(c.get("id") or "") == "viability_freshness" for c in failed)
    except Exception:
        return False


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


def _fmt_limit_price_buy(p: float) -> str:
    """Format a BUY limit price as a venue-safe tick string. Prices >= $1 (the
    equity Ross band is $1-$20) round UP to the penny so the marketable buy stays
    marketable (limit at/above the ask); sub-$1 (crypto / penny names) passes finer
    precision for the venue adapter to quantize to its own increment. Rounding UP on
    a buy never makes the limit LESS marketable, so the fill is not starved. Pure +
    side-effect-free for unit testing. (docs/DESIGN/MOMENTUM_LANE.md)"""
    try:
        if not math.isfinite(p) or p <= 0:
            return "0"
        if p >= 1.0:
            ticked = math.ceil(p * 100.0 - 1e-9) / 100.0
            return f"{ticked:.2f}"
        s = f"{p:.8f}".rstrip("0").rstrip(".")
        return s if s else "0"
    except Exception:
        return f"{p}"


def _notional_guard_multiplier() -> float:
    try:
        raw_bps = getattr(settings, "chili_momentum_order_notional_guard_bps", 25.0)
        bps = 25.0 if raw_bps is None else float(raw_bps)
    except (TypeError, ValueError):
        bps = 25.0
    return 1.0 + max(0.0, bps) / 10_000.0


def _entry_chase_ceiling_px(*, limit_px: float, expected_move_bps: float | None) -> float:
    """Bid may drift this far ABOVE the buy limit before the resting marketable order
    is abandoned as 'left behind'. ONE base knob (bps), widened by a fraction of the
    name's own expected per-bar move (explosive names get proportionally more rope,
    quiet names almost none — never a fixed cent), HARD-CAPPED at the same adaptive
    max-spread the entry gate already enforces (the chase can never exceed the cost
    the risk model sized against). It only TOLERATES the existing resting limit — it
    never re-pegs the price up into a spike. base_bps=0 (default) ⇒ returns ``limit_px``
    (today's cancel-on-first-tick — parity). Pure + side-effect-free."""
    try:
        base_bps = float(getattr(settings, "chili_momentum_entry_chase_ceiling_bps", 0.0) or 0.0)
    except (TypeError, ValueError):
        base_bps = 0.0
    if base_bps <= 0 or limit_px <= 0:
        return limit_px
    try:
        ratio = float(getattr(settings, "chili_momentum_entry_chase_move_ratio", 0.25) or 0.0)
    except (TypeError, ValueError):
        ratio = 0.0
    tol_bps = max(base_bps, (expected_move_bps or 0.0) * ratio)
    tol_bps = min(tol_bps, _adaptive_live_max_spread_bps(expected_move_bps))
    return limit_px * (1.0 + tol_bps / 10_000.0)


def _entry_repeg_price(
    *, original_limit_px: float, live_ask: float, expected_move_bps: float | None
) -> float | None:
    """Bounded marketable RE-PEG price for a left-behind entry chase. Returns a new buy
    limit (a guarded live ask, so marketable) capped by a CUMULATIVE ceiling =
    ``original_limit_px x (1 + adaptive_max_spread_bps)`` — the ONE spread budget the risk
    model already accepts — so TOTAL entry drift (hence the 2:1 R:R against the FIXED
    structural stop) can never erode past that budget no matter how many re-pegs
    accumulate. Returns ``None`` when the live ask has already run PAST the ceiling (the
    move left for good -> cancel + re-watch) or inputs are invalid. Pure, side-effect-free.
    Bounds R:R erosion + thin-book sweep by construction (red-team corrections C + sweep)."""
    try:
        if original_limit_px <= 0 or live_ask <= 0:
            return None
    except (TypeError, ValueError):
        return None
    ceiling = original_limit_px * (1.0 + _adaptive_live_max_spread_bps(expected_move_bps) / 10_000.0)
    if live_ask > ceiling:
        return None  # ran past the cumulative spread budget -> do not chase
    new_px = live_ask * _adaptive_notional_guard_multiplier(expected_move_bps=expected_move_bps)
    return min(new_px, ceiling)


def _adaptive_notional_guard_multiplier(*, expected_move_bps: float | None) -> float:
    """Marketable-limit premium over the ask. Base = the documented notional-guard bps
    (25 today); on a volatile name widen toward a fraction of its expected move so the
    limit actually clears a wide offer, capped at the adaptive max-spread. With
    ``guard_move_ratio=0`` (default) ⇒ returns ``_notional_guard_multiplier()`` exactly
    (parity). Pure + side-effect-free."""
    base_mult = _notional_guard_multiplier()
    try:
        ratio = float(getattr(settings, "chili_momentum_entry_guard_move_ratio", 0.0) or 0.0)
    except (TypeError, ValueError):
        ratio = 0.0
    if expected_move_bps is None or ratio <= 0:
        return base_mult
    base_bps = max(0.0, (base_mult - 1.0) * 10_000.0)
    bps = min(max(base_bps, expected_move_bps * ratio), _adaptive_live_max_spread_bps(expected_move_bps))
    return 1.0 + bps / 10_000.0


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
    try:
        raw_cap = getattr(settings, "chili_momentum_risk_max_spread_bps_abs_cap", 300.0)
        abs_cap = 300.0 if raw_cap is None else float(raw_cap)
    except (TypeError, ValueError):
        abs_cap = 300.0
    return adaptive_max_spread_bps(base, expected_move_bps, ratio, abs_cap_bps=abs_cap)


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


def _midday_viability_bump() -> float:
    """Additive raise to entry_viability_min during the 10:30-14:30 ET midday lull
    (project_profitability_levers): the live data shows a 6% midday win-rate vs 29%
    morning, so the lane should demand a HIGHER bar to admit a NEW entry in the chop.
    Kill-switch OFF (or bump<=0) => 0.0 => byte-identical (caller never raises the
    bar, never emits). Entry-side only; never reaches an exit path."""
    if not bool(getattr(settings, "chili_momentum_midday_deweight_enabled", False)):
        return 0.0
    try:
        v = float(getattr(settings, "chili_momentum_midday_viability_bump", 0.05) or 0.0)
    except (TypeError, ValueError):
        return 0.0
    return v if v > 0 else 0.0


def _effective_entry_viability_min(
    flat_min: float, symbol: str | None, *, now: datetime | None = None
) -> tuple[float, bool, float]:
    """Effective entry-viability bar at the WATCHING_LIVE advance, applying the midday
    de-weight. Returns ``(eff_min, in_lull, bump)``.

    OFF / bump<=0 / outside the lull / crypto  => ``(flat_min, False, bump)`` so the
    caller's ``_score_ok`` is byte-identical. Equity inside the 10:30-14:30 ET lull =>
    ``(min(0.95, flat_min + bump), True, bump)`` — a SOFT raise clamped to the schema
    ceiling. Pure + unit-testable: reads only settings + the shared clock, never reads
    or mutates a position, order, or exit. (project_profitability_levers)"""
    bump = _midday_viability_bump()
    if not bump:
        return float(flat_min), False, 0.0
    from .market_profile import in_midday_lull

    if not in_midday_lull(symbol, now=now):
        return float(flat_min), False, bump
    return min(0.95, float(flat_min) + bump), True, bump


_INTERVAL_SECONDS: dict[str, float] = {
    "1m": 60.0, "2m": 120.0, "5m": 300.0, "15m": 900.0, "30m": 1800.0,
    "60m": 3600.0, "90m": 5400.0, "1h": 3600.0, "1d": 86400.0,
}


_DAY_PNL_CACHE: dict[str, float] = {"at": 0.0, "v": 0.0}


def _day_realized_usd_cached(db: Session, user_id: int) -> float:
    """Today's GLOBAL realized PnL (ET), 60s-cached — the cushion input for the
    adaptive trail. Fail-safe 0.0 (= tightest patience) on any error."""
    import time as _time

    if _time.monotonic() - _DAY_PNL_CACHE["at"] < 60.0:
        return _DAY_PNL_CACHE["v"]
    v = 0.0
    try:
        from ..governance import global_realized_pnl_today_et

        v = float(global_realized_pnl_today_et(db, user_id)["total_usd"])
    except Exception:
        v = 0.0
    _DAY_PNL_CACHE["at"] = _time.monotonic()
    _DAY_PNL_CACHE["v"] = v
    return v


def _entry_interval_seconds() -> float:
    iv = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m").lower()
    return float(_INTERVAL_SECONDS.get(iv, 300.0))


def _opening_bell_suppresses_fresh_trigger(
    symbol: str, le: dict, *, has_position: bool, now: datetime | None = None,
    armed_at: datetime | None = None,
) -> tuple[bool, dict[str, Any]]:
    """HVM101 edge (A) — opening-bell suppression of FRESH triggers.

    The first ~N minutes after the 09:30 ET regular open is opening-auction whipsaw:
    a fresh curl/dip/reversal that fires here is usually noise that reverses. SUPPRESS
    a brand-new entry in that window — but NEVER a CONTINUATION of an already-armed
    premarket runner (Ross's gap-and-go that armed/watched before the bell), and never
    an already-held position's management.

    CONTINUATION exemption (the fix): a name that was ARMED / watching BEFORE the
    regular open is a premarket gap-and-go continuation, NOT a fresh open-auction
    print — so it is EXEMPT even though it has not filled yet (``has_position`` is
    False while WATCHING_LIVE). We key the continuation off the session's arm time
    (``armed_at`` = ``sess.started_at``): if the session armed before ~the open, the
    break we're confirming is a continuation of the premarket setup, not a freshly-
    armed post-open trigger. ONLY a genuinely-fresh trigger — armed at/after the open
    AND now inside the first ~N min — is suppressed. (Previously this keyed only off
    FILLED/has_position, which wrongly suppressed un-filled premarket continuations.)

    Adaptive N: base ``chili_momentum_opening_bell_suppress_base_min`` (the ONE
    documented base ~2 min), widened by the instrument's own opening volatility via a
    day-range/ATR multiple when present in ``le`` — a calmer name clears fast, a wild
    opener stays suppressed a touch longer. No scattered magic clocks.

    EQUITY/RH ONLY (microstructure edge): crypto (``-USD``) and any
    premarket/weekend/closed clock → ``minutes_since_regular_open`` is ``None`` → FAIL
    OPEN (no suppression). Returns ``(suppress, debug)``; pure + side-effect-free.
    """
    debug: dict[str, Any] = {}
    sym = str(symbol or "").upper()
    if sym.endswith("-USD"):
        return False, debug  # crypto: 24/7, no opening bell
    if has_position:
        return False, debug  # holding already — management, never a fresh trigger
    try:
        from .market_profile import minutes_since_regular_open

        _mins = minutes_since_regular_open(sym, now=now)
    except Exception:
        _mins = None
    if _mins is None or _mins < 0:
        # crypto / weekend / closed / premarket → fail open (no suppression).
        # A premarket-armed runner that is now continuing INTO the open reads as
        # _mins>=0 here; that CONTINUATION is preserved by the armed-before-open
        # exemption below (we no longer rely on it being in a held/candidate state).
        return False, debug
    # CONTINUATION: the session armed BEFORE the regular open (premarket gap-and-go).
    # ``armed_at`` is the session's start time; reuse the SAME DST-correct open clock
    # to ask "was this armed before the bell?" — if so, the break we're confirming is
    # a continuation of the premarket setup, not a fresh open-auction print → EXEMPT.
    # A tiny tolerance (a fraction of the base window) absorbs same-instant arm/score
    # timing jitter at the open without exempting a clearly post-open fresh arm.
    if armed_at is not None:
        try:
            from .market_profile import minutes_since_regular_open as _msro

            _armed_mins = _msro(sym, now=armed_at)
        except Exception:
            _armed_mins = None
        if _armed_mins is not None and _armed_mins < 0:
            debug = {
                "exempt": "premarket_armed_continuation",
                "armed_mins_since_open": round(float(_armed_mins), 2),
            }
            return False, debug
    base_min = float(getattr(settings, "chili_momentum_opening_bell_suppress_base_min", 2.0) or 0.0)
    if base_min <= 0:
        return False, debug
    # Adaptive widen: scale the base by the opener's realized volatility relative to a
    # ~5% reference opening range (Ross small-cap). A 2x-vol opener earns up to ~2x the
    # base window; clamped to [1.0, 2.0] so it can never run away.
    vol_ref = None
    for _k in ("entry_day_range_pct", "day_range_pct", "entry_stop_atr_pct", "regime_atr_pct"):
        _v = le.get(_k) if isinstance(le, dict) else None
        try:
            if _v is not None and float(_v) > 0:
                vol_ref = float(_v)
                break
        except (TypeError, ValueError):
            continue
    widen = 1.0
    if vol_ref is not None:
        widen = max(1.0, min(2.0, vol_ref / 0.05))
    suppress_window = base_min * widen
    debug = {
        "mins_since_open": round(float(_mins), 2),
        "base_min": round(base_min, 2),
        "widen": round(widen, 2),
        "suppress_window_min": round(suppress_window, 2),
    }
    return (0.0 <= _mins < suppress_window), debug


def _bid_prop_confirms_break(
    db, symbol: str, *, window_s: float, now: datetime | None = None
) -> tuple[bool, dict[str, Any]]:
    """HVM101 edge (B) — bid-prop / book-deterioration confirmer.

    A genuine break can momentarily widen the spread as the ask lifts into the next
    level — that is NORMAL and must NOT be vetoed. We only veto when the book is
    clearly DETERIORATING UNDER the move: the best-bid is net STEPPING DOWN across the
    samples (buyers backing away, not merely one noisy tick) AND the spread has blown
    out BEYOND its own short trailing median by a margin (an air pocket opening up),
    not on a single normal widen. Both conditions must hold together to veto.

    Adaptive margin: the ONE documented base ``chili_momentum_bid_prop_spread_blowout_mult``
    (~1.5×) sets how far past the trailing median the LATEST spread must blow out before
    it counts as deterioration — a name's own recent spread is the reference, no fixed
    bps clock. Bid "stepping down" is measured net (last vs first) past a tiny relative
    epsilon so micro-noise doesn't read as a falling bid.

    FAIL-OPEN by contract: thin/absent/stale L1 tape (fewer than the min samples) →
    returns ``(True, ...)`` so it NEVER blocks a break on missing data — it only ADDS a
    veto when the tape POSITIVELY shows a falling bid into a blown-out spread.

    EQUITY/RH ONLY: callers gate to non ``-USD`` symbols (crypto L1 lives elsewhere).
    Returns ``(confirmed, debug)``; pure read, never raises.
    """
    try:
        from .nbbo_tape import recent_bid_spread_tape

        _min_n = int(getattr(settings, "chili_momentum_bid_prop_min_samples", 3) or 3)
        _max_rows = max(_min_n, int(getattr(settings, "chili_momentum_bid_prop_max_samples", 8) or 8))
        tape = recent_bid_spread_tape(db, symbol, window_s=float(window_s), max_rows=_max_rows, now_utc=now)
    except Exception:
        return True, {"reason": "bid_prop_read_error_fail_open"}
    if not tape or len(tape) < max(2, _min_n):
        # Thin/absent/stale L1 → fail open (do NOT block the break).
        return True, {"reason": "bid_prop_thin_tape_fail_open", "samples": len(tape or [])}
    bids = [b for b, _ in tape]
    spreads = [s for _, s in tape]
    last_bid = bids[-1]
    first_bid = bids[0]
    # DETERIORATION half 1 — best-bid net stepping DOWN. Measured first→last past a tiny
    # relative epsilon (0.05% of the latest bid) so a single noisy down-tick on an
    # otherwise rising bid does NOT count; only a genuine backing-away (the bid is lower
    # now than where the window started) reads as deterioration.
    eps = abs(last_bid) * 0.0005
    bid_stepping_down = last_bid < first_bid - eps
    # DETERIORATION half 2 — spread BLOWN OUT beyond its own trailing median by a margin.
    # The trailing median is the name's own recent spread; the latest spread must exceed
    # it by ``blowout_mult`` to count as an air pocket (a single normal widen sits at
    # ~1.0× and is tolerated).
    _srt = sorted(spreads)
    _m = len(_srt)
    median_spread = _srt[_m // 2] if _m % 2 else (_srt[_m // 2 - 1] + _srt[_m // 2]) / 2.0
    blowout_mult = float(getattr(settings, "chili_momentum_bid_prop_spread_blowout_mult", 1.5) or 1.5)
    if blowout_mult < 1.0:
        blowout_mult = 1.0
    spread_blown_out = spreads[-1] > (median_spread * blowout_mult) + 1e-9
    # VETO only when the book is CLEARLY deteriorating: bid backing away AND spread
    # blowing out together. A normal breakout (rising/holding bid, momentary widen) does
    # not satisfy both, so it is CONFIRMED (no veto).
    deteriorating = bool(bid_stepping_down and spread_blown_out)
    confirmed = not deteriorating
    debug = {
        "samples": len(tape),
        "bid_first": round(first_bid, 6),
        "bid_last": round(last_bid, 6),
        "bid_stepping_down": bid_stepping_down,
        "spread_last_bps": round(spreads[-1], 2),
        "spread_median_bps": round(median_spread, 2),
        "spread_blowout_mult": round(blowout_mult, 2),
        "spread_blown_out": spread_blown_out,
    }
    if not confirmed:
        debug["reason"] = "bid_prop_book_deteriorating"
    return confirmed, debug


def _build_micro_bar_df(db, symbol: str, *, bar_seconds: int, lookback_minutes: float = 30.0):
    """15s MICRO-PULLBACK (2026-06-15, "1m too slow"): build an OHLC micro-bar df
    from the densified tick tape (``momentum_nbbo_spread_tape`` rows for ``symbol``
    over the last ``lookback_minutes``) so the first-pullback trigger can run
    sub-minute. Returns a Open/High/Low/Close/Volume DataFrame (same shape as
    ``fetch_ohlcv_df``) or None.

    FAIL-SAFE / SUPERSET: insufficient tick DENSITY (only the 1-min sampler exists
    for this name) ⇒ ``_resample_micro_bars`` yields <2 micro-bars ⇒ this returns
    None ⇒ the caller falls back to the 1m bars (byte-identical). Never raises — a
    read/resample error returns None (fall back), so the micro path can only ADD an
    earlier entry where the dense tape supports it, never break the 1m path.
    """
    try:
        from datetime import timedelta as _td
        from datetime import timezone as _tz

        from sqlalchemy import text as _text

        from .micro_bars import _resample_micro_bars

        since = datetime.now(_tz.utc).replace(tzinfo=None) - _td(minutes=float(lookback_minutes))
        rows = db.execute(
            _text(
                "SELECT observed_at, bid, ask FROM momentum_nbbo_spread_tape "
                "WHERE symbol = :s AND observed_at >= :since AND bid > 0 AND ask > 0 "
                "ORDER BY observed_at ASC"
            ),
            {"s": str(symbol).upper(), "since": since},
        ).fetchall()
        if not rows or len(rows) < 2:
            return None
        df = _resample_micro_bars(
            [(r[0], float(r[1]), float(r[2])) for r in rows], bar_seconds=bar_seconds
        )
        # The trigger needs >=10 bars to evaluate (pullback_break_confirmation's own
        # ``len(df) < 10`` floor). A name with only 1-min snapshots resamples to far
        # fewer micro-bars, so this is exactly the SUPERSET/FAIL-SAFE boundary: too
        # sparse ⇒ return None ⇒ the caller uses the 1m df (byte-identical). Enforcing
        # the floor HERE keeps the density decision in one place.
        if df is None or getattr(df, "empty", True) or len(df) < 10:
            return None
        return df
    except Exception:
        return None


def _pending_entry_cancel_reason(
    *,
    bid: float | None,
    structural_stop: float,
    limit_px: float,
    elapsed_s: float | None,
    rest_bars: float,
    interval_s: float,
    chase_ceiling_px: float = 0.0,
) -> str | None:
    """EVENT-DRIVEN pending-entry lifecycle decision (operator 2026-06-11: "the
    right question is not how many seconds — it is what INVALIDATES the order").

    Returns the cancel reason, or None to keep resting:
      * ``entry_invalidated_stop_breach`` — live bid broke the structural stop:
        the setup died; a fill now would be an instant bailout. (Checked FIRST,
        unconditional — never subject to the chase ceiling.)
      * ``entry_limit_left_behind`` — live bid is ABOVE our buy limit BY MORE THAN
        the adaptive ``chase_ceiling_px``: the move left without us. With the
        default ceiling (0 ⇒ ceiling = limit_px) this is the original
        cancel-on-first-tick; a non-zero ceiling TOLERATES the resting marketable
        limit while the bid pips just past it (the fix for cancelling orders that
        are at the front of the book and about to fill — BATL/CTNT/SDOT orphans).
        It only TOLERATES the existing resting order — it never re-pegs the price up.
      * ``entry_rest_backstop`` — the order outlived the bar evidence that
        produced it (N entry-interval BARS — no free seconds).
    Pure + side-effect-free.
    """
    if bid is not None and structural_stop > 0 and bid < structural_stop:
        return "entry_invalidated_stop_breach"
    if bid is not None and limit_px > 0:
        ceiling = chase_ceiling_px if chase_ceiling_px > limit_px else limit_px
        if bid > ceiling:
            return "entry_limit_left_behind"
    if elapsed_s is not None and elapsed_s > max(0.5, rest_bars) * interval_s:
        return "entry_rest_backstop"
    return None


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


# Terminal order statuses (done — never "still working"). Anything NOT in here and
# not empty is a live/resting order. The old allow-list of OPEN statuses missed
# Robinhood's "working"/"confirmed"/"queued"/"unconfirmed"/"partially_filled" — a
# placed-but-unfilled equity order then fell through ``_order_open`` to the
# ``entry_order_state`` live_error branch and was ORPHANED on the broker (the lane's
# first real RH equity order, HIHO 2026-06-09, did exactly this). Allow-listing
# "done" states instead means any current/future broker open status is handled by
# the ack-timeout (cancel + re-watch) path rather than erroring. docs/DESIGN/MOMENTUM_LANE.md
_ORDER_TERMINAL_STATUSES = frozenset(
    {"filled", "done", "closed", "cancelled", "canceled", "expired", "failed", "rejected", "voided"}
)


def _order_open(no: NormalizedOrder) -> bool:
    """True while an order is still live on the venue (resting / unfilled), so the
    runner waits or ack-timeout-cancels it instead of erroring + orphaning it. Empty
    / "unknown" status is treated as open (indeterminate -> never abandon)."""
    st = (no.status or "").lower()
    if st in ("", "unknown"):
        return True
    return st not in _ORDER_TERMINAL_STATUSES


# ── Entry-order HISTORY: no fill is ever untracked, no stacking ───────────────
# Every placed entry order id is kept in le["entry_order_ids_all"] (the ack-timeout
# may wipe the ACTIVE pointer, never the history) with a per-id resolution map in
# le["entry_orders_resolved"] ({oid: "adopted"|"void"}). Two invariants follow:
#   1. LATE-FILL SWEEP — every pre-entry tick re-checks unresolved ids; an order
#      that filled AFTER the ack-timeout abandoned it (venue cancels are async — the
#      cancel can lose the race by SECONDS, far past #567's immediate re-fetch) is
#      re-pointed + adopted instead of becoming an unmanaged orphan.
#   2. PRE-SUBMIT GUARD — while ANY id is unresolved the runner may NOT place a new
#      entry order, making position-stacking structurally impossible.
# [BATL 2026-06-10: 5 ack-timeout cancels all lost the race -> 5 untracked fills
# stacked 4,954 sh / ~$8k with no lane stop; operator had to manage it by hand.]
_ENTRY_ORDER_HISTORY_MAX = 20  # bound the json; resolution keeps the live set ~1


def _unresolved_entry_order_ids(le: dict) -> list[str]:
    """Placed entry order ids with no terminal resolution yet, EXCLUDING the active
    pointer (the normal pending-entry handler owns that one)."""
    hist = le.get("entry_order_ids_all") or []
    resolved = le.get("entry_orders_resolved") or {}
    active = str(le.get("entry_order_id") or "")
    return [str(o) for o in hist if str(o) not in resolved and str(o) != active]


def _record_entry_order_placed(le: dict, order_id) -> None:
    if not order_id:
        return
    hist = [str(o) for o in (le.get("entry_order_ids_all") or [])]
    if str(order_id) not in hist:
        hist.append(str(order_id))
    le["entry_order_ids_all"] = hist[-_ENTRY_ORDER_HISTORY_MAX:]


def _mark_entry_order_resolved(le: dict, order_id, outcome: str) -> None:
    res = dict(le.get("entry_orders_resolved") or {})
    res[str(order_id)] = outcome
    le["entry_orders_resolved"] = res


def _sweep_unresolved_entry_orders(adapter, db, sess, le: dict) -> bool:
    """Resolve abandoned entry orders against venue truth. Returns True when a LATE
    FILL was found and the session was re-pointed at it (state -> PENDING_ENTRY so
    the existing fill-handler adopts it with the normal stop/target on the next
    pass). Cancelled-with-zero-fill ids are marked void (unblocks the submit guard);
    still-open / indeterminate ids stay unresolved (the guard keeps blocking new
    submits — fail-safe: rather not trade than buy a second clip)."""
    for oid in _unresolved_entry_order_ids(le):
        try:
            no, _ = adapter.get_order(str(oid))
        except Exception:
            continue  # indeterminate -> stays unresolved (guard keeps holding)
        if no is None:
            continue
        # OPEN-WITH-FILLS (2026-06-11 INDP): RH can leave an order in state
        # "open" with shares already filled — a cancel that silently failed
        # plus a later fill. We OWN those shares; waiting for a terminal state
        # that never comes left 612sh unmanaged at a generic -29% bracket.
        # Best-effort cancel the open remainder (single clip), then adopt.
        if _order_open(no) and float(no.filled_size or 0.0) > 0.0:
            try:
                adapter.cancel_order(str(oid))
            except Exception:
                logger.debug("[momentum_live] open-with-fills remainder cancel failed", exc_info=True)
        if _order_done_for_entry(no) or float(no.filled_size or 0.0) > 0.0:
            # LATE FILL — re-point the session at the real order and let the
            # hardened pending-entry fill-handler adopt it (position + stop/target).
            le["entry_order_id"] = str(oid)
            le["entry_submitted"] = True
            _mark_entry_order_resolved(le, oid, "adopted")
            _commit_le(sess, le)
            _emit(db, sess, "entry_late_fill_repointed", {
                "order_id": str(oid),
                "venue_status": no.status,
                "filled_size": float(no.filled_size or 0.0),
            })
            # Walk the LEGAL FSM chain to pending-entry (watching -> candidate ->
            # pending; the FSM has no watching -> pending shortcut).
            if sess.state == STATE_WATCHING_LIVE:
                _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
            if sess.state == STATE_LIVE_ENTRY_CANDIDATE:
                _safe_transition(db, sess, STATE_LIVE_PENDING_ENTRY)
            db.flush()
            return True
        if not _order_open(no):
            # terminal with zero fill (cancelled/expired/rejected clean) — safe to
            # forget; unblocks the pre-submit guard.
            _mark_entry_order_resolved(le, oid, "void")
            _commit_le(sess, le)
    return False


# ── Halt awareness (LULD circuit breakers on Ross low-floats) ─────────────────
# A halt is observable as a SUSTAINED quote freeze: `chili_momentum_halt_stale_ticks`
# consecutive stale_bbo ticks mark a suspected halt; when quotes return, entries are
# blocked for `chili_momentum_halt_resume_cooldown_seconds` (the post-resume whipsaw
# window) while watching continues. A session HOLDING a position into a halt raises a
# loud `position_halted` event (the software stop cannot execute until resume).
# [KMRK 2026-06-10: resumed through $6.81→$3.01→$5.13→$4.35→$3.33; the lane bought the
# middle of the whipsaw and the exit had to rest through the next halt.]


def _venue_broker_connected(ef: str) -> bool:
    """Cheap per-venue connectivity probe used as a tick preflight. robinhood:
    in-memory session flag (+1 local DB read at most when cold — the underlying
    is_connected may attempt one cooldown-capped re-auth, still far cheaper than a
    per-tick quote call hanging to network timeout on a dead venue). coinbase:
    TTL-cached account ping. Other adapters (alpaca: REST-per-call, no session
    concept) and any probe error return True (fail-open)."""
    try:
        if ef == "robinhood_spot":
            from ...broker_service import is_connected as _rh_connected

            return bool(_rh_connected())
        if ef == EXECUTION_FAMILY_COINBASE_SPOT:
            from ...coinbase_service import is_connected as _cb_connected

            return bool(_cb_connected())
    except Exception:
        return True
    return True


def _halt_stale_ticks_threshold() -> int:
    try:
        return max(2, int(getattr(settings, "chili_momentum_halt_stale_ticks", 3) or 3))
    except (TypeError, ValueError):
        return 3


def _halt_resume_cooldown_seconds() -> float:
    try:
        return max(0.0, float(getattr(settings, "chili_momentum_halt_resume_cooldown_seconds", 120.0) or 120.0))
    except (TypeError, ValueError):
        return 120.0


def _register_stale_quote_tick(db, sess, le: dict) -> None:
    """Count a consecutive stale-quote tick; at the threshold mark a suspected halt
    (and alert loudly if a real position is held into it)."""
    streak = int(le.get("halt_stale_streak") or 0) + 1
    le["halt_stale_streak"] = streak
    if streak == _halt_stale_ticks_threshold() and not le.get("suspected_halt_since_utc"):
        le["suspected_halt_since_utc"] = _utcnow().isoformat()
        _emit(db, sess, "suspected_halt_detected", {"stale_tick_streak": streak})
        pos = le.get("position")
        if isinstance(pos, dict) and float(pos.get("quantity") or 0) > 0:
            _log.warning(
                "[momentum_live] POSITION HALTED symbol=%s session=%s qty=%s — software stop "
                "cannot execute until the halt resumes; exit will price at the resume open.",
                sess.symbol, sess.id, pos.get("quantity"),
            )
            _emit(db, sess, "position_halted", {
                "quantity": pos.get("quantity"),
                "avg_entry_price": pos.get("avg_entry_price"),
                "stop_price": pos.get("stop_price"),
            })


def _entry_pricebook_snapshot(symbol: str) -> dict | None:
    """One-shot Nasdaq TotalView depth snapshot (RH pricebook — the same book
    Legend's bid/ask windows render) at the entry decision. Returns a compact
    dict: top-of-book sizes + 5-level depth totals + signed imbalance in
    [-1, 1] (the convention viability's Phase 4a rules score). Fail-open:
    crypto / no Gold entitlement / endpoint error -> None.

    Depth caveat (research 2026-06-11): the pricebook is Nasdaq-venue-only
    depth, not the consolidated book — partial for names routed elsewhere.
    """
    sym = (symbol or "").strip().upper()
    if not sym or sym.endswith("-USD"):
        return None
    try:
        import robin_stocks.robinhood as rh

        pb = rh.stocks.get_pricebook_by_symbol(sym)
        if not isinstance(pb, dict):
            return None
        bids = pb.get("bids") or []
        asks = pb.get("asks") or []
        if not bids and not asks:
            return None

        def _lvl(side: list, n: int = 5) -> tuple[float, float]:
            tot = 0.0
            top = 0.0
            for i, lv in enumerate(side[:n]):
                try:
                    q = float(lv.get("quantity") or 0)
                except (TypeError, ValueError):
                    continue
                tot += q
                if i == 0:
                    top = q
            return top, tot

        bid_top, bid5 = _lvl(bids)
        ask_top, ask5 = _lvl(asks)
        tot5 = bid5 + ask5
        return {
            "src": "rh_pricebook_totalview",
            "bid_top": bid_top, "ask_top": ask_top,
            "bid5": round(bid5, 0), "ask5": round(ask5, 0),
            "imbalance5": round((bid5 - ask5) / tot5, 4) if tot5 > 0 else None,
            "levels": min(len(bids), len(asks)),
        }
    except Exception:
        return None


def _register_fresh_quote_tick(db, sess, le: dict) -> None:
    """Quote is live again: clear the streak; if a suspected halt was in force, mark
    the RESUME (starts the entry cooldown) so the lane does not buy the whipsaw."""
    le["halt_stale_streak"] = 0
    if le.get("suspected_halt_since_utc"):
        le.pop("suspected_halt_since_utc", None)
        le["halt_resumed_at_utc"] = _utcnow().isoformat()
        _emit(db, sess, "halt_resumed", {
            "entry_cooldown_seconds": _halt_resume_cooldown_seconds(),
        })
        # Persist the resume marker NOW — the halt_resume_dip trigger keys its
        # entry window off it, so it must survive a process restart mid-window
        # (other le mutations ride the next commit; this one is load-bearing).
        _commit_le(sess, le)


def _halt_resume_cooldown_active(le: dict) -> bool:
    """True while we are inside the post-resume whipsaw window (entries blocked)."""
    raw = le.get("halt_resumed_at_utc")
    if not raw:
        return False
    try:
        resumed = datetime.fromisoformat(str(raw).replace("Z", "+00:00")).replace(tzinfo=None)
    except (TypeError, ValueError):
        return False
    return (_utcnow() - resumed).total_seconds() < _halt_resume_cooldown_seconds()


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


# Momentum-lane advisory-lock namespace ("ML"), distinct from auto_trader's 0x4154
# ("AT"). The decouple_watching fill-boundary lock key is (this << 32) | user_id, so
# in pg_locks the namespace lands in ``classid`` and the user in ``objid``.
_MOMENTUM_LANE_LOCK_NS = 0x4D4C


def cleanup_leaked_lane_locks(db: Session) -> int:
    """Terminate orphan sessions holding a momentum-lane advisory lock (decouple B1).

    ``pg_advisory_xact_lock`` self-releases on commit/rollback, which covers every
    NORMAL path of both dispatchers. The gap it does NOT cover: a worker force-killed
    (deploy signal / supervisor) mid-submit, before its txn boundary — that leaves the
    lane lock held by an idle-in-transaction backend, wedging EVERY subsequent entry
    for that user until the backend times out. A wedged lane is the safe-failure
    direction (blocks entries, never over-leverages), but should still be cleaned.

    Mirrors ``auto_trader._cleanup_leaked_advisory_locks``: run once per batch (NOT
    per session-tick — it commits). Cheap, idempotent, best-effort; never raises."""
    from sqlalchemy import text as _sql_text

    try:
        dialect = db.bind.dialect.name if db.bind else ""
    except Exception:
        dialect = ""
    if dialect != "postgresql":
        return 0
    try:
        threshold_s = max(60, int(getattr(settings, "chili_momentum_lane_leak_cleanup_threshold_s", 120) or 120))
    except Exception:
        threshold_s = 120
    try:
        rows = db.execute(
            _sql_text(
                "SELECT pa.pid, EXTRACT(EPOCH FROM (NOW() - pa.state_change))::int AS age_s, pa.state "
                "FROM pg_stat_activity pa "
                "JOIN pg_locks l ON l.pid = pa.pid "
                "WHERE l.locktype = 'advisory' "
                "  AND l.classid::int = :ns "
                "  AND pa.state IN ('idle in transaction', 'idle in transaction (aborted)') "
                "  AND EXTRACT(EPOCH FROM (NOW() - pa.state_change)) > :thr"
            ),
            {"ns": _MOMENTUM_LANE_LOCK_NS, "thr": threshold_s},
        ).fetchall()
        killed = 0
        for r in rows or []:
            pid, age_s, state = int(r[0]), int(r[1] or 0), r[2]
            try:
                db.execute(_sql_text("SELECT pg_terminate_backend(:p)"), {"p": pid})
                killed += 1
                _log.warning(
                    "[live_runner] lane-lock janitor: terminated leaked session pid=%s "
                    "state=%s age=%ss (orphan lock from an abandoned tick)",
                    pid, state, age_s,
                )
            except Exception as e:
                _log.debug("[live_runner] lane-lock janitor terminate pid=%s failed: %s", pid, e)
        if killed:
            db.commit()
        return killed
    except Exception as e:
        _log.debug("[live_runner] lane-lock janitor pass failed: %s", e)
        return 0


# B3 — agentic-account orphan backstop. The broker-sync reconciler runs on the MAIN
# Robinhood account and is BLIND to the isolated Agentic account, and the lane places no
# broker-side stop — so a position that filled on the agentic rail then lost its session
# (cancel-races-fill / restart) is an unmanaged orphan with no stop at RH. This sweep
# SURFACES such orphans (error-log + event) so the operator / monitor can act. It is
# INERT unless the agentic rail is the active equity rail, and rate-limited.
# RESIDUAL: detect+surface only — auto-adopt/flatten is the final hardening before
# FULLY-unattended operation (see docs/DESIGN/ROBINHOOD_AGENTIC_MCP.md §10).
_AGENTIC_SWEEP_INTERVAL = timedelta(seconds=60)
_agentic_sweep_last = [datetime.min]


def _maybe_sweep_agentic_orphans(db: Session) -> None:
    """Rate-limited, agentic-rail-only orphan detection. Fail-soft — never blocks the lane."""
    try:
        from ..execution_family_registry import EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP

        rail = str(getattr(settings, "chili_equity_execution_rail", "") or "").strip().lower()
        if rail != EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
            return  # inert until the operator opts the equity lane onto the agentic rail
        if not str(getattr(settings, "chili_robinhood_agentic_mcp_account_number", "") or "").strip():
            return
        now = datetime.utcnow()
        if now - _agentic_sweep_last[0] < _AGENTIC_SWEEP_INTERVAL:
            return
        _agentic_sweep_last[0] = now
        from ..venue.rh_agentic_orphan_sweep import sweep_agentic_orphans

        report = sweep_agentic_orphans(db)
        if report.orphan_symbols:
            _log.error(
                "[live_runner] B3 agentic orphan sweep: %d UNMANAGED position(s) %s "
                "(account_tail=%s) — no broker-side stop at RH; needs adopt/flatten",
                len(report.orphan_symbols), report.orphan_symbols, report.account_tail,
            )
    except Exception:
        _log.debug("[live_runner] agentic orphan sweep skipped (non-fatal)", exc_info=True)


def run_live_runner_batch(
    db: Session,
    *,
    limit: int = 25,
    adapter_factory: Optional[AdapterFactory] = None,
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    _maybe_sweep_agentic_orphans(db)
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

    # Venue-connectivity preflight: never carry this tick (which HOLDS the session's
    # FOR-UPDATE row lock) into broker calls against a DISCONNECTED venue — those
    # calls hang toward network timeout while the transaction sits idle (the residual
    # #565 sibling holder). Cheap in-memory/cached probes; fail-OPEN (rather tick
    # than wrongly freeze a session on a probe error). Ticks resume automatically
    # when the broker reconnects.
    if not _venue_broker_connected(ef):
        return {"ok": True, "skipped": "venue_broker_not_connected", "execution_family": ef}

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

    def _handle_kill_switch_mid_run(flatten_reason: str = "kill_switch_flatten") -> bool:
        """Safest effort: cancel open entry order; flatten if position recorded.
        Reused by the operator FLATTEN button (flatten_reason="operator_flatten")
        so manual exits flow through the same chokepoint chain."""
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
                reason=flatten_reason,
                bid=bid,
                ask=ask,
                mid=mid,
                extra={"trigger": "kill_switch"},
            )
            if not _live_exit_submit_succeeded(db, sess, le=le, result=sr, reason="kill_switch_flatten"):
                return False
            _emit(db, sess, "live_exit_submitted", {"reason": flatten_reason, "result": sr})
            poll = _poll_live_exit_fill(
                db,
                sess,
                adapter,
                le=le,
                reason=flatten_reason,
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
                        reason=flatten_reason,
                    )
                return False
            _complete_confirmed_live_exit(
                db,
                sess,
                le=le,
                quantity=float(pos["quantity"]),
                entry_price=float(pos.get("avg_entry_price") or bid or mid or 0.0),
                fill_price=float(poll["fill_price"]),
                reason=flatten_reason,
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

    # SKIP-FOR-LIMITS (operator 2026-06-23): the momentum entry is a marketable LIMIT whose
    # price bounds the fill cost, so the adaptive wide-spread gate is redundant. When enabled,
    # gate the ENTRY on only the abs-cap BROKEN-QUOTE ceiling (a halted/broken book still
    # rejects) — stale_bbo + invalid_bbo reliability checks inside _quote_quality_block ALWAYS
    # apply regardless. Spread becomes a sized COST (L2.2) + the bounded limit, not a veto.
    _skip_spread_gate = bool(getattr(settings, "chili_momentum_skip_spread_gate_for_limit_entry", True))
    _entry_spread_ceiling = (
        float(getattr(settings, "chili_momentum_risk_max_spread_bps_abs_cap", 1500.0) or 1500.0)
        if _skip_spread_gate
        else _adaptive_max_spread
    )
    quote_block = _quote_quality_block(tick, _fr, max_spread_bps=_entry_spread_ceiling)
    # Halt tracking: a SUSTAINED stale-quote streak = suspected LULD halt; quotes
    # returning = resume (starts the entry whipsaw-cooldown). A wide-but-live quote
    # is NOT a halt signal — only staleness is.
    if quote_block is not None and quote_block.get("reason") == "stale_bbo":
        _register_stale_quote_tick(db, sess, le)
    else:
        _register_fresh_quote_tick(db, sess, le)
    # SPREAD STABILITY (2026-06-11 INDP): the instantaneous BBO passed the gate
    # for ONE tick inside a flickering, hostile spread regime — we submitted,
    # the spread blew out a second later, and the eventual fill bought a dying
    # midday book. One snapshot is an opinion; the MEDIAN of the recent tape is
    # the market. Window = 1 entry bar (derived). Fails OPEN below the sample
    # floor (thin tape coverage must not block; the instantaneous gate + ack
    # lifecycle still protect).
    if not _skip_spread_gate and quote_block is None and _live_entry_quote_gate_applies(sess, le) and _adaptive_max_spread is not None:
        try:
            from .nbbo_tape import recent_spread_median_bps

            _stab_window = (
                float(getattr(settings, "chili_momentum_spread_stability_window_bars", 1.0) or 1.0)
                * _entry_interval_seconds()
            )
            _stab = recent_spread_median_bps(db, sess.symbol, window_s=_stab_window)
            _stab_min_n = int(getattr(settings, "chili_momentum_spread_stability_min_samples", 5) or 5)
            if _stab is not None and _stab[1] >= _stab_min_n and _stab[0] > float(_adaptive_max_spread):
                quote_block = {
                    "reason": "unstable_spread",
                    "median_spread_bps": round(_stab[0], 2),
                    "samples": _stab[1],
                    "window_s": round(_stab_window, 1),
                    "max_spread_bps": float(_adaptive_max_spread),
                }
        except Exception:
            _log.debug("[momentum_live] spread stability read skipped", exc_info=True)

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
            # A freshly-armed session whose ONLY boundary-risk failure is a TRANSIENT
            # `viability_freshness` staleness must NOT be terminally errored — the
            # equity refresh re-scores it within the freshness window, so re-watch and
            # retry. Viability staleness was ~100% of boundary-risk blocks at the
            # open; hard-erroring here discarded freshly-armed setups before they
            # could enter. Persistent / safety failures (kill-switch, drawdown,
            # daily-loss cap, concurrency, …) still hard-error. FAIL-SAFE: anything we
            # cannot confirm is freshness-only keeps the conservative ERROR.
            # docs/DESIGN/MOMENTUM_LANE.md
            if _only_transient_freshness_block(ev):
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
            else:
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
            # (2026-06-12: the flatten helper was called UNGATED here — the
            # aggregate at-risk cap breach liquidated ALOY (+$15 winner) and
            # ASTN simultaneously. A cap breach must block NEW risk, never
            # market-dump working positions; only the kill switch flattens.)
            if _kill_switch_blocks_live() and _handle_kill_switch_mid_run():
                _safe_transition(db, sess, STATE_LIVE_EXITED)
                db.flush()
                return {"ok": True, "blocked": True, "risk_evaluation": ev}
            # fall through to exit management (no early return)
        else:
            db.flush()
            return {"ok": True, "blocked": True, "risk_evaluation": ev}

    # ── EOD FLATTEN (2026-06-12 QH: a 3:19 PM entry was still held 2 min
    # before the FRIDAY close — momentum scalps never hold the bell, let alone
    # a weekend). Equity positions flatten through the operator-flatten
    # chokepoint when within the lead window of the 16:00 ET close. Derived
    # from the session clock; the lead is the one documented knob.
    if sess.state in _HELD_LIVE_STATES and not str(sess.symbol or "").upper().endswith("-USD"):
        try:
            from zoneinfo import ZoneInfo as _ZI

            _now_et = datetime.now(_ZI("America/New_York"))
            _lead = float(getattr(settings, "chili_momentum_eod_flatten_lead_min", 5.0) or 0.0)
            _mins_to_close = (16 * 60) - (_now_et.hour * 60 + _now_et.minute)
            if (
                _lead > 0
                and _now_et.weekday() < 5
                and 0 <= _mins_to_close <= _lead
                and not le.get("operator_flatten_requested_utc")
                and not le.get("eod_flatten_done")
            ):
                le["operator_flatten_requested_utc"] = _utcnow().isoformat()
                le["eod_flatten_done"] = True
                _commit_le(sess, le)
                _emit(db, sess, "eod_flatten_triggered", {
                    "minutes_to_close": _mins_to_close, "lead_min": _lead,
                })
        except Exception:
            _log.debug("eod flatten check failed session=%s", sess.id, exc_info=True)

    # ── Operator FLATTEN (system-mediated manual exit, 2026-06-11) ────────
    # The button sets a flag; the runner honors it HERE (quotes bound) so the
    # exit flows through the one chokepoint chain (scale-out cancel ->
    # broker-qty clamp -> place -> confirm -> reconcile) instead of a
    # broker-app sell racing the system's own resting orders (CPSH/SNDG).
    if le.get("operator_flatten_requested_utc") and sess.state in _HELD_LIVE_STATES:
        le.pop("operator_flatten_requested_utc", None)
        _commit_le(sess, le)
        _flatten_done = _handle_kill_switch_mid_run(flatten_reason="operator_flatten")
        _emit(db, sess, "operator_flatten_executed" if _flatten_done else "operator_flatten_pending", {})
        if _flatten_done:
            _safe_transition(db, sess, STATE_LIVE_EXITED)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state,
                "operator_flatten": bool(_flatten_done)}

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

    # Late-fill sweep (pre-entry states only): an entry order the ack-timeout
    # abandoned can fill SECONDS later (venue cancels are async) — re-point + adopt
    # it before doing anything else, so it becomes a managed position instead of an
    # unmanaged orphan, and so the pre-submit guard below sees venue truth.
    if st in (STATE_WATCHING_LIVE, STATE_LIVE_ENTRY_CANDIDATE, STATE_LIVE_PENDING_ENTRY) and not le.get(
        "entry_order_id"
    ):
        if _unresolved_entry_order_ids(le) and _sweep_unresolved_entry_orders(adapter, db, sess, le):
            return {"ok": True, "session_id": sess.id, "state": sess.state, "pending": "late_fill_repointed"}
        snap = dict(sess.risk_snapshot_json or {})
        le = _live_exec(snap)

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
        # MIDDAY-LULL DE-WEIGHT (project_profitability_levers): during the 10:30-14:30 ET
        # midday chop (6% live win-rate vs 29% morning; Ross sits out midday) RAISE the
        # effective viability bar so only an exceptional mover arms. SOFT (additive bump,
        # not a ban); ENTRY-side only; crypto exempt; clamped to the 0.95 schema ceiling.
        # OFF / bump<=0 => _flat_min unchanged => _score_ok byte-identical, no emit, no import.
        _flat_min = float(params["entry_viability_min"])
        _eff_min, _midday_lull, _midday_bump = _effective_entry_viability_min(_flat_min, sess.symbol)
        # MACRO RUN-R BREAKER (project_profitability_levers L2.1): when the lane's recent
        # realized-R turns negative AND worse than its own baseline (a no-follow-through
        # regime), SOFT-raise the entry bar so fewer marginal setups arm; RELATIVE so it
        # releases when the recent stretch recovers. Entry-side ONLY (never touches exits);
        # OFF / not-triggered / thin-history => _eff_min unchanged => _score_ok byte-identical.
        try:
            from .risk_policy import run_r_viability_bump
            _rr_bump, _rr_meta = run_r_viability_bump(db, sess.execution_family)
        except Exception:
            _rr_bump, _rr_meta = 0.0, None
        if _rr_bump and _rr_bump > 0:
            _new_eff = min(0.95, float(_eff_min) + float(_rr_bump))
            _vscore = float(via.viability_score or 0)
            if _vscore >= _eff_min > 0 and _vscore < _new_eff:
                # the bump actually blocked an otherwise-passing entry — the meaningful A/B event
                _emit(db, sess, "live_run_r_deweighted", {
                    **(_rr_meta or {}), "eff_min_prev": round(float(_eff_min), 3),
                    "eff_min": round(_new_eff, 3), "score": round(_vscore, 3),
                })
            _eff_min = _new_eff
        _score_ok = (
            float(via.viability_score or 0) >= _eff_min
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
            try:
                from .entry_gates import momentum_pullback_trigger, momentum_volume_confirmation
                from ..market_data import fetch_ohlcv_df

                _mode = str(getattr(settings, "chili_momentum_entry_trigger_mode", "hybrid") or "hybrid").lower()
                _interval = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
                _trigger_ok, _trigger_reason = False, "trigger_wait"
                if _mode in ("hybrid", "pullback_break"):
                    try:
                        _df_pb = fetch_ohlcv_df(sess.symbol, interval=_interval, period="5d")
                        if _df_pb is not None and not getattr(_df_pb, "empty", True):
                            # Halt-resume DIP first (Ross 06-10 DSY: "on the resumption
                            # I bought the dip"): when a suspected halt just resumed,
                            # the specialized dip trigger owns the tape for its window —
                            # it demands dip+hold+reclaim structure, stronger evidence
                            # than the generic pullback-break gives this fast a move.
                            _resumed_at = le.get("halt_resumed_at_utc")
                            if _resumed_at:
                                try:
                                    from .entry_gates import halt_resume_dip_trigger

                                    _trigger_ok, _trigger_reason, _pb_debug = halt_resume_dip_trigger(
                                        _df_pb, entry_interval=_interval,
                                        halt_resumed_at_utc=_resumed_at,
                                    )
                                except Exception:
                                    _trigger_ok = False
                            if not _trigger_ok:
                                # Shared trigger (parity): paper calls the SAME helper, so
                                # both paths take the identical Ross pullback-break entry
                                # (vol-aware, candle/VWAP/MACD, runaway). docs/DESIGN/MOMENTUM_LANE.md §8
                                # live_price = the CURRENT ask: when the completed-bar
                                # structure is valid and the live tick is already trading
                                # through the level, fire NOW (tick-break) instead of a
                                # bar-close later — Ross enters on the breaking tick.
                                _live_px = None
                                try:
                                    if tick is not None:
                                        _live_px = float(tick.ask or tick.mid or 0) or None
                                except Exception:
                                    _live_px = None
                                # 15s MICRO-PULLBACK (2026-06-15, "1m too slow"): when
                                # enabled, run the trigger on a 15s micro-bar df built
                                # from the densified tick tape so a micro-pullback break
                                # INSIDE a 1m bar fires sub-minute. The first-pullback
                                # branch in pullback_break_confirmation activates only
                                # when entry_interval == chili_momentum_first_pullback_interval
                                # (set both to '15s' to arm). FAIL-SAFE: insufficient
                                # tick density ⇒ _build_micro_bar_df returns None ⇒ fall
                                # back to the 1m df (byte-identical, no-op when off).
                                _df_trig, _iv_trig = _df_pb, _interval
                                if bool(getattr(settings, "chili_momentum_micropull_enabled", False)):
                                    _bar_s = int(getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15)
                                    _df_micro = _build_micro_bar_df(db, sess.symbol, bar_seconds=_bar_s)
                                    if _df_micro is not None and len(_df_micro) >= 10:
                                        _df_trig, _iv_trig = _df_micro, "15s"
                                _trigger_ok, _trigger_reason, _pb_debug = momentum_pullback_trigger(
                                    _df_trig, entry_interval=_iv_trig, live_price=_live_px,
                                    symbol=sess.symbol, db=db,
                                )
                            # HVM101 (C): two ADDITIVE entry triggers wired into the SAME
                            # ladder (flag-gated INSIDE each detector). Each returns the
                            # shared (ok, reason, debug) shape with pullback_low /
                            # pullback_high under the IDENTICAL keys, so the structural
                            # stop + breakout-or-bailout machinery below is reused
                            # unchanged. Only run when nothing earlier fired (the pullback
                            # break owns the tape first); each is a no-op + byte-identical
                            # when its own kill-switch flag is OFF.
                            if not _trigger_ok:
                                try:
                                    from .entry_gates import flush_dip_buy_confirmation

                                    _fd_ok, _fd_reason, _fd_debug = flush_dip_buy_confirmation(
                                        _df_trig, entry_interval=_iv_trig, live_price=_live_px,
                                        symbol=sess.symbol,
                                    )
                                    if _fd_ok:
                                        _trigger_ok, _trigger_reason, _pb_debug = _fd_ok, _fd_reason, _fd_debug
                                except Exception:
                                    pass
                            if not _trigger_ok:
                                try:
                                    from .entry_gates import TICK_ARMED_WAIT_REASONS, vwap_reclaim_confirmation

                                    _vr_ok, _vr_reason, _vr_debug = vwap_reclaim_confirmation(
                                        _df_trig, entry_interval=_iv_trig, live_price=_live_px,
                                        symbol=sess.symbol,
                                    )
                                    if _vr_ok:
                                        _trigger_ok, _trigger_reason, _pb_debug = _vr_ok, _vr_reason, _vr_debug
                                    elif (
                                        _vr_reason in TICK_ARMED_WAIT_REASONS
                                        and _trigger_reason not in TICK_ARMED_WAIT_REASONS
                                    ):
                                        # Surface the VWAP-reclaim WAIT so tick-speed dispatch
                                        # re-evaluates the instant price reclaims VWAP (the
                                        # pullback path produced only a terminal wait).
                                        _trigger_reason, _pb_debug = _vr_reason, _vr_debug
                                except Exception:
                                    pass
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
        # HVM101 (A): OPENING-BELL SUPPRESSION — hold a FRESH equity trigger in the
        # first ~N min after the 09:30 ET open (opening-auction whipsaw). Equity/RH
        # ONLY; premarket continuation of an already-armed runner is preserved (the
        # held-position short-circuit + the FSM: a premarket runner is in a held/
        # candidate state, a fresh WATCHING_LIVE trigger is by definition fresh).
        # Flag-off / crypto / outside-window ⇒ no-op, byte-identical.
        if (
            _trigger_ok
            and bool(getattr(settings, "chili_momentum_opening_bell_suppression_enabled", True))
        ):
            try:
                _ob_suppress, _ob_dbg = _opening_bell_suppresses_fresh_trigger(
                    sess.symbol, le, has_position=isinstance(le.get("position"), dict),
                    armed_at=getattr(sess, "started_at", None),
                )
            except Exception:
                _ob_suppress, _ob_dbg = False, {}
            if _ob_suppress:
                _trigger_ok = False
                _prev_reason = _trigger_reason
                _trigger_reason = "opening_bell_suppressed"
                _emit(db, sess, "live_entry_opening_bell_suppressed", {
                    "suppressed_trigger": _prev_reason, **_ob_dbg,
                })
        # HVM101 (B): BID-PROP / SPREAD-TIGHTENING CONFIRMER — confirm a fired break
        # only when, over the last few L1 samples, the best-bid is non-decreasing AND
        # the spread is at/below its short trailing median (genuine backing). Equity/RH
        # ONLY (crypto L1 lives elsewhere). FAIL-OPEN on thin/absent L1 (never blocks).
        # Flag-off ⇒ no-op, byte-identical.
        if (
            _trigger_ok
            and not str(sess.symbol or "").upper().endswith("-USD")
            and bool(getattr(settings, "chili_momentum_bid_prop_confirmer_enabled", True))
        ):
            try:
                _bp_window = (
                    float(getattr(settings, "chili_momentum_spread_stability_window_bars", 1.0) or 1.0)
                    * _entry_interval_seconds()
                )
                _bp_ok, _bp_dbg = _bid_prop_confirms_break(db, sess.symbol, window_s=_bp_window)
            except Exception:
                _bp_ok, _bp_dbg = True, {"reason": "bid_prop_error_fail_open"}
            if not _bp_ok:
                _prev_reason = _trigger_reason
                _trigger_ok = False
                _trigger_reason = "bid_prop_unconfirmed_wait"
                _emit(db, sess, "live_entry_bid_prop_unconfirmed", {
                    "blocked_trigger": _prev_reason, **_bp_dbg,
                })
        # E3: equities ENTER across the EXTENDED session (pre-market → after-hours,
        # per config) so the lane catches Ross's pre-market gap-and-go; crypto is 24/7.
        # Outside-RTH entries are flagged extended_hours at placement (below) so the
        # venue routes them (Alpaca DAY+ext, RH override) instead of rejecting.
        _mkt_open = True
        try:
            from .market_profile import is_tradeable_now

            _mkt_open = bool(is_tradeable_now(sess.symbol))
        except Exception:
            _mkt_open = True
        # Halt-resume whipsaw guard: right after a suspected halt resumes, price
        # discovery is violent — sit out the cooldown (watching continues, structure
        # rebuilds with fresh bars), then enter on a clean post-resume setup.
        # EXCEPTION: the halt_resume_dip trigger IS the sanctioned post-resume entry
        # (dip+hold+reclaim structure) — it may enter inside the cooldown.
        if (_score_ok and _trigger_ok and _mkt_open and _halt_resume_cooldown_active(le)
                and _trigger_reason != "halt_resume_dip_ok"):
            _emit(db, sess, "live_blocked_by_risk", {
                "reason": "halt_resume_cooldown",
                "halt_resumed_at_utc": le.get("halt_resumed_at_utc"),
                "cooldown_seconds": _halt_resume_cooldown_seconds(),
            })
            db.flush()
            return {"ok": True, "blocked": True, "reason": "halt_resume_cooldown"}
        if _score_ok and _trigger_ok and _mkt_open:
            # Ross structural stop: when the pullback-break trigger fired, stash the
            # pullback low so sizing + placement can stop just UNDER the structure
            # (not at a noise-tight ATR). The momentum_volume fallback has no
            # structure -> clear it so the vol-floored ATR stop is used instead.
            if _trigger_reason in (
                "pullback_break_ok", "pullback_break_tick_ok", "halt_resume_dip_ok",
                "deep_reclaim_ok", "deep_reclaim_tick_ok",
                "deep_reclaim_dipbuy_ok", "deep_reclaim_dipbuy_tick_ok",
                # HVM101 (C): the two new triggers carry pullback_low/high under the
                # SAME keys, so they reuse the IDENTICAL structural-stop + breakout-or-
                # bailout machinery.
                "flush_dip_buy", "vwap_reclaim",
            ) and _pb_debug.get("pullback_low"):
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
            # L2 depth snapshot AT THE DECISION MOMENT (Robinhood pricebook =
            # Nasdaq TotalView, the same book Legend's bid/ask windows show).
            # One GET per entry decision — NOT a stream (streaming an unofficial
            # Gold endpoint is the access pattern that risks the whole RH
            # relationship; a handful of decision-time snapshots is not).
            # Fail-open: no Gold / non-equity / any error -> no snapshot.
            _l2 = _entry_pricebook_snapshot(sess.symbol)
            if _l2 is not None:
                le["entry_l2_snapshot"] = _l2
            else:
                le.pop("entry_l2_snapshot", None)
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
            _emit(
                db, sess, "live_entry_candidate_detected",
                {"viability_score": via.viability_score, "trigger": _trigger_reason,
                 "structural_stop": le.get("structural_stop_price"),
                 "l2": _l2},
            )
        elif _score_ok and not _mkt_open:
            _emit(db, sess, "live_entry_wait_market_closed", {"symbol": sess.symbol})
        elif _score_ok:
            # Stash the level we're WAITING to break so the event loop can dispatch
            # a tick-speed re-evaluation the instant a live tick crosses it (the
            # tick-break path above then fires within seconds, like Ross).
            from .entry_gates import TICK_ARMED_WAIT_REASONS

            _wl = _pb_debug.get("pullback_high") if isinstance(_pb_debug, dict) else None
            if _trigger_reason in TICK_ARMED_WAIT_REASONS and _wl:
                if le.get("watch_break_level") != float(_wl):
                    le["watch_break_level"] = float(_wl)
                    _commit_le(sess, le)
            elif le.pop("watch_break_level", None) is not None:
                _commit_le(sess, le)
            _emit(db, sess, "live_entry_trigger_wait", {"reason": _trigger_reason})
        elif _midday_lull and via.live_eligible and float(via.viability_score or 0) >= _flat_min:
            # Forward A/B observability: this equity WOULD have advanced at the flat
            # viability bar but the midday de-weight held it back. Lets the operator
            # validate the lever LIVE (did the de-weighted names actually underperform
            # the rest-of-day?) without changing any trade. (project_profitability_levers)
            _emit(db, sess, "live_entry_midday_deweighted", {
                "viability_score": via.viability_score,
                "flat_min": _flat_min, "eff_min": _eff_min, "bump": _midday_bump,
            })
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_ENTRY_CANDIDATE:
        if float(via.viability_score or 0) < float(params["entry_revalidate_floor"]) or not via.live_eligible:
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
        else:
            _safe_transition(db, sess, STATE_LIVE_PENDING_ENTRY)
            # State-transition marker ONLY — no broker order exists yet. This used to
            # emit "live_entry_submitted" with an empty-ish payload, producing TWO
            # "submitted" events per cycle (one phantom, one real) and corrupting
            # entries-per-session / time-to-fill analytics (BATL post-mortem).
            _emit(db, sess, "live_entry_pending_place", {"note": "pending_place"})
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_PENDING_ENTRY:
        if not le.get("entry_submitted") and (
            float(via.viability_score or 0) < float(params["entry_revalidate_floor"]) or not via.live_eligible
        ):
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}
        # PRE-SUBMIT GUARD: while any previously-placed entry order is UNRESOLVED
        # (abandoned by an ack-timeout but not yet confirmed cancelled-with-zero-fill
        # by the venue), placing another order can stack a second real position on a
        # late fill of the first. Hold the submit; the sweep above resolves the ids
        # (adopt the fill / void the clean cancel) within a tick or two.
        # [BATL 2026-06-10: 5 such stacked clips -> ~$8k unmanaged.]
        if not le.get("entry_submitted"):
            _stale_oids = _unresolved_entry_order_ids(le)
            if _stale_oids:
                _emit(db, sess, "live_entry_blocked_unresolved_orders", {
                    "unresolved_order_ids": _stale_oids[:5],
                    "count": len(_stale_oids),
                })
                db.flush()
                return {
                    "ok": True, "session_id": sess.id, "state": sess.state,
                    "blocked": True, "reason": "unresolved_entry_orders",
                }
        if le.get("entry_submitted") and le.get("entry_order_id"):
            no, _ = adapter.get_order(str(le["entry_order_id"]))
            # OPEN-WITH-FILLS (2026-06-11 INDP): RH can hold an order in state
            # "open" with shares ALREADY filled (a silently-failed cancel + a
            # later fill). Those shares are OURS the moment they exist — adopt
            # on fills, never on order-state ceremony. Cancel the open
            # remainder first (single clip; extra post-adoption fills reconcile
            # via broker-sync against broker truth).
            if no and _order_open(no) and float(no.filled_size or 0.0) > 0.0:
                try:
                    adapter.cancel_order(str(le["entry_order_id"]))
                except Exception:
                    _log.debug("[momentum_live] remainder cancel failed", exc_info=True)
                _emit(db, sess, "entry_open_with_fills_adopting", {
                    "order_id": str(le["entry_order_id"]),
                    "filled_size": float(no.filled_size or 0.0),
                })
            if no and (_order_done_for_entry(no) or float(no.filled_size or 0.0) > 0.0):
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
                    "original_quantity": filled,
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
                    reward_risk=class_aware_reward_risk(sess.symbol),
                )
                le["position"]["stop_price"] = stop_px
                le["position"]["target_price"] = target_px
                le["admission_viability_score"] = float(via.viability_score or 0)
                _mark_entry_order_resolved(le, le.get("entry_order_id"), "adopted")
                _commit_le(sess, le)
                if le.get("entry_decision_packet_id"):
                    try:
                        mark_packet_executed(db, int(le["entry_decision_packet_id"]))
                    except Exception:
                        _log.debug("mark_packet_executed live skipped session=%s", sess.id, exc_info=True)
                # Fee truth (2026-06-13): book the broker-reported entry
                # commission and carry it on the session until the full exit
                # nets it out of realized PnL.
                _entry_fee = _order_total_fees_usd(no) or 0.0
                if _entry_fee > 0.0:
                    le["entry_fee_usd_unbooked"] = (
                        float(le.get("entry_fee_usd_unbooked") or 0.0) + _entry_fee
                    )
                    _commit_le(sess, le)
                _record_live_entry_ledger_safe(
                    db, sess, le=le, quantity=filled, fill_price=avg, fee=_entry_fee,
                )
                # FILL_OUTCOME_LOG (mig308) — Hook A: entry fill. broker_confirmed (we
                # polled the real order object `no`). Carries the REAL decision-time
                # spread (stashed at submit) + the entry L2 snapshot so the replay can
                # reproduce the live fill. realized_pnl is None on entry.
                _entry_intended = _float_or_none(le.get("entry_limit_price"))
                _record_fill_outcome_safe(
                    db,
                    sess,
                    side="entry",
                    fill_source="broker_confirmed",
                    broker_order_id=str(no.order_id) if getattr(no, "order_id", None) else None,
                    fill_price=float(avg),
                    qty=float(filled),
                    fees_usd=_entry_fee,
                    order_status=getattr(no, "status", None),
                    intended_price=_entry_intended,
                    spread_bps_at_decision=_float_or_none(le.get("entry_spread_bps_at_decision")),
                    entry_price=None,
                    exit_reason=None,
                    realized_pnl_usd=None,
                    entry_l2_snapshot=(
                        le.get("entry_l2_snapshot") if isinstance(le.get("entry_l2_snapshot"), dict) else None
                    ),
                    raw={"entry_fee_usd": _entry_fee, "filled_size": float(filled)},
                )
                # Sell INTO strength: rest the scale-out limit AT the target now,
                # while the move is paying the level (fail-open -> reactive path).
                _place_scale_out_limit(
                    db, sess, adapter, le=le, product_id=product_id,
                    target_px=float(target_px), filled=float(filled), prod=prod,
                )
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
                # ENTRY-FEATURE CAPTURE (2026-06-23): record the lookahead-free entry-moment
                # feature vector onto `le` for the winner/loser META-LABEL dataset (mirrors the
                # paper path; today entry features are PAPER-ONLY so live has none). POST-
                # transition + best-effort -> can NEVER affect the fill or management. Uses the
                # SHARED helper (parity with replay). Flag chili_momentum_live_capture_features.
                if bool(getattr(settings, "chili_momentum_live_capture_features", True)):
                    try:
                        from ..market_data import fetch_ohlcv_df
                        from .entry_features import capture_entry_features, macro_regime_features

                        _cap_df = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
                        if _cap_df is not None and len(_cap_df):
                            try:  # 5d frame -> slice to TODAY so session_vwap anchors correctly
                                _ld = _cap_df.index[-1].date()
                                _cap_df = _cap_df[_cap_df.index.date == _ld]
                            except Exception:
                                pass
                        _ef = capture_entry_features(
                            sess.symbol, fill_px=float(avg), stop=float(stop_px),
                            target=float(target_px), qty=float(filled),
                            want_qty=float(le.get("entry_want_qty") or filled),
                            spread_bps=float(le.get("entry_spread_bps_at_decision") or 0.0),
                            atr_pct=float(regime_atr_pct(regime) or 0.0),
                            stop_atr_pct_eff=float(atrp or 0.0),
                            mid=float(avg),
                            dollar_vol=(float(le["entry_dollar_vol"]) if le.get("entry_dollar_vol") else None),
                            liq_mult=float(le.get("entry_liq_mult") or 1.0),
                            fire_ts=_utcnow(), entry_fidelity="live",
                            trigger_debug=(le.get("entry_trigger_debug") if isinstance(le.get("entry_trigger_debug"), dict) else None),
                            session_df=_cap_df, l2_db=db, l2_as_of=None,
                            macro=macro_regime_features(),
                        )
                        le["entry_regime_snapshot_json"] = dict(regime)
                        if _ef:
                            le["entry_features"] = _ef
                            # Log the meta-label's EMITTED (p, de_rate) on the AUTHORITATIVE entry
                            # features -> flows into the outcome snapshot so the self-critic can later
                            # check CALIBRATION decay + output-de-rate shift (priority-1 sizer self-
                            # monitoring, wf_a7af66e3). Stored in the snapshot dict (sibling to
                            # features, NOT inside it -> never a leakage feature). Best-effort.
                            try:
                                from .meta_label import load_model, score_probability, size_multiplier

                                _ml = load_model("/app/data/_meta_label_model.json")
                                if _ml and _ml.get("status") == "trained":
                                    _pp = score_probability(_ef, _ml)
                                    _dr = size_multiplier(_ef, _ml, floor=float(getattr(
                                        settings, "chili_momentum_meta_label_min_size", 0.4)))
                                    le["entry_regime_snapshot_json"]["meta_label_emit"] = {
                                        "p": (round(float(_pp), 5) if _pp is not None else None),
                                        "de_rate": round(float(_dr), 4),
                                        "conf": round(float(_ml.get("confidence") or 0.0), 4),
                                    }
                            except Exception:
                                pass
                        _commit_le(sess, le)
                    except Exception:
                        _log.debug("entry-feature capture skipped session=%s", sess.id, exc_info=True)
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            if no and _order_open(no):
                # C3: EVENT-DRIVEN pending-entry lifecycle (operator 2026-06-11:
                # "the right question is not how many seconds — it is what
                # INVALIDATES the order"). Ross cancels when the setup dies, not
                # when a clock expires. Three triggers, all funneling into the
                # same race-guarded cancel sequence below:
                #   1. INVALIDATION — the live bid broke the structural stop:
                #      the trigger's premise is gone; a fill now would be an
                #      instant bailout. Event-driven (rides the 15s loop AND the
                #      LiveRunnerLoop pending-entry fast ticks).
                #   2. RUNAWAY — the live bid is ABOVE our buy limit: the move
                #      left without us and the order can only fill on the way
                #      back down (adverse selection). Re-watch decides the chase
                #      with a fresh limit.
                #   3. BACKSTOP — the order must not outlive the bar evidence
                #      that produced it: N entry-interval bars (knob in BARS,
                #      like breakout_bailout_max_bars — no free seconds). This
                #      also outwaits RH's "unconfirmed" review latency by
                #      construction (2 bars @1m = 120s >> the ~13s review that
                #      killed the CPSH/SNDG submits at the old 10s window).
                try:
                    _ptick, _pfr = adapter.get_best_bid_ask(product_id)
                    _pbid = float(_ptick.bid) if (_ptick is not None and _ptick.bid) else None
                    _pask = float(_ptick.ask) if (_ptick is not None and _ptick.ask) else None
                except Exception:
                    _pbid = None
                    _pask = None
                    _pfr = None
                _stop_px = float(le.get("structural_stop_price") or 0.0)
                try:
                    _lim_px = float(le.get("entry_limit_price") or 0.0)
                except (TypeError, ValueError):
                    _lim_px = 0.0
                submit_raw = le.get("entry_submit_utc")
                if submit_raw:
                    try:
                        t_sub = datetime.fromisoformat(str(submit_raw).replace("Z", "+00:00")).replace(tzinfo=None)
                        _emb = le.get("entry_expected_move_bps")
                        _chase_ceiling = _entry_chase_ceiling_px(
                            limit_px=_lim_px,
                            expected_move_bps=(float(_emb) if _emb else None),
                        )
                        _cancel_why = _pending_entry_cancel_reason(
                            bid=_pbid,
                            structural_stop=_stop_px,
                            limit_px=_lim_px,
                            elapsed_s=(_utcnow() - t_sub).total_seconds(),
                            rest_bars=float(getattr(
                                settings, "chili_momentum_entry_max_rest_bars", 2.0) or 2.0),
                            interval_s=_entry_interval_seconds(),
                            chase_ceiling_px=_chase_ceiling,
                        )
                        if _cancel_why is not None:
                            # RACE GUARD: the order may have FILLED between the 10s
                            # ack timeout and this (<=30s-cadence) tick — illiquid
                            # small-caps fill slowly (resting limit). Re-fetch FRESH
                            # before abandoning: a filled order abandoned here is
                            # ORPHANED — it loses the lane's tight exit management and
                            # falls to g2's far structural stop. [CTNT 2026-06-09:
                            # filled @21s, ack-timeout tick @22.9s -> orphaned -> -$283.]
                            # If it filled, leave the session pending so the entry
                            # fill-handler above ADOPTS it next tick; only cancel +
                            # re-watch a genuinely-still-open order. docs/DESIGN/MOMENTUM_LANE.md
                            _fresh, _ = adapter.get_order(str(le["entry_order_id"]))
                            # Adopt the moment ANY size is filled — a partial on a
                            # marketable limit means we are AT THE FRONT and the rest is
                            # in flight; cancelling now orphans the clip. Matches the
                            # late-fill sweep predicate (`_order_done_for_entry(no) or
                            # filled_size > 0`). [BATL/CTNT/SDOT orphan fix.]
                            if _fresh and (
                                float(getattr(_fresh, "filled_size", 0) or 0.0) > 0.0
                                or _order_done_for_entry(_fresh)
                            ):
                                db.flush()
                                return {
                                    "ok": True, "session_id": sess.id,
                                    "state": sess.state, "pending": "ack_timeout_filled_adopt",
                                }
                            adapter.cancel_order(str(le["entry_order_id"]))
                            # The CANCEL ITSELF can lose the race on a slow small-cap: the
                            # order can fill before/despite the cancel landing. Re-fetch ONCE
                            # MORE after cancelling and ADOPT a filled order — including a
                            # cancelled-but-filled (``_order_done_for_entry`` treats
                            # filled_size>0 + cancelled as done) — rather than abandoning a
                            # real position to an UNMANAGED orphan (no lane stop). Leave the
                            # session PENDING so the fill-handler above adopts it next tick.
                            # [SDOT 2026-06-10: 56sh / $1,608 filled while the ack-timeout
                            # cancel raced -> orphaned, operator had to exit it by hand.]
                            _post, _ = adapter.get_order(str(le["entry_order_id"]))
                            if _post and _order_done_for_entry(_post):
                                db.flush()
                                return {
                                    "ok": True, "session_id": sess.id, "state": sess.state,
                                    "pending": "ack_timeout_cancel_raced_fill_adopt",
                                }
                            if _post and _order_open(_post):
                                # CANCEL NOT CONFIRMED (2026-06-11 INDP): RH left the
                                # order OPEN after our cancel and it filled minutes
                                # later, unmanaged. Never walk away from a live order —
                                # keep the session PENDING with the pointer intact; the
                                # next tick re-checks (adopts fills / retries cancel).
                                _emit(db, sess, "entry_cancel_unconfirmed", {
                                    "order_id": str(le["entry_order_id"]),
                                    "reason": _cancel_why,
                                    "venue_status": _post.status,
                                    "filled_size": float(_post.filled_size or 0.0),
                                })
                                db.flush()
                                return {
                                    "ok": True, "session_id": sess.id, "state": sess.state,
                                    "pending": "cancel_unconfirmed",
                                }
                            # UNKNOWN post-cancel state (get_order failed / not-found): NEVER
                            # place a second order while the old order's fate is indeterminate
                            # (naked double-long guard). Keep PENDING with the pointer intact;
                            # the next tick re-checks venue truth. [G1 review #4]
                            if _post is None:
                                _emit(db, sess, "entry_cancel_indeterminate", {
                                    "order_id": str(le.get("entry_order_id")),
                                    "reason": _cancel_why,
                                })
                                db.flush()
                                return {
                                    "ok": True, "session_id": sess.id, "state": sess.state,
                                    "pending": "cancel_indeterminate",
                                }
                            # MARKETABLE RE-PEG (2026-06-22 G1): a left-behind RUNAWAY is the
                            # Ross play, not a miss. Reaching here, _post is a CONFIRMED
                            # terminal-cancelled order (not done, not open, not None) -> the OLD
                            # order is definitively gone -> cancel-and-replace the resting buy UP
                            # to the live ask instead of abandoning it. SAFE (red-teamed):
                            # EQUITY-ONLY (asset-class gated, crypto -USD NEVER chased),
                            # fail-CLOSED on a stale quote, price bounded by the CUMULATIVE spread
                            # budget off the original limit (R:R + thin-book sweep), RISK-FIRST
                            # re-sized (honors max_loss, not notional-only), DAY tif (never GTC),
                            # the old order marked resolved (no orphan), capped at max_repegs. Any
                            # miss -> the cancel+re-watch below.
                            _rp_n = int(le.get("entry_repeg_count", 0) or 0)
                            _rp_max = int(getattr(settings, "chili_momentum_entry_max_repegs", 3) or 0)
                            _rp_is_equity = not str(sess.symbol or "").upper().endswith("-USD")
                            if (
                                _cancel_why == "entry_limit_left_behind"
                                and bool(getattr(settings, "chili_momentum_entry_chase_enabled", True))
                                and _rp_is_equity
                                and _rp_n < _rp_max
                                and _pask and _pask > 0
                                and _pfr is not None and is_fresh_enough(_pfr)
                            ):
                                _rp_new = _entry_repeg_price(
                                    original_limit_px=float(le.get("entry_original_limit_px") or _lim_px or 0.0),
                                    live_ask=float(_pask),
                                    expected_move_bps=(float(_emb) if _emb else None),
                                )
                                _rp_maxn = float((le.get("entry_notional_guard") or {}).get("max_notional_usd") or 0.0)
                                if _rp_new and _rp_new > _lim_px and _rp_maxn > 0:
                                    # RISK-FIRST re-size at the chased price (notional is the
                                    # CEILING only) so a chase can't over-risk past max_loss.
                                    # [G1 review #2]
                                    _rb = le.get("entry_resize_basis") or {}
                                    _rp_qty, _ = compute_risk_first_quantity(
                                        entry_price=_rp_new,
                                        atr_pct=float(_rb.get("atr_pct") or 0.0),
                                        max_loss_usd=float(_rb.get("max_loss_usd") or 0.0),
                                        max_notional_ceiling_usd=_rp_maxn,
                                        base_increment=float(_rb.get("base_increment") or 1.0),
                                        base_min_size=float(_rb.get("base_min_size") or 1.0),
                                        stop_atr_mult=float(_rb.get("stop_atr_mult") or 0.60),
                                    )
                                    if _rp_qty and _rp_qty >= 1.0:
                                        _rp_old_eid = le.get("entry_order_id")
                                        _rp_pn = int(le.get("entry_place_count", 0) or 0) + 1
                                        le["entry_place_count"] = _rp_pn
                                        _rp_seed = f"{sess.id}|{sess.correlation_id or 'x'}|entry|{_rp_pn}".encode("utf-8")
                                        _rp_cid = f"chili_ml_e_{sess.id}_{(sess.correlation_id or 'x')[:8]}_{hashlib.sha1(_rp_seed).hexdigest()[:10]}"[:120]
                                        _rp_res = adapter.place_limit_order_gtc(
                                            product_id=product_id,
                                            side="buy",
                                            base_size=_fmt_base_size(_rp_qty),
                                            limit_price=_fmt_limit_price_buy(_rp_new),
                                            client_order_id=_rp_cid,
                                            time_in_force="gfd",
                                            extended_hours=bool(le.get("entry_session_extended")),
                                        ) or {}
                                        if _rp_res.get("ok"):
                                            # OLD order confirmed terminal-cancelled above -> mark
                                            # resolved so it can never resurface as an orphan.
                                            # [G1 review #3]
                                            _mark_entry_order_resolved(le, str(_rp_old_eid), "void")
                                            le["entry_order_id"] = _rp_res.get("order_id")
                                            le["entry_client_order_id"] = _rp_res.get("client_order_id") or _rp_cid
                                            le["entry_limit_price"] = _fmt_limit_price_buy(_rp_new)
                                            le["entry_repeg_count"] = _rp_n + 1
                                            le["entry_submit_utc"] = _utcnow().isoformat()
                                            le["entry_submitted"] = True
                                            _record_entry_order_placed(le, _rp_res.get("order_id"))
                                            _commit_le(sess, le)
                                            _emit(db, sess, "entry_repegged", {
                                                "old_limit": _lim_px, "new_limit": _rp_new,
                                                "live_ask": _pask, "qty": _rp_qty, "n": _rp_n + 1,
                                            })
                                            db.flush()
                                            return {"ok": True, "session_id": sess.id, "state": sess.state, "pending": "entry_repegged"}
                            _emit(db, sess, "entry_ack_timeout", {
                                "elapsed_sec": (_utcnow() - t_sub).total_seconds(),
                                "reason": _cancel_why,
                                "bid": _pbid, "limit": _lim_px or None,
                                "structural_stop": _stop_px or None,
                            })
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

        # Equity rejects FRACTIONAL shares on a LIMIT / extended-hours order (RH allows
        # fractional only on type=market + regular hours), and the momentum entry is a
        # marketable LIMIT often placed premarket. A transient get_product REST miss
        # (prod=None) must therefore default to WHOLE shares for equity (inc=mn=1.0) —
        # never a None that rounds to a fractional qty the venue rejects (a wasted live
        # entry). Crypto (-USD) keeps None (its venue quantizes fractional base size).
        _equity_share_default = not str(sess.symbol or "").upper().endswith("-USD")
        inc = prod.base_increment if prod else (1.0 if _equity_share_default else None)
        mn = prod.base_min_size if prod else (1.0 if _equity_share_default else None)
        guarded_ask = ask * _adaptive_notional_guard_multiplier(expected_move_bps=_expected_move_bps)
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
        # Liquidity-ceiling (SCALING_ENGINE.md): never size beyond what the NAME can absorb
        # on EXIT (Ross's "can't move 500k shares in 1-2 min"). As the account COMPOUNDS the
        # equity notional cap grows, but this binds on thin names so CHILI scales only as far
        # as each name's liquidity allows — instead of a 15%-of-$1M notional that can't exit a
        # thin low-float. Best-effort dollar-volume; fail-OPEN (no data / crypto -> unchanged).
        try:
            from .universe import snapshot_dollar_volumes as _snap_dvol
            _dvol = (_snap_dvol([sess.symbol]) or {}).get(str(sess.symbol or "").strip().upper())
        except Exception:
            _dvol = None
        _max_notional_pre_liq = max_notional
        max_notional = liquidity_capped_notional(max_notional, _dvol)
        if max_notional < _max_notional_pre_liq - 1e-9:
            le["liquidity_cap"] = {
                "dollar_volume_usd": round(float(_dvol), 0) if _dvol else None,
                "pre_liq_notional_usd": round(_max_notional_pre_liq, 2),
                "capped_notional_usd": round(max_notional, 2),
            }
        # Crypto liquidity ceiling (A1): the dvol-snapshot path above is equity-only
        # (fails open for -USD), so crypto had no ceiling. Bind it to the crypto
        # turnover cap (fraction of one minute's $-volume) from the same floor the
        # arm gate uses. Fail-open: no turnover datum -> unchanged.
        if str(sess.symbol or "").upper().endswith("-USD"):
            try:
                from .crypto_liquidity import crypto_liquidity_ok as _liq

                _ok, _det, _cap = _liq(sess.symbol, via, adapter=None)
                if _cap is not None and float(_cap) > 0 and float(_cap) < max_notional - 1e-9:
                    le["crypto_liquidity_cap"] = {
                        "pre_cap_notional_usd": round(max_notional, 2),
                        "capped_notional_usd": round(float(_cap), 2),
                        "per_min_vol_usd": _det.get("per_min_vol_usd"),
                    }
                    max_notional = float(_cap)
            except Exception:
                pass
        # Streak-adaptive risk (Ross): the per-trade max loss scales with the
        # lane's recent live win rate — bigger on a hot hand, half-size when
        # cold or after 3 straight losses. Bounds [0.5, 1.5]; fail-neutral 1.0.
        from .risk_policy import (
            cushion_risk_multiplier,
            prior_day_pnl_damper_multiplier,
            streak_risk_multiplier,
        )

        # Segregate the streak dial by THIS lane (ef = normalized execution_family,
        # resolved above) so a crypto/paper-twin loss never de-risks the equity lane.
        _streak_mult, _streak_meta = streak_risk_multiplier(db, execution_family=ef)
        _base_max_loss = policy_float_cap(
            caps, "max_loss_per_trade_usd", settings.chili_momentum_risk_max_loss_per_trade_usd
        )
        le["streak_risk"] = _streak_meta
        # Day-cushion ladder (Ross 06-11): start the day at half risk; earn the
        # right to full and then aggressive size from TODAY's banked P&L.
        _cushion_mult, _cushion_meta = cushion_risk_multiplier(
            db, base_loss_usd=float(_base_max_loss)
        )
        le["cushion_risk"] = _cushion_meta
        # PRIOR-DAY OUTLIER DAMPER (HVM101/SCAL101): after a statistically outlier prior
        # session (big win OR big loss, |PnL|/equity z-scored over a trailing daily window)
        # size DOWN the next session — an emotional/variance reset that reverts toward
        # baseline risk. Only ever <=1.0 (fail-NEUTRAL 1.0 on thin/degenerate/flag-off =>
        # byte-identical). Composes with the other size-down levers under the 3x clamp below.
        _prior_day_mult, _prior_day_meta = prior_day_pnl_damper_multiplier(db, execution_family=ef)
        if _prior_day_mult < 1.0:
            le["prior_day_pnl_damper"] = _prior_day_meta
        # B2 ask-heavy book size-down (2026-06-12 entry study: imbalance5 <
        # -0.4 at the decision tick = 71% of chronic-late entries vs 29%,
        # Cliff's d -0.31). The L2 snapshot is already taken at candidate
        # detection; an ask-stacked book halves the risk fraction rather than
        # skipping (counterexamples exist both ways). -0.4 is the measured
        # threshold (documented constant); the fraction is the one knob.
        _l2_mult = 1.0
        try:
            _l2s = le.get("entry_l2_snapshot") or {}
            _imb = float(_l2s.get("imbalance5"))
            if _imb < -0.4:
                _l2_mult = float(getattr(settings, "chili_momentum_entry_ask_heavy_size_fraction", 0.5) or 1.0)
                le["ask_heavy_size_down"] = {"imbalance5": round(_imb, 4), "mult": _l2_mult}
        except (TypeError, ValueError):
            _l2_mult = 1.0
        # A2 schedule risk multiplier (quant pass v2, +$3k/3d premarket leg):
        # hot (04:00–10:30 ET) ×1.5, midday ×0.5, late ×0 (entries blocked at
        # arm; this is the belt for already-armed sessions). Equities only —
        # crypto rides its own 24/7 clock. Combined multipliers are capped at
        # 3× base by the clamp below; the aggregate at-risk cap still governs.
        _sched_mult = 1.0
        if not str(sess.symbol or "").upper().endswith("-USD"):
            try:
                from .market_profile import schedule_window_now

                _win = schedule_window_now()
                _sched_mult = {"hot": 1.5, "midday": 0.5, "late": 0.0}.get(_win, 1.0)
                if _sched_mult != 1.0:
                    le["schedule_risk"] = {"window": _win, "mult": _sched_mult}
            except Exception:
                _sched_mult = 1.0
        if _sched_mult <= 0.0:
            _emit(db, sess, "live_entry_wait_late_window", {"window": "late"})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state, "skipped": "late_window"}
        # L2.2 LIQUIDITY-SCALED RISK CAP (project_profitability_levers): the biggest losers
        # are wide-spread illiquid names sized too big (QXL −$229 @119bps; the −$697 low-float
        # gap-through tail). SHRINK the risk budget as the live spread eats the name's adaptive
        # tolerance — sizes the risky names DOWN without rejecting any trade (the L3 entry filter
        # killed winners; this never does). Entry sizing only, never an exit. OFF / mult==1.0 =>
        # the product is byte-identical. Replay applies the SAME helper with the SAME inputs => parity.
        _liq_mult = 1.0
        if bool(getattr(settings, "chili_momentum_liquidity_risk_cap_enabled", True)):
            try:
                from .risk_policy import spread_liquidity_risk_multiplier
                _liq_mult, _liq_meta = spread_liquidity_risk_multiplier(
                    spread_bps_live, _expected_move_bps,
                    floor=float(getattr(settings, "chili_momentum_liquidity_risk_floor", 0.5) or 0.5),
                )
                if _liq_mult < 1.0:
                    le["liquidity_risk"] = _liq_meta
            except Exception:
                _liq_mult = 1.0
        # META-LABEL DE-RATE (2026-06-23, adaptive + regime-aware, NEVER a veto): size DOWN a
        # low-edge / loser-profile entry per the meta-label model (evidence-scaled -> INERT until
        # it earns confidence from the growing live dataset; bounded [floor,1.0], never zeroes ->
        # preserves the explosive tail). Best-effort + LIGHT (in-process L2 ring + cached macro,
        # NO df refetch -> no submit latency; front_side median-imputed). Multiplies the risk
        # budget like the other size-down levers, capped by the 3x clamp. Kill-switch
        # chili_momentum_meta_label_derate_enabled.
        _meta_mult = 1.0
        if bool(getattr(settings, "chili_momentum_meta_label_derate_enabled", True)):
            try:
                from .meta_label import load_model, size_multiplier

                _mm_model = load_model("/app/data/_meta_label_model.json")
                if _mm_model and float(_mm_model.get("confidence") or 0.0) > 0.0:
                    from .entry_features import capture_entry_features, macro_regime_features

                    _mm_stop = guarded_ask * (1.0 - float(_eff_atr_pct) * float(_stop_atr_mult))
                    _mm_rr = class_aware_reward_risk(sess.symbol)
                    _mm_tgt = (guarded_ask + _mm_rr * (guarded_ask - _mm_stop)) if guarded_ask > _mm_stop else guarded_ask
                    _mm_feats = capture_entry_features(
                        sess.symbol, fill_px=float(guarded_ask), stop=float(_mm_stop),
                        target=float(_mm_tgt), qty=1.0, want_qty=1.0,
                        spread_bps=float(spread_bps_live or 0.0), atr_pct=float(_eff_atr_pct or 0.0),
                        stop_atr_pct_eff=float(_eff_atr_pct or 0.0), mid=float(guarded_ask),
                        dollar_vol=None, liq_mult=1.0, fire_ts=_utcnow(), entry_fidelity="live",
                        trigger_debug=(le.get("entry_trigger_debug") if isinstance(le.get("entry_trigger_debug"), dict) else None),
                        session_df=None, l2_db=db, l2_as_of=None, macro=macro_regime_features())
                    _mm = size_multiplier(_mm_feats or {}, _mm_model,
                                          floor=float(getattr(settings, "chili_momentum_meta_label_min_size", 0.4)))
                    if 0.0 < _mm < 1.0:
                        _meta_mult = _mm
                        le["meta_label_derate"] = {"mult": round(_mm, 4),
                                                   "conf": round(float(_mm_model.get("confidence") or 0.0), 4)}
            except Exception:
                _meta_mult = 1.0
        _eff_max_loss = min(
            float(_base_max_loss) * float(_streak_mult) * float(_cushion_mult) * float(_l2_mult) * float(_sched_mult) * float(_liq_mult) * float(_meta_mult) * float(_prior_day_mult),
            float(_base_max_loss) * 3.0,  # hard combined-multiplier ceiling (quant pass v2)
        )
        # Freeze the risk-first sizing inputs so a marketable re-peg (G1) can RE-SIZE
        # risk-first at the chased price instead of over-sizing off notional. [G1 review #2]
        le["entry_resize_basis"] = {
            "max_loss_usd": _eff_max_loss,
            "atr_pct": float(_eff_atr_pct),
            "stop_atr_mult": float(_stop_atr_mult),
            "base_increment": float(inc) if inc else 1.0,
            "base_min_size": float(mn) if mn else 1.0,
        }
        _rf_qty, _rf_meta = compute_risk_first_quantity(
            entry_price=guarded_ask,
            atr_pct=_eff_atr_pct,
            max_loss_usd=_eff_max_loss,
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
        # Ross-style marketable-LIMIT entry: cap the fill at the guarded ask (ask +
        # the notional-guard buffer) instead of a market order that can SWEEP a thin
        # low-float book to a catastrophic price (the live stale_bbo / 300bps-abs-cap
        # failure mode that blocked every wide-spread name). The limit stays
        # marketable (at/above the ask) so it fills on the break, but never worse than
        # guarded_ask — the exact price the notional guard already sized against. If
        # it does not fill (the price ran away), the entry ack-timeout cancels it and
        # re-watches: a missed fill, not a chase. (docs/DESIGN/MOMENTUM_LANE.md)
        # MAKER-ONLY crypto entry (2026-06-13): the marketable guarded-ask limit
        # CROSSES and pays TAKER (~153bps RT) — the crypto plan's #1 lever
        # (maker-only, ~50bps) was built to avoid exactly this, but it was never
        # enforced on the order (post_only defaulted False; first live TAO trade's
        # fee $1.77 was 2x its gross loss). For crypto with maker-only enabled,
        # post a POST-ONLY limit AT THE BID (a true maker order). A post-only that
        # would cross is rejected by the venue (no order_id) → the existing
        # ack-timeout cancels + re-watches, exactly like a non-fill ("missed fill,
        # not a chase"). Equity + non-maker crypto keep the marketable guarded-ask.
        _maker_entry = (
            str(sess.symbol or "").upper().endswith("-USD")
            and bool(getattr(settings, "chili_coinbase_maker_only_enabled", False))
            and bid is not None
            and float(bid) > 0
        )
        if _maker_entry:
            entry_limit_px = float(bid)
            entry_limit_str = f"{entry_limit_px:.6f}".rstrip("0").rstrip(".")
        else:
            entry_limit_px = guarded_ask
            entry_limit_str = _fmt_limit_price_buy(entry_limit_px)
            # Anchor for the marketable re-peg chase: the ORIGINAL limit bounds the
            # cumulative drift (the R:R guard), and the re-peg counter resets per fresh entry.
            le["entry_original_limit_px"] = entry_limit_px
            le["entry_repeg_count"] = 0
        le["entry_notional_guard"] = {
            "max_notional_usd": max_notional,
            "ask": ask,
            "bid": bid,
            "mid": mid,
            "guarded_ask": guarded_ask,
            "estimated_guarded_notional_usd": estimated_guarded_notional,
            "quantity": qty,
            "order_type": "limit",
            "limit_price": entry_limit_str,
            "spread_bps": spread_bps_live,
            "slippage_bps_ref": slip_ref,
        }
        record_packet_execution_intent(
            db,
            decision_packet_id,
            {
                "surface": "momentum_live_runner_entry",
                "order_type": "limit",
                "limit_price": entry_limit_str,
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

        # B1: DETERMINISTIC entry id (idempotency). The agentic rail passes this cid as
        # ref_id; a random suffix would let a retried logical entry get a NEW id, so RH
        # could not dedup -> double-submit. Derive the suffix from stable inputs so a
        # re-submit of the SAME logical entry reuses the SAME cid. Format/length are
        # byte-identical to the old uuid form (robin_stocks ignores ref_id; only the
        # string identity matters for that path). Exit/scale/bailout cids stay random
        # this pass (documented follow-up).
        # REF-ID UNIQUENESS (2026-06-22): the agentic rail passes this cid as ref_id, so
        # it MUST be unique per place ATTEMPT — a re-watch / entry-chase re-peg that reused
        # the stable {sess|corr|entry} seed got RH "API 409: Reference ID must be unique"
        # and could NEVER re-enter (the entry-chase was dead the moment the rail unblocked).
        # Bump a per-session entry-place counter into the seed so each attempt is unique;
        # double-submit of the SAME attempt is guarded by the FSM transition to
        # pending_entry + the late-fill-sweep (tracked order_id). Counter persists via the
        # _commit_le after the place. Format/length byte-identical (robin_stocks ignores ref_id).
        _entry_place_n = int(le.get("entry_place_count", 0) or 0) + 1
        le["entry_place_count"] = _entry_place_n
        _entry_id_seed = f"{sess.id}|{sess.correlation_id or 'x'}|entry|{_entry_place_n}".encode("utf-8")
        _entry_suffix = hashlib.sha1(_entry_id_seed).hexdigest()[:10]
        cid = f"chili_ml_e_{sess.id}_{(sess.correlation_id or 'x')[:8]}_{_entry_suffix}"[:120]
        # Pre-market / after-hours entries must be flagged so the venue routes them
        # (Alpaca: limit + DAY tif + extended_hours; RH: extended_hours_override). In
        # the regular session this is False and the order stays a plain marketable GTC.
        try:
            from .market_profile import market_session_now

            _entry_extended = market_session_now(sess.symbol) != "regular"
        except Exception:
            _entry_extended = False
        le["entry_session_extended"] = bool(_entry_extended)
        _entry_kwargs = dict(
            product_id=product_id,
            side="buy",
            base_size=_fmt_base_size(qty),
            limit_price=entry_limit_str,
            client_order_id=cid,
            extended_hours=_entry_extended,
            # ORDER-TRUTH (2026-06-11): entry limits are DAY orders, never GTC —
            # a dead session's resting GTC buy (KMRK) filled hours later into a
            # -21.9% dump. Equity adapters map this to RH 'gfd'; crypto ignores.
            time_in_force="gfd",
        )
        # MAKER-ONLY (2026-06-13): post-only so a crossing price is rejected (no
        # taker) — the ack-timeout then cancels + re-watches. Pass post_only ONLY
        # for crypto+maker (coinbase_spot supports it); the RH equity adapter does
        # NOT accept the kwarg, so equity is called exactly as before (no regress).
        if _maker_entry:
            _entry_kwargs["post_only"] = True
        # ── ATOMIC POSITION CAP (decouple_watching: B1 + B2 + B3) ────────────────
        # Positions are born at FILL (live_pending_entry → live_entered), seconds
        # after this submit. tick_live_session row-locks only its OWN row (:2001),
        # so two watchers tick in parallel and each read the same held count → both
        # submit → cap breached. Fix: an xact-scoped advisory lock keyed on
        # (user, lane) serializes the count-and-submit across worker connections
        # (batch pool + WS event loop both land here), so each submitter SEES the
        # prior one's committed in-flight order. The count therefore charges held
        # positions PLUS in-flight-submitted entries (born-but-not-yet-held) — only
        # that pair makes the cap exact under a burst (held-only would let every
        # serialized submitter read N-1). The lock auto-releases at the per-tick
        # db.commit() (event loop :247 / batch :891), so a hung worker cannot wedge
        # the lane (the auto_trader.py:1963 orphan-lock lesson). Flag OFF ⇒ this
        # entire block is a no-op — legacy single-cap path, parity-tested.
        if getattr(settings, "chili_momentum_decouple_watching_enabled", False):
            from sqlalchemy import text as _sql_text

            from .risk_evaluator import (
                aggregate_open_crypto_risk_usd,
                count_inflight_entry_orders,
                count_open_positions,
            )
            from .risk_policy import (
                _account_equity_usd,
                effective_position_cap,
                equity_relative_loss_cap,
            )

            _is_crypto = str(sess.symbol or "").upper().endswith("-USD")
            # "ML" (momentum lane) namespace in the high word — distinct from
            # auto_trader's 0x4154 "AT"; lane_key stays well under 2**63.
            _lane_key = (_MOMENTUM_LANE_LOCK_NS << 32) | (int(sess.user_id) & 0xFFFFFFFF)
            db.execute(_sql_text("SELECT pg_advisory_xact_lock(:k)"), {"k": _lane_key})

            _cap = effective_position_cap(crypto=_is_crypto)
            _pos_ct = count_open_positions(db, user_id=sess.user_id, mode="live") + (
                count_inflight_entry_orders(db, user_id=sess.user_id, exclude_session_id=sess.id)
            )
            if _pos_ct >= _cap:
                _emit(db, sess, "live_entry_blocked_position_cap", {"pos_ct": _pos_ct, "cap": _cap})
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
                db.flush()
                return {
                    "ok": True, "session_id": sess.id, "state": sess.state,
                    "skipped": "position_cap_at_fill", "pos_ct": _pos_ct, "cap": _cap,
                }
            # ADAPTIVE DAILY TRADE-COUNT BUDGET (SCAL101): cap the NUMBER of fresh entries
            # per ET session — a discipline/overtrading guard distinct from the slot COUNT
            # above. Ceiling floats with regime heat (today's banked cushion) + recent
            # expectancy: tighten when the lane is cold, loosen when hot. _pos_ct (open +
            # in-flight, already computed under this lock) is the live open-entry count so a
            # fill-burst can't slip past the count. ADDITIVE/FAIL-OPEN: flag off / thin data
            # => allowed (byte-identical). Evaluated INSIDE the advisory lock so the count is
            # atomic with the position cap.
            from .risk_policy import daily_trade_count_budget_decision

            _tcb_ok, _tcb_meta = daily_trade_count_budget_decision(
                db, execution_family=ef, open_entry_count=_pos_ct
            )
            if not _tcb_ok:
                _emit(db, sess, "live_entry_blocked_daily_trade_count_budget", _tcb_meta)
                _safe_transition(db, sess, STATE_WATCHING_LIVE)
                db.flush()
                return {
                    "ok": True, "session_id": sess.id, "state": sess.state,
                    "skipped": "daily_trade_count_budget", "budget": _tcb_meta,
                }
            if _is_crypto:
                _cryp_ct = count_open_positions(
                    db, user_id=sess.user_id, mode="live", crypto_only=True
                ) + count_inflight_entry_orders(
                    db, user_id=sess.user_id, crypto_only=True, exclude_session_id=sess.id
                )
                _bucket_cap = int(
                    getattr(settings, "chili_momentum_max_open_positions_per_correlation_bucket", 4) or 4
                )
                if _cryp_ct >= _bucket_cap:
                    _emit(db, sess, "live_entry_blocked_crypto_bucket_cap",
                          {"cryp_ct": _cryp_ct, "bucket_cap": _bucket_cap})
                    _safe_transition(db, sess, STATE_WATCHING_LIVE)
                    db.flush()
                    return {
                        "ok": True, "session_id": sess.id, "state": sess.state,
                        "skipped": "crypto_bucket_cap", "cryp_ct": _cryp_ct, "bucket_cap": _bucket_cap,
                    }
                # B2 crypto dollar backstop. FAIL CLOSED when equity is unknown —
                # never size a position against an unknown account ([[feedback_adaptive_no_magic]]);
                # over-leverage is catastrophic, a missed entry during an equity-fetch
                # outage is not (exits don't pass through here, so open positions stay
                # managed). Equity is normally available live; this trips only on outage.
                _eq = _account_equity_usd(ef)
                if not _eq or float(_eq) <= 0:
                    _emit(db, sess, "live_entry_blocked_equity_unavailable", {"lane": "crypto"})
                    _safe_transition(db, sess, STATE_WATCHING_LIVE)
                    db.flush()
                    return {
                        "ok": True, "session_id": sess.id, "state": sess.state,
                        "skipped": "equity_unavailable",
                    }
                _open_cryp_risk, _ = aggregate_open_crypto_risk_usd(db, user_id=sess.user_id)
                _planned_usd = float(equity_relative_loss_cap(0.0, ef) or 0.0)
                # In-flight crypto entries (submitted, not yet filled) carry $ at-risk the
                # held-only aggregate can't see. Charge each a conservative per-trade proxy
                # (every crypto entry sizes to ~one loss-fraction) so a fill-burst can't
                # slip dollars past the ceiling — B3 already bounds the COUNT atomically in
                # the same lock; this bounds the DOLLARS. Over-estimating is the safe side.
                _inflight_cryp = count_inflight_entry_orders(
                    db, user_id=sess.user_id, crypto_only=True, exclude_session_id=sess.id
                )
                _inflight_cryp_risk = float(_inflight_cryp) * _planned_usd
                _cap_usd = float(
                    getattr(settings, "chili_momentum_max_aggregate_crypto_risk_pct_of_equity", 0.07) or 0.07
                ) * float(_eq)
                _proj_cryp_usd = _open_cryp_risk + _inflight_cryp_risk + _planned_usd
                if _cap_usd > 0 and _proj_cryp_usd > _cap_usd:
                    _emit(db, sess, "live_entry_blocked_crypto_dollar_cap", {
                        "open_crypto_risk_usd": round(_open_cryp_risk, 2),
                        "inflight_crypto_risk_usd": round(_inflight_cryp_risk, 2),
                        "planned_usd": round(_planned_usd, 2),
                        "projected_usd": round(_proj_cryp_usd, 2),
                        "cap_usd": round(_cap_usd, 2),
                    })
                    _safe_transition(db, sess, STATE_WATCHING_LIVE)
                    db.flush()
                    return {
                        "ok": True, "session_id": sess.id, "state": sess.state,
                        "skipped": "crypto_dollar_cap",
                    }
        # ── end atomic position cap; the lock releases when this tick's txn commits ─
        # ── ENTRY-TIME FLOW VETO: never BUY this exact tick into max selling. Keys on
        # LIVE flow (OFI + trade_flow), NOT the static book_imbalance the L2 seller-veto
        # reads. Applies to extreme movers too (selection vs entry-timing). ADDITIVE:
        # flag OFF or either flow absent (None) ⇒ no veto. On veto: stay WATCHING
        # (re-enter when flow flips positive). ──
        #
        # DATA SOURCE (deploy-blocker fix 2026-06-24): ofi/trade_flow are sourced FRESH
        # for sess.symbol from the SAME readers viability/entry_features use
        # (_live_ofi_microprice / _live_trade_flow) — NOT ex_live (execution_readiness_json),
        # which NEVER carries these keys (0 of 80,493 momentum_symbol_viability rows had
        # ofi/trade_flow; the batch-shared exec_json only persists batch feats, which have
        # them None). Reading ex_live made the None-guard ALWAYS fire ⇒ the veto was inert.
        # These are the EXACT readers capture_entry_features uses (so the value matches the
        # logged entry_features ofi=-1.0/trade_flow=-0.51 for the PLSM flush). Live default
        # as_of=None (crypto -> in-process Coinbase L2 ring + fast_orderbook fallback;
        # equity -> iqfeed_depth_snapshots / iqfeed_trade_ticks). Cheap (one short 15s-window
        # read) + exception-safe; any error -> None -> no veto (fail-open, byte-identical).
        try:
            from .pipeline import _live_ofi_microprice, _live_trade_flow

            _fv_ofi, _ = _live_ofi_microprice(sess.symbol, db=db)
            _fv_tf = _live_trade_flow(sess.symbol, db=db)
            _fv_ofi = None if _fv_ofi is None else float(_fv_ofi)
            _fv_tf = None if _fv_tf is None else float(_fv_tf)
        except Exception:
            _fv_ofi = _fv_tf = None
        if _entry_flow_veto(_fv_ofi, _fv_tf, settings):
            _log.info(
                "[momentum_neural] entry FLOW-VETO %s: OFI=%s trade_flow=%s — deferring buy into selling",
                sess.symbol, _fv_ofi, _fv_tf,
            )
            _emit(db, sess, "live_entry_flow_veto", {
                "ofi": round(_fv_ofi, 4) if _fv_ofi is not None else None,
                "trade_flow": round(_fv_tf, 4) if _fv_tf is not None else None,
                "ofi_thr": float(getattr(settings, "chili_momentum_entry_flow_veto_ofi", -0.6)),
                "trade_flow_thr": float(getattr(settings, "chili_momentum_entry_flow_veto_trade_flow", -0.25)),
            })
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True, "session_id": sess.id, "state": sess.state,
                "skipped": "entry_flow_veto",
            }
        # ── ENTRY-EXTENSION (chase) VETO: never BUY this exact tick when the entry sits
        # too far ABOVE the breakout level (bought near a local top after the move ran;
        # 06-24 RUN +19.9% / PLSM +33.8% above the break). Cap is ADAPTIVE to volatility
        # (max(floor, K·atr_pct)). entry_price = the marketable limit (entry_limit_px,
        # set just above); breakout_level = le["breakout_level_price"]; atr_pct = the
        # eff stop ATR% local. ADDITIVE: flag OFF or breakout_level/atr missing ⇒ no veto
        # (byte-identical). On veto: stay WATCHING (re-enter on a pullback toward the
        # level), NOT terminal — same defer pattern as the flow-veto + crypto_dollar_cap. ──
        try:
            _ev_lvl = _float_or_none(le.get("breakout_level_price"))
            # Extension-veto vol input = the CLEAN intraday-range proxy regime_atr_pct(_regime),
            # NOT _eff_atr_pct. _eff_atr_pct is the STOP-focused ATR after (a) effective_stop_atr_pct
            # vol-flooring and (b) the structural_or_vol_floored_atr_pct override = (entry-pullback_low)
            # /entry / stop_atr_mult. On the pullback-break path the structural override is ACTIVE, and a
            # MORE-extended chase (deeper pullback_low) INFLATES _eff_atr_pct, which would LOOSEN the cap
            # (max(floor, K*atr_pct)) on exactly the names the chase-veto must block (RUN +19.9% / PLSM
            # +33.8% slipped through). The clean regime ATR is the true intraday volatility, unaffected by
            # how deep the entry chased — so the cap stays tight as the chase extends. (06-24 recalibration.)
            _clean_regime_atr = regime_atr_pct(_regime)
            _ev_atr = float(_clean_regime_atr) if _clean_regime_atr is not None else None
            _ev_entry = float(entry_limit_px) if entry_limit_px is not None else None
        except (TypeError, ValueError):
            _ev_lvl = _ev_atr = _ev_entry = None
        if _entry_extension_veto(_ev_entry, _ev_lvl, _ev_atr, settings):
            _ext_cap = max(
                float(getattr(settings, "chili_momentum_entry_extension_floor_pct", 0.05)),
                float(getattr(settings, "chili_momentum_entry_extension_atr_mult", 1.0)) * max(0.0, float(_ev_atr or 0.0)),
            )
            _log.info(
                "[momentum_neural] entry EXTENSION-VETO %s: entry=%s vs break=%s (cap=%.4f atr_pct=%s) — deferring chase",
                sess.symbol, _ev_entry, _ev_lvl, _ext_cap, _ev_atr,
            )
            _emit(db, sess, "live_entry_extension_veto", {
                "entry_price": round(_ev_entry, 6) if _ev_entry is not None else None,
                "breakout_level": round(_ev_lvl, 6) if _ev_lvl is not None else None,
                "atr_pct": round(_ev_atr, 6) if _ev_atr is not None else None,
                "extension_cap_pct": round(_ext_cap, 6),
                "extension_pct": (
                    round((_ev_entry / _ev_lvl) - 1.0, 6)
                    if (_ev_entry is not None and _ev_lvl) else None
                ),
            })
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
            db.flush()
            return {
                "ok": True, "session_id": sess.id, "state": sess.state,
                "skipped": "entry_extension_veto",
            }
        res = adapter.place_limit_order_gtc(**_entry_kwargs)
        le["entry_submitted"] = True
        le["entry_submit_utc"] = _utcnow().isoformat()
        le["entry_order_type"] = "limit"
        le["entry_limit_price"] = entry_limit_str
        # FILL_OUTCOME_LOG (mig308): capture the REAL decision-time BBO spread at the
        # submit pulse so the fill row (and the replay) sees the spread the gate
        # actually faced, not a later NBBO snapshot. Side channel only — no behavior.
        try:
            if mid and float(mid) > 0 and bid is not None and ask is not None:
                le["entry_spread_bps_at_decision"] = max(
                    0.0, (float(ask) - float(bid)) / float(mid) * 10_000.0
                )
        except (TypeError, ValueError, ZeroDivisionError):
            pass
        # stash the name's expected-move so the chase ceiling can vol-widen at cancel time
        le["entry_expected_move_bps"] = (None if _expected_move_bps is None else float(_expected_move_bps))
        le["entry_client_order_id"] = res.get("client_order_id") or cid
        le["entry_order_id"] = res.get("order_id")
        # History: the ack-timeout may wipe the ACTIVE pointer later, but this id is
        # never forgotten — the late-fill sweep + pre-submit guard track it to a
        # terminal resolution (adopted | void). No fill can become untracked again.
        _record_entry_order_placed(le, res.get("order_id"))
        le["entry_place_result"] = {"ok": res.get("ok"), "error": res.get("error")}
        _commit_le(sess, le)
        _emit(db, sess, "live_entry_submitted", {
            "client_order_id": le["entry_client_order_id"],
            "order_type": "limit",
            "limit_price": entry_limit_str,
            "result": res,
        })
        if not res.get("ok"):
            # ADAPTIVE ENTRY-REJECT COOLDOWN (2026-06-22): the broker REFUSED this entry
            # (place_equity_order isError — a leveraged/inverse ETF tripping
            # EQUITY_SUITABILITY like RKLZ/CORD, or a name untradable in the session).
            # It will reject again the instant it re-arms; tell auto-arm to sit it out so
            # the lane stops looping arm->break->reject->reap and a FILLABLE mover gets the
            # slot. Lazy import dodges a load-time cycle; best-effort (never block the
            # error transition). Equity OR crypto — any rail that refuses an entry.
            try:
                from .auto_arm import _write_entry_reject_cooldown

                _write_entry_reject_cooldown(str(sess.symbol or "").upper())
            except Exception:
                pass
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
            is_scale_out = bool(le.get("pending_exit_is_scale_out"))
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
                if is_scale_out:
                    # Deliberate first-target scale-out confirmed on a later tick:
                    # bank the partial, move the balance to breakeven, hold the runner.
                    _scale_out_to_runner(
                        db,
                        sess,
                        le=le,
                        filled_quantity=min(max(pending_qty, 0.0), qty),
                        entry_price=avg,
                        fill_price=float(poll["fill_price"]),
                        reason=str(pending_exit_reason),
                    )
                else:
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

        # C1b: HARD MAX-LOSS-PER-TRADE CIRCUIT (#1 profitability lever, 2026-06-17).
        # The 1x C1 check above transitions to BAILOUT, but the bid-relative ladder then
        # chases a falling/gapped book and fills 5-9% deep (the -$697.76 RH low-float
        # tail: MTEN/SDOT/CCTG/CAST gapped THROUGH their tight stops). This circuit caps
        # each trade's loss at K x the REALIZED STRUCTURAL RISK (stop_distance x qty — NOT
        # the frozen risk_usd budget, ~12x overstated) and flattens at an ABSOLUTE loss
        # anchor (avg - K*stop_distance) via a single capped limit (no repeg), so a deep
        # gap-through fill is mechanically impossible. Fires INSIDE the 1x window when the
        # structural threshold is the tighter cap. Guarded: flag, state (not BAILOUT),
        # double-fire, fresh-quote (this tick was not stale), and a usable basis.
        if (
            getattr(settings, "chili_momentum_max_loss_circuit_enabled", True)
            and st in (STATE_LIVE_ENTERED, STATE_LIVE_SCALING_OUT, STATE_LIVE_TRAILING)
            and not le.get("max_loss_circuit_fired")
        ):
            # Fresh-quote gate: a finite positive bid AND this tick did NOT register a
            # stale-quote streak (the halt-stale counter is reset to 0 by a fresh tick
            # earlier this pulse; >=1 means staleness — never fire on a frozen/halted book).
            _fresh_quote = (
                bid is not None
                and math.isfinite(float(bid))
                and float(bid) > 0
                and int(le.get("halt_stale_streak") or 0) == 0
                and not le.get("suspected_halt_since_utc")
            )
            if _fresh_quote:
                # Basis = the REALIZED per-share structural stop distance frozen at entry.
                _stop_distance = None
                _es = le.get("entry_sizing")
                if isinstance(_es, dict):
                    try:
                        _sd = float(_es.get("stop_distance"))
                        if _sd > 0 and math.isfinite(_sd):
                            _stop_distance = _sd
                    except (TypeError, ValueError):
                        _stop_distance = None
                # Fallback: derive the structural distance from the live stop price.
                if _stop_distance is None:
                    try:
                        _sd2 = float(avg) - float(pos["stop_price"])
                        if _sd2 > 0 and math.isfinite(_sd2):
                            _stop_distance = _sd2
                    except (TypeError, ValueError, KeyError):
                        _stop_distance = None
                if _stop_distance is not None:
                    _k = float(getattr(settings, "chili_momentum_max_loss_risk_multiple", 2.0) or 2.0)
                    # GUARD #1 (risk-neutral pyramid): when this position has been
                    # pyramided, le["pyramid_risk_anchor_usd"] holds the STARTER's
                    # original structural risk R0. Passing it clamps the #769 circuit
                    # threshold to R0, so the ENLARGED qty cannot re-base the floor to
                    # k*sd*q1 (~3-4.5x R0) — the enlarged worst-case stays <= R0. None
                    # (no pyramid) => byte-identical legacy circuit (floor == avg-k*sd).
                    _circuit = max_loss_circuit_decision(
                        avg=avg, qty=qty, stop_distance=_stop_distance, bid=bid, k=_k,
                        risk_anchor_usd=le.get("pyramid_risk_anchor_usd"),
                    )
                    if _circuit.get("breach"):
                        le["max_loss_circuit_fired"] = True
                        le["max_loss_circuit_floor_price"] = _circuit["floor_price"]
                        # EQUITY-FIRST: the absolute floor + repeg-skip apply to the RH
                        # EQUITY paths only (where the gap-through tail lives) — BOTH the
                        # unofficial robin_stocks rail (robinhood_spot) AND the sanctioned
                        # Agentic Trading MCP rail (robinhood_agentic_mcp), which trade the
                        # SAME RH low-float names with the SAME gap-through risk (LULD halts,
                        # overnight gaps). Crypto (-USD) may fire but keeps the bid-relative
                        # ladder (dust, 24/7, no LULD).
                        if normalize_execution_family(sess.execution_family) in (
                            EXECUTION_FAMILY_ROBINHOOD_SPOT,
                            EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
                        ):
                            le["exit_floor_anchored"] = True
                        _commit_le(sess, le)
                        _safe_transition(db, sess, STATE_LIVE_BAILOUT)
                        _emit(db, sess, "live_bailout", {
                            "reason": "max_loss_circuit",
                            "unrealized_pnl": _circuit["unrealized_pnl"],
                            "structural_risk_usd": _circuit["structural_risk_usd"],
                            "threshold_usd": _circuit["threshold_usd"],
                            "floor_price": _circuit["floor_price"],
                            "risk_multiple_used": _k,
                        })
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
            # MAX-LOSS-CIRCUIT HALT GATE (2026-06-17): a circuit-originated bailout must
            # NOT submit into a halted/frozen book — a marketable-but-capped floor cannot
            # fill while quotes are stale, and submitting risks a stranded order or a fill
            # the instant the halt lifts at a worse price. Hold in BAILOUT and re-attempt
            # on the first fresh-quote tick after resume (suspected_halt cleared by
            # _register_fresh_quote_tick). Keyed on max_loss_circuit_fired so legacy 1x /
            # stop / breakout bailouts are byte-identical (they still submit through halts).
            if le.get("max_loss_circuit_fired") and le.get("suspected_halt_since_utc"):
                _emit(db, sess, "max_loss_circuit_halt_deferred", {
                    "floor_price": le.get("max_loss_circuit_floor_price"),
                    "suspected_halt_since_utc": le.get("suspected_halt_since_utc"),
                })
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "halt_deferred": True}
            cid = f"chili_ml_b_{sess.id}_{uuid.uuid4().hex[:12]}"
            # Circuit bailouts on the RH (equity) path flatten at the ABSOLUTE loss-anchored
            # floor (no bid-relative ladder, no repeg). EQUITY-FIRST: keyed on
            # exit_floor_anchored, which the circuit sets ONLY for the RH equity rails
            # (robinhood_spot AND robinhood_agentic_mcp) — crypto circuit fires but
            # exit_floor_anchored is unset, so it falls through to None and keeps the legacy
            # bid-relative ladder (dust, 24/7, no LULD). All OTHER bailout reasons pass None
            # => byte-identical legacy ladder.
            _bailout_floor = (
                le.get("max_loss_circuit_floor_price")
                if le.get("exit_floor_anchored")
                else None
            )
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
                hard_floor_price=_bailout_floor,
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
            _sm = float(params.get("stop_atr_mult") or 0.60)
            _q0 = _float_or_none(pos.get("original_quantity")) or _float_or_none(pos.get("quantity")) or 0.0
            # 5m EMA9 structural anchor for the runner trail — refreshed at most
            # once per minute per session (cached in le), fail-open (None).
            _ema5 = None
            try:
                _min_key = _utcnow().strftime("%Y%m%d%H%M")
                if le.get("ema5m_min") == _min_key:
                    _ema5 = _float_or_none(le.get("ema5m_val"))
                else:
                    from ..market_data import fetch_ohlcv_df as _e5_fetch

                    _df5 = _e5_fetch(sess.symbol, interval="5m", period="1d")
                    if _df5 is not None and len(_df5) >= 9:
                        _ema5 = float(_df5["Close"].ewm(span=9, adjust=False).mean().iloc[-1])
                    le["ema5m_min"] = _min_key
                    le["ema5m_val"] = _ema5
                    _commit_le(sess, le)
            except Exception:
                _ema5 = None
            _trailed = cushion_adaptive_trail_stop(
                high_water_mark=_hwm_trail,
                entry_price=avg,
                atr_pct=_atr_pct_trail,
                stop_atr_mult=_sm,
                day_realized_usd=_day_realized_usd_cached(db, int(sess.user_id)),
                position_risk_usd=(avg * max(0.003, _atr_pct_trail * _sm)) * _q0,
                breakeven_floor=_be_floor,
                current_stop=stop_px,
                side_long=True,
                ema_5m=_ema5,
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

            # Adaptive order-flow EXHAUSTION LOCK (crypto runner). Runs AFTER the
            # cushion ratchet so `current_band_bps` is the band's REALIZED output
            # this tick — the lock can then only ever tighten vs the actual trail
            # (never clamps to a looser theoretical band). Crypto-only: equity is
            # byte-identical (this entire block is gated on `-USD`). The lock is
            # ratchet-only over the structural stop and clamped no looser than the
            # band. The A/B counterfactual (fixed-R:R stop, lock OFF) is emitted on
            # EVERY armed tick so realized PnL is measured vs baseline LIVE before
            # the partial (Action B) ever moves size. (docs/DESIGN/ADAPTIVE_OFI_EXIT.md)
            if (
                sess.symbol.endswith("-USD")
                or bool(getattr(settings, "chili_momentum_exit_adaptive_equity_enabled", True))
            ) and bool(
                getattr(settings, "chili_momentum_exit_ofi_lock_enabled", True)
            ):
                try:
                    from .pipeline import _live_ofi_microprice

                    _ofi_x, _mpe_x = _live_ofi_microprice(sess.symbol, db=db)
                    # Hidden-seller absorption (accelerant) only when its flag is on;
                    # reads the in-process COINBASE book ring — CRYPTO-ONLY (an equity
                    # symbol has no ring entry → empty → _hs_x stays None anyway, but
                    # gate it explicitly so the crypto-only coupling is documented).
                    _hs_x = None
                    if sess.symbol.endswith("-USD") and bool(
                        getattr(settings, "chili_momentum_exit_ofi_hidden_seller_enabled", False)
                    ):
                        try:
                            from ..microstructure import get_book_buffer
                            from ..fast_path.microstructure_log import _hidden_seller

                            _hs_win = float(
                                getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0
                            )
                            _hs_snaps = get_book_buffer().recent(sess.symbol, window_secs=_hs_win)
                            if len(_hs_snaps) >= 2:
                                _hs_x = _hidden_seller(_hs_snaps)
                        except Exception:
                            _hs_x = None
                    # current_band_bps = the cushion band's REALIZED stop this tick.
                    _band_bps = ((_hwm_trail - stop_px) / _hwm_trail * 10_000.0) if _hwm_trail > 0 else 0.0
                    # 1m candle exhaustion confirmer: the entry trigger runs on 1m but
                    # the lock's only candle read upstream is the coarse 15m _entry_df.
                    # Fetch a 1m df at most once/min/session (mirrors the 5m-EMA cache
                    # above) and read a topping-tail (+ optional MACD-hist rollover) as
                    # ONE MORE AND-gated corroborant into the lock's FLOW confluence.
                    # Fail-open (None). The gate goes LIVE only under _confirm_live;
                    # default emits the candle_would_suppress A/B only. Class-agnostic
                    # (crypto + equity, same fetch). docs/DESIGN/ADAPTIVE_OFI_EXIT.md
                    _candle_exh = None
                    if bool(getattr(settings, "chili_momentum_exit_candle_confirm_enabled", True)):
                        try:
                            _cc_key = _utcnow().strftime("%Y%m%d%H%M")
                            if le.get("exit_candle1m_min") == _cc_key:
                                _candle_exh = le.get("exit_candle1m_exh")
                            else:
                                from ..market_data import fetch_ohlcv_df as _c1_fetch
                                from .candles import (
                                    topping_tail_from_df,
                                    macd_hist_rollover_from_df,
                                )

                                _df1 = _c1_fetch(sess.symbol, interval="1m", period="1d")
                                if _df1 is not None and len(_df1) >= 2:
                                    _tt1 = bool(topping_tail_from_df(_df1))
                                    _mh1 = (
                                        bool(macd_hist_rollover_from_df(_df1))
                                        if bool(getattr(settings, "chili_momentum_exit_candle_confirm_use_macd", True))
                                        else False
                                    )
                                    _candle_exh = bool(_tt1 or _mh1)
                                le["exit_candle1m_min"] = _cc_key
                                le["exit_candle1m_exh"] = _candle_exh
                                _commit_le(sess, le)
                        except Exception:
                            _candle_exh = None
                    _lock = ofi_exhaustion_lock(
                        high_water_mark=_hwm_trail,
                        entry_price=avg,
                        bid=bid,
                        atr_pct=_atr_pct_trail,
                        stop_atr_mult=_sm,
                        ofi=_ofi_x,
                        micro_edge=_mpe_x,
                        hidden_seller=_hs_x,
                        reward_risk=class_aware_reward_risk(sess.symbol),
                        current_stop=stop_px,
                        breakeven_floor=_be_floor,
                        current_band_bps=_band_bps,
                        candle_exhaustion=_candle_exh,
                        candle_gate_live=bool(
                            getattr(settings, "chili_momentum_exit_candle_confirm_live", False)
                        ),
                        side_long=True,
                    )
                    # A/B telemetry on every ARMED tick (winner past the profit-arm),
                    # whether or not the lock fired — this is the counterfactual that
                    # proves capture vs the fixed-R:R baseline before we trust it.
                    if _lock.get("armed"):
                        _emit(db, sess, "live_ofi_exhaustion_lock", {
                            "fired": bool(_lock.get("fired")),
                            "trigger": _lock.get("trigger"),
                            "peak_r": _lock.get("peak_r"),
                            "lock_bps": _lock.get("lock_bps"),
                            "band_bps": round(_band_bps, 2),
                            "ofi": _ofi_x,
                            "micro_edge": _mpe_x,
                            "hidden_seller": _hs_x,
                            "adaptive_stop": _lock.get("new_stop_floor"),
                            "counterfactual_fixed_stop": _lock.get("counterfactual_fixed_stop"),
                            "partial_arm": bool(_lock.get("partial_arm")),
                            "candle_exhaustion": _lock.get("candle_exhaustion"),
                            "candle_ok": _lock.get("candle_ok"),
                            "candle_gate_live": _lock.get("candle_gate_live"),
                            "candle_would_suppress": _lock.get("candle_would_suppress"),
                            "bid": bid,
                            "high_water_mark": _hwm_trail,
                        })
                    # Action A: ratchet-only stop write (belt-and-suspenders > guard).
                    _lock_stop = _float_or_none(_lock.get("new_stop_floor"))
                    if _lock.get("fired") and _lock_stop is not None and _lock_stop > stop_px:
                        pos["stop_price"] = _lock_stop
                        stop_px = _lock_stop
                        le["position"] = pos
                        _commit_le(sess, le)
                    # Action B: arm the early partial (one-tick flag read at 3778).
                    # Default OFF (log-would-fire-first); promote after A/B proves out.
                    if bool(getattr(settings, "chili_momentum_exit_ofi_lock_partial_enabled", False)):
                        if _lock.get("fired") and _lock.get("partial_arm"):
                            le["exhaustion_lock_partial_armed"] = True
                            _commit_le(sess, le)
                except Exception:
                    pass

            # v2 PROACTIVE sell-into-strength (Ross ladder read) — sibling to v1, runs
            # AFTER it so they compose: v1 DEFENDS (tightens the stop on exhaustion),
            # v2 HARVESTS the top (a small resting limit into genuine strength). The
            # counterfactual A/B + the INVARIANT-A stop-ratchet are LIVE on every armed
            # tick; the size-moving resting limit is gated by exit_ladder_live (2-step
            # ship). CLASS-AWARE: crypto always runs (byte-identical); equity runs when
            # chili_momentum_exit_adaptive_equity_enabled (default ON) — equity L2 from
            # iqfeed, same helpers. Fail-open: any error => no-op.
            if (
                sess.symbol.endswith("-USD")
                or bool(getattr(settings, "chili_momentum_exit_adaptive_equity_enabled", True))
            ) and bool(
                getattr(settings, "chili_momentum_exit_ladder_enabled", True)
            ):
                try:
                    from .paper_execution import sell_into_strength_ladder
                    from .pipeline import read_ladder_distribution

                    _cooldown = False
                    try:
                        _cd_raw = le.get("ladder_cooldown_until_utc")
                        if _cd_raw:
                            _cooldown = _utcnow() < datetime.fromisoformat(_cd_raw)
                    except Exception:
                        _cooldown = False
                    _ladder = read_ladder_distribution(sess.symbol, db=db)
                    _sis = sell_into_strength_ladder(
                        high_water_mark=_hwm_trail,
                        entry_price=avg,
                        bid=bid,
                        atr_pct=_atr_pct_trail,
                        stop_atr_mult=_sm,
                        reward_risk=class_aware_reward_risk(sess.symbol),
                        current_stop=stop_px,
                        breakeven_floor=_be_floor,
                        remaining_qty=_float_or_none(pos.get("quantity")) or 0.0,
                        ladder=_ladder,
                        prior_partial_taken=bool(pos.get("partial_taken")),
                        cooldown_active=_cooldown,
                        side_long=True,
                    )
                    if _sis.get("armed"):
                        _emit(db, sess, "live_sell_into_strength", {
                            "state": _sis.get("state"),
                            "fired": bool(_sis.get("fired")),
                            "vetoed_by": _sis.get("vetoed_by"),
                            "reason": _sis.get("reason"),
                            "peak_r": _sis.get("peak_r"),
                            "dist_pctile": _sis.get("dist_pctile"),
                            "rung_bps": _sis.get("rung_bps"),
                            "first_increment_frac": _sis.get("first_increment_frac"),
                            "limit_px": _sis.get("limit_px"),
                            "sell_qty": _sis.get("sell_qty"),
                            "adaptive_stop": _sis.get("new_stop_floor"),
                            "counterfactual_hold_stop": _sis.get("counterfactual_hold_stop"),
                            "ofi": getattr(_ladder, "ofi", None),
                            "micro_edge": getattr(_ladder, "micro_edge", None),
                            "bid_refill": getattr(_ladder, "bid_refill", None),
                            "n_snaps": getattr(_ladder, "n_snaps", 0),
                            "live": bool(getattr(settings, "chili_momentum_exit_ladder_live", False)),
                            "bid": bid,
                            "high_water_mark": _hwm_trail,
                        })
                    # Action A: ratchet-only stop (INVARIANT A; live-on, can only help).
                    _sis_stop = _float_or_none(_sis.get("new_stop_floor"))
                    if _sis_stop is not None and _sis_stop > stop_px:
                        pos["stop_price"] = _sis_stop
                        stop_px = _sis_stop
                        le["position"] = pos
                        _commit_le(sess, le)
                    # Size-moving resting limit — GATED. Reuse the scale-out limit
                    # adoption machinery; one scale limit at a time (don't collide with
                    # v1's partial). On fill, the runner remainder ratchets to the fill.
                    if (
                        bool(getattr(settings, "chili_momentum_exit_ladder_live", False))
                        and _sis.get("fired")
                        and _sis.get("action") == "sell_limit"
                        and not le.get("scale_limit_order_id")
                    ):
                        _ll_px = _float_or_none(_sis.get("limit_px"))
                        _ll_qty = _float_or_none(_sis.get("sell_qty"))
                        if _ll_px and _ll_qty and _ll_qty > 0:
                            _ll_cid = f"chili_ml_sis_{sess.id}_{uuid.uuid4().hex[:12]}"
                            _ll_kwargs = dict(
                                product_id=product_id,
                                side="sell",
                                base_size=_fmt_base_size(_ll_qty),
                                limit_price=_fmt_limit_price_sell(_ll_px),
                                client_order_id=_ll_cid,
                            )
                            if not sess.symbol.endswith("-USD"):
                                # EQUITY: a DAY order (auto-cancels at the close — the
                                # free-option expires daily, never a stale resting GTC);
                                # extended_hours flagged when outside RTH. Crypto keeps the
                                # bare 24/7 GTC call (byte-identical).
                                try:
                                    from .market_profile import market_session_now

                                    _ll_ext = market_session_now(sess.symbol) != "regular"
                                except Exception:
                                    _ll_ext = False
                                _ll_kwargs["time_in_force"] = "gfd"
                                _ll_kwargs["extended_hours"] = _ll_ext
                            _ll_res = adapter.place_limit_order_gtc(**_ll_kwargs) or {}
                            if _ll_res.get("ok") and _ll_res.get("order_id"):
                                le["scale_limit_order_id"] = str(_ll_res["order_id"])
                                le["scale_limit_px"] = float(_ll_px)
                                le["scale_limit_qty"] = float(_ll_qty)
                                le["scale_limit_adopted_qty"] = 0.0
                                le["scale_limit_source"] = "sell_into_strength"
                                # cooldown so a second rung can't stack for ~15s
                                le["ladder_cooldown_until_utc"] = (
                                    _utcnow() + timedelta(seconds=15)
                                ).isoformat()
                                _commit_le(sess, le)
                                _emit(db, sess, "sell_into_strength_limit_placed", {
                                    "order_id": le["scale_limit_order_id"],
                                    "qty": float(_ll_qty), "limit_price": float(_ll_px),
                                    "peak_r": _sis.get("peak_r"), "rung_bps": _sis.get("rung_bps"),
                                })
                except Exception:
                    pass

            # ── RISK-NEUTRAL CONFIRMATION PYRAMID (single add to a winner) ───────
            # Placed AFTER the cushion-trail ratchet + OFI-lock + v2-ladder, so
            # `stop_px` here is the FRESHEST ratcheted value, and BEFORE the
            # stop-breach block below — the add is entry-side ONLY and physically
            # cannot precede or delay an exit. The whole block is a no-op when the
            # flag is OFF (byte-identical: no add, no pos mutation, no emit, no extra
            # broker call, #769 anchor stays None). Two phases, both FALL THROUGH
            # (never early-return) so the freshly-ratcheted stop-breach check still
            # runs this same tick. (docs/DESIGN/MOMENTUM_LANE.md)
            if bool(getattr(settings, "chili_momentum_pyramid_enabled", False)):
                try:
                    # PHASE 1 — resolve an IN-FLIGHT add order (mirror entry adopt).
                    # Mutate pos ONLY on a CONFIRMED fill; a partial blends ONLY the
                    # filled qty. While an order is in flight, PHASE 2 cannot submit a
                    # second (idempotency). No early return: on a confirmed/partial
                    # fill we fall through with the freshly-blended pos + ratcheted s1.
                    _pyr_oid = le.get("pyramid_order_id")
                    if _pyr_oid:
                        _pno, _ = adapter.get_order(str(_pyr_oid))
                        if _pno is not None and not _order_open(_pno):
                            _pyr_filled = float(getattr(_pno, "filled_size", 0) or 0)
                            if _pyr_filled > 0:
                                # CONFIRMED ADD FILL — blend via the SHARED pure helper
                                # (one source of truth with replay + tests). A partial add
                                # blends ONLY the filled qty.
                                _qa_f = _pyr_filled
                                _Pa_f = float(
                                    getattr(_pno, "average_filled_price", 0) or 0
                                ) or float(le.get("pyramid_limit_px") or ask)
                                _q0p = float(pos["quantity"])
                                _a0p = float(pos["avg_entry_price"])
                                _R0p = _float_or_none(le.get("pyramid_pending_R0"))
                                _prev_stop = float(pos["stop_price"])
                                _blend = pyramid_blend_on_fill(
                                    q0=_q0p, a0=_a0p, qa_f=_qa_f, Pa_f=_Pa_f,
                                    stop_px=_prev_stop,
                                    original_quantity=_float_or_none(pos.get("original_quantity")),
                                )
                                _q1 = _blend["q1"]
                                _a1 = _blend["a1"]
                                # INVARIANT-A: stop only TIGHTENS (asserted in the helper).
                                _s1 = _blend["s1"]
                                pos["avg_entry_price"] = _a1
                                pos["quantity"] = _q1
                                # GROW original_quantity so the Ross scale-out de-risks
                                # the ENLARGED position (scale_out_quantity bases its
                                # fraction on original_quantity; the can_split dust guard
                                # there is re-checked at scale time against the new size).
                                pos["original_quantity"] = _blend["original_quantity"]
                                pos["notional_usd"] = _q1 * _a1
                                pos["stop_price"] = _s1
                                stop_px = _s1
                                # Freeze R0 as the #769 risk anchor so the circuit
                                # re-bases to the STARTER's original risk (GUARD #1).
                                if _R0p is not None and _R0p > 0:
                                    le["pyramid_risk_anchor_usd"] = _R0p
                                le["pyramid_add_count"] = int(le.get("pyramid_add_count") or 0) + 1
                                # Book the add's entry fee (mirrors the entry adopt) so
                                # realized PnL nets it at the full exit.
                                _add_fee = _order_total_fees_usd(_pno) or 0.0
                                if _add_fee > 0.0:
                                    le["entry_fee_usd_unbooked"] = (
                                        float(le.get("entry_fee_usd_unbooked") or 0.0) + _add_fee
                                    )
                                le.pop("pyramid_order_id", None)
                                le.pop("pyramid_limit_px", None)
                                le.pop("pyramid_pending_R0", None)
                                le["position"] = pos
                                _commit_le(sess, le)
                                _emit(db, sess, "live_pyramid_add", {
                                    "add_qty": _qa_f, "add_price": _Pa_f,
                                    "q0": _q0p, "a0": _a0p, "q1": _q1, "a1": _a1,
                                    "old_stop": float(le.get("pyramid_prev_stop") or _s1),
                                    "new_stop": _s1, "R0": _R0p,
                                    "rho": float(getattr(settings, "chili_momentum_pyramid_add_risk_fraction", 0.5) or 0.5),
                                    "cushion_r": ((bid - _a0p) * _q0p / _R0p) if (_R0p and _R0p > 0) else None,
                                    "ofi": le.get("pyramid_confirm_ofi"),
                                    "risk_anchor_usd": _R0p,
                                })
                                le.pop("pyramid_prev_stop", None)
                                le.pop("pyramid_confirm_ofi", None)
                            else:
                                # Order terminal with NO fill (rejected / cancelled /
                                # post-only-cross) — clear the in-flight marker so a
                                # future tick may try again. No pos mutation.
                                le.pop("pyramid_order_id", None)
                                le.pop("pyramid_limit_px", None)
                                le.pop("pyramid_pending_R0", None)
                                le.pop("pyramid_prev_stop", None)
                                le.pop("pyramid_confirm_ofi", None)
                                _commit_le(sess, le)
                        # else: still working — leave it in flight, do NOT submit again.

                    # PHASE 2 — TRIGGER a new add (only if none in flight + under cap).
                    # EQUITY-FIRST: gate to equity. Crypto is deferred — its L2/OFI
                    # ring is only partially populated in the scheduler process
                    # (_live_ofi_microprice returns None for many crypto names), so the
                    # confirmation can't be trusted to fire an extra BUY; revisit when
                    # crypto L2 coverage is complete. (project_l2_integration)
                    _is_equity_pyr = not str(sess.symbol or "").upper().endswith("-USD")
                    _max_adds = int(getattr(settings, "chili_momentum_pyramid_max_adds", 1) or 1)
                    if st == STATE_LIVE_TRAILING and not le.get("pyramid_order_id"):
                        # R0 = the STARTER's ORIGINAL structural risk = d0 * q0, where
                        # d0 is the frozen entry stop_distance (the C1b basis) and q0,a0
                        # are the STARTER size/avg. Use original_quantity as q0 so a
                        # post-partial runner still funds the add off the full starter R.
                        _es_p = le.get("entry_sizing") if isinstance(le.get("entry_sizing"), dict) else {}
                        _d0 = _float_or_none(_es_p.get("stop_distance"))
                        if _d0 is None or _d0 <= 0:
                            _d0 = max(0.0, float(avg) - float(pos["stop_price"])) or None
                        _q0_starter = (
                            _float_or_none(pos.get("original_quantity"))
                            or _float_or_none(pos.get("quantity"))
                            or 0.0
                        )
                        _a0_starter = float(pos["avg_entry_price"])
                        # OFI thrust (the confirmation; None for many crypto → fail-closed).
                        _pyr_ofi = None
                        try:
                            from .pipeline import _live_ofi_microprice as _pyr_ofi_fn
                            _pyr_ofi, _ = _pyr_ofi_fn(sess.symbol, db=db)
                        except Exception:
                            _pyr_ofi = None
                        # Anchor the entry-stop reference ONCE so "ratcheted since first
                        # considered" is monotone (the headroom test). Persist it.
                        _entry_stop0 = _float_or_none(le.get("pyramid_entry_stop_ref"))
                        if _entry_stop0 is None:
                            _entry_stop0 = float(pos["stop_price"])
                            le["pyramid_entry_stop_ref"] = _entry_stop0
                            _commit_le(sess, le)
                        # Anti-Ross midday: no add during the equity midday lull
                        # (entry-side parity with the #770 lull de-weight).
                        _lull = False
                        try:
                            from .market_profile import in_midday_lull as _pyr_lull
                            _lull = bool(_pyr_lull(sess.symbol))
                        except Exception:
                            _lull = False
                        # ICEBERG / HIDDEN-SELLER probe (Ross SS101 #038) — EQUITY-ONLY,
                        # ADD-PATH ONLY, fail-OPEN. Read the short-window top-of-book ASK
                        # series (price+size) from iqfeed_depth_snapshots (same source +
                        # window as the OFI/micro-price read) and score refill-vs-advance:
                        # a refilling displayed ask => an absorbing seller => block the add.
                        # None (flag off, crypto, or absent/stale L2) => the add is allowed.
                        _iceberg_score = None
                        _iceberg_thresh = None
                        if _is_equity_pyr and bool(
                            getattr(settings, "chili_momentum_iceberg_add_probe_enabled", True)
                        ):
                            try:
                                from sqlalchemy import text as _ice_sql

                                _ice_win = float(
                                    getattr(settings, "chili_crypto_l2_ofi_window_s", 15.0) or 15.0
                                )
                                _ice_rows = db.execute(
                                    _ice_sql(
                                        "SELECT ask_top, ask_top_size "
                                        "FROM iqfeed_depth_snapshots "
                                        "WHERE symbol = :s AND observed_at > "
                                        "(now() at time zone 'utc') - make_interval(secs => :w) "
                                        "ORDER BY observed_at ASC"
                                    ),
                                    {"s": str(sess.symbol or "").strip().upper(), "w": _ice_win},
                                ).fetchall()
                                _ice_series = [
                                    (float(r[0]), float(r[1]))
                                    for r in _ice_rows
                                    if r[0] is not None and r[1] is not None
                                ]
                                _iceberg_score = iceberg_seller_score(_ice_series)
                                if _iceberg_score is not None:
                                    _iceberg_thresh = float(
                                        getattr(
                                            settings,
                                            "chili_momentum_iceberg_add_refill_ratio",
                                            1.0,
                                        )
                                        or 1.0
                                    )
                            except Exception:
                                # Fail-OPEN: any L2 read/parse error leaves the add unchanged.
                                _iceberg_score = None
                                _iceberg_thresh = None
                        # SHARED pure predicate (one source of truth w/ replay + tests).
                        _decn = pyramid_add_decision(
                            enabled=True,  # outer block already gated on the flag
                            is_equity=_is_equity_pyr,
                            add_count=int(le.get("pyramid_add_count") or 0),
                            max_adds=_max_adds,
                            in_flight=bool(le.get("pyramid_order_id")),
                            a0=_a0_starter,
                            q0=_q0_starter,
                            d0=_d0,
                            bid=float(bid),
                            stop_px=float(stop_px),
                            entry_stop_ref=_entry_stop0,
                            high_water_mark=_float_or_none(pos.get("high_water_mark")),
                            ofi=_pyr_ofi,
                            ofi_threshold=float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25),
                            min_cushion_r=float(getattr(settings, "chili_momentum_pyramid_min_cushion_r", 1.0) or 1.0),
                            midday_lull=_lull,
                            iceberg_score=_iceberg_score,
                            iceberg_threshold=_iceberg_thresh,
                        )
                        _R0 = _decn.get("R0")
                        if _decn.get("fire"):
                            # GUARD #4 ADMISSION — the add is the FIRST post-entry BUY;
                            # it MUST be refused whenever a NEW entry would be refused.
                            # Route through the SAME risk_evaluator admission the
                            # decouple-watching entry path uses (kill-switch, per-broker
                            # + global daily-loss registry, governance inhibit, position
                            # cap, aggregate crypto risk). ABORT THE ADD on refusal —
                            # NEVER the exit (we fall through to the stop-breach below).
                            _adm_ok, _adm_ev = runner_boundary_risk_ok(
                                db, sess, expected_move_bps=_expected_move_bps
                            )
                            if not _adm_ok:
                                _emit(db, sess, "live_pyramid_add_blocked", {
                                    "reason": "risk_admission_refused",
                                    "severity": _adm_ev.get("severity"),
                                    "errors": _adm_ev.get("errors"),
                                })
                            else:
                                # SIZE THE ADD via the SAME machinery (never a hardcoded
                                # share block): add_risk_budget = rho * R0, the SAME
                                # frozen ATR (entry_stop_atr_pct => the same d0), at the
                                # guarded ask; notional ceiling = the equity-relative
                                # per-trade notional cap, liquidity-capped on $-vol.
                                _rho = float(getattr(settings, "chili_momentum_pyramid_add_risk_fraction", 0.5) or 0.5)
                                _add_budget = _rho * _R0
                                _pyr_ask = float(ask) if (ask and float(ask) > 0) else float(bid)
                                _pyr_guard_ask = _pyr_ask * _adaptive_notional_guard_multiplier(
                                    expected_move_bps=_expected_move_bps
                                )
                                _eq_shares = not str(sess.symbol or "").upper().endswith("-USD")
                                _inc = prod.base_increment if prod else (1.0 if _eq_shares else None)
                                _mn = prod.base_min_size if prod else (1.0 if _eq_shares else None)
                                _add_atr_pct = _float_or_none(le.get("entry_stop_atr_pct")) or 0.0
                                _add_ceiling = equity_relative_notional_cap(
                                    policy_float_cap(
                                        caps, "max_notional_per_trade_usd",
                                        settings.chili_momentum_risk_max_notional_per_trade_usd,
                                    ),
                                    normalize_execution_family(sess.execution_family),
                                )
                                try:
                                    from .universe import snapshot_dollar_volumes as _pyr_dvol_fn
                                    _pyr_dvol = (_pyr_dvol_fn([sess.symbol]) or {}).get(
                                        str(sess.symbol or "").strip().upper()
                                    )
                                except Exception:
                                    _pyr_dvol = None
                                _add_ceiling = liquidity_capped_notional(_add_ceiling, _pyr_dvol)
                                _qa, _qa_meta = compute_risk_first_quantity(
                                    entry_price=_pyr_guard_ask,
                                    atr_pct=_add_atr_pct,
                                    max_loss_usd=_add_budget,
                                    max_notional_ceiling_usd=_add_ceiling,
                                    base_increment=_inc,
                                    base_min_size=_mn,
                                    stop_atr_mult=float(params.get("stop_atr_mult") or 0.60),
                                )
                                if not _qa or _qa <= 0:
                                    _emit(db, sess, "live_pyramid_add_blocked", {
                                        "reason": "add_size_zero",
                                        "detail": _qa_meta.get("reason"),
                                        "add_budget_usd": round(_add_budget, 2),
                                    })
                                else:
                                    # SUBMIT a marketable-limit BUY (mirror the entry
                                    # submit). post_only only for crypto-maker — but this
                                    # path is equity-only, so post_only is never set (RH
                                    # adapter has no such kwarg). DAY tif like the entry.
                                    _pyr_limit_str = _fmt_limit_price_buy(_pyr_guard_ask)
                                    _pyr_cid = (
                                        f"chili_ml_pyr_{sess.id}_{uuid.uuid4().hex[:12]}"
                                    )
                                    try:
                                        from .market_profile import market_session_now as _pyr_sess_now
                                        _pyr_ext = _pyr_sess_now(sess.symbol) != "regular"
                                    except Exception:
                                        _pyr_ext = False
                                    _pyr_kwargs = dict(
                                        product_id=product_id,
                                        side="buy",
                                        base_size=_fmt_base_size(_qa),
                                        limit_price=_pyr_limit_str,
                                        client_order_id=_pyr_cid,
                                        extended_hours=_pyr_ext,
                                        time_in_force="gfd",
                                    )
                                    _pyr_res = adapter.place_limit_order_gtc(**_pyr_kwargs) or {}
                                    if _pyr_res.get("ok") and _pyr_res.get("order_id"):
                                        # Stash in-flight state. Mutate pos ONLY on the
                                        # confirmed poll (PHASE 1) — NEVER on submit.
                                        le["pyramid_order_id"] = str(_pyr_res["order_id"])
                                        le["pyramid_limit_px"] = float(_pyr_guard_ask)
                                        le["pyramid_pending_R0"] = float(_R0)
                                        le["pyramid_prev_stop"] = float(stop_px)
                                        le["pyramid_confirm_ofi"] = (
                                            None if _pyr_ofi is None else float(_pyr_ofi)
                                        )
                                        _commit_le(sess, le)
                                        _emit(db, sess, "live_pyramid_add_submitted", {
                                            "order_id": le["pyramid_order_id"],
                                            "client_order_id": _pyr_cid,
                                            "add_qty": float(_qa),
                                            "limit_price": _pyr_limit_str,
                                            "R0": float(_R0), "rho": _rho,
                                            "add_budget_usd": round(_add_budget, 2),
                                            "cushion_r": (
                                                round(_decn["cushion_r"], 3)
                                                if _decn.get("cushion_r") is not None else None
                                            ),
                                            "ofi": (None if _pyr_ofi is None else float(_pyr_ofi)),
                                            "iceberg_score": (
                                                None if _iceberg_score is None
                                                else round(float(_iceberg_score), 4)
                                            ),
                                            "stop_at_submit": float(stop_px),
                                        })
                                    else:
                                        _emit(db, sess, "live_pyramid_add_blocked", {
                                            "reason": "submit_failed",
                                            "error": _pyr_res.get("error"),
                                        })
                except Exception:
                    # Fail-safe: any pyramid error is swallowed so the exit path below
                    # ALWAYS runs. The add never blocks/delays/loosens an exit.
                    _log.debug("[momentum_live] pyramid add block error", exc_info=True)

            # ── MICRO-PULLBACK RE-ENTRY (Ross "scale out into the pop, RE-LOAD on the
            # next micro-pullback dip"). A PARALLEL sub-branch to the #772 pyramid add:
            # OWN predicate, OWN counter (micropullback_reentry_count), OWN kill-switch,
            # OWN in-flight marker (micropullback_reentry_order_id). The #772 add above
            # is byte-identical when this flag is on/off — they share NOTHING but the
            # pyramid_blend_on_fill / pyramid_risk_anchor_usd rails (so the max-loss
            # circuit keeps re-basing to the STARTER R0). EQUITY-FIRST (crypto deferred).
            #
            # ADDITIVE: when chili_momentum_micropullback_reentry_enabled is False the
            # whole block is a no-op (no re-load, no pos mutation, no emit, no broker
            # call). Two phases (resolve-in-flight, trigger-new), both FALL THROUGH so
            # the stop-breach/exit block below ALWAYS runs this tick. The entire block is
            # in a try/except that swallows to the fall-through — a re-load NEVER blocks,
            # delays, or loosens an exit. SESSION-SCOPED 15s frame from _build_micro_bar_df
            # (the momentum_nbbo_spread_tape resampler), NEVER the 5d fetch_ohlcv_df.
            # (docs/DESIGN/MOMENTUM_LANE.md)
            if bool(getattr(settings, "chili_momentum_micropullback_reentry_enabled", True)):
                try:
                    # PHASE 1 — resolve an IN-FLIGHT re-load order (mirror the pyramid
                    # adopt). Blend ONLY on a CONFIRMED fill via the SHARED helper; the
                    # circuit re-bases to the STARTER R0 (pyramid_risk_anchor_usd). While
                    # an order is in flight PHASE 2 cannot submit a second (idempotency).
                    _mpr_oid = le.get("micropullback_reentry_order_id")
                    if _mpr_oid:
                        _mno, _ = adapter.get_order(str(_mpr_oid))
                        if _mno is not None and not _order_open(_mno):
                            _mpr_filled = float(getattr(_mno, "filled_size", 0) or 0)
                            if _mpr_filled > 0:
                                _qa_f = _mpr_filled
                                _Pa_f = float(
                                    getattr(_mno, "average_filled_price", 0) or 0
                                ) or float(le.get("micropullback_reentry_limit_px") or ask)
                                _q0m = float(pos["quantity"])
                                _a0m = float(pos["avg_entry_price"])
                                _R0m = _float_or_none(le.get("micropullback_reentry_pending_R0"))
                                _prev_stop_m = float(pos["stop_price"])
                                _blend_m = pyramid_blend_on_fill(
                                    q0=_q0m, a0=_a0m, qa_f=_qa_f, Pa_f=_Pa_f,
                                    stop_px=_prev_stop_m,
                                    original_quantity=_float_or_none(pos.get("original_quantity")),
                                )
                                _q1m = _blend_m["q1"]
                                _a1m = _blend_m["a1"]
                                _s1m = _blend_m["s1"]  # INVARIANT-A: tighten-only (asserted)
                                pos["avg_entry_price"] = _a1m
                                pos["quantity"] = _q1m
                                pos["original_quantity"] = _blend_m["original_quantity"]
                                pos["notional_usd"] = _q1m * _a1m
                                pos["stop_price"] = _s1m
                                stop_px = _s1m
                                # Re-base the max-loss circuit to the STARTER R0 (GUARD #1),
                                # VERBATIM with the pyramid add — re-loads NEVER inflate the
                                # per-trade loss budget.
                                if _R0m is not None and _R0m > 0:
                                    le["pyramid_risk_anchor_usd"] = _R0m
                                le["micropullback_reentry_count"] = (
                                    int(le.get("micropullback_reentry_count") or 0) + 1
                                )
                                _add_fee_m = _order_total_fees_usd(_mno) or 0.0
                                if _add_fee_m > 0.0:
                                    le["entry_fee_usd_unbooked"] = (
                                        float(le.get("entry_fee_usd_unbooked") or 0.0) + _add_fee_m
                                    )
                                # RATCHET the shelf to THIS re-load's higher-low so the
                                # NEXT re-load must hold above this dip, not the stale
                                # original breakout (a refinement vs a fixed shelf).
                                _mpr_dip = _float_or_none(le.get("micropullback_pending_dip_low"))
                                if _mpr_dip is not None and _mpr_dip > 0:
                                    le["micropullback_last_shelf"] = _mpr_dip
                                from datetime import timezone as _tz_m
                                _cool_s = max(
                                    float(getattr(settings, "chili_momentum_micropullback_reentry_cooldown_seconds", 30.0) or 30.0),
                                    2.0 * float(getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15),
                                )
                                le["micropullback_reentry_cooldown_until_utc"] = (
                                    datetime.now(_tz_m.utc) + timedelta(seconds=_cool_s)
                                ).replace(tzinfo=None).isoformat()
                                le.pop("micropullback_reentry_order_id", None)
                                le.pop("micropullback_reentry_limit_px", None)
                                le.pop("micropullback_reentry_pending_R0", None)
                                le.pop("micropullback_pending_dip_low", None)
                                le["position"] = pos
                                _commit_le(sess, le)
                                _emit(db, sess, "live_micro_pullback_reentry_fill", {
                                    "add_qty": _qa_f, "add_price": _Pa_f,
                                    "q0": _q0m, "a0": _a0m, "q1": _q1m, "a1": _a1m,
                                    "old_stop": float(le.get("micropullback_prev_stop") or _s1m),
                                    "new_stop": _s1m, "R0": _R0m,
                                    "reentry_count": le["micropullback_reentry_count"],
                                    "shelf": le.get("micropullback_last_shelf"),
                                    "confirm_ofi": le.get("micropullback_confirm_ofi"),
                                    "confirm_trade_flow": le.get("micropullback_confirm_trade_flow"),
                                })
                                le.pop("micropullback_prev_stop", None)
                                le.pop("micropullback_confirm_ofi", None)
                                le.pop("micropullback_confirm_trade_flow", None)
                            else:
                                # Terminal with NO fill — clear the in-flight marker (a
                                # future tick may try again). No pos mutation.
                                le.pop("micropullback_reentry_order_id", None)
                                le.pop("micropullback_reentry_limit_px", None)
                                le.pop("micropullback_reentry_pending_R0", None)
                                le.pop("micropullback_pending_dip_low", None)
                                le.pop("micropullback_prev_stop", None)
                                le.pop("micropullback_confirm_ofi", None)
                                le.pop("micropullback_confirm_trade_flow", None)
                                _commit_le(sess, le)

                    # PHASE 2 — TRIGGER a new re-load (only if none in flight + under cap
                    # + cooldown elapsed). EQUITY-FIRST (crypto deferred per the pyramid
                    # _is_equity_pyr gate). Only on a winning runner (STATE_LIVE_TRAILING).
                    _is_equity_mpr = not str(sess.symbol or "").upper().endswith("-USD")
                    _max_reentries = int(
                        getattr(settings, "chili_momentum_micropullback_reentry_max", 3) or 3
                    )
                    if (
                        _is_equity_mpr
                        and st == STATE_LIVE_TRAILING
                        and not le.get("micropullback_reentry_order_id")
                        and not le.get("pyramid_order_id")  # never two adds in flight at once
                    ):
                        _mpr_count = int(le.get("micropullback_reentry_count") or 0)
                        if _mpr_count >= _max_reentries:
                            pass  # cap reached — no re-load (silently, not every tick noise)
                        else:
                            # COOLDOWN (pinned to >= 2*bar_seconds in the fill handler).
                            _cool_ok = True
                            _cool_raw = le.get("micropullback_reentry_cooldown_until_utc")
                            if _cool_raw:
                                try:
                                    _cool_ok = datetime.utcnow() >= datetime.fromisoformat(str(_cool_raw))
                                except (TypeError, ValueError):
                                    _cool_ok = True
                            if not _cool_ok:
                                _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                    "reason": "cooldown", "until": _cool_raw})
                            else:
                                # GUARD #2 cushion (knife defense) — only re-load when the
                                # runner has ALREADY banked >= min_cushion_r * R0 AND the
                                # stop is at/above the starter entry (breakeven+). A falling
                                # knife never banks cushion ⇒ structurally cannot re-load.
                                _es_m = le.get("entry_sizing") if isinstance(le.get("entry_sizing"), dict) else {}
                                _d0m = _float_or_none(_es_m.get("stop_distance"))
                                if _d0m is None or _d0m <= 0:
                                    _d0m = max(0.0, float(avg) - float(pos["stop_price"])) or None
                                _q0s = (
                                    _float_or_none(pos.get("original_quantity"))
                                    or _float_or_none(pos.get("quantity"))
                                    or 0.0
                                )
                                _a0s = float(pos["avg_entry_price"])
                                _R0_m = (float(_d0m) * float(_q0s)) if (_d0m and _q0s) else None
                                _min_cush = float(
                                    getattr(settings, "chili_momentum_pyramid_min_cushion_r", 1.0) or 1.0
                                )
                                _cushion_usd = (float(bid) - _a0s) * float(_q0s)
                                _cushion_banked = bool(
                                    _R0_m is not None and _R0_m > 0
                                    and _cushion_usd >= _min_cush * _R0_m
                                    and float(stop_px) >= _a0s
                                )
                                if not _cushion_banked:
                                    pass  # no cushion -> no re-load (silent; the common case)
                                else:
                                    # Anti-Ross midday lull (parity with the pyramid add).
                                    _lull_m = False
                                    try:
                                        from .market_profile import in_midday_lull as _mpr_lull
                                        _lull_m = bool(_mpr_lull(sess.symbol))
                                    except Exception:
                                        _lull_m = False
                                    # RATCHETING SHELF: max(starter entry, breakout level,
                                    # last re-load's higher-low). Re-load N must hold above
                                    # re-load N-1's dip, never the stale original breakout.
                                    _shelf = _a0s
                                    _bk = _float_or_none(le.get("breakout_level_price"))
                                    if _bk is not None and _bk > _shelf:
                                        _shelf = _bk
                                    _last_shelf = _float_or_none(le.get("micropullback_last_shelf"))
                                    if _last_shelf is not None and _last_shelf > _shelf:
                                        _shelf = _last_shelf
                                    # SESSION-SCOPED 15s micro-bar frame (NOT the 5d frame).
                                    _bar_s_m = int(
                                        getattr(settings, "chili_momentum_micropull_bar_seconds", 15) or 15
                                    )
                                    _df_mpr = _build_micro_bar_df(db, sess.symbol, bar_seconds=_bar_s_m)
                                    from .entry_gates import micro_pullback_reentry_detect
                                    from .candles import bounce_curl_from_df
                                    _det = micro_pullback_reentry_detect(
                                        _df_mpr,
                                        shelf=_shelf,
                                        max_dip_pct=float(
                                            getattr(settings, "chili_momentum_micropullback_reentry_max_dip_pct", 0.04) or 0.04
                                        ),
                                    )
                                    # Per-bar curl-conviction confirm (fail-SAFE to False).
                                    _curl_ok = bounce_curl_from_df(_df_mpr)
                                    if not (_det.get("fire") and _curl_ok):
                                        pass  # no micro-pullback geometry this tick (silent)
                                    elif _lull_m:
                                        _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                            "reason": "midday_lull"})
                                    else:
                                        _emit(db, sess, "live_micro_pullback_detected", {
                                            "bounce_high": _det.get("bounce_high"),
                                            "dip_low": _det.get("dip_low"),
                                            "shelf": _shelf, "curl_ok": _curl_ok,
                                        })
                                        # FLOW GATE — route EVERY re-load through the SAME
                                        # _entry_flow_veto VERBATIM (hard negative-side
                                        # precondition: defer if True — never buy into
                                        # selling, the 06-24 fix), THEN require POSITIVE
                                        # confirmation (ofi & trade_flow turning up). The
                                        # veto fails-OPEN on None; the positive-confirm
                                        # fails-CLOSED on None (an extra BUY needs proof).
                                        _mpr_ofi = None
                                        _mpr_tf = None
                                        try:
                                            from .pipeline import _live_ofi_microprice as _mpr_ofi_fn
                                            from .pipeline import _live_trade_flow as _mpr_tf_fn
                                            _mpr_ofi, _ = _mpr_ofi_fn(sess.symbol, db=db)
                                            _mpr_tf = _mpr_tf_fn(sess.symbol, db=db)
                                            _mpr_ofi = None if _mpr_ofi is None else float(_mpr_ofi)
                                            _mpr_tf = None if _mpr_tf is None else float(_mpr_tf)
                                        except Exception:
                                            _mpr_ofi = _mpr_tf = None
                                        _ofi_floor = float(
                                            getattr(settings, "chili_momentum_micropullback_reentry_ofi_thr", 0.30) or 0.30
                                        )
                                        _tf_floor = float(
                                            getattr(settings, "chili_momentum_micropullback_reentry_trade_flow_thr", 0.20) or 0.20
                                        )
                                        _veto = _entry_flow_veto(_mpr_ofi, _mpr_tf, settings)
                                        _pos_confirm = (
                                            _mpr_ofi is not None and _mpr_tf is not None
                                            and _mpr_ofi >= _ofi_floor and _mpr_tf >= _tf_floor
                                        )
                                        if _veto or not _pos_confirm:
                                            _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                "reason": "flow",
                                                "veto": bool(_veto),
                                                "ofi": _mpr_ofi, "trade_flow": _mpr_tf,
                                                "ofi_floor": _ofi_floor, "tf_floor": _tf_floor,
                                            })
                                        else:
                                            # ADMISSION — route the re-load through the SAME
                                            # risk_evaluator gate a NEW entry uses (kill-
                                            # switch, per-broker + global daily-loss,
                                            # drawdown, position cap, aggregate crypto risk).
                                            # ABORT THE ADD on refusal — NEVER the exit.
                                            _adm_ok_m, _adm_ev_m = runner_boundary_risk_ok(
                                                db, sess, expected_move_bps=_expected_move_bps
                                            )
                                            if not _adm_ok_m:
                                                _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                    "reason": "risk_admission",
                                                    "severity": _adm_ev_m.get("severity"),
                                                    "errors": _adm_ev_m.get("errors"),
                                                })
                                            elif not (_R0_m and _R0_m > 0):
                                                _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                    "reason": "bad_R0"})
                                            else:
                                                # SIZE via the SAME machinery as the pyramid
                                                # add: re-load risk budget = rho_reload * R0,
                                                # at the guarded ask, liquidity-capped on
                                                # $-vol, equity-relative notional ceiling.
                                                _rho_m = float(
                                                    getattr(settings, "chili_momentum_micropullback_reentry_risk_fraction", 0.30) or 0.30
                                                )
                                                _budget_m = _rho_m * _R0_m
                                                _mpr_ask = float(ask) if (ask and float(ask) > 0) else float(bid)
                                                _mpr_guard_ask = _mpr_ask * _adaptive_notional_guard_multiplier(
                                                    expected_move_bps=_expected_move_bps
                                                )
                                                _inc_m = prod.base_increment if prod else 1.0
                                                _mn_m = prod.base_min_size if prod else 1.0
                                                _atr_m = _float_or_none(le.get("entry_stop_atr_pct")) or 0.0
                                                _ceil_m = equity_relative_notional_cap(
                                                    policy_float_cap(
                                                        caps, "max_notional_per_trade_usd",
                                                        settings.chili_momentum_risk_max_notional_per_trade_usd,
                                                    ),
                                                    normalize_execution_family(sess.execution_family),
                                                )
                                                try:
                                                    from .universe import snapshot_dollar_volumes as _mpr_dvol_fn
                                                    _mpr_dvol = (_mpr_dvol_fn([sess.symbol]) or {}).get(
                                                        str(sess.symbol or "").strip().upper()
                                                    )
                                                except Exception:
                                                    _mpr_dvol = None
                                                _ceil_m = liquidity_capped_notional(_ceil_m, _mpr_dvol)
                                                _qa_m, _qa_meta_m = compute_risk_first_quantity(
                                                    entry_price=_mpr_guard_ask,
                                                    atr_pct=_atr_m,
                                                    max_loss_usd=_budget_m,
                                                    max_notional_ceiling_usd=_ceil_m,
                                                    base_increment=_inc_m,
                                                    base_min_size=_mn_m,
                                                    stop_atr_mult=float(params.get("stop_atr_mult") or 0.60),
                                                )
                                                if not _qa_m or _qa_m <= 0:
                                                    _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                        "reason": "size_zero",
                                                        "detail": _qa_meta_m.get("reason"),
                                                        "budget_usd": round(_budget_m, 2),
                                                    })
                                                else:
                                                    _mpr_limit_str = _fmt_limit_price_buy(_mpr_guard_ask)
                                                    _mpr_cid = (
                                                        f"chili_ml_mpr_{sess.id}_{uuid.uuid4().hex[:12]}"
                                                    )
                                                    try:
                                                        from .market_profile import market_session_now as _mpr_sess_now
                                                        _mpr_ext = _mpr_sess_now(sess.symbol) != "regular"
                                                    except Exception:
                                                        _mpr_ext = False
                                                    _mpr_res = adapter.place_limit_order_gtc(
                                                        product_id=product_id,
                                                        side="buy",
                                                        base_size=_fmt_base_size(_qa_m),
                                                        limit_price=_mpr_limit_str,
                                                        client_order_id=_mpr_cid,
                                                        extended_hours=_mpr_ext,
                                                        time_in_force="gfd",
                                                    ) or {}
                                                    if _mpr_res.get("ok") and _mpr_res.get("order_id"):
                                                        le["micropullback_reentry_order_id"] = str(_mpr_res["order_id"])
                                                        le["micropullback_reentry_limit_px"] = float(_mpr_guard_ask)
                                                        le["micropullback_reentry_pending_R0"] = float(_R0_m)
                                                        le["micropullback_prev_stop"] = float(stop_px)
                                                        le["micropullback_pending_dip_low"] = _float_or_none(_det.get("dip_low"))
                                                        le["micropullback_confirm_ofi"] = _mpr_ofi
                                                        le["micropullback_confirm_trade_flow"] = _mpr_tf
                                                        _commit_le(sess, le)
                                                        _emit(db, sess, "live_micro_pullback_reentry_submitted", {
                                                            "order_id": le["micropullback_reentry_order_id"],
                                                            "client_order_id": _mpr_cid,
                                                            "add_qty": float(_qa_m),
                                                            "limit_price": _mpr_limit_str,
                                                            "R0": float(_R0_m), "rho": _rho_m,
                                                            "budget_usd": round(_budget_m, 2),
                                                            "reentry_count": _mpr_count,
                                                            "shelf": _shelf,
                                                            "bounce_high": _det.get("bounce_high"),
                                                            "dip_low": _det.get("dip_low"),
                                                            "ofi": _mpr_ofi, "trade_flow": _mpr_tf,
                                                            "stop_at_submit": float(stop_px),
                                                        })
                                                    else:
                                                        _emit(db, sess, "live_micro_pullback_reentry_blocked", {
                                                            "reason": "submit_failed",
                                                            "error": _mpr_res.get("error"),
                                                        })
                except Exception:
                    # Fail-safe: any re-load error is swallowed so the exit path below
                    # ALWAYS runs. The re-load NEVER blocks/delays/loosens an exit.
                    _log.debug("[momentum_live] micro-pullback re-entry block error", exc_info=True)

        if bid > stop_px and le.pop("stop_breach_pending_utc", None) is not None:
            # breach -> recovery between reads = flicker dodged; clear the marker
            # AND the L2 chop-hold counter (the shake-out recovered — exactly the
            # OPG-USD case the anti-shake-out hold is meant to ride out).
            _holds_dodged = le.pop("stop_breach_chop_holds", None)
            _commit_le(sess, le)
            _emit(db, sess, "stop_breach_flicker_dodged", {
                "bid": bid, "stop_price": stop_px, "chop_holds": _holds_dodged})
        if bid <= stop_px:
            # SHAKE-OUT flicker guard (tick-speed exits): one bad bid print can show
            # a breach for a single cached quote; a REAL breakdown persists. Confirm
            # on a SECOND read >=1s apart before selling — the event loop redispatches
            # within ~2s while the breach holds, so a true stop pays at most ~2s of
            # delay; a transient flicker clears the marker on the recovery read.
            _pend_raw = le.get("stop_breach_pending_utc")
            if not _pend_raw:
                le["stop_breach_pending_utc"] = _utcnow().isoformat()
                _commit_le(sess, le)
                _emit(db, sess, "stop_breach_pending_confirm", {"bid": bid, "stop_price": stop_px})
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "stop_pending_confirm": True}
            try:
                _pend_t = datetime.fromisoformat(str(_pend_raw).replace("Z", "+00:00")).replace(tzinfo=None)
                if (_utcnow() - _pend_t).total_seconds() < 1.0:
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state, "stop_pending_confirm": True}
            except (TypeError, ValueError):
                _pend_t = None  # unparseable marker — treat as confirmed (protective default)

            # ── L2-aware anti-shake-out (LOSS side) ──────────────────────────
            # The >=1s flicker guard above has confirmed the breach PERSISTS. Before
            # paying the stop, read L2/OFI to separate a real BREAKDOWN (sell now)
            # from a CHOP dip with bids absorbing (the OPG-USD shake-out: stopped at
            # a dip valley, then recovered). BREAKDOWN is vetoed FIRST and stale/
            # missing L2 => BREAKDOWN, so a real breakdown's latency is <= today's;
            # only a CONFIRMED chop earns a hard-bounded hold. INVARIANT A untouched:
            # this delays the SELL execution only — it never moves/loosens the stop.
            # Default OFF = Stage-0 dark logging (classify + emit the A/B
            # counterfactual, always take today's sell path).
            try:
                _l2_thr = float(getattr(settings, "chili_momentum_ofi_threshold", 0.25) or 0.25)
                _l2_max_age = float(getattr(settings, "chili_momentum_stop_l2_confirm_max_age_s", 2.5) or 2.5)
                _l2_min_snaps = int(getattr(settings, "chili_momentum_stop_l2_confirm_min_snaps", 3) or 3)
                _l2_max_ticks = int(getattr(settings, "chili_momentum_stop_l2_confirm_max_ticks", 2) or 2)
                _l2_enabled = bool(getattr(settings, "chili_momentum_stop_l2_confirm_enabled", False))
                from .paper_execution import classify_stop_breach
                from .pipeline import read_ladder_distribution

                _bl = read_ladder_distribution(sess.symbol, db=db)
                _bc = classify_stop_breach(
                    ladder=_bl, ofi_threshold=_l2_thr,
                    max_age_s=_l2_max_age, min_snaps=_l2_min_snaps,
                )
                _holds = int(le.get("stop_breach_chop_holds") or 0)
                try:
                    _held_s = (_utcnow() - _pend_t).total_seconds() if _pend_t else 0.0
                except Exception:
                    _held_s = 0.0
                _within_bounds = (_holds < _l2_max_ticks) and (_held_s < _l2_max_age)
                _do_hold = bool(_l2_enabled and _bc.get("cls") == "CHOP" and _within_bounds)
                _emit(db, sess, "stop_breach_l2_classify", {
                    "bid": bid, "stop_price": stop_px, "cls": _bc.get("cls"),
                    "reason": _bc.get("reason"), "enabled": _l2_enabled,
                    "held_s": round(_held_s, 2), "holds": _holds,
                    "would_hold": bool(_bc.get("cls") == "CHOP" and _within_bounds),
                    "did_hold": _do_hold, "signals": _bc.get("signals"),
                })
                if _do_hold:
                    le["stop_breach_chop_holds"] = _holds + 1
                    # KEEP stop_breach_pending_utc so the wall-clock cap stays anchored
                    _commit_le(sess, le)
                    _emit(db, sess, "stop_breach_chop_hold", {
                        "bid": bid, "stop_price": stop_px, "hold_n": _holds + 1,
                        "max_ticks": _l2_max_ticks, "held_s": round(_held_s, 2),
                    })
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state, "stop_chop_hold": True}
            except Exception:
                pass  # any L2 failure => fall through to today's sell (protective)

            le.pop("stop_breach_chop_holds", None)
            le.pop("stop_breach_pending_utc", None)
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

        # Sell-into-strength: while a resting scale-out limit is working the
        # target, ADOPT its fill instead of firing the reactive market partial.
        # If price blew >2% through the target and the order is somehow still
        # open (stale book state), cancel-adopt and let the reactive path run.
        if (
            st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)
            and not pos.get("partial_taken")
            and le.get("scale_limit_order_id")
        ):
            _sl_oid = str(le["scale_limit_order_id"])
            _no_sl, _ = adapter.get_order(_sl_oid)
            _sl_filled = float(getattr(_no_sl, "filled_size", 0) or 0) if _no_sl is not None else 0.0
            if _no_sl is not None and not _order_open(_no_sl) and _sl_filled > 0:
                _px_f = float(getattr(_no_sl, "average_filled_price", 0) or 0) or float(
                    le.get("scale_limit_px") or target_px
                )
                _already = float(le.get("scale_limit_adopted_qty") or 0.0)
                le.pop("scale_limit_order_id", None)
                _commit_le(sess, le)
                _new_qty = max(0.0, _sl_filled - _already)
                if _new_qty > 0:
                    _scale_out_to_runner(
                        db, sess, le=le, filled_quantity=_new_qty,
                        entry_price=avg, fill_price=_px_f, reason="scale_out_limit",
                    )
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            if bid is not None and bid >= target_px * 1.02 and _no_sl is not None and _order_open(_no_sl):
                _cancel_scale_limit_and_clamp(
                    db, sess, adapter, le=le, requested_qty=0.0, reason="stale_scale_limit"
                )
                # cleared — the reactive path below takes over this pulse

        # First-target (2:1) reached and not yet scaled — take the Ross partial.
        # Fires from ENTERED or from TRAILING (price drifted up past trail-activate
        # before reaching the target); the partial_taken guard ensures it fires once.
        # Skipped while a resting scale-out limit is working the level (above).
        #
        # OR-in the adaptive order-flow EXHAUSTION partial: a crypto runner whose
        # flow exhausted BELOW the fixed target arms `exhaustion_lock_partial_armed`
        # (primary hook). It routes through the SAME audited SCALING_OUT path (which
        # flips _be_floor to breakeven — the MEGA give-back fix). Gated directly on
        # `-USD` (NOT transitively via the flag) so equity is byte-identical, and on
        # the partial flag + the same `not scale_limit_order_id` contract so it never
        # races a resting limit. (docs/DESIGN/ADAPTIVE_OFI_EXIT.md)
        _ofi_partial_armed = bool(
            sess.symbol.endswith("-USD")
            and getattr(settings, "chili_momentum_exit_ofi_lock_partial_enabled", False)
            and le.get("exhaustion_lock_partial_armed")
        )
        if (
            st in (STATE_LIVE_ENTERED, STATE_LIVE_TRAILING)
            and not pos.get("partial_taken")
            and not le.get("scale_limit_order_id")
            and (bid >= target_px * 0.995 or _ofi_partial_armed)
        ):
            _exit_kind = "target" if bid >= target_px * 0.995 else "ofi_exhaustion"
            le.pop("exhaustion_lock_partial_armed", None)
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_SCALING_OUT)
            _emit(db, sess, "live_partial_exit", {
                "bid": bid, "target_price": target_px, "trigger": _exit_kind,
            })
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_SCALING_OUT:
            # Ross asymmetric exit: sell `scale_out_fraction` of the ORIGINAL size
            # into the first (2:1) target, then move the balance stop to breakeven
            # and HOLD the runner (-> TRAILING). A position too small to leave a
            # sellable runner is flattened whole at target (the old flat exit) so we
            # never strand un-sellable dust. (docs/DESIGN/MOMENTUM_LANE.md)
            _eq_shares = not str(sess.symbol or "").upper().endswith("-USD")
            inc = prod.base_increment if prod else (1.0 if _eq_shares else None)
            mn = prod.base_min_size if prod else (1.0 if _eq_shares else None)
            orig_qty = _float_or_none(pos.get("original_quantity")) or qty
            frac = scale_out_fraction(symbol=sess.symbol)
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
