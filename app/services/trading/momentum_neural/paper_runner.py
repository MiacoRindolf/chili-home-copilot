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
import math
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import flag_modified

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
from .risk_policy import RISK_SNAPSHOT_KEY, policy_float_cap, policy_int_cap
from ..indicator_core import compute_atr
from .paper_execution import (
    breakeven_stop_after_partial,
    build_synthetic_quote,
    default_reference_mid,
    effective_stop_atr_pct,
    long_entry_fill_price,
    long_exit_fill_price,
    regime_atr_pct,
    roundtrip_fee_usd,
    runner_trail_stop,
    scale_out_fraction,
    scale_out_quantity,
    stop_target_prices,
    structural_or_vol_floored_atr_pct,
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
from ..decision_ledger import (
    finalize_packet_after_simulated_exit,
    mark_packet_executed,
    run_momentum_entry_decision,
)
from ..deployment_ladder_service import record_trade_outcome_metrics
from ..market_data import fetch_ohlcv_df
from .entry_gates import bos_exit_triggered_long, run_paper_entry_gates

_log = logging.getLogger(__name__)

KEY_PAPER_EXEC = "momentum_paper_execution"

QuoteFn = Callable[[str], dict[str, Any]]


def _utcnow() -> datetime:
    return datetime.utcnow()


def _finite_float_or_default(value: Any, default: float) -> float:
    if isinstance(value, bool) or value is None:
        return default
    try:
        out = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    if not math.isfinite(out):
        return default
    return out


def _execution_readiness_costs(execution_readiness: Any) -> tuple[float, float, float]:
    ex = execution_readiness if isinstance(execution_readiness, dict) else {}
    return (
        _finite_float_or_default(ex.get("spread_bps"), 12.0),
        _finite_float_or_default(ex.get("slippage_estimate_bps"), 10.0),
        _finite_float_or_default(ex.get("fee_to_target_ratio"), 0.08),
    )


def _effective_viability(via: MomentumSymbolViability, max_age_sec: float) -> float:
    """Linear decay of score when viability row is stale past half of policy max age."""
    raw = float(via.viability_score or 0.0)
    ft = getattr(via, "freshness_ts", None)
    if ft is None:
        return raw
    try:
        ft_naive = ft.replace(tzinfo=None) if getattr(ft, "tzinfo", None) else ft
        age = (_utcnow() - ft_naive).total_seconds()
    except Exception:
        return raw
    half = max(60.0, float(max_age_sec) / 2.0)
    if age <= half:
        return raw
    decay = min(0.25, (age - half) / max(float(max_age_sec), 1.0) * 0.25)
    return max(0.0, raw - decay)


def _via_entry_paused(via: MomentumSymbolViability) -> bool:
    ex = via.explain_json if isinstance(via.explain_json, dict) else {}
    until_raw = ex.get("variant_symbol_pause_until_utc")
    if not until_raw:
        return False
    try:
        until = datetime.fromisoformat(str(until_raw).replace("Z", "+00:00")).replace(tzinfo=None)
        return _utcnow() < until
    except Exception:
        return False


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
    # Force the JSON column dirty so a second in-tick mutation around an intervening
    # flush is never silently dropped (mirrors live _commit_le; see note there).
    try:
        flag_modified(sess, "risk_snapshot_json")
    except Exception:
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
    decision_packet_id: int | None = None,
) -> None:
    try:
        row = append_trading_automation_simulated_fill(
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
            decision_packet_id=decision_packet_id,
        )
        _record_sim_fill_ledger_safe(
            db,
            sess,
            simulated_fill_id=int(row.id),
            fill_type=fill_type,
            quantity=quantity,
            price=price,
            pnl_usd=pnl_usd,
            position_state_after=position_state_after,
            reason=reason,
            marker_json=marker_json,
            decision_packet_id=decision_packet_id,
        )
    except Exception:
        _log.debug("paper simulated fill audit skipped for session %s", sess.id, exc_info=True)


def _scan_pattern_id_for_session(db: Session, sess: TradingAutomationSession) -> int | None:
    try:
        variant = variant_for_id(db, int(sess.variant_id))
        sid = getattr(variant, "scan_pattern_id", None) if variant is not None else None
        return int(sid) if sid is not None else None
    except Exception:
        return None


