from __future__ import annotations

from contextlib import contextmanager, nullcontext
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import hashlib
import inspect
import json
import sys
from types import ModuleType, SimpleNamespace

import pytest

from app.config import settings
from app.services.trading.momentum_neural import captured_paper_entry_intent as intent_contract
from app.services.trading.momentum_neural.captured_paper_admission import (
    CommittedCapturedPaperAdmission,
)
from app.services.trading.momentum_neural.captured_paper_outbox import (
    CapturedPaperBrokerAcceptanceProof,
)
from app.services.trading.momentum_neural import (
    captured_paper_outbox as outbox_contract,
)
from app.services.trading.momentum_neural import (
    captured_paper_transport_coordinator as transport,
)
from app.services.trading.momentum_neural import (
    captured_paper_positive_acceptance as positive_acceptance,
)
from app.services.trading.momentum_neural import (
    captured_paper_financial_breaker as financial_breaker,
)
from app.services.trading.venue.alpaca_spot import (
    quantize_alpaca_equity_limit_price,
)
from app.services.trading.venue import alpaca_spot


UTC = timezone.utc
NOW = datetime(2036, 7, 15, 16, 30, tzinfo=UTC)
ACCOUNT_ID = "d7cc580c-2b8f-432f-b771-1cecfb3fe87a"
OWNER_A = "2ed29ed9-79dd-4f75-ae44-2e5a33b8e77e"
OWNER_B = "53fb486d-f420-4dab-a202-c4de1346b9eb"
RESERVATION_ID = "da45acc8-6b95-4d20-8579-8da28e203511"
ARM_TOKEN = "d2b8f7d8-6ad5-4cd0-a94e-8a9ca146d3ab"
BINDER_ID = "122158cc-18ae-4cef-bc52-f1c5b689b352"


def _sha_json(value):
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _request(
    *, entry_limit_price: str = "3", structural_stop_price: str = "2.5"
) -> intent_contract.CapturedPaperPostCommitRequest:
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
        structural_stop_price=structural_stop_price,
        entry_limit_ceiling_price=entry_limit_price,
        account_receipt_sha256="d" * 64,
        bbo_receipt_sha256="e" * 64,
        setup_evidence_sha256="f" * 64,
        policy_sha256="1" * 64,
        feature_flags_sha256="2" * 64,
    )
    return intent_contract.CapturedPaperPostCommitRequest(
        intent=intent,
        completion_generation="73dbcf92-94ea-436e-978c-b0e31ce7252d",
    )


