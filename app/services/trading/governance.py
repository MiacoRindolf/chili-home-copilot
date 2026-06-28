"""Human-in-the-loop governance for the trading brain.

Provides safety mechanisms:
- Kill switch: instantly halt all trading activity
- Approval queue: promoted patterns must be approved before going live
- Size threshold gates: large trades require manual approval
- Trade velocity limits: prevent runaway automated trading
"""
from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings
from .return_math import (
    paper_trade_realized_pnl,
    paper_trade_return_pct,
    trade_realized_pnl,
)

logger = logging.getLogger(__name__)

# ── Kill Switch ───────────────────────────────────────────────────────

_kill_switch = False
_kill_switch_reason: str | None = None
_kill_switch_set_at: datetime | None = None
_kill_switch_db_error: str | None = None
_kill_switch_db_persisted: bool | None = None
_kill_switch_db_fail_closed_active: bool = False
_kill_switch_last_db_check_monotonic: float = 0.0
_daily_breach_recovery_last_check_monotonic: float = 0.0
_kill_switch_lock = threading.Lock()


def _trade_realized_pnl_with_raw_fallback(trade: Any) -> float | None:
    pnl = trade_realized_pnl(trade)
    if pnl is not None:
        return pnl
    try:
        raw = getattr(trade, "pnl", None)
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _paper_realized_pnl_with_raw_fallback(paper_trade: Any) -> float | None:
    pnl = paper_trade_realized_pnl(paper_trade)
    if pnl is not None:
        return pnl
    try:
        raw = getattr(paper_trade, "pnl", None)
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _paper_directional_win(paper_trade: Any) -> bool | None:
    ret = paper_trade_return_pct(paper_trade)
    if ret is not None:
        return ret > 0.0
    pnl = _paper_realized_pnl_with_raw_fallback(paper_trade)
    return (pnl > 0.0) if pnl is not None else None


def _kill_switch_db_poll_enabled() -> bool:
    return bool(getattr(settings, "chili_kill_switch_db_poll_enabled", True))


def _kill_switch_db_poll_interval_s() -> float:
    try:
        return max(0.0, float(getattr(settings, "chili_kill_switch_db_poll_interval_s", 0.0) or 0.0))
    except Exception:
        return 0.0


def _kill_switch_db_fail_closed() -> bool:
    return bool(getattr(settings, "chili_kill_switch_db_fail_closed", True))


def _looks_like_missing_risk_state_table(exc: BaseException) -> bool:
    msg = str(exc).lower()
    return "trading_risk_state" in msg and (
        "does not exist" in msg or "undefinedtable" in msg or "no such table" in msg
    )


def _apply_kill_switch_state(
    *,
    active: bool,
    reason: str | None,
    set_at: datetime | None,
    db_error: str | None = None,
    transient_db_fail_closed: bool = False,
) -> None:
    global _kill_switch, _kill_switch_reason, _kill_switch_set_at, _kill_switch_db_error, _kill_switch_db_persisted, _kill_switch_db_fail_closed_active
    with _kill_switch_lock:
        _kill_switch = bool(active)
        _kill_switch_reason = (reason or None) if active else None
        _kill_switch_set_at = set_at if active else None
        _kill_switch_db_error = db_error
        _kill_switch_db_fail_closed_active = bool(active and transient_db_fail_closed)
        if db_error is None:
            _kill_switch_db_persisted = True


def _fetch_latest_kill_switch_state(
    sess: Session,
) -> tuple[bool, str | None, datetime | None] | None:
    row = sess.execute(text(
        "SELECT breaker_tripped, breaker_reason, created_at "
        "FROM trading_risk_state "
        "WHERE regime = 'kill_switch' "
        "ORDER BY created_at DESC, id DESC LIMIT 1"
    )).fetchone()
    if not row:
        return None
    return bool(row[0]), (row[1] or None), row[2]


def _fetch_latest_kill_switch_state_from_db() -> tuple[bool, str | None, datetime | None] | None:
    from ...db import SessionLocal

    sess = SessionLocal()
    try:
        return _fetch_latest_kill_switch_state(sess)
    finally:
        try:
            sess.rollback()
        except Exception:
            pass
        sess.close()


def _refresh_kill_switch_from_db_if_due(
    *,
    force: bool = False,
    db: Session | None = None,
) -> None:
    """Refresh process-local kill-switch state from durable DB state.

    The scheduler and API run in separate processes. Live order paths call
    ``is_kill_switch_active`` repeatedly, so this small DB-backed refresh makes
    a kill switch flipped in the API visible to the scheduler without restart.
    """
    global _kill_switch, _kill_switch_reason, _kill_switch_set_at
    global _kill_switch_db_error, _kill_switch_db_persisted, _kill_switch_db_fail_closed_active
    global _kill_switch_last_db_check_monotonic
    if not _kill_switch_db_poll_enabled():
        return
    now = time.monotonic()
    interval = _kill_switch_db_poll_interval_s()
    with _kill_switch_lock:
        if not force and interval > 0 and (now - _kill_switch_last_db_check_monotonic) < interval:
            return
        _kill_switch_last_db_check_monotonic = now
    try:
        state = (
            _fetch_latest_kill_switch_state(db)
            if db is not None
            else _fetch_latest_kill_switch_state_from_db()
        )
    except Exception as exc:
        if db is not None:
            try:
                db.rollback()
            except Exception:
                logger.debug("[governance] kill-switch session rollback failed", exc_info=True)
        if _looks_like_missing_risk_state_table(exc):
            logger.debug("[governance] kill-switch DB refresh skipped; trading_risk_state missing")
            return
        msg = f"kill_switch_db_read_failed:{type(exc).__name__}"
        if _kill_switch_db_fail_closed():
            with _kill_switch_lock:
                preserve_existing_halt = _kill_switch and not _kill_switch_db_fail_closed_active
                if preserve_existing_halt:
                    _kill_switch_db_error = str(exc)[:500]
            if not preserve_existing_halt:
                _apply_kill_switch_state(
                    active=True,
                    reason=msg,
                    set_at=datetime.utcnow(),
                    db_error=str(exc)[:500],
                    transient_db_fail_closed=True,
                )
            logger.warning("[governance] Kill-switch DB read failed; failing closed", exc_info=True)
        else:
            with _kill_switch_lock:
                _kill_switch_db_error = str(exc)[:500]
            logger.warning("[governance] Kill-switch DB read failed; using process-local state", exc_info=True)
        return
    if state is None:
        with _kill_switch_lock:
            _kill_switch_db_error = None
            if _kill_switch_db_fail_closed_active:
                _kill_switch = False
                _kill_switch_reason = None
                _kill_switch_set_at = None
                _kill_switch_db_persisted = None
                _kill_switch_db_fail_closed_active = False
        return
    active, reason, set_at = state
    _apply_kill_switch_state(active=active, reason=reason, set_at=set_at, db_error=None)


