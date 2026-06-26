"""Event/structure-based abandonment of pre-entry watchers (Ross stays on a strong
stock all day). Proves the keep-vs-reap building blocks the reaper uses:

  * conviction: ross_score>=floor OR rvol>=coiling-exempt extreme floor OR daily_breaking
    (SAME source the arm-queue / continuation gate read) — no per-session fetch;
  * front-side: fail-OPEN on absent snapshot, but AFFIRMATIVE backside (cached is_backside,
    retrace>veto, or last_mid<session_vwap) demotes a watcher to reap (no slot leak);
  * hard ceiling: derived adaptively from the extend window (one documented multiple);
  * kill-switch OFF => the helper is inert (the reaper never calls it).

These are pure-function proofs (no DB) so they run anywhere without TEST_DATABASE_URL.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural.auto_arm import (
    _event_based_abandonment_enabled,
    _event_based_max_extend_seconds,
    _session_still_front_side,
    _session_still_high_conviction,
    _watch_extend_seconds,
)


def _row(symbol: str, *, ross: float | None = None, rvol: float | None = None,
         daily_breaking: bool = False):
    """A fake MomentumSymbolViability carrying the SAME persisted shape the helpers read
    (execution_readiness_json.extra.ross_scores[SYM] + ross_signals[SYM])."""
    sig: dict = {"ticker": symbol}
    if rvol is not None:
        sig["vol_ratio"] = rvol
    if daily_breaking:
        sig["daily_breaking_major"] = True
    extra: dict = {"ross_signals": {symbol.upper(): sig}}
    if ross is not None:
        extra["ross_scores"] = {symbol.upper(): ross}
    return SimpleNamespace(symbol=symbol, execution_readiness_json={"extra": extra})


# ── kill-switch parity ────────────────────────────────────────────────────────────────
def test_flag_off_by_default():
    # default OFF => reaper never runs the event check => byte-identical fixed clock.
    assert _event_based_abandonment_enabled() is False


def test_flag_on_when_set(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_event_based_abandonment_enabled", True)
    assert _event_based_abandonment_enabled() is True


# ── conviction (same source as the arm-queue) ─────────────────────────────────────────
def test_high_conviction_by_ross_score(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", 0.7)
    assert _session_still_high_conviction(_row("IVF", ross=0.85)) is True
    assert _session_still_high_conviction(_row("IVF", ross=0.50)) is False


def test_high_conviction_by_extreme_rvol(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_explosive_rvol_floor", 3.0)
    monkeypatch.setattr(settings, "chili_momentum_coiling_exempt_rvol_mult", 3.0)
    # floor = 3.0 * 3.0 = 9.0x
    assert _session_still_high_conviction(_row("IVF", rvol=9.5)) is True
    assert _session_still_high_conviction(_row("IVF", rvol=5.0)) is False


def test_high_conviction_by_daily_breaking():
    assert _session_still_high_conviction(_row("IVF", daily_breaking=True)) is True


def test_no_row_is_not_high_conviction():
    # a watcher with NO fresh candidate row => not high-conviction => falls through to reap.
    assert _session_still_high_conviction(None) is False


# ── front-side (fail-open; affirmative backside reaps) ────────────────────────────────
def test_front_side_fail_open_on_missing_snapshot():
    assert _session_still_front_side(None) is True
    assert _session_still_front_side({}) is True  # no backside evidence => keep-eligible


def test_cached_is_backside_flag_authoritative():
    assert _session_still_front_side({"is_backside": True}) is False
    assert _session_still_front_side({"is_backside": False}) is True


def test_faded_retrace_demotes(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_event_based_retrace_veto", 0.66)
    assert _session_still_front_side({"retrace_from_hod": 0.80}) is False  # faded
    assert _session_still_front_side({"retrace_from_hod": 0.40}) is True   # still near highs


def test_below_vwap_demotes():
    assert _session_still_front_side({"last_mid": 9.0, "session_vwap": 10.0}) is False
    assert _session_still_front_side({"last_mid": 11.0, "session_vwap": 10.0}) is True


def test_garbage_axis_is_ignored_fail_open():
    # unparseable values must not cut a keep candidate short.
    assert _session_still_front_side({"retrace_from_hod": "nan?", "last_mid": "x"}) is True


# ── hard ceiling (adaptive, one documented multiple) ──────────────────────────────────
def test_ceiling_is_multiple_of_extend(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_max_watch_seconds", 300)
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_watch_extend_seconds", 600)
    monkeypatch.setattr(settings, "chili_momentum_event_based_max_extend_mult", 3.0)
    assert _watch_extend_seconds() == 600
    assert _event_based_max_extend_seconds() == 1800  # 3 * 600


def test_ceiling_never_tighter_than_extend(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_auto_arm_watch_extend_seconds", 600)
    monkeypatch.setattr(settings, "chili_momentum_event_based_max_extend_mult", 1.0)
    # mult 1.0 => ceiling == extend window (clamp floor), never below it.
    assert _event_based_max_extend_seconds() == _watch_extend_seconds()


# ── the composite keep rule the loop applies ──────────────────────────────────────────
def test_keep_requires_both_conviction_and_front_side(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_continuation_ross_floor", 0.7)
    hot = _row("IVF", ross=0.9)
    cold = _row("DUD", ross=0.3)
    # high-conviction + front-side => KEEP eligible
    assert _session_still_high_conviction(hot) and _session_still_front_side({}) is True
    # high-conviction but FADED => reap (front-side False)
    assert _session_still_high_conviction(hot) and _session_still_front_side(
        {"is_backside": True}
    ) is False
    # front-side but COOLED out of conviction => reap (conviction False)
    assert _session_still_high_conviction(cold) is False
