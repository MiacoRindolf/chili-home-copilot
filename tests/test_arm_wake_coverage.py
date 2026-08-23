"""Arm-wake coverage + bridge loop-drain (2026-08-23).

Ang ignition→arm bridge lang ang may arm wake mula 08-21; ang FULL scheduler
auto-arm pass at ang tape-delta ignition path ay hindi — kaya ang arm mula
doon ay naghihintay ng susunod na live-runner batch (10-30s) bago ang unang
WATCHING tick. Dagdag pa: ang `session_id` sa pass summary ay LAST-WRITER-WINS
(naa-overwrite ng deduped na kandidato) at hindi kasama ang Alpaca twin — ang
mismong session na naglalagay ng order sa paper endpoint.

Runnable: pytest tests/test_arm_wake_coverage.py -v
"""
from __future__ import annotations

import inspect
import threading
from unittest.mock import patch

from app.services.trading.momentum_neural import auto_arm
from app.services.trading.momentum_neural import ignition_loop as il


# ── armed_session_ids: append-only, kasama ang twin ─────────────────────────

def test_pass_collects_every_confirmed_arm_not_last_writer():
    src = inspect.getsource(auto_arm.run_auto_arm_pass)
    # append sa CONFIRMED arm (primary)
    assert 'out["armed_session_ids"].append(int(begin.get("session_id")))' in src
    # append sa CONFIRMED Alpaca twin
    assert 'out["armed_session_ids"].append(' in src
    assert '_tb.get("session_id")' in src


def test_armed_session_ids_initialized_empty():
    src = inspect.getsource(auto_arm.run_auto_arm_pass)
    assert 'out["armed_session_ids"] = []' in src


def test_dedupe_branch_does_not_append():
    """Ang deduped/already-active na kandidato ay nag-o-overwrite ng session_id
    pero HINDI dapat pumasok sa armed_session_ids."""
    src = inspect.getsource(auto_arm.run_auto_arm_pass)
    lo = src.index('out["skipped"] = "already_active"')
    seg = src[lo:lo + 260]
    assert 'out["session_id"] = begin.get("session_id")' in seg
    assert "armed_session_ids" not in seg


# ── wake_armed_sessions ────────────────────────────────────────────────────

def test_wake_armed_sessions_spawns_each():
    spawned: list = []
    with patch.object(il, "_spawn_arm_wake", side_effect=lambda s: spawned.append(s) or True):
        n = il.wake_armed_sessions([11, 12, 13])
    assert n == 3 and spawned == [11, 12, 13]


def test_wake_armed_sessions_tolerates_empty_and_scalar():
    with patch.object(il, "_spawn_arm_wake", return_value=True) as sp:
        assert il.wake_armed_sessions(None) == 0
        assert il.wake_armed_sessions([]) == 0
        sp.assert_not_called()
        assert il.wake_armed_sessions(7) == 1


def test_wake_armed_sessions_survives_one_failure():
    def _flaky(sid):
        if sid == 2:
            raise RuntimeError("boom")
        return True

    with patch.object(il, "_spawn_arm_wake", side_effect=_flaky):
        assert il.wake_armed_sessions([1, 2, 3]) == 2


def test_bridge_arm_uses_armed_session_ids():
    """Ang bridge ay dapat gumamit ng listahan, hindi ng last-writer session_id."""
    src = inspect.getsource(il.IgnitionScoringLoop._bridge_arm)
    assert 'wake_armed_sessions(out.get("armed_session_ids"))' in src
    assert '_spawn_arm_wake(out.get("session_id"))' not in src


# ── scheduler wiring ───────────────────────────────────────────────────────

def test_scheduler_wakes_full_pass_arms():
    from app.services import trading_scheduler as ts

    src = inspect.getsource(ts._run_momentum_auto_arm_live_job)
    assert "wake_armed_sessions" in src
    assert 'summary.get("armed_session_ids")' in src


def test_tape_delta_path_captures_and_wakes():
    from app.services import trading_scheduler as ts

    src = inspect.getsource(ts)
    lo = src.index("_arm_out = run_scoped_ignition_arm(db, _scored_syms)")
    seg = src[lo:lo + 700]
    assert "wake_armed_sessions" in seg
    assert '_arm_out.get("armed_session_ids")' in seg


# ── post-wake freshness: sessions AND subscriptions ────────────────────────

def test_post_wake_hook_syncs_subscriptions():
    src = inspect.getsource(il.IgnitionScoringLoop.start)
    assert "_refresh_sessions_and_subscriptions" in src
    hook = inspect.getsource(il.IgnitionScoringLoop._refresh_sessions_and_subscriptions)
    assert "_sessions.refresh()" in hook
    assert "_sync_subscriptions()" in hook


# ── bridge loop-drain ──────────────────────────────────────────────────────

