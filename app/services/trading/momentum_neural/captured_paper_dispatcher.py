"""Single production dispatch boundary for captured Alpaca PAPER ticks.

The live event loop and the scheduler batch both enter the momentum FSM through
this module.  Non-Alpaca sessions retain the ordinary runner path.  An Alpaca
session, however, can run only through an explicitly registered captured-paper
runtime whose account identity and immutable provenance still match effective
configuration at the instant of dispatch.

This module deliberately does *not* construct adaptive inputs, broker adapters,
or an IQFeed host.  The future host supervisor owns those capabilities and must
register one handler after its own content-addressed launch/capture validation.
Until then Alpaca PAPER fails before the FSM (and therefore before any order
claim, risk reservation, adapter construction, or broker mutation).
"""

from __future__ import annotations

from contextlib import contextmanager
import contextvars
from dataclasses import dataclass, field
import hashlib
import json
import re
import threading
import uuid
from typing import Any, Callable, Iterator, Mapping, Protocol

from sqlalchemy.orm.attributes import flag_modified

from ....config import settings
from ....models.trading import TradingAutomationSession
from ..execution_family_registry import (
    EXECUTION_FAMILY_ALPACA_SHORT,
    EXECUTION_FAMILY_ALPACA_SPOT,
    normalize_execution_family,
)
from .captured_paper_entry_intent import (
    CapturedPaperIntentContractError,
    CapturedPaperPostCommitHandler,
    CapturedPaperPostCommitRequest,
    CapturedPaperRouteDriftError,
    CapturedPaperRouteToken,
    revalidate_captured_paper_route_token as _revalidate_route_token,
)
from .adaptive_risk_account_lock import AdaptiveRiskAccountLockIdentity


_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SCOPE = "alpaca:paper"
_FIRST_DIP_POLICY_MODES = frozenset({"baseline", "candidate", "promoted"})
_SESSION_OWNER_KEY = "captured_paper_session_owner"
_SESSION_OWNER_SCHEMA_VERSION = "chili.captured-paper-session-owner.v1"
_SESSION_OWNER_BODY_KEYS = frozenset(
    {
        "schema_version",
        "session_id",
        "symbol",
        "account_scope",
        "expected_account_id",
        "runtime_generation",
        "execution_family",
        "route_token_sha256",
        "code_build_sha256",
        "config_sha256",
        "capture_receipt_sha256",
    }
)
_SESSION_OWNER_KEYS = _SESSION_OWNER_BODY_KEYS | {"content_sha256"}


class CapturedPaperDispatchError(RuntimeError):
    """Base class for a fail-closed captured-paper dispatch rejection."""

    def __init__(self, reason: str):
        self.reason = str(reason or "captured_paper_dispatch_rejected")
        super().__init__(self.reason)


class CapturedPaperRuntimeUnavailableError(CapturedPaperDispatchError):
    """No matching validated runtime is available for this PAPER decision."""


class CapturedPaperExecutionProhibitedError(CapturedPaperDispatchError):
    """The requested route is outside the fake-money long-equity boundary."""


@dataclass(frozen=True, slots=True)
class CapturedPaperDispatchRequest:
    """Immutable identity/provenance passed to the registered capture owner."""

    session_id: int
    symbol: str
    execution_family: str
    account_scope: str
    expected_account_id: str
    code_build_sha256: str
    config_sha256: str
    capture_receipt_sha256: str
    runtime_generation: str
    first_dip_policy_mode: str
    route_token: CapturedPaperRouteToken = field(init=False)
    provenance_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        token = CapturedPaperRouteToken(
            session_id=self.session_id,
            symbol=self.symbol,
            execution_family=self.execution_family,
            account_scope=self.account_scope,
            expected_account_id=self.expected_account_id,
            code_build_sha256=self.code_build_sha256,
            config_sha256=self.config_sha256,
            capture_receipt_sha256=self.capture_receipt_sha256,
            runtime_generation=self.runtime_generation,
            first_dip_policy_mode=self.first_dip_policy_mode,
        )
        object.__setattr__(self, "route_token", token)
        object.__setattr__(
            self,
            "provenance_sha256",
            token.route_token_sha256,
        )

    def verify(self) -> None:
        """Detect mutation before the request crosses a later composition seam."""

        self.route_token.verify()
        exact = {
            "session_id": self.session_id,
            "symbol": self.symbol,
            "execution_family": self.execution_family,
            "account_scope": self.account_scope,
            "expected_account_id": self.expected_account_id,
            "code_build_sha256": self.code_build_sha256,
            "config_sha256": self.config_sha256,
            "capture_receipt_sha256": self.capture_receipt_sha256,
            "runtime_generation": self.runtime_generation,
            "first_dip_policy_mode": self.first_dip_policy_mode,
        }
        if any(
            getattr(self.route_token, name) != value
            for name, value in exact.items()
        ) or self.provenance_sha256 != self.route_token.route_token_sha256:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_dispatch_request_mutated"
            )


