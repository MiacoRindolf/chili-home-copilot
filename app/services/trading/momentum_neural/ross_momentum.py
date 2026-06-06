"""Ross-Cameron-style momentum-quality scoring for the Momentum Lane (M2).

Ranks EXPLOSIVE instruments (high relative volume + already-moving + low-float/
small-cap) above generic ones — the *selection* edge a momentum day-trader
relies on. Consumes the screen-able Ross pillars that
``scanner._score_ticker_intraday`` already computes (``vol_ratio`` /
``gap_pct`` / ``daily_change_pct``) but that the scanner→viability bridge
historically discarded (see ``docs/DESIGN/MOMENTUM_LANE.md`` §7).

Design constraints (operator: "no magic numbers, use adaptive logic"):
  * **Adaptive, not hardcoded.** Each pillar is PERCENTILE-RANKED within the
    current universe batch, so there is no fixed ``RVOL >= 5x`` cutoff — the bar
    floats with whatever is actually moving right now. Ross's literal ``5x`` was
    calibrated to the small-cap equity tape; percentile ranking generalises it
    to any universe (incl. 24/7 crypto) without a tuned constant.
  * **Crypto-aware.** Works on Coinbase 24/7 crypto (``-USD``; "float" → market
    cap, and the volume surge itself is the catalyst proxy) and on equities.
  * **Transparent.** Every score carries a per-pillar breakdown for audit.
  * **Pure functions.** No DB, no IO — trivially testable in isolation.

The only fixed constants are the pillar WEIGHTS, which encode Ross Cameron's
own stated priority (relative volume is his #1 filter, then "already moving",
then float/liquidity). They are documented as such, not arbitrary tuning, and
can move to settings later.
"""

from __future__ import annotations

import bisect
import logging
import math
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

# Ross's stated 5-pillar priority, normalised to the screen-able structural
# pillars. RVOL is his explicit #1 ("at a minimum I need a relative volume of
# 5"); "already up on the day" is #2 ("never buy what isn't already moving");
# low float / small-cap is the supply side. Sum to 1.0.
ROSS_PILLAR_WEIGHTS: dict[str, float] = {
    "rvol": 0.45,
    "momentum": 0.35,
    "liquidity": 0.20,
}

# Max tilt the Ross momentum quality applies to a momentum-neural viability
# score. ``ross_score`` in [0,1] is centered at 0.5, so the applied tilt is
# +/- (TILT/2): at 0.20 a top-decile explosive setup gets +0.10 — enough to
# clear the live-eligibility floor a generic setup would miss — and a dull one
# -0.10. Same order of magnitude as the existing hand-coded viability nudges.
ROSS_QUALITY_VIABILITY_TILT = 0.20


@dataclass
class RossMomentumScore:
    """Adaptive Ross momentum-quality result for one instrument in a batch."""

    symbol: str
    score: float  # [0,1] blended quality (weighted percentile across pillars)
    rvol_pct: float  # percentile rank of relative volume within the universe
    momentum_pct: float  # percentile rank of daily-change/gap within the universe
    liquidity_pct: float | None  # percentile rank of supply tier (None if absent)
    rank: int  # 1 = most explosive in this universe
    universe_size: int
    breakdown: dict = field(default_factory=dict)

    def in_top_fraction(self, frac: float) -> bool:
        """True if this instrument is in the top ``frac`` of the universe by
        rank (adaptive — the caller, not this module, owns the cutoff)."""
        if self.universe_size <= 0 or frac <= 0.0:
            return False
        return self.rank <= max(1, round(self.universe_size * frac))


def _to_float(v) -> float | None:
    try:
        if v is None:
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def _percentile_rank(value: float, sorted_vals: list[float]) -> float:
    """Fraction of the universe at-or-below ``value``, in [0,1]. The adaptive
    bar — an instrument is "high RVOL" relative to the batch, not an absolute."""
    n = len(sorted_vals)
    if n == 0:
        return 0.5
    return bisect.bisect_right(sorted_vals, value) / n


