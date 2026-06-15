"""Premarket universe warming — surface already-printing gappers by ~04:00 ET.

ROOT CAUSE (06/12): the day's movers (CUPR/DSY/GMM) only appeared in the selection/
viability board at ~09:34-09:40 ET because `build_equity_universe` drops any snapshot
row whose vendor `todaysChangePerc` is null — which is exactly the sparse premarket
rows. DSY/GMM were TRADING from 03:00 ET but never surfaced until after the open.

FIX: `_premarket_change_pct` derives change% from today's open (else prevDay close)
vs the live premarket print, mirroring the PROVEN nbbo_tape fallback — so a printing
premarket gapper enters the universe by ~04:00 ET. RTH is byte-unchanged (the vendor
field is populated RTH → the fallback is never consulted). Fail-CLOSED on no base.
"""
from app.config import settings
from app.services.trading.momentum_neural.universe import (
    EQUITY_ROSS_SMALLCAP,
    build_equity_universe,
)

import pytest


def _pm_row(ticker, *, last_price, day_open=None, prev_close=None, min_av=2_000_000.0, change=None):
    """A premarket-shaped snapshot row: zeroed `day` aggregate, a live `lastTrade`
    print, accumulated extended-hours volume in `min.av`, and (usually) a null
    vendor `todaysChangePerc`."""
    row = {
        "ticker": ticker,
        "todaysChangePerc": change,
        "lastTrade": {"p": last_price},
        "day": {"v": 0.0},
        "min": {"av": min_av},
    }
    if day_open is not None:
        row["day"]["o"] = day_open
    if prev_close is not None:
        row["prevDay"] = {"c": prev_close}
    return row


@pytest.fixture(autouse=True)
def _flag_on():
    old = settings.chili_momentum_premarket_change_fallback_enabled
    settings.chili_momentum_premarket_change_fallback_enabled = True
    yield
    settings.chili_momentum_premarket_change_fallback_enabled = old


def test_premarket_gapper_surfaces_via_fallback():
    """todaysChangePerc=None + day.o=1.00 + live print 1.60 → +60% → enters universe
    (the DSY/GMM case: printing premarket, no vendor change% → was dropped)."""
    snap = [_pm_row("DSY", last_price=1.60, day_open=1.00, min_av=2_000_000.0)]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "DSY" in out


def test_fallback_uses_prevday_close_when_no_open():
    """No today-open → falls back to prevDay close (matches nbbo_tape base order)."""
    snap = [_pm_row("GMM", last_price=4.60, prev_close=4.00, min_av=2_000_000.0)]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "GMM" in out  # (4.60-4.00)/4.00 = +15% > 5% floor


def test_rth_parity_vendor_field_takes_priority():
    """When todaysChangePerc IS populated (RTH), the fallback is NOT consulted —
    even if open/prev-close would imply a DIFFERENT (dropping) change. Proves RTH
    byte-identical behavior."""
    # vendor says +8% (keep), but open=2.0 vs price=1.0 would imply -50% (drop).
    snap = [_pm_row("RTHX", last_price=1.0, day_open=2.0, min_av=2_000_000.0, change=8.0)]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "RTHX" in out  # kept on the vendor +8%, not dropped by the -50% fallback


def test_no_usable_base_is_dropped_not_invented():
    """todaysChangePerc=None AND no open AND no prevDay close → base None → fail-closed
    → dropped (never invent a mover from a no-base row)."""
    snap = [_pm_row("NOBASE", last_price=1.60, min_av=2_000_000.0)]  # no day.o, no prevDay
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "NOBASE" not in out


def test_thin_no_turnover_dropped_before_change():
    """A thin name with no premarket turnover fails the $-volume floor BEFORE the
    change check — the anti-garbage floor (no flood of illiquid premarket noise)."""
    snap = [_pm_row("THIN", last_price=1.60, day_open=1.00, min_av=0.0)]  # $-vol = 0
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "THIN" not in out


def test_flag_off_is_rollback_to_old_behavior():
    """Kill-switch off → fallback returns None → the premarket gapper is dropped
    exactly as before the fix (instant per-flag rollback)."""
    settings.chili_momentum_premarket_change_fallback_enabled = False
    snap = [_pm_row("DSY", last_price=1.60, day_open=1.00, min_av=2_000_000.0)]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "DSY" not in out


def test_below_floor_still_dropped():
    """A real but weak premarket move (+3%) is still below the +5% in-play floor →
    dropped (the floor still governs; the fallback only fills the null-vendor gap)."""
    snap = [_pm_row("WEAK", last_price=1.03, day_open=1.00, min_av=5_000_000.0)]
    out = build_equity_universe(EQUITY_ROSS_SMALLCAP, snapshot=snap)
    assert "WEAK" not in out
