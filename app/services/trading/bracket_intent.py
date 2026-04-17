"""Phase G - pure bracket-intent computation (no DB, no broker).

Given a minimal trade snapshot + brain context, produce the stop / target
prices we would have placed as a server-side bracket at the broker. This
is the single canonical place the "what" of a bracket is decided; the
DB writer and reconciliation service are separate, side-effectful
consumers.

Reuses ``stop_engine._compute_initial_stop`` + ``BrainContext`` so
paper and shadow brackets remain consistent with the live stop engine's
math. The pure module does **not** query the database or any broker.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .stop_engine import (
    BREAKEVEN_R,
    TRAILING_R,
    WARN_PROXIMITY_R,
    _LIFECYCLE_STOP_FACTOR,
    _REGIME_STOP_TIGHTEN,
    _REGIME_WARN_PROXIMITY,
    BrainContext,
    _compute_initial_stop,
)


def _is_crypto(ticker: str) -> bool:
    return (ticker or "").upper().endswith("-USD")


@dataclass(frozen=True)
class BracketIntentInput:
    """Pure input to bracket computation.

    All fields are optional / defaulted except the four absolute minima
    (ticker, direction, entry_price, quantity). Unknown regime /
    lifecycle collapse to the existing stop-engine defaults, matching
    live behavior when brain context is unavailable.
    """

    ticker: str
    direction: str
    entry_price: float
    quantity: float
    atr: Optional[float] = None
    stop_model: Optional[str] = None
    pattern_name: Optional[str] = None
    pattern_id: Optional[int] = None
    lifecycle_stage: Optional[str] = None
    pattern_win_rate: Optional[float] = None
    regime: str = "cautious"
    stop_mult_override: Optional[float] = None
    target_mult_override: Optional[float] = None


@dataclass(frozen=True)
class BracketIntentResult:
    """Pure output of bracket computation.

    Prices are already rounded consistent with ``_compute_initial_stop``
    (8 decimals for crypto, 4 for equities). ``reasoning`` is a short
    human-readable tag used for ops logging and the reconciliation
    ``delta_payload``.
    """

    stop_price: Optional[float]
    target_price: Optional[float]
    stop_model_resolved: Optional[str]
    reasoning: str
    brain_summary: dict[str, Any] = field(default_factory=dict)


def _build_pure_brain(input_: BracketIntentInput) -> BrainContext:
    """Build a ``BrainContext`` without any DB reads.

    Mirrors ``stop_engine._build_brain_context`` except we take brain
    fields from the pure input. This keeps the math identical and keeps
    stop_engine as the single source of truth.
    """
    ctx = BrainContext()
    ctx.pattern_name = input_.pattern_name
    ctx.pattern_id = input_.pattern_id
    ctx.lifecycle_stage = input_.lifecycle_stage
    ctx.pattern_win_rate = input_.pattern_win_rate
    ctx.stop_mult_override = input_.stop_mult_override
    ctx.target_mult_override = input_.target_mult_override

    stage = input_.lifecycle_stage or "candidate"
    ctx.lifecycle_stop_factor = _LIFECYCLE_STOP_FACTOR.get(stage, 1.0)
    if stage in ("decayed", "retired"):
        ctx.breakeven_r = 0.75
        ctx.trailing_r = 1.5
    else:
        ctx.breakeven_r = BREAKEVEN_R
        ctx.trailing_r = TRAILING_R

    ctx.regime = input_.regime or "cautious"
    ctx.regime_stop_factor = _REGIME_STOP_TIGHTEN.get(ctx.regime, 1.0)
    ctx.warn_proximity = _REGIME_WARN_PROXIMITY.get(ctx.regime, WARN_PROXIMITY_R)

    if input_.pattern_win_rate is not None and input_.pattern_win_rate < 0.40:
        ctx.lifecycle_stop_factor *= 0.90

    return ctx


def compute_bracket_intent(input_: BracketIntentInput) -> BracketIntentResult:
    """Compute the stop / target bracket for a trade, purely.

    Raises ``ValueError`` on inputs that cannot produce a meaningful
    bracket (zero quantity, non-positive entry, unknown direction).
    Callers should validate upstream; the reconciliation sweep will
    mark such rows as ``kind='unreconciled'`` rather than call this
    function with bad inputs.
    """
    direction = (input_.direction or "").lower()
    if direction not in ("long", "short"):
        raise ValueError(f"unknown direction: {input_.direction!r}")

    entry = float(input_.entry_price)
    if entry <= 0:
        raise ValueError(f"entry_price must be positive, got {entry}")

    qty = float(input_.quantity)
    if qty <= 0:
        raise ValueError(f"quantity must be positive, got {qty}")

    brain = _build_pure_brain(input_)
    is_crypto = _is_crypto(input_.ticker)

    stop_price, target_price = _compute_initial_stop(
        entry=entry,
        direction=direction,
        atr=input_.atr,
        price=entry,
        stop_model=input_.stop_model,
        is_crypto=is_crypto,
        brain=brain,
    )

    reasoning_parts = [
        f"direction={direction}",
        f"model={input_.stop_model or 'snapshot'}",
        f"regime={brain.regime}",
        f"lifecycle={brain.lifecycle_stage or 'candidate'}",
    ]
    if input_.atr and input_.atr > 0:
        reasoning_parts.append(f"atr={input_.atr:.6f}")
    else:
        reasoning_parts.append("atr=none_pct_fallback")

    return BracketIntentResult(
        stop_price=stop_price,
        target_price=target_price,
        stop_model_resolved=input_.stop_model or "snapshot",
        reasoning=" ".join(reasoning_parts),
        brain_summary=brain.summary_dict(),
    )
