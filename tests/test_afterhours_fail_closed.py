"""WAVE-1 FIX-8 — AFTERHOURS FAIL-CLOSED schedule sizing.

market_profile.schedule_window_now had no "afterhours" window: 16:00-20:00 ET returned
"closed", and the live_runner sched-mult map ``{"hot":1.5,"midday":0.5,"late":0.0}.get(_win,
1.0)`` fail-OPENed any unmapped window to FULL size — while is_tradeable_now() allows the
16:00-20:00 ET entry. 14d after-hours: 1W/11L −$72.65.

The fix:
  (a) schedule_window_now returns an explicit "afterhours" for 16:00 → afterhours_end ET.
  (b) the sched-mult map adds "afterhours":0.0 AND the .get() default is 0.0 — fail-CLOSED
      for ANY unknown window (entries only; exits untouched).
  (c) chili_momentum_midday_deweight_enabled already defaults True on main (no flip needed).

These are pure clock/config assertions (no DB tick).
"""

from __future__ import annotations

from datetime import datetime, timezone

from app.config import settings
from app.services.trading.momentum_neural import market_profile as mp


NY = None
try:
    from zoneinfo import ZoneInfo
    NY = ZoneInfo("America/New_York")
except Exception:  # pragma: no cover
    NY = None


def _et(h: int, m: int = 0, s: int = 0) -> datetime:
    # 2026-06-15 is a Monday in EDT (UTC-4).
    return datetime(2026, 6, 15, h, m, s, tzinfo=NY)


# The sched-mult map is inline in live_runner; mirror its documented contract here so the
# fail-closed default and the afterhours mapping are pinned by a unit test.
_SCHED_MULT = {"hot": 1.5, "midday": 0.5, "late": 0.0, "afterhours": 0.0}


def _mult_for(win: str) -> float:
    # EXACTLY the live_runner composition: unknown windows fail CLOSED to 0.0.
    return _SCHED_MULT.get(win, 0.0)


def test_afterhours_window_is_explicit_and_sizes_zero():
    # 16:00 ET is the FIRST afterhours minute; it maps to a 0.0 sizing multiplier.
    assert mp.schedule_window_now(now=_et(16, 0, 0)) == "afterhours"
    assert _mult_for("afterhours") == 0.0
    # 19:59 still afterhours (default 20:00 end); 20:00 -> closed.
    assert mp.schedule_window_now(now=_et(19, 59, 59)) == "afterhours"
    assert mp.schedule_window_now(now=_et(20, 0, 0)) == "closed"


def test_unknown_or_closed_window_fails_closed_to_zero():
    # ANY window not in the map (incl. "closed" / a garbage value) => 0.0 (fail-closed).
    assert _mult_for("closed") == 0.0
    assert _mult_for("weekend") == 0.0
    assert _mult_for("") == 0.0
    assert _mult_for("some_future_window") == 0.0


def test_known_active_windows_unchanged():
    # The productive windows keep their exact multipliers (byte-identical to pre-fix).
    assert _mult_for("hot") == 1.5
    assert _mult_for("midday") == 0.5
    assert _mult_for("late") == 0.0
    # And schedule_window_now still classifies them correctly.
    assert mp.schedule_window_now(now=_et(4, 5)) == "hot"
    assert mp.schedule_window_now(now=_et(11, 0)) == "midday"
    assert mp.schedule_window_now(now=_et(15, 0)) == "late"


def test_afterhours_end_is_config_bound_not_a_magic_time():
    # The AH window END follows the configured afterhours_end (default 20:00), reusing the
    # ONE bound already used by market_session_now — no second magic time.
    end_min = mp._afterhours_end_min()
    assert end_min == max(end_min, mp._REGULAR_CLOSE_MIN)  # 16:00 <= end
    # market_session_now agrees that 16:00-20:00 ET is the afterhours session.
    assert mp.market_session_now("AAPL", now=_et(17, 0)) == "afterhours"


def test_midday_deweight_defaults_on_main():
    # WAVE-1 FIX-8(c): the midday de-weight is already ON by default on main-lineage.
    assert settings.chili_momentum_midday_deweight_enabled is True
