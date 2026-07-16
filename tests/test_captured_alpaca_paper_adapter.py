from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
import contextvars
import threading
import uuid

import pytest

from app.services.trading.momentum_neural.alpaca_paper_account_receipt import (
    ALPACA_PAPER_ACCOUNT_PAYLOAD_KEYS,
    ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION,
    ALPACA_PAPER_ACCOUNT_PROVIDER,
    alpaca_paper_account_capture_query,
)
from app.services.trading.momentum_neural.captured_alpaca_paper_adapter import (
    CapturedAlpacaPaperAccountAuthority,
    CapturedAlpacaPaperAdapter,
    CapturedAlpacaPaperObservationAdapter,
    CapturedAlpacaPaperReadError,
    CapturedPaperDecisionIdentityChanged,
    verify_captured_alpaca_paper_account_authority,
)
from app.services.trading.momentum_neural.live_replay_capture import (
    CaptureSessionState,
    CaptureSubmission,
    CapturedReadResult,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    ALPACA_NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
    ALPACA_NBBO_QUOTE_QUERY_SCHEMA_VERSION,
    CaptureClocks,
    CaptureEvent,
    CaptureEventRef,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureStream,
    CaptureTier,
    CoverageMode,
    ActiveCaptureReadEvidence,
    FSMDependencyProfile,
    FSMStreamDependency,
    STREAM_POLICIES,
    _issue_active_capture_input_attestation,
    captured_read_result_sha256,
    sha256_json,
)
from app.services.trading.venue.protocol import (
    FreshnessMeta,
    NormalizedProduct,
    NormalizedTicker,
)


UTC = timezone.utc
BASE = datetime(2026, 7, 15, 18, 0, tzinfo=UTC)
ACCOUNT_ID = "6c143be2-d40a-4a5e-a8a8-d6fc19d2cd79"


class _Clock:
    def __init__(self) -> None:
        self.now = BASE

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> datetime:
        self.now += timedelta(seconds=seconds)
        return self.now


class _PaperAdapter:
    broker_environment = "paper"

    def __init__(self, clock: _Clock) -> None:
        self.clock = clock
        self.bound_account_id = ACCOUNT_ID
        self.account_id = ACCOUNT_ID
        self.paper: Any = True
        self.account_received_clock = True
        self.provider_age_seconds = 0.01
        self.status = "ACTIVE"
        self.account_blocked: Any = False
        self.trading_blocked: Any = False
        self.trade_suspended_by_user: Any = False
        self.account_calls = 0
        self.quote_calls = 0
        self.lifecycle_calls = 0
        self.product_calls = 0

    def get_account_snapshot(self) -> dict[str, Any]:
        self.account_calls += 1
        received = self.clock.advance(0.001)
        return {
            "ok": True,
            "account_id": self.account_id,
            "equity": 71_876.85,
            "last_equity": 73_588.07,
            "buying_power": 287_507.40,
            "cash": -125.50,
            "status": self.status,
            "account_blocked": self.account_blocked,
            "trading_blocked": self.trading_blocked,
            "trade_suspended_by_user": self.trade_suspended_by_user,
            "paper": self.paper,
            "retrieved_at_utc": (
                received.isoformat() if self.account_received_clock else None
            ),
        }

    def get_execution_bbo(self, symbol: str, *, max_age_seconds: float):
        self.quote_calls += 1
        received = self.clock.advance(0.001)
        provider_at = received - timedelta(seconds=self.provider_age_seconds)
        meta = FreshnessMeta(
            retrieved_at_utc=received,
            provider_time_utc=provider_at,
            max_age_seconds=max_age_seconds,
        )
        return (
            NormalizedTicker(
                product_id=symbol,
                bid=2.98,
                ask=3.00,
                mid=2.99,
                bid_size=1_200.0,
                ask_size=900.0,
                freshness=meta,
                raw={
                    "feed": "iex",
                    "timestamp_basis": "provider_event_at",
                    "provider_event_at_utc": provider_at.isoformat(),
                    "received_at_utc": received.isoformat(),
                },
            ),
            meta,
        )

    def get_best_bid_ask(self, _symbol: str):  # forbidden fallback seam
        raise AssertionError("captured adapter fell back to an uncaptured quote")

    def get_product(self, symbol: str):
        self.product_calls += 1
        received = self.clock.advance(0.001)
        return NormalizedProduct(
            product_id=str(symbol).strip().upper(),
            base_currency=str(symbol).strip().upper(),
            quote_currency="USD",
            status="active",
            trading_disabled=False,
            cancel_only=False,
            limit_only=False,
            post_only=False,
            auction_mode=False,
            product_type="equity",
            raw={"exchange": "NASDAQ"},
        ), FreshnessMeta(
            retrieved_at_utc=received,
            provider_time_utc=received,
            max_age_seconds=60.0,
        )

    def list_open_orders_truth(self, *, product_id=None, limit=50):
        self.lifecycle_calls += 1
        return {"readable": True, "orders": (), "product_id": product_id, "limit": limit}


