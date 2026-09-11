"""Actual held evaluation invocations and feature-pointer lifecycle; no broker."""
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import entry_gates as eg
from app.services.trading.momentum_neural import held_evaluation_audit as audit
from app.services.trading.momentum_neural import live_runner as lr
from tests.test_exit_verdict_f_state_machine import (
    Env, T_ENTRY, _quiet_tape, _le, _sess, _tick, _DB, PROD,
)


def setup(monkeypatch):
    tape = _quiet_tape()
    env = Env(monkeypatch, tape=tape, now=T_ENTRY)
    faults = {"receipt": False}

    def emit(db, sess, name, payload):
        if name == "live_exit_evaluation" and faults["receipt"]:
            raise RuntimeError("optional receipt failure")
        env.emitted.append((name, deepcopy(payload)))
        return SimpleNamespace(id=len(env.emitted))

    monkeypatch.setattr(lr, "_emit", emit)
    return env, _le(), faults


def state(le):
    return le["exit_verdict"]["evaluation_audit"]


def test_noaction_repeated_same_asof_appends_distinct_evaluations(monkeypatch):
    env, le, _ = setup(monkeypatch)
    first = _tick(env, le, seconds=36)
    prior = deepcopy(state(le)["previous_feature"])
    second = _tick(env, le, seconds=36)
    rows = env.events("live_exit_evaluation")
    assert first["action"] is second["action"] is None and len(rows) == 2
    assert rows[0]["evaluation_id"] != rows[1]["evaluation_id"]
    assert rows[0]["decision_as_of"] == rows[1]["decision_as_of"]
    assert rows[1]["previous_feature"] == prior
    assert rows[1]["feature_pointer_advanced"] is True
    assert state(le)["previous_feature"]["evaluation_id"] == rows[1]["evaluation_id"]
    assert rows[0]["captured_prefix"] is rows[0]["replay_authority"] is False
    assert rows[0]["common_prefix_id"] is None


def test_stale_and_unreadable_append_without_advancing_feature_chain(monkeypatch):
    env, le, faults = setup(monkeypatch)
    _tick(env, le, seconds=36)
    prior = deepcopy(state(le)["previous_feature"])
    _tick(env, le, seconds=100)
    stale = env.events("live_exit_evaluation")[-1]
    assert stale["result"]["stale"] is True and stale["feature_pointer_advanced"] is False
    assert state(le)["previous_feature"] == prior
    env.tape.fail_with = {"why": "unreadable", "error": "test source fault"}
    _tick(env, le, seconds=101)
    unreadable = env.events("live_exit_evaluation")[-1]
    assert unreadable["feature_pointer_advanced"] is False
    assert state(le)["previous_feature"] == prior
    # Missing evaluation coverage does not invalidate an older successful feature.
    faults["receipt"] = True
    _tick(env, le, seconds=102)
    assert state(le)["previous_feature"] == prior
    assert state(le)["unrecorded_count"] == 1


def test_failed_receipt_invalidates_only_newly_advanced_feature(monkeypatch):
    env, le, faults = setup(monkeypatch)
    _tick(env, le, seconds=36)
    faults["receipt"] = True
    _tick(env, le, seconds=36)
    missing = deepcopy(state(le)["previous_feature"])
    assert missing["status"] == "advanced_without_receipt"
    assert missing["evaluation_id"] is missing["read_id"] is missing["event_id"] is None
    faults["receipt"] = False
    _tick(env, le, seconds=36)
    restored = env.events("live_exit_evaluation")[-1]
    assert restored["prior_unrecorded_evaluations"] == 1
    assert restored["previous_feature"] == missing
    assert state(le)["unrecorded_count"] == 0