_ACTIVE_SESSION_OWNER_REQUEST: contextvars.ContextVar[
    CapturedPaperDispatchRequest | None
] = contextvars.ContextVar(
    "_chili_captured_paper_session_owner_request",
    default=None,
)


def _sha256_json(payload: Mapping[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            dict(payload),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        ).encode("utf-8")
    ).hexdigest()


def captured_paper_session_owner_marker(
    request: CapturedPaperDispatchRequest,
) -> dict[str, Any]:
    """Return the exact content-addressed durable owner marker for one route."""

    if type(request) is not CapturedPaperDispatchRequest:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_request_invalid"
        )
    request.verify()
    if request.account_scope != _ACCOUNT_SCOPE:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_session_owner_scope_prohibited"
        )
    if request.execution_family != EXECUTION_FAMILY_ALPACA_SPOT:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_session_owner_execution_family_prohibited"
        )
    if not request.symbol or "-" in request.symbol or "/" in request.symbol:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_session_owner_non_equity_prohibited"
        )
    body: dict[str, Any] = {
        "schema_version": _SESSION_OWNER_SCHEMA_VERSION,
        "session_id": request.session_id,
        "symbol": request.symbol,
        "account_scope": request.account_scope,
        "expected_account_id": request.expected_account_id,
        "runtime_generation": request.runtime_generation,
        "execution_family": request.execution_family,
        "route_token_sha256": request.route_token.route_token_sha256,
        "code_build_sha256": request.code_build_sha256,
        "config_sha256": request.config_sha256,
        "capture_receipt_sha256": request.capture_receipt_sha256,
    }
    return {**body, "content_sha256": _sha256_json(body)}


def _validated_session_owner_marker(raw: Any) -> dict[str, Any]:
    if type(raw) is not dict or set(raw) != _SESSION_OWNER_KEYS:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_marker_invalid"
        )
    marker = dict(raw)
    if marker.get("schema_version") != _SESSION_OWNER_SCHEMA_VERSION:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_marker_invalid"
        )
    if marker.get("account_scope") != _ACCOUNT_SCOPE:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_session_owner_scope_prohibited"
        )
    if marker.get("execution_family") != EXECUTION_FAMILY_ALPACA_SPOT:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_session_owner_execution_family_prohibited"
        )
    try:
        if isinstance(marker.get("session_id"), bool):
            raise ValueError("boolean session id")
        session_id = int(marker.get("session_id"))
        if session_id <= 0 or session_id != marker.get("session_id"):
            raise ValueError("non-canonical session id")
        symbol = str(marker.get("symbol") or "").strip().upper()
        if symbol != marker.get("symbol") or not symbol or "-" in symbol or "/" in symbol:
            raise ValueError("non-equity symbol")
        expected_account_id = _canonical_uuid(
            str(marker.get("expected_account_id") or ""),
            field_name="captured PAPER durable owner account id",
        )
        runtime_generation = _canonical_uuid(
            str(marker.get("runtime_generation") or ""),
            field_name="captured PAPER durable owner generation",
        )
        for name in (
            "route_token_sha256",
            "code_build_sha256",
            "config_sha256",
            "capture_receipt_sha256",
            "content_sha256",
        ):
            value = marker.get(name)
            if not isinstance(value, str) or _validated_sha256(
                value,
                field_name=name,
            ) != value:
                raise ValueError(f"{name} is not canonical")
    except (TypeError, ValueError) as exc:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_marker_invalid"
        ) from exc
    if (
        expected_account_id != marker["expected_account_id"]
        or runtime_generation != marker["runtime_generation"]
    ):
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_marker_invalid"
        )
    body = {name: marker[name] for name in _SESSION_OWNER_BODY_KEYS}
    if _sha256_json(body) != marker["content_sha256"]:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_marker_hash_mismatch"
        )
    return marker


def _validate_owner_marker_against_session(
    marker: Mapping[str, Any],
    locked_session: Any,
) -> None:
    snapshot = getattr(locked_session, "risk_snapshot_json", None)
    if type(snapshot) is not dict:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_snapshot_invalid"
        )
    exact = {
        "session_id": int(getattr(locked_session, "id", 0) or 0),
        "symbol": str(getattr(locked_session, "symbol", "") or "").strip().upper(),
        "execution_family": normalize_execution_family(
            getattr(locked_session, "execution_family", None)
        ),
        "account_scope": str(snapshot.get("alpaca_account_scope") or "").strip(),
        "expected_account_id": str(snapshot.get("alpaca_account_id") or "").strip(),
    }
    if any(marker.get(name) != value for name, value in exact.items()):
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_route_mismatch"
        )