def _admission(
    *,
    extended_hours: bool = False,
    time_in_force: str = "day",
    entry_limit_price: str = "3",
    structural_stop_price: str = "2.5",
) -> CommittedCapturedPaperAdmission:
    request = _request(
        entry_limit_price=entry_limit_price,
        structural_stop_price=structural_stop_price,
    )
    order = {
        "asset_class": "us_equity",
        "client_order_id": request.intent.client_order_id,
        "extended_hours": extended_hours,
        "limit_price": quantize_alpaca_equity_limit_price(
            entry_limit_price, "buy"
        ),
        "position_intent": "buy_to_open",
        "qty": "4578",
        "side": "buy",
        "symbol": request.intent.route_token.symbol,
        "time_in_force": time_in_force,
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


class _Ledger:
    def __init__(self):
        self.active_transactions = 0
        self.state = "pending"
        self.post_calls = []
        self.lookup_calls = []
        self.acceptance_kind = None
        self.fill_reads = 0
        self.fill_appends = 0
        self.transactions = []
        self.durable_instruction = None
        self.reservation_authority_current = True
        self.dispatch_consumed = False

    @contextmanager
    def transaction(self, name):
        assert self.active_transactions == 0
        self.active_transactions += 1
        self.transactions.append(name)
        try:
            yield
        finally:
            self.active_transactions -= 1


class _Store:
    def __init__(self, ledger):
        self.ledger = ledger
        self._lease_token = "eb266345-a759-43cc-9867-176097a0caa1"

    def _lease(self, instruction, owner, reconciliation_only):
        return transport.CapturedPaperCommittedLease(
            completion_sha256=instruction.request.completion_sha256,
            lease_token=self._lease_token,
            lease_owner_id=owner,
            lease_expires_at=NOW + timedelta(minutes=5),
            reconciliation_only=reconciliation_only,
        )

    def verify_committed_instruction(self, instruction):
        if self.ledger.durable_instruction is None:
            self.ledger.durable_instruction = instruction
        if (
            self.ledger.durable_instruction.instruction_sha256
            != instruction.instruction_sha256
        ):
            raise transport.CapturedPaperTransportContractError(
                "committed_admission_durable_instruction_mismatch"
            )
        return self.ledger.durable_instruction

    def load_instruction(self, completion_sha256):
        instruction = self.ledger.durable_instruction
        if (
            instruction is None
            or instruction.request.completion_sha256 != completion_sha256
        ):
            raise transport.CapturedPaperTransportContractError(
                "durable_transport_instruction_missing"
            )
        return instruction

    def next_due_initial_instruction(self):
        if self.ledger.state not in {"pending", "leased"}:
            return None
        return self.ledger.durable_instruction

    def next_due_reconciliation_instruction(self, *, recovery_limit):
        assert recovery_limit > 0
        if self.ledger.state != "transport_indeterminate":
            return None
        return self.ledger.durable_instruction

    def lease_initial(self, instruction, *, lease_owner_id, lease_seconds):
        with self.ledger.transaction("lease_initial"):
            assert lease_seconds > 0
            if self.ledger.state != "pending":
                return None
            self.ledger.state = "leased"
            return self._lease(instruction, lease_owner_id, False)

    def start_transport(self, instruction, lease):
        with self.ledger.transaction("start_transport"):
            assert self.ledger.state == "leased"
            assert lease.reconciliation_only is False
            self.ledger.state = "transport_started"
            return transport.CapturedPaperTransportStart(
                lease=lease,
                instruction_sha256=instruction.instruction_sha256,
                transport_authority_sha256=instruction.authority.authority_sha256,
                started_at=NOW + timedelta(seconds=1),
            )

    def authorize_transport_invocation(self, instruction, start):
        with self.ledger.transaction("authorize_transport_invocation"):
            assert self.ledger.state == "transport_started"
            assert start.instruction_sha256 == instruction.instruction_sha256
            return transport.CapturedPaperTransportInvocationAuthority(
                completion_sha256=start.lease.completion_sha256,
                transport_authority_sha256=(
                    instruction.authority.authority_sha256
                ),
                transport_instruction_sha256=instruction.instruction_sha256,
                lease_token=start.lease.lease_token,
                lease_owner_id=start.lease.lease_owner_id,
                transport_started_at=start.started_at,
                verified_at=start.started_at,
                valid_until=start.lease.lease_expires_at,
                outbox_version=3,
                authorization_event_sequence=3,
                previous_event_sha256="f" * 64,
            )

    def record_financial_breaker_authority(
        self,
        instruction,
        start,
        invocation_authority,
        receipt,
    ):
        with self.ledger.transaction("record_financial_breaker_authority"):
            assert self.ledger.state == "transport_started"
            invocation_authority.verify_for(
                instruction.authority,
                transport_instruction_sha256=instruction.instruction_sha256,
                lease_token=start.lease.lease_token,
                lease_owner_id=start.lease.lease_owner_id,
            )
            receipt.verify_for_request(
                instruction.request,
                phase="pre_post",
                now=receipt.issued_at,
                require_allowed=False,
                transport_instruction_sha256=instruction.instruction_sha256,
                transport_invocation_authority_sha256=(
                    invocation_authority.invocation_authority_sha256
                ),
            )
            return receipt

    def consume_dispatch_authority(
        self,
        instruction,
        start,
        invocation_authority,
        financial_breaker_receipt,
        pre_dispatch_evidence,
    ):
        with self.ledger.transaction("consume_dispatch_authority"):
            assert self.ledger.state == "transport_started"
            assert self.ledger.dispatch_consumed is False
            invocation_authority.verify_for(
                instruction.authority,
                transport_instruction_sha256=instruction.instruction_sha256,
                lease_token=start.lease.lease_token,
                lease_owner_id=start.lease.lease_owner_id,
            )
            pre_dispatch_evidence.verify_for(
                instruction.authority,
                invocation_authority,
                transport_instruction_sha256=instruction.instruction_sha256,
            )
            if not self.ledger.reservation_authority_current:
                raise transport.CapturedPaperTransportContractError(
                    "transport_dispatch_reservation_authority_invalid"
                )
            verified_at = pre_dispatch_evidence.prepared_at
            valid_until = min(
                start.lease.lease_expires_at,
                invocation_authority.valid_until,
                financial_breaker_receipt.valid_until,
                pre_dispatch_evidence.valid_until,
            )
            authority = outbox_contract._attest_transport_dispatch_authority(
                transport.CapturedPaperTransportDispatchAuthority(
                    completion_sha256=instruction.request.completion_sha256,
                    transport_authority_sha256=(
                        instruction.authority.authority_sha256
                    ),
                    transport_instruction_sha256=(
                        instruction.instruction_sha256
                    ),
                    invocation_authority_sha256=(
                        invocation_authority.invocation_authority_sha256
                    ),
                    financial_breaker_receipt_sha256=(
                        financial_breaker_receipt.receipt_sha256
                    ),
                    pre_dispatch_evidence_sha256=(
                        pre_dispatch_evidence.evidence_sha256
                    ),
                    connection_receipt_sha256=(
                        pre_dispatch_evidence.connection_receipt_sha256
                    ),
                    lease_token=start.lease.lease_token,
                    lease_owner_id=start.lease.lease_owner_id,
                    verified_at=verified_at,
                    valid_until=valid_until,
                    outbox_version=4,
                    dispatch_event_sequence=5,
                    previous_event_sha256="e" * 64,
                )
            )
            self.ledger.dispatch_consumed = True
            return authority

    @contextmanager
    def acquire_dispatch_linearization(
        self,
        instruction,
        start,
        invocation_authority,
        financial_breaker_receipt,
        pre_dispatch_evidence,
        dispatch_authority,
    ):
        with self.ledger.transaction("acquire_dispatch_linearization"):
            assert self.ledger.state == "transport_started"
            assert self.ledger.dispatch_consumed is True
            dispatch_authority.verify_for(
                instruction.authority,
                invocation_authority,
                financial_breaker_receipt,
                pre_dispatch_evidence,
                transport_instruction_sha256=instruction.instruction_sha256,
            )
            if not self.ledger.reservation_authority_current:
                raise transport.CapturedPaperTransportContractError(
                    "transport_dispatch_reservation_authority_invalid"
                )
        assert self.ledger.active_transactions == 0
        yield

    def mark_transport_indeterminate(self, start, *, evidence_sha256):
        with self.ledger.transaction("mark_transport_indeterminate"):
            assert self.ledger.state == "transport_started"
            assert len(evidence_sha256) == 64
            self.ledger.state = "transport_indeterminate"

    def complete_direct_acceptance(self, instruction, start, acceptance):
        with self.ledger.transaction("complete_direct_acceptance"):
            assert self.ledger.state == "broker_accepted"
            assert self.ledger.acceptance_kind == "post_response"
            assert self.ledger.dispatch_consumed is True
            self.ledger.state = "completed"

    def lease_reconciliation(
        self, instruction, *, lease_owner_id, lease_seconds
    ):
        with self.ledger.transaction("lease_reconciliation"):
            assert lease_seconds > 0
            if self.ledger.state != "transport_indeterminate":
                return None
            self.ledger.state = "reconciling"
            return self._lease(instruction, lease_owner_id, True)

    def mark_reconciliation_pending(self, lease, *, evidence_sha256):
        with self.ledger.transaction("mark_reconciliation_pending"):
            assert self.ledger.state == "reconciling"
            assert lease.reconciliation_only is True
            assert len(evidence_sha256) == 64
            self.ledger.state = "transport_indeterminate"

    def complete_reconciliation_acceptance(
        self, instruction, lease, acceptance
    ):
        with self.ledger.transaction("complete_reconciliation_acceptance"):
            assert self.ledger.state == "broker_accepted"
            assert self.ledger.acceptance_kind == "same_cid_reconciliation"
            assert lease.reconciliation_only is True
            self.ledger.state = "completed"


def _unresolved(reason):
    return transport.CapturedPaperUnresolvedObservation(
        reason=reason,
        evidence_sha256=_sha_json({"reason": reason}),
    )


def _positive(instruction, *, order_id="alpaca-order-ACTU-1"):
    exact = transport.CapturedPaperExactBrokerOrderObservation(
        account_scope=instruction.account_scope,
        expected_account_id=instruction.expected_account_id,
        verified_adapter_account_id=instruction.expected_account_id,
        account_binding_source=(
            transport.EXACT_PAPER_ACCOUNT_BINDING_SOURCE
        ),
        broker_account_id=instruction.expected_account_id,
        client_order_id=instruction.client_order_id,
        broker_order_id=order_id,
        symbol=instruction.symbol,
        side="buy",
        order_type="limit",
        asset_class="us_equity",
        quantity_shares=instruction.quantity_shares,
        broker_quantity_echo=str(instruction.quantity_shares),
        broker_filled_quantity_echo="0",
        cumulative_filled_quantity_shares=0,
        limit_price=instruction.limit_price,
        broker_limit_price_echo=instruction.limit_price,
        time_in_force=instruction.time_in_force,
        extended_hours=instruction.extended_hours,
        position_intent_echo=None,
        broker_order_status="accepted",
        broker_order_status_echo="accepted",
        broker_connection_generation="alpaca-paper-rest-generation-1",
        broker_order_evidence_sha256=_sha_json(
            {"cid": instruction.client_order_id, "order_id": order_id}
        ),
        observed_at=NOW + timedelta(seconds=2),
        available_at=NOW + timedelta(seconds=3),
    )
    return transport.CapturedPaperPositiveOrderObservation(order=exact)


def _fill_required(instruction, *, order_id="alpaca-order-ACTU-1"):
    exact = replace(
        _positive(instruction, order_id=order_id).order,
        broker_filled_quantity_echo="1",
        cumulative_filled_quantity_shares=1,
        broker_order_status="partially_filled",
        broker_order_status_echo="partially_filled",
        broker_order_evidence_sha256=_sha_json(
            {
                "cid": instruction.client_order_id,
                "order_id": order_id,
                "filled": 1,
            }
        ),
    )
    return transport.CapturedPaperFillReconciliationRequiredObservation(
        order=exact
    )


def _direct_response(kwargs, **patch):
    response = {
        "ok": True,
        "order_id": "alpaca-order-1",
        "client_order_id": kwargs["client_order_id"],
        "status": "open",
        "position_intent": "buy_to_open",
        "broker_account_id_echo": ACCOUNT_ID,
        "broker_order_id_echo": "alpaca-order-1",
        "broker_client_order_id_echo": kwargs["client_order_id"],
        "broker_symbol_echo": kwargs["product_id"],
        "broker_side_echo": kwargs["side"],
        "broker_order_type_echo": "limit",
        "broker_quantity_echo": kwargs["base_size"],
        "broker_limit_price_echo": kwargs["limit_price"],
        "broker_time_in_force_echo": kwargs["time_in_force"],
        "broker_extended_hours_echo": kwargs["extended_hours"],
        "broker_order_status_echo": "accepted",
        "broker_filled_quantity_echo": "0",
        "broker_cumulative_filled_quantity": 0,
        "broker_position_intent_echo": None,
        "broker_asset_class_echo": kwargs["asset_class"],
    }
    response.update(patch)
    return response


def _interpret_direct_response(exact, instruction):
    """Exercise pure response classification without bypassing live I/O fences."""

    raw = exact._adapter.place_limit_order_gtc(
        **instruction.adapter_kwargs()
    )
    return exact._interpret_direct_post_result(
        instruction,
        raw=raw,
        requested_at=NOW + timedelta(seconds=1),
        available_at=NOW + timedelta(seconds=2),
        connection_receipt_sha256="9" * 64,
    )


class _ExactSdkClient:
    def __init__(self):
        self.submit_calls = []
        self.lookup_calls = []
        self.lookup_order = None

    def submit_order(self, *, order_data):
        self.submit_calls.append(order_data)
        return SimpleNamespace(
            id="alpaca-order-1",
            account_id=ACCOUNT_ID,
            client_order_id=order_data.client_order_id,
            symbol=order_data.symbol,
            side=order_data.side,
            order_type="limit",
            type="limit",
            qty=order_data.qty,
            limit_price=order_data.limit_price,
            time_in_force=order_data.time_in_force,
            extended_hours=bool(
                getattr(order_data, "extended_hours", False)
            ),
            status="accepted",
            filled_qty="0",
            position_intent=order_data.position_intent,
            asset_class="us_equity",
            filled_avg_price=None,
            created_at=NOW,
            filled_at=None,
            submitted_at=NOW,
        )

    def get_order_by_client_id(self, client_order_id):
        self.lookup_calls.append(client_order_id)
        if self.lookup_order is None:
            raise AssertionError("unexpected fake same-CID lookup")
        return self.lookup_order


class _FinancialBreakerIssuer:
    def __init__(
        self,
        clock,
        *,
        checked_clock=None,
        allowed=True,
        mutate=None,
        raises=None,
    ):
        self._clock = clock
        self._checked_clock = checked_clock or clock
        self.allowed = allowed
        self.mutate = mutate
        self.raises = raises
        self.calls = []

    def issue_for_request(
        self,
        request,
        *,
        phase,
        transport_instruction_sha256=None,
        transport_invocation_authority_sha256=None,
        authority_valid_until=None,
    ):
        self.calls.append((request.completion_sha256, phase))
        if self.raises is not None:
            raise self.raises
        now = self._clock()
        checked_at = self._checked_clock()
        blocker = None if self.allowed else "governance_kill_switch"
        reason = None if self.allowed else "governance_kill_switch"
        evidence = {
            "schema_version": "chili.alpaca-final-breaker-admission.v1",
            "phase": phase,
            "execution_family": request.route_token.execution_family,
            "checked_at_utc": checked_at.isoformat(),
            "checks": [
                {
                    "id": "governance_kill_switch",
                    "ok": self.allowed,
                }
            ],
            "allowed": self.allowed,
            "breaker": blocker,
            "reason": reason,
        }
        route = request.route_token
        intent = request.intent
        receipt = financial_breaker.CapturedPaperFinancialBreakerReceipt(
            phase=phase,
            completion_sha256=request.completion_sha256,
            route_token_sha256=route.route_token_sha256,
            intent_sha256=intent.intent_sha256,
            session_id=route.session_id,
            symbol=route.symbol,
            execution_family=route.execution_family,
            account_scope=route.account_scope,
            expected_account_id=route.expected_account_id,
            code_build_sha256=route.code_build_sha256,
            config_sha256=route.config_sha256,
            feature_flags_sha256=intent.feature_flags_sha256,
            policy_sha256=intent.policy_sha256,
            runtime_generation=route.runtime_generation,
            intent_generation=intent.intent_generation,
            completion_generation=request.completion_generation,
            decision_id=intent.decision_id,
            capture_receipt_sha256=route.capture_receipt_sha256,
            checked_at=checked_at,
            issued_at=now,
            valid_until=min(
                checked_at + timedelta(seconds=5),
                authority_valid_until,
                intent.confirmed_arm_generation.expires_at,
            ),
            allowed=self.allowed,
            blocker=blocker,
            reason=reason,
            breaker_evidence=evidence,
            transport_instruction_sha256=transport_instruction_sha256,
            transport_invocation_authority_sha256=(
                transport_invocation_authority_sha256
            ),
        )
        return self.mutate(receipt) if self.mutate is not None else receipt


def _install_exact_transport(
    monkeypatch,
    *,
    client=None,
    now=NOW,
):
    client = client or _ExactSdkClient()
    holder = {"client": client, "now": now}
    alpaca_pkg = ModuleType("alpaca")
    alpaca_pkg.__path__ = []
    trading_pkg = ModuleType("alpaca.trading")
    trading_pkg.__path__ = []
    enums_mod = ModuleType("alpaca.trading.enums")
    requests_mod = ModuleType("alpaca.trading.requests")

    class _OrderSide:
        BUY = "buy"
        SELL = "sell"

    class _PositionIntent:
        BUY_TO_OPEN = "buy_to_open"
        BUY_TO_CLOSE = "buy_to_close"
        SELL_TO_OPEN = "sell_to_open"
        SELL_TO_CLOSE = "sell_to_close"

    class _TimeInForce:
        DAY = "day"
        GTC = "gtc"

    class _OrderRequest:
        def __init__(self, **kwargs):
            for key, value in kwargs.items():
                setattr(self, key, value)

    enums_mod.OrderSide = _OrderSide
    enums_mod.PositionIntent = _PositionIntent
    enums_mod.TimeInForce = _TimeInForce
    requests_mod.LimitOrderRequest = _OrderRequest
    requests_mod.MarketOrderRequest = _OrderRequest
    monkeypatch.setitem(sys.modules, "alpaca", alpaca_pkg)
    monkeypatch.setitem(sys.modules, "alpaca.trading", trading_pkg)
    monkeypatch.setitem(sys.modules, "alpaca.trading.enums", enums_mod)
    monkeypatch.setitem(sys.modules, "alpaca.trading.requests", requests_mod)
    monkeypatch.setattr(settings, "chili_alpaca_enabled", True, raising=False)
    monkeypatch.setattr(settings, "chili_alpaca_paper", True, raising=False)
    monkeypatch.setattr(
        settings, "chili_alpaca_api_key", "test-paper-key", raising=False
    )
    monkeypatch.setattr(
        settings, "chili_alpaca_api_secret", "test-paper-secret", raising=False
    )
    monkeypatch.setattr(
        settings,
        "chili_alpaca_expected_account_id",
        ACCOUNT_ID,
        raising=False,
    )
    monkeypatch.setattr(
        alpaca_spot, "_trading_client", lambda: holder["client"]
    )
    monkeypatch.setattr(alpaca_spot, "_now", lambda: holder["now"])
    monkeypatch.setitem(alpaca_spot._clients, "trading:paper", client)
    monkeypatch.setitem(
        alpaca_spot._clients, "trading:observed_account_id", ACCOUNT_ID
    )
    monkeypatch.setitem(
        alpaca_spot._clients, "trading:fingerprint", "a" * 64
    )
    adapter = alpaca_spot.AlpacaSpotAdapter()
    assert adapter.bind_account_id(ACCOUNT_ID) is True
    receipt = alpaca_spot._EXACT_PAPER_CONNECTION_RECEIPT_METHOD(adapter)
    exact = transport.ExactAlpacaPaperEntryTransport(
        adapter=adapter,
        expected_account_id=ACCOUNT_ID,
        broker_connection_generation=receipt[
            "adapter_connection_generation"
        ],
        observation_clock=lambda: holder["now"],
        acquire_external_dispatch_authority=nullcontext,
    )
    return exact, adapter, client, holder, receipt


def _drift_exact_client(monkeypatch, holder):
    replacement = _ExactSdkClient()
    holder["client"] = replacement
    monkeypatch.setitem(alpaca_spot._clients, "trading:paper", replacement)
    monkeypatch.setitem(
        alpaca_spot._clients, "trading:observed_account_id", ACCOUNT_ID
    )
    monkeypatch.setitem(
        alpaca_spot._clients, "trading:fingerprint", "b" * 64
    )
    return replacement


class _Broker:
    def __init__(self, ledger, *, posts=(), lookups=()):
        self.ledger = ledger
        self.posts = list(posts)
        self.lookups = list(lookups)

    def preflight(self, instruction):
        assert self.ledger.active_transactions == 0
        assert instruction.account_scope == "alpaca:paper"
        kwargs = instruction.adapter_kwargs()
        assert kwargs["product_id"] == "ACTU"
        assert kwargs["side"] == "buy"
        assert kwargs["base_size"] == "4578"
        assert kwargs["limit_price"] == instruction.limit_price
        assert kwargs["client_order_id"] == "chili_ml_ACTU_41_1"
        assert type(kwargs["extended_hours"]) is bool
        assert kwargs["position_intent"] == "buy_to_open"
        assert kwargs["time_in_force"] in {"day", "gtc"}
        assert not (
            kwargs["extended_hours"] is True
            and kwargs["time_in_force"] != "day"
        )
        assert kwargs["asset_class"] == "us_equity"

    def prepare_limit_buy(
        self,
        instruction,
        *,
        invocation_authority,
    ):
        assert self.ledger.active_transactions == 0
        invocation_authority.verify_for(
            instruction.authority,
            transport_instruction_sha256=instruction.instruction_sha256,
            lease_token=invocation_authority.lease_token,
            lease_owner_id=invocation_authority.lease_owner_id,
        )
        prepared_at = invocation_authority.verified_at
        return transport.CapturedPaperTransportPreDispatchEvidence(
            completion_sha256=instruction.request.completion_sha256,
            transport_authority_sha256=(
                instruction.authority.authority_sha256
            ),
            transport_instruction_sha256=instruction.instruction_sha256,
            invocation_authority_sha256=(
                invocation_authority.invocation_authority_sha256
            ),
            connection_receipt_sha256="b" * 64,
            account_scope=instruction.account_scope,
            expected_account_id=instruction.expected_account_id,
            broker_connection_generation="alpaca-paper-rest-generation-1",
            adapter_build_sha256="a" * 64,
            connection_available_at=prepared_at,
            prepared_at=prepared_at,
            valid_until=invocation_authority.valid_until,
        )

    def post_limit_buy(
        self,
        instruction,
        *,
        invocation_authority,
        financial_breaker_receipt,
        pre_dispatch_evidence,
        dispatch_authority,
    ):
        assert self.ledger.active_transactions == 0
        invocation_authority.verify_for(
            instruction.authority,
            transport_instruction_sha256=instruction.instruction_sha256,
            lease_token=invocation_authority.lease_token,
            lease_owner_id=invocation_authority.lease_owner_id,
        )
        financial_breaker_receipt.verify_for_request(
            instruction.request,
            phase="pre_post",
            now=financial_breaker_receipt.issued_at,
            require_allowed=True,
            transport_instruction_sha256=instruction.instruction_sha256,
            transport_invocation_authority_sha256=(
                invocation_authority.invocation_authority_sha256
            ),
        )
        dispatch_authority.verify_for(
            instruction.authority,
            invocation_authority,
            financial_breaker_receipt,
            pre_dispatch_evidence,
            transport_instruction_sha256=instruction.instruction_sha256,
        )
        outbox_contract._consume_transport_dispatch_process_attestation(
            dispatch_authority
        )
        assert self.ledger.dispatch_consumed is True
        self.ledger.post_calls.append(instruction.client_order_id)
        value = self.posts.pop(0)
        return value(instruction) if callable(value) else value

    def lookup_same_cid(self, instruction):
        assert self.ledger.active_transactions == 0
        self.ledger.lookup_calls.append(instruction.client_order_id)
        value = self.lookups.pop(0)
        return value(instruction) if callable(value) else value


class _AcceptanceRecorder:
    def __init__(self, ledger):
        self.ledger = ledger

    def persist_positive_acceptance(
        self, instruction, observation, *, acceptance_kind
    ):
        assert self.ledger.active_transactions == 0
        observation.verify_for_instruction(instruction)
        with self.ledger.transaction("persist_positive_acceptance"):
            assert self.ledger.state in {"transport_started", "reconciling"}
            self.ledger.state = "broker_accepted"
            self.ledger.acceptance_kind = acceptance_kind
            return CapturedPaperBrokerAcceptanceProof(
                acceptance_kind=acceptance_kind,
                completion_sha256=instruction.request.completion_sha256,
                account_scope=instruction.account_scope,
                expected_account_id=instruction.expected_account_id,
                client_order_id=instruction.client_order_id,
                broker_order_id=observation.broker_order_id,
                reservation_id=instruction.authority.reservation_id,
                action_claim_token=instruction.authority.action_claim_token,
                binder_id=instruction.authority.binder_id,
                broker_order_evidence_sha256=(
                    observation.broker_order_evidence_sha256
                ),
                observed_at=observation.observed_at,
                available_at=observation.available_at,
            )


class _FillCapture:
    def __init__(self, ledger):
        self.ledger = ledger

    def read_exact_order_fills(self, instruction, observation):
        assert self.ledger.active_transactions == 0
        self.ledger.fill_reads += 1
        positive = (
            type(observation)
            is transport.CapturedPaperFillReconciliationRequiredObservation
        )
        return transport.CapturedPaperFillReadAuthority(
            account_scope=instruction.account_scope,
            expected_account_id=instruction.expected_account_id,
            reservation_id=instruction.authority.reservation_id,
            client_order_id=instruction.client_order_id,
            broker_order_id=observation.broker_order_id,
            query_receipt_sha256="8" * 64,
            observation_sha256="9" * 64,
            exact_activity_count=int(positive),
            positive_fill_observed=positive,
            pagination_complete=True,
            available_at=NOW + timedelta(seconds=4),
        )

    def append_fill_read(
        self,
        read,
        *,
        instruction,
        fill_handoff_required,
    ):
        assert self.ledger.active_transactions == 0
        assert instruction.authority.reservation_id == read.reservation_id
        assert instruction.client_order_id == read.client_order_id
        with self.ledger.transaction("append_fill_read"):
            self.ledger.fill_appends += 1
            if fill_handoff_required:
                assert self.ledger.state == "transport_indeterminate"
                self.ledger.state = "fill_handoff_committed"
            return transport.CapturedPaperFillAppendReceipt(
                observation_sha256=read.observation_sha256,
                durable_receipt_sha256="a" * 64,
                committed_at=NOW + timedelta(seconds=5),
                positive_fill_handoff_committed=fill_handoff_required,
                fill_handoff_proof_sha256=(
                    "b" * 64 if fill_handoff_required else None
                ),
                outbox_fill_handoff_receipt_sha256=(
                    "c" * 64 if fill_handoff_required else None
                ),
            )


def _coordinator(
    ledger,
    broker,
    *,
    fill_capture=None,
    financial_issuer=None,
    external_authority_guard=None,
):
    return transport.CapturedPaperTransportCoordinator(
        store=_Store(ledger),
        broker_transport=broker,
        financial_breaker_issuer=(
            financial_issuer or _FinancialBreakerIssuer(lambda: NOW)
        ),
        acceptance_recorder=_AcceptanceRecorder(ledger),
        fill_capture=fill_capture,
        assert_external_authority_current=(
            external_authority_guard or (lambda: None)
        ),
    )


def test_timeout_then_restart_and_cid_absence_never_reposts_or_terminalizes():
    ledger = _Ledger()
    broker = _Broker(
        ledger,
        posts=[_unresolved("transport_timeout")],
        lookups=[
            _unresolved("cid_absent"),
            _unresolved("cid_unreadable"),
        ],
    )
    first = _coordinator(ledger, broker).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )
    assert first.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]

    # A restarted initial worker sees no generic completion work.
    restarted = _coordinator(ledger, broker).submit_once(
        _admission(), lease_owner_id=OWNER_B, lease_seconds=30
    )
    assert restarted.status == "no_work"
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]

    for expected_reason in ("cid_absent", "cid_unreadable"):
        pending = _coordinator(ledger, broker).reconcile_once(
            _admission(), lease_owner_id=OWNER_B, lease_seconds=30
        )
        assert pending.status == "reconciliation_pending"
        assert pending.evidence_sha256 == _sha_json({"reason": expected_reason})
        assert ledger.state == "transport_indeterminate"
        assert ledger.post_calls == ["chili_ml_ACTU_41_1"]
    assert len(ledger.lookup_calls) == 2