def activate_kill_switch(reason: str = "manual") -> None:
    """Immediately halt all trading activity. Persists to DB.

    broker-truth-self-heal (2026-05-04): idempotent on same reason. The
    prior implementation re-armed (and re-emitted the CRITICAL log) on
    every call, which produced a 5-min flood of identical kill-switch
    activations from the price-monitor guardrail. Same-reason calls now
    no-op; a different reason still writes (state change worth recording).
    """
    global _kill_switch, _kill_switch_reason, _kill_switch_set_at, _kill_switch_db_error, _kill_switch_db_persisted, _kill_switch_db_fail_closed_active
    needs_persist = True
    with _kill_switch_lock:
        if _kill_switch and _kill_switch_reason == reason:
            needs_persist = _kill_switch_db_persisted is not True
            if not needs_persist:
                return
        else:
            _kill_switch = True
            _kill_switch_reason = reason
            _kill_switch_set_at = datetime.utcnow()
            _kill_switch_db_error = None
            _kill_switch_db_persisted = None
            _kill_switch_db_fail_closed_active = False
    persisted = _persist_kill_switch_state(True, reason)
    with _kill_switch_lock:
        _kill_switch_db_persisted = bool(persisted)
    if needs_persist:
        logger.critical("[governance] KILL SWITCH ACTIVATED: %s", reason)


def deactivate_kill_switch() -> None:
    """Re-enable trading activity. Persists to DB."""
    global _kill_switch, _kill_switch_reason, _kill_switch_set_at, _kill_switch_db_error, _kill_switch_db_persisted, _kill_switch_db_fail_closed_active
    with _kill_switch_lock:
        _kill_switch = False
        _kill_switch_reason = None
        _kill_switch_set_at = None
        _kill_switch_db_error = None
        _kill_switch_db_persisted = None
        _kill_switch_db_fail_closed_active = False
    persisted = _persist_kill_switch_state(False, None)
    with _kill_switch_lock:
        _kill_switch_db_persisted = bool(persisted)
    logger.info("[governance] Kill switch deactivated")


def _auto_clear_stale_daily_breach() -> None:
    """A DAILY-loss breach is scoped to its ET trading day (operator 2026-06-11:
    a daily cap should not need a manual reset) — once the ET date rolls, the
    breach that armed the switch no longer describes reality, so it self-clears.
    Manual and non-daily activations are untouched (operator action required)."""
    with _kill_switch_lock:
        active = _kill_switch
        reason = _kill_switch_reason or ""
        set_at = _kill_switch_set_at
    if not active or not reason.startswith("global_daily_loss_breach") or set_at is None:
        return
    try:
        from zoneinfo import ZoneInfo

        from datetime import timezone as _tz

        et = ZoneInfo("America/New_York")
        set_day = set_at.replace(tzinfo=_tz.utc).astimezone(et).date()
        today = datetime.utcnow().replace(tzinfo=_tz.utc).astimezone(et).date()
        if today > set_day:
            logger.warning(
                "[governance] daily-loss kill switch auto-cleared at ET day roll (was: %s, set_at=%s)",
                reason, set_at,
            )
            deactivate_kill_switch()
    except Exception:
        logger.debug("[governance] daily-breach auto-clear skipped", exc_info=True)


def _auto_clear_recovered_daily_breach(db: Session | None = None) -> None:
    """A DAILY-loss breach that has RECOVERED intraday self-clears (operator
    2026-06-16: today a transient 09:10 ET −$300 blip recovered to +$265 realized
    but stayed FROZEN all day because the only auto-clear was the ET-day-roll — the
    whole profitable day was locked out, and CHILI missed every mover). When the
    switch is active for a ``global_daily_loss_breach`` AND today's realized PnL has
    climbed back to ABOVE ``-(cap * fraction)``, the breach no longer describes
    reality → clear it. The fraction is a HYSTERESIS band (recovery must clear the
    cap by a margin) so realized hovering at the threshold cannot trip/clear/trip.
    Re-trips normally if realized falls back to ``<= -cap``. Manual / non-daily /
    per-broker-backstop activations are untouched (operator action required)."""
    global _daily_breach_recovery_last_check_monotonic
    with _kill_switch_lock:
        active = _kill_switch
        reason = _kill_switch_reason or ""
    if not active or not reason.startswith("global_daily_loss_breach"):
        return
    # The per-broker aggregate failsafe ('backstop') has its own clear path.
    if "backstop" in reason:
        return
    frac = float(
        getattr(settings, "chili_daily_loss_recovery_clear_fraction", 0.5) or 0.0
    )
    if frac <= 0.0:
        return  # feature disabled → date-roll / manual only
    # Throttle: this runs a DB PnL sum and is_kill_switch_active() is on the hot
    # order path. Bound it to at most once per interval.
    now = time.monotonic()
    interval = float(
        getattr(settings, "chili_daily_loss_recovery_check_interval_s", 30.0) or 0.0
    )
    with _kill_switch_lock:
        if interval > 0 and (now - _daily_breach_recovery_last_check_monotonic) < interval:
            return
        _daily_breach_recovery_last_check_monotonic = now
    _own_db = False
    try:
        if db is None:
            from ...db import SessionLocal

            db = SessionLocal()
            _own_db = True
        # Non-mutating re-evaluation (activate=False → never trips here).
        res = check_daily_loss_breach(db, activate=False)
        realized = float(res.get("realized_usd", 0.0))
        limit = float(res.get("limit_usd", 0.0) or 0.0)
        if limit <= 0:
            return
        if realized >= -(limit * frac):
            logger.warning(
                "[governance] daily-loss kill switch auto-cleared on intraday RECOVERY "
                "(realized=%.2f >= -%.2f [cap=%.2f x frac=%.2f], was: %s)",
                realized, limit * frac, limit, frac, reason,
            )
            deactivate_kill_switch()
    except Exception:
        if _own_db and db is not None:
            try:
                db.rollback()
            except Exception:
                pass
        logger.debug("[governance] daily-breach recovery auto-clear skipped", exc_info=True)
    finally:
        if _own_db and db is not None:
            try:
                db.close()
            except Exception:
                pass


def is_kill_switch_active() -> bool:
    _refresh_kill_switch_from_db_if_due()
    _auto_clear_stale_daily_breach()
    _auto_clear_recovered_daily_breach()
    try:
        clear_stale_broker_daily_loss_blocks()
    except Exception:
        pass
    with _kill_switch_lock:
        return _kill_switch


def is_kill_switch_active_for_session(db: Session) -> bool:
    _refresh_kill_switch_from_db_if_due(db=db)
    _auto_clear_stale_daily_breach()
    _auto_clear_recovered_daily_breach(db=db)
    with _kill_switch_lock:
        return _kill_switch


