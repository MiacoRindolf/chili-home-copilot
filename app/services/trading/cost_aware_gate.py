"""f-coinbase-autotrader-enablement-phase-5-cost-aware-sizing (2026-05-09).

Cost-aware min-edge gate + per-venue cap helpers + Coinbase
buying-power resolver.

Three layers, intentionally separable so each can be replaced /
tightened in isolation:

  1. :func:`resolve_coinbase_buying_power` — returns
     ``{usd, usdc, total, last_updated}``. 30s in-process cache.
     Reads ``cash`` (USD wallet) from
     :func:`coinbase_service.get_portfolio` AND USDC quantity from
     :func:`coinbase_service.get_positions` so the total reflects
     actual buying power per Phase 2 G1 (operator's funded $2.2k is
     held as USDC; ``portfolio.cash`` reports USD-only).

  2. :func:`cost_aware_min_edge_gate` — returns ``{allowed, reason,
     fee_bps, threshold_bps, edge_bps}``. For RH-eligible tickers
     fee=0 (RH crypto is fee-free, equity is sub-bps); the gate is
     a no-op. For Coinbase-only tickers, the projected edge must
     clear ``fee_bps + buffer_bps`` else block.

  3. :func:`per_venue_cap_check` — returns ``{allowed, reason,
     current_positions, current_notional}``. Per-venue caps are
     INDEPENDENT (Phase 1 design constraint #1; no cross-venue
     aggregation).

Helper-level testable: every public function accepts injection
seams (settings_, db, fast_path_active) so unit tests can run
without hitting production state.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

logger = logging.getLogger(__name__)


# Reason constants — pinned by tests so a typo flips visibly red.
REASON_GATE_RH_FEE_FREE = "rh_fee_free"
REASON_GATE_COINBASE_PASSED = "coinbase_clears_fee_threshold"
REASON_GATE_COINBASE_BLOCKED = "coinbase_below_fee_threshold"
REASON_GATE_NO_VENUE = "no_venue_supports"
REASON_CAP_OK = "within_cap"
REASON_CAP_NOTIONAL = "venue_notional_cap_exceeded"
REASON_CAP_POSITIONS = "venue_concurrent_positions_cap_exceeded"


@dataclass(frozen=True)
class CostGateDecision:
    allowed: bool
    reason: str
    fee_bps: int
    threshold_bps: int
    edge_bps: int


@dataclass(frozen=True)
class CapDecision:
    allowed: bool
    reason: str
    current_positions: int
    current_notional_usd: float


# ── Buying-power resolver ────────────────────────────────────────────


_BUYING_POWER_CACHE: dict[str, Any] = {"value": None, "ts": 0.0}
_BUYING_POWER_CACHE_TTL_S = 30.0


def resolve_coinbase_buying_power(
    *, force_refresh: bool = False,
    portfolio_fn=None, positions_fn=None,
) -> dict[str, Any]:
    """Returns ``{usd, usdc, total, last_updated}``.

    * ``usd`` — `cash` field from :func:`coinbase_service.get_portfolio`.
    * ``usdc`` — quantity of the ``USDC-USD`` position from
      :func:`coinbase_service.get_positions` (treated 1:1 with USD).
    * ``total`` — sum of the two; the autotrader's effective Coinbase
      buying power.
    * ``last_updated`` — unix ts of resolution.

    30s in-process cache. ``portfolio_fn`` / ``positions_fn`` are the
    test-injection seams; production callers leave None.
    """
    now = time.time()
    if (
        not force_refresh
        and _BUYING_POWER_CACHE["value"] is not None
        and (now - _BUYING_POWER_CACHE["ts"]) < _BUYING_POWER_CACHE_TTL_S
    ):
        return dict(_BUYING_POWER_CACHE["value"])

    if portfolio_fn is None or positions_fn is None:
        try:
            from ..coinbase_service import get_portfolio, get_positions
            portfolio_fn = portfolio_fn or get_portfolio
            positions_fn = positions_fn or get_positions
        except Exception as exc:
            logger.warning(
                "[cost_aware_gate] coinbase_service import failed: %s", exc,
            )
            return {
                "usd": 0.0, "usdc": 0.0, "total": 0.0,
                "last_updated": now, "error": str(exc),
            }

    try:
        portfolio = portfolio_fn() or {}
    except Exception as exc:
        logger.warning(
            "[cost_aware_gate] get_portfolio failed: %s", exc,
        )
        portfolio = {}
    try:
        positions = positions_fn() or []
    except Exception as exc:
        logger.warning(
            "[cost_aware_gate] get_positions failed: %s", exc,
        )
        positions = []

    try:
        usd = float(portfolio.get("cash") or 0.0)
    except (TypeError, ValueError):
        usd = 0.0

    usdc_qty = 0.0
    for p in positions:
        if not isinstance(p, dict):
            continue
        ticker = str(p.get("ticker") or "").upper()
        if ticker in ("USDC-USD", "USDC"):
            try:
                usdc_qty = float(p.get("quantity") or 0.0)
            except (TypeError, ValueError):
                usdc_qty = 0.0
            break

    total = usd + usdc_qty
    result = {
        "usd": usd,
        "usdc": usdc_qty,
        "total": total,
        "last_updated": now,
    }
    _BUYING_POWER_CACHE["value"] = result
    _BUYING_POWER_CACHE["ts"] = now
    return dict(result)


# ── Cost-aware min-edge gate ─────────────────────────────────────────


def cost_aware_min_edge_gate(
    *,
    ticker: str,
    projected_profit_pct: Optional[float],
    settings_=None,
) -> CostGateDecision:
    """Refuses Coinbase entries whose projected edge does not clear
    (fee + buffer). RH-eligible tickers pass with fee=0 (no behavior
    change vs pre-Phase-5).

    ``projected_profit_pct`` is the percent (e.g. 6.71 for 6.71%).
    Converted to bps (671) for the fee comparison.
    """
    s = settings_
    if s is None:
        try:
            from ...config import settings as _s
            s = _s
        except Exception:
            s = None

    fee_bps = int(getattr(s, "chili_coinbase_taker_fee_bps_round_trip", 120))
    buffer_bps = int(getattr(s, "chili_min_edge_safety_buffer_bps", 30))
    threshold_bps = fee_bps + buffer_bps

    edge_bps = (
        int(round(float(projected_profit_pct) * 100.0))
        if projected_profit_pct is not None
        else 0
    )

    # Routing-aware: RH-eligible tickers pay no fee at the autotrader
    # level (RH crypto fee-free; equities sub-bps and absorbed by
    # the existing min_projected_profit_pct floor).
    try:
        from .broker_selector import (
            resolve_coinbase_whitelist,
            resolve_rh_whitelist,
        )
    except Exception:
        # If selector module isn't importable, default to "no opinion"
        # — the existing rule_gate's min_projected_profit_pct handles
        # the floor. Cost-gate becomes a soft pass.
        return CostGateDecision(
            allowed=True, reason=REASON_GATE_RH_FEE_FREE,
            fee_bps=0, threshold_bps=0, edge_bps=edge_bps,
        )

    if resolve_rh_whitelist(ticker):
        return CostGateDecision(
            allowed=True, reason=REASON_GATE_RH_FEE_FREE,
            fee_bps=0, threshold_bps=0, edge_bps=edge_bps,
        )

    if not resolve_coinbase_whitelist(ticker):
        # No venue supports — selector will skip downstream.
        return CostGateDecision(
            allowed=False, reason=REASON_GATE_NO_VENUE,
            fee_bps=0, threshold_bps=0, edge_bps=edge_bps,
        )

    # Coinbase routing: must clear fee + buffer.
    if edge_bps >= threshold_bps:
        return CostGateDecision(
            allowed=True, reason=REASON_GATE_COINBASE_PASSED,
            fee_bps=fee_bps, threshold_bps=threshold_bps,
            edge_bps=edge_bps,
        )
    return CostGateDecision(
        allowed=False, reason=REASON_GATE_COINBASE_BLOCKED,
        fee_bps=fee_bps, threshold_bps=threshold_bps,
        edge_bps=edge_bps,
    )


# ── Per-venue cap check ──────────────────────────────────────────────


def per_venue_cap_check(
    *,
    venue: str,
    proposed_notional_usd: float,
    db,
    user_id: Optional[int] = None,
    settings_=None,
) -> CapDecision:
    """Per-venue notional + concurrent-position cap. Independent
    per-venue per Phase 1 design constraint #1.

    For Coinbase: caps come from ``chili_coinbase_max_notional_usd``
    and ``chili_coinbase_max_concurrent_positions``. For other
    venues today this returns ``allowed=True`` (the existing RH
    autotrader has its own size/heat gates upstream).

    Reads currently-open Trades from ``trading_trades`` filtered to
    ``broker_source = venue``.
    """
    venue_l = (venue or "").strip().lower()
    s = settings_
    if s is None:
        try:
            from ...config import settings as _s
            s = _s
        except Exception:
            s = None

    if venue_l != "coinbase":
        # RH cap stays in existing RH-side logic; Phase 5 doesn't
        # touch it.
        return CapDecision(
            allowed=True, reason=REASON_CAP_OK,
            current_positions=0, current_notional_usd=0.0,
        )

    max_notional = float(
        getattr(s, "chili_coinbase_max_notional_usd", 50.0)
    )
    max_positions = int(
        getattr(s, "chili_coinbase_max_concurrent_positions", 3)
    )

    try:
        from sqlalchemy import text
        rows = db.execute(text("""
            SELECT id,
                   COALESCE(quantity * entry_price, 0.0) AS notional
              FROM trading_trades
             WHERE status = 'open'
               AND LOWER(COALESCE(broker_source, '')) = 'coinbase'
        """)).fetchall()
        current_positions = len(rows)
        current_notional = sum(float(r.notional or 0.0) for r in rows)
    except Exception as exc:
        logger.warning(
            "[cost_aware_gate] per_venue_cap_check query failed: %s", exc,
            exc_info=True,
        )
        # Conservative on failure: assume cap is consumed -> block.
        return CapDecision(
            allowed=False, reason=REASON_CAP_NOTIONAL,
            current_positions=999, current_notional_usd=99999.0,
        )

    if current_positions >= max_positions:
        return CapDecision(
            allowed=False, reason=REASON_CAP_POSITIONS,
            current_positions=current_positions,
            current_notional_usd=current_notional,
        )
    if (current_notional + float(proposed_notional_usd)) > max_notional:
        return CapDecision(
            allowed=False, reason=REASON_CAP_NOTIONAL,
            current_positions=current_positions,
            current_notional_usd=current_notional,
        )
    return CapDecision(
        allowed=True, reason=REASON_CAP_OK,
        current_positions=current_positions,
        current_notional_usd=current_notional,
    )


__all__ = [
    "CapDecision",
    "CostGateDecision",
    "REASON_CAP_NOTIONAL",
    "REASON_CAP_OK",
    "REASON_CAP_POSITIONS",
    "REASON_GATE_COINBASE_BLOCKED",
    "REASON_GATE_COINBASE_PASSED",
    "REASON_GATE_NO_VENUE",
    "REASON_GATE_RH_FEE_FREE",
    "cost_aware_min_edge_gate",
    "per_venue_cap_check",
    "resolve_coinbase_buying_power",
]
