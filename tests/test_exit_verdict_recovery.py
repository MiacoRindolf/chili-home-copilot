"""Behavioral regressions for the recovered whole-position exit review."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import entry_gates as eg
from tests.test_exit_verdict_f_state_machine import (
    Env, T_ENTRY, _le, _spike_tape, _spike_sells, _tick,
)


def test_recycle_drops_previous_fill_anchor_and_keeps_exit_history():
    last_exit = {"price": 10.4, "source_event_id": 123}
    le = {
        "position": {"quantity": 0},
        "entry_filled_at_utc": "2026-09-10T14:00:00+00:00",
        "entry_fill_event_id": 123,
        "exit_verdict": {"phase": "exited"},
        "last_exit_receipt": last_exit,
        "trade_cycles": 2,
    }
    lr._reset_entry_state_on_recycle(le)
    le["position"] = {"quantity": 10, "avg_entry_price": 11.0}
    assert lr._exit_verdict_entry_at(le) is None
    assert "entry_fill_event_id" not in le
    assert "exit_verdict" not in le
    assert le["last_exit_receipt"] == last_exit
    assert le["trade_cycles"] == 2


@pytest.mark.parametrize("average", [10.2, None])
def test_recovered_position_cannot_inherit_prior_verdict_or_fill(average, monkeypatch):
    now = datetime(2026, 9, 10, 15)
    le = {
        "entry_filled_at_utc": (now-timedelta(hours=1)).isoformat(),
        "entry_fill_event_id": 123,
        "exit_verdict": {"phase": "exit_pending", "entry_px": 7.0},
        "side": "long",
    }
    sess = SimpleNamespace(symbol="TNON", state="live_pending_entry")
    order = SimpleNamespace(filled_size=10, average_filled_price=average,
                            order_id="new-fill", client_order_id="new-client")
    monkeypatch.setattr(lr, "_utcnow", lambda: now)
    monkeypatch.setattr(lr, "_order_open", lambda _: False)
    monkeypatch.setattr(lr, "_broker_position_basis_for_emergency", lambda *_: (10, None))
    monkeypatch.setattr(lr, "_mark_entry_order_resolved", lambda *_: None)
    monkeypatch.setattr(lr, "_resolve_alpaca_entry_claim_from_terminal_order", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_transition_recovered_primary_to_pending", lambda *_: True)
    monkeypatch.setattr(lr, "_safe_transition", lambda _db, s, state: setattr(s, "state", state))
    monkeypatch.setattr(lr, "_commit_le", lambda *_: None)
    monkeypatch.setattr(lr, "_emit", lambda *a, **k: None)
    assert lr._adopt_recovered_primary_fill_for_safety(
        None, sess, None, le=le, order=order, product_id="TNON") is True
    assert le["position"]["quantity"] == 10
    assert le["position"]["avg_entry_price"] == average
    assert lr._exit_verdict_supported(sess, le) is False
    assert "entry_fill_event_id" not in le and "exit_verdict" not in le
    assert le["operator_flatten_requested_utc"] == now.isoformat()
    assert sess.state == lr.STATE_LIVE_ENTERED


def test_rollover_does_not_wait_for_the_independent_since_high_query(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31)
    _tick(env, le, seconds=4)
    assert le["exit_verdict"]["accel_prev"] > 0
    _spike_sells(tape)

    def must_not_read(*args, **kwargs):
        pytest.fail("a decided G rollover attempted the independent D query")

    monkeypatch.setattr(eg, "leg_prints_since_high", must_not_read)
    result = _tick(env, le, seconds=12.5)
    assert result["action"] == "accel_rollover"
    assert result["verdict"]["binding"] == "not_evaluated_rollover_precedence"
    assert result["exit_receipt"]["remaining_qty"] == 31
    assert result["exit_receipt"]["exit_fraction"] == 1
    assert env.commits[-1]["exit_verdict"]["phase"] == "exit_pending"


def test_failed_since_high_read_preserves_the_independent_g_observation(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=31)

    def unavailable(*args, err, **kwargs):
        err.update(why="timeout", error="OperationalError")
        return None

    monkeypatch.setattr(eg, "leg_prints_since_high", unavailable)
    first = _tick(env, le, seconds=4)
    assert first["action"] is None and first["unreadable"] == "timeout"
    assert first["verdict"]["binding"] == "since_high_unreadable"
    assert le["exit_verdict"]["accel_prev"] > 0
    assert env.events("live_exit_verdict_unreadable")[-1]["component"] == "since_high"
    _spike_sells(tape)
    result = _tick(env, le, seconds=12.5)
    assert result["action"] == "accel_rollover"
    assert "unreadable_why" not in le["exit_verdict"]


def test_trail_authority_requires_this_ticks_successful_read_and_recovers(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le()
    _tick(env, le, seconds=4)
    authority = lr._exit_verdict_trail_authority(le, as_of=env.now)
    assert authority == {"bypass": True, "binding": "tick_deadman", "fallback_reason": None}
    # A previous successful phase cannot stand in for this next tick's read.
    next_tick = env.now + timedelta(seconds=1)
    assert lr._exit_verdict_trail_authority(le, as_of=next_tick)["fallback_reason"] == "no_current_tick_evaluation"
    tape.fail_with = {"why": "timeout", "error": "OperationalError"}
    _tick(env, le, seconds=5)
    assert le["exit_verdict"]["phase"] == "armed"
    assert lr._exit_verdict_trail_authority(le, as_of=env.now) == {
        "bypass": False, "binding": "chandelier", "fallback_reason": "tape_unreadable",
    }
    tape.fail_with = None
    _tick(env, le, seconds=6)
    assert lr._exit_verdict_trail_authority(le, as_of=env.now)["bypass"] is True


@pytest.mark.parametrize("change, reason", [
    ("stale", "stale_tape"),
    ("features", "tape_features_unreadable"),
    ("missing_level", "deadman_level_unproven"),
    ("nonfinite_level", "deadman_level_unproven"),
    ("missing_print", "deadman_level_unproven"),
    ("level_above_print", "deadman_level_unproven"),
])
def test_unproven_deadman_cannot_suppress_fallback_protection(monkeypatch, change, reason):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le()
    _tick(env, le, seconds=4)
    ev = le["exit_verdict"]
    if change == "stale":
        ev["last"]["stale"] = True
    elif change == "features":
        ev["last"]["tape_features_readable"] = False
    elif change == "missing_level":
        ev["deadman"]["level"] = None
    elif change == "nonfinite_level":
        ev["deadman"]["level"] = float("nan")
    elif change == "missing_print":
        ev["last_print"] = None
    elif change == "level_above_print":
        ev["deadman"]["level"] = ev["last_print"] + 1
    result = lr._exit_verdict_trail_authority(le, as_of=env.now)
    assert result["bypass"] is False and result["fallback_reason"] == reason


def test_durable_whole_exit_keeps_authority_without_another_tape_read(monkeypatch):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le()
    _tick(env, le, seconds=4)
    _spike_sells(tape)
    assert _tick(env, le, seconds=12.5)["action"] == "accel_rollover"
    result = lr._exit_verdict_trail_authority(le, as_of=env.now + timedelta(hours=1))
    assert result == {"bypass": True, "binding": "exit_pending", "fallback_reason": None}


@pytest.mark.parametrize("side_long, existing_stop, expected", [
    (True, 9.0, 10.0), (True, 10.1, 10.1),
    (False, 11.0, 10.0), (False, 9.9, 9.9),
])
def test_actual_partial_fill_persists_breakeven_even_with_print_trail_authority(monkeypatch, side_long, existing_stop, expected):
    tape = _spike_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    le = _le(qty=10)
    _tick(env, le, seconds=4)
    assert lr._exit_verdict_trail_authority(le, as_of=env.now)["bypass"] is True
    le["position"]["stop_price"] = existing_stop
    le["side_long"] = side_long
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    lr._apply_confirmed_live_partial_exit(
        None, SimpleNamespace(id=1), le=le, filled_quantity=4,
        entry_price=10.0, fill_price=10.4, reason="confirmed_legacy_partial",
    )
    assert le["position"]["quantity"] == 6
    assert le["position"]["partial_taken"] is True
    assert le["position"]["stop_price"] == expected
    assert le["position"]["breakeven_floor_source"] == "confirmed_partial_fill"
    assert env.commits[-1]["position"]["stop_price"] == expected
    assert lr._exit_verdict_trail_authority(le, as_of=env.now)["bypass"] is True