def test_restart_resumes_exact_durable_marker_free_instruction_once():
    ledger = _Ledger()
    admission = _admission()
    ledger.durable_instruction = (
        transport.CapturedPaperTransportInstruction.from_admission(admission)
    )
    broker = _Broker(
        ledger,
        posts=[lambda instruction: _positive(instruction)],
    )

    outcome = _coordinator(ledger, broker).resume_restart_once(
        lease_owner_id=OWNER_B,
        lease_seconds=30,
        recovery_limit=10,
    )

    assert outcome is not None
    assert outcome.status == "accepted"
    assert ledger.state == "completed"
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]
    assert ledger.lookup_calls == []


def test_restart_after_transport_marker_only_reconciles_same_cid():
    ledger = _Ledger()
    broker = _Broker(
        ledger,
        posts=[_unresolved("transport_timeout")],
        lookups=[_unresolved("cid_absent")],
    )
    first = _coordinator(ledger, broker).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )
    assert first.status == "transport_indeterminate"

    outcome = _coordinator(ledger, broker).resume_restart_once(
        lease_owner_id=OWNER_B,
        lease_seconds=30,
        recovery_limit=10,
    )

    assert outcome is not None
    assert outcome.status == "reconciliation_pending"
    assert ledger.state == "transport_indeterminate"
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]
    assert ledger.lookup_calls == ["chili_ml_ACTU_41_1"]


