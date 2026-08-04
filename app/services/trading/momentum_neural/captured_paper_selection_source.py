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
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any, Callable, Mapping, Sequence

from sqlalchemy import func, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session

from app.models.captured_paper_selection_frontier import (
    CapturedPaperSelectionRouteState,
)
from app.models.trading import (
    BrainNodeState,
    MomentumOrtexRequestAttempt,
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
    CaptureContractError,
    CaptureEvent,
    CaptureEventRef,
    CaptureOrtexSelectionSnapshot,
    CaptureReadReceipt,
    CaptureRunIdentity,
    CaptureStream,
    CoverageGap,
    FSMDependencyProfile,
    FSMStreamDependency,
    StreamCoverage,
    ORTEX_SNAPSHOT_PROVIDER,
    ValidatedOrtexSqueezeFuelBatchManifest,
    bind_ortex_squeeze_fuel_batch_reference,
    captured_read_result_sha256,
    sha256_json,
    validate_ortex_squeeze_fuel_batch_manifest,
    validate_ortex_squeeze_fuel_batch_reference,
    validate_ortex_selection_endpoint_discovery_chain,
    validate_ortex_selection_batch,
)
from .short_mechanics import ortex_public_policy
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
    "chili.captured-paper-fundamentals-query.v2"
)
ORTEX_BATCH_STATUS_KEY = "ortex_squeeze_fuel_batch"

# 2026-07-23 (a79 finding): the hub is the CRYPTO intel node and its
# symbols_evaluated intermittently contains crypto pairs like BTC-USD -- which
# this module itself expects (equity_symbols filters `endswith("-USD")` right
# after per-symbol validation).  The old charset forbade "-", so any tick with
# a crypto symbol present rejected derived_source_hub_symbol_invalid before
# the split could run.  Allow hyphens, matching the reader's own design.
_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,35}$")
_SHA_RE = re.compile(r"^[0-9a-f]{64}$")
# Bound on the viability-driven admission-universe probe (fundamentals are
# fetched per admitted symbol, ~1 network call each, so the union must stay
# small).  Newest-complete symbols win.
_VIABILITY_UNIVERSE_CAP = 24
_READ_ONLY_TRANSACTION_SQL = (
    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
)


class CapturedPaperSelectionSourceUnavailable(RuntimeError):
    def __init__(self, reason: str) -> None:
        self.reason = str(reason or "captured_selection_source_unavailable")
        super().__init__(self.reason)


def _reject(reason: str) -> None:
    raise CapturedPaperSelectionSourceUnavailable(reason)


def _finite_json(value: Any) -> Any:
    """Map non-finite floats (NaN/Infinity) to None, recursively.

    2026-07-24 (a86-0948): provider fundamentals data (yfinance-shaped) can
    legitimately carry NaN/Infinity floats for missing fields.  Canonical JSON
    cannot represent them at all -- ``canonical_json_bytes`` raises ValueError
    ("Out of range float values are not JSON compliant"), which killed the
    whole fenced start on the first symbol whose fundamentals held a NaN.
    None is the honest JSON encoding of "provider had no finite value here";
    the receipt still hashes deterministically and downstream consumers treat
    missing and non-finite identically.
    """
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if isinstance(value, dict):
        return {key: _finite_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_finite_json(item) for item in value]
    return value


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


@dataclass(frozen=True, slots=True)
class _OrtexSelectionEvidence:
    affected: bool
    snapshot: CaptureOrtexSelectionSnapshot | None
    coverage_reason: str | None


def _ortex_global_batch_inventory(
    db: Session,
    feature_meta: Mapping[str, Any],
    *,
    required_symbol: str,
    read_at: datetime,
    public_policy_sha256: str,
    freshness_ttl_seconds: float,
    manifest_cache: dict[
        str,
        ValidatedOrtexSqueezeFuelBatchManifest,
    ],
) -> tuple[Mapping[str, Mapping[str, Any]] | None, str | None, str | None]:
    """Resolve a compact row receipt to the exact full hub manifest.

    Scheduler persistence is intentionally chunked while Ortex percentile
    ranks are computed against the complete field.  The full manifest is
    stored once on ``BrainNodeState(HUB_NODE_ID)``; each viability row carries
    only a compact content-addressed pointer plus its chunk-local signal.  Both
    rows are read inside the caller's existing repeatable-read, read-only
    transaction.  Durable provider-attempt rows are still reconstructed below
    before any member becomes economic evidence.
    """

    raw_reference = feature_meta.get(ORTEX_BATCH_STATUS_KEY)
    if not isinstance(raw_reference, Mapping):
        return None, None, "ortex_selection_batch_reference_missing"
    try:
        typed_reference = validate_ortex_squeeze_fuel_batch_reference(
            raw_reference,
            read_at=read_at,
            expected_quota_policy_sha256=public_policy_sha256,
        )
    except CaptureContractError:
        return None, None, "ortex_selection_batch_reference_invalid"

    typed_manifest = manifest_cache.get(typed_reference.batch_sha256)
    if typed_manifest is None:
        hub_row = (
            db.query(BrainNodeState)
            .filter(BrainNodeState.node_id == HUB_NODE_ID)
            .one_or_none()
        )
        if hub_row is None or not isinstance(hub_row.local_state, Mapping):
            return (
                None,
                typed_reference.batch_sha256,
                "ortex_selection_batch_hub_missing",
            )
        raw_manifest = hub_row.local_state.get(ORTEX_BATCH_STATUS_KEY)
        if not isinstance(raw_manifest, Mapping):
            return (
                None,
                typed_reference.batch_sha256,
                "ortex_selection_batch_hub_missing",
            )
        try:
            typed_manifest = validate_ortex_squeeze_fuel_batch_manifest(
                raw_manifest,
                read_at=read_at,
                expected_quota_policy_sha256=public_policy_sha256,
                freshness_ttl_seconds=freshness_ttl_seconds,
            )
        except CaptureContractError as exc:
            reason = str(exc).lower()
            if "stale" in reason:
                typed_reason = "ortex_selection_batch_hub_stale"
            elif "future" in reason:
                typed_reason = "ortex_selection_batch_hub_from_future"
            else:
                typed_reason = "ortex_selection_batch_hub_invalid"
            return None, typed_reference.batch_sha256, typed_reason
        manifest_cache[typed_manifest.batch_sha256] = typed_manifest
    try:
        bound_manifest = bind_ortex_squeeze_fuel_batch_reference(
            typed_reference,
            typed_manifest,
        )
    except CaptureContractError:
        return (
            None,
            typed_reference.batch_sha256,
            "ortex_selection_batch_hub_reference_mismatch",
        )
    if not bound_manifest.complete:
        return (
            None,
            bound_manifest.batch_sha256,
            "ortex_selection_batch_coverage_unavailable",
        )
    inventory = bound_manifest.signal_by_symbol
    if required_symbol not in inventory:
        return (
            None,
            bound_manifest.batch_sha256,
            "ortex_selection_batch_symbol_missing",
        )
    return inventory, bound_manifest.batch_sha256, None


def _ortex_row_signal_matches_global_member(
    signal: Mapping[str, Any],
    member: Mapping[str, Any],
) -> bool:
    """Require the persisted chunk projection to equal its global member."""

    if (
        signal.get("ortex_selection_reference")
        != member.get("ortex_selection_reference")
    ):
        return False
    for field_name in ("squeeze_fuel_pct", "squeeze_fuel_rank_pct"):
        current = signal.get(field_name)
        expected = member.get(field_name)
        if current is None or expected is None:
            if current is not expected:
                return False
            continue
        if (
            isinstance(current, bool)
            or isinstance(expected, bool)
            or not isinstance(current, (int, float))
            or not isinstance(expected, (int, float))
            or not math.isfinite(float(current))
            or not math.isfinite(float(expected))
            or float(current) != float(expected)
        ):
            return False
    return True