def validate_captured_paper_session_owner_inventory(
    session: Any,
    *,
    expected_account_id: str,
    expected_runtime_generation: str,
    expected_execution_family: str = EXECUTION_FAMILY_ALPACA_SPOT,
) -> dict[str, Any]:
    """Validate one runnable row for the dedicated captured-PAPER inventory.

    This is deliberately narrower than the FSM dispatch validator: it performs
    no configuration, context, database, provider, or broker lookup.  The
    durable final-owner marker must already exist and its content hash, row
    route, fake-money account, runtime generation, and execution family must
    all match the exact dedicated process scope.
    """

    try:
        account_id = _canonical_uuid(
            expected_account_id,
            field_name="captured PAPER inventory account id",
        )
        runtime_generation = _canonical_uuid(
            expected_runtime_generation,
            field_name="captured PAPER inventory runtime generation",
        )
    except ValueError as exc:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_inventory_scope_invalid"
        ) from exc
    family = normalize_execution_family(expected_execution_family)
    if family != EXECUTION_FAMILY_ALPACA_SPOT:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_inventory_scope_invalid"
        )

    snapshot = getattr(session, "risk_snapshot_json", None)
    if type(snapshot) is not dict or snapshot.get(_SESSION_OWNER_KEY) is None:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_missing"
        )
    marker = _validated_session_owner_marker(snapshot[_SESSION_OWNER_KEY])
    _validate_owner_marker_against_session(marker, session)
    if (
        marker["account_scope"] != _ACCOUNT_SCOPE
        or marker["expected_account_id"] != account_id
        or marker["runtime_generation"] != runtime_generation
        or marker["execution_family"] != family
    ):
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_inventory_scope_mismatch"
        )
    return dict(marker)


@contextmanager
def _activate_session_owner_request(
    request: CapturedPaperDispatchRequest,
) -> Iterator[CapturedPaperDispatchRequest]:
    request.verify()
    if _ACTIVE_SESSION_OWNER_REQUEST.get() is not None:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_request_already_active"
        )
    token = _ACTIVE_SESSION_OWNER_REQUEST.set(request)
    try:
        yield request
    finally:
        _ACTIVE_SESSION_OWNER_REQUEST.reset(token)


def revalidate_captured_paper_session_owner(
    locked_session: Any,
    *,
    request: CapturedPaperDispatchRequest | None = None,
    require_decision_context: bool = True,
) -> dict[str, Any] | None:
    """Fail a foreign/bare tick before mutation when a durable owner exists.

    Absence preserves the pre-bind path.  Presence is strict: the marker, row,
    current fake-money configuration, exact runtime request, and (for an FSM
    invocation) installed selection/observation capability must all agree.
    """

    snapshot = getattr(locked_session, "risk_snapshot_json", None)
    if type(snapshot) is not dict:
        return None
    raw = snapshot.get(_SESSION_OWNER_KEY)
    if raw is None:
        return None
    marker = _validated_session_owner_marker(raw)
    _validate_owner_marker_against_session(marker, locked_session)
    if getattr(settings, "chili_alpaca_paper", None) is not True:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_live_cash_execution_prohibited"
        )
    configured_account_id = str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip()
    if configured_account_id != marker["expected_account_id"]:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_config_account_mismatch"
        )

    active_request = request
    if active_request is None:
        active_request = _ACTIVE_SESSION_OWNER_REQUEST.get()
        if active_request is None:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_session_owned_by_isolated_runtime"
            )
    if type(active_request) is not CapturedPaperDispatchRequest:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_request_invalid"
        )
    active_request.verify()
    expected_marker = captured_paper_session_owner_marker(active_request)
    if marker != expected_marker:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_request_mismatch"
        )

    if require_decision_context:
        from .captured_paper_selection import (
            captured_paper_observation_context_active,
            captured_paper_selection_context_active,
            captured_paper_selection_required,
        )

        if not captured_paper_selection_required(
            execution_family=EXECUTION_FAMILY_ALPACA_SPOT
        ) or not (
            captured_paper_selection_context_active(
                execution_family=EXECUTION_FAMILY_ALPACA_SPOT
            )
            or captured_paper_observation_context_active(
                execution_family=EXECUTION_FAMILY_ALPACA_SPOT
            )
        ):
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_session_owner_decision_context_missing"
            )
    return marker