def _record_sim_fill_ledger_safe(
    db: Session,
    sess: TradingAutomationSession,
    *,
    simulated_fill_id: int,
    fill_type: str | None,
    quantity: float | None,
    price: float | None,
    pnl_usd: float | None,
    position_state_after: str | None,
    reason: str | None,
    marker_json: Optional[dict[str, Any]],
    decision_packet_id: int | None,
) -> None:
    """Mirror momentum paper fills into the canonical economic ledger."""
    try:
        from .. import economic_ledger as _ledger

        if not _ledger.mode_is_active():
            return
        marker = marker_json if isinstance(marker_json, dict) else {}
        mode = (sess.mode or "paper").lower()
        scan_pattern_id = _scan_pattern_id_for_session(db, sess)
        common = {
            "session_id": int(sess.id),
            "user_id": sess.user_id,
            "scan_pattern_id": scan_pattern_id,
            "ticker": sess.symbol,
            "quantity": float(quantity) if quantity is not None else 0.0,
            "fill_price": float(price) if price is not None else 0.0,
            "venue": sess.venue,
            "mode": mode,
            "decision_packet_id": decision_packet_id,
            "provenance": {
                "runner": "momentum_paper_runner",
                "simulated_fill_id": simulated_fill_id,
                "reason": reason,
            },
        }
        if fill_type == "entry" and position_state_after == "long":
            _ledger.record_automation_session_entry_fill(db, fee=0.0, **common)
            return
        if fill_type != "exit":
            return
        entry = marker.get("entry")
        if entry is None:
            return
        if position_state_after == "long":
            _ledger.record_automation_session_partial_exit_fill(
                db,
                entry_price=float(entry),
                realized_pnl_usd=pnl_usd,
                **common,
            )
            return
        if position_state_after != "flat":
            return
        _ledger.record_automation_session_exit_fill(
            db,
            entry_price=float(entry),
            realized_pnl_usd=pnl_usd,
            **common,
        )
        pe = _paper_exec(dict(sess.risk_snapshot_json or {}))
        cumulative = pe.get("realized_pnl_usd")
        _ledger.reconcile_automation_session(
            db,
            session_id=int(sess.id),
            user_id=sess.user_id,
            scan_pattern_id=scan_pattern_id,
            ticker=sess.symbol,
            legacy_pnl=float(cumulative) if cumulative is not None else pnl_usd,
            mode=mode,
            provenance={
                "runner": "momentum_paper_runner",
                "simulated_fill_id": simulated_fill_id,
                "reason": reason,
            },
        )
    except Exception:
        _log.debug("paper economic ledger automation hook skipped for session %s", sess.id, exc_info=True)


def _record_paper_exit_basis(
    pe: dict[str, Any],
    *,
    quantity: float,
    entry_price: float,
    exit_price: float,
    pnl_usd: float,
    reason: str,
) -> None:
    try:
        qty = float(quantity)
        entry = float(entry_price)
        exit_px = float(exit_price)
        pnl = float(pnl_usd)
    except (TypeError, ValueError):
        return
    notional_basis = abs(entry * qty)
    pe["last_exit_quantity"] = qty
    pe["last_exit_entry_price"] = entry
    pe["last_exit_price"] = exit_px
    pe["last_exit_notional_basis_usd"] = notional_basis
    pe["last_exit_return_bps"] = (pnl / notional_basis) * 10_000.0 if notional_basis > 1e-12 else None
    pe["last_exit_reason"] = reason


