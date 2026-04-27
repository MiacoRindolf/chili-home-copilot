"""Auto-synthesize option_meta from an equity alert.

Phase 3 of the options integration. Lets the autotrader translate a
bullish equity pattern_imminent alert into a long-call option entry
without operator pre-filling option metadata.

Design choices for the minimum viable version:

* **Direction**: always bullish (long call). The autotrader doesn't
  fire bearish equity entries today (no short selling), so the only
  direction available is bullish. Phase 4 / future work can add put
  synthesis for bearish patterns once the autotrader supports them.
* **Strike**: ATM (closest available strike at-or-above the current
  spot). ATM has the most reliable liquidity + Greeks behave
  predictably. Future work: skew toward OTM for higher leverage.
* **Expiration**: nearest tradable expiration ~30 DTE out. Far enough
  that theta isn't catastrophic, near enough that gamma matters.
* **Quantity**: notional / (premium × 100), minimum 1 contract.
* **Liquidity check**: reject if ask is None / 0, or if spread > 15%
  of mid. Bad liquidity = bad fill = avoid.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

logger = logging.getLogger(__name__)


def _pick_expiration(adapter, underlying: str, target_dte: int = 30) -> Optional[str]:
    """Find the closest available expiration to ``target_dte`` days from
    today. Returns ISO ``YYYY-MM-DD`` or None.

    Uses ``rh.options.get_chains`` since the adapter doesn't expose
    expirations directly. Best-effort — None means caller should skip
    synthesis entirely.
    """
    try:
        import robin_stocks.robinhood as rh
        chains = rh.options.get_chains((underlying or "").strip().upper())
        if not isinstance(chains, dict):
            return None
        exps = chains.get("expiration_dates") or []
        if not exps:
            return None
        today = datetime.utcnow().date()

        def _gap(s: str) -> int:
            try:
                return abs((datetime.strptime(s, "%Y-%m-%d").date() - today).days - target_dte)
            except Exception:
                return 9999

        return sorted(exps, key=_gap)[0]
    except Exception as e:
        logger.debug("[options_synth] _pick_expiration(%s) failed: %s", underlying, e)
        return None


def _pick_strike(spot: float, increment: float = 5.0) -> float:
    """Round spot UP to the nearest standard strike interval. Most
    high-liquidity names (SPY, QQQ, AAPL, etc.) use $5 increments at
    far strikes and $1 at near. We default to $5 so we hit the most
    liquid strikes; the contract finder will retry adjacent strikes
    if this exact one isn't listed.
    """
    if spot <= 0:
        return 0.0
    return round(round(spot / increment + 0.5) * increment, 2)


def synthesize_option_meta(
    *,
    underlying: str,
    spot: float,
    notional_usd: float,
    target_dte: int = 30,
    max_spread_pct: float = 15.0,
) -> Optional[dict[str, Any]]:
    """Build an ``option_meta`` dict from an equity context, or None to
    skip (illiquid, no chain, etc.).

    Returned dict carries the fields the rule gate validates +
    `_execute_broker_buy` reads:

      strike, expiration, option_type, limit_price, quantity (contracts)

    Plus diagnostic fields the audit row will surface:

      synthesis_source = 'equity_substitute'
      synthesis_target_dte
      synthesis_spread_pct
    """
    sym = (underlying or "").strip().upper()
    if not sym or spot <= 0 or notional_usd <= 0:
        return None

    # Lazy import — robin_stocks may not be ready in test contexts.
    from ..venue.robinhood_options import RobinhoodOptionsAdapter

    adapter = RobinhoodOptionsAdapter()
    expiration = _pick_expiration(adapter, sym, target_dte=target_dte)
    if not expiration:
        logger.info("[options_synth] %s: no expiration ~%dDTE; skipping", sym, target_dte)
        return None

    target_strike = _pick_strike(spot, increment=5.0)
    contract = adapter.find_contract(sym, expiration, target_strike, "call")
    if not contract:
        # Try $1 increments around target as fallback for low-priced names.
        for offset in (1.0, -1.0, 2.0, -2.0, 5.0, -5.0):
            alt = round(target_strike + offset, 2)
            contract = adapter.find_contract(sym, expiration, alt, "call")
            if contract:
                target_strike = alt
                break
    if not contract:
        logger.info(
            "[options_synth] %s %s: no tradable call near strike %s; skipping",
            sym, expiration, target_strike,
        )
        return None

    quote = adapter.get_quote(str(contract.get("id", "")))
    if not quote:
        logger.info("[options_synth] %s %s%s: no quote; skipping", sym, expiration, target_strike)
        return None
    try:
        bid = float(quote.get("bid_price") or 0)
        ask = float(quote.get("ask_price") or 0)
    except (TypeError, ValueError):
        return None
    if ask <= 0:
        return None
    mid = (bid + ask) / 2.0 if bid > 0 else ask
    spread_pct = ((ask - bid) / mid * 100.0) if (bid > 0 and mid > 0) else 100.0
    if spread_pct > max_spread_pct:
        logger.info(
            "[options_synth] %s %s%s: spread %.1f%% > %.1f%%; illiquid, skipping",
            sym, expiration, target_strike, spread_pct, max_spread_pct,
        )
        return None

    # Limit price = ask (cross the spread to fill cleanly). For smoke
    # safety the operator-set limit_price overrides via opt_meta.
    limit_price = ask
    cost_per_contract_usd = limit_price * 100.0
    contracts = max(1, int(notional_usd // cost_per_contract_usd))

    return {
        "underlying": sym,
        "strike": target_strike,
        "expiration": expiration,
        "option_type": "call",
        "limit_price": round(limit_price, 2),
        "quantity": int(contracts),
        "synthesis_source": "equity_substitute",
        "synthesis_target_dte": target_dte,
        "synthesis_spread_pct": round(spread_pct, 3),
        "synthesis_spot_at_pick": round(spot, 4),
    }


__all__ = ["synthesize_option_meta"]