class _Coordinator:
    state = CaptureSessionState.RUNNING
    certification_symbol = "ACTU"

    def __init__(self) -> None:
        account_identity = {
            "broker": "alpaca",
            "environment": "paper",
            "account_id": ACCOUNT_ID,
        }
        self.identity = CaptureRunIdentity(
            run_id=str(uuid.uuid4()),
            generation=1,
            code_build_sha256="1" * 64,
            config_sha256="2" * 64,
            feature_flags_sha256="3" * 64,
            account_identity_sha256=sha256_json(account_identity),
            broker="alpaca",
            broker_environment="paper",
        )
        self._coordinator_producer_id = "captured_paper"
        self._owner_by_stream = {
            CaptureStream.ALPACA_NBBO_QUOTE: self._coordinator_producer_id,
            CaptureStream.ACCOUNT_RISK_SNAPSHOT: self._coordinator_producer_id,
        }
        self.next_sequence = 1
        self.calls: list[dict[str, Any]] = []
        self.results: list[CapturedReadResult] = []
        self.tamper_stream: CaptureStream | None = None
        self.receipt_network_fallback = False

    def capture_query_result(self, **kwargs: Any) -> CapturedReadResult:
        self.calls.append(dict(kwargs))
        observed = kwargs["results"][0]
        payload = dict(observed.payload)
        if kwargs["stream"] is self.tamper_stream:
            payload["account_scope"] = "alpaca:live"
        source = CaptureEvent(
            identity=self.identity,
            sequence=self.next_sequence,
            stream=kwargs["stream"],
            clocks=observed.clocks,
            payload=payload,
            provider=kwargs["provider"],
            symbol=kwargs.get("symbol"),
            query=kwargs["query"],
        )
        self.next_sequence += 1
        source_ref = CaptureEventRef.from_event(source)
        receipt = CaptureReadReceipt(
            read_id=kwargs["read_id"],
            decision_id=kwargs["decision_id"],
            identity_sha256=self.identity.identity_sha256,
            stream=kwargs["stream"],
            provider=kwargs["provider"],
            symbol=kwargs.get("symbol"),
            requested_at=kwargs["requested_at"],
            returned_at=kwargs["returned_at"],
            query_sha256=sha256_json(kwargs["query"]),
            source_event_sha256s=(source.event_sha256,),
            empty_result=False,
            result_sha256=captured_read_result_sha256((source_ref,)),
            content_verified=True,
            replay_network_fallback_used=self.receipt_network_fallback,
            query=kwargs["query"],
        )
        receipt_event = CaptureEvent(
            identity=self.identity,
            sequence=self.next_sequence,
            stream=CaptureStream.READ_RECEIPT,
            clocks=CaptureClocks(
                received_at=kwargs["returned_at"],
                available_at=kwargs["returned_at"],
            ),
            payload=receipt.to_dict(),
            provider=kwargs["provider"],
            symbol=kwargs.get("symbol"),
        )
        self.next_sequence += 1
        submission = CaptureSubmission(
            accepted=True,
            event=receipt_event,
            coverage_gap_recorded=False,
            disposition="fixture_durable_receipt",
        )
        result = CapturedReadResult(
            receipt=receipt,
            source_events=(source,),
            receipt_submission=submission,
            coverage_gap_recorded=False,
        )
        self.results.append(result)
        return result


