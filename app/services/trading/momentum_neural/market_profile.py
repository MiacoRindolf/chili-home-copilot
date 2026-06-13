"""Shared market-profile helpers for Autopilot symbols.

Trading hours follow Ross Cameron's reality: the biggest low-float moves are the
pre-market gap-and-go (he streams 7:00am ET), so the equity momentum lane is tradeable
across the EXTENDED session, not just RTH. The regular session is a fixed US-exchange
fact; the pre-market start and after-hours end are the only tunable bounds (config).
"""

from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from ....config import settings


_NY_TZ = ZoneInfo("America/New_York")

# US regular session — a fixed exchange fact (NOT a tunable knob), named once here.
_REGULAR_OPEN_MIN = 9 * 60 + 30   # 09:30 ET
_REGULAR_CLOSE_MIN = 16 * 60      # 16:00 ET
# Standard pre-market open / after-hours close — used only as safe fallbacks if the
# configured strings are malformed. The live values come from settings (see config.py).
_DEFAULT_PREMARKET_MIN = 7 * 60   # 07:00 ET (Ross-time)
_DEFAULT_AFTERHOURS_MIN = 20 * 60  # 20:00 ET


def asset_class_for_symbol(symbol: str | None) -> str:
    sym = (symbol or "").strip().upper()
    return "crypto" if sym.endswith("-USD") else "stock"


def is_coinbase_spot_symbol(symbol: str | None) -> bool:
    sym = (symbol or "").strip().upper()
    if not sym.endswith("-USD"):
        return False
    base = sym[:-4]
    return bool(base) and base.isalnum()


def _parse_hhmm(value: str | None, fallback_min: int) -> int:
    """Parse an "HH:MM" ET string into minutes-of-day; fail-safe to the fallback."""
    try:
        hh, mm = str(value).strip().split(":", 1)
        m = int(hh) * 60 + int(mm)
        return m if 0 <= m <= 24 * 60 else fallback_min
    except Exception:
        return fallback_min


def _premarket_start_min() -> int:
    return _parse_hhmm(getattr(settings, "chili_momentum_premarket_start_et", None), _DEFAULT_PREMARKET_MIN)


def _afterhours_end_min() -> int:
    return _parse_hhmm(getattr(settings, "chili_momentum_afterhours_end_et", None), _DEFAULT_AFTERHOURS_MIN)


def market_session_now(symbol: str | None, *, now: datetime | None = None) -> str:
    """Return the current session for ``symbol``: one of
    ``premarket`` | ``regular`` | ``afterhours`` | ``closed``.

    Crypto is 24/7 → always ``regular``. Equity bounds: premarket_start (config) →
    9:30 = premarket, 9:30 → 16:00 = regular, 16:00 → afterhours_end (config) =
    afterhours, otherwise closed. Setting premarket_start to "09:30" collapses
    premarket; afterhours_end to "16:00" collapses afterhours.
    """
    if asset_class_for_symbol(symbol) == "crypto":
        return "regular"
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    local = ref.astimezone(_NY_TZ)
    if local.weekday() >= 5:
        return "closed"
    mod = local.hour * 60 + local.minute
    pre = min(_premarket_start_min(), _REGULAR_OPEN_MIN)
    post = max(_afterhours_end_min(), _REGULAR_CLOSE_MIN)
    if pre <= mod < _REGULAR_OPEN_MIN:
        return "premarket"
    if _REGULAR_OPEN_MIN <= mod < _REGULAR_CLOSE_MIN:
        return "regular"
    if _REGULAR_CLOSE_MIN <= mod < post:
        return "afterhours"
    return "closed"


def schedule_window_now(now: datetime | None = None) -> str:
    """Intraday schedule window for the equity lane (2026-06-12 quant pass v2:
    the day's edge is heavily clock-shaped — premarket+open carries the tails,
    midday bleeds, late entries lose). Windows:
      ``hot``    04:00–10:30 ET  (premarket + open drive)
      ``midday`` 10:30–14:30 ET  (lull — wide lane off, board half-risk)
      ``late``   14:30–16:00 ET  (no NEW entries; exits unaffected)
      ``closed`` otherwise / weekends
    Pure clock policy — the tape-derived regime dials measured WORSE (rejected
    list, pass v2)."""
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    local = ref.astimezone(_NY_TZ)
    if local.weekday() >= 5:
        return "closed"
    mod = local.hour * 60 + local.minute
    if 4 * 60 <= mod < 10 * 60 + 30:
        return "hot"
    if 10 * 60 + 30 <= mod < 14 * 60 + 30:
        return "midday"
    if 14 * 60 + 30 <= mod < 16 * 60:
        return "late"
    return "closed"


_EXCHANGE_EXT_OPEN_MIN = 4 * 60   # 04:00 ET — US extended session opens (exchange fact)


def _data_session_open_min() -> int:
    """Minute-of-day the DATA/selection window opens — DERIVED, never a fixed
    clock (operator 2026-06-11, twice: selection must be WARM before the entry
    window opens, whatever that window is). data open = entry start − prep
    lead, and never LATER than the exchange's own 04:00 ET extended open (a
    07:00 entry config keeps the historical 04:00 data start; a 04:00 entry
    config pulls data sampling to 03:00 so the first allowed entry meets a
    warm tape, not a cold one)."""
    try:
        lead = int(getattr(settings, "chili_momentum_selection_prep_lead_min", 60) or 60)
    except (TypeError, ValueError):
        lead = 60
    return max(0, min(_EXCHANGE_EXT_OPEN_MIN, _premarket_start_min() - max(0, lead)))


def is_data_session_now(symbol: str | None, *, now: datetime | None = None) -> bool:
    """True whenever the lane should be SAMPLING/SELECTING (Mon-Fri, derived
    open → 20:00 ET) — the DATA window, deliberately WIDER than the entry
    window: the movers traded at window-open develop BEFORE it, so the
    watchlist, tape, and viability must already be WARM when the first entry
    is allowed — preparation time, not extra trading time. The open is derived
    from the entry window (``_data_session_open_min``). Crypto: always True (24/7)."""
    if asset_class_for_symbol(symbol) == "crypto":
        return True
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    local = ref.astimezone(_NY_TZ)
    if local.weekday() >= 5:
        return False
    mod = local.hour * 60 + local.minute
    return _data_session_open_min() <= mod < max(_afterhours_end_min(), _REGULAR_CLOSE_MIN)


def market_open_now(symbol: str | None, *, now: datetime | None = None) -> bool:
    """True only during the REGULAR session (9:30–16:00 ET) — used for display/labels.
    For the trade/arm/entry decision use ``is_tradeable_now`` (extended-hours aware)."""
    return market_session_now(symbol, now=now) == "regular"


def is_tradeable_now(symbol: str | None, *, now: datetime | None = None) -> bool:
    """True whenever the symbol can be traded NOW — regular OR extended hours for
    equities (crypto always). This is the gate the auto-arm + live entry use so the
    lane can catch Ross's pre-market runners, not just the RTH leftovers."""
    return market_session_now(symbol, now=now) in ("premarket", "regular", "afterhours")
