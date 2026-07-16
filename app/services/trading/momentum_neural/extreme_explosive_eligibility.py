"""LEVER 1 — extreme-vol / explosive live-eligibility (risk-bounded admission).

The Ross momentum lane SELECTS explosive low-float runners — which are, by
construction, ``VolatilityRegime.extreme``. The legacy rule blanket-blocked every
extreme-vol name from live (``live_eligible = False``), so the very names the lane
is built to trade could never go live. That is the no-fill trap.

This lever replaces the blanket block with a *gated* allowance:

  extreme-vol  AND  explosive-floor-clear  AND  product-tradable  AND  ok-spread
      => live-eligible, flagged for RISK-BOUNDED (size-down) admission

  extreme-vol  AND  NOT explosive (or not tradable / bad spread)
      => still gated (live_eligible stays False)

The decision is a pure function so it can be unit-tested and replayed; the caller
(``score_viability``) applies the size-down multiple to its risk budget when the
result says ``risk_bounded``. Worst-case qty/loss therefore stays bounded: the
admitted name trades at ``risk_mult`` (<= 1.0) of the normal risk budget.

Flag-OFF => the caller keeps the legacy blanket block (byte-identical).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ExtremeExplosiveDecision:
    """Outcome of the extreme-vol eligibility gate.

    ``eligible``      — may this extreme-vol name be live-eligible?
    ``risk_bounded``  — if eligible, must it be sized DOWN (always True when eligible)?
    ``risk_mult``     — multiple to apply to the risk budget (<= 1.0); 1.0 when not eligible.
    ``reason``        — short machine-readable reason for the audit trail.
    """

    eligible: bool
    risk_bounded: bool
    risk_mult: float
    reason: str


def evaluate_extreme_explosive(
    *,
    is_extreme_vol: bool,
    explosive_score: float | None,
    product_tradable: bool | None,
    ok_spread: bool,
    enabled: bool,
    explosive_floor: float,
    risk_mult: float,
) -> ExtremeExplosiveDecision:
    """Pure gate. See module docstring for the contract.

    Only meaningful for extreme-vol names — for non-extreme names the caller does
    not consult this gate at all, so ``is_extreme_vol=False`` yields a no-op
    "not extreme" decision (eligible=False, mult=1.0) that the caller ignores.
    """
    # Flag-off => blanket-block parity. The legacy behavior is "extreme => gated",
    # so we surface eligible=False and a full-size (1.0) risk mult that the caller's
    # legacy path would never even apply (it just keeps live_eligible=False).
    if not enabled:
        return ExtremeExplosiveDecision(
            eligible=False,
            risk_bounded=False,
            risk_mult=1.0,
            reason="flag_off_blanket_block",
        )

    if not is_extreme_vol:
        # Not our case — caller shouldn't have asked, but be safe / explicit.
        return ExtremeExplosiveDecision(
            eligible=False,
            risk_bounded=False,
            risk_mult=1.0,
            reason="not_extreme_vol",
        )

    # Gate conditions for an extreme-vol name to earn live eligibility.
    if explosive_score is None or float(explosive_score) < float(explosive_floor):
        return ExtremeExplosiveDecision(
            eligible=False,
            risk_bounded=False,
            risk_mult=1.0,
            reason="below_explosive_floor",
        )
    if product_tradable is False:
        return ExtremeExplosiveDecision(
            eligible=False,
            risk_bounded=False,
            risk_mult=1.0,
            reason="not_tradable",
        )
    if not ok_spread:
        return ExtremeExplosiveDecision(
            eligible=False,
            risk_bounded=False,
            risk_mult=1.0,
            reason="spread_gated",
        )

    # Admitted — but ALWAYS risk-bounded (size-down) so worst-case loss is capped.
    bounded_mult = max(0.0, min(1.0, float(risk_mult)))
    return ExtremeExplosiveDecision(
        eligible=True,
        risk_bounded=True,
        risk_mult=bounded_mult,
        reason="extreme_explosive_risk_bounded",
    )
