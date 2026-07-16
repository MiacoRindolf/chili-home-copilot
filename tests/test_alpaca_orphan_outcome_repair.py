from __future__ import annotations

from contextlib import nullcontext
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.trading.momentum_neural import alpaca_reconcile as ar
from app.services.trading.venue import alpaca_spot as alpaca_spot_mod
from app.services.trading.momentum_neural.live_fsm import STATE_LIVE_CANCELLED
from app.services.trading.momentum_neural.outcome_extract import derive_outcome_class
from app.services.trading.momentum_neural.outcome_labels import OUTCOME_GOVERNANCE_EXIT


class _FakeDb:
    def __init__(self) -> None:
        self.added = []

    def begin_nested(self):
        return nullcontext()

    def add(self, value) -> None:
        self.added.append(value)

    def execute(self, statement, params=None):
        sql = str(statement)
        params = params or {}
        if "SELECT execution_family, upper(symbol)" in sql:
            row = ("alpaca_spot", "ACTU", "alpaca:paper")
        elif "settle_orphan_position" in sql:
            source_event_id = int(params.get("source_event_id") or 0)
            row = next(
                (
                    (1,)
                    for event in self.added
                    if getattr(event, "event_type", None) == "alpaca_orphan_reconcile"
                    and isinstance(getattr(event, "payload_json", None), dict)
                    and event.payload_json.get("action") == "settle_orphan_position"
                    and int(event.payload_json.get("source_event_id") or 0) == source_event_id
                ),
                None,
            )
        else:
            raise AssertionError(f"unexpected fake SQL: {sql}")
        return SimpleNamespace(fetchone=lambda: row)


class _Adapter:
    def __init__(self, order) -> None:
        self.order = order

    def get_order(self, _order_id):
        return self.order, None

    def get_order_by_client_order_id(self, _client_order_id):
        return self.order, None


def _objects():
    started = datetime(2026, 7, 13, 16, 0, 0)
    sess = SimpleNamespace(
        id=123,
        mode="live",
        execution_family="alpaca_spot",
        symbol="ACTU",
        started_at=started,
        ended_at=started + timedelta(minutes=2),
        correlation_id="corr-actu",
        source_node_id="momentum-exec",
        risk_snapshot_json={
            "alpaca_account_scope": "alpaca:paper",
            "momentum_live_execution": {
                "entry_decision_packet_id": 777,
                "realized_pnl_usd": None,
                "position": None,
            }
        },
    )
    outcome = SimpleNamespace(
        outcome_class="cancelled_pre_entry",
        realized_pnl_usd=None,
        return_bps=None,
        exit_reason=None,
        extracted_summary_json={
            "entry_occurred": False,
            "entry_decision_packet_id": 777,
            "evolution_credit": {
                "contributes_to_evolution": False,
                "reason_codes": [
                    "no_entry",
                    "missing_economic_result",
                    "non_strategy_outcome_cancelled_pre_entry",
                ],
            },
        },
        broker_recon_status=None,
        terminal_at=started + timedelta(minutes=2),
        hold_seconds=120,
        contributes_to_evolution=False,
    )
    return sess, outcome


def _pending(qty=17_991.0):
    return [{
        "event_id": 901,
        "session_id": 123,
        "payload": {
            "action": "flatten_orphan_position",
            "ok": True,
            "symbol": "ACTU",
            "qty": qty,
            "entry_price": 1.48,
            "repair_eligible": True,
            "entry_order_id": "entry-actu",
            "entry_client_order_id": "cid-actu",
            "entry_filled_at_utc": "2026-07-13T16:03:57.073291Z",
            "order_id": "exit-actu",
            "client_order_id": "orphrec-ACTU-202607131625",
        },
    }]


def _filled_order(qty=17_991.0):
    return SimpleNamespace(
        order_id="exit-actu",
        client_order_id="orphrec-ACTU-202607131625",
        product_id="ACTU",
        side="sell",
        status="filled",
        filled_size=qty,
        average_filled_price=1.41,
        raw={"filled_at": "2026-07-13T16:25:07.852185Z"},
    )


def test_empty_postgres_pending_read_is_valid(db):
    assert ar._pending_orphan_flatten_events(db) == []