def _wrapper(
    *,
    clock: _Clock | None = None,
    adapter: _PaperAdapter | None = None,
    coordinator: _Coordinator | None = None,
    account_max_age_seconds: float = 5.0,
):
    clock = clock or _Clock()
    adapter = adapter or _PaperAdapter(clock)
    coordinator = coordinator or _Coordinator()
    wrapper = CapturedAlpacaPaperAdapter(
        adapter=adapter,
        coordinator=coordinator,
        expected_account_id=ACCOUNT_ID,
        wall_clock=clock,
        quote_max_age_seconds=2.0,
        account_max_age_seconds=account_max_age_seconds,
    )
    return wrapper, clock, adapter, coordinator


def _account_input_attestation(
    coordinator: _Coordinator,
    *,
    decision_id: str,
    expires_at: datetime,
):
    result = coordinator.results[0]
    assert result.receipt is not None
    assert result.receipt_submission is not None
    assert result.receipt_submission.event is not None
    receipt_event = result.receipt_submission.event
    read = ActiveCaptureReadEvidence(
        receipt=result.receipt,
        receipt_sha256=sha256_json(result.receipt.to_dict()),
        receipt_event_sha256=receipt_event.event_sha256,
        receipt_event_sequence=receipt_event.sequence,
        receipt_committed_available_at=receipt_event.clocks.available_at,
        producer_id=coordinator._coordinator_producer_id,
        producer_generation=coordinator.identity.generation,
        source_event_refs=tuple(
            CaptureEventRef.from_event(event) for event in result.source_events
        ),
    )
    dependency = FSMStreamDependency(
        stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        exact_provider_event_at_required=False,
        market_reference_at_required=True,
        max_source_age_seconds=5.0,
        coverage_start_at=result.source_events[0].clocks.received_at,
    )
    profile = FSMDependencyProfile(
        required_streams=frozenset({CaptureStream.ACCOUNT_RISK_SNAPSHOT}),
        required_read_ids=(result.receipt.read_id,),
        stream_dependencies=(dependency,),
    )
    return _issue_active_capture_input_attestation(
        run_id=coordinator.identity.run_id,
        generation=coordinator.identity.generation,
        decision_id=decision_id,
        input_prefix_sequence=receipt_event.sequence,
        input_prefix_root_sha256=sha256_json(
            {"receipt_event_sha256": receipt_event.event_sha256}
        ),
        attested_available_at=receipt_event.clocks.available_at,
        expires_at=expires_at,
        dependency_profile=profile,
        identity_sha256=coordinator.identity.identity_sha256,
        account_identity_sha256=coordinator.identity.account_identity_sha256,
        code_build_sha256=coordinator.identity.code_build_sha256,
        config_sha256=coordinator.identity.config_sha256,
        feature_flags_sha256=coordinator.identity.feature_flags_sha256,
        resource_binding_sha256=sha256_json({"resource": "captured-paper-test"}),
        producer_generations={
            coordinator._coordinator_producer_id: coordinator.identity.generation
        },
        required_read_ids=(result.receipt.read_id,),
        read_evidence=(read,),
        continuity_evidence=(),
    )


def test_alpaca_nbbo_stream_is_exact_query_receipt_and_not_iqfeed_nbbo():
    policy = STREAM_POLICIES[CaptureStream.ALPACA_NBBO_QUOTE]
    assert policy.coverage_mode is CoverageMode.QUERY_RECEIPT
    assert policy.tier is CaptureTier.ALWAYS
    assert policy.exact_provider_event_clock_required is True
    assert policy.query_parameters_required is True
    assert CaptureStream.ALPACA_NBBO_QUOTE is not CaptureStream.NBBO_QUOTE
    assert ALPACA_NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION.endswith("alpaca-paper-nbbo.v2")
    assert ALPACA_NBBO_QUOTE_QUERY_SCHEMA_VERSION.endswith(
        "alpaca-paper-nbbo-query.v1"
    )


