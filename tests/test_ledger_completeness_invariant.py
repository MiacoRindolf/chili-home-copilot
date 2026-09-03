"""Ledger-completeness invariant: a filled session cannot vanish from the books.

REGRESSION SCOPE. Every test here fails on origin/main at 89cb0eb and passes on
this branch. They pin the four load-bearing facts of the 2026-09-02 ledger census:

  * ``live_arm_expired`` is terminal IN THE LEDGER even though no runner
    transition reaches it. Over 2026-08-12..2026-09-02 it wrote 0 outcome rows out
    of 291 sessions across its entire lifetime; 17 of those had taken real Alpaca
    fills worth −$916.26, including MOVE 19244 at −$364.14 and SSM 19315, which
    reached +3.20R and gave it all back while the operator watched.

  * Making it bookable is NOT sufficient and is dangerous alone. Nine sessions
    were filled by the broker with no fill event, no fill leg and no durable P&L,
    so both entry-occurred predicates read False for them. Booking those naively
    would write nine confidently WRONG ``cancelled_pre_entry`` / $0 rows and make
    the ledger look complete while staying wrong. They must book as UNRECONCILED.

  * Booking failure must not be silent. The emit helper used to swallow every
    exception into ``_log.debug`` and return None, and all four call sites
    discarded the result.

  * The reconciliation must be anchored on the BROKER. The three in-DB sources are
    strictly nested (4 booked-with-P&L ⊂ 12 mfe events ⊂ 18 fill-leg sessions ⊂ 26
    broker fills), so no DB-to-DB cross-check can find the outermost ring.

Uses the truncating ``db`` fixture (TEST_DATABASE_URL, _test DB). No broker calls:
the broker-attribution tests drive the pure episode builder with synthetic orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

import pytest

from app.models.trading import (
    MomentumAutomationOutcome,
    MomentumStrategyVariant,
    TradingAutomationSession,
)
from app.services.trading.momentum_neural import ledger_integrity as li
from app.services.trading.momentum_neural.feedback_emit import (
    LEDGER_INTEGRITY_UNRECONCILED,
    emit_feedback_after_terminal_transition,
    try_emit_momentum_session_feedback,
)
from app.services.trading.momentum_neural.live_fsm import (
    LIVE_LEDGER_TERMINAL_STATES,
    LIVE_RUNNER_TERMINAL_STATES,
    STATE_LIVE_ARM_EXPIRED,
)
from app.services.trading.momentum_neural.outcome_extract import (
    entry_submission_evidence,
    extract_momentum_session_outcome,
    session_terminal_for_feedback,
)

_seq = 0


def _variant(db):
    global _seq
    _seq += 1
    v = MomentumStrategyVariant(
        family="test_family",
        variant_key=f"ledgerinv_{_seq}",
        label="ledger invariant variant",
        params_json={},
    )
    db.add(v)
    db.flush()
    return v


def _session(db, *, state, le=None, symbol="SSM"):
    v = _variant(db)
    sess = TradingAutomationSession(
        user_id=None,
        venue="test",
        execution_family="alpaca_spot",
        mode="live",
        symbol=symbol,
        variant_id=v.id,
        state=state,
        risk_snapshot_json={"momentum_live_execution": dict(le or {})},
        correlation_id="corr-ledgerinv",
    )
    db.add(sess)
    db.flush()
    return sess


def _outcome(db, session_id):
    return (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(session_id))
        .one_or_none()
    )


# ── 1. the terminal vocabulary ─────────────────────────────────────────────


def test_live_arm_expired_is_ledger_terminal_but_not_runner_terminal():
    """One canonical set. The runner never transitions here; the ledger still ends."""
    assert STATE_LIVE_ARM_EXPIRED in LIVE_LEDGER_TERMINAL_STATES
    assert STATE_LIVE_ARM_EXPIRED not in LIVE_RUNNER_TERMINAL_STATES
    assert LIVE_RUNNER_TERMINAL_STATES <= LIVE_LEDGER_TERMINAL_STATES


def test_session_terminal_for_feedback_accepts_live_arm_expired():
    """THE one-line omission that cost the books $916.26 of real fills."""
    assert session_terminal_for_feedback("live", "live_arm_expired") is True
    # unchanged for everything else
    assert session_terminal_for_feedback("live", "live_finished") is True
    assert session_terminal_for_feedback("live", "watching_live") is False
    assert session_terminal_for_feedback("paper", "live_arm_expired") is False


def test_missing_feedback_scanner_state_list_covers_arm_expired():
    """The designated backfill's own hardcoded tuple was blind to the same state."""
    import inspect

    from app.services.trading.momentum_neural import feedback_emit

    src = inspect.getsource(feedback_emit.scan_terminal_sessions_missing_feedback)
    assert "LIVE_LEDGER_TERMINAL_STATES" in src, (
        "the scanner must derive its states from the canonical set, not re-type a tuple"
    )