def get_kill_switch_status() -> dict[str, Any]:
    _refresh_kill_switch_from_db_if_due()
    with _kill_switch_lock:
        return {
            "active": _kill_switch,
            "reason": _kill_switch_reason,
            "set_at": _kill_switch_set_at.isoformat() + "Z" if _kill_switch_set_at else None,
            "db_error": _kill_switch_db_error,
            "transient_db_fail_closed": _kill_switch_db_fail_closed_active,
        }


def get_kill_switch_reason() -> str | None:
    _refresh_kill_switch_from_db_if_due()
    with _kill_switch_lock:
        return _kill_switch_reason


def _persist_kill_switch_state(active: bool, reason: str | None) -> bool:
    """Write kill-switch state to trading_risk_state so it survives restarts."""
    try:
        from ...db import SessionLocal
        from sqlalchemy import text
        sess = SessionLocal()
        try:
            sess.execute(text(
                "INSERT INTO trading_risk_state (user_id, snapshot_date, breaker_tripped, breaker_reason, regime, capital) "
                "VALUES (:uid, NOW(), :tripped, :reason, 'kill_switch', 0) "
            ), {"uid": None, "tripped": active, "reason": reason or ""})
            sess.commit()
            return True
        finally:
            # FIX 46 pattern: rollback to end implicit read txn before close.
            try:
                sess.rollback()
            except Exception:
                pass
            sess.close()
    except Exception:
        logger.debug("[governance] Failed to persist kill-switch state to DB", exc_info=True)
        return False


def restore_kill_switch_from_db() -> None:
    """Restore kill-switch state from DB on startup."""
    global _kill_switch, _kill_switch_reason, _kill_switch_set_at, _kill_switch_db_error, _kill_switch_db_persisted, _kill_switch_db_fail_closed_active
    try:
        state = _fetch_latest_kill_switch_state_from_db()
        if state is None:
            return
        active, reason, set_at = state
        with _kill_switch_lock:
            _kill_switch = active
            _kill_switch_reason = (reason or "restored from DB") if active else None
            _kill_switch_set_at = set_at if active else None
            _kill_switch_db_error = None
            _kill_switch_db_persisted = True
            _kill_switch_db_fail_closed_active = False
        if active:
            logger.warning("[governance] Kill switch restored from DB: %s", _kill_switch_reason)
    except Exception:
        logger.debug("[governance] Could not restore kill-switch from DB", exc_info=True)


# ── Approval Queue ────────────────────────────────────────────────────

_approval_queue: list[dict[str, Any]] = []
_approval_lock = threading.Lock()


def _insert_approval_row(action_type: str, details: dict[str, Any]) -> int | None:
    """Persist approval request and return DB id when available."""
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            row = sess.execute(
                text(
                    """
                    INSERT INTO trading_governance_approvals (
                        action_type, details_json, submitted_at, status
                    ) VALUES (
                        :action_type, CAST(:details_json AS JSONB), NOW(), 'pending'
                    )
                    RETURNING id
                    """
                ),
                {
                    "action_type": action_type,
                    "details_json": json.dumps(details or {}, ensure_ascii=True),
                },
            ).fetchone()
            sess.commit()
            return int(row[0]) if row and row[0] is not None else None
        finally:
            # FIX 46 pattern: rollback to end implicit read txn before close.
            try:
                sess.rollback()
            except Exception:
                pass
            sess.close()
    except Exception:
        logger.debug("[governance] Failed to persist approval request", exc_info=True)
        return None


def _fetch_pending_approvals_from_db() -> list[dict[str, Any]]:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            rows = sess.execute(
                text(
                    """
                    SELECT id, action_type, details_json, submitted_at, status, decision, decided_at, notes
                    FROM trading_governance_approvals
                    WHERE status = 'pending'
                    ORDER BY submitted_at DESC
                    """
                )
            ).fetchall()
            out: list[dict[str, Any]] = []
            for row in rows:
                details = row[2] or {}
                if isinstance(details, str):
                    try:
                        details = json.loads(details)
                    except Exception:
                        details = {}
                out.append(
                    {
                        "id": int(row[0]),
                        "action_type": row[1],
                        "details": details if isinstance(details, dict) else {},
                        "submitted_at": row[3].isoformat() + "Z" if row[3] else None,
                        "status": row[4],
                        "decision": row[5],
                        "decided_at": row[6].isoformat() + "Z" if row[6] else None,
                        "notes": row[7] or "",
                    }
                )
            return out
        finally:
            # FIX 46 pattern: rollback to end implicit read txn before close.
            try:
                sess.rollback()
            except Exception:
                pass
            sess.close()
    except Exception:
        logger.debug("[governance] Failed to fetch DB approvals", exc_info=True)
        return []


def _set_approval_decision_db(approval_id: int, decision: str, notes: str = "") -> bool:
    try:
        from ...db import SessionLocal

        sess = SessionLocal()
        try:
            row = sess.execute(
                text(
                    """
                    UPDATE trading_governance_approvals
                    SET status = :status,
                        decision = :decision,
                        decided_at = NOW(),
                        notes = :notes
                    WHERE id = :approval_id
                      AND status = 'pending'
                    RETURNING id
                    """
                ),
                {
                    "status": "approved" if decision == "approved" else "rejected",
                    "decision": decision,
                    "notes": notes or "",
                    "approval_id": int(approval_id),
                },
            ).fetchone()
            sess.commit()
            return bool(row)
        finally:
            # FIX 46 pattern: rollback to end implicit read txn before close.
            try:
                sess.rollback()
            except Exception:
                pass
            sess.close()
    except Exception:
        logger.debug("[governance] Failed to update DB approval decision", exc_info=True)
        return False


def submit_for_approval(
    action_type: str,
    details: dict[str, Any],
    *,
    auto_approve_if: bool = False,
) -> dict[str, Any]:
    """Submit an action for human approval.

    action_type: "pattern_to_live", "large_trade", "model_promotion"
    """
    if auto_approve_if:
        return {"approved": True, "auto": True}

    entry = {
        "id": None,
        "action_type": action_type,
        "details": details,
        "submitted_at": datetime.utcnow().isoformat() + "Z",
        "status": "pending",
        "decision": None,
        "decided_at": None,
    }
    db_id = _insert_approval_row(action_type, details)
    if db_id is not None:
        entry["id"] = db_id
    with _approval_lock:
        if entry["id"] is None:
            entry["id"] = len(_approval_queue) + 1
        _approval_queue.append(entry)

    logger.info("[governance] Submitted for approval: %s #%d", action_type, entry["id"])
    return {"approved": False, "queued": True, "approval_id": entry["id"]}


def get_pending_approvals() -> list[dict[str, Any]]:
    """Get all pending approval requests."""
    db_rows = _fetch_pending_approvals_from_db()
    if db_rows:
        return db_rows
    with _approval_lock:
        return [a for a in _approval_queue if a["status"] == "pending"]