def test_loop_drain_runs_extra_passes_while_holding_single_flight():
    """Ang symbol na na-enqueue HABANG tumatakbo ang pass ay dapat ma-drain sa
    parehong lock hold, hindi maiwan hanggang may ibang tick."""
    calls: list[int] = []

    def _locked(db, *, debounce):
        # ang totoong drain body ay nag-CLEAR ng pending sa entry — gayahin iyon
        auto_arm._IGNITION_BRIDGE_PENDING.clear()
        calls.append(1)
        if len(calls) == 1:
            # gayahin ang isang producer na nag-enqueue HABANG tumatakbo ang pass
            auto_arm._IGNITION_BRIDGE_PENDING.add("LATE")
            return {"armed": 1, "armed_session_ids": [101], "armed_symbols": ["EARLY"]}
        return {"armed": 1, "armed_session_ids": [202], "armed_symbols": ["LATE"]}

    auto_arm._IGNITION_BRIDGE_PENDING.clear()
    with patch.object(auto_arm, "_run_scoped_ignition_arm_locked", side_effect=_locked):
        out = auto_arm.run_scoped_ignition_arm(None, ["EARLY"])
    assert len(calls) == 2
    assert out["armed"] == 2
    assert out["armed_session_ids"] == [101, 202]
    assert out["drain_passes"] == 2
    auto_arm._IGNITION_BRIDGE_PENDING.clear()


def test_loop_drain_stops_when_nothing_pending():
    calls: list[int] = []

    def _locked(db, *, debounce):
        auto_arm._IGNITION_BRIDGE_PENDING.clear()
        calls.append(1)
        return {"armed": 0, "armed_session_ids": []}

    auto_arm._IGNITION_BRIDGE_PENDING.clear()
    with patch.object(auto_arm, "_run_scoped_ignition_arm_locked", side_effect=_locked):
        auto_arm.run_scoped_ignition_arm(None, ["ONE"])
    assert len(calls) == 1  # walang natirang pending => isang pass lang
    auto_arm._IGNITION_BRIDGE_PENDING.clear()


def test_loop_drain_terminates_on_none_from_debounce():
    """Ang muling na-queue na symbol ay na-debounce => None => tumitigil."""
    calls: list[int] = []

    def _locked(db, *, debounce):
        auto_arm._IGNITION_BRIDGE_PENDING.clear()
        calls.append(1)
        auto_arm._IGNITION_BRIDGE_PENDING.add("REQUEUED")
        return None if len(calls) > 1 else {"armed": 1, "armed_session_ids": [5]}

    auto_arm._IGNITION_BRIDGE_PENDING.clear()
    with patch.object(auto_arm, "_run_scoped_ignition_arm_locked", side_effect=_locked):
        out = auto_arm.run_scoped_ignition_arm(None, ["X"])
    assert len(calls) == 2
    assert out["armed"] == 1
    auto_arm._IGNITION_BRIDGE_PENDING.clear()


def test_loop_drain_respects_pass_cap(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(
        real_settings, "chili_momentum_ignition_bridge_drain_passes", 0, raising=False
    )
    calls: list[int] = []

    def _locked(db, *, debounce):
        auto_arm._IGNITION_BRIDGE_PENDING.clear()
        calls.append(1)
        auto_arm._IGNITION_BRIDGE_PENDING.add("ALWAYS")
        return {"armed": 0, "armed_session_ids": []}

    auto_arm._IGNITION_BRIDGE_PENDING.clear()
    with patch.object(auto_arm, "_run_scoped_ignition_arm_locked", side_effect=_locked):
        auto_arm.run_scoped_ignition_arm(None, ["X"])
    assert len(calls) == 1  # cap 0 = lumang behavior
    auto_arm._IGNITION_BRIDGE_PENDING.clear()


def test_loop_drain_restamps_holder_each_pass():
    """Ang chain ng lehitimong pass ay hindi dapat magmukhang wedged bridge."""
    src = inspect.getsource(auto_arm.run_scoped_ignition_arm)
    lo = src.index("LOOP-DRAIN")
    seg = src[lo:]
    assert "_ignition_bridge_holder = (" in seg
    assert "_time.monotonic()," in seg


def test_single_flight_released_even_when_drain_raises():
    def _locked(db, *, debounce):
        raise RuntimeError("boom")

    auto_arm._IGNITION_BRIDGE_PENDING.clear()
    with patch.object(auto_arm, "_run_scoped_ignition_arm_locked", side_effect=_locked):
        try:
            auto_arm.run_scoped_ignition_arm(None, ["X"])
        except RuntimeError:
            pass
    # ang single-flight ay dapat libre — kung hindi, patay ang bridge process-wide
    assert auto_arm._ignition_bridge_inflight.acquire(blocking=False) is True
    auto_arm._ignition_bridge_inflight.release()
    auto_arm._IGNITION_BRIDGE_PENDING.clear()
