"""Repeg spoof-wall detector (Ross "Forcing a Crash" 2026-08-21).

Ang MEMX signature: pekeng ask wall na kinakansela't inaakyat kapag nilalapitan;
WALL_EATEN kapag tinuluyan ng tunay na prints. PURE synthetic-ladder tests + ang
kontrata na RETIRED na ang veto na gumagamit nito ([2] review, 2026-09-11).
Runnable: pytest tests/test_repeg_wall_detector.py -v
"""
from __future__ import annotations

import inspect
from datetime import datetime, timedelta

from app.services.trading.momentum_neural.repeg_wall import (
    ICEBERG_REAL,
    SPOOF_WALL_ACTIVE,
    WALL_EATEN,
    WALL_NONE,
    classify_repeg_wall,
)

_T0 = datetime(2026, 8, 21, 12, 0, 0)


def _snap(sec: float, ask_top: float, asks: list[list[float]]) -> dict:
    return {"observed_at": _T0 + timedelta(seconds=sec), "ask_top": ask_top,
            "asks": asks}


_BASE = [[9.99, 200.0], [10.05, 300.0]]  # ordinaryong ladder levels


def test_spoof_wall_active_on_two_upward_repegs():
    """ANG MEMX CASE: 50k wall sa 10.10 -> nawala nang WALANG prints -> sumulpot
    sa 10.20 -> ulit sa 10.30 = SPOOF_WALL_ACTIVE."""
    snaps = [
        _snap(0, 9.99, _BASE + [[10.10, 50_000.0]]),
        _snap(1, 9.99, _BASE + [[10.20, 50_000.0]]),   # repeg 1 (walang prints)
        _snap(2, 9.99, _BASE + [[10.30, 50_000.0]]),   # repeg 2
    ]
    state, dbg = classify_repeg_wall(snaps, prints=[])
    assert state == SPOOF_WALL_ACTIVE, dbg
    assert dbg["repeg_events"] >= 2


def test_wall_eaten_when_prints_consume_it():
    """Ang punch signal: prints sa/lampas sa wall price >= 80% ng size, nawala
    ang wall, walang kapalit sa itaas = WALL_EATEN."""
    snaps = [
        _snap(0, 9.99, _BASE + [[10.10, 10_000.0]]),
        _snap(2, 10.12, list(_BASE)),  # wala na ang wall
    ]
    prints = [
        (_T0 + timedelta(seconds=1.0), 10.10, 6_000.0),
        (_T0 + timedelta(seconds=1.5), 10.11, 3_500.0),
    ]
    state, dbg = classify_repeg_wall(snaps, prints)
    assert state == WALL_EATEN, dbg


def test_iceberg_real_when_wall_absorbs_and_stays():
    snaps = [
        _snap(0, 9.99, _BASE + [[10.10, 10_000.0]]),
        _snap(1, 9.99, _BASE + [[10.10, 10_000.0]]),
        _snap(2, 9.99, _BASE + [[10.10, 10_000.0]]),
    ]
    prints = [(_T0 + timedelta(seconds=0.5), 10.10, 2_000.0),
              (_T0 + timedelta(seconds=1.5), 10.10, 2_500.0)]
    state, dbg = classify_repeg_wall(snaps, prints)
    assert state == ICEBERG_REAL, dbg


def test_none_on_quiet_book_or_insufficient_data():
    snaps = [_snap(0, 9.99, list(_BASE)), _snap(1, 9.99, list(_BASE))]
    assert classify_repeg_wall(snaps, [])[0] == WALL_NONE
    assert classify_repeg_wall([], [])[0] == WALL_NONE
    assert classify_repeg_wall([_snap(0, 9.99, _BASE)], [])[0] == WALL_NONE


def test_single_repeg_is_not_yet_spoof():
    """Isang repeg lang = hindi pa aktibong spoof (WALL_NONE — walang paratang
    sa isang pangyayari)."""
    snaps = [
        _snap(0, 9.99, _BASE + [[10.10, 50_000.0]]),
        _snap(1, 9.99, _BASE + [[10.20, 50_000.0]]),
    ]
    state, dbg = classify_repeg_wall(snaps, [])
    assert state == WALL_NONE, dbg


def test_the_veto_that_consumed_the_detector_is_retired():
    """Ang tanging gate na gumamit ng detector na ito ay ang ``_l2_entry_veto`` — at
    RETIRED na iyon ([2] review, 2026-09-11): sinukat as-of sa 456 last-gate instant na
    naka-force ON ang dark flag, ang 45 na tatanggihan sana ng spoof-wall ay may forward
    255-print na +0.120% (11 symbol-day) laban sa +0.107% ng hindi tinanggihan — hindi
    mas masama. Kaya ang veto ay hindi na nagbabasa ng kahit ano at ang detector ay
    nananatiling LIBRARY na may sariling pure tests (sa itaas), walang gate."""
    from app.services.trading.momentum_neural import entry_gates

    src = inspect.getsource(entry_gates._l2_entry_veto)
    assert "read_repeg_wall_state" not in src
    assert 'return "l2_spoof_wall_active"' not in src
    assert entry_gates._l2_entry_veto("ABCD", db=object(), l2_as_of=_T0) is None
