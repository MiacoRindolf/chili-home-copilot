"""Per-symbol attempt fatigue must count ENTRIES, not session rows.

Ross's rule is "stop trading a symbol after ~N attempts"
(`chili_momentum_per_symbol_max_attempts`, default 3). The counter behind it read
one row per session, on the stated assumption that "the lane arms one live
session per entry attempt".

The recycle broke that assumption: a session that stops out re-enters IN PLACE.
Live 2026-09-08 — WYHG took EIGHT entries in 31 minutes for -$212.83 inside a
SINGLE session, so the counter would have read 1 against a cap of 3 and vetoed
nothing. The account-wide consecutive-loss halt was blind the same way and for
the same reason: it counts outcome rows, and a recycling session emits one.

DB-free: exercises the level mapping and the cycle arithmetic.
"""
from __future__ import annotations

import pytest

from app.services.trading.momentum_neural import auto_arm


def _attempts_from(snapshots: list[dict]) -> int:
    """The arithmetic the query performs, over rows it would have fetched."""
    attempts = 0
    for snap in snapshots:
        attempts += 1
        try:
            le = (snap or {}).get("momentum_live_execution") or {}
            cycles = int(le.get("trade_cycles") or 0)
        except (AttributeError, TypeError, ValueError):
            cycles = 0
        if cycles > 1:
            attempts += cycles - 1
    return attempts


def _sess(cycles=None):
    le = {} if cycles is None else {"trade_cycles": cycles}
    return {"momentum_live_execution": le}


def test_the_wyhg_shape_now_counts_eight_not_one():
    """One session, eight entries — the day that cost -$212.83."""
    assert _attempts_from([_sess(8)]) == 8


def test_a_session_that_never_recycled_counts_once():
    for cycles in (None, 0, 1):
        assert _attempts_from([_sess(cycles)]) == 1, cycles


def test_sessions_and_their_recycles_add_up():
    assert _attempts_from([_sess(1), _sess(3), _sess(2)]) == 6


def test_a_malformed_snapshot_still_counts_the_session_itself():
    """Fail-open on the cycle field must not lose the attempt entirely."""
    for bad in ({}, {"momentum_live_execution": None},
                {"momentum_live_execution": {"trade_cycles": "many"}},
                {"momentum_live_execution": {"trade_cycles": None}}):
        assert _attempts_from([bad]) == 1, bad


def test_no_sessions_is_zero():
    assert _attempts_from([]) == 0


@pytest.mark.parametrize("n,expected", [
    (0, "green"), (1, "green"),
    (2, "yellow"),          # the borderline last allowed try — size down
    (3, "red"), (8, "red"), # at/above the cap — veto a NEW entry
])
def test_the_cap_bands(monkeypatch, n, expected):
    monkeypatch.setattr(auto_arm.settings,
                        "chili_momentum_per_symbol_fatigue_enabled", True, raising=False)
    monkeypatch.setattr(auto_arm.settings,
                        "chili_momentum_per_symbol_max_attempts", 3, raising=False)
    assert auto_arm._per_symbol_fatigue_level(n) == expected


def test_wyhg_would_have_been_vetoed_at_the_third_entry(monkeypatch):
    """The whole point: eight entries should never have been reachable."""
    monkeypatch.setattr(auto_arm.settings,
                        "chili_momentum_per_symbol_fatigue_enabled", True, raising=False)
    monkeypatch.setattr(auto_arm.settings,
                        "chili_momentum_per_symbol_max_attempts", 3, raising=False)
    levels = [auto_arm._per_symbol_fatigue_level(_attempts_from([_sess(c)]))
              for c in range(1, 9)]
    assert levels[:2] == ["green", "yellow"]
    assert all(x == "red" for x in levels[2:]), levels


def test_disabled_is_always_green(monkeypatch):
    """OFF stays byte-identical — the counter change alone cannot veto anything."""
    monkeypatch.setattr(auto_arm.settings,
                        "chili_momentum_per_symbol_fatigue_enabled", False, raising=False)
    assert auto_arm._per_symbol_fatigue_level(99) == "green"