def approve(approval_id: int, *, notes: str = "") -> bool:
    """Approve a pending request."""
    if _set_approval_decision_db(approval_id, "approved", notes=notes):
        logger.info("[governance] Approved #%d via DB", approval_id)
        return True
    with _approval_lock:
        for a in _approval_queue:
            if a["id"] == approval_id and a["status"] == "pending":
                a["status"] = "approved"
                a["decision"] = "approved"
                a["decided_at"] = datetime.utcnow().isoformat() + "Z"
                a["notes"] = notes
                logger.info("[governance] Approved #%d: %s", approval_id, a["action_type"])
                return True
    return False


def reject(approval_id: int, *, reason: str = "") -> bool:
    """Reject a pending request."""
    if _set_approval_decision_db(approval_id, "rejected", notes=reason):
        logger.info("[governance] Rejected #%d via DB (%s)", approval_id, reason)
        return True
    with _approval_lock:
        for a in _approval_queue:
            if a["id"] == approval_id and a["status"] == "pending":
                a["status"] = "rejected"
                a["decision"] = "rejected"
                a["decided_at"] = datetime.utcnow().isoformat() + "Z"
                a["notes"] = reason
                logger.info("[governance] Rejected #%d: %s (%s)", approval_id, a["action_type"], reason)
                return True
    return False


# ── Size Threshold Gates ──────────────────────────────────────────────

DEFAULT_MAX_AUTO_NOTIONAL = 10_000.0  # trades above this need approval
DEFAULT_MAX_AUTO_RISK_PCT = 2.0       # risk % above this needs approval


def check_trade_gate(
    notional: float,
    risk_pct: float,
    *,
    max_auto_notional: float = DEFAULT_MAX_AUTO_NOTIONAL,
    max_auto_risk_pct: float = DEFAULT_MAX_AUTO_RISK_PCT,
    ticker: str = "",
) -> dict[str, Any]:
    """Check if a trade passes automatic execution gates.

    Returns {"allowed": True/False, "reason": str, "needs_approval": bool}
    """
    if is_kill_switch_active():
        return {"allowed": False, "reason": "kill_switch_active", "needs_approval": False}

    if notional > max_auto_notional:
        result = submit_for_approval("large_trade", {
            "ticker": ticker,
            "notional": notional,
            "risk_pct": risk_pct,
            "threshold": max_auto_notional,
        })
        return {"allowed": False, "reason": f"notional ${notional:.0f} > gate ${max_auto_notional:.0f}", "needs_approval": True}

    if risk_pct > max_auto_risk_pct:
        result = submit_for_approval("large_trade", {
            "ticker": ticker,
            "notional": notional,
            "risk_pct": risk_pct,
            "threshold_pct": max_auto_risk_pct,
        })
        return {"allowed": False, "reason": f"risk {risk_pct:.1f}% > gate {max_auto_risk_pct:.1f}%", "needs_approval": True}

    return {"allowed": True, "reason": "within_auto_gates", "needs_approval": False}


# ── Global Daily-Loss Halt (P0.2) ─────────────────────────────────────
#
# Single source of truth that spans BOTH AutoTrader v1 (Trade table) and
# momentum_neural (MomentumAutomationOutcome table). Intentionally additive
# to the path-local caps:
#   • auto_trader_monitor uses its own $150 v1-only cap
#   • momentum_neural uses policy.max_daily_loss_usd ($250)
# Either a local cap or this global cap can fire the kill switch; defense
# in depth. Use the MORE CONSERVATIVE of (usd, pct_of_equity) when both
# are configured.


def global_realized_pnl_today_et(
    db: Session, user_id: int | None = None
) -> dict[str, Any]:
    """Sum realized PnL across ALL trading paths for today's US/Eastern calendar day.

    Unlike `auto_trader_rules.autotrader_realized_pnl_today_et` (which filters
    `auto_trader_version == "v1"`), this aggregates across:
      - `Trade` rows (ALL auto_trader_versions, closed with exit_date in today's ET session)
      - `MomentumAutomationOutcome` rows (sessions with terminal_at in today's ET session)

    Returns {"total_usd", "autotrader_usd", "momentum_usd"} — signed
    (negative == loss). `user_id=None` → sum across all users.
    """
    from datetime import datetime as _dt, timedelta as _td
    from zoneinfo import ZoneInfo
    from sqlalchemy import func as sa_func
    from ...models.trading import Trade, MomentumAutomationOutcome

    et = ZoneInfo("America/New_York")
    now_et = _dt.now(et)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + _td(days=1)
    start_utc = start_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    # Trade rows (all versions; v1 and any future variants)
    tq = db.query(Trade).filter(
        Trade.status == "closed",
        Trade.exit_date.isnot(None),
        Trade.exit_date >= start_utc,
        Trade.exit_date < end_utc,
    )
    if user_id is not None:
        tq = tq.filter(Trade.user_id == user_id)
    trade_total = 0.0
    for t in tq.all():
        pnl = _trade_realized_pnl_with_raw_fallback(t)
        if pnl is not None:
            trade_total += pnl

    # Momentum automation outcomes. ALPACA PAPER EXCLUSION (2026-06-12): the
    # alpaca_spot twin-soak sessions trade FAKE money against the paper API —
    # their outcomes must never move the REAL daily-loss math (a fake -$300
    # would trip the real kill switch).
    from ...models.trading import TradingAutomationSession as _TAS

    mq = db.query(
        sa_func.coalesce(sa_func.sum(MomentumAutomationOutcome.realized_pnl_usd), 0.0)
    ).join(
        _TAS, _TAS.id == MomentumAutomationOutcome.session_id
    ).filter(
        MomentumAutomationOutcome.terminal_at >= start_utc,
        MomentumAutomationOutcome.terminal_at < end_utc,
        _TAS.execution_family != "alpaca_spot",
    )
    if user_id is not None:
        mq = mq.filter(MomentumAutomationOutcome.user_id == user_id)
    momentum_total = float(mq.scalar() or 0.0)

    return {
        "total_usd": trade_total + momentum_total,
        "autotrader_usd": trade_total,
        "momentum_usd": momentum_total,
    }


