"""Auto-synthesize option_meta from an equity alert.

The autotrader can translate a bullish equity ``pattern_imminent`` alert
into a long-call option entry. This module is the contract-selection layer:
it chooses expiration, searches nearby strikes, enforces liquidity and
premium budget, and only returns a contract when the option-specific
entry-quality model agrees with the underlying stop/target scenario.
"""
from __future__ import annotations

import logging
import math
import time
from collections import Counter
from datetime import date, datetime
from decimal import Decimal, ROUND_HALF_UP
from types import SimpleNamespace
from typing import Any, Optional

from sqlalchemy.orm import Session

from .contracts import normalize_expiration

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
NO_SURVIVOR_CACHE_PRICE_DECIMALS: int = 4
NO_SURVIVOR_CACHE_MONEY_DECIMALS: int = 2
NO_SURVIVOR_CACHE_RATIO_DECIMALS: int = 6
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
_NO_SURVIVOR_CACHE: dict[tuple[Any, ...], tuple[float, dict[str, Any]]] = {}


def _finite_float_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _positive_float_or_none(value: Any) -> float | None:
    out = _finite_float_or_none(value)
    if out is None or out <= 0.0:
        return None
    return out


def _clamped_float(
    value: Any,
    *,
    default: float,
    min_value: float | None = None,
    max_value: float | None = None,
) -> float:
    out = _finite_float_or_none(value)
    if out is None:
        out = float(default)
    if min_value is not None:
        out = max(float(min_value), out)
    if max_value is not None:
        out = min(float(max_value), out)
    return out


def _clamped_int(
    value: Any,
    *,
    default: int,
    min_value: int | None = None,
    max_value: int | None = None,
) -> int:
    out = int(
        _clamped_float(
            value,
            default=float(default),
            min_value=float(min_value) if min_value is not None else None,
            max_value=float(max_value) if max_value is not None else None,
        )
    )
    return out


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
        valid_expirations: list[tuple[str, date]] = []
        for expiration in expirations:
            exp = normalize_expiration(expiration)
            if not exp:
                continue
            try:
                exp_date = datetime.strptime(exp, "%Y-%m-%d").date()
            except Exception:
                continue
            if exp_date < today:
                continue
            valid_expirations.append((exp, exp_date))
        if not valid_expirations:
            return None

        def _gap(row: tuple[str, date]) -> int:
            _exp, exp_date = row
            return abs((exp_date - today).days - target_dte)

        return sorted(valid_expirations, key=_gap)[0][0]
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


def clear_synthesis_no_survivor_cache() -> None:
    """Clear the process-local synthesis miss cache for tests and restarts."""
    _NO_SURVIVOR_CACHE.clear()


def _cache_float(value: Optional[float], places: int) -> Optional[float]:
    value_f = _finite_float_or_none(value)
    if value_f is None:
        return None
    return round(value_f, places)


def _no_survivor_cache_ttl_seconds(settings: Any) -> float:
    from ....config import (
        AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TTL_SECONDS,
        AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_MAX_TTL_SECONDS,
        AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_MIN_TTL_SECONDS,
    )

    try:
        raw = getattr(
            settings,
            "chili_autotrader_options_synthesis_no_survivor_cache_ttl_seconds",
            AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TTL_SECONDS,
        )
    except Exception:
        raw = AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TTL_SECONDS
    return _clamped_float(
        raw,
        default=float(AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_DEFAULT_TTL_SECONDS),
        min_value=float(AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_MIN_TTL_SECONDS),
        max_value=float(AUTOTRADER_OPTIONS_SYNTHESIS_NO_SURVIVOR_CACHE_MAX_TTL_SECONDS),
    )


def _no_survivor_cache_key(
    *,
    sym: str,
    target_dte: int,
    max_spread_pct: float,
    strike_increment: float,
    base_strike: float,
    contract_budget_usd: float,
    underlying_target: Optional[float],
    underlying_stop: Optional[float],
    confidence: Optional[float],
) -> tuple[Any, ...]:
    return (
        "no_survivor_v1",
        sym,
        int(target_dte),
        _cache_float(max_spread_pct, NO_SURVIVOR_CACHE_RATIO_DECIMALS),
        _cache_float(strike_increment, NO_SURVIVOR_CACHE_PRICE_DECIMALS),
        _cache_float(base_strike, NO_SURVIVOR_CACHE_PRICE_DECIMALS),
        _cache_float(contract_budget_usd, NO_SURVIVOR_CACHE_MONEY_DECIMALS),
        _cache_float(underlying_target, NO_SURVIVOR_CACHE_PRICE_DECIMALS),
        _cache_float(underlying_stop, NO_SURVIVOR_CACHE_PRICE_DECIMALS),
        _cache_float(confidence, NO_SURVIVOR_CACHE_RATIO_DECIMALS),
    )


