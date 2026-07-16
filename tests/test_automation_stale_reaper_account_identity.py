from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_BAILOUT,
    STATE_LIVE_CANCELLED,
    STATE_LIVE_ERROR,
)


class _Query:
    def __init__(self, sess):
        self.sess = sess

    def filter(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def with_for_update(self, *_args, **_kwargs):
        return self

    def populate_existing(self, *_args, **_kwargs):
        return self

    def all(self):
        return [self.sess]

    def one_or_none(self):
        return self.sess


class _Db:
    def __init__(self, sess):
        self.sess = sess

    def query(self, *_args, **_kwargs):
        return _Query(self.sess)

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name="sqlite"))

    def flush(self):
        return None


def _session(
    *,
    state: str = STATE_LIVE_ERROR,
    frozen_account_id: str | None = "acct-frozen",
    expires: bool = False,
    entry_order_id: str | None = None,
):
    old = datetime.utcnow() - timedelta(hours=4)
    live_exec = {"side_long": True}
    if entry_order_id:
        live_exec["entry_order_id"] = entry_order_id
    snapshot = {
        "alpaca_account_scope": "alpaca:paper",
        "momentum_live_execution": live_exec,
    }
    if frozen_account_id is not None:
        snapshot["alpaca_account_id"] = frozen_account_id
    if expires:
        snapshot["expires_at_utc"] = (old - timedelta(minutes=1)).isoformat()
    return SimpleNamespace(
        id=55101,
        user_id=7,
        mode="live",
        venue="alpaca",
        execution_family="alpaca_spot",
        symbol="ACTU",
        state=state,
        risk_snapshot_json=snapshot,
        correlation_id="corr-stale-account-generation",
        started_at=old,
        created_at=old,
        updated_at=old,
        ended_at=None,
    )


def _configure(monkeypatch, events, *, expected_account_id: str) -> None:
    monkeypatch.setattr(aq, "_tables_present", lambda _db: True)
    monkeypatch.setattr(aq.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        aq.settings,
        "chili_alpaca_expected_account_id",
        expected_account_id,
    )
    monkeypatch.setattr(
        aq.settings,
        "chili_momentum_stale_session_reaper_enabled",
        True,
    )
    monkeypatch.setattr(
        aq.settings,
        "chili_momentum_stale_session_reaper_ttl_seconds",
        300.0,
    )
    monkeypatch.setattr(
        aq,
        "append_trading_automation_event",
        lambda _db, _sid, event_type, payload, **_kwargs: events.append(
            (event_type, payload)
        ),
    )


@pytest.mark.parametrize(
    ("expected_account_id", "frozen_account_id", "expected_reason"),
    [
        ("", "acct-frozen", "alpaca_expected_account_id_unconfigured"),
        ("acct-current", None, "alpaca_account_generation_mismatch"),
        ("acct-current", "acct-old", "alpaca_account_generation_mismatch"),
    ],
)
def test_reaper_broker_reads_require_configured_pin_matching_frozen_generation(
    monkeypatch,
    expected_account_id,
    frozen_account_id,
    expected_reason,
):
    events = []
    sess = _session(
        frozen_account_id=frozen_account_id,
        entry_order_id="entry-oid",
    )
    _configure(monkeypatch, events, expected_account_id=expected_account_id)

    import app.services.trading.venue.factory as venue_factory

    monkeypatch.setattr(
        venue_factory,
        "get_adapter",
        lambda *_a, **_k: pytest.fail("account-generation quarantine reached broker I/O"),
    )

    assert aq._reaper_has_working_entry_order(sess) is None
    flat, detail = aq._reaper_broker_position_truth(sess)

    assert flat is None
    assert detail["reason"] == expected_reason
    assert detail["execution_quarantined"] is True
    assert detail["broker_calls"] == 0


def test_stale_reaper_quarantines_generation_mismatch_without_terminalizing(
    monkeypatch,
):
    events = []
    sess = _session(frozen_account_id="acct-old")
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-current")
    monkeypatch.setattr(
        aq,
        "_reaper_has_working_entry_order",
        lambda *_a, **_k: pytest.fail("mismatched generation reached order truth"),
    )
    monkeypatch.setattr(
        aq,
        "cancel_automation_session",
        lambda *_a, **_k: pytest.fail("mismatched generation reached cancel"),
    )

    result = aq.reap_stale_live_sessions(db, user_id=7)

    assert result["reaped"] == 0
    assert result["skipped_execution_quarantine"] == 1
    assert sess.state == STATE_LIVE_ERROR
    assert sess.ended_at is None
    assert len(events) == 1
    assert events[0][0] == "alpaca_execution_quarantined"
    quarantine = sess.risk_snapshot_json["momentum_live_execution"][
        "alpaca_execution_quarantine"
    ]
    assert quarantine["reason"] == "alpaca_account_generation_mismatch"
    assert quarantine["context"] == "stale_live_session_reaper"