def bind_captured_paper_session_owner(
    db: Any,
    *,
    request: CapturedPaperDispatchRequest,
    account_lock_identity: AdaptiveRiskAccountLockIdentity,
) -> dict[str, Any]:
    """Bind one owner after a successful tick, inside its outer transaction.

    The caller must have acquired the canonical Alpaca action/adaptive account
    locks before the FSM's session row lock.  The exact identity returned by
    that helper is required here.  This function takes the session ``FOR
    UPDATE``, performs no network operation, never commits, and is idempotent
    only for the byte-exact same owner.
    """

    if type(request) is not CapturedPaperDispatchRequest:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_request_invalid"
        )
    request.verify()
    if getattr(settings, "chili_alpaca_paper", None) is not True:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_live_cash_execution_prohibited"
        )
    if str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip() != request.expected_account_id:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_config_account_mismatch"
        )
    active_request = _ACTIVE_SESSION_OWNER_REQUEST.get()
    if (
        active_request is None
        or type(active_request) is not CapturedPaperDispatchRequest
        or active_request.provenance_sha256 != request.provenance_sha256
        or active_request.route_token.route_token_sha256
        != request.route_token.route_token_sha256
    ):
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_bind_capability_missing"
        )
    from .captured_paper_selection import captured_paper_selection_required

    if not captured_paper_selection_required(
        execution_family=EXECUTION_FAMILY_ALPACA_SPOT
    ):
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_bind_capability_missing"
        )
    expected_lock = AdaptiveRiskAccountLockIdentity.for_scope(_ACCOUNT_SCOPE)
    if (
        type(account_lock_identity) is not AdaptiveRiskAccountLockIdentity
        or account_lock_identity != expected_lock
    ):
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_account_lock_missing"
        )
    in_transaction = getattr(db, "in_transaction", None)
    if not callable(in_transaction) or not in_transaction():
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_transaction_missing"
        )
    try:
        locked_session = (
            db.query(TradingAutomationSession)
            .populate_existing()
            .filter(
                TradingAutomationSession.id == request.session_id,
                TradingAutomationSession.mode == "live",
            )
            .with_for_update()
            .one_or_none()
        )
    except CapturedPaperDispatchError:
        raise
    except Exception as exc:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_row_lock_unavailable"
        ) from exc
    if locked_session is None:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_session_missing"
        )

    marker = captured_paper_session_owner_marker(request)
    _validate_owner_marker_against_session(marker, locked_session)
    snapshot = getattr(locked_session, "risk_snapshot_json", None)
    existing = snapshot.get(_SESSION_OWNER_KEY) if type(snapshot) is dict else None
    if existing is not None:
        verified = revalidate_captured_paper_session_owner(
            locked_session,
            request=request,
            require_decision_context=False,
        )
        if verified != marker:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_session_owner_request_mismatch"
            )
        return dict(verified)

    next_snapshot = dict(snapshot)
    next_snapshot[_SESSION_OWNER_KEY] = marker
    locked_session.risk_snapshot_json = next_snapshot
    flag_modified(locked_session, "risk_snapshot_json")
    flush = getattr(db, "flush", None)
    if not callable(flush):
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_owner_flush_unavailable"
        )
    flush()
    return dict(marker)


class CapturedPaperTickHandler(Protocol):
    """Phase-one handler; it must revalidate ``request.route_token`` later."""

    def __call__(self, db: Any, request: CapturedPaperDispatchRequest) -> Any: ...


def _canonical_uuid(value: str, *, field_name: str) -> str:
    raw = str(value or "").strip().lower()
    try:
        parsed = uuid.UUID(raw)
    except (AttributeError, TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be a canonical UUID") from exc
    canonical = str(parsed)
    if raw != canonical:
        raise ValueError(f"{field_name} must be a canonical UUID")
    return canonical


def _validated_sha256(value: str, *, field_name: str) -> str:
    digest = str(value or "").strip()
    if _SHA256_RE.fullmatch(digest) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return digest


@dataclass(frozen=True, slots=True)
class CapturedPaperRuntime:
    """A structurally validated, externally attested runtime registration.

    Hash syntax and cross-field identity are verified here.  File/hash authority
    remains the launch preflight's job; the dispatcher only makes sure those
    exact immutable values cannot be dropped or silently changed on the way to
    the capture owner and FSM.
    """

    handler: CapturedPaperTickHandler = field(repr=False, compare=False)
    expected_account_id: str
    code_build_sha256: str
    config_sha256: str
    capture_receipt_sha256: str
    runtime_generation: str
    first_dip_policy_mode: str
    account_scope: str = _ACCOUNT_SCOPE
    settings_projection_sha256: str | None = None
    config_sha256_resolver: Callable[[str], str] | None = field(
        default=None,
        repr=False,
        compare=False,
    )
    post_commit_handler: CapturedPaperPostCommitHandler | None = field(
        default=None,
        repr=False,
        compare=False,
    )

    def __post_init__(self) -> None:
        if not callable(self.handler):
            raise ValueError("captured PAPER handler must be callable")
        if self.post_commit_handler is not None and not callable(
            self.post_commit_handler
        ):
            raise ValueError(
                "captured PAPER post-commit handler must be callable"
            )
        if self.account_scope != _ACCOUNT_SCOPE:
            raise ValueError("captured PAPER account scope must be alpaca:paper")
        object.__setattr__(
            self,
            "expected_account_id",
            _canonical_uuid(
                self.expected_account_id,
                field_name="captured PAPER expected account id",
            ),
        )
        for field_name in (
            "code_build_sha256",
            "config_sha256",
            "capture_receipt_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _validated_sha256(getattr(self, field_name), field_name=field_name),
            )
        settings_sha = self.settings_projection_sha256
        if settings_sha is not None:
            object.__setattr__(
                self,
                "settings_projection_sha256",
                _validated_sha256(
                    settings_sha,
                    field_name="settings_projection_sha256",
                ),
            )
        if self.config_sha256_resolver is not None and not callable(
            self.config_sha256_resolver
        ):
            raise ValueError(
                "captured PAPER config SHA-256 resolver must be callable"
            )
        object.__setattr__(
            self,
            "runtime_generation",
            _canonical_uuid(
                self.runtime_generation,
                field_name="captured PAPER runtime generation",
            ),
        )
        mode = str(self.first_dip_policy_mode or "").strip().lower()
        if mode not in _FIRST_DIP_POLICY_MODES:
            raise ValueError("captured PAPER first-dip policy mode is invalid")
        object.__setattr__(self, "first_dip_policy_mode", mode)

    def resolve_config_sha256(self, symbol: str) -> str:
        """Resolve the exact final capture config for one active hot run.

        ``config_sha256`` remains the fixed-path fallback used by sealed unit
        fixtures.  Production supplies a per-symbol resolver because the full
        config includes the certification symbol and active IQFeed generation
        roster and therefore cannot be represented by one process-wide hash.
        """

        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_config_symbol_invalid"
            )
        resolver = self.config_sha256_resolver
        if resolver is None:
            return self.config_sha256
        try:
            resolved = resolver(normalized)
            return _validated_sha256(
                resolved,
                field_name="resolved_config_sha256",
            )
        except CapturedPaperRuntimeUnavailableError:
            raise
        except Exception as exc:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_capture_config_unavailable"
            ) from exc