def test_exact_account_and_bbo_are_receipted_once_and_pinned_per_decision():
    wrapper, _clock, adapter, coordinator = _wrapper()

    with wrapper.decision_scope("decision-1"):
        first, first_fresh = wrapper.get_best_bid_ask("actu")
        second, second_fresh = wrapper.get_execution_bbo(
            "ACTU", max_age_seconds=1.0
        )
        account = wrapper.current_account_evidence
        assert account is not None
        assert account.account_id == ACCOUNT_ID
        assert account.paper is True
        assert account.status == "ACTIVE"
        assert account.last_equity > account.equity
        assert account.cash is not None and account.cash < 0
        assert first.raw["capture_event_sha256"] == second.raw["capture_event_sha256"]
        assert first.raw["capture_content_sha256"] == second.raw["capture_content_sha256"]
        assert first.raw["capture_read_receipt_sha256"] == second.raw[
            "capture_read_receipt_sha256"
        ]
        assert first.raw["capture_sequence"] == second.raw["capture_sequence"]
        assert first.raw["capture_identity_sha256"] == second.raw[
            "capture_identity_sha256"
        ]
        assert first_fresh.provider_time_utc == second_fresh.provider_time_utc
        assert wrapper.get_account_snapshot()["capture_event_sha256"] == (
            account.read.capture_event_sha256
        )
        assert wrapper.list_open_orders_truth(product_id="ACTU", limit=7) == {
            "readable": True,
            "orders": (),
            "product_id": "ACTU",
            "limit": 7,
        }

    assert adapter.account_calls == 1
    assert adapter.quote_calls == 1
    assert adapter.lifecycle_calls == 1
    assert [call["stream"] for call in coordinator.calls] == [
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        CaptureStream.ALPACA_NBBO_QUOTE,
    ]
    account_call = coordinator.calls[0]
    assert account_call["provider"] == ALPACA_PAPER_ACCOUNT_PROVIDER
    assert account_call["query"] == alpaca_paper_account_capture_query(ACCOUNT_ID)
    account_payload = account_call["results"][0].payload
    assert set(account_payload) == ALPACA_PAPER_ACCOUNT_PAYLOAD_KEYS
    assert (
        account_payload["schema_version"]
        == ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION
    )
    quote_call = coordinator.calls[-1]
    assert quote_call["results"][0].clocks.provider_event_at is not None
    assert quote_call["query"]["account_scope"] == "alpaca:paper"
    assert quote_call["results"][0].payload["account_scope"] == "alpaca:paper"


def test_exact_captured_results_are_one_shot_originals_in_active_scope_only():
    wrapper, _clock, _adapter, coordinator = _wrapper()

    with wrapper.decision_scope("decision-exact-result-handoff"):
        wrapper.get_execution_bbo("ACTU")
        account, quote = wrapper.consume_current_captured_reads(symbol="ACTU")
        assert account is coordinator.results[0]
        assert quote is coordinator.results[1]
        assert account.receipt is not None
        assert quote.receipt is not None
        assert account.receipt.stream is CaptureStream.ACCOUNT_RISK_SNAPSHOT
        assert quote.receipt.stream is CaptureStream.ALPACA_NBBO_QUOTE
        with pytest.raises(CapturedAlpacaPaperReadError, match="unavailable"):
            wrapper.consume_current_captured_reads(symbol="ACTU")

    with pytest.raises(CapturedAlpacaPaperReadError, match="decision scope"):
        wrapper.consume_current_captured_reads(symbol="ACTU")


def test_exact_captured_result_handoff_rejects_missing_wrong_or_replaced_reads():
    wrapper, clock, _adapter, _coordinator = _wrapper(
        account_max_age_seconds=10.0
    )
    with wrapper.decision_scope("decision-missing-nbbo"):
        wrapper.capture_account_snapshot()
        with pytest.raises(CapturedAlpacaPaperReadError, match="unavailable"):
            wrapper.consume_current_captured_reads(symbol="ACTU")

    wrapper, clock, _adapter, _coordinator = _wrapper(
        account_max_age_seconds=10.0
    )
    with wrapper.decision_scope("decision-replaced-nbbo"):
        wrapper.get_execution_bbo("ACTU", max_age_seconds=2.0)
        clock.advance(2.5)
        with pytest.raises(CapturedPaperDecisionIdentityChanged):
            wrapper.get_execution_bbo("ACTU", max_age_seconds=2.0)
        with pytest.raises(CapturedAlpacaPaperReadError, match="unavailable"):
            wrapper.consume_current_captured_reads(symbol="ACTU")


