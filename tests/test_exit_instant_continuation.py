"""Exit-side instant continuation (2026-08-23) — ang kapatid ng #1097.

Tatlong butas ang isinasara nito:
  1. Ang out-of-band wake paths ay NAGTATAPON ng CapturedPaperPostCommitRequest
     (ang sealed-lane phase two = ang aktwal na POST) — isang wake ay puwedeng
     mag-stage ng phase one tapos ibagsak ang POST.
  2. Ang deferred PRE-PLACE exit blocks (deadman handoff phase 1,
     cancel-not-terminal, literal-BBO refresh, unconfirmed scale-limit) ay
     naghihintay ng buong scheduler cadence para sa susunod na mekanikal na
     hakbang.
  3. Ang pending exit (order na nasa broker na) ay walang wake kapag bumalik ang
     presyo sa ibabaw ng stop — walang cross, walang bagong high.

Runnable: pytest tests/test_exit_instant_continuation.py -v
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace
from unittest.mock import patch

from app.services.trading.momentum_neural import captured_paper_dispatcher as cpd
from app.services.trading.momentum_neural import ignition_loop as il
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_ENTERED


# ── 1. two-phase seam: ang staged POST ay hindi na nawawala ────────────────

class _FakeDb:
    def __init__(self, log):
        self._log = log

    def commit(self):
        self._log.append("commit")

    def rollback(self):
        pass

    def close(self):
        self._log.append("close")


def test_two_phase_dispatches_post_commit_after_commit():
    log: list = []
    req = cpd.CapturedPaperPostCommitRequest.__new__(
        cpd.CapturedPaperPostCommitRequest
    )
    with patch.object(cpd, "dispatch_live_runner_tick", return_value=req), \
         patch.object(cpd, "dispatch_captured_paper_post_commit",
                      side_effect=lambda r: log.append("post_commit")):
        ok = cpd.run_live_runner_tick_two_phase(lambda: _FakeDb(log), 5)
    assert ok is True
    # ang POST ay dapat MATAPOS ng commit at matapos isara ang db session
    assert log == ["commit", "close", "post_commit"]


def test_two_phase_no_post_commit_for_ordinary_result():
    log: list = []
    with patch.object(cpd, "dispatch_live_runner_tick", return_value={"ok": True}), \
         patch.object(cpd, "dispatch_captured_paper_post_commit",
                      side_effect=lambda r: log.append("post_commit")):
        ok = cpd.run_live_runner_tick_two_phase(lambda: _FakeDb(log), 5)
    assert ok is True
    assert "post_commit" not in log


def test_two_phase_no_post_commit_when_phase_one_raises():
    log: list = []
    with patch.object(cpd, "dispatch_live_runner_tick",
                      side_effect=RuntimeError("boom")), \
         patch.object(cpd, "dispatch_captured_paper_post_commit",
                      side_effect=lambda r: log.append("post_commit")):
        try:
            cpd.run_live_runner_tick_two_phase(lambda: _FakeDb(log), 5)
        except RuntimeError:
            pass
    assert "post_commit" not in log
    assert "commit" not in log


def test_wake_paths_use_the_two_phase_seam():
    """Parehong out-of-band waker ay dapat dumaan sa two-phase seam."""
    assert "run_live_runner_tick_two_phase" in inspect.getsource(il._wake_runner_tick)
    assert "run_live_runner_tick_two_phase" in inspect.getsource(
        lr._stop_confirm_wake_tick
    )
    # at hindi na sa hubad na dispatch na nagtatapon ng return
    assert "dispatch_live_runner_tick(db" not in inspect.getsource(il._wake_runner_tick)


# ── 2. exit continuation gating ────────────────────────────────────────────

def test_continuation_wanted_for_deferred_preplace_block():
    result = {"ok": False, "deferred": True, "pre_place_blocked": True,
              "error": "deadman_successor_intent_frozen_for_next_pulse"}
    assert lr._exit_result_wants_continuation(result, {}) is True


def test_continuation_respects_armed_broker_backoff():
    """Ang armadong exit_next_retry_at_utc = tunay na broker backoff — igalang."""
    result = {"ok": False, "deferred": True, "pre_place_blocked": True}
    le = {"exit_next_retry_at_utc": "2026-08-23T12:00:00+00:00"}
    assert lr._exit_result_wants_continuation(result, le) is False


def test_continuation_not_wanted_after_successful_post():
    assert lr._exit_result_wants_continuation({"ok": True}, {}) is False
    # deferred pero ang order ay TUMAWID na sa transport (walang pre_place_blocked)
    assert lr._exit_result_wants_continuation(
        {"ok": False, "deferred": True}, {}
    ) is False


def test_continuation_handles_non_dict_result():
    assert lr._exit_result_wants_continuation(None, {}) is False
    assert lr._exit_result_wants_continuation("blocked", {}) is False


def test_exit_continuation_kill_switch(monkeypatch):
    from app.config import settings as real_settings

    monkeypatch.setattr(
        real_settings, "chili_momentum_exit_continuation_wake_enabled", False,
        raising=False,
    )
    assert lr._schedule_exit_continuation(90) is False


def test_exit_continuation_delay_is_shorter_than_stop_confirm():
    """Walang 1s flicker guard na dapat lampasan sa exit continuation."""
    assert lr._EXIT_CONTINUATION_WAKE_DELAY_S < lr._STOP_CONFIRM_WAKE_DELAY_S
    assert lr._EXIT_CONTINUATION_WAKE_DELAY_S > 0


def test_submit_wrapper_schedules_on_deferred_block():
    sess = SimpleNamespace(id=42)
    le = {}
    blocked = {"ok": False, "deferred": True, "pre_place_blocked": True}
    scheduled: list[int] = []
    with patch.object(lr, "_submit_live_market_exit_impl", return_value=blocked), \
         patch.object(lr, "_schedule_exit_continuation",
                      side_effect=lambda sid: scheduled.append(sid)):
        out = lr._submit_live_market_exit(None, sess, None, le=le, quantity=1.0)
    assert out is blocked
    assert scheduled == [42]


def test_submit_wrapper_silent_on_success():
    sess = SimpleNamespace(id=43)
    ok_result = {"ok": True, "order_id": "x"}
    scheduled: list[int] = []
    with patch.object(lr, "_submit_live_market_exit_impl", return_value=ok_result), \
         patch.object(lr, "_schedule_exit_continuation",
                      side_effect=lambda sid: scheduled.append(sid)):
        out = lr._submit_live_market_exit(None, sess, None, le={}, quantity=1.0)
    assert out is ok_result
    assert scheduled == []


def test_submit_wrapper_never_masks_impl_result_on_schedule_failure():
    """Ang exit result ay HINDI dapat masira ng anumang wake error."""
    sess = SimpleNamespace(id=44)
    blocked = {"ok": False, "deferred": True, "pre_place_blocked": True}
    with patch.object(lr, "_submit_live_market_exit_impl", return_value=blocked), \
         patch.object(lr, "_schedule_exit_continuation",
                      side_effect=RuntimeError("wake exploded")):
        out = lr._submit_live_market_exit(None, sess, None, le={}, quantity=1.0)
    assert out is blocked


# ── 3. pending-exit wake ───────────────────────────────────────────────────

def _tracker_with(symbol, entries):
    t = il._SessionCrossTracker()
    t._by_symbol = {symbol: entries}
    return t


def _quote(bid=None, mid=None):
    return SimpleNamespace(bid=bid, mid=mid, last=None)


def test_pending_exit_wakes_without_any_cross():
    """Flush-tapos-bounce: ang presyo ay nasa IBABAW ng stop, walang bagong
    high — dating walang wake; ngayon ang nakabinbing exit ay ginigising."""
    t = _tracker_with("SDOT", [{
        "session_id": 21, "state": STATE_LIVE_ENTERED,
        "stop_px": 16.00, "target_px": 30.0, "pending_exit": True,
    }])
    t._hi[21] = 20.0  # naabot na ang mas mataas na high kanina
    assert t.crossed("SDOT", _quote(bid=17.00, mid=17.02)) == [21]


def test_without_pending_exit_the_same_tick_is_silent():
    t = _tracker_with("SDOT", [{
        "session_id": 22, "state": STATE_LIVE_ENTERED,
        "stop_px": 16.00, "target_px": 30.0,
    }])
    t._hi[22] = 20.0
    assert t.crossed("SDOT", _quote(bid=17.00, mid=17.02)) == []


def test_pending_exit_marker_read_from_le():
    """Ang refresh ay dapat magmarka ng pending_exit mula sa alinman sa
    pending_exit_reason o sa deadman handoff record."""
    src = inspect.getsource(il._SessionCrossTracker.refresh)
    assert 'le.get("pending_exit_reason")' in src
    assert 'le.get("deadman_released_for_close")' in src
    assert '"pending_exit"' in src
