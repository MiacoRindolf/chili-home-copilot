"""PRINCIPAL-LEVEL edge-case hunt: [timezone-dst] for the Ross momentum lane.

The lane is a thicket of ET clock windows (premarket 04:00, RTH 09:30/16:00, the
04:00-10:30 / 14:30 schedule bands, premarket PR-cadence windows, green-day ET-calendar
bucketing). Every one of these is a landmine across the two US DST transitions:

  * SPRING-FORWARD  2026-03-08: at 02:00 ET clocks jump to 03:00 ET (the 02:xx hour does
    not exist). UTC offset goes EST(-05:00) -> EDT(-04:00).
  * FALL-BACK       2026-11-01: at 02:00 ET clocks fall back to 01:00 ET (the 01:xx hour
    happens twice). UTC offset goes EDT(-04:00) -> EST(-05:00).

These tests build timezone-AWARE datetimes on the REAL 2026 transition dates and assert the
SPECIFIC window / day classification — each is designed to FAIL if the clock math is subtly
wrong (e.g. fixed-offset arithmetic, naive .replace, timedelta(days=1) across a transition).

Pure-logic + settings mocks; NO DB. The DB-backed green-day/prior-day functions are exercised
by feeding a fake `db` whose query returns synthetic (terminal_at, pnl, outcome_class) rows, so
the ONLY thing under test is the ET-calendar bucketing of UTC terminal_at timestamps.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover
    ZoneInfo = None  # type: ignore

# Ensure repo root import (tests/ sibling of app/).
_ROOT = Path(__file__).resolve().parents[1]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from app.services.trading.momentum_neural import catalyst as cat  # noqa: E402
from app.services.trading.momentum_neural import market_profile as mp  # noqa: E402
from app.services.trading.momentum_neural import risk_policy as rp  # noqa: E402

NY = ZoneInfo("America/New_York")
UTC = timezone.utc

# Real 2026 US DST transition instants.
SPRING_FORWARD = datetime(2026, 3, 8)   # 02:00->03:00 ET (Sunday)
FALL_BACK = datetime(2026, 11, 1)       # 02:00->01:00 ET (Sunday)


def _et(y, mo, d, h, mi=0, s=0):
    """Aware ET wall-clock datetime."""
    return datetime(y, mo, d, h, mi, s, tzinfo=NY)


def _utc(y, mo, d, h, mi=0, s=0):
    return datetime(y, mo, d, h, mi, s, tzinfo=UTC)


# ---------------------------------------------------------------------------
# Sanity: the zoneinfo DB on this machine actually models the 2026 transitions.
# If these fail, the whole suite's premise is void (stale tzdata) — fail loud.
# ---------------------------------------------------------------------------

def test_tzdata_models_2026_spring_forward_offset_shift():
    # 06:30 UTC on 2026-03-08 is 01:30 EST (before the jump, -05:00).
    before = _utc(2026, 3, 8, 6, 30).astimezone(NY)
    # 07:30 UTC is 03:30 EDT (after the jump, -04:00) — the 02:xx hour was skipped.
    after = _utc(2026, 3, 8, 7, 30).astimezone(NY)
    assert before.utcoffset() == timedelta(hours=-5)
    assert before.hour == 1 and before.minute == 30
    assert after.utcoffset() == timedelta(hours=-4)
    assert after.hour == 3 and after.minute == 30  # NOT 02:30 — the hour vanished


def test_tzdata_models_2026_fall_back_repeated_hour():
    # 05:30 UTC on 2026-11-01 is 01:30 EDT (first 01:xx, -04:00).
    first = _utc(2026, 11, 1, 5, 30).astimezone(NY)
    # 06:30 UTC is 01:30 EST (second 01:xx, -05:00) — the repeated hour.
    second = _utc(2026, 11, 1, 6, 30).astimezone(NY)
    assert first.hour == 1 and first.utcoffset() == timedelta(hours=-4)
    assert second.hour == 1 and second.utcoffset() == timedelta(hours=-5)
    # Same wall clock (01:30) but genuinely distinct INSTANTS — only visible once projected
    # back to UTC (aware == on a repeated wall-clock is True via fold, so compare UTC).
    assert first.astimezone(UTC) != second.astimezone(UTC)
    assert (second.astimezone(UTC) - first.astimezone(UTC)) == timedelta(hours=1)


# ===========================================================================
# (1) PRIME WINDOW + 14:30 FALLBACK CUTOFF across DST transitions
#     schedule_window_now: hot 04:00-10:30, midday 10:30-14:30, late 14:30-16:00
# ===========================================================================

def test_schedule_window_hot_boundaries_inclusive_exclusive():
    # 04:00:00 ET is the FIRST hot minute (inclusive); 03:59 is closed.
    assert mp.schedule_window_now(now=_et(2026, 6, 15, 4, 0, 0)) == "hot"
    assert mp.schedule_window_now(now=_et(2026, 6, 15, 3, 59, 59)) == "closed"
    # 10:30:00 leaves hot -> midday (start inclusive on midday, end exclusive on hot).
    assert mp.schedule_window_now(now=_et(2026, 6, 15, 10, 29, 59)) == "hot"
    assert mp.schedule_window_now(now=_et(2026, 6, 15, 10, 30, 0)) == "midday"


def test_schedule_window_1430_cutoff_exact_boundary():
    # 14:29 = midday (wide lane), 14:30:00 = late (NO new entries) — the fallback cutoff.
    assert mp.schedule_window_now(now=_et(2026, 6, 15, 14, 29, 59)) == "midday"
    assert mp.schedule_window_now(now=_et(2026, 6, 15, 14, 30, 0)) == "late"
    # 16:00:00 ends 'late' -> closed.
    assert mp.schedule_window_now(now=_et(2026, 6, 15, 15, 59, 59)) == "late"
    assert mp.schedule_window_now(now=_et(2026, 6, 15, 16, 0, 0)) == "closed"


def test_schedule_window_dst_transition_days_are_weekends_closed():
    """KEY REALITY: both US DST transitions ALWAYS fall on a SUNDAY (2026-03-08, 2026-11-01).
    schedule_window_now short-circuits weekday()>=5 to 'closed'. So on the transition INSTANT
    the intraday windows are NEVER exercised — they're always 'closed' that day, regardless of
    the clock. Pin this so a future refactor that drops the weekend guard is caught. (The real
    DST-offset correctness is exercised on the MONDAY AFTER, below.)"""
    # 09:30 ET wall-clock on each transition Sunday -> closed (weekend), not hot/regular.
    assert mp.schedule_window_now(now=_et(2026, 3, 8, 9, 30)) == "closed"
    assert mp.schedule_window_now(now=_et(2026, 11, 1, 9, 30)) == "closed"


def test_schedule_window_monday_after_spring_forward_uses_edt():
    """The first TRADING day after spring-forward is Mon 2026-03-09 (now EDT, -04:00). A UTC
    `now` must convert with the NEW offset. 13:30 UTC Mon = 09:30 EDT = hot. A frozen -5 (EST)
    bug would read 08:30 (still hot) so use the sharp discriminator: 14:31 UTC = 10:31 EDT =
    MIDDAY, whereas a stale EST -5 would read 09:31 = HOT."""
    assert mp.schedule_window_now(now=_utc(2026, 3, 9, 13, 30)) == "hot"
    assert mp.schedule_window_now(now=_utc(2026, 3, 9, 14, 31)) == "midday"
    # The 14:30 'late' cutoff in EDT: 18:30 UTC = 14:30 EDT = late.
    assert mp.schedule_window_now(now=_utc(2026, 3, 9, 18, 30)) == "late"


def test_schedule_window_monday_after_fall_back_uses_est():
    """First trading day after fall-back is Mon 2026-11-02 (now EST, -05:00). 18:31 UTC =
    13:31 EST = midday; a stale -4 (EDT) bug would read 14:31 = LATE. And the 14:30 cutoff in
    EST: 19:30 UTC = 14:30 EST = late."""
    assert mp.schedule_window_now(now=_utc(2026, 11, 2, 18, 31)) == "midday"
    assert mp.schedule_window_now(now=_utc(2026, 11, 2, 19, 30)) == "late"
    # 13:30 UTC = 08:30 EST = hot (within 04:00-10:30 EST).
    assert mp.schedule_window_now(now=_utc(2026, 11, 2, 13, 30)) == "hot"


def test_schedule_window_naive_datetime_assumed_utc():
    # A naive datetime is treated as UTC. 13:30 naive -> 09:30 EDT (summer) = hot.
    assert mp.schedule_window_now(now=datetime(2026, 6, 15, 13, 30)) == "hot"


def test_schedule_window_weekend_closed_even_in_hot_clock():
    # 2026-03-08 is a SUNDAY — weekday()>=5 short-circuits to 'closed' regardless of clock.
    assert mp.schedule_window_now(now=_et(2026, 3, 8, 9, 0)) == "closed"


# ===========================================================================
# (1b) _parse_hhmm + market_session_now HH:MM parse around DST
# ===========================================================================

def test_parse_hhmm_basic_and_failsafe():
    assert mp._parse_hhmm("09:30", 0) == 570
    assert mp._parse_hhmm("04:00", 999) == 240
    assert mp._parse_hhmm("24:00", 7) == 1440          # boundary allowed
    assert mp._parse_hhmm("24:01", 111) == 111         # > 1440 -> fallback
    assert mp._parse_hhmm("garbage", 240) == 240
    assert mp._parse_hhmm(None, 240) == 240
    # NOTE: _parse_hhmm does NOT validate mm<60. "9:90" -> 9*60+90 = 630 <= 1440 so it is
    # ACCEPTED as 10:30, not rejected. Document the actual behavior (potential source bug).
    assert mp._parse_hhmm("9:90", 0) == 630


def test_market_session_now_premarket_regular_afterhours_boundaries():
    # Use explicit config so the bounds are deterministic regardless of env.
    with patch.object(mp.settings, "chili_momentum_premarket_start_et", "07:00"), \
         patch.object(mp.settings, "chili_momentum_afterhours_end_et", "20:00"), \
         patch.object(mp.settings, "chili_momentum_early_premarket_enabled", False):
        # 06:59 closed (before 07:00 premarket), 07:00 premarket.
        assert mp.market_session_now("AAPL", now=_et(2026, 6, 15, 6, 59)) == "closed"
        assert mp.market_session_now("AAPL", now=_et(2026, 6, 15, 7, 0)) == "premarket"
        # 09:29 premarket, 09:30 regular, 15:59 regular, 16:00 afterhours.
        assert mp.market_session_now("AAPL", now=_et(2026, 6, 15, 9, 29)) == "premarket"
        assert mp.market_session_now("AAPL", now=_et(2026, 6, 15, 9, 30)) == "regular"
        assert mp.market_session_now("AAPL", now=_et(2026, 6, 15, 15, 59)) == "regular"
        assert mp.market_session_now("AAPL", now=_et(2026, 6, 15, 16, 0)) == "afterhours"
        # 19:59 afterhours, 20:00 closed.
        assert mp.market_session_now("AAPL", now=_et(2026, 6, 15, 19, 59)) == "afterhours"
        assert mp.market_session_now("AAPL", now=_et(2026, 6, 15, 20, 0)) == "closed"


def test_market_session_monday_after_spring_forward_open_via_utc_uses_edt():
    """The transition day (2026-03-08) is a Sunday -> 'closed' (weekend guard). The first
    trading day is Mon 2026-03-09, now EDT (-04:00). The 09:30 RTH open is 13:30 UTC. A fixed
    -5 (EST) offset bug would put 13:30 UTC at 08:30 ET = premarket, NOT regular."""
    with patch.object(mp.settings, "chili_momentum_premarket_start_et", "07:00"), \
         patch.object(mp.settings, "chili_momentum_afterhours_end_et", "20:00"), \
         patch.object(mp.settings, "chili_momentum_early_premarket_enabled", False):
        # Sunday transition day is always closed regardless of clock.
        assert mp.market_session_now("AAPL", now=_utc(2026, 3, 8, 13, 30)) == "closed"
        # Monday after, EDT: 13:30 UTC = 09:30 EDT = regular.
        assert mp.market_session_now("AAPL", now=_utc(2026, 3, 9, 13, 30)) == "regular"
        # 13:29 UTC = 09:29 EDT = premarket (one minute earlier flips the session).
        assert mp.market_session_now("AAPL", now=_utc(2026, 3, 9, 13, 29)) == "premarket"


def test_market_session_crypto_always_regular_even_on_dst_sunday():
    assert mp.market_session_now("BTC-USD", now=_et(2026, 3, 8, 2, 30)) == "regular"
    assert mp.market_session_now("BTC-USD", now=_et(2026, 11, 1, 1, 30)) == "regular"


# ===========================================================================
# (4) EARLY-PREMARKET ADAPTIVE WINDOW: 03:59 vs 04:00 boundary
#     early_premarket_unlocked: only meaningful in [04:00, premarket_start)
# ===========================================================================

def _fake_first_mover(first_at_utc, n_movers, sym="FCUV", pct=12.3):
    """Return a stand-in for nbbo_tape.early_premarket_first_mover."""
    def _f(*args, **kwargs):
        return first_at_utc, n_movers, sym, pct
    return _f


def test_early_premarket_band_0359_vs_0400_boundary():
    """03:59 ET is BEFORE the exchange 04:00 extended-open floor -> outside_early_band
    (no unlock). 04:00 ET is the first eligible minute."""
    with patch.object(mp.settings, "chili_momentum_premarket_start_et", "07:00"), \
         patch.object(mp.settings, "chili_momentum_early_premarket_enabled", True):
        ok, first, detail = mp.early_premarket_unlocked(now=_et(2026, 6, 15, 3, 59))
        assert ok is False and first is None
        assert detail.get("reason") == "outside_early_band"
        # 04:00 is inside the band; with no tape mover plugged it returns insufficient/err,
        # NOT outside_early_band. Patch the tape so we reach the mover logic at 04:00.
        fake = _fake_first_mover(_utc(2026, 6, 15, 8, 12), 3)  # 04:12 ET first mover
        with patch.dict(
            sys.modules,
            {"app.services.trading.momentum_neural.nbbo_tape": SimpleNamespace(
                early_premarket_first_mover=fake)},
        ):
            ok2, first2, detail2 = mp.early_premarket_unlocked(now=_et(2026, 6, 15, 4, 0))
            assert ok2 is True
            assert first2 == 4 * 60 + 12  # 04:12 ET, floored at 04:00 (12:00 past)


def test_early_premarket_first_mover_floored_at_0400():
    """A first-mover observed_at BEFORE 04:00 (e.g. 03:30 ET) must be FLOORED to 04:00 —
    the window never opens earlier than the exchange extended-open."""
    with patch.object(mp.settings, "chili_momentum_premarket_start_et", "07:00"), \
         patch.object(mp.settings, "chili_momentum_early_premarket_enabled", True):
        # 07:30 UTC = 03:30 EDT (winter would differ; June is EDT). Use a clean summer date.
        fake = _fake_first_mover(_utc(2026, 6, 15, 7, 30), 5)  # 03:30 ET observed
        with patch.dict(
            sys.modules,
            {"app.services.trading.momentum_neural.nbbo_tape": SimpleNamespace(
                early_premarket_first_mover=fake)},
        ):
            ok, first, _ = mp.early_premarket_unlocked(now=_et(2026, 6, 15, 5, 0))
            assert ok is True
            assert first == 4 * 60  # floored to 04:00 even though tape said 03:30


def test_early_premarket_insufficient_movers_no_unlock():
    with patch.object(mp.settings, "chili_momentum_premarket_start_et", "07:00"), \
         patch.object(mp.settings, "chili_momentum_early_premarket_enabled", True), \
         patch.object(mp.settings, "chili_momentum_early_premarket_min_movers", 3):
        fake = _fake_first_mover(_utc(2026, 6, 15, 8, 12), 2)  # only 2 < 3
        with patch.dict(
            sys.modules,
            {"app.services.trading.momentum_neural.nbbo_tape": SimpleNamespace(
                early_premarket_first_mover=fake)},
        ):
            ok, first, detail = mp.early_premarket_unlocked(now=_et(2026, 6, 15, 5, 0))
            assert ok is False and first is None
            assert detail.get("reason") == "insufficient_movers"


def test_early_premarket_unlock_pulls_market_session_to_premarket():
    """End-to-end: at 05:00 ET with premarket_start=07:00, the session is normally 'closed'
    (05:00 < 07:00). With a qualifying early-premarket mover at 04:12, market_session_now
    must pull the premarket open EARLIER and report 'premarket' at 05:00."""
    with patch.object(mp.settings, "chili_momentum_premarket_start_et", "07:00"), \
         patch.object(mp.settings, "chili_momentum_afterhours_end_et", "20:00"), \
         patch.object(mp.settings, "chili_momentum_early_premarket_enabled", True):
        # WITHOUT the mover -> closed.
        fake_none = _fake_first_mover(None, 0)
        with patch.dict(
            sys.modules,
            {"app.services.trading.momentum_neural.nbbo_tape": SimpleNamespace(
                early_premarket_first_mover=fake_none)},
        ):
            assert mp.market_session_now("FCUV", now=_et(2026, 6, 15, 5, 0)) == "closed"
        # WITH a 04:12 mover -> premarket (unlocked early).
        fake = _fake_first_mover(_utc(2026, 6, 15, 8, 12), 4)
        with patch.dict(
            sys.modules,
            {"app.services.trading.momentum_neural.nbbo_tape": SimpleNamespace(
                early_premarket_first_mover=fake)},
        ):
            assert mp.market_session_now("FCUV", now=_et(2026, 6, 15, 5, 0)) == "premarket"


def test_early_premarket_weekend_short_circuit():
    with patch.object(mp.settings, "chili_momentum_premarket_start_et", "07:00"), \
         patch.object(mp.settings, "chili_momentum_early_premarket_enabled", True):
        # 2026-03-08 Sunday at 05:00 -> weekend reason, no tape read.
        ok, first, detail = mp.early_premarket_unlocked(now=_et(2026, 3, 8, 5, 0))
        assert ok is False and first is None and detail.get("reason") == "weekend"


# ===========================================================================
# (2) PR-CADENCE WINDOWS: exact 04:00:00 / 09:30:00 inclusive/exclusive boundaries
# ===========================================================================

def test_parse_cadence_windows_inclusive_start_exclusive_end():
    wins = cat._parse_cadence_windows("04:00-04:45,09:25-09:35")
    assert wins == [(240, 285), (565, 575)]


def test_parse_cadence_windows_malformed_entries_skipped():
    # One good, one reversed (a>=b rejected), one garbage -> only the good window kept.
    wins = cat._parse_cadence_windows("08:00-08:30,09:00-08:00,zzz")
    assert wins == [(480, 510)]


def test_parse_cadence_windows_fully_malformed_falls_back_to_default():
    wins = cat._parse_cadence_windows("totally-bogus")
    # Default expands the whole premarket; first window is 04:00-04:45.
    assert (240, 285) in wins
    assert len(wins) >= 5


def test_pr_cadence_active_0400_inclusive_start():
    with patch.object(cat.settings, "chili_momentum_news_pr_cadence_hours", "04:00-04:45"):
        # 04:00:00 is the inclusive first minute.
        assert cat.pr_cadence_active(now=_et(2026, 6, 15, 4, 0, 0)) is True
        # 03:59:59 just before -> not active.
        assert cat.pr_cadence_active(now=_et(2026, 6, 15, 3, 59, 59)) is False
        # 04:45:00 is the EXCLUSIVE end -> not active.
        assert cat.pr_cadence_active(now=_et(2026, 6, 15, 4, 45, 0)) is False
        assert cat.pr_cadence_active(now=_et(2026, 6, 15, 4, 44, 59)) is True


def test_pr_cadence_active_0930_premarket_cutoff_exclusive():
    """A cadence window that straddles 09:30 must die at the RTH open (premarket-only gate).
    Window 09:25-09:35; 09:29 active, 09:30:00 NOT (cur >= 09:30 short-circuits)."""
    with patch.object(cat.settings, "chili_momentum_news_pr_cadence_hours", "09:25-09:35"):
        assert cat.pr_cadence_active(now=_et(2026, 6, 15, 9, 29, 59)) is True
        assert cat.pr_cadence_active(now=_et(2026, 6, 15, 9, 30, 0)) is False
        assert cat.pr_cadence_active(now=_et(2026, 6, 15, 9, 31, 0)) is False


def test_pr_cadence_naive_assumed_ET_not_UTC():
    """catalyst.pr_cadence_active treats a NAIVE datetime as ET (now.replace(tzinfo=_NY_TZ)),
    UNLIKE market_profile which treats naive as UTC. This asymmetry is a real footgun — pin
    it: naive 08:15 is read as 08:15 ET (inside an 08:00-08:50 window), NOT converted."""
    with patch.object(cat.settings, "chili_momentum_news_pr_cadence_hours", "08:00-08:50"):
        assert cat.pr_cadence_active(now=datetime(2026, 6, 15, 8, 15)) is True
        # If it had assumed UTC, 08:15 UTC = 04:15 ET -> outside 08:00-08:50 -> False.


def test_pr_cadence_aware_utc_converted_to_ET_across_dst():
    """An AWARE UTC datetime is converted to ET. On spring-forward day 12:15 UTC = 08:15 EDT
    -> inside 08:00-08:50. A frozen -5 (EST) bug would read 07:15 -> outside (with this
    window) — discriminates the offset."""
    with patch.object(cat.settings, "chili_momentum_news_pr_cadence_hours", "08:00-08:50"):
        assert cat.pr_cadence_active(now=_utc(2026, 3, 8, 12, 15)) is True
        # 07:15 UTC = 03:15 EDT -> outside 08:00-08:50.
        assert cat.pr_cadence_active(now=_utc(2026, 3, 8, 7, 15)) is False


def test_pr_cadence_fall_back_repeated_hour_uses_correct_offset():
    """On fall-back day, 13:15 UTC = 08:15 EST (-5) -> inside 08:00-08:50. A stale -4 (EDT)
    bug would read 09:15 -> outside, AND past the 09:30 cutoff is irrelevant here. Pins EST."""
    with patch.object(cat.settings, "chili_momentum_news_pr_cadence_hours", "08:00-08:50"):
        assert cat.pr_cadence_active(now=_utc(2026, 11, 1, 13, 15)) is True
        # 12:15 UTC = 07:15 EST -> outside this window.
        assert cat.pr_cadence_active(now=_utc(2026, 11, 1, 12, 15)) is False


# ===========================================================================
# (3) GREEN-DAY ET-CALENDAR BUCKETING: ET-midnight crossing of UTC timestamps
# ===========================================================================

class _FakeQuery:
    """Minimal SQLAlchemy-query stand-in: ignores .filter()/.query column args and
    returns the canned rows on .all(). Rows are (terminal_at, realized_pnl, outcome_class)
    tuples for green-day, or whatever the caller canned."""

    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def order_by(self, *a, **k):
        return self

    def limit(self, *a, **k):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows

    def query(self, *a, **k):
        return _FakeQuery(self._rows)


@pytest.fixture
def _bucketing_env():
    """Explicit (NOT autouse) fixture for the DB-backed bucketing tests: widen the ET-day
    query bounds so synthetic rows always fall in-window, and treat every synthetic
    outcome_class as a real entry so the ET-calendar bucketing (the part under test) runs.

    Deliberately NOT autouse so the `_et_day_bounds_utc` tests below see the REAL function."""
    with patch.object(rp, "_et_day_bounds_utc", side_effect=_wide_bounds), \
         patch(
            "app.services.trading.momentum_neural.outcome_labels.is_real_entry_outcome",
            return_value=True):
        yield


def _wide_bounds(*, days_ago: int = 0):
    """Deterministic bounds so synthetic UTC rows always fall inside the query window,
    and 'today' is far in the future (so all synthetic past days count). We bypass
    datetime.now()-dependence to keep the test stable over real time."""
    # far_start = epoch-ish, today_start = year 2100 (everything is 'past').
    far = datetime(2000, 1, 1)
    today = datetime(2100, 1, 1)
    return (far, today)


def test_green_day_bucketing_2330_utc_crosses_to_next_et_day_in_winter(_bucketing_env):
    """A terminal_at at 03:30 UTC in WINTER (EST -5) = 22:30 the PRIOR ET day. And 23:30
    ET = 04:30 UTC next day. Build two fills that look adjacent in UTC but land in the SAME
    ET day vs different ET days; assert the daily PnL sum buckets by ET, not UTC.

    Fills (winter, EST -5):
      A: 2026-01-15 03:30 UTC  -> 2026-01-14 22:30 ET   (ET day = Jan 14)  pnl +10
      B: 2026-01-15 04:30 UTC  -> 2026-01-14 23:30 ET   (ET day = Jan 14)  pnl +5
      C: 2026-01-15 05:30 UTC  -> 2026-01-15 00:30 ET   (ET day = Jan 15)  pnl -3
    UTC-naive bucketing would put all three on Jan 15. ET bucketing: Jan14=+15 (green),
    Jan15=-3 (red). Streak walked from most-recent: Jan15 red -> streak 0.
    """
    rows = [
        (datetime(2026, 1, 15, 3, 30), 10.0, "win"),
        (datetime(2026, 1, 15, 4, 30), 5.0, "win"),
        (datetime(2026, 1, 15, 5, 30), -3.0, "loss"),
    ]
    db = _FakeDB(rows)
    streak, meta = rp.consecutive_green_days(db, execution_family="momentum_neural")
    # Most-recent ET day (Jan 15) is red -> streak 0; two ET days seen (Jan14, Jan15).
    assert meta["days_seen"] == 2
    assert streak == 0


def test_green_day_streak_counts_contiguous_green_et_days(_bucketing_env):
    """Three contiguous green ET days then a red -> streak 3, stops at the red.
    Uses 18:00 UTC fills (=13:00 EST, unambiguous mid-day ET) so bucketing is clean."""
    rows = [
        (datetime(2026, 1, 12, 18, 0), 4.0, "win"),   # Jan 12 ET green
        (datetime(2026, 1, 13, 18, 0), 6.0, "win"),   # Jan 13 ET green
        (datetime(2026, 1, 14, 18, 0), 2.0, "win"),   # Jan 14 ET green
        (datetime(2026, 1, 15, 18, 0), -9.0, "loss"), # Jan 15 ET red
    ]
    streak, meta = rp.consecutive_green_days(_FakeDB(rows), execution_family="momentum_neural")
    assert streak == 0  # most-recent (Jan15) is red -> immediate stop
    # Drop the red day: now the 3 greens are the most-recent contiguous run.
    streak2, meta2 = rp.consecutive_green_days(_FakeDB(rows[:3]), execution_family="momentum_neural")
    assert streak2 == 3
    assert meta2["green_usd"] == pytest.approx(12.0)


def test_green_day_2359_vs_0001_et_different_days_winter(_bucketing_env):
    """GNARLY boundary: 23:59 ET vs 00:01 ET are DIFFERENT ET calendar days. In winter
    (EST -5): 23:59 ET on Jan 14 = 04:59 UTC Jan 15; 00:01 ET on Jan 15 = 05:01 UTC Jan 15.
    Same UTC day, adjacent by 2 minutes, but DIFFERENT ET days. A +PnL at 23:59 and a -PnL
    at 00:01 must land on separate ET days (Jan14 green, Jan15 red)."""
    rows = [
        (datetime(2026, 1, 15, 4, 59), 8.0, "win"),    # 23:59 ET Jan 14
        (datetime(2026, 1, 15, 5, 1), -8.0, "loss"),   # 00:01 ET Jan 15
    ]
    streak, meta = rp.consecutive_green_days(_FakeDB(rows), execution_family="momentum_neural")
    assert meta["days_seen"] == 2          # two distinct ET days, not one
    assert streak == 0                     # most-recent ET day (Jan15) is red


def test_green_day_2359_vs_0001_et_summer_edt(_bucketing_env):
    """Same boundary in SUMMER (EDT -4): 23:59 ET on Jul 14 = 03:59 UTC Jul 15; 00:01 ET on
    Jul 15 = 04:01 UTC Jul 15. Different ET days. Make BOTH green so streak should be 2."""
    rows = [
        (datetime(2026, 7, 15, 3, 59), 8.0, "win"),    # 23:59 EDT Jul 14
        (datetime(2026, 7, 15, 4, 1), 3.0, "win"),     # 00:01 EDT Jul 15
    ]
    streak, meta = rp.consecutive_green_days(_FakeDB(rows), execution_family="momentum_neural")
    assert meta["days_seen"] == 2
    assert streak == 2


def test_green_day_spring_forward_day_buckets_correctly(_bucketing_env):
    """On 2026-03-08 (spring-forward), fills around the skipped 02:xx hour still bucket to
    the SAME ET calendar day (Mar 8). 06:30 UTC = 01:30 EST; 08:30 UTC = 04:30 EDT — both
    Mar 8 ET. Sum green. The missing wall-clock hour must not split or drop the day."""
    rows = [
        (datetime(2026, 3, 8, 6, 30), 5.0, "win"),    # 01:30 EST Mar 8
        (datetime(2026, 3, 8, 8, 30), 7.0, "win"),    # 04:30 EDT Mar 8
    ]
    streak, meta = rp.consecutive_green_days(_FakeDB(rows), execution_family="momentum_neural")
    assert meta["days_seen"] == 1          # one ET day despite the DST jump between fills
    assert meta["green_usd"] == pytest.approx(12.0)
    assert streak == 1


def test_green_day_fall_back_repeated_hour_same_et_day(_bucketing_env):
    """On 2026-11-01 (fall-back), the 01:xx hour repeats. 05:30 UTC = 01:30 EDT and 06:30
    UTC = 01:30 EST are TWO distinct instants on the SAME ET wall-clock and the SAME ET
    calendar day (Nov 1). Both must bucket to Nov 1 and sum."""
    rows = [
        (datetime(2026, 11, 1, 5, 30), 4.0, "win"),   # 01:30 EDT Nov 1
        (datetime(2026, 11, 1, 6, 30), 6.0, "win"),   # 01:30 EST Nov 1 (repeat)
        (datetime(2026, 11, 1, 18, 0), 1.0, "win"),   # 13:00 EST Nov 1
    ]
    streak, meta = rp.consecutive_green_days(_FakeDB(rows), execution_family="momentum_neural")
    assert meta["days_seen"] == 1
    assert meta["green_usd"] == pytest.approx(11.0)


def test_green_day_naive_terminal_at_treated_as_utc(_bucketing_env):
    """terminal_at rows are naive (DB stores naive UTC); the code does ts.replace(tzinfo=utc).
    Confirm a naive 18:00 is read as 18:00 UTC = 13:00 EST -> Jan 15 ET."""
    rows = [(datetime(2026, 1, 15, 18, 0), 5.0, "win")]
    streak, meta = rp.consecutive_green_days(_FakeDB(rows), execution_family="momentum_neural")
    assert streak == 1
    assert meta["days_seen"] == 1


# ===========================================================================
# (3b) _et_day_bounds_utc — the SUSPECTED DST off-by-one in day-back subtraction
# ===========================================================================

class _FrozenDatetime(datetime):
    """A datetime subclass whose ``.now(tz)`` returns a pinned instant, so we can place
    ``_et_day_bounds_utc`` ON a real DST-transition day. Everything else (constructors,
    ``.astimezone``) delegates to the real ``datetime``."""

    _frozen: datetime = datetime(2026, 6, 27, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):  # type: ignore[override]
        return cls._frozen.astimezone(tz) if tz is not None else cls._frozen


def _frozen_at(et_instant: datetime):
    """Patch ``rp.datetime`` so ``datetime.now(et)`` lands on ``et_instant`` (an aware ET dt)."""
    fd = _FrozenDatetime
    fd._frozen = et_instant
    return patch.object(rp, "datetime", fd)


def test_et_day_bounds_window_is_a_true_et_calendar_day_winter():
    """FIX MED-4: each [start,end) is a TRUE ET calendar day. On a normal (non-transition)
    winter day the window is exactly 24h, and start localizes to ET midnight."""
    et = ZoneInfo("America/New_York")
    with _frozen_at(datetime(2026, 1, 15, 14, 0, tzinfo=et)):
        start, end = rp._et_day_bounds_utc(days_ago=0)
    # start/end are naive UTC; localize back to ET and confirm both are ET midnight and the
    # span is one calendar day apart.
    start_et = start.replace(tzinfo=timezone.utc).astimezone(et)
    end_et = end.replace(tzinfo=timezone.utc).astimezone(et)
    assert (start_et.hour, start_et.minute) == (0, 0)
    assert (end_et.hour, end_et.minute) == (0, 0)
    assert end_et.date() == start_et.date() + timedelta(days=1)
    assert (end - start) == timedelta(days=1)  # winter day really is 24h


def test_et_day_bounds_spring_forward_day_is_23h_not_24h():
    """FIX MED-4: on SPRING-FORWARD (2026-03-08) the true ET calendar day is 23h. The fixed
    calendar-date arithmetic yields a 23h [start,end) window, NOT the old always-24h drift."""
    et = ZoneInfo("America/New_York")
    with _frozen_at(datetime(2026, 3, 8, 14, 0, tzinfo=et)):  # the spring-forward day, post-jump
        start, end = rp._et_day_bounds_utc(days_ago=0)
    start_et = start.replace(tzinfo=timezone.utc).astimezone(et)
    end_et = end.replace(tzinfo=timezone.utc).astimezone(et)
    assert (start_et.hour, start_et.minute) == (0, 0)
    assert (end_et.hour, end_et.minute) == (0, 0)
    assert end_et.date() == start_et.date() + timedelta(days=1)
    assert (end - start) == timedelta(hours=23)  # spring-forward day is a true 23h ET day


def test_et_day_bounds_fall_back_day_is_25h_not_24h():
    """FIX MED-4: on FALL-BACK (2026-11-01) the true ET calendar day is 25h. The fixed
    arithmetic yields a 25h window aligned to ET midnight, not the old absolute-24h drift."""
    et = ZoneInfo("America/New_York")
    with _frozen_at(datetime(2026, 11, 1, 14, 0, tzinfo=et)):  # the fall-back day
        start, end = rp._et_day_bounds_utc(days_ago=0)
    start_et = start.replace(tzinfo=timezone.utc).astimezone(et)
    end_et = end.replace(tzinfo=timezone.utc).astimezone(et)
    assert (start_et.hour, start_et.minute) == (0, 0)
    assert (end_et.hour, end_et.minute) == (0, 0)
    assert end_et.date() == start_et.date() + timedelta(days=1)
    assert (end - start) == timedelta(hours=25)  # fall-back day is a true 25h ET day


def test_et_day_bounds_days_ago_lands_on_true_et_calendar_dates():
    """FIX MED-4: days_ago subtracts CALENDAR days (date space), so each window is the ET
    calendar day exactly N days before today — regardless of any DST transition in between.
    Frozen two days after spring-forward: days_ago=2 must land squarely on the 23h transition
    day and produce a 23h window aligned to ET midnight (the old absolute-timedelta path drifted)."""
    et = ZoneInfo("America/New_York")
    with _frozen_at(datetime(2026, 3, 10, 9, 0, tzinfo=et)):  # Tue, two days after the 03-08 jump
        for n in (0, 1, 7, 20, 30):
            s, e = rp._et_day_bounds_utc(days_ago=n)
            s_et = s.replace(tzinfo=timezone.utc).astimezone(et)
            e_et = e.replace(tzinfo=timezone.utc).astimezone(et)
            assert (s_et.hour, s_et.minute) == (0, 0)
            assert e_et.date() == s_et.date() + timedelta(days=1)
            assert s_et.date() == (datetime(2026, 3, 10).date() - timedelta(days=n))
        # days_ago=2 lands ON the spring-forward calendar day -> a true 23h window.
        s2, e2 = rp._et_day_bounds_utc(days_ago=2)
        assert (e2 - s2) == timedelta(hours=23)


# ===========================================================================
# prior_day / _prior_session ET bucketing parity with green-day
# ===========================================================================

def test_prior_session_buckets_by_et_day_winter_boundary(_bucketing_env):
    """_prior_session_pnl_over_equity buckets by ET day too. Two fills straddling ET midnight
    in winter must land on different ET days; the 'prior' (most-recent past) day is the later
    ET day. Patch equity to a known basis so normalization is deterministic."""
    rows = [
        (datetime(2026, 1, 15, 4, 59), 100.0),   # 23:59 ET Jan 14
        (datetime(2026, 1, 15, 5, 1), 50.0),     # 00:01 ET Jan 15
    ]
    db = _FakeDB(rows)
    with patch.object(rp, "_account_equity_usd", return_value=1000.0):
        prior, sample = rp._prior_session_pnl_over_equity(
            db, execution_family="momentum_neural", lookback_days=20)
    # Two ET days -> sample length 2; prior = most-recent (Jan15) = 50/1000 = 0.05.
    assert len(sample) == 2
    assert prior == pytest.approx(0.05)
    assert sorted(sample) == pytest.approx([0.05, 0.10])


if __name__ == "__main__":  # pragma: no cover
    sys.exit(pytest.main([__file__, "-v"]))
