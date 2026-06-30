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
# Alpaca SHORT lane — its OWN execution family, kept ISOLATED from the long agentic
# lane so risk, daily-loss, and concurrency caps don't co-mingle (SHORT_SIDE_LANE.md).
# Same AlpacaSpotAdapter, but the runner sets SELL_TO_OPEN / BUY_TO_CLOSE per-order.
# PAPER-only by construction: excluded from REAL_DAILY_LOSS_FAMILIES until a later
# phase adds a live alpaca family + per-broker cap. (P0: adapter + family only — NO
# momentum-lane short triggers yet; gated behind chili_momentum_short_lane_enabled.)
EXECUTION_FAMILY_ALPACA_SHORT = "alpaca_short"

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
        EXECUTION_FAMILY_ALPACA_SHORT,
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
    EXECUTION_FAMILY_ALPACA_SHORT,
})

# Asset-class grouping: a symbol may route to ANY venue of its asset class — an EQUITY can go
# to robinhood_spot OR alpaca_spot (the same-name A/B), a crypto pair only to coinbase_spot.
# The risk gate validates the requested venue against the symbol's ASSET CLASS (not the single
# default-resolved venue), while still blocking the dangerous cross-class case (equity routed
# to the crypto venue, or vice versa). (docs/DESIGN/ALPACA_LANE.md)
_CRYPTO_EXECUTION_FAMILIES: frozenset[str] = frozenset({EXECUTION_FAMILY_COINBASE_SPOT})
_EQUITY_EXECUTION_FAMILIES: frozenset[str] = frozenset({
    EXECUTION_FAMILY_ROBINHOOD_SPOT,
    EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP,
    EXECUTION_FAMILY_ALPACA_SPOT,
    EXECUTION_FAMILY_ALPACA_SHORT,
})

# Asset classes each family can actually TRADE. Alpaca serves BOTH (US equities
# AND spot crypto — the paper crypto soak rides this, 2026-06-12); the legacy
# single-class map above stays for asset_class_of_execution_family's primary
# answer (alpaca's primary remains equity).
_EXECUTION_FAMILY_ASSET_CLASSES: dict[str, frozenset[str]] = {
    EXECUTION_FAMILY_COINBASE_SPOT: frozenset({"crypto"}),
    EXECUTION_FAMILY_ROBINHOOD_SPOT: frozenset({"equity"}),
    EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP: frozenset({"equity"}),
    EXECUTION_FAMILY_ALPACA_SPOT: frozenset({"equity", "crypto"}),
    # The short lane is equity-only (Alpaca cannot short crypto).
    EXECUTION_FAMILY_ALPACA_SHORT: frozenset({"equity"}),
}


def execution_family_supports_asset_class(execution_family: str, asset_class: str) -> bool:
    """True when the family can trade the asset class (alpaca: both). Unknown
    family -> False (fail closed at the cross-class gate)."""
    ef = normalize_execution_family(execution_family)
    return str(asset_class or "").strip().lower() in _EXECUTION_FAMILY_ASSET_CLASSES.get(ef, frozenset())


def asset_class_of_execution_family(execution_family: str) -> str:
    """'crypto' for coinbase_spot, 'equity' for the RH/Alpaca/MCP equity venues, else 'other'
    (the planned stub families). Used to validate a requested venue against the symbol's asset
    class — an equity may legitimately route to ANY equity venue, not just the default."""
    ef = normalize_execution_family(execution_family)
    if ef in _CRYPTO_EXECUTION_FAMILIES:
        return "crypto"
    if ef in _EQUITY_EXECUTION_FAMILIES:
        return "equity"
    return "other"


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
        EXECUTION_FAMILY_ALPACA_SHORT: (
            "Implemented (P0 adapter only): Alpaca SHORT lane — isolated execution family on the "
            "same AlpacaSpotAdapter with SELL_TO_OPEN / BUY_TO_CLOSE position-intent. PAPER-only "
            "(excluded from REAL_DAILY_LOSS_FAMILIES); gated behind chili_momentum_short_lane_enabled "
            "(default OFF). No momentum-lane short triggers wired yet (P1+)."
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
    ROUTABLE bearer is configured.

    Rail selection (``chili_equity_execution_rail``) is a conscious account-routing
    choice — which Robinhood account trades — defaulting to ``robinhood_spot`` so live
    equity flow is unchanged until the operator opts in.

    Activation is no longer mere token-presence: a bundle must be ROUTABLE
    (``bundle_is_routable`` — token + refresh present + not hard-dead, cheap/no-network)
    so a dead or refreshless bundle that can never recover headlessly does NOT select
    the rail. A legacy env / explicit raw token (no bundle file) still activates the rail.
    """
    try:
        from ...config import settings

        rail = str(getattr(settings, "chili_equity_execution_rail", "") or "").strip().lower()
    except Exception:
        return False
    if rail != EXECUTION_FAMILY_ROBINHOOD_AGENTIC_MCP:
        return False
    try:
        import os as _os

        from .venue.rh_mcp_client import bundle_is_routable

        # A routable on-disk bundle (refreshable) — the headless path.
        if bundle_is_routable():
            return True
        # Legacy raw token via env (no bundle file) still activates the rail.
        env = _os.environ.get("CHILI_ROBINHOOD_AGENTIC_MCP_TOKEN")
        return bool(env and env.strip())
    except Exception:
        return False


def resolve_execution_family_for_symbol(symbol: str, *, mode: str = "live") -> str:
    """Route a symbol to its execution family by asset class.

    Crypto pairs (BASE-USD) -> coinbase_spot (the proven momentum path). Equities
    (bare tickers — ARKK, CLSK, AAPL) -> ``robinhood_spot`` via the unofficial robin_stocks
    API by DEFAULT, or the officially-sanctioned ``robinhood_agentic_mcp`` rail when the
    operator selects it and a token is present (see ``_equity_rail_is_agentic_mcp``).

    PAPER equities -> ``alpaca_spot`` when the Alpaca paper rail is configured
    (docs/DESIGN/ALPACA_LANE.md): the soak that measures DMA-style limit-posting
    fill quality against the RH live lane on the SAME names, at zero risk.
    """
    try:
        from .venue.robinhood_spot import _is_crypto_product

        if _is_crypto_product(symbol):
            return EXECUTION_FAMILY_COINBASE_SPOT
        from ...config import settings

        if (
            str(mode or "").lower() == "paper"
            and bool(getattr(settings, "chili_alpaca_enabled", False))
            and bool(getattr(settings, "chili_alpaca_paper", True))
            and str(getattr(settings, "chili_alpaca_api_key", "") or "")
        ):
            return EXECUTION_FAMILY_ALPACA_SPOT
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
    if ef in (EXECUTION_FAMILY_ALPACA_SPOT, EXECUTION_FAMILY_ALPACA_SHORT):
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
    if ef in (EXECUTION_FAMILY_ALPACA_SPOT, EXECUTION_FAMILY_ALPACA_SHORT):
        # The short lane reuses the SAME Alpaca adapter; the runner sets the
        # per-order SELL_TO_OPEN / BUY_TO_CLOSE position_intent (SHORT_SIDE_LANE.md).
        from .venue.alpaca_spot import AlpacaSpotAdapter

        return AlpacaSpotAdapter
    raise ExecutionFamilyNotImplementedError(ef)
