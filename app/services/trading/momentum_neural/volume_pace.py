"""Time-normalized RVOL / volume-pace helpers for the momentum lane."""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

_EASTERN = ZoneInfo("America/New_York")

_PREMARKET_OPEN_MIN = 4 * 60
_RTH_OPEN_MIN = 9 * 60 + 30
_RTH_CLOSE_MIN = 16 * 60
_AFTER_HOURS_CLOSE_MIN = 20 * 60

BAD_CUMULATIVE_RVOL_BASES = frozenset(
    {
        "cumulative_day_over_prev_day",
        "cumulative_day_volume_over_full_day_adv",
        "cumulative_day_over_full_day_adv",
        "today_cumulative_over_prev_day",
        "today_volume_over_adv",
        "day_volume_over_adv",
    }
)

TRUSTED_RVOL_BASES = frozenset(
    {
        "actual_cum_over_expected_cum",
        "time_normalized_cumulative",
        "time_normalized_volume_pace",
        "per_symbol_intraday_curve",
        "market_session_curve",
        "regular_market_session_curve",
        "expected_cum_vol",
    }
)

TRUSTED_RVOL_SOURCES = frozenset(
    {
        "rvol_pace",
        "per_symbol_intraday_curve",
        "market_session_curve",
        "regular_market_session_curve",
        "expected_cum_vol",
    }
)


@dataclass(frozen=True)
class VolumePace:
    """Audit-friendly result for cumulative volume pace."""

    rvol_source: str
    rvol_pace: float | None
    expected_cum_vol: float | None
    actual_cum_vol: float | None
    session_elapsed_fraction: float | None
    session_bucket: str
    fallback_reason: str | None = None
    rvol_basis: str = "actual_cum_over_expected_cum"

    def to_telemetry(self) -> dict[str, Any]:
        return asdict(self)


def _finite_float(v: Any) -> float | None:
    try:
        if v is None or v == "":
            return None
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _nonnegative_float(v: Any) -> float | None:
    f = _finite_float(v)
    if f is None or f < 0:
        return None
    return f


def _positive_float(v: Any) -> float | None:
    f = _finite_float(v)
    if f is None or f <= 0:
        return None
    return f


def _norm_text(v: Any) -> str:
    return str(v or "").strip().lower()


def is_cumulative_day_adv_basis(*values: Any) -> bool:
    """True when metadata says the value is raw day-volume / full-day ADV."""

    for value in values:
        t = _norm_text(value)
        if not t:
            continue
        if t in BAD_CUMULATIVE_RVOL_BASES:
            return True
        if "cumulative" in t and ("adv" in t or "prev_day" in t or "full_day" in t):
            if "expected" not in t and "time_normal" not in t:
                return True
    return False


def is_time_normalized_rvol_basis(*values: Any) -> bool:
    """True when metadata says the value is actual cumulative / expected cumulative."""

    for value in values:
        t = _norm_text(value)
        if not t:
            continue
        if t in TRUSTED_RVOL_BASES or t in TRUSTED_RVOL_SOURCES:
            return True
        if "expected_cum" in t or "time_normal" in t or "volume_pace" in t:
            return True
        if "session_curve" in t or "intraday_curve" in t:
            return True
    return False


def trusted_rvol_value(
    value: Any,
    *,
    basis: Any = None,
    source: Any = None,
    fallback_legacy: bool = True,
) -> float | None:
    """Return a trusted RVOL value, rejecting raw cumulative day/ADV semantics."""

    f = _positive_float(value)
    if f is None:
        return None
    if _norm_text(source) == "rvol_incomplete":
        return None
    if is_cumulative_day_adv_basis(basis, source):
        return None
    if is_time_normalized_rvol_basis(basis, source):
        return f
    return f if fallback_legacy else None


def equity_session_bucket_and_fraction(now: datetime | None = None) -> tuple[str, float | None]:
    """Return equity session bucket plus elapsed fraction within that bucket."""

    if now is None:
        now = datetime.now(tz=_EASTERN)
    if now.tzinfo is None:
        now_et = now.replace(tzinfo=_EASTERN)
    else:
        now_et = now.astimezone(_EASTERN)
    if now_et.weekday() >= 5:
        return "closed", None

    minute = now_et.hour * 60 + now_et.minute + now_et.second / 60.0
    if _PREMARKET_OPEN_MIN <= minute < _RTH_OPEN_MIN:
        frac = (minute - _PREMARKET_OPEN_MIN) / (_RTH_OPEN_MIN - _PREMARKET_OPEN_MIN)
        return "premarket", max(0.0, min(1.0, frac))
    if _RTH_OPEN_MIN <= minute <= _RTH_CLOSE_MIN:
        frac = (minute - _RTH_OPEN_MIN) / (_RTH_CLOSE_MIN - _RTH_OPEN_MIN)
        return "regular", max(0.0, min(1.0, frac))
    if _RTH_CLOSE_MIN < minute <= _AFTER_HOURS_CLOSE_MIN:
        frac = (minute - _RTH_CLOSE_MIN) / (_AFTER_HOURS_CLOSE_MIN - _RTH_CLOSE_MIN)
        return "after_hours", max(0.0, min(1.0, frac))
    return "closed", None


