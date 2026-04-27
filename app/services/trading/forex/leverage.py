"""Q2.T2 — hard 10:1 effective-leverage cap for the FX lane.

Even when the broker (OANDA) allows 50:1 or more, CHILI enforces a 10:1
cap calculated as:

    effective_leverage = sum(|notional|) / account_equity

where notional for a position = units * entry_price (in account ccy).

At trade-acceptance time, the proposed trade's notional is added to the
existing FX portfolio's total notional, divided by current equity, and
rejected if > 10.0.

Bypass: ``CHILI_FOREX_LEVERAGE_BYPASS=true`` env (testing only).
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .strategies import FxProposal

logger = logging.getLogger(__name__)

_HARD_LEVERAGE_CAP = 10.0


@dataclass
class LeverageCheckResult:
    accepted: bool
    reasons: list[str]
    current_leverage: float
    after_proposal_leverage: float
    cap: float


def _sum_open_fx_notional(db: Session, user_id: Optional[int]) -> float:
    try:
        row = db.execute(
            text(
                """
                SELECT COALESCE(SUM(ABS(units) * entry_price), 0)
                FROM fx_position
                WHERE (user_id = :uid OR :uid IS NULL)
                  AND closed_at IS NULL
                """
            ),
            {"uid": user_id},
        ).fetchone()
        return float(row[0] or 0)
    except Exception as e:
        logger.debug("[forex.leverage] sum failed: %s", e)
        return 0.0


def check_leverage(
    db: Session,
    user_id: Optional[int],
    proposal: FxProposal,
    account_equity_usd: float,
    *,
    quote_to_usd_rate: float = 1.0,
) -> LeverageCheckResult:
    """Hard check: would this proposal push effective leverage > 10:1?

    Args:
        quote_to_usd_rate: convert quote currency to USD for non-USD pairs.
                           Default 1.0 assumes the proposal's notional is
                           already USD-equivalent.
    """
    bypass = os.environ.get("CHILI_FOREX_LEVERAGE_BYPASS", "").lower() in ("true", "1")

    current_notional = _sum_open_fx_notional(db, user_id)
    proposal_notional = abs(proposal.units) * proposal.entry_price * quote_to_usd_rate
    new_total = current_notional + proposal_notional
    current_lev = current_notional / max(account_equity_usd, 1e-9)
    after_lev = new_total / max(account_equity_usd, 1e-9)

    reasons: list[str] = []
    if after_lev > _HARD_LEVERAGE_CAP:
        reasons.append(
            f"leverage_breach: {after_lev:.2f}:1 > {_HARD_LEVERAGE_CAP}:1 "
            f"(current {current_lev:.2f}:1 + proposed {proposal_notional/max(account_equity_usd,1e-9):.2f}:1)"
        )

    accepted = len(reasons) == 0
    if not accepted and bypass:
        reasons.insert(0, "BYPASS_VIA_CHILI_FOREX_LEVERAGE_BYPASS")
        accepted = True

    return LeverageCheckResult(
        accepted=accepted,
        reasons=reasons,
        current_leverage=current_lev,
        after_proposal_leverage=after_lev,
        cap=_HARD_LEVERAGE_CAP,
    )
