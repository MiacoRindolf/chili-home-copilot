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
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

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
    # ── S6 (Ross "Forcing a Crash" 08-21) — SUPPRESSION/SQUEEZE axis ─────────────────────────
    # Hiwalay na MARKET-LEVEL axis mula sa daily mover hold-vs-fade aggregates (squeeze_regime
    # module, momentum_squeeze_regime_daily). OBSERVABILITY-ONLY sa phase na ito: naka-log at
    # naka-persist, magagamit ng sizing/selection bilang tilt sa SUSUNOD na phase — walang
    # decision path na nagbabasa nito ngayon. Defaults sa dulo = ang mga umiiral na positional
    # construction (kasama ang mga test) ay nananatiling byte-identical na neutral.
    squeeze_label: str = "neutral"           # "suppression" | "neutral" | "squeeze"
    squeeze_reason: str = "no_daily_rows"
    squeeze_hold_ratio: float | None = None
    squeeze_window_movers_50: int = 0
    squeeze_window_movers_100: int = 0

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


# PER-MINUTE MEMO (2026-08-17, Window-2 py-spy autopsy): ang regime na ito ay
# MARKET-WIDE at tinatawag mula sa 3+ site (auto_arm wildcard, live_runner,
# risk_policy) — ang dalawang trailing-history query nito ay full-scan sa
# milyon-milyong momentum_viability_history rows, at ito ang #1 na dahilan
# kaya ang every-10s arm pass ay tumatagal ng 8+ MINUTO. Ang mga baseline ay
# per-date/per-hour lang magbago, kaya ang per-MINUTO na memo ay semantically
# lossless. Ang susi ay ang IPINASANG clock (hindi wall) para sa replay:
# sa sim time, bawat sim-minuto ay sariwang compute pa rin.
_REGIME_MEMO: dict[str, Any] = {"key": None, "value": None}
# SINGLE-FLIGHT sa recompute (2026-08-20 bridge starvation): sa ilalim ng market-
# hours load ang uncached compute ay umabot ng 100-180s — mas mahaba kaysa sa 60s
# TTL — kaya bawat caller pagkatapos mag-expire ay nagsimula ng SARILING scan
# (stampede), at ang ignition→arm bridge ay natigil nang 175-344s sa ilalim ng
# process-wide lock nito habang naghihintay dito ⇒ 6 arms vs 107 kahapon. Iisang
# thread na lang ang nagre-recompute; ang matatalo sa lock ay nagsisilbi ng STALE
# na memo (ang regime ay per-hour ang bilis magbago, kaya semantically lossless)
# o _NEUTRAL kapag wala pang unang value (ang parehong fail-closed na hugis).
_REGIME_RECOMPUTE_LOCK = threading.Lock()


def compute_breadth_regime(
    db: "Session | None",
    *,
    now: datetime | None = None,
) -> BreadthRegime:
    """TTL-memoized (60s monotonic) na harap ng :func:`_compute_breadth_regime_uncached`.

    Ang regime ay market-wide at mabagal magbago (per-hour baselines), kaya
    ang 60s TTL sa kabuuan ng LAHAT ng call site (auto_arm/live_runner/
    risk_policy) ay semantically lossless sa live. BYPASS ang memo kapag
    GOLDEN=1 (replay — pinapanatili ang A/A byte-identical determinism ng
    #970) o CHILI_PYTEST (tests — walang cross-test leak); pareho silang
    eksaktong pre-memo behavior."""
    if db is None:
        return _NEUTRAL
    if not bool(getattr(settings, "chili_momentum_wildcard_breadth_regime_enabled", True)):
        return _NEUTRAL
    import os as _os
    import time as _time

    if _os.environ.get("GOLDEN") == "1" or _os.environ.get("CHILI_PYTEST"):
        return _attach_squeeze_axis(db, _compute_breadth_regime_uncached(db, now=now))
    _t = _time.monotonic()
    _at = _REGIME_MEMO.get("computed_at_monotonic")
    if (
        _REGIME_MEMO.get("value") is not None
        and _at is not None
        and (_t - float(_at)) < 60.0
    ):
        return _REGIME_MEMO["value"]
    # Isang thread lang ang nagre-recompute; ang iba ay nagsisilbi ng stale (o
    # _NEUTRAL bago ang unang compute) sa halip na dumagdag sa stampede — tingnan
    # ang komento sa _REGIME_RECOMPUTE_LOCK.
    if not _REGIME_RECOMPUTE_LOCK.acquire(blocking=False):
        _stale = _REGIME_MEMO.get("value")
        return _stale if _stale is not None else _NEUTRAL
    try:
        result = _attach_squeeze_axis(db, _compute_breadth_regime_uncached(db, now=now))
        _REGIME_MEMO["computed_at_monotonic"] = _time.monotonic()
        _REGIME_MEMO["value"] = result
        return result
    finally:
        _REGIME_RECOMPUTE_LOCK.release()


