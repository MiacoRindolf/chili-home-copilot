from __future__ import annotations

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
from types import SimpleNamespace
import uuid

import pytest

from app.services.trading.momentum_neural import (
    captured_paper_fill_watch as fill_watch,
)
from app.services.trading.momentum_neural import captured_paper_outbox as outbox
from app.services.trading.momentum_neural import (
    captured_paper_transport_coordinator as transport,
)
from app.services.trading.momentum_neural.captured_paper_admission import (
    CommittedCapturedPaperAdmission,
)
from app.services.trading.momentum_neural import (
    captured_paper_entry_intent as intent_contract,
)
from app.services.trading.venue import alpaca_spot
from app.services.trading.venue.alpaca_spot import (
    quantize_alpaca_equity_limit_price,
)


UTC = timezone.utc
NOW = datetime(2036, 7, 15, 16, 30, tzinfo=UTC)
ACCOUNT_ID = "d7cc580c-2b8f-432f-b771-1cecfb3fe87a"
RESERVATION_ID = "da45acc8-6b95-4d20-8579-8da28e203511"
ARM_TOKEN = "d2b8f7d8-6ad5-4cd0-a94e-8a9ca146d3ab"
BINDER_ID = "122158cc-18ae-4cef-bc52-f1c5b689b352"
WORKER_ID = "2ed29ed9-79dd-4f75-ae44-2e5a33b8e77e"
ORDER_ID = "alpaca-order-ACTU-1"
CONNECTION_GENERATION = "alpaca-paper-rest:" + "d" * 64