_registry_lock = threading.RLock()
_registered_runtime: CapturedPaperRuntime | None = None
_registration_token: str | None = None
_inflight_by_token: dict[str, int] = {}


class CapturedPaperRuntimeHandle:
    """Ownership token preventing a stale supervisor from removing a new runtime."""

    __slots__ = ("_token", "_closed")

    def __init__(self, token: str) -> None:
        self._token = token
        self._closed = False

    def close(self) -> None:
        if self._closed:
            return
        _unregister_captured_paper_runtime(self._token)
        self._closed = True

    def __enter__(self) -> "CapturedPaperRuntimeHandle":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


def register_captured_paper_runtime(
    runtime: CapturedPaperRuntime,
) -> CapturedPaperRuntimeHandle:
    """Install one process-wide capture owner; replacement is never implicit."""

    if not isinstance(runtime, CapturedPaperRuntime):
        raise TypeError("runtime must be CapturedPaperRuntime")
    global _registered_runtime, _registration_token
    with _registry_lock:
        if _registered_runtime is not None:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_runtime_already_registered"
            )
        token = str(uuid.uuid4())
        _registered_runtime = runtime
        _registration_token = token
        _inflight_by_token[token] = 0
    return CapturedPaperRuntimeHandle(token)


def _unregister_captured_paper_runtime(token: str) -> None:
    global _registered_runtime, _registration_token
    with _registry_lock:
        if token != _registration_token or _registered_runtime is None:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_runtime_registration_mismatch"
            )
        if _inflight_by_token.get(token, 0) > 0:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_runtime_dispatch_in_flight"
            )
        _registered_runtime = None
        _registration_token = None
        _inflight_by_token.pop(token, None)


@contextmanager
def _leased_runtime() -> Iterator[CapturedPaperRuntime]:
    with _registry_lock:
        runtime = _registered_runtime
        token = _registration_token
        if runtime is None or token is None:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_runtime_not_registered"
            )
        _inflight_by_token[token] = _inflight_by_token.get(token, 0) + 1
    try:
        yield runtime
    finally:
        with _registry_lock:
            if token in _inflight_by_token:
                _inflight_by_token[token] = max(
                    0, _inflight_by_token[token] - 1
                )


def _load_live_session(db: Any, session_id: int) -> Any | None:
    try:
        return (
            # Scalar columns avoid populating SQLAlchemy's identity map before
            # ``tick_live_session`` performs its own locked entity load.  This
            # preliminary classification is deliberately unlocked: a captured
            # PAPER handler receives a content-addressed route token and must
            # revalidate it against the later locked entity before admission.
            db.query(
                TradingAutomationSession.id,
                TradingAutomationSession.symbol,
                TradingAutomationSession.execution_family,
                TradingAutomationSession.risk_snapshot_json,
            )
            .filter(
                TradingAutomationSession.id == int(session_id),
                TradingAutomationSession.mode == "live",
            )
            .one_or_none()
        )
    except CapturedPaperDispatchError:
        raise
    except Exception as exc:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_route_unavailable"
        ) from exc