def test_broker_filled_orphan_repairs_cancelled_pre_entry_truth(monkeypatch):
    db = _FakeDb()
    sess, outcome = _objects()
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", lambda _db: _pending())
    monkeypatch.setattr(ar, "_load_session_outcome_for_update", lambda _db, _sid: (sess, outcome))

    result = ar._settle_submitted_orphan_flattens(db, _Adapter(_filled_order()))

    expected_pnl = (1.41 - 1.48) * 17_991.0
    assert result == {"orphan_fills_settled": 1, "outcomes_repaired": 1, "settlement_pending": 0}
    assert outcome.realized_pnl_usd == pytest.approx(expected_pnl)
    assert outcome.return_bps == pytest.approx(expected_pnl / (1.48 * 17_991.0) * 10_000.0)
    assert outcome.outcome_class == OUTCOME_GOVERNANCE_EXIT
    assert outcome.exit_reason == "alpaca_orphan_reconcile"
    assert outcome.extracted_summary_json["entry_occurred"] is True
    assert outcome.contributes_to_evolution is False
    assert outcome.broker_recon_status == "fee_unconfirmed"
    assert outcome.broker_realized_pnl_usd == pytest.approx(expected_pnl)
    assert outcome.broker_divergence_usd is None
    exact_exit_time = datetime(2026, 7, 13, 16, 25, 7, 852185)
    assert outcome.terminal_at == exact_exit_time
    assert outcome.hold_seconds == 1_270
    assert sess.ended_at == exact_exit_time
    le = sess.risk_snapshot_json["momentum_live_execution"]
    assert le["position"] is None
    assert le["realized_pnl_usd"] == pytest.approx(expected_pnl)
    assert le["last_exit_reason"] == "alpaca_orphan_reconcile"
    truth = le["orphan_reconcile_truth"]
    assert truth["entry_order_id"] == "entry-actu"
    assert truth["entry_client_order_id"] == "cid-actu"
    assert truth["entry_filled_at_utc"] == "2026-07-13T16:03:57.073291Z"
    assert truth["exit_order_id"] == "exit-actu"
    assert truth["exit_client_order_id"] == "orphrec-ACTU-202607131625"
    assert truth["filled_at_utc"] == "2026-07-13T16:25:07.852185Z"
    assert truth["hold_seconds"] == 1_270
    assert {event.event_type for event in db.added} == {
        "alpaca_orphan_reconcile",
        "live_exit_filled",
    }
    settle = next(event for event in db.added if event.event_type == "alpaca_orphan_reconcile")
    assert settle.payload_json["accounting_repaired"] is True
    assert settle.payload_json["source_event_id"] == 901
    assert settle.payload_json["filled_at_utc"] == "2026-07-13T16:25:07.852185Z"
    live_exit = next(event for event in db.added if event.event_type == "live_exit_filled")
    assert live_exit.ts == exact_exit_time
    assert live_exit.payload_json["filled_at_utc"] == "2026-07-13T16:25:07.852185Z"
    assert live_exit.payload_json["entry_filled_at_utc"] == "2026-07-13T16:03:57.073291Z"
    assert live_exit.payload_json["client_order_id"] == "orphrec-ACTU-202607131625"


def test_qty_mismatch_is_audited_but_never_rewrites_outcome(monkeypatch):
    db = _FakeDb()
    sess, outcome = _objects()
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", lambda _db: _pending())
    monkeypatch.setattr(ar, "_load_session_outcome_for_update", lambda _db, _sid: (sess, outcome))

    result = ar._settle_submitted_orphan_flattens(db, _Adapter(_filled_order(qty=10_000.0)))

    assert result == {"orphan_fills_settled": 1, "outcomes_repaired": 0, "settlement_pending": 0}
    assert outcome.outcome_class == "cancelled_pre_entry"
    assert outcome.realized_pnl_usd is None
    assert len(db.added) == 1
    assert db.added[0].payload_json["accounting_repaired"] is False
    assert db.added[0].payload_json["reason"] == "filled_qty_mismatch"


@pytest.mark.parametrize("filled_at", [None, "not-a-broker-time", "2026-07-13T16:25:07"])
def test_missing_or_unparseable_exit_fill_time_stays_pending_without_marker_or_mutation(
    monkeypatch,
    filled_at,
):
    db = _FakeDb()
    sess, outcome = _objects()
    order = _filled_order()
    order.raw = {"filled_at": filled_at}
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", lambda _db: _pending())
    monkeypatch.setattr(ar, "_load_session_outcome_for_update", lambda _db, _sid: (sess, outcome))

    result = ar._settle_submitted_orphan_flattens(db, _Adapter(order))

    assert result == {"orphan_fills_settled": 0, "outcomes_repaired": 0, "settlement_pending": 1}
    assert db.added == []
    assert outcome.outcome_class == "cancelled_pre_entry"
    assert outcome.realized_pnl_usd is None
    assert sess.risk_snapshot_json["momentum_live_execution"]["position"] is None


