"""Atomic adaptive-risk reservation foundation (no broker transport).

This module owns only economic admission durability.  It never places, cancels,
or reconciles an external order and it does not enable any runner.  Every public
mutation opens one short transaction, takes the same account-scoped PostgreSQL
advisory lock, and updates a hash-chained append-only audit beside the mutable
reservation projection.

The once-per-ET-day opportunity is deliberately two-phase:

* admission makes it ``reserved``;
* only a confirmed non-zero cumulative broker fill makes it ``consumed``;
* an unambiguous zero-fill terminal outcome makes it ``available`` again;
* ``submit_indeterminate`` cannot be released by timeout or inference.

All quantity/risk math comes from ``AdaptiveRiskInputs`` v2 and the pure shared
resolver.  There are no activation-only dollar caps or fixed symbol-count caps.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict, dataclass, field, is_dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_EVEN
import hashlib
import hmac
import json
import math
import re
import secrets
from typing import Any, Callable, Iterator, Mapping
import uuid
from zoneinfo import ZoneInfo

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session, sessionmaker

from app.models.trading import (
    AdaptiveRiskDecisionPacket,
    AdaptiveRiskOpportunityClaim,
    AdaptiveRiskOpportunityEvent,
    AdaptiveRiskReservation,
    AdaptiveRiskReservationEvent,
    AlpacaPaperAccountSettlementHead,
    AlpacaPaperBuyingPowerReflectionItem,
    AlpacaPaperBuyingPowerReflectionReceipt,
    AlpacaPaperCycleSettlement,
    AlpacaPaperFillActivity,
    AlpacaPaperFillObservationActivity,
    AlpacaPaperFillQueryObservation,
    AlpacaPaperPostSettlementFillContradiction,
    AlpacaPaperTerminalFillObservationReceipt,
    BrokerSymbolActionClaim,
    CapturedPaperPostCommitOutbox,
    TradingAutomationSession,
    TradingAutomationSimulatedFill,
)

from .adaptive_risk_policy import (
    AdaptiveRiskContractError,
    AdaptiveRiskInputs,
    AdaptiveRiskPolicy,
    ResolvedAdaptiveRisk,
    RiskInputEvidence,
    load_and_verify_adaptive_risk_decision_packet,
    resolve_adaptive_risk,
)
from .adaptive_risk_account_lock import acquire_adaptive_risk_account_locks
from .alpaca_cycle_settlement import (
    SETTLED_DAILY_PNL_EVIDENCE_SOURCE,
    AlpacaCycleSettlementIntegrityError,
    AlpacaPaperSettledDailyPnlEvidence,
    new_zero_settlement_head,
    verify_cycle_settlement_content,
    verify_settlement_head_content,
)
from .captured_alpaca_paper_adapter import (
    CapturedAlpacaPaperAccountAuthority,
    CapturedAlpacaPaperReadError,
    verify_captured_alpaca_paper_account_authority,
)
from .alpaca_fill_activity import (
    AlpacaFillActivityError,
    POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND,
    verify_alpaca_paper_fill_activity_row,
    verify_alpaca_paper_post_settlement_fill_contradiction_row,
    verify_alpaca_paper_terminal_fill_observation_receipt,
)
from .alpaca_buying_power_reflection import (
    AlpacaBuyingPowerReflectionError,
    PreparedAlpacaPaperBuyingPowerDoubleCensus,
    verify_alpaca_paper_buying_power_double_census,
)


UTC = timezone.utc
ET = ZoneInfo("America/New_York")
FOUNDATION_SCHEMA_VERSION = "chili.adaptive-risk-reservation-foundation.v1"
RESERVATION_REQUEST_SCHEMA_VERSION = (
    "chili.adaptive-risk-reservation-request.v2"
)
RESERVATION_LEDGER_GENERATION = "adaptive-risk-reservation-ledger.v1"
LOCKED_ADMISSION_SNAPSHOT_SCHEMA_VERSION = (
    "chili.adaptive-risk-locked-admission-snapshot.v2"
)
_MUTABLE_RESERVATION_EXECUTION_SURFACES = frozenset(
    {"alpaca_paper", "db_paper"}
)
_ADVISORY_LOCK_NAMESPACE = 0x4152  # ``AR``; second key is hashtext(account_scope).
_LOCKED_ADMISSION_RECEIPTS_SESSION_KEY = (
    "chili.adaptive-risk-locked-admission-receipts.v1"
)
_LOCKED_ALPACA_PAPER_BUNDLE_RECEIPTS_SESSION_KEY = (
    "chili.alpaca-paper-locked-admission-bundle-receipts.v1"
)
_LOCKED_ALPACA_PAPER_ADMISSION_KEY = secrets.token_bytes(32)
_LOCKED_ALPACA_PAPER_ADMISSION_TOKEN = object()
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_MONEY_QUANTUM = Decimal("0.0000000001")
_ALPACA_OPEN_ORDER_STATUSES = frozenset(
    {
        "accepted",
        "accepted_for_bidding",
        "calculated",
        "done_for_day",
        "held",
        "new",
        "partially_filled",
        "pending_cancel",
        "pending_new",
        "pending_replace",
        "pending_review",
        "stopped",
        "suspended",
    }
)
_SAFE_ZERO_RELEASE_REASONS = frozenset(
    {
        "pre_post_release",
        "broker_rejected",
        "broker_canceled",
        "broker_expired",
        "confirmed_zero_fill",
    }
)
_LIFECYCLE_EVENT_KINDS = frozenset(
    {
        "order_accepted",
        "cumulative_fill",
        "terminal_zero_fill",
        "filled_entry_terminal",
        "position_reduced",
        "position_flat",
    }
)
_LIFECYCLE_DURABILITY_KINDS = frozenset(
    {
        "authoritative_broker_event",
        "committed_alpaca_paper_fill",
        POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND,
        "committed_db_paper_fill",
    }
)
_DB_PAPER_FILL_TABLE = "trading_automation_simulated_fills"
_ALPACA_PAPER_FILL_TABLE = "alpaca_paper_fill_activities"
_ALPACA_POST_SETTLEMENT_FILL_TABLE = (
    "alpaca_paper_post_settlement_fill_contradictions"
)
_RISK_LEDGER_AGGREGATE_FIELDS = frozenset(
    {
        "open_structural_risk_usd",
        "pending_reserved_risk_usd",
        "existing_same_symbol_structural_risk_usd",
        "pending_same_symbol_structural_risk_usd",
        "current_cluster_structural_risk_usd",
        "pending_correlation_cluster_risk_usd",
        "portfolio_gross_notional_usd",
        "pending_portfolio_gross_notional_usd",
        "open_buying_power_impact_usd",
        "pending_buying_power_impact_usd",
    }
)


class AdaptiveReservationError(RuntimeError):
    """Base class for a durable reservation contract violation."""


class AdaptiveReservationIdempotencyConflict(AdaptiveReservationError):
    """The same immutable id was reused with different economics/provenance."""


class AdaptiveReservationStateConflict(AdaptiveReservationError):
    """A lifecycle transition contradicts already-durable broker truth."""


class AdaptiveRiskPendingSettlement(AdaptiveRiskContractError):
    """A flat PAPER cycle has unresolved net economics for this account."""

    reason = "adaptive_risk_pending_cycle_settlement"

    def __init__(
        self,
        *,
        account_scope: str,
        pending_settlements: list[Mapping[str, Any]],
        locked_snapshot: "LockedAdaptiveRiskAdmissionSnapshot | None" = None,
    ) -> None:
        normalized = _json_safe(list(pending_settlements))
        self.account_scope = str(account_scope)
        self.pending_settlements = tuple(normalized)
        self.locked_snapshot = locked_snapshot
        self.provenance = {
            "schema_version": "chili.adaptive-risk-pending-settlement.v1",
            "reason": self.reason,
            "account_scope": self.account_scope,
            "pending_count": len(normalized),
            "pending_settlements": normalized,
            "ledger_sha256": (
                locked_snapshot.ledger_sha256
                if locked_snapshot is not None
                else None
            ),
            "observed_at": (
                locked_snapshot.observed_at
                if locked_snapshot is not None
                else None
            ),
        }
        super().__init__(
            f"{self.reason}: account_scope={self.account_scope}; "
            f"pending_count={len(normalized)}"
        )


class AdaptiveRiskExposureQuarantined(AdaptiveRiskContractError):
    """New entry is forbidden while contradictory broker exposure is unresolved."""

    reason = "adaptive_risk_exposure_quarantined"

    def __init__(
        self,
        *,
        account_scope: str,
        quarantined_exposures: list[Mapping[str, Any]],
        locked_snapshot: "LockedAdaptiveRiskAdmissionSnapshot | None" = None,
    ) -> None:
        normalized = _json_safe(list(quarantined_exposures))
        self.account_scope = str(account_scope)
        self.quarantined_exposures = tuple(normalized)
        self.locked_snapshot = locked_snapshot
        self.provenance = {
            "schema_version": "chili.adaptive-risk-exposure-quarantine.v1",
            "reason": self.reason,
            "account_scope": self.account_scope,
            "quarantined_count": len(normalized),
            "quarantined_exposures": normalized,
            "ledger_sha256": (
                locked_snapshot.ledger_sha256
                if locked_snapshot is not None
                else None
            ),
            "observed_at": (
                locked_snapshot.observed_at
                if locked_snapshot is not None
                else None
            ),
        }
        super().__init__(
            f"{self.reason}: account_scope={self.account_scope}; "
            f"quarantined_count={len(normalized)}"
        )


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise AdaptiveRiskContractError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _iso(value: datetime) -> str:
    return _utc(value, "datetime").isoformat().replace("+00:00", "Z")


def _parse_utc(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        return _utc(value, field)
    if not isinstance(value, str) or not value.strip():
        raise AdaptiveRiskContractError(f"{field} must be ISO-8601 text")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdaptiveRiskContractError(f"{field} must be ISO-8601 text") from exc
    return _utc(parsed, field)


def _canonical_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, uuid.UUID):
        return str(value)
    if is_dataclass(value):
        return asdict(value)
    raise TypeError(f"not JSON serializable: {type(value).__name__}")


def _canonical_json(value: Any) -> bytes:
    try:
        return json.dumps(
            value,
            default=_canonical_default,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        ).encode("utf-8")
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(
            f"non-canonical adaptive reservation payload: {exc}"
        ) from exc


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _json_safe(value: Any) -> Any:
    return json.loads(_canonical_json(value).decode("utf-8"))


def _sha(value: Any) -> bool:
    return _SHA256_RE.fullmatch(str(value or "").strip().lower()) is not None


def _finite(value: Any) -> bool:
    return (
        isinstance(value, (int, float, Decimal))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _money(value: Any) -> Decimal:
    if not _finite(value):
        raise AdaptiveRiskContractError("economic reservation value must be finite")
    return Decimal(str(value)).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)


def _norm(value: Any, field: str, *, upper: bool = False, lower: bool = False) -> str:
    result = str(value or "").strip()
    if not result:
        raise AdaptiveRiskContractError(f"{field} is required")
    if upper:
        result = result.upper()
    if lower:
        result = result.lower()
    return result


def _optional_norm(
    value: Any,
    *,
    upper: bool = False,
    lower: bool = False,
) -> str | None:
    result = str(value or "").strip()
    if not result:
        return None
    if upper:
        result = result.upper()
    if lower:
        result = result.lower()
    return result


def _exact_nonnegative_decimal(value: Any, field: str) -> Decimal:
    if value is None or isinstance(value, bool):
        raise AdaptiveRiskContractError(f"{field} is unavailable")
    try:
        result = Decimal(str(value))
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(f"{field} is invalid") from exc
    if not result.is_finite() or result < 0:
        raise AdaptiveRiskContractError(f"{field} is invalid")
    return result


def _exact_nonnegative_integer(value: Any, field: str) -> int:
    result = _exact_nonnegative_decimal(value, field)
    if result != result.to_integral_value():
        raise AdaptiveRiskContractError(f"{field} is not whole-share truth")
    return int(result)


@dataclass(frozen=True)
class ImmutableAccountRiskSnapshot:
    """One broker/account generation supplying every account-risk fact.

    Requiring the account and daily-PnL evidence to name this same content hash
    prevents an Alpaca decision from silently inheriting a Robinhood loss cap,
    equity snapshot, or stale provider generation.
    """

    snapshot_id: str
    source: str
    provider_generation: str
    account_scope: str
    execution_family: str
    broker_environment: str
    venue: str
    account_identity_sha256: str
    observed_at: datetime
    available_at: datetime
    equity_usd: Decimal
    buying_power_usd: Decimal
    broker_day_change_usd: Decimal
    local_realized_pnl_usd: float
    pending_policy_buying_power_reflected_usd: float

    def __post_init__(self) -> None:
        for field in (
            "snapshot_id",
            "source",
            "provider_generation",
            "account_scope",
        ):
            object.__setattr__(self, field, _norm(getattr(self, field), field))
        for field in ("execution_family", "broker_environment", "venue"):
            object.__setattr__(
                self, field, _norm(getattr(self, field), field, lower=True)
            )
        identity = str(self.account_identity_sha256 or "").strip().lower()
        if not _sha(identity):
            raise AdaptiveRiskContractError(
                "account_snapshot.account_identity_sha256 must be lowercase SHA256"
            )
        object.__setattr__(self, "account_identity_sha256", identity)
        observed = _utc(self.observed_at, "account_snapshot.observed_at")
        available = _utc(self.available_at, "account_snapshot.available_at")
        if available < observed:
            raise AdaptiveRiskContractError(
                "account_snapshot.available_at cannot precede observed_at"
            )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        for field in (
            "equity_usd",
            "buying_power_usd",
            "broker_day_change_usd",
            "local_realized_pnl_usd",
            "pending_policy_buying_power_reflected_usd",
        ):
            value = getattr(self, field)
            if not _finite(value):
                raise AdaptiveRiskContractError(f"account_snapshot.{field} must be finite")
            object.__setattr__(self, field, float(value))
        if self.equity_usd <= 0:
            raise AdaptiveRiskContractError("account_snapshot.equity_usd must be positive")
        if self.buying_power_usd < 0:
            raise AdaptiveRiskContractError(
                "account_snapshot.buying_power_usd must be non-negative"
            )
        if self.pending_policy_buying_power_reflected_usd < 0:
            raise AdaptiveRiskContractError(
                "account_snapshot.pending_policy_buying_power_reflected_usd "
                "must be non-negative"
            )

    @property
    def snapshot_sha256(self) -> str:
        return _sha256_json(asdict(self))

    def to_payload(self) -> dict[str, Any]:
        payload = _json_safe(asdict(self))
        payload["snapshot_sha256"] = self.snapshot_sha256
        return payload


@dataclass(frozen=True)
class AlpacaPaperBrokerAccountFacts:
    """Broker-only account facts captured before entering database locks.

    Local realized P&L is deliberately absent.  The locked admission method
    derives it exclusively from the verified append-only settlement chain and
    constructs ``ImmutableAccountRiskSnapshot`` internally.
    """

    snapshot_id: str
    source: str
    provider_generation: str
    account_identity_sha256: str
    observed_at: datetime
    available_at: datetime
    equity_usd: float
    buying_power_usd: float
    broker_day_change_usd: float
    capture_authority: CapturedAlpacaPaperAccountAuthority
    account_scope: str = "alpaca:paper"
    execution_family: str = "alpaca_spot"
    broker_environment: str = "paper"
    venue: str = "alpaca"

    def __post_init__(self) -> None:
        for field in ("snapshot_id", "source", "provider_generation"):
            object.__setattr__(self, field, _norm(getattr(self, field), field))
        expected = {
            "account_scope": "alpaca:paper",
            "execution_family": "alpaca_spot",
            "broker_environment": "paper",
            "venue": "alpaca",
        }
        for field, required in expected.items():
            normalized = _norm(getattr(self, field), field, lower=True)
            if normalized != required:
                raise AdaptiveRiskContractError(
                    f"broker_account_facts.{field} is not Alpaca PAPER"
                )
            object.__setattr__(self, field, normalized)
        identity = str(self.account_identity_sha256 or "").strip().lower()
        if not _sha(identity):
            raise AdaptiveRiskContractError(
                "broker_account_facts.account_identity_sha256 is invalid"
            )
        object.__setattr__(self, "account_identity_sha256", identity)
        observed = _utc(self.observed_at, "broker_account_facts.observed_at")
        available = _utc(self.available_at, "broker_account_facts.available_at")
        if available < observed:
            raise AdaptiveRiskContractError(
                "broker account availability precedes observation"
            )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        for field in (
            "equity_usd",
            "buying_power_usd",
            "broker_day_change_usd",
        ):
            value = getattr(self, field)
            if type(value) is not Decimal or not value.is_finite():
                raise AdaptiveRiskContractError(
                    f"broker_account_facts.{field} must be exact Decimal"
                )
        if self.equity_usd <= 0 or self.buying_power_usd < 0:
            raise AdaptiveRiskContractError(
                "broker account equity/buying power is out of range"
            )
        try:
            authority = verify_captured_alpaca_paper_account_authority(
                self.capture_authority
            )
        except CapturedAlpacaPaperReadError as exc:
            raise AdaptiveRiskContractError(
                "broker account facts lack capture-issued authority"
            ) from exc
        expected_authority = {
            "snapshot_id": (self.snapshot_id, authority.snapshot_id),
            "source": (self.source, authority.source),
            "provider_generation": (
                self.provider_generation,
                authority.provider_generation,
            ),
            "account_identity_sha256": (
                self.account_identity_sha256,
                authority.account_identity_sha256,
            ),
            "observed_at": (observed, authority.observed_at),
            "available_at": (available, authority.available_at),
            "equity_usd": (
                self.equity_usd,
                authority.equity_usd,
            ),
            "buying_power_usd": (
                self.buying_power_usd,
                authority.buying_power_usd,
            ),
            "broker_day_change_usd": (
                self.broker_day_change_usd,
                authority.broker_day_change_usd,
            ),
        }
        changed = sorted(
            name
            for name, (actual, required) in expected_authority.items()
            if actual != required
        )
        if changed:
            raise AdaptiveRiskContractError(
                "broker account facts differ from capture authority: "
                + ",".join(changed)
            )

    @classmethod
    def from_capture_authority(
        cls,
        authority: CapturedAlpacaPaperAccountAuthority,
    ) -> "AlpacaPaperBrokerAccountFacts":
        """Project only capture-bound broker facts into the locked store."""

        try:
            verified = verify_captured_alpaca_paper_account_authority(authority)
        except CapturedAlpacaPaperReadError as exc:
            raise AdaptiveRiskContractError(
                "broker account capture authority is invalid"
            ) from exc
        return cls(
            snapshot_id=verified.snapshot_id,
            source=verified.source,
            provider_generation=verified.provider_generation,
            account_identity_sha256=verified.account_identity_sha256,
            observed_at=verified.observed_at,
            available_at=verified.available_at,
            equity_usd=verified.equity_usd,
            buying_power_usd=verified.buying_power_usd,
            broker_day_change_usd=verified.broker_day_change_usd,
            capture_authority=verified,
        )

    @property
    def account_evidence(self) -> RiskInputEvidence:
        authority = verify_captured_alpaca_paper_account_authority(
            self.capture_authority
        )
        return RiskInputEvidence(
            source=self.source,
            observed_at=self.observed_at,
            available_at=self.available_at,
            content_sha256=authority.account_payload_sha256,
            provider_generation=self.provider_generation,
        )

    @property
    def broker_facts_sha256(self) -> str:
        return _sha256_json(
            {
                "snapshot_id": self.snapshot_id,
                "source": self.source,
                "provider_generation": self.provider_generation,
                "account_identity_sha256": self.account_identity_sha256,
                "observed_at": self.observed_at,
                "available_at": self.available_at,
                "account_evidence": asdict(self.account_evidence),
                "equity_usd": self.equity_usd,
                "buying_power_usd": self.buying_power_usd,
                "broker_day_change_usd": self.broker_day_change_usd,
                "capture_authority_sha256": (
                    self.capture_authority.authority_sha256
                ),
            }
        )


@dataclass(frozen=True)
class AdaptiveRiskOpportunityKey:
    """Canonical captured opportunity identity carried into reservation.

    The decision clock owns ``trading_date``.  Database wall time is deliberately
    absent from this content-addressed key; the store may observe DB time only to
    reject a decision that claims to come from the future.
    """

    account_scope: str
    symbol: str
    trading_date: date
    setup_family: str

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "account_scope",
            _norm(self.account_scope, "opportunity_key.account_scope"),
        )
        object.__setattr__(
            self,
            "symbol",
            _norm(self.symbol, "opportunity_key.symbol").upper(),
        )
        object.__setattr__(
            self,
            "setup_family",
            _norm(
                self.setup_family,
                "opportunity_key.setup_family",
                lower=True,
            ),
        )
        trading_date = self.trading_date
        if isinstance(trading_date, str):
            try:
                trading_date = date.fromisoformat(trading_date)
            except ValueError as exc:
                raise AdaptiveRiskContractError(
                    "opportunity_key.trading_date must be an ISO-8601 date"
                ) from exc
        if isinstance(trading_date, datetime) or not isinstance(trading_date, date):
            raise AdaptiveRiskContractError(
                "opportunity_key.trading_date must be an ISO-8601 date"
            )
        object.__setattr__(self, "trading_date", trading_date)

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> "AdaptiveRiskOpportunityKey":
        if not isinstance(payload, Mapping):
            raise AdaptiveRiskContractError("opportunity_key must be a mapping")
        raw = dict(payload)
        required = {"account_scope", "symbol", "trading_date", "setup_family"}
        if set(raw) != required:
            missing = sorted(required - set(raw))
            unexpected = sorted(set(raw) - required)
            details = []
            if missing:
                details.append("missing=" + ",".join(missing))
            if unexpected:
                details.append("unexpected=" + ",".join(unexpected))
            raise AdaptiveRiskContractError(
                "opportunity_key fields are invalid: " + ";".join(details)
            )
        return cls(
            account_scope=raw["account_scope"],
            symbol=raw["symbol"],
            trading_date=raw["trading_date"],
            setup_family=raw["setup_family"],
        )

    def to_payload(self) -> dict[str, str]:
        return {
            "account_scope": self.account_scope,
            "symbol": self.symbol,
            "trading_date": self.trading_date.isoformat(),
            "setup_family": self.setup_family,
        }

    @property
    def key_sha256(self) -> str:
        return _sha256_json(self.to_payload())


@dataclass(frozen=True)
class AdaptiveRiskReservationRequest:
    policy: AdaptiveRiskPolicy
    inputs: AdaptiveRiskInputs
    account_snapshot: ImmutableAccountRiskSnapshot
    account_scope: str
    setup_family: str
    correlation_cluster: str
    client_order_id: str
    entry_limit_price: float
    opportunity_key: AdaptiveRiskOpportunityKey | Mapping[str, Any] | None = None
    broker_account_evidence: RiskInputEvidence | None = None
    settled_daily_pnl_evidence: RiskInputEvidence | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "account_scope", _norm(self.account_scope, "account_scope")
        )
        object.__setattr__(
            self,
            "setup_family",
            _norm(self.setup_family, "setup_family", lower=True),
        )
        object.__setattr__(
            self,
            "correlation_cluster",
            _norm(self.correlation_cluster, "correlation_cluster", lower=True),
        )
        object.__setattr__(
            self, "client_order_id", _norm(self.client_order_id, "client_order_id")
        )
        if not _finite(self.entry_limit_price) or float(self.entry_limit_price) <= 0:
            raise AdaptiveRiskContractError("entry_limit_price must be finite and positive")
        object.__setattr__(self, "entry_limit_price", float(self.entry_limit_price))
        opportunity_key = self.opportunity_key
        if isinstance(opportunity_key, Mapping):
            opportunity_key = AdaptiveRiskOpportunityKey.from_payload(opportunity_key)
        if opportunity_key is not None and not isinstance(
            opportunity_key, AdaptiveRiskOpportunityKey
        ):
            raise AdaptiveRiskContractError("opportunity_key must be canonical")
        if self.setup_family == "first_dip_reclaim" and opportunity_key is None:
            raise AdaptiveRiskContractError(
                "first_dip_reclaim requires a captured opportunity_key"
            )
        if opportunity_key is not None:
            expected = {
                "account_scope": self.account_scope,
                "symbol": self.inputs.symbol.upper(),
                "trading_date": self.inputs.as_of.astimezone(ET).date().isoformat(),
                "setup_family": self.setup_family,
            }
            actual = opportunity_key.to_payload()
            changed = sorted(
                name
                for name, expected_value in expected.items()
                if actual.get(name) != expected_value
            )
            if changed:
                raise AdaptiveRiskContractError(
                    "opportunity_key does not match captured decision: "
                    + ",".join(changed)
                )
        object.__setattr__(self, "opportunity_key", opportunity_key)
        for name in (
            "broker_account_evidence",
            "settled_daily_pnl_evidence",
        ):
            value = getattr(self, name)
            if value is not None and type(value) is not RiskInputEvidence:
                raise AdaptiveRiskContractError(
                    f"{name} must be typed risk-input evidence"
                )

    def _payload_body(self) -> dict[str, Any]:
        payload = {
            "schema_version": RESERVATION_REQUEST_SCHEMA_VERSION,
            "policy": asdict(self.policy),
            "inputs": asdict(self.inputs),
            "account_snapshot": self.account_snapshot.to_payload(),
            "account_scope": self.account_scope,
            "setup_family": self.setup_family,
            "correlation_cluster": self.correlation_cluster,
            "client_order_id": self.client_order_id,
            "entry_limit_price": self.entry_limit_price,
            "opportunity_key": (
                self.opportunity_key.to_payload() if self.opportunity_key else None
            ),
            "opportunity_key_sha256": (
                self.opportunity_key.key_sha256 if self.opportunity_key else None
            ),
        }
        if self.broker_account_evidence is not None:
            payload["broker_account_evidence"] = asdict(
                self.broker_account_evidence
            )
        if self.settled_daily_pnl_evidence is not None:
            payload["settled_daily_pnl_evidence"] = asdict(
                self.settled_daily_pnl_evidence
            )
        return payload

    @property
    def request_sha256(self) -> str:
        return _sha256_json(self._payload_body())

    def to_payload(self) -> dict[str, Any]:
        payload = _json_safe(self._payload_body())
        payload["request_sha256"] = self.request_sha256
        return payload


def adaptive_risk_reservation_request_rejections(
    request: AdaptiveRiskReservationRequest,
) -> tuple[str, ...]:
    """Return intrinsic request/snapshot mismatches without reading mutable state."""

    inputs = request.inputs
    snapshot = request.account_snapshot
    reasons: list[str] = []
    exact_text = {
        "account_scope": (request.account_scope, snapshot.account_scope),
        "execution_family": (inputs.execution_family, snapshot.execution_family),
        "broker_environment": (
            inputs.broker_environment,
            snapshot.broker_environment,
        ),
        "venue": (inputs.venue, snapshot.venue),
        "account_identity_sha256": (
            inputs.account_identity_sha256,
            snapshot.account_identity_sha256,
        ),
    }
    for name, (actual, expected) in exact_text.items():
        if str(actual) != str(expected):
            reasons.append(f"account_snapshot_mismatch:{name}")
    exact_numbers = {
        "equity_usd": (inputs.equity_usd, snapshot.equity_usd),
        "buying_power_usd": (
            inputs.buying_power_usd,
            snapshot.buying_power_usd,
        ),
        "broker_day_change_usd": (
            inputs.broker_day_change_usd,
            snapshot.broker_day_change_usd,
        ),
        "local_realized_pnl_usd": (
            inputs.local_realized_pnl_usd,
            snapshot.local_realized_pnl_usd,
        ),
    }
    for name, (actual, expected) in exact_numbers.items():
        if not _finite(actual) or Decimal(str(actual)) != Decimal(str(expected)):
            reasons.append(f"account_snapshot_mismatch:{name}")
    if inputs.correlation_cluster_id != request.correlation_cluster:
        reasons.append("reservation_context_mismatch:correlation_cluster")

    has_bound_paper_evidence = (
        request.broker_account_evidence is not None
        or request.settled_daily_pnl_evidence is not None
    )
    if inputs.execution_surface == "alpaca_paper" and has_bound_paper_evidence:
        expected_evidence = {
            "account": request.broker_account_evidence,
            "daily_pnl": request.settled_daily_pnl_evidence,
        }
        for evidence_name, expected in expected_evidence.items():
            actual = inputs.evidence.get(evidence_name)
            if type(expected) is not RiskInputEvidence:
                reasons.append(
                    f"account_snapshot_evidence_missing:{evidence_name}"
                )
            elif actual != expected:
                reasons.append(
                    f"account_snapshot_evidence_mismatch:{evidence_name}:authority"
                )
        if request.broker_account_evidence == request.settled_daily_pnl_evidence:
            reasons.append("account_snapshot_evidence_mismatch:authority_alias")
    else:
        # Legacy/source-only PAPER material remains loadable for diagnostics,
        # sealed-fixture reconstruction and final-tape mechanics.  It cannot
        # enter the mutable PAPER store: reserve() independently requires the
        # process-private locked bundle and exact split authorities.
        for evidence_name in ("account", "daily_pnl"):
            evidence = inputs.evidence.get(evidence_name)
            if not isinstance(evidence, RiskInputEvidence):
                reasons.append(f"account_snapshot_evidence_missing:{evidence_name}")
                continue
            if evidence.source != snapshot.source:
                reasons.append(
                    f"account_snapshot_evidence_mismatch:{evidence_name}:source"
                )
            if evidence.provider_generation != snapshot.provider_generation:
                reasons.append(
                    f"account_snapshot_evidence_mismatch:{evidence_name}:generation"
                )
            if evidence.content_sha256 != snapshot.snapshot_sha256:
                reasons.append(
                    f"account_snapshot_evidence_mismatch:{evidence_name}:content"
                )
            if evidence.observed_at != snapshot.observed_at:
                reasons.append(
                    f"account_snapshot_evidence_mismatch:{evidence_name}:observed_at"
                )
            if evidence.available_at != snapshot.available_at:
                reasons.append(
                    f"account_snapshot_evidence_mismatch:{evidence_name}:available_at"
                )
    return tuple(reasons)


def validate_adaptive_risk_reservation_request(
    request: AdaptiveRiskReservationRequest,
) -> AdaptiveRiskReservationRequest:
    """Fail closed on intrinsic request mismatches shared by replay and runtime."""

    reasons = adaptive_risk_reservation_request_rejections(request)
    if reasons:
        raise AdaptiveRiskContractError(
            "adaptive risk reservation request boundary mismatch: "
            + ",".join(reasons)
        )
    return request


def load_adaptive_risk_reservation_request(
    payload: Mapping[str, Any],
) -> AdaptiveRiskReservationRequest:
    """Strictly rebuild one content-addressed runtime reservation request."""

    if not isinstance(payload, Mapping):
        raise AdaptiveRiskContractError(
            "adaptive risk reservation request must be a mapping"
        )
    raw = dict(payload)
    supplied_request_sha256 = raw.pop("request_sha256", None)
    if raw.get("schema_version") != RESERVATION_REQUEST_SCHEMA_VERSION:
        raise AdaptiveRiskContractError(
            "unsupported adaptive risk reservation request schema"
        )
    policy_raw = raw.get("policy")
    inputs_raw = raw.get("inputs")
    account_raw = raw.get("account_snapshot")
    if not all(isinstance(value, Mapping) for value in (policy_raw, inputs_raw, account_raw)):
        raise AdaptiveRiskContractError(
            "adaptive risk reservation request snapshots are missing"
        )
    try:
        policy = AdaptiveRiskPolicy(**dict(policy_raw))
        account_values = dict(account_raw)
        supplied_account_sha256 = account_values.pop("snapshot_sha256", None)
        account_values["observed_at"] = _parse_utc(
            account_values.get("observed_at"), "account_snapshot.observed_at"
        )
        account_values["available_at"] = _parse_utc(
            account_values.get("available_at"), "account_snapshot.available_at"
        )
        account_snapshot = ImmutableAccountRiskSnapshot(**account_values)
        if supplied_account_sha256 != account_snapshot.snapshot_sha256:
            raise AdaptiveRiskContractError("account snapshot hash mismatch")

        input_values = dict(inputs_raw)
        input_values["as_of"] = _parse_utc(input_values.get("as_of"), "inputs.as_of")
        evidence_raw = input_values.get("evidence")
        if not isinstance(evidence_raw, Mapping):
            raise AdaptiveRiskContractError("adaptive risk request evidence is missing")
        evidence: dict[str, RiskInputEvidence] = {}
        for name, evidence_payload in evidence_raw.items():
            if not isinstance(evidence_payload, Mapping):
                raise AdaptiveRiskContractError(
                    f"adaptive risk request evidence is invalid: {name}"
                )
            evidence_values = dict(evidence_payload)
            evidence_values["observed_at"] = _parse_utc(
                evidence_values.get("observed_at"),
                f"inputs.evidence.{name}.observed_at",
            )
            evidence_values["available_at"] = _parse_utc(
                evidence_values.get("available_at"),
                f"inputs.evidence.{name}.available_at",
            )
            evidence[str(name)] = RiskInputEvidence(**evidence_values)
        input_values["evidence"] = evidence
        inputs = AdaptiveRiskInputs(**input_values)

        def load_bound_evidence(name: str) -> RiskInputEvidence | None:
            value = raw.get(name)
            if value is None:
                return None
            if not isinstance(value, Mapping):
                raise AdaptiveRiskContractError(
                    f"adaptive risk request {name} is invalid"
                )
            fields = dict(value)
            fields["observed_at"] = _parse_utc(
                fields.get("observed_at"), f"{name}.observed_at"
            )
            fields["available_at"] = _parse_utc(
                fields.get("available_at"), f"{name}.available_at"
            )
            return RiskInputEvidence(**fields)

        opportunity_raw = raw.get("opportunity_key")
        supplied_opportunity_sha256 = raw.get("opportunity_key_sha256")
        if opportunity_raw is None:
            if supplied_opportunity_sha256 is not None:
                raise AdaptiveRiskContractError(
                    "opportunity_key hash exists without an opportunity_key"
                )
            opportunity_key = None
        else:
            opportunity_key = AdaptiveRiskOpportunityKey.from_payload(opportunity_raw)
            if supplied_opportunity_sha256 != opportunity_key.key_sha256:
                raise AdaptiveRiskContractError("opportunity_key hash mismatch")
        request = AdaptiveRiskReservationRequest(
            policy=policy,
            inputs=inputs,
            account_snapshot=account_snapshot,
            account_scope=raw.get("account_scope"),
            setup_family=raw.get("setup_family"),
            correlation_cluster=raw.get("correlation_cluster"),
            client_order_id=raw.get("client_order_id"),
            entry_limit_price=raw.get("entry_limit_price"),
            opportunity_key=opportunity_key,
            broker_account_evidence=load_bound_evidence(
                "broker_account_evidence"
            ),
            settled_daily_pnl_evidence=load_bound_evidence(
                "settled_daily_pnl_evidence"
            ),
        )
    except AdaptiveRiskContractError:
        raise
    except (TypeError, ValueError) as exc:
        raise AdaptiveRiskContractError(
            f"adaptive risk reservation request is invalid: {exc}"
        ) from exc

    if supplied_request_sha256 != request.request_sha256:
        raise AdaptiveRiskContractError(
            "adaptive risk reservation request hash mismatch"
        )
    if _canonical_json(payload) != _canonical_json(request.to_payload()):
        raise AdaptiveRiskContractError(
            "adaptive risk reservation request failed canonical recomputation"
        )
    return validate_adaptive_risk_reservation_request(request)


@dataclass(frozen=True)
class DurableOrderLifecycleEvidence:
    """Content-addressed authoritative order/fill fact.

    A DB-paper caller cannot turn a local assertion into a fill: its evidence
    must point to the exact canonical simulated-fill row visible in the same
    caller-owned transaction (or an already-committed row during recovery).
    Broker events bind one account, environment, connection generation, CID,
    and broker order for the entire reservation lifecycle.
    """

    event_kind: str
    durability_kind: str
    provider_event_id: str
    broker_source: str
    connection_generation: str
    account_scope: str
    execution_family: str
    broker_environment: str
    account_identity_sha256: str
    client_order_id: str
    broker_order_id: str
    observed_at: datetime
    available_at: datetime
    event_content_sha256: str
    cumulative_filled_quantity: int
    source_record_table: str
    source_record_id: str
    order_status: str
    remaining_open_quantity: int | None = None

    def __post_init__(self) -> None:
        for field in (
            "event_kind",
            "durability_kind",
            "broker_source",
            "execution_family",
            "broker_environment",
            "order_status",
        ):
            object.__setattr__(
                self, field, _norm(getattr(self, field), field, lower=True)
            )
        for field in (
            "provider_event_id",
            "connection_generation",
            "account_scope",
            "client_order_id",
            "broker_order_id",
            "source_record_table",
            "source_record_id",
        ):
            object.__setattr__(self, field, _norm(getattr(self, field), field))
        if self.event_kind not in _LIFECYCLE_EVENT_KINDS:
            raise AdaptiveRiskContractError("unsupported lifecycle evidence event_kind")
        if self.durability_kind not in _LIFECYCLE_DURABILITY_KINDS:
            raise AdaptiveRiskContractError(
                "unsupported lifecycle evidence durability_kind"
            )
        identity = str(self.account_identity_sha256 or "").strip().lower()
        if not _sha(identity):
            raise AdaptiveRiskContractError(
                "lifecycle account_identity_sha256 must be lowercase SHA256"
            )
        object.__setattr__(self, "account_identity_sha256", identity)
        content_sha = str(self.event_content_sha256 or "").strip().lower()
        if not _sha(content_sha):
            raise AdaptiveRiskContractError(
                "lifecycle event_content_sha256 must be lowercase SHA256"
            )
        object.__setattr__(self, "event_content_sha256", content_sha)
        observed = _utc(self.observed_at, "lifecycle.observed_at")
        available = _utc(self.available_at, "lifecycle.available_at")
        if available < observed:
            raise AdaptiveRiskContractError(
                "lifecycle available_at cannot precede observed_at"
            )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)
        quantity = self.cumulative_filled_quantity
        if isinstance(quantity, bool):
            raise AdaptiveRiskContractError(
                "lifecycle cumulative fill must be a non-negative integer"
            )
        try:
            normalized_quantity = int(quantity)
        except (TypeError, ValueError) as exc:
            raise AdaptiveRiskContractError(
                "lifecycle cumulative fill must be a non-negative integer"
            ) from exc
        if normalized_quantity < 0 or normalized_quantity != quantity:
            raise AdaptiveRiskContractError(
                "lifecycle cumulative fill must be a non-negative integer"
            )
        object.__setattr__(self, "cumulative_filled_quantity", normalized_quantity)
        remaining = self.remaining_open_quantity
        if remaining is not None:
            if isinstance(remaining, bool):
                raise AdaptiveRiskContractError(
                    "lifecycle remaining open quantity must be a non-negative integer"
                )
            try:
                normalized_remaining = int(remaining)
            except (TypeError, ValueError) as exc:
                raise AdaptiveRiskContractError(
                    "lifecycle remaining open quantity must be a non-negative integer"
                ) from exc
            if normalized_remaining < 0 or normalized_remaining != remaining:
                raise AdaptiveRiskContractError(
                    "lifecycle remaining open quantity must be a non-negative integer"
                )
            object.__setattr__(
                self, "remaining_open_quantity", normalized_remaining
            )
        if self.event_kind == "terminal_zero_fill" and normalized_quantity != 0:
            raise AdaptiveRiskContractError(
                "terminal-zero lifecycle evidence must confirm cumulative fill zero"
            )
        if self.event_kind == "position_reduced":
            if (
                self.remaining_open_quantity is None
                or self.remaining_open_quantity <= 0
                or self.remaining_open_quantity >= normalized_quantity
            ):
                raise AdaptiveRiskContractError(
                    "position-reduced evidence must bind a positive smaller remainder"
                )
        elif self.event_kind == "position_flat":
            if self.remaining_open_quantity != 0:
                raise AdaptiveRiskContractError(
                    "position-flat evidence must bind zero remaining quantity"
                )
        elif self.remaining_open_quantity is not None:
            raise AdaptiveRiskContractError(
                "remaining open quantity is only valid for position lifecycle facts"
            )
        if self.durability_kind == "committed_db_paper_fill":
            if self.broker_source != "db_paper":
                raise AdaptiveRiskContractError(
                    "committed DB-paper evidence must use broker_source=db_paper"
                )
            if self.source_record_table != _DB_PAPER_FILL_TABLE:
                raise AdaptiveRiskContractError(
                    "DB-paper evidence must bind the canonical simulated-fill table"
                )
        elif self.durability_kind == "committed_alpaca_paper_fill":
            if self.broker_source != "alpaca":
                raise AdaptiveRiskContractError(
                    "committed Alpaca PAPER fill must use broker_source=alpaca"
                )
            if self.source_record_table != _ALPACA_PAPER_FILL_TABLE:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER fill evidence must bind the canonical fill table"
                )
            if self.event_kind != "cumulative_fill":
                raise AdaptiveRiskContractError(
                    "committed Alpaca PAPER fill authority is entry-fill-only"
                )
        elif (
            self.durability_kind
            == POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
        ):
            if self.broker_source != "alpaca":
                raise AdaptiveRiskContractError(
                    "post-settlement Alpaca fill must use broker_source=alpaca"
                )
            if self.source_record_table != _ALPACA_POST_SETTLEMENT_FILL_TABLE:
                raise AdaptiveRiskContractError(
                    "post-settlement Alpaca fill must bind its contradiction ledger"
                )
            if self.event_kind != "cumulative_fill":
                raise AdaptiveRiskContractError(
                    "post-settlement Alpaca authority is entry-fill-only"
                )

    @property
    def evidence_sha256(self) -> str:
        return _sha256_json(asdict(self))

    def to_payload(self) -> dict[str, Any]:
        payload = _json_safe(asdict(self))
        payload["evidence_sha256"] = self.evidence_sha256
        return payload


@dataclass(frozen=True)
class DurableSubmitAttemptEvidence:
    """Content-addressed local truth for an unknowable POST outcome.

    A transport timeout is not broker evidence and must never assert a zero
    fill.  It still needs the exact account/broker generation, client id,
    clocks, and captured transport record which caused the reservation to be
    retained.  ``broker_order_id`` is explicitly nullable because the failure
    can occur before the broker returns one; ``cumulative_filled_quantity`` is
    deliberately ``None`` because any known quantity belongs on an
    authoritative lifecycle event instead.
    """

    attempt_event_id: str
    broker_source: str
    connection_generation: str
    account_scope: str
    execution_family: str
    broker_environment: str
    account_identity_sha256: str
    client_order_id: str
    broker_order_id: str | None
    observed_at: datetime
    available_at: datetime
    event_content_sha256: str
    source_record_table: str
    source_record_id: str
    outcome_status: str = "indeterminate"
    cumulative_filled_quantity: None = None

    def __post_init__(self) -> None:
        for field in (
            "broker_source",
            "execution_family",
            "broker_environment",
            "outcome_status",
        ):
            object.__setattr__(
                self, field, _norm(getattr(self, field), field, lower=True)
            )
        for field in (
            "attempt_event_id",
            "connection_generation",
            "account_scope",
            "client_order_id",
            "source_record_table",
            "source_record_id",
        ):
            object.__setattr__(self, field, _norm(getattr(self, field), field))
        if self.broker_order_id is not None:
            object.__setattr__(
                self,
                "broker_order_id",
                _norm(self.broker_order_id, "broker_order_id"),
            )
        if self.outcome_status != "indeterminate":
            raise AdaptiveRiskContractError(
                "submit-attempt evidence must remain outcome_status=indeterminate"
            )
        if self.cumulative_filled_quantity is not None:
            raise AdaptiveRiskContractError(
                "indeterminate submit evidence cannot assert a cumulative fill"
            )
        identity = str(self.account_identity_sha256 or "").strip().lower()
        if not _sha(identity):
            raise AdaptiveRiskContractError(
                "submit-attempt account_identity_sha256 must be lowercase SHA256"
            )
        object.__setattr__(self, "account_identity_sha256", identity)
        content_sha = str(self.event_content_sha256 or "").strip().lower()
        if not _sha(content_sha):
            raise AdaptiveRiskContractError(
                "submit-attempt event_content_sha256 must be lowercase SHA256"
            )
        object.__setattr__(self, "event_content_sha256", content_sha)
        observed = _utc(self.observed_at, "submit_attempt.observed_at")
        available = _utc(self.available_at, "submit_attempt.available_at")
        if available < observed:
            raise AdaptiveRiskContractError(
                "submit-attempt available_at cannot precede observed_at"
            )
        object.__setattr__(self, "observed_at", observed)
        object.__setattr__(self, "available_at", available)

    @property
    def evidence_sha256(self) -> str:
        return _sha256_json(asdict(self))

    def to_payload(self) -> dict[str, Any]:
        payload = _json_safe(asdict(self))
        payload["evidence_sha256"] = self.evidence_sha256
        return payload


def canonical_db_paper_fill_payload(
    row: TradingAutomationSimulatedFill,
) -> dict[str, Any]:
    """Canonical content used to verify one transactionally durable paper fill."""

    def db_clock(value: datetime | None) -> str | None:
        if value is None:
            return None
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return _iso(value)

    def db_float(value: Any) -> float | None:
        # SQLAlchemy may retain an assigned integer on the just-flushed ORM
        # object, then hydrate the same PostgreSQL FLOAT as ``100.0`` in a new
        # Session.  Normalize DB numeric columns before hashing so transaction,
        # commit, reload, and concurrent recovery all share one content address.
        return float(value) if value is not None else None

    return {
        "source_record_table": _DB_PAPER_FILL_TABLE,
        "id": int(row.id),
        "session_id": int(row.session_id),
        "ts": db_clock(row.ts),
        "symbol": row.symbol,
        "lane": row.lane,
        "side": row.side,
        "action": row.action,
        "fill_type": row.fill_type,
        "quantity": db_float(row.quantity),
        "price": db_float(row.price),
        "reference_price": db_float(row.reference_price),
        "fees_usd": db_float(row.fees_usd),
        "pnl_usd": db_float(row.pnl_usd),
        "position_state_before": row.position_state_before,
        "position_state_after": row.position_state_after,
        "reason": row.reason,
        "marker_json": dict(row.marker_json or {}),
        "decision_packet_id": (
            int(row.decision_packet_id)
            if row.decision_packet_id is not None
            else None
        ),
        "created_at": db_clock(row.created_at),
    }


def canonical_db_paper_fill_content_sha256(
    row: TradingAutomationSimulatedFill,
) -> str:
    return _sha256_json(canonical_db_paper_fill_payload(row))


@dataclass(frozen=True)
class AdaptiveReservationDecision:
    schema_version: str
    admission_accepted: bool
    idempotent_retry: bool
    rejection_reasons: tuple[str, ...]
    decision_packet_sha256: str
    reservation_id: uuid.UUID | None
    account_scope: str
    symbol: str
    trading_date: date
    setup_family: str
    client_order_id: str
    quantity_shares: int
    structural_risk_usd: Decimal
    gross_notional_usd: Decimal
    buying_power_impact_usd: Decimal


@dataclass(frozen=True)
class LockedAdaptiveRiskAdmissionSnapshot:
    """Account-lock-owned economic snapshot used by one final admission bundle.

    This object is created only after the account advisory lock is held in the
    caller's outer transaction.  A DB-paper runner may then assemble its final
    market/eligibility/account bundle and call ``reserve(..., locked_snapshot=)``.
    The store validates the exact values but does not replace them with a later
    ledger read or a later decision clock.
    """

    schema_version: str
    account_scope: str
    symbol: str
    correlation_cluster: str
    account_snapshot_sha256: str
    transaction_id: str
    backend_pid: int
    lock_receipt_id: str
    observed_at: datetime
    aggregates: Mapping[str, float]
    ledger_payload: Mapping[str, Any]
    ledger_sha256: str
    policy_buying_power_capacity_usd: float
    content_sha256: str

    @classmethod
    def create(
        cls,
        *,
        account_scope: str,
        symbol: str,
        correlation_cluster: str,
        account_snapshot_sha256: str,
        transaction_id: str,
        backend_pid: int,
        lock_receipt_id: str,
        observed_at: datetime,
        aggregates: Mapping[str, Any],
        ledger_payload: Mapping[str, Any],
        policy_buying_power_capacity_usd: float,
    ) -> "LockedAdaptiveRiskAdmissionSnapshot":
        normalized_aggregates = {
            str(name): float(value) for name, value in dict(aggregates).items()
        }
        normalized_ledger = _json_safe(dict(ledger_payload))
        ledger_sha = _sha256_json(normalized_ledger)
        body = {
            "schema_version": LOCKED_ADMISSION_SNAPSHOT_SCHEMA_VERSION,
            "account_scope": str(account_scope or "").strip(),
            "symbol": str(symbol or "").strip().upper(),
            "correlation_cluster": str(correlation_cluster or "").strip().lower(),
            "account_snapshot_sha256": str(
                account_snapshot_sha256 or ""
            ).strip().lower(),
            "transaction_id": str(transaction_id or "").strip(),
            "backend_pid": int(backend_pid),
            "lock_receipt_id": str(lock_receipt_id or "").strip().lower(),
            "observed_at": _utc(observed_at, "locked_snapshot.observed_at"),
            "aggregates": normalized_aggregates,
            "ledger_payload": normalized_ledger,
            "ledger_sha256": ledger_sha,
            "policy_buying_power_capacity_usd": float(
                policy_buying_power_capacity_usd
            ),
        }
        return cls(**body, content_sha256=_sha256_json(body))

    def __post_init__(self) -> None:
        self.verify()

    def verify(self) -> "LockedAdaptiveRiskAdmissionSnapshot":
        """Recompute every canonical invariant at the point of consumption."""

        if self.schema_version != LOCKED_ADMISSION_SNAPSHOT_SCHEMA_VERSION:
            raise AdaptiveRiskContractError("locked admission snapshot schema is invalid")
        if (
            not self.account_scope
            or self.account_scope != self.account_scope.strip()
            or not self.symbol
            or self.symbol != self.symbol.strip().upper()
            or not self.correlation_cluster
            or self.correlation_cluster
            != self.correlation_cluster.strip().lower()
            or not _sha(self.account_snapshot_sha256)
            or not self.transaction_id
            or not self.transaction_id.isdigit()
            or int(self.backend_pid) <= 0
        ):
            raise AdaptiveRiskContractError("locked admission snapshot identity is incomplete")
        try:
            canonical_receipt = str(uuid.UUID(self.lock_receipt_id))
        except (TypeError, ValueError, AttributeError) as exc:
            raise AdaptiveRiskContractError(
                "locked admission snapshot receipt is invalid"
            ) from exc
        if canonical_receipt != self.lock_receipt_id:
            raise AdaptiveRiskContractError(
                "locked admission snapshot receipt is not canonical"
            )
        observed_at = _utc(self.observed_at, "locked_snapshot.observed_at")
        if observed_at != self.observed_at:
            raise AdaptiveRiskContractError(
                "locked admission snapshot clock is not canonical UTC"
            )
        if set(self.aggregates) != _RISK_LEDGER_AGGREGATE_FIELDS:
            raise AdaptiveRiskContractError(
                "locked admission snapshot aggregates are incomplete"
            )
        if any(
            not _finite(value) or float(value) < 0
            for value in self.aggregates.values()
        ):
            raise AdaptiveRiskContractError(
                "locked admission snapshot aggregates are invalid"
            )
        if (
            not _finite(self.policy_buying_power_capacity_usd)
            or float(self.policy_buying_power_capacity_usd) < 0
        ):
            raise AdaptiveRiskContractError(
                "locked admission buying-power capacity is invalid"
            )
        ledger = dict(self.ledger_payload)
        if (
            ledger.get("schema_version") != RESERVATION_LEDGER_GENERATION
            or ledger.get("account_scope") != self.account_scope
            or _canonical_json(ledger.get("aggregates"))
            != _canonical_json(dict(self.aggregates))
            or not isinstance(ledger.get("active_reservations"), list)
            or not isinstance(ledger.get("pending_settlements"), list)
            or not isinstance(ledger.get("quarantined_exposures"), list)
            or not isinstance(ledger.get("paper_position_bindings"), list)
        ):
            raise AdaptiveRiskContractError(
                "locked admission ledger payload is invalid"
            )
        if not _sha(self.ledger_sha256) or not _sha(self.content_sha256):
            raise AdaptiveRiskContractError("locked admission snapshot digest is invalid")
        if self.ledger_sha256 != _sha256_json(dict(self.ledger_payload)):
            raise AdaptiveRiskContractError("locked admission ledger digest changed")
        if self.content_sha256 != _sha256_json(self.body_without_content_sha()):
            raise AdaptiveRiskContractError("locked admission snapshot changed")
        return self

    def body_without_content_sha(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "account_scope": self.account_scope,
            "symbol": self.symbol,
            "correlation_cluster": self.correlation_cluster,
            "account_snapshot_sha256": self.account_snapshot_sha256,
            "transaction_id": self.transaction_id,
            "backend_pid": self.backend_pid,
            "lock_receipt_id": self.lock_receipt_id,
            "observed_at": self.observed_at,
            "aggregates": dict(self.aggregates),
            "ledger_payload": dict(self.ledger_payload),
            "ledger_sha256": self.ledger_sha256,
            "policy_buying_power_capacity_usd": (
                self.policy_buying_power_capacity_usd
            ),
        }

    def to_payload(self) -> dict[str, Any]:
        payload = _json_safe(self.body_without_content_sha())
        payload["content_sha256"] = self.content_sha256
        return payload


def alpaca_paper_settled_daily_pnl_risk_evidence(
    evidence: AlpacaPaperSettledDailyPnlEvidence,
    *,
    terminal_fill_authority_sha256: str | None = None,
) -> RiskInputEvidence:
    """Project typed settlement evidence into the shared risk-input schema."""

    if type(evidence) is not AlpacaPaperSettledDailyPnlEvidence:
        raise AdaptiveRiskContractError(
            "settled daily P&L evidence is not the authoritative typed value"
        )
    try:
        evidence.verify()
    except AlpacaCycleSettlementIntegrityError as exc:
        raise AdaptiveRiskContractError(
            "settled daily P&L evidence failed verification"
        ) from exc
    terminal_authority = (
        None
        if terminal_fill_authority_sha256 is None
        else str(terminal_fill_authority_sha256).strip().lower()
    )
    if terminal_authority is not None and not _sha(terminal_authority):
        raise AdaptiveRiskContractError(
            "settled daily P&L terminal-fill authority is invalid"
        )
    content_sha256 = evidence.evidence_sha256
    source = SETTLED_DAILY_PNL_EVIDENCE_SOURCE
    provider_generation = (
        f"alpaca-paper-settlement-head:{evidence.settlement_head_content_sha256}"
    )
    if terminal_authority is not None:
        content_sha256 = _sha256_json(
            {
                "schema_version": (
                    "chili.alpaca-paper-terminal-fill-bound-daily-pnl.v1"
                ),
                "settled_daily_pnl_evidence_sha256": evidence.evidence_sha256,
                "terminal_fill_authority_sha256": terminal_authority,
            }
        )
        source += ":terminal-fill-bound"
        provider_generation += f":terminal-fill:{terminal_authority}"
    return RiskInputEvidence(
        source=source,
        observed_at=evidence.observed_at,
        available_at=evidence.available_at,
        content_sha256=content_sha256,
        provider_generation=provider_generation,
    )


@dataclass(frozen=True)
class LockedAlpacaPaperDailyPnlAttestation:
    """Process-private proof that PAPER P&L was built under canonical locks."""

    account_scope: str
    account_id: str
    account_identity_sha256: str
    decision_id: str
    run_id: str
    generation: int
    broker_provider_generation: str
    decision_as_of: datetime
    expires_at: datetime
    transaction_id: str
    backend_pid: int
    lock_receipt_id: str
    account_snapshot_sha256: str
    account_payload_sha256: str
    account_read_receipt_sha256: str
    active_input_attestation_sha256: str
    account_capture_authority_sha256: str
    daily_pnl_evidence_sha256: str
    daily_terminal_fill_authority_sha256: str
    buying_power_reflection_receipt_sha256: str | None
    ledger_snapshot_content_sha256: str
    ledger_sha256: str
    bundle_sha256: str
    _verification_tag: str = field(repr=False)
    _verification_token: object = field(repr=False, compare=False)

    def _body(self) -> dict[str, Any]:
        return {
            "schema_version": "chili.locked-alpaca-paper-daily-pnl-attestation.v3",
            "account_scope": self.account_scope,
            "account_id": self.account_id,
            "account_identity_sha256": self.account_identity_sha256,
            "decision_id": self.decision_id,
            "run_id": self.run_id,
            "generation": self.generation,
            "broker_provider_generation": self.broker_provider_generation,
            "decision_as_of": self.decision_as_of,
            "expires_at": self.expires_at,
            "transaction_id": self.transaction_id,
            "backend_pid": self.backend_pid,
            "lock_receipt_id": self.lock_receipt_id,
            "account_snapshot_sha256": self.account_snapshot_sha256,
            "account_payload_sha256": self.account_payload_sha256,
            "account_read_receipt_sha256": self.account_read_receipt_sha256,
            "active_input_attestation_sha256": (
                self.active_input_attestation_sha256
            ),
            "account_capture_authority_sha256": (
                self.account_capture_authority_sha256
            ),
            "daily_pnl_evidence_sha256": self.daily_pnl_evidence_sha256,
            "daily_terminal_fill_authority_sha256": (
                self.daily_terminal_fill_authority_sha256
            ),
            "buying_power_reflection_receipt_sha256": (
                self.buying_power_reflection_receipt_sha256
            ),
            "ledger_snapshot_content_sha256": (
                self.ledger_snapshot_content_sha256
            ),
            "ledger_sha256": self.ledger_sha256,
            "bundle_sha256": self.bundle_sha256,
        }

    def __post_init__(self) -> None:
        verify_locked_alpaca_paper_daily_pnl_attestation(self)

    def __reduce__(self):
        raise TypeError("locked Alpaca PAPER admission attestation cannot be pickled")


def _locked_alpaca_paper_attestation_tag(payload: Mapping[str, Any]) -> str:
    return hmac.new(
        _LOCKED_ALPACA_PAPER_ADMISSION_KEY,
        _canonical_json(dict(payload)),
        hashlib.sha256,
    ).hexdigest()


def verify_locked_alpaca_paper_daily_pnl_attestation(
    value: LockedAlpacaPaperDailyPnlAttestation,
) -> LockedAlpacaPaperDailyPnlAttestation:
    if type(value) is not LockedAlpacaPaperDailyPnlAttestation:
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission attestation is malformed"
        )
    if value._verification_token is not _LOCKED_ALPACA_PAPER_ADMISSION_TOKEN:
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission attestation token is invalid"
        )
    if value.account_scope != "alpaca:paper":
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission attestation scope is invalid"
        )
    try:
        canonical_account_id = str(uuid.UUID(value.account_id))
    except (TypeError, ValueError, AttributeError) as exc:
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission account UUID is invalid"
        ) from exc
    if canonical_account_id != value.account_id:
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission account UUID is noncanonical"
        )
    for name in (
        "account_identity_sha256",
        "account_snapshot_sha256",
        "account_payload_sha256",
        "account_read_receipt_sha256",
        "active_input_attestation_sha256",
        "account_capture_authority_sha256",
        "daily_pnl_evidence_sha256",
        "daily_terminal_fill_authority_sha256",
        "ledger_snapshot_content_sha256",
        "ledger_sha256",
        "bundle_sha256",
    ):
        if not _sha(getattr(value, name)):
            raise AdaptiveRiskContractError(
                f"locked Alpaca PAPER admission attestation {name} is invalid"
            )
    reflection_sha = value.buying_power_reflection_receipt_sha256
    if reflection_sha is not None and not _sha(reflection_sha):
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER buying-power reflection receipt is invalid"
        )
    decision_at = _utc(value.decision_as_of, "attestation.decision_as_of")
    expires_at = _utc(value.expires_at, "attestation.expires_at")
    if decision_at != value.decision_as_of or expires_at != value.expires_at:
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission clocks are not canonical UTC"
        )
    if decision_at > expires_at:
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission was issued after expiry"
        )
    try:
        if str(uuid.UUID(value.run_id)) != value.run_id:
            raise ValueError
        if str(uuid.UUID(value.lock_receipt_id)) != value.lock_receipt_id:
            raise ValueError
    except (TypeError, ValueError, AttributeError) as exc:
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission UUID is invalid"
        ) from exc
    if (
        not value.decision_id
        or not value.transaction_id.isdigit()
        or int(value.backend_pid) <= 0
        or isinstance(value.generation, bool)
        or int(value.generation) <= 0
        or not value.broker_provider_generation
    ):
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission identity is incomplete"
        )
    expected = _locked_alpaca_paper_attestation_tag(value._body())
    if not hmac.compare_digest(value._verification_tag, expected):
        raise AdaptiveRiskContractError(
            "locked Alpaca PAPER admission attestation changed"
        )
    return value


@dataclass(frozen=True)
class LockedAlpacaPaperAdmissionBundle:
    """Atomic broker-account, settled-P&L and reservation-ledger authority."""

    account_snapshot: ImmutableAccountRiskSnapshot
    settled_daily_pnl_evidence: AlpacaPaperSettledDailyPnlEvidence
    locked_risk_snapshot: LockedAdaptiveRiskAdmissionSnapshot
    attestation: LockedAlpacaPaperDailyPnlAttestation
    account_evidence: RiskInputEvidence
    daily_pnl_evidence: RiskInputEvidence
    account_id: str
    account_payload_sha256: str
    account_read_receipt_sha256: str
    active_input_attestation_sha256: str
    account_capture_authority_sha256: str
    daily_terminal_fill_authority_sha256: str
    buying_power_reflection_receipt_sha256: str | None

    def __post_init__(self) -> None:
        self.verify()

    @property
    def decision_as_of(self) -> datetime:
        return self.settled_daily_pnl_evidence.decision_as_of

    @property
    def risk_date_et(self) -> date:
        return self.settled_daily_pnl_evidence.risk_date_et

    @property
    def bundle_sha256(self) -> str:
        return _sha256_json(
            {
                "schema_version": "chili.locked-alpaca-paper-admission-bundle.v3",
                "account_snapshot_sha256": self.account_snapshot.snapshot_sha256,
                "settled_daily_pnl_evidence_sha256": (
                    self.settled_daily_pnl_evidence.evidence_sha256
                ),
                "locked_risk_snapshot_content_sha256": (
                    self.locked_risk_snapshot.content_sha256
                ),
                "account_evidence": asdict(self.account_evidence),
                "daily_pnl_evidence": asdict(self.daily_pnl_evidence),
                "account_id": self.account_id,
                "account_payload_sha256": self.account_payload_sha256,
                "account_read_receipt_sha256": (
                    self.account_read_receipt_sha256
                ),
                "active_input_attestation_sha256": (
                    self.active_input_attestation_sha256
                ),
                "account_capture_authority_sha256": (
                    self.account_capture_authority_sha256
                ),
                "daily_terminal_fill_authority_sha256": (
                    self.daily_terminal_fill_authority_sha256
                ),
                "buying_power_reflection_receipt_sha256": (
                    self.buying_power_reflection_receipt_sha256
                ),
            }
        )

    def verify(self) -> "LockedAlpacaPaperAdmissionBundle":
        if type(self.account_snapshot) is not ImmutableAccountRiskSnapshot:
            raise AdaptiveRiskContractError(
                "locked Alpaca PAPER account snapshot is malformed"
            )
        if type(self.settled_daily_pnl_evidence) is not (
            AlpacaPaperSettledDailyPnlEvidence
        ):
            raise AdaptiveRiskContractError(
                "locked Alpaca PAPER daily P&L evidence is malformed"
            )
        if type(self.locked_risk_snapshot) is not LockedAdaptiveRiskAdmissionSnapshot:
            raise AdaptiveRiskContractError(
                "locked Alpaca PAPER risk snapshot is malformed"
            )
        self.settled_daily_pnl_evidence.verify()
        self.locked_risk_snapshot.verify()
        verify_locked_alpaca_paper_daily_pnl_attestation(self.attestation)
        expected_daily = alpaca_paper_settled_daily_pnl_risk_evidence(
            self.settled_daily_pnl_evidence,
            terminal_fill_authority_sha256=(
                self.daily_terminal_fill_authority_sha256
            ),
        )
        if self.daily_pnl_evidence != expected_daily:
            raise AdaptiveRiskContractError(
                "locked Alpaca PAPER daily P&L projection changed"
            )
        expected = {
            "account_scope": (
                self.account_snapshot.account_scope,
                "alpaca:paper",
            ),
            "account_identity": (
                self.account_snapshot.account_identity_sha256,
                self.settled_daily_pnl_evidence.account_identity_sha256,
            ),
            "local_realized_pnl": (
                Decimal(str(self.account_snapshot.local_realized_pnl_usd)),
                self.settled_daily_pnl_evidence.local_realized_pnl_usd,
            ),
            "snapshot_binding": (
                self.locked_risk_snapshot.account_snapshot_sha256,
                self.account_snapshot.snapshot_sha256,
            ),
            "decision_clock": (
                self.locked_risk_snapshot.observed_at,
                self.decision_as_of,
            ),
            "risk_date": (
                self.risk_date_et,
                self.decision_as_of.astimezone(ET).date(),
            ),
            "attested_account_id": (
                self.attestation.account_id,
                self.account_id,
            ),
            "attested_account_payload": (
                self.attestation.account_payload_sha256,
                self.account_payload_sha256,
            ),
            "attested_account_receipt": (
                self.attestation.account_read_receipt_sha256,
                self.account_read_receipt_sha256,
            ),
            "attested_active_input": (
                self.attestation.active_input_attestation_sha256,
                self.active_input_attestation_sha256,
            ),
            "attested_account_capture_authority": (
                self.attestation.account_capture_authority_sha256,
                self.account_capture_authority_sha256,
            ),
            "attested_terminal_fill_authority": (
                self.attestation.daily_terminal_fill_authority_sha256,
                self.daily_terminal_fill_authority_sha256,
            ),
            "attested_buying_power_reflection": (
                self.attestation.buying_power_reflection_receipt_sha256,
                self.buying_power_reflection_receipt_sha256,
            ),
            "attested_snapshot": (
                self.attestation.account_snapshot_sha256,
                self.account_snapshot.snapshot_sha256,
            ),
            "attested_daily": (
                self.attestation.daily_pnl_evidence_sha256,
                self.settled_daily_pnl_evidence.evidence_sha256,
            ),
            "attested_ledger_snapshot": (
                self.attestation.ledger_snapshot_content_sha256,
                self.locked_risk_snapshot.content_sha256,
            ),
            "attested_ledger": (
                self.attestation.ledger_sha256,
                self.locked_risk_snapshot.ledger_sha256,
            ),
            "attested_bundle": (
                self.attestation.bundle_sha256,
                self.bundle_sha256,
            ),
            "account_evidence_source": (
                self.account_evidence.source,
                self.account_snapshot.source,
            ),
            "account_evidence_generation": (
                self.account_evidence.provider_generation,
                self.account_snapshot.provider_generation,
            ),
            "account_evidence_observed": (
                self.account_evidence.observed_at,
                self.account_snapshot.observed_at,
            ),
            "account_evidence_available": (
                self.account_evidence.available_at,
                self.account_snapshot.available_at,
            ),
            "account_evidence_content": (
                self.account_evidence.content_sha256,
                self.account_payload_sha256,
            ),
            "paper_pending_reflection_authority": (
                self.buying_power_reflection_receipt_sha256 is not None,
                self.locked_risk_snapshot.aggregates[
                    "pending_buying_power_impact_usd"
                ]
                > 0,
            ),
            "paper_buying_power_capacity": (
                _money(self.locked_risk_snapshot.policy_buying_power_capacity_usd),
                _money(
                    self.account_snapshot.buying_power_usd
                    + self.locked_risk_snapshot.aggregates[
                        "open_buying_power_impact_usd"
                    ]
                    + self.account_snapshot.pending_policy_buying_power_reflected_usd
                ),
            ),
        }
        changed = sorted(
            name for name, (actual, required) in expected.items()
            if actual != required
        )
        if changed:
            raise AdaptiveRiskContractError(
                "locked Alpaca PAPER admission bundle mismatch: "
                + ",".join(changed)
            )
        return self


def _verify_daily_terminal_fill_authority(
    *,
    settlements: list[AlpacaPaperCycleSettlement],
    terminal_fills: Mapping[str, AlpacaPaperFillActivity],
    terminal_observation_receipts: Mapping[
        str, AlpacaPaperTerminalFillObservationReceipt
    ],
    fill_observations: Mapping[str, AlpacaPaperFillQueryObservation],
    fill_observation_mappings: Mapping[
        tuple[str, str], AlpacaPaperFillObservationActivity
    ],
    decision_as_of: datetime,
) -> tuple[Mapping[str, datetime], str]:
    """Bind each settlement to its authoritative provider execution clock."""

    as_of = _utc(decision_as_of, "terminal_fill_authority.decision_as_of")
    expected_events = {row.terminal_fill_event_sha256 for row in settlements}
    if set(terminal_fills) != expected_events:
        raise AdaptiveRiskContractError(
            "Alpaca PAPER terminal-fill authority inventory is incomplete"
        )
    if set(terminal_observation_receipts) != {
        row.settlement_sha256 for row in settlements
    }:
        raise AdaptiveRiskContractError(
            "Alpaca PAPER terminal-fill observation receipt inventory is incomplete"
        )
    execution_by_settlement: dict[str, datetime] = {}
    inventory: list[dict[str, Any]] = []
    for settlement in settlements:
        fill = terminal_fills.get(settlement.terminal_fill_event_sha256)
        if type(fill) is not AlpacaPaperFillActivity:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER terminal-fill authority row is malformed"
            )
        receipt = terminal_observation_receipts.get(settlement.settlement_sha256)
        if type(receipt) is not AlpacaPaperTerminalFillObservationReceipt:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER terminal-fill observation receipt is malformed"
            )
        observation = fill_observations.get(receipt.observation_sha256)
        mapping = fill_observation_mappings.get(
            (receipt.observation_sha256, fill.event_sha256)
        )
        if (
            type(observation) is not AlpacaPaperFillQueryObservation
            or type(mapping) is not AlpacaPaperFillObservationActivity
        ):
            raise AdaptiveRiskContractError(
                "Alpaca PAPER terminal-fill observation predecessors are missing"
            )
        try:
            prepared = verify_alpaca_paper_fill_activity_row(fill)
            verify_alpaca_paper_terminal_fill_observation_receipt(
                receipt,
                settlement=settlement,
                observation=observation,
                terminal_fill=fill,
                mapping=mapping,
            )
        except AlpacaFillActivityError as exc:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER terminal-fill authority failed verification"
            ) from exc
        provider_at = _utc(
            fill.provider_transaction_at,
            "terminal_fill_authority.provider_transaction_at",
        )
        event_at = _utc(
            fill.provider_event_at,
            "terminal_fill_authority.provider_event_at",
        )
        received_at = _utc(
            fill.received_at,
            "terminal_fill_authority.received_at",
        )
        available_at = _utc(
            fill.available_at,
            "terminal_fill_authority.available_at",
        )
        settlement_available_at = _utc(
            settlement.closed_available_at,
            "terminal_fill_authority.settlement_available_at",
        )
        exact = {
            "event_sha256": (
                fill.event_sha256,
                settlement.terminal_fill_event_sha256,
            ),
            "reservation_id": (
                str(fill.reservation_id),
                str(settlement.reservation_id),
            ),
            "account_scope": (fill.account_scope, settlement.account_scope),
            "account_identity": (
                fill.account_identity_sha256,
                settlement.account_identity_sha256,
            ),
            "sequence": (
                int(fill.sequence),
                int(settlement.terminal_fill_sequence),
            ),
            "provider_event_at": (event_at, provider_at),
            "prepared_provider_event_at": (
                prepared.provider_event_at,
                provider_at,
            ),
        }
        changed = sorted(
            name for name, (actual, required) in exact.items()
            if actual != required
        )
        if changed:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER terminal-fill/settlement mismatch: "
                + ",".join(changed)
            )
        if not (
            fill.capture_authority_status == "verified"
            and fill.provider_event_clock_status == "authoritative"
            and fill.provider_event_clock_field == "transaction_time"
            and fill.order_role == "exit"
            and fill.side == "sell"
            and provider_at <= received_at <= available_at
            and available_at
            <= _utc(
                receipt.terminal_fill_available_at,
                "terminal_fill_authority.observation_available_at",
            )
            <= settlement_available_at
            <= as_of
        ):
            raise AdaptiveRiskContractError(
                "Alpaca PAPER terminal fill lacks authoritative causal execution"
            )
        execution_by_settlement[settlement.settlement_sha256] = provider_at
        inventory.append(
            {
                "settlement_sha256": settlement.settlement_sha256,
                "terminal_fill_event_sha256": fill.event_sha256,
                "terminal_fill_record_content_sha256": (
                    fill.record_content_sha256
                ),
                "provider_transaction_at": _iso(provider_at),
                "fill_available_at": _iso(available_at),
                "settlement_available_at": _iso(settlement_available_at),
                "terminal_fill_observation_receipt_sha256": (
                    receipt.receipt_sha256
                ),
                "query_receipt_sha256": receipt.query_receipt_sha256,
                "adapter_connection_generation": (
                    receipt.adapter_connection_generation
                ),
                "adapter_build_sha256": receipt.adapter_build_sha256,
            }
        )
    authority_sha256 = _sha256_json(
        {
            "schema_version": "chili.alpaca-paper-daily-terminal-fill-authority.v1",
            "decision_as_of": _iso(as_of),
            "terminal_fills": inventory,
        }
    )
    return execution_by_settlement, authority_sha256


def _derive_verified_alpaca_paper_daily_pnl_evidence(
    *,
    head: AlpacaPaperAccountSettlementHead,
    settlements: list[AlpacaPaperCycleSettlement],
    terminal_fills: Mapping[str, AlpacaPaperFillActivity],
    terminal_observation_receipts: Mapping[
        str, AlpacaPaperTerminalFillObservationReceipt
    ],
    fill_observations: Mapping[str, AlpacaPaperFillQueryObservation],
    fill_observation_mappings: Mapping[
        tuple[str, str], AlpacaPaperFillObservationActivity
    ],
    decision_as_of: datetime,
) -> tuple[AlpacaPaperSettledDailyPnlEvidence, str]:
    """Verify one locked account chain and derive its exact ET-day net P&L."""

    try:
        verify_settlement_head_content(head)
    except AlpacaCycleSettlementIntegrityError as exc:
        raise AdaptiveRiskContractError(
            "Alpaca PAPER settlement head integrity failed"
        ) from exc
    as_of = _utc(decision_as_of, "alpaca_paper_daily_pnl.decision_as_of")
    risk_date = as_of.astimezone(ET).date()
    terminal_execution_at, terminal_authority_sha256 = (
        _verify_daily_terminal_fill_authority(
            settlements=settlements,
            terminal_fills=terminal_fills,
            terminal_observation_receipts=terminal_observation_receipts,
            fill_observations=fill_observations,
            fill_observation_mappings=fill_observation_mappings,
            decision_as_of=as_of,
        )
    )
    expected_count = int(head.settled_cycle_sequence)
    if len(settlements) != expected_count:
        raise AdaptiveRiskContractError(
            "Alpaca PAPER settlement chain length does not match head"
        )
    previous: str | None = None
    gross = Decimal("0")
    fees = Decimal("0")
    net = Decimal("0")
    daily_net = Decimal("0")
    included: list[str] = []
    for expected_sequence, row in enumerate(settlements, start=1):
        try:
            verify_cycle_settlement_content(row)
        except AlpacaCycleSettlementIntegrityError as exc:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER settlement chain content failed verification"
            ) from exc
        if not (
            row.account_scope == "alpaca:paper"
            and row.account_identity_sha256 == head.account_identity_sha256
            and row.execution_family == "alpaca_spot"
            and row.broker_environment == "paper"
            and int(row.terminal_sequence) == expected_sequence
            and row.previous_account_settlement_sha256 == previous
        ):
            raise AdaptiveRiskContractError(
                "Alpaca PAPER settlement chain identity/link is invalid"
            )
        available_at = _utc(
            row.closed_available_at,
            "alpaca_paper_cycle_settlement.closed_available_at",
        )
        if available_at > as_of:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER settlement chain contains future economics"
            )
        row_gross = _money(row.gross_realized_pnl_usd)
        row_fees = _money(row.fee_usd)
        row_net = _money(row.net_realized_pnl_usd)
        if row_net != row_gross - row_fees:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER settlement row economics are inconsistent"
            )
        gross += row_gross
        fees += row_fees
        net += row_net
        # Economic attribution follows the authoritative provider execution
        # time retained in the terminal append-only fill.  Settlement/fill
        # availability only proves the economics were knowable by this decision.
        if terminal_execution_at[row.settlement_sha256].astimezone(ET).date() == risk_date:
            daily_net += row_net
            included.append(row.settlement_sha256)
        previous = row.settlement_sha256
    if (
        previous != head.last_settlement_sha256
        or _money(head.cumulative_gross_realized_pnl_usd) != _money(gross)
        or _money(head.cumulative_fee_usd) != _money(fees)
        or _money(head.cumulative_net_realized_pnl_usd) != _money(net)
    ):
        raise AdaptiveRiskContractError(
            "Alpaca PAPER settlement head cumulative economics are inconsistent"
        )
    if expected_count == 0:
        if head.last_settled_at is not None:
            raise AdaptiveRiskContractError(
                "zero Alpaca PAPER settlement head has a terminal clock"
            )
    else:
        last_available = _utc(
            settlements[-1].closed_available_at,
            "alpaca_paper_cycle_settlement.last_available_at",
        )
        if _utc(head.last_settled_at, "settlement_head.last_settled_at") != last_available:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER settlement head terminal clock is inconsistent"
            )
    try:
        evidence = AlpacaPaperSettledDailyPnlEvidence.create(
            account_identity_sha256=head.account_identity_sha256,
            risk_date_et=risk_date,
            decision_as_of=as_of,
            local_realized_pnl_usd=_money(daily_net),
            settlement_head_content_sha256=head.head_content_sha256,
            settlement_head_sequence=expected_count,
            settlement_head_tail_sha256=head.last_settlement_sha256,
            included_day_settlement_sha256s=tuple(included),
        )
        return evidence, terminal_authority_sha256
    except AlpacaCycleSettlementIntegrityError as exc:
        raise AdaptiveRiskContractError(
            "Alpaca PAPER settled daily P&L evidence construction failed"
        ) from exc


@dataclass(frozen=True)
class AdaptiveReservationState:
    reservation_id: uuid.UUID
    decision_packet_sha256: str
    account_scope: str
    symbol: str
    trading_date: date
    setup_family: str
    correlation_cluster: str
    state: str
    planned_quantity_shares: int
    cumulative_filled_quantity_shares: int
    open_quantity_shares: int
    pending_structural_risk_usd: Decimal
    pending_gross_notional_usd: Decimal
    pending_buying_power_impact_usd: Decimal
    open_structural_risk_usd: Decimal
    open_gross_notional_usd: Decimal
    open_buying_power_impact_usd: Decimal
    opportunity_status: str
    event_sequence: int
    broker_source: str | None
    broker_connection_generation: str | None
    broker_order_id: str | None
    last_broker_observed_at: datetime | None
    last_broker_available_at: datetime | None
    last_source_event_content_sha256: str | None
    lifecycle_contradiction_source_state: str | None
    lifecycle_contradiction_at: datetime | None
    lifecycle_contradiction_evidence_sha256: str | None


@dataclass(frozen=True)
class _PreparedAlpacaPaperBuyingPowerReflection:
    batch: PreparedAlpacaPaperBuyingPowerDoubleCensus
    reservation_ledger_sha256: str
    pending_buying_power_impact_usd: Decimal
    reflected_pending_buying_power_usd: Decimal
    item_bodies: tuple[dict[str, Any], ...]


class AdaptiveRiskReservationStore:
    """Dedicated short-transaction store for adaptive economic claims."""

    def __init__(self, bind: Engine) -> None:
        if getattr(bind.dialect, "name", "") != "postgresql":
            raise AdaptiveRiskContractError(
                "adaptive reservations require PostgreSQL advisory locks"
            )
        self._bind = bind
        self._session_factory = sessionmaker(
            bind=bind,
            autocommit=False,
            autoflush=False,
            expire_on_commit=False,
        )

    @contextmanager
    def _transaction(
        self,
        existing_session: Session | None = None,
    ) -> Iterator[Session]:
        if existing_session is not None:
            existing_bind = existing_session.get_bind()
            existing_engine = getattr(existing_bind, "engine", existing_bind)
            if existing_engine is not self._bind:
                raise AdaptiveRiskContractError(
                    "caller transaction uses a different reservation database"
                )
            if not existing_session.in_transaction():
                raise AdaptiveRiskContractError(
                    "caller Session must already own the outer transaction"
                )
            try:
                yield existing_session
                # A later transition may expire/reload the same projection to
                # acquire its row lock.  Flush this method's event+projection
                # atomically into the caller transaction first so no unflushed
                # sequence/hash state can be discarded.
                existing_session.flush()
            except Exception:
                raise
            return
        session = self._session_factory()
        try:
            with session.begin():
                yield session
        finally:
            session.close()

    @staticmethod
    def _lock_account(session: Session, account_scope: str) -> None:
        session.execute(
            text(
                "SELECT pg_advisory_xact_lock(:namespace, hashtext(:account_scope))"
            ),
            {
                "namespace": _ADVISORY_LOCK_NAMESPACE,
                "account_scope": account_scope,
            },
        )

    @staticmethod
    def _clock(session: Session) -> datetime:
        return _utc(
            session.execute(text("SELECT clock_timestamp()")).scalar_one(),
            "database clock",
        )

    @staticmethod
    def _transaction_identity(session: Session) -> tuple[str, int]:
        transaction_id, backend_pid = session.execute(
            text("SELECT pg_current_xact_id()::text, pg_backend_pid()")
        ).one()
        normalized_transaction_id = str(transaction_id or "").strip()
        normalized_backend_pid = int(backend_pid)
        if not normalized_transaction_id or normalized_backend_pid <= 0:
            raise AdaptiveRiskContractError(
                "database transaction identity is unavailable"
            )
        return normalized_transaction_id, normalized_backend_pid

    def lock_admission_snapshot(
        self,
        *,
        account_scope: str,
        symbol: str,
        correlation_cluster: str,
        account_snapshot: ImmutableAccountRiskSnapshot,
        session: Session,
    ) -> LockedAdaptiveRiskAdmissionSnapshot:
        """Lock one account and expose the only economic snapshot admission may use."""

        with self._transaction(session) as owned:
            normalized_scope = str(account_scope or "").strip()
            if normalized_scope == "alpaca:paper":
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER requires lock_alpaca_paper_admission_bundle"
                )
            normalized_symbol = str(symbol or "").strip().upper()
            normalized_cluster = str(correlation_cluster or "").strip().lower()
            if (
                not normalized_scope
                or not normalized_symbol
                or not normalized_cluster
                or account_snapshot.account_scope != normalized_scope
            ):
                raise AdaptiveRiskContractError(
                    "locked admission snapshot identity is invalid"
                )
            self._lock_account(owned, normalized_scope)
            transaction_id, backend_pid = self._transaction_identity(owned)
            lock_receipt_id = str(uuid.uuid4())
            observed_at = self._clock(owned)
            aggregates, ledger_payload = self._active_ledger(
                owned,
                account_scope=normalized_scope,
                symbol=normalized_symbol,
                correlation_cluster=normalized_cluster,
            )
            reflected_pending_bp = float(
                account_snapshot.pending_policy_buying_power_reflected_usd
            )
            policy_capacity = (
                float(account_snapshot.buying_power_usd)
                + float(aggregates["open_buying_power_impact_usd"])
                + reflected_pending_bp
            )
            snapshot = LockedAdaptiveRiskAdmissionSnapshot.create(
                account_scope=normalized_scope,
                symbol=normalized_symbol,
                correlation_cluster=normalized_cluster,
                account_snapshot_sha256=account_snapshot.snapshot_sha256,
                transaction_id=transaction_id,
                backend_pid=backend_pid,
                lock_receipt_id=lock_receipt_id,
                observed_at=observed_at,
                aggregates=aggregates,
                ledger_payload=ledger_payload,
                policy_buying_power_capacity_usd=policy_capacity,
            )
            self._raise_if_pending_settlement(
                snapshot.ledger_payload,
                locked_snapshot=snapshot,
            )
            self._raise_if_quarantined_exposure(
                snapshot.ledger_payload,
                locked_snapshot=snapshot,
            )
            receipts = owned.info.setdefault(
                _LOCKED_ADMISSION_RECEIPTS_SESSION_KEY, {}
            )
            receipts[normalized_scope] = {
                "transaction_id": transaction_id,
                "backend_pid": backend_pid,
                "lock_receipt_id": lock_receipt_id,
                "account_snapshot_sha256": account_snapshot.snapshot_sha256,
                "snapshot_content_sha256": snapshot.content_sha256,
            }
            return snapshot

    def lock_alpaca_paper_admission_bundle(
        self,
        *,
        broker_account_facts: AlpacaPaperBrokerAccountFacts,
        symbol: str,
        correlation_cluster: str,
        session: Session,
        buying_power_double_census: (
            PreparedAlpacaPaperBuyingPowerDoubleCensus | None
        ) = None,
    ) -> LockedAlpacaPaperAdmissionBundle:
        """Issue one final PAPER risk bundle under A1/A2/head/reservation locks.

        The broker facts must already have been captured before this method is
        called.  No provider/network callback is accepted or invoked while the
        database locks are held.  The decision clock is issued only after the
        settlement head, immutable chain and ordered reservation projection are
        stable in this transaction.
        """

        if type(broker_account_facts) is not AlpacaPaperBrokerAccountFacts:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER broker account facts are missing"
            )
        try:
            account_authority = verify_captured_alpaca_paper_account_authority(
                broker_account_facts.capture_authority
            )
        except CapturedAlpacaPaperReadError as exc:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER broker account facts lack capture authority"
            ) from exc
        normalized_symbol = _norm(symbol, "symbol", upper=True)
        normalized_cluster = _norm(
            correlation_cluster, "correlation_cluster", lower=True
        )
        normalized_decision_id = account_authority.decision_id
        normalized_run_id = account_authority.run_id
        generation = account_authority.generation
        expires_at = account_authority.expires_at
        account_scope = "alpaca:paper"
        with self._transaction(session) as owned:
            acquire_adaptive_risk_account_locks(
                owned,
                account_scope=account_scope,
            )
            transaction_id, backend_pid = self._transaction_identity(owned)
            lock_receipt_id = str(uuid.uuid4())

            head = owned.scalar(
                select(AlpacaPaperAccountSettlementHead)
                .where(
                    AlpacaPaperAccountSettlementHead.account_scope
                    == account_scope,
                    AlpacaPaperAccountSettlementHead.account_identity_sha256
                    == broker_account_facts.account_identity_sha256,
                )
                .with_for_update()
            )
            zero_head_requires_insert = head is None
            if head is None:
                head = new_zero_settlement_head(
                    account_identity_sha256=(
                        broker_account_facts.account_identity_sha256
                    )
                )
            else:
                try:
                    verify_settlement_head_content(head)
                except AlpacaCycleSettlementIntegrityError as exc:
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER settlement head failed verification"
                    ) from exc

            aggregates, ledger_payload = self._active_ledger(
                owned,
                account_scope=account_scope,
                symbol=normalized_symbol,
                correlation_cluster=normalized_cluster,
            )
            self._raise_if_pending_settlement(ledger_payload)
            self._raise_if_quarantined_exposure(ledger_payload)
            settlements = list(
                owned.scalars(
                    select(AlpacaPaperCycleSettlement)
                    .where(
                        AlpacaPaperCycleSettlement.account_scope
                        == account_scope,
                        AlpacaPaperCycleSettlement.account_identity_sha256
                        == broker_account_facts.account_identity_sha256,
                    )
                    .order_by(AlpacaPaperCycleSettlement.terminal_sequence)
                )
            )
            terminal_event_sha256s = tuple(
                row.terminal_fill_event_sha256 for row in settlements
            )
            terminal_fill_rows = (
                list(
                    owned.scalars(
                        select(AlpacaPaperFillActivity).where(
                            AlpacaPaperFillActivity.event_sha256.in_(
                                terminal_event_sha256s
                            )
                        )
                    )
                )
                if terminal_event_sha256s
                else []
            )
            terminal_fills = {
                row.event_sha256: row for row in terminal_fill_rows
            }
            settlement_sha256s = tuple(
                row.settlement_sha256 for row in settlements
            )
            terminal_receipt_rows = (
                list(
                    owned.scalars(
                        select(AlpacaPaperTerminalFillObservationReceipt).where(
                            AlpacaPaperTerminalFillObservationReceipt.settlement_sha256.in_(
                                settlement_sha256s
                            )
                        )
                    )
                )
                if settlement_sha256s
                else []
            )
            terminal_observation_receipts = {
                row.settlement_sha256: row for row in terminal_receipt_rows
            }
            observation_sha256s = tuple(
                row.observation_sha256 for row in terminal_receipt_rows
            )
            observation_rows = (
                list(
                    owned.scalars(
                        select(AlpacaPaperFillQueryObservation).where(
                            AlpacaPaperFillQueryObservation.observation_sha256.in_(
                                observation_sha256s
                            )
                        )
                    )
                )
                if observation_sha256s
                else []
            )
            fill_observations = {
                row.observation_sha256: row for row in observation_rows
            }
            observation_mapping_rows = (
                list(
                    owned.scalars(
                        select(AlpacaPaperFillObservationActivity).where(
                            AlpacaPaperFillObservationActivity.observation_sha256.in_(
                                observation_sha256s
                            ),
                            AlpacaPaperFillObservationActivity.fill_event_sha256.in_(
                                terminal_event_sha256s
                            ),
                        )
                    )
                )
                if observation_sha256s and terminal_event_sha256s
                else []
            )
            fill_observation_mappings = {
                (row.observation_sha256, row.fill_event_sha256): row
                for row in observation_mapping_rows
            }

            # This is the final live decision clock.  Caller-supplied intent
            # time is never accepted as economic authority.
            decision_as_of = self._clock(owned)
            if (
                broker_account_facts.observed_at > decision_as_of
                or broker_account_facts.available_at > decision_as_of
                or decision_as_of > expires_at
            ):
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER broker/capture clock is unavailable at final decision"
                )
            try:
                current_authority = (
                    verify_captured_alpaca_paper_account_authority(
                        broker_account_facts.capture_authority
                    )
                )
            except CapturedAlpacaPaperReadError as exc:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER capture authority changed under account locks"
                ) from exc
            if current_authority is not account_authority:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER capture authority identity changed"
                )
            pending_bp = _money(
                aggregates["pending_buying_power_impact_usd"]
            )
            prepared_bp_reflection: (
                _PreparedAlpacaPaperBuyingPowerReflection | None
            ) = None
            if pending_bp > 0:
                if type(buying_power_double_census) is not (
                    PreparedAlpacaPaperBuyingPowerDoubleCensus
                ):
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER pending buying-power reflection receipt unavailable"
                    )
                prepared_bp_reflection = (
                    self._prepare_alpaca_paper_buying_power_reflection(
                        owned,
                        account_authority=account_authority,
                        batch=buying_power_double_census,
                        aggregates=aggregates,
                        ledger_payload=ledger_payload,
                        decision_as_of=decision_as_of,
                    )
                )
            elif buying_power_double_census is not None:
                if type(buying_power_double_census) is not (
                    PreparedAlpacaPaperBuyingPowerDoubleCensus
                ):
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER buying-power double census type is invalid"
                    )
                try:
                    verified_empty_batch = (
                        verify_alpaca_paper_buying_power_double_census(
                            buying_power_double_census,
                            verified_at=decision_as_of,
                        )
                    )
                except AlpacaBuyingPowerReflectionError as exc:
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER buying-power double census failed verification"
                    ) from exc
                if verified_empty_batch.account_authority is not account_authority:
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER buying-power census account authority changed"
                    )
            reflected_pending_bp = (
                Decimal("0")
                if prepared_bp_reflection is None
                else prepared_bp_reflection.reflected_pending_buying_power_usd
            )
            daily, terminal_fill_authority_sha256 = (
                _derive_verified_alpaca_paper_daily_pnl_evidence(
                 head=head,
                 settlements=settlements,
                 terminal_fills=terminal_fills,
                 terminal_observation_receipts=terminal_observation_receipts,
                 fill_observations=fill_observations,
                 fill_observation_mappings=fill_observation_mappings,
                 decision_as_of=decision_as_of,
            )
            )
            account_snapshot = ImmutableAccountRiskSnapshot(
                snapshot_id=broker_account_facts.snapshot_id,
                source=broker_account_facts.source,
                provider_generation=broker_account_facts.provider_generation,
                account_scope=account_scope,
                execution_family="alpaca_spot",
                broker_environment="paper",
                venue="alpaca",
                account_identity_sha256=(
                    broker_account_facts.account_identity_sha256
                ),
                observed_at=broker_account_facts.observed_at,
                available_at=broker_account_facts.available_at,
                equity_usd=broker_account_facts.equity_usd,
                buying_power_usd=broker_account_facts.buying_power_usd,
                broker_day_change_usd=(
                    broker_account_facts.broker_day_change_usd
                ),
                local_realized_pnl_usd=float(daily.local_realized_pnl_usd),
                pending_policy_buying_power_reflected_usd=float(
                    reflected_pending_bp
                ),
            )
            policy_capacity = (
                account_snapshot.buying_power_usd
                + float(aggregates["open_buying_power_impact_usd"])
                + float(reflected_pending_bp)
            )
            locked_snapshot = LockedAdaptiveRiskAdmissionSnapshot.create(
                account_scope=account_scope,
                symbol=normalized_symbol,
                correlation_cluster=normalized_cluster,
                account_snapshot_sha256=account_snapshot.snapshot_sha256,
                transaction_id=transaction_id,
                backend_pid=backend_pid,
                lock_receipt_id=lock_receipt_id,
                observed_at=decision_as_of,
                aggregates=aggregates,
                ledger_payload=ledger_payload,
                policy_buying_power_capacity_usd=policy_capacity,
            )
            buying_power_reflection_receipt_sha256: str | None = None
            if zero_head_requires_insert or prepared_bp_reflection is not None:
                # Re-read the DB clock immediately before the first mutation.
                # Census verification and local row matching above are pure;
                # neither may consume an expired capture authority.
                mutation_clock = self._clock(owned)
                if (
                    mutation_clock > expires_at
                    or mutation_clock.astimezone(ET).date()
                    != daily.risk_date_et
                ):
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER capture authority expired before first mutation"
                    )
                try:
                    verify_captured_alpaca_paper_account_authority(
                        broker_account_facts.capture_authority
                    )
                except CapturedAlpacaPaperReadError as exc:
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER capture authority changed before first mutation"
                    ) from exc
            if zero_head_requires_insert:
                owned.add(head)
                owned.flush([head])
            if prepared_bp_reflection is not None:
                buying_power_reflection_receipt_sha256 = (
                    self._persist_alpaca_paper_buying_power_reflection(
                        owned,
                        prepared=prepared_bp_reflection,
                        account_snapshot=account_snapshot,
                        open_buying_power_impact_usd=_money(
                            aggregates["open_buying_power_impact_usd"]
                        ),
                    )
                )
            daily_risk_evidence = alpaca_paper_settled_daily_pnl_risk_evidence(
                daily,
                terminal_fill_authority_sha256=(
                    terminal_fill_authority_sha256
                ),
            )
            account_evidence = broker_account_facts.account_evidence
            bundle_body = {
                "schema_version": "chili.locked-alpaca-paper-admission-bundle.v3",
                "account_snapshot_sha256": account_snapshot.snapshot_sha256,
                "settled_daily_pnl_evidence_sha256": daily.evidence_sha256,
                "locked_risk_snapshot_content_sha256": (
                    locked_snapshot.content_sha256
                ),
                "account_evidence": asdict(account_evidence),
                "daily_pnl_evidence": asdict(daily_risk_evidence),
                "account_id": account_authority.account_id,
                "account_payload_sha256": (
                    account_authority.account_payload_sha256
                ),
                "account_read_receipt_sha256": (
                    account_authority.account_read_receipt_sha256
                ),
                "active_input_attestation_sha256": (
                    account_authority.active_input_attestation_sha256
                ),
                "account_capture_authority_sha256": (
                    account_authority.authority_sha256
                ),
                "daily_terminal_fill_authority_sha256": (
                    terminal_fill_authority_sha256
                ),
                "buying_power_reflection_receipt_sha256": (
                    buying_power_reflection_receipt_sha256
                ),
            }
            bundle_sha = _sha256_json(bundle_body)
            attestation_body = {
                "schema_version": (
                    "chili.locked-alpaca-paper-daily-pnl-attestation.v3"
                ),
                "account_scope": account_scope,
                "account_id": account_authority.account_id,
                "account_identity_sha256": (
                    broker_account_facts.account_identity_sha256
                ),
                "decision_id": normalized_decision_id,
                "run_id": normalized_run_id,
                "generation": int(generation),
                "broker_provider_generation": (
                    broker_account_facts.provider_generation
                ),
                "decision_as_of": decision_as_of,
                "expires_at": expires_at,
                "transaction_id": transaction_id,
                "backend_pid": backend_pid,
                "lock_receipt_id": lock_receipt_id,
                "account_snapshot_sha256": account_snapshot.snapshot_sha256,
                "account_payload_sha256": (
                    account_authority.account_payload_sha256
                ),
                "account_read_receipt_sha256": (
                    account_authority.account_read_receipt_sha256
                ),
                "active_input_attestation_sha256": (
                    account_authority.active_input_attestation_sha256
                ),
                "account_capture_authority_sha256": (
                    account_authority.authority_sha256
                ),
                "daily_pnl_evidence_sha256": daily.evidence_sha256,
                "daily_terminal_fill_authority_sha256": (
                    terminal_fill_authority_sha256
                ),
                "buying_power_reflection_receipt_sha256": (
                    buying_power_reflection_receipt_sha256
                ),
                "ledger_snapshot_content_sha256": locked_snapshot.content_sha256,
                "ledger_sha256": locked_snapshot.ledger_sha256,
                "bundle_sha256": bundle_sha,
            }
            attestation = LockedAlpacaPaperDailyPnlAttestation(
                **{
                    name: value
                    for name, value in attestation_body.items()
                    if name != "schema_version"
                },
                _verification_tag=_locked_alpaca_paper_attestation_tag(
                    attestation_body
                ),
                _verification_token=_LOCKED_ALPACA_PAPER_ADMISSION_TOKEN,
            )
            bundle = LockedAlpacaPaperAdmissionBundle(
                account_snapshot=account_snapshot,
                settled_daily_pnl_evidence=daily,
                locked_risk_snapshot=locked_snapshot,
                attestation=attestation,
                account_evidence=account_evidence,
                daily_pnl_evidence=daily_risk_evidence,
                account_id=account_authority.account_id,
                account_payload_sha256=(
                    account_authority.account_payload_sha256
                ),
                account_read_receipt_sha256=(
                    account_authority.account_read_receipt_sha256
                ),
                active_input_attestation_sha256=(
                    account_authority.active_input_attestation_sha256
                ),
                account_capture_authority_sha256=(
                    account_authority.authority_sha256
                ),
                daily_terminal_fill_authority_sha256=(
                    terminal_fill_authority_sha256
                ),
                buying_power_reflection_receipt_sha256=(
                    buying_power_reflection_receipt_sha256
                ),
            )

            base_receipts = owned.info.setdefault(
                _LOCKED_ADMISSION_RECEIPTS_SESSION_KEY, {}
            )
            base_receipts[account_scope] = {
                "transaction_id": transaction_id,
                "backend_pid": backend_pid,
                "lock_receipt_id": lock_receipt_id,
                "account_snapshot_sha256": account_snapshot.snapshot_sha256,
                "snapshot_content_sha256": locked_snapshot.content_sha256,
            }
            paper_receipts = owned.info.setdefault(
                _LOCKED_ALPACA_PAPER_BUNDLE_RECEIPTS_SESSION_KEY, {}
            )
            paper_receipts[account_scope] = {
                "transaction_id": transaction_id,
                "backend_pid": backend_pid,
                "lock_receipt_id": lock_receipt_id,
                "decision_id": normalized_decision_id,
                "run_id": normalized_run_id,
                "generation": int(generation),
                "account_snapshot_sha256": account_snapshot.snapshot_sha256,
                "account_id": account_authority.account_id,
                "account_payload_sha256": (
                    account_authority.account_payload_sha256
                ),
                "account_read_receipt_sha256": (
                    account_authority.account_read_receipt_sha256
                ),
                "active_input_attestation_sha256": (
                    account_authority.active_input_attestation_sha256
                ),
                "account_capture_authority_sha256": (
                    account_authority.authority_sha256
                ),
                "daily_pnl_evidence_sha256": daily.evidence_sha256,
                "daily_terminal_fill_authority_sha256": (
                    terminal_fill_authority_sha256
                ),
                "buying_power_reflection_receipt_sha256": (
                    buying_power_reflection_receipt_sha256
                ),
                "ledger_snapshot_content_sha256": locked_snapshot.content_sha256,
                "ledger_sha256": locked_snapshot.ledger_sha256,
                "bundle_sha256": bundle.bundle_sha256,
                "verification_tag": attestation._verification_tag,
            }
            return bundle

    @staticmethod
    def _raise_if_pending_settlement(
        ledger_payload: Mapping[str, Any],
        *,
        locked_snapshot: LockedAdaptiveRiskAdmissionSnapshot | None = None,
    ) -> None:
        pending = ledger_payload.get("pending_settlements")
        if not isinstance(pending, list):
            raise AdaptiveRiskContractError(
                "adaptive reservation ledger lacks pending-settlement coverage"
            )
        if pending:
            raise AdaptiveRiskPendingSettlement(
                account_scope=str(ledger_payload.get("account_scope") or ""),
                pending_settlements=pending,
                locked_snapshot=locked_snapshot,
            )

    @staticmethod
    def _raise_if_quarantined_exposure(
        ledger_payload: Mapping[str, Any],
        *,
        locked_snapshot: LockedAdaptiveRiskAdmissionSnapshot | None = None,
    ) -> None:
        quarantined = ledger_payload.get("quarantined_exposures")
        if not isinstance(quarantined, list):
            raise AdaptiveRiskContractError(
                "adaptive reservation ledger lacks exposure-quarantine coverage"
            )
        if quarantined:
            raise AdaptiveRiskExposureQuarantined(
                account_scope=str(ledger_payload.get("account_scope") or ""),
                quarantined_exposures=quarantined,
                locked_snapshot=locked_snapshot,
            )

    @staticmethod
    def _account_snapshot_rejections(
        request: AdaptiveRiskReservationRequest,
    ) -> list[str]:
        return list(adaptive_risk_reservation_request_rejections(request))

    @staticmethod
    def _db_paper_position_bindings(
        session: Session,
        *,
        account_scope: str,
        rows: list[AdaptiveRiskReservation],
    ) -> list[dict[str, Any]]:
        paper_sessions = list(
            session.scalars(
                select(TradingAutomationSession)
                .where(TradingAutomationSession.mode == "paper")
                .where(
                    text(
                        "risk_snapshot_json #> "
                        "'{momentum_paper_execution,position}' IS NOT NULL "
                        "AND risk_snapshot_json #> "
                        "'{momentum_paper_execution,position}' <> 'null'::jsonb"
                    )
                )
                .order_by(TradingAutomationSession.id)
                .with_for_update()
            )
        )
        reservations_by_id = {str(row.reservation_id): row for row in rows}
        position_bindings: list[dict[str, Any]] = []
        matched_open_reservations: set[str] = set()
        for paper_session in paper_sessions:
            snapshot = dict(paper_session.risk_snapshot_json or {})
            binding = snapshot.get("db_paper_account_binding")
            binding = dict(binding) if isinstance(binding, Mapping) else {}
            bound_scope = str(binding.get("account_scope") or "").strip()
            pe = snapshot.get("momentum_paper_execution")
            pe = dict(pe) if isinstance(pe, Mapping) else {}
            position = pe.get("position")
            if not isinstance(position, Mapping):
                continue
            if not bound_scope:
                raise AdaptiveRiskContractError(
                    "unscoped open DB-paper position blocks adaptive admission"
                )
            if bound_scope != account_scope:
                continue
            pe_scope = str(pe.get("adaptive_risk_account_scope") or "").strip()
            reservation_id = str(
                pe.get("adaptive_risk_reservation_id") or ""
            ).strip()
            reservation = reservations_by_id.get(reservation_id)
            if pe_scope != account_scope or reservation is None:
                raise AdaptiveRiskContractError(
                    "unledgered open DB-paper position blocks adaptive admission"
                )
            try:
                position_quantity = int(position.get("quantity"))
            except (TypeError, ValueError, OverflowError) as exc:
                raise AdaptiveRiskContractError(
                    "open DB-paper position quantity is invalid"
                ) from exc
            if (
                reservation.state != "filled"
                or reservation.symbol != str(paper_session.symbol or "").strip().upper()
                or int(reservation.open_quantity_shares) != position_quantity
                or position_quantity <= 0
            ):
                raise AdaptiveRiskContractError(
                    "open DB-paper position differs from adaptive ledger"
                )
            matched_open_reservations.add(reservation_id)
            position_bindings.append(
                {
                    "session_id": int(paper_session.id),
                    "reservation_id": reservation_id,
                    "symbol": reservation.symbol,
                    "open_quantity_shares": position_quantity,
                    "position_sha256": _sha256_json(dict(position)),
                }
            )
        unmatched_open = sorted(
            str(row.reservation_id)
            for row in rows
            if int(row.open_quantity_shares) > 0
            and str(row.reservation_id) not in matched_open_reservations
        )
        if unmatched_open:
            raise AdaptiveRiskContractError(
                "adaptive ledger open exposure lacks canonical DB-paper position: "
                + ",".join(unmatched_open)
            )
        return position_bindings

    @staticmethod
    def _active_ledger(
        session: Session,
        *,
        account_scope: str,
        symbol: str,
        correlation_cluster: str,
    ) -> tuple[dict[str, float], dict[str, Any]]:
        rows = list(
            session.scalars(
                select(AdaptiveRiskReservation)
                .where(AdaptiveRiskReservation.account_scope == account_scope)
                .where(
                    (
                        AdaptiveRiskReservation.pending_structural_risk_usd > 0
                    )
                    | (AdaptiveRiskReservation.open_structural_risk_usd > 0)
                    | (
                        AdaptiveRiskReservation.state
                        == "flat_pending_settlement"
                    )
                    | (
                        AdaptiveRiskReservation.state
                        == "exposure_quarantined"
                    )
                )
                .order_by(AdaptiveRiskReservation.reservation_id)
                .with_for_update()
            )
        )
        position_bindings = (
            AdaptiveRiskReservationStore._db_paper_position_bindings(
                session,
                account_scope=account_scope,
                rows=rows,
            )
            if account_scope.startswith("db-paper:")
            else []
        )

        def total(field: str, predicate=lambda _row: True) -> float:
            return float(
                sum(
                    (Decimal(getattr(row, field)) for row in rows if predicate(row)),
                    Decimal("0"),
                )
            )

        same_symbol = lambda row: row.symbol == symbol
        same_cluster = lambda row: row.correlation_cluster == correlation_cluster
        aggregates = {
            "open_structural_risk_usd": total("open_structural_risk_usd"),
            "pending_reserved_risk_usd": total("pending_structural_risk_usd"),
            "existing_same_symbol_structural_risk_usd": total(
                "open_structural_risk_usd", same_symbol
            ),
            "pending_same_symbol_structural_risk_usd": total(
                "pending_structural_risk_usd", same_symbol
            ),
            "current_cluster_structural_risk_usd": total(
                "open_structural_risk_usd", same_cluster
            ),
            "pending_correlation_cluster_risk_usd": total(
                "pending_structural_risk_usd", same_cluster
            ),
            "portfolio_gross_notional_usd": total("open_gross_notional_usd"),
            "pending_portfolio_gross_notional_usd": total(
                "pending_gross_notional_usd"
            ),
            "open_buying_power_impact_usd": total(
                "open_buying_power_impact_usd"
            ),
            "pending_buying_power_impact_usd": total(
                "pending_buying_power_impact_usd"
            ),
        }
        payload = {
            "schema_version": RESERVATION_LEDGER_GENERATION,
            "account_scope": account_scope,
            "aggregates": aggregates,
            "active_reservations": [
                {
                    "reservation_id": str(row.reservation_id),
                    "decision_packet_sha256": row.decision_packet_sha256,
                    "symbol": row.symbol,
                    "correlation_cluster": row.correlation_cluster,
                    "state": row.state,
                    "planned_quantity_shares": int(row.planned_quantity_shares),
                    "cumulative_filled_quantity_shares": int(
                        row.cumulative_filled_quantity_shares
                    ),
                    "open_quantity_shares": int(row.open_quantity_shares),
                    "pending_structural_risk_usd": Decimal(
                        row.pending_structural_risk_usd
                    ),
                    "pending_gross_notional_usd": Decimal(
                        row.pending_gross_notional_usd
                    ),
                    "pending_buying_power_impact_usd": Decimal(
                        row.pending_buying_power_impact_usd
                    ),
                    "open_structural_risk_usd": Decimal(
                        row.open_structural_risk_usd
                    ),
                    "open_gross_notional_usd": Decimal(
                        row.open_gross_notional_usd
                    ),
                    "open_buying_power_impact_usd": Decimal(
                        row.open_buying_power_impact_usd
                    ),
                    "version": int(row.version),
                }
                for row in rows
            ],
            "pending_settlements": [
                {
                    "reservation_id": str(row.reservation_id),
                    "decision_packet_sha256": row.decision_packet_sha256,
                    "symbol": row.symbol,
                    "trading_date": row.trading_date.isoformat(),
                    "setup_family": row.setup_family,
                    "state": row.state,
                    "cumulative_filled_quantity_shares": int(
                        row.cumulative_filled_quantity_shares
                    ),
                    "last_broker_available_at": row.last_broker_available_at,
                    "last_source_event_content_sha256": (
                        row.last_source_event_content_sha256
                    ),
                    "version": int(row.version),
                }
                for row in rows
                if row.state == "flat_pending_settlement"
            ],
            "quarantined_exposures": [
                {
                    "reservation_id": str(row.reservation_id),
                    "decision_packet_sha256": row.decision_packet_sha256,
                    "symbol": row.symbol,
                    "trading_date": row.trading_date.isoformat(),
                    "setup_family": row.setup_family,
                    "state": row.state,
                    "cumulative_filled_quantity_shares": int(
                        row.cumulative_filled_quantity_shares
                    ),
                    "open_quantity_shares": int(row.open_quantity_shares),
                    "pending_structural_risk_usd": Decimal(
                        row.pending_structural_risk_usd
                    ),
                    "open_structural_risk_usd": Decimal(
                        row.open_structural_risk_usd
                    ),
                    "contradiction_source_state": (
                        row.lifecycle_contradiction_source_state
                    ),
                    "contradiction_at": row.lifecycle_contradiction_at,
                    "contradiction_evidence_sha256": (
                        row.lifecycle_contradiction_evidence_sha256
                    ),
                    "version": int(row.version),
                }
                for row in rows
                if row.state == "exposure_quarantined"
            ],
            "paper_position_bindings": position_bindings,
        }
        return aggregates, payload

    @staticmethod
    def _prepare_alpaca_paper_buying_power_reflection(
        session: Session,
        *,
        account_authority: CapturedAlpacaPaperAccountAuthority,
        batch: PreparedAlpacaPaperBuyingPowerDoubleCensus,
        aggregates: Mapping[str, float],
        ledger_payload: Mapping[str, Any],
        decision_as_of: datetime,
    ) -> _PreparedAlpacaPaperBuyingPowerReflection:
        """Match every locked pending claim to exact stable broker order truth."""

        try:
            verified = verify_alpaca_paper_buying_power_double_census(
                batch,
                verified_at=decision_as_of,
            )
        except AlpacaBuyingPowerReflectionError as exc:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER buying-power double census failed verification"
            ) from exc
        if verified.account_authority is not account_authority:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER buying-power census account authority changed"
            )
        before = verified.before
        after = verified.after
        if (
            before.adapter_connection_generation
            != after.adapter_connection_generation
            or before.adapter_build_sha256 != after.adapter_build_sha256
            or before.inventory_canonical_json != after.inventory_canonical_json
            or before.inventory_sha256 != after.inventory_sha256
        ):
            raise AdaptiveRiskContractError(
                "Alpaca PAPER buying-power census changed across account read"
            )

        pending_total = _money(aggregates["pending_buying_power_impact_usd"])
        rows = list(
            session.scalars(
                select(AdaptiveRiskReservation)
                .where(AdaptiveRiskReservation.account_scope == "alpaca:paper")
                .where(AdaptiveRiskReservation.pending_buying_power_impact_usd > 0)
                .order_by(AdaptiveRiskReservation.reservation_id)
                .with_for_update()
            )
        )
        row_total = sum(
            (_money(row.pending_buying_power_impact_usd) for row in rows),
            Decimal("0"),
        )
        if row_total != pending_total or not rows:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER pending buying-power ledger changed under lock"
            )

        packet_hashes = tuple(row.decision_packet_sha256 for row in rows)
        packets = {
            packet.decision_packet_sha256: packet
            for packet in session.scalars(
                select(AdaptiveRiskDecisionPacket).where(
                    AdaptiveRiskDecisionPacket.decision_packet_sha256.in_(
                        packet_hashes
                    )
                )
            )
        }
        if set(packets) != set(packet_hashes):
            raise AdaptiveRiskContractError(
                "Alpaca PAPER pending decision packet inventory is incomplete"
            )
        client_order_ids = tuple(
            packets[row.decision_packet_sha256].client_order_id for row in rows
        )
        actions = list(
            session.scalars(
                select(BrokerSymbolActionClaim)
                .where(BrokerSymbolActionClaim.account_scope == "alpaca:paper")
                .where(BrokerSymbolActionClaim.client_order_id.in_(client_order_ids))
                .order_by(BrokerSymbolActionClaim.symbol)
                .with_for_update()
            )
        )
        action_by_cid = {
            str(action.client_order_id): action for action in actions
        }
        outboxes = list(
            session.scalars(
                select(CapturedPaperPostCommitOutbox)
                .where(CapturedPaperPostCommitOutbox.account_scope == "alpaca:paper")
                .where(
                    CapturedPaperPostCommitOutbox.client_order_id.in_(
                        client_order_ids
                    )
                )
                .order_by(CapturedPaperPostCommitOutbox.completion_sha256)
                .with_for_update()
            )
        )
        outbox_by_cid = {row.client_order_id: row for row in outboxes}
        if (
            len(action_by_cid) != len(rows)
            or len(outbox_by_cid) != len(rows)
        ):
            raise AdaptiveRiskContractError(
                "Alpaca PAPER pending transport inventory is incomplete"
            )

        from .captured_paper_entry_intent import CapturedPaperPostCommitRequest

        inventory_by_cid: dict[str, dict[str, Any]] = {}
        for provider_order in before.orders:
            cid = _optional_norm(provider_order.get("client_order_id"))
            if cid is None or cid in inventory_by_cid:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER open-order census CID inventory is ambiguous"
                )
            inventory_by_cid[cid] = dict(provider_order)

        item_bodies: list[dict[str, Any]] = []
        reflected_total = Decimal("0")
        pretransport_statuses = {
            "pending",
            "leased",
            "retry_wait",
            "retry_exhausted",
        }
        transported_statuses = {
            "transport_started",
            "transport_indeterminate",
            "reconciling",
            "completed",
        }
        for ordinal, row in enumerate(rows):
            packet = packets[row.decision_packet_sha256]
            cid = packet.client_order_id
            action = action_by_cid.get(cid)
            outbox = outbox_by_cid.get(cid)
            if action is None or outbox is None:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER pending transport binding disappeared"
                )
            try:
                post_commit = CapturedPaperPostCommitRequest.from_canonical_json(
                    outbox.payload_canonical_json
                )
            except Exception as exc:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER pending outbox payload failed verification"
                ) from exc
            if (
                hashlib.sha256(
                    outbox.payload_canonical_json.encode("utf-8")
                ).hexdigest()
                != outbox.payload_sha256
                or post_commit.completion_sha256 != outbox.completion_sha256
                or post_commit.intent.client_order_id != cid
                or post_commit.intent.binder_id != str(outbox.binder_id)
                or post_commit.intent.symbol_claim_token
                != outbox.symbol_claim_token
                or post_commit.intent.route_token.symbol != row.symbol
                or post_commit.intent.route_token.account_scope != "alpaca:paper"
            ):
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER pending outbox content changed"
                )
            metadata = action.metadata_json
            if type(metadata) is not dict:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER action-claim metadata is unavailable"
                )
            order_request = metadata.get("order_request")
            if type(order_request) is not dict:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER pending order request is unavailable"
                )
            expected_request_fields = {
                "asset_class",
                "client_order_id",
                "extended_hours",
                "limit_price",
                "position_intent",
                "qty",
                "side",
                "symbol",
                "time_in_force",
                "type",
            }
            if set(order_request) != expected_request_fields:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER pending order request shape changed"
                )
            planned = int(row.planned_quantity_shares)
            cumulative = int(row.cumulative_filled_quantity_shares)
            remaining = planned - cumulative
            if planned <= 0 or cumulative < 0 or remaining <= 0:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER pending reservation quantity is invalid"
                )
            pending = _money(row.pending_buying_power_impact_usd)
            per_share_bp = _money(
                Decimal(row.planned_buying_power_impact_usd)
                / Decimal(planned)
            )
            if _money(per_share_bp * Decimal(remaining)) != pending:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER pending buying-power basis changed"
                )
            entry_limit = _money(packet.entry_limit_price)
            local_tif = _norm(
                order_request.get("time_in_force"),
                "local_time_in_force",
                lower=True,
            )
            local_extended = order_request.get("extended_hours")
            local_intent = _norm(
                order_request.get("position_intent"),
                "local_position_intent",
                lower=True,
            )
            if not (
                packet.account_scope == "alpaca:paper"
                and packet.execution_surface == "alpaca_paper"
                and packet.execution_family == "alpaca_spot"
                and packet.broker_environment == "paper"
                and packet.resolved_quantity_shares == planned
                and action.action == "entry"
                and action.symbol == row.symbol
                and action.claim_token == outbox.symbol_claim_token
                and action.owner_session_id == outbox.session_id
                and str(action.client_order_id) == cid
                and metadata.get("adaptive_risk_reservation_id")
                == str(row.reservation_id)
                and metadata.get("entry_post_bind_token")
                == str(outbox.binder_id)
                and metadata.get("alpaca_account_id")
                == account_authority.account_id
                and order_request.get("asset_class") == "us_equity"
                and order_request.get("client_order_id") == cid
                and order_request.get("symbol") == row.symbol
                and order_request.get("side") == "buy"
                and order_request.get("type") == "limit"
                and order_request.get("qty") == str(planned)
                and _money(
                    _exact_nonnegative_decimal(
                        order_request.get("limit_price"),
                        "local_order_request.limit_price",
                    )
                )
                == entry_limit
                and type(local_extended) is bool
                and local_tif in {"day", "gtc"}
                and (local_extended is not True or local_tif == "day")
                and local_intent == "buy_to_open"
            ):
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER pending local order binding changed"
                )

            provider_order = inventory_by_cid.get(cid)
            pretransport = outbox.status in pretransport_statuses
            transported = outbox.status in transported_statuses
            if pretransport == transported:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER pending transport phase is invalid"
                )
            provider_fields: dict[str, Any]
            if pretransport:
                if not (
                    action.phase == "claimed"
                    and action.broker_order_id is None
                    and metadata.get("entry_transport_started") is None
                    and row.state == "reserved"
                    and cumulative == 0
                    and row.broker_order_id is None
                    and row.broker_source is None
                    and row.broker_connection_generation is None
                    and outbox.transport_started_at is None
                    and outbox.transport_evidence_sha256 is None
                    and provider_order is None
                ):
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER pre-transport claim is not unreflected"
                    )
                classification = "unreflected_pre_transport"
                provider_fields = {
                    "provider_order_id": None,
                    "provider_client_order_id": None,
                    "provider_order_sha256": None,
                    "provider_order_status": None,
                    "provider_order_side": None,
                    "provider_order_type": None,
                    "provider_quantity_shares": None,
                    "provider_filled_quantity_shares": None,
                    "provider_remaining_quantity_shares": None,
                    "provider_limit_price": None,
                    "provider_asset_class": None,
                    "provider_time_in_force": None,
                    "provider_extended_hours": None,
                    "provider_position_intent": None,
                }
            else:
                marker = metadata.get("entry_transport_started")
                if not (
                    type(marker) is dict
                    and action.phase in {"submit_indeterminate", "submitted"}
                    and outbox.transport_started_at is not None
                    and _sha(outbox.transport_evidence_sha256)
                    and provider_order is not None
                ):
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER transported claim lacks exact broker order"
                    )
                provider_order_id = _norm(
                    provider_order.get("id"), "provider_order_id"
                )
                provider_cid = _norm(
                    provider_order.get("client_order_id"),
                    "provider_client_order_id",
                )
                provider_symbol = _norm(
                    provider_order.get("symbol"),
                    "provider_symbol",
                    upper=True,
                )
                provider_status = _norm(
                    provider_order.get("status"),
                    "provider_order_status",
                    lower=True,
                )
                provider_side = _norm(
                    provider_order.get("side"),
                    "provider_order_side",
                    lower=True,
                )
                order_type = _optional_norm(
                    provider_order.get("order_type"), lower=True
                )
                type_alias = _optional_norm(
                    provider_order.get("type"), lower=True
                )
                if (
                    order_type is not None
                    and type_alias is not None
                    and order_type != type_alias
                ):
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER provider order type aliases differ"
                    )
                provider_type = order_type or type_alias
                provider_qty = _exact_nonnegative_integer(
                    provider_order.get("qty"), "provider_order.qty"
                )
                provider_filled = _exact_nonnegative_integer(
                    provider_order.get("filled_qty"),
                    "provider_order.filled_qty",
                )
                provider_remaining = provider_qty - provider_filled
                provider_limit = _money(
                    _exact_nonnegative_decimal(
                        provider_order.get("limit_price"),
                        "provider_order.limit_price",
                    )
                )
                provider_asset = _norm(
                    provider_order.get("asset_class"),
                    "provider_order.asset_class",
                    lower=True,
                )
                provider_tif = _norm(
                    provider_order.get("time_in_force"),
                    "provider_order.time_in_force",
                    lower=True,
                )
                provider_extended = provider_order.get("extended_hours")
                provider_intent = _optional_norm(
                    provider_order.get("position_intent"), lower=True
                )
                provider_account = _optional_norm(
                    provider_order.get("account_id")
                )
                if not (
                    provider_order_id == row.broker_order_id
                    == action.broker_order_id
                    and provider_cid == cid
                    and provider_symbol == row.symbol
                    and provider_status in _ALPACA_OPEN_ORDER_STATUSES
                    and provider_side == "buy"
                    and provider_type == "limit"
                    and provider_qty == planned
                    and provider_filled == cumulative
                    and provider_remaining == remaining
                    and provider_remaining > 0
                    and provider_limit == entry_limit
                    and provider_asset == "us_equity"
                    and provider_tif == local_tif
                    and type(provider_extended) is bool
                    and provider_extended == local_extended
                    and provider_intent in {None, local_intent}
                    and provider_account
                    in {None, account_authority.account_id}
                    and row.broker_source == "alpaca"
                    and row.broker_connection_generation
                    == before.adapter_connection_generation
                ):
                    raise AdaptiveRiskContractError(
                        "Alpaca PAPER provider order differs from pending claim"
                    )
                classification = "reflected_open_limit"
                reflected_total += pending
                provider_fields = {
                    "provider_order_id": provider_order_id,
                    "provider_client_order_id": provider_cid,
                    "provider_order_sha256": _sha256_json(provider_order),
                    "provider_order_status": provider_status,
                    "provider_order_side": provider_side,
                    "provider_order_type": provider_type,
                    "provider_quantity_shares": provider_qty,
                    "provider_filled_quantity_shares": provider_filled,
                    "provider_remaining_quantity_shares": provider_remaining,
                    "provider_limit_price": provider_limit,
                    "provider_asset_class": provider_asset,
                    "provider_time_in_force": provider_tif,
                    "provider_extended_hours": provider_extended,
                    "provider_position_intent": provider_intent,
                }

            metadata_json = _canonical_json(metadata).decode("utf-8")
            item_bodies.append(
                {
                    "schema_version": (
                        "chili.alpaca-paper-bp-reflection-item.v1"
                    ),
                    "item_ordinal": ordinal,
                    "reservation_id": str(row.reservation_id),
                    "decision_packet_sha256": row.decision_packet_sha256,
                    "reservation_state": row.state,
                    "reservation_version": int(row.version),
                    "symbol": row.symbol,
                    "client_order_id": cid,
                    "classification": classification,
                    "local_planned_quantity_shares": planned,
                    "local_cumulative_filled_quantity_shares": cumulative,
                    "local_remaining_quantity_shares": remaining,
                    "local_pending_buying_power_impact_usd": pending,
                    "local_planned_per_share_buying_power_usd": per_share_bp,
                    "local_entry_limit_price": entry_limit,
                    "local_time_in_force": local_tif,
                    "local_extended_hours": local_extended,
                    "local_position_intent": local_intent,
                    "local_broker_order_id": row.broker_order_id,
                    "local_broker_source": row.broker_source,
                    "local_broker_connection_generation": (
                        row.broker_connection_generation
                    ),
                    "local_action_claim_token": action.claim_token,
                    "local_action_claim_phase": action.phase,
                    "local_action_claim_metadata_canonical_json": metadata_json,
                    "local_action_claim_metadata_sha256": hashlib.sha256(
                        metadata_json.encode("utf-8")
                    ).hexdigest(),
                    "local_outbox_completion_sha256": outbox.completion_sha256,
                    "local_outbox_payload_canonical_json": (
                        outbox.payload_canonical_json
                    ),
                    "local_outbox_payload_sha256": outbox.payload_sha256,
                    "local_outbox_status": outbox.status,
                    "local_outbox_version": int(outbox.version),
                    "local_outbox_transport_started_at": (
                        outbox.transport_started_at
                    ),
                    "local_outbox_transport_evidence_sha256": (
                        outbox.transport_evidence_sha256
                    ),
                    "local_order_request": order_request,
                    "provider_order": provider_order,
                    **provider_fields,
                }
            )

        if reflected_total < 0 or reflected_total > pending_total:
            raise AdaptiveRiskContractError(
                "Alpaca PAPER reflected buying-power total is invalid"
            )
        return _PreparedAlpacaPaperBuyingPowerReflection(
            batch=verified,
            reservation_ledger_sha256=_sha256_json(ledger_payload),
            pending_buying_power_impact_usd=pending_total,
            reflected_pending_buying_power_usd=reflected_total,
            item_bodies=tuple(item_bodies),
        )

    @staticmethod
    def _persist_alpaca_paper_buying_power_reflection(
        session: Session,
        *,
        prepared: _PreparedAlpacaPaperBuyingPowerReflection,
        account_snapshot: ImmutableAccountRiskSnapshot,
        open_buying_power_impact_usd: Decimal,
    ) -> str:
        batch = prepared.batch
        authority = batch.account_authority
        before = batch.before
        after = batch.after
        broker_bp = _money(authority.buying_power_usd)
        open_bp = _money(open_buying_power_impact_usd)
        reflected = prepared.reflected_pending_buying_power_usd
        capacity = _money(broker_bp + open_bp + reflected)
        item_rows: list[tuple[dict[str, Any], str, str]] = []
        for body in prepared.item_bodies:
            item_json = _canonical_json(body).decode("utf-8")
            item_sha = hashlib.sha256(item_json.encode("utf-8")).hexdigest()
            item_rows.append((body, item_json, item_sha))
        receipt_body = {
            "schema_version": "chili.alpaca-paper-bp-reflection.v1",
            "authority_status": "verified",
            "batch_content_sha256": batch.batch_content_sha256,
            "decision_id": authority.decision_id,
            "run_id": authority.run_id,
            "generation": int(authority.generation),
            "account_scope": "alpaca:paper",
            "account_id": authority.account_id,
            "account_identity_sha256": authority.account_identity_sha256,
            "account_snapshot_sha256": account_snapshot.snapshot_sha256,
            "account_read_receipt_sha256": authority.account_read_receipt_sha256,
            "account_provider_generation": authority.provider_generation,
            "account_observed_at": authority.observed_at,
            "account_available_at": authority.available_at,
            "broker_buying_power_usd": broker_bp,
            "reservation_ledger_sha256": prepared.reservation_ledger_sha256,
            "open_buying_power_impact_usd": open_bp,
            "pending_buying_power_impact_usd": (
                prepared.pending_buying_power_impact_usd
            ),
            "reflected_pending_buying_power_usd": reflected,
            "policy_buying_power_capacity_usd": capacity,
            "census_a_read_binding_canonical_json": (
                before.read_binding_canonical_json
            ),
            "census_a_read_binding_sha256": before.read_binding_sha256,
            "census_a_query_receipt_canonical_json": (
                before.query_receipt_canonical_json
            ),
            "census_a_query_receipt_sha256": before.query_receipt_sha256,
            "census_a_inventory_canonical_json": before.inventory_canonical_json,
            "census_a_inventory_sha256": before.inventory_sha256,
            "census_a_adapter_connection_generation": (
                before.adapter_connection_generation
            ),
            "census_a_adapter_build_sha256": before.adapter_build_sha256,
            "census_a_requested_at": before.requested_at,
            "census_a_received_at": before.received_at,
            "census_a_available_at": before.available_at,
            "census_b_read_binding_canonical_json": (
                after.read_binding_canonical_json
            ),
            "census_b_read_binding_sha256": after.read_binding_sha256,
            "census_b_query_receipt_canonical_json": (
                after.query_receipt_canonical_json
            ),
            "census_b_query_receipt_sha256": after.query_receipt_sha256,
            "census_b_inventory_canonical_json": after.inventory_canonical_json,
            "census_b_inventory_sha256": after.inventory_sha256,
            "census_b_adapter_connection_generation": (
                after.adapter_connection_generation
            ),
            "census_b_adapter_build_sha256": after.adapter_build_sha256,
            "census_b_requested_at": after.requested_at,
            "census_b_received_at": after.received_at,
            "census_b_available_at": after.available_at,
            "item_count": len(item_rows),
            "item_sha256s": [item_sha for _, _, item_sha in item_rows],
        }
        receipt_json = _canonical_json(receipt_body).decode("utf-8")
        receipt_sha = hashlib.sha256(receipt_json.encode("utf-8")).hexdigest()
        receipt_columns = dict(receipt_body)
        receipt_columns.pop("schema_version")
        receipt_columns.pop("item_sha256s")
        receipt_row = AlpacaPaperBuyingPowerReflectionReceipt(
            receipt_sha256=receipt_sha,
            receipt_schema_version=(
                "chili.alpaca-paper-bp-reflection.v1"
            ),
            receipt_content_canonical_json=receipt_json,
            receipt_content_sha256=receipt_sha,
            **receipt_columns,
        )
        session.add(receipt_row)
        # There is deliberately no mutable ORM relationship between the sealed
        # receipt and its append-only item inventory. Flush the immutable parent
        # explicitly so SQLAlchemy cannot emit an item before its FK target. The
        # deferred completeness trigger still evaluates the full set at commit.
        session.flush([receipt_row])
        for body, item_json, item_sha in item_rows:
            columns = dict(body)
            for non_column in (
                "schema_version",
                "local_action_claim_metadata_canonical_json",
                "local_outbox_payload_canonical_json",
                "local_outbox_payload_sha256",
                "local_order_request",
                "provider_order",
            ):
                columns.pop(non_column)
            session.add(
                AlpacaPaperBuyingPowerReflectionItem(
                    receipt_sha256=receipt_sha,
                    item_content_canonical_json=item_json,
                    item_sha256=item_sha,
                    **columns,
                )
            )
        session.flush()
        return receipt_sha

    @staticmethod
    def _verify_retry(
        row: AdaptiveRiskDecisionPacket,
        request: AdaptiveRiskReservationRequest,
    ) -> None:
        comparisons = {
            "reservation_request_sha256": (
                row.reservation_request_sha256,
                request.request_sha256,
            ),
            "decision_id": (row.decision_id, request.inputs.decision_id),
            "symbol": (row.symbol, request.inputs.symbol.upper()),
            "setup_family": (row.setup_family, request.setup_family),
            "correlation_cluster": (
                row.correlation_cluster,
                request.correlation_cluster,
            ),
            "execution_surface": (
                row.execution_surface,
                request.inputs.execution_surface,
            ),
            "execution_family": (
                row.execution_family,
                request.inputs.execution_family,
            ),
            "broker_environment": (
                row.broker_environment,
                request.inputs.broker_environment,
            ),
            "account_identity_sha256": (
                row.account_identity_sha256,
                request.inputs.account_identity_sha256,
            ),
            "account_snapshot_sha256": (
                row.account_snapshot_sha256,
                request.account_snapshot.snapshot_sha256,
            ),
            "effective_config_sha256": (
                row.effective_config_sha256,
                request.inputs.effective_config_sha256,
            ),
            "code_build_sha256": (
                row.code_build_sha256,
                request.inputs.code_build_sha256,
            ),
            "feature_flags_sha256": (
                row.feature_flags_sha256,
                request.inputs.feature_flags_sha256,
            ),
            "capture_prefix_root_sha256": (
                row.capture_prefix_root_sha256,
                request.inputs.capture_prefix_root_sha256,
            ),
            "structural_stop": (
                Decimal(row.structural_stop),
                _money(request.inputs.structural_stop),
            ),
            "entry_limit_price": (
                Decimal(row.entry_limit_price),
                _money(request.entry_limit_price),
            ),
        }
        if request.setup_family == "first_dip_reclaim":
            opportunity_key = request.opportunity_key
            if not isinstance(opportunity_key, AdaptiveRiskOpportunityKey):
                raise AdaptiveReservationIdempotencyConflict(
                    "first_dip_reclaim retry is missing its opportunity_key"
                )
            comparisons["trading_date"] = (
                row.trading_date,
                opportunity_key.trading_date,
            )
        changed = sorted(
            name for name, (persisted, supplied) in comparisons.items() if persisted != supplied
        )
        if changed:
            raise AdaptiveReservationIdempotencyConflict(
                "client_order_id retry changed immutable fields: " + ",".join(changed)
            )

    @staticmethod
    def _decision_from_row(
        session: Session,
        row: AdaptiveRiskDecisionPacket,
        *,
        idempotent_retry: bool,
    ) -> AdaptiveReservationDecision:
        reservation = session.scalar(
            select(AdaptiveRiskReservation).where(
                AdaptiveRiskReservation.decision_packet_sha256
                == row.decision_packet_sha256
            )
        )
        accepted = bool(row.admission_accepted and reservation is not None)
        return AdaptiveReservationDecision(
            schema_version=FOUNDATION_SCHEMA_VERSION,
            admission_accepted=accepted,
            idempotent_retry=idempotent_retry,
            rejection_reasons=tuple(row.rejection_reasons_json or ()),
            decision_packet_sha256=row.decision_packet_sha256,
            reservation_id=reservation.reservation_id if reservation else None,
            account_scope=row.account_scope,
            symbol=row.symbol,
            trading_date=row.trading_date,
            setup_family=row.setup_family,
            client_order_id=row.client_order_id,
            quantity_shares=(int(reservation.planned_quantity_shares) if reservation else 0),
            structural_risk_usd=(
                Decimal(reservation.planned_structural_risk_usd)
                if reservation
                else Decimal("0")
            ),
            gross_notional_usd=(
                Decimal(reservation.planned_gross_notional_usd)
                if reservation
                else Decimal("0")
            ),
            buying_power_impact_usd=(
                Decimal(reservation.planned_buying_power_impact_usd)
                if reservation
                else Decimal("0")
            ),
        )

    @staticmethod
    def _append_reservation_event(
        session: Session,
        reservation: AdaptiveRiskReservation,
        *,
        event_type: str,
        effective_at: datetime,
        details: Mapping[str, Any],
        broker_event_id: str | None = None,
    ) -> None:
        sequence = int(reservation.event_sequence) + 1
        event_details = dict(details)
        if reservation.opportunity_claim_id is None:
            event_details.setdefault("opportunity_status", "not_applicable")
        payload = {
            "schema_version": FOUNDATION_SCHEMA_VERSION,
            "event_type": event_type,
            "reservation_id": str(reservation.reservation_id),
            "opportunity_claim_id": (
                int(reservation.opportunity_claim_id)
                if reservation.opportunity_claim_id is not None
                else None
            ),
            "sequence": sequence,
            "effective_at": _iso(effective_at),
            "previous_event_sha256": reservation.last_event_sha256,
            "broker_event_id": broker_event_id,
            "state": reservation.state,
            "planned_quantity_shares": int(reservation.planned_quantity_shares),
            "cumulative_filled_quantity_shares": int(
                reservation.cumulative_filled_quantity_shares
            ),
            "open_quantity_shares": int(reservation.open_quantity_shares),
            "pending": {
                "structural_risk_usd": Decimal(
                    reservation.pending_structural_risk_usd
                ),
                "gross_notional_usd": Decimal(
                    reservation.pending_gross_notional_usd
                ),
                "buying_power_impact_usd": Decimal(
                    reservation.pending_buying_power_impact_usd
                ),
            },
            "open": {
                "structural_risk_usd": Decimal(reservation.open_structural_risk_usd),
                "gross_notional_usd": Decimal(reservation.open_gross_notional_usd),
                "buying_power_impact_usd": Decimal(
                    reservation.open_buying_power_impact_usd
                ),
            },
            "details": event_details,
        }
        event_sha = _sha256_json(payload)
        session.add(
            AdaptiveRiskReservationEvent(
                reservation_id=reservation.reservation_id,
                sequence=sequence,
                event_type=event_type,
                previous_event_sha256=reservation.last_event_sha256,
                event_sha256=event_sha,
                broker_event_id=broker_event_id,
                payload_json=_json_safe(payload),
                effective_at=effective_at,
            )
        )
        reservation.event_sequence = sequence
        reservation.last_event_sha256 = event_sha

    @staticmethod
    def _append_opportunity_event(
        session: Session,
        opportunity: AdaptiveRiskOpportunityClaim,
        *,
        reservation_id: uuid.UUID,
        event_type: str,
        effective_at: datetime,
        details: Mapping[str, Any],
    ) -> None:
        sequence = int(opportunity.event_sequence) + 1
        payload = {
            "schema_version": FOUNDATION_SCHEMA_VERSION,
            "event_type": event_type,
            "opportunity_claim_id": int(opportunity.id),
            "opportunity_key": {
                "account_scope": opportunity.account_scope,
                "symbol": opportunity.symbol,
                "trading_date": opportunity.trading_date.isoformat(),
                "setup_family": opportunity.setup_family,
            },
            "reservation_id": str(reservation_id),
            "sequence": sequence,
            "effective_at": _iso(effective_at),
            "previous_event_sha256": opportunity.last_event_sha256,
            "status": opportunity.status,
            "details": dict(details),
        }
        event_sha = _sha256_json(payload)
        session.add(
            AdaptiveRiskOpportunityEvent(
                opportunity_claim_id=opportunity.id,
                reservation_id=reservation_id,
                sequence=sequence,
                event_type=event_type,
                previous_event_sha256=opportunity.last_event_sha256,
                event_sha256=event_sha,
                payload_json=_json_safe(payload),
                effective_at=effective_at,
            )
        )
        opportunity.event_sequence = sequence
        opportunity.last_event_sha256 = event_sha

    def reserve(
        self,
        request: AdaptiveRiskReservationRequest,
        *,
        session: Session | None = None,
        locked_snapshot: LockedAdaptiveRiskAdmissionSnapshot | None = None,
        prepared_resolution: ResolvedAdaptiveRisk | None = None,
        prepared_decision_packet: Mapping[str, Any] | None = None,
        locked_alpaca_paper_bundle: LockedAlpacaPaperAdmissionBundle | None = None,
    ) -> AdaptiveReservationDecision:
        """Resolve and reserve atomically; retries return the immutable result.

        ``locked_snapshot`` is the strict DB-paper path.  Its account advisory
        lock already belongs to ``session`` and its values must be byte-for-byte
        represented in ``request.inputs``.  In that mode the caller must also
        provide the one resolution produced from that exact final bundle.  The
        store strictly recomputes that packet for verification and persists only
        the verified result, performing no later economic ledger/clock
        substitution.
        """

        # ReplayV3 consumes a content-addressed recorded ledger through the pure
        # resolver. It must never enter this mutable operational store, whose
        # PostgreSQL clock and current account ledger are intentionally live
        # authorities. Reject unsupported/historical surfaces before opening a
        # transaction so a future integration cannot silently wall-stamp a replay
        # decision or read current state as recorded evidence. This is an
        # operational correctness boundary, not a strategy or sizing throttle.
        if (
            request.inputs.execution_surface
            not in _MUTABLE_RESERVATION_EXECUTION_SURFACES
        ):
            raise AdaptiveRiskContractError(
                "mutable adaptive reservation store does not accept execution "
                f"surface: {request.inputs.execution_surface}"
            )

        alpaca_paper = request.inputs.execution_surface == "alpaca_paper"
        if alpaca_paper:
            if type(locked_alpaca_paper_bundle) is not LockedAlpacaPaperAdmissionBundle:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER reservation requires the locked admission bundle"
                )
            locked_alpaca_paper_bundle.verify()
            if locked_snapshot is None:
                locked_snapshot = locked_alpaca_paper_bundle.locked_risk_snapshot
            elif locked_snapshot is not locked_alpaca_paper_bundle.locked_risk_snapshot:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER reservation received conflicting locked snapshots"
                )
            exact_bundle_request = {
                "account_snapshot": (
                    request.account_snapshot.snapshot_sha256,
                    locked_alpaca_paper_bundle.account_snapshot.snapshot_sha256,
                ),
                "decision_as_of": (
                    request.inputs.as_of,
                    locked_alpaca_paper_bundle.decision_as_of,
                ),
                "decision_id": (
                    request.inputs.decision_id,
                    locked_alpaca_paper_bundle.attestation.decision_id,
                ),
                "run_id": (
                    request.inputs.replay_or_paper_run_id,
                    locked_alpaca_paper_bundle.attestation.run_id,
                ),
                "generation": (
                    request.inputs.generation,
                    locked_alpaca_paper_bundle.attestation.generation,
                ),
                "risk_date_et": (
                    request.inputs.as_of.astimezone(ET).date(),
                    locked_alpaca_paper_bundle.risk_date_et,
                ),
                "pending_buying_power_reflection": (
                    request.account_snapshot.pending_policy_buying_power_reflected_usd,
                    locked_alpaca_paper_bundle.account_snapshot
                    .pending_policy_buying_power_reflected_usd,
                ),
                "broker_account_evidence": (
                    request.broker_account_evidence,
                    locked_alpaca_paper_bundle.account_evidence,
                ),
                "settled_daily_pnl_evidence": (
                    request.settled_daily_pnl_evidence,
                    locked_alpaca_paper_bundle.daily_pnl_evidence,
                ),
            }
            changed = sorted(
                name
                for name, (actual, required) in exact_bundle_request.items()
                if actual != required
            )
            if changed:
                raise AdaptiveRiskContractError(
                    "Alpaca PAPER locked admission/request mismatch: "
                    + ",".join(changed)
                )
        elif locked_alpaca_paper_bundle is not None:
            raise AdaptiveRiskContractError(
                "locked Alpaca PAPER admission bundle is invalid for DB paper"
            )

        first_dip_opportunity: AdaptiveRiskOpportunityKey | None = None
        if request.setup_family == "first_dip_reclaim":
            if not isinstance(request.opportunity_key, AdaptiveRiskOpportunityKey):
                raise AdaptiveRiskContractError(
                    "first_dip_reclaim reservation requires a captured opportunity_key"
                )
            first_dip_opportunity = request.opportunity_key

        caller_owned_transaction = session is not None
        paper_attestation: LockedAlpacaPaperDailyPnlAttestation | None = None
        with self._transaction(session) as session:
            if locked_alpaca_paper_bundle is not None:
                if not caller_owned_transaction:
                    raise AdaptiveRiskContractError(
                        "locked Alpaca PAPER bundle requires caller transaction"
                    )
                current_transaction_id, current_backend_pid = (
                    self._transaction_identity(session)
                )
                receipts = session.info.get(
                    _LOCKED_ALPACA_PAPER_BUNDLE_RECEIPTS_SESSION_KEY
                )
                issued = (
                    receipts.get(request.account_scope)
                    if isinstance(receipts, Mapping)
                    else None
                )
                attestation = verify_locked_alpaca_paper_daily_pnl_attestation(
                    locked_alpaca_paper_bundle.attestation
                )
                paper_attestation = attestation
                if not (
                    isinstance(issued, Mapping)
                    and current_transaction_id == attestation.transaction_id
                    and current_backend_pid == attestation.backend_pid
                    and issued.get("transaction_id") == current_transaction_id
                    and issued.get("backend_pid") == current_backend_pid
                    and issued.get("lock_receipt_id")
                    == attestation.lock_receipt_id
                    and issued.get("decision_id") == attestation.decision_id
                    and issued.get("run_id") == attestation.run_id
                    and issued.get("generation") == attestation.generation
                    and issued.get("account_id") == attestation.account_id
                    and issued.get("account_snapshot_sha256")
                    == attestation.account_snapshot_sha256
                    and issued.get("account_payload_sha256")
                    == attestation.account_payload_sha256
                    and issued.get("account_read_receipt_sha256")
                    == attestation.account_read_receipt_sha256
                    and issued.get("active_input_attestation_sha256")
                    == attestation.active_input_attestation_sha256
                    and issued.get("account_capture_authority_sha256")
                    == attestation.account_capture_authority_sha256
                    and issued.get("daily_pnl_evidence_sha256")
                    == attestation.daily_pnl_evidence_sha256
                    and issued.get("daily_terminal_fill_authority_sha256")
                    == attestation.daily_terminal_fill_authority_sha256
                    and issued.get(
                        "buying_power_reflection_receipt_sha256"
                    )
                    == attestation.buying_power_reflection_receipt_sha256
                    and issued.get("ledger_snapshot_content_sha256")
                    == attestation.ledger_snapshot_content_sha256
                    and issued.get("ledger_sha256") == attestation.ledger_sha256
                    and issued.get("bundle_sha256") == attestation.bundle_sha256
                    and issued.get("verification_tag")
                    == attestation._verification_tag
                ):
                    raise AdaptiveRiskContractError(
                        "locked Alpaca PAPER bundle was not issued by the current "
                        "database transaction"
                    )
                reflection_sha = (
                    attestation.buying_power_reflection_receipt_sha256
                )
                reflection_row = (
                    None
                    if reflection_sha is None
                    else session.get(
                        AlpacaPaperBuyingPowerReflectionReceipt,
                        reflection_sha,
                    )
                )
                if reflection_sha is not None and not (
                    reflection_row is not None
                    and reflection_row.receipt_content_sha256 == reflection_sha
                    and hashlib.sha256(
                        reflection_row.receipt_content_canonical_json.encode(
                            "utf-8"
                        )
                    ).hexdigest()
                    == reflection_sha
                    and reflection_row.account_identity_sha256
                    == request.account_snapshot.account_identity_sha256
                    and reflection_row.account_snapshot_sha256
                    == request.account_snapshot.snapshot_sha256
                    and reflection_row.reservation_ledger_sha256
                    == locked_alpaca_paper_bundle.locked_risk_snapshot.ledger_sha256
                    and _money(
                        reflection_row.reflected_pending_buying_power_usd
                    )
                    == _money(
                        request.account_snapshot
                        .pending_policy_buying_power_reflected_usd
                    )
                ):
                    raise AdaptiveRiskContractError(
                        "locked Alpaca PAPER buying-power receipt changed"
                    )
                # The private receipt proves where the bundle came from; it
                # does not freeze time.  Final request construction can perform
                # non-trivial CPU work while this caller-owned transaction is
                # open, so reject a bundle whose capture window expired before
                # the reservation is actually consumed.  This clock is used
                # only as a fail-closed freshness check and never substitutes
                # new economic inputs into the prepared decision.
                consumed_at = self._clock(session)
                if not (
                    attestation.decision_as_of
                    <= consumed_at
                    <= attestation.expires_at
                ):
                    raise AdaptiveRiskContractError(
                        "locked Alpaca PAPER bundle expired before reservation"
                    )
            if locked_snapshot is None:
                if prepared_resolution is not None or prepared_decision_packet is not None:
                    raise AdaptiveRiskContractError(
                        "prepared resolution requires a locked admission snapshot"
                    )
                self._lock_account(session, request.account_scope)
            else:
                if not caller_owned_transaction:
                    raise AdaptiveRiskContractError(
                        "locked admission snapshot requires caller transaction"
                    )
                # This snapshot is a transaction capability, not an extension
                # point.  An ``isinstance`` check would let a subclass override
                # ``verify`` and bypass the consumption-time content check.
                if type(locked_snapshot) is not LockedAdaptiveRiskAdmissionSnapshot:
                    raise AdaptiveRiskContractError(
                        "locked admission snapshot is malformed"
                    )
                # Frozen dataclasses are not immutable capabilities: their
                # ``__dict__`` and nested mappings can still be altered after
                # construction.  Recompute the complete content binding at the
                # consumption boundary before trusting the session receipt.
                locked_snapshot.verify()
                if (
                    locked_snapshot.account_scope != request.account_scope
                    or locked_snapshot.symbol != request.inputs.symbol.upper()
                    or locked_snapshot.correlation_cluster
                    != request.correlation_cluster
                ):
                    raise AdaptiveRiskContractError(
                        "locked admission snapshot does not match request identity"
                    )
                if (
                    locked_snapshot.account_snapshot_sha256
                    != request.account_snapshot.snapshot_sha256
                ):
                    raise AdaptiveRiskContractError(
                        "locked admission snapshot does not match request account snapshot"
                    )
                # A caller-created dataclass is not lock authority.  Reacquire
                # the same advisory lock, prove the snapshot was issued by this
                # exact top-level PostgreSQL transaction/backend, and verify the
                # current ledger byte-for-byte before trusting its final bundle.
                self._lock_account(session, request.account_scope)
                current_transaction_id, current_backend_pid = (
                    self._transaction_identity(session)
                )
                receipts = session.info.get(
                    _LOCKED_ADMISSION_RECEIPTS_SESSION_KEY
                )
                issued = (
                    receipts.get(request.account_scope)
                    if isinstance(receipts, Mapping)
                    else None
                )
                if not (
                    isinstance(issued, Mapping)
                    and locked_snapshot.transaction_id == current_transaction_id
                    and locked_snapshot.backend_pid == current_backend_pid
                    and issued.get("transaction_id") == current_transaction_id
                    and issued.get("backend_pid") == current_backend_pid
                    and issued.get("lock_receipt_id")
                    == locked_snapshot.lock_receipt_id
                    and issued.get("account_snapshot_sha256")
                    == request.account_snapshot.snapshot_sha256
                    and issued.get("snapshot_content_sha256")
                    == locked_snapshot.content_sha256
                ):
                    raise AdaptiveRiskContractError(
                        "locked admission snapshot was not issued by the current "
                        "database transaction"
                    )
                current_aggregates, current_ledger_payload = self._active_ledger(
                    session,
                    account_scope=request.account_scope,
                    symbol=request.inputs.symbol.upper(),
                    correlation_cluster=request.correlation_cluster,
                )
                current_ledger_sha = _sha256_json(current_ledger_payload)
                self._raise_if_pending_settlement(
                    current_ledger_payload,
                    locked_snapshot=locked_snapshot,
                )
                self._raise_if_quarantined_exposure(
                    current_ledger_payload,
                    locked_snapshot=locked_snapshot,
                )
                if (
                    current_ledger_sha != locked_snapshot.ledger_sha256
                    or _canonical_json(current_aggregates)
                    != _canonical_json(dict(locked_snapshot.aggregates))
                    or _canonical_json(current_ledger_payload)
                    != _canonical_json(dict(locked_snapshot.ledger_payload))
                ):
                    raise AdaptiveRiskContractError(
                        "locked admission ledger changed before reservation"
                    )
                expected_policy_capacity = (
                    float(request.account_snapshot.buying_power_usd)
                    + float(current_aggregates["open_buying_power_impact_usd"])
                    + float(
                        request.account_snapshot
                        .pending_policy_buying_power_reflected_usd
                    )
                )
                if _money(expected_policy_capacity) != _money(
                    locked_snapshot.policy_buying_power_capacity_usd
                ):
                    raise AdaptiveRiskContractError(
                        "locked admission buying-power capacity is not database-bound"
                    )
                if not isinstance(prepared_resolution, ResolvedAdaptiveRisk) or not isinstance(
                    prepared_decision_packet, Mapping
                ):
                    raise AdaptiveRiskContractError(
                        "locked admission snapshot requires one prepared resolution"
                    )
                # A directly constructed ``ResolvedAdaptiveRisk`` and its own
                # packet are only self-consistent assertions.  Strictly rebuild
                # the typed snapshots and rerun the pure resolver, then use only
                # that recomputed result for admission and persistence.
                verified_resolution = (
                    load_and_verify_adaptive_risk_decision_packet(
                        prepared_decision_packet
                    )
                )
                verified_packet = verified_resolution.to_decision_packet()
                if _canonical_json(
                    prepared_resolution.to_decision_packet()
                ) != _canonical_json(verified_packet):
                    raise AdaptiveRiskContractError(
                        "prepared resolution differs from strict recomputation"
                    )
                prepared_resolution = verified_resolution
                prepared_decision_packet = verified_packet
                canonical_packet = verified_packet
                exact_resolution = {
                    "policy_sha256": (
                        prepared_resolution.policy_sha256,
                        request.policy.policy_sha256,
                    ),
                    "input_sha256": (
                        prepared_resolution.input_sha256,
                        request.inputs.input_sha256,
                    ),
                    "economic_input_sha256": (
                        prepared_resolution.economic_input_sha256,
                        request.inputs.economic_input_sha256,
                    ),
                    "policy_snapshot": (
                        _canonical_json(prepared_resolution.policy_snapshot),
                        _canonical_json(asdict(request.policy)),
                    ),
                    "input_snapshot": (
                        _canonical_json(prepared_resolution.input_snapshot),
                        _canonical_json(asdict(request.inputs)),
                    ),
                    "decision_packet": (
                        _canonical_json(prepared_decision_packet),
                        _canonical_json(canonical_packet),
                    ),
                }
                changed_resolution = sorted(
                    name
                    for name, (actual, expected) in exact_resolution.items()
                    if actual != expected
                )
                if changed_resolution:
                    raise AdaptiveRiskContractError(
                        "prepared resolution differs from the locked request: "
                        + ",".join(changed_resolution)
                    )
            existing = session.scalar(
                select(AdaptiveRiskDecisionPacket)
                .where(AdaptiveRiskDecisionPacket.account_scope == request.account_scope)
                .where(
                    AdaptiveRiskDecisionPacket.client_order_id
                    == request.client_order_id
                )
                .with_for_update()
            )
            if existing is not None:
                self._verify_retry(existing, request)
                if (
                    prepared_resolution is not None
                    and existing.decision_packet_sha256
                    != prepared_resolution.decision_packet_sha256
                ):
                    raise AdaptiveReservationIdempotencyConflict(
                        "client_order_id retry changed the prepared resolution"
                    )
                return self._decision_from_row(
                    session, existing, idempotent_retry=True
                )

            duplicate_decision = session.scalar(
                select(AdaptiveRiskDecisionPacket)
                .where(AdaptiveRiskDecisionPacket.account_scope == request.account_scope)
                .where(
                    AdaptiveRiskDecisionPacket.decision_id
                    == request.inputs.decision_id
                )
                .with_for_update()
            )
            if duplicate_decision is not None:
                raise AdaptiveReservationIdempotencyConflict(
                    "decision_id already belongs to a different client_order_id"
                )

            symbol = (
                first_dip_opportunity.symbol
                if first_dip_opportunity is not None
                else request.inputs.symbol.upper()
            )
            reservation_setup_family = (
                first_dip_opportunity.setup_family
                if first_dip_opportunity is not None
                else request.setup_family
            )
            if locked_snapshot is None:
                decision_clock = self._clock(session)
                aggregates, ledger_payload = self._active_ledger(
                    session,
                    account_scope=request.account_scope,
                    symbol=symbol,
                    correlation_cluster=request.correlation_cluster,
                )
                ledger_sha = _sha256_json(ledger_payload)
            else:
                decision_clock = locked_snapshot.observed_at
                aggregates = dict(locked_snapshot.aggregates)
                ledger_payload = dict(locked_snapshot.ledger_payload)
                ledger_sha = locked_snapshot.ledger_sha256
            self._raise_if_pending_settlement(
                ledger_payload,
                locked_snapshot=locked_snapshot,
            )
            self._raise_if_quarantined_exposure(
                ledger_payload,
                locked_snapshot=locked_snapshot,
            )
            if (
                first_dip_opportunity is not None
                and request.inputs.as_of > decision_clock
            ):
                raise AdaptiveRiskContractError(
                    "first_dip_reclaim captured decision is after the database "
                    "transaction observation"
                )
            trading_date = (
                first_dip_opportunity.trading_date
                if first_dip_opportunity is not None
                else decision_clock.astimezone(ET).date()
            )
            evidence = dict(request.inputs.evidence)
            exact_ledger_evidence = RiskInputEvidence(
                source="postgresql:adaptive_risk_reservations",
                observed_at=decision_clock,
                available_at=decision_clock,
                content_sha256=ledger_sha,
                provider_generation=RESERVATION_LEDGER_GENERATION,
            )
            if locked_snapshot is None:
                evidence["reservation_ledger"] = exact_ledger_evidence
            reflected_pending_bp = float(
                request.account_snapshot.pending_policy_buying_power_reflected_usd
            )
            if locked_snapshot is None:
                derived_inputs = replace(
                    request.inputs,
                    as_of=decision_clock,
                    open_structural_risk_usd=aggregates[
                        "open_structural_risk_usd"
                    ],
                    pending_reserved_risk_usd=aggregates[
                        "pending_reserved_risk_usd"
                    ],
                    existing_same_symbol_structural_risk_usd=aggregates[
                        "existing_same_symbol_structural_risk_usd"
                    ],
                    pending_same_symbol_structural_risk_usd=aggregates[
                        "pending_same_symbol_structural_risk_usd"
                    ],
                    current_cluster_structural_risk_usd=aggregates[
                        "current_cluster_structural_risk_usd"
                    ],
                    pending_correlation_cluster_risk_usd=aggregates[
                        "pending_correlation_cluster_risk_usd"
                    ],
                    portfolio_gross_notional_usd=aggregates[
                        "portfolio_gross_notional_usd"
                    ],
                    pending_portfolio_gross_notional_usd=aggregates[
                        "pending_portfolio_gross_notional_usd"
                    ],
                    policy_buying_power_capacity_usd=(
                        float(request.inputs.buying_power_usd)
                        + aggregates["open_buying_power_impact_usd"]
                        + reflected_pending_bp
                    ),
                    open_buying_power_impact_usd=aggregates[
                        "open_buying_power_impact_usd"
                    ],
                    pending_buying_power_impact_usd=aggregates[
                        "pending_buying_power_impact_usd"
                    ],
                    evidence=evidence,
                )
            else:
                expected_values = {
                    "as_of": decision_clock,
                    "open_structural_risk_usd": aggregates[
                        "open_structural_risk_usd"
                    ],
                    "pending_reserved_risk_usd": aggregates[
                        "pending_reserved_risk_usd"
                    ],
                    "existing_same_symbol_structural_risk_usd": aggregates[
                        "existing_same_symbol_structural_risk_usd"
                    ],
                    "pending_same_symbol_structural_risk_usd": aggregates[
                        "pending_same_symbol_structural_risk_usd"
                    ],
                    "current_cluster_structural_risk_usd": aggregates[
                        "current_cluster_structural_risk_usd"
                    ],
                    "pending_correlation_cluster_risk_usd": aggregates[
                        "pending_correlation_cluster_risk_usd"
                    ],
                    "portfolio_gross_notional_usd": aggregates[
                        "portfolio_gross_notional_usd"
                    ],
                    "pending_portfolio_gross_notional_usd": aggregates[
                        "pending_portfolio_gross_notional_usd"
                    ],
                    "policy_buying_power_capacity_usd": (
                        locked_snapshot.policy_buying_power_capacity_usd
                    ),
                    "open_buying_power_impact_usd": aggregates[
                        "open_buying_power_impact_usd"
                    ],
                    "pending_buying_power_impact_usd": aggregates[
                        "pending_buying_power_impact_usd"
                    ],
                }
                changed = [
                    name
                    for name, expected in expected_values.items()
                    if getattr(request.inputs, name) != expected
                ]
                if request.inputs.evidence.get(
                    "reservation_ledger"
                ) != exact_ledger_evidence:
                    changed.append("reservation_ledger_evidence")
                if changed:
                    raise AdaptiveRiskContractError(
                        "final bundle differs from locked admission snapshot: "
                        + ",".join(sorted(changed))
                    )
                derived_inputs = request.inputs
            if locked_snapshot is None:
                resolved = resolve_adaptive_risk(request.policy, derived_inputs)
                packet = resolved.to_decision_packet()
            else:
                assert prepared_resolution is not None
                assert prepared_decision_packet is not None
                resolved = prepared_resolution
                packet = dict(prepared_decision_packet)
            foundation_rejections = self._account_snapshot_rejections(request)
            if reflected_pending_bp > (
                aggregates["pending_buying_power_impact_usd"] + 1e-9
            ):
                foundation_rejections.append(
                    "account_snapshot_pending_bp_reflection_exceeds_ledger"
                )
            if resolved.valid and float(request.entry_limit_price) > (
                float(resolved.effective_entry_price) + 1e-9
            ):
                foundation_rejections.append("entry_limit_exceeds_risk_model")
            rejections = list(resolved.rejection_reasons) + foundation_rejections

            def recheck_paper_clock_before_first_mutation() -> None:
                if paper_attestation is None:
                    return
                mutation_at = self._clock(session)
                if not (
                    paper_attestation.decision_as_of
                    <= mutation_at
                    <= paper_attestation.expires_at
                    and mutation_at.astimezone(ET).date()
                    == locked_alpaca_paper_bundle.risk_date_et
                ):
                    raise AdaptiveRiskContractError(
                        "locked Alpaca PAPER authority expired before first mutation"
                    )

            first_mutation_clock_checked = False

            opportunity: AdaptiveRiskOpportunityClaim | None = None
            admission_accepted = bool(
                resolved.valid
                and int(resolved.quantity_shares) > 0
                and not foundation_rejections
            )
            if admission_accepted and first_dip_opportunity is not None:
                opportunity = session.scalar(
                    select(AdaptiveRiskOpportunityClaim)
                    .where(
                        AdaptiveRiskOpportunityClaim.account_scope
                        == request.account_scope
                    )
                    .where(AdaptiveRiskOpportunityClaim.symbol == symbol)
                    .where(
                        AdaptiveRiskOpportunityClaim.trading_date == trading_date
                    )
                    .where(
                        AdaptiveRiskOpportunityClaim.setup_family
                        == reservation_setup_family
                    )
                    .with_for_update()
                )
                if opportunity is None:
                    recheck_paper_clock_before_first_mutation()
                    first_mutation_clock_checked = True
                    opportunity = AdaptiveRiskOpportunityClaim(
                        account_scope=request.account_scope,
                        symbol=symbol,
                        trading_date=trading_date,
                        setup_family=reservation_setup_family,
                        status="available",
                        reservation_id=None,
                        consumed_by_reservation_id=None,
                        event_sequence=0,
                        version=1,
                    )
                    session.add(opportunity)
                    session.flush()
                if opportunity.status == "consumed":
                    admission_accepted = False
                    rejections.append("opportunity_already_consumed")
                elif opportunity.status == "reserved":
                    admission_accepted = False
                    rejections.append("opportunity_already_reserved")

            evidence_sha = _sha256_json(packet["input_snapshot"]["evidence"])
            decision_row = AdaptiveRiskDecisionPacket(
                decision_packet_sha256=resolved.decision_packet_sha256,
                reservation_request_sha256=request.request_sha256,
                decision_id=derived_inputs.decision_id,
                account_scope=request.account_scope,
                symbol=symbol,
                trading_date=trading_date,
                setup_family=reservation_setup_family,
                correlation_cluster=request.correlation_cluster,
                client_order_id=request.client_order_id,
                execution_surface=derived_inputs.execution_surface,
                execution_family=derived_inputs.execution_family,
                broker_environment=derived_inputs.broker_environment,
                account_identity_sha256=derived_inputs.account_identity_sha256,
                account_snapshot_sha256=request.account_snapshot.snapshot_sha256,
                account_snapshot_generation=request.account_snapshot.provider_generation,
                policy_sha256=resolved.policy_sha256,
                input_sha256=resolved.input_sha256,
                economic_input_sha256=resolved.economic_input_sha256,
                economic_resolution_sha256=resolved.economic_resolution_sha256,
                effective_config_sha256=derived_inputs.effective_config_sha256,
                code_build_sha256=derived_inputs.code_build_sha256,
                feature_flags_sha256=derived_inputs.feature_flags_sha256,
                capture_prefix_root_sha256=derived_inputs.capture_prefix_root_sha256,
                evidence_sha256=evidence_sha,
                reservation_ledger_sha256=ledger_sha,
                resolved_quantity_shares=int(resolved.quantity_shares),
                structural_stop=_money(derived_inputs.structural_stop),
                entry_limit_price=_money(request.entry_limit_price),
                resolver_valid=bool(resolved.valid),
                admission_accepted=admission_accepted,
                rejection_reasons_json=list(dict.fromkeys(rejections)),
                account_snapshot_json=request.account_snapshot.to_payload(),
                decision_packet_json=packet,
            )
            if not first_mutation_clock_checked:
                recheck_paper_clock_before_first_mutation()
            session.add(decision_row)
            session.flush()

            if not admission_accepted:
                return self._decision_from_row(
                    session, decision_row, idempotent_retry=False
                )

            if first_dip_opportunity is not None and opportunity is None:
                raise AdaptiveReservationStateConflict(
                    "first-dip reservation lost its opportunity claim"
                )
            reservation_id = uuid.uuid4()
            now = self._clock(session)
            reservation = AdaptiveRiskReservation(
                reservation_id=reservation_id,
                decision_packet_sha256=resolved.decision_packet_sha256,
                opportunity_claim_id=(opportunity.id if opportunity else None),
                account_scope=request.account_scope,
                symbol=symbol,
                trading_date=trading_date,
                setup_family=reservation_setup_family,
                correlation_cluster=request.correlation_cluster,
                state="reserved",
                planned_quantity_shares=int(resolved.quantity_shares),
                cumulative_filled_quantity_shares=0,
                open_quantity_shares=0,
                planned_structural_risk_usd=_money(
                    resolved.planned_structural_risk_usd
                ),
                planned_gross_notional_usd=_money(resolved.planned_notional_usd),
                planned_buying_power_impact_usd=_money(
                    resolved.planned_buying_power_impact_usd
                ),
                pending_structural_risk_usd=_money(
                    resolved.planned_structural_risk_usd
                ),
                pending_gross_notional_usd=_money(resolved.planned_notional_usd),
                pending_buying_power_impact_usd=_money(
                    resolved.planned_buying_power_impact_usd
                ),
                open_structural_risk_usd=Decimal("0"),
                open_gross_notional_usd=Decimal("0"),
                open_buying_power_impact_usd=Decimal("0"),
                event_sequence=0,
                version=1,
            )
            session.add(reservation)
            session.flush()
            if opportunity is not None:
                opportunity.status = "reserved"
                opportunity.reservation_id = reservation_id
                opportunity.updated_at = now
                opportunity.version = int(opportunity.version) + 1
            self._append_reservation_event(
                session,
                reservation,
                event_type="reservation_created",
                effective_at=now,
                details={
                    "decision_packet_sha256": resolved.decision_packet_sha256,
                    "client_order_id": request.client_order_id,
                    "opportunity_consumed": False,
                    "opportunity_status": (
                        opportunity.status if opportunity else "not_applicable"
                    ),
                },
            )
            if opportunity is not None:
                self._append_opportunity_event(
                    session,
                    opportunity,
                    reservation_id=reservation_id,
                    event_type="opportunity_reserved",
                    effective_at=now,
                    details={"consumed": False},
                )
            return self._decision_from_row(
                session, decision_row, idempotent_retry=False
            )

    @staticmethod
    def _reservation_for_transition(
        session: Session, reservation_id: uuid.UUID
    ) -> AdaptiveRiskReservation:
        initial = session.get(AdaptiveRiskReservation, reservation_id)
        if initial is None:
            raise AdaptiveReservationStateConflict("reservation does not exist")
        AdaptiveRiskReservationStore._lock_account(session, initial.account_scope)
        session.expire(initial)
        row = session.scalar(
            select(AdaptiveRiskReservation)
            .where(AdaptiveRiskReservation.reservation_id == reservation_id)
            .with_for_update()
        )
        if row is None:
            raise AdaptiveReservationStateConflict("reservation does not exist")
        return row

    @staticmethod
    def _opportunity_for_transition(
        session: Session, reservation: AdaptiveRiskReservation
    ) -> AdaptiveRiskOpportunityClaim | None:
        if reservation.setup_family != "first_dip_reclaim":
            return None
        if reservation.opportunity_claim_id is None:
            raise AdaptiveReservationStateConflict(
                "first-dip reservation opportunity claim is missing"
            )
        row = session.scalar(
            select(AdaptiveRiskOpportunityClaim)
            .where(
                AdaptiveRiskOpportunityClaim.id
                == reservation.opportunity_claim_id
            )
            .with_for_update()
        )
        if row is None:
            raise AdaptiveReservationStateConflict("opportunity claim is missing")
        return row

    @staticmethod
    def _state_snapshot(
        reservation: AdaptiveRiskReservation,
        opportunity: AdaptiveRiskOpportunityClaim | None,
    ) -> AdaptiveReservationState:
        return AdaptiveReservationState(
            reservation_id=reservation.reservation_id,
            decision_packet_sha256=reservation.decision_packet_sha256,
            account_scope=reservation.account_scope,
            symbol=reservation.symbol,
            trading_date=reservation.trading_date,
            setup_family=reservation.setup_family,
            correlation_cluster=reservation.correlation_cluster,
            state=reservation.state,
            planned_quantity_shares=int(reservation.planned_quantity_shares),
            cumulative_filled_quantity_shares=int(
                reservation.cumulative_filled_quantity_shares
            ),
            open_quantity_shares=int(reservation.open_quantity_shares),
            pending_structural_risk_usd=Decimal(
                reservation.pending_structural_risk_usd
            ),
            pending_gross_notional_usd=Decimal(
                reservation.pending_gross_notional_usd
            ),
            pending_buying_power_impact_usd=Decimal(
                reservation.pending_buying_power_impact_usd
            ),
            open_structural_risk_usd=Decimal(
                reservation.open_structural_risk_usd
            ),
            open_gross_notional_usd=Decimal(
                reservation.open_gross_notional_usd
            ),
            open_buying_power_impact_usd=Decimal(
                reservation.open_buying_power_impact_usd
            ),
            opportunity_status=(
                opportunity.status if opportunity is not None else "not_applicable"
            ),
            event_sequence=int(reservation.event_sequence),
            broker_source=reservation.broker_source,
            broker_connection_generation=reservation.broker_connection_generation,
            broker_order_id=reservation.broker_order_id,
            last_broker_observed_at=(
                _utc(reservation.last_broker_observed_at, "last_broker_observed_at")
                if reservation.last_broker_observed_at is not None
                else None
            ),
            last_broker_available_at=(
                _utc(
                    reservation.last_broker_available_at,
                    "last_broker_available_at",
                )
                if reservation.last_broker_available_at is not None
                else None
            ),
            last_source_event_content_sha256=(
                reservation.last_source_event_content_sha256
            ),
            lifecycle_contradiction_source_state=(
                reservation.lifecycle_contradiction_source_state
            ),
            lifecycle_contradiction_at=(
                _utc(
                    reservation.lifecycle_contradiction_at,
                    "lifecycle_contradiction_at",
                )
                if reservation.lifecycle_contradiction_at is not None
                else None
            ),
            lifecycle_contradiction_evidence_sha256=(
                reservation.lifecycle_contradiction_evidence_sha256
            ),
        )

    def read_state(
        self,
        reservation_id: uuid.UUID,
        *,
        session: Session | None = None,
    ) -> AdaptiveReservationState:
        """Return one transactionally consistent lifecycle projection.

        Live broker recovery needs to decide whether a repeated REST observation
        advances durable truth before appending another lifecycle event.  Keep
        that read behind the same account advisory lock as every mutation so a
        restart cannot race an in-flight fill/terminal transition.
        """

        with self._transaction(session) as session:
            reservation = self._reservation_for_transition(session, reservation_id)
            opportunity = self._opportunity_for_transition(session, reservation)
            return self._state_snapshot(reservation, opportunity)

    @staticmethod
    def _existing_broker_event(
        session: Session,
        reservation_id: uuid.UUID,
        broker_event_id: str,
    ) -> AdaptiveRiskReservationEvent | None:
        return session.scalar(
            select(AdaptiveRiskReservationEvent)
            .where(
                AdaptiveRiskReservationEvent.reservation_id == reservation_id
            )
            .where(
                AdaptiveRiskReservationEvent.broker_event_id == broker_event_id
            )
        )

    @staticmethod
    def _latest_submit_attempt_payload(
        session: Session,
        reservation_id: uuid.UUID,
    ) -> Mapping[str, Any] | None:
        event = session.scalar(
            select(AdaptiveRiskReservationEvent)
            .where(AdaptiveRiskReservationEvent.reservation_id == reservation_id)
            .where(AdaptiveRiskReservationEvent.event_type == "submit_indeterminate")
            .order_by(AdaptiveRiskReservationEvent.sequence.desc())
            .limit(1)
        )
        if event is None:
            return None
        details = dict((event.payload_json or {}).get("details") or {})
        evidence = details.get("lifecycle_evidence")
        if not isinstance(evidence, Mapping):
            raise AdaptiveReservationStateConflict(
                "prior submit-indeterminate event lacks typed evidence"
            )
        return evidence

    @staticmethod
    def _validate_against_prior_submit_attempt(
        session: Session,
        reservation: AdaptiveRiskReservation,
        *,
        broker_source: str,
        connection_generation: str,
        account_scope: str,
        client_order_id: str,
        broker_order_id: str | None,
        observed_at: datetime,
        available_at: datetime,
    ) -> None:
        prior = AdaptiveRiskReservationStore._latest_submit_attempt_payload(
            session, reservation.reservation_id
        )
        if prior is None:
            return
        comparisons = {
            "broker_source": (broker_source, prior.get("broker_source")),
            "connection_generation": (
                connection_generation,
                prior.get("connection_generation"),
            ),
            "account_scope": (account_scope, prior.get("account_scope")),
            "client_order_id": (client_order_id, prior.get("client_order_id")),
        }
        prior_broker_order_id = prior.get("broker_order_id")
        if prior_broker_order_id is not None:
            comparisons["broker_order_id"] = (
                broker_order_id,
                prior_broker_order_id,
            )
        mismatches = sorted(
            name
            for name, (supplied, persisted) in comparisons.items()
            if supplied != persisted
        )
        if mismatches:
            raise AdaptiveReservationStateConflict(
                "lifecycle differs from retained submit attempt: "
                + ",".join(mismatches)
            )
        prior_observed = _parse_utc(
            prior.get("observed_at"), "prior_submit_attempt.observed_at"
        )
        prior_available = _parse_utc(
            prior.get("available_at"), "prior_submit_attempt.available_at"
        )
        if observed_at < prior_observed or available_at < prior_available:
            raise AdaptiveReservationStateConflict(
                "out-of-order lifecycle predates retained submit attempt"
            )

    @staticmethod
    def _verify_committed_alpaca_paper_entry_fill(
        session: Session,
        reservation: AdaptiveRiskReservation,
        packet: AdaptiveRiskDecisionPacket,
        evidence: DurableOrderLifecycleEvidence,
    ) -> None:
        """Bind a cumulative watermark to one already-appended exact fill.

        A REST order projection is useful reconciliation evidence, but it is
        not an execution record.  The PAPER cumulative-fill watermark may
        therefore advance only from the immutable activity row and the exact
        broker-query observation that published it in this same transaction
        (or in an earlier committed transaction during recovery).
        """

        if (
            evidence.durability_kind != "committed_alpaca_paper_fill"
            or evidence.source_record_table != _ALPACA_PAPER_FILL_TABLE
        ):
            raise AdaptiveReservationStateConflict(
                "Alpaca PAPER cumulative fill lacks committed fill authority"
            )
        fill = session.scalar(
            select(AlpacaPaperFillActivity)
            .where(
                AlpacaPaperFillActivity.event_sha256
                == evidence.source_record_id
            )
            .with_for_update()
        )
        if fill is None:
            raise AdaptiveReservationStateConflict(
                "committed Alpaca PAPER fill source row is missing"
            )
        try:
            prepared = verify_alpaca_paper_fill_activity_row(fill)
        except AlpacaFillActivityError as exc:
            raise AdaptiveReservationStateConflict(
                "committed Alpaca PAPER fill source row failed verification"
            ) from exc

        observed_cumulative = _exact_nonnegative_decimal(
            fill.cumulative_quantity,
            "alpaca_fill.cumulative_quantity",
        )
        if observed_cumulative != observed_cumulative.to_integral_value():
            raise AdaptiveReservationStateConflict(
                "committed Alpaca PAPER cumulative fill is not whole-share"
            )
        cumulative = int(observed_cumulative)
        expected_status = (
            "filled"
            if cumulative >= int(reservation.planned_quantity_shares)
            else "partially_filled"
        )
        provider_event_prefix = (
            f"alpaca-fill:{fill.event_sha256}:observation:"
        )
        if not evidence.provider_event_id.startswith(provider_event_prefix):
            raise AdaptiveReservationStateConflict(
                "committed Alpaca PAPER fill lacks exact observation identity"
            )
        observation_sha256 = evidence.provider_event_id[
            len(provider_event_prefix) :
        ]
        if _SHA256_RE.fullmatch(observation_sha256) is None:
            raise AdaptiveReservationStateConflict(
                "committed Alpaca PAPER observation identity is malformed"
            )
        observed = session.execute(
            select(
                AlpacaPaperFillObservationActivity,
                AlpacaPaperFillQueryObservation,
            )
            .join(
                AlpacaPaperFillQueryObservation,
                AlpacaPaperFillQueryObservation.observation_sha256
                == AlpacaPaperFillObservationActivity.observation_sha256,
            )
            .where(
                AlpacaPaperFillObservationActivity.observation_sha256
                == observation_sha256,
                AlpacaPaperFillObservationActivity.fill_event_sha256
                == fill.event_sha256,
                AlpacaPaperFillObservationActivity.immutable_fill_identity_sha256
                == fill.immutable_fill_identity_sha256,
                AlpacaPaperFillQueryObservation.reservation_id
                == reservation.reservation_id,
                AlpacaPaperFillQueryObservation.provider_order_id
                == fill.provider_order_id,
                AlpacaPaperFillQueryObservation.order_role == "entry",
                AlpacaPaperFillQueryObservation.observation_authority_status
                == "verified",
                AlpacaPaperFillQueryObservation.pagination_complete.is_(True),
            )
            .with_for_update()
        ).first()
        if observed is None:
            raise AdaptiveReservationStateConflict(
                "committed Alpaca PAPER fill lacks its exact read observation"
            )
        mapping, observation = observed
        observation_content_sha256 = hashlib.sha256(
            observation.observation_content_canonical_json.encode("utf-8")
        ).hexdigest()
        expected_provider_event_id = (
            f"{provider_event_prefix}{observation.observation_sha256}"
        )
        exact_checks = {
            "reservation_id": (fill.reservation_id, reservation.reservation_id),
            "decision_packet_sha256": (
                fill.decision_packet_sha256,
                packet.decision_packet_sha256,
            ),
            "account_scope": (fill.account_scope, "alpaca:paper"),
            "account_identity_sha256": (
                fill.account_identity_sha256,
                packet.account_identity_sha256,
            ),
            "execution_family": (fill.execution_family, "alpaca_spot"),
            "position_direction": (fill.position_direction, "long"),
            "symbol": (fill.symbol, reservation.symbol),
            "capture_schema_version": (
                fill.capture_schema_version,
                "chili.alpaca-paper-fill-activity.v2",
            ),
            "capture_authority_status": (
                fill.capture_authority_status,
                "verified",
            ),
            "order_role": (fill.order_role, "entry"),
            "order_ownership_status": (
                fill.order_ownership_status,
                "reservation_bound",
            ),
            "side": (fill.side, "buy"),
            "entry_provider_order_id": (
                fill.entry_provider_order_id,
                reservation.broker_order_id,
            ),
            "provider_order_id": (
                fill.provider_order_id,
                reservation.broker_order_id,
            ),
            "provider_client_order_id_status": (
                fill.provider_client_order_id_status,
                "authoritative",
            ),
            "provider_client_order_id": (
                fill.provider_client_order_id,
                packet.client_order_id,
            ),
            "provider_event_clock_status": (
                fill.provider_event_clock_status,
                "authoritative",
            ),
            "provider_event_clock_field": (
                fill.provider_event_clock_field,
                "transaction_time",
            ),
            "broker_connection_generation": (
                fill.broker_connection_generation,
                evidence.connection_generation,
            ),
            "provider_event_id": (
                evidence.provider_event_id,
                expected_provider_event_id,
            ),
            "observation_content_sha256": (
                observation_content_sha256,
                observation.observation_content_sha256,
            ),
            "observation_sha256": (
                observation.observation_sha256,
                observation.observation_content_sha256,
            ),
            "observation_decision_packet_sha256": (
                observation.decision_packet_sha256,
                packet.decision_packet_sha256,
            ),
            "observation_account_scope": (
                observation.account_scope,
                packet.account_scope,
            ),
            "observation_account_identity_sha256": (
                observation.account_identity_sha256,
                packet.account_identity_sha256,
            ),
            "observation_execution_family": (
                observation.execution_family,
                packet.execution_family,
            ),
            "observation_symbol": (
                observation.symbol,
                reservation.symbol,
            ),
            "observation_expected_client_order_id": (
                observation.expected_client_order_id,
                packet.client_order_id,
            ),
            "observation_cycle_connection_generation": (
                observation.cycle_broker_connection_generation,
                evidence.connection_generation,
            ),
            "mapping_provider_activity_id": (
                mapping.provider_activity_id,
                fill.provider_activity_id,
            ),
            "mapping_provider_payload_sha256": (
                mapping.provider_payload_sha256,
                fill.provider_payload_sha256,
            ),
            "source_record_id": (
                evidence.source_record_id,
                fill.event_sha256,
            ),
            "event_content_sha256": (
                evidence.event_content_sha256,
                fill.event_sha256,
            ),
            "cumulative_filled_quantity": (
                evidence.cumulative_filled_quantity,
                cumulative,
            ),
            "order_status": (evidence.order_status, expected_status),
            "observed_at": (
                evidence.observed_at,
                _utc(fill.provider_transaction_at, "fill.provider_transaction_at"),
            ),
            "available_at": (
                evidence.available_at,
                _utc(observation.available_at, "observation.available_at"),
            ),
            "immutable_fill_identity_sha256": (
                fill.immutable_fill_identity_sha256,
                prepared.immutable_fill_identity_sha256,
            ),
        }
        changed = sorted(
            name for name, (actual, expected) in exact_checks.items()
            if actual != expected
        )
        if changed:
            raise AdaptiveReservationStateConflict(
                "committed Alpaca PAPER fill binding mismatch: "
                + ",".join(changed)
            )

    @staticmethod
    def _verify_committed_alpaca_post_settlement_fill(
        session: Session,
        reservation: AdaptiveRiskReservation,
        packet: AdaptiveRiskDecisionPacket,
        evidence: DurableOrderLifecycleEvidence,
    ) -> None:
        if (
            evidence.durability_kind
            != POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
            or evidence.source_record_table
            != _ALPACA_POST_SETTLEMENT_FILL_TABLE
        ):
            raise AdaptiveReservationStateConflict(
                "post-settlement Alpaca cumulative fill lacks contradiction authority"
            )
        row = session.scalar(
            select(AlpacaPaperPostSettlementFillContradiction)
            .where(
                AlpacaPaperPostSettlementFillContradiction.contradiction_sha256
                == evidence.source_record_id
            )
            .with_for_update()
        )
        if row is None:
            raise AdaptiveReservationStateConflict(
                "post-settlement Alpaca contradiction row is missing"
            )
        try:
            verify_alpaca_paper_post_settlement_fill_contradiction_row(row)
        except AlpacaFillActivityError as exc:
            raise AdaptiveReservationStateConflict(
                "post-settlement Alpaca contradiction failed verification"
            ) from exc
        settlement = session.scalar(
            select(AlpacaPaperCycleSettlement)
            .where(
                AlpacaPaperCycleSettlement.settlement_sha256
                == row.settlement_sha256
            )
            .with_for_update()
        )
        if settlement is None:
            raise AdaptiveReservationStateConflict(
                "post-settlement Alpaca immutable settlement is missing"
            )
        try:
            verify_cycle_settlement_content(settlement)
        except AlpacaCycleSettlementIntegrityError as exc:
            raise AdaptiveReservationStateConflict(
                "post-settlement Alpaca settlement failed verification"
            ) from exc
        cumulative_decimal = _exact_nonnegative_decimal(
            row.broker_observed_cumulative_quantity,
            "post_settlement_fill.broker_observed_cumulative_quantity",
        )
        if cumulative_decimal != cumulative_decimal.to_integral_value():
            raise AdaptiveReservationStateConflict(
                "post-settlement Alpaca cumulative fill is not whole-share"
            )
        cumulative = int(cumulative_decimal)
        expected_status = (
            "filled"
            if cumulative >= int(reservation.planned_quantity_shares)
            else "partially_filled"
        )
        source_state_valid = row.source_state == reservation.state
        if (
            reservation.state == "exposure_quarantined"
            and row.source_state == "closed"
            and reservation.lifecycle_contradiction_source_state == "closed"
            and reservation.lifecycle_contradiction_evidence_sha256
            == row.contradiction_sha256
        ):
            source_state_valid = True
        exact_checks = {
            "terminal_authority": (bool(row.is_projection_terminal), True),
            "source_state": (source_state_valid, True),
            "reservation_id": (row.reservation_id, reservation.reservation_id),
            "decision_packet_sha256": (
                row.decision_packet_sha256,
                packet.decision_packet_sha256,
            ),
            "settlement_reservation_id": (
                settlement.reservation_id,
                reservation.reservation_id,
            ),
            "account_scope": (row.account_scope, packet.account_scope),
            "account_identity_sha256": (
                row.account_identity_sha256,
                packet.account_identity_sha256,
            ),
            "execution_family": (
                row.execution_family,
                packet.execution_family,
            ),
            "broker_environment": (
                row.broker_environment,
                packet.broker_environment,
            ),
            "symbol": (row.symbol, reservation.symbol),
            "client_order_id": (
                row.expected_client_order_id,
                packet.client_order_id,
            ),
            "broker_order_id": (
                row.broker_order_id,
                reservation.broker_order_id,
            ),
            "connection_generation": (
                row.broker_connection_generation,
                evidence.connection_generation,
            ),
            "provider_event_id": (
                evidence.provider_event_id,
                f"alpaca-post-settlement-fill:{row.contradiction_sha256}",
            ),
            "event_content_sha256": (
                evidence.event_content_sha256,
                row.contradiction_sha256,
            ),
            "cumulative_filled_quantity": (
                evidence.cumulative_filled_quantity,
                cumulative,
            ),
            "order_status": (evidence.order_status, expected_status),
            "observed_at": (
                evidence.observed_at,
                _utc(
                    row.provider_transaction_at,
                    "post_settlement_fill.provider_transaction_at",
                ),
            ),
            "available_at": (
                evidence.available_at,
                _utc(
                    row.provider_available_at,
                    "post_settlement_fill.provider_available_at",
                ),
            ),
            "source_record_id": (
                evidence.source_record_id,
                row.contradiction_sha256,
            ),
        }
        changed = sorted(
            name
            for name, (actual, expected) in exact_checks.items()
            if actual != expected
        )
        if changed:
            raise AdaptiveReservationStateConflict(
                "post-settlement Alpaca contradiction binding mismatch: "
                + ",".join(changed)
            )

    @staticmethod
    def _validate_lifecycle_evidence(
        session: Session,
        reservation: AdaptiveRiskReservation,
        evidence: DurableOrderLifecycleEvidence,
        *,
        expected_event_kind: str,
        idempotent_replay: bool = False,
    ) -> AdaptiveRiskDecisionPacket:
        if evidence.event_kind != expected_event_kind:
            raise AdaptiveRiskContractError(
                f"expected {expected_event_kind} lifecycle evidence"
            )
        if expected_event_kind == "order_accepted":
            if evidence.cumulative_filled_quantity != 0 or evidence.order_status not in {
                "accepted",
                "new",
                "open",
                "working",
            }:
                raise AdaptiveReservationStateConflict(
                    "order-accepted evidence has incompatible status/fill truth"
                )
        elif expected_event_kind == "terminal_zero_fill":
            if evidence.cumulative_filled_quantity != 0 or evidence.order_status not in {
                "rejected",
                "canceled",
                "cancelled",
                "expired",
                "confirmed_zero",
            }:
                raise AdaptiveReservationStateConflict(
                    "terminal-zero evidence has incompatible status/fill truth"
                )
        elif expected_event_kind == "cumulative_fill":
            allowed_statuses = (
                {"accepted", "new", "open", "working"}
                if evidence.cumulative_filled_quantity == 0
                else {"partially_filled", "filled"}
            )
            if evidence.order_status not in allowed_statuses:
                raise AdaptiveReservationStateConflict(
                    "cumulative-fill evidence has incompatible status/fill truth"
                )
        elif expected_event_kind == "filled_entry_terminal":
            if (
                evidence.cumulative_filled_quantity <= 0
                or evidence.order_status
                not in {"canceled", "cancelled", "expired"}
            ):
                raise AdaptiveReservationStateConflict(
                    "filled-entry terminal evidence has incompatible status/fill truth"
                )
        elif expected_event_kind == "position_reduced":
            if evidence.order_status != "partially_exited":
                raise AdaptiveReservationStateConflict(
                    "position-reduced evidence must carry order_status=partially_exited"
                )
        elif expected_event_kind == "position_flat" and evidence.order_status != "flat":
            raise AdaptiveReservationStateConflict(
                "position-flat evidence must carry order_status=flat"
            )
        packet = session.get(
            AdaptiveRiskDecisionPacket, reservation.decision_packet_sha256
        )
        if packet is None:
            raise AdaptiveReservationStateConflict(
                "immutable decision packet is missing"
            )
        if (
            packet.execution_surface == "alpaca_paper"
            and expected_event_kind == "cumulative_fill"
        ):
            if (
                evidence.durability_kind
                == POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
            ):
                AdaptiveRiskReservationStore._verify_committed_alpaca_post_settlement_fill(
                    session,
                    reservation,
                    packet,
                    evidence,
                )
            else:
                AdaptiveRiskReservationStore._verify_committed_alpaca_paper_entry_fill(
                    session,
                    reservation,
                    packet,
                    evidence,
                )
        comparisons = {
            "account_scope": (evidence.account_scope, reservation.account_scope),
            "execution_family": (
                evidence.execution_family,
                packet.execution_family,
            ),
            "broker_environment": (
                evidence.broker_environment,
                packet.broker_environment,
            ),
            "account_identity_sha256": (
                evidence.account_identity_sha256,
                packet.account_identity_sha256,
            ),
            "client_order_id": (evidence.client_order_id, packet.client_order_id),
        }
        mismatches = sorted(
            name
            for name, (supplied, persisted) in comparisons.items()
            if supplied != persisted
        )
        if mismatches:
            raise AdaptiveReservationStateConflict(
                "lifecycle identity mismatch: " + ",".join(mismatches)
            )

        database_now = AdaptiveRiskReservationStore._clock(session)
        if evidence.available_at > database_now:
            raise AdaptiveReservationStateConflict(
                "lifecycle evidence is not yet available at the database clock"
            )

        account_snapshot = dict(packet.account_snapshot_json or {})
        if packet.execution_surface == "db_paper":
            if evidence.broker_source != "db_paper":
                raise AdaptiveReservationStateConflict(
                    "DB-paper reservation requires db_paper lifecycle source"
                )
            if evidence.durability_kind != "committed_db_paper_fill":
                raise AdaptiveReservationStateConflict(
                    "DB-paper fill requires a canonical durable row"
                )
        else:
            expected_source = str(account_snapshot.get("venue") or "").strip().lower()
            if evidence.broker_source != expected_source:
                raise AdaptiveReservationStateConflict(
                    "lifecycle broker source differs from immutable account snapshot"
                )
            expected_durability = (
                evidence.durability_kind
                if packet.execution_surface == "alpaca_paper"
                and expected_event_kind == "cumulative_fill"
                and evidence.durability_kind
                in {
                    "committed_alpaca_paper_fill",
                    POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND,
                }
                else "authoritative_broker_event"
            )
            if evidence.durability_kind != expected_durability:
                raise AdaptiveReservationStateConflict(
                    "broker reservation lifecycle durability is incompatible"
                )

        if reservation.broker_source is not None and (
            reservation.broker_source != evidence.broker_source
        ):
            raise AdaptiveReservationStateConflict(
                "lifecycle broker source changed after reservation binding"
            )
        if reservation.broker_connection_generation is not None and (
            reservation.broker_connection_generation
            != evidence.connection_generation
        ):
            raise AdaptiveReservationStateConflict(
                "lifecycle connection generation changed without reconciliation"
            )
        if reservation.broker_order_id is not None and (
            reservation.broker_order_id != evidence.broker_order_id
        ):
            raise AdaptiveReservationStateConflict(
                "lifecycle broker_order_id changed after reservation binding"
            )
        is_post_settlement_late_fill = (
            evidence.durability_kind
            == POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
        )
        if (
            not idempotent_replay
            and not is_post_settlement_late_fill
            and reservation.last_broker_observed_at is not None
            and evidence.observed_at
            < _utc(
                reservation.last_broker_observed_at,
                "reservation.last_broker_observed_at",
            )
        ):
            raise AdaptiveReservationStateConflict(
                "out-of-order lifecycle observed_at regression"
            )
        if (
            not idempotent_replay
            and reservation.last_broker_available_at is not None
            and evidence.available_at
            < _utc(
                reservation.last_broker_available_at,
                "reservation.last_broker_available_at",
            )
        ):
            raise AdaptiveReservationStateConflict(
                "out-of-order lifecycle available_at regression"
            )
        AdaptiveRiskReservationStore._validate_against_prior_submit_attempt(
            session,
            reservation,
            broker_source=evidence.broker_source,
            connection_generation=evidence.connection_generation,
            account_scope=evidence.account_scope,
            client_order_id=evidence.client_order_id,
            broker_order_id=evidence.broker_order_id,
            observed_at=evidence.observed_at,
            available_at=evidence.available_at,
        )

        if evidence.durability_kind == "committed_db_paper_fill":
            try:
                source_id = int(evidence.source_record_id)
            except (TypeError, ValueError) as exc:
                raise AdaptiveReservationStateConflict(
                    "DB-paper canonical source_record_id is invalid"
                ) from exc
            source_row = session.get(
                TradingAutomationSimulatedFill,
                source_id,
                with_for_update=True,
            )
            if source_row is None:
                raise AdaptiveReservationStateConflict(
                    "DB-paper canonical fill is missing from the caller transaction"
                )
            marker = dict(source_row.marker_json or {})
            required_marker = {
                "adaptive_risk_lifecycle_event_id": evidence.provider_event_id,
                "adaptive_risk_reservation_id": str(reservation.reservation_id),
                "adaptive_risk_decision_packet_sha256": (
                    reservation.decision_packet_sha256
                ),
                "adaptive_risk_client_order_id": packet.client_order_id,
                "adaptive_risk_account_scope": reservation.account_scope,
                "adaptive_risk_connection_generation": (
                    evidence.connection_generation
                ),
                "adaptive_risk_cumulative_fill_quantity": int(
                    evidence.cumulative_filled_quantity
                ),
            }
            if expected_event_kind in {"position_reduced", "position_flat"}:
                required_marker["adaptive_risk_remaining_open_quantity"] = int(
                    evidence.remaining_open_quantity
                )
            if any(marker.get(key) != value for key, value in required_marker.items()):
                raise AdaptiveReservationStateConflict(
                    "DB-paper canonical fill is not bound to this reservation"
                )
            if source_row.symbol != reservation.symbol:
                raise AdaptiveReservationStateConflict(
                    "DB-paper canonical fill symbol mismatch"
                )
            if source_row.lane != "simulation" or source_row.side != "long":
                raise AdaptiveReservationStateConflict(
                    "DB-paper canonical fill lane/side mismatch"
                )
            if expected_event_kind == "cumulative_fill":
                if not (
                    source_row.action == "enter_long"
                    and source_row.fill_type == "entry"
                    and source_row.position_state_after == "long"
                ):
                    raise AdaptiveReservationStateConflict(
                        "DB-paper canonical row is not an entry fill"
                    )
                try:
                    row_quantity = int(float(source_row.quantity))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise AdaptiveReservationStateConflict(
                        "DB-paper canonical fill quantity is invalid"
                    ) from exc
                if (
                    float(source_row.quantity) != row_quantity
                    or row_quantity != evidence.cumulative_filled_quantity
                ):
                    raise AdaptiveReservationStateConflict(
                        "DB-paper canonical fill quantity mismatch"
                    )
            elif expected_event_kind in {"position_reduced", "position_flat"}:
                expected_after = (
                    "long" if expected_event_kind == "position_reduced" else "flat"
                )
                if not (
                    source_row.fill_type == "exit"
                    and source_row.position_state_after == expected_after
                ):
                    raise AdaptiveReservationStateConflict(
                        "DB-paper canonical row is not the claimed position fill"
                    )
                try:
                    row_quantity = int(float(source_row.quantity))
                except (TypeError, ValueError, OverflowError) as exc:
                    raise AdaptiveReservationStateConflict(
                        "DB-paper position fill quantity is invalid"
                    ) from exc
                expected_exit_quantity = int(reservation.open_quantity_shares) - int(
                    evidence.remaining_open_quantity
                )
                if (
                    float(source_row.quantity) != row_quantity
                    or row_quantity != expected_exit_quantity
                    or row_quantity <= 0
                ):
                    raise AdaptiveReservationStateConflict(
                        "DB-paper position fill quantity mismatch"
                    )
            expected_content_sha = canonical_db_paper_fill_content_sha256(source_row)
            if evidence.event_content_sha256 != expected_content_sha:
                raise AdaptiveReservationStateConflict(
                    "DB-paper canonical fill content hash mismatch"
                )
            created_at = source_row.created_at
            if created_at is not None:
                if created_at.tzinfo is None:
                    created_at = created_at.replace(tzinfo=UTC)
                if evidence.available_at < created_at.astimezone(UTC):
                    raise AdaptiveReservationStateConflict(
                        "DB-paper evidence predates its canonical durable row"
                    )
        return packet

    @staticmethod
    def _validate_submit_attempt_evidence(
        session: Session,
        reservation: AdaptiveRiskReservation,
        evidence: DurableSubmitAttemptEvidence,
    ) -> None:
        packet = session.get(
            AdaptiveRiskDecisionPacket, reservation.decision_packet_sha256
        )
        if packet is None:
            raise AdaptiveReservationStateConflict(
                "immutable decision packet is missing"
            )
        account_snapshot = dict(packet.account_snapshot_json or {})
        comparisons = {
            "account_scope": (evidence.account_scope, reservation.account_scope),
            "execution_family": (
                evidence.execution_family,
                packet.execution_family,
            ),
            "broker_environment": (
                evidence.broker_environment,
                packet.broker_environment,
            ),
            "account_identity_sha256": (
                evidence.account_identity_sha256,
                packet.account_identity_sha256,
            ),
            "client_order_id": (evidence.client_order_id, packet.client_order_id),
            "broker_source": (
                evidence.broker_source,
                str(account_snapshot.get("venue") or "").strip().lower(),
            ),
        }
        mismatches = sorted(
            name
            for name, (supplied, persisted) in comparisons.items()
            if supplied != persisted
        )
        if mismatches:
            raise AdaptiveReservationStateConflict(
                "submit-attempt identity mismatch: " + ",".join(mismatches)
            )
        database_now = AdaptiveRiskReservationStore._clock(session)
        if evidence.available_at > database_now:
            raise AdaptiveReservationStateConflict(
                "submit-attempt evidence is not yet available at the database clock"
            )
        if reservation.broker_source is not None:
            if reservation.broker_source != evidence.broker_source:
                raise AdaptiveReservationStateConflict(
                    "submit-attempt broker source changed after reservation binding"
                )
            if (
                reservation.broker_connection_generation
                != evidence.connection_generation
            ):
                raise AdaptiveReservationStateConflict(
                    "submit-attempt connection generation changed without reconciliation"
                )
            if reservation.broker_order_id != evidence.broker_order_id:
                raise AdaptiveReservationStateConflict(
                    "submit-attempt broker_order_id changed after reservation binding"
                )
            if reservation.last_broker_observed_at is not None and (
                evidence.observed_at
                < _utc(
                    reservation.last_broker_observed_at,
                    "reservation.last_broker_observed_at",
                )
            ):
                raise AdaptiveReservationStateConflict(
                    "out-of-order submit-attempt observed_at regression"
                )
            if reservation.last_broker_available_at is not None and (
                evidence.available_at
                < _utc(
                    reservation.last_broker_available_at,
                    "reservation.last_broker_available_at",
                )
            ):
                raise AdaptiveReservationStateConflict(
                    "out-of-order submit-attempt available_at regression"
                )
        AdaptiveRiskReservationStore._validate_against_prior_submit_attempt(
            session,
            reservation,
            broker_source=evidence.broker_source,
            connection_generation=evidence.connection_generation,
            account_scope=evidence.account_scope,
            client_order_id=evidence.client_order_id,
            broker_order_id=evidence.broker_order_id,
            observed_at=evidence.observed_at,
            available_at=evidence.available_at,
        )

    @staticmethod
    def _bind_lifecycle_evidence(
        reservation: AdaptiveRiskReservation,
        evidence: DurableOrderLifecycleEvidence,
    ) -> None:
        reservation.broker_source = evidence.broker_source
        reservation.broker_connection_generation = evidence.connection_generation
        reservation.broker_order_id = evidence.broker_order_id
        reservation.last_broker_observed_at = evidence.observed_at
        reservation.last_broker_available_at = evidence.available_at
        reservation.last_source_event_content_sha256 = (
            evidence.event_content_sha256
        )

    @staticmethod
    def _verify_existing_lifecycle_event(
        existing: AdaptiveRiskReservationEvent,
        evidence: DurableOrderLifecycleEvidence | DurableSubmitAttemptEvidence,
        *,
        expected_event_type: str,
    ) -> None:
        details = dict((existing.payload_json or {}).get("details") or {})
        persisted_evidence = details.get("lifecycle_evidence")
        if not isinstance(persisted_evidence, Mapping):
            raise AdaptiveReservationIdempotencyConflict(
                "existing lifecycle event lacks durable evidence"
            )
        supplied_evidence = evidence.to_payload()
        if (
            existing.event_type != expected_event_type
            or persisted_evidence.get("evidence_sha256")
            != evidence.evidence_sha256
        ):
            changed_fields = sorted(
                key
                for key in set(persisted_evidence) | set(supplied_evidence)
                if persisted_evidence.get(key) != supplied_evidence.get(key)
            )
            suffix = (
                ":" + ",".join(changed_fields)
                if changed_fields
                else ""
            )
            raise AdaptiveReservationIdempotencyConflict(
                "provider_event_id was reused for different lifecycle evidence"
                + suffix
            )

    def mark_submitted(
        self,
        reservation_id: uuid.UUID,
        *,
        evidence: DurableOrderLifecycleEvidence,
        session: Session | None = None,
    ) -> AdaptiveReservationState:
        """Persist a broker-accepted POST without changing economic dimensions."""

        with self._transaction(session) as session:
            reservation = self._reservation_for_transition(session, reservation_id)
            opportunity = self._opportunity_for_transition(session, reservation)
            existing = self._existing_broker_event(
                session, reservation_id, evidence.provider_event_id
            )
            # Re-validate authority on every invocation, including an
            # idempotent broker-event retry.  Otherwise a lifecycle row
            # cannot bypass current identity and clock requirements merely
            # because its provider_event_id is already durable.
            self._validate_lifecycle_evidence(
                session,
                reservation,
                evidence,
                expected_event_kind="order_accepted",
                idempotent_replay=existing is not None,
            )
            if existing is not None:
                self._verify_existing_lifecycle_event(
                    existing, evidence, expected_event_type="broker_submitted"
                )
                return self._state_snapshot(reservation, opportunity)
            if evidence.cumulative_filled_quantity != 0:
                raise AdaptiveReservationStateConflict(
                    "order-accepted evidence cannot hide a non-zero fill"
                )
            if reservation.state == "submitted":
                if reservation.broker_order_id != evidence.broker_order_id:
                    raise AdaptiveReservationIdempotencyConflict(
                        "submitted retry changed broker_order_id"
                    )
            if reservation.state not in {
                "reserved",
                "submitted",
                "submit_indeterminate",
            }:
                raise AdaptiveReservationStateConflict(
                    f"cannot mark {reservation.state} reservation submitted"
                )
            now = self._clock(session)
            reservation.state = "submitted"
            self._bind_lifecycle_evidence(reservation, evidence)
            reservation.submitted_at = now
            reservation.updated_at = now
            reservation.version = int(reservation.version) + 1
            self._append_reservation_event(
                session,
                reservation,
                event_type="broker_submitted",
                effective_at=now,
                broker_event_id=evidence.provider_event_id,
                details={
                    "broker_order_id": evidence.broker_order_id,
                    "opportunity_consumed": False,
                    "opportunity_status": (
                        opportunity.status if opportunity else "not_applicable"
                    ),
                    "lifecycle_evidence": evidence.to_payload(),
                },
            )
            return self._state_snapshot(reservation, opportunity)

    def mark_submit_indeterminate(
        self,
        reservation_id: uuid.UUID,
        *,
        evidence: DurableSubmitAttemptEvidence,
        reason: str,
        session: Session | None = None,
    ) -> AdaptiveReservationState:
        """Keep every dimension reserved when POST outcome is unknowable."""

        reason = _norm(reason, "reason")
        with self._transaction(session) as session:
            reservation = self._reservation_for_transition(session, reservation_id)
            opportunity = self._opportunity_for_transition(session, reservation)
            existing = self._existing_broker_event(
                session, reservation_id, evidence.attempt_event_id
            )
            if existing is not None:
                self._verify_existing_lifecycle_event(
                    existing,
                    evidence,
                    expected_event_type="submit_indeterminate",
                )
                details = dict((existing.payload_json or {}).get("details") or {})
                if details.get("reason") != reason:
                    raise AdaptiveReservationIdempotencyConflict(
                        "submit-attempt retry changed its retained reason"
                    )
                return self._state_snapshot(reservation, opportunity)
            self._validate_submit_attempt_evidence(session, reservation, evidence)
            if reservation.state not in {
                "reserved",
                "submitted",
                "submit_indeterminate",
            }:
                raise AdaptiveReservationStateConflict(
                    f"cannot mark {reservation.state} submit-indeterminate"
                )
            now = self._clock(session)
            reservation.state = "submit_indeterminate"
            reservation.updated_at = now
            reservation.version = int(reservation.version) + 1
            self._append_reservation_event(
                session,
                reservation,
                event_type="submit_indeterminate",
                effective_at=now,
                broker_event_id=evidence.attempt_event_id,
                details={
                    "reason": reason,
                    "reservation_retained": True,
                    "opportunity_status": (
                        opportunity.status if opportunity else "not_applicable"
                    ),
                    "lifecycle_evidence": evidence.to_payload(),
                },
            )
            return self._state_snapshot(reservation, opportunity)

    def release_zero_fill(
        self,
        reservation_id: uuid.UUID,
        *,
        reason: str,
        evidence: DurableOrderLifecycleEvidence | None = None,
        session: Session | None = None,
        pre_post_claim_fence: (
            Callable[[Session, AdaptiveRiskReservation], bool] | None
        ) = None,
    ) -> AdaptiveReservationState:
        """Release only a pre-POST or authoritative terminal zero-fill outcome."""

        reason = _norm(reason, "reason", lower=True)
        if reason not in _SAFE_ZERO_RELEASE_REASONS:
            raise AdaptiveRiskContractError("unsafe adaptive reservation release reason")
        if reason != "pre_post_release" and evidence is None:
            raise AdaptiveRiskContractError(
                "terminal zero-fill release requires durable lifecycle evidence"
            )
        if reason == "pre_post_release" and evidence is not None:
            raise AdaptiveRiskContractError(
                "pre-POST release cannot claim a broker lifecycle event"
            )
        if pre_post_claim_fence is not None and reason != "pre_post_release":
            raise AdaptiveRiskContractError(
                "action-claim fence is only valid for a pre-POST release"
            )
        with self._transaction(session) as session:
            initial = session.get(AdaptiveRiskReservation, reservation_id)
            if initial is None:
                raise AdaptiveReservationStateConflict(
                    "reservation does not exist"
                )
            account_locked_release = bool(
                pre_post_claim_fence is not None
                or (
                    reason == "pre_post_release"
                    and initial.account_scope == "alpaca:paper"
                )
            )
            if account_locked_release:
                # The cross-ledger release owns both the Alpaca action claim and
                # adaptive reservation.  Acquire their advisory locks in the one
                # canonical order before locking either mutable row.  The initial
                # read discovers only the immutable account scope; it grants no
                # release authority.
                acquire_adaptive_risk_account_locks(
                    session,
                    account_scope=initial.account_scope,
                )
                session.expire(initial)
                reservation = session.scalar(
                    select(AdaptiveRiskReservation)
                    .where(
                        AdaptiveRiskReservation.reservation_id == reservation_id
                    )
                    .with_for_update()
                )
                if reservation is None:
                    raise AdaptiveReservationStateConflict(
                        "reservation does not exist"
                    )
            else:
                reservation = self._reservation_for_transition(
                    session,
                    reservation_id,
                )
            if (
                reason == "pre_post_release"
                and reservation.account_scope == "alpaca:paper"
                and pre_post_claim_fence is None
            ):
                packet = session.get(
                    AdaptiveRiskDecisionPacket,
                    reservation.decision_packet_sha256,
                )
                if packet is None:
                    raise AdaptiveReservationStateConflict(
                        "reservation decision packet does not exist"
                    )
                action_claim = session.scalar(
                    select(BrokerSymbolActionClaim)
                    .where(
                        BrokerSymbolActionClaim.account_scope
                        == reservation.account_scope,
                        BrokerSymbolActionClaim.symbol
                        == reservation.symbol,
                        BrokerSymbolActionClaim.action == "entry",
                    )
                    .with_for_update()
                )
                bound_or_active_claim = bool(
                    action_claim is not None
                    and (
                        action_claim.phase != "resolved"
                        or action_claim.client_order_id
                        == packet.client_order_id
                    )
                )
                if bound_or_active_claim:
                    # A local assertion that no HTTP call occurred is not
                    # durable proof once an Alpaca PAPER CID/action claim
                    # exists.  Require the exact claim+marker fence in this same
                    # transaction so a committed transport-start/dispatch
                    # generation can never free risk or make the once-per-day
                    # opportunity reusable.  A reservation with no bound/active
                    # claim remains safely releasable while both account locks
                    # prevent a cooperating claim creator from racing this
                    # decision.
                    raise AdaptiveReservationStateConflict(
                        "Alpaca PAPER pre-POST release requires exact action-claim fence"
                    )
            if evidence is not None:
                existing = self._existing_broker_event(
                    session, reservation_id, evidence.provider_event_id
                )
                if existing is not None:
                    self._verify_existing_lifecycle_event(
                        existing,
                        evidence,
                        expected_event_type="zero_fill_released",
                    )
                    opportunity = self._opportunity_for_transition(
                        session,
                        reservation,
                    )
                    return self._state_snapshot(reservation, opportunity)
            if reservation.state == "released":
                if reservation.release_reason != reason:
                    raise AdaptiveReservationIdempotencyConflict(
                        "release retry changed terminal reason"
                    )
                if pre_post_claim_fence is not None and not bool(
                    pre_post_claim_fence(session, reservation)
                ):
                    raise AdaptiveReservationStateConflict(
                        "pre-POST action claim fence was not confirmed"
                    )
                opportunity = self._opportunity_for_transition(
                    session,
                    reservation,
                )
                return self._state_snapshot(reservation, opportunity)
            if evidence is not None:
                self._validate_lifecycle_evidence(
                    session,
                    reservation,
                    evidence,
                    expected_event_kind="terminal_zero_fill",
                )
                allowed_statuses_by_reason = {
                    "broker_rejected": {"rejected"},
                    "broker_canceled": {"canceled", "cancelled"},
                    "broker_expired": {"expired"},
                    "confirmed_zero_fill": {"confirmed_zero"},
                }
                if evidence.order_status not in allowed_statuses_by_reason[reason]:
                    raise AdaptiveReservationStateConflict(
                        "zero-fill release reason disagrees with durable order status"
                    )
            elif reservation.state == "submit_indeterminate":
                raise AdaptiveReservationStateConflict(
                    "submit-indeterminate reservation must remain reserved"
                )
            if int(reservation.cumulative_filled_quantity_shares) != 0:
                raise AdaptiveReservationStateConflict(
                    "non-zero cumulative fill cannot release its opportunity"
                )
            if reservation.state not in {
                "reserved",
                "submitted",
                "submit_indeterminate",
            }:
                raise AdaptiveReservationStateConflict(
                    f"cannot zero-fill release {reservation.state} reservation"
                )
            if reason == "pre_post_release" and reservation.state != "reserved":
                raise AdaptiveReservationStateConflict(
                    "pre-POST release is only valid before broker submission"
                )
            if pre_post_claim_fence is not None and not bool(
                pre_post_claim_fence(session, reservation)
            ):
                raise AdaptiveReservationStateConflict(
                    "pre-POST action claim fence was not confirmed"
                )
            # The fenced path has now locked reservation -> action claim.  Lock
            # the once-per-day opportunity only after both, preserving the shared
            # canonical row order and preventing a claim/opportunity deadlock.
            opportunity = self._opportunity_for_transition(session, reservation)
            if opportunity is not None and (
                opportunity.status != "reserved"
                or opportunity.reservation_id != reservation.reservation_id
            ):
                raise AdaptiveReservationStateConflict(
                    "opportunity is not reserved by this reservation"
                )
            now = self._clock(session)
            if evidence is not None:
                self._bind_lifecycle_evidence(reservation, evidence)
            reservation.state = "released"
            reservation.pending_structural_risk_usd = Decimal("0")
            reservation.pending_gross_notional_usd = Decimal("0")
            reservation.pending_buying_power_impact_usd = Decimal("0")
            reservation.release_reason = reason
            reservation.released_at = now
            reservation.updated_at = now
            reservation.version = int(reservation.version) + 1
            if opportunity is not None:
                opportunity.status = "available"
                opportunity.reservation_id = None
                opportunity.updated_at = now
                opportunity.version = int(opportunity.version) + 1
            self._append_reservation_event(
                session,
                reservation,
                event_type="zero_fill_released",
                effective_at=now,
                broker_event_id=(
                    evidence.provider_event_id if evidence is not None else None
                ),
                details={
                    "reason": reason,
                    "opportunity_available": opportunity is not None,
                    "opportunity_status": (
                        opportunity.status if opportunity else "not_applicable"
                    ),
                    "lifecycle_evidence": (
                        evidence.to_payload() if evidence is not None else None
                    ),
                },
            )
            if opportunity is not None:
                self._append_opportunity_event(
                    session,
                    opportunity,
                    reservation_id=reservation.reservation_id,
                    event_type="opportunity_released",
                    effective_at=now,
                    details={"reason": reason, "confirmed_cumulative_fill": 0},
                )
            return self._state_snapshot(reservation, opportunity)

    def apply_cumulative_fill(
        self,
        reservation_id: uuid.UUID,
        *,
        evidence: DurableOrderLifecycleEvidence,
        session: Session | None = None,
    ) -> AdaptiveReservationState:
        """Advance fill truth; consume a first-dip claim on the first positive fact."""

        observed = evidence.cumulative_filled_quantity
        with self._transaction(session) as session:
            reservation = self._reservation_for_transition(session, reservation_id)
            opportunity = self._opportunity_for_transition(session, reservation)
            existing = self._existing_broker_event(
                session, reservation_id, evidence.provider_event_id
            )
            # Re-validate the sealed source row and exact observation even on
            # an idempotent retry.  Otherwise a lifecycle event written by a
            # retired projection-only path could survive indefinitely merely
            # because it reused the same provider_event_id.
            self._validate_lifecycle_evidence(
                session,
                reservation,
                evidence,
                expected_event_kind="cumulative_fill",
                idempotent_replay=existing is not None,
            )
            if existing is not None:
                if existing.event_type not in {
                    "fill_observation_no_advance",
                    "cumulative_fill_advanced",
                    "late_cumulative_fill_quarantined",
                    "cumulative_overfill_quarantined",
                    "quarantined_cumulative_fill_advanced",
                }:
                    raise AdaptiveReservationIdempotencyConflict(
                        "provider_event_id was reused for a non-fill lifecycle fact"
                    )
                self._verify_existing_lifecycle_event(
                    existing,
                    evidence,
                    expected_event_type=existing.event_type,
                )
                return self._state_snapshot(reservation, opportunity)
            previous = int(reservation.cumulative_filled_quantity_shares)
            source_state = str(reservation.state or "").strip().lower()
            allowed_source_states = {
                "reserved",
                "submitted",
                "submit_indeterminate",
                "partially_filled",
                "filled",
                "flat_pending_settlement",
                "released",
                "closed",
                "exposure_quarantined",
            }
            if source_state not in allowed_source_states:
                raise AdaptiveReservationStateConflict(
                    f"cannot apply cumulative fill from {source_state} reservation"
                )
            if observed < previous:
                raise AdaptiveReservationStateConflict(
                    "broker cumulative fill regressed below durable truth"
                )
            effective = observed
            now = (
                evidence.available_at
                if evidence.durability_kind
                == POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
                else self._clock(session)
            )
            if effective == previous:
                if source_state == "exposure_quarantined":
                    # The contradictory exposure and its first authoritative
                    # evidence are already durable.  A redundant cumulative
                    # observation must not mutate or de-quarantine that row.
                    return self._state_snapshot(reservation, opportunity)
                self._bind_lifecycle_evidence(reservation, evidence)
                self._append_reservation_event(
                    session,
                    reservation,
                    event_type="fill_observation_no_advance",
                    effective_at=now,
                    broker_event_id=evidence.provider_event_id,
                    details={
                        "broker_observed_cumulative_quantity": observed,
                        "effective_cumulative_quantity": effective,
                        "lifecycle_evidence": evidence.to_payload(),
                    },
                )
                return self._state_snapshot(reservation, opportunity)

            planned_quantity = int(reservation.planned_quantity_shares)
            terminal_source_states = {
                "flat_pending_settlement",
                "released",
                "closed",
            }
            quarantine_transition = bool(
                source_state in terminal_source_states
                or source_state == "exposure_quarantined"
                or effective > planned_quantity
            )
            opportunity_conflict: str | None = None
            if opportunity is not None:
                if opportunity.status == "reserved":
                    if opportunity.reservation_id != reservation.reservation_id:
                        if quarantine_transition:
                            opportunity_conflict = (
                                "reserved_by_another_reservation"
                            )
                        else:
                            raise AdaptiveReservationStateConflict(
                                "positive fill conflicts with another opportunity reservation"
                            )
                elif opportunity.status == "consumed":
                    if (
                        opportunity.consumed_by_reservation_id
                        != reservation.reservation_id
                    ):
                        if quarantine_transition:
                            opportunity_conflict = (
                                "consumed_by_another_reservation"
                            )
                        else:
                            raise AdaptiveReservationStateConflict(
                                "opportunity was consumed by another reservation"
                            )
                elif opportunity.status != "available":
                    if quarantine_transition:
                        opportunity_conflict = "unknown_opportunity_state"
                    else:
                        raise AdaptiveReservationStateConflict(
                            "unknown opportunity state"
                        )

            pending_quantity = max(0, planned_quantity - effective)
            fill_delta = effective - previous

            def split(planned_value: Decimal) -> tuple[Decimal, Decimal]:
                per_share = Decimal(planned_value) / Decimal(planned_quantity)
                pending_value = (per_share * Decimal(pending_quantity)).quantize(
                    _MONEY_QUANTUM, rounding=ROUND_HALF_EVEN
                )
                open_value = (
                    Decimal(planned_value)
                    / Decimal(planned_quantity)
                    * Decimal(fill_delta)
                    + Decimal("0")
                ).quantize(
                    _MONEY_QUANTUM, rounding=ROUND_HALF_EVEN
                )
                return pending_value, open_value

            pending_risk, open_risk = split(
                Decimal(reservation.planned_structural_risk_usd)
            )
            pending_gross, open_gross = split(
                Decimal(reservation.planned_gross_notional_usd)
            )
            pending_bp, open_bp = split(
                Decimal(reservation.planned_buying_power_impact_usd)
            )
            first_positive = previous == 0 and effective > 0
            self._bind_lifecycle_evidence(reservation, evidence)
            reservation.cumulative_filled_quantity_shares = effective
            reservation.open_quantity_shares = (
                int(reservation.open_quantity_shares) + fill_delta
            )
            reservation.pending_structural_risk_usd = pending_risk
            reservation.pending_gross_notional_usd = pending_gross
            reservation.pending_buying_power_impact_usd = pending_bp
            reservation.open_structural_risk_usd = (
                Decimal(reservation.open_structural_risk_usd) + open_risk
            )
            reservation.open_gross_notional_usd = (
                Decimal(reservation.open_gross_notional_usd) + open_gross
            )
            reservation.open_buying_power_impact_usd = (
                Decimal(reservation.open_buying_power_impact_usd) + open_bp
            )
            if quarantine_transition:
                reservation.state = "exposure_quarantined"
                if source_state != "exposure_quarantined":
                    reservation.lifecycle_contradiction_source_state = source_state
                    reservation.lifecycle_contradiction_at = now
                    reservation.lifecycle_contradiction_evidence_sha256 = (
                        evidence.event_content_sha256
                    )
            else:
                reservation.state = (
                    "partially_filled"
                    if effective < planned_quantity
                    else "filled"
                )
            if first_positive:
                reservation.first_fill_at = now
            reservation.updated_at = now
            reservation.version = int(reservation.version) + 1

            opportunity_consumed_now = bool(
                first_positive
                and opportunity is not None
                and opportunity_conflict is None
                and (
                    opportunity.status == "available"
                    or (
                        opportunity.status == "reserved"
                        and opportunity.reservation_id
                        == reservation.reservation_id
                    )
                )
            )
            if opportunity_consumed_now and opportunity is not None:
                opportunity.status = "consumed"
                opportunity.reservation_id = None
                opportunity.consumed_by_reservation_id = reservation.reservation_id
                opportunity.consumed_at = now
                opportunity.updated_at = now
                opportunity.version = int(opportunity.version) + 1
            if quarantine_transition:
                if source_state == "exposure_quarantined":
                    event_type = "quarantined_cumulative_fill_advanced"
                elif source_state in terminal_source_states:
                    event_type = "late_cumulative_fill_quarantined"
                else:
                    event_type = "cumulative_overfill_quarantined"
            else:
                event_type = "cumulative_fill_advanced"
            self._append_reservation_event(
                session,
                reservation,
                event_type=event_type,
                effective_at=now,
                broker_event_id=evidence.provider_event_id,
                details={
                    "broker_observed_cumulative_quantity": observed,
                    "previous_cumulative_quantity": previous,
                    "effective_cumulative_quantity": effective,
                    "source_state": source_state,
                    "result_state": reservation.state,
                    "exposure_quarantined": quarantine_transition,
                    "contradiction_evidence_sha256": (
                        reservation.lifecycle_contradiction_evidence_sha256
                    ),
                    "opportunity_consumed_now": opportunity_consumed_now,
                    "opportunity_conflict": opportunity_conflict,
                    "opportunity_status": (
                        opportunity.status if opportunity else "not_applicable"
                    ),
                    "overfill_quantity": max(0, effective - planned_quantity),
                    "lifecycle_evidence": evidence.to_payload(),
                },
            )
            if opportunity_consumed_now and opportunity is not None:
                self._append_opportunity_event(
                    session,
                    opportunity,
                    reservation_id=reservation.reservation_id,
                    event_type="opportunity_consumed_by_fill",
                    effective_at=now,
                    details={
                        "confirmed_cumulative_fill": effective,
                        "broker_event_id": evidence.provider_event_id,
                        "lifecycle_evidence_sha256": evidence.evidence_sha256,
                    },
                )
            return self._state_snapshot(reservation, opportunity)

    def apply_post_settlement_fill_contradiction(
        self,
        reservation_id: uuid.UUID,
        *,
        contradiction_sha256: str,
        session: Session | None = None,
    ) -> AdaptiveReservationState:
        """Apply only a committed terminal contradiction from the sealed ledger."""

        source_sha = str(contradiction_sha256 or "").strip().lower()
        if not _sha(source_sha):
            raise AdaptiveRiskContractError(
                "contradiction_sha256 must be SHA-256"
            )
        with self._transaction(session) as session:
            row = session.scalar(
                select(AlpacaPaperPostSettlementFillContradiction)
                .where(
                    AlpacaPaperPostSettlementFillContradiction.contradiction_sha256
                    == source_sha
                )
                .with_for_update()
            )
            if row is None or row.reservation_id != reservation_id:
                raise AdaptiveReservationStateConflict(
                    "post-settlement fill contradiction is missing"
                )
            try:
                verify_alpaca_paper_post_settlement_fill_contradiction_row(row)
            except AlpacaFillActivityError as exc:
                raise AdaptiveReservationStateConflict(
                    "post-settlement fill contradiction failed verification"
                ) from exc
            if not row.is_projection_terminal:
                raise AdaptiveReservationStateConflict(
                    "post-settlement fill projection requires a terminal row"
                )
            cumulative_decimal = _exact_nonnegative_decimal(
                row.broker_observed_cumulative_quantity,
                "post_settlement_fill.broker_observed_cumulative_quantity",
            )
            if cumulative_decimal != cumulative_decimal.to_integral_value():
                raise AdaptiveReservationStateConflict(
                    "post-settlement fill cumulative quantity is fractional"
                )
            reservation = self._reservation_for_transition(
                session,
                reservation_id,
            )
            cumulative = int(cumulative_decimal)
            evidence = DurableOrderLifecycleEvidence(
                event_kind="cumulative_fill",
                durability_kind=(
                    POST_SETTLEMENT_FILL_CONTRADICTION_DURABILITY_KIND
                ),
                provider_event_id=(
                    f"alpaca-post-settlement-fill:{row.contradiction_sha256}"
                ),
                broker_source="alpaca",
                connection_generation=row.broker_connection_generation,
                account_scope=row.account_scope,
                execution_family=row.execution_family,
                broker_environment=row.broker_environment,
                account_identity_sha256=row.account_identity_sha256,
                client_order_id=row.expected_client_order_id,
                broker_order_id=row.broker_order_id,
                observed_at=row.provider_transaction_at,
                available_at=row.provider_available_at,
                event_content_sha256=row.contradiction_sha256,
                cumulative_filled_quantity=cumulative,
                source_record_table=_ALPACA_POST_SETTLEMENT_FILL_TABLE,
                source_record_id=row.contradiction_sha256,
                order_status=(
                    "filled"
                    if cumulative >= int(reservation.planned_quantity_shares)
                    else "partially_filled"
                ),
            )
            return self.apply_cumulative_fill(
                reservation_id,
                evidence=evidence,
                session=session,
            )

    def lock_reservation(
        self,
        reservation_id: uuid.UUID,
        *,
        session: Session,
    ) -> AdaptiveReservationState:
        """Fence one caller-owned transaction before canonical fill creation."""

        with self._transaction(session) as session:
            reservation = self._reservation_for_transition(session, reservation_id)
            opportunity = self._opportunity_for_transition(session, reservation)
            return self._state_snapshot(reservation, opportunity)

    def reduce_open_exposure(
        self,
        reservation_id: uuid.UUID,
        *,
        evidence: DurableOrderLifecycleEvidence,
        reason: str = "partial_exit_confirmed",
        session: Session | None = None,
    ) -> AdaptiveReservationState:
        """Atomically reduce open risk dimensions without closing the position."""

        reason = _norm(reason, "reason")
        with self._transaction(session) as session:
            reservation = self._reservation_for_transition(session, reservation_id)
            opportunity = self._opportunity_for_transition(session, reservation)
            existing = self._existing_broker_event(
                session, reservation_id, evidence.provider_event_id
            )
            if existing is not None:
                self._verify_existing_lifecycle_event(
                    existing,
                    evidence,
                    expected_event_type="open_exposure_reduced",
                )
                return self._state_snapshot(reservation, opportunity)
            self._validate_lifecycle_evidence(
                session,
                reservation,
                evidence,
                expected_event_kind="position_reduced",
            )
            if evidence.cumulative_filled_quantity != int(
                reservation.cumulative_filled_quantity_shares
            ):
                raise AdaptiveReservationStateConflict(
                    "position-reduced evidence cumulative fill mismatch"
                )
            current_open = int(reservation.open_quantity_shares)
            remaining = int(evidence.remaining_open_quantity)
            if current_open <= 0 or remaining <= 0 or remaining >= current_open:
                raise AdaptiveReservationStateConflict(
                    "position-reduced evidence does not reduce current open quantity"
                )
            if reservation.state not in {"filled", "exposure_quarantined"}:
                raise AdaptiveReservationStateConflict(
                    "cannot reduce exposure before entry lifecycle is filled"
                )
            pending_dimensions = (
                Decimal(reservation.pending_structural_risk_usd),
                Decimal(reservation.pending_gross_notional_usd),
                Decimal(reservation.pending_buying_power_impact_usd),
            )
            if any(value != 0 for value in pending_dimensions):
                raise AdaptiveReservationStateConflict(
                    "cannot reduce while entry dimensions remain pending"
                )
            planned_quantity = int(reservation.planned_quantity_shares)

            def remaining_dimension(planned_value: Decimal) -> Decimal:
                return (
                    Decimal(planned_value)
                    / Decimal(planned_quantity)
                    * Decimal(remaining)
                ).quantize(_MONEY_QUANTUM, rounding=ROUND_HALF_EVEN)

            now = self._clock(session)
            self._bind_lifecycle_evidence(reservation, evidence)
            reservation.open_quantity_shares = remaining
            reservation.open_structural_risk_usd = remaining_dimension(
                Decimal(reservation.planned_structural_risk_usd)
            )
            reservation.open_gross_notional_usd = remaining_dimension(
                Decimal(reservation.planned_gross_notional_usd)
            )
            reservation.open_buying_power_impact_usd = remaining_dimension(
                Decimal(reservation.planned_buying_power_impact_usd)
            )
            reservation.updated_at = now
            reservation.version = int(reservation.version) + 1
            self._append_reservation_event(
                session,
                reservation,
                event_type="open_exposure_reduced",
                effective_at=now,
                broker_event_id=evidence.provider_event_id,
                details={
                    "reason": reason,
                    "remaining_open_quantity": remaining,
                    "opportunity_remains_consumed": opportunity is not None,
                    "opportunity_status": (
                        opportunity.status if opportunity else "not_applicable"
                    ),
                    "lifecycle_evidence": evidence.to_payload(),
                },
            )
            return self._state_snapshot(reservation, opportunity)

    def close_open_exposure(
        self,
        reservation_id: uuid.UUID,
        *,
        evidence: DurableOrderLifecycleEvidence,
        reason: str = "position_flat_confirmed",
        session: Session | None = None,
    ) -> AdaptiveReservationState:
        """Remove filled/open dimensions after authoritative flat-position proof.

        Alpaca PAPER becomes ``flat_pending_settlement`` here.  It may advance
        to ``closed`` only in the separate atomic transaction that appends the
        authoritative cycle settlement and advances the account settlement
        head.  Non-Alpaca execution families retain the direct close behavior.
        """

        reason = _norm(reason, "reason")
        with self._transaction(session) as session:
            reservation = self._reservation_for_transition(session, reservation_id)
            opportunity = self._opportunity_for_transition(session, reservation)
            existing = self._existing_broker_event(
                session, reservation_id, evidence.provider_event_id
            )
            alpaca_paper = reservation.account_scope == "alpaca:paper"
            quarantined = reservation.state == "exposure_quarantined"
            terminal_event_type = (
                "quarantined_exposure_flat_observed"
                if quarantined
                else (
                    "open_exposure_flat_pending_settlement"
                    if alpaca_paper
                    else "open_exposure_closed"
                )
            )
            if existing is not None:
                self._verify_existing_lifecycle_event(
                    existing,
                    evidence,
                    expected_event_type=terminal_event_type,
                )
                return self._state_snapshot(reservation, opportunity)
            self._validate_lifecycle_evidence(
                session,
                reservation,
                evidence,
                expected_event_kind="position_flat",
            )
            if (
                evidence.cumulative_filled_quantity
                != int(reservation.cumulative_filled_quantity_shares)
            ):
                raise AdaptiveReservationStateConflict(
                    "flat-position evidence cumulative fill mismatch"
                )
            if int(reservation.cumulative_filled_quantity_shares) <= 0:
                raise AdaptiveReservationStateConflict(
                    "zero-fill reservation has no open exposure to close"
                )
            if int(reservation.open_quantity_shares) <= 0:
                raise AdaptiveReservationStateConflict(
                    "reservation has no remaining open quantity to close"
                )
            if reservation.state not in {"filled", "exposure_quarantined"}:
                raise AdaptiveReservationStateConflict(
                    "cannot close exposure before entry lifecycle is filled"
                )
            pending_dimensions = (
                Decimal(reservation.pending_structural_risk_usd),
                Decimal(reservation.pending_gross_notional_usd),
                Decimal(reservation.pending_buying_power_impact_usd),
            )
            if any(value != 0 for value in pending_dimensions):
                raise AdaptiveReservationStateConflict(
                    "cannot close while entry dimensions remain pending"
                )
            now = self._clock(session)
            self._bind_lifecycle_evidence(reservation, evidence)
            if not quarantined:
                reservation.state = (
                    "flat_pending_settlement" if alpaca_paper else "closed"
                )
            reservation.open_quantity_shares = 0
            reservation.open_structural_risk_usd = Decimal("0")
            reservation.open_gross_notional_usd = Decimal("0")
            reservation.open_buying_power_impact_usd = Decimal("0")
            if not quarantined:
                reservation.closed_at = None if alpaca_paper else now
            reservation.updated_at = now
            reservation.version = int(reservation.version) + 1
            self._append_reservation_event(
                session,
                reservation,
                event_type=terminal_event_type,
                effective_at=now,
                broker_event_id=evidence.provider_event_id,
                details={
                    "reason": reason,
                    "cycle_settlement_required": alpaca_paper and not quarantined,
                    "quarantine_reconciliation_required": quarantined,
                    "opportunity_remains_consumed": opportunity is not None,
                    "opportunity_status": (
                        opportunity.status if opportunity else "not_applicable"
                    ),
                    "lifecycle_evidence": evidence.to_payload(),
                },
            )
            return self._state_snapshot(reservation, opportunity)

    def finalize_filled_entry_remainder(
        self,
        reservation_id: uuid.UUID,
        *,
        evidence: DurableOrderLifecycleEvidence,
        reason: str = "broker_canceled_unfilled_remainder",
        session: Session | None = None,
    ) -> AdaptiveReservationState:
        """Release only the unfilled dimensions after a terminal partial fill.

        The already-filled/open dimensions remain authoritative.  A first-dip
        ET-day opportunity remains consumed; other setups have no opportunity
        lifecycle.  This is intentionally different from ``release_zero_fill``.
        """

        reason = _norm(reason, "reason")
        with self._transaction(session) as session:
            reservation = self._reservation_for_transition(session, reservation_id)
            opportunity = self._opportunity_for_transition(session, reservation)
            quarantined = reservation.state == "exposure_quarantined"
            terminal_event_type = (
                "quarantined_entry_remainder_released"
                if quarantined
                else "filled_entry_remainder_released"
            )
            existing = self._existing_broker_event(
                session, reservation_id, evidence.provider_event_id
            )
            if existing is not None:
                self._verify_existing_lifecycle_event(
                    existing,
                    evidence,
                    expected_event_type=terminal_event_type,
                )
                return self._state_snapshot(reservation, opportunity)
            self._validate_lifecycle_evidence(
                session,
                reservation,
                evidence,
                expected_event_kind="filled_entry_terminal",
            )
            if int(reservation.cumulative_filled_quantity_shares) <= 0:
                raise AdaptiveReservationStateConflict(
                    "zero-fill terminal outcome must use release_zero_fill"
                )
            if (
                evidence.cumulative_filled_quantity
                != int(reservation.cumulative_filled_quantity_shares)
            ):
                raise AdaptiveReservationStateConflict(
                    "entry-terminal evidence cumulative fill mismatch"
                )
            if opportunity is not None and (
                opportunity.status != "consumed"
                or opportunity.consumed_by_reservation_id
                != reservation.reservation_id
            ):
                raise AdaptiveReservationStateConflict(
                    "partially filled reservation must own consumed opportunity"
                )
            if reservation.state == "closed":
                raise AdaptiveReservationStateConflict(
                    "closed exposure cannot finalize an entry remainder"
                )
            now = self._clock(session)
            self._bind_lifecycle_evidence(reservation, evidence)
            reservation.pending_structural_risk_usd = Decimal("0")
            reservation.pending_gross_notional_usd = Decimal("0")
            reservation.pending_buying_power_impact_usd = Decimal("0")
            if not quarantined:
                reservation.state = "filled"
            reservation.updated_at = now
            reservation.version = int(reservation.version) + 1
            self._append_reservation_event(
                session,
                reservation,
                event_type=terminal_event_type,
                effective_at=now,
                broker_event_id=evidence.provider_event_id,
                details={
                    "reason": reason,
                    "confirmed_cumulative_fill": int(
                        reservation.cumulative_filled_quantity_shares
                    ),
                    "quarantine_reconciliation_required": quarantined,
                    "opportunity_remains_consumed": opportunity is not None,
                    "opportunity_status": (
                        opportunity.status if opportunity else "not_applicable"
                    ),
                    "lifecycle_evidence": evidence.to_payload(),
                },
            )
            return self._state_snapshot(reservation, opportunity)
