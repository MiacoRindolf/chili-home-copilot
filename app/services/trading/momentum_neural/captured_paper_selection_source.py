"""Read-only derived selection source for the captured Alpaca PAPER lane.

The independently scheduled legacy viability producer remains nomination-only.
This module reads one repeatable-read snapshot of its canonical rows, rebuilds
the exact typed scorer inputs, resolves every helper/DB scalar once, and returns
an immutable occurrence ready for the durable captured-selection queue.  The
queue consumer never has a database, provider, network, or broker fallback.

This bridge is intentionally honest about authority: its source event is a
``derived_viability_snapshot``.  It does not claim that upstream Massive/IQFeed
frames are independently certifiable.  Those broader raw streams remain a
post-PAPER capture-hardening workstream; the exact inputs actually scored and
read by PAPER/Replay are nevertheless content-addressable and reproducible.
"""

from __future__ import annotations

import copy
import math
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.trading import (
    BrainNodeState,
    MomentumStrategyVariant,
    MomentumSymbolViability,
)
from app.services.yf_session import (
    FundamentalsProviderState,
    FundamentalsReceipt,
    FundamentalsReceiptOrigin,
    FundamentalsReceiptStatus,
)

from .captured_paper_initial_admission import (
    captured_paper_initial_variant_sha256,
    captured_paper_initial_viability_sha256,
)
from .captured_paper_selection_producer import CapturedPaperSelectionAuthority
from .captured_paper_variant_binding import (
    CapturedPaperVariantBindingApplication,
)
from .captured_viability_adapter import (
    REQUIRED_COMPONENTS,
    CapturedViabilityDependencyBinding,
    CapturedViabilityDependencyInventory,
    CapturedViabilityInputBundle,
    CapturedViabilityPostScoreAdjustment,
    CapturedViabilityScoringAuthority,
    captured_viability_component_sha256s,
    captured_viability_read_receipt_sha256,
)
from .context import (
    ChopExpansionRegime,
    MomentumRegimeContext,
    VolatilityRegime,
)
from .features import ExecutionReadinessFeatures
from .leveraged_etf import is_excluded_fund_name, is_leveraged_etf_name
from .replay_capture_contract import (
    CaptureClocks,
    CaptureEvent,
    CaptureEventRef,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    FSMDependencyProfile,
    FSMStreamDependency,
    StreamCoverage,
    captured_read_result_sha256,
    sha256_json,
)
from .variants import MomentumStrategyFamily, get_family
from .viability import (
    ViabilityExternalInputs,
    ViabilitySettingsProjection,
    resolve_viability_external_inputs_for_capture,
)


UTC = timezone.utc
SOURCE_SCHEMA_VERSION = "chili.captured-paper-derived-viability-source.v1"
CONFIG_SCHEMA_VERSION = "chili.captured-paper-viability-config.v1"
CODE_SCHEMA_VERSION = "chili.captured-paper-viability-code.v1"
ACCOUNT_SCHEMA_VERSION = "chili.captured-paper-selection-account.v1"
FEATURE_FLAGS_SCHEMA_VERSION = "chili.captured-paper-selection-flags.v1"
SOURCE_PROVIDER = "legacy_viability_derived_snapshot"
CONFIG_PROVIDER = "captured_paper_runtime_config"
CODE_PROVIDER = "captured_paper_code_build"
FEATURE_FLAGS_PROVIDER = "captured_paper_adaptive_policy"
FUNDAMENTALS_PROVIDER = "yfinance_fundamentals_primary"
HUB_NODE_ID = "nm_momentum_crypto_intel"
FUNDAMENTALS_QUERY_SCHEMA_VERSION = (
    "chili.captured-paper-fundamentals-query.v1"
)

_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.]{0,35}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
_READ_ONLY_TRANSACTION_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)


class CapturedPaperSelectionSourceUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "captured_selection_source_unavailable")
        super().__init__(self.reason)


def _reject(reason: str) -> None:
    raise CapturedPaperSelectionSourceUnavailable(reason)


def _utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, datetime):
        _reject(f"{field_name}_invalid")
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    try:
        if value.utcoffset() is None:
            _reject(f"{field_name}_invalid")
    except Exception as exc:
        raise CapturedPaperSelectionSourceUnavailable(
            f"{field_name}_invalid"
        ) from exc
    return value.astimezone(UTC)


def _parse_utc(value: Any, field_name: str) -> datetime:
    if not isinstance(value, str) or not value:
        _reject(f"{field_name}_invalid")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), field_name)
    except ValueError as exc:
        raise CapturedPaperSelectionSourceUnavailable(
            f"{field_name}_invalid"
        ) from exc


def _sha(value: Any, field_name: str) -> str:
    normalized = str(value or "").strip()
    if normalized != value or _SHA_RE.fullmatch(normalized) is None:
        _reject(f"{field_name}_invalid")
    return normalized


