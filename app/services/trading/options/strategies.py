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
import math
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
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float], list[str]]:
    """Sum greeks across legs (signed by qty).

    Invalid leg inputs are returned as missing risk rather than allowing the
    Black-Scholes degenerate zero sentinel to masquerade as proven exposure.
    """
    missing = _missing_greek_inputs(legs, spot, risk_free_rate, today)
    if missing:
        return None, None, None, None, missing

    net_d = net_g = net_t = net_v = 0.0
    spot_f = float(spot)
    rate_f = float(risk_free_rate)
    for idx, leg in enumerate(legs):
        T = max((leg.expiration - today).days / 365.0, 1.0 / 365.0)
        # Best-effort vol if not provided: use ATM-ish approx via market price
        # via implied_vol(). For seed scaffolding, assume 0.30 fallback.
        vol = leg.entry_price / spot_f * 4
        vol = max(0.05, min(2.0, vol))
        g = bs_greeks(
            spot=spot_f, strike=float(leg.strike),
            time_to_expiry_years=T, risk_free_rate=rate_f,
            volatility=vol, opt_type=leg.opt_type,
        )
        if not all(math.isfinite(v) for v in (g.delta, g.gamma, g.theta, g.vega)):
            return None, None, None, None, [f"leg_{idx}:greek_result"]
        net_d += g.delta * leg.qty
        net_g += g.gamma * leg.qty
        net_t += g.theta * leg.qty
        net_v += g.vega * leg.qty
    return net_d, net_g, net_t, net_v, []


def _finite_float(value: object) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


def _positive_float(value: object) -> float | None:
    out = _finite_float(value)
    if out is None or out <= 0.0:
        return None
    return out


def _missing_greek_inputs(
    legs: list[Leg],
    spot: object,
    risk_free_rate: object,
    today: date,
) -> list[str]:
    missing: list[str] = []
    if _positive_float(spot) is None:
        missing.append("spot")
    if _finite_float(risk_free_rate) is None:
        missing.append("risk_free_rate")
    if not isinstance(today, date):
        missing.append("today")

    for idx, leg in enumerate(legs):
        prefix = f"leg_{idx}"
        if not isinstance(leg.expiration, date):
            missing.append(f"{prefix}:expiration")
        elif isinstance(today, date) and leg.expiration < today:
            missing.append(f"{prefix}:expiration_expired")
        if _positive_float(leg.strike) is None:
            missing.append(f"{prefix}:strike")
        if str(leg.opt_type).strip().lower() not in {"call", "put"}:
            missing.append(f"{prefix}:option_type")
        if isinstance(leg.qty, bool) or not isinstance(leg.qty, int) or leg.qty == 0:
            missing.append(f"{prefix}:qty")
        if _positive_float(leg.entry_price) is None:
            missing.append(f"{prefix}:entry_price")
    return missing


def _risk_meta(missing_greek_inputs: list[str]) -> dict:
    if not missing_greek_inputs:
        return {}
    return {
        "risk_status": "missing_greek_inputs",
        "missing_greek_inputs": missing_greek_inputs,
    }


def _blank_greeks_if_missing(
    missing_risk_inputs: list[str],
    net_delta: Optional[float],
    net_gamma: Optional[float],
    net_theta: Optional[float],
    net_vega: Optional[float],
) -> tuple[Optional[float], Optional[float], Optional[float], Optional[float]]:
    if missing_risk_inputs:
        return None, None, None, None
    return net_delta, net_gamma, net_theta, net_vega


def _occ_symbol(underlying: str, expiration: object, opt_type: str, strike: object) -> str:
    exp_token = expiration.strftime("%y%m%d") if isinstance(expiration, date) else "000000"
    strike_f = _positive_float(strike)
    strike_token = int(strike_f * 1000) if strike_f is not None else 0
    opt_char = "C" if opt_type == "call" else "P" if opt_type == "put" else "X"
    return f"{underlying.upper()}{exp_token}{opt_char}{strike_token:08d}"


def _fmt_price(value: object) -> str:
    parsed = _finite_float(value)
    return f"{parsed:.2f}" if parsed is not None else "unproven"


