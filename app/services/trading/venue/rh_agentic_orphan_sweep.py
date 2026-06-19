"""B3 — agentic-account orphan sweep (the reconciler is BLIND to this account).

CHILI's momentum lane places no broker-side stop — it polls price in-process and
then places a market/limit exit. The broker-sync reconciler runs a *separate*
``robin_stocks`` session on the MAIN Robinhood account and never sees the isolated
**Agentic** account, so a position that filled on the agentic rail has NO reconciler
backstop. On a scheduler restart (or a lost in-process session) that filled position
becomes an unmanaged orphan at RH — held, with no stop.

This module is the minimal, agentic-rail-ONLY backstop: it reads the agentic account's
open positions/orders via the sanctioned MCP rail and surfaces any momentum position
that has no live in-process session, so the runner's restart/adopt path can re-adopt it
(mirroring how robin_stocks orphans are adopted — ``cancel_automation_session``
``FILLED_NEEDS_ADOPTION`` + the broker-sync scope stamp ``management_scope='momentum_neural'``).

Scope: this NEVER touches the robin_stocks reconciler or the main account. It is a
read + detect + log/surface pass; the actual re-adoption decision stays with the runner.

RESIDUAL (documented activation blocker, see docs/DESIGN/ROBINHOOD_AGENTIC_MCP.md): this
is detect + surface, NOT a fully-automated continuous reconciliation loop. The operator
must wire ``sweep_agentic_orphans`` into the restart/adopt path (or a scheduled job) and
confirm the adopt branch before flipping the rail live.
"""

from __future__ import annotations

# stdlib
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

# relative app
from ....config import settings

logger = logging.getLogger(__name__)

# Live in-process session states (a position managed by one of these is NOT an orphan).
_LIVE_MANAGED_STATES = frozenset(
    {"live_entered", "live_scaling_out", "live_trailing", "live_bailout"}
)


@dataclass
class AgenticOrphanReport:
    """Result of one sweep pass (no side effects beyond logging)."""

    checked: bool = False
    account_tail: str = ""
    open_positions: int = 0
    managed_symbols: list[str] = field(default_factory=list)
    orphan_symbols: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "checked": self.checked,
            "account_tail": self.account_tail,
            "open_positions": self.open_positions,
            "managed_symbols": list(self.managed_symbols),
            "orphan_symbols": list(self.orphan_symbols),
            "error": self.error,
        }


def _position_symbol(pos: dict) -> str:
    for k in ("symbol", "ticker", "instrument_symbol"):
        v = pos.get(k)
        if v:
            return str(v).strip().upper()
    return ""


def _position_is_open(pos: dict) -> bool:
    for k in ("quantity", "shares", "position", "size"):
        v = pos.get(k)
        if v is None:
            continue
        try:
            if abs(float(v)) > 0:
                return True
        except (TypeError, ValueError):
            continue
    # If no quantity field is present, assume the position list only contains open rows.
    return True


def _live_managed_agentic_symbols(db: Any, *, user_id: Optional[int] = None) -> set[str]:
    """Symbols that DO have a live in-process agentic session (not orphans)."""
    try:
        from ...models.trading import TradingAutomationSession
        from ..execution_family_registry import EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP

        q = db.query(TradingAutomationSession).filter(
            TradingAutomationSession.mode == "live",
            TradingAutomationSession.execution_family == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
            TradingAutomationSession.state.in_(tuple(_LIVE_MANAGED_STATES)),
        )
        if user_id is not None:
            q = q.filter(TradingAutomationSession.user_id == int(user_id))
        return {str(s.symbol or "").strip().upper() for s in q.all() if s.symbol}
    except Exception as exc:  # noqa: BLE001
        logger.warning("[rh_agentic_orphan_sweep] live-session query failed: %s", exc)
        return set()


def sweep_agentic_orphans(
    db: Any,
    *,
    user_id: Optional[int] = None,
    adapter: Optional[Any] = None,
) -> AgenticOrphanReport:
    """Detect agentic-account positions with no live in-process session.

    Returns an ``AgenticOrphanReport``; logs an error-level line per detected orphan so
    the operator (and any wired restart/adopt path) can re-adopt it. Pure read + surface —
    no order is placed or cancelled here. Fail-open (the report carries ``error`` on any
    failure; the lane is never blocked by a sweep failure).
    """
    report = AgenticOrphanReport()
    acct = str(getattr(settings, "chili_robinhood_agentic_mcp_account_number", "") or "").strip()
    if not acct:
        report.error = "no_agentic_account"
        return report
    report.account_tail = acct[-4:]

    if adapter is None:
        try:
            from .robinhood_mcp import RobinhoodAgenticMcpAdapter

            adapter = RobinhoodAgenticMcpAdapter()
        except Exception as exc:  # noqa: BLE001
            report.error = f"adapter_init:{type(exc).__name__}"
            return report

    try:
        positions = adapter.get_agentic_open_positions() or []
    except Exception as exc:  # noqa: BLE001
        report.error = f"positions_read:{type(exc).__name__}"
        return report

    report.checked = True
    open_syms: list[str] = []
    for pos in positions:
        if not isinstance(pos, dict):
            continue
        if not _position_is_open(pos):
            continue
        sym = _position_symbol(pos)
        if sym:
            open_syms.append(sym)
    report.open_positions = len(open_syms)

    managed = _live_managed_agentic_symbols(db, user_id=user_id)
    report.managed_symbols = sorted(managed)
    orphans = sorted({s for s in open_syms if s not in managed})
    report.orphan_symbols = orphans

    for sym in orphans:
        logger.error(
            "[rh_agentic_orphan_sweep] UNMANAGED agentic position symbol=%s account_tail=%s "
            "— no live in-process session; needs re-adoption (no broker-side stop at RH)",
            sym, report.account_tail,
        )
    return report