def check_daily_loss_breach(
    db: Session,
    *,
    user_id: int | None = None,
    equity_usd: float | None = None,
    activate: bool = True,
) -> dict[str, Any]:
    """Check if today's realized loss breaches the global daily-loss cap.

    Limits (in priority: the more conservative wins):
      - `chili_global_max_daily_loss_usd` (absolute dollar cap; disabled if 0)
      - `chili_global_max_daily_loss_pct_of_equity * equity_usd`
        (pct cap; requires `equity_usd`; disabled if pct=0 or equity missing)

    If breached and `activate=True`, fires the global kill switch.

    Returns:
      {
        "breached": bool,
        "reason": str,
        "realized_usd": float,        # signed; negative means losing
        "limit_usd": float,           # positive dollar amount of the chosen cap
        "source": "usd"|"pct_equity"|"none",
        "breakdown": {"autotrader_usd": float, "momentum_usd": float},
      }
    """
    usd_cap = float(getattr(settings, "chili_global_max_daily_loss_usd", 0.0) or 0.0)
    pct_cap = float(getattr(settings, "chili_global_max_daily_loss_pct_of_equity", 0.0) or 0.0)

    pnl = global_realized_pnl_today_et(db, user_id)
    realized = float(pnl["total_usd"])

    # ADAPTIVE CAP (operator 2026-06-11: "dapat adaptive siya"): when the caller
    # didn't supply equity, resolve it ourselves so the pct-of-equity leg governs
    # at EVERY call site (previously no caller passed equity, so the fixed $300
    # leg always won the more-conservative race).
    if pct_cap > 0 and (equity_usd is None or float(equity_usd) <= 0):
        try:
            from .momentum_neural.risk_policy import _account_equity_usd
            from .execution_family_registry import EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP

            # BASIS FIX (2026-06-22): size the GLOBAL daily-loss cap off the account the
            # LIVE lane actually trades — the agentic equity rail (~$10.3k BP) — NOT the
            # legacy None->Coinbase default (~$3.7k) documented as buggy below. That default
            # froze the $13.7k agentic lane at a spurious 1.5% x $3.7k = $55 cap, tripping on
            # an -$84 day that is well within the real ~5% x $10.3k ≈ $515 budget. The
            # per-broker model (below) is the long-term fix but is currently disabled + has an
            # agentic-family realized-PnL bug; this resolves the GLOBAL basis correctly now.
            # apply_margin_multiple=False -> unlevered buying power (the RISK-cap basis).
            # [[project_per_broker_daily_loss]] [[feedback_adaptive_no_magic]]
            equity_usd = _account_equity_usd(
                EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP, apply_margin_multiple=False,
                prefer_equity=True,  # stable account equity (~$13.8k) not fluctuating BP
            )
        except Exception:
            equity_usd = None

    candidates: list[tuple[float, str]] = []
    if usd_cap > 0:
        candidates.append((usd_cap, "usd"))
    if pct_cap > 0 and equity_usd is not None and float(equity_usd) > 0:
        candidates.append((pct_cap * float(equity_usd), "pct_equity"))
    if pct_cap > 0 and not candidates:
        # Equity unresolvable AND no explicit usd override: fail CLOSED to the
        # documented fail-safe floor rather than trading uncapped.
        candidates.append((
            float(getattr(settings, "chili_global_daily_loss_failsafe_usd", 300.0) or 300.0),
            "usd_failsafe",
        ))

    if not candidates:
        return {
            "breached": False,
            "reason": "no_daily_loss_limit_configured",
            "realized_usd": realized,
            "limit_usd": 0.0,
            "source": "none",
            "breakdown": {
                "autotrader_usd": pnl["autotrader_usd"],
                "momentum_usd": pnl["momentum_usd"],
            },
        }

    # More conservative = smaller positive dollar amount
    limit_usd, source = min(candidates, key=lambda kv: kv[0])
    breached = realized <= -limit_usd
    reason = (
        f"global_daily_loss_breach_{source}_${limit_usd:.0f}"
        if breached
        else f"within_global_daily_loss_cap_${limit_usd:.0f}"
    )

    if breached and activate and not is_kill_switch_active():
        activate_kill_switch(reason)
        logger.critical(
            "[governance] Global daily-loss breach (realized=%.2f limit=-%.2f source=%s) — kill switch",
            realized,
            limit_usd,
            source,
        )

    return {
        "breached": bool(breached),
        "reason": reason,
        "realized_usd": realized,
        "limit_usd": limit_usd,
        "source": source,
        "breakdown": {
            "autotrader_usd": pnl["autotrader_usd"],
            "momentum_usd": pnl["momentum_usd"],
        },
    }


# ── Per-BROKER daily-loss caps ────────────────────────────────────────
# The single global daily-loss cap (above) froze BOTH brokers when ONE broke,
# and was sized off the WRONG broker (the None->Coinbase default in
# _account_equity_usd). The per-broker model fixes both: each broker is capped
# off ITS OWN real equity, and a breach blocks ONLY that broker — via a SEPARATE
# process-local registry that NEVER touches the global _kill_switch boolean, so
# exits stay live and true-global halts (manual/emergency/drawdown) still freeze
# everything. Operator 2026-06-15: "dapat ang kill switch is by broker".
# robinhood_agentic_mcp is first-class (operator 2026-06-25): it is the ACTIVE equities
# rail (a separate ~$13.6k cash account), so its realized PnL + cap must NOT collapse into
# the drained legacy robinhood_spot (~$19) — that mis-attribution + tiny-account cap is what
# produced the false "HALTED" on a -$4.72 agentic BLZE trade. Each rail caps off its OWN
# account and a breach blocks only that rail. [[project_per_broker_daily_loss]]
REAL_DAILY_LOSS_FAMILIES = ("robinhood_spot", "robinhood_agentic_mcp", "coinbase_spot")

# {family: {"reason": str, "et_date": date, "realized": float, "limit": float, "set_at": datetime}}
_per_broker_daily_loss: dict[str, dict[str, Any]] = {}
_per_broker_lock = threading.Lock()


def _et_today_date():
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo

    return datetime.utcnow().replace(tzinfo=_tz.utc).astimezone(ZoneInfo("America/New_York")).date()


