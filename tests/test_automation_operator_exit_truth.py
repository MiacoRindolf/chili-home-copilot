from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_CANCELLED,
    STATE_LIVE_ENTERED,
    STATE_LIVE_PENDING_ENTRY,
    STATE_WATCHING_LIVE,
)


_TEST_ALPACA_ACCOUNT_ID = "acct-automation-query-test"


@pytest.fixture(autouse=True)
def _configured_alpaca_account_pin(monkeypatch):
    monkeypatch.setattr(
        aq.settings,
        "chili_alpaca_expected_account_id",
        _TEST_ALPACA_ACCOUNT_ID,
    )


class _Query:
    def __init__(self, sess):
        self.sess = sess

    def filter(self, *_args, **_kwargs):
        return self

    def with_for_update(self, *_args, **_kwargs):
        return self

    def populate_existing(self, *_args, **_kwargs):
        return self

    def one_or_none(self):
        return self.sess


class _Db:
    def __init__(self, sess):
        self.sess = sess
        self.flushes = 0

    def query(self, *_args, **_kwargs):
        return _Query(self.sess)

    def flush(self):
        self.flushes += 1

    def expire(self, _value):
        return None

    def refresh(self, _value):
        return None


class _StrictVisibilityAdapter:
    def __init__(self):
        self.position_readable = True
        self.open_readable = True
        self.open_orders = []

    def get_account_identity_truth(self):
        return {"readable": True, "identity": "operator-test-account-v1"}

    def get_position_quantity_truth(self, _product_id):
        return {
            "readable": self.position_readable,
            "quantity": 0.0 if self.position_readable else None,
        }

    def list_open_orders_truth(self, *, product_id=None, limit=250):
        if not self.open_readable:
            return {"readable": False, "orders": None}
        orders = [
            order
            for order in self.open_orders
            if product_id is None or order.product_id == product_id
        ]
        return {"readable": True, "orders": orders[:limit]}


def _session(
    *,
    family: str = "robinhood_agentic_mcp",
    symbol: str = "ACTU",
    state: str = STATE_LIVE_ENTERED,
    live_exec: dict | None = None,
):
    snap = {"momentum_live_execution": dict(live_exec or {})}
    if family in {"alpaca_spot", "alpaca_short"}:
        snap["alpaca_account_scope"] = "alpaca:paper"
        snap["alpaca_account_id"] = _TEST_ALPACA_ACCOUNT_ID
    else:
        snap["non_alpaca_account_identity"] = "operator-test-account-v1"
    return SimpleNamespace(
        id=123,
        user_id=1,
        mode="live",
        execution_family=family,
        symbol=symbol,
        state=state,
        ended_at=None,
        updated_at=datetime(2026, 7, 13, 16, 0, 0),
        correlation_id="corr-operator-truth",
        risk_snapshot_json=snap,
    )


def _patch_common(monkeypatch, events):
    monkeypatch.setattr(aq, "_tables_present", lambda _db: True)
    monkeypatch.setattr(
        aq,
        "_owned_unresolved_alpaca_entry_claim",
        lambda _db, _sess: (True, None),
    )
    monkeypatch.setattr(
        aq,
        "append_trading_automation_event",
        lambda _db, _sid, event_type, payload, **_kwargs: events.append(
            (event_type, payload)
        ),
    )


def test_operator_stop_held_position_uses_emergency_service_without_local_flat(monkeypatch):
    sess = _session(
        live_exec={
            "position": {
                "product_id": "ACTU",
                "quantity": 100.0,
                "avg_entry_price": 2.0,
            }
        }
    )
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)

    def _tick(_db, session_id):
        assert session_id == sess.id
        le = sess.risk_snapshot_json["momentum_live_execution"]
        assert le["operator_flatten_requested_utc"]
        assert sess.risk_snapshot_json.get("operator_pause")
        # Accepted/submitted is not flat proof: leave the position and state intact.
        return {"ok": True, "operator_flatten": False, "state": sess.state}

    monkeypatch.setattr(aq, "tick_live_session", _tick)

    result = aq.stop_automation_session(db, user_id=1, session_id=sess.id)

    assert result["pending"] == "broker_flat_confirmation"
    assert sess.state == STATE_LIVE_ENTERED
    assert sess.ended_at is None
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"]["quantity"] == 100.0
    assert [event for event, _ in events] == ["operator_stop_emergency_requested"]