def test_restart_never_reconstructs_instruction_from_drifted_config():
    ledger = _Ledger()
    sealed = _admission(extended_hours=False, time_in_force="day")
    ledger.durable_instruction = (
        transport.CapturedPaperTransportInstruction.from_admission(sealed)
    )
    broker = _Broker(ledger)

    with pytest.raises(
        transport.CapturedPaperTransportContractError,
        match="committed_admission_durable_instruction_mismatch",
    ):
        _coordinator(ledger, broker).submit_once(
            _admission(extended_hours=False, time_in_force="gtc"),
            lease_owner_id=OWNER_B,
            lease_seconds=30,
        )

    assert ledger.state == "pending"
    assert ledger.post_calls == []
    assert ledger.lookup_calls == []


def test_positive_same_cid_reconciliation_completes_with_one_lifetime_post():
    ledger = _Ledger()
    broker = _Broker(
        ledger,
        posts=[_unresolved("transport_server_error")],
        lookups=[lambda instruction: _positive(instruction)],
    )
    fill = _FillCapture(ledger)
    coordinator = _coordinator(ledger, broker, fill_capture=fill)

    first = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )
    assert first.status == "transport_indeterminate"
    reconciled = coordinator.reconcile_once(
        _admission(), lease_owner_id=OWNER_B, lease_seconds=30
    )

    assert reconciled.status == "accepted"
    assert reconciled.fill_status == "durably_appended"
    assert reconciled.fill_receipt_sha256 == "a" * 64
    assert ledger.state == "completed"
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]
    assert ledger.lookup_calls == ["chili_ml_ACTU_41_1"]
    assert ledger.acceptance_kind == "same_cid_reconciliation"
    assert ledger.fill_reads == ledger.fill_appends == 1
    assert ledger.active_transactions == 0