def realized_pnl_today_by_broker(
    db: Session, user_id: int | None = None
) -> dict[str, float]:
    """Today's (ET-day) realized PnL split BY BROKER (execution_family).

    Mirrors global_realized_pnl_today_et's window + sources but buckets per
    broker so a per-broker cap can isolate a losing broker. alpaca_spot PAPER
    twins are excluded from the real-money math. Returns
    {"robinhood_spot": usd, "coinbase_spot": usd} (signed; negative == loss).
    Trade rows (autotrader) split by Trade.broker_source ("coinbase" -> Coinbase,
    else Robinhood; reconcile_import always excluded; manual excluded unless
    chili_per_broker_count_manual_as_rh). Momentum outcomes split by the
    session's execution_family (same proven join global_realized_pnl_today_et uses).
    """
    from datetime import timedelta as _td
    from zoneinfo import ZoneInfo

    from sqlalchemy import func as sa_func

    from ...models.trading import (
        MomentumAutomationOutcome,
        Trade,
        TradingAutomationSession as _TAS,
    )

    et = ZoneInfo("America/New_York")
    now_et = datetime.now(et)
    start_et = now_et.replace(hour=0, minute=0, second=0, microsecond=0)
    end_et = start_et + _td(days=1)
    start_utc = start_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)
    end_utc = end_et.astimezone(ZoneInfo("UTC")).replace(tzinfo=None)

    out: dict[str, float] = {fam: 0.0 for fam in REAL_DAILY_LOSS_FAMILIES}

    # (A) Autotrader Trade rows, bucketed by broker_source.
    count_manual = bool(getattr(settings, "chili_per_broker_count_manual_as_rh", False))
    tq = db.query(Trade).filter(
        Trade.status == "closed",
        Trade.exit_date.isnot(None),
        Trade.exit_date >= start_utc,
        Trade.exit_date < end_utc,
    )
    if user_id is not None:
        tq = tq.filter(Trade.user_id == user_id)
    for t in tq.all():
        src = (getattr(t, "broker_source", None) or "").lower()
        if src == "reconcile_import":
            continue
        if src == "manual" and not count_manual:
            continue
        pnl = _trade_realized_pnl_with_raw_fallback(t)
        if pnl is None:
            continue
        out["coinbase_spot" if src == "coinbase" else "robinhood_spot"] += pnl

    # (B) Momentum outcomes, grouped by the session's execution_family.
    mq = db.query(
        _TAS.execution_family,
        sa_func.coalesce(sa_func.sum(MomentumAutomationOutcome.realized_pnl_usd), 0.0),
    ).join(
        _TAS, _TAS.id == MomentumAutomationOutcome.session_id
    ).filter(
        MomentumAutomationOutcome.terminal_at >= start_utc,
        MomentumAutomationOutcome.terminal_at < end_utc,
        _TAS.execution_family != "alpaca_spot",
    )
    if user_id is not None:
        mq = mq.filter(MomentumAutomationOutcome.user_id == user_id)
    for ef, total in mq.group_by(_TAS.execution_family).all():
        # Attribute each session to its OWN rail so the active agentic account's PnL is
        # NOT mis-booked onto the drained legacy robinhood_spot (the -$4.72 BLZE false-HALT
        # cause, 2026-06-25). _normalize_real_family keeps robinhood_agentic_mcp first-class
        # (in REAL_DAILY_LOSS_FAMILIES) and folds unknown/blank to robinhood_spot.
        fam = _normalize_real_family(ef)
        out[fam] += float(total or 0.0)

    return out


def per_broker_daily_loss_cap_usd(family: str) -> tuple[float, str]:
    """(positive_cap_usd, source) for ONE broker's daily-loss cap.

    RISK basis = the broker's account CASH VALUE / total equity (operator 2026-06-25:
    base the daily-loss cap on cash value, NOT buying power). For robinhood_agentic_mcp
    that is the stable total account value (_agentic_equity_cached ~$13.6k -> 5% = ~$680);
    for robinhood_spot / coinbase it is pf["equity"]. So pass prefer_cash_value=True, which
    forces the stabilized total-equity path (never BP, never the 2x-margin sizing number)
    routed through the last-good guard so a flaky read cannot collapse the cap to ~$1
    (the documented failure mode, risk_policy.py:264-266). SIZING is unchanged (it keeps
    apply_margin_multiple=True / buying-power basis elsewhere). pct reuses the existing
    chili_global_max_daily_loss_pct_of_equity knob (no new magic number); conservative-wins
    with the optional usd cap. Fail-CLOSED to a documented floor when cash value is
    unavailable (Hard Rule #2: never an uncapped path).
    """
    from .momentum_neural.risk_policy import _account_equity_usd

    pct = float(getattr(settings, "chili_global_max_daily_loss_pct_of_equity", 0.0) or 0.0)
    usd_cap = float(getattr(settings, "chili_global_max_daily_loss_usd", 0.0) or 0.0)
    eq = _account_equity_usd(family, prefer_cash_value=True)

    candidates: list[tuple[float, str]] = []
    if usd_cap > 0:
        candidates.append((usd_cap, "usd"))
    if pct > 0 and eq is not None and float(eq) > 0:
        candidates.append((pct * float(eq), "pct_cash_value"))
    if not candidates:
        floor = float(getattr(settings, "chili_global_daily_loss_failsafe_usd", 300.0) or 300.0)
        return floor, "usd_failsafe"
    return min(candidates, key=lambda kv: kv[0])


def _normalize_real_family(family: str | None) -> str:
    """Resolve to one of REAL_DAILY_LOSS_FAMILIES; default robinhood_spot.

    robinhood_agentic_mcp is preserved as its own family (it is in
    REAL_DAILY_LOSS_FAMILIES) so the active agentic rail caps + accounts off its
    OWN ~$13.6k account, NOT the drained legacy robinhood_spot. We do NOT rely on
    normalize_execution_family's None default (coinbase_spot — the very bug we are
    fixing); an unknown/blank broker for a daily-loss cap is safer attributed to the
    larger equities account.
    """
    from .execution_family_registry import normalize_execution_family

    if not family:
        return "robinhood_spot"
    fam = normalize_execution_family(family)
    return fam if fam in REAL_DAILY_LOSS_FAMILIES else "robinhood_spot"


def clear_stale_broker_daily_loss_blocks() -> None:
    """Drop per-broker blocks whose ET day has rolled (a daily cap self-clears)."""
    today = _et_today_date()
    cleared: list[str] = []
    with _per_broker_lock:
        for fam in list(_per_broker_daily_loss.keys()):
            if _per_broker_daily_loss[fam].get("et_date") != today:
                _per_broker_daily_loss.pop(fam, None)
                cleared.append(fam)
    for fam in cleared:
        logger.info("[governance] per-broker daily-loss block auto-cleared at ET roll: %s", fam)


def set_broker_daily_loss_block(family: str, *, reason: str, realized: float, limit: float) -> None:
    """Mark ONE broker daily-loss-blocked for today (sticky until ET roll). Loud."""
    fam = _normalize_real_family(family)
    with _per_broker_lock:
        already = fam in _per_broker_daily_loss
        _per_broker_daily_loss[fam] = {
            "reason": reason,
            "et_date": _et_today_date(),
            "realized": float(realized),
            "limit": float(limit),
            "set_at": datetime.utcnow(),
        }
    if not already:
        logger.warning(
            "[governance] PER-BROKER DAILY-LOSS BLOCK %s realized=%.2f limit=-%.2f (%s) — "
            "new %s entries halted for the ET day; exits + the OTHER broker stay live",
            fam, realized, limit, reason, fam,
        )