def _sha_json(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _admission() -> CommittedCapturedPaperAdmission:
    route = intent_contract.CapturedPaperRouteToken(
        session_id=41,
        symbol="ACTU",
        execution_family="alpaca_spot",
        account_scope="alpaca:paper",
        expected_account_id=ACCOUNT_ID,
        code_build_sha256="a" * 64,
        config_sha256="b" * 64,
        capture_receipt_sha256="c" * 64,
        runtime_generation="f6ef5ba0-5b91-49bf-a2f5-e71e8e270eb3",
        first_dip_policy_mode="candidate",
    )
    arm = intent_contract.CapturedPaperConfirmedArmGeneration(
        session_id=route.session_id,
        arm_token=ARM_TOKEN,
        expires_at=NOW + timedelta(minutes=30),
        symbol_claim_token=f"arm-{ARM_TOKEN}",
        account_scope=route.account_scope,
        expected_account_id=route.expected_account_id,
        confirmed_at=NOW - timedelta(minutes=30),
    )
    opportunity = intent_contract.CapturedPaperOpportunityKey(
        account_scope=route.account_scope,
        symbol=route.symbol,
        trading_date=date(2036, 7, 15),
        setup_family="first_dip_reclaim",
    )
    intent = intent_contract.CapturedPaperEntryIntent(
        route_token=route,
        confirmed_arm_generation=arm,
        symbol_claim_token=arm.symbol_claim_token,
        binder_id=BINDER_ID,
        opportunity_key=opportunity,
        intent_generation="39f55a65-e6f2-4ccc-bd02-f50dc9c27c69",
        decision_id="chili_ml_ACTU_41_1",
        client_order_id="chili_ml_ACTU_41_1",
        setup_family="first_dip_reclaim",
        decision_at=NOW,
        structural_stop_price="2.5",
        entry_limit_ceiling_price="3",
        account_receipt_sha256="d" * 64,
        bbo_receipt_sha256="e" * 64,
        setup_evidence_sha256="f" * 64,
        policy_sha256="1" * 64,
        feature_flags_sha256="2" * 64,
    )
    request = intent_contract.CapturedPaperPostCommitRequest(
        intent=intent,
        completion_generation="73dbcf92-94ea-436e-978c-b0e31ce7252d",
    )
    order = {
        "asset_class": "us_equity",
        "client_order_id": intent.client_order_id,
        "extended_hours": False,
        "limit_price": quantize_alpaca_equity_limit_price("3", "buy"),
        "position_intent": "buy_to_open",
        "qty": "4578",
        "side": "buy",
        "symbol": route.symbol,
        "time_in_force": "day",
        "type": "limit",
    }
    return CommittedCapturedPaperAdmission(
        post_commit_request=request,
        reservation_id=RESERVATION_ID,
        decision_packet_sha256="3" * 64,
        reservation_request_sha256="4" * 64,
        adaptive_input_evidence_sha256="5" * 64,
        account_identity_sha256="6" * 64,
        quantity_shares=4578,
        structural_risk_usd="2289",
        gross_notional_usd="13734",
        buying_power_impact_usd="13734",
        order_request=order,
        order_request_sha256=_sha_json(order),
        admission_record_sha256="7" * 64,
        committed_at=NOW,
    )


def _watch_instruction() -> fill_watch.CapturedPaperCompletedFillWatchInstruction:
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )
    durable = outbox.CapturedPaperDurableTransportBundle(
        request=instruction.request,
        authority=instruction.authority,
        order_request=dict(instruction.order_request),
        order_request_sha256=instruction.order_request_sha256,
        admission_record={},
        admission_record_sha256="8" * 64,
        committed_admission={},
        committed_admission_sha256="9" * 64,
        transport_instruction=instruction._content_payload(),
        transport_instruction_sha256=instruction.instruction_sha256,
        reconciliation_retry_delay_seconds=15,
        reconciliation_health_escalation_delay_seconds=60,
    )
    lease = outbox.CapturedPaperCompletedFillWatchLease(
        completion_sha256=instruction.request.completion_sha256,
        lease_token=str(uuid.uuid4()),
        lease_owner_id=WORKER_ID,
        lease_expires_at=NOW + timedelta(seconds=30),
        attempt_count=1,
        recovered=False,
    )
    bundle = outbox.CapturedPaperCompletedFillWatchBundle(
        durable_transport=durable,
        lease=lease,
        completion_proof_sha256="a" * 64,
        completion_event_type="direct_completion_accepted",
        broker_order_id=ORDER_ID,
        broker_connection_generation=CONNECTION_GENERATION,
        broker_order_evidence_sha256="b" * 64,
        broker_observed_at=NOW - timedelta(seconds=2),
        broker_available_at=NOW - timedelta(seconds=1),
    )
    return fill_watch.CapturedPaperCompletedFillWatchInstruction(
        transport_instruction=instruction,
        watch_bundle=bundle,
    )


def _exact_order(
    instruction: fill_watch.CapturedPaperCompletedFillWatchInstruction,
    *,
    status: str,
    filled: int,
) -> transport.CapturedPaperExactBrokerOrderObservation:
    typed = instruction.transport_instruction
    return transport.CapturedPaperExactBrokerOrderObservation(
        account_scope=typed.account_scope,
        expected_account_id=typed.expected_account_id,
        verified_adapter_account_id=typed.expected_account_id,
        account_binding_source=transport.EXACT_PAPER_ACCOUNT_BINDING_SOURCE,
        broker_account_id=typed.expected_account_id,
        client_order_id=typed.client_order_id,
        broker_order_id=instruction.broker_order_id,
        symbol=typed.symbol,
        side="buy",
        order_type="limit",
        asset_class="us_equity",
        quantity_shares=typed.quantity_shares,
        broker_quantity_echo=str(typed.quantity_shares),
        broker_filled_quantity_echo=str(filled),
        cumulative_filled_quantity_shares=filled,
        limit_price=typed.limit_price,
        broker_limit_price_echo=typed.limit_price,
        time_in_force=typed.time_in_force,
        extended_hours=typed.extended_hours,
        position_intent_echo="buy_to_open",
        broker_order_status=status,
        broker_order_status_echo=status,
        broker_connection_generation=CONNECTION_GENERATION,
        broker_order_evidence_sha256=_sha_json(
            {"order_id": instruction.broker_order_id, "status": status}
        ),
        observed_at=NOW,
        available_at=NOW,
    )