def test_direct_positive_fill_is_retained_and_appended_before_any_acceptance():
    ledger = _Ledger()
    broker = _Broker(
        ledger,
        posts=[lambda instruction: _fill_required(instruction)],
    )
    fill = _FillCapture(ledger)

    coordinator = _coordinator(ledger, broker, fill_capture=fill)
    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "fill_reconciliation_required"
    assert outcome.fill_status == "fill_handoff_committed"
    assert outcome.fill_receipt_sha256 == "c" * 64
    assert outcome.broker_order_id == "alpaca-order-ACTU-1"
    assert ledger.state == "fill_handoff_committed"
    assert ledger.acceptance_kind is None
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]
    assert ledger.fill_reads == ledger.fill_appends == 1
    assert coordinator.resume_restart_once(
        lease_owner_id=OWNER_B,
        lease_seconds=30,
        recovery_limit=10,
    ) is None
    assert ledger.lookup_calls == []


def test_same_cid_positive_fill_never_becomes_zero_fill_acceptance():
    ledger = _Ledger()
    broker = _Broker(
        ledger,
        posts=[_unresolved("transport_timeout")],
        lookups=[lambda instruction: _fill_required(instruction)],
    )
    fill = _FillCapture(ledger)
    coordinator = _coordinator(ledger, broker, fill_capture=fill)
    coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    outcome = coordinator.reconcile_once(
        _admission(), lease_owner_id=OWNER_B, lease_seconds=30
    )

    assert outcome.status == "fill_reconciliation_required"
    assert outcome.fill_status == "fill_handoff_committed"
    assert outcome.fill_receipt_sha256 == "c" * 64
    assert ledger.state == "fill_handoff_committed"
    assert ledger.acceptance_kind is None
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]
    assert ledger.lookup_calls == ["chili_ml_ACTU_41_1"]


def test_positive_fill_without_committed_handoff_stays_fail_closed():
    ledger = _Ledger()
    broker = _Broker(
        ledger,
        posts=[lambda instruction: _fill_required(instruction)],
    )

    class MissingHandoff(_FillCapture):
        def append_fill_read(
            self,
            read,
            *,
            instruction,
            fill_handoff_required,
        ):
            assert fill_handoff_required is True
            with self.ledger.transaction("append_fill_read"):
                self.ledger.fill_appends += 1
                return transport.CapturedPaperFillAppendReceipt(
                    observation_sha256=read.observation_sha256,
                    durable_receipt_sha256="a" * 64,
                    committed_at=NOW + timedelta(seconds=5),
                )

    outcome = _coordinator(
        ledger,
        broker,
        fill_capture=MissingHandoff(ledger),
    ).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "fill_reconciliation_required"
    assert outcome.fill_status == "coverage_unavailable"
    assert outcome.fill_receipt_sha256 is None
    assert ledger.state == "transport_indeterminate"
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]
    assert ledger.acceptance_kind is None


def test_direct_positive_acceptance_without_fill_authority_fails_fill_closed():
    ledger = _Ledger()
    broker = _Broker(
        ledger,
        posts=[lambda instruction: _positive(instruction)],
    )
    outcome = _coordinator(ledger, broker, fill_capture=None).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "accepted"
    assert outcome.fill_status == "coverage_unavailable"
    assert outcome.fill_receipt_sha256 is None
    assert ledger.state == "completed"
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]
    assert ledger.acceptance_kind == "post_response"


def test_transport_exception_after_marker_is_indeterminate_and_not_retried():
    ledger = _Ledger()

    class RaisingBroker(_Broker):
        def post_limit_buy(
            self,
            instruction,
            *,
            invocation_authority,
            financial_breaker_receipt,
            pre_dispatch_evidence,
            dispatch_authority,
        ):
            assert self.ledger.active_transactions == 0
            assert type(invocation_authority) is (
                transport.CapturedPaperTransportInvocationAuthority
            )
            assert financial_breaker_receipt.allowed is True
            dispatch_authority.verify_for(
                instruction.authority,
                invocation_authority,
                financial_breaker_receipt,
                pre_dispatch_evidence,
                transport_instruction_sha256=instruction.instruction_sha256,
            )
            outbox_contract._consume_transport_dispatch_process_attestation(
                dispatch_authority
            )
            self.ledger.post_calls.append(instruction.client_order_id)
            raise TimeoutError("lost response")

    broker = RaisingBroker(ledger)
    coordinator = _coordinator(ledger, broker)
    first = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )
    restarted = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_B, lease_seconds=30
    )

    assert first.status == "transport_indeterminate"
    assert restarted.status == "no_work"
    assert ledger.state == "transport_indeterminate"
    assert ledger.post_calls == ["chili_ml_ACTU_41_1"]


def test_host_revocation_before_fresh_lease_leaves_outbox_untouched():
    ledger = _Ledger()
    broker = _Broker(ledger, posts=[])

    def _revoked():
        raise RuntimeError("host activation revoked")

    coordinator = _coordinator(
        ledger,
        broker,
        external_authority_guard=_revoked,
    )

    with pytest.raises(RuntimeError, match="host activation revoked"):
        coordinator.submit_once(
            _admission(), lease_owner_id=OWNER_A, lease_seconds=30
        )

    assert ledger.state == "pending"
    assert "lease_initial" not in ledger.transactions
    assert ledger.dispatch_consumed is False
    assert ledger.post_calls == []


def test_authority_invalidated_after_fence_is_zero_post_and_reconciliation_only():
    """PT-C3: drift after the committed fence still means zero POST."""

    ledger = _Ledger()
    broker = _Broker(ledger, posts=[])

    class _InvalidateAfterDispatchConsumeStore(_Store):
        def consume_dispatch_authority(self, *args, **kwargs):
            authority = super().consume_dispatch_authority(*args, **kwargs)
            # The one-shot dispatch event has committed.  In the real store a
            # separate DB transaction now revokes reservation/admission before
            # the live invocation context can acquire its serialization locks.
            self.ledger.reservation_authority_current = False
            return authority

    coordinator = transport.CapturedPaperTransportCoordinator(
        store=_InvalidateAfterDispatchConsumeStore(ledger),
        broker_transport=broker,
        financial_breaker_issuer=_FinancialBreakerIssuer(
            lambda: NOW + timedelta(seconds=1)
        ),
        acceptance_recorder=_AcceptanceRecorder(ledger),
        fill_capture=None,
        assert_external_authority_current=lambda: None,
    )

    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert ledger.post_calls == []
    assert "start_transport" in ledger.transactions
    assert "authorize_transport_invocation" in ledger.transactions
    assert "record_financial_breaker_authority" in ledger.transactions
    assert "consume_dispatch_authority" in ledger.transactions
    assert "acquire_dispatch_linearization" in ledger.transactions
    assert ledger.dispatch_consumed is True
    assert "mark_transport_indeterminate" in ledger.transactions


def test_financial_breaker_denied_after_fence_is_zero_post_and_reconciliation_only(
    monkeypatch,
):
    """A fresh kill-switch denial cannot inherit an earlier healthy admission."""

    ledger = _Ledger()
    holder = {"now": NOW + timedelta(seconds=1)}
    issuer = _FinancialBreakerIssuer(
        lambda: holder["now"],
        allowed=False,
    )
    exact, _adapter, client, runtime_clock, _receipt = _install_exact_transport(
        monkeypatch,
        now=holder["now"],
    )
    runtime_clock["now"] = holder["now"]
    coordinator = _coordinator(
        ledger,
        exact,
        financial_issuer=issuer,
    )

    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert client.submit_calls == []
    assert issuer.calls == [(_request().completion_sha256, "pre_post")]
    assert "start_transport" in ledger.transactions
    assert "authorize_transport_invocation" in ledger.transactions
    assert "mark_transport_indeterminate" in ledger.transactions


def test_kill_switch_activating_during_broker_refresh_is_zero_post():
    """The last breaker evaluation happens after broker/account preparation."""

    ledger = _Ledger()
    issuer = _FinancialBreakerIssuer(lambda: NOW + timedelta(seconds=1))

    class _BreakerFlipsDuringPreparation(_Broker):
        def prepare_limit_buy(self, *args, **kwargs):
            evidence = super().prepare_limit_buy(*args, **kwargs)
            issuer.allowed = False
            return evidence

    broker = _BreakerFlipsDuringPreparation(ledger, posts=[])
    outcome = _coordinator(
        ledger,
        broker,
        financial_issuer=issuer,
    ).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert issuer.calls == [(_request().completion_sha256, "pre_post")]
    assert ledger.post_calls == []
    assert ledger.dispatch_consumed is False
    assert "record_financial_breaker_authority" in ledger.transactions
    assert "consume_dispatch_authority" not in ledger.transactions


