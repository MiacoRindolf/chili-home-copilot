"""Structured outcome extraction from terminal momentum automation sessions (Phase 9)."""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, Optional

from sqlalchemy.orm import Session

from ....models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationEvent,
    TradingAutomationSession,
)
from .live_fsm import (
    LIVE_LEDGER_TERMINAL_STATES,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_ERROR,
    STATE_LIVE_FINISHED,
)
from .outcome_labels import (
OUTCOME_ARCHIVED,
OUTCOME_BAILOUT,
OUTCOME_CANCELLED_IN_TRADE,
OUTCOME_CANCELLED_PRE_ENTRY,
OUTCOME_ERROR_EXIT,
    OUTCOME_EXPIRED_PRE_RUN,
    OUTCOME_FLAT_UNKNOWN,
    OUTCOME_GOVERNANCE_EXIT,
    OUTCOME_NO_FILL,
    OUTCOME_RISK_BLOCK,
    OUTCOME_SMALL_WIN,
    OUTCOME_STALE_DATA_ABORT,
    OUTCOME_STOP_LOSS,
    OUTCOME_SUCCESS,
    OUTCOME_TIMED_EXIT,
)
from .paper_fsm import (
    STATE_ARCHIVED,
    STATE_CANCELLED,
    STATE_ERROR,
    STATE_EXPIRED,
    STATE_FINISHED,
)
from .risk_policy import RISK_SNAPSHOT_KEY

KEY_PAPER = "momentum_paper_execution"
KEY_LIVE = "momentum_live_execution"

# Deployed code build stamped onto every outcome so expectancy can be segmented by version
# (daily fixes make a pooled cross-build PF invalid for sizing-scaling). Read once at import
# from the deploy-pinned image tag; None when unset (back-compat / non-exec writers).
# (feedback_sizing_expectancy_code_drift)
_CODE_VERSION: Optional[str] = (
    os.getenv("CHILI_MOMENTUM_EXEC_IMAGE")
    or os.getenv("CHILI_CODE_VERSION")
    or os.getenv("CHILI_IMAGE_TAG")
    or None
)

_NON_STRATEGY_CREDIT_OUTCOMES = frozenset(
    {
        OUTCOME_ARCHIVED,
        OUTCOME_CANCELLED_IN_TRADE,
        OUTCOME_CANCELLED_PRE_ENTRY,
        OUTCOME_ERROR_EXIT,
        OUTCOME_EXPIRED_PRE_RUN,
        OUTCOME_GOVERNANCE_EXIT,
        OUTCOME_NO_FILL,
        OUTCOME_RISK_BLOCK,
        OUTCOME_STALE_DATA_ABORT,
    }
)


def _utc(d: Optional[datetime]) -> Optional[datetime]:
    if d is None:
        return None
    if d.tzinfo is not None:
        return d.replace(tzinfo=None)
    return d


def _positive_float(value: Any) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return 0.0
    return out if out > 0 else 0.0


def session_terminal_for_feedback(mode: str, state: str) -> bool:
    """Whether this (mode, state) should emit a durable feedback row (at most once).

    LIVE reads ``live_fsm.LIVE_LEDGER_TERMINAL_STATES`` — the LEDGER set, not the
    RUNNER set. Before 2026-09-02 this listed the three runner-terminal states
    literally and therefore omitted ``live_arm_expired``, which is terminal in the
    ledger but never reached by a runner transition. Consequence, measured over
    2026-08-12..2026-09-02: 291 ``live_arm_expired`` sessions, 0 outcome rows, and
    the 17 among them that had taken a real Alpaca fill (−$916.26 of realised loss,
    including MOVE 19244 at −$364.14 and SSM 19315 which reached +3.20R before
    giving it all back) were invisible to every study built on the outcomes table.
    """
    m = (mode or "").lower()
    st = state or ""
    if m == "paper":
        return st in (STATE_FINISHED, STATE_CANCELLED, STATE_ERROR, STATE_EXPIRED, STATE_ARCHIVED)
    if m == "live":
        return st in LIVE_LEDGER_TERMINAL_STATES or st in (STATE_EXPIRED, STATE_ARCHIVED)
    return False


def load_recent_automation_events(db: Session, session_id: int, *, limit: int = 40) -> list[TradingAutomationEvent]:
    return (
        db.query(TradingAutomationEvent)
        .filter(TradingAutomationEvent.session_id == int(session_id))
        .order_by(TradingAutomationEvent.ts.desc())
        .limit(max(1, min(limit, 200)))
        .all()
    )


def _entry_occurred_from_events(events: list[TradingAutomationEvent], mode: str) -> bool:
    if mode == "paper":
        markers = {"paper_entry_filled", "paper_exit_filled", "paper_partial_exit"}
    else:
        markers = {
            "live_entry_filled",
            "live_exit_filled",
            "live_exit_submitted",
            "live_partial_exit",
            "live_partial_exit_filled",
        }
    for ev in events:
        if ev.event_type in markers:
            return True
    return False