def _fill_read(instruction, *, count):
    typed = instruction.transport_instruction
    return transport.CapturedPaperFillReadAuthority(
        account_scope=typed.account_scope,
        expected_account_id=typed.expected_account_id,
        reservation_id=typed.authority.reservation_id,
        client_order_id=typed.client_order_id,
        broker_order_id=instruction.broker_order_id,
        query_receipt_sha256="c" * 64,
        observation_sha256="d" * 64,
        exact_activity_count=count,
        positive_fill_observed=bool(count),
        pagination_complete=True,
        available_at=NOW,
    )


def _append_receipt(read):
    positive = read.positive_fill_observed
    return transport.CapturedPaperFillAppendReceipt(
        observation_sha256=read.observation_sha256,
        durable_receipt_sha256="e" * 64,
        committed_at=NOW,
        positive_fill_handoff_committed=positive,
        fill_handoff_proof_sha256="f" * 64 if positive else None,
        outbox_fill_handoff_receipt_sha256="0" * 64 if positive else None,
    )


class _Store:
    def __init__(self, instruction):
        self.instruction = instruction
        self.reschedules = []
        self.terminals = []

    def lease_next(self, *, lease_owner_id, lease_seconds):
        assert lease_owner_id == WORKER_ID
        assert lease_seconds == 30
        return self.instruction.lease

    def load_instruction(self, lease):
        assert lease == self.instruction.lease
        return self.instruction

    def reschedule(self, instruction, **kwargs):
        assert instruction is self.instruction
        self.reschedules.append(kwargs)

    def complete_terminal_zero_fill(self, instruction, **kwargs):
        assert instruction is self.instruction
        self.terminals.append(kwargs)
        return "1" * 64


class _Reader:
    def __init__(self, observation):
        self.observation = observation
        self.calls = 0

    def lookup_exact_order(self, instruction):
        self.calls += 1
        return self.observation


class _FillCapture:
    def __init__(self, read):
        self.read = read
        self.read_calls = 0
        self.append_calls = []

    def read_exact_order_fills(self, instruction, observation):
        self.read_calls += 1
        return self.read

    def append_fill_read(
        self, read, *, instruction, fill_handoff_required
    ):
        assert read is self.read
        assert fill_handoff_required == read.positive_fill_observed
        self.append_calls.append(fill_handoff_required)
        return _append_receipt(read)


def _coordinator(instruction, observation, read):
    store = _Store(instruction)
    reader = _Reader(observation)
    fill = _FillCapture(read)
    coordinator = fill_watch.CapturedPaperCompletedFillWatchCoordinator(
        store=store,
        reader=reader,
        fill_capture=fill,
        retry_delay_seconds=15,
    )
    return coordinator, store, reader, fill


def test_terminal_projection_cannot_release_when_fresh_fill_read_is_positive():
    instruction = _watch_instruction()
    observation = transport.CapturedPaperTerminalZeroFillObservation(
        order=_exact_order(instruction, status="canceled", filled=0)
    )
    read = _fill_read(instruction, count=1)
    coordinator, store, _reader, fill = _coordinator(
        instruction, observation, read
    )

    result = coordinator.run_one_cycle(
        lease_owner_id=WORKER_ID, lease_seconds=30
    )

    assert result.status == "fill_handoff_committed"
    assert fill.append_calls == [True]
    assert store.terminals == []
    assert store.reschedules == []


