"""Option-aware entry quality checks for AutoTrader option substitutions."""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from sqlalchemy.orm import Session

from .. import strategy_parameter


OPTION_CONTRACT_MULTIPLIER: float = 100.0
PERCENT_SCALE: float = 100.0
PROBABILITY_FLOOR: float = 0.0
PROBABILITY_CEILING: float = 1.0
ZERO_PAYOFF: float = 0.0

# These are economic identity defaults, not curve-fit thresholds:
# 1.0 reward/risk is payoff parity, and 0.0 EV pct is breakeven.
BREAKEVEN_REWARD_RISK: float = 1.0
BREAKEVEN_EXPECTED_VALUE_PCT: float = 0.0

# StrategyParameter safety bounds. The live value still comes from config or
# the parameter ledger; these just keep adaptive proposals inside sane ranges.
REWARD_RISK_PARAM_MIN: float = 0.0
REWARD_RISK_PARAM_MAX: float = 10.0
EXPECTED_VALUE_PCT_PARAM_MIN: float = -100.0
EXPECTED_VALUE_PCT_PARAM_MAX: float = 500.0

SNAPSHOT_PRICE_DECIMALS: int = 4
SNAPSHOT_DOLLAR_DECIMALS: int = 2
SNAPSHOT_RATIO_DECIMALS: int = 4
STRATEGY_FAMILY: str = "autotrader_options"
UNSET_SETTING = object()


@dataclass(frozen=True)
class OptionEntryThresholds:
    min_underlying_reward_risk: float = BREAKEVEN_REWARD_RISK
    min_option_reward_risk: float = BREAKEVEN_REWARD_RISK
    min_expected_value_pct: float = BREAKEVEN_EXPECTED_VALUE_PCT

    def as_snapshot(self) -> dict[str, float]:
        return {
            "min_underlying_reward_risk": round(
                self.min_underlying_reward_risk, SNAPSHOT_RATIO_DECIMALS
            ),
            "min_option_reward_risk": round(
                self.min_option_reward_risk, SNAPSHOT_RATIO_DECIMALS
            ),
            "min_expected_value_pct": round(
                self.min_expected_value_pct, SNAPSHOT_RATIO_DECIMALS
            ),
        }


@dataclass(frozen=True)
class OptionEntryDecision:
    accepted: bool
    reason: str
    snapshot: dict[str, Any]


def _setting_float(settings: Any, names: tuple[str, ...], default: float) -> float:
    explicit_attrs = vars(settings) if hasattr(settings, "__dict__") else {}
    for name in names:
        value = explicit_attrs.get(name, UNSET_SETTING)
        if value is UNSET_SETTING:
            value = getattr(settings, name, UNSET_SETTING)
        if value is UNSET_SETTING:
            continue
        if value.__class__.__module__ == "unittest.mock":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            continue
    return default


def _coerce_positive_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out) or out <= ZERO_PAYOFF:
        return None
    return out


def _coerce_float(value: Any) -> Optional[float]:
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(out):
        return None
    return out


def _round_optional(value: Optional[float], places: int = SNAPSHOT_PRICE_DECIMALS) -> Optional[float]:
    if value is None:
        return None
    if math.isinf(value):
        return None
    return round(value, places)


def _quote_snapshot_price(option_meta: Mapping[str, Any], *keys: str) -> Optional[float]:
    quote_snapshot = option_meta.get("quote_snapshot")
    if isinstance(quote_snapshot, Mapping):
        for key in keys:
            value = _coerce_positive_float(quote_snapshot.get(key))
            if value is not None:
                return value
    for key in keys:
        value = _coerce_positive_float(option_meta.get(f"synthesis_{key}"))
        if value is not None:
            return value
    return None


