"""Bailout depth guard — corpus 2026-08-29 (27/36 fast-bail = shakeout).

Ang fast-bail ay dapat makakita ng dip na umubos na ng >=min_frac ng
entry->stop na distansya bago pumutok; ang mababaw na dip sa isang volatile
mover ay ingay, hindi distribusyon. Ang hard stop ang nananatiling sahig.

Runnable: pytest tests/test_bailout_depth_guard.py -v
"""
from __future__ import annotations

from app.services.trading.momentum_neural.live_runner import (
    bailout_depth_guard_holds,
)


def test_shallow_dip_holds():
    # SDOT shape: entry 22.25, bid 22.20 (23bps), stop 21.80 (202bps risk)
    # -> depth 0.11 < 0.5 => HUMAWAK.
    hold, depth = bailout_depth_guard_holds(
        enabled=True, avg_entry=22.25, bid=22.20, stop_price=21.80, min_frac=0.5,
    )
    assert hold is True
    assert 0.10 < depth < 0.12


def test_deep_dip_allows_bail():
    # PMI shape: entry 6.70, bid 6.30, stop 6.10 -> depth 0.67 >= 0.5 => bail.
    hold, depth = bailout_depth_guard_holds(
        enabled=True, avg_entry=6.70, bid=6.30, stop_price=6.10, min_frac=0.5,
    )
    assert hold is False
    assert depth > 0.6


def test_exact_threshold_allows_bail():
    hold, depth = bailout_depth_guard_holds(
        enabled=True, avg_entry=10.0, bid=9.5, stop_price=9.0, min_frac=0.5,
    )
    assert hold is False
    assert depth == 0.5


def test_missing_stop_falls_open_to_old_behavior():
    hold, depth = bailout_depth_guard_holds(
        enabled=True, avg_entry=10.0, bid=9.9, stop_price=None, min_frac=0.5,
    )
    assert hold is False
    assert depth is None


def test_stop_above_entry_falls_open():
    # Ratcheted-to-profit na stop: wala nang saysay ang depth unit dito.
    hold, depth = bailout_depth_guard_holds(
        enabled=True, avg_entry=10.0, bid=9.9, stop_price=10.5, min_frac=0.5,
    )
    assert hold is False
    assert depth is None


def test_disabled_flag_is_noop():
    hold, depth = bailout_depth_guard_holds(
        enabled=False, avg_entry=22.25, bid=22.20, stop_price=21.80, min_frac=0.5,
    )
    assert hold is False
    assert depth is None


def test_bid_below_stop_always_bails():
    # Lampas na sa stop ang bid — depth > 1 >= min_frac; hindi tayo pipigil
    # (ang stop machinery mismo ang bahala, pero ang guard ay hindi haharang).
    hold, depth = bailout_depth_guard_holds(
        enabled=True, avg_entry=10.0, bid=8.9, stop_price=9.0, min_frac=0.5,
    )
    assert hold is False
    assert depth > 1.0
