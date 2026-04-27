"""Q2.T2 — three seed FX strategies.

  - london_breakout      : Asia-session range break at London open.
                           Pre-London range = high - low of preceding 6h.
                           Entry on close above range_high (long) or below
                           range_low (short) within first 90 min of London.
                           Stop = opposite side of range; TP = 2x range width.

  - carry_with_risk_off  : Long high-yield ccy / short low-yield ccy IF
                           macro regime is risk-on. Skip during risk-off
                           (VIX > 25, macro_regime label = 'risk_off').
                           Yield differential from FRED + ECB + BoJ rates.

  - news_fade            : 30-min after high-impact event (NFP, CPI),
                           fade the initial spike if it lacks follow-through.
                           Entry = bar 5 retests bar 1's range. Tight stop.

All three flag-gated by chili_forex_lane_enabled. All write proposed
trades to fx_position with is_paper=True until operator promotes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Optional

from sqlalchemy import text
from sqlalchemy.orm import Session

from .sessions import LONDON, LONDON_NY, NY, session_for_utc

logger = logging.getLogger(__name__)


@dataclass
class FxProposal:
    pair: str
    side: str             # 'long' | 'short'
    units: int            # signed: positive long, negative short
    entry_price: float
    stop_price: float
    take_profit_price: float
    strategy_family: str
    confidence: float
    rationale: str
    session_at_entry: str
    expected_pnl_pips: float
    risk_pips: float
    meta: dict = field(default_factory=dict)


def _to_pips(price_diff: float, pip_size: float) -> float:
    return abs(price_diff) / pip_size


def london_breakout(
    *,
    pair: str,
    pip_size: float,
    asia_high: float,
    asia_low: float,
    current_price: float,
    now_utc: datetime,
    units: int = 1000,
    risk_reward: float = 2.0,
    confidence: float = 0.55,
) -> Optional[FxProposal]:
    """Returns a proposal iff the current_price has broken the Asia range
    AND we're in the first 90 min of London session."""
    session = session_for_utc(now_utc)
    if session not in (LONDON, LONDON_NY):
        return None
    # First 90 min of London (08:00-09:30 UTC).
    if now_utc.hour > 9 or (now_utc.hour == 9 and now_utc.minute > 30):
        return None

    range_width = asia_high - asia_low
    if range_width <= 0:
        return None

    if current_price > asia_high:
        side = "long"
        signed = abs(units)
        entry = current_price
        stop = asia_low
        tp = entry + risk_reward * range_width
    elif current_price < asia_low:
        side = "short"
        signed = -abs(units)
        entry = current_price
        stop = asia_high
        tp = entry - risk_reward * range_width
    else:
        return None

    return FxProposal(
        pair=pair,
        side=side,
        units=signed,
        entry_price=entry,
        stop_price=stop,
        take_profit_price=tp,
        strategy_family="london_breakout",
        confidence=confidence,
        rationale=(
            f"Asia range {asia_low:.5f}-{asia_high:.5f} "
            f"({_to_pips(range_width, pip_size):.1f} pips); "
            f"break {side} at {entry:.5f}, stop {stop:.5f}, TP {tp:.5f}."
        ),
        session_at_entry=session,
        expected_pnl_pips=_to_pips(tp - entry, pip_size),
        risk_pips=_to_pips(entry - stop, pip_size),
    )


