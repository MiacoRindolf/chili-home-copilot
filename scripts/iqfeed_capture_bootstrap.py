"""Inert, fail-closed composition for the future unified IQFeed capture host.

This module performs no provider, database, broker, task-scheduler, or service
I/O.  It turns one already-verified bootstrap preflight into a single bounded
in-process L1/L2 ingress graph.  A caller may later install an already-created
shared-store runtime plus a no-fetch startup-input provider.  That deferred
factory binds the exact active L1/L2 bridge generations into every run and
fails closed if either generation is absent, changes during packaging, or
escapes the hash-bound preflight.  Preparing this composition never installs
that factory and never starts a provider or creates a store.

Starting this object starts only two local queue-drain threads.  It does not
bind either queue to an IQFeed socket or to the legacy bridge processes.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from enum import Enum
import hashlib
import json
import os
from pathlib import Path
import stat
import threading
import time
from types import MappingProxyType
from typing import Any, Callable, Mapping
import uuid

from app.services.trading.momentum_neural.iqfeed_l1_capture import (
    BoundedIqfeedL1CaptureHandoff,
    IqfeedL1ProcessCaptureSink,
)
from app.services.trading.momentum_neural.iqfeed_l2_capture import (
    BoundedIqfeedL2CaptureHandoff,
    IqfeedL2ProcessCaptureSink,
)
from app.services.trading.momentum_neural.live_replay_capture import (
    CaptureIdentityEvidence,
    LiveCaptureRunInputs,
    LiveCaptureRunConfiguration,
    LiveCaptureStartupInputProvider,
    LiveReplayCaptureProcessService,
    LiveReplayCaptureSupervisor,
    SharedStoreLiveCaptureRunFactory,
)
from app.services.trading.momentum_neural.replay_capture_contract import (
    CaptureContractError,
    CaptureExternalProducerGeneration,
    CaptureProducerSpec,
    CaptureRunIdentity,
    CaptureStream,
    sha256_json,
)
from app.services.trading.momentum_neural.replay_capture_runtime import (
    CaptureAdaptivePressureController,
    CaptureBudgetPolicy,
    CapturePressureSample,
    CaptureResourceBinding,
    CaptureResourceMeasurement,
    SharedCaptureAdmissionBudget,
    SharedCaptureStoreRuntime,
)
from scripts.iqfeed_capture_bootstrap_preflight import (
    IQFEED_HANDOFF_BUDGET_SCHEMA_VERSION,
    IqfeedCaptureBootstrapPreflight,
)


UTC = timezone.utc
_MAX_REHASH_BYTES = 64 * 1024 * 1024
_REPARSE_ATTRIBUTE = 0x400
_COMPOSITION_SCHEMA_VERSION = "chili.iqfeed-capture-ingress-composition.v1"
_GENERATION_ROSTER_SCHEMA_VERSION = (
    "chili.iqfeed-external-producer-generation-roster.v1"
)
_CAPTURED_PAPER_RUNTIME_CONFIG_SCHEMA_VERSION = (
    "chili.captured-paper-capture-runtime-config.v1"
)
_CAPTURED_PAPER_SETTINGS_PROJECTION_KEY = (
    "captured_paper_settings_projection_sha256"
)
_STATIC_BLOCKING_REASONS = (
    "iqfeed_l1_l2_host_process_launcher_uninstalled",
    "iqfeed_l2_initial_snapshot_completion_watermark_unavailable",
    "loaded_python_module_identity_not_attested_by_fresh_host_process",
    "downstream_gap_ledger_remainder_not_composed_into_hot_runs",
    "live_fsm_hot_admission_boundary_not_attached",
    "paper_live_recertification_pending",
)


class IqfeedIngressCompositionState(str, Enum):
    PREPARED = "prepared"
    INGRESS_RUNNING = "ingress_running"
    CLOSED = "closed"
    FAILED = "failed"


def _utc(value: datetime, field: str) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise CaptureContractError(f"{field} must be timezone-aware")
    return value.astimezone(UTC)


def _positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise CaptureContractError(f"{field} must be a positive integer")
    return int(value)


def _mapping(value: Any, field: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or not value:
        raise CaptureContractError(f"{field} is malformed")
    return value


def _sha256(value: Any, field: str) -> str:
    digest = str(value or "").strip().lower()
    if len(digest) != 64 or any(ch not in "0123456789abcdef" for ch in digest):
        raise CaptureContractError(f"{field} must be a lowercase SHA-256 digest")
    return digest


def captured_paper_base_capture_config(
    *,
    settings_projection_sha256: str,
    certification_symbol: str,
    resource_binding: CaptureResourceBinding,
    run_configuration: LiveCaptureRunConfiguration,
    capture_store_root: str | Path,
    additional_config: Mapping[str, Any] | None = None,
) -> Mapping[str, Any]:
    """Build the exact pre-IQFeed-generation PAPER capture configuration.

    The parsed settings projection is deliberately one hash-bound leaf, not
    the run config identity itself.  The generation wrapper below extends this
    value with the exact active L1/L2 process roster and re-hashes the complete
    object before a run can be admitted.
    """

    settings_sha = _sha256(
        settings_projection_sha256,
        "captured PAPER settings projection",
    )
    symbol = str(certification_symbol or "").strip().upper()
    if not symbol:
        raise CaptureContractError(
            "captured PAPER capture certification symbol is required"
        )
    if not isinstance(resource_binding, CaptureResourceBinding):
        raise CaptureContractError(
            "captured PAPER capture resource binding is malformed"
        )
    if not isinstance(run_configuration, LiveCaptureRunConfiguration):
        raise CaptureContractError(
            "captured PAPER live capture run configuration is malformed"
        )
    store_root = Path(capture_store_root)
    if not store_root.is_absolute():
        raise CaptureContractError(
            "captured PAPER capture-store root must be absolute"
        )
    store_root = store_root.resolve(strict=False)
    reserved = {
        "schema_version",
        _CAPTURED_PAPER_SETTINGS_PROJECTION_KEY,
        "capture_certification_symbol",
        "capture_resource_binding",
        "capture_resource_binding_sha256",
        "capture_store_root",
        "live_capture_run_configuration",
        "live_capture_run_configuration_sha256",
        *_GenerationBoundStartupInputProvider._RESERVED_CONFIG_KEYS,
    }
    extras = dict(additional_config or {})
    collisions = reserved.intersection(extras)
    if collisions:
        raise CaptureContractError(
            "captured PAPER additional config overwrote reserved provenance"
        )
    config = {
        "schema_version": _CAPTURED_PAPER_RUNTIME_CONFIG_SCHEMA_VERSION,
        _CAPTURED_PAPER_SETTINGS_PROJECTION_KEY: settings_sha,
        "capture_certification_symbol": symbol,
        "capture_resource_binding": resource_binding.to_record(),
        "capture_resource_binding_sha256": resource_binding.binding_sha256,
        "capture_store_root": str(store_root),
        "live_capture_run_configuration": run_configuration.to_dict(),
        "live_capture_run_configuration_sha256": (
            run_configuration.configuration_sha256
        ),
        **extras,
    }
    # Force canonical-JSON validation before this mapping crosses a provider
    # seam.  Downstream immediately binds these bytes into CaptureRunIdentity
    # and revalidates them before constructing a coordinator.
    sha256_json(config)
    return config


@dataclass(frozen=True)
class CapturedPaperStartupEvidence:
    """Already-verified service identity/account facts; never a fetch seam."""

    code_build: Mapping[str, Any]
    feature_flags: Mapping[str, Any]
    account_identity: Mapping[str, Any]
    account_risk_snapshot: Mapping[str, Any]
    account_query: Mapping[str, Any]
    account_provider: str
    settings_projection_sha256: str
    additional_config: Mapping[str, Any]
    activation_generation: int
    service_instance_id: str

    def __post_init__(self) -> None:
        for name in (
            "code_build",
            "feature_flags",
            "account_identity",
            "account_risk_snapshot",
            "account_query",
        ):
            value = getattr(self, name)
            if not isinstance(value, Mapping) or not value:
                raise CaptureContractError(
                    f"captured PAPER startup {name} is unavailable"
                )
            sha256_json(value)
        if str(self.account_identity.get("broker") or "").strip().lower() != "alpaca":
            raise CaptureContractError(
                "captured PAPER startup account broker is not Alpaca"
            )
        if str(self.account_identity.get("environment") or "").strip().lower() != "paper":
            raise CaptureContractError(
                "captured PAPER startup account environment is not paper"
            )
        if not str(self.account_identity.get("account_id") or "").strip():
            raise CaptureContractError(
                "captured PAPER startup account UUID is unavailable"
            )
        _sha256(self.settings_projection_sha256, "captured PAPER settings projection")
        if isinstance(self.activation_generation, bool) or int(self.activation_generation) <= 0:
            raise CaptureContractError(
                "captured PAPER activation generation must be positive"
            )
        try:
            instance = str(uuid.UUID(str(self.service_instance_id or "").strip()))
        except (ValueError, AttributeError) as exc:
            raise CaptureContractError(
                "captured PAPER service instance id must be a UUID"
            ) from exc
        if not str(self.account_provider or "").strip():
            raise CaptureContractError(
                "captured PAPER startup account provider is unavailable"
            )
        object.__setattr__(self, "service_instance_id", instance)
        object.__setattr__(self, "activation_generation", int(self.activation_generation))
        object.__setattr__(
            self,
            "additional_config",
            MappingProxyType(dict(self.additional_config or {})),
        )


class CapturedPaperLiveCaptureStartupInputProvider:
    """Concrete no-fetch base provider for ``install_hot_run_factory``.

    A single service-owned producer owns every local FSM read family, including
    exactly one scanner owner and one provider-OHLCV owner.  IQFeed L1/L2 are
    intentionally absent; ``_GenerationBoundStartupInputProvider`` appends the
    exact active external generations after this provider returns.
    """

    _LOCAL_STREAMS = tuple(
        sorted(
            {
                CaptureStream.CODE_BUILD,
                CaptureStream.CONFIG_SNAPSHOT,
                CaptureStream.FEATURE_FLAG_SNAPSHOT,
                CaptureStream.ACCOUNT_RISK_SNAPSHOT,
                CaptureStream.ALPACA_NBBO_QUOTE,
                CaptureStream.PROVIDER_OHLCV,
                CaptureStream.ORTEX_SNAPSHOT,
                CaptureStream.SCANNER_SNAPSHOT,
                CaptureStream.CATALYST_NEWS,
                CaptureStream.ADMISSION_ELIGIBILITY,
                CaptureStream.HALT_LULD_STATE,
                CaptureStream.SSR_STATE,
                CaptureStream.MARKET_SESSION_STATE,
                CaptureStream.BROKER_ORDER_LIFECYCLE,
                CaptureStream.FSM_DECISION,
            },
            key=lambda stream: stream.value,
        )
    )

    def __init__(
        self,
        evidence: CapturedPaperStartupEvidence,
        *,
        run_id_factory: Callable[[str], str] | None = None,
    ) -> None:
        if type(evidence) is not CapturedPaperStartupEvidence:
            raise CaptureContractError(
                "captured PAPER startup evidence is not typed"
            )
        if run_id_factory is not None and not callable(run_id_factory):
            raise CaptureContractError(
                "captured PAPER run-id factory is malformed"
            )
        self._evidence = evidence
        self._run_id_factory = run_id_factory or (lambda _symbol: str(uuid.uuid4()))

    def __call__(
        self,
        symbol: str,
        *,
        resource_binding: CaptureResourceBinding,
        run_configuration: LiveCaptureRunConfiguration,
        capture_store_root: Path,
    ) -> LiveCaptureRunInputs:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise CaptureContractError(
                "captured PAPER startup certification symbol is required"
            )
        evidence = self._evidence
        producer_roster = {
            "schema_version": "chili.captured-paper-local-producer-roster.v1",
            "producer_id": "captured_paper_service",
            "instance_id": evidence.service_instance_id,
            "generation": evidence.activation_generation,
            "streams": [stream.value for stream in self._LOCAL_STREAMS],
        }
        config = captured_paper_base_capture_config(
            settings_projection_sha256=evidence.settings_projection_sha256,
            certification_symbol=normalized,
            resource_binding=resource_binding,
            run_configuration=run_configuration,
            capture_store_root=capture_store_root,
            additional_config={
                **dict(evidence.additional_config),
                "captured_paper_local_producer_roster": producer_roster,
                "captured_paper_local_producer_roster_sha256": sha256_json(
                    producer_roster
                ),
            },
        )
        try:
            run_id = str(uuid.UUID(str(self._run_id_factory(normalized))))
        except (ValueError, AttributeError) as exc:
            raise CaptureContractError(
                "captured PAPER run id is not a UUID"
            ) from exc
        identity = CaptureRunIdentity(
            run_id=run_id,
            generation=evidence.activation_generation,
            code_build_sha256=sha256_json(evidence.code_build),
            config_sha256=sha256_json(config),
            feature_flags_sha256=sha256_json(evidence.feature_flags),
            account_identity_sha256=sha256_json(evidence.account_identity),
            broker="alpaca",
            broker_environment="paper",
        )
        identity_evidence = CaptureIdentityEvidence(
            code_build=dict(evidence.code_build),
            config=config,
            feature_flags=dict(evidence.feature_flags),
            account_identity=dict(evidence.account_identity),
            account_risk_snapshot=dict(evidence.account_risk_snapshot),
            account_query=dict(evidence.account_query),
            account_provider=str(evidence.account_provider).strip().lower(),
        )
        producer = CaptureProducerSpec(
            producer_id="captured_paper_service",
            instance_id=evidence.service_instance_id,
            generation=identity.generation,
            streams=self._LOCAL_STREAMS,
            code_build_sha256=identity.code_build_sha256,
            config_sha256=identity.config_sha256,
            feature_flags_sha256=identity.feature_flags_sha256,
            resource_binding_sha256=resource_binding.binding_sha256,
        )
        identity_evidence.validate_for(identity, certification_symbol=normalized)
        return LiveCaptureRunInputs(
            identity=identity,
            evidence=identity_evidence,
            producers=(producer,),
        )


def _reject_reparse_chain(path: Path) -> None:
    current = path
    while True:
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise CaptureContractError(
                f"bootstrap input disappeared during composition: {path}"
            ) from exc
        attributes = int(getattr(info, "st_file_attributes", 0) or 0)
        if stat.S_ISLNK(info.st_mode) or attributes & _REPARSE_ATTRIBUTE:
            raise CaptureContractError(
                f"bootstrap input traverses a reparse point: {path}"
            )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _stable_read(path: Path, expected_sha256: str) -> bytes:
    expected = str(expected_sha256 or "").strip().lower()
    if len(expected) != 64 or any(ch not in "0123456789abcdef" for ch in expected):
        raise CaptureContractError("bootstrap expected SHA-256 is malformed")
    resolved = Path(path)
    if not resolved.is_absolute():
        raise CaptureContractError("bootstrap rehash path must be absolute")
    _reject_reparse_chain(resolved)
    before = os.stat(resolved, follow_symlinks=False)
    if not stat.S_ISREG(before.st_mode) or before.st_size <= 0:
        raise CaptureContractError("bootstrap rehash target is not a regular file")
    if before.st_size > _MAX_REHASH_BYTES:
        raise CaptureContractError("bootstrap rehash target exceeds the bounded size")
    digest = hashlib.sha256()
    chunks: list[bytes] = []
    total = 0
    try:
        with resolved.open("rb") as handle:
            while True:
                chunk = handle.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_REHASH_BYTES:
                    raise CaptureContractError(
                        "bootstrap rehash target grew beyond the bounded size"
                    )
                digest.update(chunk)
                chunks.append(chunk)
    except OSError as exc:
        raise CaptureContractError("bootstrap input could not be rehashed") from exc
    after = os.stat(resolved, follow_symlinks=False)
    identity_before = (
        before.st_dev,
        before.st_ino,
        before.st_size,
        before.st_mtime_ns,
    )
    identity_after = (
        after.st_dev,
        after.st_ino,
        after.st_size,
        after.st_mtime_ns,
    )
    if identity_before != identity_after or total != after.st_size:
        raise CaptureContractError("bootstrap input changed while it was rehashed")
    actual = digest.hexdigest()
    if actual != expected:
        raise CaptureContractError("bootstrap input drifted after preflight")
    return b"".join(chunks)


def _strict_json(raw: bytes, field: str) -> Mapping[str, Any]:
    def _pairs(rows: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in rows:
            if key in value:
                raise CaptureContractError(f"{field} repeats JSON key {key}")
            value[key] = item
        return value

    def _constant(value: str) -> Any:
        raise CaptureContractError(f"{field} contains non-finite JSON {value}")

    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_pairs,
            parse_constant=_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CaptureContractError(f"{field} is not strict UTF-8 JSON") from exc
    if not isinstance(value, Mapping):
        raise CaptureContractError(f"{field} root is not an object")
    return value


def _same_path(left: Any, right: Path) -> bool:
    try:
        return Path(str(left or "")).resolve(strict=False) == Path(right).resolve(
            strict=False
        )
    except OSError:
        return False


def _parse_iso(value: Any, field: str) -> datetime:
    if not isinstance(value, str):
        raise CaptureContractError(f"{field} must be ISO-8601 text")
    try:
        return _utc(datetime.fromisoformat(value.replace("Z", "+00:00")), field)
    except ValueError as exc:
        raise CaptureContractError(f"{field} must be ISO-8601 text") from exc


def _reverify_preflight(
    preflight: IqfeedCaptureBootstrapPreflight,
) -> CaptureResourceBinding:
    manifest = _strict_json(
        _stable_read(Path(preflight.manifest_path), preflight.manifest_sha256),
        "bootstrap manifest",
    )
    startup = _strict_json(
        _stable_read(
            Path(preflight.startup_evidence_path),
            preflight.startup_evidence_sha256,
        ),
        "bootstrap startup evidence",
    )
    resource = _strict_json(
        _stable_read(
            Path(preflight.resource_benchmark_path),
            preflight.resource_benchmark_sha256,
        ),
        "bootstrap resource benchmark",
    )

    startup_ref = _mapping(manifest.get("startup_evidence"), "startup reference")
    resource_ref = _mapping(manifest.get("resource_benchmark"), "resource reference")
    if (
        not _same_path(startup_ref.get("path"), preflight.startup_evidence_path)
        or startup_ref.get("sha256") != preflight.startup_evidence_sha256
        or not _same_path(
            resource_ref.get("path"), preflight.resource_benchmark_path
        )
        or resource_ref.get("sha256") != preflight.resource_benchmark_sha256
        or not _same_path(manifest.get("capture_store_root"), preflight.capture_store_root)
    ):
        raise CaptureContractError("preflight object escaped its hash-bound manifest")
    if dict(manifest.get("run_configuration") or {}) != dict(
        preflight.run_configuration
    ):
        raise CaptureContractError("preflight run configuration drifted from manifest")
    manifest_handoff = _mapping(
        manifest.get("handoff_configuration"), "manifest handoff configuration"
    )
    if (
        manifest_handoff.get("schema_version")
        != preflight.handoff_configuration.get("schema_version")
        or dict(manifest_handoff.get("l1") or {})
        != dict(preflight.handoff_configuration.get("l1") or {})
        or dict(manifest_handoff.get("l2") or {})
        != dict(preflight.handoff_configuration.get("l2") or {})
    ):
        raise CaptureContractError("preflight handoff budget drifted from manifest")

    if (
        str(startup.get("process_instance_id") or "")
        != preflight.startup_process_instance_id
        or startup.get("generation") != preflight.startup_generation
        or startup.get("broker") != preflight.broker
        or startup.get("broker_environment") != preflight.broker_environment
        or dict(startup.get("bridge_configuration") or {})
        != dict(preflight.bridge_configuration)
    ):
        raise CaptureContractError("preflight startup identity drifted from evidence")
    startup_hash_fields = {
        "code_build_sha256": "code_build",
        "effective_config_sha256": "effective_config",
        "feature_flags_sha256": "feature_flags",
        "account_identity_sha256": "account_identity",
        "account_risk_snapshot_sha256": "account_risk_snapshot",
        "account_query_sha256": "account_query",
        "bridge_configuration_sha256": "bridge_configuration",
        "iqfeed_l1_clock_contract_sha256": "iqfeed_l1_clock_contract",
        "iqfeed_l2_clock_contract_sha256": "iqfeed_l2_clock_contract",
    }
    for hash_field, evidence_field in startup_hash_fields.items():
        if sha256_json(startup.get(evidence_field)) != preflight.startup_evidence_hashes.get(
            hash_field
        ):
            raise CaptureContractError(
                f"preflight {evidence_field} hash drifted from startup evidence"
            )

    code_build = _mapping(startup.get("code_build"), "startup code build")
    artifacts = code_build.get("artifacts")
    if not isinstance(artifacts, list):
        raise CaptureContractError("startup source roster is malformed")
    source_rows: dict[str, Mapping[str, Any]] = {}
    for row in artifacts:
        item = _mapping(row, "startup source row")
        role = str(item.get("role") or "").strip().lower()
        if not role or role in source_rows:
            raise CaptureContractError("startup source roster repeats a role")
        source_rows[role] = item
    if set(preflight.source_paths) != set(preflight.source_hashes):
        raise CaptureContractError("bootstrap source path/hash roster mismatch")
    if set(source_rows) != set(preflight.source_paths):
        raise CaptureContractError("bootstrap source roster drifted from startup evidence")
    for role in sorted(preflight.source_paths):
        row = source_rows[role]
        if (
            not _same_path(row.get("path"), Path(preflight.source_paths[role]))
            or row.get("sha256") != preflight.source_hashes[role]
        ):
            raise CaptureContractError(f"bootstrap source role {role} drifted")
        _stable_read(Path(preflight.source_paths[role]), preflight.source_hashes[role])

    resolved = _mapping(
        resource.get("resolved_resource_binding"), "resolved resource binding"
    )
    measurement_raw = dict(
        _mapping(resolved.get("measurement"), "resource measurement")
    )
    measurement_raw["measured_at"] = _parse_iso(
        measurement_raw.get("measured_at"), "resource measurement measured_at"
    )
    policy_raw = _mapping(resolved.get("policy"), "resource policy")
    measurement = CaptureResourceMeasurement(**measurement_raw)
    policy = CaptureBudgetPolicy(**dict(policy_raw))
    binding = CaptureResourceBinding.resolve(measurement, policy)
    expected_binding_sha256 = preflight.resource_binding.binding_sha256
    if (
        binding.binding_sha256 != expected_binding_sha256
        or resolved.get("binding_sha256") != expected_binding_sha256
        or resource_ref.get("binding_sha256") != expected_binding_sha256
    ):
        raise CaptureContractError(
            "rehydrated capture resource binding differs from verified preflight"
        )
    return binding


def _validated_handoff_budget(
    preflight: IqfeedCaptureBootstrapPreflight,
    binding: CaptureResourceBinding,
) -> Mapping[str, Any]:
    raw = _mapping(preflight.handoff_configuration, "handoff configuration")
    if raw.get("schema_version") != IQFEED_HANDOFF_BUDGET_SCHEMA_VERSION:
        raise CaptureContractError("handoff configuration schema mismatch")
    lanes: dict[str, dict[str, int]] = {}
    for name in ("l1", "l2"):
        lane = _mapping(raw.get(name), f"handoff {name}")
        lanes[name] = {
            field: _positive_int(lane.get(field), f"handoff {name} {field}")
            for field in (
                "max_pending_events",
                "max_pending_bytes",
                "max_gap_keys",
            )
        }
    aggregate = {
        field: lanes["l1"][field] + lanes["l2"][field]
        for field in lanes["l1"]
    }
    measured = {
        "max_pending_events": binding.budget.max_queue_events,
        "max_pending_bytes": binding.budget.async_queue_bytes,
        "max_gap_keys": binding.budget.max_gap_keys,
    }
    downstream = {
        field: measured[field] - aggregate[field]
        for field in measured
    }
    if min(downstream.values()) <= 0:
        raise CaptureContractError(
            "IQFeed handoffs leave no positive downstream resource budget"
        )
    if dict(raw.get("aggregate") or {}) != aggregate:
        raise CaptureContractError("handoff aggregate differs from lane allocations")
    if dict(raw.get("downstream_admission") or {}) != downstream:
        raise CaptureContractError(
            "handoff downstream budget differs from measured remainder"
        )
    return {
        "schema_version": IQFEED_HANDOFF_BUDGET_SCHEMA_VERSION,
        **lanes,
        "aggregate": aggregate,
        "downstream_admission": downstream,
    }


def _validate_pressure_start(
    sample: CapturePressureSample,
    *,
    binding: CaptureResourceBinding,
    now: datetime,
) -> None:
    if not isinstance(sample, CapturePressureSample):
        raise CaptureContractError("capture pressure sample is malformed")
    if sample.resource_binding_sha256 != binding.binding_sha256:
        raise CaptureContractError("capture pressure sample binding mismatch")
    observed_at = _utc(sample.observed_at, "pressure sample observed_at")
    age = (now - observed_at).total_seconds()
    if age < 0 or age > binding.policy.pressure_sample_max_age_seconds:
        raise CaptureContractError("capture pressure sample is stale or future-dated")
    policy = binding.policy
    reasons: list[str] = []
    if sample.cpu_percent >= policy.pressure_cpu_enter_percent:
        reasons.append("cpu")
    if sample.available_memory_bytes <= (
        policy.memory_reserve_bytes + policy.pressure_memory_enter_margin_bytes
    ):
        reasons.append("memory")
    if sample.disk_free_bytes <= (
        policy.disk_reserve_bytes + policy.pressure_disk_enter_margin_bytes
    ):
        reasons.append("disk")
    if (
        sample.write_latency_milliseconds
        >= policy.pressure_write_latency_enter_milliseconds
    ):
        reasons.append("write_latency")
    if reasons:
        raise CaptureContractError(
            "capture resources are already pressured at composition: "
            + ",".join(reasons)
        )


@dataclass(frozen=True)
class IqfeedExternalProducerGenerationRoster:
    """Exact active bridge processes observed while one run is packaged."""

    l1: CaptureExternalProducerGeneration
    l2: CaptureExternalProducerGeneration
    composition_sha256: str
    manifest_sha256: str
    startup_evidence_sha256: str
    schema_version: str = _GENERATION_ROSTER_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != _GENERATION_ROSTER_SCHEMA_VERSION:
            raise CaptureContractError("IQFeed generation roster schema is unsupported")
        if not isinstance(self.l1, CaptureExternalProducerGeneration) or not isinstance(
            self.l2, CaptureExternalProducerGeneration
        ):
            raise CaptureContractError("IQFeed generation roster is malformed")
        if self.l1.producer_id != "iqfeed_l1" or self.l2.producer_id != "iqfeed_l2":
            raise CaptureContractError("IQFeed generation roster producer IDs are malformed")
        if self.l1.provider != "iqfeed" or self.l2.provider != "iqfeed":
            raise CaptureContractError("IQFeed generation roster provider is malformed")
        expected_streams = {
            "iqfeed_l1": tuple(
                sorted(
                    (CaptureStream.IQFEED_PRINT, CaptureStream.NBBO_QUOTE),
                    key=lambda stream: stream.value,
                )
            ),
            "iqfeed_l2": tuple(
                sorted(
                    (CaptureStream.L2_DEPTH_CHECKPOINT, CaptureStream.L2_DEPTH_DELTA),
                    key=lambda stream: stream.value,
                )
            ),
        }
        if (
            self.l1.streams != expected_streams["iqfeed_l1"]
            or self.l2.streams != expected_streams["iqfeed_l2"]
        ):
            raise CaptureContractError("IQFeed generation roster stream ownership is malformed")
        for name in (
            "composition_sha256",
            "manifest_sha256",
            "startup_evidence_sha256",
        ):
            value = str(getattr(self, name) or "").strip().lower()
            if len(value) != 64 or any(ch not in "0123456789abcdef" for ch in value):
                raise CaptureContractError(f"IQFeed generation roster {name} is malformed")
            object.__setattr__(self, name, value)

    @property
    def generations(self) -> tuple[CaptureExternalProducerGeneration, ...]:
        return (self.l1, self.l2)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "composition_sha256": self.composition_sha256,
            "manifest_sha256": self.manifest_sha256,
            "startup_evidence_sha256": self.startup_evidence_sha256,
            "producers": [generation.to_dict() for generation in self.generations],
        }

    @property
    def roster_sha256(self) -> str:
        return sha256_json(self.to_dict())


class _DeferredHotRunFactory:
    """One-shot factory gate; installing it has no provider/store side effects."""

    def __init__(self) -> None:
        self._factory: Callable[..., Any] | None = None
        self._lock = threading.RLock()

    @property
    def installed(self) -> bool:
        with self._lock:
            return self._factory is not None

    def install(self, factory: Callable[..., Any]) -> None:
        if not callable(factory):
            raise CaptureContractError("IQFeed hot run factory is malformed")
        with self._lock:
            if self._factory is not None:
                raise CaptureContractError("IQFeed hot run factory is already installed")
            self._factory = factory

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        with self._lock:
            factory = self._factory
        if factory is None:
            raise CaptureContractError(
                "IQFeed hot admission unavailable: generation-bound run factory is not installed"
            )
        return factory(*args, **kwargs)


@dataclass(frozen=True)
class IqfeedIngressCompositionProvenance:
    manifest_sha256: str
    startup_evidence_sha256: str
    resource_benchmark_sha256: str
    resource_binding_sha256: str
    supervisor_identity_sha256: str
    l1_handoff_configuration_sha256: str
    l2_handoff_configuration_sha256: str
    source_hashes: Mapping[str, str]
    schema_version: str = _COMPOSITION_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_hashes",
            MappingProxyType(dict(sorted(self.source_hashes.items()))),
        )

    @property
    def composition_sha256(self) -> str:
        return sha256_json(
            {
                "schema_version": self.schema_version,
                "manifest_sha256": self.manifest_sha256,
                "startup_evidence_sha256": self.startup_evidence_sha256,
                "resource_benchmark_sha256": self.resource_benchmark_sha256,
                "resource_binding_sha256": self.resource_binding_sha256,
                "supervisor_identity_sha256": self.supervisor_identity_sha256,
                "l1_handoff_configuration_sha256": (
                    self.l1_handoff_configuration_sha256
                ),
                "l2_handoff_configuration_sha256": (
                    self.l2_handoff_configuration_sha256
                ),
                "source_hashes": dict(sorted(self.source_hashes.items())),
            }
        )


class _GenerationBoundStartupInputProvider:
    """Add the exact stable IQFeed process generations to a no-fetch run input."""

    _RESERVED_CONFIG_KEYS = frozenset(
        {
            "iqfeed_external_producer_generation_roster",
            "iqfeed_external_producer_generation_roster_sha256",
            "iqfeed_ingress_composition_sha256",
            "iqfeed_bootstrap_manifest_sha256",
            "iqfeed_bootstrap_startup_evidence_sha256",
        }
    )

    def __init__(
        self,
        *,
        composition: "IqfeedCaptureIngressComposition",
        base_provider: LiveCaptureStartupInputProvider,
        settings_projection_sha256: str | None = None,
    ) -> None:
        if not callable(base_provider):
            raise CaptureContractError("IQFeed base startup-input provider is malformed")
        self._composition = composition
        self._base_provider = base_provider
        self._settings_projection_sha256 = (
            None
            if settings_projection_sha256 is None
            else _sha256(
                settings_projection_sha256,
                "captured PAPER settings projection",
            )
        )

    @staticmethod
    def _validate_generation(
        generation: CaptureExternalProducerGeneration,
        *,
        producer_id: str,
        expected_streams: tuple[CaptureStream, ...],
        bridge_source_sha256: str,
        bridge_configuration_sha256: str,
        resource_binding_sha256: str,
        handoff_configuration_sha256: str,
    ) -> None:
        if not isinstance(generation, CaptureExternalProducerGeneration):
            raise CaptureContractError(
                f"active {producer_id} producer generation is malformed"
            )
        expected = tuple(sorted(expected_streams, key=lambda stream: stream.value))
        if (
            generation.producer_id != producer_id
            or generation.provider != "iqfeed"
            or generation.streams != expected
            or generation.bridge_source_sha256 != bridge_source_sha256
            or generation.bridge_configuration_sha256
            != bridge_configuration_sha256
            or generation.capture_resource_binding_sha256
            != resource_binding_sha256
            or generation.handoff_configuration_sha256
            != handoff_configuration_sha256
        ):
            raise CaptureContractError(
                f"active {producer_id} producer generation escaped preflight provenance"
            )

    def _snapshot(self) -> IqfeedExternalProducerGenerationRoster:
        composition = self._composition
        l1 = composition.l1_handoff.active_producer_generation()
        l2 = composition.l2_handoff.active_producer_generation()
        if l1 is None or l2 is None:
            missing = ",".join(
                name
                for name, value in (("iqfeed_l1", l1), ("iqfeed_l2", l2))
                if value is None
            )
            raise CaptureContractError(
                "active IQFeed producer generation roster is incomplete: " + missing
            )
        self._validate_generation(
            l1,
            producer_id="iqfeed_l1",
            expected_streams=(CaptureStream.IQFEED_PRINT, CaptureStream.NBBO_QUOTE),
            bridge_source_sha256=composition.preflight.source_hashes[
                "iqfeed_trade_bridge"
            ],
            bridge_configuration_sha256=(
                composition.l1_handoff.bridge_configuration_sha256
            ),
            resource_binding_sha256=composition.binding.binding_sha256,
            handoff_configuration_sha256=(
                composition.l1_handoff.handoff_configuration_sha256
            ),
        )
        self._validate_generation(
            l2,
            producer_id="iqfeed_l2",
            expected_streams=(
                CaptureStream.L2_DEPTH_CHECKPOINT,
                CaptureStream.L2_DEPTH_DELTA,
            ),
            bridge_source_sha256=composition.preflight.source_hashes[
                "iqfeed_depth_bridge"
            ],
            bridge_configuration_sha256=(
                composition.l2_handoff.bridge_configuration_sha256
            ),
            resource_binding_sha256=composition.binding.binding_sha256,
            handoff_configuration_sha256=(
                composition.l2_handoff.handoff_configuration_sha256
            ),
        )
        return IqfeedExternalProducerGenerationRoster(
            l1=l1,
            l2=l2,
            composition_sha256=composition.provenance.composition_sha256,
            manifest_sha256=composition.preflight.manifest_sha256,
            startup_evidence_sha256=composition.preflight.startup_evidence_sha256,
        )

    def __call__(
        self,
        symbol: str,
        *,
        resource_binding: CaptureResourceBinding,
        run_configuration: LiveCaptureRunConfiguration,
        capture_store_root: Path,
    ) -> LiveCaptureRunInputs:
        normalized = str(symbol or "").strip().upper()
        if not normalized:
            raise CaptureContractError("IQFeed run certification symbol is required")
        composition = self._composition
        if resource_binding != composition.binding:
            raise CaptureContractError("IQFeed run resource binding mismatch")
        if run_configuration != composition.run_configuration:
            raise CaptureContractError("IQFeed run configuration mismatch")
        supplied_root = Path(capture_store_root)
        expected_root = Path(composition.preflight.capture_store_root)
        if (
            not supplied_root.is_absolute()
            or supplied_root.resolve() != expected_root.resolve()
        ):
            raise CaptureContractError("IQFeed run capture-store root mismatch")

        before = self._snapshot()
        inputs = self._base_provider(
            normalized,
            resource_binding=resource_binding,
            run_configuration=run_configuration,
            capture_store_root=supplied_root,
        )
        if not isinstance(inputs, LiveCaptureRunInputs):
            raise CaptureContractError(
                "IQFeed base startup-input provider returned malformed inputs"
            )
        inputs.evidence.validate_for(inputs.identity, certification_symbol=normalized)
        if self._settings_projection_sha256 is not None:
            base_config = inputs.evidence.config
            if (
                base_config.get("schema_version")
                != _CAPTURED_PAPER_RUNTIME_CONFIG_SCHEMA_VERSION
                or base_config.get(_CAPTURED_PAPER_SETTINGS_PROJECTION_KEY)
                != self._settings_projection_sha256
                or base_config.get("capture_resource_binding")
                != resource_binding.to_record()
                or base_config.get("capture_resource_binding_sha256")
                != resource_binding.binding_sha256
                or base_config.get("live_capture_run_configuration")
                != run_configuration.to_dict()
                or base_config.get("live_capture_run_configuration_sha256")
                != run_configuration.configuration_sha256
                or str(base_config.get("capture_store_root") or "")
                != str(supplied_root.resolve())
            ):
                raise CaptureContractError(
                    "captured PAPER base capture config escaped settings/run/resource/store provenance"
                )
        external_ids = {generation.producer_id for generation in before.generations}
        external_streams = {
            stream for generation in before.generations for stream in generation.streams
        }
        for producer in inputs.producers:
            if producer.producer_id in external_ids or external_streams.intersection(
                producer.streams
            ):
                raise CaptureContractError(
                    "base startup producer roster conflicts with IQFeed ownership"
                )
            if (
                producer.code_build_sha256 != inputs.identity.code_build_sha256
                or producer.config_sha256 != inputs.identity.config_sha256
                or producer.feature_flags_sha256
                != inputs.identity.feature_flags_sha256
                or producer.resource_binding_sha256 != resource_binding.binding_sha256
            ):
                raise CaptureContractError(
                    "base startup producer escaped its run/resource identity"
                )
        scanner_owners = tuple(
            producer.producer_id
            for producer in inputs.producers
            if CaptureStream.SCANNER_SNAPSHOT in producer.streams
        )
        if len(scanner_owners) != 1:
            raise CaptureContractError(
                "base startup roster must declare one scanner snapshot capture owner"
            )
        ohlcv_owners = tuple(
            producer.producer_id
            for producer in inputs.producers
            if CaptureStream.PROVIDER_OHLCV in producer.streams
        )
        if len(ohlcv_owners) != 1:
            raise CaptureContractError(
                "base startup roster must declare one provider OHLCV capture owner"
            )

        config = dict(inputs.evidence.config)
        collision = self._RESERVED_CONFIG_KEYS.intersection(config)
        if collision:
            raise CaptureContractError(
                "base startup config pre-populated reserved IQFeed provenance"
            )
        roster_document = before.to_dict()
        config.update(
            {
                "iqfeed_external_producer_generation_roster": roster_document,
                "iqfeed_external_producer_generation_roster_sha256": (
                    before.roster_sha256
                ),
                "iqfeed_ingress_composition_sha256": (
                    composition.provenance.composition_sha256
                ),
                "iqfeed_bootstrap_manifest_sha256": (
                    composition.preflight.manifest_sha256
                ),
                "iqfeed_bootstrap_startup_evidence_sha256": (
                    composition.preflight.startup_evidence_sha256
                ),
            }
        )
        config_sha256 = sha256_json(config)
        identity = replace(inputs.identity, config_sha256=config_sha256)
        evidence = replace(inputs.evidence, config=config)
        base_producers = tuple(
            replace(producer, config_sha256=config_sha256)
            for producer in inputs.producers
        )
        external_producers = tuple(
            CaptureProducerSpec(
                producer_id=generation.producer_id,
                instance_id=generation.provider_instance_id,
                generation=generation.provider_generation,
                streams=generation.streams,
                code_build_sha256=identity.code_build_sha256,
                config_sha256=identity.config_sha256,
                feature_flags_sha256=identity.feature_flags_sha256,
                resource_binding_sha256=resource_binding.binding_sha256,
            )
            for generation in before.generations
        )
        after = self._snapshot()
        if after.roster_sha256 != before.roster_sha256:
            raise CaptureContractError(
                "active IQFeed producer generation changed while packaging the run"
            )
        evidence.validate_for(identity, certification_symbol=normalized)
        return LiveCaptureRunInputs(
            identity=identity,
            evidence=evidence,
            producers=(*base_producers, *external_producers),
        )


class _GenerationBoundHotRunFactory:
    """Recheck bridge generations after the shared-store run is constructed."""

    def __init__(
        self,
        *,
        delegate: SharedStoreLiveCaptureRunFactory,
        startup_input_provider: _GenerationBoundStartupInputProvider,
    ) -> None:
        if not isinstance(delegate, SharedStoreLiveCaptureRunFactory):
            raise CaptureContractError("IQFeed shared-store run factory is malformed")
        self._delegate = delegate
        self._startup_input_provider = startup_input_provider

    @staticmethod
    def _discard_unstarted(coordinator: Any, *, reason: str) -> None:
        try:
            coordinator.discard_unstarted(reason=reason)
        except BaseException as exc:
            raise CaptureContractError(
                "IQFeed generation-bound run cleanup failed closed"
            ) from exc

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        coordinator, evidence = self._delegate(*args, **kwargs)
        expected = str(
            evidence.config.get(
                "iqfeed_external_producer_generation_roster_sha256"
            )
            or ""
        ).strip().lower()
        try:
            current = self._startup_input_provider._snapshot()
        except CaptureContractError as exc:
            self._discard_unstarted(
                coordinator,
                reason="iqfeed_generation_unavailable_after_run_construction",
            )
            raise CaptureContractError(
                "active IQFeed producer generation became unavailable during run construction"
            ) from exc
        if current.roster_sha256 != expected:
            self._discard_unstarted(
                coordinator,
                reason="iqfeed_generation_changed_after_run_construction",
            )
            raise CaptureContractError(
                "active IQFeed producer generation changed during run construction"
            )
        return coordinator, evidence


class IqfeedCaptureIngressComposition:
    """One-shot local queue graph; provider and hot-run activation stay blocked."""

    def __init__(
        self,
        *,
        preflight: IqfeedCaptureBootstrapPreflight,
        binding: CaptureResourceBinding,
        run_configuration: LiveCaptureRunConfiguration,
        handoff_budget: Mapping[str, Any],
        pressure_controller: CaptureAdaptivePressureController,
        shared_admission_budget: SharedCaptureAdmissionBudget,
        supervisor: LiveReplayCaptureSupervisor,
        service: LiveReplayCaptureProcessService,
        run_factory_gate: _DeferredHotRunFactory,
        l1_handoff: BoundedIqfeedL1CaptureHandoff,
        l2_handoff: BoundedIqfeedL2CaptureHandoff,
        provenance: IqfeedIngressCompositionProvenance,
    ) -> None:
        self.preflight = preflight
        self.binding = binding
        self.run_configuration = run_configuration
        try:
            self._handoff_budget_json = json.dumps(
                handoff_budget,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (TypeError, ValueError) as exc:
            raise CaptureContractError("IQFeed handoff budget is not canonical JSON") from exc
        self.pressure_controller = pressure_controller
        self.shared_admission_budget = shared_admission_budget
        self.supervisor = supervisor
        self.service = service
        self._run_factory_gate = run_factory_gate
        self.l1_handoff = l1_handoff
        self.l2_handoff = l2_handoff
        self.provenance = provenance
        self._generation_bound_startup_provider: (
            _GenerationBoundStartupInputProvider | None
        ) = None
        self._state = IqfeedIngressCompositionState.PREPARED
        self._lock = threading.RLock()

    @property
    def state(self) -> IqfeedIngressCompositionState:
        with self._lock:
            return self._state

    @property
    def handoff_budget(self) -> Mapping[str, Any]:
        """Return an isolated copy so callers cannot mutate audit provenance."""

        return json.loads(self._handoff_budget_json)

    def install_hot_run_factory(
        self,
        *,
        shared_store_runtime: SharedCaptureStoreRuntime,
        startup_input_provider: LiveCaptureStartupInputProvider,
        settings_projection_sha256: str | None = None,
    ) -> None:
        """Install a no-fetch run factory without creating a store or provider.

        The caller owns creation/authorization of the supplied shared runtime.
        This method only verifies that it is the exact preflight-bound runtime
        and wraps the supplied already-observed startup facts with a stable
        external-producer generation roster.
        """

        with self._lock:
            if self._state in {
                IqfeedIngressCompositionState.CLOSED,
                IqfeedIngressCompositionState.FAILED,
            }:
                raise CaptureContractError(
                    "cannot install IQFeed hot run factory on terminal composition"
                )
            if not isinstance(shared_store_runtime, SharedCaptureStoreRuntime):
                raise CaptureContractError("IQFeed shared capture runtime is malformed")
            if shared_store_runtime.resource_binding != self.binding:
                raise CaptureContractError(
                    "IQFeed shared capture runtime resource binding mismatch"
                )
            if (
                shared_store_runtime.shared_admission_budget
                is not self.shared_admission_budget
            ):
                raise CaptureContractError(
                    "IQFeed shared capture runtime admission budget mismatch"
                )
            store_root = Path(shared_store_runtime.store.root)
            expected_root = Path(self.preflight.capture_store_root)
            if store_root.resolve() != expected_root.resolve():
                raise CaptureContractError(
                    "IQFeed shared capture runtime store root mismatch"
                )
            wrapped = _GenerationBoundStartupInputProvider(
                composition=self,
                base_provider=startup_input_provider,
                settings_projection_sha256=settings_projection_sha256,
            )
            shared_store_factory = SharedStoreLiveCaptureRunFactory(
                shared_store_runtime=shared_store_runtime,
                run_configuration=self.run_configuration,
                startup_input_provider=wrapped,
            )
            factory = _GenerationBoundHotRunFactory(
                delegate=shared_store_factory,
                startup_input_provider=wrapped,
            )
            self._run_factory_gate.install(factory)
            self._generation_bound_startup_provider = wrapped

    def start_ingress(self) -> None:
        """Start local drains only; no socket, store, DB, broker, or task mutation."""

        with self._lock:
            if self._state is not IqfeedIngressCompositionState.PREPARED:
                raise CaptureContractError("IQFeed ingress composition is one-shot")
            try:
                self.l1_handoff.start()
                self.l2_handoff.start()
            except BaseException as exc:
                rollback_failures: list[BaseException] = []
                for handoff in (self.l2_handoff, self.l1_handoff):
                    try:
                        if handoff.health()["started"]:
                            handoff.close()
                    except BaseException as rollback_exc:
                        rollback_failures.append(rollback_exc)
                self._state = IqfeedIngressCompositionState.FAILED
                detail = (
                    " with rollback failure"
                    if rollback_failures
                    else ""
                )
                raise CaptureContractError(
                    "IQFeed ingress composition failed to start atomically" + detail
                ) from exc
            self._state = IqfeedIngressCompositionState.INGRESS_RUNNING

    def close(self) -> Mapping[str, Any]:
        with self._lock:
            if self._state is IqfeedIngressCompositionState.CLOSED:
                return self.health()
            if self._state is IqfeedIngressCompositionState.PREPARED:
                self._state = IqfeedIngressCompositionState.CLOSED
                return self.health()
            if self._state is IqfeedIngressCompositionState.FAILED:
                raise CaptureContractError("failed IQFeed composition cannot be cleanly closed")
            service_health = self.service.health()
            if service_health["pending_symbols"] or service_health["running_symbols"]:
                raise CaptureContractError(
                    "cannot close IQFeed ingress composition with active capture runs"
                )
            failures: list[BaseException] = []
            for handoff in (self.l2_handoff, self.l1_handoff):
                try:
                    handoff.close()
                except BaseException as exc:
                    failures.append(exc)
            if failures:
                self._state = IqfeedIngressCompositionState.FAILED
                raise CaptureContractError(
                    "IQFeed ingress composition closed with unpersisted loss"
                ) from failures[0]
            self._state = IqfeedIngressCompositionState.CLOSED
            return self.health()

    def health(self) -> Mapping[str, Any]:
        with self._lock:
            roster: IqfeedExternalProducerGenerationRoster | None = None
            roster_status = "factory_uninstalled"
            if self._generation_bound_startup_provider is not None:
                try:
                    roster = self._generation_bound_startup_provider._snapshot()
                except CaptureContractError:
                    roster_status = "unavailable_or_invalid"
                else:
                    roster_status = "valid"
            generation_bound_run_factory_ready = bool(
                self._state is IqfeedIngressCompositionState.INGRESS_RUNNING
                and self._run_factory_gate.installed
                and roster is not None
            )
            blocking_reasons = list(_STATIC_BLOCKING_REASONS)
            if not self._run_factory_gate.installed:
                blocking_reasons.insert(
                    0, "iqfeed_generation_bound_hot_run_factory_uninstalled"
                )
            elif roster is None:
                blocking_reasons.insert(
                    0, "iqfeed_active_external_producer_generation_roster_unavailable"
                )
            return {
                "schema_version": _COMPOSITION_SCHEMA_VERSION,
                "state": self._state.value,
                "activation_authorized": False,
                "provider_socket_started": False,
                "database_or_broker_started": False,
                # Host/task wiring, initial-L2 completion authority, and
                # recertification remain open even when the local run factory
                # is fully testable.
                "hot_admission_available": False,
                "generation_bound_run_factory_ready": (
                    generation_bound_run_factory_ready
                ),
                "hot_run_factory_installed": self._run_factory_gate.installed,
                "external_producer_generation_roster_status": roster_status,
                "external_producer_generation_roster_sha256": (
                    None if roster is None else roster.roster_sha256
                ),
                "network_fallback_allowed": False,
                "blocking_reasons": tuple(blocking_reasons),
                "composition_sha256": self.provenance.composition_sha256,
                "resource_binding_sha256": self.binding.binding_sha256,
                "handoff_budget": self.handoff_budget,
                "pressure": self.pressure_controller.health(),
                "shared_admission": self.shared_admission_budget.health(),
                "service": self.service.health(),
                "l1_handoff": self.l1_handoff.health(),
                "l2_handoff": self.l2_handoff.health(),
            }


def prepare_iqfeed_capture_ingress(
    preflight: IqfeedCaptureBootstrapPreflight,
    *,
    pressure_sample: CapturePressureSample,
    wall_clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    monotonic_clock: Callable[[], float] = time.monotonic,
) -> IqfeedCaptureIngressComposition:
    """Reverify immutable inputs and build one inert bounded ingress graph."""

    if not isinstance(preflight, IqfeedCaptureBootstrapPreflight):
        raise CaptureContractError("IQFeed bootstrap preflight is malformed")
    if not callable(wall_clock) or not callable(monotonic_clock):
        raise CaptureContractError("IQFeed bootstrap clocks are malformed")
    reverify_started_at = _utc(wall_clock(), "IQFeed composition wall clock")
    binding = _reverify_preflight(preflight)
    now = _utc(wall_clock(), "IQFeed composition wall clock")
    if now < reverify_started_at:
        raise CaptureContractError("IQFeed composition wall clock moved backwards")
    handoff_budget = _validated_handoff_budget(preflight, binding)
    run_configuration = LiveCaptureRunConfiguration(**dict(preflight.run_configuration))
    downstream = handoff_budget["downstream_admission"]
    if (
        run_configuration.writer_batch_events > downstream["max_pending_events"]
        or run_configuration.writer_batch_bytes > downstream["max_pending_bytes"]
    ):
        raise CaptureContractError(
            "live capture writer batch exceeds the handoff-reserved remainder"
        )
    _validate_pressure_start(pressure_sample, binding=binding, now=now)
    pressure_controller = CaptureAdaptivePressureController(
        binding, monotonic_clock=monotonic_clock
    )
    pressure_controller.observe(pressure_sample)
    if not pressure_controller.required_full_fidelity_admissible:
        raise CaptureContractError("capture pressure controller failed closed at start")

    identity = CaptureRunIdentity(
        run_id=preflight.startup_process_instance_id,
        generation=preflight.startup_generation,
        code_build_sha256=preflight.startup_evidence_hashes["code_build_sha256"],
        config_sha256=preflight.startup_evidence_hashes["effective_config_sha256"],
        feature_flags_sha256=preflight.startup_evidence_hashes[
            "feature_flags_sha256"
        ],
        account_identity_sha256=preflight.startup_evidence_hashes[
            "account_identity_sha256"
        ],
        broker=preflight.broker,
        broker_environment=preflight.broker_environment,
    )
    shared_admission = SharedCaptureAdmissionBudget(
        resource_binding=binding,
        max_events=downstream["max_pending_events"],
        max_bytes=downstream["max_pending_bytes"],
        sustained_write_budget_bytes_per_second=(
            binding.budget.sustained_write_budget_bytes_per_second
        ),
        pressure_controller=pressure_controller,
        monotonic_clock=monotonic_clock,
    )
    supervisor = LiveReplayCaptureSupervisor.create(
        identity=identity,
        resource_binding=binding,
        pressure_controller=pressure_controller,
        wall_clock=wall_clock,
        pretrigger_horizon=timedelta(
            seconds=run_configuration.pretrigger_horizon_seconds
        ),
        per_symbol_pretrigger_events=run_configuration.per_symbol_pretrigger_events,
        shared_admission_budget=shared_admission,
    )
    run_factory_gate = _DeferredHotRunFactory()
    service = LiveReplayCaptureProcessService(
        supervisor=supervisor,
        run_factory=run_factory_gate,
    )
    bridge_configuration = _mapping(
        preflight.bridge_configuration, "IQFeed bridge configuration"
    )
    l1_bridge_configuration = _mapping(
        bridge_configuration.get("iqfeed_l1"), "IQFeed L1 bridge configuration"
    )
    l2_bridge_configuration = _mapping(
        bridge_configuration.get("iqfeed_l2"), "IQFeed L2 bridge configuration"
    )
    l1_lane = handoff_budget["l1"]
    l2_lane = handoff_budget["l2"]
    l1_handoff = BoundedIqfeedL1CaptureHandoff(
        sink=IqfeedL1ProcessCaptureSink(service),
        max_pending_events=l1_lane["max_pending_events"],
        max_pending_bytes=l1_lane["max_pending_bytes"],
        max_gap_keys=l1_lane["max_gap_keys"],
        bridge_source_sha256=preflight.source_hashes["iqfeed_trade_bridge"],
        bridge_configuration=l1_bridge_configuration,
        bridge_configuration_sha256=sha256_json(l1_bridge_configuration),
    )
    l2_handoff = BoundedIqfeedL2CaptureHandoff(
        sink=IqfeedL2ProcessCaptureSink(service),
        max_pending_events=l2_lane["max_pending_events"],
        max_pending_bytes=l2_lane["max_pending_bytes"],
        max_gap_keys=l2_lane["max_gap_keys"],
        bridge_source_sha256=preflight.source_hashes["iqfeed_depth_bridge"],
        bridge_configuration=l2_bridge_configuration,
        bridge_configuration_sha256=sha256_json(l2_bridge_configuration),
    )
    provenance = IqfeedIngressCompositionProvenance(
        manifest_sha256=preflight.manifest_sha256,
        startup_evidence_sha256=preflight.startup_evidence_sha256,
        resource_benchmark_sha256=preflight.resource_benchmark_sha256,
        resource_binding_sha256=binding.binding_sha256,
        supervisor_identity_sha256=identity.identity_sha256,
        l1_handoff_configuration_sha256=l1_handoff.handoff_configuration_sha256,
        l2_handoff_configuration_sha256=l2_handoff.handoff_configuration_sha256,
        source_hashes=dict(preflight.source_hashes),
    )
    return IqfeedCaptureIngressComposition(
        preflight=preflight,
        binding=binding,
        run_configuration=run_configuration,
        handoff_budget=handoff_budget,
        pressure_controller=pressure_controller,
        shared_admission_budget=shared_admission,
        supervisor=supervisor,
        service=service,
        run_factory_gate=run_factory_gate,
        l1_handoff=l1_handoff,
        l2_handoff=l2_handoff,
        provenance=provenance,
    )


__all__ = [
    "CapturedPaperLiveCaptureStartupInputProvider",
    "CapturedPaperStartupEvidence",
    "IqfeedCaptureIngressComposition",
    "IqfeedExternalProducerGenerationRoster",
    "IqfeedIngressCompositionProvenance",
    "IqfeedIngressCompositionState",
    "captured_paper_base_capture_config",
    "prepare_iqfeed_capture_ingress",
]
