"""Auto-synthesize option_meta from an equity alert.

The autotrader can translate a bullish equity ``pattern_imminent`` alert
into a long-call option entry. This module is the contract-selection layer:
it chooses expiration, searches nearby strikes, enforces liquidity and
premium budget, and only returns a contract when the option-specific
entry-quality model agrees with the underlying stop/target scenario.
"""
from __future__ import annotations

import logging
from collections import Counter
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

STRATEGY_FAMILY = "autotrader_options"

DEFAULT_TARGET_DTE_DAYS: int = 30
TARGET_DTE_MIN_DAYS: float = 7.0
TARGET_DTE_MAX_DAYS: float = 90.0

DEFAULT_MAX_SPREAD_PCT: float = 15.0
MAX_SPREAD_MIN_PCT: float = 3.0
MAX_SPREAD_MAX_PCT: float = 30.0

DEFAULT_STRIKE_INCREMENT: float = 5.0
STRIKE_INCREMENT_MIN: float = 1.0
STRIKE_INCREMENT_MAX: float = 10.0

# 0.0 means "use the autotrader per-trade notional as the contract cap".
DEFAULT_MAX_CONTRACT_NOTIONAL_USD: float = 0.0
PERCENT_SCALE: float = 100.0
NO_BID_SPREAD_PCT: float = 100.0
STRIKE_SEARCH_OFFSETS: tuple[float, ...] = (
    0.0,
    -1.0,
    1.0,
    -2.0,
    2.0,
    -3.0,
    3.0,
)
STRIKE_QUANTUM = Decimal("0.01")


def _register_synthesis_parameters(db: Session) -> None:
    """Idempotently register the adaptive synthesis knobs."""
    try:
        from ....config import settings
        from ..strategy_parameter import ParameterSpec, register_parameter

        register_parameter(
            db,
            ParameterSpec(
                strategy_family=STRATEGY_FAMILY,
                parameter_key="synthesis_target_dte",
                initial_value=float(
                    getattr(
                        settings,
                        "chili_autotrader_options_substitute_dte",
                        DEFAULT_TARGET_DTE_DAYS,
                    )
                ),
                min_value=TARGET_DTE_MIN_DAYS,
                max_value=TARGET_DTE_MAX_DAYS,
                description=(
                    "Target days-to-expiration for substituted long calls. "
                    "The learner adapts this from realized option outcomes."
                ),
            ),
        )
        register_parameter(
            db,
            ParameterSpec(
                strategy_family=STRATEGY_FAMILY,
                parameter_key="synthesis_max_spread_pct",
                initial_value=DEFAULT_MAX_SPREAD_PCT,
                min_value=MAX_SPREAD_MIN_PCT,
                max_value=MAX_SPREAD_MAX_PCT,
                description=(
                    "Maximum bid-ask spread as a percent of mid for an "
                    "eligible substituted option contract."
                ),
            ),
        )
        register_parameter(
            db,
            ParameterSpec(
                strategy_family=STRATEGY_FAMILY,
                parameter_key="synthesis_strike_increment",
                initial_value=DEFAULT_STRIKE_INCREMENT,
                min_value=STRIKE_INCREMENT_MIN,
                max_value=STRIKE_INCREMENT_MAX,
                description=(
                    "Strike rounding increment used to build the nearby "
                    "call search set for option substitutions."
                ),
            ),
        )
    except Exception as exc:
        logger.debug("[options_synth] parameter registration failed: %s", exc)


def _pick_expiration(_adapter: Any, underlying: str, target_dte: int = DEFAULT_TARGET_DTE_DAYS) -> Optional[str]:
    """Find the listed expiration closest to ``target_dte`` calendar days."""
    try:
        from ... import broker_service as broker_service

        chains = broker_service.get_option_chains((underlying or "").strip().upper())
        if not isinstance(chains, dict):
            return None
        expirations = chains.get("expiration_dates") or []
        if not expirations:
            return None
        today = datetime.utcnow().date()

        def _gap(expiration: str) -> int:
            try:
                exp_date = datetime.strptime(expiration, "%Y-%m-%d").date()
                return abs((exp_date - today).days - target_dte)
            except Exception:
                return 9999

        return sorted(expirations, key=_gap)[0]
    except Exception as exc:
        logger.debug("[options_synth] expiration pick failed for %s: %s", underlying, exc)
        return None