def test_operator_cancel_legacy_cid_pauses_and_never_terminalizes(monkeypatch):
    sess = _session(
        family="alpaca_spot",
        state=STATE_WATCHING_LIVE,
        live_exec={
            "entry_submitted": True,
            "entry_client_order_id": "legacy-actu-cid",
        },
    )
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(aq.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        aq,
        "tick_live_session",
        lambda *_a, **_k: pytest.fail("pre-entry legacy identity must not cross broker service"),
    )

    result = aq.cancel_automation_session(db, user_id=1, session_id=sess.id)

    assert result["pending"] == "entry_order_truth_reconcile"
    assert sess.state == STATE_WATCHING_LIVE
    assert sess.ended_at is None
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert le["entry_client_order_id"] == "legacy-actu-cid"
    assert le["operator_cancel_reconcile_requested_utc"]
    assert sess.risk_snapshot_json.get("operator_pause")
    assert [event for event, _ in events] == ["operator_cancel_emergency_requested"]


def test_operator_cancel_pending_entry_is_serviced_but_not_locally_terminalized(monkeypatch):
    sess = _session(
        family="alpaca_spot",
        state=STATE_LIVE_PENDING_ENTRY,
        live_exec={
            "entry_submitted": True,
            "entry_order_id": "alpaca-entry-open",
            "entry_client_order_id": "alpaca-entry-cid",
        },
    )
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(aq.settings, "chili_alpaca_paper", True)
    calls = []

    def _tick(_db, session_id):
        calls.append(session_id)
        return {"ok": True, "flattened": False, "state": sess.state}

    monkeypatch.setattr(aq, "tick_live_session", _tick)

    result = aq.cancel_automation_session(db, user_id=1, session_id=sess.id)

    assert calls == [sess.id]
    assert result["pending"] == "broker_flat_confirmation"
    assert sess.state == STATE_LIVE_PENDING_ENTRY
    assert sess.ended_at is None


def test_live_stop_without_local_identity_needs_explicit_broker_flat(monkeypatch):
    sess = _session(state=STATE_WATCHING_LIVE, live_exec={})
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)
    class _StrictEmptyAdapter:
        def __init__(self):
            self.position_readable = False

        def get_account_identity_truth(self):
            return {"readable": True, "identity": "operator-test-account-v1"}

        def get_position_quantity_truth(self, _product_id):
            return {
                "readable": self.position_readable,
                "quantity": 0.0 if self.position_readable else None,
            }

        def list_open_orders_truth(self, *, product_id=None, limit=250):
            return {"readable": True, "orders": []}

    adapter = _StrictEmptyAdapter()
    monkeypatch.setattr(
        "app.services.trading.venue.factory.get_adapter",
        lambda _family: adapter,
    )

    unknown = aq.stop_automation_session(db, user_id=1, session_id=sess.id)

    assert unknown["pending"] == "broker_terminal_truth_reconcile"
    assert (
        unknown["live_stop"]["quarantine_reason"]
        == "terminalization_position_unknown"
    )
    assert sess.state == STATE_WATCHING_LIVE
    assert sess.ended_at is None

    adapter.position_readable = True
    first_flat = aq.stop_automation_session(db, user_id=1, session_id=sess.id)

    assert first_flat["pending"] == "broker_terminal_truth_reconcile"
    assert (
        first_flat["live_stop"]["quarantine_reason"]
        == "terminalization_identity_loss_stability_pending"
    )
    assert sess.state == STATE_WATCHING_LIVE
    assert sess.ended_at is None

    back_to_back_flat = aq.stop_automation_session(
        db,
        user_id=1,
        session_id=sess.id,
    )

    assert back_to_back_flat["pending"] == "broker_terminal_truth_reconcile"
    assert (
        back_to_back_flat["live_stop"]["quarantine_reason"]
        == "terminalization_identity_loss_stability_pending"
    )
    assert sess.state == STATE_WATCHING_LIVE
    assert sess.ended_at is None

    observation = sess.risk_snapshot_json["momentum_live_execution"][
        "non_alpaca_identity_loss_observation"
    ]
    observation["first_observed_at_utc"] = (
        datetime.utcnow()
        - timedelta(
            seconds=(
                aq._NON_ALPACA_IDENTITY_LOSS_VISIBILITY_GRACE_SECONDS + 1.0
            )
        )
    ).isoformat()

    stable_flat = aq.stop_automation_session(db, user_id=1, session_id=sess.id)

    assert stable_flat["state"] == STATE_LIVE_CANCELLED
    assert sess.state == STATE_LIVE_CANCELLED
    assert sess.ended_at is not None