def test_loss_guard_history_counts_arm_expired_sessions():
    """The guard failed loudly on 2 sessions and SILENTLY on 291. Close the silent one."""
    from app.services.trading.momentum_neural.risk_policy import (
        _loss_history_terminal_states,
    )

    assert "live_arm_expired" in _loss_history_terminal_states()
    assert "live_finished" in _loss_history_terminal_states()


# ── 2. a completed round trip that arm-expired still books ─────────────────


def test_arm_expired_session_with_realized_pnl_books_the_outcome(db):
    """SSM 19315's exact shape: full round trip, recycled, then arm-expired.

    ``le`` carries realized_pnl_usd −25.98 and last_exit_entry_price 4.01 — the
    durable proof of a real fill that survived the recycle — and the session then
    terminalised as live_arm_expired and was never booked. On origin/main this
    returns ``skipped: not_terminal_for_feedback`` and writes nothing.
    """
    sess = _session(
        db,
        state="live_arm_expired",
        le={
            "realized_pnl_usd": -25.98,
            "last_exit_entry_price": 4.01,
            "last_exit_notional_basis_usd": 1736.33,
            "last_exit_reason": "stop",
            "trade_cycles": 1,
            "stopout_cycles": 1,
        },
    )
    result = try_emit_momentum_session_feedback(db, sess)
    db.flush()

    assert result.get("skipped") != "not_terminal_for_feedback"
    assert result.get("emitted") is True

    row = _outcome(db, sess.id)
    assert row is not None, "a filled, arm-expired session must not vanish from the books"
    assert row.realized_pnl_usd == pytest.approx(-25.98, abs=1e-6)
    summary = row.extracted_summary_json or {}
    assert summary.get("ledger_integrity_v1", {}).get("status") == "clean"


def test_arm_expired_booking_is_idempotent(db):
    """Repeat calls must dedupe on UNIQUE(session_id), never double-book."""
    sess = _session(
        db,
        state="live_arm_expired",
        le={"realized_pnl_usd": -4.91, "last_exit_entry_price": 1.65, "last_exit_reason": "stop"},
    )
    first = try_emit_momentum_session_feedback(db, sess)
    db.flush()
    second = try_emit_momentum_session_feedback(db, sess)
    db.flush()

    assert first.get("emitted") is True
    assert second.get("deduped") is True
    assert (
        db.query(MomentumAutomationOutcome)
        .filter(MomentumAutomationOutcome.session_id == int(sess.id))
        .count()
        == 1
    )


# ── 3. the dangerous case: fill never adopted ──────────────────────────────


def test_entry_submission_evidence_detects_an_unadopted_broker_fill():
    """MOVE 19244's shape: an entry order id and a position marker, no fill proof."""
    evidence = entry_submission_evidence(
        {"entry_order_id": "c3a60320-c282-4859-b24b-02ff55730a63", "position": {"quantity": 119}},
        [],
    )
    assert "entry_order_id" in evidence
    assert "position_marker" in evidence
    # A genuinely empty envelope must stay empty — this is what keeps 3,687
    # cancelled-pre-entry rows classified as clean.
    assert entry_submission_evidence({}, []) == []


def test_unadopted_fill_books_as_unreconciled_not_as_a_clean_zero(db):
    """The failure this guard prevents is WORSE than the one it fixes.

    Session 19244 MOVE lost $364.14 — the largest loss of the window — with an
    ``entry_order_id`` in its snapshot and nothing else. Booking it naively writes
    ``cancelled_pre_entry`` / $0 and the ledger LOOKS complete. It must book, so the
    session stops being invisible, but never as an authoritative zero.
    """
    sess = _session(
        db,
        state="live_arm_expired",
        symbol="MOVE",
        le={"entry_order_id": "c3a60320-c282-4859-b24b-02ff55730a63"},
    )
    extracted = extract_momentum_session_outcome(db, sess)
    assert extracted["entry_occurred"] is False
    assert extracted["entry_submission_evidence"], "submission evidence must survive extraction"

    result = try_emit_momentum_session_feedback(db, sess)
    db.flush()
    assert result.get("emitted") is True

    row = _outcome(db, sess.id)
    assert row is not None
    stamp = (row.extracted_summary_json or {}).get("ledger_integrity_v1") or {}
    assert stamp.get("status") == LEDGER_INTEGRITY_UNRECONCILED
    assert "entry_order_id" in (stamp.get("entry_submission_evidence") or [])
    assert row.contributes_to_evolution is False, (
        "an unverified row must never teach the network"
    )