def _attach_squeeze_axis(db: "Session | None", base: BreadthRegime) -> BreadthRegime:
    """S6: ilakip ang suppression/squeeze axis sa breadth regime context. Ang read
    ay LIMIT-25 na PK probe ng 1-row-per-day na ``momentum_squeeze_regime_daily``
    (may sariling memo) — ligtas sa ilalim ng recompute lock (HINDI ito ang klase
    ng scan na nagdulot ng 08-20 bridge starvation). FAIL-OPEN: anumang error =>
    ``base`` nang walang pagbabago (neutral ang axis defaults)."""
    try:
        from dataclasses import replace as _dc_replace

        from .squeeze_regime import read_squeeze_regime

        sq = read_squeeze_regime(db)
        return _dc_replace(
            base,
            squeeze_label=sq.label,
            squeeze_reason=sq.reason,
            squeeze_hold_ratio=sq.window_hold_ratio,
            squeeze_window_movers_50=sq.window_movers_50,
            squeeze_window_movers_100=sq.window_movers_100,
        )
    except Exception:
        logger.debug("[breadth_regime] squeeze-axis attach failed (fail-open)", exc_info=True)
        return base


def _compute_breadth_regime_uncached(
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
        # 2026-08-17 perf: range predicates sa observed_at (index-usable) sa
        # halip na ::date cast na nagpi-pilit ng full scan sa milyon-milyong
        # history rows. 2026-08-20: 45d -> 30d — ang table ay 43 araw / 11 GB
        # ang laman kaya ang 45-araw na sahig ay nagbabasa ng HALOS LAHAT ng
        # pahina; ang 30 araw ay sagana pa rin para sa _TRAILING_SESSIONS (20)
        # na sesyon kahit may holidays, at direktang pinuputol ang scan na
        # nagpapatagal sa compute (100-180s) sa ilalim ng bridge lock.
        _today_start = datetime(now_utc.year, now_utc.month, now_utc.day)
        _hist_floor = _today_start - timedelta(days=30)
        hist = db.execute(
            text(
                "SELECT observed_at::date AS d, COUNT(DISTINCT symbol) AS n "
                "FROM momentum_viability_history "
                "WHERE live_eligible = true AND symbol NOT LIKE '%-USD%' "
                "AND observed_at >= :hist_floor AND observed_at < :today_start "
                "AND EXTRACT(HOUR FROM observed_at) BETWEEN :h_lo AND :h_hi "
                "GROUP BY observed_at::date ORDER BY d DESC LIMIT :lim"
            ),
            {
                "hist_floor": _hist_floor,
                "today_start": _today_start,
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
                "AND observed_at >= :hist_floor AND observed_at < :today_start "
                "AND viability_score IS NOT NULL "
                "AND EXTRACT(HOUR FROM observed_at) BETWEEN :h_lo AND :h_hi "
                "GROUP BY observed_at::date ORDER BY d DESC LIMIT :lim"
            ),
            {
                "hist_floor": _hist_floor,
                "today_start": _today_start,
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
