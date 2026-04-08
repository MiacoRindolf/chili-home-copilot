"""Versioned momentum strategy families (neural-owned taxonomy)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


@dataclass(frozen=True)
class MomentumStrategyFamily:
    family_id: str
    version: int
    label: str
    entry_style: str
    default_stop_logic: str
    default_exit_logic: str


MOMENTUM_STRATEGY_FAMILIES: tuple[MomentumStrategyFamily, ...] = (
    MomentumStrategyFamily(
        "impulse_breakout",
        1,
        "Impulse breakout",
        "Long on impulse candle break of micro high with volume confirmation",
        "Below impulse low or fixed % adverse move",
        "Scale at extension; trail after 1R",
    ),
    MomentumStrategyFamily(
        "micro_pullback_continuation",
        1,
        "1m micro pullback continuation",
        "Enter on shallow pullback to micro EMA/VWAP after thrust",
        "Tight swing low / thrust origin",
        "Exit on stall or VWAP loss",
    ),
    MomentumStrategyFamily(
        "rolling_range_high_breakout",
        1,
        "Rolling range high breakout",
        "Break of rolling N-bar high (crypto session-agnostic)",
        "Below range high reclaim failure",
        "Partial into next liquidity; trail remainder",
    ),
    MomentumStrategyFamily(
        "breakout_reclaim",
        1,
        "Breakout reclaim",
        "Long failed breakdown reclaim through key level",
        "Invalidation below reclaimed level",
        "Exit on false reclaim / close back inside range",
    ),
    MomentumStrategyFamily(
        "vwap_reclaim_continuation",
        1,
        "VWAP reclaim continuation",
        "Price reclaims session/rolling VWAP with hold",
        "Stop under VWAP / band",
        "Exit on VWAP lost again",
    ),
    MomentumStrategyFamily(
        "ema_reclaim_continuation",
        1,
        "EMA reclaim continuation",
        "Reclaim fast EMA stack after pullback",
        "Below reclaim bar low",
        "Exit on EMA stack roll",
    ),
    MomentumStrategyFamily(
        "compression_expansion_breakout",
        1,
        "Compression to expansion breakout",
        "Enter on expansion bar out of tight range",
        "Opposite side of range",
        "Exit on expansion failure / inside bar",
    ),
    MomentumStrategyFamily(
        "momentum_follow_through_scalp",
        1,
        "Momentum follow-through scalp",
        "Add/enter on higher-low sequence into strength",
        "Last higher low",
        "Exit on lower high or momentum fade",
    ),
    MomentumStrategyFamily(
        "failed_breakout_bailout",
        1,
        "Failed breakout bailout",
        "Defensive exit / flip guard when break fails quickly",
        "Structured adverse level",
        "Flat / small loss cap",
    ),
    MomentumStrategyFamily(
        "no_follow_through_exit",
        1,
        "No-follow-through / exhaustion exit",
        "Exit when thrust does not continue within T bars",
        "Time / range stop",
        "Protect against chop",
    ),
)


def iter_momentum_families() -> Iterator[MomentumStrategyFamily]:
    yield from MOMENTUM_STRATEGY_FAMILIES


def get_family(family_id: str) -> MomentumStrategyFamily | None:
    for f in MOMENTUM_STRATEGY_FAMILIES:
        if f.family_id == family_id:
            return f
    return None