def _ortex_selection_evidence(
    features: ExecutionReadinessFeatures,
    *,
    batch: Mapping[str, Any] | None,
    affected: bool,
    preflight_reason: str | None,
    symbol: str,
    read_at: datetime,
) -> _OrtexSelectionEvidence:
    if not affected:
        return _OrtexSelectionEvidence(False, None, None)
    if batch is None:
        return _OrtexSelectionEvidence(
            True,
            None,
            str(preflight_reason or "ortex_selection_batch_unavailable"),
        )
    meta = features.meta if isinstance(features.meta, Mapping) else {}
    ross_signals = meta.get("ross_signals")
    if not isinstance(ross_signals, Mapping):
        return _OrtexSelectionEvidence(
            True,
            None,
            "ortex_selection_signal_inventory_missing",
        )
    signal = ross_signals.get(symbol)
    if not isinstance(signal, Mapping):
        return _OrtexSelectionEvidence(
            True,
            None,
            "ortex_selection_ranked_signal_missing",
        )
    rank_present = signal.get("squeeze_fuel_rank_pct") is not None
    try:
        typed = validate_ortex_selection_batch(
            batch,
            ranked_symbol=symbol,
            expected_rank_pct=(
                None
                if signal.get("squeeze_fuel_rank_pct") is None
                else float(signal["squeeze_fuel_rank_pct"])
            ),
        )
    except (CaptureContractError, TypeError, ValueError):
        return _OrtexSelectionEvidence(
            True,
            None,
            "ortex_selection_snapshot_invalid",
        )
    if typed.returned_at > read_at:
        return _OrtexSelectionEvidence(
            True,
            None,
            "ortex_selection_snapshot_from_future",
        )
    source_age = (read_at - typed.source_received_at).total_seconds()
    if source_age < 0.0:
        return _OrtexSelectionEvidence(
            True,
            None,
            "ortex_selection_source_clock_from_future",
        )
    if source_age > typed.success_cache_ttl_seconds:
        return _OrtexSelectionEvidence(
            True,
            None,
            "ortex_selection_source_stale",
        )
    if typed.outcome == "SUCCESS":
        if not typed.complete or typed.rank_pct is None or not rank_present:
            return _OrtexSelectionEvidence(
                True,
                None,
                "ortex_selection_success_rank_incomplete",
            )
    elif typed.outcome in {
        "AUTHORITATIVE_EMPTY",
        "NOT_APPLICABLE",
        "UNSUPPORTED_SYMBOL_OR_EXCHANGE",
    }:
        if not typed.complete or typed.rank_pct is not None or rank_present:
            return _OrtexSelectionEvidence(
                True,
                None,
                "ortex_selection_neutral_rank_mismatch",
            )
    else:
        return _OrtexSelectionEvidence(
            True,
            None,
            f"ortex_selection_outcome_unavailable:{typed.outcome.lower()}",
        )
    return _OrtexSelectionEvidence(True, typed, None)