def _adaptive_parameter(
    db: Optional[Session],
    *,
    parameter_key: str,
    initial_value: float,
    min_value: float,
    max_value: float,
    description: str,
) -> float:
    if db is None:
        return initial_value
    try:
        strategy_parameter.register_parameter(
            db,
            strategy_parameter.ParameterSpec(
                strategy_family=STRATEGY_FAMILY,
                parameter_key=parameter_key,
                initial_value=float(initial_value),
                min_value=float(min_value),
                max_value=float(max_value),
                description=description,
            ),
        )
        learned = strategy_parameter.get_parameter(
            db,
            strategy_family=STRATEGY_FAMILY,
            parameter_key=parameter_key,
            default=float(initial_value),
        )
        return float(learned) if learned is not None else initial_value
    except Exception:
        return initial_value


def resolve_option_entry_thresholds(
    db: Optional[Session],
    *,
    settings: Any,
) -> OptionEntryThresholds:
    """Load option entry thresholds from config, then the adaptive ledger."""

    min_underlying_rr = _setting_float(
        settings,
        (
            "options_min_underlying_reward_risk",
            "chili_autotrader_options_min_underlying_reward_risk",
        ),
        BREAKEVEN_REWARD_RISK,
    )
    min_option_rr = _setting_float(
        settings,
        (
            "options_min_option_reward_risk",
            "chili_autotrader_options_min_option_reward_risk",
        ),
        BREAKEVEN_REWARD_RISK,
    )
    min_ev_pct = _setting_float(
        settings,
        (
            "options_min_expected_value_pct",
            "chili_autotrader_options_min_expected_value_pct",
        ),
        BREAKEVEN_EXPECTED_VALUE_PCT,
    )

    return OptionEntryThresholds(
        min_underlying_reward_risk=_adaptive_parameter(
            db,
            parameter_key="entry_min_underlying_reward_risk",
            initial_value=min_underlying_rr,
            min_value=REWARD_RISK_PARAM_MIN,
            max_value=REWARD_RISK_PARAM_MAX,
            description=(
                "Minimum reward/risk of the underlying target-vs-stop scenario "
                "for an autotrader option substitution."
            ),
        ),
        min_option_reward_risk=_adaptive_parameter(
            db,
            parameter_key="entry_min_option_reward_risk",
            initial_value=min_option_rr,
            min_value=REWARD_RISK_PARAM_MIN,
            max_value=REWARD_RISK_PARAM_MAX,
            description=(
                "Minimum option payoff reward/risk at the underlying target "
                "and stop for an autotrader option substitution."
            ),
        ),
        min_expected_value_pct=_adaptive_parameter(
            db,
            parameter_key="entry_min_expected_value_pct",
            initial_value=min_ev_pct,
            min_value=EXPECTED_VALUE_PCT_PARAM_MIN,
            max_value=EXPECTED_VALUE_PCT_PARAM_MAX,
            description=(
                "Minimum expected value as a percent of option premium, using "
                "the alert confidence as the directional probability input."
            ),
        ),
    )