def carry_with_risk_off(
    *,
    pair: str,
    pip_size: float,
    base_yield_annual: float,
    quote_yield_annual: float,
    current_price: float,
    macro_label: str,                 # 'risk_on' | 'cautious' | 'risk_off'
    vix_level: float,
    now_utc: datetime,
    units: int = 1000,
    confidence: float = 0.50,
) -> Optional[FxProposal]:
    """Long high-yielder, short low-yielder, gated on macro regime.

    Skip when:
      - macro_label is 'risk_off'
      - vix_level > 25
      - yield differential < 1% annualized (carry too thin)
    """
    if macro_label == "risk_off":
        return None
    if vix_level > 25.0:
        return None
    diff = base_yield_annual - quote_yield_annual
    if abs(diff) < 1.0:
        return None

    session = session_for_utc(now_utc)
    if diff > 0:
        side = "long"
        signed = abs(units)
        entry = current_price
        stop = entry - 50 * pip_size
        tp = entry + 100 * pip_size
    else:
        side = "short"
        signed = -abs(units)
        entry = current_price
        stop = entry + 50 * pip_size
        tp = entry - 100 * pip_size

    return FxProposal(
        pair=pair,
        side=side,
        units=signed,
        entry_price=entry,
        stop_price=stop,
        take_profit_price=tp,
        strategy_family="carry_with_risk_off",
        confidence=confidence,
        rationale=(
            f"Yield diff {diff:+.2f}%, macro={macro_label}, VIX={vix_level:.1f}; "
            f"{side} carry trade, 50p stop / 100p TP."
        ),
        session_at_entry=session,
        expected_pnl_pips=100,
        risk_pips=50,
        meta={
            "yield_differential_pct": diff,
            "macro_label": macro_label,
            "vix_level": vix_level,
        },
    )


def news_fade(
    *,
    pair: str,
    pip_size: float,
    spike_price: float,
    pre_event_close: float,
    current_price: float,
    minutes_since_event: float,
    units: int = 500,
    confidence: float = 0.45,
) -> Optional[FxProposal]:
    """Fade the initial post-news spike if it failed to follow through.

    Triggered:
      - 25-45 min after a 3-star event
      - Initial spike was >30 pips
      - Current price has retraced >50% of the spike
    """
    if minutes_since_event < 25 or minutes_since_event > 45:
        return None
    spike_pips = _to_pips(spike_price - pre_event_close, pip_size)
    if spike_pips < 30:
        return None
    retrace_pct = (
        abs(spike_price - current_price) / max(abs(spike_price - pre_event_close), 1e-9)
    )
    if retrace_pct < 0.50:
        return None

    if spike_price > pre_event_close:
        # Spike up; fade short
        side = "short"
        signed = -abs(units)
        entry = current_price
        stop = spike_price + 5 * pip_size  # tight, just above spike high
        tp = pre_event_close
    else:
        side = "long"
        signed = abs(units)
        entry = current_price
        stop = spike_price - 5 * pip_size
        tp = pre_event_close

    return FxProposal(
        pair=pair,
        side=side,
        units=signed,
        entry_price=entry,
        stop_price=stop,
        take_profit_price=tp,
        strategy_family="news_fade",
        confidence=confidence,
        rationale=(
            f"News spike {spike_pips:.1f} pips, retrace {retrace_pct*100:.0f}%, "
            f"{minutes_since_event:.0f}min after event; fade {side}."
        ),
        session_at_entry=session_for_utc(datetime.now(timezone.utc)),
        expected_pnl_pips=_to_pips(tp - entry, pip_size),
        risk_pips=_to_pips(entry - stop, pip_size),
        meta={"spike_pips": spike_pips, "retrace_pct": retrace_pct},
    )


def persist_proposal(
    db: Session,
    user_id: Optional[int],
    proposal: FxProposal,
    is_paper: bool = True,
) -> Optional[int]:
    """Insert into fx_position with closed_at NULL (open). Returns new id."""
    try:
        row = db.execute(
            text(
                """
                INSERT INTO fx_position
                    (user_id, pair, side, units, entry_price, entry_session,
                     stop_price, take_profit_price, strategy_family, is_paper)
                VALUES
                    (:uid, :p, :side, :u, :ep, :s, :sp, :tp, :sf, :paper)
                RETURNING id
                """
            ),
            {
                "uid": user_id,
                "p": proposal.pair,
                "side": proposal.side,
                "u": proposal.units,
                "ep": proposal.entry_price,
                "s": proposal.session_at_entry,
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
        logger.warning("[forex.strategies] persist_proposal failed: %s", e)
        return None