def _entry_occurred_durable(exec_dict: Any) -> bool:
    """Durable proof a real entry FILL happened, independent of event-window aging
    or position-dict zeroing.

    The event-based and live-position-quantity signals are both TRANSIENT: a
    long-held session's ``*_entry_filled`` event can age out of the recent-events
    window, and the broker-zero-reconcile exit path zeroes ``exec["position"]``.
    Either alone makes a real round-trip read as "never entered" and mislabels it
    ``cancelled_pre_entry`` (EIGEN sessions 57/64). A realized P&L or a recorded
    exit-entry price cannot exist without a real entry fill, so they are durable
    entry evidence that survives both. Submission markers are intentionally NOT
    used — a zero-fill submission must stay non-entered.
    """
    if not isinstance(exec_dict, dict):
        return False
    if exec_dict.get("realized_pnl_usd") is not None:
        return True
    if exec_dict.get("last_exit_entry_price") is not None:
        return True
    # A CLOSED CYCLE cannot exist without a real entry fill — the runner appends one
    # only after an exit fills. It also survives the recycle entry-state reset that
    # clears entry_order_id / position, which is exactly when the other two proofs
    # can be missing. Ten sessions in 2026-08-12..2026-09-02 completed a full round
    # trip and were recycled back to watching_live (19315 SSM's +3.20R → −1.60R
    # among them); this is what keeps that readable after the reset.
    if closed_cycles(exec_dict):
        return True
    return False


def closed_cycles(exec_dict: Any) -> list[dict[str, Any]]:
    """The session's FSM-closed round trips, oldest first. Never raises.

    ``momentum_automation_outcomes`` has ``UNIQUE(session_id)``, so one session can
    hold exactly one outcome row — while this runner trades many cycles per session.
    CANF 19471 ran two round trips under one id on 2026-09-02 and the second
    (−$108.85, broker orders f3ed508d / ddba3ed2) had nowhere in the schema to go.
    The runner appends each closed cycle here at recycle time; this reader is what
    makes that leg-level history usable to the extractor and to the integrity check,
    which compares the cycle count against the broker's own episode count.
    """
    if not isinstance(exec_dict, dict):
        return []
    raw = exec_dict.get("closed_cycles")
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, dict)]


def closed_cycle_summary(exec_dict: Any) -> dict[str, Any]:
    """Leg-level history for the outcome row's extracted summary.

    ``realized_pnl_usd_cumulative`` is CUMULATIVE across the session's closed cycles
    — that is how the runner itself reads it for the symbol-day brake — so the
    per-cycle P&L is the successive difference and the total is the LAST value, never
    the sum. Summing would double-count every cycle after the first.
    """
    cycles = closed_cycles(exec_dict)
    if not cycles:
        return {"count": 0}
    per_cycle: list[dict[str, Any]] = []
    prev = 0.0
    for c in cycles:
        cum = c.get("realized_pnl_usd_cumulative")
        try:
            cum_f = float(cum) if cum is not None else None
        except (TypeError, ValueError):
            cum_f = None
        per_cycle.append({
            "cycle_index": c.get("cycle_index"),
            "closed_at_utc": c.get("closed_at_utc"),
            "realized_pnl_usd_cumulative": cum_f,
            "realized_pnl_usd_this_cycle": (
                round(cum_f - prev, 6) if cum_f is not None else None
            ),
            "last_exit_reason": c.get("last_exit_reason"),
            "entry_order_id": c.get("entry_order_id"),
        })
        if cum_f is not None:
            prev = cum_f
    return {
        "count": len(cycles),
        "realized_pnl_usd_cumulative_final": prev,
        "cycles": per_cycle,
    }


# Markers proving an entry order REACHED THE BROKER. Deliberately weaker than the
# entry-occurred predicates above: a submission is not a fill. Their whole purpose is
# to tell "we know nothing happened" apart from "we cannot see what happened", which
# are the two cases the ledger used to book identically as cancelled_pre_entry / $0.
_ENTRY_SUBMISSION_EVENTS = frozenset(
    {
        "live_entry_submitted",
        "live_entry_fill_self_healed",
        "alpaca_owner_claim_primary_fill_adopted",
        "operator_state_repair_fill_adopted",
        "live_emergency_exit_unpriced",
        "operator_flatten_executed",
    }
)