@pytest.mark.parametrize("failure", ["unavailable", "mismatched"])
def test_financial_breaker_unavailable_or_mismatched_after_fence_never_posts(
    monkeypatch,
    failure,
):
    ledger = _Ledger()
    holder = {"now": NOW + timedelta(seconds=1)}
    if failure == "unavailable":
        issuer = _FinancialBreakerIssuer(
            lambda: holder["now"],
            raises=financial_breaker.CapturedPaperFinancialBreakerError(
                "financial_breaker_truth_unavailable"
            ),
        )
    else:
        issuer = _FinancialBreakerIssuer(
            lambda: holder["now"],
            mutate=lambda receipt: replace(receipt, session_id=999),
        )
    exact, _adapter, client, runtime_clock, _receipt = _install_exact_transport(
        monkeypatch,
        now=holder["now"],
    )
    runtime_clock["now"] = holder["now"]

    outcome = _coordinator(
        ledger,
        exact,
        financial_issuer=issuer,
    ).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert client.submit_calls == []


def test_financial_breaker_receipt_expiring_during_account_refresh_never_posts(
    monkeypatch,
):
    ledger = _Ledger()
    exact, _adapter, client, holder, _receipt = _install_exact_transport(
        monkeypatch,
        now=NOW + timedelta(seconds=1),
    )
    original_receipt = exact._verify_fresh_connection_receipt_before_io

    def _slow_account_refresh(*, operation):
        result = original_receipt(operation=operation)
        holder["now"] = NOW + timedelta(seconds=7)
        return result

    exact._verify_fresh_connection_receipt_before_io = _slow_account_refresh

    outcome = _coordinator(ledger, exact).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert client.submit_calls == []


def test_financial_breaker_age_four_then_dispatch_two_seconds_later_never_posts(
    monkeypatch,
):
    """Breaker TTL starts at observation time, not receipt issuance time."""

    ledger = _Ledger()
    runtime = {"now": NOW + timedelta(seconds=4)}
    issuer = _FinancialBreakerIssuer(
        lambda: runtime["now"],
        checked_clock=lambda: NOW,
    )
    exact, _adapter, client, exact_clock, _receipt = _install_exact_transport(
        monkeypatch,
        now=runtime["now"],
    )
    exact_clock["now"] = runtime["now"]

    class _DelayAfterFinalFenceStore(_Store):
        def consume_dispatch_authority(self, *args, **kwargs):
            authority = super().consume_dispatch_authority(*args, **kwargs)
            runtime["now"] = NOW + timedelta(seconds=6)
            exact_clock["now"] = runtime["now"]
            return authority

    coordinator = transport.CapturedPaperTransportCoordinator(
        store=_DelayAfterFinalFenceStore(ledger),
        broker_transport=exact,
        financial_breaker_issuer=issuer,
        acceptance_recorder=_AcceptanceRecorder(ledger),
        fill_capture=None,
        assert_external_authority_current=lambda: None,
    )
    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert ledger.dispatch_consumed is True
    assert client.submit_calls == []


def test_generation_drift_at_post_preflight_never_calls_adapter_or_reposts():
    ledger = _Ledger()
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )

    class _Adapter:
        bound_account_id = ACCOUNT_ID

        def __init__(self):
            self.calls = 0

        def place_limit_order_gtc(self, **kwargs):
            self.calls += 1
            return _direct_response(kwargs)

    adapter = _Adapter()
    exact = object.__new__(transport.ExactAlpacaPaperEntryTransport)
    exact._adapter = adapter
    exact._expected_account_id = instruction.expected_account_id
    exact._connection_generation = "alpaca-paper-rest-generation-1"
    exact._clock = lambda: NOW + timedelta(seconds=2)
    preflight_calls = 0

    def _drifting_preflight(supplied):
        nonlocal preflight_calls
        assert supplied.instruction_sha256 == instruction.instruction_sha256
        preflight_calls += 1
        if preflight_calls == 2:
            raise transport.CapturedPaperTransportContractError(
                "broker_connection_generation_drift"
            )

    exact.preflight = _drifting_preflight
    coordinator = _coordinator(ledger, exact)

    first = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )
    restarted = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_B, lease_seconds=30
    )

    assert first.status == "transport_indeterminate"
    assert restarted.status == "no_work"
    assert ledger.state == "transport_indeterminate"
    assert preflight_calls == 3
    assert adapter.calls == 0


def test_exact_transport_preflight_is_local_and_does_not_mint_receipt(
    monkeypatch,
):
    exact, _adapter, _client, _holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )
    monkeypatch.setattr(
        alpaca_spot,
        "_trading_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("preflight must not acquire a broker client")
        ),
    )

    exact.preflight(instruction)


def test_exact_transport_uses_fresh_invocation_authority_for_one_post(
    monkeypatch,
):
    ledger = _Ledger()
    exact, _adapter, client, holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    holder["now"] = NOW + timedelta(seconds=2)
    outcome = _coordinator(ledger, exact).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "accepted"
    assert ledger.state == "completed"
    assert len(client.submit_calls) == 1
    assert client.submit_calls[0].client_order_id == "chili_ml_ACTU_41_1"
    assert "authorize_transport_invocation" in ledger.transactions


def test_host_revocation_after_dispatch_consume_is_zero_post_same_cid_only(
    monkeypatch,
):
    ledger = _Ledger()
    current = {"value": True}

    def _assert_current():
        if not current["value"]:
            raise RuntimeError("host activation revoked")

    exact, _adapter, client, holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    @contextmanager
    def _acquire_dispatch_authority():
        _assert_current()
        yield

    exact._acquire_external_dispatch_authority = (
        _acquire_dispatch_authority
    )
    holder["now"] = NOW + timedelta(seconds=2)

    class _RevokeAfterDispatchConsumeStore(_Store):
        def consume_dispatch_authority(self, *args, **kwargs):
            authority = super().consume_dispatch_authority(*args, **kwargs)
            current["value"] = False
            return authority

    coordinator = transport.CapturedPaperTransportCoordinator(
        store=_RevokeAfterDispatchConsumeStore(ledger),
        broker_transport=exact,
        financial_breaker_issuer=_FinancialBreakerIssuer(lambda: NOW),
        acceptance_recorder=_AcceptanceRecorder(ledger),
        fill_capture=None,
        assert_external_authority_current=_assert_current,
    )

    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert ledger.dispatch_consumed is True
    assert client.submit_calls == []
    assert ledger.post_calls == []


def test_dispatch_database_connection_loss_before_invocation_is_zero_post(
    monkeypatch,
):
    ledger = _Ledger()
    exact, _adapter, client, holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    holder["now"] = NOW + timedelta(seconds=2)

    class _ConnectionLostAfterConsumeStore(_Store):
        @contextmanager
        def acquire_dispatch_linearization(self, *args, **kwargs):
            with self.ledger.transaction("acquire_dispatch_linearization"):
                assert self.ledger.dispatch_consumed is True
                raise ConnectionError("database connection lost")
            yield  # pragma: no cover - makes this a context manager

    outcome = transport.CapturedPaperTransportCoordinator(
        store=_ConnectionLostAfterConsumeStore(ledger),
        broker_transport=exact,
        financial_breaker_issuer=_FinancialBreakerIssuer(lambda: NOW),
        acceptance_recorder=_AcceptanceRecorder(ledger),
        fill_capture=None,
        assert_external_authority_current=lambda: None,
    ).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert ledger.dispatch_consumed is True
    assert client.submit_calls == []
    assert ledger.post_calls == []


def test_dispatch_authority_expiring_while_waiting_for_host_lock_is_zero_post(
    monkeypatch,
):
    ledger = _Ledger()
    exact, _adapter, client, holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    holder["now"] = NOW + timedelta(seconds=2)

    @contextmanager
    def _delayed_dispatch_lock():
        holder["now"] = NOW + timedelta(minutes=6)
        yield

    exact._acquire_external_dispatch_authority = _delayed_dispatch_lock
    outcome = _coordinator(ledger, exact).submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert ledger.dispatch_consumed is True
    assert client.submit_calls == []
    assert ledger.post_calls == []


def test_structurally_forged_dispatch_authority_cannot_reach_exact_post(
    monkeypatch,
):
    """A duck-typed/nonpersisting store cannot self-attest final dispatch."""

    ledger = _Ledger()
    exact, _adapter, client, holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    holder["now"] = NOW + timedelta(seconds=2)

    class _UnattestedStore(_Store):
        def consume_dispatch_authority(self, *args, **kwargs):
            genuine = super().consume_dispatch_authority(*args, **kwargs)
            self.ledger.dispatch_consumed = False
            return replace(genuine, process_attestation_hmac_sha256="")

    coordinator = transport.CapturedPaperTransportCoordinator(
        store=_UnattestedStore(ledger),
        broker_transport=exact,
        financial_breaker_issuer=_FinancialBreakerIssuer(lambda: NOW),
        acceptance_recorder=_AcceptanceRecorder(ledger),
        fill_capture=None,
        assert_external_authority_current=lambda: None,
    )
    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert ledger.post_calls == []
    assert client.submit_calls == []