def test_exit_order_identity_mismatch_stays_pending_without_marker_or_mutation(monkeypatch):
    db = _FakeDb()
    sess, outcome = _objects()
    order = _filled_order()
    order.order_id = "some-other-exit"
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", lambda _db: _pending())
    monkeypatch.setattr(ar, "_load_session_outcome_for_update", lambda _db, _sid: (sess, outcome))

    result = ar._settle_submitted_orphan_flattens(db, _Adapter(order))

    assert result == {"orphan_fills_settled": 0, "outcomes_repaired": 0, "settlement_pending": 1}
    assert db.added == []
    assert outcome.outcome_class == "cancelled_pre_entry"


def test_impossible_entry_after_exit_chronology_stays_pending_without_marker(monkeypatch):
    db = _FakeDb()
    sess, outcome = _objects()
    pending = _pending()
    pending[0]["payload"]["entry_filled_at_utc"] = "2026-07-13T16:30:00Z"
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", lambda _db: pending)
    monkeypatch.setattr(ar, "_load_session_outcome_for_update", lambda _db, _sid: (sess, outcome))

    result = ar._settle_submitted_orphan_flattens(db, _Adapter(_filled_order()))

    assert result == {"orphan_fills_settled": 0, "outcomes_repaired": 0, "settlement_pending": 1}
    assert db.added == []
    assert outcome.outcome_class == "cancelled_pre_entry"


def test_zero_legacy_pnl_reports_real_broker_divergence(monkeypatch):
    db = _FakeDb()
    sess, outcome = _objects()
    outcome.realized_pnl_usd = 0.0
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", lambda _db: _pending())
    monkeypatch.setattr(ar, "_load_session_outcome_for_update", lambda _db, _sid: (sess, outcome))

    ar._settle_submitted_orphan_flattens(db, _Adapter(_filled_order()))

    expected_pnl = (1.41 - 1.48) * 17_991.0
    assert outcome.broker_divergence_usd == pytest.approx(expected_pnl)


def test_source_event_scope_cannot_process_another_pending_event(monkeypatch):
    db = _FakeDb()
    sess, outcome = _objects()
    two = _pending() + [{
        "event_id": 902,
        "session_id": 99999,
        "payload": {**_pending()[0]["payload"], "order_id": "exit-other"},
    }]
    observed_scope = []

    def _pending_scoped(_db, *, source_event_id=None):
        observed_scope.append(source_event_id)
        return two  # defense-in-depth must still discard 902

    adapter = _Adapter(_filled_order())
    adapter.calls = []
    original_get = adapter.get_order

    def _get_order(order_id):
        adapter.calls.append(order_id)
        return original_get(order_id)

    adapter.get_order = _get_order
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", _pending_scoped)
    monkeypatch.setattr(ar, "_load_session_outcome_for_update", lambda _db, _sid: (sess, outcome))

    result = ar._settle_submitted_orphan_flattens(db, adapter, source_event_id=901)

    assert observed_scope == [901]
    assert adapter.calls == ["exit-actu"]
    assert result["outcomes_repaired"] == 1
    assert all(getattr(event, "session_id", None) != 99999 for event in db.added)


def test_second_settlement_invocation_with_durable_marker_is_noop(monkeypatch):
    db = _FakeDb()
    sess, outcome = _objects()
    sweeps = iter([_pending(), []])
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", lambda _db: next(sweeps))
    monkeypatch.setattr(ar, "_load_session_outcome_for_update", lambda _db, _sid: (sess, outcome))

    first = ar._settle_submitted_orphan_flattens(db, _Adapter(_filled_order()))
    marker_count = len(db.added)
    truth_after_first = dict(sess.risk_snapshot_json["momentum_live_execution"])
    second = ar._settle_submitted_orphan_flattens(db, _Adapter(_filled_order()))

    assert first["outcomes_repaired"] == 1
    assert second == {"orphan_fills_settled": 0, "outcomes_repaired": 0, "settlement_pending": 0}
    assert len(db.added) == marker_count
    assert sess.risk_snapshot_json["momentum_live_execution"] == truth_after_first