def _prune_no_survivor_cache(now: float) -> None:
    expired = [
        key
        for key, (expires_at, _payload) in _NO_SURVIVOR_CACHE.items()
        if expires_at <= now
    ]
    for key in expired:
        _NO_SURVIVOR_CACHE.pop(key, None)


def _no_survivor_cache_hit(
    cache_key: tuple[Any, ...],
    *,
    ttl_seconds: float,
    now: float,
) -> dict[str, Any] | None:
    if ttl_seconds <= 0:
        return None
    _prune_no_survivor_cache(now)
    cached = _NO_SURVIVOR_CACHE.get(cache_key)
    if cached is None:
        return None
    expires_at, payload = cached
    if expires_at <= now:
        _NO_SURVIVOR_CACHE.pop(cache_key, None)
        return None
    return payload


def _remember_no_survivor_cache(
    cache_key: tuple[Any, ...],
    *,
    ttl_seconds: float,
    now: float,
    payload: dict[str, Any],
) -> None:
    if ttl_seconds <= 0:
        return
    _prune_no_survivor_cache(now)
    _NO_SURVIVOR_CACHE[cache_key] = (now + ttl_seconds, dict(payload))


def _quote_prices(quote: dict[str, Any]) -> Optional[tuple[float, float, float, float]]:
    """Return bid, ask, mid, and spread percent from a broker quote."""
    bid = _quote_price_float(quote.get("bid_price"), default=0.0)
    ask = _quote_price_float(quote.get("ask_price"))
    if bid is None or ask is None:
        return None
    if bid < 0 or ask <= 0:
        return None
    if bid > 0 and bid > ask:
        return None
    mid = (bid + ask) / 2.0 if bid > 0 else ask
    spread_pct = (
        (ask - bid) / mid * PERCENT_SCALE
        if bid > 0 and mid > 0
        else NO_BID_SPREAD_PCT
    )
    return bid, ask, mid, spread_pct


def _quote_price_float(value: Any, *, default: float | None = None) -> Optional[float]:
    if value in (None, ""):
        return default
    return _finite_float_or_none(value)


def _sort_float(value: Any, *, default: float = 0.0) -> float:
    out = _finite_float_or_none(value)
    return out if out is not None else default


