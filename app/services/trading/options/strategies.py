"""Q2.T1 — seed multi-leg option strategies.

Four strategy families, each producing a ``StrategyProposal`` (the
``options_strategy_proposal`` table row). Operator (or auto-trader, when
the options lane is enabled live) decides whether to place.

  - covered_call      : long underlying + short OTM call
                        Income-with-cap. Best in flat-to-modestly-up markets.
  - cash_secured_put  : short OTM put + cash collateral
                        Substitute for buy-the-dip with premium income.
  - vertical_spread   : long ATM call/put + short OTM same-expiry
                        Defined-risk directional bet.
  - iron_condor       : short OTM call spread + short OTM put spread, same expiry
                        Income on range-bound underlying.

Every proposal computes net debit/credit, max loss, max profit,
breakevens, and net portfolio greeks. Operator's ``options_greeks_budget``
is consulted before acceptance.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .greeks import bs_greeks

logger = logging.getLogger(__name__)


@dataclass
class Leg:
    occ_symbol: str
    underlying: str
    expiration: date
    strike: float
    opt_type: str          # 'call' | 'put'
    qty: int               # +N long, -N short
    entry_price: float


@dataclass
class StrategyProposal:
    underlying: str
    strategy_family: str
    legs: list[Leg]
    net_debit: Optional[float]
    net_credit: Optional[float]
    max_loss: Optional[float]
    max_profit: Optional[float]
    breakevens: list[float]
    net_delta: Optional[float]
    net_gamma: Optional[float]
    net_theta: Optional[float]
    net_vega: Optional[float]
    confidence: float
    rationale: str
    meta: dict = field(default_factory=dict)


def _aggregate_greeks(
    legs: list[Leg],
    spot: float,
    risk_free_rate: float,
    today: date,
) -> tuple[float, float, float, float]:
    """Sum greeks across legs (signed by qty)."""
    net_d = net_g = net_t = net_v = 0.0
    for leg in legs:
        T = max((leg.expiration - today).days / 365.0, 1.0 / 365.0)
        # Best-effort vol if not provided: use ATM-ish approx via market price
        # via implied_vol(). For seed scaffolding, assume 0.30 fallback.
        vol = leg.entry_price > 0 and leg.entry_price / max(spot, 1e-9) * 4 or 0.30
        vol = max(0.05, min(2.0, vol))
        g = bs_greeks(
            spot=spot, strike=leg.strike,
            time_to_expiry_years=T, risk_free_rate=risk_free_rate,
            volatility=vol, opt_type=leg.opt_type,
        )
        net_d += g.delta * leg.qty
        net_g += g.gamma * leg.qty
        net_t += g.theta * leg.qty
        net_v += g.vega * leg.qty
    return net_d, net_g, net_t, net_v


def covered_call(
    *,
    underlying: str,
    spot: float,
    short_call_strike: float,
    short_call_expiration: date,
    short_call_premium: float,
    risk_free_rate: float = 0.045,
    today: Optional[date] = None,
    confidence: float = 0.5,
) -> StrategyProposal:
    """Covered call: long 100 shares + short 1 OTM call."""
    today = today or date.today()
    short_call_leg = Leg(
        occ_symbol=f"{underlying.upper()}{short_call_expiration.strftime('%y%m%d')}C{int(short_call_strike*1000):08d}",
        underlying=underlying,
        expiration=short_call_expiration,
        strike=short_call_strike,
        opt_type="call",
        qty=-1,
        entry_price=short_call_premium,
    )
    legs = [short_call_leg]
    net_d, net_g, net_t, net_v = _aggregate_greeks(legs, spot, risk_free_rate, today)
    # Add long-stock delta (+100 shares = +1.0 delta on the option-equivalent basis).
    net_d += 1.0  # underlying is treated as +1 delta per 100 shares in the option-leg sum.

    max_profit = (short_call_strike - spot) * 100 + short_call_premium * 100
    max_loss = spot * 100 - short_call_premium * 100  # underlying goes to zero
    breakeven = spot - short_call_premium

    return StrategyProposal(
        underlying=underlying,
        strategy_family="covered_call",
        legs=legs,
        net_debit=None,
        net_credit=short_call_premium * 100,
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=[breakeven],
        net_delta=net_d,
        net_gamma=net_g,
        net_theta=net_t,
        net_vega=net_v,
        confidence=confidence,
        rationale=(
            f"Sell {short_call_strike:.2f} call exp {short_call_expiration} "
            f"for ${short_call_premium:.2f}; cap upside at {short_call_strike:.2f}, "
            f"breakeven {breakeven:.2f}."
        ),
    )


def cash_secured_put(
    *,
    underlying: str,
    spot: float,
    short_put_strike: float,
    short_put_expiration: date,
    short_put_premium: float,
    risk_free_rate: float = 0.045,
    today: Optional[date] = None,
    confidence: float = 0.5,
) -> StrategyProposal:
    """Cash-secured put: short 1 OTM put + cash collateral."""
    today = today or date.today()
    short_put_leg = Leg(
        occ_symbol=f"{underlying.upper()}{short_put_expiration.strftime('%y%m%d')}P{int(short_put_strike*1000):08d}",
        underlying=underlying,
        expiration=short_put_expiration,
        strike=short_put_strike,
        opt_type="put",
        qty=-1,
        entry_price=short_put_premium,
    )
    legs = [short_put_leg]
    net_d, net_g, net_t, net_v = _aggregate_greeks(legs, spot, risk_free_rate, today)

    max_profit = short_put_premium * 100
    max_loss = (short_put_strike - short_put_premium) * 100
    breakeven = short_put_strike - short_put_premium

    return StrategyProposal(
        underlying=underlying,
        strategy_family="cash_secured_put",
        legs=legs,
        net_debit=None,
        net_credit=short_put_premium * 100,
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=[breakeven],
        net_delta=net_d,
        net_gamma=net_g,
        net_theta=net_t,
        net_vega=net_v,
        confidence=confidence,
        rationale=(
            f"Sell {short_put_strike:.2f} put exp {short_put_expiration} "
            f"for ${short_put_premium:.2f}; assigned at "
            f"effective basis {breakeven:.2f}."
        ),
    )


def vertical_spread(
    *,
    underlying: str,
    spot: float,
    long_strike: float,
    short_strike: float,
    expiration: date,
    long_premium: float,
    short_premium: float,
    direction: str,            # 'bull_call' | 'bear_put'
    risk_free_rate: float = 0.045,
    today: Optional[date] = None,
    confidence: float = 0.5,
) -> StrategyProposal:
    """Defined-risk directional spread."""
    today = today or date.today()
    if direction == "bull_call":
        opt_type = "call"
        if long_strike >= short_strike:
            raise ValueError("bull_call: long_strike must be < short_strike")
    elif direction == "bear_put":
        opt_type = "put"
        if long_strike <= short_strike:
            raise ValueError("bear_put: long_strike must be > short_strike")
    else:
        raise ValueError(f"direction must be 'bull_call' or 'bear_put', got {direction!r}")

    long_leg = Leg(
        occ_symbol=f"{underlying.upper()}{expiration.strftime('%y%m%d')}{opt_type[0].upper()}{int(long_strike*1000):08d}",
        underlying=underlying, expiration=expiration, strike=long_strike,
        opt_type=opt_type, qty=1, entry_price=long_premium,
    )
    short_leg = Leg(
        occ_symbol=f"{underlying.upper()}{expiration.strftime('%y%m%d')}{opt_type[0].upper()}{int(short_strike*1000):08d}",
        underlying=underlying, expiration=expiration, strike=short_strike,
        opt_type=opt_type, qty=-1, entry_price=short_premium,
    )
    legs = [long_leg, short_leg]
    net_d, net_g, net_t, net_v = _aggregate_greeks(legs, spot, risk_free_rate, today)

    net_debit = (long_premium - short_premium) * 100
    spread_width = abs(short_strike - long_strike)
    max_profit = (spread_width * 100) - net_debit
    max_loss = net_debit
    if direction == "bull_call":
        breakeven = long_strike + net_debit / 100
    else:
        breakeven = long_strike - net_debit / 100

    return StrategyProposal(
        underlying=underlying,
        strategy_family=f"vertical_spread_{direction}",
        legs=legs,
        net_debit=net_debit,
        net_credit=None,
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=[breakeven],
        net_delta=net_d,
        net_gamma=net_g,
        net_theta=net_t,
        net_vega=net_v,
        confidence=confidence,
        rationale=(
            f"{direction} {long_strike:.2f}/{short_strike:.2f} {opt_type} spread "
            f"exp {expiration}; debit ${net_debit:.2f}, max profit "
            f"${max_profit:.2f}, breakeven {breakeven:.2f}."
        ),
    )


def iron_condor(
    *,
    underlying: str,
    spot: float,
    short_put_strike: float,
    long_put_strike: float,
    short_call_strike: float,
    long_call_strike: float,
    expiration: date,
    short_put_premium: float,
    long_put_premium: float,
    short_call_premium: float,
    long_call_premium: float,
    risk_free_rate: float = 0.045,
    today: Optional[date] = None,
    confidence: float = 0.5,
) -> StrategyProposal:
    """Iron condor: short OTM call spread + short OTM put spread."""
    today = today or date.today()
    if not (
        long_put_strike < short_put_strike < short_call_strike < long_call_strike
    ):
        raise ValueError(
            "iron_condor strikes must satisfy: "
            "long_put < short_put < short_call < long_call"
        )
    legs = [
        Leg(
            occ_symbol=f"{underlying.upper()}{expiration.strftime('%y%m%d')}P{int(long_put_strike*1000):08d}",
            underlying=underlying, expiration=expiration, strike=long_put_strike,
            opt_type="put", qty=1, entry_price=long_put_premium,
        ),
        Leg(
            occ_symbol=f"{underlying.upper()}{expiration.strftime('%y%m%d')}P{int(short_put_strike*1000):08d}",
            underlying=underlying, expiration=expiration, strike=short_put_strike,
            opt_type="put", qty=-1, entry_price=short_put_premium,
        ),
        Leg(
            occ_symbol=f"{underlying.upper()}{expiration.strftime('%y%m%d')}C{int(short_call_strike*1000):08d}",
            underlying=underlying, expiration=expiration, strike=short_call_strike,
            opt_type="call", qty=-1, entry_price=short_call_premium,
        ),
        Leg(
            occ_symbol=f"{underlying.upper()}{expiration.strftime('%y%m%d')}C{int(long_call_strike*1000):08d}",
            underlying=underlying, expiration=expiration, strike=long_call_strike,
            opt_type="call", qty=1, entry_price=long_call_premium,
        ),
    ]
    net_d, net_g, net_t, net_v = _aggregate_greeks(legs, spot, risk_free_rate, today)

    net_credit = (
        short_put_premium - long_put_premium
        + short_call_premium - long_call_premium
    ) * 100
    put_wing = (short_put_strike - long_put_strike) * 100
    call_wing = (long_call_strike - short_call_strike) * 100
    max_loss = max(put_wing, call_wing) - net_credit
    max_profit = net_credit
    breakevens = [
        short_put_strike - net_credit / 100,
        short_call_strike + net_credit / 100,
    ]

    return StrategyProposal(
        underlying=underlying,
        strategy_family="iron_condor",
        legs=legs,
        net_debit=None,
        net_credit=net_credit,
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=breakevens,
        net_delta=net_d,
        net_gamma=net_g,
        net_theta=net_t,
        net_vega=net_v,
        confidence=confidence,
        rationale=(
            f"Iron condor {long_put_strike:.2f}/{short_put_strike:.2f} | "
            f"{short_call_strike:.2f}/{long_call_strike:.2f} exp {expiration}; "
            f"credit ${net_credit:.2f}, range "
            f"{breakevens[0]:.2f}-{breakevens[1]:.2f}."
        ),
    )


# --- Persistence -------------------------------------------------------

def persist_proposal(
    db: Session,
    user_id: Optional[int],
    proposal: StrategyProposal,
) -> Optional[int]:
    """Insert into options_strategy_proposal. Returns the new id."""
    try:
        legs_json = json.dumps([
            {
                "occ_symbol": leg.occ_symbol,
                "underlying": leg.underlying,
                "expiration": leg.expiration.isoformat(),
                "strike": leg.strike,
                "opt_type": leg.opt_type,
                "qty": leg.qty,
                "entry_price": leg.entry_price,
            }
            for leg in proposal.legs
        ])
        row = db.execute(
            text(
                """
                INSERT INTO options_strategy_proposal
                    (user_id, underlying, strategy_family, legs_json,
                     net_debit, net_credit, max_loss, max_profit, breakevens,
                     net_delta, net_gamma, net_theta, net_vega,
                     confidence, rationale, status)
                VALUES (:uid, :u, :sf, :lj, :nd, :nc, :ml, :mp, :be,
                        :ndelta, :ngamma, :ntheta, :nvega,
                        :c, :r, 'proposed')
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "u": proposal.underlying,
                "sf": proposal.strategy_family,
                "lj": legs_json,
                "nd": proposal.net_debit,
                "nc": proposal.net_credit,
                "ml": proposal.max_loss,
                "mp": proposal.max_profit,
                "be": proposal.breakevens,
                "ndelta": proposal.net_delta,
                "ngamma": proposal.net_gamma,
                "ntheta": proposal.net_theta,
                "nvega": proposal.net_vega,
                "c": proposal.confidence,
                "r": proposal.rationale,
            },
        ).fetchone()
        db.commit()
        return int(row[0]) if row else None
    except Exception as e:
        try:
            db.rollback()
        except Exception:
            pass
        logger.warning("[options.strategies] persist_proposal failed: %s", e)
        return None