def is_broker_daily_loss_blocked(family: str) -> bool:
    """True if THIS broker is sticky-blocked for today (registry read).

    Consults chili_per_broker_daily_loss_enabled FIRST: when the feature is OFF the
    gate must NOT block, even if a STALE in-memory block lingers from when it was ON
    (2026-06-25: a stale block showed "HALTED" while the flag was already False —
    lane_health surfaced a freeze the lane wasn't actually enforcing). Mirrors the
    activating reader (broker_daily_loss_breached) which already early-returns on the flag.
    """
    if not bool(getattr(settings, "chili_per_broker_daily_loss_enabled", True)):
        return False
    clear_stale_broker_daily_loss_blocks()
    fam = _normalize_real_family(family)
    with _per_broker_lock:
        return fam in _per_broker_daily_loss


def get_broker_daily_loss_block(family: str) -> dict[str, Any] | None:
    """The sticky per-broker block entry for THIS broker, or None when not blocked.

    A cheap, read-only view of the per-broker registry (reason / set_at / realized /
    limit / et_date) so callers (e.g. the lane-health freeze alert) can render WHY a
    broker is frozen and HOW LONG without reaching into private module state.
    """
    clear_stale_broker_daily_loss_blocks()
    fam = _normalize_real_family(family)
    with _per_broker_lock:
        blk = _per_broker_daily_loss.get(fam)
        return dict(blk) if blk else None


def broker_daily_loss_breached(
    db: Session, family: str, *, user_id: int | None = None
) -> tuple[bool, dict[str, Any]]:
    """Authoritative per-broker daily-loss gate (self-healing + sticky).

    Returns (blocked, info). Sticky: once a broker breaches today it stays
    blocked until ET roll (mirrors the old once-per-day cap semantics — a late
    winning exit does not re-open the budget). Recomputes from live DB PnL when
    not yet blocked, and sets the sticky block on first breach so ANY gate that
    notices a breach protects the broker without relying on the monitor pass.
    """
    fam = _normalize_real_family(family)
    # Honor the operator's per-broker disable flag. When OFF, this gate must NOT block —
    # the momentum lane's equity-relative daily-loss cap (off the REAL account equity) +
    # the global failsafe remain the daily breakers. Bug 2026-06-23: the agentic lane
    # normalizes to robinhood_spot, whose tiny $38.92 buying power yields a ~$0.97 cap
    # that HALTED the lane on a -$17 loss — while this flag was ALREADY False but never
    # consulted here. Returning early also bypasses any in-memory sticky block.
    if not bool(getattr(settings, "chili_per_broker_daily_loss_enabled", True)):
        return False, {"family": fam, "disabled": True, "sticky": False}
    if is_broker_daily_loss_blocked(fam):
        with _per_broker_lock:
            blk = dict(_per_broker_daily_loss.get(fam, {}))
        return True, {"family": fam, "sticky": True, **blk}
    by_broker = realized_pnl_today_by_broker(db, user_id)
    realized = float(by_broker.get(fam, 0.0))
    cap, src = per_broker_daily_loss_cap_usd(fam)
    breached = (realized <= -cap) if cap > 0 else (realized < 0.0)
    info = {"family": fam, "realized": realized, "cap": cap, "source": src, "sticky": False}
    if breached:
        set_broker_daily_loss_block(
            fam,
            reason=f"broker_daily_loss_breach_{fam}_{src}_${cap:.0f}",
            realized=realized,
            limit=cap,
        )
    return breached, info


def check_per_broker_daily_loss(
    db: Session, *, user_id: int | None = None, activate: bool = True
) -> dict[str, Any]:
    """Per-broker replacement for check_daily_loss_breach. Evaluates EVERY broker
    (so the monitor pass blocks any breached broker) and applies a GLOBAL backstop:
    if the AGGREGATE loss exceeds the sum of per-broker caps (x mult), the TRUE
    global kill switch trips (a real catastrophic-total halt). Per-broker breaches
    never touch the global flag (exits stay live)."""
    results: dict[str, Any] = {}
    agg_realized = 0.0
    agg_cap = 0.0
    for fam in REAL_DAILY_LOSS_FAMILIES:
        breached, info = (
            broker_daily_loss_breached(db, fam, user_id=user_id)
            if activate
            else _peek_broker_breach(db, fam, user_id=user_id)
        )
        results[fam] = {**info, "breached": breached}
        agg_realized += float(info.get("realized", 0.0) or 0.0)
        agg_cap += float(info.get("cap", 0.0) or 0.0)
    mult = float(getattr(settings, "chili_per_broker_aggregate_backstop_mult", 1.0) or 1.0)
    backstop = agg_cap * max(1.0, mult)
    if activate and backstop > 0 and agg_realized <= -backstop and not is_kill_switch_active():
        activate_kill_switch(f"global_daily_loss_breach_backstop_${backstop:.0f}")
        logger.critical(
            "[governance] AGGREGATE daily-loss backstop (realized=%.2f limit=-%.2f) — global kill switch",
            agg_realized, backstop,
        )
    return {"by_broker": results, "aggregate_realized": agg_realized, "aggregate_cap": agg_cap, "backstop": backstop}


def _peek_broker_breach(
    db: Session, family: str, *, user_id: int | None = None
) -> tuple[bool, dict[str, Any]]:
    """Non-activating read of a broker's breach (for status/alerts)."""
    fam = _normalize_real_family(family)
    if not bool(getattr(settings, "chili_per_broker_daily_loss_enabled", True)):
        return False, {"family": fam, "disabled": True, "sticky": False}
    if is_broker_daily_loss_blocked(fam):
        with _per_broker_lock:
            blk = dict(_per_broker_daily_loss.get(fam, {}))
        return True, {"family": fam, "sticky": True, **blk}
    by_broker = realized_pnl_today_by_broker(db, user_id)
    realized = float(by_broker.get(fam, 0.0))
    cap, src = per_broker_daily_loss_cap_usd(fam)
    breached = (realized <= -cap) if cap > 0 else (realized < 0.0)
    return breached, {"family": fam, "realized": realized, "cap": cap, "source": src, "sticky": False}


def _kill_switch_halts_exits() -> bool:
    """Whether the CURRENT global kill-switch reason should also halt EXITS.

    A daily-loss breach (legacy global OR the per-broker aggregate backstop) must
    stop NEW entries but NEVER strand an open position — you always manage out of
    risk. Manual / emergency / price-monitor reasons DO halt exits. Callers that
    gate exits should use `is_kill_switch_active() and _kill_switch_halts_exits()`.
    """
    with _kill_switch_lock:
        if not _kill_switch:
            return False
        reason = _kill_switch_reason or ""
    return not reason.startswith("global_daily_loss_breach")