def evaluate_long_option_entry(
    db: Optional[Session],
    *,
    alert: Any,
    option_meta: Mapping[str, Any],
    current_underlying_price: float,
    confidence: float,
    settings: Any,
) -> OptionEntryDecision:
    """Evaluate a long single-leg option using underlying stop/target states.

    The alert's entry price is an option premium, while stop/target remain
    underlying prices. This model keeps those domains separate, then projects
    option intrinsic value at the underlying target and stop.
    """

    thresholds = resolve_option_entry_thresholds(db, settings=settings)
    snapshot: dict[str, Any] = {
        "model": "single_leg_underlying_scenario_ev_v1",
        "thresholds": thresholds.as_snapshot(),
        "contract_multiplier": OPTION_CONTRACT_MULTIPLIER,
    }

    legs = option_meta.get("legs")
    if isinstance(legs, list) and len(legs) >= 2:
        snapshot["legs"] = len(legs)
        return OptionEntryDecision(
            accepted=False,
            reason="multi_leg_entry_quality_model_missing",
            snapshot=snapshot,
        )

    premium = _coerce_positive_float(option_meta.get("limit_price"))
    if premium is None:
        premium = _coerce_positive_float(getattr(alert, "entry_price", None))
    strike = _coerce_positive_float(option_meta.get("strike"))
    underlying = _coerce_positive_float(current_underlying_price)
    target = _coerce_float(getattr(alert, "target_price", None))
    stop = _coerce_float(getattr(alert, "stop_loss", None))
    option_type = str(option_meta.get("option_type") or "").lower()

    snapshot.update(
        {
            "option_type": option_type or None,
            "premium": _round_optional(premium),
            "premium_contract": _round_optional(
                premium * OPTION_CONTRACT_MULTIPLIER if premium is not None else None,
                SNAPSHOT_DOLLAR_DECIMALS,
            ),
            "strike": _round_optional(strike),
            "underlying_price": _round_optional(underlying),
            "underlying_target": _round_optional(target),
            "underlying_stop": _round_optional(stop),
        }
    )

    if premium is None:
        return OptionEntryDecision(False, "missing_option_premium", snapshot)
    if strike is None:
        return OptionEntryDecision(False, "missing_option_strike", snapshot)
    if underlying is None:
        return OptionEntryDecision(False, "missing_underlying_price", snapshot)
    if target is None:
        return OptionEntryDecision(False, "missing_underlying_target", snapshot)
    if stop is None:
        return OptionEntryDecision(False, "missing_underlying_stop", snapshot)

    underlying_reward = abs(target - underlying)
    underlying_risk = abs(underlying - stop)
    if underlying_risk <= ZERO_PAYOFF:
        return OptionEntryDecision(False, "underlying_risk_non_positive", snapshot)
    underlying_reward_risk = underlying_reward / underlying_risk

    if option_type == "call":
        if target <= underlying:
            return OptionEntryDecision(False, "call_target_not_above_underlying", snapshot)
        if stop >= underlying:
            return OptionEntryDecision(False, "call_stop_not_below_underlying", snapshot)
        target_intrinsic = max(target - strike, ZERO_PAYOFF)
        stop_intrinsic = max(stop - strike, ZERO_PAYOFF)
    elif option_type == "put":
        if target >= underlying:
            return OptionEntryDecision(False, "put_target_not_below_underlying", snapshot)
        if stop <= underlying:
            return OptionEntryDecision(False, "put_stop_not_above_underlying", snapshot)
        target_intrinsic = max(strike - target, ZERO_PAYOFF)
        stop_intrinsic = max(strike - stop, ZERO_PAYOFF)
    else:
        return OptionEntryDecision(False, "unsupported_option_type", snapshot)

    option_profit_at_target = target_intrinsic - premium
    option_loss_at_stop = min(premium, max(premium - stop_intrinsic, ZERO_PAYOFF))
    option_reward_risk = (
        option_profit_at_target / option_loss_at_stop
        if option_loss_at_stop > ZERO_PAYOFF
        else math.inf
    )

    probability = min(PROBABILITY_CEILING, max(PROBABILITY_FLOOR, float(confidence)))
    expected_value_per_share = (
        probability * option_profit_at_target
        - (PROBABILITY_CEILING - probability) * option_loss_at_stop
    )
    expected_value_pct = expected_value_per_share / premium * PERCENT_SCALE
    expected_value_per_contract = expected_value_per_share * OPTION_CONTRACT_MULTIPLIER

    snapshot.update(
        {
            "confidence_probability": round(probability, SNAPSHOT_RATIO_DECIMALS),
            "underlying_reward": _round_optional(underlying_reward),
            "underlying_risk": _round_optional(underlying_risk),
            "underlying_reward_risk": _round_optional(
                underlying_reward_risk, SNAPSHOT_RATIO_DECIMALS
            ),
            "target_intrinsic": _round_optional(target_intrinsic),
            "stop_intrinsic": _round_optional(stop_intrinsic),
            "option_profit_at_target": _round_optional(option_profit_at_target),
            "option_loss_at_stop": _round_optional(option_loss_at_stop),
            "option_reward_risk": _round_optional(
                option_reward_risk, SNAPSHOT_RATIO_DECIMALS
            ),
            "expected_value_per_contract": _round_optional(
                expected_value_per_contract, SNAPSHOT_DOLLAR_DECIMALS
            ),
            "expected_value_pct_of_premium": _round_optional(
                expected_value_pct, SNAPSHOT_RATIO_DECIMALS
            ),
        }
    )

    quote_bid = _quote_snapshot_price(option_meta, "bid", "bid_price")
    quote_ask = _quote_snapshot_price(option_meta, "ask", "ask_price")
    liquidity_cost_per_share = ZERO_PAYOFF
    if quote_bid is not None and quote_ask is not None:
        snapshot.update(
            {
                "entry_bid": _round_optional(quote_bid),
                "entry_ask": _round_optional(quote_ask),
            }
        )
        if quote_bid > quote_ask:
            return OptionEntryDecision(False, "crossed_option_quote_snapshot", snapshot)
        liquidity_cost_per_share = max(quote_ask - quote_bid, ZERO_PAYOFF)

    option_profit_after_cost = option_profit_at_target - liquidity_cost_per_share
    option_loss_after_cost = min(premium, option_loss_at_stop + liquidity_cost_per_share)
    option_reward_risk_after_cost = (
        option_profit_after_cost / option_loss_after_cost
        if option_loss_after_cost > ZERO_PAYOFF
        else math.inf
    )
    expected_value_after_cost_per_share = (
        probability * option_profit_after_cost
        - (PROBABILITY_CEILING - probability) * option_loss_after_cost
    )
    expected_value_after_cost_pct = (
        expected_value_after_cost_per_share / premium * PERCENT_SCALE
    )
    expected_value_after_cost_per_contract = (
        expected_value_after_cost_per_share * OPTION_CONTRACT_MULTIPLIER
    )
    snapshot.update(
        {
            "execution_cost_model": "entry_spread_penalty_v1",
            "liquidity_cost_per_share": _round_optional(liquidity_cost_per_share),
            "liquidity_cost_pct_of_premium": _round_optional(
                liquidity_cost_per_share / premium * PERCENT_SCALE,
                SNAPSHOT_RATIO_DECIMALS,
            ),
            "option_profit_at_target_after_cost": _round_optional(
                option_profit_after_cost
            ),
            "option_loss_at_stop_after_cost": _round_optional(option_loss_after_cost),
            "option_reward_risk_after_cost": _round_optional(
                option_reward_risk_after_cost, SNAPSHOT_RATIO_DECIMALS
            ),
            "expected_value_after_cost_per_contract": _round_optional(
                expected_value_after_cost_per_contract,
                SNAPSHOT_DOLLAR_DECIMALS,
            ),
            "expected_value_after_cost_pct_of_premium": _round_optional(
                expected_value_after_cost_pct,
                SNAPSHOT_RATIO_DECIMALS,
            ),
        }
    )

    if underlying_reward_risk < thresholds.min_underlying_reward_risk:
        return OptionEntryDecision(False, "underlying_reward_risk_below_min", snapshot)
    if option_profit_at_target <= ZERO_PAYOFF:
        return OptionEntryDecision(False, "option_target_profit_non_positive", snapshot)
    if option_reward_risk < thresholds.min_option_reward_risk:
        return OptionEntryDecision(False, "option_reward_risk_below_min", snapshot)
    if expected_value_pct < thresholds.min_expected_value_pct:
        return OptionEntryDecision(False, "option_expected_value_below_min", snapshot)
    if option_profit_after_cost <= ZERO_PAYOFF:
        return OptionEntryDecision(
            False, "option_target_profit_after_cost_non_positive", snapshot
        )
    if option_reward_risk_after_cost < thresholds.min_option_reward_risk:
        return OptionEntryDecision(False, "option_reward_risk_after_cost_below_min", snapshot)
    if expected_value_after_cost_pct < thresholds.min_expected_value_pct:
        return OptionEntryDecision(False, "option_expected_value_after_cost_below_min", snapshot)

    return OptionEntryDecision(True, "ok", snapshot)