def _finalize_paper_decision_after_exit(
    db: Session,
    sess: TradingAutomationSession,
    *,
    pe: dict[str, Any],
    realized_pnl_usd: float,
    slip_bps: float,
) -> None:
    pid = pe.get("last_entry_decision_packet_id")
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
            mode="paper",
            realized_pnl_usd=realized_pnl_usd,
            slippage_bps=slip_bps,
            missed_fill=False,
            partial_fill=False,
            cumulative_session_pnl_usd=float(pe.get("realized_pnl_usd") or 0.0),
        )
    except Exception:
        _log.debug("decision packet finalize skipped session=%s", sess.id, exc_info=True)


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
        try:
            out.append(tick_paper_session(db, int(sess.id), quote_fn=quote_fn))
        except Exception:
            _log.warning("[paper_runner] tick failed session=%s", sess.id, exc_info=True)
            out.append({"ok": False, "session_id": sess.id, "error": "tick_exception"})
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

    try:
        sess = (
            db.query(TradingAutomationSession)
            .filter(
                TradingAutomationSession.id == int(session_id),
                TradingAutomationSession.mode == "paper",
            )
            .with_for_update(nowait=True)
            .one_or_none()
        )
    except Exception:
        return {"ok": True, "skipped": "concurrent_tick"}
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

    spread_bps, slip_bps, fee_ratio = _execution_readiness_costs(via.execution_readiness_json)

    caps = _policy_caps(snap)
    try:
        cap_max_hold = int(caps.get("max_hold_seconds") or settings.chili_momentum_risk_max_hold_seconds)
    except (TypeError, ValueError):
        cap_max_hold = int(settings.chili_momentum_risk_max_hold_seconds)
    max_hold = min(int(params.get("max_hold_seconds") or cap_max_hold), cap_max_hold)
    max_notional = policy_float_cap(
        caps,
        "max_notional_per_trade_usd",
        settings.chili_momentum_risk_max_notional_per_trade_usd,
    )

    try:
        max_age_sec = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
    except (TypeError, ValueError):
        max_age_sec = 600.0

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
                _record_paper_exit_basis(
                    pe,
                    quantity=qty,
                    entry_price=entry,
                    exit_price=exit_px,
                    pnl_usd=pnl,
                    reason="risk_block_forced_exit",
                )
                pe["position"] = None
                dpid = pe.get("last_entry_decision_packet_id")
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
                    decision_packet_id=int(dpid) if dpid else None,
                )
                _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
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
        if _via_entry_paused(via):
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state, "variant_pause": True}
        eff_v = _effective_viability(via, max_age_sec)
        if eff_v >= float(params["entry_viability_min"]) and via.paper_eligible:
            _safe_transition(db, sess, STATE_ENTRY_CANDIDATE)
            _emit(
                db,
                sess,
                "paper_entry_candidate_detected",
                {"viability_score": via.viability_score, "effective_viability": eff_v, "mid": mid},
            )
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_ENTRY_CANDIDATE:
        eff_v = _effective_viability(via, max_age_sec)
        if eff_v < float(params["entry_revalidate_floor"]) or not via.paper_eligible:
            _safe_transition(db, sess, STATE_WATCHING)
            _emit(db, sess, "paper_watch_started", {"reason": "candidate_regressed"})
        else:
            regime_pre = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
            ok_g, reason_g, dbg = run_paper_entry_gates(
                db,
                symbol=sess.symbol,
                variant=variant,
                regime_snapshot=regime_pre,
                family_id=variant.family if variant is not None else None,
            )
            if not ok_g:
                _safe_transition(db, sess, STATE_WATCHING)
                _emit(
                    db,
                    sess,
                    "paper_entry_gates_blocked",
                    {"reason": reason_g, "debug": dbg, "mid": mid},
                )
            else:
                # Stash the structural stop (pullback low) + breakout level from the
                # SHARED trigger so PENDING_ENTRY's stop mirrors live's structural stop
                # (parity). Cleared when the trigger had no structure.
                _pblow = dbg.get("pullback_low")
                if _pblow:
                    pe["structural_stop_price"] = float(_pblow)
                    _pbhigh = dbg.get("pullback_high")
                    if _pbhigh:
                        pe["breakout_level_price"] = float(_pbhigh)
                    else:
                        pe.pop("breakout_level_price", None)
                else:
                    pe.pop("structural_stop_price", None)
                    pe.pop("breakout_level_price", None)
                _commit_pe(sess, pe)
                _safe_transition(db, sess, STATE_PENDING_ENTRY)
                _emit(db, sess, "paper_entry_submitted",
                      {"mid": mid, "structural_stop": pe.get("structural_stop_price")})
        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_PENDING_ENTRY:
        eff_v = _effective_viability(via, max_age_sec)
        if eff_v < float(params["entry_revalidate_floor"]) or not via.paper_eligible:
            _safe_transition(db, sess, STATE_WATCHING)
            _emit(db, sess, "paper_watch_started", {"reason": "entry_aborted"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        regime = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
        entry_px = long_entry_fill_price(ask, mid, slip_bps)
        notional = min(250.0, max_notional)
        decision_packet_id: int | None = None
        if bool(getattr(settings, "brain_enable_decision_ledger", True)):
            dec = run_momentum_entry_decision(
                db,
                session=sess,
                viability=via,
                variant=variant,
                user_id=sess.user_id,
                max_notional_policy=float(max_notional),
                quote_mid=mid,
                spread_bps=spread_bps,
                execution_mode="paper",
                regime_snapshot=regime,
            )
            if not dec.get("proceed"):
                alloc = dec.get("allocation") or {}
                _emit(
                    db,
                    sess,
                    "paper_entry_abstain",
                    {
                        "packet_id": dec.get("packet_id"),
                        "reason": alloc.get("abstain_reason_code"),
                        "detail": alloc.get("abstain_reason_text"),
                    },
                )
                _safe_transition(db, sess, STATE_WATCHING)
                _sync_runtime_snapshot(db, sess, via=via)
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state, "abstained": True}
            decision_packet_id = dec.get("packet_id")
            notional = min(float(dec["allocation"]["recommended_notional"]), max_notional, 250.0)
        if bool(getattr(settings, "brain_decision_packet_required_for_runners", True)) and decision_packet_id is None:
            _emit(db, sess, "paper_error", {"reason": "decision_packet_required_missing"})
            _safe_transition(db, sess, STATE_ERROR)
            db.flush()
            return {"ok": False, "error": "decision_packet_missing"}
        qty = notional / entry_px
        # Stop PARITY with live: vol-floored + 0.15-capped ATR (effective_stop_atr_pct),
        # then the structural pullback stop (take the WIDER) — the SAME chain the live
        # runner uses, NOT the raw regime ATR. The 2:1 target auto-scales off the
        # actual stop distance. docs/DESIGN/MOMENTUM_LANE.md
        _sam = float(params["stop_atr_mult"])
        _regime_atr = regime_atr_pct(regime)
        _em_bps = None
        try:
            _df15 = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
            if _df15 is not None and not getattr(_df15, "empty", True) and len(_df15) >= 5:
                _atr15 = compute_atr(_df15["High"], _df15["Low"], _df15["Close"])
                _last = float(_df15["Close"].iloc[-1])
                _av = float(_atr15.iloc[-1])
                if _last > 0 and _av == _av:  # _av == _av: NaN-guard
                    _em_bps = _av / _last * 10_000.0
        except Exception:
            _em_bps = None
        _eff_atr = effective_stop_atr_pct(
            _regime_atr, _em_bps, stop_atr_mult=_sam,
            vol_floor_mult=float(getattr(settings, "chili_momentum_risk_stop_vol_floor_mult", 0.5) or 0.5),
        )
        _eff_atr, _stop_model = structural_or_vol_floored_atr_pct(
            vol_floored_atr_pct=_eff_atr,
            structural_stop_price=pe.get("structural_stop_price"),
            entry_price=entry_px,
            stop_atr_mult=_sam,
        )
        stop_px, target_px = stop_target_prices(
            entry_px,
            atr_pct=_eff_atr,
            side_long=True,
            stop_atr_mult=_sam,
            target_atr_mult=float(params["target_atr_mult"]),
        )
        fees = roundtrip_fee_usd(notional, fee_ratio, entry=entry_px, target=target_px)
        opened = _utcnow()
        pe["position"] = {
            "side": "long",
            "entry_price": entry_px,
            "quantity": qty,
            "original_quantity": qty,
            "notional_usd": notional,
            "opened_at_utc": opened.isoformat(),
            "stop_price": stop_px,
            "target_price": target_px,
            "spread_bps": spread_bps,
            "slippage_bps_used": slip_bps,
            "fee_to_target_ratio": fee_ratio,
            "fees_est_usd": fees,
            # Ross asymmetric exit: freeze the entry ATR-pct (the runner trail rides
            # the same ATR distance the initial stop used) + seed the high-water mark.
            # = the effective stop ATR-pct from the live-parity stop chain above
            # (matches the live runner's entry_stop_atr_pct).
            "entry_atr_pct": _eff_atr,
            "high_water_mark": entry_px,
        }
        pe["entry_regime_snapshot_json"] = dict(regime)
        pe["reference_mid_at_entry"] = ref_mid
        pe["entry_quote_source"] = quote_src
        pe["last_entry_decision_packet_id"] = decision_packet_id
        _safe_transition(db, sess, STATE_ENTERED)
        _commit_pe(sess, pe)
        if decision_packet_id:
            try:
                mark_packet_executed(db, int(decision_packet_id))
            except Exception:
                _log.debug("mark_packet_executed skipped session=%s", sess.id, exc_info=True)
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
            decision_packet_id=decision_packet_id,
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
        regime_live = via.regime_snapshot_json if isinstance(via.regime_snapshot_json, dict) else {}
        atrp = regime_atr_pct(regime_live)
        mult_trail = 1.0 + min(0.5, max(-0.2, (atrp - 0.015) / 0.03))
        base_act = float(params["trail_activate_return_bps"]) * mult_trail
        trail_activate_return = 1.0 + base_act / 10_000.0

        # Ross runner: track the high-water mark (peak bid) for the chandelier trail.
        _hwm_prev = pos.get("high_water_mark")
        try:
            _hwm_prev_f = float(_hwm_prev) if _hwm_prev is not None else entry
        except (TypeError, ValueError):
            _hwm_prev_f = entry
        _hwm = max(_hwm_prev_f, float(bid))
        if _hwm_prev is None or _hwm > _hwm_prev_f:
            pos["high_water_mark"] = _hwm
            pe["position"] = pos
            _commit_pe(sess, pe)

        if st == STATE_BAILOUT:
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            _record_paper_exit_basis(
                pe,
                quantity=qty,
                entry_price=entry,
                exit_price=exit_px,
                pnl_usd=pnl,
                reason="bailout",
            )
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            dpid = pe.get("last_entry_decision_packet_id")
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
                decision_packet_id=int(dpid) if dpid else None,
            )
            _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "bailout"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # Time-based stop tightening (no meaningful progress toward target)
        progress_mid = entry + 0.25 * (target_px - entry)
        progress_ok = mid >= progress_mid
        if held >= 0.5 * max_hold and not progress_ok:
            new_s = max(float(pos["stop_price"]), entry)
            if new_s > float(pos["stop_price"]):
                pos["stop_price"] = new_s
        if held >= 0.75 * max_hold and not progress_ok:
            new_s = max(float(pos["stop_price"]), entry * 1.0015)
            if new_s > float(pos["stop_price"]):
                pos["stop_price"] = new_s
        stop_px = float(pos["stop_price"])
        pe["position"] = pos
        _commit_pe(sess, pe)

        # Break of structure (last closed bar vs swing low)
        try:
            df_bos = fetch_ohlcv_df(sess.symbol, interval="15m", period="5d")
            if df_bos is not None and not df_bos.empty:
                last_close = float(df_bos["Close"].astype(float).iloc[-1])
                if bos_exit_triggered_long(df_bos, current_close=last_close):
                    pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
                    pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
                    _record_paper_exit_basis(
                        pe,
                        quantity=qty,
                        entry_price=entry,
                        exit_price=exit_px,
                        pnl_usd=pnl,
                        reason="bos",
                    )
                    pe["position"] = None
                    _safe_transition(db, sess, STATE_EXITED)
                    _commit_pe(sess, pe)
                    dpid = pe.get("last_entry_decision_packet_id")
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
                        reason="bos",
                        marker_json={"entry": entry, "stop": stop_px, "target": target_px},
                        decision_packet_id=int(dpid) if dpid else None,
                    )
                    _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
                    _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "bos"})
                    _sync_runtime_snapshot(db, sess, via=via)
                    db.flush()
                    return {"ok": True, "session_id": sess.id, "state": sess.state}
        except Exception:
            _log.debug("paper_runner BOS path skipped session=%s", sess.id, exc_info=True)

        # bailout: viability collapse
        eff_bail = _effective_viability(via, max_age_sec)
        if eff_bail < float(params["bailout_viability_floor"]):
            _safe_transition(db, sess, STATE_BAILOUT)
            _emit(
                db,
                sess,
                "paper_bailout",
                {"viability_score": via.viability_score, "effective_viability": eff_bail, "bid": bid},
            )
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if held >= max_hold:
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            _record_paper_exit_basis(
                pe,
                quantity=qty,
                entry_price=entry,
                exit_price=exit_px,
                pnl_usd=pnl,
                reason="max_hold",
            )
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            dpid = pe.get("last_entry_decision_packet_id")
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
                decision_packet_id=int(dpid) if dpid else None,
            )
            _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": "max_hold"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # Ross runner trail: in TRAILING, ratchet the stop UP to a chandelier off the
        # high-water mark (same ATR distance the initial stop used), floored at
        # breakeven once the first-target partial de-risked the runner. The stop check
        # below enforces it SAME tick. Derived from the frozen entry ATR — not a
        # static floor. (docs/DESIGN/MOMENTUM_LANE.md)
        if st == STATE_TRAILING:
            _atr_pct_trail = pos.get("entry_atr_pct")
            try:
                _atr_pct_trail = float(_atr_pct_trail) if _atr_pct_trail is not None else atrp
            except (TypeError, ValueError):
                _atr_pct_trail = atrp
            _be_floor = entry if pos.get("partial_taken") else stop_px
            _trailed = runner_trail_stop(
                high_water_mark=float(pos.get("high_water_mark") or entry),
                atr_pct=_atr_pct_trail,
                stop_atr_mult=float(params.get("stop_atr_mult") or 0.60),
                breakeven_floor=_be_floor,
                current_stop=stop_px,
                side_long=True,
            )
            if _trailed > stop_px:
                pos["stop_price"] = _trailed
                stop_px = _trailed
                pe["position"] = pos
                _commit_pe(sess, pe)
                _emit(db, sess, "paper_trail_ratchet", {
                    "new_stop": _trailed,
                    "high_water_mark": pos.get("high_water_mark"),
                    "partial_taken": bool(pos.get("partial_taken")),
                })

        if exit_px <= stop_px:
            # A stop hit while TRAILING (or after the first-target partial) IS the
            # runner's trailing stop; before that it's the initial protective stop.
            _stop_reason = "trail_stop" if (st == STATE_TRAILING or pos.get("partial_taken")) else "stop"
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            _record_paper_exit_basis(
                pe,
                quantity=qty,
                entry_price=entry,
                exit_price=exit_px,
                pnl_usd=pnl,
                reason=_stop_reason,
            )
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            dpid = pe.get("last_entry_decision_packet_id")
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
                reason=_stop_reason,
                marker_json={"entry": entry, "stop": stop_px, "target": target_px},
                decision_packet_id=int(dpid) if dpid else None,
            )
            _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
            _emit(db, sess, "paper_exit_filled", {"price": exit_px, "pnl_usd": pnl, "reason": _stop_reason})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        # First-target (2:1) reached and not yet scaled — take the Ross partial.
        # Fires from ENTERED or TRAILING (price drifted up past trail-activate before
        # reaching the target); the partial_taken guard ensures it fires once.
        if (
            st in (STATE_ENTERED, STATE_TRAILING)
            and not pos.get("partial_taken")
            and exit_px >= target_px * 0.995
        ):
            _safe_transition(db, sess, STATE_SCALING_OUT)
            _emit(db, sess, "paper_partial_exit", {"price": exit_px, "note": "target_zone"})
            _sync_runtime_snapshot(db, sess, via=via)
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_SCALING_OUT:
            # Ross asymmetric exit: sell `scale_out_fraction` of the ORIGINAL size into
            # the first (2:1) target, move the balance stop to breakeven, and HOLD the
            # runner (-> TRAILING). A position too small to leave a sellable runner is
            # flattened whole at target (the old flat exit). (docs/DESIGN/MOMENTUM_LANE.md)
            orig_qty = float(pos.get("original_quantity") or qty)
            frac = scale_out_fraction()
            scale_qty, runner_qty, can_split = scale_out_quantity(
                current_qty=qty,
                original_qty=orig_qty,
                fraction=frac,
            )
            if can_split and not pos.get("partial_taken"):
                total_fees = float(pos.get("fees_est_usd") or 0.0)
                fee_part = total_fees * (scale_qty / orig_qty) if orig_qty > 0 else 0.0
                pnl_p = (exit_px - entry) * scale_qty - fee_part
                pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl_p
                pos["fees_est_usd"] = max(0.0, total_fees - fee_part)
                pos["quantity"] = runner_qty
                pos["partial_taken"] = True
                pos["stop_price"] = breakeven_stop_after_partial(entry, float(pos["stop_price"]), side_long=True)
                pos["scaled_out_at_utc"] = utc_iso()
                pos["scale_out_fraction"] = frac
                pe["position"] = pos
                _safe_transition(db, sess, STATE_TRAILING)
                _commit_pe(sess, pe)
                dpid = pe.get("last_entry_decision_packet_id")
                _record_sim_fill(
                    db,
                    sess,
                    action="exit_long",
                    fill_type="exit",
                    price=exit_px,
                    quantity=scale_qty,
                    reference_price=mid,
                    pnl_usd=pnl_p,
                    position_state_before="long",
                    position_state_after="long",
                    reason="scale_out_target",
                    marker_json={"entry": entry, "partial": True, "runner_qty": runner_qty, "breakeven_stop": pos["stop_price"]},
                    decision_packet_id=int(dpid) if dpid else None,
                )
                _emit(
                    db,
                    sess,
                    "paper_scaled_out_to_runner",
                    {"qty": scale_qty, "runner_qty": runner_qty, "breakeven_stop": pos["stop_price"], "pnl_usd": pnl_p},
                )
                _sync_runtime_snapshot(db, sess, via=via)
                db.flush()
                return {"ok": True, "session_id": sess.id, "state": sess.state}
            # Un-splittable (tiny) position: flatten whole at target.
            pnl = (exit_px - entry) * qty - float(pos.get("fees_est_usd") or 0.0)
            pe["realized_pnl_usd"] = float(pe.get("realized_pnl_usd") or 0.0) + pnl
            _record_paper_exit_basis(
                pe,
                quantity=qty,
                entry_price=entry,
                exit_price=exit_px,
                pnl_usd=pnl,
                reason="target",
            )
            pe["position"] = None
            _safe_transition(db, sess, STATE_EXITED)
            _commit_pe(sess, pe)
            dpid = pe.get("last_entry_decision_packet_id")
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
                decision_packet_id=int(dpid) if dpid else None,
            )
            _finalize_paper_decision_after_exit(db, sess, pe=pe, realized_pnl_usd=pnl, slip_bps=slip_bps)
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

        # TRAILING runs the chandelier ratchet above; the shared stop check enforces
        # the trailed stop. No dedicated static-floor trail exit remains.

        _sync_runtime_snapshot(db, sess, via=via)
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_EXITED:
        cd_sec = policy_int_cap(
            caps,
            "cooldown_after_stopout_seconds",
            settings.chili_momentum_risk_cooldown_after_stopout_seconds,
        )
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
            pe.pop("cooldown_until_utc", None)
            pe["trade_cycles"] = int(pe.get("trade_cycles") or 0) + 1
            _commit_pe(sess, pe)
            _safe_transition(db, sess, STATE_WATCHING)
            _emit(db, sess, "paper_recycled", {
                "realized_pnl_usd": pe.get("realized_pnl_usd"),
                "trade_cycles": pe["trade_cycles"],
            })
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
        out["original_quantity"] = pos.get("original_quantity")
        out["notional_usd"] = pos.get("notional_usd")
        out["stop_price"] = pos.get("stop_price")
        out["target_price"] = pos.get("target_price")
        out["high_water_mark"] = pos.get("high_water_mark")
        # Ross asymmetric exit state: first-target partial taken yet + runner info.
        out["partial_taken"] = bool(pos.get("partial_taken"))
        out["scaled_out_at_utc"] = pos.get("scaled_out_at_utc")
        out["scale_out_fraction"] = pos.get("scale_out_fraction")
    else:
        out["in_position"] = False
    return out