def test_exact_captured_result_handoff_rejects_copied_context_on_other_thread():
    wrapper, _clock, _adapter, _coordinator = _wrapper()
    failures: list[BaseException] = []

    with wrapper.decision_scope("decision-thread-bound"):
        wrapper.get_execution_bbo("ACTU")
        copied = contextvars.copy_context()

        def consume() -> None:
            try:
                copied.run(
                    wrapper.consume_current_captured_reads,
                    symbol="ACTU",
                )
            except BaseException as exc:  # expected fail-closed boundary
                failures.append(exc)

        thread = threading.Thread(target=consume)
        thread.start()
        thread.join(timeout=5.0)
        assert not thread.is_alive()
        assert len(failures) == 1
        assert isinstance(failures[0], CapturedAlpacaPaperReadError)
        account, quote = wrapper.consume_current_captured_reads(symbol="ACTU")
        assert account is not quote


def test_account_authority_is_private_and_binds_exact_receipt_and_input_proof():
    wrapper, clock, _adapter, coordinator = _wrapper()
    decision_id = "decision-account-authority"
    with wrapper.decision_scope(decision_id):
        snapshot = wrapper.capture_account_snapshot()
        proof = _account_input_attestation(
            coordinator,
            decision_id=decision_id,
            expires_at=clock.now + timedelta(seconds=30),
        )
        authority = wrapper.issue_account_authority(proof)

    assert isinstance(authority, CapturedAlpacaPaperAccountAuthority)
    assert verify_captured_alpaca_paper_account_authority(authority) is authority
    assert authority.account_id == ACCOUNT_ID
    assert authority.account_payload_sha256 == snapshot.payload_sha256
    assert authority.account_read_receipt_sha256 == (
        snapshot.read.capture_read_receipt_sha256
    )
    assert authority.active_input_attestation_sha256 == proof.attestation_sha256
    with pytest.raises(CapturedAlpacaPaperReadError, match="changed"):
        verify_captured_alpaca_paper_account_authority(
            authority.__class__(
                **{
                    **authority.__dict__,
                    "buying_power_usd": authority.buying_power_usd + 1,
                }
            )
        )


def test_account_authority_rejects_other_decision_or_unattested_account_read():
    wrapper, clock, _adapter, coordinator = _wrapper()
    with wrapper.decision_scope("decision-authority-a"):
        wrapper.capture_account_snapshot()
        wrong_decision = _account_input_attestation(
            coordinator,
            decision_id="decision-authority-a",
            expires_at=clock.now + timedelta(seconds=30),
        )
        object.__setattr__(wrong_decision, "decision_id", "decision-authority-b")
        with pytest.raises(CapturedAlpacaPaperReadError, match="invalid"):
            wrapper.issue_account_authority(wrong_decision)

    wrapper, clock, _adapter, coordinator = _wrapper()
    with wrapper.decision_scope("decision-authority-missing"):
        with pytest.raises(CapturedAlpacaPaperReadError, match="pinned"):
            wrapper.issue_account_authority(object())


def test_expired_pin_captures_new_generation_then_forces_decision_defer():
    wrapper, clock, adapter, coordinator = _wrapper(account_max_age_seconds=10.0)
    with wrapper.decision_scope("decision-expiry"):
        first, _ = wrapper.get_execution_bbo("ACTU", max_age_seconds=2.0)
        clock.advance(2.5)
        with pytest.raises(CapturedPaperDecisionIdentityChanged) as raised:
            wrapper.get_execution_bbo("ACTU", max_age_seconds=2.0)
        assert raised.value.kind == "quote"
        assert raised.value.previous == first.raw["capture_identity_sha256"]
        assert raised.value.current != raised.value.previous
        replacement, _ = wrapper.get_execution_bbo("ACTU", max_age_seconds=2.0)
        assert replacement.raw["capture_generation"] == 2
        assert replacement.raw["capture_identity_sha256"] == raised.value.current

    assert adapter.quote_calls == 2
    assert sum(
        call["stream"] is CaptureStream.ALPACA_NBBO_QUOTE
        for call in coordinator.calls
    ) == 2