def _proposal_missing_risk_reasons(proposal: StrategyProposal) -> list[str]:
    reasons: list[str] = []
    missing_greeks = [
        name
        for name in ("delta", "gamma", "theta", "vega")
        if _finite_float(getattr(proposal, f"net_{name}", None)) is None
    ]
    if missing_greeks:
        reasons.append("missing_complete_greeks:" + ",".join(missing_greeks))
    if proposal.meta.get("risk_status") == "missing_greek_inputs":
        missing_inputs = proposal.meta.get("missing_greek_inputs")
        if isinstance(missing_inputs, list) and missing_inputs:
            reasons.append("missing_greek_inputs:" + ",".join(map(str, missing_inputs)))
        else:
            reasons.append("missing_greek_inputs")
    return reasons


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
        occ_symbol=_occ_symbol(underlying, short_call_expiration, "call", short_call_strike),
        underlying=underlying,
        expiration=short_call_expiration,
        strike=short_call_strike,
        opt_type="call",
        qty=-1,
        entry_price=short_call_premium,
    )
    legs = [short_call_leg]
    net_d, net_g, net_t, net_v, missing_greek_inputs = _aggregate_greeks(
        legs, spot, risk_free_rate, today
    )
    # Add long-stock delta (+100 shares = +1.0 delta on the option-equivalent basis).
    if net_d is not None:
        net_d += 1.0  # underlying is treated as +1 delta per 100 shares in the option-leg sum.

    missing_risk_inputs = list(missing_greek_inputs)
    spot_f = _positive_float(spot)
    strike_f = _positive_float(short_call_strike)
    premium_f = _positive_float(short_call_premium)
    if premium_f is not None and spot_f is not None and premium_f >= spot_f:
        missing_risk_inputs.append("covered_call:premium_exceeds_spot")
    if (
        spot_f is not None
        and strike_f is not None
        and premium_f is not None
        and not missing_risk_inputs
    ):
        net_credit = premium_f * 100
        max_profit = (strike_f - spot_f) * 100 + net_credit
        max_loss = spot_f * 100 - net_credit  # underlying goes to zero
        breakevens = [spot_f - premium_f]
    else:
        net_credit = max_loss = max_profit = None
        breakevens = []
    net_d, net_g, net_t, net_v = _blank_greeks_if_missing(
        missing_risk_inputs, net_d, net_g, net_t, net_v
    )

    return StrategyProposal(
        underlying=underlying,
        strategy_family="covered_call",
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
            f"Sell {_fmt_price(short_call_strike)} call exp {short_call_expiration} "
            f"for ${_fmt_price(short_call_premium)}; cap upside at {_fmt_price(short_call_strike)}, "
            f"breakeven {_fmt_price(breakevens[0] if breakevens else None)}."
        ),
        meta=_risk_meta(missing_risk_inputs),
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
        occ_symbol=_occ_symbol(underlying, short_put_expiration, "put", short_put_strike),
        underlying=underlying,
        expiration=short_put_expiration,
        strike=short_put_strike,
        opt_type="put",
        qty=-1,
        entry_price=short_put_premium,
    )
    legs = [short_put_leg]
    net_d, net_g, net_t, net_v, missing_greek_inputs = _aggregate_greeks(
        legs, spot, risk_free_rate, today
    )

    missing_risk_inputs = list(missing_greek_inputs)
    strike_f = _positive_float(short_put_strike)
    premium_f = _positive_float(short_put_premium)
    if strike_f is not None and premium_f is not None and premium_f >= strike_f:
        missing_risk_inputs.append("cash_secured_put:premium_exceeds_strike")
    if strike_f is not None and premium_f is not None and not missing_risk_inputs:
        net_credit = premium_f * 100
        max_profit = net_credit
        max_loss = (strike_f - premium_f) * 100
        breakevens = [strike_f - premium_f]
    else:
        net_credit = max_loss = max_profit = None
        breakevens = []
    net_d, net_g, net_t, net_v = _blank_greeks_if_missing(
        missing_risk_inputs, net_d, net_g, net_t, net_v
    )

    return StrategyProposal(
        underlying=underlying,
        strategy_family="cash_secured_put",
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
            f"Sell {_fmt_price(short_put_strike)} put exp {short_put_expiration} "
            f"for ${_fmt_price(short_put_premium)}; assigned at "
            f"effective basis {_fmt_price(breakevens[0] if breakevens else None)}."
        ),
        meta=_risk_meta(missing_risk_inputs),
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
    long_strike_f = _positive_float(long_strike)
    short_strike_f = _positive_float(short_strike)
    if direction == "bull_call":
        opt_type = "call"
        if long_strike_f is not None and short_strike_f is not None and long_strike_f >= short_strike_f:
            raise ValueError("bull_call: long_strike must be < short_strike")
    elif direction == "bear_put":
        opt_type = "put"
        if long_strike_f is not None and short_strike_f is not None and long_strike_f <= short_strike_f:
            raise ValueError("bear_put: long_strike must be > short_strike")
    else:
        raise ValueError(f"direction must be 'bull_call' or 'bear_put', got {direction!r}")

    long_leg = Leg(
        occ_symbol=_occ_symbol(underlying, expiration, opt_type, long_strike),
        underlying=underlying, expiration=expiration, strike=long_strike,
        opt_type=opt_type, qty=1, entry_price=long_premium,
    )
    short_leg = Leg(
        occ_symbol=_occ_symbol(underlying, expiration, opt_type, short_strike),
        underlying=underlying, expiration=expiration, strike=short_strike,
        opt_type=opt_type, qty=-1, entry_price=short_premium,
    )
    legs = [long_leg, short_leg]
    net_d, net_g, net_t, net_v, missing_greek_inputs = _aggregate_greeks(
        legs, spot, risk_free_rate, today
    )

    missing_risk_inputs = list(missing_greek_inputs)
    long_premium_f = _positive_float(long_premium)
    short_premium_f = _positive_float(short_premium)
    if (
        long_strike_f is not None
        and short_strike_f is not None
        and long_premium_f is not None
        and short_premium_f is not None
    ):
        spread_width = abs(short_strike_f - long_strike_f)
        computed_net_debit = (long_premium_f - short_premium_f) * 100
        if computed_net_debit <= 0:
            missing_risk_inputs.append("vertical_spread:net_debit_nonpositive")
        elif computed_net_debit > spread_width * 100:
            missing_risk_inputs.append("vertical_spread:net_debit_exceeds_width")
        if not missing_risk_inputs:
            net_debit = computed_net_debit
            max_profit = (spread_width * 100) - net_debit
            max_loss = net_debit
            if direction == "bull_call":
                breakevens = [long_strike_f + net_debit / 100]
            else:
                breakevens = [long_strike_f - net_debit / 100]
        else:
            net_debit = max_loss = max_profit = None
            breakevens = []
    else:
        net_debit = max_loss = max_profit = None
        breakevens = []
    net_d, net_g, net_t, net_v = _blank_greeks_if_missing(
        missing_risk_inputs, net_d, net_g, net_t, net_v
    )

    return StrategyProposal(
        underlying=underlying,
        strategy_family=f"vertical_spread_{direction}",
        legs=legs,
        net_debit=net_debit,
        net_credit=None,
        max_loss=max_loss,
        max_profit=max_profit,
        breakevens=breakevens,
        net_delta=net_d,
        net_gamma=net_g,
        net_theta=net_t,
        net_vega=net_v,
        confidence=confidence,
        rationale=(
            f"{direction} {_fmt_price(long_strike)}/{_fmt_price(short_strike)} {opt_type} spread "
            f"exp {expiration}; debit ${_fmt_price(net_debit)}, max profit "
            f"${_fmt_price(max_profit)}, breakeven {_fmt_price(breakevens[0] if breakevens else None)}."
        ),
        meta=_risk_meta(missing_risk_inputs),
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
    long_put_f = _positive_float(long_put_strike)
    short_put_f = _positive_float(short_put_strike)
    short_call_f = _positive_float(short_call_strike)
    long_call_f = _positive_float(long_call_strike)
    valid_strikes = [long_put_f, short_put_f, short_call_f, long_call_f]
    if all(value is not None for value in valid_strikes) and not (
        long_put_f < short_put_f < short_call_f < long_call_f
    ):
        raise ValueError(
            "iron_condor strikes must satisfy: "
            "long_put < short_put < short_call < long_call"
        )
    legs = [
        Leg(
            occ_symbol=_occ_symbol(underlying, expiration, "put", long_put_strike),
            underlying=underlying, expiration=expiration, strike=long_put_strike,
            opt_type="put", qty=1, entry_price=long_put_premium,
        ),
        Leg(
            occ_symbol=_occ_symbol(underlying, expiration, "put", short_put_strike),
            underlying=underlying, expiration=expiration, strike=short_put_strike,
            opt_type="put", qty=-1, entry_price=short_put_premium,
        ),
        Leg(
            occ_symbol=_occ_symbol(underlying, expiration, "call", short_call_strike),
            underlying=underlying, expiration=expiration, strike=short_call_strike,
            opt_type="call", qty=-1, entry_price=short_call_premium,
        ),
        Leg(
            occ_symbol=_occ_symbol(underlying, expiration, "call", long_call_strike),
            underlying=underlying, expiration=expiration, strike=long_call_strike,
            opt_type="call", qty=1, entry_price=long_call_premium,
        ),
    ]
    net_d, net_g, net_t, net_v, missing_greek_inputs = _aggregate_greeks(
        legs, spot, risk_free_rate, today
    )

    missing_risk_inputs = list(missing_greek_inputs)
    short_put_premium_f = _positive_float(short_put_premium)
    long_put_premium_f = _positive_float(long_put_premium)
    short_call_premium_f = _positive_float(short_call_premium)
    long_call_premium_f = _positive_float(long_call_premium)
    if (
        all(value is not None for value in valid_strikes)
        and short_put_premium_f is not None
        and long_put_premium_f is not None
        and short_call_premium_f is not None
        and long_call_premium_f is not None
    ):
        computed_net_credit = (
            short_put_premium_f - long_put_premium_f
            + short_call_premium_f - long_call_premium_f
        ) * 100
        put_wing = (short_put_f - long_put_f) * 100
        call_wing = (long_call_f - short_call_f) * 100
        max_wing = max(put_wing, call_wing)
        if computed_net_credit <= 0:
            missing_risk_inputs.append("iron_condor:net_credit_nonpositive")
        elif computed_net_credit >= max_wing:
            missing_risk_inputs.append("iron_condor:net_credit_exceeds_wing")
        if not missing_risk_inputs:
            net_credit = computed_net_credit
            max_loss = max_wing - net_credit
            max_profit = net_credit
            breakevens = [
                short_put_f - net_credit / 100,
                short_call_f + net_credit / 100,
            ]
        else:
            net_credit = max_loss = max_profit = None
            breakevens = []
    else:
        net_credit = max_loss = max_profit = None
        breakevens = []
    net_d, net_g, net_t, net_v = _blank_greeks_if_missing(
        missing_risk_inputs, net_d, net_g, net_t, net_v
    )

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
            f"Iron condor {_fmt_price(long_put_strike)}/{_fmt_price(short_put_strike)} | "
            f"{_fmt_price(short_call_strike)}/{_fmt_price(long_call_strike)} exp {expiration}; "
            f"credit ${_fmt_price(net_credit)}, range "
            f"{_fmt_price(breakevens[0] if breakevens else None)}-{_fmt_price(breakevens[1] if len(breakevens) > 1 else None)}."
        ),
        meta=_risk_meta(missing_risk_inputs),
    )


# --- Persistence -------------------------------------------------------

def persist_proposal(
    db: Session,
    user_id: Optional[int],
    proposal: StrategyProposal,
) -> Optional[int]:
    """Insert into options_strategy_proposal. Returns the new id."""
    try:
        missing_risk = _proposal_missing_risk_reasons(proposal)
        if missing_risk:
            logger.warning(
                "[options.strategies] refusing proposal with missing risk: %s",
                missing_risk,
            )
            return None
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
