from __future__ import annotations

from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import automation_query as aq
from app.services.trading.momentum_neural.live_fsm import (
    STATE_LIVE_CANCELLED,
    STATE_LIVE_ERROR,
    STATE_WATCHING_LIVE,
)


class _Query:
    def __init__(self, session):
        self.session = session

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

    def one_or_none(self):
        return self.session

    def all(self):
        return [self.session]


class _Db:
    def __init__(self, session):
        self.session = session

    def query(self, *_args, **_kwargs):
        return _Query(self.session)

    def flush(self):
        return None


def _order(
    *,
    oid: str,
    status: str,
    filled: float = 0.0,
    cid: str | None = None,
    symbol: str = "ACTU",
    side: str = "buy",
    quantity: float = 10.0,
):
    return SimpleNamespace(
        order_id=oid,
        client_order_id=cid,
        product_id=symbol,
        status=status,
        filled_size=filled,
        side=side,
        raw={"quantity": quantity},
    )


class _StrictAdapter:
    def __init__(
        self,
        *,
        orders: dict[str, object] | None = None,
        open_orders: list[object] | None = None,
        order_readable: bool = True,
        open_readable: bool = True,
        cancel_ok: bool = True,
        cid_truth: dict | None = None,
        position_readable: bool = True,
        position_quantity: float = 0.0,
        account_readable: bool = True,
        account_identity: str = "test-account-v1",
        cancel_transition: object | None = None,
        on_open_read=None,
    ):
        self.orders = dict(orders or {})
        self.open_orders = list(open_orders or [])
        self.order_readable = order_readable
        self.open_readable = open_readable
        self.cancel_ok = cancel_ok
        self.cid_truth = cid_truth
        self.position_readable = position_readable
        self.position_quantity = position_quantity
        self.account_readable = account_readable
        self.account_identity = account_identity
        self.cancel_transition = cancel_transition
        self.on_open_read = on_open_read
        self.cancel_calls: list[str] = []
        self.account_calls = 0
        self.position_calls = 0
        self.order_calls = 0
        self.open_calls = 0

    def get_account_identity_truth(self):
        self.account_calls += 1
        return {
            "readable": self.account_readable,
            "identity": self.account_identity if self.account_readable else None,
        }

    def get_position_quantity_truth(self, product_id: str):
        self.position_calls += 1
        return {
            "readable": self.position_readable,
            "quantity": self.position_quantity if self.position_readable else None,
        }

    def get_order(self, oid: str):
        return self.orders.get(oid), None

    def get_order_truth(self, oid: str):
        self.order_calls += 1
        if not self.order_readable:
            raise RuntimeError("strict order read failed")
        order = self.orders.get(oid)
        return {
            "readable": True,
            "found": order is not None,
            "order": order,
        }

    def list_open_orders_truth(self, *, product_id=None, limit=250):
        self.open_calls += 1
        if callable(self.on_open_read):
            self.on_open_read(self.open_calls)
        if not self.open_readable:
            return {"readable": False, "orders": None}
        orders = [
            order
            for order in self.open_orders
            if product_id is None or order.product_id == product_id
        ]
        return {"readable": True, "orders": orders[:limit]}

    def get_order_by_client_order_id_truth(self, cid: str):
        if self.cid_truth is not None:
            return dict(self.cid_truth)
        order = next(
            (
                order
                for order in self.orders.values()
                if getattr(order, "client_order_id", None) == cid
            ),
            None,
        )
        return {
            "readable": True,
            "found": order is not None,
            "order": order,
        }

    def cancel_order(self, oid: str):
        self.cancel_calls.append(oid)
        if not self.cancel_ok:
            return {"ok": False, "error": "cancel failed"}
        prior = self.orders[oid]
        terminal = self.cancel_transition or _order(
            oid=oid,
            status="cancelled",
            filled=0.0,
            cid=getattr(prior, "client_order_id", None),
            symbol=prior.product_id,
            side=getattr(prior, "side", "buy"),
            quantity=float(getattr(prior, "raw", {}).get("quantity") or 10.0),
        )
        self.orders[oid] = terminal
        self.open_orders = [
            order for order in self.open_orders if order.order_id != oid
        ]
        return {"ok": True}


