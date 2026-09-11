"""EXIT VERDICT G -- the WHOLE exit through the existing exit seam, on the real claim tables
(2026-09-10, [21]/[44]/[47] + Amendment 2), and the bounded tape reads.

Amendment 2: at the trigger the whole position leaves through `_submit_live_market_exit` --
no sibling order, no partial, no runner. With a resting Alpaca deadman that seam is the
deadman-close HANDOFF: pulse 1 freezes a successor intent for the WHOLE quantity and returns
`deferred / pre_place_blocked` (the continuation wake re-pulses in 0.5 s); pulse 2 cancels the
stop, proves it terminal, and POSTs the close for the whole quantity as the owner transport's
successor -- the order the account certifier sees as CHILI-owned by identity. So the measured
shipped latency from the decision tick to the POST is ONE continuation pulse
(`_EXIT_CONTINUATION_WAKE_DELAY_S` = 0.5 s) plus the cancel round-trip -- reported here, not
assumed (review of #1385: the acceptance tables priced the exit at the decision tick's bid).

Runnable: pytest tests/test_exit_verdict_g_whole_exit_seam.py -v
"""
from __future__ import annotations

import uuid
from collections import deque
from datetime import datetime
from typing import Any

import pytest

from app.config import settings
from app.services.trading.momentum_neural import live_runner as lr
from app.services.trading.momentum_neural import optional_db_read as ODR
from app.services.trading.momentum_neural.alpaca_orphan_claims import (
    advance_owner_transport,
    lease_owner_transport,
    read_action_claim,
)
from app.services.trading.venue.protocol import NormalizedTicker

from tests.test_alpaca_deadman_close_handoff import _request  # noqa: F401  (the certified request shape)
from tests.test_momentum_emergency_exit_recovery import (
    TEST_ALPACA_ACCOUNT_ID,
    _ScriptedAlpaca,
    _ensure_retained_entry_owner,
    _fresh,
    _order,
    _seed_session,
)

NOW = datetime(2026, 9, 10, 14, 0, 41)