def entry_submission_evidence(exec_dict: Any, events: list[TradingAutomationEvent]) -> list[str]:
    """Reasons to believe an entry order reached the broker for this session.

    ⚠️ WHY THIS EXISTS — the failure mode it prevents is WORSE than the one it fixes.
    Nine sessions in 2026-08-12..2026-09-02 took a real Alpaca fill and emitted no
    ``live_entry_filled`` event, wrote no ``momentum_fill_outcomes`` leg, and left no
    ``realized_pnl_usd`` / ``last_exit_entry_price`` in the snapshot — so BOTH
    entry-occurred predicates read False for them (14732 BCCQ −117.76, 14794 ADXN
    +1.73, 16759 RDIB −1.53, 17712 CELU −47.20, 19192 AEHL 0.00, 19244 MOVE −364.14,
    19338 GYGY −26.48, 19398 INBS −16.15, and 17370 BRNX which has an exit event but
    no entry event). Simply making ``live_arm_expired`` bookable would have booked
    those nine as ``cancelled_pre_entry`` with $0 — replacing 9 MISSING rows with 9
    confidently WRONG ones and making the ledger look complete while staying wrong.

    A non-empty result on a row extracted as ``entry_occurred=False`` means the
    extraction is UNVERIFIED, not clean. ``feedback_emit`` refuses to present such a
    row as an authoritative zero: it books it (so the session stops being invisible),
    marks it ``ledger_integrity_v1.status = entry_evidence_unreconciled``, bars it
    from evolution, and says so loudly. Broker truth then settles it via
    ``ledger_integrity`` / ``outcome_reconcile``.
    """
    out: list[str] = []
    ex = exec_dict if isinstance(exec_dict, dict) else {}
    if ex.get("entry_order_id"):
        out.append("entry_order_id")
    if ex.get("entry_submitted"):
        out.append("entry_submitted_flag")
    if ex.get("entry_client_order_id"):
        out.append("entry_client_order_id")
    pos = ex.get("position")
    if isinstance(pos, dict) and pos:
        out.append("position_marker")
    if ex.get("emergency_exit_accounting_pending"):
        out.append("emergency_exit_accounting_pending")
    seen = {ev.event_type for ev in (events or [])}
    for marker in sorted(_ENTRY_SUBMISSION_EVENTS & seen):
        out.append(f"event:{marker}")
    return out


def _governance_context_from_events(events: list[TradingAutomationEvent]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "kill_switch_exit": False,
        "kill_switch_blocked": False,
        "risk_evaluation_block": False,
        "stale_quote_abort": False,
        "policy_drift": False,
    }
    for ev in events:
        et = ev.event_type
        payload = ev.payload_json if isinstance(ev.payload_json, dict) else {}
        reason = str(payload.get("reason") or "")
        if et in ("live_blocked_by_risk", "paper_blocked_by_risk"):
            if "kill_switch" in reason or payload.get("reason") == "kill_switch":
                out["kill_switch_blocked"] = True
            if "stale" in reason:
                out["stale_quote_abort"] = True
            errs = payload.get("errors") or payload.get("risk_evaluation")
            if errs:
                out["risk_evaluation_block"] = True
        if et == "paper_policy_drift":
            out["policy_drift"] = True
        if et == "live_exit_submitted" and payload.get("reason") == "kill_switch":
            out["kill_switch_exit"] = True
        if et == "live_exit_filled" and payload.get("reason") == "kill_switch":
            out["kill_switch_exit"] = True
    return out