def _session(
    *,
    state: str = STATE_WATCHING_LIVE,
    live_exec: object = None,
    account_identity: str | None = "test-account-v1",
):
    old = datetime.utcnow() - timedelta(hours=4)
    snapshot = {
        "momentum_live_execution": (
            {} if live_exec is None else live_exec
        )
    }
    if account_identity is not None:
        snapshot["non_alpaca_account_identity"] = account_identity
    return SimpleNamespace(
        id=78101,
        user_id=1,
        mode="live",
        venue="robinhood",
        execution_family="robinhood_agentic_mcp",
        symbol="ACTU",
        state=state,
        risk_snapshot_json=snapshot,
        correlation_id="corr-non-alpaca-terminal-truth",
        started_at=old,
        created_at=old,
        updated_at=old,
        ended_at=None,
    )


def _configure(monkeypatch, session, events, adapter):
    monkeypatch.setattr(aq, "_tables_present", lambda _db: True)
    monkeypatch.setattr(
        aq.settings,
        "chili_momentum_adopt_on_cancel_fill_enabled",
        False,
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
        "_owned_unresolved_alpaca_entry_claim",
        lambda *_args, **_kwargs: (True, None),
    )
    monkeypatch.setattr(
        aq,
        "_reaper_broker_position_truth",
        lambda _sess: (True, {"broker_quantity": 0.0}),
    )
    recorder = lambda _db, _sid, event_type, payload, **_kwargs: events.append(
        (event_type, payload)
    )
    monkeypatch.setattr(aq, "append_trading_automation_event", recorder)
    from app.services.trading.momentum_neural import persistence
    from app.services.trading.momentum_neural import live_runner

    monkeypatch.setattr(persistence, "append_trading_automation_event", recorder)
    monkeypatch.setattr(live_runner, "append_trading_automation_event", recorder)
    import app.services.trading.venue.factory as venue_factory

    monkeypatch.setattr(venue_factory, "get_adapter", lambda _family: adapter)


def _assert_nonterminal_uncertainty(session, events):
    assert session.state != STATE_LIVE_CANCELLED
    assert session.ended_at is None
    assert session.risk_snapshot_json.get("operator_pause")
    forbidden = {"session_cancelled", "live_cancelled", "session_stopped", "stale_session_reaped"}
    assert not any(event in forbidden for event, _payload in events)
    assert any(event == "live_terminalization_quarantined" for event, _ in events)


def test_cancel_missing_adapter_persists_quarantine_and_never_terminalizes(monkeypatch):
    session = _session(live_exec={"entry_order_id": "entry-oid"})
    db = _Db(session)
    events = []
    _configure(monkeypatch, session, events, adapter=None)

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["pending"] == "broker_terminal_truth_reconcile"
    assert result["quarantine_reason"] == "terminalization_adapter_missing"
    _assert_nonterminal_uncertainty(session, events)


def test_legacy_missing_account_identity_quarantines_before_any_adapter_io(monkeypatch):
    session = _session(
        live_exec={"entry_order_id": "entry-oid"},
        account_identity=None,
    )
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        orders={"entry-oid": _order(oid="entry-oid", status="cancelled")},
    )
    _configure(monkeypatch, session, events, adapter)

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["quarantine_reason"] == "non_alpaca_account_identity_unfrozen"
    assert adapter.account_calls == 0
    assert adapter.position_calls == 0
    assert adapter.order_calls == 0
    assert adapter.open_calls == 0
    assert adapter.cancel_calls == []
    _assert_nonterminal_uncertainty(session, events)


def test_account_generation_mismatch_blocks_position_order_and_cancel(monkeypatch):
    session = _session(
        live_exec={"entry_order_id": "entry-oid"},
        account_identity="frozen-account-v1",
    )
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        orders={"entry-oid": _order(oid="entry-oid", status="open")},
        account_identity="current-account-v2",
    )
    _configure(monkeypatch, session, events, adapter)

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["quarantine_reason"] == "non_alpaca_account_identity_mismatch"
    assert adapter.account_calls == 1
    assert adapter.position_calls == 0
    assert adapter.order_calls == 0
    assert adapter.open_calls == 0
    assert adapter.cancel_calls == []
    _assert_nonterminal_uncertainty(session, events)


def test_unreadable_strict_position_never_certifies_flat(monkeypatch):
    session = _session(live_exec={})
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(position_readable=False)
    _configure(monkeypatch, session, events, adapter)

    result = aq.stop_automation_session(db, user_id=1, session_id=session.id)

    assert result["live_stop"]["quarantine_reason"] == "terminalization_position_unknown"
    assert adapter.open_calls == 0
    assert adapter.cancel_calls == []
    _assert_nonterminal_uncertainty(session, events)


