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
from ..governance import is_kill_switch_active
from ..venue.protocol import NormalizedOrder, NormalizedProduct, is_fresh_enough
from .persistence import append_trading_automation_event
from ..decision_ledger import finalize_packet_after_simulated_exit, mark_packet_executed, run_momentum_entry_decision
from ..deployment_ladder_service import record_trade_outcome_metrics
from .risk_evaluator import evaluate_proposed_momentum_automation
from .risk_policy import RISK_SNAPSHOT_KEY
from .paper_execution import regime_atr_pct, stop_target_prices, utc_iso
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
        out.append(tick_live_session(db, int(sess.id), adapter_factory=adapter_factory))
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

    sess = (
        db.query(TradingAutomationSession)
        .filter(
            TradingAutomationSession.id == int(session_id),
            TradingAutomationSession.mode == "live",
        )
        .one_or_none()
    )
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

    def _kill_switch_blocks_live() -> bool:
        pol = snap.get("momentum_risk_policy_summary") or {}
        if not pol.get("disable_live_if_governance_inhibit", True):
            return False
        return is_kill_switch_active()

    def _handle_kill_switch_mid_run() -> None:
        """Safest effort: cancel open entry order; flatten if position recorded."""
        nonlocal le, snap
        if le.get("entry_order_id") and not le.get("position"):
            oid = str(le["entry_order_id"])
            cr = adapter.cancel_order(oid)
            _emit(db, sess, "live_order_cancelled", {"order_id": oid, "raw": cr})
        pos = le.get("position")
        if isinstance(pos, dict) and float(pos.get("quantity") or 0) > 0:
            pid = pos.get("product_id") or sess.symbol
            qty = _fmt_base_size(float(pos["quantity"]))
            cid = f"chili_ml_x_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = adapter.place_market_order(product_id=str(pid), side="sell", base_size=qty, client_order_id=cid)
            le["exit_order_id"] = sr.get("order_id")
            le["exit_client_order_id"] = sr.get("client_order_id")
            le["last_exit_reason"] = "kill_switch_flatten"
            _emit(db, sess, "live_exit_submitted", {"reason": "kill_switch", "result": sr})
            le["position"] = None
        _commit_le(sess, le)

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

    if tick.freshness and not is_fresh_enough(tick.freshness):
        _emit(db, sess, "live_blocked_by_risk", {"reason": "stale_bbo", "age": tick.freshness.age_seconds()})
        if sess.state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE, STATE_WATCHING_LIVE):
            db.flush()
            return {"ok": True, "blocked": True, "reason": "stale_quote"}

    mid = float(tick.mid)
    bid = float(tick.bid or mid)
    ask = float(tick.ask or mid)

    ok_b, ev = runner_boundary_risk_ok(db, sess)
    if not ok_b:
        _emit(
            db,
            sess,
            "live_blocked_by_risk",
            {"severity": ev.get("severity"), "errors": ev.get("errors")},
        )
        if sess.state in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE):
            _safe_transition(db, sess, STATE_LIVE_ERROR)
        elif sess.state == STATE_LIVE_PENDING_ENTRY and le.get("entry_order_id") and not le.get("position"):
            adapter.cancel_order(str(le["entry_order_id"]))
            _safe_transition(db, sess, STATE_WATCHING_LIVE)
        elif sess.state == STATE_LIVE_ENTERED and le.get("position"):
            _handle_kill_switch_mid_run()
            _safe_transition(db, sess, STATE_LIVE_EXITED)
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
        _handle_kill_switch_mid_run()
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
    try:
        max_notional = float(
            caps.get("max_notional_per_trade_usd") or settings.chili_momentum_risk_max_notional_per_trade_usd
        )
    except (TypeError, ValueError):
        max_notional = float(settings.chili_momentum_risk_max_notional_per_trade_usd)
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
        if float(via.viability_score or 0) >= float(params["entry_viability_min"]) and via.live_eligible:
            _safe_transition(db, sess, STATE_LIVE_ENTRY_CANDIDATE)
            _emit(db, sess, "live_entry_candidate_detected", {"viability_score": via.viability_score})
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
                atrp = regime_atr_pct(regime)
                stop_px, target_px = stop_target_prices(
                    avg,
                    atr_pct=atrp,
                    side_long=True,
                    stop_atr_mult=float(params["stop_atr_mult"]),
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
        qty_raw = max_notional / ask
        qty = _round_base_size(qty_raw, inc, mn)
        if qty <= 0:
            _emit(db, sess, "live_error", {"reason": "size_zero_after_rounding"})
            _safe_transition(db, sess, STATE_LIVE_ERROR)
            db.flush()
            return {"ok": False, "error": "size_zero"}

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
            sr = adapter.place_market_order(
                product_id=product_id, side="sell", base_size=_fmt_base_size(qty), client_order_id=cid
            )
            le["exit_order_id"] = sr.get("order_id")
            le["exit_client_order_id"] = sr.get("client_order_id")
            xpx = bid
            if sr.get("ok") and le.get("exit_order_id"):
                nox, _ = adapter.get_order(str(le["exit_order_id"]))
                if nox and nox.average_filled_price:
                    xpx = float(nox.average_filled_price)
            pnl = (xpx - avg) * qty
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            le["realized_pnl_usd"] = float(le.get("realized_pnl_usd") or 0.0) + pnl
            _finalize_live_decision_after_exit(db, sess, le=le, realized_pnl_usd=pnl, slip_bps=slip_live)
            le["last_exit_reason"] = "bailout"
            le["position"] = None
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_EXITED)
            _emit(db, sess, "live_exit_filled", {"reason": "bailout", "pnl_usd": pnl, "sell_result": sr})
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
            sr = adapter.place_market_order(
                product_id=product_id, side="sell", base_size=_fmt_base_size(qty), client_order_id=cid
            )
            le["exit_order_id"] = sr.get("order_id")
            xpx = bid
            if sr.get("ok") and le.get("exit_order_id"):
                nox, _ = adapter.get_order(str(le["exit_order_id"]))
                if nox and nox.average_filled_price:
                    xpx = float(nox.average_filled_price)
            pnl = (xpx - avg) * qty
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            le["realized_pnl_usd"] = float(le.get("realized_pnl_usd") or 0.0) + pnl
            _finalize_live_decision_after_exit(db, sess, le=le, realized_pnl_usd=pnl, slip_bps=slip_live)
            le["last_exit_reason"] = "max_hold"
            le["position"] = None
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_EXITED)
            _emit(db, sess, "live_exit_filled", {"reason": "max_hold", "pnl_usd": pnl})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if bid <= stop_px:
            cid = f"chili_ml_s_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = adapter.place_market_order(
                product_id=product_id, side="sell", base_size=_fmt_base_size(qty), client_order_id=cid
            )
            le["exit_order_id"] = sr.get("order_id")
            xpx = bid
            if sr.get("ok") and le.get("exit_order_id"):
                nox, _ = adapter.get_order(str(le["exit_order_id"]))
                if nox and nox.average_filled_price:
                    xpx = float(nox.average_filled_price)
            pnl = (xpx - avg) * qty
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            le["realized_pnl_usd"] = float(le.get("realized_pnl_usd") or 0.0) + pnl
            _finalize_live_decision_after_exit(db, sess, le=le, realized_pnl_usd=pnl, slip_bps=slip_live)
            le["last_exit_reason"] = "stop"
            le["position"] = None
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_EXITED)
            _emit(db, sess, "live_exit_filled", {"reason": "stop", "pnl_usd": pnl})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_ENTERED and bid >= target_px * 0.995:
            _safe_transition(db, sess, STATE_LIVE_SCALING_OUT)
            _emit(db, sess, "live_partial_exit", {"bid": bid})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        if st == STATE_LIVE_SCALING_OUT:
            cid = f"chili_ml_p_{sess.id}_{uuid.uuid4().hex[:12]}"
            sr = adapter.place_market_order(
                product_id=product_id, side="sell", base_size=_fmt_base_size(qty), client_order_id=cid
            )
            xpx = bid
            if sr.get("ok") and sr.get("order_id"):
                nox, _ = adapter.get_order(str(sr["order_id"]))
                if nox and nox.average_filled_price:
                    xpx = float(nox.average_filled_price)
            pnl = (xpx - avg) * qty
            slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
            le["realized_pnl_usd"] = float(le.get("realized_pnl_usd") or 0.0) + pnl
            _finalize_live_decision_after_exit(db, sess, le=le, realized_pnl_usd=pnl, slip_bps=slip_live)
            le["last_exit_reason"] = "target"
            le["position"] = None
            _commit_le(sess, le)
            _safe_transition(db, sess, STATE_LIVE_EXITED)
            _emit(db, sess, "live_exit_filled", {"reason": "target", "pnl_usd": pnl})
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
                sr = adapter.place_market_order(
                    product_id=product_id, side="sell", base_size=_fmt_base_size(qty), client_order_id=cid
                )
                xpx = bid
                if sr.get("ok") and sr.get("order_id"):
                    nox, _ = adapter.get_order(str(sr["order_id"]))
                    if nox and nox.average_filled_price:
                        xpx = float(nox.average_filled_price)
                pnl = (xpx - avg) * qty
                slip_live = float(le.get("entry_slip_bps_ref") or 6.0)
                le["realized_pnl_usd"] = float(le.get("realized_pnl_usd") or 0.0) + pnl
                _finalize_live_decision_after_exit(db, sess, le=le, realized_pnl_usd=pnl, slip_bps=slip_live)
                le["last_exit_reason"] = "trail_stop"
                le["position"] = None
                _commit_le(sess, le)
                _safe_transition(db, sess, STATE_LIVE_EXITED)
                _emit(db, sess, "live_exit_filled", {"reason": "trail_stop", "pnl_usd": pnl})
            db.flush()
            return {"ok": True, "session_id": sess.id, "state": sess.state}

        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    if st == STATE_LIVE_EXITED:
        try:
            cd_sec = int(
                caps.get("cooldown_after_stopout_seconds")
                or settings.chili_momentum_risk_cooldown_after_stopout_seconds
            )
        except (TypeError, ValueError):
            cd_sec = int(settings.chili_momentum_risk_cooldown_after_stopout_seconds)
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
            sess.ended_at = _utcnow()
            _safe_transition(db, sess, STATE_LIVE_FINISHED)
            _emit(db, sess, "live_finished", {"realized_pnl_usd": le.get("realized_pnl_usd")})
        db.flush()
        return {"ok": True, "session_id": sess.id, "state": sess.state}

    db.flush()
    return {"ok": True, "session_id": sess.id, "state": sess.state}
