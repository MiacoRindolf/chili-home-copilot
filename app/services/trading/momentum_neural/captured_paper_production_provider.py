"""Service-owned capture providers for the dedicated Alpaca PAPER runtime.

The providers in this module own the only legitimate transition from a raw
paper adapter plus already-durable market/governance reads to the immutable
material consumed by :mod:`captured_paper_production_material`.  They never
open a database transaction, submit/cancel an order, or reconstruct a capture
receipt.  The original account/BBO ``CapturedReadResult`` objects are retained
from ``CapturedAlpacaPaperAdapter`` and every supplemental input remains bound
to the same live capture coordinator and decision id.
"""

from __future__ import annotations

from contextlib import AbstractContextManager, ExitStack, contextmanager
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
import math
import re
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping, Protocol, Sequence

from .captured_adaptive_risk_source import (
    CapturedAdaptiveRiskDecisionIdentity,
    CapturedAdaptiveRiskEconomicInputs,
    CapturedAdaptiveRiskEvidenceSet,
    CapturedAdaptiveRiskFactProvenance,
    CapturedAdaptiveRiskPolicySpec,
    captured_adaptive_risk_fact_payloads,
)
from .captured_alpaca_paper_adapter import CapturedAlpacaPaperAdapter
from .captured_paper_admission import (
    CapturedFirstDipDetectorAudit,
    CapturedPaperOperationalPolicy,
)
from .captured_paper_dispatcher import CapturedPaperDispatchRequest
from .captured_paper_production_material import (
    CapturedPaperBoundInputScope,
    CapturedPaperDurableCandidateSnapshot,
    CapturedPaperDurableObservationSnapshot,
    CapturedPaperObservationCapture,
    CapturedPaperProductionCapture,
    CapturedPaperProductionMaterialFactory,
    CapturedPaperProductionMaterialUnavailable,
)
from .first_dip_tape_policy import (
    FirstDipTapePolicy,
    FirstDipTapePolicyError,
    FirstDipTapeReadQuery,
)
from .live_replay_capture import (
    CapturedReadResult,
    FirstDipFinalCaptureRead,
    FirstDipFinalReadProvider,
    LiveMicrostructureCaptureBridge,
    LiveOhlcvCaptureBridge,
    LiveReplayCaptureCoordinator,
    LiveScannerSnapshotCaptureBridge,
    ObservedCaptureInput,
)
from .replay_capture_contract import (
    CaptureClocks,
    CaptureContractError,
    CaptureIqfeedPrint,
    CaptureMicrostructureOperation,
    CaptureMicrostructureReadQuery,
    CaptureScannerSnapshot,
    CaptureStream,
    CoverageMode,
    FSMDependencyProfile,
    FSMStreamDependency,
    PROVIDER_OHLCV_PAYLOAD_SCHEMA_VERSION,
    PROVIDER_OHLCV_QUERY_SCHEMA_VERSION,
    STREAM_POLICIES,
    resolve_capture_source_payload,
    sha256_json,
)
from .replay_errors import (
    ReplayMicrostructureInputUnavailableError,
    ReplayOhlcvInputUnavailableError,
    ReplayScannerSnapshotUnavailableError,
)
from ..venue.alpaca_spot import quantize_alpaca_equity_limit_price
from ..venue.protocol import FreshnessMeta, NormalizedProduct


UTC = timezone.utc
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FACT_NAMES = (
    "structural_stop",
    "setup_quality",
    "volatility",
    "liquidity",
    "correlation",
    "candidate_buying_power_estimate",
)
_ADAPTER_OWNED_STREAMS = frozenset(
    {
        CaptureStream.ACCOUNT_RISK_SNAPSHOT,
        CaptureStream.ALPACA_NBBO_QUOTE,
    }
)
_CURRENT_SYMBOL_STATE_STREAMS = (
    CaptureStream.SCANNER_SNAPSHOT,
    CaptureStream.PROVIDER_OHLCV,
)
_CURRENT_GLOBAL_IDENTITY_STREAMS = (
    CaptureStream.CONFIG_SNAPSHOT,
    CaptureStream.FEATURE_FLAG_SNAPSHOT,
    CaptureStream.CODE_BUILD,
)
_ADMISSION_ELIGIBILITY_SCHEMA_VERSION = (
    "chili.captured-paper-admission-eligibility.v1"
)
_CANDIDATE_FACT_READ_KEYS = MappingProxyType(
    {
        "structural_stop": ("candidate_snapshot",),
        "setup_quality": (
            "candidate_snapshot",
            CaptureStream.SCANNER_SNAPSHOT.value,
            CaptureStream.PROVIDER_OHLCV.value,
        ),
        "volatility": (CaptureStream.PROVIDER_OHLCV.value,),
        "liquidity": (
            CaptureStream.SCANNER_SNAPSHOT.value,
            CaptureStream.PROVIDER_OHLCV.value,
            CaptureStream.CONFIG_SNAPSHOT.value,
            "admission_eligibility",
        ),
        "correlation": (
            "candidate_snapshot",
            CaptureStream.CONFIG_SNAPSHOT.value,
        ),
        "candidate_buying_power_estimate": (
            "candidate_snapshot",
            CaptureStream.SCANNER_SNAPSHOT.value,
        ),
    }
)


def _unavailable(reason: str) -> None:
    raise CapturedPaperProductionMaterialUnavailable(reason)


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        _unavailable(f"{field_name}_clock_unavailable")
    return value.astimezone(UTC)


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value.strip():
        _unavailable(f"{field_name}_clock_unavailable")
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        _unavailable(f"{field_name}_clock_unavailable")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _positive(value: Any, field_name: str) -> float:
    if isinstance(value, bool):
        _unavailable(f"{field_name}_unavailable")
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        _unavailable(f"{field_name}_unavailable")
    if not math.isfinite(result) or result <= 0.0:
        _unavailable(f"{field_name}_unavailable")
    return result


def _sha(value: Any, field_name: str) -> str:
    digest = str(value or "").strip().lower()
    if _SHA256_RE.fullmatch(digest) is None:
        _unavailable(f"{field_name}_sha256_unavailable")
    return digest


def _typed_reads(
    rows: Sequence[CapturedReadResult],
    profile: FSMDependencyProfile,
    *,
    decision_id: str,
    forbidden_streams: frozenset[CaptureStream],
) -> tuple[CapturedReadResult, ...]:
    reads = tuple(rows)
    if not reads or any(
        type(row) is not CapturedReadResult
        or not row.durable
        or row.receipt is None
        or row.receipt.decision_id != decision_id
        for row in reads
    ):
        _unavailable("production_supplemental_reads_unavailable")
    read_ids = tuple(sorted(row.receipt.read_id for row in reads if row.receipt))
    streams = frozenset(row.receipt.stream for row in reads if row.receipt)
    if (
        len(read_ids) != len(set(read_ids))
        or type(profile) is not FSMDependencyProfile
        or profile.required_read_ids != read_ids
        or streams != profile.required_streams
        or streams.intersection(forbidden_streams)
    ):
        _unavailable("production_supplemental_profile_mismatch")
    return reads


@dataclass(frozen=True, slots=True)
class CapturedPaperFactSource:
    """Exact source inventory used to derive one adaptive-risk fact."""

    source: str
    observed_at: datetime
    available_at: datetime
    provider_generation: str
    source_read_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        source = str(self.source or "").strip()
        generation = str(self.provider_generation or "").strip()
        read_ids = tuple(sorted(str(value or "").strip() for value in self.source_read_ids))
        if (
            not source
            or not generation
            or not read_ids
            or len(read_ids) != len(set(read_ids))
            or any(not value for value in read_ids)
        ):
            _unavailable("production_fact_source_unavailable")
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "provider_generation", generation)
        object.__setattr__(self, "source_read_ids", read_ids)
        object.__setattr__(self, "observed_at", _utc(self.observed_at, "fact_observed"))
        object.__setattr__(self, "available_at", _utc(self.available_at, "fact_available"))


@dataclass(frozen=True, slots=True)
class CapturedPaperObservedRead:
    """One already-returned service input waiting for durable receipting.

    This record deliberately carries no callback.  Network/provider work, if
    any, must finish in the dedicated service before this value is returned.
    The capture provider below only envelopes these exact bytes and clocks.
    """

    key: str
    stream: CaptureStream
    provider: str
    query: Mapping[str, Any]
    requested_at: datetime
    returned_at: datetime
    results: tuple[ObservedCaptureInput, ...]
    max_source_age_seconds: float
    symbol_scoped: bool = True
    finalize_local_continuity: bool = False

    def __post_init__(self) -> None:
        key = str(self.key or "").strip()
        provider = str(self.provider or "").strip().lower()
        if not key or not provider or type(self.stream) is not CaptureStream:
            _unavailable("observed_service_read_identity_unavailable")
        policy = STREAM_POLICIES[self.stream]
        if policy.coverage_mode in {CoverageMode.CONTINUOUS, CoverageMode.CONTROL}:
            _unavailable("observed_service_read_mode_unsupported")
        query = dict(self.query) if isinstance(self.query, Mapping) else {}
        if not query:
            _unavailable("observed_service_read_query_unavailable")
        requested = _utc(self.requested_at, "observed_service_read_requested")
        returned = _utc(self.returned_at, "observed_service_read_returned")
        if returned < requested:
            _unavailable("observed_service_read_clock_reversed")
        results = tuple(self.results)
        if not results or any(type(row) is not ObservedCaptureInput for row in results):
            _unavailable("observed_service_read_result_unavailable")
        if policy.coverage_mode is not CoverageMode.QUERY_RECEIPT and len(results) != 1:
            _unavailable("observed_service_scalar_read_ambiguous")
        if any(row.clocks.available_at > returned for row in results):
            _unavailable("observed_service_read_availability_after_return")
        if type(self.symbol_scoped) is not bool:
            _unavailable("observed_service_read_symbol_scope_unavailable")
        if type(self.finalize_local_continuity) is not bool:
            _unavailable("observed_service_read_continuity_flag_unavailable")
        if self.finalize_local_continuity and policy.coverage_mode is not CoverageMode.CHANGE_LOG:
            _unavailable("observed_service_read_continuity_mode_unavailable")
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "provider", provider)
        object.__setattr__(self, "query", query)
        object.__setattr__(self, "requested_at", requested)
        object.__setattr__(self, "returned_at", returned)
        object.__setattr__(self, "results", results)
        object.__setattr__(
            self,
            "max_source_age_seconds",
            _positive(self.max_source_age_seconds, "observed_service_read_max_age"),
        )


