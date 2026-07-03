"""Shared market-profile helpers for Autopilot symbols.

Trading hours follow Ross Cameron's reality: the biggest low-float moves are the
pre-market gap-and-go (he streams 7:00am ET), so the equity momentum lane is tradeable
across the EXTENDED session, not just RTH. The regular session is a fixed US-exchange
fact; the pre-market start and after-hours end are the only tunable bounds (config).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
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
    # TIER-1 ADAPTIVE EARLY-PREMARKET (chili_momentum_early_premarket_enabled, default ON):
    # when the tape shows a genuine pre-premarket-start mover, the premarket session opens
    # from the FIRST-MOVER time (floored at 04:00 ET) instead of the fixed premarket_start
    # clock — so a 04:23 igniter (FCUV) is tradeable the moment the window unlocks, not at
    # 07:00. Flag OFF / no qualifying tape => `pre` is unchanged (byte-identical 07:00 clock).
    if _early_premarket_enabled() and mod < pre and _EXCHANGE_EXT_OPEN_MIN <= mod:
        try:
            _unlocked, _first_mod, _ = early_premarket_unlocked(now=ref)
            if _unlocked and _first_mod is not None:
                pre = min(pre, int(_first_mod))
        except Exception:
            pass
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
      ``hot``        04:00–10:30 ET  (premarket + open drive)
      ``midday``     10:30–14:30 ET  (lull — wide lane off, board half-risk)
      ``late``       14:30–16:00 ET  (no NEW entries; exits unaffected)
      ``afterhours`` 16:00–afterhours_end ET  (WAVE-1 FIX-8: explicit window so the
                     sched-mult map fails CLOSED to 0.0 — a 16:00-20:00 ET entry where
                     is_tradeable_now() is True would otherwise fall through to the map
                     default and size FULL; 14d AH = 1W/11L −$72.65)
      ``closed``     otherwise / weekends
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
    if 14 * 60 + 30 <= mod < _REGULAR_CLOSE_MIN:
        return "late"
    # WAVE-1 FIX-8: the extended after-hours session (16:00 ET → the configured
    # afterhours_end, default 20:00) is an EXPLICIT window so the sched-mult map can size
    # it to 0.0 (fail-CLOSED). is_tradeable_now() treats 16:00-20:00 as tradeable, so
    # without this it would map to the fall-through default and size at FULL risk.
    if _REGULAR_CLOSE_MIN <= mod < max(_afterhours_end_min(), _REGULAR_CLOSE_MIN):
        return "afterhours"
    return "closed"


def in_midday_lull(symbol: str | None, *, now: datetime | None = None) -> bool:
    """True iff ``symbol`` is an EQUITY currently inside the documented midday-lull
    window (the SAME 10:30-14:30 ET ``schedule_window_now`` "midday" band already
    used for the 0.5x size cushion — reused here so there is ONE canonical window,
    no second magic bound). Crypto is always False (24/7, no US midday concept; and
    the lane is equity-only live). DST-correct via the shared _NY_TZ clock.

    Used by the live runner to RAISE the entry viability bar during the lull (a
    6%-win cohort in the live data) — an ADMISSION filter that complements, not
    duplicates, the existing midday SIZE halving. (project_profitability_levers)
    """
    if asset_class_for_symbol(symbol) == "crypto":
        return False
    return schedule_window_now(now=now) == "midday"


def crypto_session_active_now(now: datetime | None = None) -> bool:
    """Crypto entry-window clock (2026-06-13 crypto-live plan, A5).

    Crypto trades 24/7 but the 2026-06-13 clock analysis found the lane earned
    0/21 in the 21:00–05:00 UTC dead band (thin overnight books, no follow-
    through) and that bursts + follow-through concentrate in two UTC windows:
      ACTIVE  05:00–10:00 UTC  (EU morning + US pre-open crypto drive)
      ACTIVE  12:00–21:00 UTC  (US session overlap)
    Outside those = QUIET → no NEW crypto entries (exits/management unaffected,
    like the equity ``late`` window). This is a documented schedule policy, not
    a hard exchange fact — bounds are the two windows above. Returns True iff
    the lane should arm NEW crypto entries now.

    Gated by ``chili_crypto_schedule_enabled`` at the call site; this fn always
    answers the clock question.
    """
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    u = ref.astimezone(timezone.utc)
    h = u.hour + u.minute / 60.0
    return (5.0 <= h < 10.0) or (12.0 <= h < 21.0)


def crypto_schedule_enabled() -> bool:
    return bool(getattr(settings, "chili_crypto_schedule_enabled", True))


_EXCHANGE_EXT_OPEN_MIN = 4 * 60   # 04:00 ET — US extended session opens (exchange fact)


def _early_premarket_enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_early_premarket_enabled", True))


def _data_session_open_min() -> int:
    """Minute-of-day the DATA/selection window opens — DERIVED, never a fixed
    clock (operator 2026-06-11, twice: selection must be WARM before the entry
    window opens, whatever that window is). data open = entry start − prep
    lead, and never LATER than the exchange's own 04:00 ET extended open (a
    07:00 entry config keeps the historical 04:00 data start; a 04:00 entry
    config pulls data sampling to 03:00 so the first allowed entry meets a
    warm tape, not a cold one).

    TIER-1 PULL (chili_momentum_early_premarket_enabled, default ON): the early-
    premarket adaptive unlock reads the tape between the exchange 04:00 ET open and
    premarket_start to find the FIRST mover, so the sampler must be warm by 04:00
    regardless of a late premarket_start config. When early-premarket is enabled the
    open always reaches the 04:00 floor (min of the lead-derived open and 04:00); flag
    OFF = the historical lead-derived open (byte-identical)."""
    try:
        lead = int(getattr(settings, "chili_momentum_selection_prep_lead_min", 60) or 60)
    except (TypeError, ValueError):
        lead = 60
    _lead_open = _premarket_start_min() - max(0, lead)
    if _early_premarket_enabled():
        # Guarantee the 04:00-06:00 sampling that drives the adaptive unlock without
        # touching the 60-min prep-lead default for the entry side: the data open is the
        # EARLIER of the lead-derived open and the exchange 04:00 ET extended open.
        return max(0, min(_EXCHANGE_EXT_OPEN_MIN, _lead_open))
    # Flag OFF = byte-identical to the historical lead-derived open (premarket_start -
    # prep_lead), with NO 04:00 floor — so disabling the feature fully restores prior
    # sampling behavior (the parity contract in this function's docstring).
    return max(0, _lead_open)


def early_premarket_unlocked(now: datetime | None = None) -> tuple[bool, int | None, dict]:
    """TIER-1 adaptive early-premarket unlock (chili_momentum_early_premarket_enabled,
    default ON). Returns (unlocked, first_mover_minute_of_day_ET | None, detail).

    Derive the entry window from WHEN names actually move, not the fixed premarket_start
    clock: read the NBBO tape (the rows the sampler already writes from 04:00 ET) for
    >= N (base 3) DISTINCT spread-clean symbols whose move >= the existing 5% floor within
    the last M (base 5) minutes AND row freshness < 30s. When unlocked, the entry window
    opens from the EARLIEST qualifying tape observed_at (the first-mover time), floored at
    the 04:00 ET exchange extended-open (never earlier).

    The >=3-distinct-symbol + <30s-freshness + spread-clean (_ross_row already drops
    crossed/locked/>5000bps rows) gates mitigate a false unlock on stale/garbage tape.

    Pure + best-effort: any failure / flag-off returns (False, None, {...}) so
    market_session_now falls back to the fixed premarket_start clock (byte-identical)."""
    if not _early_premarket_enabled():
        return False, None, {"reason": "flag_off"}
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    local = ref.astimezone(_NY_TZ)
    if local.weekday() >= 5:
        return False, None, {"reason": "weekend"}
    mod = local.hour * 60 + local.minute
    # Only meaningful in the pre-entry-window band: at/after the exchange 04:00 open and
    # BEFORE the configured premarket_start (the window we want to unlock EARLY into).
    if mod < _EXCHANGE_EXT_OPEN_MIN or mod >= _premarket_start_min():
        return False, None, {"reason": "outside_early_band"}
    try:
        min_movers = int(getattr(settings, "chili_momentum_early_premarket_min_movers", 3) or 3)
        window_min = int(getattr(settings, "chili_momentum_early_premarket_window_min", 5) or 5)
    except (TypeError, ValueError):
        min_movers, window_min = 3, 5
    try:
        from .nbbo_tape import early_premarket_first_mover

        _now_utc = ref.astimezone(timezone.utc)
        try:
            first_at_utc, n_movers, lead_sym, lead_pct = early_premarket_first_mover(
                now_utc=_now_utc,
                window_min=float(window_min),
                min_move_pct=float(_MIN_ABS_CHANGE_PCT),
                freshness_sec=30.0,
                # GAP axis (STEP-C #13): a name gapped >= the same mover floor vs prior close
                # qualifies even with a flat trailing tape (the overnight gapper).
                gap_floor_pct=float(_MIN_ABS_CHANGE_PCT),
            )
        except TypeError:
            # Back-compat: a caller/test that stubbed early_premarket_first_mover without the
            # new gap_floor_pct kwarg — fall back to the velocity-only signature.
            first_at_utc, n_movers, lead_sym, lead_pct = early_premarket_first_mover(
                now_utc=_now_utc,
                window_min=float(window_min),
                min_move_pct=float(_MIN_ABS_CHANGE_PCT),
                freshness_sec=30.0,
            )
    except Exception:
        return False, None, {"reason": "tape_read_error"}
    # ADAPTIVE N (STEP-C #13): the configured min-movers is a CEILING, not a floor. Clamp it
    # to what the sampler is actually surfacing (p75 of concurrent distinct movers) so a 3-bar
    # isn't structurally dead on a day the tape only ever carried 2. Coverage unavailable (or
    # the reader stubbed out) => keep the configured value (fail toward the strict bar).
    effective_min_movers = min_movers
    _coverage_p75: int | None = None
    try:
        from .nbbo_tape import early_premarket_mover_coverage_p75

        _coverage_p75 = early_premarket_mover_coverage_p75(now_utc=_now_utc)
        if _coverage_p75 is not None:
            effective_min_movers = min(min_movers, max(1, int(_coverage_p75)))
    except Exception:
        pass
    if first_at_utc is None or n_movers < effective_min_movers:
        return False, None, {
            "reason": "insufficient_movers",
            "n_movers": n_movers,
            "min_movers": effective_min_movers,
            "configured_min_movers": min_movers,
        }
    first_local = first_at_utc.astimezone(_NY_TZ)
    first_mod = max(_EXCHANGE_EXT_OPEN_MIN, first_local.hour * 60 + first_local.minute)
    return True, first_mod, {
        "n_movers": n_movers,
        "lead_symbol": lead_sym,
        "lead_pct": lead_pct,
        "first_mover_min": first_mod,
        "min_movers": effective_min_movers,
        "configured_min_movers": min_movers,
        "coverage_p75": _coverage_p75,
    }


# Reuse the NBBO-tape 5% mover floor (the existing _MIN_ABS_CHANGE_PCT) so the unlock and
# the sampler share ONE definition of "moving" — no second magic move threshold.
_MIN_ABS_CHANGE_PCT = 5.0


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


def is_overnight_now(symbol: str | None = None, *, now: datetime | None = None) -> bool:
    """TIER-2 pure clock predicate: True for EQUITIES inside the RH 24-hour OVERNIGHT band
    — the weekday hours OUTSIDE premarket/regular/afterhours, i.e. 20:00 ET → 04:00 ET (the
    next morning's exchange extended-open). RH's 24h equities run Sun 20:00 ET → Fri 20:00 ET
    (5 overnight sessions); the weekend (Fri 20:00 → Mon 04:00, modulo the Sun-evening open)
    stays CLOSED, so this returns False then.

    Crypto is 24/7 and has no overnight concept here -> False. This is ONLY the clock fact;
    the tradeability tier (flag + 24h-eligibility + 24h-liquid + safety) gates the actual
    trade in ``is_tradeable_now``. The only hard clocks are exchange facts already named once
    (_EXCHANGE_EXT_OPEN_MIN, _AFTERHOURS via _afterhours_end_min)."""
    if asset_class_for_symbol(symbol) == "crypto":
        return False
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    local = ref.astimezone(_NY_TZ)
    wd = local.weekday()  # Mon=0 .. Sun=6
    mod = local.hour * 60 + local.minute
    post = max(_afterhours_end_min(), _REGULAR_CLOSE_MIN)  # 20:00 ET afterhours end
    # The RH overnight session is the EVENING band (post -> 24:00) on Sun..Thu, plus the
    # EARLY-MORNING band (00:00 -> 04:00) on Mon..Fri. Friday evening and all of Saturday
    # are closed; Sunday evening (>= post) opens the week.
    evening = mod >= post and wd in (6, 0, 1, 2, 3)   # Sun, Mon, Tue, Wed, Thu evenings
    morning = mod < _EXCHANGE_EXT_OPEN_MIN and wd in (0, 1, 2, 3, 4)  # Mon..Fri early AM
    return bool(evening or morning)


def _overnight_trading_enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_overnight_trading_enabled", False))


def _overnight_tape_enabled() -> bool:
    return bool(getattr(settings, "chili_momentum_overnight_tape_enabled", False))


def is_sample_session_now(symbol: str | None, *, now: datetime | None = None) -> bool:
    """TIER-2 sampler gate: the lane should SAMPLE the NBBO tape now. This is
    ``is_data_session_now`` OR (overnight-tape flag ON AND ``is_overnight_now``). The
    24h-eligible WHITELIST restriction + the 60s cadence cap are enforced at the sampler
    call site (nbbo_tape) — this is just the clock OR. Both Tier-2 flags OFF => exactly
    ``is_data_session_now`` (byte-identical)."""
    if is_data_session_now(symbol, now=now):
        return True
    if _overnight_tape_enabled() and is_overnight_now(symbol, now=now):
        return True
    return False


def _next_regular_open_utc(local: datetime, *, allow_extended_hours: bool = False) -> datetime:
    """The next weekday session open (regular 09:30 ET, or premarket start when
    ``allow_extended_hours``) at/after ``local`` (an America/New_York datetime), as UTC."""
    open_min = min(_premarket_start_min(), _REGULAR_OPEN_MIN) if allow_extended_hours else _REGULAR_OPEN_MIN
    days_ahead = 0
    while True:
        candidate_date = local.date() + timedelta(days=days_ahead)
        candidate_local = datetime(
            candidate_date.year,
            candidate_date.month,
            candidate_date.day,
            tzinfo=_NY_TZ,
        ) + timedelta(minutes=open_min)
        if candidate_local.weekday() < 5 and candidate_local > local:
            return candidate_local.astimezone(timezone.utc)
        days_ahead += 1


def market_session_for_symbol(
    symbol: str | None,
    *,
    now: datetime | None = None,
    allow_extended_hours: bool = False,
) -> dict[str, object]:
    """Tradability descriptor for callers that explicitly choose extended hours.

    Returns ``{asset_class, market_session, is_tradable, deferred_until_utc}``. Equities
    are tradable in the regular session always, and in premarket/afterhours only when
    ``allow_extended_hours`` — matching the lane's extended-session reality. Crypto is
    always tradable. ``deferred_until_utc`` is the next session open when not tradable."""
    if asset_class_for_symbol(symbol) == "crypto":
        return {
            "asset_class": "crypto",
            "market_session": "crypto_24_7",
            "is_tradable": True,
            "deferred_until_utc": None,
        }

    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    local = ref.astimezone(_NY_TZ)
    session = market_session_now(symbol, now=ref)
    session_name = {
        "premarket": "pre_market",
        "regular": "regular_hours",
        "afterhours": "post_market",
    }.get(session, "closed_weekend" if local.weekday() >= 5 else "closed_overnight")
    is_tradable = session == "regular" or (
        allow_extended_hours and session in {"premarket", "afterhours"}
    )
    return {
        "asset_class": "stock",
        "market_session": session_name,
        "is_tradable": is_tradable,
        "deferred_until_utc": (
            None
            if is_tradable
            else _next_regular_open_utc(
                local, allow_extended_hours=allow_extended_hours
            ).isoformat()
        ),
    }


def market_open_now(
    symbol: str | None,
    *,
    now: datetime | None = None,
    allow_extended_hours: bool = False,
) -> bool:
    """True during the REGULAR session (9:30–16:00 ET); with ``allow_extended_hours``,
    also True in premarket/afterhours. For the trade/arm/entry decision use
    ``is_tradeable_now`` (always extended-hours aware)."""
    return bool(
        market_session_for_symbol(
            symbol,
            now=now,
            allow_extended_hours=allow_extended_hours,
        )["is_tradable"]
    )


def minutes_since_regular_open(symbol: str | None, *, now: datetime | None = None) -> float | None:
    """Signed minutes since the 09:30 ET regular open for an EQUITY symbol on a
    weekday, else ``None``. Negative before the open (premarket), positive after.

    The opening-bell-suppression edge (HVM101) reads this so the "first ~N minutes
    after the open" window is one DST-correct clock fact (reusing ``_REGULAR_OPEN_MIN``
    + ``_NY_TZ``), never a scattered magic time. Crypto / weekend / non-equity →
    ``None`` so the caller fails OPEN (no suppression)."""
    if asset_class_for_symbol(symbol) == "crypto":
        return None
    ref = now or datetime.now(timezone.utc)
    if ref.tzinfo is None:
        ref = ref.replace(tzinfo=timezone.utc)
    local = ref.astimezone(_NY_TZ)
    if local.weekday() >= 5:
        return None
    mod = local.hour * 60 + local.minute + local.second / 60.0
    return float(mod - _REGULAR_OPEN_MIN)


def is_tradeable_now(symbol: str | None, *, now: datetime | None = None) -> bool:
    """True whenever the symbol can be traded NOW — regular OR extended hours for
    equities (crypto always). This is the gate the auto-arm + live entry use so the
    lane can catch Ross's pre-market runners, not just the RTH leftovers.

    TIER-2 OVERNIGHT (chili_momentum_overnight_trading_enabled, DEFAULT OFF): adds an
    overnight branch — True overnight ONLY IF (flag ON) AND (the symbol is 24h-eligible
    per the RH tradability probe) AND (it passes the 24h-liquid floor). Equity arming
    (auto_arm) and entry (live_runner) inherit this automatically with no new call sites.
    Flag OFF => only premarket|regular|afterhours (byte-identical to today)."""
    if market_session_now(symbol, now=now) in ("premarket", "regular", "afterhours"):
        return True
    if (
        _overnight_trading_enabled()
        and asset_class_for_symbol(symbol) == "stock"
        and is_overnight_now(symbol, now=now)
    ):
        # 24h-eligibility + 24h-liquid floor live in auto_arm (the proactive RH tradability
        # probe + dollar-volume floor); both are checked there BEFORE arming. Keep this
        # predicate the CLOCK+FLAG gate so the auto-arm path stays the single owner of the
        # per-symbol eligibility/liquidity checks (no duplicate MCP call here). Fail-closed:
        # if auto_arm's _is_24h_eligible is unavailable, the auto-arm gate skips the name.
        try:
            from .auto_arm import _is_24h_eligible

            return bool(_is_24h_eligible(symbol))
        except Exception:
            return False
    return False