def test_g_unreadable_and_d_unreadable_have_different_feature_advancement(monkeypatch):
    env, le, _ = setup(monkeypatch)
    _tick(env, le, seconds=36)
    prior = deepcopy(state(le)["previous_feature"])
    original = eg.signed_tape_accel_features
    monkeypatch.setattr(eg, "signed_tape_accel_features", lambda *a, **k: None)
    _tick(env, le, seconds=36)
    assert state(le)["previous_feature"] == prior
    monkeypatch.setattr(eg, "signed_tape_accel_features", original)
    monkeypatch.setattr(eg, "leg_prints_since_high", lambda *a, **k: None)
    _tick(env, le, seconds=36)
    last = env.events("live_exit_evaluation")[-1]
    assert last["result"]["unreadable"] == "error"
    assert last["feature_pointer_advanced"] is True
    assert state(le)["previous_feature"]["evaluation_id"] == last["evaluation_id"]


def test_exitpending_and_recycle_do_not_inherit_other_leg_feature(monkeypatch):
    env, le, _ = setup(monkeypatch)
    _tick(env, le, seconds=36)
    prior = deepcopy(state(le)["previous_feature"])
    old_leg = state(le)["leg_key"]
    le["exit_verdict"]["phase"] = "exit_pending"
    le["pending_exit_reason"] = "tick_deadman_stop"
    assert _tick(env, le, seconds=37) == {"action": None, "phase": "exit_pending"}
    assert state(le)["previous_feature"] == prior
    pending = env.events("live_exit_evaluation")[-1]
    assert all(value["status"] == "not_reached" for value in pending["read_roles"].values())
    lr._clear_position_entry_anchor(le)
    le.pop("pending_exit_reason")
    le["entry_filled_at_utc"] = (T_ENTRY + timedelta(seconds=35)).isoformat()
    _tick(env, le, seconds=37)
    recycled = env.events("live_exit_evaluation")[-1]
    assert recycled["leg_key"] != old_leg
    assert recycled["previous_feature"]["status"] == "prior_receipt_unknown"
    assert recycled["previous_evaluation"] is None


def test_decorated_and_unwrapped_strategy_results_match(monkeypatch):
    env, le, _ = setup(monkeypatch)
    plain_le = deepcopy(le)
    observed = _tick(env, le, seconds=36)
    # the SAME inputs `_tick` passes: the position's stop ([65] review: the receipt reports it
    # as `stop_price_now`, so a different stop would be a different input, not a different strategy)
    plain = lr._exit_verdict_tick.__wrapped__(
        _DB, _sess(), plain_le, as_of=env.now, bid=10.1, ask=10.11, mid=10.105,
        qty=10., avg=10., stop_px=plain_le["position"]["stop_price"], prod=PROD)
    # The existing receipt gains an invocation link; the strategy shape/math is unchanged.
    observed["receipt"].pop("evaluation_id")
    plain["receipt"].pop("evaluation_id")
    assert observed == plain
    le["exit_verdict"].pop("evaluation_audit")
    assert le == plain_le


def test_original_exception_is_preserved_and_context_resets_after_receipt(monkeypatch):
    env, le, _ = setup(monkeypatch)
    failure = RuntimeError("original strategy failure")

    def fail(*a, **k):
        raise failure

    monkeypatch.setattr(eg, "leg_prints_between", fail)
    with pytest.raises(RuntimeError) as caught:
        _tick(env, le, seconds=36)
    assert caught.value is failure
    row, = env.events("live_exit_evaluation")
    assert row["error_type"] == "RuntimeError" and row["result"] is None
    assert audit.current_evaluation_id() is None


@pytest.mark.parametrize("phase", ["exited", "unknown_entry_anchor"])
def test_early_return_still_has_one_evaluation_without_source_membership(monkeypatch, phase):
    env, le, _ = setup(monkeypatch)
    if phase == "exited":
        le["exit_verdict"] = {"phase": "exited"}
    else:
        le.pop("entry_filled_at_utc")
    assert _tick(env, le, seconds=36) is None
    row, = env.events("live_exit_evaluation")
    assert row["reads"] == [] and row["feature_pointer_advanced"] is False
    assert all(v["status"] == "not_reached" for v in row["read_roles"].values())
