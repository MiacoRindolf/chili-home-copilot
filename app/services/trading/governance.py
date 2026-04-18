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
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ...config import settings

logger = logging.getLogger(__name__)

# ── Kill Switch ───────────────────────────────────────────────────────

_kill_switch = False
_kill_switch_reason: str | None = None
_kill_switch_lock = threading.Lock()


def activate_kill_switch(reason: str = "manual") -> None:
    """Immediately halt all trading activity. Persists to DB."""
    global _kill_switch, _kill_switch_reason
    with _kill_switch_lock:
        _kill_switch = True
        _kill_switch_reason = reason
    _persist_kill_switch_state(True, reason)
    logger.critical("[governance] KILL SWITCH ACTIVATED: %s", reason)


def deactivate_kill_switch() -> None:
    """Re-enable trading activity. Persists to DB."""
    global _kill_switch, _kill_switch_reason
    with _kill_switch_lock:
        _kill_switch = False
        _kill_switch_reason = None
    _persist_kill_switch_state(False, None)
    logger.info("[governance] Kill switch deactivated")


def is_kill_switch_active() -> bool:
    with _kill_switch_lock:
        return _kill_switch


def get_kill_switch_status() -> dict[str, Any]:
    with _kill_switch_lock:
        return {"active": _kill_switch, "reason": _kill_switch_reason}


def _persist_kill_switch_state(active: bool, reason: str | None) -> None:
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
        finally:
            sess.close()
    except Exception:
        logger.debug("[governance] Failed to persist kill-switch state to DB", exc_info=True)


def restore_kill_switch_from_db() -> None:
    """Restore kill-switch state from DB on startup."""
    global _kill_switch, _kill_switch_reason
    try:
        from ...db import SessionLocal
        from sqlalchemy import text
        sess = SessionLocal()
        try:
            row = sess.execute(text(
                "SELECT breaker_tripped, breaker_reason FROM trading_risk_state "
                "WHERE regime = 'kill_switch' ORDER BY created_at DESC LIMIT 1"
            )).fetchone()
            if row and row[0]:
                with _kill_switch_lock:
                    _kill_switch = True
                    _kill_switch_reason = row[1] or "restored from DB"
                logger.warning("[governance] Kill switch restored from DB: %s", _kill_switch_reason)
        finally:
            sess.close()
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
        if t.pnl is not None:
            trade_total += float(t.pnl)

    # Momentum automation outcomes
    mq = db.query(
        sa_func.coalesce(sa_func.sum(MomentumAutomationOutcome.realized_pnl_usd), 0.0)
    ).filter(
        MomentumAutomationOutcome.terminal_at >= start_utc,
        MomentumAutomationOutcome.terminal_at < end_utc,
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

    candidates: list[tuple[float, str]] = []
    if usd_cap > 0:
        candidates.append((usd_cap, "usd"))
    if pct_cap > 0 and equity_usd is not None and float(equity_usd) > 0:
        candidates.append((pct_cap * float(equity_usd), "pct_equity"))

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
            wins = sum(1 for t in paper_trades if (t.pnl or 0) > 0)
            wr = wins / len(paper_trades) * 100
            total_pnl = sum(t.pnl or 0 for t in paper_trades)
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
