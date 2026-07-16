from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.services.trading.momentum_neural import replay_v3 as rv3
from app.services.trading.momentum_neural.replay_mock_broker import (
    MockBrokerAdapter,
    RecordedBrokerTransition,
    RecordedOrderIntent,
    RecordedQuote,
    SEALED_REPLAY_CANCEL_RECEIPT_ARCHITECTURAL_BLOCKER,
    SEALED_REPLAY_SYNC_ACK_ARCHITECTURAL_BLOCKER,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 13, 13, 0, tzinfo=UTC)


def _intent() -> RecordedOrderIntent:
    return RecordedOrderIntent(
        order_intent_sha256="1" * 64,
        client_order_id="captured-cid-1",
        product_id="VEEE",
        side="buy",
        order_type="limit",
        base_size=100.0,
        time_in_force="day",
        extended_hours=False,
        limit_price=10.01,
    )


def _transitions() -> tuple[RecordedBrokerTransition, ...]:
    common = {
        "order_intent_sha256": "1" * 64,
        "client_order_id": "captured-cid-1",
        "broker_order_id": "broker-order-1",
        "order_quantity": 100.0,
    }
    return (
        RecordedBrokerTransition(
            event_sha256="2" * 64,
            sequence=10,
            available_at=BASE,
            transition="submitted",
            cumulative_filled_quantity=0.0,
            last_fill_quantity=0.0,
            last_fill_price=None,
            **common,
        ),
        RecordedBrokerTransition(
            event_sha256="3" * 64,
            sequence=11,
            available_at=BASE + timedelta(seconds=1),
            transition="partially_filled",
            cumulative_filled_quantity=40.0,
            last_fill_quantity=40.0,
            last_fill_price=10.01,
            **common,
        ),
        RecordedBrokerTransition(
            event_sha256="4" * 64,
            sequence=12,
            available_at=BASE + timedelta(seconds=2),
            transition="filled",
            cumulative_filled_quantity=100.0,
            last_fill_quantity=60.0,
            last_fill_price=10.02,
            **common,
        ),
    )


def _configured_mock() -> MockBrokerAdapter:
    mock = MockBrokerAdapter()
    mock.configure_recorded_lifecycle(
        intents=(_intent(),), transitions=_transitions()
    )
    mock.set_clock(BASE)
    mock.set_quote("VEEE", RecordedQuote(bid=9.99, ask=10.01, last=10.0))
    return mock


def test_exact_place_request_consumes_its_submitted_response_not_quote_fill() -> None:
    mock = _configured_mock()
    mock.release_recorded_transition(_transitions()[0])
    assert mock.recorded_place_response_available("captured-cid-1") is True

    result = mock.place_limit_order_gtc(
        product_id="VEEE",
        side="buy",
        base_size="100",
        limit_price="10.01",
        client_order_id="captured-cid-1",
        time_in_force="gfd",
    )

    assert result["ok"] is True
    assert result["order_id"] == "broker-order-1"
    assert mock.recorded_place_response_available("captured-cid-1") is False
    assert mock.get_fills(limit=10)[0] == []


def test_place_does_not_read_the_future_transition_inventory() -> None:
    mock = _configured_mock()
    mock.release_recorded_transition(_transitions()[0])

    class _ForbiddenFutureInventory(dict):
        def __getitem__(self, _key):
            raise AssertionError("PLACE read the future transition inventory")

        def get(self, _key, _default=None):
            raise AssertionError("PLACE read the future transition inventory")

    mock._recorded_all_transitions = _ForbiddenFutureInventory(  # type: ignore[attr-defined]
        mock._recorded_all_transitions  # type: ignore[attr-defined]
    )

    result = mock.place_limit_order_gtc(
        product_id="VEEE",
        side="buy",
        base_size="100",
        limit_price="10.01",
        client_order_id="captured-cid-1",
        time_in_force="gfd",
    )

    assert result["ok"] is True
    assert result["order_id"] == "broker-order-1"


