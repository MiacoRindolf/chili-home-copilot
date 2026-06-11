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


_DATA_SESSION_OPEN_MIN = 4 * 60   # 04:00 ET — US extended session opens (exchange fact)


def is_data_session_now(symbol: str | None, *, now: datetime | None = None) -> bool:
    """True whenever US equity QUOTES are live (Mon-Fri 04:00-20:00 ET) — the
    DATA/selection window, deliberately WIDER than the lane's entry window
    (premarket_start, default 07:00). The movers Ross trades at 7:00 develop
    from 4:00; sampling/selection from 4:00 means the watchlist, tape, and
    viability are WARM before the first entry is allowed — preparation time,
    not extra trading time. Crypto: always True (24/7)."""
    if asset_class_for_symbol(symbol) == "crypto":
        return True
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    local = ref.astimezone(_NY_TZ)
    if local.weekday() >= 5:
        return False
    mod = local.hour * 60 + local.minute
    return _DATA_SESSION_OPEN_MIN <= mod < max(_afterhours_end_min(), _REGULAR_CLOSE_MIN)


def market_open_now(symbol: str | None, *, now: datetime | None = None) -> bool:
    """True only during the REGULAR session (9:30–16:00 ET) — used for display/labels.
    For the trade/arm/entry decision use ``is_tradeable_now`` (extended-hours aware)."""
    return market_session_now(symbol, now=now) == "regular"


def is_tradeable_now(symbol: str | None, *, now: datetime | None = None) -> bool:
    """True whenever the symbol can be traded NOW — regular OR extended hours for
    equities (crypto always). This is the gate the auto-arm + live entry use so the
    lane can catch Ross's pre-market runners, not just the RTH leftovers."""
    return market_session_now(symbol, now=now) in ("premarket", "regular", "afterhours")
