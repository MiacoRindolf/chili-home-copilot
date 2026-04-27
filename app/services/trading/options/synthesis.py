"""Auto-synthesize option_meta from an equity alert.

Phase 3 of the options integration. Lets the autotrader translate a
bullish equity pattern_imminent alert into a long-call option entry
without operator pre-filling option metadata.

The tunable parameters (DTE target, max spread, etc.) are NOT
hardcoded — they're registered in the StrategyParameter ledger
(family='autotrader_options') so the brain's learning loop can adapt
them from realized outcomes the same way it adapts confidence_floor
in Q2.T4. Env values (``chili_autotrader_options_*``) are first-run
bootstraps; once the parameter exists in the DB, the DB value wins.

Design choices for the minimum viable version:

* **Direction**: always bullish (long call). The autotrader doesn't
  fire bearish equity entries today (no short selling), so the only
  direction available is bullish.
* **Strike**: ATM (closest available strike at-or-above the current
  spot). ATM has the most reliable liquidity + Greeks behave
  predictably.
* **Expiration**: nearest tradable expiration to the brain-tuned DTE
  target.
* **Quantity**: notional / (premium × 100), minimum 1 contract.
* **Liquidity check**: reject if spread > brain-tuned max spread.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Any, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# StrategyParameter family used for all option synthesis + exit knobs.
# Keep this stable — the learning loop's per-family aggregations key on
# it, and renaming would orphan the historical adaptation curve.
STRATEGY_FAMILY = "autotrader_options"


def _register_synthesis_parameters(db: Session) -> None:
    """Idempotent registration of the synthesis params. Bootstraps with
    env values on first call; subsequent calls no-op when the rows
    already exist (the DB value is authoritative thereafter).
    """
    try:
        from ....config import settings
        from ..strategy_parameter import ParameterSpec, register_parameter

        register_parameter(db, ParameterSpec(
            strategy_family=STRATEGY_FAMILY,
            parameter_key="synthesis_target_dte",
            initial_value=float(getattr(settings, "chili_autotrader_options_substitute_dte", 30)),
            min_value=7.0, max_value=90.0,
            description=(
                "Target days-to-expiration for substituted long calls. "
                "Lower = less theta but more gamma exposure. The brain "
                "adapts within [7, 90] from realized PnL of substituted "
                "entries vs the equity alternative they replaced."
            ),
        ))
        register_parameter(db, ParameterSpec(
            strategy_family=STRATEGY_FAMILY,
            parameter_key="synthesis_max_spread_pct",
            initial_value=15.0,
            min_value=3.0, max_value=30.0,
            description=(
                "Maximum bid-ask spread (% of mid) below which a "
                "synthesized contract is considered tradable. Tighter "
                "= cleaner fills, fewer eligible alerts. Brain adapts "
                "from realized fill quality (entry slippage) vs the "
                "rejected-eligible-pool size."
            ),
        ))
        register_parameter(db, ParameterSpec(
            strategy_family=STRATEGY_FAMILY,
            parameter_key="synthesis_strike_increment",
            initial_value=5.0,
            min_value=1.0, max_value=10.0,
            description=(
                "Strike rounding increment when picking ATM. SPY/QQQ "
                "have $5 strikes far from spot; small caps have $1. "
                "Brain adapts per-ticker (scope=ticker) from "
                "find_contract retry count."
            ),
        ))
    except Exception as e:
        logger.debug("[options_synth] _register_synthesis_parameters failed: %s", e)


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
    db: Session,
    underlying: str,
    spot: float,
    notional_usd: float,
) -> Optional[dict[str, Any]]:
    """Build an ``option_meta`` dict from an equity context, or None to
    skip (illiquid, no chain, etc.).

    Tunables (DTE target, max spread, strike increment) come from the
    StrategyParameter ledger (family='autotrader_options'). The first
    call seeds them from env; subsequent calls trust the DB value the
    brain's learning loop has adapted from realized outcomes.

    Returned dict carries the fields the rule gate validates +
    `_execute_broker_buy` reads:

      strike, expiration, option_type, limit_price, quantity (contracts)

    Plus diagnostic fields the audit row will surface:

      synthesis_source = 'equity_substitute'
      synthesis_target_dte
      synthesis_spread_pct
      synthesis_spot_at_pick
    """
    sym = (underlying or "").strip().upper()
    if not sym or spot <= 0 or notional_usd <= 0:
        return None

    # Lazy imports — robin_stocks may not be ready in test contexts.
    from ..venue.robinhood_options import RobinhoodOptionsAdapter
    from ....config import settings
    from ..strategy_parameter import get_parameter

    _register_synthesis_parameters(db)

    target_dte = int(get_parameter(
        db, STRATEGY_FAMILY, "synthesis_target_dte",
        default=float(getattr(settings, "chili_autotrader_options_substitute_dte", 30)),
    ) or 30)
    max_spread_pct = float(get_parameter(
        db, STRATEGY_FAMILY, "synthesis_max_spread_pct",
        default=15.0,
    ) or 15.0)
    strike_increment = float(get_parameter(
        db, STRATEGY_FAMILY, "synthesis_strike_increment",
        scope="ticker", scope_value=sym,
        default=float(get_parameter(
            db, STRATEGY_FAMILY, "synthesis_strike_increment",
            default=5.0,
        ) or 5.0),
    ) or 5.0)

    adapter = RobinhoodOptionsAdapter()
    expiration = _pick_expiration(adapter, sym, target_dte=target_dte)
    if not expiration:
        logger.info("[options_synth] %s: no expiration ~%dDTE; skipping", sym, target_dte)
        return None

    target_strike = _pick_strike(spot, increment=strike_increment)
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

    # Limit price = ask (cross the spread to fill cleanly).
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
        "synthesis_max_spread_pct": max_spread_pct,
        "synthesis_spread_pct": round(spread_pct, 3),
        "synthesis_spot_at_pick": round(spot, 4),
    }


__all__ = ["synthesize_option_meta"]