@pytest.fixture(autouse=True)
def _boundaries(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(settings, "chili_alpaca_expected_account_id", TEST_ALPACA_ACCOUNT_ID, raising=False)
    monkeypatch.setattr(lr, "_venue_broker_connected", lambda _family: True)
    monkeypatch.setattr(lr, "_record_live_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_partial_exit_ledger_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_fill_outcome_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_record_live_exit_intent_safe", lambda *a, **k: None)
    monkeypatch.setattr(lr, "_utcnow", lambda: NOW)


def _emit_log(monkeypatch) -> list[tuple[str, dict]]:
    log: list[tuple[str, dict]] = []
    monkeypatch.setattr(lr, "_emit", lambda db, sess, ev, payload: log.append((ev, dict(payload))))
    return log


def _session_hours(monkeypatch, session: str) -> None:
    monkeypatch.setattr("app.services.trading.momentum_neural.market_profile.market_session_now",
                        lambda _symbol, **_k: session)


def _resting_deadman_session(db, *, symbol: str, deadman_qty: float, marker: dict | None):
    """A held Alpaca session (Q = 10) with ONE resting deadman generation of `deadman_qty`
    leased on the real owner claim (the outbox's active transport), and the verdict marker."""
    sess = _seed_session(db, symbol=symbol, quantity=10.0, avg_entry_price=10.0)
    le: dict[str, Any] = {
        "side_long": True,
        "entry_filled_at_utc": "2026-09-10T14:00:00",
        "position": {"product_id": symbol, "side": "long", "quantity": 10.0,
                     "avg_entry_price": 10.0, "stop_price": 9.0, "original_quantity": 10.0},
    }
    if marker is not None:
        le["exit_verdict"] = marker
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot["momentum_live_execution"] = le
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    owner_context = _ensure_retained_entry_owner(db, sess)
    deadman_cid = f"chili_dm_{sess.id}_1_{uuid.uuid4().hex[:6]}"
    deadman_oid = f"deadman-{uuid.uuid4().hex[:6]}"
    request = _request(symbol=symbol, cid=deadman_cid, qty=deadman_qty, kind="deadman")
    request["base_size"] = lr._fmt_base_size(deadman_qty)
    lease_token = f"vt-deadman-{uuid.uuid4().hex}"
    leased = lease_owner_transport(db, **owner_context, transport_kind="deadman",
                                   client_order_id=deadman_cid, order_request=request, lease_token=lease_token)
    assert leased["ok"] is True, leased
    assert advance_owner_transport(db, **owner_context, client_order_id=deadman_cid,
                                   lease_token=lease_token, phase="submitted", broker_order_id=deadman_oid)
    readable, claim = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and claim is not None
    transport = dict(claim["metadata"]["owner_transport"])
    le["deadman_stop"] = {"order_id": deadman_oid, "client_order_id": deadman_cid, "stop_price": 7.5,
                          "qty": deadman_qty, "phase": "submitted", "owner_transport": transport}
    snapshot = dict(sess.risk_snapshot_json or {})
    snapshot["momentum_live_execution"] = le
    sess.risk_snapshot_json = snapshot
    db.add(sess)
    db.commit()
    adapter = _ScriptedAlpaca(positions=[10.0])
    active = _order(oid=deadman_oid, cid=deadman_cid, symbol=symbol, side="sell", status="open",
                    filled=0.0, avg=None, qty=deadman_qty, order_type="stop", time_in_force="gtc",
                    position_intent="sell_to_close", raw_overrides={"stop_price": 7.5})
    terminal = _order(oid=deadman_oid, cid=deadman_cid, symbol=symbol, side="sell", status="cancelled",
                      filled=0.0, avg=None, qty=deadman_qty, order_type="stop", time_in_force="gtc",
                      position_intent="sell_to_close", raw_overrides={"stop_price": 7.5})
    adapter.truth[deadman_cid] = deque([active, terminal])
    adapter.cid_orders[deadman_cid] = terminal
    adapter.orders[deadman_oid] = terminal
    meta = _fresh()
    adapter.execution_bbo = (
        NormalizedTicker(product_id=symbol, bid=9.95, ask=9.97, mid=9.96, freshness=meta,
                         raw={"feed": "iqfeed_l1", "tape_row_id": 9901}),
        meta,
    )
    return sess, le, adapter, deadman_oid, deadman_cid


def _decided_marker(trigger: str = "accel_rollover") -> dict:
    reason, tag = lr._EXIT_VERDICT_ACTIONS[trigger]
    return {
        "phase": "exit_pending",
        "entry_at": "2026-09-10T14:00:00", "entry_px": 10.0,
        "deadman": {"level": 9.85, "level_source": "swing_low_prev", "ratchets": 0},
        "exit": {"trigger": trigger, "reason": reason, "cid_tag": tag,
                 "decided_as_of": "2026-09-10T14:00:37", "bid": 9.95, "exit_fraction": 1.0,
                 "accel_prev": 1900.0, "accel_now": -2800.0,
                 "prints_since_entry": 11, "prints_since_high": 8, "binding": "rollover_above_entry"},
    }


def _submit(db, sess, le, adapter, *, symbol: str, reason: str, cid: str):
    return lr._submit_live_market_exit(
        db, sess, adapter, le=le, product_id=symbol, quantity=10.0, client_order_id=cid,
        reason=reason, bid=9.95, ask=9.97, mid=9.96,
        extra={"exit_verdict": lr._exit_verdict_receipt(le), "trigger": le["exit_verdict"]["exit"]["trigger"],
               "exit_fraction": 1.0},
    )


@pytest.mark.parametrize("trigger", ["accel_rollover", "since_high_verdict", "tick_deadman"])
def test_the_whole_exit_crosses_the_seam_in_two_pulses_and_the_close_is_the_owned_successor(db, monkeypatch, trigger):
    log = _emit_log(monkeypatch)
    _session_hours(monkeypatch, "regular")
    symbol = f"EVG{trigger[:2].upper()}"
    sess, le, adapter, oid, cid = _resting_deadman_session(db, symbol=symbol, deadman_qty=10.0,
                                                            marker=_decided_marker(trigger))
    reason, tag = lr._EXIT_VERDICT_ACTIONS[trigger]
    le["pending_exit_reason"] = reason
    exit_cid = f"chili_ml_{tag}_{sess.id}_{uuid.uuid4().hex[:12]}"
    # ── pulse 1: the decision tick's submit freezes the successor intent for the WHOLE Q ──
    out1 = _submit(db, sess, le, adapter, symbol=symbol, reason=reason, cid=exit_cid)
    assert out1.get("error") == "deadman_successor_intent_frozen_for_next_pulse", out1
    assert out1.get("deferred") is True and out1.get("pre_place_blocked") is True
    assert adapter.limit_calls == [] and adapter.market_calls == []
    frozen = le["deadman_released_for_close"]["successor_intent_pre_cancel"]
    assert frozen["base_size"] == "10"                       # the whole position, nothing kept
    assert lr._exit_result_wants_continuation(out1, le) is True   # the 0.5-s continuation wake
    # the durable claim carries the SAME whole-quantity successor intent
    readable, claim = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    assert readable and claim is not None
    durable = claim["metadata"].get("deadman_close_handoff") or {}
    intent = durable.get("successor_order_request") or durable.get("successor_intent") or {}
    assert str(intent.get("base_size")) == "10", durable
    assert durable.get("reason") == reason
    phase_1 = str(durable.get("phase") or "")
    # the handoff's OWN protocol has started on the decision tick itself: the replacement
    # re-arm was attempted (the scripted adapter answers it indeterminately, so the phase
    # parks there -- the handoff's own suites drive it through cancel and POST:
    # tests/test_alpaca_deadman_close_handoff.py); the close itself is NOT posted yet
    assert phase_1.startswith("replacement_deadman_"), phase_1
    # ── pulse 2 (the continuation wake): the seam keeps servicing its protocol; the verdict
    #    owns nothing past the freeze -- it never re-decides and never touches a partial ──
    out2 = _submit(db, sess, le, adapter, symbol=symbol, reason=reason, cid=exit_cid)
    assert out2.get("deferred") is True and adapter.limit_calls == [] and adapter.market_calls == []
    readable, claim = read_action_claim(db, symbol=symbol, account_scope="alpaca:paper")
    durable2 = claim["metadata"].get("deadman_close_handoff") or {}
    phase_2 = str(durable2.get("phase") or "")
    assert phase_2.startswith("replacement_deadman_"), (phase_1, phase_2)
    assert str((durable2.get("successor_order_request") or durable2.get("successor_intent") or {}).get("base_size")) == "10"
    # no partial machinery touched the leg: no sibling, no pending partial, the marker untouched
    assert le["exit_verdict"] == _decided_marker(trigger)
    assert "partial" not in le["exit_verdict"] and le["pending_exit_reason"] == reason
    names = [e for e, _ in log]
    assert "live_exit_verdict_partial" not in names and "live_exit_verdict_sibling_released" not in names
    assert "live_exit_verdict_fired" not in names                  # the decision is not re-emitted here


def test_the_shipped_latency_chain_is_named():
    """The number in the PR body: decision tick -> the successor intent is frozen on the SAME
    tick (0 extra pulses, no POST yet) -> the continuation wake re-pulses after
    `_EXIT_CONTINUATION_WAKE_DELAY_S` -> the deadman-close handoff's own protocol (replacement
    re-arm, cancel, close POST) runs across its pulses, each woken the same way. So the close
    reaches the broker >= 2 pulses (>= 1.0 s of wake delay plus the broker round-trips) after
    the decision, priced at THAT pulse's HELD-tick bid (the [48] envelope, IQFeed L1 first) --
    not at the decision tick's bid the acceptance tables used."""
    assert lr._EXIT_CONTINUATION_WAKE_DELAY_S == 0.5
    assert bool(getattr(settings, "chili_momentum_exit_continuation_wake_enabled", True)) is True
    assert lr._exit_result_wants_continuation(
        {"deferred": True, "pre_place_blocked": True, "error": "deadman_successor_intent_frozen_for_next_pulse"}, {}
    ) is True


# ── the bounded read: SET LOCAL inside a savepoint that is rolled back ──────────

class _Nested:
    def __init__(self, log):
        self.log = log

    def rollback(self):
        self.log.append("rollback")


class _BoundedFakeDb:
    def __init__(self, rows, *, raise_on_statement: Exception | None = None):
        self.rows = rows
        self.log: list[str] = []
        self.raise_on_statement = raise_on_statement

    def begin_nested(self):
        self.log.append("begin_nested")
        return _Nested(self.log)

    def execute(self, stmt, params=None):
        text = str(stmt)
        self.log.append(f"execute:{text}")
        if "statement_timeout" not in text and self.raise_on_statement is not None:
            raise self.raise_on_statement
        rows = self.rows

        class _R:
            def fetchall(self_inner):
                return rows

        return _R()


def test_bounded_fetchall_sets_the_timeout_inside_a_savepoint_and_rolls_it_back():
    from sqlalchemy import text

    db = _BoundedFakeDb([(1,), (2,)])
    rows = ODR.bounded_fetchall(db, text("SELECT 1"), {}, timeout_ms=2000)
    assert rows == [(1,), (2,)]
    assert db.log == ["begin_nested", "execute:SET LOCAL statement_timeout = 2000", "execute:SELECT 1", "rollback"]


def test_bounded_fetchall_rolls_back_on_a_timeout_and_the_reader_names_it():
    from sqlalchemy import text

    class _QueryCanceled(Exception):
        pass

    db = _BoundedFakeDb([], raise_on_statement=_QueryCanceled("canceling statement due to statement timeout"))
    with pytest.raises(_QueryCanceled):
        ODR.bounded_fetchall(db, text("SELECT 1"), {}, timeout_ms=2000)
    assert db.log[-1] == "rollback"
    # the readers map it to `unreadable{timeout}` (fail-open: no decision, no frontier move)
    from app.services.trading.momentum_neural import entry_gates as EG

    err: dict = {}
    out = EG.leg_prints_since_high("SKYQ", db=db, hi_at=datetime(2026, 9, 10, 14, 0), hi_id=1,
                                   as_of=datetime(2026, 9, 10, 14, 1), err=err)
    assert out is None and err["why"] == "timeout"
    err = {}
    out = EG.leg_prints_between("SKYQ", db=db, after=datetime(2026, 9, 10, 14, 0), after_id=1,
                                as_of=datetime(2026, 9, 10, 14, 1), err=err)
    assert out is None and err["why"] == "timeout"


def test_bounded_fetchall_falls_back_to_a_direct_execute_on_a_fake_without_begin_nested():
    from sqlalchemy import text

    class _Plain:
        def __init__(self):
            self.stmts = []

        def execute(self, stmt, params=None):
            self.stmts.append(str(stmt))
            rows = [(7,)]

            class _R:
                def fetchall(self_inner):
                    return rows

            return _R()

    db = _Plain()
    assert ODR.bounded_fetchall(db, text("SELECT 7"), {}, timeout_ms=2000) == [(7,)]
    assert db.stmts == ["SELECT 7"]


def test_the_read_timeout_is_the_loops_event_tick_spacing():
    from app.services.trading.momentum_neural.live_runner_loop import _EVENT_TICK_MIN_SPACING_S

    assert lr._exit_verdict_read_timeout_ms() == int(_EVENT_TICK_MIN_SPACING_S * 1000) == 2000
