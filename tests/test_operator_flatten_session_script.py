"""scripts/operator_flatten_session.py — the operator flatten THROUGH the FSM.

2026-09-02 CANF 19471: the FSM's own entry (f3ed508d, 165 @ 4.62) filled while
the session sat paused in live_pending_entry; the operator sold OUTSIDE the FSM
(chili_ops_flat_19471_...) and the ledger booked only an unpriced leg
(−108.85 invisible to the loss guard). The script's contract: dry-run by
default, ONE key written (``operator_flatten_requested_utc``) on a locked,
re-checked row, then WAIT for the lane's tick to emit
``operator_flatten_executed``, then ``stop_automation_session``. Never places
or cancels an order, never clears the pause, never pops entry keys.

DB-free: the script is loaded from its path and every DB/broker seam is faked.
Runnable: pytest tests/test_operator_flatten_session_script.py -v
"""
from __future__ import annotations

import ast
import importlib.util
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = REPO_ROOT / "scripts" / "operator_flatten_session.py"


@pytest.fixture(scope="module")
def mod():
    spec = importlib.util.spec_from_file_location("operator_flatten_session", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _sess(state="live_pending_entry", *, le=None, family="alpaca_spot", mode="live",
          user_id=1, sid=19471, paused=True):
    snap = {
        "alpaca_account_scope": "alpaca:paper",
        "momentum_live_execution": dict(le or {}),
    }
    if paused:
        snap["operator_pause"] = {"active": True, "paused_at_utc": "x", "resume_state": state}
    return SimpleNamespace(
        id=sid, symbol="CANF", mode=mode, state=state, execution_family=family,
        user_id=user_id, risk_snapshot_json=snap, correlation_id="c", updated_at=None,
        started_at=None, ended_at=None,
    )


# ── eligibility ──────────────────────────────────────────────────────────────


def test_the_19471_shape_is_eligible_on_a_running_lane(mod):
    """Paused live_pending_entry with a submitted (filled) entry and no position."""
    s = _sess(le={"entry_submitted": True, "entry_order_id": "f3ed508d"})
    ok, reasons = mod.evaluate_eligibility(
        s, quarantine_reason=None, lane_ok=True, allow_lane_down=False, inline_tick=False)
    assert ok, reasons


@pytest.mark.parametrize("state", ["live_entered", "live_scaling_out", "live_trailing",
                                   "live_bailout", "live_pending_entry"])
def test_every_held_and_pending_state_is_flattenable(mod, state):
    ok, reasons = mod.evaluate_eligibility(
        _sess(state), quarantine_reason=None, lane_ok=True, allow_lane_down=False, inline_tick=False)
    assert ok, reasons


@pytest.mark.parametrize("state,expect", [
    ("watching_live", "state_not_flattenable:watching_live"),
    ("live_exited", "state_not_flattenable:live_exited"),
    ("live_cancelled", "already_terminal:live_cancelled"),
    ("live_finished", "already_terminal:live_finished"),
])
def test_non_flattenable_states_are_refused(mod, state, expect):
    ok, reasons = mod.evaluate_eligibility(
        _sess(state), quarantine_reason=None, lane_ok=True, allow_lane_down=False, inline_tick=False)
    assert not ok and expect in reasons


def test_refusals_paper_family_quarantine_userless(mod):
    ok, r = mod.evaluate_eligibility(_sess(mode="paper"), quarantine_reason=None, lane_ok=True,
                                     allow_lane_down=False, inline_tick=False)
    assert not ok and "mode_not_live:paper" in r
    ok, r = mod.evaluate_eligibility(_sess(family="coinbase_spot"), quarantine_reason=None, lane_ok=True,
                                     allow_lane_down=False, inline_tick=False)
    assert not ok and "execution_family_not_alpaca:coinbase_spot" in r
    ok, r = mod.evaluate_eligibility(_sess(), quarantine_reason="alpaca_short_execution_not_certified",
                                     lane_ok=True, allow_lane_down=False, inline_tick=False)
    assert not ok and "execution_quarantined:alpaca_short_execution_not_certified" in r
    ok, r = mod.evaluate_eligibility(_sess(user_id=None), quarantine_reason=None, lane_ok=True,
                                     allow_lane_down=False, inline_tick=False)
    assert not ok and "user_id_missing_stop_would_be_refused" in r
    ok, r = mod.evaluate_eligibility(None, quarantine_reason=None, lane_ok=True,
                                     allow_lane_down=False, inline_tick=False)
    assert not ok and r == ["session_not_found"]


def test_lane_down_needs_an_explicit_choice(mod):
    """The tick is the only executor: a dead lane means nothing happens unless
    the operator parks the request (--allow-lane-down) or ticks inline."""
    s = _sess()
    ok, r = mod.evaluate_eligibility(s, quarantine_reason=None, lane_ok=False,
                                     allow_lane_down=False, inline_tick=False)
    assert not ok and "lane_down_use_allow_lane_down_or_inline_tick" in r
    ok, r = mod.evaluate_eligibility(s, quarantine_reason=None, lane_ok=False,
                                     allow_lane_down=True, inline_tick=False)
    assert ok, r
    ok, r = mod.evaluate_eligibility(s, quarantine_reason=None, lane_ok=False,
                                     allow_lane_down=False, inline_tick=True)
    assert ok, r


def test_inline_tick_is_refused_while_a_lane_heartbeat_is_fresh(mod):
    """Two tickers on one row is the reaper-race shape all over again."""
    ok, r = mod.evaluate_eligibility(_sess(), quarantine_reason=None, lane_ok=True,
                                     allow_lane_down=False, inline_tick=True)
    assert not ok and "inline_tick_refused_lane_heartbeat_fresh" in r
    ok, r = mod.evaluate_eligibility(_sess(), quarantine_reason=None, lane_ok=None,
                                     allow_lane_down=False, inline_tick=True)
    assert not ok and "inline_tick_refused_lane_driver_not_event_loop" in r


# ── the single-key write ─────────────────────────────────────────────────────


def test_apply_flatten_request_adds_only_the_key_and_is_idempotent(mod):
    now = datetime(2026, 9, 2, 13, 20, 0)
    before = {"entry_submitted": True, "entry_order_id": "f3ed508d", "position": None,
              "entry_client_order_id": "chili_ml_e_19471_x"}
    after = mod.apply_flatten_request(before, now=now)
    assert mod.diff_keys(before, after) == {"operator_flatten_requested_utc"}
    assert after["operator_flatten_requested_utc"] == "2026-09-02T13:20:00"
    again = mod.apply_flatten_request(after, now=datetime(2030, 1, 1))
    assert again == after, "an existing request is never overwritten"
    assert before.get("operator_flatten_requested_utc") is None, "input not mutated"


class _Db:
    def __init__(self):
        self.calls: list[str] = []

    def commit(self):
        self.calls.append("commit")

    def rollback(self):
        self.calls.append("rollback")


def _wire_request(mod, monkeypatch, sess, events):
    monkeypatch.setattr(mod, "load_session", lambda db, sid, for_update=False: (
        events.append(("load", sid, for_update)) or sess))
    import sqlalchemy.orm.attributes as _attrs
    monkeypatch.setattr(_attrs, "flag_modified", lambda obj, key: events.append(("flag", key)))
    from app.services.trading.momentum_neural import persistence as _p
    monkeypatch.setattr(_p, "append_trading_automation_event",
                        lambda db, sid, et, payload, **kw: events.append(("event", et, payload, kw)))


def test_request_flatten_locks_rechecks_writes_one_key_and_commits(mod, monkeypatch):
    events: list = []
    s = _sess(le={"entry_submitted": True, "entry_order_id": "f3ed508d", "position": None})
    _wire_request(mod, monkeypatch, s, events)
    db = _Db()
    out = mod.request_flatten(db, 19471, now=datetime(2026, 9, 2, 13, 20), actor="test")
    assert out == {"ok": True, "already_requested": False,
                   "requested_utc": "2026-09-02T13:20:00", "state": "live_pending_entry"}
    assert ("load", 19471, True) in events, "row must be locked FOR UPDATE"
    le = s.risk_snapshot_json["momentum_live_execution"]
    assert le["operator_flatten_requested_utc"] == "2026-09-02T13:20:00"
    assert le["entry_submitted"] is True and le["entry_order_id"] == "f3ed508d"
    assert "position" in le and le["position"] is None, "entry/position keys untouched"
    assert s.risk_snapshot_json["operator_pause"]["active"] is True, "pause NEVER cleared"
    assert ("flag", "risk_snapshot_json") in events
    ev = [e for e in events if e[0] == "event"]
    assert len(ev) == 1 and ev[0][1] == "operator_flatten_requested"
    assert ev[0][2]["by"] == "test"
    assert ev[0][3]["source_node_id"] == "operator_flatten_session_script"
    assert db.calls == ["commit"]


def test_request_flatten_is_a_no_write_when_already_requested(mod, monkeypatch):
    events: list = []
    s = _sess(le={"operator_flatten_requested_utc": "2026-09-02T13:00:00"})
    _wire_request(mod, monkeypatch, s, events)
    db = _Db()
    out = mod.request_flatten(db, 19471, now=datetime(2026, 9, 2, 13, 20), actor="test")
    assert out["ok"] and out["already_requested"] and out["requested_utc"] == "2026-09-02T13:00:00"
    assert db.calls == ["rollback"] and not [e for e in events if e[0] == "event"]


def test_request_flatten_guard_fails_on_the_locked_row(mod, monkeypatch):
    """The state may have changed between the dry-run read and the lock."""
    events: list = []
    s = _sess(state="live_cancelled")
    _wire_request(mod, monkeypatch, s, events)
    db = _Db()
    out = mod.request_flatten(db, 19471, now=datetime.utcnow(), actor="test")
    assert not out["ok"] and out["error"] == "guard_failed"
    assert "already_terminal:live_cancelled" in out["reasons"]
    assert db.calls == ["rollback"]
    assert "operator_flatten_requested_utc" not in s.risk_snapshot_json["momentum_live_execution"]


def test_request_flatten_not_found(mod, monkeypatch):
    monkeypatch.setattr(mod, "load_session", lambda db, sid, for_update=False: None)
    db = _Db()
    assert mod.request_flatten(db, 1, now=datetime.utcnow(), actor="t") == {"ok": False, "error": "not_found"}
    assert db.calls == ["rollback"]


# ── waiting for the tick ─────────────────────────────────────────────────────


def _ev(i, et):
    return SimpleNamespace(id=i, ts=None, event_type=et, payload_json={})


def test_classify_events(mod):
    assert mod.classify_events([]) is None
    assert mod.classify_events([_ev(1, "live_order_cancelled")]) is None
    assert mod.classify_events([_ev(1, "operator_flatten_pending")]) == "pending"
    assert mod.classify_events([_ev(1, "operator_flatten_pending"), _ev(2, "operator_flatten_executed")]) == "executed"
    assert mod.classify_events([{"event_type": "operator_flatten_executed"}]) == "executed"


def test_wait_for_execution_sees_the_recipe_chain_then_stops_polling(mod, monkeypatch):
    """The 19471 recipe: live_order_cancelled (422 no-op) -> live_emergency_exit_
    unpriced (broker zero) -> operator_flatten_executed."""
    polls = [
        [],
        [_ev(11, "live_order_cancelled")],
        [_ev(12, "live_emergency_exit_unpriced"), _ev(13, "operator_flatten_executed")],
        [_ev(14, "should_never_be_read")],
    ]
    seen_after: list = []

    def _recent(db, sid, *, limit=15, after_id=None):
        seen_after.append(after_id)
        return polls.pop(0)

    monkeypatch.setattr(mod, "recent_events", _recent)
    monkeypatch.setattr(mod, "load_session", lambda db, sid, for_update=False: _sess())
    clock = iter([0.0, 1.0, 2.0, 3.0, 4.0])
    slept: list[float] = []
    status, last = mod.wait_for_execution(
        _Db(), 19471, after_event_id=10, wait_seconds=60, poll_seconds=2,
        sleep=slept.append, clock=lambda: next(clock))
    assert status == "executed" and last == 13
    assert seen_after == [10, 10, 11], "each poll continues from the last event id"
    assert len(polls) == 1, "polling stops on operator_flatten_executed"
    assert slept == [2.0, 2.0]


def test_wait_for_execution_times_out_with_pending(mod, monkeypatch):
    monkeypatch.setattr(mod, "recent_events",
                        lambda db, sid, *, limit=15, after_id=None: [_ev(21, "operator_flatten_pending")] if after_id == 20 else [])
    monkeypatch.setattr(mod, "load_session", lambda db, sid, for_update=False: _sess())
    t = iter([0.0, 5.0, 100.0, 200.0])
    status, last = mod.wait_for_execution(
        _Db(), 19471, after_event_id=20, wait_seconds=90, poll_seconds=2,
        sleep=lambda s: None, clock=lambda: next(t))
    assert status == "pending" and last == 21


def test_wait_for_execution_returns_terminal_when_the_row_terminalizes(mod, monkeypatch):
    monkeypatch.setattr(mod, "recent_events", lambda db, sid, *, limit=15, after_id=None: [])
    monkeypatch.setattr(mod, "load_session", lambda db, sid, for_update=False: _sess(state="live_cancelled"))
    status, _ = mod.wait_for_execution(
        _Db(), 19471, after_event_id=None, wait_seconds=10, poll_seconds=1,
        sleep=lambda s: None, clock=lambda: 0.0)
    assert status == "terminal"


def test_stop_outcome_exit_codes(mod):
    assert mod.stop_outcome_exit_code({"ok": True, "state": "live_cancelled"}) == 0
    assert mod.stop_outcome_exit_code({"ok": True, "pending": "broker_flat_confirmation"}) == 2
    assert mod.stop_outcome_exit_code({"ok": False, "error": "broker_flat_unconfirmed"}) == 1


# ── CLI: dry-run by default ──────────────────────────────────────────────────


def test_default_is_dry_run_and_flag_shapes(mod):
    args = mod.build_parser().parse_args(["--session-id", "19471"])
    assert args.execute is False and args.stop_only is False and args.inline_tick is False
    assert args.wait_seconds == 90 and args.allow_lane_down is False


def test_inline_tick_without_execute_is_refused_before_any_db(mod, monkeypatch):
    monkeypatch.setattr(mod, "open_db", lambda: (_ for _ in ()).throw(AssertionError("db opened")))
    assert mod.main(["--session-id", "1", "--inline-tick"]) == 1
    assert mod.main(["--session-id", "1", "--stop-only", "--execute"]) == 1


def _wire_main(mod, monkeypatch, sess, *, lane_ok=True, request_result=None,
               wait_status="executed", stop_result=None, log=None):
    log = log if log is not None else []
    monkeypatch.setattr(mod, "open_db", lambda: _Db())
    monkeypatch.setattr(mod, "load_session", lambda db, sid, for_update=False: sess)
    monkeypatch.setattr(mod, "lane_status", lambda db: {"ok": lane_ok, "reason": None if lane_ok else "live_runner_loop_heartbeat_stale"})
    monkeypatch.setattr(mod, "read_claim", lambda db, s: {"readable": True, "claim": None})
    monkeypatch.setattr(mod, "read_outcome", lambda db, sid: None)
    monkeypatch.setattr(mod, "recent_events", lambda db, sid, *, limit=15, after_id=None: [_ev(5, "live_entry_pending_place")])
    monkeypatch.setattr(mod, "broker_truth", lambda s, le: (log.append("broker_read") or {"position_quantity": 0.0}))
    monkeypatch.setattr(mod, "pause_info_of", lambda s: {"active": True})
    from app.services.trading.momentum_neural import operator_actions as _oa
    monkeypatch.setattr(_oa, "_persisted_alpaca_execution_quarantine_reason", lambda s: None)
    monkeypatch.setattr(mod, "request_flatten", lambda db, sid, *, now, actor: (
        log.append(("request", sid)) or (request_result or {"ok": True, "already_requested": False, "requested_utc": "x", "state": sess.state})))
    monkeypatch.setattr(mod, "wait_for_execution", lambda db, sid, **kw: (log.append(("wait", sid)) or (wait_status, 9)))
    monkeypatch.setattr(mod, "run_inline_ticks", lambda db, sid, *, max_ticks, sleep=None: (log.append(("inline", sid, max_ticks)) or "executed"))
    monkeypatch.setattr(mod, "stop_session", lambda db, s: (log.append(("stop", s.id)) or (stop_result or {"ok": True, "state": "live_cancelled"})))
    return log


def test_main_dry_run_reads_everything_and_writes_nothing(mod, monkeypatch, capsys):
    s = _sess(le={"entry_submitted": True, "entry_order_id": "f3ed508d"})
    log = _wire_main(mod, monkeypatch, s)
    assert mod.main(["--session-id", "19471"]) == 0
    assert log == ["broker_read"], "no request, no wait, no stop"
    out = capsys.readouterr().out
    assert "DRY-RUN" in out and '"ok": true' in out


def test_main_skip_broker_performs_no_broker_get(mod, monkeypatch):
    log = _wire_main(mod, monkeypatch, _sess())
    assert mod.main(["--session-id", "19471", "--skip-broker"]) == 0
    assert log == []


def test_main_execute_happy_path_request_wait_stop(mod, monkeypatch):
    s = _sess(le={"entry_submitted": True, "entry_order_id": "f3ed508d"})
    log = _wire_main(mod, monkeypatch, s)
    assert mod.main(["--session-id", "19471", "--execute", "--skip-broker"]) == 0
    assert log == [("request", 19471), ("wait", 19471), ("stop", 19471)]


def test_main_execute_no_stop_flag(mod, monkeypatch):
    log = _wire_main(mod, monkeypatch, _sess())
    assert mod.main(["--session-id", "19471", "--execute", "--skip-broker", "--no-stop"]) == 0
    assert log == [("request", 19471), ("wait", 19471)]


def test_main_execute_wait_timeout_leaves_key_and_exits_4(mod, monkeypatch):
    log = _wire_main(mod, monkeypatch, _sess(), wait_status="pending")
    assert mod.main(["--session-id", "19471", "--execute", "--skip-broker"]) == 4
    assert ("stop", 19471) not in log, "never terminalize an un-flattened session"


def test_main_execute_deferred_stop_exits_2(mod, monkeypatch):
    log = _wire_main(mod, monkeypatch, _sess(), stop_result={"ok": True, "pending": "broker_flat_confirmation"})
    assert mod.main(["--session-id", "19471", "--execute", "--skip-broker"]) == 2
    assert log[-1] == ("stop", 19471)


def test_main_execute_lane_down_is_refused_without_allow(mod, monkeypatch):
    log = _wire_main(mod, monkeypatch, _sess(), lane_ok=False)
    assert mod.main(["--session-id", "19471", "--execute", "--skip-broker"]) == 1
    assert log == []


def test_main_execute_lane_down_parks_the_request_with_allow(mod, monkeypatch, capsys):
    log = _wire_main(mod, monkeypatch, _sess(), lane_ok=False)
    assert mod.main(["--session-id", "19471", "--execute", "--skip-broker", "--allow-lane-down"]) == 3
    assert log == [("request", 19471)], "parked: no wait, no stop"
    assert "PARKED" in capsys.readouterr().out


def test_main_execute_inline_tick_only_when_lane_down(mod, monkeypatch):
    log = _wire_main(mod, monkeypatch, _sess(), lane_ok=False)
    assert mod.main(["--session-id", "19471", "--execute", "--skip-broker", "--inline-tick", "--inline-ticks", "3"]) == 0
    assert log == [("request", 19471), ("inline", 19471, 3), ("stop", 19471)]
    log = _wire_main(mod, monkeypatch, _sess(), lane_ok=True)
    assert mod.main(["--session-id", "19471", "--execute", "--skip-broker", "--inline-tick"]) == 1
    assert log == []


def test_main_stop_only(mod, monkeypatch):
    log = _wire_main(mod, monkeypatch, _sess(state="live_exited"))
    assert mod.main(["--session-id", "19471", "--stop-only", "--skip-broker"]) == 0
    assert log == [("stop", 19471)]


def test_main_refuses_ineligible_on_execute(mod, monkeypatch):
    log = _wire_main(mod, monkeypatch, _sess(state="watching_live"))
    assert mod.main(["--session-id", "19471", "--execute", "--skip-broker"]) == 1
    assert log == []


# ── AST guards: the script never acts at the broker or erodes the recipe ─────


def _calls(tree):
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            if isinstance(f, ast.Attribute):
                out.add(f.attr)
            elif isinstance(f, ast.Name):
                out.add(f.id)
    return out


def test_script_never_places_cancels_or_closes_at_the_broker():
    tree = ast.parse(SCRIPT_PATH.read_text(encoding="utf-8"))
    calls = _calls(tree)
    for forbidden in ("place_order", "cancel_order", "close_position", "submit_order",
                      "cancel_all_orders", "close_all_positions", "sell_to_close",
                      "place_limit_order", "place_market_order", "flatten_position"):
        assert forbidden not in calls, forbidden
    allowed_broker = {"get_position_quantity", "get_order_truth",
                      "get_order_by_client_order_id_truth", "list_open_orders"}
    adapter_calls = {c for c in calls if c.startswith(("get_", "list_"))} - {
        "get", "getattr", "get_position_quantity", "get_order_truth",
        "get_order_by_client_order_id_truth", "list_open_orders"}
    assert not adapter_calls, adapter_calls
    assert allowed_broker <= calls


def test_script_never_clears_pause_pops_entry_keys_or_writes_a_position():
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "clear_operator_pause" not in src
    assert 'pop("entry_' not in src and "pop('entry_" not in src
    assert '["position"] =' not in src
    tree = ast.parse(src)
    # The envelope writer assigns exactly ONE subscript: the flatten key. And no
    # other function in the script writes into a `le`/`snap` envelope subscript
    # except request_flatten's `snap[LIVE_EXEC_KEY] = after` re-attachment.
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}
    assigned = []
    for node in ast.walk(fns["apply_flatten_request"]):
        if isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Subscript):
                    assigned.append(ast.unparse(t.slice))
    assert assigned == ["FLATTEN_KEY"], assigned
    envelope_writes = []
    for name, fn in fns.items():
        for node in ast.walk(fn):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Subscript) and isinstance(t.value, ast.Name) and t.value.id in ("le", "snap", "after", "before"):
                        envelope_writes.append((name, ast.unparse(t)))
    assert envelope_writes == [("request_flatten", "snap[LIVE_EXEC_KEY]")], envelope_writes


def test_script_locks_the_row_and_terminalizes_only_via_stop_automation_session():
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    assert "with_for_update(nowait=True)" in src
    assert src.count("stop_automation_session(") == 1
    assert "SET statement_timeout" in src
    assert "_safe_transition" not in src and ".state = " not in src


def test_script_imports_no_secret_surface():
    src = SCRIPT_PATH.read_text(encoding="utf-8")
    for token in ("chili_alpaca_api_key", "chili_alpaca_api_secret", "os.environ["):
        assert token not in src, token