# ── 4. booking failure is loud ─────────────────────────────────────────────


def test_emit_after_terminal_returns_a_result_and_does_not_swallow(db, monkeypatch):
    """The helper used to return None and log at debug. Every call site discarded it."""
    sess = _session(db, state="live_arm_expired", le={"realized_pnl_usd": -1.0})

    def _boom(*_a, **_k):
        raise RuntimeError("booking blew up")

    monkeypatch.setattr(
        "app.services.trading.momentum_neural.feedback_emit.try_emit_momentum_session_feedback",
        _boom,
    )
    result = emit_feedback_after_terminal_transition(db, sess)
    assert isinstance(result, dict), "the result must be observable by the caller"
    assert result.get("ok") is False
    assert result.get("error") == "exception"


def test_emit_after_terminal_records_failure_on_the_session_tape(db, monkeypatch):
    """A swallowed failure must still leave a mark where an operator already looks."""
    from sqlalchemy import text

    sess = _session(db, state="live_arm_expired", le={"realized_pnl_usd": -1.0})
    monkeypatch.setattr(
        "app.services.trading.momentum_neural.feedback_emit.try_emit_momentum_session_feedback",
        lambda *_a, **_k: {"ok": False, "error": "insert_failed", "detail": "boom"},
    )
    emit_feedback_after_terminal_transition(db, sess)
    db.flush()

    n = int(
        db.execute(
            text(
                "SELECT COUNT(*) FROM trading_automation_events "
                "WHERE session_id = :s AND event_type = 'ledger_booking_failed'"
            ),
            {"s": int(sess.id)},
        ).scalar()
        or 0
    )
    assert n == 1


def test_backfill_provenance_repairs_the_books_without_retraining_the_network(db):
    """Repairing the ledger and retraining the network are two different decisions.

    Session 17370 BRNX (a −18.2R loss from 2026-08-27) and 19394 LIDR both extract
    with ``contributes_to_evolution=True``. Backfilling them through the normal path
    would drive ``maybe_pause_symbol_variant_after_losses`` and
    ``maybe_kill_underperforming_variant`` on the LIVE lane from days-old trades — an
    irreversible side effect nobody asked for. The row must still be WRITTEN.
    """
    sess = _session(
        db,
        state="live_arm_expired",
        symbol="BRNX",
        le={
            "realized_pnl_usd": -82.0,
            "last_exit_entry_price": 8.58,
            "last_exit_notional_basis_usd": 171.6,
            "last_exit_reason": "operator_flatten",
        },
    )
    result = try_emit_momentum_session_feedback(
        db, sess, backfill_provenance="ledger_completeness_0902"
    )
    db.flush()

    assert result.get("emitted") is True
    row = _outcome(db, sess.id)
    assert row is not None
    assert row.realized_pnl_usd == pytest.approx(-82.0, abs=1e-6), "the books ARE repaired"
    assert row.contributes_to_evolution is False, "but the network is NOT retrained"
    marker = (row.extracted_summary_json or {}).get("ledger_backfill_v1") or {}
    assert marker.get("provenance") == "ledger_completeness_0902"
    assert marker.get("evolution_suppressed") is True


def test_backfill_row_is_identifiable_and_therefore_reversible(db):
    """Every backfilled row carries provenance, so the write can be undone exactly."""
    sess = _session(db, state="live_arm_expired", le={"realized_pnl_usd": -2.86})
    try_emit_momentum_session_feedback(db, sess, backfill_provenance="ledger_completeness_0902")
    db.flush()

    marked = (
        db.query(MomentumAutomationOutcome)
        .filter(
            MomentumAutomationOutcome.extracted_summary_json["ledger_backfill_v1"][
                "provenance"
            ].astext
            == "ledger_completeness_0902"
        )
        .all()
    )
    assert [int(r.session_id) for r in marked] == [int(sess.id)]


# ── 5. broker-anchored attribution ─────────────────────────────────────────


