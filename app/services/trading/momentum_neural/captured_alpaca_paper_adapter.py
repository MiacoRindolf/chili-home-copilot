"""Capture-native, paper-only Alpaca decision reads.

This adapter is deliberately inert until a caller supplies both an already
account-bound Alpaca PAPER adapter and a running live-capture coordinator whose
local producer owns ``ALPACA_NBBO_QUOTE`` and ``ACCOUNT_RISK_SNAPSHOT``.  It has
no database/provider fallback: every decision quote comes from the wrapped
adapter's direct, exact-timestamp Alpaca execution quote and is durably receipted
before it is returned to the FSM.

The wrapper does not activate a runner and does not weaken the wrapped adapter's
order lifecycle.  Unknown public methods are delegated only after rechecking the
static paper/account binding.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import hmac
import math
import secrets
import threading
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping
import uuid

from ..venue.protocol import FreshnessMeta, NormalizedProduct, NormalizedTicker
from .alpaca_paper_identity import (
    ALPACA_PAPER_ACCOUNT_SCOPE,
    alpaca_paper_account_identity_payload,
    canonical_alpaca_paper_account_id,
)
from .alpaca_paper_account_receipt import (
    ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION,
    ALPACA_PAPER_ACCOUNT_PROVIDER,
    ALPACA_PAPER_ACCOUNT_READY_STATUS,
    alpaca_paper_account_capture_query,
)
from .live_replay_capture import (
    CaptureSessionState,
    CapturedReadResult,
    ObservedCaptureInput,
)
from .replay_capture_contract import (
    ALPACA_NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
    ALPACA_NBBO_QUOTE_QUERY_SCHEMA_VERSION,
    CaptureClocks,
    CaptureContractError,
    CaptureEvent,
    CaptureEventRef,
    CaptureStream,
    ActiveCaptureInputPrefixAttestation,
    captured_read_result_sha256,
    sha256_json,
    verify_active_capture_input_attestation,
)


UTC = timezone.utc
_ALPACA_NBBO_PROVIDER = "alpaca_market_data_paper"
_FUTURE_TOLERANCE_SECONDS = 1.0
_CAPTURED_ACCOUNT_AUTHORITY_TOKEN = object()
_CAPTURED_ACCOUNT_AUTHORITY_KEY = secrets.token_bytes(32)


class CapturedAlpacaPaperReadError(CaptureContractError):
    """A captured PAPER read could not prove its exact boundary."""


class CapturedPaperDecisionIdentityChanged(CapturedAlpacaPaperReadError):
    """A decision's pinned evidence expired and was replaced.

    The replacement is captured before this exception is raised.  The caller
    must defer the current decision and begin a new decision scope so adaptive
    sizing and final placement can never use different evidence identities.
    """

    def __init__(self, *, kind: str, previous: str, current: str) -> None:
        self.kind = str(kind)
        self.previous = str(previous)
        self.current = str(current)
        super().__init__(f"captured paper {self.kind} identity changed in decision")


@dataclass(frozen=True)
class CapturedAlpacaPaperAccountAuthority:
    """Opaque process-local authority for one exact captured account read.

    Ordinary account dictionaries, public content hashes and reconstructed
    dataclasses cannot mint this capability.  The capture wrapper issues it
    only after the active input-prefix attestation proves the exact durable
    account receipt and source payload used by the decision.
    """

    account_id: str
    account_identity_sha256: str
    decision_id: str
    run_id: str
    generation: int
    expires_at: datetime
    active_input_attestation_sha256: str
    account_payload_sha256: str
    account_read_id: str
    account_read_receipt_sha256: str
    account_source_event_sha256: str
    snapshot_id: str
    source: str
    provider_generation: str
    observed_at: datetime
    available_at: datetime
    equity_usd: Decimal
    buying_power_usd: Decimal
    broker_day_change_usd: Decimal
    _verification_tag: str = field(repr=False)
    _verification_token: object = field(repr=False, compare=False)

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": "chili.captured-alpaca-paper-account-authority.v1",
            "account_id": self.account_id,
            "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
            "account_identity_sha256": self.account_identity_sha256,
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "generation": self.generation,
            "expires_at": _iso(self.expires_at),
            "active_input_attestation_sha256": (
                self.active_input_attestation_sha256
            ),
            "account_payload_sha256": self.account_payload_sha256,
            "account_read_id": self.account_read_id,
            "account_read_receipt_sha256": self.account_read_receipt_sha256,
            "account_source_event_sha256": self.account_source_event_sha256,
            "snapshot_id": self.snapshot_id,
            "source": self.source,
            "provider_generation": self.provider_generation,
            "observed_at": _iso(self.observed_at),
            "available_at": _iso(self.available_at),
            "equity_usd": str(self.equity_usd),
            "buying_power_usd": str(self.buying_power_usd),
            "broker_day_change_usd": str(self.broker_day_change_usd),
        }

    @property
    def authority_sha256(self) -> str:
        return self._verification_tag

    def __post_init__(self) -> None:
        verify_captured_alpaca_paper_account_authority(self)

    def __reduce__(self):
        raise TypeError("captured Alpaca PAPER account authority cannot be pickled")


def _captured_account_authority_tag(payload: Mapping[str, Any]) -> str:
    return hmac.new(
        _CAPTURED_ACCOUNT_AUTHORITY_KEY,
        sha256_json(dict(payload)).encode("ascii"),
        hashlib.sha256,
    ).hexdigest()


def verify_captured_alpaca_paper_account_authority(
    value: CapturedAlpacaPaperAccountAuthority,
) -> CapturedAlpacaPaperAccountAuthority:
    """Re-verify the private token and every canonical authority field."""

    if type(value) is not CapturedAlpacaPaperAccountAuthority:
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority is malformed"
        )
    if value._verification_token is not _CAPTURED_ACCOUNT_AUTHORITY_TOKEN:
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority token is invalid"
        )
    try:
        account_id = canonical_alpaca_paper_account_id(value.account_id)
        run_id = str(uuid.UUID(value.run_id))
        read_id = str(uuid.UUID(value.account_read_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority identity is invalid"
        ) from exc
    if account_id != value.account_id or run_id != value.run_id or read_id != value.account_read_id:
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority identity is noncanonical"
        )
    if (
        not value.decision_id
        or isinstance(value.generation, bool)
        or int(value.generation) <= 0
        or not value.snapshot_id
        or value.source != ALPACA_PAPER_ACCOUNT_PROVIDER
        or not value.provider_generation
    ):
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority is incomplete"
        )
    for name in (
        "account_identity_sha256",
        "active_input_attestation_sha256",
        "account_payload_sha256",
        "account_read_receipt_sha256",
        "account_source_event_sha256",
    ):
        raw = str(getattr(value, name) or "")
        if len(raw) != 64 or any(ch not in "0123456789abcdef" for ch in raw):
            raise CapturedAlpacaPaperReadError(
                f"captured Alpaca PAPER account authority {name} is invalid"
            )
    expected_identity = sha256_json(
        dict(alpaca_paper_account_identity_payload(account_id))
    )
    if value.account_identity_sha256 != expected_identity:
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority UUID binding changed"
        )
    observed = _utc(value.observed_at, "account authority observed_at")
    available = _utc(value.available_at, "account authority available_at")
    expires = _utc(value.expires_at, "account authority expires_at")
    if observed != value.observed_at or available != value.available_at or expires != value.expires_at:
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority clocks are noncanonical"
        )
    if available < observed or expires < available:
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority clocks are inconsistent"
        )
    for name in ("equity_usd", "buying_power_usd", "broker_day_change_usd"):
        value_decimal = getattr(value, name)
        if type(value_decimal) is not Decimal or not value_decimal.is_finite():
            raise CapturedAlpacaPaperReadError(
                f"captured Alpaca PAPER account authority {name} is invalid"
            )
    if value.equity_usd <= 0 or value.buying_power_usd < 0:
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority economics are invalid"
        )
    expected_tag = _captured_account_authority_tag(value._body())
    if not hmac.compare_digest(value._verification_tag, expected_tag):
        raise CapturedAlpacaPaperReadError(
            "captured Alpaca PAPER account authority changed"
        )
    return value


@dataclass(frozen=True)
class CapturedPaperReadEvidence:
    decision_id: str
    stream: CaptureStream
    provider: str
    capture_identity_sha256: str
    capture_event_sha256: str
    capture_content_sha256: str
    capture_sequence: int
    capture_read_id: str
    capture_read_receipt_sha256: str
    capture_result_sha256: str
    requested_at: datetime
    provider_event_at: datetime | None
    received_at: datetime
    available_at: datetime


@dataclass(frozen=True)
class CapturedPaperQuoteEvidence:
    symbol: str
    generation: int
    bid: float
    ask: float
    mid: float
    spread_bps: float
    bid_size: float | None
    ask_size: float | None
    feed: str
    read: CapturedPaperReadEvidence


@dataclass(frozen=True)
class CapturedPaperAccountSnapshot:
    account_id: str
    account_scope: str
    paper: bool
    status: str
    equity: Decimal
    last_equity: Decimal
    buying_power: Decimal
    cash: Decimal | None
    account_blocked: bool | None
    trading_blocked: bool | None
    trade_suspended_by_user: bool | None
    read: CapturedPaperReadEvidence
    payload_sha256: str
    payload: Mapping[str, Any] = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))

    def to_adapter_snapshot(self) -> dict[str, Any]:
        """Drop-in account mapping plus immutable capture provenance."""

        return {
            "ok": True,
            "account_id": self.account_id,
            "equity": float(self.equity),
            "last_equity": float(self.last_equity),
            "buying_power": float(self.buying_power),
            "cash": None if self.cash is None else float(self.cash),
            "status": self.status,
            "account_blocked": self.account_blocked,
            "trading_blocked": self.trading_blocked,
            "trade_suspended_by_user": self.trade_suspended_by_user,
            "paper": True,
            "account_scope": self.account_scope,
            "capture_event_sha256": self.read.capture_event_sha256,
            "capture_content_sha256": self.read.capture_content_sha256,
            "capture_sequence": self.read.capture_sequence,
            "capture_read_id": self.read.capture_read_id,
            "capture_read_receipt_sha256": (
                self.read.capture_read_receipt_sha256
            ),
            "capture_identity_sha256": self.read.capture_identity_sha256,
            "received_at_utc": _iso(self.read.received_at),
            "available_at_utc": _iso(self.read.available_at),
        }


@dataclass
class _DecisionState:
    decision_id: str
    owner_thread_id: int
    capture_identity_sha256: str
    capture_generation: int
    account: CapturedPaperAccountSnapshot | None = None
    account_generation: int = 0
    quotes: dict[str, CapturedPaperQuoteEvidence] = field(default_factory=dict)
    captured_results: dict[
        tuple[CaptureStream, str | None], CapturedReadResult
    ] = field(default_factory=dict)
    captured_result_replaced: bool = False
    captured_results_consumed: bool = False
    bp_census_before: Any | None = None
    bp_census_after: Any | None = None
    product: NormalizedProduct | None = None
    product_freshness: FreshnessMeta | None = None


def _utc(value: datetime, name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CapturedAlpacaPaperReadError(f"{name} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "timestamp").isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, name: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, name)
    raw = str(value or "").strip()
    if not raw:
        raise CapturedAlpacaPaperReadError(f"{name} is missing")
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturedAlpacaPaperReadError(f"{name} is malformed") from exc
    return _utc(parsed, name)


def _positive_decimal(value: Any, name: str, *, allow_zero: bool = False) -> Decimal:
    if isinstance(value, bool):
        raise CapturedAlpacaPaperReadError(f"{name} is malformed")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CapturedAlpacaPaperReadError(f"{name} is malformed") from exc
    if not parsed.is_finite() or (parsed < 0 if allow_zero else parsed <= 0):
        raise CapturedAlpacaPaperReadError(f"{name} is outside its valid range")
    return parsed


def _finite_decimal(value: Any, name: str) -> Decimal:
    if isinstance(value, bool):
        raise CapturedAlpacaPaperReadError(f"{name} is malformed")
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise CapturedAlpacaPaperReadError(f"{name} is malformed") from exc
    if not parsed.is_finite():
        raise CapturedAlpacaPaperReadError(f"{name} is malformed")
    return parsed


def _required_false_broker_flag(value: Any, name: str) -> bool:
    if value is not False:
        raise CapturedAlpacaPaperReadError(
            f"Alpaca PAPER {name} must be the broker-native false value"
        )
    return False


class CapturedAlpacaPaperAdapter:
    """Paper-only adapter that returns only durable decision-time reads."""

    def __init__(
        self,
        *,
        adapter: Any,
        coordinator: Any,
        expected_account_id: str,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        quote_max_age_seconds: float = 2.0,
        account_max_age_seconds: float = 5.0,
    ) -> None:
        try:
            canonical_id = canonical_alpaca_paper_account_id(expected_account_id)
        except ValueError as exc:
            raise CapturedAlpacaPaperReadError(
                "expected Alpaca PAPER account UUID is malformed"
            ) from exc
        if not callable(wall_clock):
            raise CapturedAlpacaPaperReadError("captured paper wall clock is malformed")
        quote_age = float(quote_max_age_seconds)
        account_age = float(account_max_age_seconds)
        if (
            not math.isfinite(quote_age)
            or not math.isfinite(account_age)
            or quote_age <= 0
            or account_age <= 0
        ):
            raise CapturedAlpacaPaperReadError(
                "captured paper freshness bounds must be finite and positive"
            )
        self._adapter = adapter
        self._coordinator = coordinator
        self._expected_account_id = canonical_id
        self._wall_clock = wall_clock
        self._quote_max_age_seconds = quote_age
        self._account_max_age_seconds = account_age
        self._decision: ContextVar[_DecisionState | None] = ContextVar(
            f"captured_alpaca_paper_decision_{id(self)}", default=None
        )
        self._assert_static_binding()

    @property
    def bound_account_id(self) -> str:
        return self._expected_account_id

    @property
    def broker_environment(self) -> str:
        return "paper"

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    def _now(self) -> datetime:
        return _utc(self._wall_clock(), "captured paper wall clock")

    def _assert_coordinator_owned(self, stream: CaptureStream) -> None:
        owners = getattr(self._coordinator, "_owner_by_stream", None)
        local_owner = getattr(self._coordinator, "_coordinator_producer_id", None)
        if (
            not isinstance(owners, Mapping)
            or not str(local_owner or "").strip()
            or owners.get(stream) != local_owner
        ):
            raise CapturedAlpacaPaperReadError(
                f"running capture roster lacks coordinator-owned {stream.value}"
            )

    def _assert_static_binding(self) -> None:
        state = getattr(self._coordinator, "state", None)
        state_value = getattr(state, "value", state)
        if state is not CaptureSessionState.RUNNING and state_value != "running":
            raise CapturedAlpacaPaperReadError("capture coordinator is not running")
        identity = getattr(self._coordinator, "identity", None)
        if (
            str(getattr(identity, "broker", "") or "").strip().lower() != "alpaca"
            or str(getattr(identity, "broker_environment", "") or "")
            .strip()
            .lower()
            != "paper"
        ):
            raise CapturedAlpacaPaperReadError(
                "capture identity is not Alpaca PAPER"
            )
        account_identity = alpaca_paper_account_identity_payload(
            self._expected_account_id
        )
        if sha256_json(dict(account_identity)) != str(
            getattr(identity, "account_identity_sha256", "") or ""
        ):
            raise CapturedAlpacaPaperReadError(
                "capture account identity differs from expected Alpaca PAPER UUID"
            )
        if (
            str(getattr(self._adapter, "broker_environment", "") or "")
            .strip()
            .lower()
            != "paper"
            or str(getattr(self._adapter, "bound_account_id", "") or "")
            .strip()
            .lower()
            != self._expected_account_id
        ):
            raise CapturedAlpacaPaperReadError(
                "underlying adapter is not bound to the expected Alpaca PAPER UUID"
            )
        self._assert_coordinator_owned(CaptureStream.ALPACA_NBBO_QUOTE)
        self._assert_coordinator_owned(CaptureStream.ACCOUNT_RISK_SNAPSHOT)

    def _state(self) -> _DecisionState:
        state = self._decision.get()
        if state is None:
            raise CapturedAlpacaPaperReadError(
                "captured paper reads require an explicit decision scope"
            )
        return state

    @contextmanager
    def decision_scope(self, decision_id: str) -> Iterator["CapturedAlpacaPaperAdapter"]:
        normalized = str(decision_id or "").strip()
        if not normalized:
            raise CapturedAlpacaPaperReadError("captured paper decision_id is required")
        if self._decision.get() is not None:
            raise CapturedAlpacaPaperReadError(
                "captured paper decision scopes cannot be nested"
            )
        self._assert_static_binding()
        identity = self._coordinator.identity
        token = self._decision.set(
            _DecisionState(
                decision_id=normalized,
                owner_thread_id=threading.get_ident(),
                capture_identity_sha256=identity.identity_sha256,
                capture_generation=identity.generation,
            )
        )
        try:
            yield self
        finally:
            state = self._decision.get()
            if state is not None:
                # The exact CapturedReadResult objects are a process-private,
                # one-decision handoff.  Never retain them on the adapter after
                # normal or exceptional scope exit.
                state.captured_results.clear()
                state.captured_results_consumed = True
            self._decision.reset(token)

    def _retain_captured_result(
        self,
        state: _DecisionState,
        *,
        result: CapturedReadResult,
        stream: CaptureStream,
        symbol: str | None,
    ) -> None:
        """Retain the original typed object; never reconstruct provenance."""

        normalized_symbol = str(symbol or "").strip().upper() or None
        key = (stream, normalized_symbol)
        identity = self._coordinator.identity
        receipt = result.receipt
        if (
            threading.get_ident() != state.owner_thread_id
            or identity.identity_sha256 != state.capture_identity_sha256
            or identity.generation != state.capture_generation
            or receipt is None
            or receipt.decision_id != state.decision_id
            or receipt.stream is not stream
            or receipt.symbol != normalized_symbol
            or state.captured_results_consumed
        ):
            raise CapturedAlpacaPaperReadError(
                "captured paper read retention identity mismatch"
            )
        if key in state.captured_results:
            # Preserve the wrapper's established identity-change behavior for
            # an expired pin, but make the whole proof bundle unavailable.
            # A production decision must restart under a new scope rather than
            # silently choose one of two account/NBBO generations.
            state.captured_result_replaced = True
        state.captured_results[key] = result

    def consume_current_captured_reads(
        self,
        *,
        symbol: str,
    ) -> tuple[CapturedReadResult, CapturedReadResult]:
        """Return the exact account/NBBO results once in this decision scope.

        The tuple contains the original immutable objects returned by the
        coordinator, in account-then-NBBO order.  It is intentionally not a
        serializable material field and is cleared from adapter state before
        returning.  Later proof issuance still verifies the exact decision,
        run and generation, so a retained caller reference cannot authorize a
        different decision.
        """

        self._assert_static_binding()
        state = self._state()
        normalized_symbol = str(symbol or "").strip().upper()
        identity = self._coordinator.identity
        account_key = (CaptureStream.ACCOUNT_RISK_SNAPSHOT, None)
        quote_key = (CaptureStream.ALPACA_NBBO_QUOTE, normalized_symbol)
        if (
            not normalized_symbol
            or threading.get_ident() != state.owner_thread_id
            or identity.identity_sha256 != state.capture_identity_sha256
            or identity.generation != state.capture_generation
            or state.captured_results_consumed
            or state.captured_result_replaced
            or set(state.captured_results) != {account_key, quote_key}
            or state.account is None
            or normalized_symbol not in state.quotes
        ):
            raise CapturedAlpacaPaperReadError(
                "captured paper account/NBBO read bundle is unavailable"
            )
        account = state.captured_results[account_key]
        quote = state.captured_results[quote_key]
        account_receipt = account.receipt
        quote_receipt = quote.receipt
        account_evidence = state.account.read
        quote_evidence = state.quotes[normalized_symbol].read
        if (
            account_receipt is None
            or quote_receipt is None
            or account_receipt.read_id != account_evidence.capture_read_id
            or quote_receipt.read_id != quote_evidence.capture_read_id
            or sha256_json(account_receipt.to_dict())
            != account_evidence.capture_read_receipt_sha256
            or sha256_json(quote_receipt.to_dict())
            != quote_evidence.capture_read_receipt_sha256
            or account.source_events[0].event_sha256
            != account_evidence.capture_event_sha256
            or quote.source_events[0].event_sha256
            != quote_evidence.capture_event_sha256
        ):
            raise CapturedAlpacaPaperReadError(
                "captured paper account/NBBO read bundle changed"
            )
        state.captured_results_consumed = True
        state.captured_results.clear()
        return account, quote

    @staticmethod
    def _validate_captured_result(
        result: Any,
        *,
        coordinator: Any,
        decision_id: str,
        stream: CaptureStream,
        provider: str,
        symbol: str | None,
        query: Mapping[str, Any],
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
    ) -> tuple[CaptureEvent, CapturedPaperReadEvidence]:
        if not isinstance(result, CapturedReadResult) or not result.durable:
            raise CapturedAlpacaPaperReadError(
                f"{stream.value} read was not durably receipted"
            )
        if len(result.source_events) != 1 or result.receipt is None:
            raise CapturedAlpacaPaperReadError(
                f"{stream.value} read has an ambiguous source inventory"
            )
        event = result.source_events[0]
        receipt = result.receipt
        receipt_submission = result.receipt_submission
        assert receipt_submission is not None and receipt_submission.event is not None
        receipt_event = receipt_submission.event
        identity = getattr(coordinator, "identity", None)
        normalized_symbol = str(symbol or "").strip().upper() or None
        if (
            event.identity != identity
            or event.stream is not stream
            or event.provider != provider
            or event.symbol != normalized_symbol
            or event.query != query
            or event.payload != payload
            or event.clocks != clocks
        ):
            raise CapturedAlpacaPaperReadError(
                f"{stream.value} source event differs from the observed provider result"
            )
        expected_result_sha = captured_read_result_sha256(
            (CaptureEventRef.from_event(event),)
        )
        if (
            receipt.decision_id != decision_id
            or receipt.identity_sha256 != identity.identity_sha256
            or receipt.stream is not stream
            or receipt.provider != provider
            or receipt.symbol != normalized_symbol
            or receipt.query != query
            or receipt.query_sha256 != sha256_json(query)
            or receipt.source_event_sha256s != (event.event_sha256,)
            or receipt.result_sha256 != expected_result_sha
            or receipt.empty_result
            or not receipt.content_verified
            or receipt.replay_network_fallback_used
            or receipt.requested_at > receipt.returned_at
            or receipt.returned_at != clocks.available_at
        ):
            raise CapturedAlpacaPaperReadError(
                f"{stream.value} receipt does not bind the exact observed result"
            )
        if (
            receipt_event.identity != identity
            or receipt_event.stream is not CaptureStream.READ_RECEIPT
            or receipt_event.payload != receipt.to_dict()
            or receipt_event.sequence <= event.sequence
        ):
            raise CapturedAlpacaPaperReadError(
                f"{stream.value} receipt event is not append-only after its source"
            )
        receipt_sha256 = sha256_json(receipt.to_dict())
        capture_identity_sha256 = sha256_json(
            {
                "capture_run_identity_sha256": identity.identity_sha256,
                "source_event_sha256": event.event_sha256,
                "receipt_sha256": receipt_sha256,
            }
        )
        return event, CapturedPaperReadEvidence(
            decision_id=decision_id,
            stream=stream,
            provider=provider,
            capture_identity_sha256=capture_identity_sha256,
            capture_event_sha256=event.event_sha256,
            capture_content_sha256=event.payload_sha256,
            capture_sequence=event.sequence,
            capture_read_id=receipt.read_id,
            capture_read_receipt_sha256=receipt_sha256,
            capture_result_sha256=receipt.result_sha256,
            requested_at=receipt.requested_at,
            provider_event_at=event.clocks.provider_event_at,
            received_at=event.clocks.received_at,
            available_at=event.clocks.available_at,
        )

    def _capture_account(self, state: _DecisionState) -> CapturedPaperAccountSnapshot:
        self._assert_static_binding()
        requested_at = self._now()
        raw = self._adapter.get_account_snapshot()
        returned_at = self._now()
        if returned_at < requested_at:
            raise CapturedAlpacaPaperReadError(
                "Alpaca PAPER account query returned before it was requested"
            )
        if not isinstance(raw, Mapping) or raw.get("ok") is not True:
            raise CapturedAlpacaPaperReadError(
                "fresh Alpaca PAPER account snapshot is unavailable"
            )
        try:
            account_id = canonical_alpaca_paper_account_id(raw.get("account_id"))
        except ValueError as exc:
            raise CapturedAlpacaPaperReadError(
                "fresh account snapshot has a non-canonical Alpaca PAPER UUID"
            ) from exc
        if account_id != self._expected_account_id or raw.get("paper") is not True:
            raise CapturedAlpacaPaperReadError(
                "fresh account snapshot is not the expected Alpaca PAPER UUID"
            )
        received_at = _parse_utc(raw.get("retrieved_at_utc"), "account received_at")
        if received_at < requested_at or received_at > returned_at:
            raise CapturedAlpacaPaperReadError(
                "Alpaca PAPER account receive clock escaped its exact query window"
            )
        status = raw.get("status")
        if status != ALPACA_PAPER_ACCOUNT_READY_STATUS:
            raise CapturedAlpacaPaperReadError(
                "Alpaca PAPER account status is not ACTIVE"
            )
        equity = _positive_decimal(raw.get("equity"), "account equity")
        last_equity = _positive_decimal(
            raw.get("last_equity"), "account last_equity"
        )
        buying_power = _positive_decimal(
            raw.get("buying_power"), "account buying_power", allow_zero=True
        )
        cash_raw = raw.get("cash")
        cash = (
            None
            if cash_raw is None
            else _finite_decimal(cash_raw, "account cash")
        )
        account_blocked = _required_false_broker_flag(
            raw.get("account_blocked"), "account_blocked"
        )
        trading_blocked = _required_false_broker_flag(
            raw.get("trading_blocked"), "trading_blocked"
        )
        trade_suspended = _required_false_broker_flag(
            raw.get("trade_suspended_by_user"), "trade_suspended_by_user"
        )
        payload: dict[str, Any] = {
            "schema_version": ALPACA_PAPER_ACCOUNT_PAYLOAD_SCHEMA_VERSION,
            "account_id": account_id,
            "account_identity_sha256": self._coordinator.identity.account_identity_sha256,
            "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
            "paper": True,
            "status": status,
            "equity_usd": str(equity),
            "last_equity_usd": str(last_equity),
            "buying_power_usd": str(buying_power),
            "cash_usd": None if cash is None else str(cash),
            "account_blocked": account_blocked,
            "trading_blocked": trading_blocked,
            "trade_suspended_by_user": trade_suspended,
            "received_at": _iso(received_at),
        }
        query = alpaca_paper_account_capture_query(self._expected_account_id)
        clocks = CaptureClocks(
            received_at=received_at,
            available_at=returned_at,
            market_reference_at=returned_at,
        )
        generation = state.account_generation + 1
        read_id = str(
            uuid.uuid5(
                uuid.UUID(self._coordinator.identity.run_id),
                f"alpaca-paper-account:{state.decision_id}:{generation}",
            )
        )
        result = self._coordinator.capture_query_result(
            decision_id=state.decision_id,
            stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            provider=ALPACA_PAPER_ACCOUNT_PROVIDER,
            query=query,
            requested_at=requested_at,
            returned_at=returned_at,
            results=(ObservedCaptureInput(payload=payload, clocks=clocks),),
            symbol=None,
            read_id=read_id,
        )
        event, read = self._validate_captured_result(
            result,
            coordinator=self._coordinator,
            decision_id=state.decision_id,
            stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            provider=ALPACA_PAPER_ACCOUNT_PROVIDER,
            symbol=None,
            query=query,
            payload=payload,
            clocks=clocks,
        )
        self._retain_captured_result(
            state,
            result=result,
            stream=CaptureStream.ACCOUNT_RISK_SNAPSHOT,
            symbol=None,
        )
        snapshot = CapturedPaperAccountSnapshot(
            account_id=account_id,
            account_scope=ALPACA_PAPER_ACCOUNT_SCOPE,
            paper=True,
            status=status,
            equity=equity,
            last_equity=last_equity,
            buying_power=buying_power,
            cash=cash,
            account_blocked=account_blocked,
            trading_blocked=trading_blocked,
            trade_suspended_by_user=trade_suspended,
            read=read,
            payload_sha256=event.payload_sha256,
            payload=payload,
        )
        state.account_generation = generation
        return snapshot

    def _account_fresh(self, snapshot: CapturedPaperAccountSnapshot, now: datetime) -> bool:
        age = (now - snapshot.read.available_at).total_seconds()
        return -_FUTURE_TOLERANCE_SECONDS <= age <= self._account_max_age_seconds

    def capture_account_snapshot(self) -> CapturedPaperAccountSnapshot:
        state = self._state()
        existing = state.account
        if existing is not None and self._account_fresh(existing, self._now()):
            return existing
        current = self._capture_account(state)
        state.account = current
        if existing is not None:
            raise CapturedPaperDecisionIdentityChanged(
                kind="account",
                previous=existing.read.capture_identity_sha256,
                current=current.read.capture_identity_sha256,
            )
        return current

    def capture_account_with_open_order_census(
        self,
    ) -> CapturedPaperAccountSnapshot:
        """Capture stable open orders A -> account -> open orders B.

        This method must be the first account read in a decision.  It never
        reuses an earlier snapshot because that would place the account outside
        the broker-order bracket used to resolve pending buying-power reflection.
        """

        state = self._state()
        if (
            state.account is not None
            or state.bp_census_before is not None
            or state.bp_census_after is not None
        ):
            raise CapturedAlpacaPaperReadError(
                "buying-power census requires the first account read in a decision"
            )
        from .alpaca_buying_power_reflection import (
            AlpacaBuyingPowerReflectionError,
            read_verified_alpaca_paper_open_order_census,
        )

        try:
            before = read_verified_alpaca_paper_open_order_census(
                self,
                decision_id=state.decision_id,
                phase="before_account",
            )
            account = self._capture_account(state)
            after = read_verified_alpaca_paper_open_order_census(
                self,
                decision_id=state.decision_id,
                phase="after_account",
            )
        except AlpacaBuyingPowerReflectionError as exc:
            raise CapturedAlpacaPaperReadError(
                "Alpaca PAPER buying-power census is unavailable"
            ) from exc
        state.account = account
        state.bp_census_before = before
        state.bp_census_after = after
        return account

    def prepare_buying_power_double_census(
        self,
        account_authority: CapturedAlpacaPaperAccountAuthority,
    ) -> Any:
        """Bind the stored A/account/B bracket to the issued account authority."""

        state = self._state()
        if state.bp_census_before is None or state.bp_census_after is None:
            raise CapturedAlpacaPaperReadError(
                "captured open-order double census is missing"
            )
        from .alpaca_buying_power_reflection import (
            AlpacaBuyingPowerReflectionError,
            prepare_alpaca_paper_buying_power_double_census,
        )

        try:
            return prepare_alpaca_paper_buying_power_double_census(
                account_authority=account_authority,
                before=state.bp_census_before,
                after=state.bp_census_after,
                verified_at=self._now(),
            )
        except AlpacaBuyingPowerReflectionError as exc:
            raise CapturedAlpacaPaperReadError(
                "captured open-order double census failed verification"
            ) from exc

    def get_account_snapshot(self) -> dict[str, Any]:
        return self.capture_account_snapshot().to_adapter_snapshot()

    @property
    def current_account_evidence(self) -> CapturedPaperAccountSnapshot | None:
        state = self._decision.get()
        return None if state is None else state.account

    def issue_account_authority(
        self,
        active_input_attestation: ActiveCaptureInputPrefixAttestation,
    ) -> CapturedAlpacaPaperAccountAuthority:
        """Bind the pinned account read to the runtime's private input proof.

        The attestation must have been minted after the account receipt was
        durably appended.  This method never fetches or refreshes the account;
        if the current scope has no pinned read, the decision is incomplete.
        """

        self._assert_static_binding()
        state = self._state()
        snapshot = state.account
        if snapshot is None:
            raise CapturedAlpacaPaperReadError(
                "captured account authority requires a pinned account read"
            )
        try:
            proof = verify_active_capture_input_attestation(
                active_input_attestation
            )
        except CaptureContractError as exc:
            raise CapturedAlpacaPaperReadError(
                "captured account authority input attestation is invalid"
            ) from exc
        identity = self._coordinator.identity
        now = self._now()
        expected = {
            "run_id": (proof.run_id, identity.run_id),
            "generation": (proof.generation, identity.generation),
            "decision_id": (proof.decision_id, state.decision_id),
            "capture_identity": (proof.identity_sha256, identity.identity_sha256),
            "account_identity": (
                proof.account_identity_sha256,
                identity.account_identity_sha256,
            ),
        }
        changed = sorted(
            name
            for name, (actual, required) in expected.items()
            if actual != required
        )
        if changed:
            raise CapturedAlpacaPaperReadError(
                "captured account authority attestation mismatch: "
                + ",".join(changed)
            )
        if not proof.attested_available_at <= now <= proof.expires_at:
            raise CapturedAlpacaPaperReadError(
                "captured account authority attestation is stale or from the future"
            )
        account_fresh_through = snapshot.read.available_at + timedelta(
            seconds=self._account_max_age_seconds
        )
        authority_expires_at = min(proof.expires_at, account_fresh_through)
        if now > authority_expires_at:
            raise CapturedAlpacaPaperReadError(
                "captured account authority account read is stale"
            )
        matches = tuple(
            row
            for row in proof.read_evidence
            if row.receipt.read_id == snapshot.read.capture_read_id
        )
        if len(matches) != 1:
            raise CapturedAlpacaPaperReadError(
                "captured account receipt is absent from the input attestation"
            )
        read = matches[0]
        refs = tuple(read.source_event_refs)
        receipt = read.receipt
        if (
            len(refs) != 1
            or receipt.stream is not CaptureStream.ACCOUNT_RISK_SNAPSHOT
            or receipt.provider != ALPACA_PAPER_ACCOUNT_PROVIDER
            or receipt.symbol is not None
            or receipt.decision_id != state.decision_id
            or receipt.identity_sha256 != identity.identity_sha256
            or receipt.empty_result
            or not receipt.content_verified
            or receipt.replay_network_fallback_used
            or read.receipt_sha256 != snapshot.read.capture_read_receipt_sha256
            or receipt.returned_at != snapshot.read.available_at
            or read.receipt_committed_available_at > proof.attested_available_at
        ):
            raise CapturedAlpacaPaperReadError(
                "captured account receipt cannot authorize this decision"
            )
        ref = refs[0]
        payload = dict(snapshot.payload)
        payload_sha = sha256_json(payload)
        if (
            ref.event_sha256 != snapshot.read.capture_event_sha256
            or ref.payload_sha256 != snapshot.payload_sha256
            or payload_sha != snapshot.payload_sha256
            or snapshot.read.capture_content_sha256 != snapshot.payload_sha256
            or ref.received_at != snapshot.read.received_at
            or ref.available_at != snapshot.read.available_at
            or ref.identity_sha256 != identity.identity_sha256
            or ref.stream is not CaptureStream.ACCOUNT_RISK_SNAPSHOT
            or ref.provider != ALPACA_PAPER_ACCOUNT_PROVIDER
            or ref.symbol is not None
        ):
            raise CapturedAlpacaPaperReadError(
                "captured account source payload differs from the pinned snapshot"
            )
        provider_generation = f"{read.producer_id}:{read.producer_generation}"
        body = {
            "schema_version": "chili.captured-alpaca-paper-account-authority.v1",
            "account_id": snapshot.account_id,
            "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
            "account_identity_sha256": proof.account_identity_sha256,
            "decision_id": state.decision_id,
            "run_id": proof.run_id,
            "generation": proof.generation,
            "expires_at": _iso(authority_expires_at),
            "active_input_attestation_sha256": proof.attestation_sha256,
            "account_payload_sha256": snapshot.payload_sha256,
            "account_read_id": snapshot.read.capture_read_id,
            "account_read_receipt_sha256": (
                snapshot.read.capture_read_receipt_sha256
            ),
            "account_source_event_sha256": snapshot.read.capture_event_sha256,
            "snapshot_id": (
                f"alpaca-paper-account-read:{snapshot.read.capture_read_id}"
            ),
            "source": ALPACA_PAPER_ACCOUNT_PROVIDER,
            "provider_generation": provider_generation,
            "observed_at": _iso(snapshot.read.received_at),
            "available_at": _iso(snapshot.read.available_at),
            "equity_usd": str(snapshot.equity),
            "buying_power_usd": str(snapshot.buying_power),
            "broker_day_change_usd": str(snapshot.equity - snapshot.last_equity),
        }
        return CapturedAlpacaPaperAccountAuthority(
            account_id=snapshot.account_id,
            account_identity_sha256=proof.account_identity_sha256,
            decision_id=state.decision_id,
            run_id=proof.run_id,
            generation=proof.generation,
            expires_at=authority_expires_at,
            active_input_attestation_sha256=proof.attestation_sha256,
            account_payload_sha256=snapshot.payload_sha256,
            account_read_id=snapshot.read.capture_read_id,
            account_read_receipt_sha256=(
                snapshot.read.capture_read_receipt_sha256
            ),
            account_source_event_sha256=snapshot.read.capture_event_sha256,
            snapshot_id=f"alpaca-paper-account-read:{snapshot.read.capture_read_id}",
            source=ALPACA_PAPER_ACCOUNT_PROVIDER,
            provider_generation=provider_generation,
            observed_at=snapshot.read.received_at,
            available_at=snapshot.read.available_at,
            equity_usd=snapshot.equity,
            buying_power_usd=snapshot.buying_power,
            broker_day_change_usd=snapshot.equity - snapshot.last_equity,
            _verification_tag=_captured_account_authority_tag(body),
            _verification_token=_CAPTURED_ACCOUNT_AUTHORITY_TOKEN,
        )

    def _capture_quote(
        self,
        state: _DecisionState,
        symbol: str,
        *,
        max_age_seconds: float,
        generation: int,
    ) -> CapturedPaperQuoteEvidence:
        # Identity/account truth is always observed and receipted before the
        # first market-data source can reach adaptive sizing or an order path.
        self.capture_account_snapshot()
        requested_at = self._now()
        tick, meta = self._adapter.get_execution_bbo(
            symbol, max_age_seconds=max_age_seconds
        )
        returned_at = self._now()
        if not isinstance(tick, NormalizedTicker) or not isinstance(meta, FreshnessMeta):
            raise CapturedAlpacaPaperReadError(
                "direct Alpaca PAPER execution quote is unavailable"
            )
        if returned_at < requested_at:
            raise CapturedAlpacaPaperReadError(
                "Alpaca PAPER quote returned before it was requested"
            )
        provider_at = _utc(meta.provider_time_utc, "quote provider_event_at")
        received_at = _utc(meta.retrieved_at_utc, "quote received_at")
        if received_at < requested_at or received_at > returned_at:
            raise CapturedAlpacaPaperReadError(
                "Alpaca PAPER quote receive clock escaped its exact query window"
            )
        provider_age = (returned_at - provider_at).total_seconds()
        received_age = (returned_at - received_at).total_seconds()
        if (
            provider_age < -_FUTURE_TOLERANCE_SECONDS
            or received_age < -_FUTURE_TOLERANCE_SECONDS
            or provider_age > max_age_seconds
            or received_age > max_age_seconds
        ):
            raise CapturedAlpacaPaperReadError(
                "direct Alpaca PAPER execution quote is stale or future-dated"
            )
        normalized = str(symbol or "").strip().upper()
        if str(tick.product_id or "").strip().upper() != normalized:
            raise CapturedAlpacaPaperReadError("Alpaca quote symbol mismatch")
        values = (tick.bid, tick.ask)
        if (
            any(isinstance(value, bool) or not isinstance(value, (int, float)) for value in values)
            or not all(math.isfinite(float(value)) for value in values)
            or float(tick.bid) <= 0
            or float(tick.ask) < float(tick.bid)
        ):
            raise CapturedAlpacaPaperReadError("Alpaca quote prices are malformed")
        sizes = (tick.bid_size, tick.ask_size)
        if any(
            value is not None
            and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or float(value) < 0.0
            )
            for value in sizes
        ):
            raise CapturedAlpacaPaperReadError("Alpaca quote sizes are malformed")
        raw = tick.raw if isinstance(tick.raw, Mapping) else {}
        feed = str(raw.get("feed") or "").strip()
        if not feed or "iqfeed" in feed.lower():
            raise CapturedAlpacaPaperReadError(
                "IQFeed Q/trade-reference data cannot authorize Alpaca execution"
            )
        if str(raw.get("timestamp_basis") or "") != "provider_event_at":
            raise CapturedAlpacaPaperReadError(
                "Alpaca quote lacks exact provider-event timestamp basis"
            )
        if _parse_utc(raw.get("provider_event_at_utc"), "raw quote provider_event_at") != provider_at:
            raise CapturedAlpacaPaperReadError(
                "Alpaca quote provider clock metadata mismatch"
            )
        if _parse_utc(raw.get("received_at_utc"), "raw quote received_at") != received_at:
            raise CapturedAlpacaPaperReadError(
                "Alpaca quote receive clock metadata mismatch"
            )
        bid = float(tick.bid)
        ask = float(tick.ask)
        mid = (bid + ask) / 2.0
        spread_bps = (ask - bid) / mid * 10_000.0
        payload = {
            "schema_version": ALPACA_NBBO_QUOTE_PAYLOAD_SCHEMA_VERSION,
            "symbol": normalized,
            "bid": bid,
            "ask": ask,
            "bid_size": tick.bid_size,
            "ask_size": tick.ask_size,
            # Alpaca's current CTA/UTP quote-size contract reports shares.
            # Bind the unit in the immutable payload so risk sizing never
            # guesses whether a numeric size is shares or legacy round lots.
            "size_unit": "shares",
            "feed": feed,
            "provider_event_at": _iso(provider_at),
            "received_at": _iso(received_at),
            "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
        }
        query = {
            "schema_version": ALPACA_NBBO_QUOTE_QUERY_SCHEMA_VERSION,
            "operation": "get_execution_bbo",
            "symbol": normalized,
            "feed": feed,
            "max_age_seconds": float(max_age_seconds),
            "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
        }
        clocks = CaptureClocks(
            provider_event_at=provider_at,
            received_at=received_at,
            available_at=returned_at,
        )
        read_id = str(
            uuid.uuid5(
                uuid.UUID(self._coordinator.identity.run_id),
                f"alpaca-paper-nbbo:{state.decision_id}:{normalized}:{generation}",
            )
        )
        result = self._coordinator.capture_query_result(
            decision_id=state.decision_id,
            stream=CaptureStream.ALPACA_NBBO_QUOTE,
            provider=_ALPACA_NBBO_PROVIDER,
            query=query,
            requested_at=requested_at,
            returned_at=returned_at,
            results=(ObservedCaptureInput(payload=payload, clocks=clocks),),
            symbol=normalized,
            read_id=read_id,
        )
        _event, read = self._validate_captured_result(
            result,
            coordinator=self._coordinator,
            decision_id=state.decision_id,
            stream=CaptureStream.ALPACA_NBBO_QUOTE,
            provider=_ALPACA_NBBO_PROVIDER,
            symbol=normalized,
            query=query,
            payload=payload,
            clocks=clocks,
        )
        self._retain_captured_result(
            state,
            result=result,
            stream=CaptureStream.ALPACA_NBBO_QUOTE,
            symbol=normalized,
        )
        return CapturedPaperQuoteEvidence(
            symbol=normalized,
            generation=generation,
            bid=bid,
            ask=ask,
            mid=mid,
            spread_bps=spread_bps,
            bid_size=tick.bid_size,
            ask_size=tick.ask_size,
            feed=feed,
            read=read,
        )

    @staticmethod
    def _quote_ticker(
        evidence: CapturedPaperQuoteEvidence, *, max_age_seconds: float
    ) -> tuple[NormalizedTicker, FreshnessMeta]:
        read = evidence.read
        freshness = FreshnessMeta(
            retrieved_at_utc=read.received_at,
            provider_time_utc=read.provider_event_at,
            max_age_seconds=float(max_age_seconds),
        )
        raw = {
            "feed": evidence.feed,
            "timestamp_basis": "provider_event_at",
            "provider_event_at_utc": (
                None if read.provider_event_at is None else _iso(read.provider_event_at)
            ),
            "received_at_utc": _iso(read.received_at),
            "available_at_utc": _iso(read.available_at),
            "capture_event_sha256": read.capture_event_sha256,
            "capture_content_sha256": read.capture_content_sha256,
            "capture_sequence": read.capture_sequence,
            "capture_read_id": read.capture_read_id,
            "capture_read_receipt_sha256": read.capture_read_receipt_sha256,
            "capture_result_sha256": read.capture_result_sha256,
            "capture_identity_sha256": read.capture_identity_sha256,
            "capture_generation": evidence.generation,
            "decision_id": read.decision_id,
            "account_scope": ALPACA_PAPER_ACCOUNT_SCOPE,
        }
        return (
            NormalizedTicker(
                product_id=evidence.symbol,
                bid=evidence.bid,
                ask=evidence.ask,
                mid=evidence.mid,
                spread_abs=evidence.ask - evidence.bid,
                spread_bps=evidence.spread_bps,
                bid_size=evidence.bid_size,
                ask_size=evidence.ask_size,
                freshness=freshness,
                raw=raw,
            ),
            freshness,
        )

    @staticmethod
    def _quote_fresh(
        evidence: CapturedPaperQuoteEvidence,
        now: datetime,
        max_age_seconds: float,
    ) -> bool:
        provider_at = evidence.read.provider_event_at
        if provider_at is None:
            return False
        ages = (
            (now - provider_at).total_seconds(),
            (now - evidence.read.received_at).total_seconds(),
            (now - evidence.read.available_at).total_seconds(),
        )
        return all(
            -_FUTURE_TOLERANCE_SECONDS <= age <= max_age_seconds for age in ages
        )

    def _captured_bbo(
        self, product_id: str, *, max_age_seconds: float
    ) -> tuple[NormalizedTicker, FreshnessMeta]:
        self._assert_static_binding()
        state = self._state()
        normalized = str(product_id or "").strip().upper()
        if not normalized:
            raise CapturedAlpacaPaperReadError("Alpaca quote symbol is required")
        max_age = min(float(max_age_seconds), self._quote_max_age_seconds)
        if not math.isfinite(max_age) or max_age <= 0:
            raise CapturedAlpacaPaperReadError(
                "Alpaca execution quote freshness bound is malformed"
            )
        existing = state.quotes.get(normalized)
        if existing is not None and self._quote_fresh(existing, self._now(), max_age):
            return self._quote_ticker(existing, max_age_seconds=max_age)
        generation = 1 if existing is None else existing.generation + 1
        current = self._capture_quote(
            state,
            normalized,
            max_age_seconds=max_age,
            generation=generation,
        )
        state.quotes[normalized] = current
        if existing is not None:
            raise CapturedPaperDecisionIdentityChanged(
                kind="quote",
                previous=existing.read.capture_identity_sha256,
                current=current.read.capture_identity_sha256,
            )
        return self._quote_ticker(current, max_age_seconds=max_age)

    def get_execution_bbo(
        self, product_id: str, *, max_age_seconds: float = 2.0
    ) -> tuple[NormalizedTicker, FreshnessMeta]:
        return self._captured_bbo(product_id, max_age_seconds=max_age_seconds)

    def get_best_bid_ask(
        self, product_id: str
    ) -> tuple[NormalizedTicker, FreshnessMeta]:
        return self._captured_bbo(
            product_id, max_age_seconds=self._quote_max_age_seconds
        )

    def get_ticker(self, product_id: str) -> tuple[NormalizedTicker, FreshnessMeta]:
        return self.get_best_bid_ask(product_id)

    @staticmethod
    def _copy_product(product: NormalizedProduct) -> NormalizedProduct:
        return NormalizedProduct(
            **{
                name: deepcopy(getattr(product, name))
                for name in NormalizedProduct.__dataclass_fields__
            }
        )

    def capture_product_eligibility(
        self,
        product_id: str,
    ) -> tuple[NormalizedProduct, FreshnessMeta]:
        """Fetch and pin one product read for the durable eligibility receipt.

        The material provider calls this once and immediately captures the
        returned value as ``ADMISSION_ELIGIBILITY``.  The FSM's later
        ``get_product`` call then consumes only this frozen value; it cannot
        perform a second current provider/network read or turn a provider
        failure into the legacy ``product is None`` continuation.
        """

        self._assert_static_binding()
        state = self._state()
        identity = self._coordinator.identity
        normalized = str(product_id or "").strip().upper()
        if (
            not normalized
            or threading.get_ident() != state.owner_thread_id
            or identity.identity_sha256 != state.capture_identity_sha256
            or identity.generation != state.capture_generation
        ):
            raise CapturedAlpacaPaperReadError(
                "Alpaca PAPER product decision identity is unavailable"
            )
        if state.product is not None:
            if state.product.product_id.upper() != normalized:
                raise CapturedAlpacaPaperReadError(
                    "captured PAPER product route mismatch"
                )
            assert state.product_freshness is not None
            return self._copy_product(state.product), state.product_freshness
        getter = getattr(self._adapter, "get_product", None)
        if not callable(getter):
            raise CapturedAlpacaPaperReadError(
                "direct Alpaca PAPER product read is unavailable"
            )
        try:
            product, freshness = getter(normalized)
        except Exception as exc:
            raise CapturedAlpacaPaperReadError(
                "direct Alpaca PAPER product read failed"
            ) from exc
        if (
            type(product) is not NormalizedProduct
            or type(freshness) is not FreshnessMeta
            or str(product.product_id or "").strip().upper() != normalized
        ):
            raise CapturedAlpacaPaperReadError(
                "direct Alpaca PAPER product read is malformed"
            )
        state.product = self._copy_product(product)
        state.product_freshness = freshness
        return self._copy_product(state.product), freshness

    def get_product(
        self,
        product_id: str,
    ) -> tuple[NormalizedProduct, FreshnessMeta]:
        """Return only the product already pinned for this decision."""

        self._assert_static_binding()
        state = self._state()
        identity = self._coordinator.identity
        normalized = str(product_id or "").strip().upper()
        if (
            state.product is None
            or state.product_freshness is None
            or state.product.product_id.upper() != normalized
            or threading.get_ident() != state.owner_thread_id
            or identity.identity_sha256 != state.capture_identity_sha256
            or identity.generation != state.capture_generation
        ):
            raise CapturedAlpacaPaperReadError(
                "captured PAPER product eligibility is unavailable"
            )
        return self._copy_product(state.product), state.product_freshness

    def get_products(self) -> tuple[list[NormalizedProduct], FreshnessMeta]:
        """Expose the one frozen product only; never delegate a broad read."""

        state = self._state()
        if state.product is None:
            raise CapturedAlpacaPaperReadError(
                "captured PAPER product eligibility is unavailable"
            )
        product, freshness = self.get_product(state.product.product_id)
        return [product], freshness

    def __getattr__(self, name: str) -> Any:
        # Never expose a private provider seam through the wrapper.  Public
        # lifecycle reads/mutations retain the wrapped adapter's arguments and
        # return values, with only the immutable paper/account binding recheck.
        if str(name).startswith("_"):
            raise AttributeError(name)
        self._assert_static_binding()
        target = getattr(self._adapter, name)
        if not callable(target):
            return target

        def guarded(*args: Any, **kwargs: Any) -> Any:
            self._assert_static_binding()
            return target(*args, **kwargs)

        return guarded


class CapturedAlpacaPaperObservationAdapter:
    """Read-only captured view for WATCHING/QUEUED detector ticks.

    The view exposes only the already-captured account, BBO, and product
    eligibility facts.  It deliberately has no order, position, fill, preview,
    cancel, or generic delegation surface, so an observation tick cannot turn a
    missing admission packet into broker I/O.
    """

    __slots__ = (
        "_captured",
        "_eligibility_event_sha256",
        "_eligibility_read_id",
        "_freshness",
        "_observation_decision_id",
        "_product",
    )

    def __init__(
        self,
        *,
        captured_adapter: CapturedAlpacaPaperAdapter,
        product: NormalizedProduct,
        freshness: FreshnessMeta,
        eligibility_read_id: str,
        eligibility_event_sha256: str,
        observation_decision_id: str,
    ) -> None:
        if type(captured_adapter) is not CapturedAlpacaPaperAdapter:
            raise CapturedAlpacaPaperReadError(
                "captured PAPER observation adapter is unavailable"
            )
        if type(product) is not NormalizedProduct or type(freshness) is not FreshnessMeta:
            raise CapturedAlpacaPaperReadError(
                "captured PAPER observation eligibility is unavailable"
            )
        read_id = str(eligibility_read_id or "").strip()
        event_sha = str(eligibility_event_sha256 or "").strip().lower()
        decision_id = str(observation_decision_id or "").strip()
        try:
            read_id = str(uuid.UUID(read_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise CapturedAlpacaPaperReadError(
                "captured PAPER observation eligibility read is unavailable"
            ) from exc
        if (
            len(event_sha) != 64
            or any(ch not in "0123456789abcdef" for ch in event_sha)
            or not decision_id
        ):
            raise CapturedAlpacaPaperReadError(
                "captured PAPER observation eligibility identity is unavailable"
            )
        self._captured = captured_adapter
        self._product = NormalizedProduct(
            **{
                name: deepcopy(getattr(product, name))
                for name in NormalizedProduct.__dataclass_fields__
            }
        )
        self._freshness = freshness
        self._eligibility_read_id = read_id
        self._eligibility_event_sha256 = event_sha
        self._observation_decision_id = decision_id

    @property
    def bound_account_id(self) -> str:
        return self._captured.bound_account_id

    @property
    def broker_environment(self) -> str:
        return "paper"

    @property
    def network_fallback_allowed(self) -> bool:
        return False

    @property
    def eligibility_read_id(self) -> str:
        return self._eligibility_read_id

    @property
    def eligibility_event_sha256(self) -> str:
        return self._eligibility_event_sha256

    @property
    def observation_decision_id(self) -> str:
        return self._observation_decision_id

    def bind_account_id(self, account_id: str) -> bool:
        if str(account_id or "").strip() != self.bound_account_id:
            return False
        bind = getattr(self._captured, "bind_account_id", None)
        return bool(callable(bind) and bind(self.bound_account_id) is True)

    def is_enabled(self) -> bool:
        return True

    def capture_account_snapshot(self) -> CapturedPaperAccountSnapshot:
        return self._captured.capture_account_snapshot()

    def get_account_snapshot(self) -> dict[str, Any]:
        return self._captured.get_account_snapshot()

    @property
    def current_account_evidence(self) -> CapturedPaperAccountSnapshot | None:
        return self._captured.current_account_evidence

    def get_execution_bbo(
        self,
        product_id: str,
        *,
        max_age_seconds: float = 2.0,
    ) -> tuple[NormalizedTicker, FreshnessMeta]:
        return self._captured.get_execution_bbo(
            product_id,
            max_age_seconds=max_age_seconds,
        )

    def get_best_bid_ask(
        self,
        product_id: str,
    ) -> tuple[NormalizedTicker, FreshnessMeta]:
        return self._captured.get_best_bid_ask(product_id)

    def get_ticker(
        self,
        product_id: str,
    ) -> tuple[NormalizedTicker, FreshnessMeta]:
        return self._captured.get_ticker(product_id)

    def get_product(
        self,
        product_id: str,
    ) -> tuple[NormalizedProduct | None, FreshnessMeta]:
        if str(product_id or "").strip().upper() != self._product.product_id.upper():
            raise CapturedAlpacaPaperReadError(
                "captured PAPER observation product route mismatch"
            )
        product = NormalizedProduct(
            **{
                name: deepcopy(getattr(self._product, name))
                for name in NormalizedProduct.__dataclass_fields__
            }
        )
        return product, self._freshness

    def get_products(self) -> tuple[list[NormalizedProduct], FreshnessMeta]:
        product, freshness = self.get_product(self._product.product_id)
        assert product is not None
        return [product], freshness

    @staticmethod
    def _mutation_unavailable(*_args: Any, **_kwargs: Any) -> Any:
        raise CapturedAlpacaPaperReadError(
            "captured PAPER observation has no broker mutation capability"
        )

    place_market_order = _mutation_unavailable
    place_limit_order_gtc = _mutation_unavailable
    place_deadman_stop = _mutation_unavailable
    cancel_order = _mutation_unavailable
    cancel_order_by_id = _mutation_unavailable
    preview_market_order = _mutation_unavailable

    def __getattr__(self, name: str) -> Any:
        raise CapturedAlpacaPaperReadError(
            f"captured PAPER observation capability unavailable:{name}"
        )

    def __reduce__(self):
        raise TypeError("captured PAPER observation adapters cannot be serialized")


__all__ = [
    "CapturedAlpacaPaperObservationAdapter",
    "CapturedAlpacaPaperAccountAuthority",
    "CapturedAlpacaPaperAdapter",
    "CapturedAlpacaPaperReadError",
    "CapturedPaperAccountSnapshot",
    "CapturedPaperDecisionIdentityChanged",
    "CapturedPaperQuoteEvidence",
    "CapturedPaperReadEvidence",
    "verify_captured_alpaca_paper_account_authority",
]