def equity_rth_market_curve_fraction(elapsed_fraction: float) -> float:
    """Generic RTH cumulative-volume curve, used only as an honest fallback."""

    e = max(0.0, min(1.0, float(elapsed_fraction)))
    knots = (
        (0.00, 0.00),
        (0.05, 0.12),
        (0.10, 0.20),
        (0.25, 0.34),
        (0.50, 0.55),
        (0.75, 0.74),
        (0.90, 0.86),
        (1.00, 1.00),
    )
    prev_x, prev_y = knots[0]
    for x, y in knots[1:]:
        if e <= x:
            if x <= prev_x:
                return y
            t = (e - prev_x) / (x - prev_x)
            return prev_y + t * (y - prev_y)
        prev_x, prev_y = x, y
    return 1.0


def compute_volume_pace(
    *,
    actual_cum_vol: Any,
    full_session_baseline_vol: Any = None,
    now: datetime | None = None,
    session_bucket: str | None = None,
    session_elapsed_fraction: Any = None,
    expected_cum_vol: Any = None,
    curve_fraction: Any = None,
    curve_source: str | None = None,
) -> VolumePace:
    """Compute time-normalized volume pace.

    Pace is actual cumulative volume so far divided by expected cumulative volume
    at this session time. When no per-symbol curve is available during regular
    hours, this uses a generic market-session curve and marks that fallback.
    Premarket/after-hours never borrow the regular full-day curve blindly.
    """

    bucket, clock_fraction = equity_session_bucket_and_fraction(now)
    if session_bucket:
        bucket = _norm_text(session_bucket)
    elapsed = _finite_float(session_elapsed_fraction)
    if elapsed is None:
        elapsed = clock_fraction
    if elapsed is not None:
        elapsed = max(0.0, min(1.0, elapsed))

    actual = _nonnegative_float(actual_cum_vol)
    if actual is None:
        return VolumePace(
            rvol_source="rvol_incomplete",
            rvol_pace=None,
            expected_cum_vol=None,
            actual_cum_vol=None,
            session_elapsed_fraction=elapsed,
            session_bucket=bucket,
            fallback_reason="missing_actual_cum_vol",
            rvol_basis="rvol_incomplete",
        )

    expected = _positive_float(expected_cum_vol)
    if expected is not None:
        pace = actual / expected
        return VolumePace(
            rvol_source=curve_source or "expected_cum_vol",
            rvol_pace=pace,
            expected_cum_vol=expected,
            actual_cum_vol=actual,
            session_elapsed_fraction=elapsed,
            session_bucket=bucket,
        )

    baseline = _positive_float(full_session_baseline_vol)
    if baseline is None:
        return VolumePace(
            rvol_source="rvol_incomplete",
            rvol_pace=None,
            expected_cum_vol=None,
            actual_cum_vol=actual,
            session_elapsed_fraction=elapsed,
            session_bucket=bucket,
            fallback_reason="missing_baseline_volume",
            rvol_basis="rvol_incomplete",
        )

    cf = _positive_float(curve_fraction)
    if cf is not None:
        cf = max(0.0, min(1.0, cf))
        expected = baseline * cf
        pace = actual / expected if expected > 0 else None
        return VolumePace(
            rvol_source=curve_source or "per_symbol_intraday_curve",
            rvol_pace=pace,
            expected_cum_vol=expected,
            actual_cum_vol=actual,
            session_elapsed_fraction=elapsed,
            session_bucket=bucket,
        )

    if bucket == "regular":
        if elapsed is None or elapsed <= 0:
            return VolumePace(
                rvol_source="rvol_incomplete",
                rvol_pace=None,
                expected_cum_vol=None,
                actual_cum_vol=actual,
                session_elapsed_fraction=elapsed,
                session_bucket=bucket,
                fallback_reason="regular_session_clock_unavailable",
                rvol_basis="rvol_incomplete",
            )
        market_curve = equity_rth_market_curve_fraction(elapsed)
        expected = baseline * market_curve
        pace = actual / expected if expected > 0 else None
        return VolumePace(
            rvol_source="market_session_curve",
            rvol_pace=pace,
            expected_cum_vol=expected,
            actual_cum_vol=actual,
            session_elapsed_fraction=elapsed,
            session_bucket=bucket,
            fallback_reason="per_symbol_curve_unavailable",
        )

    return VolumePace(
        rvol_source="rvol_incomplete",
        rvol_pace=None,
        expected_cum_vol=None,
        actual_cum_vol=actual,
        session_elapsed_fraction=elapsed,
        session_bucket=bucket,
        fallback_reason=f"{bucket}_curve_unavailable",
        rvol_basis="rvol_incomplete",
    )


def snapshot_volume_pace(
    *,
    today_shares: Any,
    adv_shares: Any,
    now: datetime | None = None,
    session_bucket: str | None = None,
    session_elapsed_fraction: Any = None,
    expected_cum_vol: Any = None,
    curve_fraction: Any = None,
    curve_source: str | None = None,
) -> dict[str, Any]:
    """Convenience wrapper that returns telemetry-ready pace fields."""

    return compute_volume_pace(
        actual_cum_vol=today_shares,
        full_session_baseline_vol=adv_shares,
        now=now,
        session_bucket=session_bucket,
        session_elapsed_fraction=session_elapsed_fraction,
        expected_cum_vol=expected_cum_vol,
        curve_fraction=curve_fraction,
        curve_source=curve_source,
    ).to_telemetry()