def test_stale_pending_snapshot_rechecks_marker_after_row_lock(monkeypatch):
    db = _FakeDb()
    sess, outcome = _objects()
    # Simulate two workers that both discovered the source before either marker
    # committed: the discovery reader intentionally remains stale on call two.
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", lambda _db: _pending())
    monkeypatch.setattr(ar, "_load_session_outcome_for_update", lambda _db, _sid: (sess, outcome))

    first = ar._settle_submitted_orphan_flattens(db, _Adapter(_filled_order()))
    marker_count = len(db.added)
    second = ar._settle_submitted_orphan_flattens(db, _Adapter(_filled_order()))

    assert first["outcomes_repaired"] == 1
    assert second == {"orphan_fills_settled": 0, "outcomes_repaired": 0, "settlement_pending": 0}
    assert len(db.added) == marker_count


def test_uncertified_settlement_source_makes_zero_broker_calls(monkeypatch):
    db = _FakeDb()
    adapter = _Adapter(_filled_order())
    adapter.calls = 0

    def _get_order(_order_id):
        adapter.calls += 1
        return adapter.order, None

    adapter.get_order = _get_order
    monkeypatch.setattr(ar, "_pending_orphan_flatten_events", lambda _db: _pending())
    monkeypatch.setattr(
        ar,
        "_settlement_source_quarantine_reason",
        lambda *_a, **_k: "alpaca_short_execution_not_certified",
    )

    result = ar._settle_submitted_orphan_flattens(db, adapter)

    assert result["settlement_quarantined"] == 1
    assert result["settlement_pending"] == 1
    assert adapter.calls == 0


def test_orphan_reconcile_exit_is_non_strategy_governance_outcome():
    assert derive_outcome_class(
        mode="live",
        terminal_state=STATE_LIVE_CANCELLED,
        entry_occurred=True,
        partial_exit=False,
        realized_pnl_usd=-1_259.37,
        return_bps=-472.97,
        exit_reason="alpaca_orphan_reconcile",
        governance_context={},
        events=[],
    ) == OUTCOME_GOVERNANCE_EXIT


def test_orphan_reconcile_needs_explicit_authority_when_live_runner_is_off(monkeypatch):
    monkeypatch.setattr(
        ar.settings, "chili_momentum_alpaca_orphan_reconcile_enabled", True
    )
    monkeypatch.setattr(ar.settings, "chili_momentum_live_runner_enabled", False)
    monkeypatch.setattr(
        ar.settings,
        "chili_momentum_alpaca_orphan_reconcile_standalone_enabled",
        False,
        raising=False,
    )

    result = ar.run_alpaca_orphan_reconcile(object())

    assert result["skipped"] == "live_runner_disabled_without_standalone_authority"


class _RunDb:
    def __init__(self) -> None:
        self.in_read_transaction = False
        self.rollbacks = 0
        self.commits = 0

    def rollback(self) -> None:
        self.in_read_transaction = False
        self.rollbacks += 1

    def commit(self) -> None:
        self.commits += 1


class _RecheckAdapter:
    def __init__(self, db: _RunDb, symbol: str) -> None:
        self.db = db
        self.symbol = symbol
        self.market_orders = []

    def is_enabled(self):
        return True

    def list_positions(self):
        return ([{
            "product_id": self.symbol,
            "raw_symbol": self.symbol,
            "qty": 100.0,
            "asset_class": "us_equity",
            "avg_entry_price": 2.0,
            "market_value": 200.0,
            "unrealized_pl": -5.0,
        }], None)

    def list_open_orders(self, **_kwargs):
        return [], None

    def place_market_order(self, **kwargs):
        assert self.db.in_read_transaction is False
        self.market_orders.append(kwargs)
        return {
            "ok": True,
            "order_id": "orphan-exit",
            "client_order_id": kwargs["client_order_id"],
        }