def extract_momentum_session_outcome(
    db: Session,
    sess: TradingAutomationSession,
    *,
    variant: Optional[MomentumStrategyVariant] = None,
    events: Optional[list[TradingAutomationEvent]] = None,
) -> dict[str, Any]:
    """Build normalized extraction dict (not yet persisted)."""
    snap = dict(sess.risk_snapshot_json or {})
    pe = snap.get(KEY_PAPER) if isinstance(snap.get(KEY_PAPER), dict) else {}
    le = snap.get(KEY_LIVE) if isinstance(snap.get(KEY_LIVE), dict) else {}

    if events is None:
        events = load_recent_automation_events(db, int(sess.id))

    mode = (sess.mode or "paper").lower()
    gov = _governance_context_from_events(events)

    var = variant
    if var is None and sess.variant_id:
        var = db.query(MomentumStrategyVariant).filter(MomentumStrategyVariant.id == int(sess.variant_id)).one_or_none()

    family = var.family if var else None
    variant_key = var.variant_key if var else None
    version = var.version if var else None

    risk_frozen = snap.get(RISK_SNAPSHOT_KEY) if isinstance(snap.get(RISK_SNAPSHOT_KEY), dict) else {}
    admission_snapshot_json = {
        RISK_SNAPSHOT_KEY: risk_frozen,
        "momentum_risk_policy_summary": snap.get("momentum_risk_policy_summary"),
        "momentum_policy_caps": snap.get("momentum_policy_caps"),
    }

    # Regime / readiness: prefer current viability row at extract time (traceable).
    regime_snapshot_json: dict[str, Any] = {}
    readiness_snapshot_json: dict[str, Any] = {}
    viability_score_at_extract: Optional[float] = None
    paper_eligible_snap: Optional[bool] = None
    live_eligible_snap: Optional[bool] = None
    from ....models.trading import MomentumSymbolViability

    via = (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.symbol == sess.symbol,
            MomentumSymbolViability.variant_id == int(sess.variant_id),
        )
        .one_or_none()
    )
    # Entry regime/features frozen at the entry fill — mode-symmetric: paper stores it on
    # `pe`, live on `le` (2026-06-23: live capture mirrors paper so the meta-label dataset
    # grows from real trades, not just paper). The rich entry-feature vector rides INTO the
    # existing entry_regime_snapshot_json JSONB under ["features"] (no new column/migration).
    # Pre-change sessions lack both keys -> {} (byte-identical to before).
    entry_regime_snapshot_json: dict[str, Any] = {}
    _entry_exec = le if mode == "live" else pe
    if isinstance(_entry_exec.get("entry_regime_snapshot_json"), dict):
        entry_regime_snapshot_json = dict(_entry_exec["entry_regime_snapshot_json"])
    if isinstance(_entry_exec.get("entry_features"), dict):
        entry_regime_snapshot_json["features"] = dict(_entry_exec["entry_features"])

    if via:
        regime_snapshot_json = dict(via.regime_snapshot_json or {})
        readiness_snapshot_json = dict(via.execution_readiness_json or {})
        viability_score_at_extract = float(via.viability_score)
        paper_eligible_snap = bool(via.paper_eligible)
        live_eligible_snap = bool(via.live_eligible)

    admission_viability = risk_frozen.get("viability_at_admission") if isinstance(risk_frozen, dict) else None
    if isinstance(admission_viability, dict):
        admission_snapshot_json["viability_at_admission"] = admission_viability

    terminal_at = _utc(sess.ended_at) or _utc(sess.updated_at) or datetime.utcnow()
    started = _utc(sess.started_at)

    realized: Optional[float] = None
    exit_reason: Optional[str] = None
    partial_exit = False
    entry_occurred = False
    entry_decision_packet_id: Optional[int] = None
    quote_source_at_entry: Optional[str] = None

    if mode == "paper":
        try:
            entry_decision_packet_id = (
                int(pe["last_entry_decision_packet_id"])
                if pe.get("last_entry_decision_packet_id")
                else None
            )
        except (TypeError, ValueError):
            entry_decision_packet_id = None
        realized = pe.get("realized_pnl_usd")
        if realized is not None:
            try:
                realized = float(realized)
            except (TypeError, ValueError):
                realized = None
        exit_reason = pe.get("last_exit_reason")
        if isinstance(exit_reason, str):
            exit_reason = exit_reason.strip() or None
        quote_source_raw = pe.get("entry_quote_source") or pe.get("last_quote_source")
        if quote_source_raw is not None:
            quote_source_at_entry = str(quote_source_raw).strip() or None
        pos = pe.get("position")
        entry_occurred = (
            _entry_occurred_from_events(events, "paper")
            or _entry_occurred_durable(pe)
            or (isinstance(pos, dict) and float(pos.get("quantity") or 0) > 0)
        )
        for ev in events:
            if ev.event_type == "paper_partial_exit":
                partial_exit = True
    else:
        try:
            entry_decision_packet_id = (
                int(le["entry_decision_packet_id"])
                if le.get("entry_decision_packet_id")
                else None
            )
        except (TypeError, ValueError):
            entry_decision_packet_id = None
        realized = le.get("realized_pnl_usd")
        if realized is not None:
            try:
                realized = float(realized)
            except (TypeError, ValueError):
                realized = None
        # BROKER-TRUTH OVERRIDE (2026-06-12 quant pass v2 A1): the runtime
        # self-report is censored by flatten cascades / reconcile paths — the
        # 30d ledger read −$234 while broker truth was +$2,568, and the streak
        # multiplier halved size off the phantom record. When the session's
        # own entry order id matches a closed broker-synced Trade row, the
        # BROKER's realized is the truth. Joined on broker_order_id — never
        # by symbol/time (the operator's manual trades share the account).
        _bt = _broker_truth_realized_for_session(db, sess, le)
        if _bt is not None:
            if realized is None or abs(_bt - (realized or 0.0)) > 0.01:
                realized = _bt
        exit_reason = le.get("last_exit_reason")
        if isinstance(exit_reason, str):
            exit_reason = exit_reason.strip() or None
        pos = le.get("position")
        entry_occurred = (
            _entry_occurred_from_events(events, "live")
            or _entry_occurred_durable(le)
            or (isinstance(pos, dict) and float(pos.get("quantity") or 0) > 0)
        )
        for ev in events:
            if ev.event_type in ("live_partial_exit", "live_partial_exit_filled"):
                partial_exit = True

    hold_seconds: Optional[int] = None
    if started and terminal_at:
        hold_seconds = max(0, int((terminal_at - started).total_seconds()))

    notional_basis = 0.0
    if mode == "paper" and isinstance(pe.get("position"), dict):
        try:
            notional_basis = float(pe["position"].get("notional_usd") or 0)
        except (TypeError, ValueError):
            notional_basis = 0.0
    elif mode == "live" and isinstance(le.get("position"), dict):
        try:
            notional_basis = float(le["position"].get("notional_usd") or 0)
        except (TypeError, ValueError):
            notional_basis = 0.0
    if notional_basis <= 0 and mode == "paper":
        notional_basis = _positive_float(pe.get("last_exit_notional_basis_usd"))
    if notional_basis <= 0 and mode == "live":
        notional_basis = _positive_float(le.get("last_exit_notional_basis_usd"))
    if notional_basis <= 0 and mode == "paper" and isinstance(pe.get("position"), dict):
        posp = pe["position"]
        try:
            ep = float(posp.get("entry_price") or 0)
            q = float(posp.get("quantity") or 0)
            if ep > 0 and q > 0:
                notional_basis = abs(ep * q)
        except (TypeError, ValueError):
            pass

    return_bps: Optional[float] = None
    if realized is not None and notional_basis > 1e-9:
        return_bps = (realized / notional_basis) * 10000.0

    outcome_class = derive_outcome_class(
        mode=mode,
        terminal_state=sess.state,
        entry_occurred=entry_occurred,
        partial_exit=partial_exit,
        realized_pnl_usd=realized,
        return_bps=return_bps,
        exit_reason=exit_reason,
        governance_context=gov,
        events=events,
    )

    extracted = {
        "session_id": int(sess.id),
        "user_id": int(sess.user_id) if sess.user_id is not None else None,
        "variant_id": int(sess.variant_id),
        "variant_family": family,
        "variant_key": variant_key,
        "variant_version": version,
        "symbol": sess.symbol,
        "mode": mode,
        "execution_family": sess.execution_family or "coinbase_spot",
        "terminal_state": sess.state,
        "terminal_at_utc": terminal_at.isoformat(),
        "started_at_utc": started.isoformat() if started else None,
        "hold_seconds": hold_seconds,
        "outcome_class": outcome_class,
        "realized_pnl_usd": realized,
        "return_bps": return_bps,
        "notional_basis_usd": notional_basis if notional_basis > 0 else None,
        "exit_reason": exit_reason,
        "entry_occurred": entry_occurred,
        # Only meaningful when entry_occurred is False: submission-level proof that
        # this "no entry" may be a BLIND SPOT rather than a fact. See
        # entry_submission_evidence() for why booking a $0 row on these is worse
        # than booking nothing.
        "entry_submission_evidence": (
            [] if entry_occurred else entry_submission_evidence(le if mode == "live" else pe, events)
        ),
        # LEG-LEVEL HISTORY THE ROW ITSELF CANNOT HOLD. UNIQUE(session_id) means one
        # session gets one row, but the runner recycles and trades many cycles under
        # one id. Carrying the per-cycle ledger here is what makes a multi-cycle
        # session auditable at all — and it is what ledger_integrity compares against
        # the broker's own episode count, so CANF 19471's second round trip (two
        # broker episodes, one outcome row) is a detectable divergence rather than a
        # silent −$108.85.
        "closed_cycles_v1": closed_cycle_summary(le if mode == "live" else pe),
        "entry_decision_packet_id": entry_decision_packet_id,
        "quote_source_at_entry": quote_source_at_entry,
        "partial_exit_occurred": partial_exit,
        "regime_snapshot_json": regime_snapshot_json,
        "entry_regime_snapshot_json": entry_regime_snapshot_json,
        "exit_regime_snapshot_json": dict(regime_snapshot_json),
        "readiness_snapshot_json": readiness_snapshot_json,
        "admission_snapshot_json": admission_snapshot_json,
        "governance_context_json": gov,
        "viability_score_at_extract": viability_score_at_extract,
        "paper_eligible_snapshot": paper_eligible_snap,
        "live_eligible_snapshot": live_eligible_snap,
        "correlation_id": sess.correlation_id,
        "source_node_id": sess.source_node_id,
    }
    return extracted