def test_cancel_raised_order_read_never_becomes_absence(monkeypatch):
    session = _session(live_exec={"entry_order_id": "entry-oid"})
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(order_readable=False, open_orders=[])
    _configure(monkeypatch, session, events, adapter)

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["quarantine_reason"] == "terminalization_persisted_order_unknown"
    assert adapter.cancel_calls == []
    _assert_nonterminal_uncertainty(session, events)


def test_cancel_active_zero_fill_with_cancel_failure_stays_nonterminal(monkeypatch):
    active = _order(oid="entry-oid", status="open", cid="entry-cid")
    session = _session(
        live_exec={
            "entry_order_id": "entry-oid",
            "entry_client_order_id": "entry-cid",
            "entry_want_qty": 10.0,
            "entry_submitted": True,
        }
    )
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        orders={"entry-oid": active},
        open_orders=[active],
        cancel_ok=False,
    )
    _configure(monkeypatch, session, events, adapter)

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["quarantine_reason"] == "terminalization_order_cancel_failed"
    assert adapter.cancel_calls == ["entry-oid"]
    _assert_nonterminal_uncertainty(session, events)


@pytest.mark.parametrize(
    ("observed_side", "observed_quantity"),
    [("sell", 10.0), ("buy", 9.0)],
)
def test_cancel_requires_exact_side_quantity_and_client_identity(
    monkeypatch,
    observed_side,
    observed_quantity,
):
    active = _order(
        oid="entry-oid",
        status="open",
        cid="entry-cid",
        side=observed_side,
        quantity=observed_quantity,
    )
    session = _session(
        live_exec={
            "entry_order_id": "entry-oid",
            "entry_client_order_id": "entry-cid",
            "entry_want_qty": 10.0,
            "entry_submitted": True,
        }
    )
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        orders={"entry-oid": active},
        open_orders=[active],
    )
    _configure(monkeypatch, session, events, adapter)

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["quarantine_reason"] == "terminalization_order_cancel_authority_unproven"
    assert adapter.cancel_calls == []
    _assert_nonterminal_uncertainty(session, events)


def test_filled_status_with_zero_quantity_is_not_terminal_no_fill(monkeypatch):
    filled_zero = _order(oid="entry-oid", status="filled", filled=0.0)
    session = _session(live_exec={"entry_order_id": "entry-oid"})
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(orders={"entry-oid": filled_zero})
    _configure(monkeypatch, session, events, adapter)

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["quarantine_reason"] == "terminalization_filled_order_requires_management"
    assert adapter.cancel_calls == []
    _assert_nonterminal_uncertainty(session, events)


def test_cancel_reread_fill_is_adopted_instead_of_orphaned(monkeypatch):
    active = _order(
        oid="entry-oid",
        status="open",
        cid="entry-cid",
        side="buy",
        quantity=10.0,
    )
    filled = _order(
        oid="entry-oid",
        status="filled",
        filled=10.0,
        cid="entry-cid",
        side="buy",
        quantity=10.0,
    )
    session = _session(
        live_exec={
            "entry_order_id": "entry-oid",
            "entry_order_ids_all": ["entry-oid"],
            "entry_client_order_id": "entry-cid",
            "entry_want_qty": 10.0,
            "entry_submitted": True,
        }
    )
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        orders={"entry-oid": active},
        open_orders=[active],
        cancel_transition=filled,
    )
    _configure(monkeypatch, session, events, adapter)
    monkeypatch.setattr(
        aq.settings,
        "chili_momentum_adopt_on_cancel_fill_enabled",
        True,
    )

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["adopted"] is True
    assert result["state"] == aq.STATE_LIVE_PENDING_ENTRY
    assert adapter.cancel_calls == ["entry-oid"]
    le = session.risk_snapshot_json["momentum_live_execution"]
    assert le["entry_orders_resolved"]["entry-oid"] == "adopted"
    assert not any(event == "session_cancelled" for event, _ in events)


