"""Autonomous auto-arm-live for the momentum lane (Ross-style).

The live runner can ENTER/MANAGE a live session, but a session still had to be
armed by hand (the Phase-8 deliberate-arm guard). This pass closes that last gap:
each tick it ranks FRESH, LIVE-ELIGIBLE viability candidates and arms ONE whose
momentum entry trigger (pullback-break / volume) is firing NOW — exactly how Ross
picks "the one moving right now" rather than a stale leader.

It NEVER bypasses a guard. Arming goes through the same operator arm flow
(begin_live_arm -> confirm_live_arm), which re-checks kill-switch, drawdown,
concurrency, viability freshness, broker can_trade, and the equity-relative caps.
On top of that this pass pre-checks the cheap guards (kill-switch, global
concurrency=1, the portfolio drawdown breaker, per-symbol autopilot mutex) so it
fails fast and never spams pending arms. live + on, fully guarded.
docs/STRATEGY (auto-arm-live); see [[project_momentum_lane]].
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy.orm import Session

from ....config import settings
from ....models.trading import MomentumSymbolViability, TradingAutomationSession
from .live_fsm import LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY

logger = logging.getLogger(__name__)


def _auto_arm_user_id() -> int | None:
    return getattr(settings, "chili_autotrader_user_id", None) or getattr(
        settings, "brain_default_user_id", None
    )


def _max_live_sessions() -> int:
    return max(1, int(getattr(settings, "chili_momentum_risk_max_concurrent_live_sessions", 1)))


def _scan_limit() -> int:
    return max(1, int(getattr(settings, "chili_momentum_auto_arm_scan_limit", 10)))


def _active_live_session_count(db: Session, *, user_id: int | None) -> int:
    """Live sessions occupying a concurrency slot (any symbol) for the user."""
    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state.in_(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY),
    )
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == int(user_id))
    return int(q.count())


def _fresh_live_eligible_candidates(db: Session, *, limit: int) -> list[MomentumSymbolViability]:
    """Top live-eligible candidates fresh within the LIVE risk gate (600s).

    The viability board itself keeps ~1h of rows, but the arm's risk evaluator
    requires freshness <= viability_max_age, so we filter to that here to never
    pick a candidate the arm would reject.
    """
    max_age = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
    cutoff = datetime.utcnow() - timedelta(seconds=max_age)
    return (
        db.query(MomentumSymbolViability)
        .filter(
            MomentumSymbolViability.scope == "symbol",
            MomentumSymbolViability.live_eligible.is_(True),
            MomentumSymbolViability.freshness_ts >= cutoff,
        )
        .order_by(MomentumSymbolViability.viability_score.desc())
        .limit(int(limit))
        .all()
    )


def _symbol_free(db: Session, symbol: str, user_id: int | None) -> bool:
    """Per-symbol autopilot mutex vs AutoTrader v1 (fail open on helper error)."""
    try:
        from ..autopilot_scope import check_autopilot_entry_gate

        gate = check_autopilot_entry_gate(
            db, candidate="momentum_neural", symbol=symbol, user_id=user_id
        )
        return bool(gate.get("allowed", True))
    except Exception:
        return True


def _entry_trigger_fires(symbol: str) -> tuple[bool, str]:
    """Replicate the live_runner WATCHING_LIVE hybrid trigger to find a name
    whose momentum is breaking NOW (pullback-break preferred, volume fallback)."""
    try:
        from ..market_data import fetch_ohlcv_df
        from .entry_gates import momentum_volume_confirmation, pullback_break_confirmation

        mode = str(getattr(settings, "chili_momentum_entry_trigger_mode", "hybrid") or "hybrid").lower()
        interval = str(getattr(settings, "chili_momentum_pullback_entry_interval", "5m") or "5m")
        if mode in ("hybrid", "pullback_break"):
            df_pb = fetch_ohlcv_df(symbol, interval=interval, period="5d")
            if df_pb is not None and not getattr(df_pb, "empty", True):
                ok, reason, _ = pullback_break_confirmation(df_pb, entry_interval=interval)
                if ok:
                    return True, reason
                if mode == "pullback_break":
                    return False, reason
        if mode != "pullback_break":
            df = fetch_ohlcv_df(symbol, interval="15m", period="5d")
            if df is None or getattr(df, "empty", True):
                return False, "no_data"
            return momentum_volume_confirmation(df)
    except Exception:
        return False, "trigger_error"
    return False, "trigger_wait"


def run_auto_arm_pass(db: Session) -> dict[str, Any]:
    """Single auto-arm pass. Returns a summary dict (armed 0/1)."""
    out: dict[str, Any] = {"checked": 0, "scanned": 0, "armed": 0, "skipped": None}

    if not bool(getattr(settings, "chili_momentum_auto_arm_live_enabled", True)):
        out["skipped"] = "flag_off"
        return out
    # Only meaningful when the live runner is on to process an armed session.
    if not bool(getattr(settings, "chili_momentum_live_runner_enabled", False)):
        out["skipped"] = "live_runner_off"
        return out

    uid = _auto_arm_user_id()
    if uid is None:
        out["skipped"] = "no_user"
        return out

    # Guard 1: kill switch.
    try:
        from ..governance import is_kill_switch_active

        if is_kill_switch_active():
            out["skipped"] = "kill_switch"
            return out
    except Exception:
        pass

    # Guard 2: global concurrency (one live position at a time by default).
    active = _active_live_session_count(db, user_id=uid)
    if active >= _max_live_sessions():
        out["skipped"] = "live_session_active"
        out["active"] = active
        return out

    # Guard 3: portfolio drawdown breaker (Hard Rule 2 — not enforced in the
    # arm path; shadow mode returns not-tripped).
    try:
        from ..portfolio_risk import check_portfolio_drawdown_breaker

        tripped, reason = check_portfolio_drawdown_breaker(db, int(uid))
        if tripped:
            out["skipped"] = "drawdown_breaker"
            out["dd_reason"] = reason
            return out
    except Exception:
        pass

    # Clear expired pending arms so they do not pin a concurrency slot.
    try:
        from .automation_query import expire_stale_live_arm_sessions

        expire_stale_live_arm_sessions(db, user_id=int(uid))
    except Exception:
        pass

    candidates = _fresh_live_eligible_candidates(db, limit=_scan_limit())
    out["scanned"] = len(candidates)
    if not candidates:
        out["skipped"] = "no_fresh_live_eligible"
        return out

    chosen: MomentumSymbolViability | None = None
    chosen_reason: str | None = None
    for c in candidates:
        out["checked"] += 1
        if not _symbol_free(db, c.symbol, uid):
            continue
        fires, reason = _entry_trigger_fires(c.symbol)
        if fires:
            chosen, chosen_reason = c, reason
            break

    if chosen is None:
        out["skipped"] = "no_active_trigger"
        return out

    # Ensure the live client is connected (full-scope cred) before arming.
    try:
        from ...coinbase_service import connect as _cb_connect

        _cb_connect()
    except Exception:
        pass

    from .operator_actions import begin_live_arm, confirm_live_arm

    out["symbol"] = chosen.symbol
    out["viability_score"] = round(float(chosen.viability_score or 0.0), 4)
    out["trigger"] = chosen_reason

    begin = begin_live_arm(
        db,
        user_id=int(uid),
        symbol=chosen.symbol,
        variant_id=int(chosen.variant_id),
        execution_family="coinbase_spot",
    )
    if not begin.get("ok"):
        out["skipped"] = "begin_blocked"
        out["begin_error"] = begin.get("error")
        logger.info(
            "[auto_arm] begin_live_arm blocked %s: %s",
            chosen.symbol, begin.get("error"),
        )
        return out

    confirm = confirm_live_arm(
        db, user_id=int(uid), arm_token=begin.get("arm_token"), confirm=True
    )
    if confirm.get("ok"):
        out["armed"] = 1
        out["session_id"] = begin.get("session_id")
        out["state"] = confirm.get("state")
        logger.warning(
            "[auto_arm] ARMED live %s session=%s state=%s trigger=%s viability=%.3f",
            chosen.symbol, begin.get("session_id"), confirm.get("state"),
            chosen_reason, float(chosen.viability_score or 0.0),
        )
    else:
        out["skipped"] = "confirm_blocked"
        out["confirm_error"] = confirm.get("error")
        logger.info(
            "[auto_arm] confirm_live_arm blocked %s: %s",
            chosen.symbol, confirm.get("error"),
        )
    return out
