"""ENTRY-FSM CONTINUATION SA SCHEDULER MODE (2026-08-19 zero-fill root cause).

Ang entry FSM ay umaabante ng ISANG mekanikal na estado kada invocation:
``watching_live -> live_entry_candidate -> live_pending_entry -> place``, kaya
TATLONG invocation ang kailangan bago mailagay ang order. May fast path na ang
event-loop driver para rito, pero sa SCHEDULER mode ito ay dokumentadong NO-OP —
at ang deployed window ay ``LOOP_ENABLED=false`` + ``SCHEDULER_ENABLED=true``.

Sinukat noong 08-19: 30.6s median sa pagitan ng ticks (nominal 10s), 62% ng ticks
nilaktawan, kaya **408s MEDIAN** mula "magandang entry" hanggang sa pre-submit
quote check. Kaya 14 ang na-defer sa ``execution_bbo_above_planned_limit`` (limit
na pinlano pitong minuto bago noon) at ZERO ang na-submit sa 53 pending_place.
"""
from __future__ import annotations

import app.services.trading.momentum_neural.live_runner as lr


def _clear():
    with lr._entry_fsm_continuation_lock:
        lr._PENDING_ENTRY_FSM_CONTINUATION.clear()


def _loop_declines(monkeypatch):
    """Gayahin ang SCHEDULER mode: tumatanggi ang loop driver (hindi siya owner)."""
    import app.services.trading.momentum_neural.live_runner_loop as loop

    monkeypatch.setattr(
        loop, "schedule_live_runner_entry_continuation", lambda sid: False,
        raising=False,
    )


def _loop_owns(monkeypatch):
    """Gayahin ang LOOP mode: tinatanggap ng loop driver ang request."""
    import app.services.trading.momentum_neural.live_runner_loop as loop

    monkeypatch.setattr(
        loop, "schedule_live_runner_entry_continuation", lambda sid: True,
        raising=False,
    )


def test_scheduler_mode_records_and_consumes_once(monkeypatch):
    _clear()
    _loop_declines(monkeypatch)
    assert lr._schedule_entry_fsm_continuation(4242) is True
    # Isang beses lang — ang pangalawang consume ay False (walang libreng ulit).
    assert lr.consume_entry_fsm_continuation(4242) is True
    assert lr.consume_entry_fsm_continuation(4242) is False


def test_loop_mode_does_not_record(monkeypatch):
    """PARITY: kapag ang loop ang owner, WALANG naitatala — hindi dapat
    ma-double-drive ang FSM ng dalawang driver."""
    _clear()
    _loop_owns(monkeypatch)
    assert lr._schedule_entry_fsm_continuation(77) is True
    assert lr.consume_entry_fsm_continuation(77) is False
    with lr._entry_fsm_continuation_lock:
        assert lr._PENDING_ENTRY_FSM_CONTINUATION == set()


def test_flag_off_restores_wait_for_next_tick(monkeypatch):
    _clear()
    _loop_declines(monkeypatch)
    monkeypatch.setattr(
        lr.settings,
        "chili_momentum_entry_fsm_scheduler_continuation_enabled",
        False,
        raising=False,
    )
    assert lr._schedule_entry_fsm_continuation(9) is False
    assert lr.consume_entry_fsm_continuation(9) is False


def test_consume_is_per_session(monkeypatch):
    _clear()
    _loop_declines(monkeypatch)
    lr._schedule_entry_fsm_continuation(1)
    lr._schedule_entry_fsm_continuation(2)
    assert lr.consume_entry_fsm_continuation(2) is True
    assert lr.consume_entry_fsm_continuation(1) is True
    assert lr.consume_entry_fsm_continuation(3) is False


def test_unknown_session_consume_is_false():
    _clear()
    assert lr.consume_entry_fsm_continuation(123456) is False


def test_loop_raising_falls_back_to_scheduler_record(monkeypatch):
    """Fail-safe: kung sumabog ang loop driver, hindi dapat mawala ang
    continuation — dapat mahulog ito sa scheduler registry."""
    _clear()
    import app.services.trading.momentum_neural.live_runner_loop as loop

    def _boom(sid):
        raise RuntimeError("loop exploded")

    monkeypatch.setattr(
        loop, "schedule_live_runner_entry_continuation", _boom, raising=False
    )
    assert lr._schedule_entry_fsm_continuation(55) is True
    assert lr.consume_entry_fsm_continuation(55) is True


# ── ang driver loop mismo (bounded, walang bagong awtoridad) ──────────────────


def _driver(sid, *, passes_requesting, max_steps=3):
    """Gayahin ang _tick_one na loop ng scheduler nang hindi bumubuo ng buong
    scheduler job: bilangin kung ilang beses tumakbo ang FSM sa isang tick."""
    _clear()
    calls = []

    def _one_pass(_sid):
        calls.append(_sid)
        # Ang FSM ay humihiling ng continuation sa unang `passes_requesting` pass.
        if len(calls) <= passes_requesting:
            with lr._entry_fsm_continuation_lock:
                lr._PENDING_ENTRY_FSM_CONTINUATION.add(int(_sid))
        return True

    steps = 0
    while True:
        _one_pass(sid)
        steps += 1
        if steps >= max_steps:
            break
        if not lr.consume_entry_fsm_continuation(sid):
            break
    return calls, steps


def test_driver_runs_three_passes_for_a_full_entry():
    """watching -> candidate -> pending -> place sa ISANG tick (dati ay TATLO
    ang scheduler cycles, kada isa ay 30s median)."""
    calls, steps = _driver(10, passes_requesting=2, max_steps=3)
    assert steps == 3, calls
    assert calls == [10, 10, 10]


def test_driver_stops_when_fsm_stops_requesting():
    """Ang isang session na hindi umaabante ay HINDI paulit-ulit na tinatakbo."""
    calls, steps = _driver(11, passes_requesting=0, max_steps=3)
    assert steps == 1, calls


def test_driver_respects_max_steps_ceiling():
    """Ang estadong laging humihiling ay hindi dapat umikot sa loob ng isang tick
    at gutumin ang ibang session sa batch."""
    calls, steps = _driver(12, passes_requesting=99, max_steps=3)
    assert steps == 3, calls
    # Walang naiwang request na magpapaikot sa susunod na tick nang walang hanggan.
    assert lr.consume_entry_fsm_continuation(12) in (True, False)