@pytest.mark.parametrize(
    ("case", "observed_cid", "observed_side", "observed_quantity"),
    [
        ("wrong_client_id", "other-cid", "buy", 10.0),
        ("wrong_side", "entry-cid", "sell", 10.0),
        ("wrong_total_quantity", "entry-cid", "buy", 9.0),
        ("same_symbol_unbound_oid", "entry-cid", "buy", 10.0),
    ],
)
def test_positive_fill_adoption_requires_exact_persisted_entry_authority(
    monkeypatch,
    case,
    observed_cid,
    observed_side,
    observed_quantity,
):
    filled = _order(
        oid="entry-oid",
        status="filled",
        filled=4.0,
        cid=observed_cid,
        side=observed_side,
        quantity=observed_quantity,
    )
    live_exec = {
        "entry_order_id": "entry-oid",
        "entry_order_ids_all": ["entry-oid"],
        "entry_client_order_id": "entry-cid",
        "entry_want_qty": 10.0,
        "entry_submitted": True,
    }
    if case == "same_symbol_unbound_oid":
        # The OID is merely present in history, not the exact active entry
        # expectation. Same symbol alone must never grant adoption authority.
        live_exec.pop("entry_order_id")
    session = _session(live_exec=live_exec)
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(orders={"entry-oid": filled})
    _configure(monkeypatch, session, events, adapter)
    monkeypatch.setattr(
        aq.settings,
        "chili_momentum_adopt_on_cancel_fill_enabled",
        True,
    )

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert (
        result["quarantine_reason"]
        == "terminalization_filled_entry_adoption_authority_unproven"
    )
    assert session.state == STATE_WATCHING_LIVE
    le = session.risk_snapshot_json["momentum_live_execution"]
    assert "entry_orders_resolved" not in le
    assert not any(event == "entry_adopted_on_cancel" for event, _ in events)
    _assert_nonterminal_uncertainty(session, events)


def test_matching_partial_fill_is_adopted_with_exact_total_order_authority(
    monkeypatch,
):
    partial = _order(
        oid="entry-oid",
        status="open",
        filled=4.0,
        cid="entry-cid",
        side="buy",
        quantity=10.0,
    )
    session = _session(
        live_exec={
            "entry_order_id": "entry-oid",
            "entry_order_ids_all": ["entry-oid"],
            "entry_client_order_id": "entry-cid",
            "entry_want_qty": 10.0,
            "entry_submitted": True,
        }
    )
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        orders={"entry-oid": partial},
        open_orders=[partial],
    )
    _configure(monkeypatch, session, events, adapter)
    monkeypatch.setattr(
        aq.settings,
        "chili_momentum_adopt_on_cancel_fill_enabled",
        True,
    )

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["adopted"] is True
    assert result["state"] == aq.STATE_LIVE_PENDING_ENTRY
    assert adapter.cancel_calls == []
    le = session.risk_snapshot_json["momentum_live_execution"]
    assert le["entry_orders_resolved"]["entry-oid"] == "adopted"
    adopted_payload = next(
        payload
        for event, payload in events
        if event == "entry_adopted_on_cancel"
    )
    assert adopted_payload["filled_size"] == pytest.approx(4.0)


def test_unscoped_global_idempotency_mapping_is_never_cancel_authority(monkeypatch):
    cid = "cid-only"
    session = _session(
        live_exec={
            "entry_submitted": True,
            "entry_client_order_id": cid,
            "entry_reconcile_pending_client_order_id": cid,
        }
    )
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        cid_truth={"readable": False, "found": False, "order": None},
    )
    _configure(monkeypatch, session, events, adapter)
    from app.services.trading.venue import idempotency_store

    monkeypatch.setattr(
        idempotency_store,
        "resolve_broker_id",
        lambda _cid: pytest.fail("global CID mapping was used as broker authority"),
    )

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["quarantine_reason"] == "terminalization_client_order_unknown"
    assert adapter.cancel_calls == []
    _assert_nonterminal_uncertainty(session, events)


@pytest.mark.parametrize("open_readable", [True, False])
def test_stop_corrupt_identity_json_requires_strict_symbol_absence(
    monkeypatch,
    open_readable,
):
    working = _order(oid="unknown-working", status="open")
    session = _session(live_exec="corrupt-json")
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        open_orders=([working] if open_readable else []),
        open_readable=open_readable,
    )
    _configure(monkeypatch, session, events, adapter)

    result = aq.stop_automation_session(db, user_id=1, session_id=session.id)

    assert result["pending"] == "broker_terminal_truth_reconcile"
    assert session.risk_snapshot_json["momentum_live_execution"][
        "non_alpaca_terminalization_quarantine"
    ]
    _assert_nonterminal_uncertainty(session, events)