def test_fill_bearing_projection_reschedules_when_exact_activity_is_not_yet_visible():
    instruction = _watch_instruction()
    observation = transport.CapturedPaperFillReconciliationRequiredObservation(
        order=_exact_order(instruction, status="partially_filled", filled=1)
    )
    read = _fill_read(instruction, count=0)
    coordinator, store, _reader, fill = _coordinator(
        instruction, observation, read
    )

    result = coordinator.run_one_cycle(
        lease_owner_id=WORKER_ID, lease_seconds=30
    )

    assert result.status == "rescheduled_fill_activity_pending"
    assert fill.append_calls == [False]
    assert store.reschedules[0]["reason"] == "fill_activity_not_yet_available"
    assert store.terminals == []


def test_unavailable_order_read_reschedules_without_a_fill_query():
    instruction = _watch_instruction()
    unavailable = fill_watch.CapturedPaperFillWatchUnavailableObservation(
        completion_sha256=instruction.completion_sha256,
        broker_order_id=instruction.broker_order_id,
        reason="order_unreadable",
        evidence_sha256="2" * 64,
        available_at=NOW,
    )
    coordinator, store, _reader, fill = _coordinator(
        instruction, unavailable, _fill_read(instruction, count=0)
    )

    result = coordinator.run_one_cycle(
        lease_owner_id=WORKER_ID, lease_seconds=30
    )

    assert result.status == "rescheduled_unavailable"
    assert fill.read_calls == 0
    assert fill.append_calls == []
    assert store.reschedules[0]["reason"] == "broker_read_unavailable"


def test_unavailable_order_read_cannot_regress_behind_acceptance_clock():
    instruction = _watch_instruction()
    unavailable = fill_watch.CapturedPaperFillWatchUnavailableObservation(
        completion_sha256=instruction.completion_sha256,
        broker_order_id=instruction.broker_order_id,
        reason="order_unreadable",
        evidence_sha256="2" * 64,
        available_at=instruction.watch_bundle.broker_available_at
        - timedelta(microseconds=1),
    )
    coordinator, store, _reader, fill = _coordinator(
        instruction, unavailable, _fill_read(instruction, count=0)
    )

    with pytest.raises(
        fill_watch.CapturedPaperCompletedFillWatchError,
        match="unavailable_clock_frontier_invalid",
    ):
        coordinator.run_one_cycle(
            lease_owner_id=WORKER_ID, lease_seconds=30
        )

    assert fill.read_calls == 0
    assert store.reschedules == []


def test_terminal_zero_requires_the_empty_fill_append_before_release():
    instruction = _watch_instruction()
    observation = transport.CapturedPaperTerminalZeroFillObservation(
        order=_exact_order(instruction, status="expired", filled=0)
    )
    read = _fill_read(instruction, count=0)
    coordinator, store, _reader, fill = _coordinator(
        instruction, observation, read
    )

    result = coordinator.run_one_cycle(
        lease_owner_id=WORKER_ID, lease_seconds=30
    )

    assert result.status == "terminal_zero_fill"
    assert fill.append_calls == [False]
    assert len(store.terminals) == 1
    assert store.reschedules == []


def test_fill_read_must_follow_the_exact_order_observation_before_append():
    instruction = _watch_instruction()
    observation = transport.CapturedPaperPositiveOrderObservation(
        order=_exact_order(instruction, status="accepted", filled=0)
    )
    read = replace(
        _fill_read(instruction, count=0),
        available_at=observation.available_at - timedelta(microseconds=1),
    )
    coordinator, store, _reader, fill = _coordinator(
        instruction, observation, read
    )

    with pytest.raises(
        fill_watch.CapturedPaperCompletedFillWatchError,
        match="fill_read_binding_mismatch",
    ):
        coordinator.run_one_cycle(
            lease_owner_id=WORKER_ID, lease_seconds=30
        )

    assert fill.append_calls == []
    assert store.reschedules == []
    assert store.terminals == []