def test_place_before_submitted_release_is_an_explicit_architectural_blocker() -> None:
    mock = _configured_mock()

    result = mock.place_limit_order_gtc(
        product_id="VEEE",
        side="buy",
        base_size="100",
        limit_price="10.01",
        client_order_id="captured-cid-1",
        time_in_force="gfd",
    )

    assert result == {
        "ok": False,
        "venue": "replay_mock",
        "error": SEALED_REPLAY_SYNC_ACK_ARCHITECTURAL_BLOCKER,
        "client_order_id": "captured-cid-1",
        "architectural_blocker": True,
        "blocker_detail": (
            "captured SUBMITTED response was not causally available "
            "when synchronous PLACE required its broker order id"
        ),
    }
    assert "order_id" not in result
    assert mock.recorded_place_response_available("captured-cid-1") is False
    assert mock.recorded_bound_client_ids == ()
    assert mock.recorded_applied_event_sha256s == ()
    assert mock.get_order("broker-order-1")[0] is None
    assert mock.list_open_orders()[0] == []
    assert mock.get_fills(limit=10)[0] == []
    assert mock.recorded_request_violations == (
        "captured-cid-1:" + SEALED_REPLAY_SYNC_ACK_ARCHITECTURAL_BLOCKER,
    )


def test_post_submit_transition_cannot_precede_the_exact_place_request() -> None:
    mock = _configured_mock()

    with pytest.raises(ValueError, match="before its PLACE request"):
        mock.release_recorded_transition(_transitions()[1])


def test_missing_synchronous_post_response_identity_is_nonreplayable() -> None:
    submitted, *rest = _transitions()
    without_order_id = RecordedBrokerTransition(
        **{**submitted.__dict__, "broker_order_id": None}
    )
    mock = MockBrokerAdapter()

    with pytest.raises(ValueError, match="lacks broker order id"):
        mock.configure_recorded_lifecycle(
            intents=(_intent(),),
            transitions=(without_order_id, *rest),
        )


def test_recorded_broker_applies_only_released_exact_fill_deltas() -> None:
    mock = _configured_mock()
    submitted, partial, filled = _transitions()
    mock.release_recorded_transition(submitted)

    placed = mock.place_limit_order_gtc(
        product_id="VEEE",
        side="buy",
        base_size="100",
        limit_price="10.01",
        client_order_id="captured-cid-1",
        time_in_force="gfd",
    )
    assert placed["ok"] is True
    assert placed["order_id"] == "broker-order-1"
    assert mock.get_fills(limit=10)[0] == []

    # A later quote is a separate market fact; it cannot rewrite the captured
    # broker execution economics.
    mock.set_quote("VEEE", RecordedQuote(bid=19.99, ask=20.01, last=20.0))
    mock.release_recorded_transition(partial)
    order, _ = mock.get_order("broker-order-1")
    assert order is not None
    assert order.status == "partially_filled"
    assert order.filled_size == pytest.approx(40.0)
    assert order.average_filled_price == pytest.approx(10.01)

    mock.release_recorded_transition(filled)
    order, _ = mock.get_order("broker-order-1")
    fills, _ = mock.get_fills(limit=10)
    assert order is not None
    assert order.status == "filled"
    assert order.filled_size == pytest.approx(100.0)
    assert order.average_filled_price == pytest.approx(10.016)
    assert [(fill.size, fill.price) for fill in fills] == [
        (40.0, 10.01),
        (60.0, 10.02),
    ]
    assert [fill.fill_id for fill in fills] == [
        "broker-order-1:" + "3" * 64,
        "broker-order-1:" + "4" * 64,
    ]


def test_recorded_lifecycle_rejects_causal_regression_and_post_terminal_fact() -> None:
    submitted, partial, filled = _transitions()
    regressed = RecordedBrokerTransition(
        **{
            **partial.__dict__,
            "event_sha256": "8" * 64,
            "available_at": submitted.available_at,
            "sequence": submitted.sequence,
        }
    )
    with pytest.raises(ValueError, match="causal order regressed"):
        MockBrokerAdapter().configure_recorded_lifecycle(
            intents=(_intent(),), transitions=(submitted, regressed, filled)
        )

    after_terminal = RecordedBrokerTransition(
        **{
            **filled.__dict__,
            "event_sha256": "9" * 64,
            "sequence": filled.sequence + 1,
            "available_at": filled.available_at + timedelta(seconds=1),
            "transition": "accepted",
            "last_fill_quantity": 0.0,
            "last_fill_price": None,
        }
    )
    with pytest.raises(ValueError, match="continues after terminal"):
        MockBrokerAdapter().configure_recorded_lifecycle(
            intents=(_intent(),),
            transitions=(submitted, partial, filled, after_terminal),
        )