def test_delayed_visible_order_blocks_identity_loss_terminalization_and_resets_timer(
    monkeypatch,
):
    sess = _session(state=STATE_WATCHING_LIVE, live_exec={})
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)
    adapter = _StrictVisibilityAdapter()
    monkeypatch.setattr(
        "app.services.trading.venue.factory.get_adapter",
        lambda _family: adapter,
    )

    first = aq.stop_automation_session(db, user_id=1, session_id=sess.id)
    assert (
        first["live_stop"]["quarantine_reason"]
        == "terminalization_identity_loss_stability_pending"
    )
    observation = sess.risk_snapshot_json["momentum_live_execution"][
        "non_alpaca_identity_loss_observation"
    ]
    old_first_observed = (
        datetime.utcnow()
        - timedelta(
            seconds=(
                aq._NON_ALPACA_IDENTITY_LOSS_VISIBILITY_GRACE_SECONDS + 1.0
            )
        )
    )
    observation["first_observed_at_utc"] = old_first_observed.isoformat()
    adapter.open_orders = [
        SimpleNamespace(
            order_id="late-visible-order",
            client_order_id="late-visible-cid",
            product_id="ACTU",
            status="open",
            filled_size=0.0,
            side="buy",
            raw={"quantity": 10.0},
        )
    ]

    delayed = aq.stop_automation_session(db, user_id=1, session_id=sess.id)

    assert (
        delayed["live_stop"]["quarantine_reason"]
        == "terminalization_unowned_symbol_order_working"
    )
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert "non_alpaca_identity_loss_observation" not in le
    assert sess.state == STATE_WATCHING_LIVE
    assert sess.ended_at is None

    adapter.open_orders = []
    restarted = aq.stop_automation_session(db, user_id=1, session_id=sess.id)
    assert (
        restarted["live_stop"]["quarantine_reason"]
        == "terminalization_identity_loss_stability_pending"
    )
    restarted_observation = sess.risk_snapshot_json["momentum_live_execution"][
        "non_alpaca_identity_loss_observation"
    ]
    assert datetime.fromisoformat(
        restarted_observation["first_observed_at_utc"]
    ) > old_first_observed
    assert sess.state == STATE_WATCHING_LIVE


def test_unreadable_scan_resets_aged_identity_loss_observation(monkeypatch):
    sess = _session(state=STATE_WATCHING_LIVE, live_exec={})
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)
    adapter = _StrictVisibilityAdapter()
    monkeypatch.setattr(
        "app.services.trading.venue.factory.get_adapter",
        lambda _family: adapter,
    )

    first = aq.stop_automation_session(db, user_id=1, session_id=sess.id)
    assert (
        first["live_stop"]["quarantine_reason"]
        == "terminalization_identity_loss_stability_pending"
    )
    observation = sess.risk_snapshot_json["momentum_live_execution"][
        "non_alpaca_identity_loss_observation"
    ]
    old_first_observed = (
        datetime.utcnow()
        - timedelta(
            seconds=(
                aq._NON_ALPACA_IDENTITY_LOSS_VISIBILITY_GRACE_SECONDS + 1.0
            )
        )
    )
    observation["first_observed_at_utc"] = old_first_observed.isoformat()

    adapter.open_readable = False
    unknown = aq.stop_automation_session(db, user_id=1, session_id=sess.id)
    assert (
        unknown["live_stop"]["quarantine_reason"]
        == "terminalization_symbol_orders_unknown"
    )
    assert "non_alpaca_identity_loss_observation" not in (
        sess.risk_snapshot_json["momentum_live_execution"]
    )

    adapter.open_readable = True
    restarted = aq.stop_automation_session(db, user_id=1, session_id=sess.id)
    assert (
        restarted["live_stop"]["quarantine_reason"]
        == "terminalization_identity_loss_stability_pending"
    )
    restarted_observation = sess.risk_snapshot_json["momentum_live_execution"][
        "non_alpaca_identity_loss_observation"
    ]
    assert datetime.fromisoformat(
        restarted_observation["first_observed_at_utc"]
    ) > old_first_observed
    assert sess.state == STATE_WATCHING_LIVE
    assert sess.ended_at is None


