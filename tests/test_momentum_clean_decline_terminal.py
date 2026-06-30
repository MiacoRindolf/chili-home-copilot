"""ITEM B — a deterministic PRE-ENTRY policy decline terminalizes CLEAN (live_cancelled),
not the alarm-coloured live_error (live_error noise fix, 2026-06-29).

A known risk-eval BLOCK at the entry instant (no_bbo / not-live-eligible / spread-too-wide /
product-not-tradable — on a name that never held a position) is a DECLINE, not a runner ERROR.
Routing it to the clean terminal cuts the recurring live_error noise + reaper churn so the REAL
errors (zero-fill, place isError, missing snapshot) stand out. The block is NEVER weakened — the
session still does NOT enter; only the terminal STATE/label changes.
"""
from __future__ import annotations

import types

import app.services.trading.momentum_neural.live_runner as lr
from app.config import settings
from app.services.trading.momentum_neural import live_fsm as fsm
from app.services.trading.momentum_neural.live_fsm import (
    STATE_ARMED_PENDING_RUNNER,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_ENTERED,
    STATE_LIVE_ENTRY_CANDIDATE,
    STATE_LIVE_ERROR,
    STATE_LIVE_PENDING_ENTRY,
    STATE_QUEUED_LIVE,
    STATE_WATCHING_LIVE,
    can_transition_live,
)


class _FakeSession:
    def __init__(self, state: str):
        self.id = 1
        self.state = state
        self.mode = "live"
        self.symbol = "RVMDW"
        self.variant_id = 1
        self.correlation_id = "corr"
        self.user_id = 7
        self.updated_at = None
        self.risk_snapshot_json = {}


class _FakeDB:
    def flush(self):
        pass


def _patch_io(monkeypatch):
    """Stub the DB-touching side-effects of _emit / _safe_transition so the decline routing is
    unit-testable without a DB. Returns the list that records emitted (event_type, payload)."""
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(lr, "_emit", lambda db, sess, et, payload: events.append((et, payload)))
    # _safe_transition imports these INSIDE the function from their home modules.
    import app.services.trading.momentum_neural.feedback_emit as fe
    import app.services.trading.momentum_neural.outcome_extract as oe
    monkeypatch.setattr(fe, "emit_feedback_after_terminal_transition", lambda db, sess: None)
    monkeypatch.setattr(oe, "session_terminal_for_feedback", lambda mode, state: False)
    return events


# ── FSM: pre-entry -> live_cancelled is LEGAL; held -> live_cancelled stays ILLEGAL ──


def test_fsm_pre_entry_to_cancelled_is_legal():
    for st in (STATE_ARMED_PENDING_RUNNER, STATE_QUEUED_LIVE, STATE_WATCHING_LIVE, STATE_LIVE_ENTRY_CANDIDATE):
        assert can_transition_live(st, STATE_LIVE_CANCELLED) is True, st


def test_fsm_held_or_resting_order_to_cancelled_is_illegal():
    # A held position OR a resting-entry-order state must NOT be cleanly declinable — those
    # own/encumber capital and need the existing cancel/exit chokepoints, not a decline reroute.
    assert can_transition_live(STATE_LIVE_ENTERED, STATE_LIVE_CANCELLED) is False
    assert can_transition_live(STATE_LIVE_PENDING_ENTRY, STATE_LIVE_CANCELLED) is False


# ── _decline_terminal: policy decline -> clean terminal (flag ON, pre-entry) ──────────


def test_policy_decline_routes_to_clean_terminal(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_clean_decline_terminal_enabled", True)
    events = _patch_io(monkeypatch)
    sess = _FakeSession(STATE_ARMED_PENDING_RUNNER)
    lr._decline_terminal(_FakeDB(), sess, reason="no_bbo")
    assert sess.state == STATE_LIVE_CANCELLED          # CLEAN terminal, not live_error
    assert ("live_declined", {"reason": "no_bbo", "terminal": STATE_LIVE_CANCELLED}) in events
    # The decline still BLOCKED entry — the session is terminal, never entered.


def test_policy_decline_records_reason_and_detail(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_clean_decline_terminal_enabled", True)
    events = _patch_io(monkeypatch)
    sess = _FakeSession(STATE_QUEUED_LIVE)
    lr._decline_terminal(_FakeDB(), sess, reason="risk_block", detail={"severity": "block"})
    assert sess.state == STATE_LIVE_CANCELLED
    et, payload = events[-1]
    assert et == "live_declined"
    assert payload["reason"] == "risk_block"
    assert payload["severity"] == "block"


# ── flag OFF => byte-identical legacy (decline => live_error) ─────────────────────────


def test_flag_off_is_legacy_live_error(monkeypatch):
    monkeypatch.setattr(settings, "chili_momentum_clean_decline_terminal_enabled", False)
    _patch_io(monkeypatch)
    sess = _FakeSession(STATE_ARMED_PENDING_RUNNER)
    lr._decline_terminal(_FakeDB(), sess, reason="no_bbo")
    assert sess.state == STATE_LIVE_ERROR  # legacy terminal preserved


# ── a genuine ERROR / held state still routes to live_error (decline reroute is scoped) ──


def test_held_state_falls_back_to_live_error(monkeypatch):
    # Defensive: if somehow called from a held state, _decline_terminal must NOT attempt the
    # (illegal) clean-decline edge — it falls back to the legacy live_error path. With the flag
    # ON, the pre-entry guard excludes held states, so the fallback engages.
    monkeypatch.setattr(settings, "chili_momentum_clean_decline_terminal_enabled", True)
    _patch_io(monkeypatch)
    sess = _FakeSession(STATE_LIVE_ENTERED)
    lr._decline_terminal(_FakeDB(), sess, reason="product_not_tradable")
    assert sess.state == STATE_LIVE_ERROR  # held -> NOT cleanly declined


def test_real_exception_path_still_uses_live_error(monkeypatch):
    # Sites that emit a genuine live_error (zero_fill, missing snapshot, place isError) do NOT
    # call _decline_terminal — they call _safe_transition(..., STATE_LIVE_ERROR) directly. Prove
    # that edge is intact so the reroute never accidentally swallows a real error.
    _patch_io(monkeypatch)
    sess = _FakeSession(STATE_ARMED_PENDING_RUNNER)
    lr._safe_transition(_FakeDB(), sess, STATE_LIVE_ERROR)
    assert sess.state == STATE_LIVE_ERROR