def test_stale_arm_generation_mismatch_never_reads_claim_or_expires(monkeypatch):
    events = []
    sess = _session(
        state=aq.STATE_LIVE_ARM_PENDING,
        frozen_account_id="acct-old",
        expires=True,
    )
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-current")

    import app.services.trading.momentum_neural.alpaca_orphan_claims as claims

    monkeypatch.setattr(
        claims,
        "read_action_claim",
        lambda *_a, **_k: pytest.fail("mismatched generation read durable claim"),
    )
    monkeypatch.setattr(
        claims,
        "resolve_action_claim",
        lambda *_a, **_k: pytest.fail("mismatched generation resolved durable claim"),
    )

    changed = aq.expire_stale_live_arm_sessions(db, user_id=7)

    assert changed == 0
    assert sess.state == aq.STATE_LIVE_ARM_PENDING
    assert sess.ended_at is None
    quarantine = sess.risk_snapshot_json["momentum_live_execution"][
        "alpaca_execution_quarantine"
    ]
    assert quarantine["reason"] == "alpaca_account_generation_mismatch"
    assert quarantine["context"] == "stale_live_arm_expiry"


def test_stale_arm_matching_generation_can_expire_without_broker_io(monkeypatch):
    events = []
    sess = _session(
        state=aq.STATE_LIVE_ARM_PENDING,
        frozen_account_id="acct-frozen",
        expires=True,
    )
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-frozen")

    import app.services.trading.momentum_neural.alpaca_orphan_claims as claims
    import app.services.trading.momentum_neural.persistence as persistence

    monkeypatch.setattr(claims, "read_action_claim", lambda *_a, **_k: (True, None))
    monkeypatch.setattr(
        persistence,
        "append_trading_automation_event",
        lambda _db, _sid, event_type, payload, **_kwargs: events.append(
            (event_type, payload)
        ),
    )

    changed = aq.expire_stale_live_arm_sessions(db, user_id=7)

    assert changed == 1
    assert sess.state == aq.STATE_EXPIRED
    assert sess.ended_at is not None
    assert any(event_type == "live_arm_expired" for event_type, _ in events)


def test_pin_rotation_after_flat_read_blocks_reaper_cancel_and_terminalization(
    monkeypatch,
):
    events = []
    sess = _session(
        state=STATE_LIVE_BAILOUT,
        frozen_account_id="acct-frozen",
    )
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-frozen")
    monkeypatch.setattr(aq, "_reaper_has_working_entry_order", lambda _sess: False)

    def _flat_then_rotate_pin(_sess):
        monkeypatch.setattr(
            aq.settings,
            "chili_alpaca_expected_account_id",
            "acct-rotated",
        )
        return True, {"broker_quantity": 0.0}

    monkeypatch.setattr(aq, "_reaper_broker_position_truth", _flat_then_rotate_pin)
    monkeypatch.setattr(
        aq,
        "cancel_automation_session",
        lambda *_a, **_k: pytest.fail("rotated pin reached cancel"),
    )

    result = aq.reap_stale_live_sessions(db, user_id=7)

    assert result["reaped"] == 0
    assert result["skipped_execution_quarantine"] == 1
    assert sess.state == STATE_LIVE_BAILOUT
    assert sess.ended_at is None
    quarantine = sess.risk_snapshot_json["momentum_live_execution"][
        "alpaca_execution_quarantine"
    ]
    assert quarantine["context"] == "stale_live_session_reaper_pre_terminal"


def test_matching_generation_allows_flat_reaper_terminalization(monkeypatch):
    events = []
    sess = _session(frozen_account_id="acct-frozen")
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-frozen")

    class _Adapter:
        def bind_account_id(self, account_id):
            assert account_id == "acct-frozen"
            return True

        def get_position_quantity(self, symbol):
            assert symbol == "ACTU"
            return 0.0

    import app.services.trading.venue.factory as venue_factory

    monkeypatch.setattr(venue_factory, "get_adapter", lambda _family: _Adapter())
    monkeypatch.setattr(
        aq,
        "_owned_unresolved_alpaca_entry_claim",
        lambda *_a, **_k: (True, None),
    )

    result = aq.reap_stale_live_sessions(db, user_id=7)

    assert result["reaped"] == 1
    assert result["skipped_execution_quarantine"] == 0
    assert sess.state == STATE_LIVE_CANCELLED
    assert sess.ended_at is not None
    assert any(event_type == "stale_session_reaped" for event_type, _ in events)