def kill_switch_halts_new_entries() -> bool:
    """Whether the current global kill-switch state should halt NEW ENTRIES for
    ALL brokers/lanes. True for manual / emergency / price-monitor / DB-fail-closed
    AND the catastrophic aggregate BACKSTOP. When per-broker daily-loss is enabled,
    a LEGACY single-global daily-loss breach (pct/usd — NOT the backstop) is handled
    per broker instead, so this returns False for that reason (the per-broker gate
    does the blocking; a Coinbase-sized global breach no longer freezes Robinhood).
    """
    if not is_kill_switch_active():
        return False
    with _kill_switch_lock:
        reason = _kill_switch_reason or ""
    per_broker = bool(getattr(settings, "chili_per_broker_daily_loss_enabled", True))
    if per_broker and reason.startswith("global_daily_loss_breach") and "backstop" not in reason:
        return False
    return True


# ── Trade Velocity Limits ─────────────────────────────────────────────

_trade_timestamps: list[float] = []
_MAX_TRADES_PER_HOUR = 10
_MAX_TRADES_PER_DAY = 30


def check_velocity(
    max_per_hour: int = _MAX_TRADES_PER_HOUR,
    max_per_day: int = _MAX_TRADES_PER_DAY,
) -> dict[str, Any]:
    """Check if trade velocity is within limits."""
    import time
    now = time.time()

    hour_ago = now - 3600
    day_ago = now - 86400

    with _approval_lock:
        _trade_timestamps[:] = [t for t in _trade_timestamps if t > day_ago]
        hourly = sum(1 for t in _trade_timestamps if t > hour_ago)
        daily = len(_trade_timestamps)

    if hourly >= max_per_hour:
        return {"allowed": False, "reason": f"hourly limit ({hourly}/{max_per_hour})", "hourly": hourly, "daily": daily}
    if daily >= max_per_day:
        return {"allowed": False, "reason": f"daily limit ({daily}/{max_per_day})", "hourly": hourly, "daily": daily}

    return {"allowed": True, "hourly": hourly, "daily": daily}


def record_trade_executed() -> None:
    """Record that a trade was executed for velocity tracking."""
    import time
    with _approval_lock:
        _trade_timestamps.append(time.time())


# ── Pattern-to-Live Approval ─────────────────────────────────────────

def request_pattern_to_live(
    db: Session,
    pattern_id: int,
    *,
    auto_approve_paper_profitable: bool = True,
) -> dict[str, Any]:
    """Request approval to promote a paper-tested pattern to live trading.

    If auto_approve_paper_profitable is True and the pattern has profitable
    paper trades, it is auto-approved.
    """
    from ...models.trading import ScanPattern, PaperTrade
    from .portfolio_allocator import build_pattern_allocation_state

    pattern = db.query(ScanPattern).filter(ScanPattern.id == pattern_id).first()
    if not pattern:
        return {"approved": False, "reason": "pattern_not_found"}

    if pattern.lifecycle_stage == "challenged":
        return {
            "approved": False,
            "reason": "pattern lifecycle is challenged (research / weak-null gate); not eligible for live request",
        }
    if pattern.lifecycle_stage != "promoted":
        return {
            "approved": False,
            "reason": (
                f"pattern lifecycle is {pattern.lifecycle_stage}; "
                "live-readiness path requires promoted"
            ),
        }

    allocation = build_pattern_allocation_state(
        db,
        pattern,
        user_id=getattr(pattern, "user_id", None),
        context="pattern_to_live",
    )
    if (
        not allocation.get("allowed_if_enforced", True)
        and bool(getattr(settings, "brain_allocator_live_hard_block_enabled", False))
    ):
        db.commit()
        return {"approved": False, "reason": allocation.get("blocked_reason") or "allocator_blocked", "allocation": allocation}

    baseline_allow: bool | None = None
    auto_reason: str | None = None
    if auto_approve_paper_profitable:
        paper_trades = db.query(PaperTrade).filter(
            PaperTrade.scan_pattern_id == pattern_id,
            PaperTrade.status == "closed",
        ).all()
        if len(paper_trades) >= 3:
            win_flags = [
                win
                for win in (_paper_directional_win(t) for t in paper_trades)
                if win is not None
            ]
            wins = sum(1 for win in win_flags if win)
            wr = wins / len(win_flags) * 100 if win_flags else 0.0
            paper_pnls = [
                pnl
                for pnl in (_paper_realized_pnl_with_raw_fallback(t) for t in paper_trades)
                if pnl is not None
            ]
            total_pnl = sum(paper_pnls)
            if wr >= 50 and total_pnl > 0:
                baseline_allow = True
                auto_reason = f"Paper profitable: {wr:.0f}% WR, ${total_pnl:.2f} P&L ({len(paper_trades)} trades)"

    # Phase M.2.b — promotion gate consumer. Shadow mode always
    # defers to baseline; authoritative (with approval) can convert
    # a baseline allow into a block. Never upgrades a baseline
    # block. Never raises.
    consumer_result = None
    try:
        from . import pattern_regime_promotion_service as _promo
        if _promo.mode_is_active():
            consumer_result = _promo.evaluate_promotion_for_pattern(
                db,
                pattern_id=int(pattern_id),
                baseline_allow=baseline_allow,
                source="request_pattern_to_live",
            )
    except Exception:
        consumer_result = None

    consumer_blocked_allow = bool(
        consumer_result is not None
        and consumer_result.applied
        and baseline_allow is True
        and consumer_result.consumer_allow is False
    )

    if baseline_allow is True and not consumer_blocked_allow:
        from .lifecycle import transition_to_live
        try:
            transition_to_live(db, pattern)
            db.commit()
            return {
                "approved": True,
                "auto": True,
                "reason": auto_reason,
                "allocation": allocation,
            }
        except Exception as e:
            return {"approved": False, "reason": f"lifecycle transition failed: {e}"}

    if consumer_blocked_allow:
        return {
            "approved": False,
            "reason": (
                "pattern_regime_promotion gate blocked auto-approve "
                f"(reason={consumer_result.reason_code})"
            ),
            "allocation": allocation,
            "pattern_regime_promotion": {
                "applied": True,
                "reason_code": consumer_result.reason_code,
                "evaluation_id": consumer_result.evaluation_id,
            },
        }

    out = submit_for_approval("pattern_to_live", {
        "pattern_id": pattern_id,
        "pattern_name": pattern.name,
        "lifecycle_stage": pattern.lifecycle_stage,
        "confidence": pattern.confidence,
        "oos_win_rate": pattern.oos_win_rate,
    })
    out["allocation"] = allocation
    if consumer_result is not None:
        out["pattern_regime_promotion"] = {
            "applied": bool(consumer_result.applied),
            "consumer_allow": bool(consumer_result.consumer_allow),
            "reason_code": consumer_result.reason_code,
            "evaluation_id": consumer_result.evaluation_id,
        }
    return out


def get_governance_dashboard() -> dict[str, Any]:
    """Full governance status for the UI."""
    return {
        "ok": True,
        "kill_switch": get_kill_switch_status(),
        "pending_approvals": len(get_pending_approvals()),
        "approvals": get_pending_approvals()[:10],
        "velocity": check_velocity(),
    }
