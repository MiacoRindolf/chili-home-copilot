"""Phase I: pure risk-dial model (no DB, no I/O).

The risk dial is a scalar in ``[0.0, ceiling]`` (ceiling default 1.5)
that modulates sizing aggressiveness. It is the product of three
components:

1. **Regime default** - configured per regime bucket
   (``risk_on`` / ``cautious`` / ``risk_off``).
2. **Drawdown scaler** - linear from ``1.0`` at 0% drawdown down to
   ``drawdown_floor`` at ``drawdown_trigger_pct`` drawdown, then held
   at the floor.
3. **User / governance override multiplier** - optional, clamped to
   ``[0.0, ceiling / regime_default]`` so the final dial stays under
   the ceiling. An override that would have pushed the dial above the
   ceiling is *rejected* (the override is ignored and
   ``capped_at_ceiling`` is set to True in the output) - callers must
   use the governance approval path to legitimately exceed the
   ceiling.

This module is deliberately **pure**: it never reads a database, never
calls a broker, and never logs. It is called from
``risk_dial_service`` which handles DB state, ops logging and the
shadow / authoritative ladder.

Authoritative cutover (applying the dial inside
``position_sizer_model.compute_proposal``) is Phase I.2.
"""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from typing import Any

VALID_REGIMES = ("risk_on", "cautious", "risk_off")


@dataclass(frozen=True)
class RiskDialConfig:
    """Configuration inputs mirroring the ``brain_risk_dial_*`` settings.

    Kept as an explicit dataclass (rather than reading ``settings``
    directly) so the model stays unit-testable without the full
    application bootstrap.
    """

    default_risk_on: float = 1.0
    default_cautious: float = 0.7
    default_risk_off: float = 0.3
    drawdown_floor: float = 0.5
    drawdown_trigger_pct: float = 10.0
    ceiling: float = 1.5

    def regime_default(self, regime: str | None) -> float:
        if regime == "risk_on":
            return float(self.default_risk_on)
        if regime == "risk_off":
            return float(self.default_risk_off)
        return float(self.default_cautious)


@dataclass(frozen=True)
class RiskDialInput:
    regime: str | None = None
    drawdown_pct: float = 0.0
    user_override_multiplier: float | None = None
    user_id: int | None = None


@dataclass(frozen=True)
class RiskDialOutput:
    dial_value: float
    regime: str | None
    regime_default: float
    drawdown_multiplier: float
    override_multiplier: float
    capped_at_ceiling: bool
    override_rejected: bool
    reasoning: dict = field(default_factory=dict)


def _clamp(value: float, lo: float, hi: float) -> float:
    if math.isnan(value):
        return lo
    return max(lo, min(hi, value))


def _drawdown_multiplier(
    drawdown_pct: float,
    trigger_pct: float,
    floor: float,
) -> float:
    """Linear scaler from 1.0 at 0% DD to ``floor`` at ``trigger_pct``.

    Clamped at ``floor`` for DD values >= ``trigger_pct``. Any negative
    DD is treated as 0% (no positive boost from equity highs).
    """
    if trigger_pct <= 0:
        return 1.0
    d = max(0.0, float(drawdown_pct))
    if d >= trigger_pct:
        return float(floor)
    span = 1.0 - float(floor)
    frac = d / float(trigger_pct)
    return max(float(floor), 1.0 - span * frac)


def compute_dial(
    input: RiskDialInput,
    *,
    config: RiskDialConfig,
) -> RiskDialOutput:
    """Resolve the risk dial for a single user at a point in time.

    The output is fully deterministic for a given
    ``(input, config)`` pair.
    """
    regime = input.regime if input.regime in VALID_REGIMES else None
    regime_default = config.regime_default(regime)

    dd_mult = _drawdown_multiplier(
        input.drawdown_pct,
        trigger_pct=config.drawdown_trigger_pct,
        floor=config.drawdown_floor,
    )

    override_rejected = False
    override_mult = 1.0
    if input.user_override_multiplier is not None:
        requested = float(input.user_override_multiplier)
        if requested < 0.0:
            override_rejected = True
        else:
            # The override is allowed only if it keeps the final dial
            # at or below the ceiling. Anything above requires the
            # governance approval path (Phase I.2 feature); we reject
            # here and keep override_mult = 1.0.
            projected = regime_default * dd_mult * requested
            if projected > config.ceiling + 1e-9:
                override_rejected = True
            else:
                override_mult = requested

    raw = regime_default * dd_mult * override_mult
    dial = _clamp(raw, 0.0, config.ceiling)
    capped = raw > config.ceiling + 1e-9

    reasoning: dict[str, Any] = {
        "regime": regime,
        "regime_default": regime_default,
        "drawdown_pct": float(input.drawdown_pct),
        "drawdown_trigger_pct": float(config.drawdown_trigger_pct),
        "drawdown_floor": float(config.drawdown_floor),
        "drawdown_multiplier": dd_mult,
        "override_multiplier": override_mult,
        "override_rejected": override_rejected,
        "ceiling": float(config.ceiling),
        "capped_at_ceiling": capped,
        "raw_unclamped": raw,
    }

    return RiskDialOutput(
        dial_value=float(dial),
        regime=regime,
        regime_default=regime_default,
        drawdown_multiplier=dd_mult,
        override_multiplier=override_mult,
        capped_at_ceiling=capped,
        override_rejected=override_rejected,
        reasoning=reasoning,
    )


def compute_dial_id(
    *,
    user_id: int | None,
    regime: str | None,
    config: RiskDialConfig,
) -> str:
    """Deterministic UUID-shaped id used for idempotent writes.

    This is **not** a primary key - it's a correlation id so a single
    resolved dial value produces one ops-log line and one DB row even
    if the service is called repeatedly with identical context.
    """
    parts = [
        str(int(user_id)) if user_id is not None else "global",
        (regime or "none").lower(),
        f"{config.default_risk_on:.6g}",
        f"{config.default_cautious:.6g}",
        f"{config.default_risk_off:.6g}",
        f"{config.drawdown_floor:.6g}",
        f"{config.drawdown_trigger_pct:.6g}",
        f"{config.ceiling:.6g}",
    ]
    h = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    # Match the 32-char hex shape used by Phase H proposal_id so both
    # ids can share the VARCHAR(64) column width without surprise.
    return h[:32]


__all__ = [
    "RiskDialConfig",
    "RiskDialInput",
    "RiskDialOutput",
    "VALID_REGIMES",
    "compute_dial",
    "compute_dial_id",
]