def test_direct_cancel_generation_mismatch_is_quarantined_before_broker_work(
    monkeypatch,
):
    events = []
    sess = _session(
        state=aq.STATE_WATCHING_LIVE,
        frozen_account_id="acct-old",
        entry_order_id="entry-oid",
    )
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-current")
    monkeypatch.setattr(
        aq,
        "_owned_unresolved_alpaca_entry_claim",
        lambda *_a, **_k: pytest.fail("mismatched generation read ownership claim"),
    )
    monkeypatch.setattr(
        aq,
        "tick_live_session",
        lambda *_a, **_k: pytest.fail("mismatched generation reached broker service"),
    )

    result = aq.cancel_automation_session(db, user_id=7, session_id=sess.id)

    assert result["pending"] == "execution_quarantine"
    assert result["quarantine_reason"] == "alpaca_account_generation_mismatch"
    assert sess.state == aq.STATE_WATCHING_LIVE
    assert sess.ended_at is None


def test_cancel_adoption_pin_rotation_during_oid_read_never_adopts(monkeypatch):
    events = []
    sess = _session(
        state=aq.STATE_WATCHING_LIVE,
        frozen_account_id="acct-frozen",
        entry_order_id="entry-oid",
    )
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-frozen")

    class _Adapter:
        def bind_account_id(self, account_id):
            assert account_id == "acct-frozen"
            return True

        def get_order(self, oid):
            assert oid == "entry-oid"
            monkeypatch.setattr(
                aq.settings,
                "chili_alpaca_expected_account_id",
                "acct-rotated",
            )
            return SimpleNamespace(status="filled", filled_size=125.0), None

    import app.services.trading.venue.factory as venue_factory

    monkeypatch.setattr(venue_factory, "get_adapter", lambda _family: _Adapter())

    result = aq._try_adopt_filled_entry_on_cancel(db, sess)

    assert result is not None
    assert result["pending"] == "execution_quarantine"
    assert result["quarantine_reason"] == "alpaca_account_generation_mismatch"
    assert sess.state == aq.STATE_WATCHING_LIVE
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert le["entry_order_id"] == "entry-oid"
    assert not le.get("entry_orders_resolved")


def test_cancel_adoption_requires_adapter_account_generation_binding(monkeypatch):
    events = []
    sess = _session(
        state=aq.STATE_WATCHING_LIVE,
        frozen_account_id="acct-frozen",
        entry_order_id="entry-oid",
    )
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-frozen")

    class _UnboundAdapter:
        def bind_account_id(self, account_id):
            assert account_id == "acct-frozen"
            return False

        def get_order(self, _oid):
            pytest.fail("unbound Alpaca adapter reached broker order truth")

    import app.services.trading.venue.factory as venue_factory

    monkeypatch.setattr(
        venue_factory,
        "get_adapter",
        lambda _family: _UnboundAdapter(),
    )

    result = aq._try_adopt_filled_entry_on_cancel(db, sess)

    assert result is not None
    assert result["pending"] == "execution_quarantine"
    assert (
        result["quarantine_reason"]
        == "alpaca_adapter_account_generation_bind_failed"
    )
    assert sess.state == aq.STATE_WATCHING_LIVE
    assert not sess.risk_snapshot_json["momentum_live_execution"].get(
        "entry_orders_resolved"
    )