def test_exact_dispatch_process_attestation_is_one_shot(monkeypatch):
    ledger = _Ledger()
    store = _Store(ledger)
    exact, _adapter, client, holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    holder["now"] = NOW + timedelta(seconds=2)
    instruction = store.verify_committed_instruction(
        transport.CapturedPaperTransportInstruction.from_admission(
            _admission()
        )
    )
    lease = store.lease_initial(
        instruction,
        lease_owner_id=OWNER_A,
        lease_seconds=30,
    )
    start = store.start_transport(instruction, lease)
    invocation = store.authorize_transport_invocation(instruction, start)
    pre_dispatch = exact.prepare_limit_buy(
        instruction,
        invocation_authority=invocation,
    )
    receipt = _FinancialBreakerIssuer(
        lambda: holder["now"]
    ).issue_for_request(
        instruction.request,
        phase="pre_post",
        transport_instruction_sha256=instruction.instruction_sha256,
        transport_invocation_authority_sha256=(
            invocation.invocation_authority_sha256
        ),
        authority_valid_until=invocation.valid_until,
    )
    store.record_financial_breaker_authority(
        instruction,
        start,
        invocation,
        receipt,
    )
    dispatch = store.consume_dispatch_authority(
        instruction,
        start,
        invocation,
        receipt,
        pre_dispatch,
    )

    first = exact.post_limit_buy(
        instruction,
        invocation_authority=invocation,
        financial_breaker_receipt=receipt,
        pre_dispatch_evidence=pre_dispatch,
        dispatch_authority=dispatch,
    )
    assert type(first) is transport.CapturedPaperPositiveOrderObservation
    with pytest.raises(
        outbox_contract.CapturedPaperOutboxError,
        match="transport_dispatch_process_attestation_not_registered",
    ):
        exact.post_limit_buy(
            instruction,
            invocation_authority=invocation,
            financial_breaker_receipt=receipt,
            pre_dispatch_evidence=pre_dispatch,
            dispatch_authority=dispatch,
        )
    assert len(client.submit_calls) == 1


def test_invocation_authority_expiry_before_dispatch_is_zero_post(
    monkeypatch,
):
    ledger = _Ledger()
    exact, _adapter, client, holder, _receipt = _install_exact_transport(
        monkeypatch
    )

    class _ExpiresBeforeDispatchStore(_Store):
        def authorize_transport_invocation(self, instruction, start):
            receipt = super().authorize_transport_invocation(
                instruction, start
            )
            receipt = replace(
                receipt,
                valid_until=NOW + timedelta(seconds=1, milliseconds=500),
                invocation_authority_sha256="",
            )
            holder["now"] = NOW + timedelta(seconds=2)
            return receipt

    coordinator = transport.CapturedPaperTransportCoordinator(
        store=_ExpiresBeforeDispatchStore(ledger),
        broker_transport=exact,
        financial_breaker_issuer=_FinancialBreakerIssuer(
            lambda: NOW + timedelta(seconds=1)
        ),
        acceptance_recorder=_AcceptanceRecorder(ledger),
        fill_capture=None,
        assert_external_authority_current=lambda: None,
    )
    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert client.submit_calls == []


def test_real_connection_generation_drift_after_marker_never_posts_or_reposts(
    monkeypatch,
):
    ledger = _Ledger()
    exact, _adapter, original, holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    replacement = _drift_exact_client(monkeypatch, holder)
    coordinator = _coordinator(ledger, exact)

    first = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )
    restarted = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_B, lease_seconds=30
    )

    assert first.status == "transport_indeterminate"
    assert restarted.status == "no_work"
    assert ledger.state == "transport_indeterminate"
    assert "start_transport" in ledger.transactions
    assert original.submit_calls == []
    assert replacement.submit_calls == []


def test_real_connection_generation_drift_keeps_same_cid_lookup_pending(
    monkeypatch,
):
    ledger = _Ledger()
    exact, _adapter, original, holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    replacement = _drift_exact_client(monkeypatch, holder)
    coordinator = _coordinator(ledger, exact)
    ledger.durable_instruction = (
        transport.CapturedPaperTransportInstruction.from_admission(
            _admission()
        )
    )
    ledger.state = "transport_indeterminate"

    outcome = coordinator.reconcile_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "reconciliation_pending"
    assert ledger.state == "transport_indeterminate"
    assert original.lookup_calls == []
    assert replacement.lookup_calls == []


def test_stale_exact_generation_receipt_after_marker_never_posts(
    monkeypatch,
):
    ledger = _Ledger()
    exact, _adapter, client, holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    # The observation clock remains current while the exact adapter receipt is
    # minted outside its five-second authority window.
    holder["now"] = NOW
    monkeypatch.setattr(
        alpaca_spot, "_now", lambda: NOW - timedelta(seconds=6)
    )
    coordinator = _coordinator(ledger, exact)

    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert client.submit_calls == []


def test_noncanonical_or_bad_hash_generation_receipt_never_posts(
    monkeypatch,
):
    ledger = _Ledger()
    exact, _adapter, client, _holder, _receipt = _install_exact_transport(
        monkeypatch
    )
    exact_canonical_evidence = alpaca_spot._canonical_evidence

    def _bad_hash(value):
        canonical, _digest = exact_canonical_evidence(value)
        return canonical, "0" * 64

    monkeypatch.setattr(alpaca_spot, "_canonical_evidence", _bad_hash)
    coordinator = _coordinator(ledger, exact)

    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert client.submit_calls == []


def test_post_marker_instance_method_shadow_is_rejected_before_io(
    monkeypatch,
):
    ledger = _Ledger()
    exact, adapter, client, _holder, _receipt = _install_exact_transport(
        monkeypatch
    )

    class _ShadowAfterMarkerStore(_Store):
        def start_transport(self, instruction, lease):
            start = super().start_transport(instruction, lease)
            adapter.place_limit_order_gtc = lambda **_kwargs: (_ for _ in ()).throw(
                AssertionError("mutable instance dispatch reached")
            )
            return start

    coordinator = transport.CapturedPaperTransportCoordinator(
        store=_ShadowAfterMarkerStore(ledger),
        broker_transport=exact,
        financial_breaker_issuer=_FinancialBreakerIssuer(
            lambda: NOW + timedelta(seconds=1)
        ),
        acceptance_recorder=_AcceptanceRecorder(ledger),
        fill_capture=None,
        assert_external_authority_current=lambda: None,
    )

    outcome = coordinator.submit_once(
        _admission(), lease_owner_id=OWNER_A, lease_seconds=30
    )

    assert outcome.status == "transport_indeterminate"
    assert ledger.state == "transport_indeterminate"
    assert client.submit_calls == []


def test_connection_receipt_sha_is_cryptographically_bound_to_order_evidence(
    monkeypatch,
):
    exact, _adapter, _client, _holder, receipt = _install_exact_transport(
        monkeypatch
    )
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )
    raw = _direct_response(instruction.adapter_kwargs())

    first = exact._interpret_direct_post_result(
        instruction,
        raw=raw,
        requested_at=NOW,
        available_at=NOW,
        connection_receipt_sha256=receipt["receipt_sha256"],
    )
    second = exact._interpret_direct_post_result(
        instruction,
        raw=raw,
        requested_at=NOW,
        available_at=NOW,
        connection_receipt_sha256="f" * 64,
    )

    assert type(first) is transport.CapturedPaperPositiveOrderObservation
    assert type(second) is transport.CapturedPaperPositiveOrderObservation
    assert first.broker_order_evidence_sha256 != (
        second.broker_order_evidence_sha256
    )


def test_extended_day_and_regular_day_or_gtc_preserve_exact_admission_policy():
    for extended_hours, time_in_force in (
        (True, "day"),
        (False, "day"),
        (False, "gtc"),
    ):
        ledger = _Ledger()
        broker = _Broker(
            ledger,
            posts=[lambda instruction: _positive(instruction)],
        )
        outcome = _coordinator(ledger, broker).submit_once(
            _admission(
                extended_hours=extended_hours,
                time_in_force=time_in_force,
            ),
            lease_owner_id=OWNER_A,
            lease_seconds=30,
        )
        assert outcome.status == "accepted"
        assert ledger.post_calls == ["chili_ml_ACTU_41_1"]


def test_extended_gtc_is_rejected_before_lease_or_post():
    ledger = _Ledger()
    broker = _Broker(ledger)
    with pytest.raises(
        transport.CapturedPaperTransportContractError,
        match="transport_instruction_order_binding_mismatch",
    ):
        _coordinator(ledger, broker).submit_once(
            _admission(extended_hours=True, time_in_force="gtc"),
            lease_owner_id=OWNER_A,
            lease_seconds=30,
        )
    assert ledger.transactions == []
    assert ledger.post_calls == []


