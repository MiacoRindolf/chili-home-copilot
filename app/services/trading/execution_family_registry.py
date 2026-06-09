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
# Robinhood's officially-sanctioned agentic-trading MCP rail (isolated Agentic account;
# sanctioned execution). Counterpart to robinhood_spot (unofficial robin_stocks API).
EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP = "robinhood_agentic_mcp"
# Alpaca US equities — API-first, commission-free, FREE paper sandbox, and limit orders that
# route to the market + can REST on the book (post-inside-the-spread, which RH's PFOF routing
# cannot do). The DMA-style execution upgrade for the momentum lane. (docs/DESIGN/ALPACA_LANE.md)
EXECUTION_FAMILY_ALPACA_SPOT = "alpaca_spot"

# ── Documented stubs only (no behavior, no jobs, no hidden execution) ────────
EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE = "multi_venue_arbitrage"
EXECUTION_FAMILY_SAME_VENUE_TRIANGULAR_ARB = "same_venue_triangular_arb"
EXECUTION_FAMILY_BASIS_TRADE = "basis_trade"

DOCUMENTED_EXECUTION_FAMILIES: frozenset[str] = frozenset(
    {
        EXECUTION_FAMILY_COINBASE_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_SPOT,
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
        EXECUTION_FAMILY_ALPACA_SPOT,
        EXECUTION_FAMILY_MULTI_VENUE_ARBITRAGE,
        EXECUTION_FAMILY_SAME_VENUE_TRIANGULAR_ARB,
        EXECUTION_FAMILY_BASIS_TRADE,
    }
)

IMPLEMENTED_MOMENTUM_AUTOMATION_FAMILIES: frozenset[str] = frozenset({
    EXECUTION_FAMILY_COINBASE_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    EXECUTION_FAMILY_ALPACA_SPOT,
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
        EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP: (
            "Implemented: Robinhood equities via the official Agentic Trading MCP rail "
            "(isolated account; sanctioned execution). Active when a bearer token is configured "
            "AND chili_equity_execution_rail selects it."
        ),
        EXECUTION_FAMILY_ALPACA_SPOT: (
            "Implemented: Alpaca US equities VenueAdapter (alpaca-py) — DMA-style limit-posting, "
            "FREE paper sandbox. Active when chili_alpaca_enabled AND API keys are set "
            "(paper until chili_alpaca_paper=False)."
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


def _equity_rail_is_agentic_mcp() -> bool:
    """True when the operator has selected the sanctioned MCP rail for equities AND a
    bearer token is configured.

    Token-presence is the activation switch (a real dependency, not a default-OFF dark
    flag); rail selection (``chili_equity_execution_rail``) is a conscious account-routing
    choice — which Robinhood account trades — defaulting to ``robinhood_spot`` so live
    equity flow is unchanged until the operator opts in.
    """
    try:
        from ...config import settings

        rail = str(getattr(settings, "chili_equity_execution_rail", "") or "").strip().lower()
    except Exception:
        return False
    if rail != EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
        return False
    try:
        from .venue.rh_mcp_client import resolve_mcp_token

        return bool(resolve_mcp_token())
    except Exception:
        return False


def resolve_execution_family_for_symbol(symbol: str) -> str:
    """Route a symbol to its execution family by asset class.

    Crypto pairs (BASE-USD) -> coinbase_spot (the proven momentum path). Equities
    (bare tickers — ARKK, CLSK, AAPL) -> ``robinhood_spot`` via the unofficial robin_stocks
    API by DEFAULT, or the officially-sanctioned ``robinhood_agentic_mcp`` rail when the
    operator selects it and a token is present (see ``_equity_rail_is_agentic_mcp``).
    """
    try:
        from .venue.robinhood_spot import _is_crypto_product

        if _is_crypto_product(symbol):
            return EXECUTION_FAMILY_COINBASE_SPOT
        if _equity_rail_is_agentic_mcp():
            return EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP
        return EXECUTION_FAMILY_ROBINHOOD_SPOT
    except Exception:
        # Fallback heuristic: a "-USD" pair is crypto -> Coinbase, else equity -> RH.
        return (
            EXECUTION_FAMILY_COINBASE_SPOT
            if "-USD" in str(symbol or "").upper()
            else EXECUTION_FAMILY_ROBINHOOD_SPOT
        )


def venue_for_execution_family(execution_family: str) -> str:
    """The broker venue string for a session of this execution family."""
    ef = normalize_execution_family(execution_family)
    if ef in (EXECUTION_FAMILY_ROBINHOOD_SPOT, EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP):
        return "robinhood"
    if ef == EXECUTION_FAMILY_ALPACA_SPOT:
        return "alpaca"
    return "coinbase"


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
    if ef == EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
        from .venue.robinhood_mcp import RobinhoodAgenticMcpAdapter

        return RobinhoodAgenticMcpAdapter
    if ef == EXECUTION_FAMILY_ALPACA_SPOT:
        from .venue.alpaca_spot import AlpacaSpotAdapter

        return AlpacaSpotAdapter
    raise ExecutionFamilyNotImplementedError(ef)