# Broker-zero-reconcile appends a provenance suffix to the real exit reason
# (e.g. "trail_stop" -> "trail_stop_broker_zero_reconcile"). Strip it before
# keyword-matching the exit class so a reconciled round-trip is classified by
# what it actually was. The retry-cap variant is the longer suffix; check first.
_RECONCILE_SUFFIXES = (
    "_retry_cap_broker_zero_reconcile",
    "_broker_zero_reconcile",
)

# Genuine completed-exit outcome classes. The live_cancelled reroute only ever
# upgrades to one of these; anything ambiguous stays cancelled_in_trade.
_REAL_EXIT_OUTCOMES = frozenset(
    {
        OUTCOME_STOP_LOSS,
        OUTCOME_BAILOUT,
        OUTCOME_TIMED_EXIT,
        OUTCOME_SUCCESS,
        OUTCOME_SMALL_WIN,
        OUTCOME_GOVERNANCE_EXIT,
    }
)


def _broker_truth_realized_for_session(db, sess, le: dict) -> Optional[float]:
    """Realized PnL from the broker-synced Trade row whose broker_order_id
    matches THIS session's entry order — None when no confident match.
    Fail-open (None) on any error: the self-report remains the fallback."""
    oid = le.get("entry_order_id")
    if not oid:
        return None
    try:
        from sqlalchemy import text as _text

        row = db.execute(
            _text(
                "SELECT pnl FROM trading_trades "
                "WHERE broker_order_id = :oid AND status = 'closed' "
                "AND pnl IS NOT NULL ORDER BY exit_date DESC LIMIT 1"
            ),
            {"oid": str(oid)},
        ).fetchone()
        if row is None:
            return None
        return float(row[0])
    except Exception:
        return None


