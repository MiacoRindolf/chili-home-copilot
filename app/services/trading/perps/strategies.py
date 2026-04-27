"""Q2.T3 — two seed perp strategies.

  - funding_carry         : When funding_annualized > 15% AND basis_z_score
                            > 1.5 (perp clearly rich vs spot), short the
                            perp + buy spot. Collect funding, neutral price.
                            Closes when funding_annualized normalizes < 5%.

  - oi_divergence         : When OI rises 20%+ in 24h AND price flat-to-down
                            within ±2%, longs are crowded. Take a counter-
                            trend short on the next pullback. Close on
                            either OI reverting or price following the
                            shorts (10% drop).

Both flag-gated by chili_perps_lane_enabled. Both write to perp_position
with is_paper=True until operator promotes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)


@dataclass
class PerpProposal:
    symbol: str
    venue: str
    side: str
    contracts: float
    entry_price: float
    stop_price: Optional[float]
    take_profit_price: Optional[float]
    strategy_family: str
    confidence: float
    rationale: str
    meta: dict = field(default_factory=dict)


def funding_carry(
    *,
    symbol: str,
    venue: str,
    perp_price: float,
    funding_annualized_pct: float,
    basis_z_score_value: float,
    contracts: float = 0.01,
    entry_threshold_apy: float = 15.0,
    entry_threshold_basis_z: float = 1.5,
    confidence: float = 0.55,
) -> Optional[PerpProposal]:
    """Short the perp when carry is fat. Hedge with spot long is the
    operator's responsibility (or a follow-up Q2.T3a basket trade)."""
    if funding_annualized_pct < entry_threshold_apy:
        return None
    if basis_z_score_value < entry_threshold_basis_z:
        return None
    stop = perp_price * 1.05  # 5% stop above entry
    return PerpProposal(
        symbol=symbol,
        venue=venue,
        side="short",
        contracts=contracts,
        entry_price=perp_price,
        stop_price=stop,
        take_profit_price=None,  # exit on funding normalization, not price target
        strategy_family="funding_carry",
        confidence=confidence,
        rationale=(
            f"Funding APY {funding_annualized_pct:.1f}% > {entry_threshold_apy}%; "
            f"basis z-score {basis_z_score_value:.2f} > {entry_threshold_basis_z}; "
            f"short perp, hedge with spot long. Exit when funding < 5% APY."
        ),
        meta={
            "funding_annualized_pct": funding_annualized_pct,
            "basis_z_score": basis_z_score_value,
            "exit_signal": "funding_apy_below_5_pct",
        },
    )


def oi_divergence(
    *,
    symbol: str,
    venue: str,
    perp_price: float,
    oi_pct_change_24h: float,
    price_pct_change_24h: float,
    contracts: float = 0.01,
    oi_threshold: float = 0.20,
    price_band: float = 0.02,
    confidence: float = 0.50,
) -> Optional[PerpProposal]:
    """OI surged but price didn't follow → long-side overcrowding.

    Counter-trend short on next pullback.
    """
    if oi_pct_change_24h < oi_threshold:
        return None
    if abs(price_pct_change_24h) > price_band:
        return None
    stop = perp_price * 1.03  # tight 3% stop
    tp = perp_price * 0.93     # 7% TP
    return PerpProposal(
        symbol=symbol,
        venue=venue,
        side="short",
        contracts=contracts,
        entry_price=perp_price,
        stop_price=stop,
        take_profit_price=tp,
        strategy_family="oi_divergence",
        confidence=confidence,
        rationale=(
            f"OI +{oi_pct_change_24h*100:.1f}% in 24h vs price "
            f"{price_pct_change_24h*100:+.1f}%; longs crowded, fade short."
        ),
        meta={
            "oi_pct_change_24h": oi_pct_change_24h,
            "price_pct_change_24h": price_pct_change_24h,
        },
    )


def persist_proposal(
    db: Session,
    user_id: Optional[int],
    proposal: PerpProposal,
    is_paper: bool = True,
) -> Optional[int]:
    """Insert into perp_position with closed_at NULL."""
    try:
        row = db.execute(
            text(
                """
                INSERT INTO perp_position
                    (user_id, symbol, venue, side, contracts, entry_price,
                     stop_price, take_profit_price, strategy_family, is_paper)
                VALUES
                    (:uid, :s, :v, :side, :c, :ep, :sp, :tp, :sf, :paper)
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "s": proposal.symbol,
                "v": proposal.venue,
                "side": proposal.side,
                "c": proposal.contracts,
                "ep": proposal.entry_price,
                "sp": proposal.stop_price,
                "tp": proposal.take_profit_price,
                "sf": proposal.strategy_family,
                "paper": is_paper,
            },
        ).fetchone()
        db.commit()
        return int(row[0]) if row else None
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[perps.strategies] persist_proposal failed: %s", e)
        return None
