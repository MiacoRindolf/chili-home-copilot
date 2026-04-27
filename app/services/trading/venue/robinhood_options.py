"""Robinhood options venue adapter (Task MM Phase 1).

Sister to ``robinhood_spot.py`` but for the options API path. Uses the
same equity-scope OAuth token that ``broker_service`` already maintains —
RH options live at ``api.robinhood.com/options/`` so no separate auth
hop like crypto's nummus.

Phase 1 scope: single-leg long calls/puts only (buying-to-open and
selling-to-close). Multi-leg strategies (verticals, iron condors) need
sequential leg orchestration at the strategy layer; that's Phase 3.

The protocol surface here is intentionally narrower than the equity
adapter — the autotrader's stock pipeline expects ``place_market_order``
with a single product_id, but options orders are identified by a
4-tuple (underlying, expiration, strike, type) and a separate quantity
in CONTRACTS, not shares. So we expose option-specific entry points
``place_option_buy`` / ``place_option_sell`` rather than overloading
the existing protocol.

Flag-gated: ``CHILI_OPTIONS_VENUE_ROBINHOOD_ENABLED`` (default OFF).
The options strategy layer should consult this before routing through
this adapter; without the flag the existing Tradier path stays in
control.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

_VENUE = "robinhood_options"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class RobinhoodOptionsAdapter:
    """Single-leg options entry/exit through Robinhood.

    Not registered in ``venue/factory.py`` yet — Phase 1 keeps this
    adapter accessible only to the options lane's strategy layer, so the
    autotrader's existing equity flow (``get_adapter('robinhood')``) is
    never silently re-routed through here. Phase 2 wires it into the
    factory once paper-mode round-trips have been validated.
    """

    def is_enabled(self) -> bool:
        """True when the operator has flipped the venue flag AND the
        underlying RH session is connected. Both checks live here so
        callers can do a single ``is_enabled()`` gate before any order
        plumbing runs.
        """
        from ....config import settings
        from ...broker_service import is_connected

        return bool(
            getattr(settings, "chili_options_venue_robinhood_enabled", False)
        ) and is_connected()

    # ── Market data ────────────────────────────────────────────────────

    def find_contract(
        self,
        underlying: str,
        expiration: str,
        strike: float,
        option_type: str,
    ) -> Optional[dict[str, Any]]:
        """Locate a specific option contract. Returns the raw RH instrument
        dict or None. The instrument dict's ``id`` field is what
        ``get_quote`` consumes downstream.
        """
        from ...broker_service import find_option_contract
        return find_option_contract(underlying, expiration, strike, option_type)

    def get_quote(self, option_id: str) -> Optional[dict[str, Any]]:
        """Return market data for a contract by RH id (bid/ask/IV/greeks)."""
        from ...broker_service import get_option_quote
        return get_option_quote(option_id)

    def get_mid_price(
        self,
        underlying: str,
        expiration: str,
        strike: float,
        option_type: str,
    ) -> Optional[float]:
        """Convenience: locate contract + fetch quote + return (bid+ask)/2.

        Returns None when RH has no quote (illiquid contract, weekend,
        post-market lock, etc.). Callers should treat None as
        ``do not submit`` rather than ``submit at any price``.
        """
        contract = self.find_contract(underlying, expiration, strike, option_type)
        if not contract:
            return None
        oid = contract.get("id")
        if not oid:
            return None
        q = self.get_quote(str(oid))
        if not q:
            return None
        try:
            bid = float(q.get("bid_price") or 0)
            ask = float(q.get("ask_price") or 0)
            if bid > 0 and ask > 0:
                return (bid + ask) / 2.0
            mark = float(q.get("mark_price") or 0)
            if mark > 0:
                return mark
        except (TypeError, ValueError):
            return None
        return None

    # ── Orders ─────────────────────────────────────────────────────────

    def place_option_buy(
        self,
        *,
        underlying: str,
        expiration: str,
        strike: float,
        option_type: str,
        quantity: int,
        limit_price: float,
        time_in_force: str = "gtc",
    ) -> dict[str, Any]:
        """Open a long call/put. Returns ``{ok: bool, order_id: str, state: str, ...}``.

        The autotrader's audit log treats ``ok=False`` as ``blocked`` and
        records the ``error`` field as the reason. Clean failure modes
        we surface here:
          - ``option_contract_not_found:<symbol_expr_strike_type>`` —
            typo or RH simply doesn't list it
          - ``Robinhood options endpoint returned no order_id`` — same
            shape as the AXS/ALGO crypto rejections; usually indicates
            account-side approval gap
          - ``Not connected to Robinhood`` — session expired
        """
        from ...broker_service import place_option_buy_order
        return place_option_buy_order(
            underlying=underlying,
            expiration=expiration,
            strike=strike,
            option_type=option_type,
            quantity=int(quantity),
            limit_price=float(limit_price),
            time_in_force=time_in_force,
        )

    def place_option_sell(
        self,
        *,
        underlying: str,
        expiration: str,
        strike: float,
        option_type: str,
        quantity: int,
        limit_price: float,
        position_effect: str = "close",
        time_in_force: str = "gtc",
    ) -> dict[str, Any]:
        """Close a long, or open a short call/put.

        ``position_effect='close'`` (default) — selling-to-close a long
        position we own. Doesn't require Level 3+ approval.

        ``position_effect='open'`` — selling-to-open a short. Requires
        higher RH approval level + meaningful margin/cash. Use only for
        explicit covered-call or naked-put strategies where the operator
        has acknowledged the additional risk envelope.
        """
        from ...broker_service import place_option_sell_order
        return place_option_sell_order(
            underlying=underlying,
            expiration=expiration,
            strike=strike,
            option_type=option_type,
            quantity=int(quantity),
            limit_price=float(limit_price),
            position_effect=position_effect,
            time_in_force=time_in_force,
        )

    def cancel(self, order_id: str) -> dict[str, Any]:
        from ...broker_service import cancel_option_order
        return cancel_option_order(order_id)

    # ── Multi-leg spreads (Phase 4) ────────────────────────────────────

    def place_spread(
        self,
        *,
        underlying: str,
        legs: list[dict[str, Any]],
        quantity: int,
        limit_price: float,
        direction: str = "debit",
        time_in_force: str = "gtc",
    ) -> dict[str, Any]:
        """Submit a multi-leg spread (vertical, iron condor, etc.) as a
        single atomic order. Each leg is a dict with the keys
        ``expiration``, ``strike``, ``option_type`` (call|put),
        ``action`` (buy|sell), and optionally ``effect`` (open|close,
        default open).

        Direction is 'debit' (net pay) or 'credit' (net receive). The
        strategy layer (Q2.T1 vertical_spread, iron_condor, etc.)
        decides which based on the leg combination; the adapter just
        submits.

        Returns the same envelope shape as ``place_option_buy``.
        """
        from ...broker_service import place_option_spread
        return place_option_spread(
            legs=legs,
            underlying=underlying,
            quantity=int(quantity),
            limit_price=float(limit_price),
            direction=direction,
            time_in_force=time_in_force,
        )

    # ── Position queries ───────────────────────────────────────────────

    def get_open_positions(self) -> list[dict[str, Any]]:
        """List currently-held option legs. Used by the reconciler to
        match Trade rows against broker truth.
        """
        from ...broker_service import get_open_option_positions
        return get_open_option_positions()


__all__ = ["RobinhoodOptionsAdapter"]