@dataclass(frozen=True)
class _Order:
    order_id: str
    client_order_id: Optional[str]
    product_id: str
    side: str
    status: str
    filled_size: float
    average_filled_price: Optional[float]
    created_time: str
    order_type: str = "market"
    raw: dict[str, Any] = None  # type: ignore[assignment]


def test_broker_episode_attribution_survives_an_out_of_fsm_exit():
    """MOVE 19244 verbatim: FSM-tagged entry, operator-flatten exit, −$364.14.

    The exit carries no session id — 14 of the 26 filled sessions in the window
    were closed by something outside the FSM, and 6 filled exits carry no CHILI
    identity at all. Keying attribution on the exit would lose exactly the sessions
    that most need finding.
    """
    orders = [
        _Order(
            "c3a60320", "chili_ml_e_19244_2423e8fb_b9937ce87f", "MOVE", "buy",
            "filled", 119.0, 15.05, "2026-08-31T13:16:34Z",
        ),
        _Order(
            "c52de970", "chili_operator_flatten_move2_20260831", "MOVE", "sell",
            "filled", 119.0, 11.99, "2026-08-31T13:46:34Z",
        ),
    ]
    by_session, unattributed = li._build_broker_episodes(orders)
    assert unattributed == []
    assert 19244 in by_session
    assert by_session[19244]["realized_pnl_usd"] == pytest.approx(-364.14, abs=0.01)


def test_broker_episode_splits_two_cycles_of_one_recycled_session():
    """CANF 19471: two round trips under one session id, −78.13 then −108.85.

    ``momentum_automation_outcomes`` has UNIQUE(session_id), so one row structurally
    cannot hold both legs. The broker sum is the only complete number: −186.98.
    """
    orders = [
        _Order("552efe43", "chili_ml_e_19471_a7c3e32c_aaaaaaaaaa", "CANF", "buy",
               "filled", 355.0, 4.34, "2026-09-02T11:10:17Z"),
        _Order("af3a4b0c", "chili_ml_s_19471_a7c3e32c_bbbbbbbbbb", "CANF", "sell",
               "filled", 355.0, 4.119915, "2026-09-02T11:11:04Z"),
        _Order("f3ed508d", "chili_ml_e_19471_a7c3e32c_9738f4d973", "CANF", "buy",
               "filled", 165.0, 4.62, "2026-09-02T11:19:10Z"),
        _Order("ddba3ed2", "chili_ops_flat_19471_d8394610fc", "CANF", "sell",
               "filled", 165.0, 3.960303, "2026-09-02T11:34:30Z"),
    ]
    by_session, unattributed = li._build_broker_episodes(orders)
    assert unattributed == []
    assert by_session[19471]["episodes"] == 2
    assert by_session[19471]["realized_pnl_usd"] == pytest.approx(-186.98, abs=0.01)


def test_broker_episode_reports_a_fill_that_belongs_to_no_session():
    """The class no session-first census can ever surface."""
    orders = [
        _Order("aaa", "d4e5f6a7-0000-0000-0000-000000000001", "MSTX", "buy",
               "filled", 1.0, 11.36, "2026-08-20T12:15:00Z"),
        _Order("bbb", "d4e5f6a7-0000-0000-0000-000000000002", "MSTX", "sell",
               "filled", 1.0, 11.35, "2026-08-20T12:15:30Z"),
    ]
    by_session, unattributed = li._build_broker_episodes(orders)
    assert by_session == {}
    assert len(unattributed) == 1
    assert unattributed[0]["symbol"] == "MSTX"


def test_broker_episode_never_invents_pnl_for_a_still_open_lot():
    """An unclosed lot is reported as unmeasurable, never as a number."""
    orders = [
        _Order("aaa", "chili_ml_e_777_abcdef12_1111111111", "AAPL", "buy",
               "filled", 10.0, 100.0, "2026-09-01T10:00:00Z"),
    ]
    by_session, _ = li._build_broker_episodes(orders)
    assert by_session[777]["open"] is True
    assert by_session[777]["episodes"] == 0


# ── 6. the check itself ────────────────────────────────────────────────────