@dataclass(frozen=True, slots=True)
class CapturedPaperCandidateObservedMaterial:
    """Typed provider outputs used by the concrete candidate supplement."""

    reads: tuple[CapturedPaperObservedRead, ...]
    economics: CapturedAdaptiveRiskEconomicInputs
    fact_read_keys: Mapping[str, tuple[str, ...]]
    correlation_cluster: str
    setup_read_key: str
    existing_reads: Mapping[str, CapturedReadResult] = None  # type: ignore[assignment]
    existing_read_max_age_seconds: Mapping[str, float] = None  # type: ignore[assignment]
    buying_power_double_census: Any | None = None
    first_dip_tape_key: str | None = None
    first_dip_detector_audit: CapturedFirstDipDetectorAudit | None = None
    first_dip_final_read_provider: FirstDipFinalReadProvider | None = None

    def __post_init__(self) -> None:
        reads = tuple(self.reads)
        keys = tuple(row.key for row in reads)
        existing = dict(self.existing_reads or {})
        existing_ages = dict(self.existing_read_max_age_seconds or {})
        all_keys = set(keys).union(existing)
        if (
            not reads
            or len(keys) != len(set(keys))
            or set(existing) != set(existing_ages)
            or set(keys).intersection(existing)
            or any(type(row) is not CapturedReadResult for row in existing.values())
            or any(_positive(age, "existing_read_max_age") <= 0 for age in existing_ages.values())
        ):
            _unavailable("candidate_observed_read_inventory_unavailable")
        if type(self.economics) is not CapturedAdaptiveRiskEconomicInputs:
            _unavailable("candidate_observed_economics_unavailable")
        fact_keys = {name: tuple(values) for name, values in dict(self.fact_read_keys).items()}
        if set(fact_keys) != set(_FACT_NAMES) or any(
            not values or not set(values).issubset(all_keys)
            for values in fact_keys.values()
        ):
            _unavailable("candidate_observed_fact_lineage_unavailable")
        cluster = str(self.correlation_cluster or "").strip().lower()
        setup_key = str(self.setup_read_key or "").strip()
        first_key = str(self.first_dip_tape_key or "").strip() or None
        if not cluster or setup_key not in all_keys:
            _unavailable("candidate_observed_setup_identity_unavailable")
        trio = (first_key, self.first_dip_detector_audit, self.first_dip_final_read_provider)
        if any(value is None for value in trio) and any(value is not None for value in trio):
            _unavailable("candidate_observed_first_dip_unavailable")
        if first_key is not None and first_key not in existing:
            _unavailable("candidate_first_dip_must_be_existing_exact_read")
        object.__setattr__(self, "reads", reads)
        object.__setattr__(self, "existing_reads", existing)
        object.__setattr__(self, "existing_read_max_age_seconds", existing_ages)
        object.__setattr__(self, "fact_read_keys", fact_keys)
        object.__setattr__(self, "correlation_cluster", cluster)
        object.__setattr__(self, "setup_read_key", setup_key)
        object.__setattr__(self, "first_dip_tape_key", first_key)


@dataclass(frozen=True, slots=True)
class CapturedPaperObservationObservedMaterial:
    """Typed provider outputs used by the concrete observation supplement."""

    reads: tuple[CapturedPaperObservedRead, ...]
    observation_snapshot_key: str
    admission_eligibility_key: str
    existing_reads: Mapping[str, CapturedReadResult] = None  # type: ignore[assignment]
    existing_read_max_age_seconds: Mapping[str, float] = None  # type: ignore[assignment]
    first_dip_tape_key: str | None = None
    first_dip_detector_policy: FirstDipTapePolicy | None = None

    def __post_init__(self) -> None:
        reads = tuple(self.reads)
        keys = tuple(row.key for row in reads)
        existing = dict(self.existing_reads or {})
        existing_ages = dict(self.existing_read_max_age_seconds or {})
        all_keys = set(keys).union(existing)
        snapshot = str(self.observation_snapshot_key or "").strip()
        eligibility = str(self.admission_eligibility_key or "").strip()
        first_key = str(self.first_dip_tape_key or "").strip() or None
        if (
            not reads
            or len(keys) != len(set(keys))
            or set(keys).intersection(existing)
            or set(existing) != set(existing_ages)
            or snapshot not in all_keys
            or eligibility not in all_keys
            or snapshot == eligibility
            or any(type(row) is not CapturedReadResult for row in existing.values())
        ):
            _unavailable("observation_observed_read_inventory_unavailable")
        for age in existing_ages.values():
            _positive(age, "observation_existing_read_max_age")
        if (first_key is None) != (self.first_dip_detector_policy is None):
            _unavailable("observation_observed_first_dip_unavailable")
        if first_key is not None and first_key not in existing:
            _unavailable("observation_first_dip_must_be_existing_exact_read")
        object.__setattr__(self, "reads", reads)
        object.__setattr__(self, "existing_reads", existing)
        object.__setattr__(self, "existing_read_max_age_seconds", existing_ages)
        object.__setattr__(self, "observation_snapshot_key", snapshot)
        object.__setattr__(self, "admission_eligibility_key", eligibility)
        object.__setattr__(self, "first_dip_tape_key", first_key)


class CapturedPaperCandidateObservedInputProvider(Protocol):
    def __call__(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
        coordinator: LiveReplayCaptureCoordinator,
        adapter: CapturedAlpacaPaperAdapter,
        account_read: CapturedReadResult,
        bbo_read: CapturedReadResult,
    ) -> CapturedPaperCandidateObservedMaterial: ...


class CapturedPaperObservationObservedInputProvider(Protocol):
    def __call__(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        observation: CapturedPaperDurableObservationSnapshot,
        coordinator: LiveReplayCaptureCoordinator,
        adapter: CapturedAlpacaPaperAdapter,
        account_read: CapturedReadResult,
        bbo_read: CapturedReadResult,
    ) -> CapturedPaperObservationObservedMaterial: ...