def _load_ordinary_live_session_for_update(
    db: Any,
    session_id: int,
) -> Any | None:
    """Preserve the existing ordinary runner's locked route classification."""

    try:
        return (
            db.query(
                TradingAutomationSession.id,
                TradingAutomationSession.symbol,
                TradingAutomationSession.execution_family,
                TradingAutomationSession.risk_snapshot_json,
            )
            .filter(
                TradingAutomationSession.id == int(session_id),
                TradingAutomationSession.mode == "live",
            )
            .with_for_update(nowait=True)
            .one_or_none()
        )
    except CapturedPaperDispatchError:
        raise
    except Exception as exc:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_ordinary_route_lock_unavailable"
        ) from exc


def revalidate_captured_paper_route_token(
    token: CapturedPaperRouteToken,
    locked_session: Any,
    runtime: CapturedPaperRuntime,
) -> CapturedPaperRouteToken:
    """Recheck durable route, runtime, and effective settings after row lock.

    The caller is responsible for acquiring the session row in the reviewed
    account-risk order.  This function only validates scalar identity; it does
    not reserve, claim, write, commit, or contact an external system.
    """

    if type(runtime) is not CapturedPaperRuntime:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_route_runtime_invalid"
        )
    try:
        verified = _revalidate_route_token(token, locked_session, runtime)
    except CapturedPaperRouteDriftError as exc:
        raise CapturedPaperRuntimeUnavailableError(exc.reason) from exc
    configured_account_id = str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip()
    configured_mode = str(
        getattr(
            settings,
            "chili_momentum_first_dip_reclaim_policy_mode",
            "baseline",
        )
        or "baseline"
    ).strip().lower()
    if getattr(settings, "chili_alpaca_paper", True) is not True:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_live_cash_execution_prohibited"
        )
    if configured_account_id != token.expected_account_id:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_config_account_mismatch"
        )
    if configured_mode != token.first_dip_policy_mode:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_first_dip_policy_mismatch"
        )
    return verified


def _revalidate_post_commit_runtime(
    request: CapturedPaperPostCommitRequest,
    runtime: CapturedPaperRuntime,
) -> CapturedPaperPostCommitRequest:
    """Bind a committed handoff to the still-current PAPER runtime.

    There is deliberately no database argument here.  The phase-one owner has
    already committed and released its transaction before this check is called;
    the separately registered completion handler owns every fresh read/lock it
    may need.  This boundary only validates immutable route/runtime/config
    identity and therefore cannot accidentally carry an outer row lock across
    broker work.
    """

    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_post_commit_request_invalid"
        )
    try:
        request.verify()
        request.route_token.verify()
    except CapturedPaperIntentContractError as exc:
        raise CapturedPaperRuntimeUnavailableError(exc.reason) from exc
    if type(runtime) is not CapturedPaperRuntime:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_post_commit_runtime_invalid"
        )

    token = request.route_token
    try:
        resolved_config_sha256 = runtime.resolve_config_sha256(token.symbol)
    except CapturedPaperRuntimeUnavailableError as exc:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_post_commit_runtime_provenance_drift"
        ) from exc
    runtime_fields = {
        "account_scope": token.account_scope,
        "expected_account_id": token.expected_account_id,
        "code_build_sha256": token.code_build_sha256,
        "capture_receipt_sha256": token.capture_receipt_sha256,
        "runtime_generation": token.runtime_generation,
        "first_dip_policy_mode": token.first_dip_policy_mode,
    }
    if any(
        getattr(runtime, name, None) != expected
        for name, expected in runtime_fields.items()
    ) or resolved_config_sha256 != token.config_sha256:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_post_commit_runtime_provenance_drift"
        )
    if getattr(settings, "chili_alpaca_paper", None) is not True:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_live_cash_execution_prohibited"
        )
    configured_account_id = str(
        getattr(settings, "chili_alpaca_expected_account_id", "") or ""
    ).strip()
    if configured_account_id != token.expected_account_id:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_config_account_mismatch"
        )
    configured_mode = str(
        getattr(
            settings,
            "chili_momentum_first_dip_reclaim_policy_mode",
            "baseline",
        )
        or "baseline"
    ).strip().lower()
    if configured_mode != token.first_dip_policy_mode:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_first_dip_policy_mismatch"
        )
    if not callable(runtime.post_commit_handler):
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_post_commit_handler_not_registered"
        )
    return request


def dispatch_captured_paper_post_commit(
    request: CapturedPaperPostCommitRequest,
) -> Any:
    """Run phase two only through the runtime that owns the sealed route.

    Callers must invoke this function only *after* their phase-one transaction
    commits.  Its intentionally single-argument API prevents a SQLAlchemy
    session (and its row locks) from leaking across that commit boundary.
    """

    if type(request) is not CapturedPaperPostCommitRequest:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_post_commit_request_invalid"
        )
    with _leased_runtime() as runtime:
        verified = _revalidate_post_commit_runtime(request, runtime)
        handler = runtime.post_commit_handler
        if not callable(handler):  # Defensive against out-of-band mutation.
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_post_commit_handler_not_registered"
            )
        return handler(verified)


