"""Alpaca PAPER position/order reconciler — flatten ORPHANS no session manages.

WHY (2026-07-09 audit): the sub-penny reject storm (fixed by ``_equity_limit_price``)
made EXIT submissions bounce while their sessions terminalized via live_error /
live_cancelled — stranding SIX positions (~$65k MV; RKTO 20,815 sh, −$1,249 unrealized)
plus a stale resting buy on the paper account with NO managing session. The broker-sync
loop covers RH/Coinbase only, so the orphans persisted silently and consumed buying
power ($399k → $66k), starving new entries. Same failure class as the RH dup-Reference
orphan (#854 reconcile-not-terminalize) — this is the Alpaca-side guard.

WHAT (each scheduler pass, ~120s): compare the ACTUAL Alpaca paper account against the
DB's alpaca-family sessions:
  * ORPHAN POSITION   = a LONG EQUITY position whose symbol has (a) NO non-terminal
    alpaca-family session, (b) NO alpaca-family session created inside the grace
    window, and (c) NO outcome terminalized inside the grace window → FLATTEN
    (market sell — the sub-penny rule cannot reject a market order).
  * ORPHAN OPEN ORDER = a resting order whose symbol clears the same three checks
    → CANCEL.

SAFETY:
  * PAPER-ONLY BY CONSTRUCTION — hard-gated on ``chili_alpaca_paper`` (this never runs
    against a real-money account; extending to live requires a deliberate code change).
  * FLATTEN-ONLY — sells an existing long / cancels a resting order; never opens,
    adds, or shorts. Crypto + short positions are OUT OF SCOPE (skipped).
  * FAIL-OPEN — an unreadable account/positions/session read ⇒ NO action this pass.
  * Grace window (one documented knob) absorbs races with just-created sessions and
    just-terminalized exits still settling.
  * Per-pass action cap; idempotent per-minute client_order_id.
  * Default-ON with a kill-switch (no dark flags). CHILI automation places the
    orders — the same authority as every other lane order.
(ALPACA_PAPER_ENABLE_PLAN.md)
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from ....config import settings
from .operator_actions import _TERMINAL_OPERATOR_STATES

logger = logging.getLogger(__name__)

_ALPACA_FAMILIES = ("alpaca_spot", "alpaca_short")

# Per-symbol attempt memory (process-local): a stuck symbol (halt, reject loop) is
# re-attempted at most once per grace window, not every 120s pass — bounds repeat-fire
# over TIME (adversarial review lens 2) on top of the per-pass action cap.
_LAST_ATTEMPT: dict[str, float] = {}


def _grace_minutes() -> float:
    try:
        return max(1.0, float(getattr(settings, "chili_momentum_alpaca_orphan_grace_minutes", 15.0) or 15.0))
    except (TypeError, ValueError):
        return 15.0


def _managed_and_recent_symbols(db: Session) -> tuple[set[str], set[str]] | None:
    """(active_symbols, recent_symbols) for the alpaca families — or None on a read
    error (callers FAIL-OPEN: no reconcile action without a trustworthy DB view).

    active = any NON-terminal session (any age): the symbol is owned; hands off.
    recent = any session CREATED inside the grace window OR any outcome TERMINALIZED
    inside it: a race-guard for fills/exits still settling."""
    try:
        grace = _grace_minutes()
        rows = db.execute(text(
            "SELECT upper(symbol) AS s, state, "
            "       (created_at > (now() at time zone 'utc') - (:g * interval '1 minute')) AS is_recent "
            "FROM trading_automation_sessions "
            "WHERE execution_family = ANY(:fams)"
        ), {"fams": list(_ALPACA_FAMILIES), "g": grace}).fetchall()
        active: set[str] = set()
        recent: set[str] = set()
        for s, state, is_recent in rows:
            if str(state or "") not in _TERMINAL_OPERATOR_STATES:
                active.add(s)
            if bool(is_recent):
                recent.add(s)
        orows = db.execute(text(
            "SELECT upper(symbol) FROM momentum_automation_outcomes "
            "WHERE execution_family = ANY(:fams) "
            "  AND terminal_at > (now() at time zone 'utc') - (:g * interval '1 minute')"
        ), {"fams": list(_ALPACA_FAMILIES), "g": grace}).fetchall()
        recent.update(r[0] for r in orows)
        return active, recent
    except Exception:
        logger.warning("[alpaca_reconcile] session/outcome read failed — fail-open (no action)", exc_info=True)
        return None


def _latest_session_id(db: Session, symbol: str) -> int | None:
    """Most recent alpaca-family session for the symbol (audit-event anchor)."""
    try:
        row = db.execute(text(
            "SELECT id FROM trading_automation_sessions "
            "WHERE upper(symbol) = :s AND execution_family = ANY(:fams) "
            "ORDER BY created_at DESC LIMIT 1"
        ), {"s": symbol, "fams": list(_ALPACA_FAMILIES)}).fetchone()
        return int(row[0]) if row else None
    except Exception:
        return None


def _audit(db: Session, symbol: str, payload: dict[str, Any]) -> None:
    """Best-effort audit row on the symbol's latest session (skipped when none)."""
    sid = _latest_session_id(db, symbol)
    if sid is None:
        return
    try:
        import json

        db.execute(text(
            "INSERT INTO trading_automation_events (session_id, ts, event_type, payload_json) "
            "VALUES (:sid, (now() at time zone 'utc'), 'alpaca_orphan_reconcile', CAST(:p AS jsonb))"
        ), {"sid": sid, "p": json.dumps(payload)})
    except Exception:
        logger.debug("[alpaca_reconcile] audit insert failed", exc_info=True)