@pytest.mark.parametrize(
    ("mutator", "match"),
    [
        (lambda adapter: setattr(adapter, "account_id", str(uuid.uuid4())), "expected"),
        (lambda adapter: setattr(adapter, "paper", False), "expected"),
        (lambda adapter: setattr(adapter, "account_received_clock", False), "missing"),
        (lambda adapter: setattr(adapter, "status", "INACTIVE"), "not ACTIVE"),
        (
            lambda adapter: setattr(adapter, "account_blocked", True),
            "broker-native false",
        ),
        (
            lambda adapter: setattr(adapter, "trading_blocked", None),
            "broker-native false",
        ),
        (
            lambda adapter: setattr(adapter, "trade_suspended_by_user", True),
            "broker-native false",
        ),
    ],
)
def test_wrong_nonpaper_or_clockless_account_fails_before_quote_or_capture(
    mutator, match
):
    clock = _Clock()
    adapter = _PaperAdapter(clock)
    mutator(adapter)
    wrapper, _clock, _adapter, coordinator = _wrapper(
        clock=clock, adapter=adapter
    )
    with wrapper.decision_scope("decision-bad-account"):
        with pytest.raises(CapturedAlpacaPaperReadError, match=match):
            wrapper.get_execution_bbo("ACTU")
    assert adapter.quote_calls == 0
    assert coordinator.calls == []


def test_stale_direct_quote_fails_without_writing_false_nbbo_receipt():
    clock = _Clock()
    adapter = _PaperAdapter(clock)
    adapter.provider_age_seconds = 10.0
    wrapper, _clock, _adapter, coordinator = _wrapper(
        clock=clock, adapter=adapter
    )
    with wrapper.decision_scope("decision-stale"):
        with pytest.raises(CapturedAlpacaPaperReadError, match="stale"):
            wrapper.get_execution_bbo("ACTU", max_age_seconds=2.0)
    assert adapter.quote_calls == 1
    assert [call["stream"] for call in coordinator.calls] == [
        CaptureStream.ACCOUNT_RISK_SNAPSHOT
    ]


def test_boolean_quote_size_is_rejected_before_false_nbbo_receipt(monkeypatch):
    from dataclasses import replace

    wrapper, _clock, adapter, coordinator = _wrapper()
    original = adapter.get_execution_bbo

    def malformed(symbol: str, *, max_age_seconds: float):
        tick, meta = original(symbol, max_age_seconds=max_age_seconds)
        return replace(tick, ask_size=True), meta

    monkeypatch.setattr(adapter, "get_execution_bbo", malformed)
    with wrapper.decision_scope("decision-bool-size"):
        with pytest.raises(CapturedAlpacaPaperReadError, match="sizes are malformed"):
            wrapper.get_execution_bbo("ACTU")
    assert [call["stream"] for call in coordinator.calls] == [
        CaptureStream.ACCOUNT_RISK_SNAPSHOT
    ]


def test_product_eligibility_is_fetched_once_then_served_only_from_frozen_pin():
    wrapper, _clock, adapter, _coordinator = _wrapper()
    with wrapper.decision_scope("decision-product-pin"):
        with pytest.raises(
            CapturedAlpacaPaperReadError,
            match="product eligibility is unavailable",
        ):
            wrapper.get_product("ACTU")
        first, first_freshness = wrapper.capture_product_eligibility("ACTU")
        first.raw["tampered_after_return"] = True
        second, second_freshness = wrapper.get_product("ACTU")
        assert second.product_id == first.product_id == "ACTU"
        assert second.product_type == "equity"
        assert second.raw == {"exchange": "NASDAQ"}
        assert second_freshness is first_freshness
        with pytest.raises(CapturedAlpacaPaperReadError, match="unavailable"):
            wrapper.get_product("WRONG")
    assert adapter.product_calls == 1


