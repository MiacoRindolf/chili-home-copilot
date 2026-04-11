"""Paper automation runner — batch/tick service (Phase 7).

Risk snapshot contract (do not violate in future phases):
- ``risk_snapshot_json["momentum_risk"]`` and other admission-time keys are frozen audit
  baseline; this module never overwrites them.
- Mutable execution state lives under ``risk_snapshot_json["momentum_paper_execution"]`` only.
- Runner may re-check governance / freshness / policy via ``evaluate_proposed_momentum_automation``;
  on mismatch, emit ``paper_blocked_by_risk`` or ``paper_policy_drift`` and take a safe action
  (stall, error, or exit) — never silently rewrite historical snapshot fields.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import (
    MomentumSymbolViability,
    MomentumStrategyVariant,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)
from ..execution_family_registry import normalize_execution_family, momentum_runner_supports_execution_family
from .persistence import (
    append_trading_automation_event,
    append_trading_automation_simulated_fill,
    variant_for_id,
    build_runtime_snapshot_values,
    default_session_binding,
    upsert_trading_automation_runtime_snapshot,
    upsert_trading_automation_session_binding,
)
from .risk_evaluator import evaluate_proposed_momentum_automation
from .risk_policy import RISK_SNAPSHOT_KEY
from .paper_execution import (
    build_synthetic_quote,
    default_reference_mid,
    long_entry_fill_price,
    long_exit_fill_price,
    regime_atr_pct,
    roundtrip_fee_usd,
    stop_target_prices,
    utc_iso,
)
from .paper_fsm import (
    STATE_BAILOUT,
    STATE_COOLDOWN,
    STATE_ENTERED,
    STATE_ENTRY_CANDIDATE,
    STATE_ERROR,
    STATE_EXITED,
    STATE_FINISHED,
    STATE_PENDING_ENTRY,
    STATE_QUEUED,
    STATE_SCALING_OUT,
    STATE_TRAILING,
    STATE_WATCHING,
    assert_transition,
    is_live_intent_state,
    PAPER_RUNNER_RUNNABLE_STATES,
)
from .session_lifecycle import is_operator_paused
from .strategy_params import normalize_strategy_params

_log = logging.getLogger(__name__)

KEY_PAPER_EXEC = "momentum_paper_execution"

QuoteFn = Callable[[str], dict[str, Any]]


def _utcnow() -> datetime:
    return datetime.utcnow()


def _policy_caps(snap: dict[str, Any]) -> dict[str, Any]:
    caps = snap.get("momentum_policy_caps")
    return caps if isinstance(caps, dict) else {}


def _paper_exec(snap: dict[str, Any]) -> dict[str, Any]:
    pe = snap.get(KEY_PAPER_EXEC)
    return dict(pe) if isinstance(pe, dict) else {}


def _commit_pe(sess: TradingAutomationSession, pe: dict[str, Any]) -> None:
    snap = dict(sess.risk_snapshot_json or {})
    snap[KEY_PAPER_EXEC] = pe
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
        source_node_id="momentum_paper_runner",
    )


def _sync_runtime_snapshot(
    db: Session,
    sess: TradingAutomationSession,
    *,
    via: MomentumSymbolViability | None = None,
) -> None:
    try:
        variant = (
            db.query(MomentumStrategyVariant)
            .filter(MomentumStrategyVariant.id == int(sess.variant_id))
            .one_or_none()
        )
        trade_count = int(
            db.query(TradingAutomationSimulatedFill)
            .filter(TradingAutomationSimulatedFill.session_id == int(sess.id))
            .count()
        )
        values = build_runtime_snapshot_values(
            sess,
            variant=variant,
            viability=via,
            trade_count=trade_count,
        )
        pe = (sess.risk_snapshot_json or {}).get(KEY_PAPER_EXEC) if isinstance(sess.risk_snapshot_json, dict) else {}
        quote_source = pe.get("last_quote_source") if isinstance(pe, dict) else None
        upsert_trading_automation_runtime_snapshot(db, session_id=int(sess.id), values=values)
        upsert_trading_automation_session_binding(
            db,
            session_id=int(sess.id),
            values=default_session_binding(
                venue=sess.venue,
                mode=sess.mode,
                execution_family=sess.execution_family,
                quote_source=quote_source,
            ),
        )
    except Exception:
        _log.debug("paper runtime snapshot sync skipped for session %s", sess.id, exc_info=True)


def _record_sim_fill(
    db: Session,
    sess: TradingAutomationSession,
    *,
    action: str,
    fill_type: str,
    price: float | None,
    quantity: float | None,
    reference_price: float | None = None,
    fees_usd: float | None = None,
    pnl_usd: float | None = None,
    position_state_before: str | None = None,
    position_state_after: str | None = None,
    reason: str | None = None,
    marker_json: Optional[dict[str, Any]] = None,
) -> None:
    try:
        append_trading_automation_simulated_fill(
            db,
            session_id=int(sess.id),
            symbol=sess.symbol,
            lane="simulation",
            action=action,
            fill_type=fill_type,
            side="long",
            quantity=quantity,
            price=price,
            reference_price=reference_price,
            fees_usd=fees_usd,
            pnl_usd=pnl_usd,
            position_state_before=position_state_before,
            position_state_after=position_state_after,
            reason=reason,
            marker_json=marker_json,
        )
    except Exception:
        _log.debug("paper simulated fill audit skipped for session %s", sess.id, exc_info=True)


def _safe_transition(db: Session, sess: TradingAutomationSession, new_state: str) -> None:
    old = sess.state
    if old == new_state:
        return
    assert_transition(old, new_state)
    sess.state = new_state
    sess.updated_at = _utcnow()
    from .feedback_emit import emit_feedback_after_terminal_transition
    from .outcome_extract import session_terminal_for_feedback

    if session_terminal_for_feedback(sess.mode or "paper", new_state):
        emit_feedback_after_terminal_transition(db, sess)


def runner_boundary_risk_ok(
    db: Session,
    sess: TradingAutomationSession,
) -> tuple[bool, dict[str, Any]]:
    """Re-check policy at tick boundary; does not mutate snapshot."""
    if sess.user_id is None:
        return False, {"reason": "no_user"}
    ev = evaluate_proposed_momentum_automation(
        db,
        user_id=int(sess.user_id),
        symbol=sess.symbol,
        variant_id=int(sess.variant_id),
        mode="paper",
        execution_family=normalize_execution_family(sess.execution_family),
        exclude_session_id=int(sess.id),
    )
    return bool(ev.get("allowed", False)), ev


def _default_quote_fn(symbol: str) -> dict[str, Any]:
    try:
        from ..market_data import fetch_quote

        q = fetch_quote(symbol)
    except Exception as ex:
        _log.debug("paper_runner quote fetch failed %s: %s", symbol, ex)
        return {}
    if not q:
        return {}
    mid = q.get("price")
    try:
        mf = float(mid) if mid is not None else 0.0
    except (TypeError, ValueError):
        mf = 0.0
    if mf <= 0:
        return {}
    return {"mid": mf, "bid": q.get("bid"), "ask": q.get("ask"), "source": "fetch_quote"}


def _resolve_quote(
    symbol: str,
    spread_bps: float,
    quote_fn: Optional[QuoteFn],
) -> tuple[float, float, float, str]:
    fn = quote_fn or _default_quote_fn
    raw = fn(symbol) or {}
    mid = raw.get("mid")
    try:
        mid_f = float(mid) if mid is not None else 0.0
    except (TypeError, ValueError):
        mid_f = 0.0
    bid_r = raw.get("bid")
    ask_r = raw.get("ask")
    try:
        bid_f = float(bid_r) if bid_r is not None else 0.0
        ask_f = float(ask_r) if ask_r is not None else 0.0
    except (TypeError, ValueError):
        bid_f = ask_f = 0.0
    if mid_f > 0 and bid_f > 0 and ask_f > 0:
        return bid_f, ask_f, mid_f, str(raw.get("source") or "quote")
    syn = build_synthetic_quote(mid_f if mid_f > 0 else 100.0, spread_bps, source="synthetic_spread")
    return syn.bid, syn.ask, syn.mid, syn.source


def list_runnable_paper_sessions(db: Session, *, limit: int = 25) -> list[TradingAutomationSession]:
    lim = max(1, min(int(limit), 200))
    rows = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.mode == "paper",
            TradingAutomationSession.state.in_(PAPER_RUNNER_RUNNABLE_STATES),
        )
        .order_by(TradingAutomationSession.updated_at.asc())
        .limit(lim)
        .all()
    )
    return [r for r in rows if not is_live_intent_state(r.state) and not is_operator_paused(r.risk_snapshot_json)]


def run_paper_runner_batch(
    db: Session,
    *,
    limit: int = 25,
    quote_fn: Optional[QuoteFn] = None,
) -> list[dict[str, Any]]:
    """Scheduler/worker entry: tick several paper sessions."""
    out: list[dict[str, Any]] = []
    for sess in list_runnable_paper_sessions(db, limit=limit):
        out.append(tick_paper_session(db, int(sess.id), quote_fn=quote_fn))
    return out


def tick_paper_session(
    db: Session,
    session_id: int,
    *,
    quote_fn: Optional[QuoteFn] = None,
) -> dict[str, Any]:
    """Advance one paper automation session by one step."""
    if not settings.chili_momentum_paper_runner_enabled:
        return {"ok": True, "skipped": "paper_runner_disabled"}

    sess = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.id == int(session_id),
            TradingAutomationSession.mode == "paper",
        )
        .one_or_none()
    )
    if sess is None:
        return {"ok": False, "error": "not_found"}
    if is_live_intent_state(sess.state):
        return {"ok": True, "skipped": "live_intent_session"}
    if sess.state not in PAPER_RUNNER_RUNNABLE_STATES:
        return {"ok": True, "skipped": "not_runnable", "state": sess.state}
    if is_operator_paused(sess.risk_snapshot_json):
        return {"ok": True, "skipped": "operator_paused", "state": sess.state}

    ef = normalize_execution_family(sess.execution_family)
    if not momentum_runner_supports_execution_family(ef):
        return {"ok": True, "skipped": "execution_family_not_implemented", "execution_family": ef}

    snap = dict(sess.risk_snapshot_json or {})
    if RISK_SNAPSHOT_KEY not in snap:
        _emit(
            db,
            sess,
            "paper_error",
            {"reason": "missing_frozen_risk_snapshot", "hint": "admit_session_without_risk"},
        )
        _safe_transition(db, sess, STATE_ERROR)
        db.flush()
        return {"ok": False, "error": "missing_risk_snapshot"}

    via = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == int(sess.variant_id),
        )
        .one_or_none()
    )
    if not via:
        _emit(db, sess, "paper_error", {"reason": "viability_missing"})
        _safe_transition(db, sess, STATE_ERROR)
        db.flush()
        return {"ok": False, "error": "no_viability"}

    variant = variant_for_id(db, int(sess.variant_id))
    params = normalize_strategy_params(
        variant.params_json if variant is not None else {},
        family_id=variant.family if variant is not None else None,
    )

    ex = via.execution_readiness_json if isinstance(via.execution_readiness_json, dict) else {}
    try:
        spread_bps = float(ex.get("spread_bps") or 8.0)
    except (TypeError, ValueError):
        spread_bps = 8.0
    try:
        slip_bps = float(ex.get("slippage_estimate_bps") or 6.0)
    except (TypeError, ValueError):
        slip_bps = 6.0
    try:
        fee_ratio = float(ex.get("fee_to_target_ratio") or 0.08)
    except (TypeError, ValueError):
        fee_ratio = 0.08

    caps = _policy_caps(snap)
    try:
        cap_max_hold = int(caps.get("max_hold_seconds") or settings.chili_momentum_risk_max_hold_seconds)
    except (TypeError, ValueError):
        cap_max_hold = int(settings.chili_momentum_risk_max_hold_seconds)
    max_hold = min(int(params.get("max_hold_seconds") or cap_max_hold), cap_max_hold)
    try:
        max_notional = float(
            caps.get("max_notional_per_trade_usd") or settings.chili_momentum_risk_max_notional_per_trade_usd
        )
    except (TypeError, ValueError):
        max_notional = float(settings.chili_momentum_risk_max_notional_per_trade_usd)

    raw_quote = (quote_fn or _default_quote_fn)(sess.symbol) or {}
    try:
        qmid = float(raw_quote["mid"]) if raw_quote.get("mid") is not None else None
    except (TypeError, ValueError):
        qmid = None
    ref_mid = default_reference_mid(
        viability_score=float(via.viability_score or 0.0),
        symbol=sess.symbol,
        quote_mid=qmid,
    )
    bid, ask, mid, quote_src = _resolve_quote(sess.symbol, spread_bps, quote_fn)

    ok_boundary, ev = runner_boundary_risk_ok(db, sess)
    if not ok_boundary:
        _emit(
            db,
            sess,
            "paper_blocked_by_risk",
            {
                "severity": ev.get("severity"),
                "errors": ev.get("errors"),
                "evaluated_at_utc": ev.get("evaluated_at_utc"),
            },
        )
        if sess.state == STATE_QUEUED:
            _safe_transition(db, sess, STATE_ERROR)
        elif sess.state == STATE_ENTERED and _paper_exec(snap).get("position"):
            pe = _paper_exec(snap)
            pos = pe.get("position")
            if isinstance(pos, dict):
                entry = float(pos["entry_price"])
                qty = float(pos["quantity"])
                exit_px = long_exit_fill_price(bid, mid, slip_bps)
                pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
                pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
                pe["last_exit_price"] = exit_px
                pe["last_exit_reason"] = "risk_block_forced_exit"
                pe["position"] = None
                _record_sim_fill(
                    db,
                    sess,
                    action="forced_exit",
                    fill_type="exit",
                    price=exit_px,
                    quantity=qty,
                    reference_price=mid,
                    pnl_usd=pnl,
                    position_state_before="long",
                    position_state_after="flat",
                    reason="risk_block",
                    marker_json={"stop": pos.get("stop_price"), "target": pos.get("target_price")},
                )
            pe["last_tick_utc"] = utc_iso()
            _commit_pe(sess, pe)
            _safe_transition(db, sess, STATE_EXITED)
            _emit(
                db,
                sess,
                "paper_exit_filled",
                {"reason": "risk_block", "price": pe.get("last_exit_price")},
            )
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "blocked": True, "risk_evaluation": ev}

    pe = _paper_exec(snap)
    pe["tick_count"] = int(pe.get("tick_count") or 0) + 1
    pe["last_mid"] = mid
    pe["last_quote_source"] = quote_src
    pe["last_tick_utc"] = utc_iso()
    _commit_pe(sess, pe)
    snap = dict(sess.risk_snapshot_json or {})
    pe = _paper_exec(snap)

    st = sess.state

    if st == STATE_QUEUED:
        _safe_transition(db, sess, STATE_WATCHING)
        _emit(db, sess, "paper_runner_started", {"symbol": sess.symbol, "variant_id": sess.variant_id})
        _emit(db, sess, "paper_watch_started", {"mid": mid, "quote_source": quote_src})
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_WATCHING:
        if float(via.viability_score or 0) >= float(params["entry_viability_min"]) and via.paper_eligible:
            _safe_transition(db, sess, STATE_ENTRY_CANDIDATE)
            _emit(
                db,
                sess,
                "paper_entry_candidate_detected",
                {"viability_score": via.viability_score, "mid": mid},
            )
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_ENTRY_CANDIDATE:
        if float(via.viability_score or 0) < float(params["entry_revalidate_floor"]) or not via.paper_eligible:
            _safe_transition(db, sess, STATE_WATCHING)
            _emit(db, sess, "paper_watch_started", {"reason": "candidate_regressed"})
        else:
            _safe_transition(db, sess, STATE_PENDING_ENTRY)
            _emit(db, sess, "paper_entry_submitted", {"mid": mid})
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_PENDING_ENTRY:
        if float(via.viability_score or 0) < float(params["entry_revalidate_floor"]) or not via.paper_eligible:
            _safe_transition(db, sess, STATE_WATCHING)
            _emit(db, sess, "paper_watch_started", {"reason": "entry_aborted"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        entry_px = long_entry_fill_price(ask, mid, slip_bps)
        notional = min(250.0, max_notional)
        qty = notional / entry_px
        regime = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
        atrp = regime_atr_pct(regime)
        stop_px, target_px = stop_target_prices(
            entry_px,
            atr_pct=atrp,
            side_long=True,
            stop_atr_mult=float(params["stop_atr_mult"]),
            target_atr_mult=float(params["target_atr_mult"]),
        )
        fees = roundtrip_fee_usd(notional, fee_ratio)
        opened = _utcnow()
        pe["position"] = {
            "side": "long",
            "entry_price": entry_px,
            "quantity": qty,
            "notional_usd": notional,
            "opened_at_utc": opened.isoformat(),
            "stop_price": stop_px,
            "target_price": target_px,
            "spread_bps": spread_bps,
            "slippage_bps_used": slip_bps,
            "fee_to_target_ratio": fee_ratio,
            "fees_est_usd": fees,
        }
        pe["reference_mid_at_entry"] = ref_mid
        _safe_transition(db, sess, STATE_ENTERED)
        _commit_pe(sess, pe)
        _record_sim_fill(
            db,
            sess,
            action="enter_long",
            fill_type="entry",
            price=entry_px,
            quantity=qty,
            reference_price=ref_mid,
            fees_usd=fees,
            position_state_before="flat",
            position_state_after="long",
            reason="entry_fill",
            marker_json={"entry": entry_px, "stop": stop_px, "target": target_px},
        )
        _emit(
            db,
            sess,
            "paper_entry_filled",
            {
                "entry_price": entry_px,
                "qty": qty,
                "notional_usd": notional,
                "fees_est_usd": fees,
                "stop": stop_px,
                "target": target_px,
            },
        )
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st in (STATE_ENTERED, STATE_SCALING_OUT, STATE_TRAILING, STATE_BAILOUT):
        pos = pe.get("position")
        if not isinstance(pos, dict):
            _safe_transition(db, sess, STATE_ERROR)
            _emit(db, sess, "paper_error", {"reason": "position_missing"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": False, "error": "position_missing"}

        entry = float(pos["entry_price"])
        qty = float(pos["quantity"])
        stop_px = float(pos["stop_price"])
        target_px = float(pos["target_price"])
        exit_px = long_exit_fill_price(bid, mid, slip_bps)
        opened_at = pos.get("opened_at_utc")
        try:
            t0 = datetime.fromisoformat(str(opened_at).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            t0 = _utcnow()
        held = (_utcnow() - t0).total_seconds()
        trail_activate_return = 1.0 + float(params["trail_activate_return_bps"]) / 10_000.0
        trail_floor_return = 1.0 + float(params["trail_floor_return_bps"]) / 10_000.0

        if st == STATE_BAILOUT:
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            pe["last_exit_price"] = exit_px
            pe["last_exit_reason"] = "bailout"
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            _record_sim_fill(
                db,
                sess,
                action="exit_long",
                fill_type="exit",
                price=exit_px,
                quantity=qty,
                reference_price=mid,
                pnl_usd=pnl,
                position_state_before="long",
                position_state_after="flat",
                reason="bailout",
                marker_json={"entry": entry, "stop": stop_px, "target": target_px},
            )
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "bailout"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # bailout: viability collapse
        if float(via.viability_score or 0) < float(params["bailout_viability_floor"]):
            _safe_transition(db, sess, STATE_BAILOUT)
            _emit(db, sess, "paper_bailout", {"viability_score": via.viability_score, "bid": bid})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if held >= max_hold:
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            pe["last_exit_price"] = exit_px
            pe["last_exit_reason"] = "max_hold"
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            _record_sim_fill(
                db,
                sess,
                action="exit_long",
                fill_type="exit",
                price=exit_px,
                quantity=qty,
                reference_price=mid,
                pnl_usd=pnl,
                position_state_before="long",
                position_state_after="flat",
                reason="max_hold",
                marker_json={"entry": entry, "stop": stop_px, "target": target_px},
            )
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "max_hold"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if exit_px <= stop_px:
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            pe["last_exit_price"] = exit_px
            pe["last_exit_reason"] = "stop"
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            _record_sim_fill(
                db,
                sess,
                action="exit_long",
                fill_type="exit",
                price=exit_px,
                quantity=qty,
                reference_price=mid,
                pnl_usd=pnl,
                position_state_before="long",
                position_state_after="flat",
                reason="stop",
                marker_json={"entry": entry, "stop": stop_px, "target": target_px},
            )
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "stop"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_ENTERED and exit_px >= target_px * 0.995:
            _safe_transition(db, sess, STATE_SCALING_OUT)
            _emit(db, sess, "paper_partial_exit", {"price": exit_px, "note": "target_zone"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_SCALING_OUT:
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            pe["last_exit_price"] = exit_px
            pe["last_exit_reason"] = "target"
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            _record_sim_fill(
                db,
                sess,
                action="exit_long",
                fill_type="exit",
                price=exit_px,
                quantity=qty,
                reference_price=mid,
                pnl_usd=pnl,
                position_state_before="long",
                position_state_after="flat",
                reason="target",
                marker_json={"entry": entry, "stop": stop_px, "target": target_px},
            )
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "target"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_ENTERED and exit_px >= entry * trail_activate_return:
            _safe_transition(db, sess, STATE_TRAILING)
            _emit(db, sess, "paper_runner_started", {"note": "trail_armed", "bid": bid})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_TRAILING:
            trail_stop = max(stop_px, entry * trail_floor_return)
            if exit_px <= trail_stop:
                pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
                pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
                pe["last_exit_price"] = exit_px
                pe["last_exit_reason"] = "trail_stop"
                pe["position"] = None
                _safe_transition(db, sess, STATE_EXITED)
                _commit_pe(sess, pe)
                _record_sim_fill(
                    db,
                    sess,
                    action="exit_long",
                    fill_type="exit",
                    price=exit_px,
                    quantity=qty,
                    reference_price=mid,
                    pnl_usd=pnl,
                    position_state_before="long",
                    position_state_after="flat",
                    reason="trail_stop",
                    marker_json={"entry": entry, "stop": trail_stop, "target": target_px},
                )
                _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "trail_stop"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_EXITED:
        try:
            cd_sec = int(
                caps.get("cooldown_after_stopout_seconds")
                or settings.chili_momentum_risk_cooldown_after_stopout_seconds
            )
        except (TypeError, ValueError):
            cd_sec = int(settings.chili_momentum_risk_cooldown_after_stopout_seconds)
        until = _utcnow() + timedelta(seconds=max(0, cd_sec))
        pe["cooldown_until_utc"] = until.isoformat()
        _safe_transition(db, sess, STATE_COOLDOWN)
        _commit_pe(sess, pe)
        _emit(db, sess, "paper_cooldown_started", {"until_utc": pe["cooldown_until_utc"]})
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_COOLDOWN:
        until_raw = pe.get("cooldown_until_utc")
        try:
            until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00")).replace(tzinfo=None)
        except Exception:
            until = _utcnow()
        if _utcnow() >= until:
            sess.ended_at = _utcnow()
            _safe_transition(db, sess, STATE_FINISHED)
            _emit(db, sess, "paper_finished", {"realized_pnl_usd": pe.get("realized_pnl_usd")})
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    _sync_runtime_snapshot(db, sess, via=via)
    db.flush()
    return {"ok": True, "session_id": sess.id, "state": sess.state}


def summarize_paper_execution(snap: Any) -> dict[str, Any]:
    """Read-model helper for API/UI."""
    if not isinstance(snap, dict):
        return {}
    pe = snap.get(KEY_PAPER_EXEC)
    if not isinstance(pe, dict):
        return {}
    pos = pe.get("position")
    out: dict[str, Any] = {
        "tick_count": pe.get("tick_count"),
        "last_tick_utc": pe.get("last_tick_utc"),
        "last_mid": pe.get("last_mid"),
        "last_quote_source": pe.get("last_quote_source"),
        "realized_pnl_usd": pe.get("realized_pnl_usd"),
        "last_exit_reason": pe.get("last_exit_reason"),
        "cooldown_until_utc": pe.get("cooldown_until_utc"),
    }
    if isinstance(pos, dict):
        out["in_position"] = True
        out["entry_price"] = pos.get("entry_price")
        out["quantity"] = pos.get("quantity")
        out["notional_usd"] = pos.get("notional_usd")
        out["stop_price"] = pos.get("stop_price")
        out["target_price"] = pos.get("target_price")
    else:
        out["in_position"] = False
    return out
