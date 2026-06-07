"""News-catalyst pillar for the Ross momentum lane (E5).

Ross's selection edge is RVOL + gap + low-float + a NEWS CATALYST. The first three
pillars are scored by ross_momentum; this module adds the catalyst: a mover with a
known news event (earnings) is far more likely to be a real Ross gapper than a
random spike. Applied as an ADDITIVE viability tilt (boost catalyst names, never
penalise — crypto + many stocks have no earnings). docs/DESIGN/MOMENTUM_LANE.md

Earnings is the most accessible catalyst (Benzinga via Massive); FDA/PR/contract
feeds are a later extension. Best-effort + cached: returns an empty set (no boost)
when the data plan lacks Benzinga, so the lane degrades gracefully.
"""
from __future__ import annotations

import logging

from ....config import settings

logger = logging.getLogger(__name__)

# ONE documented knob: how strongly a news catalyst tilts viability (vs Ross's 0.20
# selection tilt). News is one confirming signal, so a smaller boost.
CATALYST_VIABILITY_TILT = 0.10


def _catalyst_tilt() -> float:
    try:
        v = float(getattr(settings, "chili_momentum_catalyst_viability_tilt", CATALYST_VIABILITY_TILT))
    except (TypeError, ValueError):
        return CATALYST_VIABILITY_TILT
    return max(0.0, min(v, 0.5))


def _norm(symbol: str) -> str:
    s = str(symbol or "").upper().strip()
    # equities are bare tickers; crypto pairs carry -USD (never have earnings).
    return s.split("-", 1)[0] if "-" in s else s


def earnings_catalyst_symbols() -> set[str]:
    """Tickers with a near-term EARNINGS catalyst (best-effort, cached upstream).

    Returns an empty set when Benzinga is unavailable so the catalyst tilt is a
    no-op rather than an error.
    """
    try:
        from ...massive_client import get_benzinga_earnings

        rows = get_benzinga_earnings(limit=200) or []
        return {_norm(t) for t in rows if t}
    except Exception:
        logger.debug("[catalyst] earnings fetch failed; no catalyst boost this pass", exc_info=True)
        return set()


def catalyst_score(symbol: str, catalyst_symbols: set[str] | None) -> float:
    """[0,1] catalyst score: 1.0 when the symbol has a known catalyst, else 0.5
    (neutral — no penalty). Crypto (-USD) is always neutral (no earnings)."""
    if not catalyst_symbols:
        return 0.5
    if "-USD" in str(symbol or "").upper():
        return 0.5
    return 1.0 if _norm(symbol) in catalyst_symbols else 0.5


def catalyst_viability_delta(symbol: str, catalyst_symbols: set[str] | None) -> float:
    """The additive viability tilt for a symbol: CATALYST_TILT x (score - 0.5).

    +tilt/2 for a catalyst name, 0 otherwise. Mirrors the Ross-quality tilt so the
    selection prefers explosive movers that ALSO have a news catalyst."""
    return _catalyst_tilt() * (catalyst_score(symbol, catalyst_symbols) - 0.5)