def backfill_outcomes_from_broker_truth(db, *, lookback_days: float = 30.0) -> dict:
    """DEPRECATED (mig309): superseded by
    ``outcome_reconcile.reconcile_momentum_outcomes_to_broker_truth``, which writes a
    SEPARATE authoritative broker_* label (never overwrites realized_pnl_usd, uses a
    COUNT==1 trade-row guard, and EXCLUDES rather than mis-labels pyramids/ambiguous
    matches). Kept as a fallback path only — DO NOT call from new code; this function
    still OVERWRITES realized_pnl_usd in place on a LIMIT-1 non-unique-key join.

    Repair censored outcome rows from broker truth (2026-06-12 quant pass v2
    A1): flatten cascades / reconcile paths wrote NULL/zero realized while the
    broker filled real money — the 30d ledger read −$234 vs +$2,568 broker
    truth, and the streak multiplier halved size off the phantom record.
    Joins outcome → session → le.entry_order_id → trading_trades.broker_order_id
    (never symbol/time — manual trades share the account). Idempotent."""
    from datetime import datetime, timedelta

    from sqlalchemy import text as _text

    from ....models.trading import MomentumAutomationOutcome, TradingAutomationSession

    fixed = 0
    checked = 0
    try:
        cutoff = datetime.utcnow() - timedelta(days=float(lookback_days))
        rows = (
            db.query(MomentumAutomationOutcome, TradingAutomationSession)
            .join(TradingAutomationSession, TradingAutomationSession.id == MomentumAutomationOutcome.session_id)
            .filter(
                MomentumAutomationOutcome.terminal_at >= cutoff,
                MomentumAutomationOutcome.mode == "live",
                TradingAutomationSession.execution_family != "alpaca_spot",
            )
            .all()
        )
    except Exception:
        return {"ok": False, "error": "query_failed"}
    for outcome, sess in rows:
        checked += 1
        try:
            snap = sess.risk_snapshot_json if isinstance(sess.risk_snapshot_json, dict) else {}
            le = snap.get("momentum_live_execution") or {}
            oid = le.get("entry_order_id")
            if not oid:
                continue
            row = db.execute(
                _text(
                    "SELECT pnl FROM trading_trades WHERE broker_order_id = :oid "
                    "AND status = 'closed' AND pnl IS NOT NULL "
                    "ORDER BY exit_date DESC LIMIT 1"
                ),
                {"oid": str(oid)},
            ).fetchone()
            if row is None:
                continue
            bt = float(row[0])
            cur = outcome.realized_pnl_usd
            if cur is None or abs(bt - float(cur or 0.0)) > 0.01:
                outcome.realized_pnl_usd = bt
                if outcome.outcome_class in ("cancelled_pre_entry", "error_exit") and abs(bt) > 0.01:
                    outcome.outcome_class = "broker_truth_reclassified"
                fixed += 1
        except Exception:
            continue
    return {"ok": True, "checked": checked, "fixed": fixed}


def _strip_reconcile_suffix(exit_reason: Optional[str]) -> Optional[str]:
    """Drop the broker-zero-reconcile provenance suffix for class matching.

    Returns the input unchanged when no suffix is present (and None/"" as-is),
    so it is safe to call on any terminal branch.
    """
    if not exit_reason:
        return exit_reason
    for suffix in _RECONCILE_SUFFIXES:
        if exit_reason.endswith(suffix):
            return exit_reason[: -len(suffix)]
    return exit_reason