@pytest.mark.parametrize(
    ("entry_limit_price", "structural_stop_price"),
    (("3.001", "2.5"), ("0.12345", "0.1")),
)
def test_noncanonical_equity_tick_is_rejected_before_lease_or_post(
    entry_limit_price, structural_stop_price
):
    ledger = _Ledger()
    broker = _Broker(ledger)
    with pytest.raises(
        RuntimeError,
        match="captured_paper_entry_limit_exceeds_frozen_ceiling",
    ):
        _coordinator(ledger, broker).submit_once(
            _admission(
                entry_limit_price=entry_limit_price,
                structural_stop_price=structural_stop_price,
            ),
            lease_owner_id=OWNER_A,
            lease_seconds=30,
        )
    assert ledger.transactions == []
    assert ledger.post_calls == []


def test_instruction_rejects_mutated_request_hash_and_has_no_magic_risk_caps():
    admission = _admission()
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        admission
    )
    assert instruction.quantity_shares == 4578
    assert instruction.authority.broker_request_sha256 == (
        admission.order_request_sha256
    )
    mutated = dict(admission.order_request)
    mutated["qty"] = "1"
    with pytest.raises(
        RuntimeError, match="captured_paper_order_request_binding_mismatch"
    ):
        replace(admission, order_request=mutated)

    source = inspect.getsource(transport)
    assert "datetime.now(" not in source
    assert ".commit(" not in source
    assert "get_order_by_client_order_id_truth" in source
    assert "place_limit_order_gtc" in source
    assert "self._adapter.place_limit_order_gtc(" not in source
    assert "self._adapter.get_order_by_client_order_id_truth(" not in source
    assert "_EXACT_ALPACA_ENTRY_POST_METHOD(" in source
    assert "_EXACT_ALPACA_CID_LOOKUP_METHOD(" in source
    assert "_EXACT_ALPACA_CONNECTION_RECEIPT_METHOD(" in source
    assert "position_intent" in source
    assert "buy_to_open" in source
    assert "sell_to_open" not in source
    assert "daily_loss" not in source
    assert "one_symbol" not in source


@pytest.mark.parametrize(
    "response_patch",
    [
        {"position_intent": None},
        {"position_intent": "sell_to_open"},
        {
            "position_intent": "buy_to_open",
            "position_intent_echo": "sell_to_open",
        },
    ],
)
def test_direct_positive_response_requires_buy_to_open_binding(response_patch):
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )

    class _Adapter:
        bound_account_id = ACCOUNT_ID

        def place_limit_order_gtc(self, **kwargs):
            response = _direct_response(kwargs)
            response.update(response_patch)
            return response

    exact = object.__new__(transport.ExactAlpacaPaperEntryTransport)
    exact._adapter = _Adapter()
    exact._expected_account_id = instruction.expected_account_id
    exact._connection_generation = "alpaca-paper-rest-generation-1"
    exact._clock = lambda: NOW + timedelta(seconds=2)
    exact.preflight = lambda supplied: None

    result = _interpret_direct_response(exact, instruction)

    assert type(result) is transport.CapturedPaperUnresolvedObservation
    assert result.reason == "transport_rejected_or_ambiguous"


def test_direct_positive_response_allows_omitted_broker_intent_echo():
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )

    class _Adapter:
        bound_account_id = ACCOUNT_ID

        def place_limit_order_gtc(self, **kwargs):
            return _direct_response(kwargs)

    exact = object.__new__(transport.ExactAlpacaPaperEntryTransport)
    exact._adapter = _Adapter()
    exact._expected_account_id = instruction.expected_account_id
    exact._connection_generation = "alpaca-paper-rest-generation-1"
    exact._clock = lambda: NOW + timedelta(seconds=2)
    exact.preflight = lambda supplied: None

    result = _interpret_direct_response(exact, instruction)

    assert type(result) is transport.CapturedPaperPositiveOrderObservation
    assert result.client_order_id == instruction.client_order_id


@pytest.mark.parametrize(
    ("account_echo", "expected_type"),
    [
        (None, transport.CapturedPaperPositiveOrderObservation),
        (
            "e27af7c8-a441-46af-a2de-94e0b0d2bd7c",
            transport.CapturedPaperUnresolvedObservation,
        ),
    ],
)
def test_order_account_echo_is_optional_but_wrong_present_echo_rejects(
    account_echo, expected_type
):
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )

    class _Adapter:
        bound_account_id = ACCOUNT_ID

        def place_limit_order_gtc(self, **kwargs):
            return _direct_response(
                kwargs,
                broker_account_id_echo=account_echo,
            )

    exact = object.__new__(transport.ExactAlpacaPaperEntryTransport)
    exact._adapter = _Adapter()
    exact._expected_account_id = instruction.expected_account_id
    exact._connection_generation = "alpaca-paper-rest-generation-1"
    exact._clock = lambda: NOW + timedelta(seconds=2)
    exact.preflight = lambda supplied: None

    result = _interpret_direct_response(exact, instruction)
    assert type(result) is expected_type


def test_positive_acceptance_marker_uses_typed_proof_account_and_order():
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )
    observation = _positive(instruction)
    proof = CapturedPaperBrokerAcceptanceProof(
        acceptance_kind="post_response",
        completion_sha256=instruction.request.completion_sha256,
        account_scope=instruction.account_scope,
        expected_account_id=instruction.expected_account_id,
        client_order_id=instruction.client_order_id,
        broker_order_id=observation.broker_order_id,
        reservation_id=instruction.authority.reservation_id,
        action_claim_token=instruction.authority.action_claim_token,
        binder_id=instruction.authority.binder_id,
        broker_order_evidence_sha256=(
            observation.broker_order_evidence_sha256
        ),
        observed_at=observation.observed_at,
        available_at=observation.available_at,
    )

    marker = positive_acceptance._acceptance_marker(
        instruction,
        observation,
        proof,
    )

    assert marker["broker_order_id"] == proof.broker_order_id
    assert marker["verified_adapter_account_id"] == ACCOUNT_ID
    assert marker["account_binding_source"] == (
        transport.EXACT_PAPER_ACCOUNT_BINDING_SOURCE
    )


@pytest.mark.parametrize(
    "response_patch",
        [
            {"broker_order_status_echo": None},
            {"broker_symbol_echo": "OTHER"},
        {"broker_side_echo": "sell"},
        {"broker_order_type_echo": "market"},
        {"broker_quantity_echo": "4577"},
        {"broker_limit_price_echo": None},
        {"broker_time_in_force_echo": None},
        {"broker_extended_hours_echo": None},
        {"broker_asset_class_echo": None},
        {
            "broker_filled_quantity_echo": None,
            "broker_cumulative_filled_quantity": None,
        },
    ],
)
def test_direct_response_requires_exact_nonterminal_zero_fill(response_patch):
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )

    class _Adapter:
        bound_account_id = ACCOUNT_ID

        def place_limit_order_gtc(self, **kwargs):
            response = _direct_response(kwargs)
            response.update(response_patch)
            return response

    exact = object.__new__(transport.ExactAlpacaPaperEntryTransport)
    exact._adapter = _Adapter()
    exact._expected_account_id = instruction.expected_account_id
    exact._connection_generation = "alpaca-paper-rest-generation-1"
    exact._clock = lambda: NOW + timedelta(seconds=2)
    exact.preflight = lambda supplied: None

    result = _interpret_direct_response(exact, instruction)

    assert type(result) is transport.CapturedPaperUnresolvedObservation
    assert result.reason == "transport_rejected_or_ambiguous"


@pytest.mark.parametrize(
    "response_patch",
    [
        {"broker_order_status_echo": "filled"},
        {"broker_order_status_echo": "partially_filled"},
        {
            "broker_filled_quantity_echo": "1",
            "broker_cumulative_filled_quantity": 1,
        },
        {
            "broker_filled_quantity_echo": "0.5",
            "broker_cumulative_filled_quantity": None,
        },
    ],
)
def test_direct_fill_bearing_echo_is_typed_for_fill_reconciliation(response_patch):
    instruction = transport.CapturedPaperTransportInstruction.from_admission(
        _admission()
    )

    class _Adapter:
        bound_account_id = ACCOUNT_ID

        def place_limit_order_gtc(self, **kwargs):
            response = _direct_response(kwargs)
            response.update(response_patch)
            return response

    exact = object.__new__(transport.ExactAlpacaPaperEntryTransport)
    exact._adapter = _Adapter()
    exact._expected_account_id = instruction.expected_account_id
    exact._connection_generation = "alpaca-paper-rest-generation-1"
    exact._clock = lambda: NOW + timedelta(seconds=2)
    exact.preflight = lambda supplied: None

    result = _interpret_direct_response(exact, instruction)

    assert type(result) is (
        transport.CapturedPaperFillReconciliationRequiredObservation
    )
    assert result.broker_order_id == "alpaca-order-1"
