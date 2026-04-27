"""Shared Autopilot desk / monitor eligibility helpers + P0.4 mutual-exclusion lease.

The lease is a *schema-based* lease: we don't persist a separate "who owns this symbol"
row. Instead, the authoritative signals for ownership are the rows we already write:

* AutoTrader v1 owns symbol S for user U iff: exists Trade with
  ticker=S, user_id=U, auto_trader_version="v1", status="open".
* momentum_neural owns symbol S for user U iff: exists TradingAutomationSession
  with symbol=S, user_id=U, mode="live", state IN LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY.

This keeps the lease state a function of the real trade / session state — there is no
separate lock table that can drift out of sync.

Callers use :func:`check_autopilot_entry_gate` before placing **entry** orders.
Exits / scale-outs / cancels are never gated — once a position is open, the same
autopilot must always be able to close or cancel it.
"""
from __future__ import annotations

from typing import Optional

from sqlalchemy import or_
from sqlalchemy.orm import Session

from ...models.trading import Trade


# ---------------------------------------------------------------------------
# Existing: live Autopilot desk scope helpers.
# ---------------------------------------------------------------------------


def live_autopilot_trade_filter():
    """SQLAlchemy filter for live trades surfaced on the Autopilot desk.

    The desk and live execution monitor should agree on the same scope:

    * AutoTrader v1 rows
    * Pattern-linked rows (scan pattern or breakout alert)
    * AI/manual plan-level rows with a saved stop or target
    """
    return or_(
        Trade.auto_trader_version == "v1",
        Trade.scan_pattern_id.isnot(None),
        Trade.related_alert_id.isnot(None),
        Trade.stop_loss.isnot(None),
        Trade.take_profit.isnot(None),
    )


def is_option_trade(trade: Trade) -> bool:
    """True if this Trade is an options position (vs equity / crypto).

    String-tolerant: trade.indicator_snapshot may be returned as a JSON
    string instead of a dict (legacy / mixed-storage rows). We json.loads
    it before inspecting.
    """
    import json as _json
    try:
        snap = trade.indicator_snapshot
    except Exception:
        return False
    if isinstance(snap, str):
        try:
            snap = _json.loads(snap)
        except Exception:
            return False
    if not isinstance(snap, dict):
        return False
    if snap.get("option_meta"):
        return True
    ba = snap.get("breakout_alert")
    if isinstance(ba, str):
        try:
            ba = _json.loads(ba)
        except Exception:
            ba = None
    if isinstance(ba, dict):
        if (ba.get("asset_type") or "").lower() == "options":
            return True
        if ba.get("option_meta"):
            return True
    return False

def classify_live_autopilot_trade_scope(trade: Trade) -> str:
    """Return the operator-facing scope label for a live trade."""
    if trade.related_alert_id is not None or trade.scan_pattern_id is not None:
        return "pattern_linked"
    if (trade.auto_trader_version or "") == "v1":
        return "autotrader_v1"
    if trade.stop_loss is not None or trade.take_profit is not None:
        return "plan_levels"
    return "other"


def is_live_autopilot_trade(trade: Trade) -> bool:
    return classify_live_autopilot_trade_scope(trade) != "other"


# ---------------------------------------------------------------------------
# P0.4 — Autopilot mutual exclusion (per-symbol lease + primary-path gate).
# ---------------------------------------------------------------------------

AUTOPILOT_AUTO_TRADER_V1 = "auto_trader_v1"
AUTOPILOT_MOMENTUM_NEURAL = "momentum_neural"
KNOWN_AUTOPILOTS = frozenset({AUTOPILOT_AUTO_TRADER_V1, AUTOPILOT_MOMENTUM_NEURAL})


def _normalize_symbol(symbol: Optional[str]) -> str:
    """Canonicalize a symbol for cross-table comparison (uppercase, trimmed)."""
    return (symbol or "").strip().upper()


def _normalize_candidate(candidate: Optional[str]) -> str:
    return (candidate or "").strip().lower()


def get_primary_autopilot() -> str:
    """Return the configured primary autopilot id, or '' if unconfigured/unknown.

    Reads from settings on each call so tests / env overrides are picked up
    without requiring a restart.
    """
    try:
        from app.config import settings  # local import to avoid cycles at import-time
        raw = getattr(settings, "chili_autopilot_primary", "") or ""
    except Exception:
        return ""
    normalized = raw.strip().lower()
    if normalized == AUTOPILOT_MOMENTUM_NEURAL:
        momentum_live_enabled = bool(getattr(settings, "chili_momentum_live_runner_enabled", False))
        autotrader_live_enabled = bool(getattr(settings, "chili_autotrader_live_enabled", False))
        if not momentum_live_enabled and autotrader_live_enabled:
            return AUTOPILOT_AUTO_TRADER_V1
    if normalized in KNOWN_AUTOPILOTS:
        return normalized
    if bool(getattr(settings, "chili_autotrader_live_enabled", False)):
        return AUTOPILOT_AUTO_TRADER_V1
    return ""