@pytest.mark.parametrize(
    ("family", "symbol", "paper", "expected_reason"),
    [
        ("alpaca_spot", "ACTU", False, "alpaca_live_posture_not_certified"),
        ("alpaca_spot", "BTC-USD", True, "alpaca_crypto_execution_not_certified"),
        ("alpaca_short", "ACTU", True, "alpaca_short_execution_not_certified"),
    ],
)
def test_operator_stop_uncertified_alpaca_row_makes_zero_broker_calls(
    monkeypatch,
    family,
    symbol,
    paper,
    expected_reason,
):
    sess = _session(
        family=family,
        symbol=symbol,
        live_exec={"position": {"product_id": symbol, "quantity": 100.0}},
    )
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(aq.settings, "chili_alpaca_paper", paper)
    monkeypatch.setattr(
        aq,
        "tick_live_session",
        lambda *_a, **_k: pytest.fail("quarantined row made a broker-service call"),
    )
    monkeypatch.setattr(
        aq,
        "_reaper_broker_position_truth",
        lambda *_a, **_k: pytest.fail("quarantined row made a broker-position call"),
    )

    result = aq.stop_automation_session(db, user_id=1, session_id=sess.id)

    assert result["pending"] == "execution_quarantine"
    assert result["terminalization_deferred"] is True
    assert result["quarantine_reason"] == expected_reason
    assert sess.state == STATE_LIVE_ENTERED
    assert sess.ended_at is None
    assert sess.risk_snapshot_json.get("operator_pause")
    assert events[0][0] == "operator_stop_execution_quarantined"


def test_missing_frozen_scope_cancel_is_quarantined_before_any_broker_service(monkeypatch):
    sess = _session(
        family="alpaca_spot",
        state=STATE_LIVE_PENDING_ENTRY,
        live_exec={"entry_client_order_id": "legacy-cid", "entry_submitted": True},
    )
    sess.risk_snapshot_json.pop("alpaca_account_scope")
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)
    monkeypatch.setattr(aq.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(
        aq,
        "tick_live_session",
        lambda *_a, **_k: pytest.fail("unscoped automated cancel reached broker service"),
    )
    monkeypatch.setattr(
        aq,
        "_reaper_broker_position_truth",
        lambda *_a, **_k: pytest.fail("unscoped automated cancel read broker position"),
    )

    result = aq.cancel_automation_session(db, user_id=1, session_id=sess.id)

    assert result["pending"] == "execution_quarantine"
    assert result["terminalization_deferred"] is True
    assert result["quarantine_reason"] == "alpaca_account_scope_unfrozen_or_mismatched"
    assert sess.state == STATE_LIVE_PENDING_ENTRY
    assert sess.risk_snapshot_json.get("operator_pause")


def _forbid_legacy_recertification_io(monkeypatch):
    import app.services.trading.momentum_neural.alpaca_orphan_claims as claims

    def _unexpected(*_args, **_kwargs):
        pytest.fail("missing-scope legacy request touched broker or durable claims")

    monkeypatch.setattr(claims, "acquire_action_claim_committed", _unexpected)
    monkeypatch.setattr(claims, "update_action_claim_phase_committed", _unexpected)
    monkeypatch.setattr(claims, "resolve_action_claim_committed", _unexpected)
    monkeypatch.setattr(
        "app.services.trading.venue.alpaca_spot.AlpacaSpotAdapter",
        _unexpected,
    )
    monkeypatch.setattr(aq, "tick_live_session", _unexpected)


def test_explicit_stop_never_recertifies_legacy_missing_scope_even_when_qty_matches(
    monkeypatch,
):
    sess = _session(
        family="alpaca_spot",
        live_exec={
            "entry_order_id": "old-paper-entry-oid",
            "entry_client_order_id": "old-paper-entry-cid",
            "position": {"product_id": "ACTU", "quantity": 100.0, "side_long": True},
            "side_long": True,
        },
    )
    sess.risk_snapshot_json.pop("alpaca_account_scope")
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)
    _forbid_legacy_recertification_io(monkeypatch)

    result = aq.stop_automation_session(db, user_id=1, session_id=sess.id)

    assert result["pending"] == "execution_quarantine"
    assert result["terminalization_deferred"] is True
    assert result["quarantine_reason"] == "alpaca_account_scope_unfrozen_or_mismatched"
    assert [event for event, _payload in events] == [
        "operator_stop_execution_quarantined"
    ]
    assert "alpaca_account_scope" not in sess.risk_snapshot_json
    assert "alpaca_close_only_recertification" not in sess.risk_snapshot_json
    live_exec = sess.risk_snapshot_json["momentum_live_execution"]
    assert "alpaca_close_only_recertification_stage" not in live_exec
    assert live_exec["position"]["quantity"] == 100.0
    assert sess.risk_snapshot_json.get("operator_pause")