def test_pending_cancel_chain_fails_closed_without_exact_request_response_receipt() -> None:
    common = {
        "order_intent_sha256": "1" * 64,
        "client_order_id": "captured-cid-1",
        "broker_order_id": "broker-order-1",
        "order_quantity": 100.0,
        "cumulative_filled_quantity": 0.0,
        "last_fill_quantity": 0.0,
        "last_fill_price": None,
    }
    transitions = (
        RecordedBrokerTransition(
            event_sha256="a" * 64,
            sequence=20,
            available_at=BASE,
            transition="submitted",
            **common,
        ),
        RecordedBrokerTransition(
            event_sha256="b" * 64,
            sequence=21,
            available_at=BASE + timedelta(seconds=1),
            transition="pending_cancel",
            **common,
        ),
        RecordedBrokerTransition(
            event_sha256="c" * 64,
            sequence=22,
            available_at=BASE + timedelta(seconds=2),
            transition="canceled",
            reject_or_cancel_reason="client_request",
            **common,
        ),
    )
    mock = MockBrokerAdapter()
    mock.configure_recorded_lifecycle(intents=(_intent(),), transitions=transitions)
    mock.set_quote("VEEE", RecordedQuote(bid=9.99, ask=10.01, last=10.0))
    mock.release_recorded_transition(transitions[0])
    placed = mock.place_limit_order_gtc(
        product_id="VEEE",
        side="buy",
        base_size="100",
        limit_price="10.01",
        client_order_id="captured-cid-1",
        time_in_force="gfd",
    )
    assert placed["ok"] is True
    assert mock.recorded_cancel_request_complete is False

    applied_before = mock.recorded_applied_event_sha256s
    bound_before = mock.recorded_bound_client_ids
    order_before, _ = mock.get_order("broker-order-1")
    assert order_before is not None

    result = mock.cancel_order("broker-order-1")

    assert result == {
        "ok": False,
        "venue": "replay_mock",
        "order_id": "broker-order-1",
        "client_order_id": "captured-cid-1",
        "status": "open",
        "error": SEALED_REPLAY_CANCEL_RECEIPT_ARCHITECTURAL_BLOCKER,
        "architectural_blocker": True,
        "blocker_detail": (
            "sealed capture has broker lifecycle transitions but no exact "
            "cancel request/response receipt"
        ),
    }
    order_after, _ = mock.get_order("broker-order-1")
    assert order_after is not None
    assert order_after.status == order_before.status == "open"
    assert mock.recorded_applied_event_sha256s == applied_before
    assert mock.recorded_bound_client_ids == bound_before
    assert mock.recorded_cancel_request_complete is False


def test_direct_canceled_transition_still_requires_exact_request_provenance() -> None:
    common = {
        "order_intent_sha256": "1" * 64,
        "client_order_id": "captured-cid-1",
        "broker_order_id": "broker-order-1",
        "order_quantity": 100.0,
        "cumulative_filled_quantity": 0.0,
        "last_fill_quantity": 0.0,
        "last_fill_price": None,
    }
    transitions = (
        RecordedBrokerTransition(
            event_sha256="d" * 64,
            sequence=30,
            available_at=BASE,
            transition="submitted",
            **common,
        ),
        RecordedBrokerTransition(
            event_sha256="e" * 64,
            sequence=31,
            available_at=BASE + timedelta(seconds=1),
            transition="canceled",
            reject_or_cancel_reason="client_request",
            **common,
        ),
    )
    mock = MockBrokerAdapter()
    mock.configure_recorded_lifecycle(intents=(_intent(),), transitions=transitions)

    assert mock.recorded_cancel_request_complete is False


