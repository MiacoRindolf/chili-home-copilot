"""Shared helpers for the three exit-monitor lanes (equity / crypto / options).

Before the 2026-05-06 options fix the equity and crypto lanes each kept a
local copy of the ``latest_monitor_decisions_by_trade`` +
``fresh_monitor_exit_meta`` helpers. Subtle drift between the copies
(crypto used ``_CRYPTO_MONITOR_EXIT_NOW_MAX_AGE_HOURS``; equity used
``_MONITOR_EXIT_NOW_MAX_AGE_HOURS``) made the next consumer's job
ambiguous.

This module is the single source of truth. All three lanes (equity in
``auto_trader_monitor.py``, crypto in ``crypto/exit_monitor.py``,
options in ``options/exit_monitor.py``) import from here -- no local
copies, no per-lane fork of the freshness window.

Why not a class: each helper is a pure function with no shared state.
A module-level function plus a single constant is simpler than a
ExitMonitorCommon class with two methods.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from sqlalchemy.orm import Session
    from ...models.trading import PatternMonitorDecision


# ── Freshness window ───────────────────────────────────────────────────
#
# An ``exit_now`` recommendation older than this is treated as stale and
# does not trigger an exit. 96h is the value the equity lane has run with
# since 2026-04 and the crypto lane adopted on 2026-05-06; options lane
# inherits from this module on 2026-05-06.
#
# If a future asset class needs a tighter window (e.g., short-dated
# options where a 96h-old advisory is materially stale), introduce a
# per-asset override at the call site rather than splitting the module.
MONITOR_EXIT_NOW_MAX_AGE_HOURS = 96.0


def latest_monitor_decisions_by_trade(
    db: "Session",
    trade_ids: list[int],
) -> dict[int, "PatternMonitorDecision"]:
    """Latest ``PatternMonitorDecision`` per trade (most recent wins).

    Execution should follow the newest advisory state only. If a prior
    ``exit_now`` has since been superseded by ``hold``, the live monitor
    must not keep selling from the stale recommendation.

    Returns a dict keyed by trade_id. Missing trade_ids (no decision in
    the table) are absent from the dict; callers should treat absence
    as "no advisory."
    """
    from ...models.trading import PatternMonitorDecision

    if not trade_ids:
        return {}
    rows = (
        db.query(PatternMonitorDecision)
        .filter(PatternMonitorDecision.trade_id.in_(trade_ids))
        .order_by(PatternMonitorDecision.created_at.desc())
        .all()
    )
    latest: dict[int, PatternMonitorDecision] = {}
    for row in rows:
        latest.setdefault(int(row.trade_id), row)
    return latest


def fresh_monitor_exit_meta(
    decision: "PatternMonitorDecision | None",
) -> dict[str, Any] | None:
    """Audit metadata when the latest monitor decision still means exit.

    Returns ``None`` when:
      * ``decision`` is None (no advisory for this trade)
      * the decision's action isn't ``exit_now``
      * the decision is older than ``MONITOR_EXIT_NOW_MAX_AGE_HOURS``

    When the lane chooses to exit on this advisory, the returned dict
    becomes the audit log entry. Audit detail belongs in the log line,
    NOT in the 50-char ``pending_exit_reason`` column.
    """
    if decision is None or (decision.action or "").lower() != "exit_now":
        return None
    age_h = (datetime.utcnow() - decision.created_at).total_seconds() / 3600.0
    if age_h > MONITOR_EXIT_NOW_MAX_AGE_HOURS:
        return None
    return {
        "decision_id": int(decision.id),
        "decision_source": decision.decision_source,
        "decision_age_hours": round(age_h, 3),
        "decision_price": (
            float(decision.price_at_decision)
            if decision.price_at_decision is not None
            else None
        ),
    }


__all__ = [
    "MONITOR_EXIT_NOW_MAX_AGE_HOURS",
    "latest_monitor_decisions_by_trade",
    "fresh_monitor_exit_meta",
]
