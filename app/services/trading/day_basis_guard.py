"""Plausibility guard for the DAY-CHANGE BASIS (the previous close).

WHY THIS EXISTS (measured, not assumed)
---------------------------------------
``move_pct`` / ``gap_pct`` / ``todays_change_perc`` are not measured quantities.
They are an unguarded division by a vendor field the code never validated:

    move_pct = (price - prev_close) / prev_close * 100

On 2026-09-02 the ignition loop logged ``symbol=HOS move_pct=3462.37`` across
798 observations. Two consecutive lines 14s apart --

    06:39:23  symbol=HOS move_pct=3462.37 scored_ok=True
    06:39:37  symbol=HOS move_pct=3447.36 scored_ok=True

-- differ by 15.01 percentage points, which is a 0.43% price move. The deltas
are honest; only the LEVEL is invented. Solving 10.687 / 35.6237 gives a basis
of exactly 0.3000, and the persisted row says so in as many words:
``momentum_symbol_viability`` HOS, updated_at 2026-09-02 22:56:34,
``ross_signals.HOS = {"price": 10.33, "gap_pct": 3345.63, "prev_close": 0.3,
...}``. A round number, not a real close. Two independent tapes say $0.30 never
happened: ``iqfeed_trade_ticks`` HOS 2026-09-02 14:00-14:30Z, 1,687 prints,
10.37-10.6099; ``momentum_nbbo_spread_tape`` 14:00-14:20Z, 36 rows, mid
10.52-10.595. HOS is absent from the independent full-market mover set
entirely.

The harm is not cosmetic and it runs in BOTH directions:

* Too LOW a basis manufactures a leader. HOS took the #1 ``ross_score`` of the
  whole board that session (0.5081 vs 0.3464 for the best real name), one of
  five ``meta["top_market_gainers"]`` slots, the +0.03 viability tilt that
  membership confers, and a slot in the 40-name sympathy set -- for a stock
  that moved 4.75% all day.
* Too HIGH a basis BENCHES a real mover. ``ross_universe_change_below_profile``
  (a +5.0% floor) fired 1,909 times across 17 symbol-days whose true day change
  was between +11.8% and +946.4%. On 2026-08-20 alone: SGLY x324 (+182.9%),
  HUIZ x323 (+186.3%), JZ x318 (+221.2%). A 5% floor cannot reject a +221% name
  unless the number it read was wrong.

WHAT THIS GUARD DOES AND DOES NOT CATCH -- stated plainly
---------------------------------------------------------
It catches ORDER-OF-MAGNITUDE fiction and internally self-contradicting rows,
using only data already present in the same snapshot. No second fetch, no
network, no new provider.

  (1) The prior session's own bar contradicts its close. ``prevDay.c`` must lie
      inside ``[prevDay.l, prevDay.h]`` (2% tolerance). A close outside the
      range it supposedly closed in is a wrong or stale row by construction.
  (2) The basis is orders of magnitude away from where the stock is trading
      NOW. ``max(ref/basis, basis/ref) > 20`` where ref is today's open, else
      the live price. HOS is 34.4x and is caught. A +525.6% real gapper (SGLD
      2026-09-02, prev close 5.08) is 6.3x and is NOT flagged -- deliberately.

It does NOT catch the ~2x class (JZ 2026-08-20: implied basis ~3.7 against a
true prev close of 1.70) unless the vendor's own prevDay bar contradicts it. A
2x disagreement between a close and today's open is an ordinary gap in this
universe, so no bound tight enough to catch JZ is safe against real movers.
That half of the defect needs a genuine second source and is not claimed here.

FAIL DIRECTION: a rejected basis yields ``None``, never a substitute number.
That follows the discipline already established in ``ignition_loop._score_symbol``
("TAPAT NA STAMPING"): an unknown day move is NOT stamped, because a faked 0.0
becomes real evidence downstream. A missing change is fail-CLOSED at the Ross
arm gate (``ross_universe_missing_change_pct``) while a fictional one is
fail-OPEN into the top of the ranking -- so refusing to divide is strictly the
safer error.
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# Verdicts.
DAY_BASIS_OK = "ok"
DAY_BASIS_MISSING = "missing"
DAY_BASIS_NON_POSITIVE = "non_positive"
DAY_BASIS_OUTSIDE_PREV_RANGE = "outside_prev_range"
DAY_BASIS_IMPLAUSIBLE_VS_SESSION = "implausible_vs_session"

# Everything except ok/missing is a REJECTION (missing simply means the caller's
# existing fallback applies, unchanged).
DAY_BASIS_REJECTED = frozenset(
    {
        DAY_BASIS_NON_POSITIVE,
        DAY_BASIS_OUTSIDE_PREV_RANGE,
        DAY_BASIS_IMPLAUSIBLE_VS_SESSION,
    }
)

# 20x = a claimed day change of +1,900% / -95%. HOS 2026-09-02 is 34.4x (10.33
# against 0.30) and is caught. The largest REAL day change in the 11-session
# market-truth set is WVVIP 2026-08-25 at +946.4% (10.5x), which stays inside
# the bound. Chosen so that no observed real mover is rejected -- a false
# positive here costs a name its stamped change (fail-open at the scorer,
# fail-closed at the arm gate), so the bound is deliberately generous.
DAY_BASIS_MAX_RATIO = 20.0

# The prior bar's own high/low, with room for a vendor rounding a fraction of a
# cent and for a close printed slightly outside the consolidated range.
_PREV_RANGE_TOLERANCE = 0.02

# Continuity: a symbol's PREV CLOSE cannot change during a session. Anything
# past this is a re-basing event -- a corporate action, or the corruption
# signature (KXIN/CDTG/CAST all show the implied basis moving intraday).
DAY_BASIS_CONTINUITY_MAX_DRIFT = 0.05


def _f(value) -> float | None:
    try:
        if value is None:
            return None
        out = float(value)
    except (TypeError, ValueError):
        return None
    if out != out:  # NaN
        return None
    return out


def classify_day_basis(
    prev_close,
    *,
    open_price=None,
    price=None,
    prev_high=None,
    prev_low=None,
    max_ratio: float | None = None,
) -> tuple[float | None, str]:
    """Return ``(basis, verdict)`` for a candidate previous close.

    ``basis`` is the accepted value, or ``None`` when the verdict is anything
    other than :data:`DAY_BASIS_OK`. The caller decides what a rejection means
    for it; this function never substitutes a different number.
    """
    base = _f(prev_close)
    if base is None:
        return None, DAY_BASIS_MISSING
    if base <= 0:
        return None, DAY_BASIS_NON_POSITIVE

    lo = _f(prev_low)
    hi = _f(prev_high)
    if lo is not None and hi is not None and lo > 0 and hi >= lo:
        if base < lo * (1.0 - _PREV_RANGE_TOLERANCE) or base > hi * (
            1.0 + _PREV_RANGE_TOLERANCE
        ):
            return None, DAY_BASIS_OUTSIDE_PREV_RANGE

    ref = _f(open_price)
    if ref is None or ref <= 0:
        ref = _f(price)
    if ref is not None and ref > 0:
        ratio = max(ref / base, base / ref)
        if ratio > float(max_ratio or DAY_BASIS_MAX_RATIO):
            return None, DAY_BASIS_IMPLAUSIBLE_VS_SESSION

    return base, DAY_BASIS_OK


def basis_continuity_broken(
    previous_basis, candidate_basis, *, max_drift: float | None = None
) -> bool:
    """True when an already-accepted basis moved mid-session beyond the drift bound.

    A previous close is fixed for the whole session. Movement is either a
    corporate action (rare, and worth a loud line either way) or the corruption
    signature. The caller FREEZES the earlier value rather than re-basing.
    """
    prev = _f(previous_basis)
    cand = _f(candidate_basis)
    if prev is None or cand is None or prev <= 0 or cand <= 0:
        return False
    drift = abs(cand - prev) / prev
    return drift > float(
        DAY_BASIS_CONTINUITY_MAX_DRIFT if max_drift is None else max_drift
    )