def get_strict_primary_mode() -> bool:
    """Return True when strict-primary gating is enabled.

    In strict mode, the *non-primary* autopilot cannot open new entries on a symbol
    even if no autopilot currently owns the symbol. In non-strict mode, the
    non-primary may seed a new entry on a *free* symbol, but is still blocked
    from overlapping an existing foreign owner.
    """
    try:
        from app.config import settings
        return bool(getattr(settings, "chili_autopilot_strict_primary", False))
    except Exception:
        return False


def _count_v1_open_trades(
    db: Session, *, symbol: str, user_id: Optional[int]
) -> int:
    q = db.query(Trade).filter(
        Trade.ticker == symbol,
        Trade.auto_trader_version == "v1",
        Trade.status == "open",
    )
    if user_id is not None:
        q = q.filter(Trade.user_id == user_id)
    return int(q.count())


def _count_momentum_live_sessions(
    db: Session, *, symbol: str, user_id: Optional[int]
) -> int:
    # Local imports to keep a light public surface and avoid import cycles at module load.
    from ...models.trading import TradingAutomationSession
    from .momentum_neural.live_fsm import LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY

    q = db.query(TradingAutomationSession).filter(
        TradingAutomationSession.symbol == symbol,
        TradingAutomationSession.mode == "live",
        TradingAutomationSession.state.in_(LIVE_RUNNER_ACTIVE_FOR_CONCURRENCY),
    )
    if user_id is not None:
        q = q.filter(TradingAutomationSession.user_id == user_id)
    return int(q.count())


def find_symbol_owner(
    db: Session, *, symbol: str, user_id: Optional[int] = None
) -> dict:
    """Identify which autopilot currently owns `symbol` for `user_id`.

    Returns a dict:
      {"owner": "auto_trader_v1" | "momentum_neural" | None,
       "v1_open_trades": int,
       "momentum_live_sessions": int,
       "symbol": normalized_symbol}

    If both paths have live rows for the same symbol (should not normally
    happen — that's what the gate is supposed to prevent), we still report
    a single canonical owner so callers get a deterministic foreign-owner
    label. The primary loses the "owner" slot in a contested pair so the
    non-primary sees itself blocked by the primary.
    """
    sym = _normalize_symbol(symbol)
    if not sym:
        return {
            "owner": None,
            "v1_open_trades": 0,
            "momentum_live_sessions": 0,
            "symbol": "",
        }
    v1 = _count_v1_open_trades(db, symbol=sym, user_id=user_id)
    mm = _count_momentum_live_sessions(db, symbol=sym, user_id=user_id)
    owner: Optional[str] = None
    if v1 > 0 and mm > 0:
        # Contested — tie-break so the non-primary sees the primary as blocker.
        primary = get_primary_autopilot()
        if primary == AUTOPILOT_MOMENTUM_NEURAL:
            owner = AUTOPILOT_MOMENTUM_NEURAL
        elif primary == AUTOPILOT_AUTO_TRADER_V1:
            owner = AUTOPILOT_AUTO_TRADER_V1
        else:
            # No primary configured — tie-break deterministically on momentum.
            owner = AUTOPILOT_MOMENTUM_NEURAL
    elif v1 > 0:
        owner = AUTOPILOT_AUTO_TRADER_V1
    elif mm > 0:
        owner = AUTOPILOT_MOMENTUM_NEURAL
    return {
        "owner": owner,
        "v1_open_trades": v1,
        "momentum_live_sessions": mm,
        "symbol": sym,
    }


def check_autopilot_entry_gate(
    db: Session,
    *,
    candidate: str,
    symbol: str,
    user_id: Optional[int] = None,
) -> dict:
    """Mutual-exclusion gate for autopilot entry placement (P0.4).

    Call this from an autopilot's **entry** code path (new-entry or scale-in)
    before placing an order. Returns a dict:

      {"allowed": bool,
       "reason": str,           # one of: ok | owner_self | not_primary |
                                 #         symbol_owned_by_other |
                                 #         unknown_candidate_allowed
       "owner": str | None,     # current owner (same-self, foreign, or None)
       "primary": str,          # configured primary (may be "")
       "strict": bool,          # strict-primary mode flag
       "symbol": str}

    The caller should short-circuit with structured audit when allowed=False.

    ⚠ This function *reads* state only; acquiring the lease happens naturally
    when the caller writes the corresponding Trade / session row.
    """
    candidate_n = _normalize_candidate(candidate)
    primary = get_primary_autopilot()
    strict = get_strict_primary_mode()

    own = find_symbol_owner(db, symbol=symbol, user_id=user_id)
    owner = own["owner"]
    sym = own["symbol"]

    base = {
        "owner": owner,
        "primary": primary,
        "strict": strict,
        "symbol": sym,
    }

    # Defensive: unknown candidate names don't get blocked (the gate is opt-in).
    if candidate_n not in KNOWN_AUTOPILOTS:
        return {"allowed": True, "reason": "unknown_candidate_allowed", **base}

    # Foreign owner — block unconditionally, regardless of primary config.
    if owner is not None and owner != candidate_n:
        return {"allowed": False, "reason": "symbol_owned_by_other", **base}

    # Self-owner — re-entry / scale-in allowed.
    if owner == candidate_n:
        return {"allowed": True, "reason": "owner_self", **base}

    # No owner. In stric