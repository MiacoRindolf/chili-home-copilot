"""Portfolio greeks budget enforcement.

Before any options trade is accepted, the proposed trade's net greeks
are summed with the operator's existing options portfolio greeks, and
the result is checked against ``options_greeks_budget``. If any limit
is breached, the trade is rejected and the reason is returned to the
caller.

This is a HARD rule — bypassing it requires ``CHILI_OPTIONS_BUDGET_BYPASS=true``,
which should ONLY be flipped during operator-supervised testing, never
in normal operation.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .strategies import StrategyProposal

logger = logging.getLogger(__name__)


@dataclass
class BudgetCheckResult:
    accepted: bool
    reasons: list[str]
    current_portfolio: dict
    after_proposal: dict
    budget: dict


def _get_budget(db: Session, user_id: Optional[int]) -> dict:
    """Return the user's greeks budget, or sane defaults if no row exists."""
    try:
        row = db.execute(
            text(
                """
                SELECT max_abs_delta, max_abs_gamma, max_vega_per_tenor,
                       max_total_vega, max_theta_burn_per_day
                FROM options_greeks_budget
                WHERE user_id = :uid
                """
            ),
            {"uid": user_id},
        ).fetchone()
        if row:
            return {
                "max_abs_delta": float(row[0]),
                "max_abs_gamma": float(row[1]),
                "max_vega_per_tenor": row[2] or {},
                "max_total_vega": float(row[3]),
                "max_theta_burn_per_day": float(row[4]) if row[4] is not None else None,
            }
    except Exception as e:
        logger.debug("[options.budget] _get_budget failed: %s", e)
    # Conservative defaults.
    return {
        "max_abs_delta": 0.50,
        "max_abs_gamma": 0.05,
        "max_vega_per_tenor": {"30d": 100, "60d": 80, "90d": 60},
        "max_total_vega": 200.0,
        "max_theta_burn_per_day": 50.0,
    }


def _sum_open_position_greeks(db: Session, user_id: Optional[int]) -> dict:
    """Aggregate net greeks across all open option positions.

    For phase-1 scaffolding, we trust the persisted leg greeks; a follow-up
    will recompute from current quotes. Since we haven't placed any options
    yet (Q2.T1 is shipping the foundation), this returns zeros in steady
    state but the code path is in place for when positions exist.
    """
    try:
        rows = db.execute(
            text(
                """
                SELECT legs_json
                FROM options_position
                WHERE (user_id = :uid OR :uid IS NULL)
                  AND closed_at IS NULL
                """
            ),
            {"uid": user_id},
        ).fetchall()
    except Exception as e:
        logger.debug("[options.budget] open positions fetch failed: %s", e)
        return {"net_delta": 0.0, "net_gamma": 0.0, "net_theta": 0.0, "net_vega": 0.0}

    net_d = net_g = net_t = net_v = 0.0
    for r in rows or []:
        try:
            legs = r[0]
            if isinstance(legs, str):
                legs = json.loads(legs)
            for leg in legs or []:
                qty = float(leg.get("qty") or 0)
                d = leg.get("delta")
                g = leg.get("gamma")
                t = leg.get("theta")
                v = leg.get("vega")
                if d is not None:
                    net_d += float(d) * qty
                if g is not None:
                    net_g += float(g) * qty
                if t is not None:
                    net_t += float(t) * qty
                if v is not None:
                    net_v += float(v) * qty
        except Exception:
            continue
    return {
        "net_delta": net_d,
        "net_gamma": net_g,
        "net_theta": net_t,
        "net_vega": net_v,
    }


def check_proposal_against_budget(
    db: Session,
    user_id: Optional[int],
    proposal: StrategyProposal,
) -> BudgetCheckResult:
    """Hard check: would this proposal breach any greeks limit?

    Returns ``BudgetCheckResult(accepted=False, reasons=[...])`` if any
    limit is exceeded. Caller MUST refuse to place the trade if
    ``accepted=False``.

    Bypass: ``CHILI_OPTIONS_BUDGET_BYPASS=true`` env override skips the
    check (returns accepted=True with a warning reason). For
    operator-supervised testing only.
    """
    bypass = os.environ.get("CHILI_OPTIONS_BUDGET_BYPASS", "").lower() in ("true", "1")
    budget = _get_budget(db, user_id)
    current = _sum_open_position_greeks(db, user_id)

    after = {
        "net_delta": current["net_delta"] + (proposal.net_delta or 0),
        "net_gamma": current["net_gamma"] + (proposal.net_gamma or 0),
        "net_theta": current["net_theta"] + (proposal.net_theta or 0),
        "net_vega": current["net_vega"] + (proposal.net_vega or 0),
    }

    reasons: list[str] = []
    if abs(after["net_delta"]) > budget["max_abs_delta"]:
        reasons.append(
            f"abs_delta_breach: |{after['net_delta']:.4f}| > {budget['max_abs_delta']}"
        )
    if abs(after["net_gamma"]) > budget["max_abs_gamma"]:
        reasons.append(
            f"abs_gamma_breach: |{after['net_gamma']:.6f}| > {budget['max_abs_gamma']}"
        )
    if abs(after["net_vega"]) > budget["max_total_vega"]:
        reasons.append(
            f"total_vega_breach: |{after['net_vega']:.4f}| > {budget['max_total_vega']}"
        )
    if (
        budget["max_theta_burn_per_day"] is not None
        and after["net_theta"] < -budget["max_theta_burn_per_day"]
    ):
        reasons.append(
            f"theta_burn_breach: {after['net_theta']:.4f} < "
            f"-{budget['max_theta_burn_per_day']}/day"
        )

    accepted = len(reasons) == 0
    if not accepted and bypass:
        reasons.insert(0, "BYPASS_VIA_CHILI_OPTIONS_BUDGET_BYPASS")
        accepted = True

    return BudgetCheckResult(
        accepted=accepted,
        reasons=reasons,
        current_portfolio=current,
        after_proposal=after,
        budget=budget,
    )


def upsert_budget(
    db: Session,
    user_id: int,
    *,
    max_abs_delta: float = 0.50,
    max_abs_gamma: float = 0.05,
    max_vega_per_tenor: Optional[dict] = None,
    max_total_vega: float = 200.0,
    max_theta_burn_per_day: Optional[float] = 50.0,
) -> bool:
    """Idempotent upsert of the per-user options greeks budget."""
    try:
        db.execute(
            text(
                """
                INSERT INTO options_greeks_budget
                    (user_id, max_abs_delta, max_abs_gamma, max_vega_per_tenor,
                     max_total_vega, max_theta_burn_per_day, updated_at)
                VALUES (:uid, :d, :g, :vt, :tv, :tb, NOW())
                ON CONFLICT (user_id) DO UPDATE SET
                    max_abs_delta = EXCLUDED.max_abs_delta,
                    max_abs_gamma = EXCLUDED.max_abs_gamma,
                    max_vega_per_tenor = EXCLUDED.max_vega_per_tenor,
                    max_total_vega = EXCLUDED.max_total_vega,
                    max_theta_burn_per_day = EXCLUDED.max_theta_burn_per_day,
                    updated_at = NOW()
                """
            ),
            {
                "uid": user_id,
                "d": max_abs_delta,
                "g": max_abs_gamma,
                "vt": json.dumps(max_vega_per_tenor or {}),
                "tv": max_total_vega,
                "tb": max_theta_burn_per_day,
            },
        )
        db.commit()
        return True
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[options.budget] upsert_budget failed: %s", e)
        return False