def _classify_real_exit(
    *,
    exit_reason: Optional[str],
    return_bps: Optional[float],
    realized_pnl_usd: Optional[float],
    entry_occurred: bool,
    governance_context: dict[str, Any],
) -> str:
    """Classify a COMPLETED exit by its reason + economic result.

    Shared by the finished terminal branch and the live_cancelled reconcile
    reroute so both label a real round-trip identically. The broker-zero-
    reconcile suffix is stripped before keyword matching.
    """
    if governance_context.get("kill_switch_exit"):
        return OUTCOME_GOVERNANCE_EXIT
    er = (_strip_reconcile_suffix(exit_reason) or "").lower()
    if er == "alpaca_orphan_reconcile":
        # A dead-session orphan flatten is a broker-safety/governance exit, not a
        # strategy stop or success.  It still carries its real PnL into risk/day
        # accounting, but it must not train entry/exit expectancy.
        return OUTCOME_GOVERNANCE_EXIT
    if "stop" in er or er == "stop":
        return OUTCOME_STOP_LOSS
    if "bailout" in er or er == "bailout":
        return OUTCOME_BAILOUT
    if "max_hold" in er or "timed" in er or er == "max_hold":
        return OUTCOME_TIMED_EXIT
    rb = return_bps
    rp = realized_pnl_usd
    if rb is not None:
        if rb >= 25.0:
            return OUTCOME_SUCCESS
        if rb > 0:
            return OUTCOME_SMALL_WIN
        if rb <= -25.0:
            return OUTCOME_STOP_LOSS
    if rp is not None:
        if rp > 0:
            return OUTCOME_SMALL_WIN
        if rp < 0:
            return OUTCOME_STOP_LOSS
    if entry_occurred:
        return OUTCOME_FLAT_UNKNOWN
    return OUTCOME_CANCELLED_PRE_ENTRY


def derive_outcome_class(
    *,
    mode: str,
    terminal_state: str,
    entry_occurred: bool,
    partial_exit: bool,
    realized_pnl_usd: Optional[float],
    return_bps: Optional[float],
    exit_reason: Optional[str],
    governance_context: dict[str, Any],
    events: list[TradingAutomationEvent],
) -> str:
    """Deterministic label from terminal state + execution hints + recent events."""
    st = terminal_state or ""
    m = (mode or "paper").lower()

    if st == STATE_EXPIRED:
        return OUTCOME_EXPIRED_PRE_RUN
    if st == STATE_ARCHIVED:
        return OUTCOME_ARCHIVED
    if st in (STATE_CANCELLED, STATE_LIVE_CANCELLED):
        # A reconcile/late cancel that nonetheless completed a REAL round-trip —
        # a full position-closing exit with a recorded exit reason — is not a
        # decision-level cancel. The broker-zero-reconcile exit path lands a real
        # exit (stop/bailout/trail/max_hold), and the session can then terminate
        # in live_cancelled (e.g. the recycled post-exit watcher is reaped, or a
        # duplicate claimant is cleaned up) instead of live_finished. Classify it
        # by its true exit class so the strategy learner sees the win/loss
        # instead of dropping it as a non-strategy cancel. Only a recorded FULL
        # exit reason counts here (a partial sets last_partial_exit_reason, not
        # exit_reason), so a position-neutral operator/dup cancel of a still-open
        # position correctly stays cancelled_in_trade.
        if (entry_occurred or partial_exit) and _strip_reconcile_suffix(exit_reason):
            reclassified = _classify_real_exit(
                exit_reason=exit_reason,
                return_bps=return_bps,
                realized_pnl_usd=realized_pnl_usd,
                entry_occurred=entry_occurred,
                governance_context=governance_context,
            )
            if reclassified in _REAL_EXIT_OUTCOMES:
                return reclassified
        if entry_occurred or partial_exit:
            return OUTCOME_CANCELLED_IN_TRADE
        return OUTCOME_CANCELLED_PRE_ENTRY

    if st in (STATE_ERROR, STATE_LIVE_ERROR):
        for ev in events:
            if ev.event_type == "live_error" and isinstance(ev.payload_json, dict):
                if ev.payload_json.get("reason") == "zero_fill":
                    return OUTCOME_NO_FILL
            if ev.event_type == "paper_error" and isinstance(ev.payload_json, dict):
                r = str(ev.payload_json.get("reason") or "")
                if "missing_frozen" in r or "risk" in r.lower():
                    return OUTCOME_RISK_BLOCK
        if governance_context.get("stale_quote_abort"):
            return OUTCOME_STALE_DATA_ABORT
        if governance_context.get("kill_switch_blocked"):
            return OUTCOME_GOVERNANCE_EXIT
        if governance_context.get("risk_evaluation_block"):
            return OUTCOME_RISK_BLOCK
        return OUTCOME_ERROR_EXIT

    if st in (STATE_FINISHED, STATE_LIVE_FINISHED):
        return _classify_real_exit(
            exit_reason=exit_reason,
            return_bps=return_bps,
            realized_pnl_usd=realized_pnl_usd,
            entry_occurred=entry_occurred,
            governance_context=governance_context,
        )

    return OUTCOME_FLAT_UNKNOWN