class _CapturedPaperCoordinatorObservedInputs:
    """Concrete no-callback source for the production PAPER builder.

    Provider reads are limited to the already-bound Alpaca PAPER adapter.
    Every other input is either a deterministic clock/calendar fact captured
    here or an exact already-durable coordinator inventory row.  There is no
    database, network fallback, or caller-supplied fact callback.
    """

    def __init__(
        self,
        *,
        wall_clock: Callable[[], datetime],
        context_max_age_seconds: float,
        first_dip_detector_policy: FirstDipTapePolicy,
    ) -> None:
        if not callable(wall_clock):
            raise TypeError("captured PAPER wall clock must be callable")
        if type(first_dip_detector_policy) is not FirstDipTapePolicy:
            raise TypeError("captured PAPER first-dip policy must be typed")
        self._wall_clock = wall_clock
        self._context_max_age_seconds = _positive(
            context_max_age_seconds,
            "production_context_max_age",
        )
        self._first_dip_detector_policy = first_dip_detector_policy

    def _now(self) -> datetime:
        return _utc(self._wall_clock(), "production_observed")

    @staticmethod
    def _source_age_bound(
        read: CapturedReadResult,
        *,
        now: datetime,
        minimum: float,
    ) -> float:
        if read.receipt is None or not read.source_events:
            _unavailable("production_current_state_read_unavailable")
        oldest = min(
            event.clocks.provider_event_at
            or event.clocks.market_reference_at
            or event.clocks.received_at
            for event in read.source_events
        )
        return max(float(minimum), (now - oldest).total_seconds() + float(minimum))

    def _current_read(
        self,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        decision_id: str,
        stream: CaptureStream,
        symbol: str | None,
        now: datetime,
        max_source_age_seconds: float | None = None,
    ) -> tuple[CapturedReadResult, float]:
        max_age = (
            self._context_max_age_seconds
            if max_source_age_seconds is None
            else _positive(
                max_source_age_seconds,
                f"production_{stream.value}_max_age",
            )
        )
        try:
            read = coordinator.capture_latest_durable_state_read(
                decision_id=decision_id,
                stream=stream,
                symbol=symbol,
                returned_at=now,
                max_source_age_seconds=max_age,
            )
        except CaptureContractError as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                f"production_{stream.value}_coverage_unavailable"
            ) from exc
        policy = STREAM_POLICIES[stream]
        if policy.coverage_mode is CoverageMode.IDENTITY:
            max_age = self._source_age_bound(
                read,
                now=now,
                minimum=self._context_max_age_seconds,
            )
        return read, max_age

    def _current_symbol_state_read(
        self,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        decision_id: str,
        stream: CaptureStream,
        symbol: str,
        now: datetime,
    ) -> tuple[CapturedReadResult, float]:
        if stream is not CaptureStream.PROVIDER_OHLCV:
            return self._current_read(
                coordinator=coordinator,
                decision_id=decision_id,
                stream=stream,
                symbol=symbol,
                now=now,
            )
        # A closed bar's source clock is naturally older than a quote clock.
        # First inventory at the largest supported bar cadence, then bind the
        # exact accepted age to that receipt's actual query interval plus the
        # configured capture/arrival grace.  This avoids both a fixed one-minute
        # throttle and an unbounded stale-frame exception.
        inventory_bound = 15.0 * 60.0 + self._context_max_age_seconds
        read, _inventory_age = self._current_read(
            coordinator=coordinator,
            decision_id=decision_id,
            stream=stream,
            symbol=symbol,
            now=now,
            max_source_age_seconds=inventory_bound,
        )
        if read.receipt is None or len(read.source_events) != 1:
            _unavailable("production_provider_ohlcv_coverage_unavailable")
        event = read.source_events[0]
        call = event.query.get("call") if isinstance(event.query, Mapping) else None
        interval = (
            str(call.get("interval") or "").strip().lower()
            if isinstance(call, Mapping)
            else ""
        )
        cadence_seconds = {"5m": 5.0 * 60.0, "15m": 15.0 * 60.0}.get(
            interval
        )
        market_at = event.clocks.market_reference_at
        if cadence_seconds is None or market_at is None:
            _unavailable("production_provider_ohlcv_query_unavailable")
        exact_bound = cadence_seconds + self._context_max_age_seconds
        age = (now - market_at).total_seconds()
        if age < 0.0 or age > exact_bound:
            _unavailable("production_provider_ohlcv_stale")
        return read, exact_bound

    @staticmethod
    def _observed_read(
        *,
        key: str,
        stream: CaptureStream,
        provider: str,
        payload: Mapping[str, Any],
        clocks: CaptureClocks,
        returned_at: datetime,
        requested_at: datetime | None = None,
        max_source_age_seconds: float,
        symbol_scoped: bool,
        finalize_local_continuity: bool = False,
    ) -> CapturedPaperObservedRead:
        return CapturedPaperObservedRead(
            key=key,
            stream=stream,
            provider=provider,
            query={
                "schema_version": "chili.captured-paper-owned-read.v1",
                "key": key,
                "stream": stream.value,
            },
            requested_at=(returned_at if requested_at is None else requested_at),
            returned_at=returned_at,
            results=(ObservedCaptureInput(payload=payload, clocks=clocks),),
            max_source_age_seconds=max_source_age_seconds,
            symbol_scoped=symbol_scoped,
            finalize_local_continuity=finalize_local_continuity,
        )

    def _market_session_read(
        self,
        *,
        symbol: str,
        now: datetime,
    ) -> CapturedPaperObservedRead:
        try:
            from . import market_profile  # noqa: PLC0415

            local = now.astimezone(market_profile._NY_TZ)
            local_minute = local.hour * 60 + local.minute
            # ``market_session_now`` optionally consults live NBBO-tape/DB state
            # before the configured premarket open.  That state is not captured
            # by this deterministic session producer, so do not let it leak in.
            if (
                market_profile._early_premarket_enabled()
                and market_profile._EXCHANGE_EXT_OPEN_MIN
                <= local_minute
                < min(
                    market_profile._premarket_start_min(),
                    market_profile._REGULAR_OPEN_MIN,
                )
            ):
                _unavailable(
                    "production_market_session_early_unlock_coverage_unavailable"
                )
            session = market_profile.market_session_now(symbol, now=now)
            detailed = market_profile.market_session_for_symbol(
                symbol,
                now=now,
                allow_extended_hours=True,
            )
            data_session = bool(market_profile.is_data_session_now(symbol, now=now))
        except CapturedPaperProductionMaterialUnavailable:
            raise
        except Exception as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "production_market_session_state_coverage_unavailable"
            ) from exc
        return self._observed_read(
            key="market_session",
            stream=CaptureStream.MARKET_SESSION_STATE,
            provider="chili_market_profile",
            payload={
                "schema_version": "chili.market-session-state.v1",
                "symbol": symbol,
                "decision_clock": now.isoformat(),
                "market_session": session,
                "market_session_for_symbol": dict(detailed),
                "is_data_session": data_session,
                # Do not call ``is_tradeable_now`` here.  Its optional overnight
                # branch consults a process-global eligibility cache and wall
                # clock, neither of which is this decision's captured authority.
                # Instrument eligibility is captured independently from the
                # bound Alpaca PAPER adapter below.
                "standard_clock_session_tradeable": session
                in {"premarket", "regular", "afterhours"},
                "overnight_tradeability_claim": None,
                "authority": "market_profile_exact_clock_and_config_only",
            },
            clocks=CaptureClocks(
                received_at=now,
                available_at=now,
                market_reference_at=now,
            ),
            returned_at=now,
            max_source_age_seconds=self._context_max_age_seconds,
            symbol_scoped=False,
            finalize_local_continuity=True,
        )

    def _eligibility_read(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        adapter: CapturedAlpacaPaperAdapter,
    ) -> CapturedPaperObservedRead:
        requested_at = self._now()
        try:
            product, freshness = adapter.capture_product_eligibility(
                request.symbol
            )
        except Exception as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "production_admission_eligibility_coverage_unavailable"
            ) from exc
        if type(product) is not NormalizedProduct or type(freshness) is not FreshnessMeta:
            _unavailable("production_admission_eligibility_coverage_unavailable")
        returned_at = self._now()
        if returned_at < requested_at:
            _unavailable("production_admission_eligibility_clock_unavailable")
        if (
            str(product.product_id or "").strip().upper() != request.symbol
            or str(product.base_currency or "").strip().upper()
            != request.symbol
            or str(product.quote_currency or "").strip().upper() != "USD"
            or str(product.product_type or "").strip().lower() != "equity"
            or not product.tradable_for_spot_momentum()
        ):
            _unavailable("production_admission_eligibility_rejected")
        retrieved = _utc(
            freshness.retrieved_at_utc,
            "production_product_retrieved",
        )
        provider_at = (
            None
            if freshness.provider_time_utc is None
            else _utc(freshness.provider_time_utc, "production_product_provider")
        )
        if retrieved > returned_at or (
            provider_at is not None and provider_at > returned_at
        ):
            _unavailable("production_admission_eligibility_clock_unavailable")
        max_age = min(
            self._context_max_age_seconds,
            _positive(freshness.max_age_seconds, "production_product_max_age"),
        )
        if (returned_at - (provider_at or retrieved)).total_seconds() > max_age:
            _unavailable("production_admission_eligibility_stale")
        return self._observed_read(
            key="admission_eligibility",
            stream=CaptureStream.ADMISSION_ELIGIBILITY,
            provider="alpaca_paper_asset",
            payload={
                "schema_version": _ADMISSION_ELIGIBILITY_SCHEMA_VERSION,
                "symbol": request.symbol,
                "execution_family": request.execution_family,
                "account_scope": request.account_scope,
                "expected_account_id": request.expected_account_id,
                "product": asdict(product),
                "max_age_seconds": max_age,
            },
            clocks=CaptureClocks(
                received_at=retrieved,
                available_at=returned_at,
                provider_event_at=provider_at,
                market_reference_at=retrieved,
            ),
            requested_at=requested_at,
            returned_at=returned_at,
            max_source_age_seconds=max_age,
            symbol_scoped=True,
        )

    def _typed_first_dip_read(
        self,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        decision_id: str,
        symbol: str,
        boundary: datetime,
        policy: FirstDipTapePolicy,
    ) -> CapturedReadResult:
        start = boundary - timedelta(seconds=policy.window_seconds)
        try:
            raw = coordinator.capture_complete_microstructure_window(
                decision_id=decision_id + ":inventory",
                operation=CaptureMicrostructureOperation.TRADE_FLOW,
                stream=CaptureStream.IQFEED_PRINT,
                provider="iqfeed",
                symbol=symbol,
                requested_at=boundary,
                returned_at=boundary,
                event_start_exclusive=start,
                event_end_inclusive=boundary,
                parameters={"window_seconds": policy.window_seconds},
            )
            if not raw.durable or raw.receipt is None:
                raise CaptureContractError(
                    "exact IQFeed print inventory is not durable"
                )
            query = CaptureMicrostructureReadQuery.from_dict(raw.receipt.query or {})
            tape_query = FirstDipTapeReadQuery(
                symbol=symbol,
                provider="iqfeed",
                event_start_exclusive=start,
                event_end_inclusive=boundary,
                decision_at=boundary,
                available_at_most=boundary,
                source_frontier_sequence=query.source_frontier_sequence,
                policy_sha256=policy.policy_sha256,
            )
            captured = coordinator.capture_durable_read(
                decision_id=decision_id,
                stream=CaptureStream.IQFEED_PRINT,
                provider="iqfeed",
                symbol=symbol,
                query=tape_query.to_dict(),
                requested_at=boundary,
                returned_at=boundary,
                source_events=raw.source_events,
                first_dip_tape=True,
            )
        except (CaptureContractError, FirstDipTapePolicyError) as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "production_iqfeed_exact_print_coverage_unavailable"
            ) from exc
        if not captured.durable or captured.receipt is None:
            _unavailable("production_iqfeed_exact_print_coverage_unavailable")
        return captured

    def _persisted_halt_state_read(
        self,
        *,
        symbol: str,
        snapshot_payload: Mapping[str, Any],
        now: datetime,
    ) -> CapturedPaperObservedRead:
        """Capture the exact halt inference the FSM is about to read.

        CHILI currently has no authoritative exchange-halt/LULD-status feed.
        The live FSM instead reads its persisted quote/print inference from the
        session risk snapshot.  Record that input honestly (including an
        explicit external-status ``not_inspected`` marker) rather than seeding
        a synthetic exchange ``not_halted`` event.  A later candidate consumes
        this observation-tick change receipt from the coordinator inventory.
        """

        risk_snapshot = snapshot_payload.get("risk_snapshot")
        if not isinstance(risk_snapshot, Mapping):
            _unavailable("production_persisted_halt_state_coverage_unavailable")
        live_exec = risk_snapshot.get("momentum_live_execution")
        live_exec = live_exec if isinstance(live_exec, Mapping) else {}
        updated_at = _parse_utc(
            snapshot_payload.get("session_updated_at"),
            "production_persisted_halt_state",
        )
        if updated_at > now:
            _unavailable("production_persisted_halt_state_clock_unavailable")
        fields = {
            key: live_exec[key]
            for key in (
                "suspected_halt_since_utc",
                "halt_resumed_at_utc",
                "halt_resumption_open",
                "halt_stale_streak",
                "halt_level",
                "print_recency_halt",
            )
            if key in live_exec
        }
        return self._observed_read(
            key=CaptureStream.HALT_LULD_STATE.value,
            stream=CaptureStream.HALT_LULD_STATE,
            provider="chili_fsm_persisted_halt_inference",
            payload={
                "schema_version": "chili.fsm-persisted-halt-luld-state.v1",
                "symbol": symbol,
                "risk_snapshot_sha256": _sha(
                    snapshot_payload.get("risk_snapshot_sha256"),
                    "production_persisted_halt_risk_snapshot",
                ),
                "fsm_suspected_halt": bool(
                    live_exec.get("suspected_halt_since_utc")
                ),
                "fsm_halt_fields": fields,
                "external_exchange_halt_status": "not_inspected",
                "authority": "exact_persisted_fsm_input_only",
            },
            clocks=CaptureClocks(
                received_at=updated_at,
                available_at=now,
                provider_event_at=updated_at,
                market_reference_at=updated_at,
            ),
            returned_at=now,
            max_source_age_seconds=self._context_max_age_seconds,
            symbol_scoped=True,
            finalize_local_continuity=True,
        )

    def _base_material(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        decision_id: str,
        snapshot_payload: Mapping[str, Any],
        snapshot_key: str,
        coordinator: LiveReplayCaptureCoordinator,
        adapter: CapturedAlpacaPaperAdapter,
        bbo_read: CapturedReadResult,
        include_dynamic_current_state: bool,
        include_first_dip: bool,
        optional_first_dip: bool = False,
    ) -> tuple[
        tuple[CapturedPaperObservedRead, ...],
        dict[str, CapturedReadResult],
        dict[str, float],
        str | None,
    ]:
        now = self._now()
        tape = None
        if include_first_dip:
            try:
                tape = self._typed_first_dip_read(
                    coordinator=coordinator,
                    decision_id=decision_id,
                    symbol=request.symbol,
                    boundary=now,
                    policy=self._first_dip_detector_policy,
                )
            except CapturedPaperProductionMaterialUnavailable as exc:
                if not (
                    optional_first_dip
                    and exc.reason
                    == "production_iqfeed_exact_print_coverage_unavailable"
                ):
                    raise
        specs = [
            self._observed_read(
                key=snapshot_key,
                stream=CaptureStream.FSM_DECISION,
                provider="chili_captured_paper",
                payload=snapshot_payload,
                clocks=CaptureClocks(
                    received_at=now,
                    available_at=now,
                    market_reference_at=now,
                ),
                returned_at=now,
                max_source_age_seconds=self._context_max_age_seconds,
                symbol_scoped=True,
            ),
            self._eligibility_read(
                request=request,
                adapter=adapter,
            ),
            self._market_session_read(symbol=request.symbol, now=now),
        ]
        existing: dict[str, CapturedReadResult] = {}
        ages: dict[str, float] = {}
        if include_dynamic_current_state:
            for stream in _CURRENT_SYMBOL_STATE_STREAMS:
                key = stream.value
                read, age = self._current_symbol_state_read(
                    coordinator=coordinator,
                    decision_id=decision_id,
                    stream=stream,
                    symbol=request.symbol,
                    now=now,
                )
                existing[key] = read
                ages[key] = age
        for stream in _CURRENT_GLOBAL_IDENTITY_STREAMS:
            key = stream.value
            read, age = self._current_read(
                coordinator=coordinator,
                decision_id=decision_id,
                stream=stream,
                symbol=None,
                now=now,
            )
            existing[key] = read
            ages[key] = age
        if include_dynamic_current_state:
            halt_read, halt_age = self._current_read(
                coordinator=coordinator,
                decision_id=decision_id,
                stream=CaptureStream.HALT_LULD_STATE,
                symbol=request.symbol,
                now=now,
            )
            existing[CaptureStream.HALT_LULD_STATE.value] = halt_read
            ages[CaptureStream.HALT_LULD_STATE.value] = halt_age
        else:
            specs.append(
                self._persisted_halt_state_read(
                    symbol=request.symbol,
                    snapshot_payload=snapshot_payload,
                    now=now,
                )
            )
        tape_key = None
        if tape is not None:
            tape_key = "first_dip_tape"
            existing[tape_key] = tape
            ages[tape_key] = self._first_dip_detector_policy.max_source_age_seconds
        return tuple(specs), existing, ages, tape_key

    @staticmethod
    def _economics(
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
        bbo_read: CapturedReadResult,
        ohlcv_read: CapturedReadResult,
        scanner_read: CapturedReadResult,
    ) -> tuple[CapturedAdaptiveRiskEconomicInputs, str]:
        """Derive the PAPER economic vector from exact captured inputs.

        No viability blob is allowed to self-attest these values.  The current
        executable spread/depth comes from the captured Alpaca PAPER BBO; the
        volatility and volume measures come from the latest exact symbol OHLCV
        query produced by the preceding observation tick; current cumulative
        volume is independently bound to the exact scanner projection.

        Alpaca PAPER US-equity fees are modeled as exactly zero by the adapter's
        existing PAPER-only fill contract.  Buying-power impact is explicitly a
        conservative full-long-notional policy estimate (one current ask dollar
        per share), not a broker preview.  The broad small-cap-momentum cluster
        intentionally pools all lane positions so it cannot understate risk by
        inventing per-symbol independence.
        """

        try:
            if (
                request.account_scope != "alpaca:paper"
                or request.execution_family != "alpaca_spot"
            ):
                _unavailable("production_adaptive_risk_paper_scope_mismatch")
            if bbo_read.receipt is None or len(bbo_read.source_events) != 1:
                _unavailable("production_adaptive_risk_bbo_coverage_unavailable")
            bbo_payload = resolve_capture_source_payload(
                bbo_read.source_events[0]
            ).payload
            if any(
                isinstance(bbo_payload.get(name), bool)
                for name in ("bid", "ask", "ask_size")
            ) or bbo_payload.get("size_unit") != "shares":
                _unavailable("production_adaptive_risk_bbo_coverage_unavailable")
            bid = float(bbo_payload["bid"])
            ask = float(bbo_payload["ask"])
            ask_size = float(bbo_payload["ask_size"])
            if not (
                math.isfinite(bid)
                and math.isfinite(ask)
                and math.isfinite(ask_size)
                and ask >= bid > 0.0
                and ask_size > 0.0
            ):
                _unavailable("production_adaptive_risk_bbo_coverage_unavailable")

            if ohlcv_read.receipt is None or len(ohlcv_read.source_events) != 1:
                _unavailable("production_adaptive_risk_ohlcv_coverage_unavailable")
            ohlcv_event = ohlcv_read.source_events[0]
            ohlcv_receipt = ohlcv_read.receipt
            if (
                ohlcv_event.stream is not CaptureStream.PROVIDER_OHLCV
                or ohlcv_event.symbol != request.symbol
                or not isinstance(ohlcv_event.query, Mapping)
            ):
                _unavailable("production_adaptive_risk_ohlcv_coverage_unavailable")
            query = ohlcv_event.query
            if (
                set(query)
                != {"schema_version", "call", "provider_parameters"}
                or query.get("schema_version")
                != PROVIDER_OHLCV_QUERY_SCHEMA_VERSION
                or ohlcv_event.query_sha256 != sha256_json(query)
                or ohlcv_receipt.query_sha256 != ohlcv_event.query_sha256
                or ohlcv_receipt.query != query
            ):
                _unavailable("production_adaptive_risk_ohlcv_query_unavailable")
            call = query.get("call")
            provider_parameters = query.get("provider_parameters")
            if (
                not isinstance(call, Mapping)
                or set(call) != {"symbol", "interval", "period"}
                or not isinstance(provider_parameters, Mapping)
                or set(provider_parameters)
                != {
                    "allow_provider_fallback",
                    "resolved_provider",
                    "cache_hit",
                    "cache_age_seconds",
                    "source_fetched_at_utc",
                    "integrity_ok",
                }
                or type(provider_parameters.get("allow_provider_fallback"))
                is not bool
                or type(provider_parameters.get("cache_hit")) is not bool
                or provider_parameters.get("integrity_ok") is not True
                or str(provider_parameters.get("resolved_provider") or "")
                .strip()
                .lower()
                != ohlcv_event.provider
            ):
                _unavailable("production_adaptive_risk_ohlcv_query_unavailable")
            cache_age = provider_parameters.get("cache_age_seconds")
            if (
                isinstance(cache_age, bool)
                or not isinstance(cache_age, (int, float))
                or not math.isfinite(float(cache_age))
                or float(cache_age) < 0.0
                or _parse_utc(
                    provider_parameters.get("source_fetched_at_utc"),
                    "production_adaptive_risk_ohlcv_source_fetched",
                )
                != ohlcv_event.clocks.received_at
            ):
                _unavailable("production_adaptive_risk_ohlcv_query_unavailable")
            if (
                str(call.get("symbol") or "").strip().upper() != request.symbol
                or str(call.get("period") or "").strip().lower() != "5d"
                or str(call.get("interval") or "").strip().lower()
                not in {"5m", "15m"}
            ):
                _unavailable("production_adaptive_risk_ohlcv_query_unavailable")
            source_interval = str(call.get("interval") or "").strip().lower()
            ohlcv_payload = resolve_capture_source_payload(ohlcv_event).payload
            if (
                set(ohlcv_payload)
                != {"schema_version", "query_sha256", "rows"}
                or ohlcv_payload.get("schema_version")
                != PROVIDER_OHLCV_PAYLOAD_SCHEMA_VERSION
                or ohlcv_payload.get("query_sha256")
                != ohlcv_event.query_sha256
            ):
                _unavailable("production_adaptive_risk_ohlcv_coverage_unavailable")
            raw_rows = ohlcv_payload.get("rows")
            if not isinstance(raw_rows, (list, tuple)) or len(raw_rows) < 5:
                _unavailable("production_adaptive_risk_ohlcv_rows_unavailable")
            rows: list[tuple[datetime, float, float, float, float, float]] = []
            for raw_row in raw_rows:
                if (
                    not isinstance(raw_row, Mapping)
                    or set(raw_row)
                    != {
                        "market_reference_at",
                        "open",
                        "high",
                        "low",
                        "close",
                        "volume",
                    }
                ):
                    _unavailable("production_adaptive_risk_ohlcv_rows_unavailable")
                market_at = _parse_utc(
                    raw_row.get("market_reference_at"),
                    "production_adaptive_risk_ohlcv_market",
                )
                values = tuple(
                    float(raw_row[name])
                    for name in ("open", "high", "low", "close", "volume")
                )
                open_px, high, low, close, volume = values
                if not (
                    all(math.isfinite(value) for value in values)
                    and high >= max(open_px, low, close) > 0.0
                    and min(open_px, high, close) >= low > 0.0
                    and volume >= 0.0
                ):
                    _unavailable("production_adaptive_risk_ohlcv_rows_unavailable")
                rows.append((market_at, open_px, high, low, close, volume))
            if rows != sorted(rows, key=lambda row: row[0]) or len(
                {row[0] for row in rows}
            ) != len(rows):
                _unavailable("production_adaptive_risk_ohlcv_order_unavailable")

            # Canonicalize every source to closed 15-minute bars so the risk
            # basis cannot silently switch between 70- and 210-minute windows
            # according to whichever legacy feature happened to query last.
            canonical_rows = rows
            if source_interval == "5m":
                grouped: dict[
                    datetime,
                    list[tuple[datetime, float, float, float, float, float]],
                ] = {}
                for row in rows:
                    at = row[0]
                    bucket = at.replace(
                        minute=(at.minute // 15) * 15,
                        second=0,
                        microsecond=0,
                    )
                    grouped.setdefault(bucket, []).append(row)
                canonical_rows = []
                for bucket in sorted(grouped):
                    members = grouped[bucket]
                    expected_minutes = {
                        bucket,
                        bucket + timedelta(minutes=5),
                        bucket + timedelta(minutes=10),
                    }
                    if len(members) != 3 or {row[0] for row in members} != expected_minutes:
                        continue
                    canonical_rows.append(
                        (
                            bucket,
                            members[0][1],
                            max(row[2] for row in members),
                            min(row[3] for row in members),
                            members[-1][4],
                            math.fsum(row[5] for row in members),
                        )
                    )
            # Aggregate timestamps are bar starts.  A captured row that had
            # not reached its 15-minute end at the exact receipt frontier is
            # partial and can understate true range, which would inflate size.
            # Later wall time cannot retroactively complete the captured row.
            ohlcv_frontier = ohlcv_receipt.returned_at
            canonical_rows = [
                row
                for row in canonical_rows
                if row[0] + timedelta(minutes=15) <= ohlcv_frontier
            ]
            if len(canonical_rows) < 5:
                _unavailable("production_adaptive_risk_canonical_ohlcv_unavailable")

            true_ranges: list[float] = []
            previous_close: float | None = None
            for _at, _open, high, low, close, _volume in canonical_rows:
                true_range = high - low
                if previous_close is not None:
                    true_range = max(
                        true_range,
                        abs(high - previous_close),
                        abs(low - previous_close),
                    )
                true_ranges.append(true_range)
                previous_close = close
            sampled_ranges = true_ranges[-min(14, len(true_ranges)) :]
            realized_volatility = (
                math.fsum(sampled_ranges)
                / float(len(sampled_ranges))
                / canonical_rows[-1][4]
            )
            if not math.isfinite(realized_volatility) or realized_volatility <= 0.0:
                _unavailable("production_adaptive_risk_volatility_unavailable")

            from zoneinfo import ZoneInfo  # noqa: PLC0415

            eastern = ZoneInfo("America/New_York")
            decision_date = bbo_read.receipt.returned_at.astimezone(eastern).date()
            regular_by_date: dict[date, dict[datetime, float]] = {}
            prior_dates_seen: set[date] = set()
            for market_at, _open, _high, _low, _close, volume in canonical_rows:
                local_at = market_at.astimezone(eastern)
                trading_date = local_at.date()
                if trading_date >= decision_date:
                    continue
                prior_dates_seen.add(trading_date)
                session_open = datetime(
                    trading_date.year,
                    trading_date.month,
                    trading_date.day,
                    9,
                    30,
                    tzinfo=eastern,
                )
                session_close = session_open + timedelta(hours=6, minutes=30)
                if session_open <= local_at < session_close:
                    regular_by_date.setdefault(trading_date, {})[
                        local_at
                    ] = volume
            # The oldest 5d boundary can start mid-session.  Every remaining
            # prior day must prove the full regular-session 15-minute lattice;
            # sparse or pagination-truncated input is coverage-unavailable,
            # never an ADV estimate.  Require two complete days after dropping
            # that boundary day.
            prior_dates = sorted(prior_dates_seen)
            if len(prior_dates) < 3:
                _unavailable("production_adaptive_risk_adv_coverage_unavailable")
            usable_dates = prior_dates[1:]
            expected_weekdays: list[date] = []
            cursor = usable_dates[0]
            while cursor < decision_date:
                if cursor.weekday() < 5:
                    expected_weekdays.append(cursor)
                cursor += timedelta(days=1)
            # Unknown exchange holidays deliberately fail this conservative
            # proof: without a captured calendar fact, a missing weekday could
            # be an omitted zero-volume session rather than a holiday.
            if usable_dates != expected_weekdays:
                _unavailable("production_adaptive_risk_adv_coverage_unavailable")
            complete_volumes: list[float] = []
            for trading_date in usable_dates:
                session_open = datetime(
                    trading_date.year,
                    trading_date.month,
                    trading_date.day,
                    9,
                    30,
                    tzinfo=eastern,
                )
                expected = {
                    session_open + timedelta(minutes=15 * index)
                    for index in range(26)
                }
                actual = regular_by_date.get(trading_date, {})
                if set(actual) != expected:
                    _unavailable(
                        "production_adaptive_risk_adv_coverage_unavailable"
                    )
                complete_volumes.append(math.fsum(actual.values()))
            if len(complete_volumes) < 2:
                _unavailable("production_adaptive_risk_adv_coverage_unavailable")
            average_daily_volume = math.fsum(complete_volumes) / len(
                complete_volumes
            )
            if average_daily_volume <= 0.0:
                _unavailable("production_adaptive_risk_adv_coverage_unavailable")

            if scanner_read.receipt is None or len(scanner_read.source_events) != 1:
                _unavailable("production_adaptive_risk_scanner_coverage_unavailable")
            scanner = CaptureScannerSnapshot.from_event(
                scanner_read.source_events[0]
            )
            scanner_market_at = scanner.event.clocks.market_reference_at
            if (
                scanner.query.symbol != request.symbol
                or scanner.share_volume is None
                or scanner.price is None
                or scanner_market_at is None
                or scanner_market_at.astimezone(eastern).date() != decision_date
            ):
                _unavailable("production_adaptive_risk_recent_volume_unavailable")
            recent_volume = _positive(
                scanner.share_volume,
                "production_adaptive_risk_recent_volume",
            )

            current_half_spread_bps = (
                (ask - bid) / ((ask + bid) / 2.0) * 5_000.0
            )
            result = CapturedAdaptiveRiskEconomicInputs(
                structural_stop=candidate.structural_stop_price,
                entry_slippage_bps=current_half_spread_bps,
                exit_slippage_bps=current_half_spread_bps,
                fees_per_share_usd=0.0,
                setup_quality=candidate.viability_score,
                realized_volatility_fraction=realized_volatility,
                average_daily_volume_shares=average_daily_volume,
                recent_volume_shares=recent_volume,
                executable_depth_shares=ask_size,
                candidate_buying_power_impact_per_share_usd=max(
                    float(quantize_alpaca_equity_limit_price(ask, "buy")),
                    float(scanner.price),
                ),
            )
        except (CaptureContractError, KeyError, TypeError, ValueError) as exc:
            raise CapturedPaperProductionMaterialUnavailable(
                "production_adaptive_risk_economics_coverage_unavailable"
            ) from exc
        return result, "equity:smallcap-momentum"

    def _final_read_provider(
        self,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        symbol: str,
        decision_id: str,
    ) -> FirstDipFinalReadProvider:
        def provider(
            *,
            adaptive_request: object,
            detector_policy: FirstDipTapePolicy,
            final_boundary_available_at: datetime,
        ) -> FirstDipFinalCaptureRead:
            del adaptive_request
            if detector_policy.policy_sha256 != self._first_dip_detector_policy.policy_sha256:
                _unavailable("production_first_dip_final_policy_mismatch")
            boundary = _utc(
                final_boundary_available_at,
                "production_first_dip_final_boundary",
            )
            read = self._typed_first_dip_read(
                coordinator=coordinator,
                decision_id=decision_id,
                symbol=symbol,
                boundary=boundary,
                policy=detector_policy,
            )
            assert read.receipt is not None
            profile = FSMDependencyProfile(
                required_streams=frozenset({CaptureStream.IQFEED_PRINT}),
                required_read_ids=(read.receipt.read_id,),
                stream_dependencies=(
                    FSMStreamDependency(
                        stream=CaptureStream.IQFEED_PRINT,
                        exact_provider_event_at_required=True,
                        market_reference_at_required=False,
                        max_source_age_seconds=detector_policy.max_source_age_seconds,
                        coverage_start_at=boundary
                        - timedelta(seconds=detector_policy.window_seconds),
                    ),
                ),
            )
            return FirstDipFinalCaptureRead(
                dependency_profile=profile,
                captured_reads=(read,),
                first_dip_tape_read_id=read.receipt.read_id,
            )

        return provider

    def candidate(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
        coordinator: LiveReplayCaptureCoordinator,
        adapter: CapturedAlpacaPaperAdapter,
        account_read: CapturedReadResult,
        bbo_read: CapturedReadResult,
    ) -> CapturedPaperCandidateObservedMaterial:
        del account_read
        first_dip = candidate.setup_family == "first_dip_reclaim"
        specs, existing, ages, tape_key = self._base_material(
            request=request,
            decision_id=candidate.client_order_id,
            snapshot_payload=candidate.to_payload(),
            snapshot_key="candidate_snapshot",
            coordinator=coordinator,
            adapter=adapter,
            bbo_read=bbo_read,
            include_dynamic_current_state=True,
            include_first_dip=first_dip,
        )
        economics, cluster = self._economics(
            request,
            candidate,
            bbo_read,
            existing[CaptureStream.PROVIDER_OHLCV.value],
            existing[CaptureStream.SCANNER_SNAPSHOT.value],
        )
        audit = None
        final_provider = None
        if first_dip:
            raw_opportunity = candidate.trigger_debug.get("opportunity_key")
            binding = candidate.trigger_debug.get(
                "first_dip_tape_decision_receipt_binding_sha256"
            )
            if type(raw_opportunity) is not dict:
                _unavailable("production_first_dip_opportunity_unavailable")
            try:
                opportunity_payload = {
                    "account_scope": request.account_scope,
                    "symbol": str(raw_opportunity["symbol"]).strip().upper(),
                    "trading_date": date.fromisoformat(
                        str(raw_opportunity["trading_date"])
                    ).isoformat(),
                    "setup_family": str(raw_opportunity["setup_family"]).strip().lower(),
                }
                opportunity_sha256 = sha256_json(opportunity_payload)
                audit = CapturedFirstDipDetectorAudit(
                    detector_policy=self._first_dip_detector_policy,
                    detector_authority_source="captured_db_paper",
                    detector_receipt_binding_sha256=_sha(
                        binding,
                        "production_first_dip_detector_binding",
                    ),
                    detector_opportunity_key_sha256=opportunity_sha256,
                )
            except (KeyError, TypeError, ValueError) as exc:
                raise CapturedPaperProductionMaterialUnavailable(
                    "production_first_dip_opportunity_unavailable"
                ) from exc
            final_provider = self._final_read_provider(
                coordinator=coordinator,
                symbol=request.symbol,
                decision_id=candidate.client_order_id,
            )
        return CapturedPaperCandidateObservedMaterial(
            reads=specs,
            existing_reads=existing,
            existing_read_max_age_seconds=ages,
            economics=economics,
            fact_read_keys=_CANDIDATE_FACT_READ_KEYS,
            correlation_cluster=cluster,
            setup_read_key="candidate_snapshot",
            first_dip_tape_key=tape_key,
            first_dip_detector_audit=audit,
            first_dip_final_read_provider=final_provider,
        )

    def observation(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        observation: CapturedPaperDurableObservationSnapshot,
        coordinator: LiveReplayCaptureCoordinator,
        adapter: CapturedAlpacaPaperAdapter,
        account_read: CapturedReadResult,
        bbo_read: CapturedReadResult,
    ) -> CapturedPaperObservationObservedMaterial:
        del account_read
        specs, existing, ages, tape_key = self._base_material(
            request=request,
            decision_id=observation.observation_decision_id,
            snapshot_payload=observation.to_payload(),
            snapshot_key="observation_snapshot",
            coordinator=coordinator,
            adapter=adapter,
            bbo_read=bbo_read,
            include_dynamic_current_state=False,
            include_first_dip=True,
            optional_first_dip=True,
        )
        return CapturedPaperObservationObservedMaterial(
            reads=specs,
            existing_reads=existing,
            existing_read_max_age_seconds=ages,
            observation_snapshot_key="observation_snapshot",
            admission_eligibility_key="admission_eligibility",
            first_dip_tape_key=tape_key,
            first_dip_detector_policy=(
                self._first_dip_detector_policy
                if tape_key is not None
                else None
            ),
        )


class _CapturedPaperExactRuntimeInputScope:
    """Require the canonical synchronous live-capture bridges for one FSM tick.

    The host installs these bridges immediately outside this bound scope.  They
    capture each *actual* scanner/OHLCV/microstructure call before its result is
    returned to the FSM, including every requested OHLCV interval.  Replacing
    them here with one preselected query would either hide calls or make the
    second interval fail for the wrong reason.
    """

    def __init__(self, reads: Sequence[CapturedReadResult]) -> None:
        read_ids: list[str] = []
        identities: set[str] = set()
        decision_ids: set[str] = set()
        for read in reads:
            if read.receipt is None:
                _unavailable("exact_runtime_receipt_unavailable")
            read_ids.append(read.receipt.read_id)
            identities.add(read.receipt.identity_sha256)
            decision_ids.add(read.receipt.decision_id)
        if (
            not read_ids
            or len(read_ids) != len(set(read_ids))
            or len(identities) != 1
            or len(decision_ids) != 1
        ):
            _unavailable("exact_runtime_receipt_inventory_unavailable")
        self._read_ids = tuple(sorted(read_ids))
        self._identity_sha256 = next(iter(identities))
        self._decision_id = next(iter(decision_ids))

    @property
    def scope_sha256(self) -> str:
        return sha256_json(
            {
                "schema_version": "chili.captured-paper-live-capture-bridge-scope.v1",
                "predecision_read_ids": self._read_ids,
                "identity_sha256": self._identity_sha256,
                "decision_id": self._decision_id,
            }
        )

    @contextmanager
    def install(self) -> Iterator[None]:
        from . import live_runner, pipeline, risk_evaluator  # noqa: PLC0415
        from ... import massive_client  # noqa: PLC0415

        micro = pipeline._MICROSTRUCTURE_READ_PROVIDER.get()
        ohlcv = live_runner._LIVE_OHLCV_CAPTURE_SINK.get()
        scanner = massive_client._MASSIVE_FULL_SNAPSHOT_CAPTURE_SINK.get()
        bridges = (micro, ohlcv, scanner)
        if (
            type(micro) is not LiveMicrostructureCaptureBridge
            or type(ohlcv) is not LiveOhlcvCaptureBridge
            or type(scanner) is not LiveScannerSnapshotCaptureBridge
            or micro.network_fallback_allowed is not False
            or any(
                bridge.coordinator.identity.identity_sha256 != self._identity_sha256
                for bridge in bridges
            )
            or any(bridge.decision_id != self._decision_id for bridge in bridges)
        ):
            _unavailable("production_live_capture_bridge_scope_unavailable")
        with risk_evaluator.captured_live_scanner_snapshot_scope():
            try:
                yield
            except BaseException:
                # The original exception already prevents a post-commit PAPER
                # request from escaping.  Do not mask it with a secondary latch.
                raise
            else:
                failures = tuple(
                    str(bridge.capture_failure_reason or "").strip()
                    for bridge in bridges
                    if str(bridge.capture_failure_reason or "").strip()
                )
                if failures:
                    _unavailable(
                        "production_live_capture_read_failed:"
                        + ",".join(sorted(set(failures)))
                    )


@dataclass(frozen=True, slots=True)
class CapturedPaperCandidateSupplement:
    """Original non-account inputs captured for one durable candidate."""

    captured_reads: tuple[CapturedReadResult, ...]
    dependency_profile: FSMDependencyProfile
    input_scope_installer: Callable[[], AbstractContextManager[Any]]
    input_scope_sha256: str
    economics: CapturedAdaptiveRiskEconomicInputs
    fact_sources: Mapping[str, CapturedPaperFactSource]
    correlation_cluster: str
    setup_read_id: str
    buying_power_double_census: Any | None = None
    first_dip_tape_read_id: str | None = None
    first_dip_detector_audit: CapturedFirstDipDetectorAudit | None = None
    first_dip_final_read_provider: FirstDipFinalReadProvider | None = None
    network_fallback_allowed: bool = False

    def __post_init__(self) -> None:
        reads = tuple(self.captured_reads)
        if any(type(row) is not CapturedReadResult for row in reads):
            _unavailable("production_supplemental_reads_unavailable")
        object.__setattr__(self, "captured_reads", reads)
        if self.network_fallback_allowed is not False:
            _unavailable("production_supplemental_network_fallback_prohibited")
        if not callable(self.input_scope_installer):
            _unavailable("production_supplemental_input_scope_unavailable")
        object.__setattr__(
            self,
            "input_scope_sha256",
            _sha(self.input_scope_sha256, "production_supplemental_input_scope"),
        )
        if type(self.economics) is not CapturedAdaptiveRiskEconomicInputs:
            _unavailable("production_supplemental_economics_unavailable")
        cluster = str(self.correlation_cluster or "").strip().lower()
        setup_id = str(self.setup_read_id or "").strip()
        if not cluster or not setup_id:
            _unavailable("production_supplemental_identity_unavailable")
        object.__setattr__(self, "correlation_cluster", cluster)
        object.__setattr__(self, "setup_read_id", setup_id)
        sources = dict(self.fact_sources)
        if set(sources) != set(_FACT_NAMES) or any(
            type(value) is not CapturedPaperFactSource for value in sources.values()
        ):
            _unavailable("production_supplemental_fact_sources_unavailable")
        object.__setattr__(self, "fact_sources", MappingProxyType(sources))
        first_id = str(self.first_dip_tape_read_id or "").strip() or None
        trio = (
            first_id,
            self.first_dip_detector_audit,
            self.first_dip_final_read_provider,
        )
        if any(value is None for value in trio) and any(value is not None for value in trio):
            _unavailable("production_supplemental_first_dip_pair_unavailable")
        object.__setattr__(self, "first_dip_tape_read_id", first_id)


@dataclass(frozen=True, slots=True)
class CapturedPaperObservationSupplement:
    """Original non-account inputs for one WATCHING/QUEUED invocation."""

    captured_reads: tuple[CapturedReadResult, ...]
    dependency_profile: FSMDependencyProfile
    input_scope_installer: Callable[[], AbstractContextManager[Any]]
    input_scope_sha256: str
    observation_snapshot_read_id: str
    admission_eligibility_read_id: str
    first_dip_tape_read_id: str | None = None
    first_dip_detector_policy: FirstDipTapePolicy | None = None
    network_fallback_allowed: bool = False

    def __post_init__(self) -> None:
        reads = tuple(self.captured_reads)
        if any(type(row) is not CapturedReadResult for row in reads):
            _unavailable("observation_supplemental_reads_unavailable")
        object.__setattr__(self, "captured_reads", reads)
        if self.network_fallback_allowed is not False:
            _unavailable("observation_supplemental_network_fallback_prohibited")
        if not callable(self.input_scope_installer):
            _unavailable("observation_supplemental_input_scope_unavailable")
        object.__setattr__(
            self,
            "input_scope_sha256",
            _sha(self.input_scope_sha256, "observation_supplemental_input_scope"),
        )
        snapshot_id = str(self.observation_snapshot_read_id or "").strip()
        eligibility_id = str(self.admission_eligibility_read_id or "").strip()
        if not snapshot_id or not eligibility_id or snapshot_id == eligibility_id:
            _unavailable("observation_supplemental_identity_unavailable")
        object.__setattr__(self, "observation_snapshot_read_id", snapshot_id)
        object.__setattr__(self, "admission_eligibility_read_id", eligibility_id)
        first_id = str(self.first_dip_tape_read_id or "").strip() or None
        if (first_id is None) != (self.first_dip_detector_policy is None):
            _unavailable("observation_supplemental_first_dip_pair_unavailable")
        object.__setattr__(self, "first_dip_tape_read_id", first_id)


class CapturedPaperCandidateSupplementProvider(Protocol):
    def __call__(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
        coordinator: LiveReplayCaptureCoordinator,
        adapter: CapturedAlpacaPaperAdapter,
        account_read: CapturedReadResult,
        bbo_read: CapturedReadResult,
    ) -> AbstractContextManager[CapturedPaperCandidateSupplement]: ...


class CapturedPaperObservationSupplementProvider(Protocol):
    def __call__(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        observation: CapturedPaperDurableObservationSnapshot,
        coordinator: LiveReplayCaptureCoordinator,
        adapter: CapturedAlpacaPaperAdapter,
        account_read: CapturedReadResult,
        bbo_read: CapturedReadResult,
    ) -> AbstractContextManager[CapturedPaperObservationSupplement]: ...


class CapturedPaperCaptureBackedSupplementProviders:
    """Receipt already-observed service facts and construct both supplements.

    The two injected input providers are the only provider-I/O seams.  They run
    after the material factory has closed its DB reader transaction.  This
    class performs capture writes only, derives every read/profile/fact binding
    itself, and installs exact one-shot runtime providers with no fallback.
    """

    def __init__(
        self,
        *,
        candidate_inputs: CapturedPaperCandidateObservedInputProvider,
        observation_inputs: CapturedPaperObservationObservedInputProvider,
    ) -> None:
        if not callable(candidate_inputs) or not callable(observation_inputs):
            raise TypeError("captured PAPER observed-input providers must be callable")
        self._candidate_inputs = candidate_inputs
        self._observation_inputs = observation_inputs

    @staticmethod
    def _capture_one(
        *,
        coordinator: LiveReplayCaptureCoordinator,
        decision_id: str,
        symbol: str,
        spec: CapturedPaperObservedRead,
    ) -> CapturedReadResult:
        scoped_symbol = symbol if spec.symbol_scoped else None
        policy = STREAM_POLICIES[spec.stream]
        if policy.coverage_mode is CoverageMode.QUERY_RECEIPT:
            captured = coordinator.capture_query_result(
                decision_id=decision_id,
                stream=spec.stream,
                provider=spec.provider,
                query=spec.query,
                requested_at=spec.requested_at,
                returned_at=spec.returned_at,
                results=spec.results,
                symbol=scoped_symbol,
            )
        else:
            observed = spec.results[0]
            if policy.coverage_mode is CoverageMode.CHANGE_LOG:
                changed = coordinator.record_change(
                    stream=spec.stream,
                    provider=spec.provider,
                    payload=observed.payload,
                    clocks=observed.clocks,
                    symbol=scoped_symbol,
                    query=spec.query,
                    broad=False,
                )
                source_event = changed.current_event
                if changed.coverage_gap_recorded or source_event is None:
                    _unavailable(f"{spec.key}_change_capture_unavailable")
            elif policy.coverage_mode in {CoverageMode.IDENTITY, CoverageMode.DERIVED}:
                submission = coordinator.submit_exact_input(
                    stream=spec.stream,
                    provider=spec.provider,
                    payload=observed.payload,
                    clocks=observed.clocks,
                    symbol=scoped_symbol,
                    query=spec.query,
                )
                source_event = submission.event
                if not submission.accepted or source_event is None:
                    _unavailable(f"{spec.key}_source_capture_unavailable")
            else:
                _unavailable(f"{spec.key}_capture_mode_unavailable")
            captured = coordinator.capture_durable_read(
                decision_id=decision_id,
                stream=spec.stream,
                provider=spec.provider,
                query=spec.query,
                requested_at=spec.requested_at,
                returned_at=spec.returned_at,
                source_events=(source_event,),
                symbol=scoped_symbol,
            )
            if spec.finalize_local_continuity:
                event_clock = (
                    source_event.clocks.provider_event_at
                    or source_event.clocks.market_reference_at
                    or source_event.clocks.available_at
                )
                coordinator.emit_provider_watermark(
                    stream=spec.stream,
                    provider=spec.provider,
                    symbol=scoped_symbol,
                    event_watermark_at=max(event_clock, spec.returned_at),
                    emitted_available_at=spec.returned_at,
                    bounded_lateness_seconds=0.001,
                    max_observed_lateness_seconds=0.0,
                    generation=coordinator.identity.generation,
                )
                coordinator.checkpoint_live_continuity(spec.stream)
        if not captured.durable or captured.receipt is None:
            _unavailable(f"{spec.key}_durable_read_unavailable")
        return captured

    @classmethod
    def _capture_inventory(
        cls,
        *,
        coordinator: LiveReplayCaptureCoordinator,
        decision_id: str,
        symbol: str,
        specs: Sequence[CapturedPaperObservedRead],
        existing: Mapping[str, CapturedReadResult],
        existing_ages: Mapping[str, float],
    ) -> tuple[
        dict[str, CapturedReadResult],
        FSMDependencyProfile,
        _CapturedPaperExactRuntimeInputScope,
    ]:
        captured_by_key = dict(existing)
        age_by_key = {
            key: _positive(value, "captured_existing_read_max_age")
            for key, value in dict(existing_ages).items()
        }
        for spec in specs:
            captured_by_key[spec.key] = cls._capture_one(
                coordinator=coordinator,
                decision_id=decision_id,
                symbol=symbol,
                spec=spec,
            )
            age_by_key[spec.key] = spec.max_source_age_seconds
        rows = tuple(captured_by_key.values())
        if not rows or any(
            row.receipt is None or row.receipt.decision_id != decision_id for row in rows
        ):
            _unavailable("captured_supplement_read_identity_unavailable")
        stream_rows: dict[CaptureStream, list[tuple[CapturedReadResult, float]]] = {}
        for key, row in captured_by_key.items():
            assert row.receipt is not None
            stream_rows.setdefault(row.receipt.stream, []).append((row, age_by_key[key]))
        dependencies: list[FSMStreamDependency] = []
        for stream, values in stream_rows.items():
            ages = {value for _row, value in values}
            if len(ages) != 1:
                _unavailable(f"{stream.value}_max_age_policy_ambiguous")
            policy = STREAM_POLICIES[stream]
            dependencies.append(
                FSMStreamDependency(
                    stream=stream,
                    exact_provider_event_at_required=(
                        policy.exact_provider_event_clock_required
                    ),
                    market_reference_at_required=(
                        policy.market_reference_clock_required
                    ),
                    max_source_age_seconds=next(iter(ages)),
                    coverage_start_at=min(
                        row.receipt.requested_at for row, _value in values
                    ),
                )
            )
        profile = FSMDependencyProfile(
            required_streams=frozenset(stream_rows),
            required_read_ids=tuple(
                sorted(row.receipt.read_id for row in rows if row.receipt is not None)
            ),
            stream_dependencies=tuple(dependencies),
        )
        return captured_by_key, profile, _CapturedPaperExactRuntimeInputScope(rows)

    @staticmethod
    def _fact_source(
        *,
        name: str,
        keys: Sequence[str],
        captured_by_key: Mapping[str, CapturedReadResult],
        coordinator: LiveReplayCaptureCoordinator,
    ) -> CapturedPaperFactSource:
        reads = tuple(captured_by_key[key] for key in keys)
        events = tuple(event for read in reads for event in read.source_events)
        if not events or any(read.receipt is None for read in reads):
            _unavailable(f"production_{name}_fact_events_unavailable")
        observed_at = max(
            event.clocks.market_reference_at
            or event.clocks.provider_event_at
            or event.clocks.received_at
            for event in events
        )
        available_at = max(event.clocks.available_at for event in events)
        providers = sorted({event.provider for event in events})
        streams = sorted({event.stream.value for event in events})
        derivation = {
            "structural_stop": "durable-fsm-structural-stop.v1",
            "setup_quality": "durable-viability-score.v1",
            "volatility": "canonical-15m-true-range-mean-14.v1",
            "liquidity": (
                "paper-bbo-scanner-complete-regular-session-volume.v1"
            ),
            "correlation": "broad-smallcap-momentum-policy-cluster.v1",
            "candidate_buying_power_estimate": (
                "max-buy-limit-scanner-reference-full-notional.v1"
            ),
        }.get(name)
        if derivation is None:
            _unavailable(f"production_{name}_derivation_method_unavailable")
        return CapturedPaperFactSource(
            source=(
                "captured:"
                + "+".join(streams)
                + ";derivation="
                + derivation
            ),
            observed_at=observed_at,
            available_at=available_at,
            provider_generation=(
                f"run={coordinator.identity.run_id};generation="
                f"{coordinator.identity.generation};providers={','.join(providers)}"
            ),
            source_read_ids=tuple(
                sorted(read.receipt.read_id for read in reads if read.receipt is not None)
            ),
        )

    @contextmanager
    def candidate_supplement(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
        coordinator: LiveReplayCaptureCoordinator,
        account_read: CapturedReadResult,
        bbo_read: CapturedReadResult,
        adapter: CapturedAlpacaPaperAdapter,
    ) -> Iterator[CapturedPaperCandidateSupplement]:
        observed = self._candidate_inputs(
            request=request,
            candidate=candidate,
            coordinator=coordinator,
            adapter=adapter,
            account_read=account_read,
            bbo_read=bbo_read,
        )
        if type(observed) is not CapturedPaperCandidateObservedMaterial:
            _unavailable("candidate_observed_material_unavailable")
        captured, profile, runtime_scope = self._capture_inventory(
            coordinator=coordinator,
            decision_id=candidate.client_order_id,
            symbol=request.symbol,
            specs=observed.reads,
            existing=observed.existing_reads,
            existing_ages=observed.existing_read_max_age_seconds,
        )
        fact_reads = dict(captured)
        fact_reads["__account_risk_snapshot"] = account_read
        fact_reads["__execution_bbo"] = bbo_read
        adapter_lineage = {
            "structural_stop": ("__execution_bbo",),
            "setup_quality": (),
            "volatility": (),
            "liquidity": ("__execution_bbo",),
            "correlation": ("__account_risk_snapshot",),
            "candidate_buying_power_estimate": (
                "__account_risk_snapshot",
                "__execution_bbo",
            ),
        }
        facts = {
            name: self._fact_source(
                name=name,
                keys=(
                    *observed.fact_read_keys[name],
                    *adapter_lineage[name],
                ),
                captured_by_key=fact_reads,
                coordinator=coordinator,
            )
            for name in _FACT_NAMES
        }
        first_read = (
            None
            if observed.first_dip_tape_key is None
            else captured[observed.first_dip_tape_key]
        )
        if first_read is not None and (
            first_read.receipt is None
            or first_read.receipt.stream is not CaptureStream.IQFEED_PRINT
        ):
            _unavailable("candidate_first_dip_exact_print_stream_unavailable")
        yield CapturedPaperCandidateSupplement(
            captured_reads=tuple(captured.values()),
            dependency_profile=profile,
            input_scope_installer=runtime_scope.install,
            input_scope_sha256=runtime_scope.scope_sha256,
            economics=observed.economics,
            fact_sources=facts,
            correlation_cluster=observed.correlation_cluster,
            setup_read_id=captured[observed.setup_read_key].receipt.read_id,
            buying_power_double_census=observed.buying_power_double_census,
            first_dip_tape_read_id=(
                None if first_read is None else first_read.receipt.read_id
            ),
            first_dip_detector_audit=observed.first_dip_detector_audit,
            first_dip_final_read_provider=observed.first_dip_final_read_provider,
        )

    @contextmanager
    def observation_supplement(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        observation: CapturedPaperDurableObservationSnapshot,
        coordinator: LiveReplayCaptureCoordinator,
        account_read: CapturedReadResult,
        bbo_read: CapturedReadResult,
        adapter: CapturedAlpacaPaperAdapter,
    ) -> Iterator[CapturedPaperObservationSupplement]:
        observed = self._observation_inputs(
            request=request,
            observation=observation,
            coordinator=coordinator,
            adapter=adapter,
            account_read=account_read,
            bbo_read=bbo_read,
        )
        if type(observed) is not CapturedPaperObservationObservedMaterial:
            _unavailable("observation_observed_material_unavailable")
        captured, profile, runtime_scope = self._capture_inventory(
            coordinator=coordinator,
            decision_id=observation.observation_decision_id,
            symbol=request.symbol,
            specs=observed.reads,
            existing=observed.existing_reads,
            existing_ages=observed.existing_read_max_age_seconds,
        )
        first_read = (
            None
            if observed.first_dip_tape_key is None
            else captured[observed.first_dip_tape_key]
        )
        if first_read is not None and (
            first_read.receipt is None
            or first_read.receipt.stream is not CaptureStream.IQFEED_PRINT
        ):
            _unavailable("observation_first_dip_exact_print_stream_unavailable")
        yield CapturedPaperObservationSupplement(
            captured_reads=tuple(captured.values()),
            dependency_profile=profile,
            input_scope_installer=runtime_scope.install,
            input_scope_sha256=runtime_scope.scope_sha256,
            observation_snapshot_read_id=(
                captured[observed.observation_snapshot_key].receipt.read_id
            ),
            admission_eligibility_read_id=(
                captured[observed.admission_eligibility_key].receipt.read_id
            ),
            first_dip_tape_read_id=(
                None if first_read is None else first_read.receipt.read_id
            ),
            first_dip_detector_policy=observed.first_dip_detector_policy,
        )


class CapturedPaperServiceCaptureProviders:
    """Concrete candidate/observation providers owned by the service process."""

    def __init__(
        self,
        *,
        raw_adapter_factory: Callable[
            [CapturedPaperDispatchRequest, LiveReplayCaptureCoordinator], Any
        ],
        candidate_supplement_provider: CapturedPaperCandidateSupplementProvider,
        observation_supplement_provider: CapturedPaperObservationSupplementProvider,
        wall_clock: Callable[[], datetime],
        quote_max_age_seconds: float,
        account_max_age_seconds: float,
    ) -> None:
        if (
            not callable(raw_adapter_factory)
            or not callable(candidate_supplement_provider)
            or not callable(observation_supplement_provider)
            or not callable(wall_clock)
        ):
            raise TypeError("captured PAPER service providers must be callable")
        self._raw_adapter_factory = raw_adapter_factory
        self._candidate_supplement_provider = candidate_supplement_provider
        self._observation_supplement_provider = observation_supplement_provider
        self._wall_clock = wall_clock
        self._quote_max_age_seconds = _positive(
            quote_max_age_seconds, "production_quote_max_age"
        )
        self._account_max_age_seconds = _positive(
            account_max_age_seconds, "production_account_max_age"
        )

    @staticmethod
    def _coordinator(value: Any, symbol: str) -> LiveReplayCaptureCoordinator:
        if (
            not isinstance(value, LiveReplayCaptureCoordinator)
            or value.certification_symbol != symbol
            or getattr(value.state, "value", value.state) != "running"
        ):
            _unavailable("production_live_capture_coordinator_unavailable")
        return value

    def _adapter(
        self,
        request: CapturedPaperDispatchRequest,
        coordinator: LiveReplayCaptureCoordinator,
    ) -> CapturedAlpacaPaperAdapter:
        raw = self._raw_adapter_factory(request, coordinator)
        return CapturedAlpacaPaperAdapter(
            adapter=raw,
            coordinator=coordinator,
            expected_account_id=request.expected_account_id,
            wall_clock=self._wall_clock,
            quote_max_age_seconds=self._quote_max_age_seconds,
            account_max_age_seconds=self._account_max_age_seconds,
        )

    @staticmethod
    def _profile(
        *,
        supplemental_profile: FSMDependencyProfile,
        account: CapturedReadResult,
        bbo: CapturedReadResult,
        account_max_age_seconds: float,
        quote_max_age_seconds: float,
    ) -> FSMDependencyProfile:
        dependencies = {
            row.stream: row for row in supplemental_profile.stream_dependencies
        }
        for captured, max_age in (
            (account, account_max_age_seconds),
            (bbo, quote_max_age_seconds),
        ):
            assert captured.receipt is not None
            stream = captured.receipt.stream
            if stream in dependencies or len(captured.source_events) != 1:
                _unavailable("production_adapter_profile_collision")
            policy = STREAM_POLICIES[stream]
            dependencies[stream] = FSMStreamDependency(
                stream=stream,
                exact_provider_event_at_required=(
                    policy.exact_provider_event_clock_required
                ),
                market_reference_at_required=(
                    policy.market_reference_clock_required
                ),
                max_source_age_seconds=max_age,
                coverage_start_at=captured.receipt.requested_at,
            )
        read_ids = tuple(
            sorted(
                (
                    *supplemental_profile.required_read_ids,
                    account.receipt.read_id,
                    bbo.receipt.read_id,
                )
            )
        )
        return FSMDependencyProfile(
            required_streams=frozenset(dependencies),
            required_read_ids=read_ids,
            stream_dependencies=tuple(dependencies.values()),
        )

    @staticmethod
    def _bound_scope(
        *,
        supplement_scope_sha256: str,
        installer: Callable[[], AbstractContextManager[Any]],
        captured_reads: Sequence[CapturedReadResult],
        profile: FSMDependencyProfile,
    ) -> CapturedPaperBoundInputScope:
        receipt_inventory = []
        for row in captured_reads:
            if row.receipt is None:
                _unavailable("production_bound_scope_receipt_unavailable")
            receipt_inventory.append(
                {
                    "read_id": row.receipt.read_id,
                    "receipt_sha256": sha256_json(row.receipt.to_dict()),
                }
            )
        return CapturedPaperBoundInputScope(
            installer=installer,
            required_read_ids=profile.required_read_ids,
            scope_sha256=sha256_json(
                {
                    "schema_version": "chili.captured-paper-service-input-scope.v1",
                    "supplement_scope_sha256": supplement_scope_sha256,
                    "dependency_profile_sha256": profile.profile_sha256,
                    "receipts": sorted(
                        receipt_inventory, key=lambda row: row["read_id"]
                    ),
                }
            ),
        )

    @staticmethod
    def _fact_evidence(
        *,
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
        supplement: CapturedPaperCandidateSupplement,
        proof: Any,
        decision_at: datetime,
    ) -> CapturedAdaptiveRiskEvidenceSet:
        identity = CapturedAdaptiveRiskDecisionIdentity(
            execution_surface="alpaca_paper",
            run_id=proof.run_id,
            generation=proof.generation,
            decision_id=candidate.client_order_id,
            symbol=request.symbol,
            setup_family=candidate.setup_family,
            correlation_cluster=supplement.correlation_cluster,
            account_scope=request.account_scope,
            decision_at=decision_at,
        )
        payloads = captured_adaptive_risk_fact_payloads(
            identity, supplement.economics
        )
        proof_ids = set(proof.required_read_ids)
        facts: dict[str, CapturedAdaptiveRiskFactProvenance] = {}
        for name in _FACT_NAMES:
            source = supplement.fact_sources[name]
            if (
                not set(source.source_read_ids).issubset(proof_ids)
                or source.observed_at > decision_at
                or source.available_at > decision_at
            ):
                _unavailable(f"production_{name}_fact_source_mismatch")
            facts[name] = CapturedAdaptiveRiskFactProvenance.create(
                payload=payloads[name],
                source=source.source,
                observed_at=source.observed_at,
                available_at=source.available_at,
                provider_generation=source.provider_generation,
                source_read_ids=source.source_read_ids,
            )
        return CapturedAdaptiveRiskEvidenceSet(**facts)

    @contextmanager
    def capture_candidate(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        candidate: CapturedPaperDurableCandidateSnapshot,
        coordinator: Any,
    ) -> Iterator[CapturedPaperProductionCapture]:
        coordinator = self._coordinator(coordinator, request.symbol)
        adapter = self._adapter(request, coordinator)
        with adapter.decision_scope(candidate.client_order_id):
            adapter.get_execution_bbo(
                request.symbol,
                max_age_seconds=self._quote_max_age_seconds,
            )
            account, bbo = adapter.consume_current_captured_reads(
                symbol=request.symbol
            )
            with self._candidate_supplement_provider(
                request=request,
                candidate=candidate,
                coordinator=coordinator,
                adapter=adapter,
                account_read=account,
                bbo_read=bbo,
            ) as supplement:
                if type(supplement) is not CapturedPaperCandidateSupplement:
                    _unavailable("production_candidate_supplement_unavailable")
                reads = _typed_reads(
                    supplement.captured_reads,
                    supplement.dependency_profile,
                    decision_id=candidate.client_order_id,
                    forbidden_streams=_ADAPTER_OWNED_STREAMS,
                )
                combined = (account, bbo, *reads)
                profile = self._profile(
                    supplemental_profile=supplement.dependency_profile,
                    account=account,
                    bbo=bbo,
                    account_max_age_seconds=self._account_max_age_seconds,
                    quote_max_age_seconds=self._quote_max_age_seconds,
                )
                proof = coordinator.attest_predecision_inputs(
                    decision_id=candidate.client_order_id,
                    dependency_profile=profile,
                    captured_reads=combined,
                    first_dip_tape_read_id=supplement.first_dip_tape_read_id,
                )
                decision_at = _utc(self._wall_clock(), "production_decision")
                if not proof.attested_available_at <= decision_at <= proof.expires_at:
                    _unavailable("production_decision_clock_outside_attestation")
                evidence = self._fact_evidence(
                    request=request,
                    candidate=candidate,
                    supplement=supplement,
                    proof=proof,
                    decision_at=decision_at,
                )
                scope = self._bound_scope(
                    supplement_scope_sha256=supplement.input_scope_sha256,
                    installer=supplement.input_scope_installer,
                    captured_reads=combined,
                    profile=profile,
                )
                yield CapturedPaperProductionCapture(
                    decision_at=decision_at,
                    adapter=adapter,
                    captured_reads=combined,
                    dependency_profile=profile,
                    active_input_attestation=proof,
                    economics=supplement.economics,
                    fact_evidence=evidence,
                    correlation_cluster=supplement.correlation_cluster,
                    setup_read_id=supplement.setup_read_id,
                    bound_input_scope=scope,
                    buying_power_double_census=(
                        supplement.buying_power_double_census
                    ),
                    first_dip_tape_read_id=supplement.first_dip_tape_read_id,
                    first_dip_detector_audit=supplement.first_dip_detector_audit,
                    first_dip_final_read_provider=(
                        supplement.first_dip_final_read_provider
                    ),
                )

    @contextmanager
    def capture_observation(
        self,
        *,
        request: CapturedPaperDispatchRequest,
        observation: CapturedPaperDurableObservationSnapshot,
        coordinator: Any,
    ) -> Iterator[CapturedPaperObservationCapture]:
        coordinator = self._coordinator(coordinator, request.symbol)
        adapter = self._adapter(request, coordinator)
        decision_id = observation.observation_decision_id
        with adapter.decision_scope(decision_id):
            adapter.get_execution_bbo(
                request.symbol,
                max_age_seconds=self._quote_max_age_seconds,
            )
            account, bbo = adapter.consume_current_captured_reads(
                symbol=request.symbol
            )
            with self._observation_supplement_provider(
                request=request,
                observation=observation,
                coordinator=coordinator,
                adapter=adapter,
                account_read=account,
                bbo_read=bbo,
            ) as supplement:
                if type(supplement) is not CapturedPaperObservationSupplement:
                    _unavailable("observation_supplement_unavailable")
                reads = _typed_reads(
                    supplement.captured_reads,
                    supplement.dependency_profile,
                    decision_id=decision_id,
                    forbidden_streams=_ADAPTER_OWNED_STREAMS,
                )
                combined = (account, bbo, *reads)
                profile = self._profile(
                    supplemental_profile=supplement.dependency_profile,
                    account=account,
                    bbo=bbo,
                    account_max_age_seconds=self._account_max_age_seconds,
                    quote_max_age_seconds=self._quote_max_age_seconds,
                )
                proof = coordinator.attest_predecision_inputs(
                    decision_id=decision_id,
                    dependency_profile=profile,
                    captured_reads=combined,
                    first_dip_tape_read_id=supplement.first_dip_tape_read_id,
                )
                decision_at = _utc(self._wall_clock(), "observation_decision")
                if not proof.attested_available_at <= decision_at <= proof.expires_at:
                    _unavailable("observation_decision_clock_outside_attestation")
                scope = self._bound_scope(
                    supplement_scope_sha256=supplement.input_scope_sha256,
                    installer=supplement.input_scope_installer,
                    captured_reads=combined,
                    profile=profile,
                )
                yield CapturedPaperObservationCapture(
                    decision_at=decision_at,
                    adapter=adapter,
                    captured_reads=combined,
                    dependency_profile=profile,
                    active_input_attestation=proof,
                    bound_input_scope=scope,
                    observation_snapshot_read_id=(
                        supplement.observation_snapshot_read_id
                    ),
                    admission_eligibility_read_id=(
                        supplement.admission_eligibility_read_id
                    ),
                    first_dip_tape_read_id=supplement.first_dip_tape_read_id,
                    first_dip_detector_policy=(
                        supplement.first_dip_detector_policy
                    ),
                )


def _build_captured_paper_service_material_factory(
    *,
    coordinator_for: Callable[[str], LiveReplayCaptureCoordinator],
    capture_config_for: Callable[[str], Mapping[str, Any]],
    settings_projection_sha256: str,
    raw_adapter_factory: Callable[
        [CapturedPaperDispatchRequest, LiveReplayCaptureCoordinator], Any
    ],
    candidate_supplement_provider: CapturedPaperCandidateSupplementProvider,
    observation_supplement_provider: CapturedPaperObservationSupplementProvider,
    policy_spec: CapturedAdaptiveRiskPolicySpec,
    operational_policy: CapturedPaperOperationalPolicy,
    wall_clock: Callable[[], datetime],
    quote_max_age_seconds: float,
    account_max_age_seconds: float,
    candidate_reader: Callable[[Any, CapturedPaperDispatchRequest], Any] | None = None,
) -> CapturedPaperProductionMaterialFactory:
    """Construct the exact factory installed by the dedicated PAPER service."""

    if candidate_reader is None:
        from .live_runner import read_captured_paper_durable_candidate

        candidate_reader = read_captured_paper_durable_candidate
    providers = CapturedPaperServiceCaptureProviders(
        raw_adapter_factory=raw_adapter_factory,
        candidate_supplement_provider=candidate_supplement_provider,
        observation_supplement_provider=observation_supplement_provider,
        wall_clock=wall_clock,
        quote_max_age_seconds=quote_max_age_seconds,
        account_max_age_seconds=account_max_age_seconds,
    )
    return CapturedPaperProductionMaterialFactory(
        candidate_reader=candidate_reader,
        capture_provider=providers.capture_candidate,
        observation_capture_provider=providers.capture_observation,
        coordinator_for=coordinator_for,
        capture_config_for=capture_config_for,
        settings_projection_sha256=settings_projection_sha256,
        policy_spec=policy_spec,
        operational_policy=operational_policy,
    )


def build_capture_backed_paper_service_material_factory(
    *,
    coordinator_for: Callable[[str], LiveReplayCaptureCoordinator],
    capture_config_for: Callable[[str], Mapping[str, Any]],
    settings_projection_sha256: str,
    raw_adapter_factory: Callable[
        [CapturedPaperDispatchRequest, LiveReplayCaptureCoordinator], Any
    ],
    policy_spec: CapturedAdaptiveRiskPolicySpec,
    operational_policy: CapturedPaperOperationalPolicy,
    first_dip_detector_policy: FirstDipTapePolicy,
    wall_clock: Callable[[], datetime],
    quote_max_age_seconds: float,
    account_max_age_seconds: float,
    context_max_age_seconds: float,
) -> CapturedPaperProductionMaterialFactory:
    """Build the callback-free capture-backed Alpaca PAPER factory.

    The host may supply only runtime ownership/configuration and the raw Alpaca
    PAPER adapter.  Candidate/observation facts cannot be injected.  Missing
    capture inventory is a decision-local unavailable result.
    """

    observed = _CapturedPaperCoordinatorObservedInputs(
        wall_clock=wall_clock,
        context_max_age_seconds=context_max_age_seconds,
        first_dip_detector_policy=first_dip_detector_policy,
    )
    supplements = CapturedPaperCaptureBackedSupplementProviders(
        candidate_inputs=observed.candidate,
        observation_inputs=observed.observation,
    )
    return _build_captured_paper_service_material_factory(
        coordinator_for=coordinator_for,
        capture_config_for=capture_config_for,
        settings_projection_sha256=settings_projection_sha256,
        raw_adapter_factory=raw_adapter_factory,
        candidate_supplement_provider=supplements.candidate_supplement,
        observation_supplement_provider=supplements.observation_supplement,
        policy_spec=policy_spec,
        operational_policy=operational_policy,
        wall_clock=wall_clock,
        quote_max_age_seconds=quote_max_age_seconds,
        account_max_age_seconds=account_max_age_seconds,
    )


def build_live_fsm_captured_paper_service_material_factory(
    *,
    host: Any,
    settings: Any,
    settings_projection_sha256: str,
    raw_adapter_factory: Callable[
        [CapturedPaperDispatchRequest, LiveReplayCaptureCoordinator], Any
    ],
    policy_spec: CapturedAdaptiveRiskPolicySpec,
    operational_policy: CapturedPaperOperationalPolicy,
    wall_clock: Callable[[], datetime],
    quote_max_age_seconds: float,
    account_max_age_seconds: float,
) -> CapturedPaperProductionMaterialFactory:
    """Compatibility entry point consumed by the SHA-bound PAPER service.

    Only the validated IQFeed host may supply runtime ownership.  Candidate and
    observation facts are still produced internally; this surface exposes no
    fact callback and no candidate-reader override.
    """

    composition = getattr(host, "composition", None)
    service = getattr(composition, "service", None)
    coordinator_for = getattr(service, "coordinator_for", None)
    capture_config_for = getattr(host, "captured_paper_config_evidence_for", None)
    if not callable(coordinator_for) or not callable(capture_config_for):
        raise TypeError("captured PAPER host lacks exact capture ownership APIs")
    first_dip_policy = FirstDipTapePolicy.from_settings(settings)
    context_max_age_seconds = getattr(
        settings,
        "chili_momentum_adaptive_risk_context_data_max_age_seconds",
        None,
    )
    return build_capture_backed_paper_service_material_factory(
        coordinator_for=coordinator_for,
        capture_config_for=capture_config_for,
        settings_projection_sha256=settings_projection_sha256,
        raw_adapter_factory=raw_adapter_factory,
        policy_spec=policy_spec,
        operational_policy=operational_policy,
        first_dip_detector_policy=first_dip_policy,
        wall_clock=wall_clock,
        quote_max_age_seconds=quote_max_age_seconds,
        account_max_age_seconds=account_max_age_seconds,
        context_max_age_seconds=context_max_age_seconds,
    )


__all__ = [
    "build_capture_backed_paper_service_material_factory",
    "build_live_fsm_captured_paper_service_material_factory",
]