def test_cancel_does_not_read_the_future_transition_inventory() -> None:
    common = {
        "order_intent_sha256": "1" * 64,
        "client_order_id": "captured-cid-1",
        "broker_order_id": "broker-order-1",
        "order_quantity": 100.0,
        "cumulative_filled_quantity": 0.0,
        "last_fill_quantity": 0.0,
        "last_fill_price": None,
    }
    transitions = (
        RecordedBrokerTransition(
            event_sha256="a" * 64,
            sequence=20,
            available_at=BASE,
            transition="submitted",
            **common,
        ),
        RecordedBrokerTransition(
            event_sha256="b" * 64,
            sequence=21,
            available_at=BASE + timedelta(seconds=1),
            transition="pending_cancel",
            **common,
        ),
    )
    mock = MockBrokerAdapter()
    mock.configure_recorded_lifecycle(intents=(_intent(),), transitions=transitions)
    mock.set_quote("VEEE", RecordedQuote(bid=9.99, ask=10.01, last=10.0))
    mock.release_recorded_transition(transitions[0])
    placed = mock.place_limit_order_gtc(
        product_id="VEEE",
        side="buy",
        base_size="100",
        limit_price="10.01",
        client_order_id="captured-cid-1",
        time_in_force="gfd",
    )
    assert placed["ok"] is True

    class _ForbiddenFutureInventory(dict):
        def __getitem__(self, _key):
            raise AssertionError("CANCEL read the future transition inventory")

        def get(self, _key, _default=None):
            raise AssertionError("CANCEL read the future transition inventory")

        def items(self):
            raise AssertionError("CANCEL read the future transition inventory")

        def values(self):
            raise AssertionError("CANCEL read the future transition inventory")

    mock._recorded_all_transitions = _ForbiddenFutureInventory(  # type: ignore[attr-defined]
        mock._recorded_all_transitions  # type: ignore[attr-defined]
    )

    result = mock.cancel_order("broker-order-1")

    assert result["ok"] is False
    assert result["error"] == SEALED_REPLAY_CANCEL_RECEIPT_ARCHITECTURAL_BLOCKER
    assert result["architectural_blocker"] is True
    assert mock.recorded_cancel_request_complete is False


def _binding(*, trace: str = "6" * 64) -> rv3.ReplayV3RunBinding:
    return rv3.ReplayV3RunBinding(
        identity_sha256="1" * 64,
        final_capture_seal_sha256="2" * 64,
        manifest_sha256="3" * 64,
        release_order_root_sha256="4" * 64,
        decision_checkpoint_sha256="5" * 64,
        result_trace_sha256=trace,
        broker_lifecycle_root_sha256="7" * 64,
        adapter_network_attempt_count=0,
        python_network_attempt_count=0,
        adapter_rejected_provider_request_count=0,
    )


def test_schema_valid_os_self_attestation_cannot_remove_blocker() -> None:
    binding = _binding()
    with pytest.raises(rv3.SealedReplayInputError, match="trusted in-process"):
        rv3.ReplayOsZeroEgressAttestation(
            run_binding_sha256=binding.run_binding_sha256,
            network_namespace="none",
            non_loopback_interfaces=(),
            non_loopback_routes=(),
            blocked_connect_ex=101,
            database_transport="unix_domain_socket",
            adapter_network_attempt_count=0,
            python_network_attempt_count=0,
        )


def test_os_attestation_cannot_remove_mutable_database_blocker() -> None:
    binding = _binding()
    result = rv3.ReplayResult(
        certification_failures=[
            rv3.MUTABLE_DATABASE_CERTIFICATION_BLOCKER,
            "os_level_external_network_denial_not_proven",
        ],
        sealed_run_binding=binding,
        sealed_execution_receipt=rv3.ReplayV3ExecutionReceipt(
            binding=binding,
            _verification_token=rv3._REPLAY_V3_EXECUTION_RECEIPT_TOKEN,
        ),
    )
    attestation = rv3.ReplayOsZeroEgressAttestation(
        run_binding_sha256=binding.run_binding_sha256,
        network_namespace="none",
        non_loopback_interfaces=(),
        non_loopback_routes=(),
        blocked_connect_ex=101,
        database_transport="unix_domain_socket",
        adapter_network_attempt_count=0,
        python_network_attempt_count=0,
        _verification_token=rv3._REPLAY_OS_ZERO_EGRESS_ATTESTATION_TOKEN,
    )

    updated = rv3.apply_os_zero_egress_attestation(result, attestation)

    assert updated.certification_failures == [
        rv3.MUTABLE_DATABASE_CERTIFICATION_BLOCKER
    ]
    assert updated.certification_eligible is False


