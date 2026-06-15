"""Adaptive ceiling on stop-loss DISTANCE for ATR-derived brackets.

Why this exists (QTEX, 2026-06-15)
----------------------------------
A momentum/runner position must never inherit a base-anchored, multi-day-ATR
stop. When a low-float name bases near $0.40 and runs to a $2.60 high, the
*daily* ATR balloons to ~30% of price. The ATR bracket math then computes
``entry - stop_mult * ATR`` = ``2.16 - 2.5 * 0.71`` ≈ ``$0.375`` — a stop
**~83% below** a $2.16 entry, i.e. the whole position at risk (~$2,100).
Trade 2338 (QTEX) was exactly this: broker-sync recomputed *swing* daily-ATR
levels for a momentum entry and labelled it ``atr_swing``.

Two ATR-geometry chokepoints produce these brackets, and **both** bound the
ATR/price *ratio* but never the resulting *stop distance*, so a
``0.33 ATR/price × 2.5 mult = 0.83`` stop sails straight through:
  * ``scanner._long_atr_trade_levels``   (alert / snapshot geometry)
  * ``stop_engine._compute_initial_stop`` (live bracket / momentum / fast-path)
This module is the single shared guard both call.

Behaviour
---------
* Long:  clamp when ``(entry - stop) / entry > max_frac``.
* Short: clamp when ``(stop - entry) / entry > max_frac``.
The stop is only ever **tightened** (moved toward entry) — never widened. The
target is scaled by the same factor so the configured reward:risk is preserved
(an unbounded ATR also produces a fantasy-far target). Genuine swing setups
(ATR ~2-5% of price -> stop ~5-15%) sit well under the ceiling and pass through
byte-identical.

Single documented knob per asset class (env-overridable, kill-switchable):
  CHILI_MAX_STOP_DISTANCE_FRACTION_STOCK   (default 0.30)
  CHILI_MAX_STOP_DISTANCE_FRACTION_CRYPTO  (default 0.35)
Defaults sit just above the live p90 stop-distance (equity ~0.27, crypto ~0.33,
measured over 60d of real trades) so the legitimate swing distribution is
untouched while the pathological tail (QTEX-class hyper-movers) is clamped.
Set the knob >= 2.0 to effectively disable (reverts to legacy unbounded
geometry — the reversible kill-switch).

This is deliberately a *fixed documented* safety ceiling rather than a
percentile-derived one: a bound computed from the same distribution that
contains the pathological tail would be raised by that tail, and data-derived
risk bounds have spiked catastrophically before (see
docs/DESIGN/MOMENTUM_LANE_ENTRY_STOP_REALIGNMENT.md §ME-1). It mirrors the
existing ``scanner._max_atr_fraction_for_levels`` env-knob pattern.
"""
from __future__ import annotations

import logging
import os
from typing import Any

logger = logging.getLogger(__name__)

# Bounds on the configured fraction itself. Floor keeps a sane minimum risk
# band; ceil (2.0) lets the operator disable the guard entirely.
_FRACTION_FLOOR = 0.02
_FRACTION_CEIL = 2.0
_DEFAULT_STOCK = 0.30
_DEFAULT_CRYPTO = 0.35


def max_stop_distance_fraction(*, crypto: bool) -> float:
    """Adaptive (env-overridable) ceiling on stop distance as a fraction of entry."""
    env_key = (
        "CHILI_MAX_STOP_DISTANCE_FRACTION_CRYPTO"
        if crypto
        else "CHILI_MAX_STOP_DISTANCE_FRACTION_STOCK"
    )
    default = _DEFAULT_CRYPTO if crypto else _DEFAULT_STOCK
    try:
        value = float(os.getenv(env_key, str(default)))
    except (TypeError, ValueError):
        return default
    if value != value:  # NaN guard
        return default
    return max(_FRACTION_FLOOR, min(_FRACTION_CEIL, value))


def bound_stop_distance(
    *,
    entry: float,
    stop: float | None,
    target: float | None = None,
    is_long: bool = True,
    crypto: bool = False,
    context: str = "",
) -> tuple[float | None, float | None, dict[str, Any] | None]:
    """Clamp a stop sitting more than the adaptive max fraction from entry.

    Returns ``(stop, target, clamp_info)``. ``clamp_info`` is ``None`` when no
    clamp was needed (the common, no-op case). The stop is only ever tightened
    toward entry; the target is scaled by the same factor so reward:risk is
    preserved. A wrong-sided or non-positive stop is left untouched (that is a
    different bug and must not be masked here).
    """
    try:
        entry_f = float(entry)
        stop_f = float(stop) if stop is not None else None
    except (TypeError, ValueError):
        return stop, target, None
    if entry_f <= 0 or stop_f is None or stop_f <= 0:
        return stop, target, None

    stop_distance = (entry_f - stop_f) if is_long else (stop_f - entry_f)
    if stop_distance <= 0:
        # Wrong-sided / zero stop — not our concern.
        return stop, target, None

    max_frac = max_stop_distance_fraction(crypto=crypto)
    max_distance = entry_f * max_frac
    if stop_distance <= max_distance:
        return stop, target, None

    scale = max_distance / stop_distance
    new_stop = (entry_f - max_distance) if is_long else (entry_f + max_distance)

    new_target: float | None = target
    try:
        target_f = float(target) if target is not None else None
    except (TypeError, ValueError):
        target_f = None
    if target_f is not None:
        target_distance = (target_f - entry_f) if is_long else (entry_f - target_f)
        if target_distance > 0:
            scaled = target_distance * scale
            new_target = (entry_f + scaled) if is_long else (entry_f - scaled)

    info: dict[str, Any] = {
        "context": context or "stop_distance_guard",
        "is_long": is_long,
        "crypto": crypto,
        "entry": entry_f,
        "orig_stop": stop_f,
        "orig_stop_frac": round(stop_distance / entry_f, 4),
        "max_frac": max_frac,
        "new_stop": new_stop,
        "new_stop_frac": round(max_distance / entry_f, 4),
        "orig_target": target_f,
        "new_target": new_target,
        "scale": round(scale, 4),
    }
    logger.warning(
        "[stop_distance_guard] CLAMPED base-anchored stop: context=%s entry=%.6f "
        "orig_stop=%.6f (%.1f%% away) -> new_stop=%.6f (cap=%.1f%%) "
        "orig_target=%s -> new_target=%s crypto=%s",
        info["context"], entry_f, stop_f, stop_distance / entry_f * 100.0,
        new_stop, max_frac * 100.0,
        f"{target_f:.6f}" if target_f is not None else "None",
        f"{new_target:.6f}" if new_target is not None else "None",
        crypto,
    )
    return new_stop, new_target, info