def _extract_pillars(signal: dict) -> tuple[float | None, float | None, float | None]:
    """(rvol, momentum, liquidity) raw pillar values from a scanner result dict.

    * rvol      — relative volume (``vol_ratio``).
    * momentum  — signed "already moving" %, the stronger of daily change / gap
                  (long bias: a down-mover ranks low, a high-volume *dump* —
                  high rvol but negative momentum — correctly does not top-rank).
    * liquidity — explosiveness of supply: SMALLER float / market-cap → MORE
                  explosive, so we return ``-log10(size)`` (bigger = smaller =
                  more explosive). ``None`` when the size field is unavailable
                  (common for crypto via the intraday scanner today).
    """
    rvol = _to_float(signal.get("vol_ratio"))

    daily = _to_float(signal.get("daily_change_pct"))
    gap = _to_float(signal.get("gap_pct"))
    cands = [x for x in (daily, gap) if x is not None]
    momentum = max(cands) if cands else None

    shares = _to_float(signal.get("float_shares"))
    mcap = _to_float(signal.get("market_cap"))
    size = shares if (shares and shares > 0) else (mcap if (mcap and mcap > 0) else None)
    liquidity = -math.log10(size) if size else None

    return rvol, momentum, liquidity


def score_universe(
    signals: dict[str, dict],
    *,
    weights: dict[str, float] | None = None,
) -> dict[str, RossMomentumScore]:
    """Rank a batch of instruments by Ross momentum quality, adaptively.

    ``signals``: ``{symbol: scanner_result_dict}`` (the dicts
    ``scanner._score_ticker_intraday`` returns — carrying ``vol_ratio``,
    ``gap_pct``, ``daily_change_pct``, optionally ``float_shares``/``market_cap``).

    Returns ``{symbol: RossMomentumScore}`` with ``rank`` assigned (1 = most
    explosive). Each pillar is percentile-ranked within this batch and blended
    with Ross-priority weights, renormalised over whichever pillars are present
    (so a missing liquidity field degrades gracefully to rvol+momentum rather
    than zeroing the score).
    """
    if not signals:
        return {}
    w = dict(weights or ROSS_PILLAR_WEIGHTS)

    pillars: dict[str, tuple[float | None, float | None, float | None]] = {
        sym: _extract_pillars(sig or {}) for sym, sig in signals.items()
    }

    rvol_sorted = sorted(p[0] for p in pillars.values() if p[0] is not None)
    mom_sorted = sorted(p[1] for p in pillars.values() if p[1] is not None)
    liq_sorted = sorted(p[2] for p in pillars.values() if p[2] is not None)

    out: dict[str, RossMomentumScore] = {}
    for sym, (rvol, mom, liq) in pillars.items():
        rvol_pct = _percentile_rank(rvol, rvol_sorted) if rvol is not None else None
        mom_pct = _percentile_rank(mom, mom_sorted) if mom is not None else None
        liq_pct = _percentile_rank(liq, liq_sorted) if liq is not None else None

        present: list[tuple[float, float]] = []  # (percentile, weight)
        if rvol_pct is not None:
            present.append((rvol_pct, w["rvol"]))
        if mom_pct is not None:
            present.append((mom_pct, w["momentum"]))
        if liq_pct is not None:
            present.append((liq_pct, w["liquidity"]))

        wsum = sum(wt for _, wt in present)
        score = (sum(pct * wt for pct, wt in present) / wsum) if wsum > 0 else 0.0

        out[sym] = RossMomentumScore(
            symbol=sym,
            score=round(score, 4),
            rvol_pct=round(rvol_pct, 4) if rvol_pct is not None else 0.0,
            momentum_pct=round(mom_pct, 4) if mom_pct is not None else 0.0,
            liquidity_pct=round(liq_pct, 4) if liq_pct is not None else None,
            rank=0,
            universe_size=len(signals),
            breakdown={
                "rvol": rvol,
                "momentum": mom,
                "liquidity_neglog_size": liq,
                "pillars_present": [
                    name
                    for name, val in (("rvol", rvol_pct), ("momentum", mom_pct), ("liquidity", liq_pct))
                    if val is not None
                ],
            },
        )

    # rank: highest blended score first; ties broken by rvol then momentum
    ordered = sorted(
        out.values(), key=lambda s: (s.score, s.rvol_pct, s.momentum_pct), reverse=True
    )
    for i, s in enumerate(ordered, start=1):
        s.rank = i
    return out