def _ordinary_tick(
    db: Any,
    session_id: int,
    *,
    non_paper_tick: Callable[[Any, int], Any] | None,
) -> Any:
    tick = non_paper_tick
    if tick is None:
        # Lazy import preserves the existing monkeypatch/injection seams and
        # avoids a module cycle.  This call is reachable only after the current
        # durable session was proven non-Alpaca.
        from .live_runner import tick_live_session

        tick = tick_live_session
    return tick(db, int(session_id))


def dispatch_live_runner_tick(
    db: Any,
    session_id: int,
    *,
    non_paper_tick: Callable[[Any, int], Any] | None = None,
    captured_paper_only: bool = False,
    expected_account_id: str | None = None,
    expected_runtime_generation: str | None = None,
    expected_execution_family: str | None = None,
) -> Any:
    """Route one current durable session through its only permitted owner.

    The preliminary read is intentional: a stale scheduler/listener hint cannot
    select the ordinary path.  The real FSM subsequently locks and revalidates
    the same row.  If route classification itself is unavailable, no FSM runs.
    """

    if isinstance(session_id, bool) or int(session_id) <= 0:
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_session_id_invalid"
        )
    if captured_paper_only:
        try:
            dedicated_account_id = _canonical_uuid(
                str(expected_account_id or ""),
                field_name="dedicated captured PAPER account id",
            )
            dedicated_generation = _canonical_uuid(
                str(expected_runtime_generation or ""),
                field_name="dedicated captured PAPER runtime generation",
            )
        except ValueError as exc:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_dedicated_scope_invalid"
            ) from exc
        dedicated_family = normalize_execution_family(
            expected_execution_family
        )
        if dedicated_family != EXECUTION_FAMILY_ALPACA_SPOT:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_dedicated_scope_invalid"
            )
    else:
        dedicated_account_id = None
        dedicated_generation = None
        dedicated_family = None
    sess = _load_live_session(db, int(session_id))
    if sess is None:
        # An unknown/deleted row has no execution-family proof.  Do not race a
        # same-id replacement into the ordinary path; the owning caller rolls
        # back and a later inventory refresh removes the stale dispatch hint.
        raise CapturedPaperRuntimeUnavailableError(
            "captured_paper_live_session_not_found"
        )

    family = normalize_execution_family(getattr(sess, "execution_family", None))
    if family not in {EXECUTION_FAMILY_ALPACA_SPOT, EXECUTION_FAMILY_ALPACA_SHORT}:
        if captured_paper_only:
            # The dedicated fake-money process has no ordinary-runner escape
            # hatch.  A foreign runnable session is an inventory breach, not a
            # reason to invoke a legacy strategy/broker path.
            raise CapturedPaperExecutionProhibitedError(
                "captured_paper_dedicated_foreign_execution_family"
            )
        # Preserve the ordinary path's pre-existing row-lock ownership while
        # keeping the preliminary route read unlocked for captured PAPER.  If
        # the row changed between reads, never route a newly-Alpaca session into
        # the bare runner.
        locked = _load_ordinary_live_session_for_update(db, int(session_id))
        if locked is None:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_ordinary_route_drift"
            )
        locked_family = normalize_execution_family(
            getattr(locked, "execution_family", None)
        )
        locked_symbol = str(getattr(locked, "symbol", "") or "").strip().upper()
        preliminary_symbol = str(
            getattr(sess, "symbol", "") or ""
        ).strip().upper()
        if (
            int(getattr(locked, "id", 0) or 0) != int(session_id)
            or locked_family != family
            or locked_symbol != preliminary_symbol
            or locked_family
            in {EXECUTION_FAMILY_ALPACA_SPOT, EXECUTION_FAMILY_ALPACA_SHORT}
        ):
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_ordinary_route_drift"
            )
        return _ordinary_tick(
            db,
            int(session_id),
            non_paper_tick=non_paper_tick,
        )

    # Short and live-cash routes have no authority through this fake-money
    # dispatcher.  These checks precede the runtime callback by construction.
    if family == EXECUTION_FAMILY_ALPACA_SHORT:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_short_execution_not_certified"
        )
    if getattr(settings, "chili_alpaca_paper", True) is not True:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_live_cash_execution_prohibited"
        )

    symbol = str(getattr(sess, "symbol", "") or "").strip().upper()
    if not symbol or "-" in symbol or "/" in symbol:
        raise CapturedPaperExecutionProhibitedError(
            "captured_paper_non_equity_execution_prohibited"
        )
    snapshot = getattr(sess, "risk_snapshot_json", None)
    snapshot = snapshot if isinstance(snapshot, dict) else {}

    with _leased_runtime() as runtime:
        if captured_paper_only and (
            runtime.account_scope != _ACCOUNT_SCOPE
            or runtime.expected_account_id != dedicated_account_id
            or runtime.runtime_generation != dedicated_generation
            or family != dedicated_family
        ):
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_dedicated_runtime_scope_mismatch"
            )
        configured_account_id = str(
            getattr(settings, "chili_alpaca_expected_account_id", "") or ""
        ).strip()
        configured_mode = str(
            getattr(
                settings,
                "chili_momentum_first_dip_reclaim_policy_mode",
                "baseline",
            )
            or "baseline"
        ).strip().lower()
        if configured_account_id != runtime.expected_account_id:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_config_account_mismatch"
            )
        if configured_mode != runtime.first_dip_policy_mode:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_first_dip_policy_mismatch"
            )
        if str(snapshot.get("alpaca_account_scope") or "").strip() != _ACCOUNT_SCOPE:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_session_account_scope_mismatch"
            )
        if str(snapshot.get("alpaca_account_id") or "").strip() != runtime.expected_account_id:
            raise CapturedPaperRuntimeUnavailableError(
                "captured_paper_session_account_id_mismatch"
            )
        if captured_paper_only:
            generation_claims: list[str] = []
            for container in (
                snapshot,
                snapshot.get("momentum_live_execution"),
                snapshot.get("captured_paper_admission"),
            ):
                if isinstance(container, Mapping):
                    claimed = str(
                        container.get("captured_paper_runtime_generation")
                        or container.get("runtime_generation")
                        or ""
                    ).strip()
                    if claimed:
                        generation_claims.append(claimed)
            if any(claim != dedicated_generation for claim in generation_claims):
                raise CapturedPaperRuntimeUnavailableError(
                    "captured_paper_session_runtime_generation_mismatch"
                )

        request = CapturedPaperDispatchRequest(
            session_id=int(session_id),
            symbol=symbol,
            execution_family=family,
            account_scope=runtime.account_scope,
            expected_account_id=runtime.expected_account_id,
            code_build_sha256=runtime.code_build_sha256,
            config_sha256=runtime.resolve_config_sha256(symbol),
            capture_receipt_sha256=runtime.capture_receipt_sha256,
            runtime_generation=runtime.runtime_generation,
            first_dip_policy_mode=runtime.first_dip_policy_mode,
        )
        request.verify()
        # This unlocked check is only an early rejection.  ``tick_live_session``
        # repeats it after its own ``FOR UPDATE`` so another process cannot
        # swap/tamper the durable owner marker between route classification and
        # the FSM boundary.
        durable_owner = revalidate_captured_paper_session_owner(
            sess,
            request=request,
            require_decision_context=False,
        )
        if captured_paper_only and durable_owner is None:
            # A dedicated first tick may start only from the exact atomic
            # PENDING_OWNER generation.  Generic owner-less Alpaca rows never
            # inherit this process's runtime merely because their route fields
            # happen to match.  The handler repeats the pending proof under
            # account/claim/session locks and installs the final owner before
            # invoking the FSM.
            from .captured_paper_pending_owner import (
                validate_captured_paper_pending_owner_inventory,
            )

            validate_captured_paper_pending_owner_inventory(
                sess,
                expected_account_id=dedicated_account_id,
                expected_runtime_generation=dedicated_generation,
                expected_execution_family=dedicated_family,
            )
        # Fence every registered captured-runtime callback from silently
        # falling through to the legacy Alpaca sizing/order path.  The handler
        # must install the exact phase-zero selection context while it invokes
        # the FSM; if it mistakenly calls the bare runner, the pre-sizing hook
        # sees REQUIRED-without-active and defers the decision.  Lazy import
        # avoids the intentional selection-contract -> dispatcher type cycle.
        from .captured_paper_selection import require_captured_paper_selection

        with (
            require_captured_paper_selection(request),
            _activate_session_owner_request(request),
        ):
            return runtime.handler(db, request)


def dispatch_captured_paper_live_runner_tick(
    db: Any,
    session_id: int,
    *,
    expected_account_id: str,
    expected_runtime_generation: str,
    expected_execution_family: str = EXECUTION_FAMILY_ALPACA_SPOT,
) -> Any:
    """Dispatch one tick with no ordinary or foreign-session fallback.

    This is the only tick boundary used by the dedicated captured Alpaca PAPER
    service.  The ordinary scheduler/event-loop dispatcher remains available
    to the rest of CHILI, but it is deliberately unreachable from this path.
    """

    return dispatch_live_runner_tick(
        db,
        session_id,
        captured_paper_only=True,
        expected_account_id=expected_account_id,
        expected_runtime_generation=expected_runtime_generation,
        expected_execution_family=expected_execution_family,
    )