def _finite_positive(value: Any, field_name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(f"{field_name}_invalid")
    normalized = float(value)
    if not math.isfinite(normalized) or normalized <= 0.0:
        _reject(f"{field_name}_invalid")
    return normalized


def _context_from_snapshot(raw: Any) -> MomentumRegimeContext:
    if not isinstance(raw, Mapping):
        _reject("derived_source_regime_snapshot_invalid")
    try:
        utc_iso = str(raw["utc_iso"])
        event_at = _parse_utc(utc_iso, "derived_source_context_clock")
        utc_hour = int(raw["utc_hour"])
        if isinstance(raw["utc_hour"], bool) or utc_hour != event_at.hour:
            _reject("derived_source_context_clock_mismatch")
        meta = copy.deepcopy(dict(raw.get("meta") or {}))
        return MomentumRegimeContext(
            utc_iso=utc_iso,
            utc_hour=utc_hour,
            session_label=str(raw["session_label"]),
            vol_regime=VolatilityRegime(str(raw["volatility_regime"])),
            chop_expansion=ChopExpansionRegime(str(raw["chop_expansion"])),
            spread_regime=str(raw["spread_regime"]),
            fee_burden_regime=str(raw["fee_burden_regime"]),
            liquidity_regime=str(raw["liquidity_regime"]),
            exhaustion_cooldown=str(raw["exhaustion_cooldown"]),
            rolling_range_state=str(raw["rolling_range_state"]),
            breakout_continuity=str(raw["breakout_continuity"]),
            meta=meta,
        )
    except CapturedPaperSelectionSourceUnavailable:
        raise
    except (KeyError, TypeError, ValueError) as exc:
        raise CapturedPaperSelectionSourceUnavailable(
            "derived_source_regime_snapshot_invalid"
        ) from exc


def _features_from_snapshot(raw: Any) -> ExecutionReadinessFeatures:
    if not isinstance(raw, Mapping):
        _reject("derived_source_readiness_snapshot_invalid")
    meta = copy.deepcopy(dict(raw.get("extra") or {}))
    supported = {
        "spread_bps",
        "bid_ask_drift_bps",
        "book_imbalance",
        "ofi",
        "micro_price_edge",
        "trade_flow",
        "tape_velocity_z",
        "slippage_estimate_bps",
        "fee_to_target_ratio",
        "product_tradable",
        "float_rotation",
        "projected_rotation_at_eod",
    }
    merged = dict(meta)
    for name in supported:
        if name in raw:
            merged[name] = raw[name]
    try:
        return ExecutionReadinessFeatures.from_meta(merged)
    except (TypeError, ValueError) as exc:
        raise CapturedPaperSelectionSourceUnavailable(
            "derived_source_readiness_snapshot_invalid"
        ) from exc


def _variant_snapshot(row: MomentumStrategyVariant) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "family": str(row.family),
        "variant_key": str(row.variant_key),
        "version": int(row.version),
        "label": str(row.label),
        "params_json": copy.deepcopy(dict(row.params_json or {})),
        "is_active": bool(row.is_active),
        "execution_family": str(row.execution_family or ""),
        "parent_variant_id": row.parent_variant_id,
        "refinement_meta_json": copy.deepcopy(dict(row.refinement_meta_json or {})),
        "scan_pattern_id": row.scan_pattern_id,
        # PostgreSQL ``timestamp without time zone`` values arrive naive even
        # though the domain clock is UTC.  Normalize before content addressing
        # so the sealed payload never relies on JSON's treatment of naive time.
        "created_at": _utc(row.created_at, "source_variant_created_at"),
        "updated_at": _utc(row.updated_at, "source_variant_updated_at"),
        "variant_sha256": captured_paper_initial_variant_sha256(row),
    }


def _viability_snapshot(row: MomentumSymbolViability) -> dict[str, Any]:
    return {
        "id": int(row.id),
        "symbol": str(row.symbol),
        "scope": str(row.scope),
        "variant_id": int(row.variant_id),
        "viability_score": float(row.viability_score),
        "paper_eligible": bool(row.paper_eligible),
        "live_eligible": bool(row.live_eligible),
        "freshness_ts": _utc(row.freshness_ts, "source_viability_freshness"),
        "regime_snapshot_json": copy.deepcopy(dict(row.regime_snapshot_json or {})),
        "execution_readiness_json": copy.deepcopy(
            dict(row.execution_readiness_json or {})
        ),
        "explain_json": copy.deepcopy(dict(row.explain_json or {})),
        "evidence_window_json": copy.deepcopy(dict(row.evidence_window_json or {})),
        "source_node_id": row.source_node_id,
        "correlation_id": row.correlation_id,
        "created_at": _utc(row.created_at, "source_viability_created_at"),
        "updated_at": _utc(row.updated_at, "source_viability_updated_at"),
        "viability_sha256": captured_paper_initial_viability_sha256(row),
    }