def test_tampered_capture_event_or_network_fallback_receipt_is_rejected():
    wrapper, _clock, _adapter, coordinator = _wrapper()
    coordinator.tamper_stream = CaptureStream.ALPACA_NBBO_QUOTE
    with wrapper.decision_scope("decision-tamper"):
        with pytest.raises(CapturedAlpacaPaperReadError, match="differs"):
            wrapper.get_execution_bbo("ACTU")

    wrapper, _clock, _adapter, coordinator = _wrapper()
    coordinator.receipt_network_fallback = True
    with wrapper.decision_scope("decision-fallback"):
        with pytest.raises(CapturedAlpacaPaperReadError, match="does not bind"):
            wrapper.get_execution_bbo("ACTU")


def test_wrapper_rejects_external_stream_owner_and_adapter_binding_rotation():
    clock = _Clock()
    adapter = _PaperAdapter(clock)
    coordinator = _Coordinator()
    coordinator._owner_by_stream[CaptureStream.ALPACA_NBBO_QUOTE] = "iqfeed_l1"
    with pytest.raises(CapturedAlpacaPaperReadError, match="coordinator-owned"):
        _wrapper(clock=clock, adapter=adapter, coordinator=coordinator)

    wrapper, _clock, adapter, _coordinator = _wrapper()
    adapter.bound_account_id = str(uuid.uuid4())
    with pytest.raises(CapturedAlpacaPaperReadError, match="underlying adapter"):
        wrapper.list_open_orders_truth(product_id="ACTU")


def test_reads_require_scope_and_never_call_uncaptured_best_bid_ask_fallback():
    wrapper, _clock, adapter, _coordinator = _wrapper()
    with pytest.raises(CapturedAlpacaPaperReadError, match="decision scope"):
        wrapper.get_best_bid_ask("ACTU")
    with wrapper.decision_scope("decision-no-fallback"):
        tick, _ = wrapper.get_best_bid_ask("ACTU")
        assert tick.raw["feed"] == "iex"
    assert adapter.quote_calls == 1


def test_observation_adapter_exposes_only_captured_reads_and_no_order_capability():
    wrapper, clock, adapter, _coordinator = _wrapper(
        account_max_age_seconds=60.0
    )
    adapter.bind_account_id = lambda account_id: account_id == ACCOUNT_ID
    decision_id = "captured-paper-observe-41-0123456789abcdef01234567"
    with wrapper.decision_scope(decision_id):
        wrapper.get_execution_bbo("ACTU", max_age_seconds=30.0)
        wrapper.consume_current_captured_reads(symbol="ACTU")
        freshness = FreshnessMeta(
            retrieved_at_utc=clock.now,
            provider_time_utc=clock.now - timedelta(milliseconds=1),
            max_age_seconds=60.0,
        )
        observation = CapturedAlpacaPaperObservationAdapter(
            captured_adapter=wrapper,
            product=NormalizedProduct(
                product_id="ACTU",
                base_currency="ACTU",
                quote_currency="USD",
                status="active",
                trading_disabled=False,
                cancel_only=False,
                limit_only=True,
                post_only=False,
                auction_mode=False,
                product_type="equity",
                raw={"captured": True},
            ),
            freshness=freshness,
            eligibility_read_id=str(uuid.uuid4()),
            eligibility_event_sha256="a" * 64,
            observation_decision_id=decision_id,
        )

        assert observation.bind_account_id(ACCOUNT_ID) is True
        product, observed_freshness = observation.get_product("ACTU")
        assert product is not None and product.product_id == "ACTU"
        assert observed_freshness is freshness
        assert observation.get_execution_bbo("ACTU")[0].ask == 3.00
        assert observation.current_account_evidence is wrapper.current_account_evidence
        before = adapter.lifecycle_calls
        with pytest.raises(
            CapturedAlpacaPaperReadError,
            match="no broker mutation capability",
        ):
            observation.place_limit_order_gtc(
                product_id="ACTU",
                side="buy",
                base_size="1",
                limit_price="3.00",
            )
        with pytest.raises(
            CapturedAlpacaPaperReadError,
            match="capability unavailable:get_order",
        ):
            observation.get_order("order-1")
        assert adapter.lifecycle_calls == before