def outcome_row_from_extracted(
    extracted: dict[str, Any],
    *,
    evidence_weight: float = 1.0,
    contributes_to_evolution: bool | None = None,
) -> MomentumAutomationOutcome:
    terminal_at = datetime.utcnow()
    try:
        terminal_at = datetime.fromisoformat(str(extracted["terminal_at_utc"]).replace("Z", "+00:00")).replace(
            tzinfo=None
        )
    except Exception:
        pass

    credit = outcome_evolution_credit_from_extracted(extracted)
    contributes = credit["contributes_to_evolution"] if contributes_to_evolution is None else bool(contributes_to_evolution)
    if contributes_to_evolution is not None:
        credit["overridden"] = True

    summary = {
        k: extracted.get(k)
        for k in (
            "entry_occurred",
            "entry_decision_packet_id",
            "partial_exit_occurred",
            "quote_source_at_entry",
            "notional_basis_usd",
            "variant_family",
            "variant_key",
        )
    }
    summary["evolution_credit"] = credit
    # Only carried when there IS multi-cycle history — an empty key on 3,687 clean
    # cancels would be noise in a hot JSONB column for no reader.
    _cycles = extracted.get("closed_cycles_v1")
    if isinstance(_cycles, dict) and int(_cycles.get("count") or 0) > 0:
        summary["closed_cycles_v1"] = _cycles

    return MomentumAutomationOutcome(
        session_id=int(extracted["session_id"]),
        user_id=extracted.get("user_id"),
        variant_id=int(extracted["variant_id"]),
        symbol=str(extracted["symbol"]),
        mode=str(extracted["mode"]),
        execution_family=str(extracted.get("execution_family") or "coinbase_spot"),
        terminal_state=str(extracted["terminal_state"]),
        terminal_at=terminal_at,
        outcome_class=str(extracted["outcome_class"]),
        realized_pnl_usd=extracted.get("realized_pnl_usd"),
        return_bps=extracted.get("return_bps"),
        hold_seconds=extracted.get("hold_seconds"),
        exit_reason=extracted.get("exit_reason"),
        regime_snapshot_json=dict(extracted.get("regime_snapshot_json") or {}),
        entry_regime_snapshot_json=dict(extracted.get("entry_regime_snapshot_json") or {}),
        exit_regime_snapshot_json=dict(extracted.get("exit_regime_snapshot_json") or {}),
        readiness_snapshot_json=dict(extracted.get("readiness_snapshot_json") or {}),
        admission_snapshot_json=dict(extracted.get("admission_snapshot_json") or {}),
        governance_context_json=dict(extracted.get("governance_context_json") or {}),
        extracted_summary_json=summary,
        evidence_weight=float(evidence_weight),
        contributes_to_evolution=bool(contributes),
        code_version=_CODE_VERSION,
    )


def outcome_evolution_credit_from_extracted(extracted: dict[str, Any]) -> dict[str, Any]:
    """Decide whether a terminal outcome may update neural evolution.

    Audit rows are still persisted, but model credit requires the closed-loop
    chain to include an actual entry, the decision packet that authorized it,
    an economic result, and a strategy-caused terminal outcome.
    """
    entry_occurred = bool(extracted.get("entry_occurred"))
    packet_id = extracted.get("entry_decision_packet_id")
    try:
        packet_id_int = int(packet_id) if packet_id is not None else None
    except (TypeError, ValueError):
        packet_id_int = None
    has_economic_result = extracted.get("return_bps") is not None or extracted.get("realized_pnl_usd") is not None
    outcome_class = str(extracted.get("outcome_class") or "").strip()
    mode = str(extracted.get("mode") or "").strip().lower()
    quote_source = str(extracted.get("quote_source_at_entry") or "").strip().lower()
    reasons: list[str] = []
    if not entry_occurred:
        reasons.append("no_entry")
    if packet_id_int is None:
        reasons.append("missing_entry_decision_packet")
    if not has_economic_result:
        reasons.append("missing_economic_result")
    if outcome_class in _NON_STRATEGY_CREDIT_OUTCOMES:
        reasons.append(f"non_strategy_outcome_{outcome_class}")
    if mode == "paper" and quote_source in {"synthetic", "synthetic_spread"}:
        reasons.append("paper_synthetic_quote_source")
    return {
        "contributes_to_evolution": not reasons,
        "reason_codes": reasons,
        "entry_decision_packet_id": packet_id_int,
        "outcome_class": outcome_class or None,
        "quote_source_at_entry": quote_source or None,
    }


def feedback_row_exists(db: Session, session_id: int) -> bool:
    return (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(session_id))
        .one_or_none()
        is not None
    )
