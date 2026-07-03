"""A3 (Ross CLRO-lesson 2026-07-02) — SCANNER-BREADTH WILDCARD REGIME.

Ross's wildcard-effect thesis: "one stock squeezes for lack of anything else … everyone
focuses on it." 07-02 is the labeled example — the scanner was DEAD (junk) except CLRO
(+200%, 832k ticks). Nothing in the lane conditioned on breadth (only the premarket
min_movers UNLOCK). This module reads the scanner's own state and detects the WILDCARD
regime, so the lane can CONCENTRATE its slots/size on the lone dominant mover.

DEFINITIONS (all read from ``momentum_symbol_viability`` — the live scanner snapshot —
plus ``momentum_viability_history`` for the trailing baseline):
  * breadth   = count of FRESHNESS-VALID, live_eligible EQUITY rows over the trailing
    window (how many real movers the scanner is carrying right now).
  * dominance = the top eligible viability score MINUS the median eligible score (how far
    the leader stands above the pack). A lone leader among junk => high dominance.

WILDCARD regime (ONE documented base = the percentile floor, p20):
  breadth <= its trailing-20-session same-time-of-day p20   (a bottom-decile-breadth day)
  AND dominance >= its own trailing percentile                (a genuinely lone leader).

EFFECTS (wired by the callers; this module only DETECTS + names the dominant symbol):
  (i)  the dominant symbol is confirmed as the arm-queue LEADER (rank boost + hoist +
       eviction-protected watch slot) and is the A1(b)/A2 top-rank beneficiary;
  (ii) B-grade admissions size-tilt DOWN (concentrate risk on the leader) — a tilt through
       the existing size-tilt family, NEVER a veto;
  (iii) PRE-HOLIDAY (a day before a US market holiday) feeds the breadth PRIOR (expect low
       breadth) as a size/trail deweight through the same tilt — never a veto of the leader.

FAIL-CLOSED for the up-weights: any unreadable breadth (thin history, DB error, empty table)
=> NEUTRAL (wildcard False, no dominant symbol, zero effects). The lane ranks/sizes exactly as
today. Flag ``chili_momentum_wildcard_breadth_regime_enabled`` default True.
docs/DESIGN/MOMENTUM_LANE.md; see [[project_momentum_lane]].
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from ....config import settings
from .market_calendar import is_pre_holiday

if TYPE_CHECKING:  # pragma: no cover
    from sqlalchemy.orm import Session

logger = logging.getLogger(__name__)

# ── A3 documented base (the ONE irreducible constant) ───────────────────────────────────────
# The breadth percentile FLOOR: a day whose breadth sits at/below the p20 of the trailing-20-
# session same-time-of-day distribution is a bottom-decile-breadth day (a wildcard candidate).
_BREADTH_PCTL_FLOOR = 0.20
# The dominance percentile the leader must clear vs its own trailing distribution (same base —
# the leader must stand out as much as the breadth is thin). Reuses the SAME p20/p80 symmetry:
# breadth in the bottom p20 AND dominance in the top (1 - p20) = p80.
_DOMINANCE_PCTL_FLOOR = 1.0 - _BREADTH_PCTL_FLOOR
# Trailing baseline window (sessions) for the same-time-of-day percentile. 20 sessions ~ a month.
_TRAILING_SESSIONS = 20
# The B-grade size-tilt DOWN multiplier applied to NON-dominant admissions in a wildcard regime
# (concentrate risk on the leader). A floor, not a veto — B-names still trade, just smaller.
_WILDCARD_B_GRADE_SIZE_TILT = 0.60
# The pre-holiday size/trail deweight (a day before a US market holiday tends to be low-breadth).
_PRE_HOLIDAY_SIZE_TILT = 0.85


@dataclass(frozen=True)
class BreadthRegime:
    """The detected regime. ``is_wildcard`` gates the up-weights; ``dominant_symbol`` names the
    lone leader (None unless wildcard). Neutral instance = fail-closed (no effects)."""
    is_wildcard: bool
    dominant_symbol: str | None
    breadth: int
    dominance: float
    breadth_floor: float          # the p20 baseline breadth this session was measured against
    is_pre_holiday: bool
    reason: str

    def b_grade_size_tilt(self) -> float:
        """The size-tilt multiplier to apply to a NON-dominant (B-grade) admission. 1.0 when not
        wildcard (byte-identical). In a wildcard regime B-names size DOWN (concentrate on the
        leader); a pre-holiday day deweights further. Never zero (a tilt, never a veto)."""
        mult = 1.0
        if self.is_wildcard:
            mult *= _WILDCARD_B_GRADE_SIZE_TILT
        if self.is_pre_holiday:
            mult *= _PRE_HOLIDAY_SIZE_TILT
        return float(max(0.05, min(1.0, mult)))


_NEUTRAL = BreadthRegime(
    is_wildcard=False, dominant_symbol=None, breadth=0, dominance=0.0,
    breadth_floor=0.0, is_pre_holiday=False, reason="neutral",
)


def _now_utc(now: datetime | None) -> datetime:
    if now is not None:
        return now.astimezone(timezone.utc).replace(tzinfo=None) if now.tzinfo else now
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _percentile(sorted_vals: list[float], q: float) -> float | None:
    """Linear-interpolated q-percentile of a pre-sorted list. None on empty."""
    n = len(sorted_vals)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_vals[0])
    pos = max(0.0, min(1.0, q)) * (n - 1)
    lo = int(pos)
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_vals[lo] * (1.0 - frac) + sorted_vals[hi] * frac)


def compute_breadth_regime(
    db: "Session | None",
    *,
    now: datetime | None = None,
) -> BreadthRegime:
    """Detect the wildcard breadth regime from the live scanner snapshot + trailing history.

    FAIL-CLOSED: flag OFF / no db / thin history / DB error / empty table => ``_NEUTRAL`` (no
    wildcard, no dominant symbol, zero effects). The lane behaves exactly as today."""
    if db is None:
        return _NEUTRAL
    if not bool(getattr(settings, "chili_momentum_wildcard_breadth_regime_enabled", True)):
        return _NEUTRAL
    now_utc = _now_utc(now)
    pre_hol = is_pre_holiday(now_utc.date())
    try:
        from sqlalchemy import text

        max_age = float(getattr(settings, "chili_momentum_risk_viability_max_age_seconds", 600.0) or 600.0)
        cutoff = now_utc - timedelta(seconds=max_age)

        # (1) CURRENT breadth + the eligible score distribution (EQUITY snapshot, freshness-valid).
        rows = db.execute(
            text(
                "SELECT symbol, viability_score FROM momentum_symbol_viability "
                "WHERE scope = 'symbol' AND live_eligible = true "
                "AND freshness_ts >= :cutoff AND symbol NOT LIKE '%-USD%'"
            ),
            {"cutoff": cutoff},
        ).fetchall()
        breadth = len(rows)
        if breadth <= 0:
            return BreadthRegime(False, None, 0, 0.0, 0.0, pre_hol, "empty_snapshot")

        scored = sorted(
            ((str(r[0]).upper(), float(r[1] or 0.0)) for r in rows),
            key=lambda t: t[1], reverse=True,
        )
        scores_desc = [s for _, s in scored]
        top_symbol, top_score = scored[0]
        median_score = _percentile(sorted(scores_desc), 0.5) or 0.0
        dominance = float(top_score - median_score)

        # (2) TRAILING same-time-of-day breadth baseline from momentum_viability_history (mig311):
        # count DISTINCT freshness-valid live_eligible equity symbols per prior session, in the
        # SAME ±1h time-of-day window, over the trailing N sessions. p20 of that = the floor.
        hour = now_utc.hour
        hist = db.execute(
            text(
                "SELECT observed_at::date AS d, COUNT(DISTINCT symbol) AS n "
                "FROM momentum_viability_history "
                "WHERE live_eligible = true AND symbol NOT LIKE '%-USD%' "
                "AND observed_at::date < :today "
                "AND EXTRACT(HOUR FROM observed_at) BETWEEN :h_lo AND :h_hi "
                "GROUP BY observed_at::date ORDER BY d DESC LIMIT :lim"
            ),
            {
                "today": now_utc.date(),
                "h_lo": max(0, hour - 1),
                "h_hi": min(23, hour + 1),
                "lim": _TRAILING_SESSIONS,
            },
        ).fetchall()
        session_breadths = sorted(float(r[1] or 0) for r in hist)
        # FAIL-CLOSED: need a real baseline (>= a handful of prior sessions) to call a day "thin".
        if len(session_breadths) < 5:
            return BreadthRegime(False, None, breadth, dominance, 0.0, pre_hol, "thin_baseline")

        breadth_floor = _percentile(session_breadths, _BREADTH_PCTL_FLOOR)
        if breadth_floor is None:
            return BreadthRegime(False, None, breadth, dominance, 0.0, pre_hol, "no_breadth_floor")

        # (3) dominance baseline: the leader must ALSO stand out (top-p80 dominance) vs the same
        # trailing sessions' dominance. We approximate the trailing dominance distribution from
        # the same history rows' per-session score spread when available; when the history lacks
        # viability_score, fall back to requiring a strictly-positive dominance (a real gap).
        dom_rows = db.execute(
            text(
                "SELECT observed_at::date AS d, "
                "MAX(viability_score) - percentile_cont(0.5) WITHIN GROUP (ORDER BY viability_score) AS dom "
                "FROM momentum_viability_history "
                "WHERE live_eligible = true AND symbol NOT LIKE '%-USD%' "
                "AND observed_at::date < :today AND viability_score IS NOT NULL "
                "AND EXTRACT(HOUR FROM observed_at) BETWEEN :h_lo AND :h_hi "
                "GROUP BY observed_at::date ORDER BY d DESC LIMIT :lim"
            ),
            {
                "today": now_utc.date(),
                "h_lo": max(0, hour - 1),
                "h_hi": min(23, hour + 1),
                "lim": _TRAILING_SESSIONS,
            },
        ).fetchall()
        dom_vals = sorted(float(r[1] or 0.0) for r in dom_rows if r[1] is not None)
        dom_floor = _percentile(dom_vals, _DOMINANCE_PCTL_FLOOR) if len(dom_vals) >= 5 else 0.0
        if dom_floor is None:
            dom_floor = 0.0

        is_wildcard = (breadth <= breadth_floor) and (dominance >= dom_floor) and (dominance > 0.0)
        dominant = top_symbol if is_wildcard else None
        reason = "wildcard" if is_wildcard else "broad"
        return BreadthRegime(
            is_wildcard=is_wildcard, dominant_symbol=dominant, breadth=breadth,
            dominance=dominance, breadth_floor=float(breadth_floor),
            is_pre_holiday=pre_hol, reason=reason,
        )
    except Exception:
        logger.debug("[breadth_regime] compute failed (fail-closed to neutral)", exc_info=True)
        return _NEUTRAL