def test_integrity_check_flags_a_filled_session_with_no_outcome_row(db, monkeypatch):
    """The headline class: 22 of 26 filled sessions, −$916.26, zero rows."""
    sess = _session(db, state="live_arm_expired", symbol="SSM", le={"realized_pnl_usd": -25.98})
    db.flush()

    monkeypatch.setattr(
        li,
        "_read_broker_orders",
        lambda **_k: {
            "readable": True,
            "truncated": False,
            "orders": [
                _Order("e1", f"chili_ml_e_{sess.id}_aaaaaaaa_1111111111", "SSM", "buy",
                       "filled", 433.0, 4.01, "2026-09-01T10:44:35Z"),
                _Order("x1", "chili_orphan_flatten_ssm", "SSM", "sell",
                       "filled", 433.0, 3.95, "2026-09-01T10:50:44Z"),
            ],
        },
    )
    out = li.check_live_ledger_integrity(db, days=30, include_broker=True)

    assert out["ok"] is False
    assert out["coverage"] == "broker_and_db"
    flagged = [r for r in out["violations"] if r["session_id"] == int(sess.id)]
    assert flagged, "a broker-filled session with no outcome row must be a violation"
    assert flagged[0]["status"] == li.STATUS_FILLED_NEVER_BOOKED
    assert flagged[0]["broker_realized_pnl_usd"] == pytest.approx(-25.98, abs=0.01)


def test_integrity_check_flags_a_booked_row_that_disagrees_with_the_broker(db, monkeypatch):
    """CANF 19471: the books said −78.13, the broker said −186.98."""
    sess = _session(db, state="live_finished", symbol="CANF", le={"realized_pnl_usd": -78.13})
    try_emit_momentum_session_feedback(db, sess)
    db.flush()

    monkeypatch.setattr(
        li,
        "_read_broker_orders",
        lambda **_k: {
            "readable": True,
            "truncated": False,
            "orders": [
                _Order("b1", f"chili_ml_e_{sess.id}_aaaaaaaa_1111111111", "CANF", "buy",
                       "filled", 355.0, 4.34, "2026-09-02T11:10:17Z"),
                _Order("s1", f"chili_ml_s_{sess.id}_aaaaaaaa_2222222222", "CANF", "sell",
                       "filled", 355.0, 4.119915, "2026-09-02T11:11:04Z"),
                _Order("b2", f"chili_ml_e_{sess.id}_aaaaaaaa_3333333333", "CANF", "buy",
                       "filled", 165.0, 4.62, "2026-09-02T11:19:10Z"),
                _Order("s2", f"chili_ops_flat_{sess.id}_4444444444", "CANF", "sell",
                       "filled", 165.0, 3.960303, "2026-09-02T11:34:30Z"),
            ],
        },
    )
    out = li.check_live_ledger_integrity(db, days=30, include_broker=True)

    flagged = [r for r in out["violations"] if r["session_id"] == int(sess.id)]
    assert flagged and flagged[0]["status"] == li.STATUS_PNL_DISAGREES
    assert flagged[0]["delta_usd"] == pytest.approx(-108.85, abs=0.01)


def test_integrity_check_refuses_to_pass_when_the_broker_is_unreadable(db, monkeypatch):
    """'We cannot see the broker' and 'the broker did nothing' must never merge.

    This is the exact reasoning error the whole workflow exists to prevent: an
    incomplete source read as an authoritative one.
    """
    monkeypatch.setattr(
        li, "_read_broker_orders",
        lambda **_k: {"readable": False, "orders": [], "error": "transport_down"},
    )
    out = li.check_live_ledger_integrity(db, days=30, include_broker=True)
    assert out["ok"] is False
    assert out["coverage"] == "db_only"
    assert out["broker_read_error"] == "transport_down"


def test_integrity_check_is_clean_on_a_correctly_booked_session(db, monkeypatch):
    """No false alarms: a booked row matching broker truth is clean."""
    sess = _session(db, state="live_cancelled", symbol="JLHL", le={"realized_pnl_usd": -18.83})
    try_emit_momentum_session_feedback(db, sess)
    db.flush()

    monkeypatch.setattr(
        li,
        "_read_broker_orders",
        lambda **_k: {
            "readable": True,
            "truncated": False,
            "orders": [
                _Order("b1", f"chili_ml_e_{sess.id}_aaaaaaaa_1111111111", "JLHL", "buy",
                       "filled", 149.0, 7.5, "2026-09-02T10:47:35Z"),
                _Order("s1", f"chili_ml_s_{sess.id}_aaaaaaaa_2222222222", "JLHL", "sell",
                       "filled", 149.0, 7.373624161, "2026-09-02T10:49:05Z"),
            ],
        },
    )
    out = li.check_live_ledger_integrity(db, days=30, include_broker=True)
    flagged = [r for r in out["violations"] if r["session_id"] == int(sess.id)]
    assert not flagged, f"clean session was flagged: {out['violations']}"
