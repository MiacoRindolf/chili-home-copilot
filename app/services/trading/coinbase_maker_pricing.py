"""Coinbase maker-order price planning helpers.

Pure helpers live here so AutoTrader and watchdog code can share maker
pricing rules without pulling venue adapters or database state into tests.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from typing import Any


NO_IMPROVEMENT_TICKS = 0


@dataclass(frozen=True)
class PostOnlyBuyLimitPlan:
    """A post-only buy limit that should not cross the current ask."""

    limit_price: float
    limit_price_text: str
    bid: float
    ask: float | None
    price_increment: float | None
    improved_ticks: int


def _positive_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        return None
    if not parsed.is_finite() or parsed <= 0:
        return None
    return parsed


def _snap_down(value: Decimal, increment: Decimal) -> Decimal:
    quotient = (value / increment).to_integral_value(rounding=ROUND_FLOOR)
    return quotient * increment


def plan_post_only_buy_limit(
    *,
    bid: Any,
    ask: Any = None,
    price_increment: Any = None,
    improve_ticks: int = NO_IMPROVEMENT_TICKS,
) -> PostOnlyBuyLimitPlan | None:
    """Return a bounded maker buy price, or ``None`` when bid is unusable.

    The plan starts at best bid. When a positive product tick and ask are
    available, it may improve by up to ``improve_ticks`` while keeping at
    least one tick below ask. Coinbase still receives ``post_only=True``;
    this helper only chooses a less passive resting price.
    """

    bid_d = _positive_decimal(bid)
    if bid_d is None:
        return None

    ask_d = _positive_decimal(ask)
    tick_d = _positive_decimal(price_increment)
    max_ticks = max(NO_IMPROVEMENT_TICKS, int(improve_ticks or NO_IMPROVEMENT_TICKS))

    limit_d = bid_d
    improved_ticks = NO_IMPROVEMENT_TICKS
    if ask_d is not None and tick_d is not None and max_ticks > NO_IMPROVEMENT_TICKS:
        maker_ceiling = ask_d - tick_d
        if maker_ceiling > bid_d:
            desired = bid_d + (tick_d * Decimal(max_ticks))
            limit_d = min(desired, maker_ceiling)
            limit_d = _snap_down(limit_d, tick_d)
            if limit_d > bid_d:
                improved_ticks = max(
                    NO_IMPROVEMENT_TICKS,
                    int(((limit_d - bid_d) / tick_d).to_integral_value(rounding=ROUND_FLOOR)),
                )
        else:
            limit_d = bid_d

    return PostOnlyBuyLimitPlan(
        limit_price=float(limit_d),
        limit_price_text=format(limit_d, "f"),
        bid=float(bid_d),
        ask=float(ask_d) if ask_d is not None else None,
        price_increment=float(tick_d) if tick_d is not None else None,
        improved_ticks=improved_ticks,
    )
