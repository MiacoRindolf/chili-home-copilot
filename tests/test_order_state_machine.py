"""P1.1 — formal order state machine.

Verifies the canonical state machine used by venue adapters and
``execution_audit`` to project broker-native statuses onto one of
``DRAFT/SUBMITTING/ACK/PARTIAL/FILLED/CANCELLED/REJECTED/EXPIRED``.

Covers:
    * Enum + transition-table invariants (what states exist, what moves
      are allowed, which states are terminal).
    * Broker-status → canonical mapping (RH/Coinbase spellings + unknowns).
    * Writer semantics (feature flag off = no-op, allowed transitions
      land, illegal transitions are rejected, same-state is a no-op,
      PARTIAL stickiness, terminal lock).
    * Readers (``get_current_state``, ``get_state_history``).
    * ``state_distribution`` latest-per-order aggregation.
    * Wiring through ``execution_audit.record_execution_event``.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest
from sqlalchemy import text

from app.services.trading.venue import order_state_machine as osm
from app.services.trading.venue.order_state_machine import (
    ALLOWED_TRANSITIONS,
    OrderState,
    TERMINAL_STATES,
    TransitionResult,
    get_current_state,
    get_state_history,
    is_terminal,
    is_transition_allowed,
    map_broker_status,
    record_from_broker_status,
    record_transition,
    state_distribution,
)


def _clear_logs(session, *, order_id: str | None = None, client_order_id: str | None = None) -> None:
    if order_id:
        session.execute(
            text("DELETE FROM trading_order_state_log WHERE order_id = :k"),
            {"k": order_id},
        )
    if client_order_id:
        session.execute(
            text("DELETE FROM trading_order_state_log WHERE client_order_id = :k"),
            {"k": client_order_id},
        )
    session.commit()


# ── Transition table invariants ──────────────────────────────────────────


class TestTransitionTableInvariants:
    def test_all_states_exist(self):
        """All 8 canonical states are present in the enum."""
        assert {s.value for s in OrderState} == {
            "draft", "submitting", "ack", "partial",
            "filled", "cancelled", "rejected", "expired",
        }

    def test_terminal_set_is_exactly_the_four_terminals(self):
        assert TERMINAL_STATES == frozenset({
            OrderState.FILLED,
            OrderState.CANCELLED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        })

    def test_every_non_terminal_state_has_allowed_entries(self):
        """Every non-terminal state + the `None` initial key maps to a
        non-empty frozenset; every terminal state maps to empty."""
        assert ALLOWED_TRANSITIONS[None]  # initial observation allows any state
        for state in OrderState:
            allowed = ALLOWED_TRANSITIONS.get(state, frozenset())
            if state in TERMINAL_STATES:
                assert allowed == frozenset(), f"terminal {state} should have no transitions"
            else:
                assert allowed, f"non-terminal {state} should have allowed transitions"

    def test_no_transition_from_terminal_to_anything(self):
        for terminal in TERMINAL_STATES:
            for to_state in OrderState:
                assert is_transition_allowed(terminal, to_state) is False, (
                    f"illegal: {terminal} → {to_state}"
                )

    def test_partial_is_sticky(self):
        """PARTIAL → PARTIAL is allowed so subsequent fills can log."""
        assert OrderState.PARTIAL in ALLOWED_TRANSITIONS[OrderState.PARTIAL]

    def test_happy_path_is_allowed(self):
        """DRAFT → SUBMITTING → ACK → PARTIAL → FILLED all legal."""
        assert is_transition_allowed(OrderState.DRAFT, OrderState.SUBMITTING)
        assert is_transition_allowed(OrderState.SUBMITTING, OrderState.ACK)
        assert is_transition_allowed(OrderState.ACK, OrderState.PARTIAL)
        assert is_transition_allowed(OrderState.PARTIAL, OrderState.FILLED)

    def test_draft_to_filled_is_rejected(self):
        """Cannot skip past SUBMITTING/ACK straight to FILLED."""
        assert is_transition_allowed(OrderState.DRAFT, OrderState.FILLED) is False


# ── Broker-status mapping ────────────────────────────────────────────────


class TestMapBrokerStatus:
    @pytest.mark.parametrize("status,expected", [
        ("filled", OrderState.FILLED),
        ("FILLED", OrderState.FILLED),
        ("  Filled ", OrderState.FILLED),
        ("done", OrderState.FILLED),
        ("complete", OrderState.FILLED),
        ("partially_filled", OrderState.PARTIAL),
        ("partial_filled", OrderState.PARTIAL),
        ("cancelled", OrderState.CANCELLED),
        ("canceled", OrderState.CANCELLED),
        ("rejected", OrderState.REJECTED),
        ("failed", OrderState.REJECTED),
        ("expired", OrderState.EXPIRED),
        ("timed_out", OrderState.EXPIRED),
        ("open", OrderState.ACK),
        ("pending", OrderState.ACK),
        ("confirmed", OrderState.ACK),
        ("queued", OrderState.ACK),
        ("submitting", OrderState.SUBMITTING),
        ("draft", OrderState.DRAFT),
    ])
    def test_known_statuses(self, status, expected):
        assert map_broker_status(status) is expected

    @pytest.mark.parametrize("bogus", ["", None, "not_a_real_status", "xyz"])
    def test_unknown_returns_none(self, bogus):
        assert map_broker_status(bogus) is None


# ── is_terminal helper ───────────────────────────────────────────────────


class TestIsTerminal:
    def test_enum_terminals(self):
        for t in TERMINAL_STATES:
            assert is_terminal(t) is True

    def test_enum_non_terminals(self):
        for s in OrderState:
            if s not in TERMINAL_STATES:
                assert is_terminal(s) is False

    def test_string_inputs(self):
        assert is_terminal("filled") is True
        assert is_terminal("ack") is False

    def test_invalid_string_or_none(self):
        assert is_terminal(None) is False
        assert is_terminal("gibberish") is False


# ── Writer semantics ─────────────────────────────────────────────────────


class TestRecordTransitionFeatureFlag:
    def test_disabled_is_a_no_op(self, db, monkeypatch):
        """With the feature flag off, writer returns disabled and writes nothing."""
        from app.config import settings
        monkeypatch.setattr(settings, "chili_order_state_machine_enabled", False)
        _clear_logs(db, client_order_id="cid-disabled-1")

        result = record_transition(
            db,
            to_state=OrderState.SUBMITTING,
            venue="coinbase",
            source="test",
            client_order_id="cid-disabled-1",
        )
        assert result.wrote is False
        assert result.reason == "disabled"
        rows = db.execute(
            text("SELECT 1 FROM trading_order_state_log WHERE client_order_id = 'cid-disabled-1'")
        ).fetchall()
        assert rows == []

    def test_override_can_force_on(self, db, monkeypatch):
        """``enabled_override=True`` flips the writer on without touching settings."""
        from app.config import settings
        monkeypatch.setattr(settings, "chili_order_state_machine_enabled", False)
        _clear_logs(db, client_order_id="cid-override-1")

        result = record_transition(
            db,
            to_state=OrderState.SUBMITTING,
            venue="coinbase",
            source="test",
            client_order_id="cid-override-1",
            enabled_override=True,
            commit=True,
        )
        assert result.wrote is True
        assert result.reason == "ok"
        _clear_logs(db, client_order_id="cid-override-1")


class TestRecordTransitionWrites:
    @pytest.fixture(autouse=True)
    def _enable_flag(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "chili_order_state_machine_enabled", True)

    def test_writes_row_with_correct_fields(self, db):
        cid = "cid-write-1"
        _clear_logs(db, client_order_id=cid)
        result = record_transition(
            db,
            to_state=OrderState.SUBMITTING,
            venue="coinbase",
            source="place_market",
            client_order_id=cid,
            raw_payload={"hello": "world"},
            commit=True,
        )
        assert result.wrote is True
        assert result.from_state is None  # first observation
        assert result.to_state is OrderState.SUBMITTING
        assert result.reason == "ok"
        row = db.execute(text("""
            SELECT from_state, to_state, venue, source, client_order_id, raw_payload
            FROM trading_order_state_log
            WHERE client_order_id = :k
        """), {"k": cid}).fetchone()
        assert row is not None
        assert row[0] is None
        assert row[1] == "submitting"
        assert row[2] == "coinbase"
        assert row[3] == "place_market"
        assert row[4] == cid
        # raw_payload comes back already parsed as a dict under psycopg
        payload = row[5] if isinstance(row[5], dict) else {}
        assert payload.get("hello") == "world"
        _clear_logs(db, client_order_id=cid)

    def test_no_key_is_rejected(self, db):
        result = record_transition(
            db,
            to_state=OrderState.SUBMITTING,
            venue="coinbase",
            source="test",
        )
        assert result.wrote is False
        assert result.reason == "no_key"

    def test_illegal_transition_rejected(self, db):
        """ACK → SUBMITTING is not allowed by the transition table."""
        cid = "cid-illegal-1"
        _clear_logs(db, client_order_id=cid)
        # First, land on ACK
        first = record_transition(db, to_state=OrderState.ACK, venue="coinbase",
                                  source="t", client_order_id=cid, commit=True)
        assert first.wrote is True, first
        # Sanity — the ACK row is committed and visible via direct SELECT.
        sanity_rows = db.execute(
            text("SELECT to_state FROM trading_order_state_log WHERE client_order_id = :k"),
            {"k": cid},
        ).fetchall()
        assert [r[0] for r in sanity_rows] == ["ack"], sanity_rows
        # Sanity — get_current_state resolves to ACK.
        assert get_current_state(db, client_order_id=cid) is OrderState.ACK
        # Then try to go back to SUBMITTING — not in ACK's allowed set.
        r = record_transition(db, to_state=OrderState.SUBMITTING, venue="coinbase",
                              source="t", client_order_id=cid)
        assert r.wrote is False, r
        assert r.reason == "illegal_transition"
        rows = db.execute(
            text("SELECT to_state FROM trading_order_state_log WHERE client_order_id = :k ORDER BY id"),
            {"k": cid},
        ).fetchall()
        assert [r[0] for r in rows] == ["ack"]
        _clear_logs(db, client_order_id=cid)

    def test_same_state_is_no_op_except_partial(self, db):
        """Re-observing ACK while in ACK writes nothing."""
        cid = "cid-samestate-1"
        _clear_logs(db, client_order_id=cid)
        record_transition(db, to_state=OrderState.ACK, venue="coinbase",
                          source="t", client_order_id=cid, commit=True)
        r = record_transition(db, to_state=OrderState.ACK, venue="coinbase",
                              source="t", client_order_id=cid, commit=True)
        assert r.wrote is False
        assert r.reason == "same_state"
        rows = db.execute(
            text("SELECT to_state FROM trading_order_state_log WHERE client_order_id = :k ORDER BY id"),
            {"k": cid},
        ).fetchall()
        assert [r[0] for r in rows] == ["ack"]
        _clear_logs(db, client_order_id=cid)

    def test_partial_is_sticky_and_writes_each_time(self, db):
        """PARTIAL → PARTIAL is a real additional-fill event and must log."""
        cid = "cid-partial-1"
        _clear_logs(db, client_order_id=cid)
        record_transition(db, to_state=OrderState.ACK, venue="coinbase",
                          source="t", client_order_id=cid, commit=True)
        r1 = record_transition(db, to_state=OrderState.PARTIAL, venue="coinbase",
                               source="t", client_order_id=cid, commit=True)
        r2 = record_transition(db, to_state=OrderState.PARTIAL, venue="coinbase",
                               source="t", client_order_id=cid, commit=True)
        assert r1.wrote is True and r2.wrote is True
        rows = db.execute(
            text("SELECT to_state FROM trading_order_state_log WHERE client_order_id = :k ORDER BY id"),
            {"k": cid},
        ).fetchall()
        assert [r[0] for r in rows] == ["ack", "partial", "partial"]
        _clear_logs(db, client_order_id=cid)

    def test_terminal_state_is_locked(self, db):
        """Once FILLED, a stale poll saying ACK must not regress the state."""
        cid = "cid-terminal-1"
        _clear_logs(db, client_order_id=cid)
        record_transition(db, to_state=OrderState.ACK, venue="coinbase",
                          source="t", client_order_id=cid, commit=True)
        record_transition(db, to_state=OrderState.FILLED, venue="coinbase",
                          source="t", client_order_id=cid, commit=True)
        # Try to re-observe ACK after FILLED
        r = record_transition(db, to_state=OrderState.ACK, venue="coinbase",
                              source="t", client_order_id=cid)
        assert r.wrote is False
        assert r.reason == "terminal_locked"
        assert r.from_state is OrderState.FILLED
        _clear_logs(db, client_order_id=cid)

    def test_resolves_by_order_id_before_client_order_id(self, db):
        """When both keys are supplied, order_id wins for current-state lookup.

        Simulates the lifecycle: write ACK with only cid, then later poll
        returns with order_id bound; the state machine must find the
        existing state via client_order_id so far but prefer order_id once
        both are known.
        """
        cid = "cid-resolve-1"
        oid = "oid-resolve-1"
        _clear_logs(db, client_order_id=cid)
        _clear_logs(db, order_id=oid)
        # First write: only client_order_id.
        record_transition(db, to_state=OrderState.ACK, venue="coinbase",
                          source="t", client_order_id=cid, commit=True)
        # Second write: both keys, transitioning to PARTIAL. The writer
        # prefers order_id for the current-state query — but no row yet
        # has order_id=oid, so ``from`` will be None (initial-observation
        # path) and PARTIAL is permitted from None.
        r = record_transition(db, to_state=OrderState.PARTIAL, venue="coinbase",
                              source="t", order_id=oid, client_order_id=cid, commit=True)
        assert r.wrote is True
        assert r.from_state is None
        # After this, querying by order_id alone finds PARTIAL.
        assert get_current_state(db, order_id=oid) is OrderState.PARTIAL
        # Querying by client_order_id alone still finds PARTIAL (newer row).
        assert get_current_state(db, client_order_id=cid) is OrderState.PARTIAL
        _clear_logs(db, client_order_id=cid)
        _clear_logs(db, order_id=oid)


# ── record_from_broker_status ────────────────────────────────────────────


class TestRecordFromBrokerStatus:
    @pytest.fixture(autouse=True)
    def _enable_flag(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "chili_order_state_machine_enabled", True)

    def test_maps_and_writes(self, db):
        cid = "cid-bstatus-1"
        _clear_logs(db, client_order_id=cid)
        r = record_from_broker_status(
            db,
            broker_status="filled",
            venue="coinbase",
            source="poll_loop",
            client_order_id=cid,
            commit=True,
        )
        assert r.wrote is True
        assert r.to_state is OrderState.FILLED
        _clear_logs(db, client_order_id=cid)

    def test_unknown_status_returns_flag(self, db):
        cid = "cid-bstatus-unknown"
        _clear_logs(db, client_order_id=cid)
        r = record_from_broker_status(
            db,
            broker_status="nonsense_status",
            venue="coinbase",
            source="poll_loop",
            client_order_id=cid,
        )
        assert r.wrote is False
        assert r.reason == "unknown_broker_status"
        rows = db.execute(
            text("SELECT 1 FROM trading_order_state_log WHERE client_order_id = :k"),
            {"k": cid},
        ).fetchall()
        assert rows == []


# ── Readers ──────────────────────────────────────────────────────────────


class TestReaders:
    @pytest.fixture(autouse=True)
    def _enable_flag(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "chili_order_state_machine_enabled", True)

    def test_current_state_none_for_unknown_order(self, db):
        assert get_current_state(db, order_id="nope-does-not-exist") is None
        assert get_current_state(db, client_order_id="nope-cid") is None

    def test_current_state_none_with_no_keys(self, db):
        assert get_current_state(db) is None

    def test_state_history_in_order(self, db):
        cid = "cid-hist-1"
        _clear_logs(db, client_order_id=cid)
        record_transition(db, to_state=OrderState.SUBMITTING, venue="coinbase",
                          source="t", client_order_id=cid, commit=True)
        record_transition(db, to_state=OrderState.ACK, venue="coinbase",
                          source="t", client_order_id=cid, commit=True)
        record_transition(db, to_state=OrderState.FILLED, venue="coinbase",
                          source="t", client_order_id=cid, commit=True)
        hist = get_state_history(db, client_order_id=cid)
        assert [h["to_state"] for h in hist] == ["submitting", "ack", "filled"]
        assert hist[0]["from_state"] is None
        assert hist[1]["from_state"] == "submitting"
        assert hist[2]["from_state"] == "ack"
        _clear_logs(db, client_order_id=cid)

    def test_state_history_returns_empty_without_keys(self, db):
        assert get_state_history(db) == []


# ── state_distribution ───────────────────────────────────────────────────


class TestStateDistribution:
    @pytest.fixture(autouse=True)
    def _enable_flag(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "chili_order_state_machine_enabled", True)

    def test_counts_distinct_latest_state_per_order(self, db):
        """Two orders with different final states → one count each."""
        # Sanity — the autouse fixture must have flipped the flag on, otherwise
        # every record_transition below would silently no-op and state_distribution
        # would return {}.
        from app.config import settings as _settings
        assert _settings.chili_order_state_machine_enabled is True

        # Clear any existing rows for the test-private venue first.
        db.execute(text("DELETE FROM trading_order_state_log WHERE venue = 'coinbase_dist_test'"))
        db.commit()

        # Order 1: SUBMITTING → ACK → FILLED (distinct latest = FILLED)
        cid1 = "cid-dist-1"
        r1a = record_transition(db, to_state=OrderState.SUBMITTING,
                                venue="coinbase_dist_test", source="t",
                                client_order_id=cid1, commit=True)
        r1b = record_transition(db, to_state=OrderState.ACK,
                                venue="coinbase_dist_test", source="t",
                                client_order_id=cid1, commit=True)
        r1c = record_transition(db, to_state=OrderState.FILLED,
                                venue="coinbase_dist_test", source="t",
                                client_order_id=cid1, commit=True)
        # Every write must have landed — if any returns False, the following
        # assertions pinpoint exactly which one (much cleaner than an
        # ambiguous empty-dist failure downstream).
        assert r1a.wrote is True, r1a
        assert r1b.wrote is True, r1b
        assert r1c.wrote is True, r1c

        # Order 2: SUBMITTING → ACK (latest = ACK)
        cid2 = "cid-dist-2"
        r2a = record_transition(db, to_state=OrderState.SUBMITTING,
                                venue="coinbase_dist_test", source="t",
                                client_order_id=cid2, commit=True)
        r2b = record_transition(db, to_state=OrderState.ACK,
                                venue="coinbase_dist_test", source="t",
                                client_order_id=cid2, commit=True)
        assert r2a.wrote is True, r2a
        assert r2b.wrote is True, r2b

        # Sanity — all 5 rows are in the DB, visible on the same session.
        sanity_rows = db.execute(text("""
            SELECT client_order_id, to_state
            FROM trading_order_state_log
            WHERE venue = 'coinbase_dist_test'
            ORDER BY id
        """)).fetchall()
        assert [(r[0], r[1]) for r in sanity_rows] == [
            ("cid-dist-1", "submitting"),
            ("cid-dist-1", "ack"),
            ("cid-dist-1", "filled"),
            ("cid-dist-2", "submitting"),
            ("cid-dist-2", "ack"),
        ], sanity_rows

        dist = state_distribution(db, venue="coinbase_dist_test")
        assert dist.get("filled", 0) == 1, dist
        assert dist.get("ack", 0) == 1, dist
        # SUBMITTING was superseded on both orders → count is 0.
        assert dist.get("submitting", 0) == 0, dist

        db.execute(text("DELETE FROM trading_order_state_log WHERE venue = 'coinbase_dist_test'"))
        db.commit()


# ── Integration: execution_audit wiring ──────────────────────────────────


class TestExecutionAuditWiring:
    """record_execution_event writes both a TradingExecutionEvent and a
    state-machine transition row when the feature flag is on.

    This is the key end-to-end guarantee: the existing event stream stays
    unchanged; enabling the flag gives us a second-order canonical state
    log for free.
    """

    @pytest.fixture(autouse=True)
    def _enable_flag(self, monkeypatch):
        from app.config import settings
        monkeypatch.setattr(settings, "chili_order_state_machine_enabled", True)

    def test_execution_audit_emits_transition_when_enabled(self, db):
        from app.services.trading.execution_audit import record_execution_event

        oid = "integ-oid-1"
        _clear_logs(db, order_id=oid)

        # Clean up any pre-existing TradingExecutionEvent rows for this order.
        from app.models.trading import TradingExecutionEvent
        db.query(TradingExecutionEvent).filter(
            TradingExecutionEvent.order_id == oid
        ).delete()
        db.commit()

        event_row = record_execution_event(
            db,
            user_id=None,
            ticker="BTC-USD",
            broker_source="coinbase",
            order_id=oid,
            event_type="ack",
            status="confirmed",  # broker-native → maps to ACK
            requested_quantity=0.01,
        )
        db.commit()

        assert event_row.order_id == oid
        # State machine should have written one row too.
        hist = get_state_history(db, order_id=oid)
        assert len(hist) == 1
        assert hist[0]["to_state"] == "ack"
        assert hist[0]["venue"] == "coinbase"
        assert hist[0]["source"].startswith("execution_audit:")

        # Follow-up fill — writes the FILLED transition.
        record_execution_event(
            db,
            user_id=None,
            ticker="BTC-USD",
            broker_source="coinbase",
            order_id=oid,
            event_type="fill",
            status="filled",
            requested_quantity=0.01,
            cumulative_filled_quantity=0.01,
            average_fill_price=50000.0,
        )
        db.commit()
        hist = get_state_history(db, order_id=oid)
        assert [h["to_state"] for h in hist] == ["ack", "filled"]
        assert hist[1]["from_state"] == "ack"

        _clear_logs(db, order_id=oid)
        db.query(TradingExecutionEvent).filter(
            TradingExecutionEvent.order_id == oid
        ).delete()
        db.commit()

    def test_execution_audit_disabled_writes_no_transition_rows(self, db, monkeypatch):
        from app.config import settings
        from app.services.trading.execution_audit import record_execution_event
        from app.models.trading import TradingExecutionEvent

        # Explicitly disable — overrides the class-level autouse fixture.
        monkeypatch.setattr(settings, "chili_order_state_machine_enabled", False)

        oid = "integ-oid-2-disabled"
        _clear_logs(db, order_id=oid)
        db.query(TradingExecutionEvent).filter(
            TradingExecutionEvent.order_id == oid
        ).delete()
        db.commit()

        record_execution_event(
            db,
            user_id=None,
            ticker="BTC-USD",
            broker_source="coinbase",
            order_id=oid,
            event_type="ack",
            status="confirmed",
        )
        db.commit()

        rows = db.execute(
            text("SELECT 1 FROM trading_order_state_log WHERE order_id = :k"),
            {"k": oid},
        ).fetchall()
        assert rows == []

        db.query(TradingExecutionEvent).filter(
            TradingExecutionEvent.order_id == oid
        ).delete()
        db.commit()