def _enable_orphan_reconcile(monkeypatch, adapter):
    monkeypatch.setattr(ar.settings, "chili_momentum_alpaca_orphan_reconcile_enabled", True)
    monkeypatch.setattr(ar.settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(ar.settings, "chili_alpaca_enabled", True)
    monkeypatch.setattr(ar.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(ar.settings, "chili_alpaca_api_key", "paper-key")
    monkeypatch.setattr(
        ar.settings,
        "chili_alpaca_expected_account_id",
        "paper-account",
        raising=False,
    )
    adapter.bind_account_id = lambda account_id: account_id == "paper-account"
    adapter.get_account_snapshot = lambda: {
        "ok": True,
        "account_id": "paper-account",
        "paper": True,
        "status": "ACTIVE",
        "account_blocked": False,
        "trading_blocked": False,
        "trade_suspended_by_user": False,
    }
    monkeypatch.setattr(alpaca_spot_mod, "AlpacaSpotAdapter", lambda: adapter)
    monkeypatch.setattr(ar, "_managed_and_recent_symbols", lambda _db: (set(), set()))
    monkeypatch.setattr(ar, "_persisted_reconcile_quarantine_reason", lambda _db: None)
    monkeypatch.setattr(ar, "_settle_submitted_orphan_flattens", lambda _db, _ad: {})
    monkeypatch.setattr(ar, "_sweep_detached_entry_claims", lambda _db, _ad: {})
    monkeypatch.setattr(ar, "_sweep_active_orphan_claims", lambda _db, _ad: {})


def test_matching_historical_shape_cannot_mint_unclaimed_close_authority(monkeypatch):
    db = _RunDb()
    adapter = _RecheckAdapter(db, "HISTMATCH")
    _enable_orphan_reconcile(monkeypatch, adapter)

    result = ar.run_alpaca_orphan_reconcile(db)

    assert result["reconcile_scope"] == "exact_claims_only"
    assert result["generic_inventory_mutation_enabled"] is False
    assert result["account_verification"]["account_snapshot_read"] is True
    assert result["flattened"] == 0
    assert adapter.market_orders == []


def test_unattributed_manual_position_is_quarantined_without_sell(monkeypatch):
    db = _RunDb()
    adapter = _RecheckAdapter(db, "MANUALPOS")
    _enable_orphan_reconcile(monkeypatch, adapter)

    result = ar.run_alpaca_orphan_reconcile(db)

    assert result["reconcile_scope"] == "exact_claims_only"
    assert result["generic_inventory_mutation_enabled"] is False
    assert result["flattened"] == 0
    assert adapter.market_orders == []


def test_unowned_manual_open_order_is_never_cancelled(monkeypatch):
    db = _RunDb()
    cancelled = []

    class _ManualOrderAdapter:
        def is_enabled(self):
            return True

        def list_positions(self):
            return [], None

        def list_open_orders(self, **_kwargs):
            return [SimpleNamespace(
                product_id="MANUALORD",
                order_id="manual-order-1",
                client_order_id="manual-protective-order",
                created_time="2026-07-13T12:00:00+00:00",
            )], None

        def cancel_order(self, order_id):
            cancelled.append(order_id)
            return {"ok": True}

    adapter = _ManualOrderAdapter()
    _enable_orphan_reconcile(monkeypatch, adapter)
    monkeypatch.setattr(ar, "_grace_minutes", lambda: 1.0)

    result = ar.run_alpaca_orphan_reconcile(db)

    assert result["reconcile_scope"] == "exact_claims_only"
    assert result["generic_inventory_mutation_enabled"] is False
    assert result["cancelled"] == 0
    assert cancelled == []


def test_exact_claim_sweeps_run_only_after_pinned_paper_account_verification(
    monkeypatch,
):
    db = _RunDb()
    adapter = _RecheckAdapter(db, "EXACT")
    _enable_orphan_reconcile(monkeypatch, adapter)
    calls = []
    monkeypatch.setattr(
        ar,
        "_settle_submitted_orphan_flattens",
        lambda _db, _adapter: calls.append("settle") or {"settled": 1},
    )
    monkeypatch.setattr(
        ar,
        "_sweep_detached_entry_claims",
        lambda _db, _adapter: calls.append("detached") or {"detached": 1},
    )
    monkeypatch.setattr(
        ar,
        "_sweep_active_orphan_claims",
        lambda _db, _adapter: calls.append("active") or {"active": 1},
    )

    result = ar.run_alpaca_orphan_reconcile(db)

    assert calls == ["settle", "detached", "active"]
    assert result["settled"] == 1
    assert result["detached"] == 1
    assert result["active"] == 1
    assert result["reconcile_scope"] == "exact_claims_only"
    assert adapter.market_orders == []


def test_wrong_paper_account_generation_blocks_all_exact_claim_sweeps(monkeypatch):
    db = _RunDb()
    adapter = _RecheckAdapter(db, "WRONGACCOUNT")
    _enable_orphan_reconcile(monkeypatch, adapter)
    adapter.get_account_snapshot = lambda: {
        "ok": True,
        "account_id": "different-paper-account",
        "paper": True,
        "status": "ACTIVE",
        "account_blocked": False,
        "trading_blocked": False,
        "trade_suspended_by_user": False,
    }
    monkeypatch.setattr(
        ar,
        "_settle_submitted_orphan_flattens",
        lambda *_args: pytest.fail("wrong account reached settlement"),
    )
    monkeypatch.setattr(
        ar,
        "_sweep_detached_entry_claims",
        lambda *_args: pytest.fail("wrong account reached detached claims"),
    )
    monkeypatch.setattr(
        ar,
        "_sweep_active_orphan_claims",
        lambda *_args: pytest.fail("wrong account reached active claims"),
    )

    result = ar.run_alpaca_orphan_reconcile(db)

    assert result["skipped"] == "alpaca_reconcile_account_generation_mismatch"
    assert result["account_verification"]["account_snapshot_read"] is True
    assert adapter.market_orders == []


def test_persisted_uncertified_row_keeps_reconciler_broker_dark(monkeypatch):
    db = _RunDb()
    monkeypatch.setattr(ar.settings, "chili_momentum_alpaca_orphan_reconcile_enabled", True)
    monkeypatch.setattr(ar.settings, "chili_momentum_live_runner_enabled", True)
    monkeypatch.setattr(ar.settings, "chili_alpaca_enabled", True)
    monkeypatch.setattr(ar.settings, "chili_alpaca_paper", True)
    monkeypatch.setattr(ar.settings, "chili_alpaca_api_key", "paper-key")
    monkeypatch.setattr(
        ar,
        "_persisted_reconcile_quarantine_reason",
        lambda _db: "alpaca_short_execution_not_certified",
    )
    monkeypatch.setattr(
        alpaca_spot_mod,
        "AlpacaSpotAdapter",
        lambda: pytest.fail("quarantined persisted row constructed an adapter"),
    )

    result = ar.run_alpaca_orphan_reconcile(db)

    assert result["skipped"] == "alpaca_execution_quarantined"
    assert result["quarantine_reason"] == "alpaca_short_execution_not_certified"
    assert result["broker_calls"] == 0


def test_unsupported_persisted_row_keeps_reconciler_broker_dark(monkeypatch):
    db = _RunDb()

    class _EmptyAdapter:
        def is_enabled(self):
            return True

        def list_positions(self):
            return [], None

        def list_open_orders(self, **_kwargs):
            return [], None

    adapter = _EmptyAdapter()
    _enable_orphan_reconcile(monkeypatch, adapter)
    monkeypatch.setattr(
        ar,
        "_persisted_reconcile_quarantine_reason",
        lambda _db: {"alpaca_short_execution_not_certified": 1},
    )

    result = ar.run_alpaca_orphan_reconcile(db)

    assert result["skipped"] == "alpaca_execution_quarantined"
    assert result["broker_calls"] == 0
    assert result["persisted_execution_quarantines"] == {
        "alpaca_short_execution_not_certified": 1
    }


@pytest.mark.parametrize(
    ("symbol", "close_side", "paper", "reason"),
    [
        ("ACTU", "sell", False, "alpaca_live_posture_not_certified"),
        ("BTC-USD", "sell", True, "alpaca_crypto_execution_not_certified"),
        ("BTC/USD", "sell", True, "alpaca_crypto_execution_not_certified"),
        ("ACTU", "buy", True, "alpaca_short_execution_not_certified"),
    ],
)
def test_uncertified_close_boundary_makes_zero_adapter_calls(
    monkeypatch,
    symbol,
    close_side,
    paper,
    reason,
):
    class _DarkAdapter:
        def __getattr__(self, name):
            pytest.fail(f"quarantined close touched adapter method {name}")

    monkeypatch.setattr(ar.settings, "chili_alpaca_paper", paper)

    result = ar._place_alpaca_equity_close(
        _DarkAdapter(),
        symbol=symbol,
        close_side=close_side,
        quantity=100.0,
        client_order_id="quarantine-cid",
    )

    assert result["pre_place_blocked"] is True
    assert result["execution_quarantined"] is True
    assert result["transport_attempted"] is False
    assert result["error"] == reason