@pytest.mark.parametrize("cid_mode", ["working", "unknown"])
def test_stale_live_error_cid_only_submitted_row_never_terminalizes_on_uncertainty(
    monkeypatch,
    cid_mode,
):
    cid = "cid-only-submitted"
    active = _order(oid="cid-recovered-oid", status="open", cid=cid)
    session = _session(
        state=STATE_LIVE_ERROR,
        live_exec={
            "entry_submitted": True,
            "entry_client_order_id": cid,
            "entry_reconcile_pending_client_order_id": cid,
        },
    )
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        orders=({active.order_id: active} if cid_mode == "working" else {}),
        open_orders=([active] if cid_mode == "working" else []),
        cancel_ok=False,
        cid_truth=(
            None
            if cid_mode == "working"
            else {"readable": False, "found": False, "order": None}
        ),
    )
    _configure(monkeypatch, session, events, adapter)

    result = aq.reap_stale_live_sessions(db, user_id=1)

    assert result["reaped"] == 0
    _assert_nonterminal_uncertainty(session, events)


def test_terminal_zero_fill_plus_stable_flat_and_no_open_order_can_complete(monkeypatch):
    terminal = _order(oid="entry-oid", status="cancelled", filled=0.0)
    session = _session(live_exec={"entry_order_id": "entry-oid"})
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        orders={"entry-oid": terminal},
        open_orders=[],
    )
    _configure(monkeypatch, session, events, adapter)

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["ok"] is True
    assert session.state == STATE_LIVE_CANCELLED
    assert session.ended_at is not None
    proof = session.risk_snapshot_json["momentum_live_execution"][
        "non_alpaca_terminalization_proof"
    ]
    assert proof["broker_flat_confirmed"] is True
    assert proof["working_symbol_orders_absent"] is True
    assert adapter.cancel_calls == []
    assert any(event == "session_cancelled" for event, _ in events)


def test_local_order_generation_change_during_proof_blocks_terminal_mutation(monkeypatch):
    terminal = _order(oid="entry-oid", status="cancelled", filled=0.0)
    session = _session(live_exec={"entry_order_id": "entry-oid"})

    def _mutate_on_final_open_read(call_number):
        if call_number == 2:
            session.risk_snapshot_json["momentum_live_execution"][
                "exit_order_id"
            ] = "new-exit-oid"

    db = _Db(session)
    events = []
    adapter = _StrictAdapter(
        orders={"entry-oid": terminal},
        open_orders=[],
        on_open_read=_mutate_on_final_open_read,
    )
    _configure(monkeypatch, session, events, adapter)

    result = aq.cancel_automation_session(db, user_id=1, session_id=session.id)

    assert result["quarantine_reason"] == "terminalization_session_generation_changed"
    _assert_nonterminal_uncertainty(session, events)


@pytest.mark.parametrize("terminalizer", ["cancel", "stop", "reaper_direct"])
def test_account_rotation_after_proof_before_terminal_mutation_is_quarantined(
    monkeypatch,
    terminalizer,
):
    if terminalizer == "stop":
        oid = "exit-oid"
        live_exec = {"exit_order_id": oid}
        state = STATE_WATCHING_LIVE
    else:
        oid = "entry-oid"
        live_exec = {"entry_order_id": oid}
        state = STATE_LIVE_ERROR if terminalizer == "reaper_direct" else STATE_WATCHING_LIVE
    terminal = _order(oid=oid, status="cancelled", filled=0.0)
    session = _session(state=state, live_exec=live_exec)
    db = _Db(session)
    events = []
    adapter = _StrictAdapter(orders={oid: terminal}, open_orders=[])
    _configure(monkeypatch, session, events, adapter)

    original_match = aq._non_alpaca_terminal_proof_matches_session

    def _match_then_rotate_account(sess, proof):
        matched = original_match(sess, proof)
        if matched:
            adapter.account_identity = "rotated-account-v2"
        return matched

    monkeypatch.setattr(
        aq,
        "_non_alpaca_terminal_proof_matches_session",
        _match_then_rotate_account,
    )

    if terminalizer == "cancel":
        result = aq.cancel_automation_session(
            db,
            user_id=1,
            session_id=session.id,
        )
        assert result["quarantine_reason"] == "non_alpaca_account_identity_mismatch"
    elif terminalizer == "stop":
        result = aq.stop_automation_session(
            db,
            user_id=1,
            session_id=session.id,
        )
        assert result["quarantine_reason"] == "non_alpaca_account_identity_mismatch"
    else:
        result = aq.reap_stale_live_sessions(db, user_id=1)
        assert result["reaped"] == 0
        assert result["skipped_execution_quarantine"] == 1
        quarantine = session.risk_snapshot_json["momentum_live_execution"][
            "non_alpaca_terminalization_quarantine"
        ]
        assert quarantine["reason"] == "non_alpaca_account_identity_mismatch"

    quarantine = session.risk_snapshot_json["momentum_live_execution"][
        "non_alpaca_terminalization_quarantine"
    ]
    assert quarantine["detail"]["phase"] == "immediately_before_terminal_state_mutation"
    assert adapter.account_calls > 0
    _assert_nonterminal_uncertainty(session, events)