def _quantize_strike(value: float) -> float:
    return float(Decimal(str(value)).quantize(STRIKE_QUANTUM, rounding=ROUND_HALF_UP))


def _pick_strike(spot: float, increment: float = DEFAULT_STRIKE_INCREMENT) -> float:
    """Round spot up to the nearest configured strike interval."""
    if spot <= 0 or increment <= 0:
        return 0.0
    steps = int(spot // increment)
    strike = steps * increment
    if strike < spot:
        strike += increment
    return _quantize_strike(strike)


def _candidate_strikes(base_strike: float, increment: float) -> list[float]:
    """Return a small symmetric strike search around the ATM anchor."""
    seen: set[float] = set()
    strikes: list[float] = []
    for offset in STRIKE_SEARCH_OFFSETS:
        strike = _quantize_strike(base_strike + offset * increment)
        if strike <= 0 or strike in seen:
            continue
        seen.add(strike)
        strikes.append(strike)
    return strikes


def _quote_prices(quote: dict[str, Any]) -> Optional[tuple[float, float, float, float]]:
    """Return bid, ask, mid, and spread percent from a broker quote."""
    try:
        bid = float(quote.get("bid_price") or 0)
        ask = float(quote.get("ask_price") or 0)
    except (TypeError, ValueError):
        return None
    if ask <= 0:
        return None
    mid = (bid + ask) / 2.0 if bid > 0 else ask
    spread_pct = (
        (ask - bid) / mid * PERCENT_SCALE
        if bid > 0 and mid > 0
        else NO_BID_SPREAD_PCT
    )
    return bid, ask, mid, spread_pct


def _quality_sort_key(meta: dict[str, Any]) -> tuple[float, float, float, float]:
    """Rank accepted contracts without hidden weights."""
    quality = meta.get("entry_quality") if isinstance(meta.get("entry_quality"), dict) else {}
    return (
        float(quality.get("expected_value_pct_of_premium") or 0.0),
        float(quality.get("option_reward_risk") or 0.0),
        -float(meta.get("synthesis_spread_pct") or NO_BID_SPREAD_PCT),
        -float(meta.get("synthesis_contract_notional_usd") or 0.0),
    )


def synthesize_option_meta(
    *,
    db: Session,
    underlying: str,
    spot: float,
    notional_usd: float,
    underlying_target: Optional[float] = None,
    underlying_stop: Optional[float] = None,
    confidence: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Build an ``option_meta`` dict from an equity context.

    Returns None when the chain, quote, liquidity, budget, or entry-quality
    gates reject all nearby contracts.
    """
    sym = (underlying or "").strip().upper()
    if not sym or spot <= 0 or notional_usd <= 0:
        return None

    from ....config import settings
    from ..strategy_parameter import get_parameter
    from ..tick_normalizer import normalize_price
    from ..venue.robinhood_options import RobinhoodOptionsAdapter
    from .entry_quality import OPTION_CONTRACT_MULTIPLIER, evaluate_long_option_entry

    _register_synthesis_parameters(db)

    target_dte = int(
        get_parameter(
            db,
            STRATEGY_FAMILY,
            "synthesis_target_dte",
            default=float(
                getattr(
                    settings,
                    "chili_autotrader_options_substitute_dte",
                    DEFAULT_TARGET_DTE_DAYS,
                )
            ),
        )
        or DEFAULT_TARGET_DTE_DAYS
    )
    max_spread_pct = float(
        get_parameter(
            db,
            STRATEGY_FAMILY,
            "synthesis_max_spread_pct",
            default=DEFAULT_MAX_SPREAD_PCT,
        )
        or DEFAULT_MAX_SPREAD_PCT
    )
    strike_increment = float(
        get_parameter(
            db,
            STRATEGY_FAMILY,
            "synthesis_strike_increment",
            scope="ticker",
            scope_value=sym,
            default=float(
                get_parameter(
                    db,
                    STRATEGY_FAMILY,
                    "synthesis_strike_increment",
                    default=DEFAULT_STRIKE_INCREMENT,
                )
                or DEFAULT_STRIKE_INCREMENT
            ),
        )
        or DEFAULT_STRIKE_INCREMENT
    )

    max_contract_notional_usd = float(
        getattr(
            settings,
            "chili_autotrader_options_max_contract_notional_usd",
            DEFAULT_MAX_CONTRACT_NOTIONAL_USD,
        )
        or DEFAULT_MAX_CONTRACT_NOTIONAL_USD
    )
    contract_budget_usd = (
        min(notional_usd, max_contract_notional_usd)
        if max_contract_notional_usd > 0
        else notional_usd
    )

    adapter = RobinhoodOptionsAdapter()
    expiration = _pick_expiration(adapter, sym, target_dte=target_dte)
    if not expiration:
        logger.info("[options_synth] %s: no expiration near %dDTE; skipping", sym, target_dte)
        return None

    base_strike = _pick_strike(spot, increment=strike_increment)
    accepted: list[dict[str, Any]] = []
    reject_counts: Counter[str] = Counter()
    candidate_strikes = _candidate_strikes(base_strike, strike_increment)

    for strike in candidate_strikes:
        contract = adapter.find_contract(sym, expiration, strike, "call")
        if not contract:
            reject_counts["no_contract"] += 1
            continue

        quote = adapter.get_quote(str(contract.get("id", "")))
        if not quote:
            reject_counts["no_quote"] += 1
            continue
        prices = _quote_prices(quote)
        if prices is None:
            reject_counts["bad_quote"] += 1
            continue
        _bid, ask, _mid, spread_pct = prices
        if spread_pct > max_spread_pct:
            reject_counts["spread_above_max"] += 1
            continue

        limit_price = normalize_price(ask, sym, asset_class="option")
        contract_notional_usd = limit_price * OPTION_CONTRACT_MULTIPLIER
        contracts = int(contract_budget_usd // contract_notional_usd)
        if contracts < 1:
            reject_counts["contract_cost_above_budget"] += 1
            continue

        meta = {
            "underlying": sym,
            "strike": strike,
            "expiration": expiration,
            "option_type": "call",
            "limit_price": limit_price,
            "quantity": int(contracts),
            "synthesis_source": "equity_substitute",
            "synthesis_target_dte": target_dte,
            "synthesis_max_spread_pct": max_spread_pct,
            "synthesis_spread_pct": round(spread_pct, 3),
            "synthesis_spot_at_pick": round(spot, 4),
            "synthesis_contract_notional_usd": round(contract_notional_usd, 2),
            "synthesis_budget_usd": round(contract_budget_usd, 2),
            "synthesis_candidate_count": len(candidate_strikes),
        }

        if (
            underlying_target is not None
            and underlying_stop is not None
            and confidence is not None
        ):
            quality_alert = SimpleNamespace(
                entry_price=limit_price,
                target_price=underlying_target,
                stop_loss=underlying_stop,
            )
            quality = evaluate_long_option_entry(
                db,
                alert=quality_alert,
                option_meta=meta,
                current_underlying_price=spot,
                confidence=float(confidence),
                settings=settings,
            )
            meta["entry_quality"] = quality.snapshot
            if not quality.accepted:
                reject_counts[f"quality:{quality.reason}"] += 1
                continue

        accepted.append(meta)

    if not accepted:
        logger.info(
            "[options_synth] %s %s: no call survived selection near strike %s; rejects=%s",
            sym,
            expiration,
            base_strike,
            dict(reject_counts),
        )
        return None

    selected = max(accepted, key=_quality_sort_key)
    selected["synthesis_selected_by"] = "expected_value_then_reward_risk_then_spread"
    selected["synthesis_reject_counts"] = dict(reject_counts)
    logger.info(
        "[options_synth] %s %s selected %sC qty=%s limit=%.2f budget=%.2f rejects=%s",
        sym,
        expiration,
        selected["strike"],
        selected["quantity"],
        selected["limit_price"],
        selected["synthesis_budget_usd"],
        dict(reject_counts),
    )
    return selected


__all__ = ["synthesize_option_meta"]