def test_explicit_flatten_never_recertifies_legacy_missing_scope(monkeypatch):
    sess = _session(
        family="alpaca_spot",
        live_exec={
            "entry_order_id": "old-paper-entry-oid",
            "entry_client_order_id": "old-paper-entry-cid",
            "position": {"product_id": "ACTU", "quantity": 100.0, "side_long": True},
            "side_long": True,
        },
    )
    sess.risk_snapshot_json.pop("alpaca_account_scope")
    db = _Db(sess)
    events = []
    _patch_common(monkeypatch, events)
    _forbid_legacy_recertification_io(monkeypatch)

    result = aq.request_flatten_session(db, user_id=1, session_id=sess.id)

    assert result["pending"] == "execution_quarantine"
    assert result["quarantine_reason"] == "alpaca_account_scope_unfrozen_or_mismatched"
    assert [event for event, _payload in events] == [
        "operator_stop_execution_quarantined"
    ]
    assert "alpaca_account_scope" not in sess.risk_snapshot_json
    live_exec = sess.risk_snapshot_json["momentum_live_execution"]
    assert "operator_flatten_requested_utc" not in live_exec
    assert live_exec["position"]["quantity"] == 100.0



def test_terminal_live_run_requires_fresh_rearm_and_never_clones_or_ticks(monkeypatch):
    sess = _session(state="live_finished", live_exec={})
    db = _Db(sess)
    monkeypatch.setattr(aq, "_tables_present", lambda _db: True)
    monkeypatch.setattr(
        aq,
        "_runner_health_for_mode",
        lambda *_a, **_k: pytest.fail("terminal live run should stop before runner work"),
    )
    monkeypatch.setattr(
        aq,
        "_clone_session_for_run",
        lambda *_a, **_k: pytest.fail("terminal live row must not clone"),
    )
    monkeypatch.setattr(
        aq,
        "tick_live_session",
        lambda *_a, **_k: pytest.fail("terminal live row must not tick"),
    )

    result = aq.run_automation_session(db, user_id=1, session_id=sess.id)

    assert result["ok"] is False
    assert result["error"] == "live_rearm_required"
    assert sess.state == "live_finished"


@pytest.mark.parametrize(
    ("family", "side_long", "quantity", "expected_flat", "expected_reason"),
    [
        ("alpaca_short", False, -100.0, False, None),
        ("alpaca_short", False, 0.0, True, None),
        ("alpaca_short", False, 100.0, None, "broker_position_direction_mismatch"),
        ("alpaca_spot", True, -100.0, None, "broker_position_direction_mismatch"),
    ],
)
def test_reaper_signed_quantity_never_hides_opposite_exposure(
    family,
    side_long,
    quantity,
    expected_flat,
    expected_reason,
):
    sess = _session(
        family=family,
        live_exec={"side_long": side_long},
    )

    flat, detail = aq._normalize_reaper_position_quantity(sess, quantity)

    assert flat is expected_flat
    assert detail.get("reason") == expected_reason