def test_coinbase_strict_open_scan_is_single_unfiltered_and_unknown_blocks(monkeypatch):
    from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter

    calls = []

    class _Client:
        def list_orders(self, **kwargs):
            calls.append(dict(kwargs))
            return {
                "orders": [
                    {
                        "order_id": "cb-unknown",
                        "client_order_id": "cb-cid",
                        "product_id": "BTC-USD",
                        "side": "BUY",
                        "status": "UNKNOWN_ORDER_STATUS",
                        "filled_size": "0",
                        "order_configuration": {
                            "limit_limit_gtc": {"base_size": "1"}
                        },
                    }
                ],
                "has_next": False,
                "cursor": "",
            }

    adapter = CoinbaseSpotAdapter(client_factory=lambda: _Client())
    monkeypatch.setattr(adapter, "is_enabled", lambda: True)

    truth = adapter.list_open_orders_truth(product_id="BTC-USD", limit=250)

    assert truth["readable"] is True
    assert [order.order_id for order in truth["orders"]] == ["cb-unknown"]
    assert len(calls) == 1
    assert "order_status" not in calls[0]


def test_coinbase_account_truth_rejects_conflicting_currency_labels(monkeypatch):
    from app.services.trading.venue.coinbase_spot import CoinbaseSpotAdapter

    class _Client:
        def get_accounts(self, **_kwargs):
            return {
                "accounts": [
                    {
                        "uuid": "wallet-1",
                        "retail_portfolio_id": "portfolio-1",
                        "currency": "BTC",
                        "available_balance": {"currency": "ETH", "value": "0"},
                        "hold": {"currency": "BTC", "value": "0"},
                    }
                ],
                "has_next": False,
                "cursor": "",
            }

    adapter = CoinbaseSpotAdapter(client_factory=lambda: _Client())
    monkeypatch.setattr(adapter, "is_enabled", lambda: True)

    assert adapter.get_account_identity_truth() == {
        "readable": False,
        "identity": None,
    }
    assert adapter.get_position_quantity_truth("BTC-USD") == {
        "readable": False,
        "quantity": None,
    }


def test_rh_mcp_absence_requires_explicit_nonpaginated_completeness():
    from app.services.trading.venue.robinhood_mcp import _strict_mcp_collection

    assert _strict_mcp_collection([], "orders") is None
    assert _strict_mcp_collection(
        {"orders": [], "has_next": False},
        "orders",
    ) is None
    assert _strict_mcp_collection(
        {"orders": [], "complete": True},
        "orders",
    ) == []
    assert _strict_mcp_collection(
        [],
        "orders",
        known_identity_query=True,
    ) == []


def test_robinhood_spot_strict_float_and_instrument_order_normalization():
    from app.services.trading.venue.robinhood_spot import (
        _normalize_rh_order_truth,
        _sf,
    )

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"symbol": "ACTU"}

    rh = SimpleNamespace(
        helper=SimpleNamespace(
            SESSION=SimpleNamespace(get=lambda *_args, **_kwargs: _Response())
        )
    )
    row = {
        "id": "rh-oid",
        "instrument": "https://api.robinhood.test/instruments/actu/",
        "state": "queued",
        "side": "buy",
        "cumulative_quantity": "0",
    }

    assert _sf("1.25") == 1.25
    assert _sf("not-a-number") is None
    order = _normalize_rh_order_truth(row, rh=rh)
    assert order is not None
    assert order.product_id == "ACTU"
    assert _normalize_rh_order_truth(
        {key: value for key, value in row.items() if key != "cumulative_quantity"},
        rh=rh,
    ) is None