def test_os_attestation_cannot_remove_unobserved_continuous_read_blocker() -> None:
    binding = _binding()
    result = rv3.ReplayResult(
        certification_failures=[
            rv3.CONTINUOUS_DECISION_READS_CERTIFICATION_BLOCKER,
            "os_level_external_network_denial_not_proven",
        ],
        sealed_run_binding=binding,
        sealed_execution_receipt=rv3.ReplayV3ExecutionReceipt(
            binding=binding,
            _verification_token=rv3._REPLAY_V3_EXECUTION_RECEIPT_TOKEN,
        ),
    )
    attestation = rv3.ReplayOsZeroEgressAttestation(
        run_binding_sha256=binding.run_binding_sha256,
        network_namespace="none",
        non_loopback_interfaces=(),
        non_loopback_routes=(),
        blocked_connect_ex=101,
        database_transport="unix_domain_socket",
        adapter_network_attempt_count=0,
        python_network_attempt_count=0,
        _verification_token=rv3._REPLAY_OS_ZERO_EGRESS_ATTESTATION_TOKEN,
    )

    updated = rv3.apply_os_zero_egress_attestation(result, attestation)

    assert updated.certification_failures == [
        rv3.CONTINUOUS_DECISION_READS_CERTIFICATION_BLOCKER
    ]
    assert updated.certification_eligible is False


@pytest.mark.parametrize(
    "blocker",
    (
        rv3.UNSEALED_CAUSAL_RUNTIME_INPUTS_CERTIFICATION_BLOCKER,
        rv3.PROCESS_GLOBAL_STATE_CERTIFICATION_BLOCKER,
    ),
)
def test_os_attestation_cannot_remove_unsealed_runtime_blockers(blocker) -> None:
    binding = _binding()
    result = rv3.ReplayResult(
        certification_failures=[
            blocker,
            "os_level_external_network_denial_not_proven",
        ],
        sealed_run_binding=binding,
        sealed_execution_receipt=rv3.ReplayV3ExecutionReceipt(
            binding=binding,
            _verification_token=rv3._REPLAY_V3_EXECUTION_RECEIPT_TOKEN,
        ),
    )
    attestation = rv3.ReplayOsZeroEgressAttestation(
        run_binding_sha256=binding.run_binding_sha256,
        network_namespace="none",
        non_loopback_interfaces=(),
        non_loopback_routes=(),
        blocked_connect_ex=101,
        database_transport="unix_domain_socket",
        adapter_network_attempt_count=0,
        python_network_attempt_count=0,
        _verification_token=rv3._REPLAY_OS_ZERO_EGRESS_ATTESTATION_TOKEN,
    )

    updated = rv3.apply_os_zero_egress_attestation(result, attestation)

    assert updated.certification_failures == [blocker]
    assert updated.certification_eligible is False


def test_execution_receipt_cannot_be_constructed_from_serialized_binding() -> None:
    with pytest.raises(rv3.SealedReplayInputError, match="receipt token"):
        rv3.ReplayV3ExecutionReceipt(binding=_binding())


def test_exact_run_binding_publication_refuses_structural_self_assertion(
    tmp_path,
) -> None:
    with pytest.raises(rv3.SealedReplayInputError, match="execution receipt"):
        rv3.publish_replay_v3_run_binding(
            rv3.ReplayResult(), tmp_path / "diagnostic.json"
        )

    with pytest.raises(rv3.SealedReplayInputError, match="execution receipt"):
        rv3.publish_replay_v3_run_binding(
            rv3.ReplayResult(sealed_run_binding=_binding()),
            tmp_path / "forged.json",
        )