def _quality_sort_key(meta: dict[str, Any]) -> tuple[float, float, float, float]:
    """Rank accepted contracts without hidden weights."""
    quality = meta.get("entry_quality") if isinstance(meta.get("entry_quality"), dict) else {}
    ev_after_cost = quality.get("expected_value_after_cost_pct_of_premium")
    reward_after_cost = quality.get("option_reward_risk_after_cost")
    spread_pct = meta.get("synthesis_spread_pct")
    return (
        _sort_float(
            ev_after_cost
            if ev_after_cost is not None
            else quality.get("expected_value_pct_of_premium"),
            default=0.0,
        ),
        _sort_float(
            reward_after_cost
            if reward_after_cost is not None
            else quality.get("option_reward_risk"),
            default=0.0,
        ),
        -_sort_float(spread_pct, default=NO_BID_SPREAD_PCT),
        -_sort_float(meta.get("synthesis_contract_notional_usd"), default=0.0),
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
    spot_f = _positive_float_or_none(spot)
    notional_f = _positive_float_or_none(notional_usd)
    if not sym or spot_f is None or notional_f is None:
        return None
    target_f = _positive_float_or_none(underlying_target)
    stop_f = _positive_float_or_none(underlying_stop)
    confidence_f = _finite_float_or_none(confidence)
    quality_inputs = (underlying_target, underlying_stop, confidence)
    if any(value is not None for value in quality_inputs) and not all(
        value is not None for value in (target_f, stop_f, confidence_f)
    ):
        return None
    if confidence_f is not None:
        confidence_f = max(0.0, min(1.0, confidence_f))

    from ....config import settings
    from ..strategy_parameter import get_parameter
    from ..tick_normalizer import normalize_price
    from ..venue.robinhood_options import RobinhoodOptionsAdapter
    from .contracts import OPTION_CONTRACT_MULTIPLIER, normalize_option_meta
    from .entry_quality import evaluate_long_option_entry
    from .quote_store import create_chain_snapshot, record_quote_snapshot

    _register_synthesis_parameters(db)

    target_dte = _clamped_int(
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
        or DEFAULT_TARGET_DTE_DAYS,
        default=DEFAULT_TARGET_DTE_DAYS,
        min_value=int(TARGET_DTE_MIN_DAYS),
        max_value=int(TARGET_DTE_MAX_DAYS),
    )
    max_spread_pct = _clamped_float(
        get_parameter(
            db,
            STRATEGY_FAMILY,
            "synthesis_max_spread_pct",
            default=DEFAULT_MAX_SPREAD_PCT,
        )
        or DEFAULT_MAX_SPREAD_PCT,
        default=DEFAULT_MAX_SPREAD_PCT,
        min_value=MAX_SPREAD_MIN_PCT,
        max_value=MAX_SPREAD_MAX_PCT,
    )
    strike_increment = _clamped_float(
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
        or DEFAULT_STRIKE_INCREMENT,
        default=DEFAULT_STRIKE_INCREMENT,
        min_value=STRIKE_INCREMENT_MIN,
        max_value=STRIKE_INCREMENT_MAX,
    )

    max_contract_notional_usd = _clamped_float(
        getattr(
            settings,
            "chili_autotrader_options_max_contract_notional_usd",
            DEFAULT_MAX_CONTRACT_NOTIONAL_USD,
        )
        or DEFAULT_MAX_CONTRACT_NOTIONAL_USD,
        default=DEFAULT_MAX_CONTRACT_NOTIONAL_USD,
        min_value=0.0,
    )
    contract_budget_usd = (
        min(notional_f, max_contract_notional_usd)
        if max_contract_notional_usd > 0
        else notional_f
    )
    base_strike = _pick_strike(spot_f, increment=strike_increment)
    candidate_strikes = _candidate_strikes(base_strike, strike_increment)
    cache_ttl_s = _no_survivor_cache_ttl_seconds(settings)
    cache_key = _no_survivor_cache_key(
        sym=sym,
        target_dte=target_dte,
        max_spread_pct=max_spread_pct,
        strike_increment=strike_increment,
        base_strike=base_strike,
        contract_budget_usd=contract_budget_usd,
        underlying_target=target_f,
        underlying_stop=stop_f,
        confidence=confidence_f,
    )
    now = time.monotonic()
    cached_reject = _no_survivor_cache_hit(
        cache_key,
        ttl_seconds=cache_ttl_s,
        now=now,
    )
    if cached_reject is not None:
        logger.info(
            "[options_synth] %s: recent no-survivor context cached; "
            "ttl_s=%.1f base_strike=%s rejects=%s",
            sym,
            cache_ttl_s,
            base_strike,
            cached_reject.get("rejects") or {},
        )
        return None

    adapter = RobinhoodOptionsAdapter()
    expiration = _pick_expiration(adapter, sym, target_dte=target_dte)
    if not expiration:
        logger.info("[options_synth] %s: no expiration near %dDTE; skipping", sym, target_dte)
        _remember_no_survivor_cache(
            cache_key,
            ttl_seconds=cache_ttl_s,
            now=now,
            payload={"rejects": {"no_expiration": 1}},
        )
        return None

    accepted: list[dict[str, Any]] = []
    reject_counts: Counter[str] = Counter()
    chain_snapshot_id = create_chain_snapshot(
        db,
        underlying=sym,
        expiration=expiration,
        venue="robinhood",
        spot_price=spot_f,
        n_contracts=len(candidate_strikes),
    )

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
        bid, ask, mid, spread_pct = prices
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
            "option_id": contract.get("id"),
            "strike": strike,
            "expiration": expiration,
            "option_type": "call",
            "limit_price": limit_price,
            "quantity": int(contracts),
            "synthesis_source": "equity_substitute",
            "synthesis_target_dte": target_dte,
            "synthesis_max_spread_pct": max_spread_pct,
            "synthesis_spread_pct": round(spread_pct, 3),
            "synthesis_bid": round(bid, 4),
            "synthesis_ask": round(ask, 4),
            "synthesis_mid": round(mid, 4),
            "synthesis_spot_at_pick": round(spot_f, 4),
            "synthesis_contract_notional_usd": round(contract_notional_usd, 2),
            "synthesis_budget_usd": round(contract_budget_usd, 2),
            "synthesis_candidate_count": len(candidate_strikes),
        }
        meta = normalize_option_meta(
            meta,
            underlying=sym,
            current_underlying_price=spot_f,
            quote=quote,
        )
        quote_recorded = record_quote_snapshot(
            db,
            chain_id=chain_snapshot_id,
            option_meta=meta,
            quote=quote,
        )
        meta["quote_snapshot_recorded"] = bool(quote_recorded)
        if chain_snapshot_id is not None:
            meta["chain_snapshot_id"] = int(chain_snapshot_id)

        if (
            target_f is not None
            and stop_f is not None
            and confidence_f is not None
        ):
            quality_alert = SimpleNamespace(
                entry_price=limit_price,
                target_price=target_f,
                stop_loss=stop_f,
            )
            quality = evaluate_long_option_entry(
                db,
                alert=quality_alert,
                option_meta=meta,
                current_underlying_price=spot_f,
                confidence=confidence_f,
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
        _remember_no_survivor_cache(
            cache_key,
            ttl_seconds=cache_ttl_s,
            now=now,
            payload={
                "expiration": expiration,
                "base_strike": base_strike,
                "rejects": dict(reject_counts),
            },
        )
        return None

    _NO_SURVIVOR_CACHE.pop(cache_key, None)
    selected = max(accepted, key=_quality_sort_key)
    selected["synthesis_selected_by"] = (
        "expected_value_after_cost_then_reward_risk_after_cost_then_spread"
    )
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


__all__ = ["clear_synthesis_no_survivor_cache", "synthesize_option_meta"]