def _materialize_ortex_selection_batch(
    db: Session,
    ross_signals: Mapping[str, Any],
    *,
    read_at: datetime,
    public_policy: Mapping[str, Any],
    public_policy_sha256: str,
) -> tuple[Mapping[str, Any] | None, str | None]:
    """Resolve compact viability refs to full response bytes in one DB snapshot."""

    outer_expected = {
        "schema",
        "version",
        "outcome_capture_sha256",
        "kind",
        "symbol",
        "requested_exchange",
        "resolved_exchange",
        "short_interest_pct",
        "cost_to_borrow",
        "utilization",
        "is_easy_to_borrow",
        "provider_event_at",
        "effective_at",
        "received_at",
        "available_at",
        "detail_code",
        "quota_policy_sha256",
        "cache_origin_sha256",
        "cache_origin_received_at",
        "cache_origin_available_at",
        "endpoint_refs",
        "selection_reference_sha256",
    }
    endpoint_expected = {
        "dataset",
        "kind",
        "exchange",
        "attempt_id",
        "request_sha256",
        "raw_response_sha256",
        "endpoint_capture_sha256",
        "selected_row_sha256",
        "http_status",
        "provider_event_at",
        "effective_at",
        "received_at",
        "available_at",
        "value",
    }
    refs: dict[str, Mapping[str, Any]] = {}
    attempt_ids: set[uuid.UUID] = set()
    for raw_symbol, raw_signal in sorted(ross_signals.items()):
        symbol = str(raw_symbol or "").strip().upper()
        if not isinstance(raw_signal, Mapping):
            continue
        raw_ref = raw_signal.get("ortex_selection_reference")
        if raw_ref is None:
            return None, "ortex_selection_reference_missing"
        if (
            not isinstance(raw_ref, Mapping)
            or set(raw_ref) != outer_expected
            or raw_ref.get("schema") != "chili.ortex.selection-reference.v1"
            or raw_ref.get("version") != 1
            or raw_ref.get("symbol") != symbol
        ):
            return None, "ortex_selection_reference_invalid"
        reference_body = {
            key: value
            for key, value in raw_ref.items()
            if key != "selection_reference_sha256"
        }
        if raw_ref.get("selection_reference_sha256") != sha256_json(
            reference_body
        ):
            return None, "ortex_selection_reference_hash_mismatch"
        if raw_ref.get("quota_policy_sha256") != public_policy_sha256:
            return None, "ortex_selection_policy_mismatch"
        try:
            _sha(
                raw_ref.get("outcome_capture_sha256"),
                "ortex_outcome_capture_sha256",
            )
        except CapturedPaperSelectionSourceUnavailable:
            return None, "ortex_selection_outcome_hash_invalid"
        endpoint_refs = raw_ref.get("endpoint_refs")
        if not isinstance(endpoint_refs, list):
            return None, "ortex_selection_endpoint_refs_invalid"
        for endpoint_ref in endpoint_refs:
            if (
                not isinstance(endpoint_ref, Mapping)
                or set(endpoint_ref) != endpoint_expected
            ):
                return None, "ortex_selection_endpoint_ref_invalid"
            dataset = str(endpoint_ref.get("dataset") or "")
            if dataset not in {"short_interest", "cost_to_borrow"}:
                return None, "ortex_selection_endpoint_dataset_invalid"
            try:
                attempt_id = uuid.UUID(
                    str(endpoint_ref.get("attempt_id") or "")
                )
            except ValueError:
                return None, "ortex_selection_attempt_id_invalid"
            for hash_field in (
                "request_sha256",
                "endpoint_capture_sha256",
            ):
                try:
                    _sha(
                        endpoint_ref.get(hash_field),
                        f"ortex_{hash_field}",
                    )
                except CapturedPaperSelectionSourceUnavailable:
                    return None, "ortex_selection_endpoint_hash_invalid"
            raw_response_sha256 = endpoint_ref.get("raw_response_sha256")
            if raw_response_sha256 is not None:
                try:
                    _sha(
                        raw_response_sha256,
                        "ortex_raw_response_sha256",
                    )
                except CapturedPaperSelectionSourceUnavailable:
                    return None, "ortex_selection_response_hash_invalid"
            attempt_ids.add(attempt_id)
        try:
            validate_ortex_selection_endpoint_discovery_chain(
                endpoint_refs,
                symbol=symbol,
                aggregate_kind=str(raw_ref.get("kind") or ""),
                requested_exchange=raw_ref.get("requested_exchange"),
                resolved_exchange=raw_ref.get("resolved_exchange"),
                expected_quota_policy_sha256=public_policy_sha256,
            )
        except CaptureContractError:
            return None, "ortex_selection_endpoint_discovery_invalid"
        refs[symbol] = copy.deepcopy(dict(raw_ref))

    if not refs:
        return None, None
    records = (
        db.query(MomentumOrtexRequestAttempt)
        .filter(MomentumOrtexRequestAttempt.attempt_id.in_(attempt_ids))
        .order_by(
            MomentumOrtexRequestAttempt.bundle_sha256.asc(),
            MomentumOrtexRequestAttempt.bundle_index.asc(),
            MomentumOrtexRequestAttempt.attempt_id.asc(),
        )
        .all()
        if attempt_ids
        else []
    )
    records_by_id = {str(row.attempt_id): row for row in records}
    if len(records_by_id) != len(attempt_ids):
        return None, "ortex_selection_attempt_rows_missing"

    try:
        from .short_mechanics import (
            OrtexShortMechanicsOutcome,
            ortex_outcome_from_completed_attempts,
        )
    except ImportError:
        return None, "ortex_selection_reconstructor_unavailable"

    members: list[dict[str, Any]] = []
    unusable_kind: str | None = None
    for symbol, reference in refs.items():
        endpoint_refs = reference["endpoint_refs"]
        selected_records = [
            records_by_id[str(endpoint_ref["attempt_id"])]
            for endpoint_ref in endpoint_refs
        ]
        try:
            observed_at = _parse_utc(
                reference.get("available_at"),
                "ortex_selection_observed_at",
            )
            if reference.get("kind") == "NOT_APPLICABLE":
                if endpoint_refs or selected_records:
                    return None, "ortex_not_applicable_attempts_present"
                outcome = OrtexShortMechanicsOutcome.not_applicable(
                    symbol=symbol,
                    reason=str(reference.get("detail_code") or ""),
                    observed_at=observed_at,
                    policy_sha256=public_policy_sha256,
                    policy=public_policy,
                )
            else:
                durable_outcome = ortex_outcome_from_completed_attempts(
                    symbol=symbol,
                    requested_exchange=reference.get("requested_exchange"),
                    records=selected_records,
                    policy=public_policy,
                    observed_at=observed_at,
                )
                durable_ref = durable_outcome.to_selection_reference_dict()
                stable_fields = (
                    "kind",
                    "symbol",
                    "requested_exchange",
                    "resolved_exchange",
                    "short_interest_pct",
                    "cost_to_borrow",
                    "utilization",
                    "is_easy_to_borrow",
                    "provider_event_at",
                    "effective_at",
                    "quota_policy_sha256",
                    "endpoint_refs",
                )
                if any(
                    durable_ref.get(field) != reference.get(field)
                    for field in stable_fields
                ):
                    return None, "ortex_selection_reference_row_mismatch"
                outcome = OrtexShortMechanicsOutcome(
                    kind=durable_outcome.kind,
                    symbol=durable_outcome.symbol,
                    requested_exchange=durable_outcome.requested_exchange,
                    resolved_exchange=durable_outcome.resolved_exchange,
                    endpoints=durable_outcome.endpoints,
                    short_interest_pct=durable_outcome.short_interest_pct,
                    cost_to_borrow=durable_outcome.cost_to_borrow,
                    utilization=durable_outcome.utilization,
                    is_easy_to_borrow=durable_outcome.is_easy_to_borrow,
                    provider_event_at=durable_outcome.provider_event_at,
                    effective_at=durable_outcome.effective_at,
                    received_at=_parse_utc(
                        reference.get("received_at"),
                        "ortex_selection_received_at",
                    ),
                    available_at=observed_at,
                    detail_code=str(reference.get("detail_code") or ""),
                    policy=public_policy,
                    quota_policy_sha256=public_policy_sha256,
                    cache_origin_sha256=reference.get(
                        "cache_origin_sha256"
                    ),
                    cache_origin_received_at=(
                        None
                        if reference.get("cache_origin_received_at") is None
                        else _parse_utc(
                            reference.get("cache_origin_received_at"),
                            "ortex_cache_origin_received_at",
                        )
                    ),
                    cache_origin_available_at=(
                        None
                        if reference.get("cache_origin_available_at") is None
                        else _parse_utc(
                            reference.get("cache_origin_available_at"),
                            "ortex_cache_origin_available_at",
                        )
                    ),
                )
            reconstructed_ref = outcome.to_selection_reference_dict()
        except (KeyError, TypeError, ValueError):
            return None, "ortex_selection_attempt_reconstruction_failed"
        if reconstructed_ref != reference:
            return None, "ortex_selection_reference_row_mismatch"
        signal = ross_signals[symbol]
        mechanics = outcome.to_capture_dict()
        members.append(
            {
                "symbol": symbol,
                "short_mechanics": mechanics,
                "short_mechanics_sha256": sha256_json(mechanics),
                "squeeze_fuel_pct": signal.get("squeeze_fuel_pct"),
                "rank_pct": signal.get("squeeze_fuel_rank_pct"),
            }
        )
        if outcome.kind.value not in {
            "SUCCESS",
            "AUTHORITATIVE_EMPTY",
            "NOT_APPLICABLE",
            "UNSUPPORTED_SYMBOL_OR_EXCHANGE",
        }:
            unusable_kind = outcome.kind.value.lower()

    if unusable_kind is not None:
        return None, f"ortex_selection_outcome_unavailable:{unusable_kind}"
    members.sort(key=lambda row: row["symbol"])
    batch = {
        "schema_version": "chili.ortex-selection-snapshot.v1",
        "members": members,
        "members_sha256": sha256_json(members),
        "complete": True,
    }
    try:
        # Validate every member as a possible ranked decision so no malformed
        # equity sibling can hide behind the currently selected symbol.
        # Non-equity policy members are still fully parsed by every validation
        # call, but cannot themselves be a ranked decision (the typed capture
        # contract intentionally rejects ``*-USD`` as ``expected_symbol``).
        for member in members:
            if member["symbol"].endswith("-USD"):
                continue
            validate_ortex_selection_batch(
                batch,
                ranked_symbol=member["symbol"],
                expected_rank_pct=member["rank_pct"],
            )
    except (CaptureContractError, TypeError, ValueError):
        return None, "ortex_selection_batch_invalid"
    if _utc(read_at, "ortex_selection_read_at") < max(
        _parse_utc(reference["available_at"], "ortex_selection_available_at")
        for reference in refs.values()
    ):
        return None, "ortex_selection_reference_from_future"
    return batch, None


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
    ortex_selection_batch: Mapping[str, Any] | None
    ortex_selection_affected: bool
    ortex_coverage_reason: str | None
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
        if type(self.ortex_selection_affected) is not bool:
            _reject("derived_source_ortex_affected_invalid")
        if self.ortex_selection_batch is not None:
            if not isinstance(self.ortex_selection_batch, Mapping):
                _reject("derived_source_ortex_batch_invalid")
            if self.ortex_coverage_reason is not None:
                _reject("derived_source_ortex_batch_reason_conflict")
            expected_batch_sha = self.source_payload.get(
                "ortex_selection_batch_sha256"
            )
            if expected_batch_sha != sha256_json(self.ortex_selection_batch):
                _reject("derived_source_ortex_batch_hash_mismatch")
        elif self.ortex_selection_affected:
            if not str(self.ortex_coverage_reason or "").strip():
                _reject("derived_source_ortex_coverage_reason_missing")
        elif (
            self.ortex_coverage_reason is not None
            or self.source_payload.get("ortex_selection_batch_sha256") is not None
        ):
            _reject("derived_source_ortex_unaffected_material_invalid")
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
        ortex_public_policy: Mapping[str, Any] | None,
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
        if ortex_public_policy is not None and not isinstance(
            ortex_public_policy, Mapping
        ):
            _reject("ortex_public_policy_invalid")
        if not callable(fundamentals_reader) or not callable(wall_clock):
            _reject("derived_source_provider_capability_invalid")
        adaptive_policy_payload = copy.deepcopy(dict(adaptive_policy_snapshot))
        candidate_code_payload = copy.deepcopy(dict(code_build_payload))
        ortex_policy_payload = (
            None
            if ortex_public_policy is None
            else copy.deepcopy(dict(ortex_public_policy))
        )
        if sha256_json(adaptive_policy_payload) != policy_sha256:
            _reject("adaptive_policy_snapshot_hash_mismatch")
        if sha256_json(candidate_code_payload) != candidate_code_build_sha256:
            _reject("candidate_code_build_payload_hash_mismatch")
        if ortex_policy_payload is not None and (
            type(ortex_policy_payload.get("monthly_limit")) is not int
            or ortex_policy_payload["monthly_limit"] != 1_000
            or type(ortex_policy_payload.get("success_cache_ttl_seconds"))
            is not int
            or not 60
            <= ortex_policy_payload["success_cache_ttl_seconds"]
            <= 7 * 24 * 60 * 60
        ):
            _reject("ortex_public_policy_contract_invalid")
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
        self.ortex_public_policy = ortex_policy_payload
        self.ortex_public_policy_sha256 = (
            None
            if ortex_policy_payload is None
            else sha256_json(ortex_policy_payload)
        )
        self.context_max_age_seconds = max_age
        self.tenbeat_entry_tilt_weight = tenbeat_weight
        self.fundamentals_reader = fundamentals_reader
        self.wall_clock = wall_clock
        self._source_to_target = source_to_target
        # A hub generation may contain many complete symbol cohorts.  One
        # read returns only one cohort so its fast-market context cannot age
        # behind a large enrichment/publish batch; these fields drain the
        # remaining cohorts before declaring that generation consumed.
        self._last_hub_snapshot_sha256: str | None = None
        self._draining_hub_snapshot_sha256: str | None = None
        self._attempted_cohort_symbols: set[str] = set()
        self._emitted_cohort_symbols: set[str] = set()
        self._last_cohort_symbol: str | None = None

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
        # The shared hub row is per-tick and alternates between equity and
        # crypto-only batches. A crypto-only tick is still a valid, fresh hub
        # snapshot; the complete/fresh equity route probe supplies the
        # selection universe below. Rejecting here starved the PAPER frontier
        # whenever the source sampled between equity batches.
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

    def _viability_universe_probe(self) -> tuple[str, ...]:
        """Symbols whose viability route-set is ALREADY complete and fresh.

        2026-07-24 (a86-1306): the hub's ``symbols_evaluated`` is per-tick — a
        tape-delta tick overwrites it with ONE symbol, so pinning the admission
        universe to the last hub tick made the eligible set flicker empty
        whenever that tick's symbols lacked complete fresh routes (the normal
        post-cutover state; measured: hub=["ARHS","S"] while 98 symbols held
        complete fresh route-sets).  Probe the viability table itself for
        complete+fresh symbols and admit the UNION with the hub universe; the
        per-symbol admission inside the read-only transaction still re-validates
        every row against read_at, the hub correlation, and the source node
        before anything is published, so this probe only WIDENS the candidate
        pool — it never bypasses a gate.
        """
        probe_at = _utc(
            self.wall_clock(), "derived_source_universe_probe_at"
        )
        cutoff = (
            probe_at.astimezone(timezone.utc)
            - timedelta(seconds=self.context_max_age_seconds)
        ).replace(tzinfo=None)
        db = Session(bind=self._bind, expire_on_commit=False)
        try:
            db.execute(text(_READ_ONLY_TRANSACTION_SQL))
            grouped = (
                db.query(
                    MomentumSymbolViability.symbol,
                    func.count(
                        func.distinct(MomentumSymbolViability.variant_id)
                    ),
                    func.max(MomentumSymbolViability.freshness_ts),
                )
                .filter(
                    MomentumSymbolViability.scope == "symbol",
                    MomentumSymbolViability.variant_id.in_(
                        self.source_variant_ids
                    ),
                    MomentumSymbolViability.source_node_id == HUB_NODE_ID,
                    MomentumSymbolViability.freshness_ts >= cutoff,
                )
                .group_by(MomentumSymbolViability.symbol)
                .all()
            )
        finally:
            try:
                db.rollback()
            finally:
                db.close()
        required = len(set(self.source_variant_ids))
        ranked: list[tuple[Any, str]] = []
        for symbol_raw, distinct_variants, newest in grouped:
            symbol = str(symbol_raw or "")
            if int(distinct_variants or 0) != required:
                continue
            if (
                symbol != symbol.strip().upper()
                or _SYMBOL_RE.fullmatch(symbol) is None
                or symbol.endswith("-USD")
            ):
                continue
            ranked.append((newest, symbol))
        ranked.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return tuple(
            symbol for _newest, symbol in ranked[:_VIABILITY_UNIVERSE_CAP]
        )

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
            typed_receipt = _finite_json(copy.deepcopy(raw.to_dict()))
            result = _finite_json(copy.deepcopy(dict(raw.data or {})))
            result_sha256 = sha256_json(result)
            short_name = result.get("short_name")
            if short_name is not None and not isinstance(short_name, str):
                _reject("derived_source_fundamentals_name_invalid")
            query = {
                "schema_version": FUNDAMENTALS_QUERY_SCHEMA_VERSION,
                "provider": FUNDAMENTALS_PROVIDER,
                "operation": "get_cached_fundamentals_receipt",
                "lookup_performed": True,
                "symbol": symbol,
                "started_at": started_at,
                "returned_at": returned_at,
                "lookup_latency_seconds": (
                    returned_at - started_at
                ).total_seconds(),
                "provider_event_at": None,
                "provider_event_clock_status": (
                    "unavailable_for_reference_fundamentals"
                ),
                "received_at": raw.observed_at,
                "available_at": returned_at,
                "last_provider_fetched_at": raw.fetched_at,
                "last_provider_request_latency_seconds": (
                    raw.provider_latency_seconds
                ),
                "last_provider_limiter_wait_seconds": (
                    raw.provider_limiter_wait_seconds
                ),
                "last_refresh_attempt": (
                    None
                    if raw.last_refresh_attempt is None
                    else raw.last_refresh_attempt.to_dict()
                ),
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
                        raw.reason
                        or "fundamentals_"
                        f"{raw.status.value.lower()}_"
                        f"{raw.provider_state.value.lower()}"
                    )
                ),
                "cache_or_network_transport": raw.origin.value.lower(),
                "cache_hit": (
                    raw.origin is FundamentalsReceiptOrigin.CACHE
                ),
                "cache_age_seconds": raw.cache_age_seconds,
                "semantic_ttl_seconds": raw.cache_ttl_seconds,
                "refresh_state": raw.refresh_state.value,
                "refresh_reason": raw.refresh_reason,
                "next_refresh_at": raw.next_refresh_at,
                "upstream_market_truth_certified": False,
            }
            receipts[symbol] = {
                **query,
                "query_receipt_sha256": sha256_json(query),
            }
        return receipts

    def _fundamentals_receipt_at_decision(
        self,
        *,
        symbol: str,
        receipt: Mapping[str, Any] | None,
        decision_at: datetime,
    ) -> Mapping[str, Any]:
        if receipt is None:
            missing = FundamentalsReceipt(
                symbol=symbol,
                status=FundamentalsReceiptStatus.UNAVAILABLE,
                provider_state=FundamentalsProviderState.UNAVAILABLE,
                origin=FundamentalsReceiptOrigin.NONE,
                observed_at=decision_at,
                cache_ttl_seconds=1.0,
                reason="fundamentals_not_in_prefetch_universe",
            )
            typed = _finite_json(missing.to_dict())
            body: dict[str, Any] = {
                "schema_version": FUNDAMENTALS_QUERY_SCHEMA_VERSION,
                "provider": FUNDAMENTALS_PROVIDER,
                "operation": (
                    "cache_lookup_not_performed_"
                    "authoritative_symbol_arrived_after_prefetch"
                ),
                "lookup_performed": False,
                "symbol": symbol,
                "started_at": decision_at,
                "returned_at": decision_at,
                "lookup_latency_seconds": 0.0,
                "provider_event_at": None,
                "provider_event_clock_status": (
                    "unavailable_for_reference_fundamentals"
                ),
                "received_at": decision_at,
                "available_at": decision_at,
                "last_provider_fetched_at": None,
                "last_provider_request_latency_seconds": None,
                "last_provider_limiter_wait_seconds": None,
                "last_refresh_attempt": None,
                "empty_result": True,
                "result": {},
                "result_sha256": sha256_json({}),
                "typed_provider_receipt": typed,
                "typed_provider_receipt_sha256": sha256_json(typed),
                "classification_usable": False,
                "classification_coverage_reason": missing.reason,
                "cache_or_network_transport": "none",
                "cache_hit": False,
                "cache_age_seconds": None,
                "semantic_ttl_seconds": missing.cache_ttl_seconds,
                "refresh_state": missing.refresh_state.value,
                "refresh_reason": missing.refresh_reason,
                "next_refresh_at": None,
                "upstream_market_truth_certified": False,
            }
        else:
            body = copy.deepcopy(dict(receipt))
            body.pop("query_receipt_sha256", None)

        returned_at = _utc(
            body.get("returned_at"),
            "fundamentals_returned_at",
        )
        received_at = _utc(
            body.get("received_at"),
            "fundamentals_received_at",
        )
        if returned_at > decision_at or received_at > decision_at:
            _reject("derived_source_provider_result_from_future")
        cache_age_raw = body.get("cache_age_seconds")
        cache_age = 0.0 if cache_age_raw is None else float(cache_age_raw)
        semantic_age = (
            decision_at - received_at
        ).total_seconds() + cache_age
        ttl = float(body.get("semantic_ttl_seconds") or 0.0)
        if (
            not math.isfinite(semantic_age)
            or semantic_age < 0.0
            or not math.isfinite(ttl)
            or ttl <= 0.0
        ):
            _reject("derived_source_fundamentals_semantic_clock_invalid")
        usable = (
            body.get("classification_usable") is True
            and semantic_age <= ttl
        )
        reason = body.get("classification_coverage_reason")
        if body.get("classification_usable") is True and not usable:
            reason = "fundamentals_semantic_ttl_expired"
        body["decision_at"] = decision_at
        body["semantic_age_at_decision_seconds"] = semantic_age
        body["classification_usable_at_decision"] = usable
        body["classification_coverage_reason"] = (
            None if usable else str(reason or "fundamentals_unavailable")
        )
        return {
            **body,
            "query_receipt_sha256": sha256_json(body),
        }

    def read_snapshot(self) -> tuple[CapturedDerivedViabilitySnapshot, ...]:
        probe = self._probe_hub()
        probe_sha = _sha(
            probe.get("hub_snapshot_sha256"),
            "derived_source_hub_snapshot_sha256",
        )
        probe_now = _utc(self.wall_clock(), "derived_source_probe_at")
        probe_tick = _utc(probe["tick_at"], "derived_source_hub_tick_at")
        probe_age = (probe_now - probe_tick).total_seconds()
        if probe_age < 0.0:
            _reject("derived_source_hub_snapshot_stale")
        if (
            self._last_hub_snapshot_sha256 == probe_sha
            and probe_age <= self.context_max_age_seconds
        ):
            return ()
        if probe_age > self.context_max_age_seconds:
            _reject("derived_source_hub_snapshot_stale")

        # Universe = hub equity symbols UNION symbols with complete fresh
        # viability route-sets (see _viability_universe_probe).  The hub's
        # per-tick symbols alone flicker down to a single tape-delta symbol.
        symbols = tuple(
            sorted(
                {str(value) for value in probe["equity_symbols"]}
                | set(self._viability_universe_probe())
            )
        )
        # Slow-changing instrument metadata is cache-only on this thread.
        # Misses schedule bounded background refreshes; no provider call or
        # rate-limit sleep may age the authoritative fast-market snapshot.
        fundamentals = self._fundamentals_receipts(symbols)
        db = Session(bind=self._bind, expire_on_commit=False)
        prepared: list[dict[str, Any]] = []
        try:
            db.execute(text(_READ_ONLY_TRANSACTION_SQL))
            # Supplemental receipt bytes are frozen before this final
            # repeatable-read transaction. Background refresh may continue, but
            # no later cache/provider result can alter this decision.
            hub = self._hub_snapshot(db)
            # 2026-07-24 (a86-1532): the probe-vs-in-transaction hub sha
            # EQUALITY pin is unsatisfiable at production cadence: the hub
            # ticks every 5-25s (measured live, even after hours), so the
            # bounded probe may already differ by the authoritative read.  The
            # IN-TRANSACTION hub read is the captured snapshot (same REPEATABLE
            # READ view as the viability rows); the probe is only a bounded
            # candidate-universe hint. The published occurrence binds THIS
            # in-transaction hub sha, and later provider work cannot rebind it.
            hub_sha = _sha(
                hub.get("hub_snapshot_sha256"),
                "derived_source_hub_snapshot_sha256",
            )
            if self._last_hub_snapshot_sha256 == hub_sha:
                return ()
            symbols = tuple(
                sorted(
                    set(symbols)
                    | {str(value) for value in hub["equity_symbols"]}
                )
            )
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
            target_variant_ids = tuple(
                sorted(self.selection_authority.variant_ids)
            )
            route_state_rows = (
                db.query(CapturedPaperSelectionRouteState)
                .filter(
                    CapturedPaperSelectionRouteState.account_scope
                    == self.selection_authority.account_scope,
                    CapturedPaperSelectionRouteState.expected_account_id
                    == self.expected_account_id,
                    CapturedPaperSelectionRouteState.activation_generation
                    == self.activation_generation,
                    CapturedPaperSelectionRouteState.symbol.in_(symbols),
                    CapturedPaperSelectionRouteState.variant_id.in_(
                        target_variant_ids
                    ),
                )
                .order_by(
                    CapturedPaperSelectionRouteState.symbol.asc(),
                    CapturedPaperSelectionRouteState.variant_id.asc(),
                )
                .all()
            )
            route_event_floors: dict[tuple[str, int], datetime] = {}
            for route_state in route_state_rows:
                if not (
                    route_state.execution_family
                    == self.selection_authority.execution_family
                    and route_state.authority_sha256
                    == self.selection_authority.authority_sha256
                ):
                    _reject("derived_source_route_state_authority_drift")
                route_event_at = _utc(
                    route_state.source_event_at,
                    "derived_source_route_state_event_at",
                )
                route_event_floors[
                    (str(route_state.symbol), int(route_state.variant_id))
                ] = route_event_at
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
            # This clock pins DB queries that require an as-of boundary.  The
            # final batch decision/read clock is sampled only after every
            # resolver read has completed.
            source_snapshot_at = _utc(
                self.wall_clock(),
                "derived_source_transaction_snapshot_at",
            )
            tick_at = _utc(hub["tick_at"], "derived_source_hub_tick_at")
            hub_age = (source_snapshot_at - tick_at).total_seconds()
            if (
                hub_age < 0.0
                or hub_age > self.context_max_age_seconds
            ):
                _reject("derived_source_hub_snapshot_stale")
            # 2026-07-23 (a80 finding; supersedes the a75/a76 per-row notes):
            # the production viability writer is INCREMENTAL and sparse -- a
            # tick rewrites rows only for the routes it evaluated (measured
            # live: 220 rows at the current tick, 210 at prior ticks, plus
            # per-route leftovers untouched for WEEKS).  A complete
            # single-generation (symbol x variant) snapshot has never existed
            # in production, so the all-routes completeness gate and any
            # cross-row generation equality reject every real activation by
            # construction.  Fail-soft per SYMBOL instead: a symbol enters
            # this cycle's selection universe only when ALL its variant
            # routes are present, fresh (bounded age), never from the future,
            # and internally coherent (one freshness + one regime sha within
            # the symbol) with the hub's correlation/source-node; symbols
            # that fail are EXCLUDED from the cycle (simply not traded), and
            # at least one symbol must survive.
            rows_by_symbol: dict[str, list[Any]] = {}
            for row in rows:
                rows_by_symbol.setdefault(str(row.symbol or ""), []).append(row)
            required_variants = set(self.source_variant_ids)
            eligible_symbols: set[str] = set()
            for symbol_key, group in rows_by_symbol.items():
                if (
                    len(group) != len(required_variants)
                    or {int(r.variant_id) for r in group} != required_variants
                ):
                    continue
                # 2026-07-23 (a81 finding): the writer is incremental at the
                # (symbol x variant) level -- measured live, one symbol's
                # variant rows span 3-8 DIFFERENT ticks (distinct freshness)
                # even at full market cadence, so same-freshness /
                # same-regime-sha within a symbol never holds; and viability
                # writes land AFTER the hub tick they belong to (measured:
                # rows 25s newer than the hub's last_tick), so a
                # rows-not-after-hub-tick pin excludes everything too.  The
                # build loop already consumes each row's OWN regime snapshot
                # (_context_from_snapshot per row), so the true per-row
                # invariants are: bounded age, never from the future of the
                # READ (ag < 0 below), and the hub's correlation/source-node.
                try:
                    coherent = True
                    for r in group:
                        fr = _utc(
                            r.freshness_ts,
                            "derived_source_viability_freshness",
                        )
                        ag = (source_snapshot_at - fr).total_seconds()
                        # 2026-07-24 (a86-0948): viability_score is a float8
                        # column, so the producer CAN persist NaN/Infinity
                        # (JSONB fields cannot).  A non-finite score survives
                        # projection into the occurrence payload and then
                        # canonical_json_bytes raises ValueError ("Out of range
                        # float values are not JSON compliant") -- killing the
                        # whole fenced start for one poisoned row.  Exclude the
                        # symbol fail-soft instead, like every other
                        # per-symbol eligibility failure.
                        # 2026-07-24 (a86-1448): DROPPED the row-vs-hub
                        # correlation equality.  The incremental writer stamps
                        # each tick's correlation on the rows it touches, so a
                        # symbol's 10 variant rows legitimately mix correlations
                        # (measured live: 46 symbols with "" + 28 with a UUID in
                        # one window) and can never all equal the LAST hub
                        # tick's -- the same per-tick-state-pinned-across-
                        # independent-writes flaw as the universe pin.  The hub
                        # itself already allows an empty correlation (a74) and
                        # the published occurrence synthesizes its own
                        # captured:<fingerprint> correlation; provenance stays
                        # pinned by source_node + freshness + from-future +
                        # variant-sha + the read-only snapshot.
                        score = r.viability_score
                        if (
                            score is None
                            or not math.isfinite(float(score))
                            or ag < 0.0
                            or ag > self.context_max_age_seconds
                            or str(r.source_node_id or "") != HUB_NODE_ID
                        ):
                            coherent = False
                            break
                    if coherent:
                        eligible_symbols.add(symbol_key)
                except (
                    CapturedPaperSelectionSourceUnavailable,
                    TypeError,
                    ValueError,
                ):
                    continue
            if not eligible_symbols:
                _reject("derived_source_current_snapshot_empty")
            # Publish one complete symbol cohort per hub generation.  The
            # captured queue is atomic at the returned tuple boundary: while
            # that tuple is being resolved/published, admission is suspended.
            # Resolving every fresh symbol here therefore aged the first
            # usable cohort beyond the configured market-context TTL that the
            # initial provider correctly enforces.  A cohort remains all bound
            # routes for one symbol (never a partial route set).  Select from
            # PAPER+LIVE-eligible source cohorts whenever any exist and walk
            # that pool in stable round-robin order.  The first cohort is
            # current-hub/newest first; subsequent hub generations advance the
            # cursor so one repeatedly ticking symbol cannot starve its peers.
            # If one symbol is malformed, continue to the next ranked complete
            # cohort so a single bad dependency cannot starve the universe.
            hub_equity_symbols = {
                str(value) for value in hub["equity_symbols"]
            }

            source_trade_eligible_symbols = {
                symbol_key
                for symbol_key in eligible_symbols
                if any(
                    bool(row.paper_eligible) and bool(row.live_eligible)
                    for row in rows_by_symbol[symbol_key]
                )
            }
            all_candidate_symbols = set(
                source_trade_eligible_symbols or eligible_symbols
            )
            attempted_symbols = (
                set(self._attempted_cohort_symbols)
                if self._draining_hub_snapshot_sha256 == hub_sha
                else set()
            )
            hub_had_survivor = bool(
                self._emitted_cohort_symbols
                if self._draining_hub_snapshot_sha256 == hub_sha
                else ()
            )
            candidate_symbols = sorted(all_candidate_symbols - attempted_symbols)
            attempted_this_read: set[str] = set()
            selected_symbol: str | None = None
            ortex_batch_cache: dict[
                str, tuple[Mapping[str, Any] | None, str | None]
            ] = {}
            ortex_manifest_cache: dict[
                str, ValidatedOrtexSqueezeFuelBatchManifest
            ] = {}

            def initial_cohort_rank(
                symbol_key: str,
            ) -> tuple[int, float, str]:
                group = rows_by_symbol[symbol_key]
                freshness_floor = min(
                    _utc(
                        row.freshness_ts,
                        "derived_source_viability_freshness",
                    )
                    for row in group
                )
                return (
                    0 if symbol_key in hub_equity_symbols else 1,
                    -freshness_floor.timestamp(),
                    symbol_key,
                )

            last_cohort_symbol = self._last_cohort_symbol
            if last_cohort_symbol in candidate_symbols:
                cursor = candidate_symbols.index(last_cohort_symbol)
                ranked_symbols = (
                    candidate_symbols[cursor + 1 :]
                    + candidate_symbols[: cursor + 1]
                )
            else:
                ranked_symbols = sorted(
                    candidate_symbols,
                    key=initial_cohort_rank,
                )

            for symbol_key in ranked_symbols:
                attempted_this_read.add(symbol_key)
                group_prepared: list[dict[str, Any]] = []
                symbol = str(symbol_key or "").strip().upper()
                if symbol not in symbols or _SYMBOL_RE.fullmatch(symbol) is None:
                    _reject("derived_source_row_symbol_invalid")
                # A malformed route invalidates only its symbol.  Build all
                # route payloads into a temporary group and publish none of
                # them unless every route survives structural parsing and
                # explicit DB-input resolution.
                try:
                    for viability in rows_by_symbol[symbol_key]:
                        source_variant = by_id[int(viability.variant_id)]
                        (
                            target_id,
                            family_id,
                            source_variant_sha,
                        ) = self._source_to_target[int(source_variant.id)]
                        family = get_family(family_id)
                        if family is None:
                            _reject("derived_source_family_unavailable")
                        context = _context_from_snapshot(
                            viability.regime_snapshot_json
                        )
                        event_at = _parse_utc(
                            context.utc_iso,
                            "derived_source_context_event_at",
                        )
                        route_event_floor = route_event_floors.get(
                            (symbol, target_id)
                        )
                        if (
                            route_event_floor is not None
                            and event_at < route_event_floor
                        ):
                            _reject("derived_source_route_clock_regressed")
                        context_age = (
                            source_snapshot_at - event_at
                        ).total_seconds()
                        if (
                            context_age < 0.0
                            or context_age > self.context_max_age_seconds
                        ):
                            _reject("derived_source_context_stale")
                        features = _features_from_snapshot(
                            viability.execution_readiness_json
                        )
                        feature_meta = (
                            features.meta
                            if isinstance(features.meta, Mapping)
                            else {}
                        )
                        ross_signals = feature_meta.get("ross_signals")
                        current_ortex_batch: Mapping[str, Any] | None = None
                        current_ortex_reason: str | None = None
                        current_ortex_affected = (
                            self.ortex_public_policy is not None
                        )
                        if current_ortex_affected and isinstance(
                            ross_signals, Mapping
                        ):
                            current_signal = ross_signals.get(symbol)
                            if not isinstance(current_signal, Mapping):
                                current_ortex_reason = (
                                    "ortex_selection_ranked_signal_missing"
                                )
                            else:
                                (
                                    global_signals,
                                    batch_cache_key,
                                    current_ortex_reason,
                                ) = _ortex_global_batch_inventory(
                                    db,
                                    feature_meta,
                                    required_symbol=symbol,
                                    read_at=source_snapshot_at,
                                    public_policy_sha256=(
                                        self.ortex_public_policy_sha256
                                    ),
                                    freshness_ttl_seconds=float(
                                        self.ortex_public_policy[
                                            "success_cache_ttl_seconds"
                                        ]
                                    ),
                                    manifest_cache=ortex_manifest_cache,
                                )
                                if (
                                    current_ortex_reason is None
                                    and global_signals is not None
                                    and batch_cache_key is not None
                                ):
                                    global_member = global_signals[symbol]
                                    if not _ortex_row_signal_matches_global_member(
                                        current_signal,
                                        global_member,
                                    ):
                                        current_ortex_reason = (
                                            "ortex_selection_batch_signal_"
                                            "projection_mismatch"
                                        )
                                    else:
                                        if batch_cache_key not in ortex_batch_cache:
                                            ortex_batch_cache[batch_cache_key] = (
                                                _materialize_ortex_selection_batch(
                                                    db,
                                                    global_signals,
                                                    read_at=source_snapshot_at,
                                                    public_policy=(
                                                        self.ortex_public_policy
                                                    ),
                                                    public_policy_sha256=(
                                                        self.ortex_public_policy_sha256
                                                    ),
                                                )
                                            )
                                        (
                                            current_ortex_batch,
                                            current_ortex_reason,
                                        ) = ortex_batch_cache[batch_cache_key]
                                if (
                                    current_ortex_batch is None
                                    and current_ortex_reason is None
                                ):
                                    current_ortex_reason = (
                                        "ortex_selection_batch_unavailable"
                                    )
                        elif current_ortex_affected:
                            current_ortex_reason = (
                                "ortex_selection_signal_inventory_missing"
                            )
                        external = resolve_viability_external_inputs_for_capture(
                            symbol,
                            family,
                            context,
                            features,
                            db=db,
                            settings_projection=self.settings_projection,
                            # Classification is filled after the transaction
                            # from the typed fundamentals receipt. Resolve all
                            # remaining DB inputs without either classifier
                            # short-circuit.
                            leveraged_etf=False,
                            excluded_fund=False,
                            decision_as_of=source_snapshot_at,
                        )
                        post_score = CapturedViabilityPostScoreAdjustment(
                            tenbeat_entry_tilt_weight=(
                                self.tenbeat_entry_tilt_weight
                            ),
                            tenbeat_breakout_score=None,
                            lookup_status=(
                                "disabled"
                                if self.tenbeat_entry_tilt_weight == 0.0
                                else "inapplicable_non_crypto"
                            ),
                            source_read_id=None,
                        )
                        group_prepared.append(
                            {
                                "symbol": symbol,
                                "source_variant_id": int(source_variant.id),
                                "target_variant_id": target_id,
                                "family": family,
                                "context": context,
                                "features": features,
                                "external": external,
                                "post_score_adjustment": post_score,
                                "source_variant": _variant_snapshot(
                                    source_variant
                                ),
                                "source_viability": _viability_snapshot(
                                    viability
                                ),
                                "source_variant_sha256": source_variant_sha,
                                "ortex_selection_batch": current_ortex_batch,
                                "ortex_selection_affected": current_ortex_affected,
                                "ortex_coverage_reason": (
                                    current_ortex_reason
                                    if current_ortex_affected
                                    else None
                                ),
                                "viability_freshness_at": _utc(
                                    viability.freshness_ts,
                                    "derived_source_viability_freshness",
                                ),
                                "event_at": event_at,
                                "correlation_id": str(
                                    viability.correlation_id or ""
                                ).strip(),
                            }
                        )
                except CapturedPaperSelectionSourceUnavailable as exc:
                    if exc.reason == "derived_source_family_unavailable":
                        raise
                    continue
                except (KeyError, TypeError, ValueError):
                    continue
                prepared.extend(group_prepared)
                selected_symbol = symbol
                break
        finally:
            try:
                db.rollback()
            finally:
                db.close()

        if not prepared and hub_had_survivor:
            # This immutable hub generation already established a valid
            # frontier.  Remaining malformed/expired cohorts are symbol-local
            # exclusions and cannot revoke or repeatedly suspend that frontier.
            self._last_hub_snapshot_sha256 = hub_sha
            self._draining_hub_snapshot_sha256 = None
            self._attempted_cohort_symbols.clear()
            self._emitted_cohort_symbols.clear()
            return ()
        if not prepared:
            _reject("derived_source_current_snapshot_empty")
        if selected_symbol is None:
            _reject("derived_source_symbol_cohort_invalid")
        read_at = _utc(
            self.wall_clock(),
            "derived_source_read_at",
        )
        if read_at < source_snapshot_at:
            _reject("derived_source_read_clock_reversed")
        if read_at.date() != source_snapshot_at.date():
            # The only time-dependent resolver input is calendar-day based.
            # Never publish a batch whose resolver and decision clocks straddle
            # that boundary.
            _reject("derived_source_external_resolution_day_crossed")
        hub_age = (read_at - tick_at).total_seconds()
        if hub_age < 0.0 or hub_age > self.context_max_age_seconds:
            _reject("derived_source_hub_snapshot_stale")
        # Only commit per-hub progress after the transaction and global clock
        # checks succeed.  From here onward a rejection belongs to this one
        # symbol cohort; it may be skipped without revoking a prior survivor.
        if self._draining_hub_snapshot_sha256 != hub_sha:
            self._draining_hub_snapshot_sha256 = hub_sha
            self._attempted_cohort_symbols.clear()
            self._emitted_cohort_symbols.clear()
        self._attempted_cohort_symbols.update(attempted_this_read)
        self._last_cohort_symbol = selected_symbol
        final_groups: dict[str, list[dict[str, Any]]] = {}
        for item in prepared:
            final_groups.setdefault(str(item["symbol"]), []).append(item)
        prepared = [
            item
            for group in final_groups.values()
            if all(
                0.0
                <= (read_at - candidate["viability_freshness_at"]).total_seconds()
                <= self.context_max_age_seconds
                and 0.0
                <= (read_at - candidate["event_at"]).total_seconds()
                <= self.context_max_age_seconds
                for candidate in group
            )
            for item in group
        ]
        if not prepared:
            if hub_had_survivor:
                return ()
            _reject("derived_source_current_snapshot_empty")
        enriched: list[dict[str, Any]] = []
        for item in prepared:
            symbol = str(item["symbol"])
            try:
                fundamentals_receipt = self._fundamentals_receipt_at_decision(
                    symbol=symbol,
                    receipt=fundamentals.get(symbol),
                    decision_at=read_at,
                )
            except CapturedPaperSelectionSourceUnavailable:
                if hub_had_survivor:
                    return ()
                raise
            fundamentals_result = dict(
                fundamentals_receipt.get("result") or {}
            )
            short_name = fundamentals_result.get("short_name")
            classification_usable = (
                fundamentals_receipt.get(
                    "classification_usable_at_decision"
                )
                is True
            )
            if classification_usable:
                leveraged_etf = bool(
                    self.settings_projection.chili_momentum_exclude_leveraged_etfs
                ) and is_leveraged_etf_name(short_name)
                excluded_fund = (
                    not leveraged_etf
                    and bool(
                        self.settings_projection.chili_momentum_exclude_fund_structures_enabled
                    )
                    and is_excluded_fund_name(short_name)
                )
            else:
                # These name classifiers are optional in the intended live
                # strategy and historically fail open.  Preserve that policy
                # explicitly while recording the exact provider/cache reason;
                # a supplemental miss must not become a paper-only dark veto.
                leveraged_etf = False
                excluded_fund = False
            resolved_external = (
                ViabilityExternalInputs.neutral(leveraged_etf=True)
                if leveraged_etf
                else replace(
                    item["external"],
                    leveraged_etf=False,
                    excluded_fund=excluded_fund,
                )
            )
            enriched.append(
                {
                    **item,
                    "fundamentals_receipt": fundamentals_receipt,
                    "short_name": short_name,
                    "classification_usable": classification_usable,
                    "leveraged_etf": leveraged_etf,
                    "excluded_fund": excluded_fund,
                    "external": resolved_external,
                }
            )

        snapshots: list[CapturedDerivedViabilitySnapshot] = []
        for item in enriched:
            symbol = str(item["symbol"])
            family = item["family"]
            context = item["context"]
            features = item["features"]
            fundamentals_receipt = item["fundamentals_receipt"]
            short_name = item["short_name"]
            classification_usable = item["classification_usable"]
            leveraged_etf = item["leveraged_etf"]
            excluded_fund = item["excluded_fund"]
            external = item["external"]

            source_freshness_floor_at = min(
                tick_at,
                item["viability_freshness_at"],
                item["event_at"],
            )
            source_payload = {
                "schema_version": SOURCE_SCHEMA_VERSION,
                "source_authority": "derived_snapshot_only",
                "upstream_raw_market_certification": "not_claimed",
                "network_source_capture": (
                    "cache_only_with_bounded_background_refresh"
                ),
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
                "hub_snapshot": copy.deepcopy(dict(hub)),
                "hub_snapshot_sha256": hub_sha,
                "fundamentals_query_receipt": copy.deepcopy(
                    dict(fundamentals_receipt)
                ),
                "instrument_classification": {
                    "short_name": short_name,
                    "status": (
                        "available"
                        if classification_usable
                        else "optional_unavailable"
                    ),
                    "coverage_reason": fundamentals_receipt.get(
                        "classification_coverage_reason"
                    ),
                    "leveraged_etf": leveraged_etf,
                    "excluded_fund": excluded_fund,
                    "required_for_decision": False,
                    "unavailable_policy": (
                        None
                        if classification_usable
                        else "neutral_fail_open_as_intended"
                    ),
                },
                "source_variant": item["source_variant"],
                "source_viability": item["source_viability"],
                "target_variant_id": item["target_variant_id"],
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
                "post_score_adjustment": item[
                    "post_score_adjustment"
                ].to_dict(),
                "ortex_selection_batch_sha256": (
                    None
                    if item["ortex_selection_batch"] is None
                    else sha256_json(item["ortex_selection_batch"])
                ),
                "ortex_selection_coverage_reason": (
                    item["ortex_coverage_reason"]
                    if item["ortex_selection_affected"]
                    else None
                ),
                "source_variant_sha256": item[
                    "source_variant_sha256"
                ],
            }
            source_age_at_read_seconds = (
                read_at - source_freshness_floor_at
            ).total_seconds()
            if (
                source_age_at_read_seconds < 0.0
                or source_age_at_read_seconds
                > self.context_max_age_seconds
            ):
                if hub_had_survivor:
                    return ()
                _reject("derived_source_market_snapshot_stale")
            source_payload["source_snapshot"] = {
                "captured_at": read_at,
                "authoritative_transaction_snapshot_at": source_snapshot_at,
                "freshness_floor_at": source_freshness_floor_at,
                "age_at_read_seconds": source_age_at_read_seconds,
                "status": "fresh",
            }
            source_payload["read_at"] = read_at
            fingerprint = sha256_json(source_payload)
            correlation = item["correlation_id"]
            if not correlation or len(correlation) > 64:
                correlation = f"captured:{fingerprint[:55]}"
            snapshots.append(
                CapturedDerivedViabilitySnapshot(
                    symbol=symbol,
                    source_variant_id=item["source_variant_id"],
                    target_variant_id=item["target_variant_id"],
                    family=family,
                    context=context,
                    features=features,
                    settings=self.settings_projection,
                    external=external,
                    post_score_adjustment=item[
                        "post_score_adjustment"
                    ],
                    source_payload=source_payload,
                    ortex_selection_batch=item["ortex_selection_batch"],
                    ortex_selection_affected=item[
                        "ortex_selection_affected"
                    ],
                    ortex_coverage_reason=(
                        item["ortex_coverage_reason"]
                        if item["ortex_selection_affected"]
                        else None
                    ),
                    source_fingerprint_sha256=fingerprint,
                    hub_snapshot_sha256=hub_sha,
                    event_at=item["event_at"],
                    read_at=read_at,
                    correlation_id=correlation,
                )
            )
        if not snapshots:
            if hub_had_survivor:
                return ()
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
        emitted_symbols = {row.symbol for row in result}
        if len(emitted_symbols) != 1:
            _reject("derived_source_symbol_cohort_invalid")
        emitted_symbol = next(iter(emitted_symbols))
        self._emitted_cohort_symbols.add(emitted_symbol)
        return result

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
            or snapshot.source_payload.get("ortex_selection_batch_sha256")
            != (
                None
                if snapshot.ortex_selection_batch is None
                else sha256_json(snapshot.ortex_selection_batch)
            )
            or snapshot.source_payload.get(
                "ortex_selection_coverage_reason"
            )
            != (
                snapshot.ortex_coverage_reason
                if snapshot.ortex_selection_affected
                else None
            )
        ):
            _reject("derived_source_snapshot_material_drift")
        read_at = snapshot.read_at
        event_at = snapshot.event_at
        ortex_evidence = _ortex_selection_evidence(
            snapshot.features,
            batch=snapshot.ortex_selection_batch,
            affected=snapshot.ortex_selection_affected,
            preflight_reason=snapshot.ortex_coverage_reason,
            symbol=snapshot.symbol,
            read_at=read_at,
        )
        # Five fixed slots per occurrence keep the optional Ortex event from
        # colliding with the next occurrence's config snapshot. The unused
        # fifth slot remains reserved when Ortex is not an input.
        slot_base = (source_sequence - 1) * 5
        config_event = CaptureEvent(
            identity=self.capture_identity,
            sequence=slot_base + 1,
            stream=CaptureStream.CONFIG_SNAPSHOT,
            clocks=CaptureClocks(received_at=read_at, available_at=read_at),
            payload=self._config_payload,
            provider=CONFIG_PROVIDER,
        )
        feature_flags_event = CaptureEvent(
            identity=self.capture_identity,
            sequence=slot_base + 2,
            stream=CaptureStream.FEATURE_FLAG_SNAPSHOT,
            clocks=CaptureClocks(received_at=read_at, available_at=read_at),
            payload=self._feature_flags_payload,
            provider=FEATURE_FLAGS_PROVIDER,
        )
        code_event = CaptureEvent(
            identity=self.capture_identity,
            sequence=slot_base + 3,
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
            sequence=slot_base + 4,
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
        core_events = (
            config_event,
            feature_flags_event,
            code_event,
            source_event,
        )
        ortex_event: CaptureEvent | None = None
        if ortex_evidence.snapshot is not None:
            typed_ortex = ortex_evidence.snapshot
            ortex_event = CaptureEvent(
                identity=self.capture_identity,
                sequence=slot_base + 5,
                stream=CaptureStream.ORTEX_SNAPSHOT,
                clocks=CaptureClocks(
                    received_at=typed_ortex.source_received_at,
                    available_at=typed_ortex.returned_at,
                    market_reference_at=typed_ortex.market_reference_at,
                ),
                payload=typed_ortex.payload,
                provider=ORTEX_SNAPSHOT_PROVIDER,
                symbol=snapshot.symbol,
                query=typed_ortex.capture_query(),
            )
            # Reparse the exact immutable event envelope before admitting it
            # into the queue.  This catches any clock/query drift introduced by
            # capture composition itself.
            CaptureOrtexSelectionSnapshot.from_event(ortex_event)
        events = core_events + (() if ortex_event is None else (ortex_event,))
        refs = tuple(CaptureEventRef.from_event(event) for event in events)
        source_ref = next(
            ref
            for ref in refs
            if ref.stream is CaptureStream.CAPTURED_VIABILITY_INPUT
        )
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
        if classification_status not in {"available", "optional_unavailable"}:
            _reject("derived_source_instrument_classification_invalid")
        if classification_status == "optional_unavailable":
            gap_reason = str(classification.get("coverage_reason") or "").strip()
            if not gap_reason:
                _reject("derived_source_fundamentals_coverage_reason_missing")
            if classification.get("required_for_decision") is not False:
                _reject("derived_source_optional_classification_contract_invalid")
            if (
                classification.get("unavailable_policy")
                != "neutral_fail_open_as_intended"
            ):
                _reject("derived_source_optional_classification_policy_invalid")
        source_snapshot = snapshot.source_payload.get("source_snapshot")
        if not isinstance(source_snapshot, Mapping):
            _reject("derived_source_snapshot_provenance_missing")
        source_snapshot_status = str(source_snapshot.get("status") or "")
        if source_snapshot_status != "fresh":
            _reject("derived_source_snapshot_provenance_invalid")
        source_snapshot_at = _utc(
            source_snapshot.get("captured_at"),
            "derived_source_snapshot_captured_at",
        )
        if source_snapshot_at != read_at:
            _reject("derived_source_snapshot_decision_clock_mismatch")
        fundamentals_started_at = _utc(
            fundamentals_receipt.get("started_at"),
            "fundamentals_started_at",
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
            requested_at=fundamentals_started_at,
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
        ortex_read_receipt: CaptureReadReceipt | None = None
        ortex_read_receipt_sha256: str | None = None
        if ortex_event is not None:
            typed_ortex = ortex_evidence.snapshot
            assert typed_ortex is not None
            ortex_ref = next(
                ref
                for ref in refs
                if ref.stream is CaptureStream.ORTEX_SNAPSHOT
            )
            ortex_read_id = str(
                uuid.uuid5(
                    uuid.UUID(self.activation_generation),
                    (
                        f"{source_sequence}:ortex:"
                        f"{ortex_event.payload_sha256}:"
                        f"{ortex_event.query_sha256}"
                    ),
                )
            )
            ortex_read_receipt = CaptureReadReceipt(
                read_id=ortex_read_id,
                decision_id=(
                    f"captured-paper-selection:{self.activation_generation}:"
                    f"{source_sequence}"
                ),
                identity_sha256=self.capture_identity.identity_sha256,
                stream=CaptureStream.ORTEX_SNAPSHOT,
                provider=ORTEX_SNAPSHOT_PROVIDER,
                symbol=snapshot.symbol,
                requested_at=typed_ortex.requested_at,
                returned_at=typed_ortex.returned_at,
                query_sha256=ortex_event.query_sha256 or "",
                source_event_sha256s=(ortex_ref.event_sha256,),
                empty_result=False,
                result_sha256=captured_read_result_sha256((ortex_ref,)),
                content_verified=True,
                replay_network_fallback_used=False,
                query=typed_ortex.capture_query(),
            )
            ortex_read_receipt_sha256 = (
                captured_viability_read_receipt_sha256(
                    ortex_read_receipt
                )
            )
        max_age = self.context_max_age_seconds
        required_streams = {
            CaptureStream.CONFIG_SNAPSHOT,
            CaptureStream.FEATURE_FLAG_SNAPSHOT,
            CaptureStream.CODE_BUILD,
            CaptureStream.CAPTURED_VIABILITY_INPUT,
        }
        if ortex_evidence.affected:
            required_streams.add(CaptureStream.ORTEX_SNAPSHOT)
        required_read_ids = [read_id]
        if ortex_read_receipt is not None:
            required_read_ids.append(ortex_read_receipt.read_id)
        stream_dependencies = [
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
        ]
        if ortex_evidence.affected:
            stream_dependencies.append(
                FSMStreamDependency(
                    stream=CaptureStream.ORTEX_SNAPSHOT,
                    exact_provider_event_at_required=False,
                    market_reference_at_required=True,
                    max_source_age_seconds=(
                        ortex_evidence.snapshot.success_cache_ttl_seconds
                        if ortex_evidence.snapshot is not None
                        else max_age
                    ),
                    coverage_start_at=(
                        ortex_event.clocks.available_at
                        if ortex_event is not None
                        else read_at
                    ),
                )
            )
        profile = FSMDependencyProfile(
            required_streams=frozenset(required_streams),
            required_read_ids=tuple(required_read_ids),
            stream_dependencies=tuple(stream_dependencies),
        )
        coverages = [
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
        ]
        if ortex_evidence.affected:
            coverages.append(
                StreamCoverage(
                    stream=CaptureStream.ORTEX_SNAPSHOT,
                    identity_sha256=self.capture_identity.identity_sha256,
                    provider=ORTEX_SNAPSHOT_PROVIDER,
                    symbol=snapshot.symbol,
                    first_available_at=(
                        ortex_event.clocks.available_at
                        if ortex_event is not None
                        else read_at
                    ),
                    last_available_at=(
                        ortex_event.clocks.available_at
                        if ortex_event is not None
                        else read_at
                    ),
                    event_count=1 if ortex_event is not None else 0,
                    exact_event_clock_complete=False,
                    content_verified=ortex_event is not None,
                    continuity_complete=ortex_event is not None,
                    query_receipt_count=(
                        1 if ortex_read_receipt is not None else 0
                    ),
                )
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
        read_receipt_hashes = [read_receipt_sha256]
        if ortex_read_receipt_sha256 is not None:
            read_receipt_hashes.append(ortex_read_receipt_sha256)
        inventory = CapturedViabilityDependencyInventory(
            dependency_profile=profile,
            bindings=tuple(
                CapturedViabilityDependencyBinding(
                    component=component,
                    component_sha256=roots[component],
                    source_event_sha256s=event_hashes,
                    read_receipt_sha256s=tuple(read_receipt_hashes),
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
            read_receipts=(
                (read_receipt,)
                if ortex_read_receipt is None
                else (read_receipt, ortex_read_receipt)
            ),
            stream_coverages=tuple(coverages),
            coverage_gaps=tuple(
                gap
                for gap in (
                    (
                        CoverageGap(
                            stream=CaptureStream.ORTEX_SNAPSHOT,
                            reason=ortex_evidence.coverage_reason,
                            first_available_at=read_at,
                            last_available_at=read_at,
                            lost_count=1,
                            symbol=snapshot.symbol,
                        )
                        if ortex_evidence.coverage_reason is not None
                        else None
                    ),
                )
                if gap is not None
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
    "ortex_public_policy",
]