def run_alpaca_orphan_reconcile(db: Session) -> dict[str, Any]:
    """One reconcile pass. Returns a summary dict (logged by the scheduler job)."""
    out: dict[str, Any] = {"flattened": 0, "cancelled": 0, "skipped_active": 0, "skipped_recent": 0}
    if not bool(getattr(settings, "chili_momentum_alpaca_orphan_reconcile_enabled", True)):
        out["skipped"] = "flag_off"
        return out
    # PAPER-ONLY hard gate: never reconcile-flatten a real-money account from here.
    if not (
        bool(getattr(settings, "chili_alpaca_enabled", False))
        and bool(getattr(settings, "chili_alpaca_paper", True))
        and str(getattr(settings, "chili_alpaca_api_key", "") or "")
    ):
        out["skipped"] = "alpaca_not_paper_ready"
        return out

    try:
        from ..venue.alpaca_spot import AlpacaSpotAdapter

        adapter = AlpacaSpotAdapter()
        if not adapter.is_enabled():
            out["skipped"] = "adapter_disabled"
            return out
    except Exception:
        out["skipped"] = "adapter_import_failed"
        return out

    views = _managed_and_recent_symbols(db)
    if views is None:
        out["skipped"] = "db_view_unreadable"
        return out
    active, recent = views
    try:
        db.rollback()  # end the read tx before the broker HTTP calls (idle-in-tx hygiene)
    except Exception:
        pass

    positions, _ = adapter.list_positions()
    if positions is None:
        out["skipped"] = "positions_unreadable"  # fail-open: no action on a bad read
        return out

    # Open orders FIRST: any symbol with ANY resting order is NOT flattened this pass
    # (cancel-first policy). This kills two failure modes: (a) HALTED-STOCK STACKING —
    # a flatten sell rests open through the halt; without this guard the next pass
    # would submit ANOTHER sell (new minute key) and on resume they would ALL fill,
    # flipping the account SHORT; (b) OVERSELL — a stranded exit sell from the dead
    # session fills while our full-qty flatten also fills. The stale-order cancel loop
    # below clears the book this pass; the clean position flattens on the NEXT pass.
    orders, _ = adapter.list_open_orders(limit=50, strict=True)
    if orders is None:
        # FAIL-OPEN: the in-flight guard (anti-double-sell) depends on a trustworthy
        # order view; an unreadable book means NO flatten and NO cancel this pass.
        out["skipped"] = "orders_unreadable"
        return out
    _inflight_syms: set[str] = set()
    for _o in orders or []:
        _os = str(getattr(_o, "product_id", None) or (_o.get("product_id") if isinstance(_o, dict) else "") or "").strip().upper()
        if _os:
            _inflight_syms.add(_os)

    max_actions = 8  # per-pass bound: a runaway loop can never mass-fire orders
    actions = 0
    minute_key = datetime.now(timezone.utc).strftime("%Y%m%d%H%M")

    for pos in positions:
        if actions >= max_actions:
            break
        sym = str(pos.get("product_id") or "").strip().upper()
        qty = float(pos.get("qty") or 0.0)
        asset_class = str(pos.get("asset_class") or "")
        raw_sym = str(pos.get("raw_symbol") or "")
        # Scope: LONG EQUITIES only. Crypto (asset_class/crypto symbol forms) and
        # shorts are explicitly out of scope for this guard.
        if not sym or qty <= 0:
            continue
        if "crypto" in asset_class or "/" in raw_sym or sym.endswith("-USD"):
            continue
        if sym in active:
            out["skipped_active"] += 1
            continue
        if sym in recent:
            out["skipped_recent"] += 1
            continue
        if sym in _inflight_syms:
            # cancel-first: an order is resting on this symbol (a stranded exit, a
            # stale entry, or our OWN in-flight flatten through a halt). Never stack
            # a second sell — the order loop below clears stale ones; flatten next pass.
            out["skipped_inflight"] = int(out.get("skipped_inflight") or 0) + 1
            continue
        import time as _time

        _last = _LAST_ATTEMPT.get(sym)
        if _last is not None and (_time.monotonic() - _last) < _grace_minutes() * 60.0:
            out["skipped_backoff"] = int(out.get("skipped_backoff") or 0) + 1
            continue
        _LAST_ATTEMPT[sym] = _time.monotonic()
        # position_intent=sell_to_close: the broker REJECTS the order if the long is
        # already gone (a stranded exit filled in the read-to-submit gap) — an
        # accidental SHORT-OPEN is impossible by construction (adversarial lens 1/2).
        res = adapter.place_market_order(
            product_id=sym, side="sell", base_size=str(qty),
            client_order_id=f"orphrec-{sym}-{minute_key}",
            position_intent="sell_to_close",
        )
        actions += 1
        ok = bool(res.get("ok"))
        out["flattened"] += 1 if ok else 0
        logger.warning(
            "[alpaca_reconcile] ORPHAN position %s qty=%s (mv=%s upl=%s) -> flatten %s (%s)",
            sym, qty, pos.get("market_value"), pos.get("unrealized_pl"),
            "SUBMITTED" if ok else "FAILED", res.get("order_id") or res.get("error"),
        )
        _audit(db, sym, {
            "action": "flatten_orphan_position", "ok": ok, "qty": qty,
            "market_value": pos.get("market_value"), "unrealized_pl": pos.get("unrealized_pl"),
            "order_id": res.get("order_id"), "error": (None if ok else str(res.get("error"))[:160]),
        })

    for o in orders or []:
        if actions >= max_actions:
            break
        sym = str(getattr(o, "product_id", None) or (o.get("product_id") if isinstance(o, dict) else "") or "").strip().upper()
        oid = str(getattr(o, "order_id", None) or (o.get("order_id") if isinstance(o, dict) else "") or "")
        coid = str(getattr(o, "client_order_id", None) or (o.get("client_order_id") if isinstance(o, dict) else "") or "")
        if not sym or not oid:
            continue
        if "/" in sym or sym.endswith("-USD"):
            continue
        if coid.startswith("orphrec-"):
            continue  # our own in-flight flatten — never cancel it
        # TOCTOU guard (adversarial lens 1): the sessions view was snapshotted at pass
        # start; a session armed + entry posted DURING this pass would look orphaned.
        # A genuine orphan order has been RESTING — only cancel orders older than the
        # grace window; unknown timestamps are skipped (fail-open).
        _created = str(getattr(o, "created_time", None) or (o.get("created_time") if isinstance(o, dict) else "") or "")
        _age_ok = False
        try:
            from datetime import datetime as _dt

            _cts = _dt.fromisoformat(_created.replace("Z", "+00:00"))
            if _cts.tzinfo is None:
                _cts = _cts.replace(tzinfo=timezone.utc)
            _age_ok = (datetime.now(timezone.utc) - _cts).total_seconds() >= _grace_minutes() * 60.0
        except Exception:
            _age_ok = False
        if not _age_ok:
            out["skipped_young_order"] = int(out.get("skipped_young_order") or 0) + 1
            continue
        if sym in active:
            out["skipped_active"] += 1
            continue
        if sym in recent:
            out["skipped_recent"] += 1
            continue
        res = adapter.cancel_order(oid)
        actions += 1
        ok = bool(res.get("ok", True))
        out["cancelled"] += 1 if ok else 0
        logger.warning(
            "[alpaca_reconcile] ORPHAN open order %s %s -> cancel %s",
            sym, oid, "OK" if ok else f"FAILED ({res.get('error')})",
        )
        _audit(db, sym, {"action": "cancel_orphan_order", "ok": ok, "order_id": oid})

    return out