@dataclass(frozen=True, slots=True)
class CapturedDerivedViabilitySnapshot:
    symbol: str
    source_variant_id: int
    target_variant_id: int
    family: MomentumStrategyFamily
    context: MomentumRegimeContext
    features: ExecutionReadinessFeatures
    settings: ViabilitySettingsProjection
    external: ViabilityExternalInputs
    post_score_adjustment: CapturedViabilityPostScoreAdjustment
    source_payload: Mapping[str, Any]
    source_fingerprint_sha256: str
    hub_snapshot_sha256: str
    event_at: datetime
    read_at: datetime
    correlation_id: str

    def __post_init__(self) -> None:
        symbol = str(self.symbol or "").strip().upper()
        if symbol != self.symbol or _SYMBOL_RE.fullmatch(symbol) is None:
            _reject("derived_source_symbol_invalid")
        if self.symbol.endswith("-USD"):
            _reject("derived_source_non_equity_forbidden")
        for name in ("source_variant_id", "target_variant_id"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                _reject(f"derived_source_{name}_invalid")
        if type(self.family) is not MomentumStrategyFamily:
            _reject("derived_source_family_invalid")
        if type(self.context) is not MomentumRegimeContext:
            _reject("derived_source_context_invalid")
        if type(self.features) is not ExecutionReadinessFeatures:
            _reject("derived_source_features_invalid")
        if type(self.settings) is not ViabilitySettingsProjection:
            _reject("derived_source_settings_invalid")
        if type(self.external) is not ViabilityExternalInputs:
            _reject("derived_source_external_invalid")
        if type(self.post_score_adjustment) is not CapturedViabilityPostScoreAdjustment:
            _reject("derived_source_post_score_invalid")
        event = _utc(self.event_at, "derived_source_event_at")
        read = _utc(self.read_at, "derived_source_read_at")
        if event > read:
            _reject("derived_source_clock_reversed")
        object.__setattr__(self, "event_at", event)
        object.__setattr__(self, "read_at", read)
        _sha(self.source_fingerprint_sha256, "source_fingerprint_sha256")
        _sha(self.hub_snapshot_sha256, "hub_snapshot_sha256")
        if sha256_json(self.source_payload) != self.source_fingerprint_sha256:
            _reject("derived_source_fingerprint_mismatch")


@dataclass(frozen=True, slots=True)
class CapturedViabilityQueueOccurrence:
    bundle: CapturedViabilityInputBundle
    scoring_authority: CapturedViabilityScoringAuthority
    source_events: tuple[CaptureEvent, ...]


class SqlAlchemyCapturedViabilitySnapshotSource:
    """Build exact scorer occurrences from a read-only canonical DB snapshot."""

    network_fallback_allowed = False
    network_access_allowed = True
    broker_access_allowed = False
    mutation_allowed = False

    def __init__(
        self,
        bind: Engine,
        *,
        variant_application: CapturedPaperVariantBindingApplication,
        selection_authority: CapturedPaperSelectionAuthority,
        settings_projection: ViabilitySettingsProjection,
        expected_account_id: str,
        activation_generation: str,
        policy_sha256: str,
        service_settings_projection_sha256: str,
        candidate_code_build_sha256: str,
        adaptive_policy_snapshot: Mapping[str, Any],
        code_build_payload: Mapping[str, Any],
        fundamentals_reader: Callable[[str], FundamentalsReceipt],
        context_max_age_seconds: float,
        tenbeat_entry_tilt_weight: float,
        wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ) -> None:
        if not isinstance(bind, Engine):
            _reject("derived_source_engine_invalid")
        if type(variant_application) is not CapturedPaperVariantBindingApplication:
            _reject("derived_source_variant_application_invalid")
        if type(selection_authority) is not CapturedPaperSelectionAuthority:
            _reject("derived_source_selection_authority_invalid")
        if type(settings_projection) is not ViabilitySettingsProjection:
            _reject("derived_source_settings_projection_invalid")
        if (
            selection_authority.expected_account_id != expected_account_id
            or selection_authority.activation_generation != activation_generation
            or selection_authority.policy_sha256 != policy_sha256
            or selection_authority.settings_projection_sha256
            != service_settings_projection_sha256
            or selection_authority.code_build_sha256
            != candidate_code_build_sha256
        ):
            _reject("derived_source_authority_mismatch")
        if (
            sha256_json(variant_application.body())
            != variant_application.application_sha256
        ):
            _reject("derived_source_variant_application_tampered")
        binding_authority = variant_application.plan.authority
        if not (
            binding_authority.expected_account_id == expected_account_id
            and binding_authority.activation_generation == activation_generation
            and binding_authority.policy_sha256 == policy_sha256
            and binding_authority.settings_projection_sha256
            == service_settings_projection_sha256
            and binding_authority.code_build_sha256
            == candidate_code_build_sha256
        ):
            _reject("derived_source_binding_authority_mismatch")
        _sha(policy_sha256, "policy_sha256")
        _sha(service_settings_projection_sha256, "settings_projection_sha256")
        _sha(candidate_code_build_sha256, "candidate_code_build_sha256")
        if not isinstance(adaptive_policy_snapshot, Mapping):
            _reject("adaptive_policy_snapshot_invalid")
        if not isinstance(code_build_payload, Mapping):
            _reject("candidate_code_build_payload_invalid")
        if not callable(fundamentals_reader) or not callable(wall_clock):
            _reject("derived_source_provider_capability_invalid")
        adaptive_policy_payload = copy.deepcopy(dict(adaptive_policy_snapshot))
        candidate_code_payload = copy.deepcopy(dict(code_build_payload))
        if sha256_json(adaptive_policy_payload) != policy_sha256:
            _reject("adaptive_policy_snapshot_hash_mismatch")
        if sha256_json(candidate_code_payload) != candidate_code_build_sha256:
            _reject("candidate_code_build_payload_hash_mismatch")
        max_age = _finite_positive(context_max_age_seconds, "context_max_age_seconds")
        if isinstance(tenbeat_entry_tilt_weight, bool) or not isinstance(
            tenbeat_entry_tilt_weight, (int, float)
        ):
            _reject("tenbeat_entry_tilt_weight_invalid")
        tenbeat_weight = float(tenbeat_entry_tilt_weight)
        if not math.isfinite(tenbeat_weight) or tenbeat_weight < 0.0:
            _reject("tenbeat_entry_tilt_weight_invalid")

        target_by_family = {
            item.family: item for item in selection_authority.variant_bindings
        }
        source_to_target: dict[int, tuple[int, str, str]] = {}
        for item in variant_application.items:
            selected = target_by_family.get(item.family)
            if not (
                selected is not None
                and selected.variant_id == item.target_variant_id
                and selected.variant_key == item.target_variant_key
                and selected.target_after_sha256 == item.target_after_sha256
            ):
                _reject("derived_source_variant_authority_mismatch")
            source_to_target[item.source_variant_id] = (
                item.target_variant_id,
                item.family,
                item.source_variant_sha256,
            )
        if not source_to_target or len(source_to_target) != len(
            selection_authority.variant_bindings
        ):
            _reject("derived_source_variant_set_incomplete")

        self._bind = bind
        self.variant_application = variant_application
        self.selection_authority = selection_authority
        self.settings_projection = settings_projection
        self.expected_account_id = str(expected_account_id)
        self.activation_generation = str(activation_generation)
        self.policy_sha256 = str(policy_sha256)
        self.service_settings_projection_sha256 = str(
            service_settings_projection_sha256
        )
        self.candidate_code_build_sha256 = str(candidate_code_build_sha256)
        self.context_max_age_seconds = max_age
        self.tenbeat_entry_tilt_weight = tenbeat_weight
        self.fundamentals_reader = fundamentals_reader
        self.wall_clock = wall_clock
        self._source_to_target = source_to_target
        self._last_hub_snapshot_sha256: str | None = None
        self._last_snapshots: tuple[CapturedDerivedViabilitySnapshot, ...] | None = None

        # Identity-stream payloads are the exact canonical objects whose hashes
        # the scorer consumes.  Do not wrap them in a second envelope: that
        # would create a different hash domain and make Replay/PAPER parity
        # impossible to prove.
        self._config_payload = copy.deepcopy(settings_projection.to_dict())
        self._feature_flags_payload = adaptive_policy_payload
        self._code_payload = candidate_code_payload
        config_sha = sha256_json(self._config_payload)
        feature_flags_sha = sha256_json(self._feature_flags_payload)
        code_sha = sha256_json(self._code_payload)
        account_sha = sha256_json(
            {
                "schema_version": ACCOUNT_SCHEMA_VERSION,
                "account_scope": "alpaca:paper",
                "expected_account_id": self.expected_account_id,
                "broker": "alpaca",
                "broker_environment": "paper",
            }
        )
        self.capture_identity = CaptureRunIdentity(
            run_id=self.activation_generation,
            # The durable queue owns generation 1.  Source events share the
            # activation UUID and authority hashes but use a distinct physical
            # generation so outer queue sequence numbers can never collide
            # with the four-event source envelope for each occurrence.
            generation=2,
            code_build_sha256=code_sha,
            config_sha256=config_sha,
            feature_flags_sha256=feature_flags_sha,
            account_identity_sha256=account_sha,
            broker="alpaca",
            broker_environment="paper",
        )

    @property
    def source_variant_ids(self) -> tuple[int, ...]:
        return tuple(sorted(self._source_to_target))

    @staticmethod
    def _hub_snapshot(db: Session) -> Mapping[str, Any]:
        row = (
            db.query(BrainNodeState)
            .filter(BrainNodeState.node_id == HUB_NODE_ID)
            .one_or_none()
        )
        state = dict(row.local_state or {}) if row is not None else {}
        symbols_raw = state.get("symbols_evaluated")
        regime = state.get("regime")
        correlation = str(state.get("correlation_id") or "").strip()
        tick_raw = state.get("last_tick_utc")
        # 2026-07-23 (a74 finding): the LIVE hub writer (live_runner ->
        # run_momentum_neural_tick) does not pass correlation_id, so the
        # production hub row ALWAYS carries an empty one -- only the
        # event-driven maybe_run_momentum_neural_tick path stamps it.
        # Requiring non-empty here rejected every real activation with
        # derived_source_hub_snapshot_invalid.  Empty is safe end to end: the
        # viability generation check compares equality (empty == empty), and
        # the per-row reader below already synthesizes a non-empty
        # `captured:<fingerprint>` correlation for the published events.
        if (
            row is None
            or not isinstance(symbols_raw, list)
            or not isinstance(regime, Mapping)
            or len(correlation) > 64
            or not isinstance(tick_raw, str)
        ):
            _reject("derived_source_hub_snapshot_invalid")
        all_symbols: list[str] = []
        for raw in symbols_raw:
            symbol = str(raw or "").strip().upper()
            if symbol != raw or _SYMBOL_RE.fullmatch(symbol) is None:
                _reject("derived_source_hub_symbol_invalid")
            all_symbols.append(symbol)
        if not all_symbols or len(all_symbols) != len(set(all_symbols)):
            _reject("derived_source_hub_universe_invalid")
        equity_symbols = tuple(
            sorted(symbol for symbol in all_symbols if not symbol.endswith("-USD"))
        )
        if not equity_symbols:
            _reject("derived_source_equity_universe_empty")
        tick_at = _parse_utc(tick_raw, "derived_source_hub_tick_at")
        body = {
            "schema_version": "chili.captured-paper-viability-hub-snapshot.v1",
            "node_id": HUB_NODE_ID,
            "correlation_id": correlation,
            "tick_at": tick_at,
            "equity_symbols": list(equity_symbols),
            "all_symbols": all_symbols,
            "regime": copy.deepcopy(dict(regime)),
            "last_activated_at": (
                _utc(row.last_activated_at, "derived_source_hub_activated_at")
                if row.last_activated_at is not None
                else None
            ),
            "updated_at": _utc(
                row.updated_at,
                "derived_source_hub_updated_at",
            ),
        }
        return {**body, "hub_snapshot_sha256": sha256_json(body)}

    def _probe_hub(self) -> Mapping[str, Any]:
        db = Session(bind=self._bind, expire_on_commit=False)
        try:
            db.execute(text(_READ_ONLY_TRANSACTION_SQL))
            return self._hub_snapshot(db)
        finally:
            try:
                db.rollback()
            finally:
                db.close()

    def _fundamentals_receipts(
        self,
        symbols: Sequence[str],
    ) -> Mapping[str, Mapping[str, Any]]:
        receipts: dict[str, Mapping[str, Any]] = {}
        for symbol in symbols:
            started_at = _utc(
                self.wall_clock(),
                "derived_source_fundamentals_started_at",
            )
            try:
                raw = self.fundamentals_reader(symbol)
            except Exception:
                raw = FundamentalsReceipt(
                    symbol=symbol,
                    status=FundamentalsReceiptStatus.UNAVAILABLE,
                    provider_state=FundamentalsProviderState.ERROR,
                    origin=FundamentalsReceiptOrigin.NONE,
                    observed_at=started_at,
                    cache_ttl_seconds=1.0,
                    reason="fundamentals_reader_exception",
                )
            returned_at = _utc(
                self.wall_clock(),
                "derived_source_fundamentals_returned_at",
            )
            if returned_at < started_at:
                _reject("derived_source_fundamentals_clock_reversed")
            if type(raw) is not FundamentalsReceipt or raw.symbol != symbol:
                raw = FundamentalsReceipt(
                    symbol=symbol,
                    status=FundamentalsReceiptStatus.UNAVAILABLE,
                    provider_state=FundamentalsProviderState.UNAVAILABLE,
                    origin=FundamentalsReceiptOrigin.NONE,
                    observed_at=returned_at,
                    cache_ttl_seconds=1.0,
                    reason="typed_fundamentals_receipt_missing_or_mismatched",
                )
            elif raw.observed_at > returned_at:
                raw = FundamentalsReceipt(
                    symbol=symbol,
                    status=FundamentalsReceiptStatus.UNAVAILABLE,
                    provider_state=FundamentalsProviderState.UNAVAILABLE,
                    origin=FundamentalsReceiptOrigin.NONE,
                    observed_at=returned_at,
                    cache_ttl_seconds=1.0,
                    reason="typed_fundamentals_receipt_from_future",
                )
            typed_receipt = copy.deepcopy(raw.to_dict())
            result = copy.deepcopy(dict(raw.data or {}))
            result_sha256 = sha256_json(result)
            short_name = result.get("short_name")
            if short_name is not None and not isinstance(short_name, str):
                _reject("derived_source_fundamentals_name_invalid")
            query = {
                "schema_version": FUNDAMENTALS_QUERY_SCHEMA_VERSION,
                "provider": FUNDAMENTALS_PROVIDER,
                "operation": "get_fundamentals",
                "symbol": symbol,
                "started_at": started_at,
                "returned_at": returned_at,
                "empty_result": raw.data is None,
                "result": result,
                "result_sha256": result_sha256,
                "typed_provider_receipt": typed_receipt,
                "typed_provider_receipt_sha256": sha256_json(typed_receipt),
                "classification_usable": raw.classification_usable,
                "classification_coverage_reason": (
                    None
                    if raw.classification_usable
                    else (
                        "fundamentals_"
                        f"{raw.status.value.lower()}_"
                        f"{raw.provider_state.value.lower()}"
                    )
                ),
                "cache_or_network_transport": raw.origin.value.lower(),
                "upstream_market_truth_certified": False,
            }
            receipts[symbol] = {
                **query,
                "query_receipt_sha256": sha256_json(query),
            }
        return receipts

    def read_snapshot(self) -> tuple[CapturedDerivedViabilitySnapshot, ...]:
        probe = self._probe_hub()
        probe_sha = _sha(
            probe.get("hub_snapshot_sha256"),
            "derived_source_hub_snapshot_sha256",
        )
        probe_now = _utc(self.wall_clock(), "derived_source_probe_at")
        probe_tick = _utc(probe["tick_at"], "derived_source_hub_tick_at")
        probe_age = (probe_now - probe_tick).total_seconds()
        if probe_age < 0.0 or probe_age > self.context_max_age_seconds:
            _reject("derived_source_hub_snapshot_stale")
        if self._last_hub_snapshot_sha256 == probe_sha:
            return ()

        symbols = tuple(str(value) for value in probe["equity_symbols"])
        fundamentals = self._fundamentals_receipts(symbols)
        db = Session(bind=self._bind, expire_on_commit=False)
        try:
            db.execute(text(_READ_ONLY_TRANSACTION_SQL))
            read_at = _utc(
                db.execute(text("SELECT transaction_timestamp()" )).scalar_one(),
                "derived_source_read_at",
            )
            hub = self._hub_snapshot(db)
            if hub.get("hub_snapshot_sha256") != probe_sha:
                _reject("derived_source_hub_changed_during_capture")
            if any(
                _utc(receipt["returned_at"], "fundamentals_returned_at") > read_at
                for receipt in fundamentals.values()
            ):
                _reject("derived_source_provider_result_from_future")
            tick_at = _utc(hub["tick_at"], "derived_source_hub_tick_at")
            hub_age = (read_at - tick_at).total_seconds()
            if hub_age < 0.0 or hub_age > self.context_max_age_seconds:
                _reject("derived_source_hub_snapshot_stale")
            variants = (
                db.query(MomentumStrategyVariant)
                .filter(MomentumStrategyVariant.id.in_(self.source_variant_ids))
                .order_by(MomentumStrategyVariant.id.asc())
                .all()
            )
            if len(variants) != len(self.source_variant_ids):
                _reject("derived_source_variant_unavailable")
            by_id = {int(row.id): row for row in variants}
            for source_id, (_target, family, source_sha) in self._source_to_target.items():
                row = by_id.get(source_id)
                if not (
                    row is not None
                    and bool(row.is_active)
                    and str(row.variant_key or "") == str(row.family or "") == family
                    and captured_paper_initial_variant_sha256(row) == source_sha
                ):
                    _reject("derived_source_variant_drift")
            rows = (
                db.query(MomentumSymbolViability)
                .filter(
                    MomentumSymbolViability.scope == "symbol",
                    MomentumSymbolViability.variant_id.in_(self.source_variant_ids),
                    MomentumSymbolViability.symbol.in_(symbols),
                )
                .order_by(
                    MomentumSymbolViability.symbol.asc(),
                    MomentumSymbolViability.variant_id.asc(),
                    MomentumSymbolViability.id.asc(),
                )
                .all()
            )
            expected_routes = {
                (symbol, source_id)
                for symbol in symbols
                for source_id in self.source_variant_ids
            }
            routes = {
                (str(row.symbol or ""), int(row.variant_id)) for row in rows
            }
            if routes != expected_routes or len(rows) != len(expected_routes):
                _reject("derived_source_family_universe_incomplete")
            snapshots: list[CapturedDerivedViabilitySnapshot] = []
            generation_freshness: datetime | None = None
            generation_regime_sha: str | None = None
            for viability in rows:
                symbol = str(viability.symbol or "").strip().upper()
                if symbol not in symbols or _SYMBOL_RE.fullmatch(symbol) is None:
                    _reject("derived_source_row_symbol_invalid")
                freshness = _utc(
                    viability.freshness_ts,
                    "derived_source_viability_freshness",
                )
                age = (read_at - freshness).total_seconds()
                if age < 0.0 or age > self.context_max_age_seconds:
                    _reject("derived_source_viability_stale")
                # 2026-07-23 (a75 finding): the LIVE hub bumps last_tick_utc on
                # EVERY tick, but a tick does not always rewrite every
                # viability row (measured live: rows 27s older than the hub
                # tick), and the regime json embeds a per-tick `utc_iso`, so
                # BOTH the exact `freshness != tick_at` equality AND the
                # row-vs-hub regime-sha pin rejected every real activation.
                # The generation semantics that matter: rows may lag the hub
                # within the bounded age (checked above) but must be one
                # SELF-CONSISTENT generation -- identical freshness and
                # identical regime sha ACROSS ROWS, never from the future,
                # same correlation and source node as the hub.
                row_regime_sha = sha256_json(
                    dict(viability.regime_snapshot_json or {})
                )
                if generation_freshness is None:
                    generation_freshness = freshness
                    generation_regime_sha = row_regime_sha
                if (
                    freshness > tick_at
                    or freshness != generation_freshness
                    or row_regime_sha != generation_regime_sha
                    or str(viability.correlation_id or "")
                    != str(hub["correlation_id"])
                    or str(viability.source_node_id or "") != HUB_NODE_ID
                ):
                    _reject("derived_source_viability_generation_mismatch")
                source_variant = by_id[int(viability.variant_id)]
                target_id, family_id, source_variant_sha = self._source_to_target[
                    int(source_variant.id)
                ]
                family = get_family(family_id)
                if family is None:
                    _reject("derived_source_family_unavailable")
                context = _context_from_snapshot(viability.regime_snapshot_json)
                event_at = _parse_utc(
                    context.utc_iso,
                    "derived_source_context_event_at",
                )
                context_age = (read_at - event_at).total_seconds()
                if context_age < 0.0 or context_age > self.context_max_age_seconds:
                    _reject("derived_source_context_stale")
                features = _features_from_snapshot(
                    viability.execution_readiness_json
                )
                fundamentals_receipt = fundamentals[symbol]
                fundamentals_result = dict(
                    fundamentals_receipt.get("result") or {}
                )
                short_name = fundamentals_result.get("short_name")
                classification_usable = (
                    fundamentals_receipt.get("classification_usable") is True
                )
                if classification_usable:
                    leveraged_etf = is_leveraged_etf_name(short_name)
                    excluded_fund = is_excluded_fund_name(short_name)
                else:
                    # These conservative placeholders are never scored: the
                    # occurrence carries an intersecting coverage gap below.
                    # They prevent any accidental fail-open inference while
                    # retaining the fixed typed scorer schema.
                    leveraged_etf = True
                    excluded_fund = True
                external = resolve_viability_external_inputs_for_capture(
                    symbol,
                    family,
                    context,
                    features,
                    db=db,
                    settings_projection=self.settings_projection,
                    leveraged_etf=leveraged_etf,
                    excluded_fund=excluded_fund,
                    decision_as_of=read_at,
                )
                post_score = CapturedViabilityPostScoreAdjustment(
                    tenbeat_entry_tilt_weight=self.tenbeat_entry_tilt_weight,
                    tenbeat_breakout_score=None,
                    lookup_status=(
                        "disabled"
                        if self.tenbeat_entry_tilt_weight == 0.0
                        else "inapplicable_non_crypto"
                    ),
                    source_read_id=None,
                )
                source_payload = {
                    "schema_version": SOURCE_SCHEMA_VERSION,
                    "source_authority": "derived_snapshot_only",
                    "upstream_raw_market_certification": "not_claimed",
                    "network_source_capture": "explicit_primary_query",
                    "account_scope": "alpaca:paper",
                    "expected_account_id": self.expected_account_id,
                    "activation_generation": self.activation_generation,
                    "selection_authority_sha256": (
                        self.selection_authority.authority_sha256
                    ),
                    "policy_sha256": self.policy_sha256,
                    "service_settings_projection_sha256": (
                        self.service_settings_projection_sha256
                    ),
                    "candidate_code_build_sha256": (
                        self.candidate_code_build_sha256
                    ),
                    "read_at": read_at,
                    "hub_snapshot": copy.deepcopy(dict(hub)),
                    "hub_snapshot_sha256": probe_sha,
                    "fundamentals_query_receipt": copy.deepcopy(
                        dict(fundamentals_receipt)
                    ),
                    "instrument_classification": {
                        "short_name": short_name,
                        "status": (
                            "available"
                            if classification_usable
                            else "coverage_unavailable"
                        ),
                        "coverage_reason": fundamentals_receipt.get(
                            "classification_coverage_reason"
                        ),
                        "leveraged_etf": (
                            leveraged_etf if classification_usable else None
                        ),
                        "excluded_fund": (
                            excluded_fund if classification_usable else None
                        ),
                        "scorer_placeholders_fail_closed": (
                            None
                            if classification_usable
                            else {
                                "leveraged_etf": True,
                                "excluded_fund": True,
                                "never_scored_without_coverage": True,
                            }
                        ),
                    },
                    "source_variant": _variant_snapshot(source_variant),
                    "source_viability": _viability_snapshot(viability),
                    "target_variant_id": target_id,
                    "family": {
                        "family_id": family.family_id,
                        "version": family.version,
                        "label": family.label,
                        "entry_style": family.entry_style,
                        "default_stop_logic": family.default_stop_logic,
                        "default_exit_logic": family.default_exit_logic,
                    },
                    "regime_context": context.to_public_dict(),
                    "execution_readiness": features.to_public_dict(),
                    "viability_settings_projection": (
                        self.settings_projection.to_dict()
                    ),
                    "resolved_external_inputs": external.to_dict(),
                    "post_score_adjustment": post_score.to_dict(),
                    "source_variant_sha256": source_variant_sha,
                }
                fingerprint = sha256_json(source_payload)
                correlation = str(viability.correlation_id or "").strip()
                if not correlation or len(correlation) > 64:
                    correlation = f"captured:{fingerprint[:55]}"
                snapshots.append(
                    CapturedDerivedViabilitySnapshot(
                        symbol=symbol,
                        source_variant_id=int(source_variant.id),
                        target_variant_id=target_id,
                        family=family,
                        context=context,
                        features=features,
                        settings=self.settings_projection,
                        external=external,
                        post_score_adjustment=post_score,
                        source_payload=source_payload,
                        source_fingerprint_sha256=fingerprint,
                        hub_snapshot_sha256=probe_sha,
                        event_at=event_at,
                        read_at=read_at,
                        correlation_id=correlation,
                    )
                )
            if not snapshots:
                _reject("derived_source_current_snapshot_empty")
            result = tuple(
                sorted(
                    snapshots,
                    key=lambda row: (
                        row.symbol,
                        row.source_variant_id,
                        row.source_fingerprint_sha256,
                    ),
                )
            )
            self._last_hub_snapshot_sha256 = probe_sha
            return result
        finally:
            try:
                db.rollback()
            finally:
                db.close()

    def build_occurrence(
        self,
        snapshot: CapturedDerivedViabilitySnapshot,
        *,
        source_sequence: int,
    ) -> CapturedViabilityQueueOccurrence:
        if type(snapshot) is not CapturedDerivedViabilitySnapshot:
            _reject("derived_source_snapshot_type_invalid")
        if type(source_sequence) is not int or source_sequence <= 0:
            _reject("derived_source_sequence_invalid")
        if (
            sha256_json(snapshot.source_payload)
            != snapshot.source_fingerprint_sha256
            or snapshot.source_payload.get("hub_snapshot_sha256")
            != snapshot.hub_snapshot_sha256
            or snapshot.source_payload.get("target_variant_id")
            != snapshot.target_variant_id
            or snapshot.source_payload.get("resolved_external_inputs")
            != snapshot.external.to_dict()
            or snapshot.source_payload.get("viability_settings_projection")
            != snapshot.settings.to_dict()
            or snapshot.source_payload.get("post_score_adjustment")
            != snapshot.post_score_adjustment.to_dict()
        ):
            _reject("derived_source_snapshot_material_drift")
        read_at = snapshot.read_at
        event_at = snapshot.event_at
        base = source_sequence * 4
        config_event = CaptureEvent(
            identity=self.capture_identity,
            sequence=base - 3,
            stream=CaptureStream.CONFIG_SNAPSHOT,
            clocks=CaptureClocks(received_at=read_at, available_at=read_at),
            payload=self._config_payload,
            provider=CONFIG_PROVIDER,
        )
        feature_flags_event = CaptureEvent(
            identity=self.capture_identity,
            sequence=base - 2,
            stream=CaptureStream.FEATURE_FLAG_SNAPSHOT,
            clocks=CaptureClocks(received_at=read_at, available_at=read_at),
            payload=self._feature_flags_payload,
            provider=FEATURE_FLAGS_PROVIDER,
        )
        code_event = CaptureEvent(
            identity=self.capture_identity,
            sequence=base - 1,
            stream=CaptureStream.CODE_BUILD,
            clocks=CaptureClocks(received_at=read_at, available_at=read_at),
            payload=self._code_payload,
            provider=CODE_PROVIDER,
        )
        source_query = {
            "schema_version": "chili.captured-paper-derived-viability-read.v1",
            "symbol": snapshot.symbol,
            "source_variant_id": snapshot.source_variant_id,
            "target_variant_id": snapshot.target_variant_id,
            "source_fingerprint_sha256": snapshot.source_fingerprint_sha256,
            "hub_snapshot_sha256": snapshot.hub_snapshot_sha256,
        }
        source_event = CaptureEvent(
            identity=self.capture_identity,
            sequence=base,
            stream=CaptureStream.CAPTURED_VIABILITY_INPUT,
            clocks=CaptureClocks(
                received_at=read_at,
                available_at=read_at,
                market_reference_at=event_at,
            ),
            payload=snapshot.source_payload,
            provider=SOURCE_PROVIDER,
            symbol=snapshot.symbol,
            query=source_query,
        )
        events = (
            config_event,
            feature_flags_event,
            code_event,
            source_event,
        )
        refs = tuple(CaptureEventRef.from_event(event) for event in events)
        source_ref = refs[-1]
        read_id = str(
            uuid.uuid5(
                uuid.UUID(self.activation_generation),
                f"{source_sequence}:{snapshot.source_fingerprint_sha256}",
            )
        )
        fundamentals_receipt = snapshot.source_payload.get(
            "fundamentals_query_receipt"
        )
        if not isinstance(fundamentals_receipt, Mapping):
            _reject("derived_source_fundamentals_receipt_missing")
        classification = snapshot.source_payload.get("instrument_classification")
        if not isinstance(classification, Mapping):
            _reject("derived_source_instrument_classification_missing")
        classification_status = str(classification.get("status") or "")
        if classification_status not in {"available", "coverage_unavailable"}:
            _reject("derived_source_instrument_classification_invalid")
        classification_gap: CoverageGap | None = None
        if classification_status == "coverage_unavailable":
            gap_reason = str(classification.get("coverage_reason") or "").strip()
            if not gap_reason:
                _reject("derived_source_fundamentals_coverage_reason_missing")
            classification_gap = CoverageGap(
                stream=CaptureStream.CAPTURED_VIABILITY_INPUT,
                reason=gap_reason,
                first_available_at=read_at,
                last_available_at=read_at,
                lost_count=1,
                symbol=snapshot.symbol,
            )
        read_receipt = CaptureReadReceipt(
            read_id=read_id,
            decision_id=(
                f"captured-paper-selection:{self.activation_generation}:"
                f"{source_sequence}"
            ),
            identity_sha256=self.capture_identity.identity_sha256,
            stream=CaptureStream.CAPTURED_VIABILITY_INPUT,
            provider=SOURCE_PROVIDER,
            symbol=snapshot.symbol,
            requested_at=_utc(
                fundamentals_receipt.get("started_at"),
                "derived_source_read_requested_at",
            ),
            returned_at=read_at,
            query_sha256=source_event.query_sha256 or "",
            source_event_sha256s=(source_ref.event_sha256,),
            empty_result=False,
            result_sha256=captured_read_result_sha256((source_ref,)),
            content_verified=True,
            replay_network_fallback_used=False,
            query=source_query,
        )
        read_receipt_sha256 = captured_viability_read_receipt_sha256(
            read_receipt
        )
        max_age = self.context_max_age_seconds
        profile = FSMDependencyProfile(
            required_streams=frozenset(
                {
                    CaptureStream.CONFIG_SNAPSHOT,
                    CaptureStream.FEATURE_FLAG_SNAPSHOT,
                    CaptureStream.CODE_BUILD,
                    CaptureStream.CAPTURED_VIABILITY_INPUT,
                }
            ),
            required_read_ids=(read_id,),
            stream_dependencies=(
                FSMStreamDependency(
                    stream=CaptureStream.CONFIG_SNAPSHOT,
                    exact_provider_event_at_required=False,
                    market_reference_at_required=False,
                    max_source_age_seconds=max_age,
                    coverage_start_at=read_at,
                ),
                FSMStreamDependency(
                    stream=CaptureStream.FEATURE_FLAG_SNAPSHOT,
                    exact_provider_event_at_required=False,
                    market_reference_at_required=False,
                    max_source_age_seconds=max_age,
                    coverage_start_at=read_at,
                ),
                FSMStreamDependency(
                    stream=CaptureStream.CODE_BUILD,
                    exact_provider_event_at_required=False,
                    market_reference_at_required=False,
                    max_source_age_seconds=max_age,
                    coverage_start_at=read_at,
                ),
                FSMStreamDependency(
                    stream=CaptureStream.CAPTURED_VIABILITY_INPUT,
                    exact_provider_event_at_required=False,
                    market_reference_at_required=True,
                    max_source_age_seconds=max_age,
                    coverage_start_at=read_at,
                ),
            ),
        )
        coverages = (
            StreamCoverage(
                stream=CaptureStream.CONFIG_SNAPSHOT,
                identity_sha256=self.capture_identity.identity_sha256,
                provider=CONFIG_PROVIDER,
                first_available_at=read_at,
                last_available_at=read_at,
                event_count=1,
                exact_event_clock_complete=False,
                content_verified=True,
                continuity_complete=True,
            ),
            StreamCoverage(
                stream=CaptureStream.FEATURE_FLAG_SNAPSHOT,
                identity_sha256=self.capture_identity.identity_sha256,
                provider=FEATURE_FLAGS_PROVIDER,
                first_available_at=read_at,
                last_available_at=read_at,
                event_count=1,
                exact_event_clock_complete=False,
                content_verified=True,
                continuity_complete=True,
            ),
            StreamCoverage(
                stream=CaptureStream.CODE_BUILD,
                identity_sha256=self.capture_identity.identity_sha256,
                provider=CODE_PROVIDER,
                first_available_at=read_at,
                last_available_at=read_at,
                event_count=1,
                exact_event_clock_complete=False,
                content_verified=True,
                continuity_complete=True,
            ),
            StreamCoverage(
                stream=CaptureStream.CAPTURED_VIABILITY_INPUT,
                identity_sha256=self.capture_identity.identity_sha256,
                provider=SOURCE_PROVIDER,
                symbol=snapshot.symbol,
                first_available_at=read_at,
                last_available_at=read_at,
                event_count=1,
                exact_event_clock_complete=False,
                content_verified=True,
                continuity_complete=True,
            ),
        )
        roots = captured_viability_component_sha256s(
            symbol=snapshot.symbol,
            variant_id=snapshot.target_variant_id,
            family=snapshot.family,
            context=snapshot.context,
            features=snapshot.features,
            settings=snapshot.settings,
            external=snapshot.external,
            post_score_adjustment=snapshot.post_score_adjustment,
            event_at=event_at,
            available_at=read_at,
            read_at=read_at,
            capture_identity_sha256=self.capture_identity.identity_sha256,
            policy_sha256=self.policy_sha256,
            config_sha256=config_event.payload_sha256,
            code_sha256=code_event.payload_sha256,
        )
        event_hashes = tuple(event.event_sha256 for event in events)
        inventory = CapturedViabilityDependencyInventory(
            dependency_profile=profile,
            bindings=tuple(
                CapturedViabilityDependencyBinding(
                    component=component,
                    component_sha256=roots[component],
                    source_event_sha256s=event_hashes,
                    read_receipt_sha256s=(read_receipt_sha256,),
                )
                for component in REQUIRED_COMPONENTS
            ),
        )
        bundle = CapturedViabilityInputBundle(
            source_sequence=source_sequence,
            event_at=event_at,
            available_at=read_at,
            read_at=read_at,
            symbol=snapshot.symbol,
            variant_id=snapshot.target_variant_id,
            family=snapshot.family,
            context=snapshot.context,
            features=snapshot.features,
            settings=snapshot.settings,
            external=snapshot.external,
            post_score_adjustment=snapshot.post_score_adjustment,
            capture_identity_sha256=self.capture_identity.identity_sha256,
            policy_sha256=self.policy_sha256,
            config_sha256=config_event.payload_sha256,
            code_sha256=code_event.payload_sha256,
            dependency_inventory=inventory,
            source_refs=refs,
            read_receipts=(read_receipt,),
            stream_coverages=coverages,
            coverage_gaps=(
                () if classification_gap is None else (classification_gap,)
            ),
            correlation_id=snapshot.correlation_id,
        )
        scoring_authority = CapturedViabilityScoringAuthority(
            capture_identity_sha256=self.capture_identity.identity_sha256,
            policy_sha256=self.policy_sha256,
            config_sha256=config_event.payload_sha256,
            code_sha256=code_event.payload_sha256,
            settings_projection_sha256=bundle.settings_projection_sha256,
            family_sha256=bundle.component_roots["family"],
            dependency_profile_sha256=profile.profile_sha256,
            variant_id=snapshot.target_variant_id,
            family_id=snapshot.family.family_id,
            family_version=snapshot.family.version,
            activation_policy_sha256=(
                self.selection_authority.policy_sha256
            ),
            activation_settings_projection_sha256=(
                self.selection_authority.settings_projection_sha256
            ),
            activation_code_build_sha256=(
                self.selection_authority.code_build_sha256
            ),
            selection_authority_sha256=(
                self.selection_authority.authority_sha256
            ),
        )
        return CapturedViabilityQueueOccurrence(
            bundle=bundle,
            scoring_authority=scoring_authority,
            source_events=events,
        )


__all__ = [
    "CapturedDerivedViabilitySnapshot",
    "CapturedPaperSelectionSourceUnavailable",
    "CapturedViabilityQueueOccurrence",
    "SqlAlchemyCapturedViabilitySnapshotSource",
]