def test_append_commit_must_follow_the_bound_fill_observation():
    instruction = _watch_instruction()
    observation = transport.CapturedPaperPositiveOrderObservation(
        order=_exact_order(instruction, status="accepted", filled=0)
    )
    read = _fill_read(instruction, count=0)
    coordinator, store, _reader, fill = _coordinator(
        instruction, observation, read
    )
    original_append = fill.append_fill_read

    def append_with_regressed_commit(*args, **kwargs):
        return replace(
            original_append(*args, **kwargs),
            committed_at=read.available_at - timedelta(microseconds=1),
        )

    fill.append_fill_read = append_with_regressed_commit

    with pytest.raises(
        fill_watch.CapturedPaperCompletedFillWatchError,
        match="append_receipt_mismatch",
    ):
        coordinator.run_one_cycle(
            lease_owner_id=WORKER_ID, lease_seconds=30
        )

    assert store.reschedules == []
    assert store.terminals == []


class _FakeSdkClient:
    def __init__(self, order):
        self.order = order
        self.order_ids = []

    def get_order_by_id(self, order_id):
        self.order_ids.append(order_id)
        return self.order


def _sdk_order(instruction, *, status="accepted", filled="0"):
    typed = instruction.transport_instruction
    return SimpleNamespace(
        id=instruction.broker_order_id,
        client_order_id=typed.client_order_id,
        symbol=typed.symbol,
        side="buy",
        status=status,
        order_type="limit",
        type="limit",
        qty=str(typed.quantity_shares),
        limit_price=typed.limit_price,
        time_in_force=typed.time_in_force,
        extended_hours=typed.extended_hours,
        position_intent="buy_to_open",
        account_id=typed.expected_account_id,
        asset_class="us_equity",
        filled_qty=filled,
        filled_avg_price=None,
        filled_at=None,
        submitted_at=NOW,
        created_at=NOW,
        notional=None,
        stop_price=None,
        replaced_by=None,
        replaces=None,
    )