def test_cancel_adoption_pin_rotation_during_cid_read_never_binds(monkeypatch):
    events = []
    sess = _session(
        state=aq.STATE_WATCHING_LIVE,
        frozen_account_id="acct-frozen",
    )
    sess.risk_snapshot_json["momentum_live_execution"].update(
        {
            "entry_submitted": True,
            "entry_client_order_id": "chili-entry-actu",
            "entry_reconcile_pending_client_order_id": "chili-entry-actu",
        }
    )
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-frozen")

    import app.services.trading.momentum_neural.live_runner as live_runner
    import app.services.trading.venue.factory as venue_factory

    def _recover(_adapter, cid):
        assert cid == "chili-entry-actu"
        monkeypatch.setattr(
            aq.settings,
            "chili_alpaca_expected_account_id",
            "acct-rotated",
        )
        return SimpleNamespace(
            order_id="recovered-oid",
            status="filled",
            filled_size=125.0,
        )

    monkeypatch.setattr(live_runner, "_recover_entry_order_by_client_id", _recover)
    monkeypatch.setattr(
        live_runner,
        "_bind_recovered_entry_order",
        lambda *_a, **_k: pytest.fail("rotated generation bound recovered identity"),
    )
    class _Adapter:
        def bind_account_id(self, account_id):
            assert account_id == "acct-frozen"
            return True

    monkeypatch.setattr(venue_factory, "get_adapter", lambda _family: _Adapter())

    result = aq._try_adopt_filled_entry_on_cancel(db, sess)

    assert result is not None
    assert result["pending"] == "execution_quarantine"
    assert result["quarantine_reason"] == "alpaca_account_generation_mismatch"
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert not le.get("entry_order_id")
    assert not le.get("entry_orders_resolved")


def test_alpaca_terminal_death_skips_redundant_unbound_order_cleanup(monkeypatch):
    events = []
    sess = _session(
        state=aq.STATE_WATCHING_LIVE,
        frozen_account_id="acct-frozen",
        entry_order_id="entry-oid",
    )
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-frozen")
    monkeypatch.setattr(
        aq.settings,
        "chili_momentum_adopt_on_cancel_fill_enabled",
        False,
    )
    monkeypatch.setattr(
        aq,
        "_owned_unresolved_alpaca_entry_claim",
        lambda *_a, **_k: (True, None),
    )
    monkeypatch.setattr(
        aq,
        "_flatten_live_session_for_stop",
        lambda *_a, **_k: {
            "ok": True,
            "broker_flat_confirmed": True,
            "action": "broker_flat",
        },
    )

    import app.services.trading.momentum_neural.persistence as persistence
    import app.services.trading.venue.factory as venue_factory

    monkeypatch.setattr(
        venue_factory,
        "get_adapter",
        lambda *_a, **_k: pytest.fail(
            "post-flatten Alpaca terminal cleanup touched an unbound adapter"
        ),
    )
    monkeypatch.setattr(
        persistence,
        "append_trading_automation_event",
        lambda _db, _sid, event_type, payload, **_kwargs: events.append(
            (event_type, payload)
        ),
    )

    result = aq.cancel_automation_session(db, user_id=7, session_id=sess.id)

    assert result["ok"] is True
    assert sess.state == STATE_LIVE_CANCELLED
    cancelled = next(payload for event, payload in events if event == "session_cancelled")
    assert cancelled["order_cleanup"] == {
        "skipped": "alpaca_exact_flatten_completed"
    }


@pytest.mark.parametrize(
    ("claim_result", "counter", "reason"),
    [
        ((False, None), "skipped_unknown", "alpaca_entry_claim_unreadable"),
        (
            (
                True,
                {
                    "claim_token": "claim-entry-actu",
                    "owner_session_id": 55101,
                    "action": "entry",
                    "phase": "bound",
                    "client_order_id": "chili-entry-actu",
                },
            ),
            "skipped_in_flight",
            "alpaca_entry_claim_unresolved",
        ),
    ],
)
def test_stale_live_error_requires_readable_clear_durable_entry_claim(
    monkeypatch,
    claim_result,
    counter,
    reason,
):
    events = []
    sess = _session(
        state=STATE_LIVE_ERROR,
        frozen_account_id="acct-frozen",
    )
    db = _Db(sess)
    _configure(monkeypatch, events, expected_account_id="acct-frozen")
    monkeypatch.setattr(aq, "_reaper_has_working_entry_order", lambda _sess: False)
    monkeypatch.setattr(
        aq,
        "_reaper_broker_position_truth",
        lambda _sess: (True, {"broker_quantity": 0.0}),
    )
    monkeypatch.setattr(
        aq,
        "_owned_unresolved_alpaca_entry_claim",
        lambda *_a, **_k: claim_result,
    )

    result = aq.reap_stale_live_sessions(db, user_id=7)

    assert result["reaped"] == 0
    assert result[counter] == 1
    assert sess.state == STATE_LIVE_ERROR
    assert sess.ended_at is None
    quarantine = sess.risk_snapshot_json["momentum_live_execution"][
        "alpaca_execution_quarantine"
    ]
    assert quarantine["reason"] == reason
    assert quarantine["context"] == "stale_live_session_reaper_pre_terminal"
