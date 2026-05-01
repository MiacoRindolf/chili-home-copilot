"""Tests for the Phase 3.1 bracket-intent state machine.

Specifies the legal-transition table that governs every mutation of
``trading_bracket_intents.intent_state``. Adding a new state or relaxing
a transition rule must update both the production code and these tests
in lockstep.
"""
from __future__ import annotations

import pytest
from sqlalchemy import text

from app.services.trading.bracket_intent_writer import (
    IntentMode,
    IntentState,
    TransitionResult,
    _coerce_state,
    _LEGAL_TRANSITIONS,
    is_terminal_state,
    mark_closed,
    mark_reconciled,
    mark_terminal_reject,
    transition,
)

# ── Pure-function tests (no DB) ────────────────────────────────────────


def test_every_state_has_a_legal_transition_entry():
    """Each IntentState must appear as a key in _LEGAL_TRANSITIONS."""
    for state in IntentState:
        assert state in _LEGAL_TRANSITIONS, (
            f"IntentState.{state.name} missing from _LEGAL_TRANSITIONS"
        )


def test_closed_is_terminal():
    """``closed`` has no legal transitions out of it."""
    assert _LEGAL_TRANSITIONS[IntentState.CLOSED] == frozenset()
    assert is_terminal_state(IntentState.CLOSED)
    assert is_terminal_state("closed")


def test_terminal_reject_can_be_unstuck_by_operator():
    """The state machine intentionally permits terminal_reject → intent so
    an operator can manually fix the underlying issue (e.g. cancel a
    covering limit) and re-arm the intent."""
    assert IntentState.INTENT in _LEGAL_TRANSITIONS[IntentState.TERMINAL_REJECT]


def test_no_state_transitions_to_itself_in_table():
    """Self-transitions are handled idempotently in transition() (ok=True
    no-op), not declared in the legal-transitions table."""
    for state, allowed in _LEGAL_TRANSITIONS.items():
        assert state not in allowed, f"{state} should not list itself"


def test_legacy_aliases_normalize():
    assert _coerce_state("authoritative_submitted") is IntentState.CONFIRMED_AT_BROKER
    assert _coerce_state("authoritative_reconciled") is IntentState.RECONCILED


def test_unknown_state_returns_none():
    assert _coerce_state("garbage") is None
    assert _coerce_state("") is None
    assert _coerce_state(None) is None


# ── DB-backed tests ────────────────────────────────────────────────────
#
# These use the ``db`` fixture from tests/conftest.py which truncates the
# app tables at test start. conftest's _ensure_postgres_test_url() guards
# against running against prod by requiring the URL to end in ``_test``.


def _seed_intent(s, state: str = "intent") -> int:
    """Insert a minimal trading_bracket_intents row and return its id.

    Creates a parent Trade row (FK target) using only the columns that
    are NOT NULL on the model; everything else stays at default.
    """
    tr = s.execute(text(
        "INSERT INTO trading_trades (ticker, direction, status, "
        " quantity, entry_price, entry_date) "
        "VALUES ('SMTEST', 'long', 'open', 1, 1.0, NOW()) "
        "RETURNING id"
    )).scalar_one()
    iid = s.execute(text(
        "INSERT INTO trading_bracket_intents "
        "(trade_id, ticker, direction, quantity, entry_price, intent_state, "
        " shadow_mode, payload_json, created_at, updated_at) "
        "VALUES (:tid, 'SMTEST', 'long', 1, 1.0, :st, false, '{}'::jsonb, NOW(), NOW()) "
        "RETURNING id"
    ), {"tid": int(tr), "st": state}).scalar_one()
    s.commit()
    return int(iid)


@pytest.mark.parametrize("from_state, to_state, expected_ok", [
    # Legal transitions
    ("intent", IntentState.CONFIRMED_AT_BROKER, True),
    ("intent", IntentState.SHADOW_LOGGED, True),
    ("intent", IntentState.TERMINAL_REJECT, True),
    ("intent", IntentState.RECONCILED, True),
    ("confirmed_at_broker", IntentState.RECONCILED, True),
    ("reconciled", IntentState.AMENDING, True),
    ("reconciled", IntentState.EXITING, True),
    ("amending", IntentState.CONFIRMED_AT_BROKER, True),
    ("exiting", IntentState.CLOSED, True),
    ("terminal_reject", IntentState.INTENT, True),
    ("terminal_reject", IntentState.CLOSED, True),
    # Illegal transitions
    ("closed", IntentState.INTENT, False),
    ("closed", IntentState.RECONCILED, False),
    ("intent", IntentState.EXITING, False),
    ("intent", IntentState.AMENDING, False),
    ("shadow_logged", IntentState.RECONCILED, False),
    ("exiting", IntentState.AMENDING, False),
])
def test_transition_respects_state_machine(db, from_state, to_state, expected_ok):
    iid = _seed_intent(db, from_state)
    res = transition(db, iid, to_state=to_state, reason="unit test")
    assert isinstance(res, TransitionResult)
    assert res.ok is expected_ok, (
        f"expected {from_state} → {to_state} ok={expected_ok}, got {res}"
    )


def test_idempotent_self_transition_returns_ok(db):
    iid = _seed_intent(db, "reconciled")
    res = transition(db, iid, to_state=IntentState.RECONCILED, reason="idempotent")
    assert res.ok is True
    assert res.reason == "ok"


def test_no_such_intent_returns_structured_failure(db):
    res = transition(db, 999_999_999, to_state=IntentState.CLOSED)
    assert res.ok is False
    assert res.reason == "no_such_intent"


def test_expected_from_precondition_enforced(db):
    """Caller passes expected_from='intent'; row is actually 'reconciled'.
    Transition is rejected (TOCTOU protection)."""
    iid = _seed_intent(db, "reconciled")
    res = transition(
        db, iid,
        to_state=IntentState.AMENDING,
        expected_from=IntentState.INTENT,
        reason="expected_from check",
    )
    assert res.ok is False
    assert res.reason == "illegal_transition"


def test_mark_reconciled_uses_state_machine(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.bracket_intent_writer._effective_mode",
        lambda *_args, **_kw: IntentMode.AUTHORITATIVE,
    )
    iid = _seed_intent(db, "intent")
    assert mark_reconciled(db, iid, reason="broker agrees")
    # Now: reconciled → intent is illegal → should refuse
    iid2 = _seed_intent(db, "closed")
    assert not mark_reconciled(db, iid2, reason="too late")


def test_mark_terminal_reject_replaces_in_process_cooldown(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.bracket_intent_writer._effective_mode",
        lambda *_args, **_kw: IntentMode.AUTHORITATIVE,
    )
    iid = _seed_intent(db, "confirmed_at_broker")
    assert mark_terminal_reject(db, iid, reason="not enough shares to sell")


def test_mark_closed_is_terminal(db, monkeypatch):
    monkeypatch.setattr(
        "app.services.trading.bracket_intent_writer._effective_mode",
        lambda *_args, **_kw: IntentMode.AUTHORITATIVE,
    )
    iid = _seed_intent(db, "exiting")
    assert mark_closed(db, iid, reason="filled at broker")
    # Now closed → anything is illegal
    res = transition(db, iid, to_state=IntentState.RECONCILED)
    assert res.ok is False