def _bound_adapter(monkeypatch, instruction):
    api_key = "paper-key"
    api_secret = "paper-secret"
    fingerprint = hashlib.sha256(
        f"paper\0{api_key}\0{api_secret}".encode("utf-8")
    ).hexdigest()
    client = _FakeSdkClient(_sdk_order(instruction))
    monkeypatch.setattr(
        alpaca_spot.settings, "chili_alpaca_paper", True, raising=False
    )
    monkeypatch.setattr(
        alpaca_spot.settings, "chili_alpaca_enabled", True, raising=False
    )
    monkeypatch.setattr(
        alpaca_spot.settings, "chili_alpaca_api_key", api_key, raising=False
    )
    monkeypatch.setattr(
        alpaca_spot.settings,
        "chili_alpaca_api_secret",
        api_secret,
        raising=False,
    )
    monkeypatch.setattr(
        alpaca_spot.settings,
        "chili_alpaca_expected_account_id",
        ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(alpaca_spot, "_now", lambda: NOW)
    alpaca_spot.reset_clients_for_tests()
    alpaca_spot._clients.update(
        {
            "trading:paper": client,
            "trading:fingerprint": fingerprint,
            "trading:observed_account_id": ACCOUNT_ID,
        }
    )
    adapter = alpaca_spot.AlpacaSpotAdapter()
    assert adapter.bind_account_id(ACCOUNT_ID) is True
    adapter.is_enabled = lambda: True
    receipt = adapter.get_paper_connection_generation_receipt()
    return adapter, client, receipt["adapter_connection_generation"]


def test_exact_reader_uses_frozen_oid_and_fresh_authenticated_generation(
    monkeypatch,
):
    instruction = _watch_instruction()
    adapter, client, generation = _bound_adapter(monkeypatch, instruction)
    instruction = replace(
        instruction,
        watch_bundle=replace(
            instruction.watch_bundle,
            broker_connection_generation=generation,
        ),
    )
    reader = fill_watch.ExactAlpacaPaperCompletedFillWatchReader(
        adapter=adapter,
        expected_account_id=ACCOUNT_ID,
        broker_connection_generation=generation,
        observation_clock=lambda: NOW,
    )

    result = reader.lookup_exact_order(instruction)

    assert type(result) is transport.CapturedPaperPositiveOrderObservation
    assert result.broker_order_id == ORDER_ID
    assert client.order_ids == [ORDER_ID]
    assert not any(
        token in name
        for name in dir(reader)
        if not name.startswith("_")
        for token in ("post", "submit", "reconcile")
    )


def test_exact_reader_refuses_a_changed_adapter_generation_before_oid_read(
    monkeypatch,
):
    instruction = _watch_instruction()
    adapter, client, generation = _bound_adapter(monkeypatch, instruction)
    stale_generation = "alpaca-paper-rest:" + "e" * 64
    instruction = replace(
        instruction,
        watch_bundle=replace(
            instruction.watch_bundle,
            broker_connection_generation=stale_generation,
        ),
    )
    reader = fill_watch.ExactAlpacaPaperCompletedFillWatchReader(
        adapter=adapter,
        expected_account_id=ACCOUNT_ID,
        broker_connection_generation=stale_generation,
        observation_clock=lambda: NOW,
    )

    result = reader.lookup_exact_order(instruction)

    assert type(result) is fill_watch.CapturedPaperFillWatchUnavailableObservation
    assert result.reason == "adapter_generation_unavailable"
    assert client.order_ids == []
    assert generation != stale_generation


def test_exact_reader_refuses_future_generation_receipt_before_oid_read(
    monkeypatch,
):
    instruction = _watch_instruction()
    adapter, client, generation = _bound_adapter(monkeypatch, instruction)
    instruction = replace(
        instruction,
        watch_bundle=replace(
            instruction.watch_bundle,
            broker_connection_generation=generation,
        ),
    )
    monkeypatch.setattr(
        alpaca_spot,
        "_now",
        lambda: NOW + timedelta(minutes=1),
    )
    reader = fill_watch.ExactAlpacaPaperCompletedFillWatchReader(
        adapter=adapter,
        expected_account_id=ACCOUNT_ID,
        broker_connection_generation=generation,
        observation_clock=lambda: NOW,
    )

    result = reader.lookup_exact_order(instruction)

    assert type(result) is fill_watch.CapturedPaperFillWatchUnavailableObservation
    assert result.reason == "adapter_generation_unavailable"
    assert client.order_ids == []


class _WorkerCoordinator:
    def __init__(self, *, fail=False):
        self.fail = fail
        self.calls = 0

    def run_one_cycle(self, *, lease_owner_id, lease_seconds):
        assert lease_owner_id == WORKER_ID
        assert lease_seconds == 30
        self.calls += 1
        if self.fail:
            raise RuntimeError("boom")
        return None


def test_worker_is_one_shot_joinable_and_reports_idle_health():
    coordinator = _WorkerCoordinator()
    worker = fill_watch.CapturedPaperCompletedFillWatchWorker(
        coordinator=coordinator,
        worker_id=WORKER_ID,
        lease_seconds=30,
        idle_poll_seconds=0.01,
        observation_clock=lambda: NOW,
    )

    worker.start()
    worker.close(join_timeout_seconds=2)
    health = worker.health()

    assert health.ever_started is True
    assert health.running is False
    assert health.stop_requested is True
    assert health.fatal is False
    assert health.cycles_completed >= 1
    assert health.idle_cycles == health.cycles_completed
    assert health.to_mapping()["worker_id"] == WORKER_ID


def test_worker_unknown_fault_is_terminal_and_visible_in_health():
    worker = fill_watch.CapturedPaperCompletedFillWatchWorker(
        coordinator=_WorkerCoordinator(fail=True),
        worker_id=WORKER_ID,
        lease_seconds=30,
        idle_poll_seconds=0.01,
        observation_clock=lambda: NOW,
    )

    worker.start()
    worker.close(join_timeout_seconds=2)
    health = worker.health()

    assert health.running is False
    assert health.fatal is True
    assert health.fatal_error_type == "RuntimeError"
    assert health.stop_requested is True
