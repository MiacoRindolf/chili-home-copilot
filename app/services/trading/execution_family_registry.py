"""Execution-family registry — Phase 11 seams only (no arbitrage implementation).

**strategy_family** (e.g. ``MomentumStrategyVariant.family``) describes *what* trade logic applies.

**execution_family** (variant + session + outcome columns) describes *how / where* orders are routed.

Neural momentum intelligence stays the owner of strategy signals; this module only classifies
execution routing. Arbitrage / multi-venue / basis paths are **not** implemented: constants below
are documented stubs for future design (inventory, transfers, risk, separate intelligence).

The only implemented automation path today is Coinbase spot spot-adapter + paper/live runners.
"""

from __future__ import annotations

from typing import Any, Callable, Optional

# ── Implemented (live adapter + runners honor this) ───────────────────────────
EXECUTION_FAMILY_COINBASE_SPOT = "coinbase_spot"
EXECUTION_FAMILY_ROBINHOOD_SPOT = "robinhood_spot"

# ── Documented stubs only (no behavior, no jobs, no hidden execution) ────────
EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE = "multi_venue_arbitrage"
EXECUTION_FAMILY_SAME_VENUE_TRIANGULAR_ARB = "same_venue_triangular_arb"
EXECUTION_FAMILY_BASIS_TRADE = "basis_trade"

DOCUMENTED_EXECUTION_FAMILIES: frozenset[str] = frozenset(
    {
        EXECUTION_FAMILY_COINBASE_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
        EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE,
        EXECUTION_FAMILY_SAME_VENUE_TRIANGULAR_ARB,
        EXECUTION_FAMILY_BASIS_TRADE,
    }
)

IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES: frozenset[str] = frozenset({
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
})


class ExecutionFamilyNotImplementedError(LookupError):
    """Raised when resolving a venue adapter for an execution_family with no implementation."""

    def __init__(self, execution_family: str):
        self.execution_family = execution_family
        super().__init__(f"execution_family not implemented: {execution_family!r}")


def normalize_execution_family(value: Optional[str]) -> str:
    s = (value or "").strip().lower()
    return s if s else EXECUTION_FAMILY_COINBASE_SPOT


def is_documented_execution_family(execution_family: str) -> bool:
    return normalize_execution_family(execution_family) in DOCUMENTED_EXECUTION_FAMILIES


def is_momentum_automation_implemented(execution_family: str) -> bool:
    """True if paper/live automation + refresh may use this family (today: coinbase_spot only)."""
    return normalize_execution_family(execution_family) in IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES


def momentum_runner_supports_execution_family(execution_family: str) -> bool:
    """Paper/live runner batch may tick sessions with this execution_family."""
    return is_momentum_automation_implemented(execution_family)


def execution_family_capabilities() -> list[dict[str, Any]]:
    """Read-only operator surface: id, status, short notes (no execution)."""
    notes_map = {
        EXECUTION_FAMILY_COINBASE_SPOT: (
            "Implemented: Coinbase spot VenueAdapter + neural momentum operator/runner path."
        ),
        EXECUTION_FAMILY_ROBINHOOD_SPOT: (
            "Implemented: Robinhood equities VenueAdapter via robin_stocks + broker_service."
        ),
        EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE: (
            "Planned seam only — needs multi-venue intelligence, inventory, transfers, risk (not built)."
        ),
        EXECUTION_FAMILY_SAME_VENUE_TRIANGULAR_ARB: (
            "Planned seam only — not implemented."
        ),
        EXECUTION_FAMILY_BASIS_TRADE: ("Planned seam only — not implemented."),
    }
    out: list[dict[str, Any]] = []
    for fam in sorted(DOCUMENTED_EXECUTION_FAMILIES):
        impl = fam in IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES
        out.append(
            {
                "id": fam,
                "status": "implemented" if impl else "planned",
                "notes": notes_map.get(fam, ""),
            }
        )
    return out


def momentum_execution_seam_meta() -> dict[str, Any]:
    """Compact JSON for viable-strategy / desk payloads (strategy vs execution split)."""
    return {
        "strategy_vs_execution": (
            "strategy_family is variant.family (logic). execution_family is routing (venue path)."
        ),
        "implemented_automation_families": sorted(IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES),
        "documented_stub_families": sorted(
            DOCUMENTED_EXECUTION_FAMILIES - IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES
        ),
    }


def resolve_live_spot_adapter_factory(execution_family: str) -> Callable[[], Any]:
    """Return a zero-arg factory producing a VenueAdapter for live spot ticks.

    Raises:
        ExecutionFamilyNotImplementedError: for any family other than ``coinbase_spot``.
    """
    ef = normalize_execution_family(execution_family)
    if ef == EXECUTION_FAMILY_COINBASE_SPOT:
        from .venue.coinbase_spot import CoinbaseSpotAdapter

        return CoinbaseSpotAdapter
    if ef == EXECUTION_FAMILY_ROBINHOOD_SPOT:
        from .venue.robinhood_spot import RobinhoodSpotAdapter

        return RobinhoodSpotAdapter
    raise ExecutionFamilyNotImplementedError(ef)
